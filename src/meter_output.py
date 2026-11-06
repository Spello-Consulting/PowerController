"""Meter-only Smart Device output implementation.

This module provides a read-only Output variant which derives ON/OFF state from
an energy meter's instantaneous power reading, with hysteresis.

Key goals:
- Select a Smart Device meter (DeviceMeter) and log energy usage/costs.
- Treat the output as ON when power draw crosses configurable thresholds.
- Use RunHistory to persist daily run history into system_state.
- No run plan generation and no Smart Device actions.
"""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING, Any

from org_enums import AppMode, RunPlanMode, StateReasonOff, StateReasonOn, SystemState
from sc_foundation import DateHelper

from local_enumerations import DEFAULT_PRICE, AmberChannel, OutputStatusData
from helpers import get_currency_symbols, resolve_current_price
from run_history import RunHistory

if TYPE_CHECKING:
    import datetime as dt

    from sc_foundation import SCConfigManager, SCLogger


class MeterOutput:
    """Read-only output backed by a Smart Device energy meter."""

    def __init__(
        self,
        output_config: dict[str, Any],
        config: SCConfigManager,
        logger: SCLogger,
        scheduler: Any,
        pricing: Any,
        view: Any,
        saved_state: dict[str, Any] | None = None,
    ):
        self.output_config = output_config
        self.config = config
        self.logger = logger
        self.scheduler = scheduler
        self.pricing = pricing

        self.type: str = "meter"

        self.system_state: SystemState = SystemState.EXTERNAL_CONTROL
        self.app_mode: AppMode = AppMode.AUTO

        self.name: str = output_config.get("Name") or "Meter"
        self.id: str = urllib.parse.quote(self.name.lower().replace(" ", "_"))

        # Pricing config (reuse existing fields where possible)
        self.device_mode: RunPlanMode = RunPlanMode.BEST_PRICE
        self.amber_channel = AmberChannel.GENERAL
        self.schedule_name: str | None = None
        self.schedule: dict | None = None
        self.default_price: float = self.config.get("General", "DefaultPrice", default=DEFAULT_PRICE) or DEFAULT_PRICE  # pyright: ignore[reportAttributeAccessIssue]
        self.currency_major_symbol, self.currency_minor_symbol = get_currency_symbols(config)

        # Meter config
        self.device_meter_name: str | None = None
        self.device_meter_id: int = 0

        # Hysteresis thresholds
        self.power_on_threshold_watts: float = 0.0
        self.power_off_threshold_watts: float = 0.0

        # OutputManager compatibility fields (meter outputs are never parent/child by default).
        self.parent_output_name: str | None = None
        self.parent_output: Any | None = None
        self.is_parent: bool = False

        # State tracking
        self.last_changed: dt.datetime | None = None
        self.reason: Any | None = None
        self._is_on: bool = bool(saved_state.get("IsOn")) if isinstance(saved_state, dict) else False

        saved_history = saved_state.get("RunHistory") if isinstance(saved_state, dict) else None
        try:
            self.run_history = RunHistory(self.logger, self._effective_history_config(output_config), saved_history)
        except RuntimeError as e:
            self.logger.log_fatal_error(f"Error initializing RunHistory for output {self.name}: {e}")
            self.run_history = RunHistory(self.logger, self._effective_history_config(output_config), None)

        self.initialise(output_config, view, saved_state)
        self.logger.log_message(f"Output {self.name} initialised.", "debug")

    @staticmethod
    def _effective_history_config(output_config: dict[str, Any]) -> dict[str, Any]:
        """Return a config dict suitable for RunHistory.

        Meter-only outputs typically don't have a meaningful TargetHours. Default to
        TargetHours=-1 ("all hours") unless explicitly provided.
        """
        cfg = dict(output_config or {})
        if cfg.get("TargetHours") is None:
            cfg["TargetHours"] = -1
        return cfg

    def set_parent_output(self, parent: Any) -> None:
        self.parent_output = parent

    def initialise(self, output_config: dict[str, Any], view: Any, _saved_state: Any) -> None:
        def _validation_error(msg):
            raise RuntimeError(msg)

        self.output_config = output_config

        try:
            self.name = output_config.get("Name") or "Meter"
            self.id = urllib.parse.quote(self.name.lower().replace(" ", "_"))

            # Meter selection
            self.device_meter_name = output_config.get("DeviceMeter")
            if not self.device_meter_name:
                _validation_error(f"DeviceMeter is not set for meter output {self.name}.")
            else:
                self.device_meter_id = view.get_meter_id(self.device_meter_name)
                if not self.device_meter_id:
                    _validation_error(f"DeviceMeter {self.device_meter_name} not found for meter output {self.name}.")

            # Thresholds (defaults chosen to be conservative)
            on_th = output_config.get("PowerOnThresholdWatts", 50)
            off_th = output_config.get("PowerOffThresholdWatts", 30)
            try:
                self.power_on_threshold_watts = float(on_th)
                self.power_off_threshold_watts = float(off_th)
            except (TypeError, ValueError):
                _validation_error(f"Invalid PowerOnThresholdWatts/PowerOffThresholdWatts for meter output {self.name}.")

            if self.power_off_threshold_watts > self.power_on_threshold_watts:
                _validation_error(
                    f"PowerOffThresholdWatts ({self.power_off_threshold_watts}) must be <= "
                    f"PowerOnThresholdWatts ({self.power_on_threshold_watts}) for meter output {self.name}."
                )

            # Pricing mode: reuse existing Mode values (BestPrice=Amber, Schedule=Schedule pricing)
            mode = output_config.get("Mode") or RunPlanMode.BEST_PRICE
            self.device_mode = mode
            if self.device_mode not in RunPlanMode:
                _validation_error(f"A valid Mode has not been set for meter output {self.name}.")
            self.currency_major_symbol, self.currency_minor_symbol = get_currency_symbols(self.config)

            # Amber channel
            self.amber_channel = output_config.get("AmberChannel", AmberChannel.GENERAL) or AmberChannel.GENERAL
            if self.amber_channel not in AmberChannel:
                _validation_error(f"Invalid AmberChannel {self.amber_channel} for meter output {self.name}.")

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

            # ParentOutput (optional; stored for generic linking)
            self.parent_output_name = output_config.get("ParentOutput")

            # Reinitialise the run_history object
            self.run_history.initialise(self._effective_history_config(output_config))

        except RuntimeError:
            raise
        except (KeyError, IndexError) as e:
            raise RuntimeError(str(e)) from e
        else:
            # Finally calculate running totals so the object is immediately usable
            self.calculate_running_totals(view)

    # --- Controller loop hooks ---
    @staticmethod
    def tell_device_status_updated(_view: Any) -> None:
        return

    def calculate_running_totals(self, view: Any, is_new_day: bool = False) -> None:  # noqa: ARG002
        power_draw = (view.get_meter_power(self.device_meter_id) or 0.0) if self.device_meter_id else 0.0

        # Apply hysteresis
        new_is_on = self._is_on
        if power_draw >= self.power_on_threshold_watts:
            new_is_on = True
        elif power_draw <= self.power_off_threshold_watts:
            new_is_on = False

        status_data = OutputStatusData(
            meter_reading=(view.get_meter_energy(self.device_meter_id) or 0.0) if self.device_meter_id else 0.0,
            power_draw=power_draw,
            is_on=new_is_on,
            target_hours=-1,
            current_price=self._get_price(),
            output_type="meter",
        )

        # Start/stop runs based on derived state
        current_run = self.run_history.get_current_run()
        if new_is_on and current_run is None:
            self.run_history.start_run(SystemState.EXTERNAL_CONTROL, StateReasonOn.POWER_INCREASE, status_data)
            self.last_changed = DateHelper.now()
            self.reason = StateReasonOn.POWER_INCREASE
        elif not new_is_on and current_run is not None:
            self.run_history.stop_run(StateReasonOff.POWER_DECREASE, status_data)
            self.last_changed = DateHelper.now()
            self.reason = StateReasonOff.POWER_DECREASE

        self._is_on = new_is_on

        # Always tick totals (handles day rollover, energy/cost accrual for open run)
        self.run_history.tick(status_data)

    @staticmethod
    def review_run_plan(_view: Any) -> None:
        return

    @staticmethod
    def evaluate_conditions(**_kwargs: Any) -> None:
        return

    @staticmethod
    def get_action_request() -> None:
        return

    def set_app_mode(self, new_mode: AppMode, view: Any, revert_minutes: Any = None) -> None:
        # Read-only output: accept the value but it has no effect.
        _ = (view, revert_minutes)
        self.app_mode = new_mode

    @staticmethod
    def shutdown(_view: Any) -> bool:
        return False

    # --- Pricing helpers ---
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

    # --- State / CSV / UI ---
    def get_save_object(self, view: Any) -> dict[str, Any]:
        _ = view
        return {
            "Name": self.name,
            "Type": self.type,
            "SystemState": self.system_state,
            "IsOn": self._is_on,
            "LastChanged": self.last_changed,
            "Reason": self.reason,
            "AppMode": self.app_mode,
            "DeviceMeterName": self.device_meter_name,
            "RunHistory": self.run_history.history,
        }

    def get_daily_usage_data(self, name: str | None = None) -> list[dict[str, Any]]:
        return self.run_history.get_daily_usage_data(name)

    def get_webapp_data(self, view: Any) -> dict[str, Any]:
        if self.output_config.get("HideFromWebApp", False):
            return {}

        today = self.run_history.get_current_day()
        actual_cost = float(today["TotalCost"]) if today else 0.0
        actual_energy_used = float(today["EnergyUsed"]) if today else 0.0
        actual_hours = float(today["ActualHours"]) if today else 0.0

        power_draw = (view.get_meter_power(self.device_meter_id) or 0.0) if self.device_meter_id else 0.0
        current_price, current_price_basis = self._get_price_and_basis()

        reason_text = self.reason.value if self.reason is not None else "Monitoring"

        

        return {
            "id": self.id,
            "allow_actions": False,
            "name": self.name,
            "is_on": self._is_on,
            "mode": self.app_mode.value,
            "max_app_mode_on_minutes": None,
            "max_app_mode_off_minutes": None,
            "app_mode_revert_time": None,
            "target_hours": "N/A",
            "actual_hours": f"{actual_hours:.1f}",
            "required_hours": "0.0",
            "planned_hours": "0.0",
            "actual_energy_used": f"{actual_energy_used / 1000.0:.3f}kWh",
            "actual_cost": f"{self.currency_major_symbol}{actual_cost:.2f}",
            "forecast_energy_used": "0.000kWh",
            "forecast_cost": f"{self.currency_major_symbol}0.00",
            "forecast_price": "N/A",
            "total_energy_used": f"{actual_energy_used / 1000.0:.3f}kWh",
            "total_cost": f"{self.currency_major_symbol}{actual_cost:.2f}",
            "average_price": "N/A",
            "next_start_time": None,
            "stopping_at": None,
            "reason": reason_text,
            "power_draw": f"{power_draw:.0f}W" if power_draw else "None",
            "current_price": f"{current_price:.1f} {self.currency_minor_symbol}/kWh",
            "current_price_basis": current_price_basis,
        }

    def get_schedule(self) -> dict | None:
        """Get the schedule for this output.

        Returns:
            dict: The schedule or None if none assigned.
        """
        return self.schedule

    def get_api_data(self, _view: Any, display_name: str | None = None) -> dict:
        """Get the data for API output.

        Args:
            _view (Any): Not used
            display_name (str | None): Optional display name for the output.

        Returns:
            dict: The data for API output.
        """
        return {
            "Name": self.name,
            "DisplayName": display_name or self.name,
            "Type": self.type,
            "State": "ON" if self._is_on else "OFF",
            "AppMode": str(self.app_mode),
            "SystemState": str(self.system_state),
            "Reason": str(self.reason) if self.reason else None,
            "LastChanged": self.last_changed,
            "DeviceMode": str(self.device_mode) if self.device_mode else None,
            "ActualHoursToday": self.run_history.get_actual_hours() if self.run_history else 0.0,
        }
