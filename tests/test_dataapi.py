"""Tests for the DataAPI FastAPI application.

Uses FastAPI's TestClient (synchronous) to exercise all endpoints without
starting a real server.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from dataapi import create_asgi_app

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

SAMPLE_OUTPUT_DATA = {
    "LastRefresh": "2025-01-01T00:00:00",
    "Outputs": [{"Name": "Network Rack", "IsOn": True}],
}

SAMPLE_METER_DATA = {
    "LastRefresh": "2025-01-01T00:00:00",
    "Meters": [{"Name": "Panel EM1.1", "Power": 150.0}],
}

SAMPLE_PROBE_DATA = {
    "LastRefresh": "2025-01-01T00:00:00",
    "TempProbes": [{"Name": "Network Rack", "Temperature": 24.5}],
}

SAMPLE_PRICE_DATA = {
    "LastRefresh": "2025-01-01T00:00:00",
    "EnergyPrices": [{"StartTime": "10:00", "Price": 15.0}],
}

SAMPLE_ALL_DATA = {
    "LastRefresh": "2025-01-01T00:00:00",
    "Outputs": [],
    "Meters": [],
    "TempProbes": [],
    "EnergyPrices": [],
}


def _make_controller(data_map: dict | None = None, all_data: dict | None = None):
    """Return a mock controller that serves different data per 'entry' argument."""
    ctrl = MagicMock()
    data_map = data_map or {
        "Outputs": SAMPLE_OUTPUT_DATA,
        "Meters": SAMPLE_METER_DATA,
        "TempProbes": SAMPLE_PROBE_DATA,
        "EnergyPrices": SAMPLE_PRICE_DATA,
    }

    def _get_api_data(entry=None):
        if entry is None:
            return all_data or SAMPLE_ALL_DATA
        return data_map.get(entry, {})

    ctrl.get_api_data.side_effect = _get_api_data
    return ctrl


@pytest.fixture
def client(logger):
    """TestClient for the DataAPI with no access key configured."""
    ctrl = _make_controller()

    # Ensure no access key is configured (open access)
    def _get(*keys, default=None):
        if keys == ("DataAPI", "AccessKey"):
            return None
        return default

    cfg = MagicMock()
    cfg.get.side_effect = _get

    app = create_asgi_app(ctrl, cfg, logger)
    return TestClient(app)


@pytest.fixture
def secured_client(logger):
    """TestClient for the DataAPI with access key 'test-secret-key'."""
    ctrl = _make_controller()

    def _get(*keys, default=None):
        if keys == ("DataAPI", "AccessKey"):
            return "test-secret-key"
        return default

    cfg = MagicMock()
    cfg.get.side_effect = _get

    app = create_asgi_app(ctrl, cfg, logger)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def empty_data_client(logger):
    """TestClient whose controller returns empty/None data for all entries."""
    ctrl = MagicMock()
    ctrl.get_api_data.return_value = None

    def _get(*_keys, default=None):
        return default

    cfg = MagicMock()
    cfg.get.side_effect = _get

    app = create_asgi_app(ctrl, cfg, logger)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Root endpoint
# ---------------------------------------------------------------------------

class TestRootEndpoint:
    def test_root_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_root_returns_api_info(self, client):
        resp = client.get("/")
        body = resp.json()
        assert body["name"] == "PowerController Data API"
        assert "endpoints" in body
        assert "/outputs" in body["endpoints"]


# ---------------------------------------------------------------------------
# /outputs
# ---------------------------------------------------------------------------

class TestOutputsEndpoint:
    def test_returns_200_with_data(self, client):
        resp = client.get("/outputs")
        assert resp.status_code == 200

    def test_returns_output_data(self, client):
        body = client.get("/outputs").json()
        assert "Outputs" in body

    def test_returns_503_when_no_data(self, empty_data_client):
        resp = empty_data_client.get("/outputs")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /meters
# ---------------------------------------------------------------------------

class TestMetersEndpoint:
    def test_returns_200_with_data(self, client):
        resp = client.get("/meters")
        assert resp.status_code == 200

    def test_returns_meter_data(self, client):
        body = client.get("/meters").json()
        assert "Meters" in body

    def test_returns_503_when_no_data(self, empty_data_client):
        resp = empty_data_client.get("/meters")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /tempprobes
# ---------------------------------------------------------------------------

class TestTempProbesEndpoint:
    def test_returns_200_with_data(self, client):
        resp = client.get("/tempprobes")
        assert resp.status_code == 200

    def test_returns_probe_data(self, client):
        body = client.get("/tempprobes").json()
        assert "TempProbes" in body

    def test_returns_503_when_no_data(self, empty_data_client):
        resp = empty_data_client.get("/tempprobes")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /energyprices
# ---------------------------------------------------------------------------

class TestEnergyPricesEndpoint:
    def test_returns_200_with_data(self, client):
        resp = client.get("/energyprices")
        assert resp.status_code == 200

    def test_returns_price_data(self, client):
        body = client.get("/energyprices").json()
        assert "EnergyPrices" in body

    def test_returns_503_when_no_data(self, empty_data_client):
        resp = empty_data_client.get("/energyprices")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /all
# ---------------------------------------------------------------------------

class TestAllEndpoint:
    def test_returns_200_with_data(self, client):
        resp = client.get("/all")
        assert resp.status_code == 200

    def test_returns_503_when_no_data(self, empty_data_client):
        resp = empty_data_client.get("/all")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Access key enforcement
# ---------------------------------------------------------------------------

class TestAccessKeyEnforcement:
    def test_missing_key_returns_401(self, secured_client):
        resp = secured_client.get("/outputs")
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self, secured_client):
        resp = secured_client.get("/outputs", params={"access_key": "wrong-key"})
        assert resp.status_code == 401

    def test_correct_key_via_url_param_returns_200(self, secured_client):
        resp = secured_client.get("/outputs", params={"access_key": "test-secret-key"})
        assert resp.status_code == 200

    def test_correct_key_via_bearer_header_returns_200(self, secured_client):
        resp = secured_client.get(
            "/outputs",
            headers={"Authorization": "Bearer test-secret-key"},
        )
        assert resp.status_code == 200

    def test_correct_key_via_x_access_key_header_returns_200(self, secured_client):
        resp = secured_client.get(
            "/outputs",
            headers={"X-Access-Key": "test-secret-key"},
        )
        assert resp.status_code == 200

    def test_url_param_takes_priority_over_header(self, secured_client):
        """URL param with correct key overrides any header with wrong key."""
        resp = secured_client.get(
            "/outputs",
            params={"access_key": "test-secret-key"},
            headers={"X-Access-Key": "wrong-key"},
        )
        assert resp.status_code == 200

    def test_open_access_when_no_key_configured(self, client):
        """No access key configured → any request is allowed."""
        resp = client.get("/outputs")
        assert resp.status_code == 200

    def test_env_var_access_key_takes_priority(self, logger):
        """DATAAPI_ACCESS_KEY env var takes priority over config value."""
        ctrl = _make_controller()

        def _get(*keys, default=None):
            if keys == ("DataAPI", "AccessKey"):
                return "config-key"  # Would be wrong
            return default

        cfg = MagicMock()
        cfg.get.side_effect = _get

        app = create_asgi_app(ctrl, cfg, logger)
        with patch.dict(os.environ, {"DATAAPI_ACCESS_KEY": "env-key"}), TestClient(app) as c:
        # config key should be rejected
            resp = c.get("/outputs", params={"access_key": "config-key"})
            assert resp.status_code == 401
            # env key should work
            resp = c.get("/outputs", params={"access_key": "env-key"})
            assert resp.status_code == 200


class TestGracefulShutdownTimeout:
    """Regression tests for issue #126.

    The Data API server runs in a non-daemon thread. Without a bounded graceful-shutdown
    timeout, a lingering connection can keep that thread alive and block the whole
    process from exiting until it is SIGKILLed. ``serve_asgi_blocking`` must give uvicorn
    a bounded timeout.
    """

    def test_serve_configures_bounded_graceful_shutdown(self, logger, monkeypatch):
        import threading

        import dataapi

        captured: dict[str, object] = {}

        class _FakeServer:
            def __init__(self, config):
                captured["config"] = config
                self.should_exit = False

            def install_signal_handlers(self) -> None:  # pragma: no cover - replaced
                pass

            async def serve(self) -> None:
                return None

        monkeypatch.setattr(dataapi.uvicorn, "Server", _FakeServer)

        cfg = MagicMock()
        cfg.get.side_effect = lambda *keys, default=None: default  # noqa: ARG005

        stop_event = threading.Event()
        stop_event.set()  # resolve the stop watcher immediately

        dataapi.serve_asgi_blocking(MagicMock(), cfg, logger, stop_event)

        config = captured["config"]
        assert config.timeout_graceful_shutdown is not None, (
            "uvicorn must have a bounded graceful-shutdown timeout"
        )
        assert config.timeout_graceful_shutdown == int(
            dataapi.DATA_API_GRACEFUL_SHUTDOWN_TIMEOUT
        )
        assert 0 < config.timeout_graceful_shutdown <= 30
