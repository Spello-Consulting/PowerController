"""Tests for state-file saving under concurrent shutdown.

Regression tests for issue #121: on SIGTERM both the controller thread (when its run
loop exits) and ``ThreadManager.stop_all()`` (main thread) call
``PowerController.shutdown()`` at the same time. The two concurrent
``_save_system_state()`` calls raced on the shared ``.tmp`` file and the loser failed
with a blank ``"Error saving system state: "`` fatal error (the real
``FileNotFoundError`` was hidden inside ``JSONEncoder.save_to_file``'s message-less
``RuntimeError``).
"""

from __future__ import annotations

# This test module deliberately exercises private controller internals and lives
# outside a package, so silence those two test-only lints for the whole file.
# ruff: noqa: INP001, SLF001
import sys
import threading
from pathlib import Path
from threading import Lock
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sc_foundation import JSONEncoder

from controller import PowerController, describe_exception

THREADS = 4
ROUNDS = 25


def make_controller(state_path: Path) -> PowerController:
    """Build a PowerController with just enough state for ``_save_system_state``.

    The controller is created without running ``__init__`` and has its I/O-touching
    collaborators replaced with mocks, so the only real work is the state-file write.

    Args:
        state_path (Path): Where the controller should write its state file.

    Returns:
        PowerController: A controller instance suitable for exercising the save path.
    """
    ctl = object.__new__(PowerController)
    ctl.logger = MagicMock()
    ctl.app_label = "PowerController"
    ctl.currency_major_symbol = "$"
    ctl.currency_minor_symbol = "c"
    ctl.save_tesla_raw_data = False
    ctl.tesla_charge_data = {}
    ctl.temp_probe_logging = {"enabled": False, "history": []}
    # A chunk of serialisable data so each write takes long enough for an unserialised
    # race to show up reliably.
    ctl.output_metering = {
        "Meters": [
            {"Output": f"Output{i}", "Usage": [{"Date": f"2099-01-{d % 28 + 1:02d}", "EnergyUsed": d} for d in range(50)]}
            for i in range(100)
        ],
    }
    ctl.outputs = []
    ctl.scheduler = MagicMock()
    ctl.scheduler.get_save_object.return_value = {}

    ctl._save_lock = Lock()
    ctl._shutdown_lock = Lock()
    ctl._shutdown_done = False

    ctl._save_output_usage_data = MagicMock()
    ctl._post_state_to_web_viewer = MagicMock()
    ctl._get_system_state_path = MagicMock(return_value=state_path)
    ctl._get_latest_status_view = MagicMock(return_value=MagicMock())
    return ctl


def _run_concurrently(target: Any, count: int) -> None:
    """Start ``count`` threads on ``target`` behind a barrier and wait for them.

    Args:
        target (Any): Zero-argument callable each thread runs.
        count (int): Number of threads to start.
    """
    barrier = threading.Barrier(count)

    def worker() -> None:
        barrier.wait()
        target()

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_concurrent_saves_do_not_fail(tmp_path: Path) -> None:
    """Many threads saving at once all succeed and leave a valid state file."""
    ctl = make_controller(tmp_path / "system_state.json")
    view = MagicMock()

    for _ in range(ROUNDS):
        _run_concurrently(lambda: ctl._save_system_state(view), THREADS)

    ctl.logger.log_fatal_error.assert_not_called()
    state = JSONEncoder.read_from_file(tmp_path / "system_state.json")
    assert isinstance(state, dict)
    assert state["StateFileType"] == "PowerController"
    assert not (tmp_path / "system_state.tmp").exists()


def test_concurrent_shutdown_saves_once(tmp_path: Path) -> None:
    """Concurrent ``shutdown()`` calls (controller thread + stop_all) save exactly once."""
    ctl = make_controller(tmp_path / "system_state.json")

    with patch.object(JSONEncoder, "save_to_file", wraps=JSONEncoder.save_to_file) as spy:
        _run_concurrently(ctl.shutdown, 2)
        ctl.shutdown()  # a later call is also a no-op

    assert spy.call_count == 1
    ctl.logger.log_fatal_error.assert_not_called()
    start_msgs = [c for c in ctl.logger.log_message.call_args_list if "Starting PowerController shutdown" in c.args[0]]
    assert len(start_msgs) == 1


def test_save_failure_logs_underlying_cause(tmp_path: Path) -> None:
    """A failed save logs the chained cause instead of a blank message."""
    ctl = make_controller(tmp_path / "system_state.json")
    view = MagicMock()

    def failing_save(_data: object, _path: Path) -> bool:
        try:
            raise FileNotFoundError(2, "No such file or directory")
        except OSError as e:
            raise RuntimeError from e

    with patch.object(JSONEncoder, "save_to_file", side_effect=failing_save):
        ctl._save_system_state(view)

    ctl.logger.log_fatal_error.assert_called_once()
    message = ctl.logger.log_fatal_error.call_args.args[0]
    assert "FileNotFoundError" in message
    assert "No such file or directory" in message
    assert str(tmp_path / "system_state.json") in message


def _raise_chained() -> None:
    """Raise a message-less RuntimeError chained from a ValueError, like JSONEncoder does.

    Raises:
        RuntimeError: Always, chained from a ValueError.
    """
    inner = ValueError("bad value")
    raise RuntimeError from inner


def test_describe_exception_formats_chain() -> None:
    """``describe_exception`` walks the cause chain and omits empty messages."""
    with pytest.raises(RuntimeError) as exc_info:
        _raise_chained()
    assert describe_exception(exc_info.value) == "RuntimeError <- ValueError: bad value"
    assert describe_exception(OSError("disk full")) == "OSError: disk full"
