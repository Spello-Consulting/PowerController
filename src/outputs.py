"""Manages the system state of a specific output device and associated resources."""
import datetime as dt
import threading
import urllib.parse
from collections.abc import Callable

from org_enums import (
    AppMode,
    RunPlanMode,
    RunPlanStatus,
    RunPlanTargetHours,
    StateReasonOff,
    StateReasonOn,
    SystemState,
)
from sc_foundation import DateHelper, SCConfigManager, SCLogger

from sc_smart_device import (
    DeviceSequenceRequest,  
    DeviceSequenceResult,   
    DeviceStep,             
    SmartDeviceView,        
    StepKind,               
)


from local_enumerations import (
    FAILED_RUNPLAN_CHECK_INTERVAL,
    RUNPLAN_CHECK_INTERVAL,
    DELAY_AFTER_STATE_CHANGE,
    AmberChannel,
    InputMode,
    OutputAction,
    OutputActionType,
    OutputStatusData,
    UPSMode,
    WeatherMode,
)
from pricing import PricingManager
from run_history import RunHistory
from run_plan import RunPlanner
from scheduler import Scheduler
from helpers import get_currency_symbols
from output_constraint import OutputConstraint
from ups_integration import UPSIntegration
from weather_integration import WeatherIntegration


class OutputManager:  # noqa: PLR0904
    """Manages the state of a single Smart Device output device and associated resources."""

    # Public Functions ============================================================================
    def __init__(self, output_config: dict, config: SCConfigManager, logger: SCLogger, scheduler: Scheduler, pricing: PricingManager, view: SmartDeviceView, ups_integration: UPSIntegration, weather_integration: WeatherIntegration, saved_state: dict | None = None):  # noqa: PLR0915
        """Manages the state of a single Smart Device output device.

        Args:
            output_config (dict): The configuration for the output device - the config file's OutputConfiguration list entry.
            config (SCConfigManager): The configuration manager for the system.
            logger (SCLogger): The logger for the system.
            scheduler (Scheduler): The scheduler for managing time-based operations.
            pricing (PricingManager): The pricing manager for handling pricing-related tasks.
            view (SmartDeviceView): The current view of the Smart Device devices.
            ups_integration (UPSIntegration): The UPS integration manager for handling UPS-related tasks.
            weather_integration (WeatherIntegration): The weather integration manager for handling weather-related constraints.
            saved_state (dict | None): The previously saved state of the output manager, if any.
        """
        self.output_config = output_config
        self.config = config
        self.logger = logger
        self.report_critical_errors_delay = config.get("General", "ReportCriticalErrorsDelay", default=None)
        if isinstance(self.report_critical_errors_delay, (int, float)):
            self.report_critical_errors_delay = round(self.report_critical_errors_delay, 0)
        else:
            self.report_critical_errors_delay = None
        self.scheduler = scheduler
        self.pricing = pricing
        self.type: str = "smart device"

        # Define the output attributes that we will initialise later
        self.system_state: SystemState = SystemState.AUTO  # The overall system state, to be updated
        self.app_mode = AppMode.AUTO  # Defines the override from the mobile app.
        self.app_mode_max_on_time: int = 0  # Default maximum time in minutes for AppMode ON
        self.app_mode_max_off_time: int = 0  # Default maximum time in minutes for AppMode OFF
        self.app_mode_revert_time: dt.datetime | None = None  # If app mode is ON or OFF, the time to revert to AUTO
        self.name = None
        self.id = None  # Lower case, url-safe version of the name
        self.output_config = output_config
        self.device_mode = RunPlanMode.SCHEDULE
        self.device_input_mode = InputMode.IGNORE
        self.parent_output = None  # The parent OutputManager, if any

        # pricing and scheduling
        self.schedule_name = None
        self.schedule = None
        self.constaint_schedule_name = None
        self.constaint_schedule = None
        self.amber_channel = AmberChannel.GENERAL
        self.max_best_price = self.max_priority_price = 0
        self.currency_major_symbol, self.currency_minor_symbol = get_currency_symbols(config)

        # Smart Device components
        self.device_id = 0   # The Smart Device  ID for the output's device
        self.device_name = None  # The name of the Device

        self.device_output_id = 0
        self.device_output_name = None

        self.device_meter_id = 0
        self.device_meter_name = None

        self.device_input_id = 0
        self.device_input_name = None

        self.parent_output_id = 0
        self.parent_output_name = None
        self.is_parent = False

        # Run planning
        self.run_plan = None
        self.invalidate_run_plan = True
        self.next_run_plan_check = DateHelper.now()
        self.last_price = 0
        self.min_hours = 0
        self.max_hours = 0
        self.run_plan_target_mode = RunPlanTargetHours.ALL_HOURS if output_config.get("TargetHours") == -1 else RunPlanTargetHours.NORMAL

        # Minimum runtime configuration
        self.min_on_time = 0  # minutes
        self.min_off_time = 0  # minutes
        self.max_off_time = 0  # minutes

        # UPS integration
        self.ups_integration = ups_integration
        self.weather_integration = weather_integration

        # Track state changes
        self._output_action_request: OutputAction | None = None
        self.last_turned_on = saved_state.get("LastTurnedOn") if saved_state else None
        self.last_turned_off = saved_state.get("LastTurnedOff") if saved_state else None

        try:
            saved_run_history = None
            if saved_state and "RunHistory" in saved_state:
                saved_run_history = saved_state.get("RunHistory")
            self.run_history = RunHistory(self.logger, output_config, saved_run_history)
        except RuntimeError as e:
            self.logger.log_fatal_error(f"Error initializing RunHistory for output {self.name}: {e}")

        # The required state of the device
        self.last_changed = None
        self.reason = None

        self.output_constraint: OutputConstraint | None = None
        self.initialise(output_config, view, saved_state)
        self.logger.log_message(f"Output {self.name} initialised.", "debug")

        # Only access this in tell_device_status_updated()
        self._last_device_online_status = view.get_device_online(self.device_id)  # If device is offline, assume the output is off

        # See if the output's saved state matches the actual device state
        device_output_saved_state = saved_state.get("IsOn") if saved_state else None
        if saved_state and view.get_device_online(self.device_id) and view.get_output_state(self.device_output_id) != device_output_saved_state:
            self.logger.log_message(f"Output {self.name} saved state does not match actual device state. Saved: {'On' if device_output_saved_state else 'Off'}, Actual: {'On' if view.get_device_online(self.device_id) else 'Off'}. Output relay may have been changed by another application.", "warning")

    def initialise(self, output_config: dict, view: SmartDeviceView, saved_state: dict | None):  # noqa: PLR0912, PLR0915
        """Initializes the output manager with the given configuration.

        Args:
            output_config (dict): The configuration for the output device.
            view (SmartDeviceView): The current view of the Smart devices.

        Raises:
            RuntimeError: If the configuration is invalid.
        """
        def _validation_error(msg):
            raise RuntimeError(msg)

        self.output_config = output_config
        self.invalidate_run_plan = True  # Force a regeneration of the run plan if config changes
        try:
            # Name
            self.name = output_config.get("Name")
            if not self.name:
                _validation_error("Name is not set for an Output configuration.")
            else:
                # self.id is url encoded version of name
                self.id = urllib.parse.quote(self.name.lower().replace(" ", "_"))

            # SmartDeviceOutput
            self.device_output_name = output_config.get("DeviceOutput")
            if not self.device_output_name:
                _validation_error(f"DeviceOutput is not set for output {self.name}.")
            else:
                self.device_output_id = view.get_output_id(self.device_output_name)
                if not self.device_output_id:
                    _validation_error(f"DeviceOutput {self.device_output_name} not found for output {self.name}.")
                else:
                    self.device_id = view.get_output_device_id(self.device_output_id)
                    self.device_name = view.get_device_name(self.device_id)

            # Mode
            self.device_mode = output_config.get("Mode")
            if not self.device_mode or self.device_mode not in RunPlanMode:
                _validation_error(f"A valid mode has not been set for output {self.name}.")
            else:
                self.system_state = SystemState.AUTO

            # App Mode (issue 106)
            if saved_state and saved_state.get("AppMode"):
                self.app_mode = saved_state.get("AppMode")
                if saved_state.get("AppModeRevertTime"):
                    self.app_mode_revert_time = saved_state.get("AppModeRevertTime")

            # Schedule
            self.schedule_name = output_config.get("Schedule")
            if self.schedule_name:
                self.schedule = self.scheduler.get_schedule_by_name(self.schedule_name)
                if not self.schedule:
                    _validation_error(f"Schedule {self.schedule_name} for output {self.name} not found in OperatingSchedules.")
            elif self.device_mode == RunPlanMode.SCHEDULE:
                _validation_error(f"Schedule is not set for output {self.name}. This is required if Mode is Schedule.")

            # ConstraintSchedule
            self.constaint_schedule_name = output_config.get("ConstraintSchedule")
            if self.constaint_schedule_name:
                self.constaint_schedule = self.scheduler.get_schedule_by_name(self.constaint_schedule_name)
                if not self.constaint_schedule:
                    _validation_error(f"Constraint schedule {self.constaint_schedule_name} for output {self.name} not found in OperatingSchedules.")
                elif self.device_mode != RunPlanMode.BEST_PRICE:
                    self.logger.log_message(f"Constraint schedule {self.constaint_schedule_name} will be ignored for for output {self.name} since the device mode is not BestPrice.", "warning")

            # AmberChannel
            self.amber_channel = output_config.get("AmberChannel", AmberChannel.GENERAL) or AmberChannel.GENERAL
            if self.amber_channel not in AmberChannel:
                _validation_error(f"Invalid AmberChannel {self.amber_channel} for output {self.name}. Must be one of {', '.join([m.value for m in AmberChannel])}.")

            # Min / Max / Target hours
            self.min_hours = output_config.get("MinHours", -1)
            self.max_hours = output_config.get("MaxHours", -1)
            self.run_plan_target_mode = RunPlanTargetHours.ALL_HOURS if output_config.get("TargetHours") == -1 else RunPlanTargetHours.NORMAL
            if self.run_plan_target_mode == RunPlanTargetHours.NORMAL:
                target_hours = self._get_target_hours()
                if (self.min_hours < 0 or
                    self.max_hours < self.min_hours or
                    self.max_hours > 24 or
                    target_hours < self.min_hours or
                    target_hours > self.max_hours
                    ):
                    _validation_error(f"Invalid MinHours / MaxHours/ TargetHours configuration for output {self.name}.")
                # Note: TargetHours is set during calculate_running_totals()
            # Note: If self.mode == RunHistoryMode.ALL_DAY, then min / max / target are ignored

            # Minimum runtime configuration
            self.min_on_time = output_config.get("MinOnTime", 0)  # minutes
            self.min_off_time = output_config.get("MinOffTime", 0)  # minutes
            self.max_off_time = output_config.get("MaxOffTime", 0)  # minutes
            if self.min_off_time > self.min_on_time:
                _validation_error(f"MinOffTime {self.min_off_time} must be less than or equal to MinOnTime {self.min_on_time} for output {self.name}.")
            if self.min_off_time > 0 and self.max_off_time > 0:
                _validation_error(f"MaxOffTime and MinOffTime cannot both be set for output {self.name}.")

            # Default revert times in minutes for AppMode ON and OFF
            self.app_mode_max_on_time = self.output_config.get("MaxAppOnTime", 0)
            self.app_mode_max_off_time = self.output_config.get("MaxAppOffTime", 0)

            # Prices
            self.max_best_price = output_config.get("MaxBestPrice")
            self.max_priority_price = output_config.get("MaxPriorityPrice")
            if (not self.max_best_price or not self.max_priority_price):
                _validation_error(f"MaxBestPrice and MaxPriorityPrice must be properly set for output {self.name}.")
            self.currency_major_symbol, self.currency_minor_symbol = get_currency_symbols(self.config)

            # DeviceMeter
            self.device_meter_name = output_config.get("DeviceMeter")
            if self.device_meter_name:
                self.device_meter_id = view.get_meter_id(self.device_meter_name)
                if not self.device_meter_id:
                    _validation_error(f"DeviceMeter {self.device_meter_name} not found for output {self.name}.")

            # DeviceInput
            self.device_input_name = output_config.get("DeviceInput")
            if self.device_input_name:
                self.device_input_id = view.get_input_id(self.device_input_name)
                if not self.device_input_id:
                    _validation_error(f"DeviceInput {self.device_input_name} not found for output {self.name}.")

            # DeviceInputMode
            self.device_input_mode = output_config.get("DeviceInputMode", InputMode.IGNORE) or InputMode.IGNORE
            if self.device_input_mode not in InputMode:
                _validation_error(f"Invalid DeviceInputMode {self.device_input_mode} for output {self.name}. Must be one of {', '.join([m.value for m in InputMode])}.")

            # TempProbeConstraints and DatesOff are parsed and validated inside OutputConstraint
            self.output_constraint = OutputConstraint(
                output_config=output_config,
                name=self.name, # pyright: ignore[reportArgumentType]
                logger=self.logger,
                ups_integration=self.ups_integration,
                weather_integration=self.weather_integration,
                device_output_id=self.device_output_id,
                view=view,
            )

            # ParentDeviceOutput
            self.parent_output_name = output_config.get("ParentOutput")
            # Note: We can't lookup the actual parent output here as it may not have been created yet.

            # Reinitialise the run_history object
            self.run_history.initialise(output_config)

        except RuntimeError:
            raise
        except (KeyError, IndexError) as e:
            raise RuntimeError(str(e)) from e
        else:
            self.calculate_running_totals(view)   # Finally calculate all running totals

    def get_save_object(self, view: SmartDeviceView) -> dict:
        """Returns the representation of this output object that can be saved to disk.

        Returns:
            dict: The representation of the output object.
        """
        output_dict = {
            "Name": self.name,
            "SystemState": self.system_state,
            "IsOn": view.get_output_state(self.device_output_id),
            "Type": self.type,
            "HideFromTempProbesSummary": self.output_config.get("HideFromTempProbesSummary", False) or False,
            "LastChanged": self.last_changed,
            "Reason": self.reason,
            "AppMode": self.app_mode,
            "AppModeRevertTime": self.app_mode_revert_time,
            "DeviceMode": self.device_mode,
            "ParentOutputName": self.parent_output_name,
            "DeviceOutputName": self.device_output_name,
            "DeviceMeterName": self.device_meter_name,
            "DeviceInputName": self.device_input_name,
            "DeviceInputMode": self.device_input_mode,
            "ScheduleName": self.schedule_name,
            "AmberChannel": self.amber_channel,
            "MaxBestPrice": self.max_best_price,
            "MaxPriorityPrice": self.max_priority_price,
            "MinHours": self.min_hours,
            "MaxHours": self.max_hours,
            "TargetHours": self._get_target_hours(),
            "RunPlanTargetMode": self.run_plan_target_mode,
            "LastTurnedOn": self.last_turned_on,
            "LastTurnedOff": self.last_turned_off,
            "MinOnTime": self.min_on_time,
            "MinOffTime": self.min_off_time,
            "MaxOffTime": self.max_off_time,
            "DatesOff": self.output_constraint.get_dates_off() if self.output_constraint else [],
            "RunPlan": self.run_plan,
            "RunHistory": self.run_history.history,
        }
        return output_dict

    def get_webapp_data(self, view: SmartDeviceView) -> dict:  # noqa: PLR0914
        """Get the data for the web application.

        Args:
            view (SmartDeviceView): The current view of the smart devices.

        Returns:
            dict: The web application data or an empty dist if the output is hidden.
        """
        if self.output_config.get("HideFromWebApp", False):
            return {}

        is_device_output_on = view.get_output_state(self.device_output_id)
        current_action = self.get_action_request()
        if current_action and current_action.request:
            reason_text = "Sequence running: " + current_action.request.label
        else:
            reason_text = self.reason.value if self.reason else "Unknown"
            if self.app_mode != AppMode.AUTO and self.app_mode_revert_time:
                # If we are in AppMode ON or OFF, append the revert time to reason
                reason_text += f" (reverting at {self.app_mode_revert_time.strftime('%H:%M:%S')})"
        target_hours = self._get_target_hours()
        current_day = self.run_history.get_current_day()
        actual_cost = current_day["TotalCost"] if current_day else 0
        forecast_cost = self.run_plan.get("EstimatedCost", 0) if self.run_plan else 0
        actual_energy_used = current_day["EnergyUsed"] if current_day else 0
        forecast_energy_used = self.run_plan.get("ForecastEnergyUsage", 0) if self.run_plan else 0
        forecast_price = self.run_plan.get("ForecastAveragePrice", 0) if self.run_plan else 0

        next_start_dt = self.run_plan.get("NextStartDateTime") if self.run_plan else None
        if next_start_dt and not is_device_output_on:
            next_start = next_start_dt.strftime("%H:%M")
        else:
            next_start = None
        stopping_at_dt = self.run_plan.get("NextStopDateTime") if self.run_plan else None
        if stopping_at_dt and is_device_output_on:
            stopping_at = stopping_at_dt.strftime("%H:%M")
        else:
            stopping_at = None
        power_draw = view.get_meter_power(self.device_meter_id) if self.device_meter_id else 0
        data = {
            "id": self.id,
            "allow_actions": True,
            "name": self.name,
            "is_on": is_device_output_on,
            "is_online": view.get_device_online(self.device_id),
            "mode": self.app_mode.value,
            "max_app_mode_on_minutes": self.app_mode_max_on_time,
            "max_app_mode_off_minutes": self.app_mode_max_off_time,
            "app_mode_revert_time": self.app_mode_revert_time.strftime("%Y-%m-%d %H:%M") if self.app_mode_revert_time else None,

            # Information on the run history and plan
            "target_hours": f"{target_hours:.1f}" if target_hours is not None else "Rest of Day",
            "actual_hours": f"{self.run_history.get_actual_hours():.1f}",
            "required_hours": f"{(self.run_plan.get("RequiredHours", 0) if self.run_plan else 0):.1f}",
            "planned_hours": f"{(self.run_plan.get("PlannedHours", 0) if self.run_plan else 0):.1f}",
            "actual_energy_used": f"{actual_energy_used / 1000:.3f}kWh",
            "actual_cost": f"{self.currency_major_symbol}{actual_cost:.2f}",
            "forecast_energy_used": f"{forecast_energy_used / 1000:.3f}kWh",
            "forecast_cost": f"{self.currency_major_symbol}{forecast_cost:.2f}",
            "forecast_price": f"{forecast_price:.2f} {self.currency_minor_symbol}/kWh" if forecast_price > 0 else "N/A",

            # These are calculated below
            "total_energy_used": 0,
            "total_cost": 0,
            "average_price": 0,

            # Information on the current run
            "next_start_time": next_start,
            "stopping_at": stopping_at,
            "reason": reason_text,
            "power_draw": f"{power_draw:.0f}W" if power_draw else "None",
            "current_price": f"{self._get_current_price():.1f} {self.currency_minor_symbol}/kWh" if self._get_current_price() > 0 else "N/A",
        }
        total_cost = actual_cost + forecast_cost
        data["total_cost"] = f"{self.currency_major_symbol}{total_cost:.2f}"
        total_energy_used = actual_energy_used + forecast_energy_used
        data["total_energy_used"] = f"{total_energy_used / 1000:.3f}kWh"
        average_price = RunHistory.calc_price(total_energy_used, total_cost)
        data["average_price"] = f"{average_price:.2f} {self.currency_minor_symbol}/kWh" if average_price > 0 else "N/A"
        return data

    def tell_device_status_updated(self, view: SmartDeviceView):
        """Notify this output that the device status may have changed."""
        if not self.device_id:
            return
        new_online_status = view.get_device_online(self.device_id)
        if new_online_status != self._last_device_online_status:
            self.invalidate_run_plan = True
            if new_online_status:
                self.logger.log_message(f"Device {self.device_name} for output {self.name} is now online.", "debug")
                self.logger.clear_notifiable_issue(entity=f"Device {self.device_name}", issue_type="Device Offline")

            elif not view.get_device_expect_offline(self.device_id):
                self.logger.log_message(f"Device {self.device_name} is offline.", "warning")
                if self.report_critical_errors_delay:
                    assert isinstance(self.report_critical_errors_delay, int)
                    self.logger.report_notifiable_issue(entity=f"Device {self.device_name}", issue_type="Device Offline", send_delay=self.report_critical_errors_delay * 60, message=f"Device is offline when trying to turn output {self.device_output_name} on.")  # pyright: ignore[reportArgumentType]

        self._last_device_online_status = new_online_status

    def calculate_running_totals(self, view: SmartDeviceView, is_new_day: bool = False):
        """Update running totals in run_history object.

        Args:
            view (SmartDeviceView): The current SmartDeviceView snapshot.
            is_new_day (bool): Indicates if it's a new day.
        """
        data_block = self._get_status_data(view)

        if is_new_day:
            self.invalidate_run_plan = True  # If it's a new day, we need a new run plan

        if self.run_history.tick(data_block):
            self.invalidate_run_plan = True  # If we've rolled over to a new day, we need a new run plan

        # Update the remaining hours in the current run plan if we have one
        if self.run_plan:
            RunPlanner.tick(self.run_plan)  # Update the run plan's internal state

    def review_run_plan(self, view: SmartDeviceView) -> bool:
        """Generate / update the run plan for this output if needed.

        Returns:
            bool: True if the run plan was successfully generated or updated, False otherwise.
        """
        if not self._new_runplan_needed(view):
            return False

        self.logger.log_message(f"Generating new run plan for output {self.name}", "debug")
        self.run_plan = None
        hourly_energy_used = self.run_history.get_hourly_energy_used()

        # Finally calculate the hours remaining for today
        actual_hours = self.run_history.get_actual_hours()
        if self.run_plan_target_mode == RunPlanTargetHours.ALL_HOURS:
            required_hours = -1
            priority_hours = max(0, self.min_hours - actual_hours)
        else:
            target_hours = self._get_target_hours()  # Should not be None
            assert target_hours is not None
            prior_shortfall, max_shortfall = self.run_history.get_prior_shortfall()
            if self.report_critical_errors_delay:
                if prior_shortfall >= max_shortfall > 0:
                    assert isinstance(self.report_critical_errors_delay, int)
                    self.logger.report_notifiable_issue(entity=f"Output {self.name}", issue_type="Reached MaxShortfall", send_delay=self.report_critical_errors_delay * 60, message=f"This output has reached the maximum shortfall of {max_shortfall} hours. Please review the configuration to make sure it's possible to run for sufficient hours each day.")  # pyright: ignore[reportArgumentType]
                else:
                    self.logger.clear_notifiable_issue(entity=f"Output {self.name}", issue_type="Reached MaxShortfall")

            hours_remaining = target_hours - actual_hours + prior_shortfall
            required_hours = max(0.0, hours_remaining)
            required_hours = min(self.max_hours, required_hours)
            priority_hours = min(self.min_hours - actual_hours, required_hours)
            priority_hours = max(0.0, priority_hours)

        # If we're in the Best Price mode, get a best price run plan
        if self.device_mode == RunPlanMode.BEST_PRICE:
            constraint_slots = None
            if self.constaint_schedule:
                # Apply the constraint schedule to limit available time slots
                constraint_slots = self.scheduler.get_schedule_slots(self.constaint_schedule)
            self.run_plan = self.pricing.get_run_plan(required_hours=required_hours, priority_hours=priority_hours, max_price=self.max_best_price, max_priority_price=self.max_priority_price, channel_id=self.amber_channel, hourly_energy_usage=hourly_energy_used, constraint_slots=constraint_slots)  # pyright: ignore[reportArgumentType]

        # If we're in Schedule mode or we get nothing back from Best Price, generate a Schedule
        if self.device_mode == RunPlanMode.SCHEDULE or not self.run_plan:
            self.run_plan = self.scheduler.get_run_plan(self.schedule_name, required_hours=required_hours, priority_hours=priority_hours, max_price=self.max_best_price, max_priority_price=self.max_priority_price, hourly_energy_usage=hourly_energy_used)  # pyright: ignore[reportArgumentType]

        # Log errors and warnings
        time_now = DateHelper.now()
        if not self.run_plan or self.run_plan["Status"] == RunPlanStatus.FAILED:
            self.next_run_plan_check = DateHelper.add_datetime(time_now, minutes=FAILED_RUNPLAN_CHECK_INTERVAL)
            logging_level = "warning" if self.run_plan_target_mode == RunPlanTargetHours.ALL_HOURS else "error"
            self.logger.log_message(f"Failed to generate run plan for output {self.name}. Next check at {self.next_run_plan_check.strftime('%H:%M')}.", logging_level)

        elif self.run_plan["Status"] == RunPlanStatus.BELOW_MINIMUM and self.run_plan_target_mode == RunPlanTargetHours.NORMAL:
            self.next_run_plan_check = DateHelper.add_datetime(time_now, minutes=FAILED_RUNPLAN_CHECK_INTERVAL)
            self.logger.log_message(f"Partially generated run plan for output {self.name}. Not enough low-price slots to meet pririority or target hours. Next check at {self.next_run_plan_check.strftime('%H:%M')}.", "warning")

        elif self.run_plan["Status"] == RunPlanStatus.PARTIAL and self.run_plan_target_mode == RunPlanTargetHours.NORMAL:
            self.next_run_plan_check = DateHelper.add_datetime(time_now, minutes=FAILED_RUNPLAN_CHECK_INTERVAL)
            self.logger.log_message(f"Partially generated run plan for output {self.name}. Not enough low-price slots to meet target hours. Next check at {self.next_run_plan_check.strftime('%H:%M')}.", "warning")

        else:
            self.next_run_plan_check = DateHelper.add_datetime(time_now, minutes=RUNPLAN_CHECK_INTERVAL)
            self.logger.log_message(f"Successfully generated run plan for output {self.name}. Next check at {self.next_run_plan_check.strftime('%H:%M')}.", "debug")

        self.invalidate_run_plan = False
        return bool(self.run_plan)

    def evaluate_conditions(self, view: SmartDeviceView, output_sequences: dict[str, DeviceSequenceRequest] | None = None, on_complete: Callable[[DeviceSequenceResult], None] | None = None) -> OutputAction | None:  # noqa: PLR0912, PLR0915
        """Evaluate the conditions for this output.

        Note: calculate_running_totals should be called before this method.

        Args:
            view (SmartDeviceView): The current view of the smart devices.
            output_sequences (dict[str, DeviceSequenceRequest] | None): Optional dictionary of the available output sequences.
            on_complete (Callable[[DeviceSequenceResult], None] | None): Optional callback to be called when the action is complete.

        Returns:
            OutputAction: The action to be taken for the output, or None if no action is needed.
        """
        # # Issue 84: If we very recently changed state, skip evaluation until the next loop so the
        # device has time to settle and the view reflects the new state accurately.
        now = DateHelper.now()
        if self.last_turned_on and (now - self.last_turned_on).total_seconds() < DELAY_AFTER_STATE_CHANGE:
            self.logger.log_message(f"Output {self.name} changed state {(now - self.last_turned_on).total_seconds():.0f}s ago. Skipping evaluate_conditions.", "debug")
            return None
        if self.last_turned_off and (now - self.last_turned_off).total_seconds() < DELAY_AFTER_STATE_CHANGE:
            self.logger.log_message(f"Output {self.name} changed state {(now - self.last_turned_off).total_seconds():.0f}s ago. Skipping evaluate_conditions.", "debug")
            return None

        new_output_state = None  # This is the new state. Once it's set, we are blocked from further checks
        new_system_state = None
        reason_on = reason_off = None
        is_device_online = view.get_device_online(self.device_id)
        is_device_output_on = view.get_output_state(self.device_output_id)

        # See if the app has overridden our state. Only allow changes if the device is online
        if is_device_online:
            if self._should_revert_app_override(view):  # Revert to auto mode if currently AppMode.ON | AppMode.OFF and time's up
                self.app_mode = AppMode.AUTO
                self.app_mode_revert_time = None
            else:
                if self.app_mode == AppMode.ON:
                    new_output_state = True
                    new_system_state = SystemState.APP_OVERRIDE
                    reason_on = StateReasonOn.APP_MODE_ON
                if self.app_mode == AppMode.OFF:
                    new_output_state = False
                    new_system_state = SystemState.APP_OVERRIDE
                    reason_off = StateReasonOff.APP_MODE_OFF

        # Otherwise see if the Input switch has overridden our state. Only allow changes if the device is online
        if new_output_state is None and is_device_online:
            input_state = self._get_input_state(view)
            if isinstance(input_state, bool):
                if input_state and self.device_input_mode == InputMode.TURN_ON:
                    new_output_state = True
                    new_system_state = SystemState.INPUT_OVERRIDE
                    reason_on = StateReasonOn.INPUT_SWITCH_ON
                if not input_state and self.device_input_mode == InputMode.TURN_OFF:
                    new_output_state = False
                    new_system_state = SystemState.INPUT_OVERRIDE
                    reason_off = StateReasonOff.INPUT_SWITCH_OFF

        # Issue 102: Check to see if output is disabled
        if new_output_state is None and self.output_config.get("Disable", False):
            new_output_state = False
            new_system_state = SystemState.DISABLED
            reason_off = StateReasonOff.DISABLED

        # Issue 66: See if the UPS health status has overridden our state. Only allow changes if the device is online
        if new_output_state is None and is_device_online:
            ups_mode: UPSMode = self.output_constraint.get_ups_health_status()  # type: ignore[union-attr]
            if ups_mode == UPSMode.TURN_ON:
                new_output_state = True
                new_system_state = SystemState.UPS_OVERRIDE
                reason_on = StateReasonOn.UPS_UNHEALTHY
            if ups_mode == UPSMode.TURN_OFF:
                new_output_state = False
                new_system_state = SystemState.UPS_OVERRIDE
                reason_off = StateReasonOff.UPS_UNHEALTHY
            # if ups_mode == UPSMode.AUTO then we just fall through to the other checks as UPS is not currently dictating the state

        # No overrides set, now see if no run today
        if new_output_state is None and self.output_constraint.is_today_excluded():  # type: ignore[union-attr]
            new_output_state = False
            new_system_state = SystemState.DATE_OFF
            reason_off = StateReasonOff.DATE_OFF

        # Issue 103: See if a weather constraint has overridden our state. Only allow changes if the device is online
        if new_output_state is None and is_device_online:
            weather_mode: WeatherMode = self.output_constraint.get_weather_constraint_status()  # type: ignore[union-attr]
            if weather_mode == WeatherMode.TURN_ON:
                new_output_state = True
                new_system_state = SystemState.WEATHER_OVERRIDE
                reason_on = StateReasonOn.WEATHER_OVERRIDE
            if weather_mode == WeatherMode.TURN_OFF:
                new_output_state = False
                new_system_state = SystemState.WEATHER_OVERRIDE
                reason_off = StateReasonOff.WEATHER_OVERRIDE
            # if weather_mode == WeatherMode.AUTO then we just fall through to the other checks as weather is not currently dictating the state

        # If new_output_state hasn't been set at this point, we're in auto mode
        if new_output_state is None:
            new_system_state = SystemState.AUTO
            if not is_device_online:
                # Device is offline, so we have to remain off
                new_output_state = False
                reason_off = StateReasonOff.DEVICE_OFFLINE
            elif not self.run_plan or self.run_plan["Status"] == RunPlanStatus.FAILED:
                # We don't have a run plan, generally an error condition
                new_output_state = False
                reason_off = StateReasonOff.NO_RUN_PLAN
            elif self.run_plan["Status"] == RunPlanStatus.NOTHING:
                # We have a valid run plan but there's nothing left to do today
                new_output_state = False
                reason_off = StateReasonOff.RUN_PLAN_COMPLETE
            elif self.run_plan["Status"] in {RunPlanStatus.PARTIAL, RunPlanStatus.BELOW_MINIMUM, RunPlanStatus.READY}:
                # See if we need to honor the maxium off time
                if self._should_respect_maximum_offtime(view):
                    new_output_state = True
                    reason_on = StateReasonOn.MAX_OFF_TIME
                else:
                    # We have a complete or partially filled run plan
                    _, run_now = RunPlanner.get_current_slot(self.run_plan)
                    if run_now:
                        # Run plan tells us to run now.
                        new_output_state = True
                        reason_on = StateReasonOn.ACTIVE_RUN_PLAN
                    else:
                        # Run plan tells us to run later.
                        new_output_state = False
                        reason_off = StateReasonOff.INACTIVE_RUN_PLAN

        # If we get here and we still haven't determined the new system state, there's a problem.
        if not isinstance(new_system_state, SystemState):
            self.logger.log_fatal_error(f"Unable to determine new system state for {self.name}.")
            return None
        assert isinstance(new_system_state, SystemState)
        # If we get here and we still haven't determined the new output state, there's a problem.
        if not isinstance(new_output_state, bool):
            self.logger.log_fatal_error(f"Unable to determine new output state for {self.name}.")
            return None
        assert isinstance(new_output_state, bool)

        if new_output_state and reason_on is None:
            self.logger.log_fatal_error(f"Output {self.name} state evaluates to On but reason_on not set.")
        elif not new_output_state and reason_off is None:
            self.logger.log_fatal_error(f"Output {self.name} state evaluates to Off but reason_off not set.")

        # If we're proposing to turn on and the system_state is AUTO, then make sure our parent output allows this
        if new_system_state == SystemState.AUTO and new_output_state and self.parent_output:
            is_parent_device_output_on = view.get_output_state(self.parent_output.device_output_id)
            if not is_parent_device_output_on:
                # The output of the parent device is off, so we have to remain off
                new_output_state = False
                reason_off = StateReasonOff.PARENT_OFF

        # If the system_state is AUTO, then make sure we don't have any temp probe constraints
        if new_system_state == SystemState.AUTO:
            is_constraint, override_state = self.output_constraint.are_there_temp_probe_constraints(view, new_output_state)  # type: ignore[union-attr]
            if is_constraint:
                if override_state:  # A temp probes requires us to stay on even though we would otherwise be off
                    new_output_state = True
                    reason_on = StateReasonOn.TEMP_PROBE_CONSTRAINT
                else:     # A temp probes requires us to stay off even though we would otherwise be on
                    new_output_state = False
                    reason_off = StateReasonOff.TEMP_PROBE_CONSTRAINT

        # Check minimum runtime constraints before applying changes
        if new_system_state == SystemState.AUTO and self._should_respect_minimum_runtime(new_output_state, view):
            if is_device_output_on:
                self.reason = StateReasonOn.MIN_ON_TIME
                self.print_to_console(f"Output {self.name} has been ON for less than MinOnTime of {self.min_on_time} minutes. Will remain ON until minimum time has elapsed.")
            else:
                self.reason = StateReasonOff.MIN_OFF_TIME
                self.print_to_console(f"Output {self.name} has been OFF for less than MinOffTime of {self.min_off_time} minutes. Will remain OFF until minimum time has elapsed.")
        else:
            # And finally we're ready to apply our changes
            if new_output_state:
                if not is_device_online:
                    self.logger.log_message(f"Device {self.device_name} is offline, cannot turn on output {self.device_output_name} _turn_on() should not have been called.", "error")
                    return None

                action = self.formulate_output_sequence(system_state=new_system_state,
                                                        reason=reason_on,     # pyright: ignore[reportArgumentType]
                                                        output_state=new_output_state,
                                                        view=view,
                                                        output_sequences=output_sequences,
                                                        on_complete=on_complete)
            else:
                action = self.formulate_output_sequence(system_state=new_system_state,
                                                        reason=reason_off,    # pyright: ignore[reportArgumentType]
                                                        output_state=new_output_state,
                                                        view=view,
                                                        output_sequences=output_sequences,
                                                        on_complete=on_complete)
            if new_output_state != is_device_output_on:
                self.print_to_console(f"Output {self.name} requesting action {action.type.value} because {action.reason.value}.")
            else:
                self.logger.log_message(f"No action required for output {self.name}. Current state is {'ON' if is_device_output_on else 'OFF'}", "all")
            return action

        self.logger.log_message(f"No action required for output {self.name}. Current state is {'ON' if is_device_output_on else 'OFF'}", "all")
        return None

    def set_parent_output(self, parent_output):
        """Sets the parent output for this output manager.

        Args:
            parent_output (OutputManager): The parent output manager.
        """
        if not parent_output:
            self.parent_output = None
            return

        # First make sure we're not setting ourselves as our own parent
        if parent_output.name == self.name:
            self.logger.log_fatal_error(f"Output {self.name} cannot be its own parent.")
            return

        # Log warnings if the threshold values don't make sense
        if self.min_hours > parent_output.min_hours:
            self.logger.log_message(f"Output {self.name} has MinHours greater than its parent output {parent_output.name}.", "warning")
        if self.max_hours > parent_output.max_hours:
            self.logger.log_message(f"Output {self.name} has MaxHours greater than its parent output {parent_output.name}.", "warning")

        self.parent_output = parent_output

    def set_app_mode(self, new_mode: AppMode, view: SmartDeviceView, revert_minutes: int | None = None):
        """Sets the app mode for this output manager.

        Args:
            new_mode (AppMode): The new app mode.
            view (SmartDeviceView): The current view of the smart devices.
            revert_minutes (int | None): Optional number of minutes after which to revert to AUTO mode.
        """
        if new_mode not in AppMode:
            self.logger.log_message(f"Invalid AppMode {new_mode} for output {self.name}.", "error")
            return

        if not view.get_device_online(self.device_id):
            self.logger.log_message(f"Cannot set AppMode to {new_mode.value} for output {self.name} because the device is offline.", "warning")
            return

        if new_mode != self.app_mode:
            # If we're setting to ON or OFF mode, set the revert time if specified
            if new_mode in {AppMode.ON, AppMode.OFF}:
                if revert_minutes and revert_minutes > 0:
                    self.app_mode_revert_time = DateHelper.add_datetime(DateHelper.now(), minutes=revert_minutes)
                else:
                    self.app_mode_revert_time = None
            else:
                self.app_mode_revert_time = None

            self.app_mode = new_mode
            self.logger.log_message(f"Output {self.name} app mode changed to {self.app_mode.value}.", "debug")
            if self.app_mode_revert_time:
                self.logger.log_message(f"Output {self.name} app mode will revert to AUTO at {self.app_mode_revert_time.strftime('%Y-%m-%d %H:%M:%S')}.", "debug")
            # If the app mode has changed, we need to re-evaluate our state
            self.evaluate_conditions(view)

    def formulate_output_sequence(self,
                                  system_state: SystemState,
                                  reason: StateReasonOn | StateReasonOff,
                                  output_state: bool,
                                  view: SmartDeviceView,
                                  output_sequences: dict[str, DeviceSequenceRequest] | None = None,
                                  on_complete: Callable[[DeviceSequenceResult], None] | None = None) -> OutputAction:
        """Formulate the output action sequence to change the output state.

        Args:
            system_state (SystemState): The desired new system state.
            reason (StateReasonOn | StateReasonOff): The reason for the state change.
            output_state (bool): The desired new state of the output (True for ON, False for OFF).
            view (SmartDeviceView): The current view of the smart devices.
            output_sequences (dict[str, DeviceSequenceRequest] | None): Optional dictionary of the available output sequences.
            on_complete (Callable[[DeviceSequenceResult], None] | None): Optional callback to be called when the sequence is complete.

        Returns:
            OutputAction: The formulated output action.
        """
        is_device_online = view.get_device_online(self.device_id)
        is_device_output_on = view.get_output_state(self.device_output_id)

        # See if there's a predefined output sequence for this action
        if output_state:  # Turn the output ON
            sequence_key = self.output_config.get("TurnOnSequence")
        else:  # Turn the output OFF
            sequence_key = self.output_config.get("TurnOffSequence")

        # If we have a valid configured sequence, use it
        if output_sequences and sequence_key and sequence_key in output_sequences:
            sequence_request = output_sequences[sequence_key]
        else:   # Otherwise, create a simple change output step
            steps = [
                DeviceStep(StepKind.CHANGE_OUTPUT, {"output_identity": self.device_output_id, "state": output_state}, retries=2, retry_backoff_s=1.0),
            ]
            label = f"Change output {self.device_output_name} to {output_state}"
            sequence_request = DeviceSequenceRequest(
                steps=steps,
                label=label,
                timeout_s=10.0,
                on_complete=on_complete
            )

        if output_state:  # Turn the output ON
            action = OutputAction(
                worker_request_id=None,
                request=sequence_request,
                type=OutputActionType.TURN_ON,
                system_state=system_state,
                reason=reason)

            # if the output is already turned on, just record the reason change
            if is_device_output_on:
                action.type = OutputActionType.UPDATE_ON_STATE

        else:  # Turn the output OFF
            action = OutputAction(
                worker_request_id=None,
                request=sequence_request,
                type=OutputActionType.TURN_OFF,
                system_state=system_state,
                reason=reason)

            # if the output is already off or the device is offline, just record the reason change
            if not is_device_output_on or not is_device_online:
                action.type = OutputActionType.UPDATE_OFF_STATE

        return action

    def record_action_request(self, action: OutputAction):
        """Records a requested output action.

        Args:
            action (OutputAction): The requested output action.
        """
        self._output_action_request = action

    def get_action_request(self) -> OutputAction | None:
        """Gets the requested output action.

        Returns:
            OutputAction | None: The requested output action, or None if no action is requested.
        """
        return self._output_action_request

    def action_request_failed(self, error_message: str):
        """Handles a failed output action request.

        Args:
            error_message (str): The error message.

        Raises:
            RuntimeError: If no action request is recorded.
        """
        if not self._output_action_request:
            exception_msg = f"Output {self.name} action_request_failed() called but no action request is recorded."
            raise RuntimeError(exception_msg)

        full_msg = f"Action {self._output_action_request.type} for output {self.device_output_name} failed: {error_message}"
        self.logger.log_message(full_msg, "error")

        if self.report_critical_errors_delay:
            assert isinstance(self.report_critical_errors_delay, int)
            self.logger.report_notifiable_issue(entity=f"Output {self.device_output_name}", issue_type="Action Request Failed", send_delay=self.report_critical_errors_delay * 60, message=full_msg)  # pyright: ignore[reportArgumentType]

        self.clear_action_request()

    def clear_action_request(self):
        """Clears the requested output action."""
        self._output_action_request = None

    def record_action_complete(self, action: OutputAction, view: SmartDeviceView):
        """Records the completion of an output action.

        Args:
            action (OutputAction): The completed output action.
            view (SmartDeviceView): The current view of the smart devices.
        """
        self.clear_action_request()

        if action.type == OutputActionType.TURN_ON:
            self.last_turned_on = DateHelper.now()
            self.logger.log_message(f"Output {self.name} state changed to ON - {action.reason}.", "detailed")

        if action.type in {OutputActionType.TURN_ON, OutputActionType.UPDATE_ON_STATE} and (self.system_state != action.system_state or self.reason != action.reason):
            self.system_state = action.system_state
            self.reason = action.reason
            self.last_changed = DateHelper.now()
            data_block = self._get_status_data(view)
            self.run_history.start_run(self.system_state, self.reason, data_block)  # pyright: ignore[reportArgumentType]

            current_run = self.run_history.get_current_run()
            if current_run:
                self.print_to_console(f"Output {self.name} ON - {action.reason}. Started at {current_run['StartTime'].strftime('%H:%M:%S')} Energy Used: {current_run['EnergyUsed']:.2f}Wh Average Price: {current_run['AveragePrice']:.2f}{self.currency_minor_symbol}/kWh Total Cost: {self.currency_major_symbol}{current_run['TotalCost']:.4f}")

        if action.type == OutputActionType.TURN_OFF:
            self.last_turned_off = DateHelper.now()
            self.logger.log_message(f"Output {self.name} state changed to OFF - {action.reason}.", "detailed")

        if action.type in {OutputActionType.TURN_OFF, OutputActionType.UPDATE_OFF_STATE} and (self.system_state != action.system_state or self.reason != action.reason):
            self.system_state = action.system_state
            self.reason = action.reason
            self.last_changed = DateHelper.now()
            data_block = self._get_status_data(view)
            self.run_history.stop_run(action.reason, data_block)  # pyright: ignore[reportArgumentType]

            self.print_to_console(f"Output {self.name} OFF - {action.reason}")

    def shutdown(self, view: SmartDeviceView) -> bool:
        """Shutdown the output manager.

        Args:
            view (SmartDeviceView): The current view of the smart devices.

        Returns:
            bool: True if the output device needs to be turned off, False otherwise.
        """
        return bool(self.output_config.get("StopOnExit", False) and view.get_device_online(self.device_id) and view.get_output_state(self.device_output_id))

    def get_info(self, view: SmartDeviceView | None = None) -> str:
        """Print the information of the output.

        Args:
            view (SmartDeviceView | None): The current view of the smart devices.

        Returns:
            str: The formatted output information.
        """
        device_output_state = ("ON" if view.get_output_state(self.device_output_id) else "OFF") if view else "Unknown"
        current_day = self.run_history.get_current_day()
        return_str = f"{self.name} Output Information:\n"
        return_str += f"   - System State: {self.system_state}, reason: {self.reason} (since {self.last_changed.strftime('%H:%M:%S') if self.last_changed else 'N/A'})\n"
        return_str += f"   - Device Output: {self.device_output_name}, currently {device_output_state}\n"
        return_str += f"   - Device Scheduling Mode: {self.device_mode}\n"
        return_str += f"   - Schedule: {self.schedule_name}\n"
        return_str += f"   - Amber Channel: {self.amber_channel}\n"
        return_str += f"   - Min Hours: {self.min_hours}, Max Hours: {self.max_hours}, Target Hours: {self._get_target_hours()}\n"
        return_str += f"   - Max Best Price: {self.max_best_price}{self.currency_minor_symbol}/kWh, Max Priority Price: {self.max_priority_price}{self.currency_minor_symbol}/kWh\n"
        return_str += f"   - Actual hours today: {self.run_history.get_actual_hours():.2f}\n"
        if self.run_plan:
            return_str += f"   - Today's run plan requires {self.run_plan.get('RequiredHours', 0):.2f} hours:\n"
            for entry in self.run_plan.get("RunPlan", []):
                return_str += f"      - From {entry['StartTime'].strftime('%H:%M')} to {entry['EndTime'].strftime('%H:%M')}. Price: {entry['Price']:.2f}{self.currency_minor_symbol}/kWh, Cost: {self.currency_major_symbol}{entry['EstimatedCost']:.2f}\n"
            return_str += f"      - Planned Hours: {self.run_plan.get('PlannedHours', 0):.2f}, Estimated Cost: {self.currency_major_symbol}{self.run_plan.get('EstimatedCost', 0):.2f}\n"
        dates_off = self.output_constraint.get_dates_off() if self.output_constraint else []
        if dates_off:
            return_str += "   - Dates off:\n"
            for date_range in dates_off:
                return_str += f"      - From {date_range.get('StartDate')} to {date_range.get('EndDate')}\n"
        return_str += f"   - Device Meter: {self.device_meter_name}, Energy Used today: {current_day['EnergyUsed'] if current_day else 0:.2f} kWh\n"
        return_str += f"   - Device Input: {self.device_input_name} (mode: {self.device_input_mode})\n"
        return_str += f"   - Parent Output: {self.parent_output_name}\n"

        return return_str

    def get_schedule(self) -> dict | None:
        """Get the schedule for this output.

        Returns:
            dict: The schedule or None if none assigned.
        """
        return self.schedule

    def get_daily_usage_data(self, name: str | None = None) -> list[dict]:
        """Get the consumption data for this output.

        Args:
            name (str | None): Optional name for the data set.

        Returns:
            list[dict]: The list of consumption data points.
        """
        # Return None if we don't have a device meter assigned
        if not self.device_meter_id:
            return []

        if self.run_history:
            return self.run_history.get_daily_usage_data(name)
        return []

    def get_days_of_history(self) -> int:
        """Get the number of days of history stored.

        Returns:
            int: The number of days of history.
        """
        if self.run_history:
            return self.run_history.get_days_of_history()
        return 14

    def print_to_console(self, message: str):
        """Print a message to the console if PrintToConsole is enabled.

        Args:
            message (str): The message to print.
        """
        if self.config.get("General", "PrintToConsole", default=False):
            print(message)

        self.logger.log_message(message, "debug")

    def run_self_tests(self, is_new_day: bool):
        """Run self tests on the output manager.

        Args:
            is_new_day (bool): Indicates if it's a new day.
        """
        pass  # Currently no self tests defined

    def get_api_data(self, view: SmartDeviceView | None = None, display_name: str | None = None) -> dict:
        """Get the data for API output.

        Args:
            view (SmartDeviceView | None): Optional view of the smart devices.
            display_name (str | None): Optional display name for the output.

        Returns:
            dict: The data for API output.
        """
        is_on = None
        if view:
            is_on = view.get_output_state(self.device_output_id)

        next_change_dt = None
        if is_on:
            next_change_dt = self.run_plan.get("NextStopDateTime") if self.run_plan else None
        elif is_on is not None:
            next_change_dt = self.run_plan.get("NextStartDateTime") if self.run_plan else None

        return {
            "Name": self.name,
            "DisplayName": display_name or self.name,
            "Type": self.type,
            "AppMode": self.app_mode.value,
            "State": ("ON" if is_on else "OFF") if view else "Unknown",
            "NextChange": next_change_dt,
            "SystemState": str(self.system_state),
            "Reason": str(self.reason) if self.reason else None,
            "LastChanged": self.last_changed,
            "OutputName": self.device_output_name,
            "DeviceMode": str(self.device_mode) if self.device_mode else None,
            "ScheduleName": self.schedule_name,
            "AmberChannel": self.amber_channel,
            "MinHours": self.min_hours,
            "MaxHours": self.max_hours,
            "TargetHours": self._get_target_hours(),
            "PlannedHours": self.run_plan.get("PlannedHours") if self.run_plan else None,
            "MaxBestPrice": self.max_best_price,
            "MaxPriorityPrice": self.max_priority_price,
            "ActualHoursToday": self.run_history.get_actual_hours() if self.run_history else 0.0,
            "RunPlan": self.run_plan.get("RunPlan") if self.run_plan else None,
            "EnergyUsage": {
                "1hr": self.run_history.get_energy_usage(1) if self.run_history else None,
                "24hr": self.run_history.get_energy_usage(24) if self.run_history else None
            },
        }

    # Private Functions ===========================================================================
    def _should_revert_app_override(self, view: SmartDeviceView) -> bool:
        """Check if we should revert an app override based on device online status.

        Args:
            view (SmartDeviceView): The current view of the smart devices.

        Returns:
            bool: True if we should revert the app override, False otherwise.
        """
        is_device_online = view.get_device_online(self.device_id)

        # If the device is offline, we cannot revert the app override
        if not is_device_online:
            return False

        passed_revert_time = False
        time_now = DateHelper.now()
        if self.app_mode_revert_time and time_now >= self.app_mode_revert_time:
            passed_revert_time = True
        if self.app_mode == AppMode.ON and passed_revert_time and self.system_state == SystemState.APP_OVERRIDE and self.reason == StateReasonOn.APP_MODE_ON:
            assert isinstance(self.last_turned_on, dt.datetime)
            time_on = (time_now - self.last_turned_on).total_seconds() / 60  # minutes
            self.logger.log_message(f"Reverting from AppMode ON to AUTO for output {self.name} after {time_on:.1f} minutes", "debug")
            return True
        if self.app_mode == AppMode.OFF and passed_revert_time and self.system_state == SystemState.APP_OVERRIDE and self.reason == StateReasonOff.APP_MODE_OFF:
            assert isinstance(self.last_turned_off, dt.datetime)
            time_off = (time_now - self.last_turned_off).total_seconds() / 60  # minutes
            self.logger.log_message(f"Reverting from AppMode OFF to AUTO for output {self.name} after {time_off:.1f} minutes", "debug")
            return True

        return False

    def _should_respect_minimum_runtime(self, proposed_state: bool, view: SmartDeviceView) -> bool:
        """Check if we should delay state change due to minimum runtime constraints.

        Args:
            proposed_state (bool): The proposed new state of the output (True for ON, False for OFF).
            view (SmartDeviceView): The current view of the smart devices.

        Returns:
            bool: True if we should delay the state change, False otherwise.
        """
        now = DateHelper.now()
        is_device_online = view.get_device_online(self.device_id)
        is_device_output_on = view.get_output_state(self.device_output_id)

        # If proposing to turn OFF but haven't met minimum ON time
        if (
            not proposed_state
            and is_device_output_on
            and self.min_on_time > 0
            and self.last_turned_on
            and is_device_online
        ):
            time_on = (now - self.last_turned_on).total_seconds() / 60  # minutes
            if time_on < self.min_on_time:
                remaining = self.min_on_time - time_on
                self.logger.log_message(
                    f"Output {self.name} must stay on for {remaining:.1f} more minutes "
                    f"(MinOnTime: {self.min_on_time})", "debug"
                )
                return True

        # If proposing to turn ON but haven't met minimum OFF time
        if (
            proposed_state
            and not is_device_output_on
            and self.min_off_time > 0
            and self.last_turned_off
            and is_device_online
        ):
            time_off = (now - self.last_turned_off).total_seconds() / 60  # minutes
            if time_off < self.min_off_time:
                remaining = self.min_off_time - time_off
                self.logger.log_message(
                    f"Output {self.name} must stay off for {remaining:.1f} more minutes "
                    f"(MinOffTime: {self.min_off_time})", "debug"
                )
                return True

        return False

    def _should_respect_maximum_offtime(self, view: SmartDeviceView) -> bool:
        """Check if we should turn an output back on due to maximum off time constraints.

        Args:
            view (SmartDeviceView): The current view of the smart devices.

        Returns:
            bool: True if we should turn the output back on due to maximum off time constraints, False otherwise.
        """
        now = DateHelper.now()
        is_device_online = view.get_device_online(self.device_id)
        is_device_output_on = view.get_output_state(self.device_output_id)

        # If device is currently OFF and has exceeded maximum off time, we need to turn it back ON
        if (
            not is_device_output_on
            and self.max_off_time > 0
            and self.last_turned_off
            and is_device_online
        ):
            time_off = (now - self.last_turned_off).total_seconds() / 60  # minutes
            if time_off > self.max_off_time:
                self.logger.log_message(
                    f"Output {self.name} has been off for more than the maximum off time of {self.max_off_time} minutes. Turning it back on.", "debug"
                )
                return True

        return False

    @staticmethod
    def _get_current_thread_name() -> str:
        """Get the name of the current thread.

        Returns:
            str: The name of the current thread.
        """
        return threading.current_thread().name

    def _new_runplan_needed(self, view: SmartDeviceView) -> bool:
        """See if we need to regenerate the run plan.

        Returns:
            bool: True if we need a new run plan, False otherwise.
        """
        # If the device is currently offline, we can't generate a run plan
        if not view.get_device_online(self.device_id):
            return False

        # Issue 102: Check to see if output is disabled
        if self.output_config.get("Disable", False):
            return False

        is_device_output_on = view.get_output_state(self.device_output_id)

        # If some other task has already invalidated the run plan, we need to regenerate it.
        # Also if we don't have a run plan at all
        if self.invalidate_run_plan or not self.run_plan:
            return True

        # If we have a plan but it's complete and there's nothing left to do, we don't need a new plan
        if self.run_plan["Status"] == RunPlanStatus.NOTHING:
            return False

        # See if we're running in a current slot
        current_slot, running_now = RunPlanner.get_current_slot(self.run_plan)

        # If we we're currently in an active run plan slot and the current price has risen significantly, we need a new plan
        if running_now and self.device_mode == RunPlanMode.BEST_PRICE:
            current_price = self.pricing.get_current_price(self.amber_channel)
            if not self.last_price or current_price > self.last_price * 1.1:    # Price has risen by 10% or more
                self.last_price = current_price
                return True

        # Output is on but the run plan is inactive, so we've just gone out of a run plan slot
        if not current_slot and is_device_output_on and self.system_state == SystemState.AUTO:
            return True

        # Check if it's been a while since we last checked the run plan
        if DateHelper.now() >= self.next_run_plan_check:  # noqa: SIM103
            return True

        return False

    def _get_input_state(self, view: SmartDeviceView) -> bool | None:
        """Get the current state of the input device if it exists.

        Returns:
            bool | None: The state of the input device (True for ON, False for OFF), or None if no input device is configured.
        """
        if not self.device_input_id:
            return None
        if not view.get_device_online(self.device_id):
            return None
        return view.get_input_state(self.device_input_id)

    def _get_target_hours(self, for_date: dt.date | None = None) -> float | None:
        """Returns the target hours for the given date.

        Args:
            for_date (dt.date | None): The date for which to retrieve the target hours. Defaults to today.

        Returns:
            float: The target hours for the given date. None if we are in ALL_DAY mode.
        """
        if self.run_plan_target_mode == RunPlanTargetHours.ALL_HOURS:
            return None
        if not for_date:
            for_date = DateHelper.today()

        month = for_date.strftime("%B")

        monthly_target_hours = self.output_config.get("MonthlyTargetHours")
        if monthly_target_hours is not None and month in monthly_target_hours:
            target_hours = monthly_target_hours.get(month)
        else:
            target_hours = self.output_config.get("TargetHours", 8.0)

        # Make sure we haven't exceeded our max hours
        target_hours = min(target_hours, self.max_hours)

        return target_hours

    def _get_current_price(self) -> float:
        """Get the current price from either PricingManager or ScheduleManager depending on the context.

        Returns:
            float: The current price.
        """
        price = None
        if self.device_mode == RunPlanMode.BEST_PRICE:
            price = self.pricing.get_current_price(self.amber_channel)

        if self.device_mode == RunPlanMode.SCHEDULE or not price:
            # Scheduler will always return a price
            price = self.scheduler.get_current_price(self.schedule)  # pyright: ignore[reportArgumentType]
        return price

    def _get_status_data(self, view: SmartDeviceView) -> OutputStatusData:
        """Get the status data needed by RunHistory.

        Returns:
            data_block(OutputStatusData)
        """
        status_data = OutputStatusData(
            meter_reading=(view.get_meter_energy(self.device_meter_id) or 0) if self.device_meter_id else 0,
            power_draw=(view.get_meter_power(self.device_meter_id) or 0) if self.device_meter_id else 0,
            is_on=view.get_output_state(self.device_output_id),
            target_hours=self._get_target_hours(),
            current_price=self._get_current_price(),
            output_type="smart device",
            expect_offline=view.get_device_expect_offline(self.device_id),
            min_hours=self.min_hours,
            send_min_hours_alerts = self.output_config.get("MinHoursAlert") or False
        )

        return status_data
