"""Tests for the weather integration and weather output constraints (Issue 103)."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from local_enumerations import WeatherMode
from output_constraint import OutputConstraint
from sc_weather.models import WeatherCondition
from weather_integration import WeatherIntegration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger():
    m = MagicMock()
    m.log_message = MagicMock()
    return m


def _make_config(weather_conf=None, location_conf=None):
    """Build a mock SCConfigManager returning the supplied WeatherClient and Location sections."""
    cfg = MagicMock()

    def _get(*keys, default=None):
        if keys == ("WeatherClient",):
            return weather_conf
        if keys == ("Location",):
            return location_conf if location_conf is not None else {}
        return default

    cfg.get.side_effect = _get
    return cfg


def _make_reading(condition=WeatherCondition.CLEAR, temperature=25.0, precip=0.1):
    """Build a minimal stand-in for a sc_weather WeatherReading."""
    sky = SimpleNamespace(icon_info=SimpleNamespace(condition_key=condition))
    return SimpleNamespace(
        sky=sky,
        temperature=SimpleNamespace(reading=temperature),
        precip_probability=precip,
    )


def _make_constraint(weather_config, reading):
    """Construct a real OutputConstraint exercising the WeatherConstraint parsing path.

    A constraint with no DatesOff/TempProbeConstraints never touches the view, so view=None is safe.
    """
    weather_integration = MagicMock()
    weather_integration.get_current_reading.return_value = reading
    output_config = {"WeatherConstraint": weather_config}
    return OutputConstraint(
        output_config=output_config,
        name="Test Output",
        logger=_make_logger(),
        ups_integration=MagicMock(),
        weather_integration=weather_integration,
        device_output_id=1,
        view=None,
        temp_probe_averager=MagicMock(),
    )


# ---------------------------------------------------------------------------
# WeatherIntegration initialisation
# ---------------------------------------------------------------------------

class TestWeatherIntegrationInit:
    def test_no_config_disabled(self):
        wi = WeatherIntegration(_make_config(weather_conf=None), _make_logger())
        assert wi.enabled is False
        assert wi.client is None

    def test_configured_without_location_disabled(self):
        weather_conf = {"RefreshIntervalMin": 5, "PreferredProvider": "open_meteo", "OWMAPIKey": ""}
        wi = WeatherIntegration(_make_config(weather_conf=weather_conf, location_conf={}), _make_logger())
        assert wi.enabled is False
        assert wi.client is None

    def test_configured_with_google_maps_url_enabled(self):
        weather_conf = {"RefreshIntervalMin": 5, "PreferredProvider": "open_meteo", "OWMAPIKey": ""}
        location_conf = {"GoogleMapsURL": "https://www.google.com/maps/place/Sydney/@-33.8478053,150.602357,10z"}
        wi = WeatherIntegration(_make_config(weather_conf=weather_conf, location_conf=location_conf), _make_logger())
        assert wi.enabled is True
        assert wi.client is not None
        assert wi.refresh_interval == 5
        assert wi.provider == "open_meteo"

    def test_owm_key_from_environment_preferred(self, monkeypatch):
        monkeypatch.setenv("OWM_API_KEY", "env-key-123")
        weather_conf = {"PreferredProvider": "owm", "OWMAPIKey": "config-key"}
        location_conf = {"Latitude": -33.85, "Longitude": 151.2}
        wi = WeatherIntegration(_make_config(weather_conf=weather_conf, location_conf=location_conf), _make_logger())
        assert wi.enabled is True
        # The env key should be used by the underlying client (OWM provider is created).
        assert wi.client._owm is not None

    def test_get_current_reading_none_when_disabled(self):
        wi = WeatherIntegration(_make_config(weather_conf=None), _make_logger())
        assert wi.get_current_reading() is None

    def test_get_current_reading_none_before_fetch(self):
        weather_conf = {"PreferredProvider": "open_meteo"}
        location_conf = {"Latitude": -33.85, "Longitude": 151.2}
        wi = WeatherIntegration(_make_config(weather_conf=weather_conf, location_conf=location_conf), _make_logger())
        assert wi.get_current_reading() is None


# ---------------------------------------------------------------------------
# WeatherConstraint parsing
# ---------------------------------------------------------------------------

class TestWeatherConstraintParsing:
    def test_no_weather_constraint_is_none(self):
        oc = OutputConstraint(
            output_config={},
            name="Test",
            logger=_make_logger(),
            ups_integration=MagicMock(),
            weather_integration=MagicMock(),
            device_output_id=1,
            view=None,
            temp_probe_averager=MagicMock(),
        )
        assert oc.weather_constraint is None

    def test_invalid_action_raises(self):
        with pytest.raises(RuntimeError, match="ActionIfMatch"):
            _make_constraint({"SkyCondition": "rain", "ActionIfMatch": "Nope"}, _make_reading())

    def test_invalid_sky_condition_raises(self):
        with pytest.raises(RuntimeError, match="Invalid SkyCondition"):
            _make_constraint({"SkyCondition": "sunshine", "ActionIfMatch": "TurnOff"}, _make_reading())

    def test_sky_conditions_parsed_case_insensitive(self):
        oc = _make_constraint({"SkyCondition": "overcast, RAIN, Snow", "ActionIfMatch": "TurnOff"}, _make_reading())
        assert oc.weather_constraint["sky_conditions"] == {
            WeatherCondition.OVERCAST, WeatherCondition.RAIN, WeatherCondition.SNOW,
        }


# ---------------------------------------------------------------------------
# WeatherConstraint evaluation (OR logic)
# ---------------------------------------------------------------------------

class TestWeatherConstraintEvaluation:
    CFG = {
        "SkyCondition": "overcast, drizzle, rain, snow, thunderstorm",
        "TemperatureBelow": 20.0,
        "PrecipitationProbabilityAbove": 0.5,
        "ActionIfMatch": "TurnOff",
    }

    def test_no_constraint_returns_auto(self):
        oc = OutputConstraint(
            output_config={},
            name="Test",
            logger=_make_logger(),
            ups_integration=MagicMock(),
            weather_integration=MagicMock(),
            device_output_id=1,
            view=None,
            temp_probe_averager=MagicMock(),
        )
        assert oc.get_weather_constraint_status() == WeatherMode.AUTO

    def test_no_reading_returns_auto(self):
        oc = _make_constraint(self.CFG, reading=None)
        assert oc.get_weather_constraint_status() == WeatherMode.AUTO

    def test_no_criteria_match_returns_auto(self):
        oc = _make_constraint(self.CFG, _make_reading(WeatherCondition.CLEAR, 25.0, 0.1))
        assert oc.get_weather_constraint_status() == WeatherMode.AUTO

    def test_sky_condition_match_turns_off(self):
        oc = _make_constraint(self.CFG, _make_reading(WeatherCondition.RAIN, 25.0, 0.1))
        assert oc.get_weather_constraint_status() == WeatherMode.TURN_OFF

    def test_temperature_below_match_turns_off(self):
        oc = _make_constraint(self.CFG, _make_reading(WeatherCondition.CLEAR, 15.0, 0.1))
        assert oc.get_weather_constraint_status() == WeatherMode.TURN_OFF

    def test_precip_above_match_turns_off(self):
        oc = _make_constraint(self.CFG, _make_reading(WeatherCondition.CLEAR, 25.0, 0.8))
        assert oc.get_weather_constraint_status() == WeatherMode.TURN_OFF

    def test_temperature_above_match(self):
        cfg = {"TemperatureAbove": 30.0, "ActionIfMatch": "TurnOff"}
        oc = _make_constraint(cfg, _make_reading(WeatherCondition.CLEAR, 35.0, 0.1))
        assert oc.get_weather_constraint_status() == WeatherMode.TURN_OFF

    def test_precip_below_match(self):
        cfg = {"PrecipitationProbabilityBelow": 0.2, "ActionIfMatch": "TurnOn"}
        oc = _make_constraint(cfg, _make_reading(WeatherCondition.CLEAR, 25.0, 0.05))
        assert oc.get_weather_constraint_status() == WeatherMode.TURN_ON

    def test_action_turn_on(self):
        cfg = dict(self.CFG, ActionIfMatch="TurnOn")
        oc = _make_constraint(cfg, _make_reading(WeatherCondition.RAIN, 25.0, 0.1))
        assert oc.get_weather_constraint_status() == WeatherMode.TURN_ON

    def test_boundary_not_inclusive(self):
        """Thresholds use strict comparisons: exactly at the boundary is not a match."""
        cfg = {"TemperatureBelow": 20.0, "PrecipitationProbabilityAbove": 0.5, "ActionIfMatch": "TurnOff"}
        oc = _make_constraint(cfg, _make_reading(WeatherCondition.CLEAR, 20.0, 0.5))
        assert oc.get_weather_constraint_status() == WeatherMode.AUTO
