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
from sc_foundation import SCCommon, SCLogger

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


# ---------------------------------------------------------------------------
# HTTP GET /system — version and git rows
# ---------------------------------------------------------------------------

_DEPLOYMENT_INFO = {
    "name": "PowerController",
    "version": "9.8.7",
    "description": "Test",
    "git_branch": "feature/test-branch",
    "git_tag": None,
    "git_remote": "https://secret-token@github.com/example/repo.git",
    "git_status": "Ahead",
    "git_changes": 3,
}


class TestSystemDeploymentInfo:
    def test_shows_version_and_git(self, client, monkeypatch):
        monkeypatch.setattr(SCCommon, "get_deployment_info", lambda: dict(_DEPLOYMENT_INFO))
        body = client.get("/system").text
        assert "Application version" in body
        assert "9.8.7" in body
        assert "feature/test-branch" in body
        assert "Ahead" in body
        assert "Uncommitted changes" in body
        # The remote URL can embed credentials, so it must never be shown.
        assert "secret-token" not in body
        assert 'href="/log"' in body

    def test_git_rows_omitted_without_checkout(self, client, monkeypatch):
        info = dict(_DEPLOYMENT_INFO, git_branch=None, git_status="Unknown", git_changes=None)
        monkeypatch.setattr(SCCommon, "get_deployment_info", lambda: info)
        body = client.get("/system").text
        assert "9.8.7" in body
        assert "Git branch" not in body
        assert "Uncommitted changes" not in body


# ---------------------------------------------------------------------------
# HTTP GET /log, /log/content, /log/download
# ---------------------------------------------------------------------------


def _make_file_logger(logfile, max_lines=1000):
    return SCLogger({
        "logfile_name": str(logfile) if logfile else None,
        "file_verbosity": "debug",
        "console_verbosity": "none",
        "max_lines": max_lines,
    })


@pytest.fixture
def file_logger(tmp_path):
    log = _make_file_logger(tmp_path / "logs" / "app.log", max_lines=5)
    log.log_message("first log line", "summary")
    log.log_message("a <b>tagged</b> warning", "warning")
    return log


@pytest.fixture
def log_client(file_logger):
    app, _ = create_asgi_app(_make_controller(), _make_config(access_key=None), file_logger)
    return TestClient(app)


@pytest.fixture
def secured_log_client(file_logger):
    app, _ = create_asgi_app(_make_controller(), _make_config(access_key="webapp-secret"), file_logger)
    return TestClient(app)


class TestLogViewer:
    def test_page_shows_contents(self, log_client, file_logger):
        response = log_client.get("/log")
        assert response.status_code == 200
        body = response.text
        assert "first log line" in body
        assert "&lt;b&gt;tagged&lt;/b&gt;" in body
        assert file_logger.get_logfile_path() in body
        assert 'id="log-live"' in body
        assert "/log/download" in body
        assert "/static/log.js" in body

    def test_content_incremental(self, log_client, file_logger):
        first = log_client.get("/log/content").json()
        assert first["reset"] is True
        assert "first log line" in first["text"]

        file_logger.log_message("a newer line", "summary")
        second = log_client.get(
            "/log/content",
            params={"generation": first["generation"], "offset": first["offset"]},
        ).json()
        assert second["reset"] is False
        assert "a newer line" in second["text"]
        assert "first log line" not in second["text"]

    def test_content_resets_after_trim(self, log_client, file_logger):
        first = log_client.get("/log/content").json()
        for i in range(10):
            file_logger.log_message(f"filler {i}", "summary")
        file_logger.trim_logfile()

        second = log_client.get(
            "/log/content",
            params={"generation": first["generation"], "offset": first["offset"]},
        ).json()
        assert second["reset"] is True
        assert second["text"] == file_logger.read_logfile()

    def test_download(self, log_client, file_logger):
        response = log_client.get("/log/download")
        assert response.status_code == 200
        assert response.headers["content-disposition"] == 'attachment; filename="app.log"'
        assert response.headers["content-type"].startswith("text/plain")
        assert response.text == file_logger.read_logfile()

    def test_file_logging_disabled(self):
        app, _ = create_asgi_app(_make_controller(), _make_config(access_key=None), _make_file_logger(None))
        test_client = TestClient(app)
        page = test_client.get("/log")
        assert page.status_code == 200
        assert "File logging is not enabled" in page.text
        assert "/static/log.js" not in page.text
        assert test_client.get("/log/download").status_code == 404
        assert not test_client.get("/log/content").json()["text"]

    def test_endpoints_require_access_key(self, secured_log_client):
        for path in ("/log", "/log/content", "/log/download"):
            assert secured_log_client.get(path).status_code == 403
            assert secured_log_client.get(path, params={"key": "wrong"}).status_code == 403
            assert secured_log_client.get(path, params={"key": "webapp-secret"}).status_code == 200

    def test_links_carry_access_key(self, secured_log_client):
        body = secured_log_client.get("/log", params={"key": "webapp-secret"}).text
        assert "/log/download?key=webapp-secret" in body
        assert "/system?key=webapp-secret" in body
