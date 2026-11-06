"""TeslaMate-backed output implementation.

This module provides a read-only "Output" variant which derives energy usage from
TeslaMate charging sessions/buckets.

Key goals:
- Keep raw TeslaMate imports in the controller state under ``TeslaChargeData``.
- Derive a realized Output-like record (including RunHistory-style daily grouping)
  from that raw data so it appears alongside other outputs in the saved state
  and CSV consumption exports.
- Split any charging activity that spans midnight into per-day entries.

This output does not participate in run planning or smart device actuation.
"""

from __future__ import annotations

import datetime as dt
import operator
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from org_enums import AppMode, RunPlanMode, StateReasonOff, StateReasonOn, SystemState
from sc_foundation import DateHelper, SCConfigManager, SCLogger

from helpers import get_currency_symbols, resolve_current_price
from local_enumerations import DEFAULT_PRICE, AmberChannel

if TYPE_CHECKING:
    from pricing import PricingManager
    from scheduler import Scheduler


def _as_local_dt(value: Any) -> dt.datetime | None:
    """Best-effort conversion of persisted values into a local tz-aware datetime.

    Args:
        value: A value read from ``TeslaChargeData``.

    Returns:
        A tz-aware datetime in local timezone, or None if conversion fails.
    """
    if isinstance(value, dt.datetime):
        return DateHelper.convert_timezone(value)

    if isinstance(value, str):
        # Accept both "2025-12-31T12:34:56" and "2025-12-31 12:34:56".
        parsed = DateHelper.extract(value, "ISO")
        if not parsed:
            parsed = DateHelper.extract(value, "%Y-%m-%d %H:%M:%S")
        if not parsed:
            return None
        assert isinstance(parsed, dt.datetime)
        if parsed.tzinfo is None:
            parsed = DateHelper.add_timezone(parsed)
        return DateHelper.convert_timezone(parsed)

    return None


def calc_energy_cost(kwh_used: float, price: float) -> float:
    """Calculate the cost in $ given energy used in Wh and price in c/kWh.

    Args:
        kwh_used (float): The energy used in kWh.
        price (float): The price in c/kWh.

    Returns:
        float: Total cost in $.
    """
    return kwh_used * (price / 100) if kwh_used > 0 else 0


@dataclass
class TeslaDailyTotals:
    """Computed daily totals for Tesla charging."""

    date: dt.date
    energy_wh: float
    total_cost: float
    actual_hours: float


class TeslaMateOutput:
    """Read-only output backed by TeslaMate charging data.

    The output builds RunHistory-style daily data from the controller's
    ``TeslaChargeData`` section. This output does not generate a run plan and never posts smart device actions.
    """

    def __init__(
        self,
        output_config: dict[str, Any],
        config: SCConfigManager,
        logger: SCLogger,
        scheduler: Scheduler,
        pricing: PricingManager,
        tesla_charge_data: dict[str, Any],
        saved_state: dict[str, Any] | None = None,
    ):
        """Create a TeslaMate-backed output.

        Args:
            output_config: Output config entry (must include Name, Type=teslamate).
            config: Global config manager.
            logger: Logger.
            pricing: PricingManager.
            scheduler (Scheduler): The scheduler for managing time-based operations.
            tesla_charge_data: Controller-owned TeslaChargeData dict.
            saved_state: Previously saved output state object, if present.
        """
        self.output_config = output_config
        self.config = config
        self.logger = logger
        self._tesla_charge_data = tesla_charge_data

        self.name: str = output_config.get("Name") or "Tesla"
        self.id: str = urllib.parse.quote(self.name.lower().replace(" ", "_"))

        self.type: str = "teslamate"
        self.car_id: int | None = None

        # pricing and scheduling
        self.pricing = pricing
        self.amber_channel = AmberChannel.GENERAL
        self.scheduler = scheduler
        self.schedule_name = None
        self.schedule = None
        self.device_mode: RunPlanMode = RunPlanMode.BEST_PRICE
        self.default_price: float = self.config.get("General", "DefaultPrice", default=DEFAULT_PRICE) or DEFAULT_PRICE  # pyright: ignore[reportAttributeAccessIssue]
        self.currency_major_symbol, self.currency_minor_symbol = get_currency_symbols(config)

        self.system_state: SystemState = SystemState.EXTERNAL_CONTROL
        self.app_mode: AppMode = AppMode.AUTO

        self.last_changed: dt.datetime | None = None
        self.reason: str | None = None

        # OutputManager compatibility fields (TeslaMate outputs are never parent/child).
        self.parent_output_name: str | None = None
        self.parent_output: Any | None = None
        self.is_parent: bool = False

        saved_history = saved_state.get("RunHistory") if isinstance(saved_state, dict) else None
        self.run_history: dict[str, Any] = saved_history if isinstance(saved_history, dict) else {}
        if not self.run_history:
            self.run_history = self._create_empty_history()

        self._last_rebuild: dt.datetime | None = None
        self.initialise(output_config, None, saved_state)
        self.logger.log_message(f"Output {self.name} initialised.", "debug")

    def set_parent_output(self, parent: Any) -> None:
        """Set the parent output reference.

        TeslaMate outputs do not participate in parent/child relationships, but the
        controller initialization links outputs generically.

        Args:
            parent: Parent output object.
        """
        self.parent_output = parent

    # --- Compatibility methods used by PowerController loops ---
    def initialise(self, output_config: dict[str, Any], _view: Any, _saved_state: Any) -> None:
        """Reinitialise this output from config.

        Args:
            output_config: Output configuration dict.
            _view: Unused for TeslaMate outputs.

        Raises:
            RuntimeError: If the configuration is invalid.
        """
        def _validation_error(msg):
            raise RuntimeError(msg)

        self.output_config = output_config

        try:
            # Name
            self.name = output_config.get("Name") or "Tesla"
            if not self.name:
                _validation_error("Name is not set for an Output configuration.")
            else:
                # self.id is url encoded version of name
                self.id = urllib.parse.quote(self.name.lower().replace(" ", "_"))

            # Pricing mode: reuse existing Mode values (BestPrice=Amber, Schedule=Schedule pricing)
            mode = output_config.get("Mode") or RunPlanMode.BEST_PRICE
            self.device_mode = mode
            if self.device_mode not in RunPlanMode:
                _validation_error(f"A valid Mode has not been set for meter output {self.name}.")
            self.currency_major_symbol, self.currency_minor_symbol = get_currency_symbols(self.config)

            # Schedule (required if using schedule pricing)
            self.schedule_name = output_config.get("Schedule")
            if self.device_mode == RunPlanMode.SCHEDULE:
                if not self.schedule_name:
                    _validation_error(f"Schedule is required for meter output {self.name} when Mode is Schedule.")
                else:
                    self.schedule = self.scheduler.get_schedule_by_name(self.schedule_name)
                    if not self.schedule:
                        _validation_error(f"Schedule {self.schedule_name} for meter output {self.name} not found in OperatingSchedules.")
            else:
                # Optional schedule for fallback pricing
                self.schedule = self.scheduler.get_schedule_by_name(self.schedule_name) if self.schedule_name else None

            # Amber Channel
            self.amber_channel = output_config.get("AmberChannel", AmberChannel.GENERAL) or AmberChannel.GENERAL
            if self.amber_channel not in AmberChannel:
                _validation_error(f"Invalid AmberChannel {self.amber_channel} for output {self.name}. Must be one of {', '.join([m.value for m in AmberChannel])}.")

            # CarID
            car_raw = output_config.get("CarID")
            if car_raw is not None and car_raw:
                try:
                    self.car_id = int(car_raw)
                except (TypeError, ValueError):
                    self.car_id = None

        except RuntimeError:
            raise
        except (KeyError, IndexError) as e:
            raise RuntimeError(str(e)) from e
        else:
            self.calculate_running_totals(_view)   # Finally calculate all running totals

    @staticmethod
    def tell_device_status_updated(_view: Any) -> None:
        """No-op for TeslaMate outputs."""
        return

    def calculate_running_totals(self, view: Any, is_new_day: bool = False) -> None:  # noqa: ARG002
        """Rebuild daily run history from TeslaChargeData."""
        _ = view
        self._rebuild_history_from_charge_data()

    @staticmethod
    def review_run_plan(_view: Any) -> None:
        """No-op for TeslaMate outputs."""
        return

    def evaluate_conditions(self, **_kwargs: Any) -> None:
        """No-op for TeslaMate outputs."""

    def get_action_request(self) -> None:
        """TeslaMate outputs never request actions."""

    def get_schedule(self) -> dict | None:
        """Get the schedule for this output.

        Returns:
            dict: The schedule or None if none assigned.
        """
        return self.schedule

    @staticmethod
    def shutdown(_view: Any) -> bool:
        """TeslaMate outputs have nothing to shut down.

        Returns:
            False always.
        """
        return False

    def set_app_mode(self, new_mode: AppMode, view: Any, revert_minutes: Any = None) -> None:
        """No-op (read-only output)."""
        _ = (view, revert_minutes)
        self.app_mode = new_mode

    def run_self_tests(self, _is_new_day: Any) -> None:
        """Run self tests on the output manager."""
        print(f"Running self tests for output {self.name}:")

        time_now = DateHelper.now()

        as_at_time = DateHelper.add_datetime(time_now, hours=-12)
        print(f"  - Price at {as_at_time.isoformat()}: {self._get_price(as_at_time):.2f} {self.currency_minor_symbol}/kWh")

        as_at_time = DateHelper.add_datetime(time_now, days=4)
        print(f"  - Price at {as_at_time.isoformat()}: {self._get_price(as_at_time):.2f} {self.currency_minor_symbol}/kWh")

    # --- State / UI / CSV ---
    def get_save_object(self, view: Any) -> dict[str, Any]:  # noqa: ARG002
        """Return a saveable dict representation.

        Args:
            view: Unused for TeslaMate outputs.

        Returns:
            Dict suitable for writing into the controller system_state file.
        """
        is_on, reason = self._current_state_from_charge_data()
        self.reason = reason
        return {
            "Name": self.name,
            "Type": self.type,
            "CarID": self.car_id,
            "SystemState": self.system_state,
            "IsOn": is_on,
            "LastChanged": self.last_changed,
            "Reason": reason,
            "AppMode": self.app_mode,
            "RunHistory": self.run_history,
        }

    def get_daily_usage_data(self, name: str | None = None) -> list[dict[str, Any]]:
        """Return daily consumption records.

        Note: Energy usage is returned in kWh.

        Args:
            name (str | None): Optional name for the data set.

        Returns:
            A list of dicts, one per day, matching the existing consumption CSV schema.
        """
        out: list[dict[str, Any]] = []
        daily = self.run_history.get("DailyData")
        if not isinstance(daily, list):
            return out

        for day in daily:
            if not isinstance(day, dict):
                continue
            day_date = day.get("Date")
            if not isinstance(day_date, dt.date):
                continue
            energy_wh = float(day.get("EnergyUsed") or 0)
            total_cost = float(day.get("TotalCost") or 0.0)
            actual_hours = float(day.get("ActualHours") or 0.0)
            avg_price = 0.0
            if energy_wh > 0:
                avg_price = (total_cost / (energy_wh / 1000.0)) * 100.0

            out.append(
                {
                    "Date": day_date,
                    "OutputName": self.name if name is None else name,
                    "ActualHours": actual_hours,
                    "TargetHours": actual_hours,
                    "EnergyUsed": energy_wh / 1000.0,
                    "TotalCost": total_cost,
                    "AveragePrice": avg_price,
                }
            )
        return out

    def get_webapp_data(self, view: Any) -> dict[str, Any]:  # noqa: ARG002
        """Return a snapshot payload for the web UI.

        Args:
            view: Unused for TeslaMate outputs.

        Returns:
            Snapshot dict in the same shape as other outputs (best-effort).
        """
        if self.output_config.get("HideFromWebApp", False):
            return {}

        is_on, reason = self._current_state_from_charge_data()
        today = DateHelper.today()
        today_obj = self._get_day(today)
        energy_wh = float(today_obj.get("EnergyUsed") or 0) if today_obj else 0.0
        total_cost = float(today_obj.get("TotalCost") or 0.0) if today_obj else 0.0
        actual_hours = float(today_obj.get("ActualHours") or 0.0) if today_obj else 0.0
        current_price, current_price_basis = self._get_price_and_basis()

        return {
            "id": self.id,
            "allow_actions": False,
            "name": self.name,
            "is_on": is_on,
            "mode": self.app_mode.value,
            "max_app_mode_on_minutes": None,
            "max_app_mode_off_minutes": None,
            "app_mode_revert_time": None,
            "target_hours": "N/A",
            "actual_hours": f"{actual_hours:.1f}",
            "required_hours": "0.0",
            "planned_hours": "0.0",
            "actual_energy_used": f"{energy_wh / 1000.0:.3f}kWh",
            "actual_cost": f"{self.currency_major_symbol}{total_cost:.2f}",
            "forecast_energy_used": "0.000kWh",
            "forecast_cost": f"{self.currency_major_symbol}0.00",
            "forecast_price": "N/A",
            "total_energy_used": f"{energy_wh / 1000.0:.3f}kWh",
            "total_cost": f"{self.currency_major_symbol}{total_cost:.2f}",
            "average_price": "N/A",
            "next_start_time": None,
            "stopping_at": None,
            "reason": reason,
            "power_draw": self._current_power_draw_text(),
            "current_price": f"{current_price:.1f} {self.currency_minor_symbol}/kWh",
            "current_price_basis": current_price_basis,
        }

    def _get_price(self, as_at_time: dt.datetime | None = None) -> float:
        """Get the energy price on this output's pricing basis (Amber, schedule, or default).

        Args:
            as_at_time: The datetime to get the price for. If None, uses current time

        Returns:
            The price in c/kWh (may be zero or negative)
        """
        return self._get_price_and_basis(as_at_time)[0]

    def _get_price_and_basis(self, as_at_time: dt.datetime | None = None) -> tuple[float, str]:
        """Get the price and the basis it was derived from, for the web app.

        Args:
            as_at_time: The datetime to get the price for. If None, uses current time

        Returns:
            tuple[float, str]: The price and a human-readable basis label.
        """
        return resolve_current_price(
            pricing=self.pricing,
            scheduler=self.scheduler,
            device_mode=self.device_mode,
            amber_channel=self.amber_channel,
            schedule=self.schedule,
            schedule_name=self.schedule_name,
            as_at_time=as_at_time,
        )

    def get_days_of_history(self) -> int:
        """Get the number of days of history stored.

        Returns:
            int: The number of days of history.
        """
        return int(self.output_config.get("DaysOfHistory") or 14)

    def get_api_data(self, _view: Any | None = None, display_name: str | None = None) -> dict:
        """Get the data for API output.

        Args:
            _view (Any | None): Not used.
            display_name (str | None): Optional display name for the output.

        Returns:
            dict: The data for API output.
        """
        is_on, reason = self._current_state_from_charge_data()
        return {
            "Name": self.name,
            "DisplayName": display_name or self.name,
            "Type": self.type,
            "AppMode": self.app_mode.value,
            "State": "ON" if is_on else "OFF",
            "SystemState": str(self.system_state),
            "Reason": reason,
            "LastChanged": self.last_changed,
            "DeviceMode": str(self.device_mode) if self.device_mode else None,
            "ActualHoursToday": float(self.run_history.get("DailyData", [{}])[-1].get("ActualHours") or 0.0) if self.run_history.get("DailyData") else 0.0,
        }

    # --- Internal helpers ---
    def _current_power_draw_text(self) -> str:
        buckets = self._filtered_buckets()
        if not buckets:
            return "None"
        last = buckets[-1]
        avg_kw = last.get("avg_kw")
        if not isinstance(avg_kw, (int, float, str)):
            return "None"
        try:
            watts = float(avg_kw) * 1000.0
        except (TypeError, ValueError):
            return "None"
        return f"{watts:.0f}W" if watts > 0 else "None"

    def _current_state_from_charge_data(self) -> tuple[bool, str]:
        now = DateHelper.now()
        sessions = self._filtered_sessions()
        if not sessions:
            return False, "No TeslaMate data"

        # Consider charging "on" if there is any in-progress session with recent buckets.
        in_progress_ids: set[int] = set()
        for s in sessions:
            end_dt = _as_local_dt(s.get("end_date"))
            if end_dt is None:
                sid = s.get("id")
                if isinstance(sid, int):
                    in_progress_ids.add(sid)

        if not in_progress_ids:
            return False, "Not charging"

        buckets = self._filtered_buckets(session_ids=in_progress_ids)
        if not buckets:
            return True, "Charging (no bucket data yet)"

        last_end = _as_local_dt(buckets[-1].get("bucket_end"))
        if last_end and (now - last_end) <= dt.timedelta(minutes=20):
            return True, "Charging"
        return False, "Not charging"

    def _filtered_sessions(self) -> list[dict[str, Any]]:
        sessions = self._tesla_charge_data.get("sessions")
        if not isinstance(sessions, list):
            return []

        out: list[dict[str, Any]] = []
        for s in sessions:
            if not isinstance(s, dict):
                continue
            if self.car_id is not None and s.get("car_id") != self.car_id:
                continue
            out.append(s)

        out.sort(key=lambda x: str(x.get("start_date") or ""))
        return out

    def _filtered_buckets(self, session_ids: set[int] | None = None) -> list[dict[str, Any]]:
        buckets = self._tesla_charge_data.get("buckets")
        if not isinstance(buckets, list):
            return []

        allowed_session_ids = session_ids
        if allowed_session_ids is None and self.car_id is not None:
            allowed_session_ids = {s.get("id") for s in self._filtered_sessions() if isinstance(s.get("id"), int)}

        out: list[dict[str, Any]] = []
        for b in buckets:
            if not isinstance(b, dict):
                continue
            sid = b.get("charging_process_id")
            if allowed_session_ids is not None and sid not in allowed_session_ids:
                continue
            out.append(b)

        out.sort(key=lambda x: str(x.get("bucket_start") or ""))
        return out

    def _get_day(self, day: dt.date) -> dict[str, Any] | None:
        daily = self.run_history.get("DailyData")
        if not isinstance(daily, list):
            return None
        for d in daily:
            if isinstance(d, dict) and d.get("Date") == day:
                return d
        return None

    @staticmethod
    def _create_empty_history() -> dict[str, Any]:
        return {
            "LastUpdate": DateHelper.now(),
            "HistoryDays": 0,
            "LastStartTime": None,
            "LastMeterRead": 0,
            "CurrentPrice": 0.0,
            "CurrentTotals": {
                "EnergyUsed": 0,
                "HourlyEnergyUsed": 0.0,
                "TotalCost": 0.0,
                "AveragePrice": 0.0,
                "ActualHours": 0.0,
                "ActualDays": 0,
                "ActualHoursPerDay": 0.0,
            },
            "EarlierTotals": {
                "EnergyUsed": 0,
                "HourlyEnergyUsed": 0.0,
                "TotalCost": 0.0,
                "AveragePrice": 0.0,
                "ActualHours": 0.0,
                "ActualDays": 0,
            },
            "AlltimeTotals": {
                "EnergyUsed": 0,
                "HourlyEnergyUsed": 0.0,
                "TotalCost": 0.0,
                "AveragePrice": 0.0,
                "ActualHours": 0.0,
                "ActualDays": 0,
            },
            "DailyData": [],
        }

    def _rebuild_history_from_charge_data(self) -> None:
        now = DateHelper.now()
        # Avoid rebuilding more than once per loop tick.
        if self._last_rebuild and (now - self._last_rebuild) < dt.timedelta(seconds=1):
            return

        sessions = self._filtered_sessions()
        buckets = self._filtered_buckets()

        session_by_id = self._index_sessions_by_id(sessions)
        per_day_session = self._aggregate_buckets(buckets)
        daily_list = self._build_daily_data(per_day_session, session_by_id)
        daily_list = self._apply_history_limit(daily_list)
        self._finalize_daily_totals(daily_list)
        current_totals = self._compute_current_totals(daily_list)
        most_recent_end: dt.datetime | None = None
        for b in buckets:
            end_dt = _as_local_dt(b.get("bucket_end"))
            if end_dt is None:
                continue
            if most_recent_end is None or end_dt > most_recent_end:
                most_recent_end = end_dt

        self.last_changed = most_recent_end or now
        self._save_history(daily_list, current_totals, now)

        self._last_rebuild = now

    @staticmethod
    def _index_sessions_by_id(sessions: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        session_by_id: dict[int, dict[str, Any]] = {}
        for s in sessions:
            sid = s.get("id")
            if isinstance(sid, int):
                session_by_id[sid] = s
        return session_by_id

    def _aggregate_buckets(self, buckets: list[dict[str, Any]]) -> dict[tuple[dt.date, int], dict[str, Any]]:
        """
        Aggregate bucket records into per-day, per-session summaries.

        Args:
            buckets: List of bucket dicts from TeslaMate data.

        Returns:
            A dict mapping (date, session_id) to aggregated data including
            start time, end time, and total kWh added.
        """
        per_day_session: dict[tuple[dt.date, int], dict[str, Any]] = {}
        for b in buckets:
            sid = b.get("charging_process_id")
            if not isinstance(sid, int):
                continue

            start_dt = _as_local_dt(b.get("bucket_start"))
            end_dt = _as_local_dt(b.get("bucket_end"))
            if start_dt is None or end_dt is None:
                continue

            kwh_raw = b.get("kwh_added")
            if not isinstance(kwh_raw, (int, float, str)):
                continue
            try:
                kwh = float(kwh_raw)
            except (TypeError, ValueError):
                continue

            # Price the charge session on this output's pricing basis for the start_dt time.
            price = self._get_price(as_at_time=start_dt)

            day = start_dt.date()
            key = (day, sid)
            entry = per_day_session.get(key)
            if entry is None:
                per_day_session[key] = {
                    "start": start_dt,
                    "end": end_dt,
                    "kwh": kwh,
                    "cost": calc_energy_cost(kwh, price),
                }
                continue

            entry["start"] = min(entry["start"], start_dt)
            entry["end"] = max(entry["end"], end_dt)
            entry["kwh"] += kwh
            entry["cost"] += calc_energy_cost(kwh, price)

        return per_day_session

    @staticmethod
    def _new_day_object(day: dt.date) -> dict[str, Any]:
        return {
            "Date": day,
            "TargetHours": -1,
            "PriorShortfall": 0.0,
            "ActualHours": 0.0,
            "EnergyUsed": 0,
            "HourlyEnergyUsed": 0.0,
            "TotalCost": 0.0,
            "AveragePrice": 0.0,
            "DeviceRuns": [],
        }

    def _build_daily_data(
        self,
        per_day_session: dict[tuple[dt.date, int], dict[str, Any]],
        session_by_id: dict[int, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Build the daily data list from aggregated per-day, per-session data.

        Args:
            per_day_session: Aggregated per-day, per-session data.
            session_by_id: Indexed session data by session ID.

        Returns:
            A list of daily data dicts.
        """
        now = DateHelper.now()
        today = now.date()

        in_progress_session_ids: set[int] = {
            sid
            for sid, sess in session_by_id.items()
            if isinstance(sess, dict) and _as_local_dt(sess.get("end_date")) is None
        }

        daily_map: dict[dt.date, dict[str, Any]] = {}
        for (day, sid), agg in per_day_session.items():
            day_obj = daily_map.get(day)
            if day_obj is None:
                day_obj = self._new_day_object(day)
                daily_map[day] = day_obj

            start_dt = agg["start"]
            end_dt = agg["end"]

            # Handle in-progress charging sessions:
            # - Totals should report up-to-date values.
            # - DeviceRun EndTime and ReasonStopped should be null.
            is_in_progress = False
            # Consider "currently charging" if the most recent bucket end is recent.
            if sid in in_progress_session_ids and day == today and (now - end_dt) <= dt.timedelta(minutes=20):
                is_in_progress = True

            end_for_totals = now if is_in_progress else end_dt
            actual_hours = max(0.0, (end_for_totals - start_dt).total_seconds() / 3600.0)
            energy_wh = round(float(agg["kwh"]) * 1000.0)
            total_cost = agg["cost"]
            average_price = 0.0
            if energy_wh > 0:
                average_price = (total_cost / (energy_wh / 1000.0)) * 100.0

            day_obj["DeviceRuns"].append(
                {
                    "SystemState": self.system_state,
                    "ReasonStarted": StateReasonOn.CHARGING_STARTED,
                    "ReasonStopped": None if is_in_progress else StateReasonOff.CHARGING_ENDED,
                    "StartTime": start_dt,
                    "EndTime": None if is_in_progress else end_dt,
                    "ActualHours": actual_hours,
                    "MeterReadAtStart": 0.0,
                    "PriorMeterRead": 0.0,
                    "LastActualPrice": 0.0,
                    "EnergyUsed": energy_wh,
                    "TotalCost": total_cost,
                    "AveragePrice": average_price,
                    "TeslaSessionId": sid,
                    "TeslaCarId": session_by_id.get(sid, {}).get("car_id"),
                }
            )

        return sorted(daily_map.values(), key=operator.itemgetter("Date"))

    def _apply_history_limit(self, daily_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
        max_days = self.get_days_of_history()
        if max_days > 0 and len(daily_list) > max_days:
            return daily_list[-max_days:]
        return daily_list

    @staticmethod
    def _finalize_daily_totals(daily_list: list[dict[str, Any]]) -> None:
        for day_obj in daily_list:
            total_hours = 0.0
            total_wh = 0
            total_cost = 0.0

            runs = day_obj.get("DeviceRuns")
            if not isinstance(runs, list):
                continue
            for run in runs:
                total_hours += float(run.get("ActualHours") or 0.0)
                total_wh += int(run.get("EnergyUsed") or 0)
                total_cost += float(run.get("TotalCost") or 0.0)

            day_obj["ActualHours"] = total_hours
            day_obj["EnergyUsed"] = total_wh
            day_obj["TotalCost"] = total_cost
            day_obj["HourlyEnergyUsed"] = (total_wh / total_hours) if total_hours > 0 else 0.0
            day_obj["AveragePrice"] = (total_cost / (total_wh / 1000.0)) * 100.0 if total_wh > 0 else 0.0

    @staticmethod
    def _compute_current_totals(daily_list: list[dict[str, Any]]) -> dict[str, Any]:
        current_totals: dict[str, Any] = {
            "EnergyUsed": 0,
            "HourlyEnergyUsed": 0.0,
            "TotalCost": 0.0,
            "AveragePrice": 0.0,
            "ActualHours": 0.0,
            "ActualDays": 0,
            "ActualHoursPerDay": 0.0,
        }
        for d in daily_list:
            current_totals["EnergyUsed"] += int(d.get("EnergyUsed") or 0)
            current_totals["TotalCost"] += float(d.get("TotalCost") or 0.0)
            current_totals["ActualHours"] += float(d.get("ActualHours") or 0.0)
            current_totals["ActualDays"] += 1

        days = int(current_totals["ActualDays"])
        current_totals["HourlyEnergyUsed"] = (current_totals["EnergyUsed"] / (days * 24)) if days > 0 else 0.0
        if current_totals["EnergyUsed"] > 0:
            current_totals["AveragePrice"] = (current_totals["TotalCost"] / (current_totals["EnergyUsed"] / 1000.0)) * 100.0
        current_totals["ActualHoursPerDay"] = (current_totals["ActualHours"] / days) if days > 0 else 0.0
        return current_totals

    def _save_history(self, daily_list: list[dict[str, Any]], current_totals: dict[str, Any], now: dt.datetime) -> None:
        self.run_history["DailyData"] = daily_list
        self.run_history["HistoryDays"] = len(daily_list)
        self.run_history["CurrentTotals"] = current_totals
        self.run_history["AlltimeTotals"] = dict(current_totals)
        self.run_history["LastUpdate"] = now

        last_start: dt.datetime | None = None
        if daily_list:
            runs = daily_list[-1].get("DeviceRuns")
            if isinstance(runs, list) and runs:
                st = runs[-1].get("StartTime")
                if isinstance(st, dt.datetime):
                    last_start = st
        self.run_history["LastStartTime"] = last_start
