"""Helper class for evaluating output constraints."""
from sc_foundation import DateHelper, SCLogger
from sc_smart_device import SmartDeviceView
from sc_weather.models import WeatherCondition

from local_enumerations import UPSMode, WeatherMode
from temp_probe_averager import TempProbeAverager
from ups_integration import UPSIntegration
from weather_integration import WeatherIntegration


class OutputConstraint:
    """Evaluates constraint conditions for an output device."""

    def __init__(self, output_config: dict, name: str, logger: SCLogger, ups_integration: UPSIntegration, weather_integration: WeatherIntegration, device_output_id: int, view: SmartDeviceView, temp_probe_averager: TempProbeAverager):
        self.output_config = output_config
        self.name = name
        self.logger = logger
        self.ups_integration = ups_integration
        self.weather_integration = weather_integration
        self.device_output_id = device_output_id
        self.temp_probe_averager = temp_probe_averager

        self.dates_off: list[dict] = []
        dates_off_list = output_config.get("DatesOff", [])
        for date_range in dates_off_list:
            start_date = date_range.get("StartDate")
            end_date = date_range.get("EndDate")
            if not start_date or not end_date:
                raise RuntimeError(f"Invalid date range in DatesOff for output {name}.")
            self.dates_off.append({"StartDate": start_date, "EndDate": end_date})

        self.temp_probe_constraints: list[dict[str, str | int | float]] = []
        for constraint in output_config.get("TempProbeConstraints", []):
            temp_probe_name = constraint.get("TempProbe")
            condition = constraint.get("Condition")
            temperature = constraint.get("Temperature")
            fall_back_temp = constraint.get("FallBackTemp")
            if not temp_probe_name or condition not in {"GreaterThan", "LessThan"} or not isinstance(temperature, (int, float)):
                raise RuntimeError(f"Invalid TempProbeConstraint in output {name}.")
            temp_probe_id = view.get_temp_probe_id(temp_probe_name)
            if not temp_probe_id:
                raise RuntimeError(f"TempProbe {temp_probe_name} not found for output {name}.")
            if condition == "GreaterThan" and fall_back_temp and fall_back_temp >= temperature:
                raise RuntimeError(f"Invalid FallBackTemp for TempProbeConstraint in output {name}. For a GreaterThan constraint, FallBackTemp must be less than the main Temperature.")
            if condition == "LessThan" and fall_back_temp and fall_back_temp <= temperature:
                raise RuntimeError(f"Invalid FallBackTemp for TempProbeConstraint in output {name}. For a LessThan constraint, FallBackTemp must be greater than the main Temperature.")
            constraint["ProbeID"] = temp_probe_id
            self.temp_probe_constraints.append(constraint)

        # Parse the optional WeatherConstraint
        self.weather_constraint: dict | None = None
        weather_config = output_config.get("WeatherConstraint")
        if weather_config:
            action = weather_config.get("ActionIfMatch")
            if action not in {"TurnOn", "TurnOff"}:
                raise RuntimeError(f"Invalid or missing ActionIfMatch in WeatherConstraint for output {name}. Must be TurnOn or TurnOff.")

            sky_conditions: set[WeatherCondition] = set()
            sky_condition_str = weather_config.get("SkyCondition")
            if sky_condition_str:
                for token in sky_condition_str.split(","):
                    if not token.strip():
                        continue
                    condition = self._parse_sky_condition(token)
                    if condition is None:
                        raise RuntimeError(f"Invalid SkyCondition '{token.strip()}' in WeatherConstraint for output {name}. Must be one of: {', '.join(c.name for c in WeatherCondition)}.")
                    sky_conditions.add(condition)

            self.weather_constraint = {
                "sky_conditions": sky_conditions,
                "temperature_below": weather_config.get("TemperatureBelow"),
                "temperature_above": weather_config.get("TemperatureAbove"),
                "precip_above": weather_config.get("PrecipitationProbabilityAbove"),
                "precip_below": weather_config.get("PrecipitationProbabilityBelow"),
                "action": action,
            }

    @staticmethod
    def _parse_sky_condition(token: str) -> WeatherCondition | None:
        """Resolve a configured sky condition token to a WeatherCondition, matching by enum name or value (case-insensitive).

        Args:
            token (str): The configured sky condition token (e.g. "overcast" or "OVERCAST").

        Returns:
            WeatherCondition | None: The matching WeatherCondition, or None if the token is invalid.
        """
        cleaned = token.strip()
        if not cleaned:
            return None
        if cleaned.upper() in WeatherCondition.__members__:
            return WeatherCondition[cleaned.upper()]
        for condition in WeatherCondition:
            if condition.value == cleaned.lower():
                return condition
        return None

    def get_dates_off(self) -> list[dict]:
        """Return the parsed DatesOff list."""
        return self.dates_off

    def is_today_excluded(self) -> bool:
        """Check if today falls within any specified DatesOff range which states that the output should be off.

        Returns:
            result(bool): True if today is excluded, False otherwise.
        """
        today = DateHelper.today()
        for rng in self.dates_off:
            if rng["StartDate"] <= today <= rng["EndDate"]:
                return True
        return False

    def get_ups_health_status(self) -> UPSMode:
        """Get the current UPS health status.

        Returns:
            UPSMode: The current UPS health status.
        """
        if not self.ups_integration:
            return UPSMode.AUTO

        ups_config = self.output_config.get("UPSIntegration", {})
        if not ups_config:
            return UPSMode.AUTO

        ups_name = ups_config.get("UPS")
        ups_action = ups_config.get("ActionIfUnhealthy")
        if not ups_name or not ups_action or ups_action not in {"TurnOff", "TurnOn"}:
            self.logger.log_message(f"Output {self.name} has an invalid UPSIntegration configuration. UPS and ActionIfUnhealthy are required. Defaulting to AUTO mode.", "error")
            return UPSMode.AUTO

        try:
            ups_status = self.ups_integration.is_ups_healthy(ups_name)
        except RuntimeError as e:
            self.logger.log_message(f"Error checking UPS status for {self.name}: {e}", "error")
            return UPSMode.AUTO
        else:
            if not ups_status and ups_action == "TurnOff":
                self.logger.log_message(f"UPS {ups_name} is unhealthy. Output {self.name} will turn OFF based on UPSIntegration configuration.", "debug")
                return UPSMode.TURN_OFF
            if not ups_status and ups_action == "TurnOn":
                self.logger.log_message(f"UPS {ups_name} is unhealthy. Output {self.name} will turn ON based on UPSIntegration configuration.", "debug")
                return UPSMode.TURN_ON

        return UPSMode.AUTO

    def get_weather_constraint_status(self) -> WeatherMode:
        """Evaluate the weather constraint against the current weather reading.

        Criteria (sky condition, temperature, precipitation probability) are combined with OR logic:
        if any configured criterion matches, the constraint is in effect and the configured ActionIfMatch applies.

        Returns:
            WeatherMode: TURN_ON or TURN_OFF if the constraint is in effect, otherwise AUTO.
        """
        if not self.weather_integration or not self.weather_constraint:
            return WeatherMode.AUTO

        reading = self.weather_integration.get_current_reading()
        if reading is None:
            # No weather data available yet, so the constraint cannot apply.
            return WeatherMode.AUTO

        constraint = self.weather_constraint
        matched_reason: str | None = None

        current_condition = reading.sky.icon_info.condition_key
        if constraint["sky_conditions"] and current_condition in constraint["sky_conditions"]:
            matched_reason = f"sky condition '{current_condition.value}' matches"

        temperature = reading.temperature.reading
        if matched_reason is None and temperature is not None:
            if constraint["temperature_below"] is not None and temperature < constraint["temperature_below"]:
                matched_reason = f"temperature {temperature}°C is below {constraint['temperature_below']}°C"
            elif constraint["temperature_above"] is not None and temperature > constraint["temperature_above"]:
                matched_reason = f"temperature {temperature}°C is above {constraint['temperature_above']}°C"

        precip = reading.precip_probability
        if matched_reason is None and precip is not None:
            if constraint["precip_above"] is not None and precip > constraint["precip_above"]:
                matched_reason = f"precipitation probability {precip} is above {constraint['precip_above']}"
            elif constraint["precip_below"] is not None and precip < constraint["precip_below"]:
                matched_reason = f"precipitation probability {precip} is below {constraint['precip_below']}"

        if matched_reason is None:
            return WeatherMode.AUTO

        if constraint["action"] == "TurnOff":
            self.logger.log_message(f"Weather constraint for output {self.name} matched ({matched_reason}). Output will turn OFF.", "debug")
            return WeatherMode.TURN_OFF

        self.logger.log_message(f"Weather constraint for output {self.name} matched ({matched_reason}). Output will turn ON.", "debug")
        return WeatherMode.TURN_ON

    def are_there_temp_probe_constraints(self, view: SmartDeviceView, new_output_state: bool) -> tuple[bool, bool | None]:  # noqa: ARG002, PLR0912
        """Evaluate the temperature probe constraints to see if they require the output to be off.

        Args:
            view (SmartDeviceView): The current view of the smart devices.
            new_output_state (bool): The proposed new state of the output (True for ON, False for OFF).

        Returns:
            tuple: A tuple containing two booleans. The first boolean is True if a temperature constraint applied, False otherwise.
                   The second boolean indicates the state to override to if a constraint is applied (True for ON, False for OFF). None if no constraint applied.
        """
        current_output_state = view.get_output_state(self.device_output_id)
        for constraint in self.temp_probe_constraints:
            probe_name = constraint.get("TempProbe", "Unknown Probe")
            probe_id = constraint.get("ProbeID")
            condition = constraint.get("Condition")
            set_temp = constraint.get("Temperature")
            fall_back_temp = constraint.get("FallBackTemp")

            if not probe_id or not isinstance(probe_id, int) or not set_temp or not isinstance(set_temp, int | float) or condition not in {"GreaterThan", "LessThan"}:
                continue

            probe_temp = self.temp_probe_averager.get_temperature(view, probe_id)

            self.logger.log_message(f"Checking constraint: output {self.name}; probe: {probe_name}; condition: {condition}; set temp: {set_temp}°C; fall back temp: {fall_back_temp}°C; probe reads: {probe_temp}", "all")

            if condition == "GreaterThan":
                if probe_temp is None:
                    # Issue 45: If temp probe = N/A for greater than condition, constaint exists
                    self.logger.log_message(f"Output {self.name} cannot turn on because temperature probe {probe_name} reading is not available.", "all")
                    return True, False
                if probe_temp >= set_temp:
                    continue  # No constaint for this condition
                if fall_back_temp is None:  # No fall back range set for this condition
                    if probe_temp < set_temp:
                        self.logger.log_message(f"Output {self.name} cannot turn on because temperature probe {probe_name} is reading {probe_temp:.1f}°C less than a minimum temperature of {set_temp}°C.", "all")
                        return True, False  # Less than set temp and fall back doesn't apply, must stay off
                else:   # Fall back range has been set
                    fall_back_temp = float(fall_back_temp)
                    if probe_temp >= fall_back_temp and probe_temp < set_temp:  # pyright: ignore[reportOperatorIssue]
                        if current_output_state:   # In the range and output currently on
                            self.logger.log_message(f"Output {self.name} is ON and temperature probe {probe_name} is reading {probe_temp:.1f}°C which is within range of {fall_back_temp}°C to {set_temp}°C. No constraint.", "all")
                            continue  # No constaint for this condition
                        else:   # In the range and output currently off  # noqa: RET507
                            self.logger.log_message(f"Output {self.name} is OFF and temperature probe {probe_name} is reading {probe_temp:.1f}°C which is within range of {fall_back_temp}°C to {set_temp}°C. Output must remain off.", "all")
                            return True, False  # Less than set temp and fall back doesn't apply, must stay off
                    if probe_temp < fall_back_temp:
                        self.logger.log_message(f"Output {self.name} cannot turn on because temperature probe {probe_name} is reading {probe_temp:.1f}°C, less than the minimum temperature of {fall_back_temp}°C.", "all")
                        return True, False  # Less than set temp and fall back doesn't apply, must stay off

            if condition == "LessThan":
                if probe_temp is None:
                    # Issue 45: Ignore temp probe = N/A for less than condition
                    self.logger.log_message(f"Temperature probe {probe_name} not available for output {self.name}.", "all")
                    continue
                if probe_temp <= set_temp:
                    continue  # No constaint for this condition
                if fall_back_temp is None:  # No fall back range set for this condition
                    if probe_temp > set_temp:
                        self.logger.log_message(f"Output {self.name} cannot turn on because temperature probe {probe_name} is reading {probe_temp:.1f}°C more than a minimum temperature of {set_temp}°C.", "all")
                        return True, False  # more than set temp and fall back doesn't apply, must stay off
                else:
                    fall_back_temp = float(fall_back_temp)
                    if probe_temp <= fall_back_temp and probe_temp > set_temp:  # pyright: ignore[reportOperatorIssue]
                        if current_output_state:  # In the range and output currently on
                            self.logger.log_message(f"Output {self.name} is ON and temperature probe {probe_name} is reading {probe_temp:.1f}°C which is within range of {fall_back_temp}°C to {set_temp}°C. No constraint.", "all")
                            continue  # No constaint for this condition
                        else:   # In the range and output currently off  # noqa: RET507
                            self.logger.log_message(f"Output {self.name} is OFF and temperature probe {probe_name} is reading {probe_temp:.1f}°C which is within range of {fall_back_temp}°C to {set_temp}°C. Output must remain off.", "all")
                            return True, False  # Less than set temp and fall back doesn't apply, must stay off
                    if probe_temp > fall_back_temp:
                        self.logger.log_message(f"Output {self.name} cannot turn on because temperature probe {probe_name} is reading {probe_temp:.1f}°C, more than the maximum temperature of {fall_back_temp}°C.", "all")
                        return True, False  # Less than set temp and fall back doesn't apply, must stay off

        return False, None
