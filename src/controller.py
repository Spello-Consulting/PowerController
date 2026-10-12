"""The PowerController class that orchestrates power management."""
import contextlib
import copy
import datetime as dt
import json  # noqa: F401
import queue
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from threading import Event, RLock
from typing import Any

from org_enums import AppMode, StateReasonOff, SystemState
from sc_foundation import (
    CSVReader,
    DateHelper,
    JSONEncoder,
    SCCommon,
    SCConfigManager,
    SCLogger,
)

from sc_smart_device import (
    DeviceSequenceRequest,  
    DeviceSequenceResult,   
    DeviceStep,             
    SmartDeviceView,        
    SmartDeviceWorker,      
    StepKind,               
    STEP_TYPE_MAP,          
)


from config_schemas import ConfigSchema
from external_services import ExternalServiceHelper
from helpers import get_currency_symbols
from local_enumerations import (
    DUMP_SMART_DEVICE_SNAPSHOT,
    SCHEMA_VERSION,
    AmberChannel,
    Command,
    OutputAction,
    OutputActionType,
)
from meter_output import MeterOutput
from outputs import OutputManager
from pricing import PricingManager
from scheduler import Scheduler
try:
    from teslamate import (
        TESLAMATE_AVAILABLE,
        get_charging_data_as_dict,
        merge_bucket_dict_records,
        merge_session_dict_records,
    )
    from teslamate_output import TeslaMateOutput # pyright: ignore[reportAssignmentType]
except ImportError:
    TESLAMATE_AVAILABLE = False

    class TeslaMateOutput:  # type: ignore[no-redef]
        """Stub used when the teslamate optional dependency is not installed."""
from ups_integration import UPSIntegration
from weather_integration import WeatherIntegration


class LookupMode(StrEnum):
    """Lookup mode used for PowerController._find_output()."""
    ID = "id"
    NAME = "name"
    OUTPUT = "output"
    METER = "meter"
    INPUT = "input"


TRIM_LOGFILE_INTERVAL = dt.timedelta(hours=2)


class PowerController:
    """The PowerController class that orchestrates power management."""

    # Public Functions ============================================================================
    def __init__(self, config: SCConfigManager, logger: SCLogger, smart_device_worker: SmartDeviceWorker, wake_event: Event):
        """Initializes the PowerController.

        Args:
            config (SCConfigManager): The configuration manager for the system.
            logger (SCLogger): The logger for the system.
            smart_device_worker: The SmartDeviceWorker obeject that we use to interface to SCSmartDevice
            wake_event (Event): The event used to wake the controller.
        """
        self.config = config
        self.last_config_check = DateHelper.now()
        self.logger = logger
        self.logger_last_trim: dt.datetime | None = None
        self.external_service_helper = ExternalServiceHelper(config, logger)
        self.viewer_website_last_post = None
        self.wake_event = wake_event
        self.smart_device_worker: SmartDeviceWorker = smart_device_worker
        self.cmd_q: queue.Queue[Command] = queue.Queue()    # Used to post commands into the controller's loop
        self.command_pending: bool = False
        self.report_critical_errors_delay = config.get("General", "ReportCriticalErrorsDelay", default=None)
        if isinstance(self.report_critical_errors_delay, (int, float)):
            self.report_critical_errors_delay = round(self.report_critical_errors_delay, 0)
        else:
            self.report_critical_errors_delay = None

        # Setup the environment
        self.outputs = []   # List of output state managers, each one a OutputStateManager object.
        self.poll_interval = 10.0
        self.last_tick_time = DateHelper.now()
        self._smart_device_sequence_requests: dict[str, DeviceSequenceRequest] = {}

        # Temp probe logging
        self.temp_probe_logging = {}
        self.temp_probe_history: list[dict] = []

        # TeslaMate charging data import
        self.tesla_import_enabled: bool = False
        self.save_tesla_raw_data: bool = False
        self.tesla_last_import_query: dt.datetime | None = None
        self.tesla_charge_data_days_of_history: int = 14    # This will be the max of all the configured teslamate outputs
        self.tesla_charge_data: dict = {
            "last_import": None,
            "sessions": [],
            "buckets": [],
        }

        # UPS Integration
        self.ups_integration = UPSIntegration(config, logger)

        # Weather Integration
        self.weather_integration = WeatherIntegration(config, logger)

        # DataAPI related
        self.data_api_data: dict = {}   # Safe copy of the latest data
        self._data_api_lock = threading.Lock()  # Thread safe lock to prevent concurrent access to the data_api_data
        self._data_api_config: dict = {}
        self._data_api_next_refresh: dt.datetime = DateHelper.now()

        # Metered Output usage data
        self.output_metering: dict = {}   # To be updated by self._update_system_state_usage_data()

        # Create the two run_planner types
        self.scheduler = Scheduler(self.config, self.logger)
        self.pricing = PricingManager(self.config, self.logger)
        self.currency_major_symbol, self.currency_minor_symbol = get_currency_symbols(config)

        self._io_shutdown_lock = RLock()  # Serialize state/CSV writes during shutdown

        # Optional callback used to notify the webapp (WebSocket) layer that a new snapshot should be pushed.
        # This is set by main.py once the ASGI webapp is initialised.
        self._webapp_notify: Callable[[], None] | None = None
        self._last_webapp_notify: dt.datetime | None = None

        self._initialise(startup_mode=True)
        self.update_device_locations = True

    def set_webapp_notifier(self, notify: Callable[[], None] | None) -> None:
        """Register a callback invoked when the webapp should push a new snapshot."""
        self._webapp_notify = notify

    def get_webapp_data(self) -> dict:
        """Returns a dict object with a snapshot of the current state of all outputs.

        Returns:
            dict: The state snapshot containing theoutputs.
        """
        loop_count = 0
        while self._have_pending_commands() and loop_count < 10:
            time.sleep(0.1)  # Small delay to let the commands be processed
            loop_count += 1

        view = self._get_latest_status_view()

        temp_probe_data = []
        if self.temp_probe_logging.get("enabled", False):
            for probe in self.temp_probe_logging.get("probes", []):
                temp_probe_data.append({
                    "name": probe.get("DisplayName", probe.get("Name", "Unknown")),
                    "temperature": probe.get("Temperature"),
                    "last_logged_time": probe.get("LastReadingTime").strftime("%H:%M") if probe.get("LastReadingTime") else "",
                })

        # Now build the snapshot
        global_data = {
            "AppLabel": self.app_label,
            "PollInterval": self.webapp_refresh,
            "TempProbeData": temp_probe_data,
        }

        # outputs_data = {
        #     output.id: output.get_webapp_data(view)
        #     for output in self.outputs
        # }

        outputs_data = {
        }
        for output in self.outputs:
            output_data = output.get_webapp_data(view)
            if output_data:
                outputs_data[output.id] = output_data

        return_dict = {
            "global": global_data,
            "outputs": outputs_data,
        }

        # self.logger.log_message("Generated webapp data snapshot.", "debug")
        # self.logger.log_message(json.dumps(return_dict), "debug")

        return return_dict

    def is_valid_output_id(self, output_id: str) -> bool:
        """Check if the output ID is valid.

        Args:
            output_id (str): The output ID to check.

        Returns:
            bool: True if the output ID is valid, False otherwise.
        """
        if not isinstance(output_id, str):
            return False

        return any(output.id == output_id for output in self.outputs)

    def post_command(self, cmd: Command) -> None:
        """Post a command to the controller from the web app."""
        self.cmd_q.put(cmd)
        self.command_pending: bool = True
        self.wake_event.set()

    def set_wake_event(self, seq_result: DeviceSequenceRequest) -> None:
        """Set the wake event to wake the controller loop."""
        self.logger.log_message(f"Waking power controller loop - Smart Device Sequence {seq_result.id} has finished.", "debug")
        self.wake_event.set()

    def run(self, stop_event: Event):
        """The main loop of the power controller.

        Args:
            stop_event (Event): The event used to stop the controller.
        """
        self.logger.log_message("Power controller starting main control loop.", "detailed")

        while not stop_event.is_set():
            time_now = DateHelper.now()
            is_new_day = self.last_tick_time.date() != time_now.date()
            console_msg = f"Main tick at {time_now.strftime('%H:%M:%S')}"
            if is_new_day:
                console_msg += " - New day!"
            self.print_to_console(console_msg)

            # Run self tests if in testing mode
            self.run_self_tests(is_new_day)

            force_refresh = self._run_scheduler_tick(is_new_day)
            # Push updates periodically and immediately after commands.
            self._maybe_notify_webapp(force=force_refresh)
            self.wake_event.clear()

            # Update the last tick time after the tick is complete
            self.last_tick_time = time_now

            self.wake_event.wait(timeout=self.poll_interval)

        self.shutdown()

    def shutdown(self):
        """Shutdown the power controller, turning off outputs if configured to do so."""
        with self._io_shutdown_lock:
            self.logger.log_message("Starting PowerController shutdown...", "debug")
            view = self._get_latest_status_view()
            for output in self.outputs:
                if output.shutdown(view):
                    self._force_output_off(output, view)

            view = self._get_latest_status_view()
            self._save_system_state(view, force_post=True)
            self.logger.log_message("PowerController shutdown complete.", "detailed")

    def print_to_console(self, message: str):
        """Print a message to the console if PrintToConsole is enabled.

        Args:
            message (str): The message to print.
        """
        if self.config.get("General", "PrintToConsole", default=False):
            print(message)

    def run_self_tests(self, is_new_day: bool) -> bool:
        """Run self tests on all outputs then exit.

        Args:
            is_new_day (bool): Indicates if it's a new day.

        Returns:
            bool: True if we should exit after the tests, False otherwise.
        """
        if not self.config.get("General", "TestingMode", default=False):
            return False

        # api_data = self.get_api_data()
        # self.logger.log_message(f"API Data: \n{json.dumps(api_data, indent=4)}", "debug")

        # api_output_data = self.get_api_data("Outputs")
        # self.logger.log_message(f"API Output Data: \n{json.dumps(api_output_data, indent=4)}", "debug")

        # api_meter_data = self.get_api_data("Meters")
        # self.logger.log_message(f"API Meter Data: \n{json.dumps(api_meter_data, indent=4)}", "debug")

        # amber_data = self.pricing.get_prices_for_data_api(channel_id=AmberChannel.GENERAL, interval_time=15, number_of_intervals=12, price_warning=45, price_critical=48)
        # amber_data_json = JSONEncoder.ready_dict_for_json(amber_data)
        # self.logger.log_message(f"Amber Data: \n{json.dumps(amber_data_json, indent=4)}", "debug")

        # Run output level self tests
        for output in self.outputs:
            run_self_tests_fn = getattr(output, "run_self_tests", None)
            if not callable(run_self_tests_fn):
                continue
            output.run_self_tests(is_new_day)

        return True

    # Helper to post a long-running sequence (for OutputManager to call)
    def post_smart_device_sequence(
        self,
        steps: list[DeviceStep],
        label: str = "",
        timeout_s: float | None = None,
        on_complete: Callable[[DeviceSequenceResult], None] | None = None,
    ) -> str | None:
        if not self.smart_device_worker:
            self.logger.log_message("Smart Device worker not available; cannot run sequence.", "error")
            return None

        # Default callback posts a command back to the controller loop with the result.
        def default_on_complete(res: DeviceSequenceResult):
            self.post_command(Command("post_smart_devicey_sequence", {
                "sequence_id": res.id,
                "label": label,
                "ok": res.ok,
                "error": res.error,
            }))

        req = DeviceSequenceRequest(
            steps=steps,
            label=label,
            timeout_s=timeout_s,
            on_complete=on_complete or default_on_complete,
        )
        return self.smart_device_worker.submit(req)

    # Example: schedule “turn on O1, wait 60s, turn on O2” and notify via Command when done
    def example_long_running_sequence(self):
        steps = [
            DeviceStep(StepKind.CHANGE_OUTPUT, {"output_identity": "Sydney Dev A O1", "state": True}, retries=2, retry_backoff_s=1.0),
            DeviceStep(StepKind.SLEEP, {"seconds": 60}),
            DeviceStep(StepKind.REFRESH_STATUS, {"output_identity": "Sydney Dev A O2", "state": True}, retries=2, retry_backoff_s=1.0),
        ]
        job_id = self.post_smart_device_sequence(steps, label="pool-seq", timeout_s=180)
        self.logger.log_message(f"Submitted pool sequence job_id={job_id}", "debug")

    def get_api_data(self, entry: str | None = None) -> dict:
        """Get the latest API data.

        Args:
            entry (str | None): Specific entry to retrieve. If None, returns all data.

        Returns:
            dict: The latest API data.
        """
        if not self._data_api_config.get("Enable", False):
            return {}  # Data API not enabled

        with self._data_api_lock:
            if entry is None:
                return self.data_api_data

            # If we were asked for a specific entry, return just that entry's data (or an empty dict if it doesn't exist)
            return_data = {
                entry: copy.deepcopy(self.data_api_data.get(entry, {})),
                "LastRefresh": self.data_api_data.get("LastRefresh"),
            }
            return return_data

    # Private Functions ===========================================================================
    def _initialise(self, startup_mode: bool | None = False):  # noqa: PLR0912, PLR0915
        """(re) initialise the power controller.

        Args:
            startup_mode (bool): If True, we're doing the initial startup initialisation. If False, we're doing a reinitialisation due to a config change.
        """
        # See if we have a system state file to load
        saved_state = self._load_system_state()
        if saved_state:
            self.logger.log_message("Initializing power controller from saved state.", "debug")
        else:
            self.logger.log_message("No saved state found, initializing power controller from scratch.", "debug")

        self.poll_interval = int(self.config.get("General", "PollingInterval", default=30) or 30)  # pyright: ignore[reportArgumentType]
        self.webapp_refresh = int(self.config.get("Website", "PageAutoRefresh", default=10) or 10)  # pyright: ignore[reportArgumentType]
        self.app_label = self.config.get("General", "Label", default="PowerController")
        self.currency_major_symbol, self.currency_minor_symbol = get_currency_symbols(self.config)

        # Reinitialise if needed
        if not startup_mode:
            # Reinitialise the SCSmartDevices controller and get the latest status
            smart_switch_settings = self.config.get("SCSmartDevices")
            if smart_switch_settings is None:
                self.logger.log_fatal_error("No SmartDevices settings found in the configuration file.")
                return

            self.smart_device_worker.reinitialise_settings(smart_switch_settings)
        # Get the latest SmartDeviceView view
        view = self._get_latest_status_view()

        # Confirm that the configured output names are unique
        output_names = [o["Name"] for o in self.config.get("Outputs", default=[]) or []]
        if len(output_names) != len(set(output_names)):
            self.logger.log_fatal_error("Output names must be unique.")
            return

        # Initialise the TeslaMate import settings
        self.save_tesla_raw_data = False
        self.tesla_import_enabled = TESLAMATE_AVAILABLE and bool(self.config.get("TeslaMate", "Enable", default=False))
        if self.tesla_import_enabled:
            self.save_tesla_raw_data: bool = bool(self.config.get("Failed to import TeslaMate charging", "SaveRawData", default=False))
        # Get tesla data from saved state
        if self.tesla_import_enabled and saved_state and saved_state.get("TeslaChargeData", {}) is not None:
            self.tesla_charge_data["last_import"] = saved_state.get("TeslaChargeData", {}).get("last_import", None)
            self.tesla_charge_data["sessions"] = saved_state.get("TeslaChargeData", {}).get("sessions", [])
            self.tesla_charge_data["buckets"] = saved_state.get("TeslaChargeData", {}).get("buckets", [])
        else:
            self.tesla_charge_data["last_import"] = None
            self.tesla_charge_data["sessions"] = []
            self.tesla_charge_data["buckets"] = []

        # Read the smart device output sequences from the config
        try:
            self._read_smart_device_sequences_from_config(view)
        except RuntimeError as e:
            self.logger.log_fatal_error(f"Error reading OutputSequences from configuration file: {e}")

        # Loop through each output read from the config file
        # Create an instance of a OutputStateManager manager object for each output we're managing
        outputs_config = self.config.get("Outputs", default=[]) or []
        self.outputs.clear()    # Clear any existing outputs
        try:
            for output_cfg in outputs_config:
                output_type = (output_cfg.get("Type") or "smart_device").strip().lower() if isinstance(output_cfg.get("Type"), str) else (output_cfg.get("Type") or "smart_device")

                # See if we can find saved state for this output
                output_state = None
                if saved_state and "Outputs" in saved_state:
                    output_state = next((o for o in saved_state["Outputs"] if o.get("Name") == output_cfg.get("Name")), None)

                # Search for an existing output with the same name and update it if found
                if any(o.name == output_cfg.get("Name") for o in self.outputs):
                    existing_output = next(o for o in self.outputs if o.name == output_cfg.get("Name"))
                    existing_output.initialise(output_config = output_cfg, view = view, saved_state = output_state)
                    continue

                # Create a new output manager
                output_manager = None
                if output_type == "smart_device":
                    output_manager = OutputManager(output_cfg, self.config, self.logger, self.scheduler, self.pricing, view, self.ups_integration, self.weather_integration, output_state)
                if output_type == "teslamate":
                    output_manager = TeslaMateOutput(output_cfg, self.config, self.logger, self.scheduler, self.pricing, self.tesla_charge_data, output_state) # pyright: ignore[reportCallIssue]
                if output_type == "meter":
                    output_manager = MeterOutput(output_cfg, self.config, self.logger, self.scheduler, self.pricing, view, output_state)
                if output_manager:
                    self.outputs.append(output_manager)
        except RuntimeError as e:
            self.logger.log_fatal_error(f"Error initializing outputs: {e}")

        # Now remove any outputs that are no longer in the config file
        self.outputs = [o for o in self.outputs if any(o.name == cfg.get("Name") for cfg in outputs_config)]

        # Now link outputs to their parent outputs if needed
        for output in self.outputs:
            if output.parent_output_name:
                parent = next((o for o in self.outputs if o.name == output.parent_output_name), None)
                if parent:
                    output.set_parent_output(parent)

        # Finally do some cross output validation
        for output in self.outputs:
            output_cfg = getattr(output, "output_config", {})

            # Make sure the output's parent is not itself or a child of itself
            if output.parent_output:
                parent = output.parent_output
                parent.is_parent = True
                while parent:
                    if parent == output:
                        self.logger.log_fatal_error(f"Output {output.name} cannot be its own parent or a child of itself.")
                        break
                    parent = parent.parent_output

            # Make sure each SmartDevice output device is only used once
            device_output_name = getattr(output, "device_output_name", None)
            if device_output_name:
                list_output_devices = self._find_output(LookupMode.OUTPUT, device_output_name)
                if len(list_output_devices) > 1:
                    self.logger.log_fatal_error(f"Output device {device_output_name} is used by more than one output.")

            # Display a warning if multiple outputs use the same meter device
            device_meter_name = getattr(output, "device_meter_name", None)
            if device_meter_name:
                list_meter_devices = self._find_output(LookupMode.METER, device_meter_name)
                if len(list_meter_devices) > 1:
                    self.logger.log_message(f"Meter device {device_meter_name} is used by {output.name} and at least one other output.", "warning")

            # Validate that the output's OutputSequence exists (SmartDevice outputs only)
            if isinstance(output_cfg, dict):
                if output_cfg.get("TurnOnSequence") and output_cfg.get("TurnOnSequence") not in self._smart_device_sequence_requests:
                    self.logger.log_fatal_error(f"Output {output.name} has invalid TurnOnSequence {output_cfg.get('TurnOnSequence')}.")
                if output_cfg.get("TurnOffSequence") and output_cfg.get("TurnOffSequence") not in self._smart_device_sequence_requests:
                    self.logger.log_fatal_error(f"Output {output.name} has invalid TurnOffSequence {output_cfg.get('TurnOffSequence')}.")

            # Set max days of history for the Tesla charging data import
            if isinstance(output, TeslaMateOutput):
                num_days = output.get_days_of_history() # pyright: ignore[reportAttributeAccessIssue]
                self.tesla_charge_data_days_of_history = max(self.tesla_charge_data_days_of_history, num_days)

        # Sort outputs so parents are evaluated first
        list.sort(self.outputs, key=lambda x: not x.is_parent)

        # Temp probe logging
        self.temp_probe_logging = self._configure_temp_probe_logging(saved_state, view)

        # Reinitialise the UPS integration
        self.ups_integration.initialise()

        # Reinitialise the weather integration
        self.weather_integration.initialise()

        # Load DataAPI data if enabled
        self._data_api_config = self.config.get("DataAPI", default={}) or {}
        self._initialise_data_api_cache()

        # Reinitialize the scheduler and pricing manager
        if not startup_mode:
            self.scheduler.initialise()
            self.pricing.initialise()

    def _run_scheduler_tick(self, is_new_day: bool) -> bool:
        """Do all the control processing of the main loop.

        Args:
            is_new_day (bool): Indicates if it's a new day.

        Returns:
            bool: True if one or more commands were processed or there has been a state change.
        """
        commands_processed = self._clear_commands()          # Get all commands from the queue and apply them

        # Refresh the Amber price data if it's time to do so
        self.pricing.refresh_price_data_if_time(is_new_day)

        # Get a snapshot of all smart devices.
        # If a sequence is already in-flight for any output, skip the blocking refresh and
        # use the last-known snapshot so the main loop keeps ticking. The sequence completion
        # callback (set_wake_event) will trigger a new iteration where a full refresh runs.
        if self._has_pending_actions():     # Issue 84: if we have pending actions, we may be in a long-running sequence and the device status may be changing frequently. In this case, skip the refresh and just get the latest view snapshot to keep the main loop ticking smoothly.
            view = self._get_latest_status_view()
        else:
            view = self._refresh_device_statuses()

        # Get the latest UPS data and update the UPSIntegration object
        self.ups_integration.read_ups_data()

        # Get the latest weather data and update the WeatherIntegration object
        self.weather_integration.read_weather_data()

        # Get the location data for all devices and save it to each OutputManager if needed
        self._save_device_location_data()

        # Log the temp probe data if needed
        self._log_temp_probes(view)

        # Monitor device internal temperatures and log if needed
        self._monitor_device_internal_temps(view)

        # Import the latest TeslaMate charging data if needed
        self._import_teslamate_charging_data_if_needed()

        # Calculate the running totals for each output
        self._calculate_running_totals(view, is_new_day=is_new_day)

        # Regenerate the run_plan for each output if needed
        self._review_run_plans(view)

        # Evaluate the conditions for each output and make changes if needed
        state_change = self._evaluate_conditions(view)

        # Deal with config changes including downstream objects
        self._check_for_configuration_changes(view)

        # Save the system state to disk
        self._save_system_state(view)

        # Update the API Data cache if time
        self._refresh_api_data_if_needed(view)

        # Ping the heartbeat monitor - this function takes care of frequency checks
        self.logger.ping_heartbeat()

        # Check for fatal error recovery
        self._check_fatal_error_recovery()

        # Trim the logfile if needed
        self._trim_logfile_if_needed()

        return commands_processed or state_change

    def _configure_temp_probe_logging(self, saved_state: dict | None, view: SmartDeviceView) -> dict:
        """Configure the temp probes to log.

        Args:
            saved_state (dict): The system state restrieved from disk.
            view (SmartDeviceView): The current SmartDeviceView snapshot.

        Returns:
            dict: The temp probe logging configuration.
        """
        temp_probe_logging = {
            "enabled": self.config.get("TempProbeLogging", "Enable", default=False),
            "probes": self.config.get("TempProbeLogging", "Probes", default=[]) or [],
            "logging_interval": self.config.get("TempProbeLogging", "LoggingInterval", default=60),  # minutes
            "last_reading_within_minutes": self.config.get("TempProbeLogging", "LastReadingWithinMinutes", default=0) or 0,
            "saved_state_file_max_days": self.config.get("TempProbeLogging", "SavedStateFileMaxDays", default=7),
            "history_data_file_name": self.config.get("TempProbeLogging", "HistoryDataFile", default=""),
            "history_data_file_max_days": self.config.get("TempProbeLogging", "SavedStateFileMaxDays", default=0),
            "last_log_time": None,
            "history": saved_state.get("TempProbeLogging", {}).get("history", []) if saved_state else [],
        }

        # Validate the temp probes
        if temp_probe_logging["enabled"]:
            for probe in temp_probe_logging["probes"]:
                probe_name = probe.get("Name")
                if probe_name:
                    temp_probe_id = view.get_temp_probe_id(probe_name)
                    if not temp_probe_id:
                        self.logger.log_fatal_error(f"TempProbe {probe_name} referenced in TempProbeLogging section of config file is invalid.")
                    else:
                        # Add the TempProbeID to the probe
                        probe["ProbeID"] = temp_probe_id
                        probe["Temperature"] = None
                        probe["LastLoggedTime"] = None
                        probe["LastReadingTime"] = None
        return temp_probe_logging

    def _get_system_state_path(self) -> Path | None:
        """Get the path to the system state file from the configuration.

        Returns:
            Path | None: The path to the system state file, or None if not configured.
        """
        system_state_file = self.config.get("Files", "SavedStateFile")
        if system_state_file:
            return SCCommon.select_file_location(system_state_file)  # pyright: ignore[reportArgumentType]
        return None

    def _load_system_state(self) -> dict | None:
        """Loads the system state from disk.

        Returns:
            dict | None: The loaded system state, or None if not found or error.
        """
        system_state_path = self._get_system_state_path()
        if not system_state_path or not system_state_path.exists():
            return None

        try:
            state_data = JSONEncoder.read_from_file(system_state_path)
            if not state_data:
                return None
            assert isinstance(state_data, dict)
            if state_data.get("StateFileType") != "PowerController":
                self.logger.log_fatal_error(f"Invalid system state file type {state_data.get('StateFileType')}, cannot load file {system_state_path.name}")
                return None

        except RuntimeError as e:
            self.logger.log_fatal_error(f"Error loading system state: {e}")
        else:
            self.logger.log_message(f"Loaded system state from {system_state_path}", "debug")
            return state_data

    def _save_system_state(self, view: SmartDeviceView, force_post: bool = False):
        """Saves the system state to disk.

        Args:
            view (SmartDeviceView): The current SmartDeviceView snapshot.
            force_post (bool): If True, force posting the state to the web viewer.
        """
        # Save the output consumption data if needed
        self._save_output_usage_data()

        system_state_path = self._get_system_state_path()
        if not system_state_path:
            return

        try:
            save_object = {
                "SchemaVersion": SCHEMA_VERSION,
                "StateFileType": "PowerController",
                "DeviceName": self.app_label,
                "SaveTime": DateHelper.now(),
                "MajorCurrencySymbol": self.currency_major_symbol,
                "MinorCurrencySymbol": self.currency_minor_symbol,
                "Outputs": [],
                "Scheduler": self.scheduler.get_save_object(),
                "TempProbeLogging": self.temp_probe_logging,
                "TeslaChargeData": None,
                "OutputMetering": self.output_metering,
            }
            # Add Tesla charge data if enabled
            if self.save_tesla_raw_data:
                save_object["TeslaChargeData"] = self.tesla_charge_data

            for output in self.outputs:
                output_save_object = output.get_save_object(view)
                save_object["Outputs"].append(output_save_object)

            # Save the file
            JSONEncoder.save_to_file(save_object, system_state_path)
        except (TypeError, ValueError, RuntimeError, OSError) as e:
            self.logger.log_fatal_error(f"Error saving system state: {e}")
        else:
            # self.logger.log_message(f"System state saved to {system_state_path}", "debug")
            pass

        # Post the state data to the PowerController Viewer web app if needed
        self._post_state_to_web_viewer(view, force_post)

    def _refresh_device_statuses(self) -> SmartDeviceView:
        """Refresh the status of all devices.

        Returns:
            A SmartDeviceView object
        """
        # Post a refresh job and wait for it to complete (bounded wait)
        req_id = self.smart_device_worker.request_refresh_status()
        # Give a long timeout as we may be blocked by a long running sequence
        done = self.smart_device_worker.wait_for_result(req_id, timeout=90.0)
        if done:
            # self.logger.log_message("Completed SmartDevice refresh.", "debug")
            pass
        else:
            self.logger.log_message("Timed out waiting for SmartDevice refresh; using last snapshot.", "warning")

        view = self._get_latest_status_view()

        if DUMP_SMART_DEVICE_SNAPSHOT:
            view_snapshot = view.get_json_snapshot()
            # Save the JSON snapshot to a file for debugging
            debug_file_path = SCCommon.select_file_location("debug_smart_device_view_snapshot.json")
            if debug_file_path:
                try:
                    JSONEncoder.save_to_file(view_snapshot, debug_file_path)
                    self.logger.log_message(f"Saved SmartDevice view snapshot to {debug_file_path}", "debug")
                except (TypeError, ValueError, RuntimeError, OSError) as e:
                    self.logger.log_message(f"Failed to save Smart Device view snapshot: {e}", "warning")

        # Tell outputs device status updated
        for output in self.outputs:
            output.tell_device_status_updated(view)

        return view

    def _save_device_location_data(self):
        """Get the location data for all devices and save it to each OutputManager."""
        if self.update_device_locations:
            for output in self.outputs:
                device_name = getattr(output, "device_name", None)
                if not device_name:
                    continue

                req_id = self.smart_device_worker.request_device_location(device_name)
                done = self.smart_device_worker.wait_for_result(req_id, timeout=4.0)
                if done:
                    self.logger.log_message(f"Completed smart device {device_name} information retrieval.", "debug")
                else:
                    self.logger.log_message(f"Timed out waiting for smart device {device_name} information.", "warning")

            # Now get the fully populated location data dict for all devices
            loc_info = self.smart_device_worker.get_location_info()

            # And save it to the Scheduler
            self.scheduler.save_device_location_info(loc_info)

            self.update_device_locations = False

    def _get_latest_status_view(self) -> SmartDeviceView:
        """Get the latest SmartDeviceView snapshot from the SmartDeviceWorker.

        Returns:
            SmartDeviceView: The latest SmartDeviceView snapshot.
        """
        # Get a deep copy of all the smart devices
        snapshot = self.smart_device_worker.get_latest_status()

        # And create a new SmartDeviceView instance to reference this data
        return snapshot

    def _calculate_running_totals(self, view: SmartDeviceView, is_new_day: bool = False):
        """Calculate the running totals for each output.

        Args:
            view (SmartDeviceView): The current SmartDeviceView snapshot.
            is_new_day (bool): Indicates if it's a new day.
        """
        for output in self.outputs:
            output.calculate_running_totals(view, is_new_day=is_new_day)

    def _review_run_plans(self, view: SmartDeviceView):
        """Generate / refresh the run plan for each output."""
        for output in self.outputs:
            output.review_run_plan(view)

    def _has_pending_actions(self) -> bool:
        """Return True if any output currently has an in-flight action request."""
        return any(output.get_action_request() is not None for output in self.outputs)

    def _evaluate_conditions(self, view: SmartDeviceView) -> bool:
        """Evaluate the conditions for each output.

        Returns:
            bool: True if one or more outputs changed state, False otherwise.
        """
        state_change = False
        for output in self.outputs:
            # See if we have a requested action from the output
            pending_action = output.get_action_request()
            if pending_action:
                # See if the action has completed
                request_result = self.smart_device_worker.get_result(pending_action.worker_request_id)
                if not request_result:
                    # Still pending
                    self.logger.log_message(f"Action {pending_action.type} for output {output.device_output_name} still pending.", "debug")
                    continue  # Go to next output, don't evaluate conditions yet

                # Request has completed, successfully or not. Clear the request ID in the output
                state_change = True
                view = self._refresh_device_statuses()  # Refresh the view after the change

                if request_result.ok:
                    output.record_action_complete(pending_action, view)  # Record the action complete
                else:
                    output.action_request_failed(request_result.error)

            # Now evaluate conditions
            requested_action = output.evaluate_conditions(view=view, output_sequences=self._smart_device_sequence_requests, on_complete=self.set_wake_event)

            if requested_action:
                state_change = True
                self._execute_action_on_output(output, requested_action, view)

        return state_change

    def _execute_action_on_output(self, output: OutputManager, requested_action: OutputAction, view: SmartDeviceView):
        # If the Output requests a change, post it to the SmartDeviceWorker and wait for it to complete
        if requested_action.type in {OutputActionType.TURN_ON, OutputActionType.TURN_OFF}:
            if not requested_action.request:
                error_msg = f"No request defined for action {requested_action.type} on output {output.device_output_name}."
                self.logger.log_message(error_msg, "error")
                raise RuntimeError(error_msg)

            # Queue the and get the request ID
            requested_action.worker_request_id = self.smart_device_worker.submit(requested_action.request)

            # And record the pending action in the output
            output.record_action_request(requested_action)
        else:
            # It's an action that we can deal with synchronously here
            output.record_action_complete(requested_action, view)

    def _force_output_off(self, output: OutputManager, view: SmartDeviceView):
        """Force an output off immediately."""
        is_device_online = view.get_device_online(output.device_id)
        is_device_output_on = view.get_output_state(output.device_output_id)

        if not is_device_online or not is_device_output_on:
            # Device is offline or already off, nothing to do
            return
        requested_action = output.formulate_output_sequence(system_state=SystemState.AUTO, reason=StateReasonOff.SHUTDOWN, output_state=False, output_sequences=self._smart_device_sequence_requests, view=view)

        requested_action.worker_request_id = self.smart_device_worker.submit(requested_action.request)  # pyright: ignore[reportArgumentType]

        # Make timeout configurable if you like; 3s is a reasonable default
        done = self.smart_device_worker.wait_for_result(requested_action.worker_request_id, timeout=3.0)
        if done:
            output.record_action_complete(requested_action, view)
        else:
            self.logger.log_message(f"Timed out waiting for force shutdown of output {output.device_output_name}.", "warning")

    def _check_for_configuration_changes(self, view: SmartDeviceView):
        """Reload the configuration from disk if it has changed and apply downstream changes."""
        last_modified = self.config.check_for_config_changes(self.last_config_check)
        if last_modified:
            self.last_config_check = last_modified
            self.logger.log_message("Configuration file has changed, reloading...", "detailed")
            self._save_system_state(view, force_post=True)  # Save state before reinitialising
            self._initialise()
            self.update_device_locations = True

    def _apply_command(self, cmd: Command) -> None:
        """Apply a command posted to the controller."""
        view = self._get_latest_status_view()
        if cmd.kind == "set_mode":
            output_id = cmd.payload["output_id"]
            new_mode = AppMode(cmd.payload["mode"])
            revert_time_mins = cmd.payload.get("revert_time_mins")
            output = self._find_output(LookupMode.ID, output_id)
            output = output[0] if output else None
            if not output:
                return

            # Set the new mode, the output will deal with it in the next tick
            # And evaluate the conditions immediately if the mode has changed
            output.set_app_mode(new_mode, view, revert_minutes=revert_time_mins)
        elif cmd.kind == "post_smart_devicey_sequence":
            seq_id = cmd.payload.get("sequence_id")
            label = cmd.payload.get("label")
            ok = bool(cmd.payload.get("ok"))
            err = cmd.payload.get("error")
            if ok:
                self.logger.log_message(f"Smart Device sequence {label or seq_id} completed.", "detailed")
            else:
                self.logger.log_message(f"Smart Device sequence {label or seq_id} failed: {err}", "error")
            # Optional: trigger a fast evaluation tick if needed
            # self._run_scheduler_tick()

    def _have_pending_commands(self) -> bool:
        """Check if there are any pending commands in the command queue.

        Returns:
            bool: True if there are pending commands, False otherwise.
        """
        if not self.cmd_q.empty():
            return True
        return bool(self.command_pending)

    def _clear_commands(self) -> bool:
        """Clear all commands in the command queue.

        Returns:
            bool: True if one or more commands were processed.
        """
        processed = False
        while True:
            try:
                cmd = self.cmd_q.get_nowait()
            except queue.Empty:
                break
            self._apply_command(cmd)
            processed = True

        self.command_pending = False
        return processed

    def _save_output_usage_data(self):
        """Save output usage data for configured outputs.

        Note: Energy usage is in kWh.
        """
        # Skip everything if OutputMetering is not enabled
        if not self.config.get("OutputMetering", "Enable", default=False):
            return

        # Save the data to CSV file for the selected metered outputs
        csv_data = self._save_usage_data_to_csv()

        # Update the OutputMetering section of the system state file
        self._update_system_state_usage_data(csv_data)

    def _save_usage_data_to_csv(self) -> list[dict]:
        """Save usage data for configured outputs to a CSV file.

        Note: Energy usage is in kWh.

        Returns:
            list[dict]: The aggregated usage data saved to CSV, or an empty list if none.
        """
        usage_data_file = self.config.get("OutputMetering", "DataFile")
        if not usage_data_file:
            return []
        file_path = SCCommon.select_file_location(usage_data_file)  # pyright: ignore[reportArgumentType]
        if not file_path:
            return []

        # Aggregate the usage data from all outputs listed in the OutputMetering: OutputsToLog section of the config file.
        aggregated_data = []
        outputs_to_log = self.config.get("OutputMetering", "OutputsToLog")
        if not outputs_to_log:
            return []

        for log_output in outputs_to_log:
            # Lookup this output in our objects
            output_name = log_output.get("Output")
            display_name = log_output.get("DisplayName") or output_name
            output = next((o for o in self.outputs if o.name == output_name), None)
            if not output:
                self.logger.log_message(f"OutputMetering: Output {output_name} not found among configured outputs; skipping.", "error")
                continue

            # Try and get the usage data (kWh) for all days in the run history for this output
            output_data = output.get_daily_usage_data(name=display_name)
            if not output_data:
                continue    # No usage data from this output
            if isinstance(output_data, list):
                aggregated_data.extend(output_data)
            else:
                aggregated_data.append(output_data)

        # If we have no data, nothing to do
        if not aggregated_data:
            return []

        max_history_days = int(self.config.get("OutputMetering", "DataFileMaxDays", default=30) or 30)  # pyright: ignore[reportArgumentType]
        if not max_history_days:
            return []

        # Check for -1 meaning unlimited
        max_history_days = None if max_history_days == -1 else max_history_days

        # Create a CSVreader to read the existing data
        csv_reader = None
        try:
            schemas = ConfigSchema()
            csv_reader = CSVReader(file_path, schemas.output_consumption_history_config)  # pyright: ignore[reportArgumentType]
            merged_data = csv_reader.update_csv_file(aggregated_data, max_days=max_history_days)
        except (ImportError, TypeError, ValueError) as e:
            self.logger.log_message(f"Error initializing CSVReader in _save_output_consumption_data(): {e}", "error")
            return []
        except RuntimeError as e:
            self.logger.log_message(f"Error updating output consumption history CSV file: {e}", "error")
            return []
        else:
            return merged_data

    def _update_system_state_usage_data(self, csv_data: list[dict]):
        """Save / update the metered output usage data in the system state file (self.output_metering >> OutputMetering section).

        Note: Energy usage is in kWh.

        Args:
            csv_data (list[dict]): The aggregated usage data saved to CSV.
        """
        self.output_metering = {}
        self.output_metering["Summary"] = {
            "LastUpdated": DateHelper.now(),
            "FirstDate": None,
            "LastDate": None,
            "NumberOfOutputs": 0,
        }
        self.output_metering["Totals"] = self.pricing.get_daily_usage_totals()     # Daily global usage totals
        self.output_metering["Meters"] = []     # List of metered outputs and the daily usage for each one

        # Now get the totals for each output and reporting period from the csv_data
        outputs_to_log = self.config.get("OutputMetering", "OutputsToLog")
        if outputs_to_log:
            for log_output in outputs_to_log:
                if log_output.get("HideFromViewerApp"):
                    continue    # Don't save this output in the system state file and therefore hide from viewer app

                # Lookup this output in our objects
                output_name = log_output.get("Output")
                display_name = log_output.get("DisplayName") or output_name
                output = next((o for o in self.outputs if o.name == output_name), None)
                if not output:
                    self.logger.log_message(f"OutputMetering: Output {output_name} not found among configured outputs; skipping.", "error")
                    continue

                # Add the header section
                meter_entry = {
                    "Output": output_name,
                    "DisplayName": display_name,
                    "FirstDate": None,
                    "LastDate": None,
                    "Usage": [],
                }

                # Now calculate the totals for this output and period from the CSV data
                first_date = None
                last_date = None
                for item in csv_data:
                    if item["OutputName"] != display_name:
                        continue

                    # Calculate first and last date we have data for
                    if not first_date or item["Date"] < first_date:
                        first_date = item["Date"]
                    if not last_date or item["Date"] > last_date:
                        last_date = item["Date"]

                    # Create the entry for this output and date
                    usage_item = {
                        "Date": item["Date"],
                        "EnergyUsed": item["EnergyUsed"],
                        "Cost": item["TotalCost"],
                    }

                    meter_entry["Usage"].append(usage_item)

                # Update first and last date
                meter_entry["FirstDate"] = first_date
                meter_entry["LastDate"] = last_date

                # And finally append this usage to the system state
                self.output_metering["Meters"].append(meter_entry)
                self.output_metering["Summary"]["NumberOfOutputs"] += 1

                # Update the overall first and last date
                if not self.output_metering["Summary"]["FirstDate"] or (first_date and first_date < self.output_metering["Summary"]["FirstDate"]):
                    self.output_metering["Summary"]["FirstDate"] = first_date
                if not self.output_metering["Summary"]["LastDate"] or (last_date and last_date > self.output_metering["Summary"]["LastDate"]):
                    self.output_metering["Summary"]["LastDate"] = last_date

    def _check_fatal_error_recovery(self):
        """Check for fatal errors in the system and handle them."""
        # If the prior run fails, send email that this run worked OK
        if self.logger.get_fatal_error():
            self.logger.log_message(f"{self.app_label} started successfully after a prior failure.", "summary")
            self.logger.clear_fatal_error()
            self.logger.send_email(f"{self.app_label} recovery", "Application was successfully started after a prior critical failure.")

    def _find_output(self, mode: LookupMode, identity: str) -> list[OutputManager]:
        """Return a list of all outputs that match the given criteria.

        Args:
            mode (LookupMode): The lookup mode to use.
            identity (str): The ID or name of the output to find.

        Returns:
            list[OutputManager]: The found output managers, or an empty list if not found.
        """
        if mode == LookupMode.ID:
            return [o for o in self.outputs if o.id == identity]
        if mode == LookupMode.NAME:
            return [o for o in self.outputs if o.name == identity]
        if mode == LookupMode.OUTPUT:
            return [o for o in self.outputs if getattr(o, "device_output_name", None) == identity]
        if mode == LookupMode.METER:
            return [o for o in self.outputs if getattr(o, "device_meter_name", None) == identity]
        if mode == LookupMode.INPUT:
            return [o for o in self.outputs if getattr(o, "device_input_name", None) == identity]
        return []

    def _read_smart_device_sequences_from_config(self, view: SmartDeviceView):  # noqa: PLR0912, PLR0914, PLR0915
        """Read the OutputSequences from the configuration, validates and builds a list of DeviceSequenceRequest objects.

        Args:
            view (SmartDeviceView): The current SmartDeviceView snapshot.

        Raises:
            RuntimeError: If there is an error in the configuration.
        """
        self._smart_device_sequence_requests.clear()

        config_data = self.config.get("OutputSequences", default=[]) or []
        if not config_data or not isinstance(config_data, list):
            return

        for sequence in config_data:
            error_msg = ""
            name = "Unknown"
            try:
                # Get the basics for the DeviceSequenceRequest object
                name = sequence.get("Name")
                if not name:
                    error_msg = "Output sequence missing 'Name' field."
                    raise RuntimeError(error_msg)
                timeout = sequence.get("Timeout", 10.0)

                steps_config = sequence.get("Steps")
                if not steps_config or not isinstance(steps_config, list):
                    error_msg = f"Output sequence '{name}' has invalid or missing 'Steps' field."
                    raise RuntimeError(error_msg)

                steps = []
                # Loop through the steps and build the DeviceStep objects
                for step_cfg in steps_config:
                    step_type_str = step_cfg.get("Type").upper()
                    if not step_type_str:
                        error_msg = f"Output sequence '{name}' has a step missing 'Type' field."
                        raise RuntimeError(error_msg)
                    step_kind = STEP_TYPE_MAP.get(step_type_str)
                    if not step_kind:
                        error_msg = f"Output sequence '{name}' has a step with invalid 'Type' value '{step_type_str}'."
                        raise RuntimeError(error_msg)

                    # Build the parameters dict based on the step type
                    parameters: dict[str, Any] = {}
                    if step_kind == StepKind.SLEEP:
                        seconds = step_cfg.get("Seconds")
                        if seconds is None:
                            error_msg = f"Output sequence '{name}' has a SLEEP step missing 'Seconds' field."
                            raise RuntimeError(error_msg)
                        parameters["seconds"] = float(seconds)
                    elif step_kind == StepKind.CHANGE_OUTPUT:
                        output_identity = step_cfg.get("OutputIdentity")
                        if not view.validate_output_id(output_identity):
                            error_msg = f"Output sequence '{name}' has a CHANGE_OUTPUT step with invalid 'OutputIdentity' value '{output_identity}'."
                            raise RuntimeError(error_msg)
                        state = bool(step_cfg.get("State"))
                        if output_identity is None or state is None:
                            error_msg = f"Output sequence '{name}' has a CHANGE_OUTPUT step missing 'OutputIdentity' or 'State' field."
                            raise RuntimeError(error_msg)
                        parameters["output_identity"] = output_identity
                        parameters["state"] = bool(state)
                    elif step_kind == StepKind.REFRESH_STATUS:
                        pass  # No parameters needed
                    elif step_kind == StepKind.GET_LOCATION:
                        device_identity = step_cfg.get("DeviceIdentity")
                        if device_identity is None:
                            error_msg = f"Output sequence '{name}' has a GET_LOCATION step missing 'DeviceIdentity' field."
                            raise RuntimeError(error_msg)
                        if not view.validate_device_id(device_identity):
                            error_msg = f"Output sequence '{name}' has a GET_LOCATION step with invalid 'DeviceIdentity' value '{device_identity}'."
                            raise RuntimeError(error_msg)
                        parameters["device_identity"] = device_identity
                    else:
                        error_msg = f"Output sequence '{name}' has a step with unsupported 'Type' value '{step_type_str}'."
                        raise RuntimeError(error_msg)
                    retries = int(step_cfg.get("Retries", 0) or 0)  # pyright: ignore[reportArgumentType]
                    retry_backoff = float(step_cfg.get("RetryBackoffS", 0.0) or 0.0)  # pyright: ignore[reportArgumentType]

                    smart_device_step = DeviceStep(
                        kind=step_kind,
                        params=parameters,
                        retries=retries,
                        retry_backoff_s=retry_backoff,
                    )
                    steps.append(smart_device_step)

                # Now build the final request object
                smart_device_sequence = DeviceSequenceRequest(
                    steps=steps,
                    label=name,
                    timeout_s=timeout,
                )

                # And save it to our list
                self.logger.log_message(f"Configured Smart Device sequence {name}", "debug")
                self._smart_device_sequence_requests[name] = smart_device_sequence

            except KeyError as e:
                error_msg = f"Output sequence '{name}' has invalid step type configuration."
                raise RuntimeError(error_msg) from e

    def _monitor_device_internal_temps(self, view: SmartDeviceView):
        """Monitor the internal temperatures of devices and log if needed."""
        # Loop through all devices in the view
        device_id_list = view.get_device_id_list()
        for device_id in device_id_list:
            device_name = view.get_device_name(device_id)
            internal_temp = view.get_device_temperature(device_id)

            # See if we need to log a warning for this device
            device_list = self.config.get("SCSmartDevices", "Devices", default=[]) or []
            device_config = next((device for device in device_list if device.get("Name") == device_name), {})
            temp_threshold = device_config.get("DeviceAlertTemp") or 0

            if temp_threshold:
                if internal_temp is not None and temp_threshold and internal_temp >= temp_threshold:
                    self.logger.log_message(f"Smart device {device_name} internal temperature is high: {internal_temp:.0f} °C", "warning")
                    if self.report_critical_errors_delay:
                        self.logger.report_notifiable_issue(entity=f"temp_threshold device {device_name}", issue_type="Internal Temperature Exceeds Threshold", send_delay=self.report_critical_errors_delay * 60, message=f"Internal device temperature is {internal_temp} which exceeds the threshold of {temp_threshold}.")  # pyright: ignore[reportOperatorIssue, reportArgumentType]
                else:
                    self.logger.clear_notifiable_issue(entity=f"temp_threshold device {device_name}", issue_type="Internal Temperature Exceeds Threshold")

    def _post_state_to_web_viewer(self, view: SmartDeviceView, force_post: bool = False):
        """
        Post to the web server if needed.

        Args:
            view (SmartDeviceView): The current SmartDeviceView snapshot.
            force_post (bool): If True, force posting the state to the web viewer.
        """
        #
        # Break each output into a seperate post to reduce the size of each post
        frequency = self.config.get("ViewerWebsite", "Frequency", default=30) or 30
        if self.config.get("ViewerWebsite", "Enable", default=False):
            if self.viewer_website_last_post:
                time_since_last_post = (DateHelper.now() - self.viewer_website_last_post).total_seconds()
            else:
                time_since_last_post = frequency + 1   # pyright: ignore[reportOperatorIssue]

            if time_since_last_post >= frequency or force_post:  # pyright: ignore[reportOperatorIssue]
                for output in self.outputs:
                    # If this output is marked as hidden from viewer app, skip it
                    # Fix: Issue 57
                    if output.output_config.get("HideFromViewerApp", False):
                        continue

                    save_object = output.get_save_object(view)
                    post_object = {
                        "SchemaVersion": SCHEMA_VERSION,
                        "StateFileType": "PowerController",
                        "DeviceName": f"{self.app_label} - {output.name}",
                        "SaveTime": DateHelper.now(),
                        "MajorCurrencySymbol": self.currency_major_symbol,
                        "MinorCurrencySymbol": self.currency_minor_symbol,
                        "Output": save_object,
                        "Scheduler": self.scheduler.get_save_object(output.get_schedule()) if output.get_schedule() else {},
                    }
                    self.external_service_helper.post_state_to_web_viewer(post_object)

                # Now post the temp probe logging data if enabled
                if self.temp_probe_logging.get("enabled", False):
                    probe_data = copy.deepcopy(self.temp_probe_logging)
                    # Remove any entries in "probes" where HideFromViewerApp is true
                    probe_data["probes"] = [probe for probe in probe_data.get("probes", []) if not probe.get("HideFromViewerApp", False)]

                    post_object = {
                        "SchemaVersion": SCHEMA_VERSION,
                        "StateFileType": "TempProbes",
                        "DeviceName": f"{self.app_label} - TempProbes",
                        "SaveTime": DateHelper.now(),
                        "MajorCurrencySymbol": self.currency_major_symbol,
                        "MinorCurrencySymbol": self.currency_minor_symbol,
                        "Charting": self.config.get("TempProbeLogging", "Charting"),
                        "TempProbeLogging": probe_data,
                    }
                    self.external_service_helper.post_state_to_web_viewer(post_object)

                # Now post the energy usage data if enabled
                if self.config.get("OutputMetering", "Enable", default=False):
                    post_object = {
                        "SchemaVersion": SCHEMA_VERSION,
                        "StateFileType": "OutputMetering",
                        "DeviceName": f"{self.app_label} - OutputMetering",
                        "SaveTime": DateHelper.now(),
                        "MajorCurrencySymbol": self.currency_major_symbol,
                        "MinorCurrencySymbol": self.currency_minor_symbol,
                        "Summary": self.output_metering.get("Summary", []),
                        "Totals": self.output_metering.get("Totals", []),
                        "Meters": self.output_metering.get("Meters", []),
                    }
                    self.external_service_helper.post_state_to_web_viewer(post_object)

                self.viewer_website_last_post = DateHelper.now()

    def _maybe_notify_webapp(self, *, force: bool = False) -> None:
        notify = self._webapp_notify
        if not notify:
            return

        now = DateHelper.now()
        # Throttle periodic pushes to the polling interval; force=True bypasses throttle.
        if not force and self._last_webapp_notify is not None and (now - self._last_webapp_notify).total_seconds() < self.poll_interval:
            return

        self._last_webapp_notify = now
        # Web push is best-effort; do not crash the controller loop.
        with contextlib.suppress(Exception):
            notify()

    def _trim_logfile_if_needed(self) -> None:
        """Trim the logfile if needed based on time interval."""
        if not self.logger_last_trim or (DateHelper.now() - self.logger_last_trim) >= TRIM_LOGFILE_INTERVAL:
            self.logger.trim_logfile()
            self.logger_last_trim = DateHelper.now()
            self.logger.log_message("Logfile trimmed.", "debug")

    def _import_teslamate_charging_data_if_needed(self) -> None:
        """Import the latest TeslaMate charging data into the controller state, if enabled and due.

        This method stores raw imported data under ``self.tesla_charge_data`` and persists it in the
        system state file under ``TeslaChargeData``.

        Notes:
            - Imported datetimes are expected to be local-time aware.
        """
        if not self.tesla_import_enabled:
            return

        query_interval_mins = int(self.config.get("TeslaMate", "RefreshInterval", default=120) or 120)  # pyright: ignore[reportArgumentType]
        if self.tesla_last_import_query and (DateHelper.now() - self.tesla_last_import_query) < dt.timedelta(minutes=query_interval_mins):
            return  # Not time to query yet

        import_start_date = (DateHelper.now() - dt.timedelta(days=self.tesla_charge_data_days_of_history)).date()

        try:
            charging_data = get_charging_data_as_dict(self.config, start_date=import_start_date, convert_to_local=True)
            if charging_data:
                self.logger.log_message(f"Imported {len(charging_data['sessions'])} charging sessions from TeslaMate starting from {import_start_date}.", "debug")

                # Merge sessions
                existing_sessions = self.tesla_charge_data.get("sessions") or []
                new_sessions = charging_data.get("sessions") or []
                self.tesla_charge_data["sessions"] = merge_session_dict_records(existing_sessions, new_sessions)

                # Remove any existing bucket records whose bucket_start date is on/after the import_start_date.
                # This avoids duplicates when we re-import a sliding window of bucket data.
                existing_buckets = self.tesla_charge_data.get("buckets") or []
                new_buckets = charging_data.get("buckets") or []
                self.tesla_charge_data["buckets"] = merge_bucket_dict_records(existing_buckets, new_buckets, import_start_date)

                # Remove any session or bucket records older than the retention period
                cutoff_date = DateHelper.now().date() - dt.timedelta(days=self.tesla_charge_data_days_of_history)
                self.tesla_charge_data["sessions"] = [s for s in self.tesla_charge_data["sessions"] if s.get("start_date") and s["start_date"].date() >= cutoff_date]
                self.tesla_charge_data["buckets"] = [b for b in self.tesla_charge_data["buckets"] if b.get("bucket_start") and b["bucket_start"].date() >= cutoff_date]

                # Now that we have data, update the start date to today
                self.tesla_charge_data["last_import"] = DateHelper.now()

            self.tesla_last_import_query = DateHelper.now()
        except ConnectionError as e:
            self.logger.log_message(f"Failed to import TeslaMate charging data: {e}", "error")
        else:
            self.logger.log_message("Successfully imported TeslaMate charging data.", "debug")
            self.tesla_last_import_query = DateHelper.now()

    def _log_temp_probes(self, view: SmartDeviceView):  # noqa: PLR0912, PLR0914, PLR0915
        """Log the temperature probes if enabled."""
        if not self.temp_probe_logging.get("enabled"):
            return

        current_time = DateHelper.now()

        # Read the temperatures and save the latest values
        for probe in self.temp_probe_logging.get("probes", []):
            probe_id = probe.get("ProbeID")
            probe_name = probe.get("Name")
            if not probe_id or not probe_name:
                continue
            temperature = view.get_temp_probe_temperature(probe_id)
            last_reading_time = view.get_temp_probe_reading_time(probe_id)

            # Check if we need to skip this reading based on last reading time
            last_reading_within_minutes = int(self.temp_probe_logging.get("last_reading_within_minutes", 0) or 0)  # pyright: ignore[reportArgumentType]
            if last_reading_within_minutes > 0 and last_reading_time:
                minutes_since_last_reading = (current_time - last_reading_time).total_seconds() / 60.0
                if minutes_since_last_reading > last_reading_within_minutes:
                    temperature = None  # Invalidate the reading

            probe["Temperature"] = temperature
            probe["LastLoggedTime"] = current_time
            probe["LastReadingTime"] = last_reading_time

        # Now record the readings to history and trim old entries if the time interval has passed
        last_log_time = self.temp_probe_logging.get("last_log_time")
        logging_interval = int(self.temp_probe_logging.get("logging_interval", 60) or 60)  # pyright: ignore[reportArgumentType]
        if last_log_time and (current_time - last_log_time).total_seconds() < logging_interval * 60:
            return  # Not time to log yet

        # Max days to keep in saved state JSON file. 0 to disable
        max_saved_state_days = int(self.temp_probe_logging.get("saved_state_file_max_days"))  # pyright: ignore[reportArgumentType]

        # Update the last log time
        self.temp_probe_logging["last_log_time"] = current_time

        # Log each probe temperature and save to history
        for probe in self.temp_probe_logging.get("probes", []):
            probe_name = probe.get("Name")
            probe_display_name = probe.get("DisplayName", probe_name)
            temperature = probe.get("Temperature")
            last_reading_time = probe.get("LastReadingTime")

            if not probe_name or not temperature:
                continue
            self.logger.log_message(f"{probe_name} = {temperature:.1f} °C", "debug")

            # Save to saved state history
            if max_saved_state_days:
                # Append the current reading to the history
                history_entry = {
                    "Timestamp": current_time,
                    "LastReadingTime": last_reading_time,
                    "ProbeName": probe_name,
                    "ProbeDisplayName": probe_display_name,
                    "Temperature": temperature,
                }
                self.temp_probe_logging["history"].append(history_entry)

                # Remove old entries from history
                saved_state_cutoff_time = DateHelper.add_datetime(current_time, days=-max_saved_state_days)
                self.temp_probe_logging["history"] = [entry for entry in self.temp_probe_logging["history"] if entry["Timestamp"] >= saved_state_cutoff_time]

        # Max days to keep in history CSV file. 0 to disable
        max_history_days = self.temp_probe_logging.get("history_data_file_max_days") or 0
        history_file_name = self.temp_probe_logging.get("history_data_file_name")
        # current_time = DateHelper.now().replace(tzinfo=None)
        current_time = DateHelper.now()
        if history_file_name and max_history_days:
            # Create a CSVreader to read the existing data
            csv_reader = None
            try:
                file_path = SCCommon.select_file_location(history_file_name)  # pyright: ignore[reportArgumentType]
                schemas = ConfigSchema()
                if file_path and file_path.exists() and file_path.stat().st_size < 30:
                    file_path.unlink()
                csv_reader = CSVReader(file_path, schemas.temp_probe_history_config)  # pyright: ignore[reportArgumentType]

                # Build the new entries
                new_data = []
                for probe in self.temp_probe_logging.get("probes", []):
                    probe_name = probe.get("Name")
                    probe_display_name = probe.get("DisplayName", probe_name)
                    temperature = probe["Temperature"]
                    if not probe_name or not temperature:
                        continue

                    new_data.append({
                        "Timestamp": current_time,
                        "ProbeName": probe_display_name,
                        "Temperature": f"{temperature:.1f}",
                    })
                if new_data:
                    csv_reader.update_csv_file(new_data, max_days=max_history_days)
            except (ImportError, TypeError, ValueError) as e:
                self.logger.log_message(f"Error initializing CSVReader in _log_temp_probes(): {e}", "error")
                return
            except RuntimeError as e:
                self.logger.log_message(f"Error updating temp probe history CSV file: {e}", "error")
                return

    def _initialise_data_api_cache(self):
        """Initialise the data API cache."""
        with self._data_api_lock:
            self.data_api_data = {
                "Outputs": [],
                "Meters": [],
                "TempProbes": [],
                "EnergyPrices": {},
                "UPS": [],
                "LastRefresh": None,
            }
            self._data_api_next_refresh = DateHelper.now()

    def _refresh_api_data_if_needed(self, view: SmartDeviceView):  # noqa: PLR0914
        """Refresh the API data if needed based on the configured refresh intervals."""
        if not self._data_api_config.get("Enable", False):
            return  # Data API not enabled

        if self._data_api_next_refresh > DateHelper.now():
            return  # Not time to refresh yet

        # Setup the new data dict with the current refresh time
        return_data = {
            "Outputs": [],
            "Meters": [],
            "TempProbes": [],
            "EnergyPrices": {},
            "UPS": [],
            "LastRefresh": DateHelper.now(),
        }

        # Get the Output data
        for output_cfg in self._data_api_config.get("Outputs", []):
            name = output_cfg.get("Name")
            display_name = output_cfg.get("DisplayName") or name
            if not name:
                continue
            output_obj = next((o for o in self.outputs if o.name == name), None)
            if not output_obj:
                self.logger.log_message(f"Data API: Output {name} not found among configured outputs; skipping.", "error")
                continue
            new_output_data = output_obj.get_api_data(view, display_name)
            return_data["Outputs"].append(new_output_data)

        # Get the Meter data - just read the current power draw directly from the Smart Device view
        for meter_cfg in self._data_api_config.get("Meters", []):
            name = meter_cfg.get("Name")
            display_name = meter_cfg.get("DisplayName") or name
            if not name:
                continue
            meter_id = view.get_meter_id(name)
            if not meter_id:
                self.logger.log_message(f"Data API: Meter {name} not found among configured outputs; skipping.", "error")
                continue
            meter_power = view.get_meter_power(meter_id)
            new_meter_data = {
                "Name": name,
                "Type": "meter",
                "DisplayName": display_name,
                "Power": meter_power,
            }
            return_data["Meters"].append(new_meter_data)

        # Get the Temp Probe data
        probe_logging_data = self.temp_probe_logging.get("history", [])
        probe_history_cutoff_time = DateHelper.add_datetime(DateHelper.now(), days=-(self._data_api_config.get("TempProbeHistoryDays", 7) or 7))  # pyright: ignore[reportArgumentType]
        for probe_cfg in self._data_api_config.get("TempProbes", []):
            name = probe_cfg.get("Name")
            display_name = probe_cfg.get("DisplayName") or name
            if not name:
                continue
            probe_id = view.get_temp_probe_id(name)
            if not probe_id:
                self.logger.log_message(f"Data API: Temp Probe {name} not found among configured probes; skipping.", "error")
                continue
            probe_temp = view.get_temp_probe_temperature(probe_id)

            # Now build a logging history list if available for this probe
            probe_history = []
            for entry in probe_logging_data:
                if entry.get("ProbeName") == name and entry.get("Timestamp") and entry["Timestamp"] >= probe_history_cutoff_time:
                    probe_history.append({
                        "Timestamp": entry.get("Timestamp"),
                        "Temperature": entry.get("Temperature"),
                    })
            new_probe_data = {
                "Name": name,
                "Type": "temp_probe",
                "DisplayName": display_name,
                "Temperature": probe_temp,
                "LastReadingTime": view.get_temp_probe_reading_time(probe_id),
                "History": probe_history,
            }
            return_data["TempProbes"].append(new_probe_data)

        # Get the energy price data
        pricing_cfg = self._data_api_config.get("EnergyPrices", {})
        if pricing_cfg:
            return_data["EnergyPrices"] = self.pricing.get_prices_for_data_api(channel_id=pricing_cfg.get("ChannelID", AmberChannel.GENERAL),
                                                            interval_time=pricing_cfg.get("IntervalTime", 30),
                                                            number_of_intervals=pricing_cfg.get("NumIntervals", 12),
                                                            price_warning=pricing_cfg.get("WarningPrice"),
                                                            price_critical=pricing_cfg.get("CriticalPrice"))

        # Get the UPS data
        return_data["UPS"] = self.ups_integration.get_dataapi_data()

        # Now build up the JSON ready data dict that we will cache and return for API calls
        return_data_json = JSONEncoder.ready_dict_for_json(return_data)  # pyright: ignore[reportReturnType]

        # Update the data API cache with the new data in JSON format
        with self._data_api_lock:
            self.data_api_data = copy.deepcopy(return_data_json)  # pyright: ignore[reportAttributeAccessIssue, reportAssignmentType]
