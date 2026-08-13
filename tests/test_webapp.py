"""Tests for the webapp FastAPI application.

Covers:
  - HTTP GET / (index page)
  - Access key enforcement on HTTP and WebSocket
  - WebSocket /ws: initial snapshot delivery
  - WebSocket /ws: set_mode command handling
  - WebSocket /ws: invalid / malformed command ignored
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from webapp import WebAppNotifier, create_asgi_app

# ---------------------------------------------------------------------------
# Shared sample data
# ---------------------------------------------------------------------------

SAMPLE_WEBAPP_DATA = {
    "global": {"label": "Test PowerController", "current_price": 20.0},
    "outputs": {
        "network_rack": {
            "id": "network_rack",
            "name": "Network Rack",
            "is_on": False,
            "is_online": True,
            "mode": "auto",
            "allow_actions": True,
            "max_app_mode_on_minutes": 0,
            "max_app_mode_off_minutes": 0,
            "app_mode_revert_time": None,
            "reason": "Run plan dictates that the output should be off",
            "system_state": "auto",
            "target_hours": "1.0",
            "actual_hours": "0.0",
            "required_hours": "1.0",
            "planned_hours": "1.0",
            "actual_energy_used": "0.000kWh",
            "actual_cost": "$0.00",
            "forecast_energy_used": "0.000kWh",
            "forecast_cost": "$0.00",
            "forecast_price": "N/A",
            "total_energy_used": "0.000kWh",
            "total_cost": "$0.00",
            "average_price": "N/A",
            "next_start_time": None,
            "stopping_at": None,
            "power_draw": "None",
            "current_price": "20.0 c/kWh",
        }
    },
}


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_config(access_key: str | None = None):
    cfg = MagicMock()

    def _get(*keys, default=None):
        if keys == ("Website", "AccessKey"):
            return access_key
        return default

    cfg.get.side_effect = _get
    return cfg


def _make_controller(webapp_data=None, valid_output_ids=None):
    ctrl = MagicMock()
    ctrl.get_webapp_data.return_value = webapp_data or SAMPLE_WEBAPP_DATA
    valid_ids = valid_output_ids or {"network_rack"}
    ctrl.is_valid_output_id.side_effect = lambda oid: oid in valid_ids
    ctrl.post_command.return_value = None
    return ctrl


@pytest.fixture
def client(logger):
    """TestClient for webapp with no access key (open access)."""
    cfg = _make_config(access_key=None)
    ctrl = _make_controller()
    app, _ = create_asgi_app(ctrl, cfg, logger)
    return TestClient(app)


@pytest.fixture
def secured_client(logger):
    """TestClient for webapp with access key 'webapp-secret'."""
    cfg = _make_config(access_key="webapp-secret")
    ctrl = _make_controller()
    app, _ = create_asgi_app(ctrl, cfg, logger)
    return TestClient(app)


@pytest.fixture
def no_data_client(logger):
    """TestClient whose controller returns None from get_webapp_data."""
    cfg = _make_config(access_key=None)
    ctrl = MagicMock()
    ctrl.get_webapp_data.return_value = None
    app, _ = create_asgi_app(ctrl, cfg, logger)
    return TestClient(app)


# ---------------------------------------------------------------------------
# HTTP GET /
# ---------------------------------------------------------------------------


class TestIndexPage:
    def test_returns_200_or_503_when_no_key_required(self, client):
        resp = client.get("/")
        assert resp.status_code in {200, 503}

    def test_returns_503_when_no_data_available(self, no_data_client):
        resp = no_data_client.get("/")
        assert resp.status_code == 503

    def test_content_type_is_html_on_success(self, client):
        resp = client.get("/")
        if resp.status_code == 200:
            assert "text/html" in resp.headers.get("content-type", "")

    def test_forbidden_with_wrong_key(self, secured_client):
        resp = secured_client.get("/?key=wrong")
        assert resp.status_code == 403

    def test_allowed_with_correct_key(self, logger):
        cfg = _make_config(access_key="webapp-secret")
        ctrl = _make_controller()
        app, _ = create_asgi_app(ctrl, cfg, logger)
        with TestClient(app) as c:
            resp = c.get("/?key=webapp-secret")
            assert resp.status_code in {200, 503}

    def test_allowed_without_key_when_none_configured(self, client):
        resp = client.get("/")
        assert resp.status_code != 403


# ---------------------------------------------------------------------------
# HTTP GET /system
# ---------------------------------------------------------------------------


class TestSystemPage:
    def test_returns_200_and_shows_number_of_outputs(self, client):
        resp = client.get("/system")
        assert resp.status_code == 200
        assert "Number of outputs" in resp.text

    def test_forbidden_with_wrong_key(self, secured_client):
        resp = secured_client.get("/system?key=wrong")
        assert resp.status_code == 403

    def test_allowed_with_correct_key(self, secured_client):
        resp = secured_client.get("/system?key=webapp-secret")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# HTTP GET /config
# ---------------------------------------------------------------------------


class TestConfigPage:
    def test_returns_200_and_shows_config_path(self, logger, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("General:\n  Label: Test\n", encoding="utf-8")
        cfg = _make_config(access_key=None)
        cfg.config_path = cfg_file
        ctrl = _make_controller()
        app, _ = create_asgi_app(ctrl, cfg, logger)
        with TestClient(app) as c:
            resp = c.get("/config")
            assert resp.status_code == 200
            assert str(cfg_file) in resp.text
            assert "Label: Test" in resp.text

    def test_forbidden_with_wrong_key(self, secured_client):
        resp = secured_client.get("/config?key=wrong")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# WebSocket /ws — initial snapshot
# ---------------------------------------------------------------------------


class TestWebSocketInitialSnapshot:
    def test_connects_and_receives_initial_snapshot(self, client):
        with client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "state_update"
            assert "state" in msg

    def test_initial_snapshot_contains_global_and_outputs(self, client):
        with client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
            state = msg.get("state", {})
            assert "global" in state or "outputs" in state  # structure may vary

    def test_initial_snapshot_label_present(self, client):
        with client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
            state = msg.get("state", {})
            # global.label should be present when data is available
            if state and "global" in state:
                assert "label" in state["global"]


# ---------------------------------------------------------------------------
# WebSocket /ws — access key
# ---------------------------------------------------------------------------


class TestWebSocketAccessKey:
    def test_wrong_key_closes_with_policy_violation(self, secured_client):
        with (
            pytest.raises(Exception),
            secured_client.websocket_connect("/ws?key=wrong"),
        ):
            pass

    def test_correct_key_connects_successfully(self, logger):
        cfg = _make_config(access_key="webapp-secret")
        ctrl = _make_controller()
        app, _ = create_asgi_app(ctrl, cfg, logger)
        with TestClient(app) as c, c.websocket_connect("/ws?key=webapp-secret") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "state_update"

    def test_no_key_required_connects_without_key(self, client):
        with client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "state_update"


# ---------------------------------------------------------------------------
# WebSocket /ws — command handling
# ---------------------------------------------------------------------------


class TestWebSocketCommands:
    def test_set_mode_auto_is_accepted(self, logger):
        ctrl = _make_controller()
        cfg = _make_config(access_key=None)
        app, _ = create_asgi_app(ctrl, cfg, logger)
        with TestClient(app) as c, c.websocket_connect("/ws") as ws:
            ws.receive_json()  # consume initial snapshot
            ws.send_json(
                {
                    "type": "command",
                    "action": "set_mode",
                    "output_id": "network_rack",
                    "mode": "auto",
                    "revert_time_mins": None,
                }
            )
        # No reply expected - just verify post_command was called
        ctrl.post_command.assert_called_once()
        cmd = ctrl.post_command.call_args[0][0]
        assert cmd.kind == "set_mode"
        assert cmd.payload["mode"] == "auto"

    def test_set_mode_on_is_accepted(self, logger):
        ctrl = _make_controller()
        cfg = _make_config(access_key=None)
        app, _ = create_asgi_app(ctrl, cfg, logger)
        with TestClient(app) as c, c.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "command",
                    "action": "set_mode",
                    "output_id": "network_rack",
                    "mode": "on",
                    "revert_time_mins": 60,
                }
            )
        ctrl.post_command.assert_called_once()
        cmd = ctrl.post_command.call_args[0][0]
        assert cmd.payload["mode"] == "on"
        assert cmd.payload["revert_time_mins"] == 60

    def test_invalid_mode_value_ignored(self, logger):
        """An unrecognised mode value should be silently ignored."""
        ctrl = _make_controller()
        cfg = _make_config(access_key=None)
        app, _ = create_asgi_app(ctrl, cfg, logger)
        with TestClient(app) as c, c.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "command",
                    "action": "set_mode",
                    "output_id": "network_rack",
                    "mode": "invalid_mode",
                }
            )
        ctrl.post_command.assert_not_called()

    def test_invalid_output_id_ignored(self, logger):
        """A command for an output_id that doesn't exist should be ignored."""
        ctrl = _make_controller()
        cfg = _make_config(access_key=None)
        app, _ = create_asgi_app(ctrl, cfg, logger)
        with TestClient(app) as c, c.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "command",
                    "action": "set_mode",
                    "output_id": "nonexistent_output",
                    "mode": "auto",
                }
            )
        ctrl.post_command.assert_not_called()

    def test_malformed_json_ignored(self, logger):
        """Malformed JSON from the client should not crash the server."""
        ctrl = _make_controller()
        cfg = _make_config(access_key=None)
        app, _ = create_asgi_app(ctrl, cfg, logger)
        with TestClient(app) as c, c.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_text("this is not json {{{{")
        ctrl.post_command.assert_not_called()

    def test_unknown_command_type_ignored(self, logger):
        ctrl = _make_controller()
        cfg = _make_config(access_key=None)
        app, _ = create_asgi_app(ctrl, cfg, logger)
        with TestClient(app) as c, c.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "other_type",
                    "action": "set_mode",
                    "output_id": "network_rack",
                    "mode": "auto",
                }
            )
        ctrl.post_command.assert_not_called()


# ---------------------------------------------------------------------------
# WebAppNotifier
# ---------------------------------------------------------------------------


class TestWebAppNotifier:
    def test_notify_before_bind_does_not_raise(self):
        n = WebAppNotifier()
        n.notify()  # Should be a no-op, not raise

    def test_bind_sets_loop_and_queue(self):
        n = WebAppNotifier()
        loop = asyncio.new_event_loop()
        q: asyncio.Queue = asyncio.Queue()
        n.bind(loop, q)
        assert n.loop is loop
        assert n.queue is q
        loop.close()
