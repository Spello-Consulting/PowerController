"""Tests for the TempProbeAverager — rolling-average temperature readings (Issue 123).

Strategy:
  - Build a real SmartDeviceView from a minimal SmartDeviceStatus so the averager
    exercises the same snapshot/accessor surface it uses in production.
  - Monkeypatch DateHelper.now() with a controllable clock so window pruning and
    de-duplication are deterministic.
"""

import datetime as dt
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import temp_probe_averager as tpa_module
from sc_smart_device import SmartDeviceStatus, SmartDeviceView
from temp_probe_averager import TempProbeAverager

PROBE_ID = 1

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger():
    m = MagicMock()
    m.log_message = MagicMock()
    return m


def _make_config(window):
    """Build a mock SCConfigManager returning the supplied averaging window."""
    cfg = MagicMock()

    def _get(*keys, default=None):
        if keys == ("General", "TempProbeAveragingWindow"):
            return window
        return default

    cfg.get.side_effect = _get
    return cfg


def _make_view(temperature, reading_time=None, probe_id=PROBE_ID):
    """Build a real SmartDeviceView with a single temp probe reading."""
    status = SmartDeviceStatus(
        devices=[{"ID": 1, "Name": "Dev", "Online": True, "ExpectOffline": False, "Temperature": None}],
        outputs=[],
        inputs=[],
        meters=[],
        temp_probes=[{
            "ID": probe_id,
            "Name": "Temp Roof",
            "DeviceID": 1,
            "Temperature": temperature,
            "LastReadingTime": reading_time,
        }],
    )
    return SmartDeviceView(snapshot=status)


class _Clock:
    """A controllable monotonic-ish clock for monkeypatching DateHelper.now()."""

    def __init__(self, start: dt.datetime):
        self.current = start

    def now(self, tzinfo=None):  # noqa: ARG002 - matches DateHelper.now signature
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += dt.timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    """Install a controllable clock in place of DateHelper.now() for the averager module."""
    c = _Clock(dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc))
    monkeypatch.setattr(tpa_module.DateHelper, "now", c.now)
    return c


def _reading_time(clock, offset_seconds=0):
    return clock.current + dt.timedelta(seconds=offset_seconds)


# ---------------------------------------------------------------------------
# Disabled behaviour (window 0 / null)
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_zero_window_disabled(self):
        avg = TempProbeAverager(_make_config(0), _make_logger())
        assert avg.enabled is False

    def test_null_window_disabled(self):
        avg = TempProbeAverager(_make_config(None), _make_logger())
        assert avg.enabled is False

    def test_disabled_returns_point_in_time(self, clock):
        avg = TempProbeAverager(_make_config(0), _make_logger())
        view = _make_view(25.0, _reading_time(clock))
        # record() is a no-op when disabled; get_temperature returns the raw value.
        avg.record(view)
        assert avg.get_temperature(view, PROBE_ID) == 25.0

    def test_disabled_record_keeps_no_samples(self, clock):
        avg = TempProbeAverager(_make_config(0), _make_logger())
        avg.record(_make_view(25.0, _reading_time(clock)))
        assert avg._samples == {}


# ---------------------------------------------------------------------------
# Averaging behaviour (window > 0)
# ---------------------------------------------------------------------------


class TestAveraging:
    def test_single_reading_equals_value(self, clock):
        avg = TempProbeAverager(_make_config(300), _make_logger())
        view = _make_view(25.0, _reading_time(clock))
        avg.record(view)
        assert avg.get_temperature(view, PROBE_ID) == 25.0

    def test_mean_of_in_window_samples(self, clock):
        avg = TempProbeAverager(_make_config(300), _make_logger())
        for i, temp in enumerate((20.0, 30.0, 40.0)):
            clock.advance(10)
            view = _make_view(temp, _reading_time(clock))
            avg.record(view)
        assert avg.get_temperature(view, PROBE_ID) == pytest.approx(30.0)

    def test_brief_spike_is_damped(self, clock):
        """The core bug: a brief spike must not drag the average over the threshold."""
        avg = TempProbeAverager(_make_config(300), _make_logger())
        view = None
        for temp in (25.0, 25.0, 25.0, 60.0, 25.0):
            clock.advance(10)
            view = _make_view(temp, _reading_time(clock))
            avg.record(view)
        result = avg.get_temperature(view, PROBE_ID)
        # Mean is 32.0, comfortably below a 40°C constraint that the raw 60 spike would trip.
        assert result == pytest.approx(32.0)
        assert result < 40.0

    def test_old_samples_pruned(self, clock):
        avg = TempProbeAverager(_make_config(60), _make_logger())
        # A high reading that will fall outside the 60s window.
        view_old = _make_view(100.0, _reading_time(clock))
        avg.record(view_old)
        # Advance past the window and feed steady 20°C readings.
        view = None
        for _ in range(3):
            clock.advance(30)
            view = _make_view(20.0, _reading_time(clock))
            avg.record(view)
        # The 100°C sample is now older than 60s and must be pruned.
        assert avg.get_temperature(view, PROBE_ID) == pytest.approx(20.0)

    def test_duplicate_reading_time_not_resampled(self, clock):
        avg = TempProbeAverager(_make_config(300), _make_logger())
        rt = _reading_time(clock)
        view = _make_view(25.0, rt)
        # Same physical reading seen on several ticks.
        for _ in range(5):
            clock.advance(10)
            avg.record(_make_view(25.0, rt))
        assert len(avg._samples[PROBE_ID]) == 1
        assert avg.get_temperature(view, PROBE_ID) == 25.0

    def test_new_reading_time_resamples(self, clock):
        avg = TempProbeAverager(_make_config(300), _make_logger())
        for temp in (20.0, 40.0):
            clock.advance(10)
            avg.record(_make_view(temp, _reading_time(clock)))
        assert len(avg._samples[PROBE_ID]) == 2

    def test_no_reading_time_samples_every_tick(self, clock):
        """Probes with no LastReadingTime cannot be de-duped, so each tick is a sample."""
        avg = TempProbeAverager(_make_config(300), _make_logger())
        for temp in (10.0, 20.0, 30.0):
            clock.advance(10)
            avg.record(_make_view(temp, reading_time=None))
        assert len(avg._samples[PROBE_ID]) == 3

    def test_unavailable_probe_returns_none(self, clock):
        avg = TempProbeAverager(_make_config(300), _make_logger())
        view = _make_view(None, _reading_time(clock))
        avg.record(view)
        assert avg.get_temperature(view, PROBE_ID) is None

    def test_enabled_no_samples_falls_back_to_point_in_time(self, clock):
        avg = TempProbeAverager(_make_config(300), _make_logger())
        view = _make_view(25.0, _reading_time(clock))
        # No record() called yet -> no samples -> fall back to the raw reading.
        assert avg.get_temperature(view, PROBE_ID) == 25.0


# ---------------------------------------------------------------------------
# Reinitialisation on config change
# ---------------------------------------------------------------------------


class TestReinitialise:
    def test_enable_via_reinitialise(self):
        config = _make_config(0)
        avg = TempProbeAverager(config, _make_logger())
        assert avg.enabled is False
        config.get.side_effect = lambda *keys, default=None: 300 if keys == ("General", "TempProbeAveragingWindow") else default
        avg.initialise()
        assert avg.enabled is True
        assert avg.window_seconds == 300

    def test_disable_clears_samples(self, clock):
        config = _make_config(300)
        avg = TempProbeAverager(config, _make_logger())
        avg.record(_make_view(25.0, _reading_time(clock)))
        assert avg._samples
        config.get.side_effect = lambda *keys, default=None: 0 if keys == ("General", "TempProbeAveragingWindow") else default
        avg.initialise()
        assert avg.enabled is False
        assert avg._samples == {}
