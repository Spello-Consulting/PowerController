"""Rolling-average temperature readings for smart-device temperature probes.

This module smooths the point-in-time temperature readings provided by the
``sc-smart-device`` package. A brief temperature spike on a probe can otherwise
immediately flip an output (for example a roof probe spike turning a solar pump on).
When a rolling window is configured via ``General.TempProbeAveragingWindow``, every
consumer of probe temperature in the app reads the trailing average instead of the raw
instantaneous value.

The averager's only data source is the temp-probe readings taken from the
``sc-smart-device`` ``SmartDeviceView`` snapshot; it is unrelated to the weather or UPS
integrations and only mirrors their controller-wiring lifecycle (construct once, re-read
on config change via :meth:`initialise`, tick once per main loop via :meth:`record`).
"""

import datetime as dt
from collections import deque

from sc_foundation import DateHelper, SCConfigManager, SCLogger
from sc_smart_device import SmartDeviceView


class TempProbeAverager:
    """Maintains per-probe rolling buffers and returns averaged probe temperatures."""

    def __init__(self, config: SCConfigManager, logger: SCLogger) -> None:
        """Initialise the averager from configuration.

        Args:
            config (SCConfigManager): The configuration manager for the system.
            logger (SCLogger): The logger for the system.
        """
        self.config = config
        self.logger = logger
        self.enabled: bool = False
        self.window_seconds: int = 0

        # Per-probe rolling buffers of (recorded_at, temperature) samples, and the last
        # physical reading time seen for each probe (used to de-duplicate repeat ticks).
        self._samples: dict[int, deque[tuple[dt.datetime, float]]] = {}
        self._last_reading_time: dict[int, dt.datetime | None] = {}

        self.initialise()

    def initialise(self) -> None:
        """Read the config and (re)initialise the averaging window.

        Averaging is enabled when ``General.TempProbeAveragingWindow`` is greater than
        zero; a value of ``0``, ``null`` or an absent key disables it and the point-in-time
        values are used instead.
        """
        window = self.config.get("General", "TempProbeAveragingWindow", default=0) or 0
        try:
            self.window_seconds = int(window)
        except (ValueError, TypeError):
            self.window_seconds = 0
        self.enabled = self.window_seconds > 0

        if self.enabled:
            self.logger.log_message(f"Temp probe averaging enabled with a {self.window_seconds} second rolling window.", "debug")
        else:
            # Disabled: drop any buffered samples so a later re-enable starts clean.
            self._samples.clear()
            self._last_reading_time.clear()
            self.logger.log_message("Temp probe averaging disabled; using point-in-time readings.", "debug")

    def record(self, view: SmartDeviceView) -> None:
        """Record the current reading for each probe in the snapshot.

        Called once per main-loop tick, before any consumer reads a probe temperature. A
        new sample is appended only when the probe reports a new physical reading (its
        ``LastReadingTime`` has changed), so a reading that persists across several ticks is
        counted once. Samples older than the configured window are pruned.

        Args:
            view (SmartDeviceView): The current smart-device snapshot.
        """
        if not self.enabled:
            return

        now = DateHelper.now()
        for probe in view.get_json_snapshot().get("temp_probes", []):
            probe_id = probe.get("ID")
            if probe_id is None:
                continue
            raw_temp = probe.get("Temperature")
            reading_time = probe.get("LastReadingTime")

            buffer = self._samples.setdefault(probe_id, deque())

            # De-duplicate: skip when this is the same physical reading as last time. When
            # the probe exposes no reading time we cannot de-dup, so we sample every tick.
            if reading_time is not None and self._last_reading_time.get(probe_id) == reading_time:
                self._prune(buffer, now)
                continue
            self._last_reading_time[probe_id] = reading_time

            if raw_temp is not None:
                try:
                    buffer.append((now, float(raw_temp)))
                except (ValueError, TypeError):
                    pass

            self._prune(buffer, now)

    def get_temperature(self, view: SmartDeviceView, probe_id: int) -> float | None:
        """Return the probe temperature, averaged over the window when enabled.

        This is the single accessor every consumer uses in place of
        ``view.get_temp_probe_temperature()``.

        Args:
            view (SmartDeviceView): The current smart-device snapshot.
            probe_id (int): The integer temperature probe ID.

        Returns:
            float | None: The rolling-average temperature when averaging is enabled and
            in-window samples exist; otherwise the point-in-time reading (which may be
            ``None`` if the probe is unavailable).
        """
        if not self.enabled:
            disabled_value: float | None = view.get_temp_probe_temperature(probe_id)
            return disabled_value

        buffer = self._samples.get(probe_id)
        if buffer:
            now = DateHelper.now()
            self._prune(buffer, now)
            temps = [temp for _, temp in buffer]
            if temps:
                return sum(temps) / len(temps)

        # Enabled but no in-window samples (probe unavailable or just started): fall back.
        fallback_value: float | None = view.get_temp_probe_temperature(probe_id)
        return fallback_value

    def _prune(self, buffer: deque[tuple[dt.datetime, float]], now: dt.datetime) -> None:
        """Drop samples older than the configured window from the left of the buffer.

        Args:
            buffer (deque): The probe's rolling sample buffer.
            now (dt.datetime): The current time to measure the window against.
        """
        cutoff = DateHelper.add_datetime(now, seconds=-self.window_seconds)
        while buffer and buffer[0][0] < cutoff:
            buffer.popleft()
