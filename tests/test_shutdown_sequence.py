"""Tests for the SIGTERM shutdown sequence (issue #121, review follow-up).

Two further races beyond the double-save guard:

1. ``ThreadManager.stop_all()`` calls the first of ``stop()``/``shutdown()``/``close()``
   on the thread target's instance. Without a ``stop()`` method it called
   ``shutdown()`` from the main thread, possibly while the controller thread was still
   in a tick. ``PowerController.stop()`` now only wakes the loop, so ``shutdown()`` runs
   on the controller thread after the tick finishes.
2. The smart device worker is registered before the controller, so ``stop_all()`` would
   stop it before ``shutdown()`` could use it to force outputs off. ``shutdown_threads()``
   joins the controller thread first, while the worker is still alive.
"""

from __future__ import annotations

# This test module exercises private controller internals and lives outside a package,
# so silence those two test-only lints for the whole file.
# ruff: noqa: INP001, SLF001
import sys
import threading
import time
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sc_foundation import ThreadManager

from controller import PowerController
from main import CONTROLLER_SHUTDOWN_TIMEOUT, shutdown_threads

# ThreadManager.stop_all() resolves the instance stopper in this order.
STOPPER_RESOLUTION_ORDER = ("stop", "shutdown", "close")


def _bare_controller() -> PowerController:
    """Build a PowerController with only the attributes stop()/shutdown() touch.

    Returns:
        PowerController: An instance created without running ``__init__``.
    """
    ctl = object.__new__(PowerController)
    ctl.wake_event = Event()
    ctl._shutdown_lock = threading.Lock()
    ctl._shutdown_done = False
    return ctl


def test_stop_wakes_loop_without_shutting_down() -> None:
    """stop() wakes the loop but performs no shutdown work."""
    ctl = _bare_controller()
    assert not ctl.wake_event.is_set()

    ctl.stop()

    assert ctl.wake_event.is_set()
    # stop() must not run shutdown(): the controller thread does that itself.
    assert ctl._shutdown_done is False


def test_stop_all_resolves_to_stop_not_shutdown() -> None:
    """ThreadManager.stop_all() would pick stop() over shutdown() for the controller."""
    ctl = _bare_controller()

    resolved = next((m for m in STOPPER_RESOLUTION_ORDER if callable(getattr(ctl, m, None))), None)
    assert resolved == "stop"

    # Invoking the resolved stopper must only wake the loop, not shut down.
    getattr(ctl, resolved)()
    assert ctl.wake_event.is_set()
    assert ctl._shutdown_done is False


class _FakeWorker:
    """A minimal stand-in for SmartDeviceWorker that records when it is stopped."""

    def __init__(self, order: list[tuple[str, bool]]) -> None:
        self._order = order
        self._stop_event = threading.Event()
        self.running = threading.Event()

    def run(self) -> None:
        """Loop until stop() is called, mirroring the real worker's own stop event."""
        self.running.set()
        while not self._stop_event.is_set():
            time.sleep(0.005)
        self.running.clear()

    def stop(self) -> None:
        """Record the stop (and whether the worker is still live) then exit the loop."""
        self._order.append(("worker_stop", self.running.is_set()))
        self._stop_event.set()


class _FakeController:
    """A minimal controller whose run() calls shutdown() on its own thread."""

    def __init__(self, worker: _FakeWorker, wake_event: Event, order: list[tuple[str, bool]]) -> None:
        self._worker = worker
        self.wake_event = wake_event
        self._order = order
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

    def run(self, stop_event: Event) -> None:
        """Tick until stop_event is set, then shut down — just like PowerController.run()."""
        while not stop_event.is_set():
            self.wake_event.wait(timeout=0.05)
            self.wake_event.clear()
        self.shutdown()

    def stop(self) -> None:
        """Only wake the loop (the issue #121 fix)."""
        self.wake_event.set()

    def shutdown(self) -> None:
        """Record shutdown and whether the worker is still alive to serve it."""
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        self._order.append(("controller_shutdown", self._worker.running.is_set()))


def test_controller_shuts_down_before_worker_is_stopped() -> None:
    """shutdown_threads() drains the controller (while the worker lives) before stopping it."""
    logger = MagicMock()
    stop_event = Event()
    wake_event = Event()
    order: list[tuple[str, bool]] = []

    worker = _FakeWorker(order)
    controller = _FakeController(worker, wake_event, order)

    tm = ThreadManager(logger, global_stop=stop_event)
    # Worker registered first, exactly as in main.py.
    tm.add(name="smart device", target=worker.run, stop_event=stop_event)
    controller_thread = tm.add(name="controller", target=controller.run, kwargs={"stop_event": stop_event})
    tm.start_all()

    # Wait for both threads to be up.
    deadline = time.monotonic() + 2.0
    while not worker.running.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert worker.running.is_set()

    shutdown_threads(tm, controller_thread, stop_event, wake_event, logger)

    # The controller's shutdown ran first, and the worker was still alive for it.
    assert order, "no shutdown steps were recorded"
    assert order[0] == ("controller_shutdown", True)
    assert ("worker_stop", True) in order
    # Both threads actually exited.
    assert not worker.running.is_set()


def test_controller_shutdown_timeout_is_generous() -> None:
    """The join timeout must exceed one tick plus the per-output forced-off waits."""
    assert CONTROLLER_SHUTDOWN_TIMEOUT >= 10.0
