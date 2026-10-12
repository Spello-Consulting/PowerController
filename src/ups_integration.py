"""UPS Integration module for monitoring UPS status via shell scripts."""

import datetime as dt
import json
import subprocess  # noqa: S404
from pathlib import Path
from typing import Any

from sc_foundation import CSVReader, DateHelper, SCCommon, SCConfigManager, SCLogger

from config_schemas import ConfigSchema


class UPSIntegration:
    """Manages UPS monitoring and health status."""

    def __init__(self, config: SCConfigManager, logger: SCLogger):
        """Initialize the UPS integration.

        Args:
            config (SCConfigManager): The configuration manager for the system.
            logger (SCLogger): The logger for the system.
        """
        self.config = config
        self.logger = logger
        self.enabled: bool = False
        self.polling_interval: int = 60  # Default to 60 seconds if not specified in config
        self.time_last_polled: dt.datetime | None = None
        self.data_file: dict = {
            "enabled": False,
            "file_name": None,
            "write_interval": 0,
            "last_write_time": None,
            "keep_max_hours": 0,
        }

        self.ups_list: list[dict[str, Any]] = []

        # Initialize from config
        self.initialise()

    def initialise(self) -> None:
        """Read the config and (re)initialise the object from config.

        Creates a list of UPS dict objects with configuration and current status.
        """
        ups_config = self.config.get("UPSIntegration", default=[])
        if not ups_config or not ups_config.get("Enable", True):
            self.ups_list = []
            return

        # Get the information from the config, with defaults if not specified
        self.polling_interval = ups_config.get("PollingInterval", 60) or 60
        self.data_file["file_name"] = ups_config.get("DataFile", None)
        self.data_file["write_interval"] = ups_config.get("DataFileWriteInterval", 60) or 60
        self.data_file["keep_max_days"] = ups_config.get("DataFileMaxDays", 3) or 3
        if self.data_file["file_name"]:
            self.data_file["enabled"] = True
        self.enabled = True

        # Now read the UPS device configurations
        self.ups_list = []
        for ups_device in ups_config.get("UPSDevices", []):
            name = ups_device.get("Name")
            script = ups_device.get("Script")

            if not name or not script:
                self.logger.log_message("UPS configuration missing Name or Script", "error")
                continue

            ups_entry = {
                "name": name,
                "script": script,
                "min_runtime_when_charging": ups_device.get("MinRuntimeWhenCharging", 0),
                "min_charge_when_charging": ups_device.get("MinChargeWhenCharging", 0),
                "min_runtime_when_discharging": ups_device.get("MinRuntimeWhenDischarging", 0),
                "min_charge_when_discharging": ups_device.get("MinChargeWhenDischarging", 0),
                "timestamp": None,
                "battery_charge_percent": None,
                "battery_runtime_seconds": None,
                "battery_state": None,
                "is_healthy": True,  # Default to healthy until proven otherwise
            }
            self.ups_list.append(ups_entry)

        self.logger.log_message(f"Initialized {len(self.ups_list)} UPS configuration(s)", "debug")

    def read_ups_data(self) -> None:
        """Read UPS data by invoking the configured shell scripts.

        For each configured UPS, invoke the shell script and parse the JSON output. Update the UPS status and health flag.
        """
        if not self.enabled:
            return

        # See if it's time to poll again based on the polling interval
        current_time = DateHelper.now()
        if self.time_last_polled and (current_time - self.time_last_polled).total_seconds() < self.polling_interval:
            return

        self.time_last_polled = current_time
        project_root = SCCommon.get_project_root()

        for ups in self.ups_list:
            try:
                self._execute_ups_script(ups, project_root)
            except subprocess.TimeoutExpired:
                self.logger.log_message(f"UPS script timeout for '{ups['name']}'", "error")
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.strip() if e.stderr else "No stderr output"
                self.logger.log_message(f"UPS script failed for '{ups['name']}': exit code {e.returncode}. Error: {stderr}", "error")
            except OSError as e:
                self.logger.log_message(f"Error executing UPS script for '{ups['name']}': {e}", "error")

        # Append the current readings to the CSV file if enabled and it's time to write
        self._write_ups_data_to_csv()

    def get_ups_results(self, ups_name: str | None = None) -> dict[str, Any]:
        """Get the results for one or all UPS devices.

        Returns a dictionary with UPS status information, keyed by UPS name.
        Each entry includes timestamp, charge, runtime, and health status.
        Returns empty dict is UPS integration is not enabled or if no UPS devices are configured.

        Args:
            ups_name (str | None): Optional UPS name to retrieve. If None, return all UPS results.

        Returns:
            dict[str, Any]: Dictionary of UPS results keyed by UPS name. Each entry contains:
                - timestamp: Last reading timestamp (ISO format string)
                - battery_charge_percent: Battery charge percentage
                - battery_runtime_seconds: Battery runtime in seconds
                - is_healthy: Health status boolean

        Raises:
            RuntimeError: If the specified UPS name is not found in the configuration.
        """
        if not self.enabled:
            return {}

        if ups_name:
            # Return just the specified UPS
            ups_entry = next((ups for ups in self.ups_list if ups["name"] == ups_name), None)
            if not ups_entry:
                msg = f"UPS '{ups_name}' not found in configuration"
                raise RuntimeError(msg)
            return {
                ups_name: {
                    "timestamp": ups_entry["timestamp"],
                    "battery_charge_percent": ups_entry["battery_charge_percent"],
                    "battery_runtime_seconds": ups_entry["battery_runtime_seconds"],
                    "battery_state": ups_entry["battery_state"],
                    "is_healthy": ups_entry["is_healthy"],
                }
            }

        # Return all UPS results
        results = {}
        for ups in self.ups_list:
            results[ups["name"]] = {
                "timestamp": ups["timestamp"],
                "battery_charge_percent": ups["battery_charge_percent"],
                "battery_runtime_seconds": ups["battery_runtime_seconds"],
                "battery_state": ups["battery_state"],
                "is_healthy": ups["is_healthy"],
            }
        return results

    def is_ups_healthy(self, ups_name: str) -> bool:
        """Check if a specific UPS is healthy.

        Args:
            ups_name (str): The name of the UPS to check.

        Returns:
            bool: True if the UPS is healthy, False otherwise. Returns True if no reading has been made yet.

        Raises:
            RuntimeError: If the specified UPS name is not found in the configuration.
        """
        if not self.enabled:
            return True

        ups_entry = next((ups for ups in self.ups_list if ups["name"] == ups_name), None)
        if not ups_entry:
            msg = f"UPS '{ups_name}' not found in configuration"
            raise RuntimeError(msg)

        # If no reading has been made yet, default to healthy
        if ups_entry["timestamp"] is None:
            return True

        return ups_entry["is_healthy"]

    def _execute_ups_script(self, ups: dict[str, Any], project_root: Path) -> None:
        """Execute UPS script and update UPS status.

        Args:
            ups (dict): The UPS configuration entry.
            project_root (Path): The project root directory.
        """
        # Parse the script command and arguments
        script_parts = ups["script"].split()
        if not script_parts:
            self.logger.log_message(f"Empty script command for UPS '{ups['name']}'", "error")
            return

        # Resolve the script path relative to project root
        script_path = script_parts[0]
        if not script_path.startswith("/"):
            # Relative path - resolve from project root
            script_path = str(project_root / script_path)

        # Build the full command with arguments
        command = [script_path, *script_parts[1:]]

        self.logger.log_message(f"Executing UPS script for '{ups['name']}': {' '.join(command)}", "all")

        # Execute the script and capture output
        result = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

        # Parse JSON output from stdout
        try:
            data = json.loads(result.stdout)

            # Update UPS entry with data from script
            timestamp = DateHelper.extract_datetime(data.get("timestamp"), format_str="%Y-%m-%d %H:%M:%S", hide_tz=True)
            ups["timestamp"] = timestamp
            ups["battery_state"] = data.get("battery_state", "unknown") or "unknown"
            ups["battery_charge_percent"] = data.get("battery_charge_percent", 100) or 100
            ups["battery_runtime_seconds"] = data.get("battery_runtime_seconds", 3600) or 3600

            # Determine health status based on thresholds
            self._update_ups_health_status(ups)

        except json.JSONDecodeError as e:
            self.logger.log_message(f"Failed to parse JSON output from UPS script '{ups['name']}': {e}. Output was: {result.stdout}", "error")

    def _update_ups_health_status(self, ups: dict[str, Any]) -> None:
        """Update the health status of a UPS based on thresholds.

        Args:
            ups (dict): The UPS configuration entry with current readings.
        """
        charge = ups.get("battery_charge_percent")
        runtime = ups.get("battery_runtime_seconds")
        battery_state = ups["battery_state"]

        # The default condition is that the UPS is healthy until we find a reason it's not
        is_healthy = True
        ups["is_healthy"] = is_healthy

        # Deal with no data available yet
        if charge is None or runtime is None or battery_state is None:
            return

        # If battery state is unknown, we can't determine health, so we will consider it healthy but log a warning
        if battery_state == "unknown":
            self.logger.log_message(f"UPS '{ups['name']}' battery state is unknown. Unable to determine health status.", "warning")        
            return

        # If battery is fully charged, then it's healthy regardless of runtime or charge thresholds
        if battery_state == "charged":
            return

        # Deal with charging scenario
        if battery_state == "charging":
            min_charge = ups.get("min_charge_when_charging", 0) or 0
            min_runtime = ups.get("min_runtime_when_charging", 0) or 0

            if min_charge > 0 and charge < min_charge:
                is_healthy = False
                self.logger.log_message(f"UPS '{ups['name']}' charge ({charge}%) below threshold ({min_charge}%) while charging", "warning")

            if min_runtime > 0 and runtime < min_runtime:
                is_healthy = False
                self.logger.log_message(f"UPS '{ups['name']}' runtime ({runtime}s) below threshold ({min_runtime}s) while charging", "warning")

            ups["is_healthy"] = is_healthy

        # Deal with discharging scenario
        if battery_state == "discharging":
            min_charge = ups.get("min_charge_when_discharging", 0) or 0
            min_runtime = ups.get("min_runtime_when_discharging", 0) or 0

            if min_charge > 0 and charge < min_charge:
                is_healthy = False
                self.logger.log_message(f"UPS '{ups['name']}' charge ({charge}%) below threshold ({min_charge}%) while discharging", "warning")

            if min_runtime > 0 and runtime < min_runtime:
                is_healthy = False
                self.logger.log_message(f"UPS '{ups['name']}' runtime ({runtime}s) below threshold ({min_runtime}s) while discharging", "warning")

            ups["is_healthy"] = is_healthy

    def _write_ups_data_to_csv(self) -> None:
        """Write UPS data to CSV file if enabled and it's time to write."""
        # If CSV writing is enabled, check if it's time to write to the CSV file based on the write interval
        if not self.data_file["enabled"]:
            return

        # See if it's time to poll again based on the polling interval
        current_time = DateHelper.now()

        if self.data_file["last_write_time"] and (current_time - self.data_file["last_write_time"]).total_seconds() < self.data_file["write_interval"]:
            return
        self.data_file["last_write_time"] = current_time

        # Build the UPS data to write
        ups_data = []
        for ups in self.ups_list:
            if ups["battery_state"] is None or ups["battery_state"] == "unknown":   # Issue 85
                self.logger.log_message(f"Skipping UPS '{ups['name']}' for CSV write due to unknown battery state", "warning")
                continue

            ups_timestamp_tz = ups["timestamp"]  # Issue 81
            if isinstance(ups_timestamp_tz, dt.datetime):
                local_tz = dt.datetime.now().astimezone().tzinfo
                if ups_timestamp_tz.tzinfo is None:
                    ups["timestamp"] = ups_timestamp_tz.replace(tzinfo=local_tz)
                else:
                    ups["timestamp"] = ups_timestamp_tz.astimezone(local_tz)

            ups_data.append({
                "Timestamp": ups["timestamp"],
                "UPSName": ups["name"],
                "BatteryChargePercent": ups["battery_charge_percent"],
                "BatteryRuntimeSeconds": ups["battery_runtime_seconds"],
                "BatteryState": ups["battery_state"],
                "IsHealthy": ups["is_healthy"],
            })

        # If no valid UPS data to write, skip writing to CSV
        if not ups_data:       # Issue 85
            return

        # Create a CSVReasder object and read the current content of the CSV file to append to it.
        schemas = ConfigSchema()
        csv_reader = CSVReader(self.data_file["file_name"], schemas.ups_data_file_csv_config)

        # Read the existing CSV data to append to it.
        max_days = self.data_file["keep_max_days"]
        csv_reader.update_csv_file(ups_data, max_days=max_days)

        # self.logger.log_message(f"Wrote UPS data to CSV file '{self.data_file['file_name']}'", "debug")

    def get_dataapi_data(self) -> list[dict]:
        """Get UPS data formatted for the data API."""
        ups_data = []
        for ups in self.ups_list:
            if ups["battery_state"] is None or ups["battery_state"] == "unknown":
                continue

            ups_data.append({
                "Timestamp": ups["timestamp"],
                "UPSName": ups["name"],
                "BatteryChargePercent": ups["battery_charge_percent"],
                "BatteryRuntimeSeconds": ups["battery_runtime_seconds"],
                "BatteryState": ups["battery_state"],
                "IsHealthy": ups["is_healthy"],
            })
        return ups_data
