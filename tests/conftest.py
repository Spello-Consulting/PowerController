"""Shared pytest fixtures for the PowerController test suite."""

import sys
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from mergedeep import merge

# Ensure src/ is importable without PYTHONPATH being set
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from config_schemas import ConfigSchema
from sc_foundation import SCConfigManager, SCLogger
from scheduler import Scheduler
from temp_probe_averager import TempProbeAverager
from ups_integration import UPSIntegration
from weather_integration import WeatherIntegration

from sc_smart_device import SCSmartDevice, SmartDeviceStatus, SmartDeviceView, SmartDeviceWorker, smart_devices_validator

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_CONFIG = str(PROJECT_ROOT / "tests" / "testing.yaml")


# ---------------------------------------------------------------------------
# Core infrastructure fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_test_access_key_env(monkeypatch):
    """Prevent shell env from leaking access-key requirements into tests."""
    monkeypatch.delenv("WEBAPP_ACCESS_KEY", raising=False)
    monkeypatch.delenv("DATAAPI_ACCESS_KEY", raising=False)


@pytest.fixture(scope="session")
def logger() -> SCLogger:
    """Return a silent logger (no file, errors only on console)."""
    return SCLogger({
        "logfile_name": None,
        "console_verbosity": "error",
    })


@pytest.fixture(scope="session")
def config() -> SCConfigManager:
    """Return the test SCConfigManager loaded from tests/testing.yaml."""
    schemas = ConfigSchema()
    merged_schema = merge(deepcopy(schemas.validation), smart_devices_validator)
    assert isinstance(merged_schema, dict)

    return SCConfigManager(
        config_file=TEST_CONFIG,
        validation_schema=merged_schema,
        placeholders=schemas.placeholders,
    )


@pytest.fixture(scope="session")
def scheduler(config, logger):
    """Return a Scheduler built from the test config."""
    return Scheduler(config, logger)


@pytest.fixture(scope="session")
def ups_integration(config, logger):
    """Return an UPSIntegration built from the test config (disabled)."""
    return UPSIntegration(config, logger)


@pytest.fixture(scope="session")
def weather_integration(config, logger):
    """Return a WeatherIntegration built from the test config (disabled unless WeatherClient configured)."""
    return WeatherIntegration(config, logger)


@pytest.fixture(scope="session")
def temp_probe_averager(config, logger):
    """Return a TempProbeAverager built from the test config (disabled unless TempProbeAveragingWindow set)."""
    return TempProbeAverager(config, logger)


@pytest.fixture(scope="session")
def smart_device_wake_event() -> threading.Event:
    """Shared wake event for smart-device test fixtures."""
    return threading.Event()


@pytest.fixture(scope="session")
def smart_device(config, logger, smart_device_wake_event) -> SCSmartDevice:
    """Return an initialized simulated SCSmartDevice instance."""
    smart_device_settings = config.get("SCSmartDevices")
    assert isinstance(smart_device_settings, dict)
    return SCSmartDevice(logger, smart_device_settings, smart_device_wake_event)


# ---------------------------------------------------------------------------
# Smart Device simulation fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def smart_device_worker(config, logger, smart_device, smart_device_wake_event) -> SmartDeviceWorker:
    """Return a SmartDeviceWorker using simulated devices. Worker thread not started."""
    worker = SmartDeviceWorker(
        smart_device,
        logger,
        smart_device_wake_event,
        max_concurrent_errors=config.get("SCSmartDevices", "MaxConcurrentErrors", default=4) or 4,
        critical_error_report_delay_mins=config.get("General", "ReportCriticalErrorsDelay", default=None),
    )
    return worker


@pytest.fixture(scope="session")
def smart_device_workers(smart_device_worker) -> SmartDeviceWorker:
    """Backward-compatible alias for older pluralized test fixtures."""
    return smart_device_worker


@pytest.fixture(scope="session")
def smart_device_view(smart_device_worker) -> SmartDeviceView:
    """Return a SmartDeviceView built from the simulated worker's initial snapshot."""
    return smart_device_worker.get_latest_status()


# ---------------------------------------------------------------------------
# Synthetic SmartDeviceView builder (for unit tests that need full control)
# ---------------------------------------------------------------------------

def make_smart_device_status(
    device_online: bool = True,
    output_state: bool = False,
    device_id: int = 1,
    output_id: int = 1,
    input_id: int = 1,
    meter_id: int = 1,
    temp_probe_id: int = 1,
    temp_probe_temp: float | None = 25.0,
) -> SmartDeviceStatus:
    """Build a minimal SmartDeviceStatus for unit tests."""
    return SmartDeviceStatus(
        devices=[{
            "ID": device_id,
            "Name": "Test Device",
            "Online": device_online,
            "ExpectOffline": False,
            "Temperature": None,
        }],
        outputs=[{
            "ID": output_id,
            "Name": "Network Rack O1",
            "DeviceID": device_id,
            "State": output_state,
        }],
        inputs=[{
            "ID": input_id,
            "Name": "Input 1",
            "DeviceID": device_id,
            "State": False,
        }],
        meters=[{
            "ID": meter_id,
            "Name": "Meter 1",
            "DeviceID": device_id,
            "Energy": 0.0,
            "Power": 0.0,
        }],
        temp_probes=[{
            "ID": temp_probe_id,
            "Name": "Network Rack",
            "DeviceID": device_id,
            "Temperature": temp_probe_temp,
        }],
    )


@pytest.fixture
def synthetic_view() -> SmartDeviceView:
    """Return a SmartDeviceView built from minimal synthetic data (device online, output off)."""
    return SmartDeviceView(snapshot=make_smart_device_status())


# ---------------------------------------------------------------------------
# Mock controller factory (for DataAPI / webapp tests)
# ---------------------------------------------------------------------------

def make_mock_controller(api_data: dict | None = None, webapp_data: dict | None = None) -> Any:
    """Return a MagicMock controller that returns supplied data from its API methods."""
    ctrl = MagicMock()
    ctrl.get_api_data.return_value = api_data or {"LastRefresh": "2025-01-01T00:00:00"}
    ctrl.get_webapp_data.return_value = webapp_data or {
        "global": {"label": "Test"},
        "outputs": {},
    }
    ctrl.is_valid_output_id.return_value = True
    ctrl.post_command.return_value = None
    return ctrl
