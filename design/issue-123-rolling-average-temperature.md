# Issue #123 — Rolling average temperature for temp probes

**Status:** Draft plan for review
**Issue:** [Spello-Consulting/PowerController#123](https://github.com/Spello-Consulting/PowerController/issues/123)
**Classification:** Major change (feature branch + tests + docs + PR)
**Branch:** `issue-123-rolling-average-temperature`

---

## 1. Problem

The app reads **point-in-time** temperature values from the `SCSmartDevices` library
(`SmartDeviceView.get_temp_probe_temperature(probe_id)`). A brief temperature spike on a
probe can therefore flip an output immediately. This is seen in the **Sydney production**
config: the `Solar Pump` output turns on when the `Temp Roof` probe briefly spikes.

## 2. Goal

Add an optional, configurable **rolling-average window** for temp probes. When configured,
*every* consumer of probe temperature in the app uses the rolling average instead of the raw
point-in-time value:

- Output `TempProbeConstraints` (the threshold logic that caused the bug)
- Temp probe logging (`TempProbeLogging`)
- DataAPI / webapp probe readings

When disabled, behaviour is byte-for-byte the current behaviour.

## 3. Configuration

New key in the `General` section:

```yaml
General:
  TempProbeAveragingWindow: 300   # seconds; rolling window to average probe readings over.
                                  # 0 or null (or omitted) => feature disabled, use point-in-time values.
```

- Type: number, seconds. Schema: `required: False, nullable: True, min: 0, max: 86400`.
- `0`, `null`, or absent ⇒ **disabled** (point-in-time values, as today).
- `> 0` ⇒ **enabled**, average each probe over a trailing window of that many seconds.

## 4. Design

### 4.1 New component — `TempProbeAverager` (`src/temp_probe_averager.py`)

A small, controller-owned service object. Its **only** data source is the temp probe
readings from the `sc-smart-device` package (`SmartDeviceView.get_temp_probe_temperature`
and the raw `temp_probes` snapshot) — it has nothing to do with the weather integration.

For *structure only* it follows the same controller-wiring shape as the existing
`WeatherIntegration` / `UPSIntegration` services (constructed once in the controller,
re-read on config change via `initialise()`, ticked each main loop) — the reference is to
that lifecycle pattern, not to any weather/UPS data. It holds per-probe rolling buffers of
`(reading_time, temperature)` samples drawn from the smart-device snapshot.

Public surface:

| Method | Purpose |
|:--|:--|
| `__init__(config, logger)` | Create and call `initialise()`. |
| `initialise()` | Read `General.TempProbeAveragingWindow`; set `enabled` / `window_seconds`. |
| `record(view)` | Called once per tick. Appends the current reading for each probe in the snapshot and prunes samples older than the window. |
| `get_temperature(view, probe_id)` | The single accessor all call sites use. Returns the rolling average when enabled (else point-in-time). |
| `enabled` (property) | Whether averaging is active. |

**Sampling / de-duplication.** Each tick, for every probe in
`view.get_json_snapshot()["temp_probes"]`, record a sample. The sample timestamp is the
probe's `LastReadingTime` when available, else `DateHelper.now()`. To avoid counting the
same physical reading on every ~30 s tick, a new sample is appended only when the probe's
`LastReadingTime` differs from the last stored one (when `LastReadingTime` is `None`, we
can't de-dup, so we sample each tick). Samples with timestamp `< now - window` are pruned.

**Averaging.** `get_temperature(view, probe_id)`:
- If **disabled** ⇒ return `view.get_temp_probe_temperature(probe_id)` (unchanged).
- If **enabled** and there are in-window samples ⇒ return their arithmetic mean.
- If **enabled** but no in-window samples (probe unavailable / just started) ⇒ fall back to
  `view.get_temp_probe_temperature(probe_id)` (which may be `None`).

Because `record()` runs before any consumer each tick, an available probe always has at
least its current reading in the buffer, so there is no cold-start gap — on the first tick
the "average" simply equals the current value.

Only the **value** is averaged. `LastReadingTime` shown in logging/API stays point-in-time
(it is metadata, not a measurement).

### 4.2 Wiring

- **Controller** (`controller.py`)
  - Construct `self.temp_probe_averager = TempProbeAverager(config, logger)` in `__init__`
    (next to `weather_integration`).
  - Call `self.temp_probe_averager.initialise()` in the reinitialise path (next to
    `self.weather_integration.initialise()`).
  - Call `self.temp_probe_averager.record(view)` once per tick in `_run_scheduler_tick`,
    right after the `view` is obtained and before any consumer runs.
  - Route `_log_temp_probes` and `_refresh_api_data_if_needed` probe reads through
    `self.temp_probe_averager.get_temperature(view, probe_id)`.
  - Pass the averager into each `OutputManager` (same way `weather_integration` is passed).

- **OutputManager** (`outputs.py`) — accept `temp_probe_averager` and forward it to
  `OutputConstraint`.

- **OutputConstraint** (`output_constraint.py`) — store the averager; in
  `are_there_temp_probe_constraints`, replace `view.get_temp_probe_temperature(probe_id)`
  with `self.temp_probe_averager.get_temperature(view, probe_id)`.

### 4.3 Schema

Add `TempProbeAveragingWindow` to the `General` schema block in `config_schemas.py`.

## 5. Files touched

| File | Change |
|:--|:--|
| `src/temp_probe_averager.py` | **New** `TempProbeAverager` class. |
| `src/config_schemas.py` | Add `TempProbeAveragingWindow` to `General`. |
| `src/controller.py` | Construct / initialise / `record()` the averager; route logging & API reads; pass to outputs. |
| `src/outputs.py` | Accept and forward `temp_probe_averager` to `OutputConstraint`. |
| `src/output_constraint.py` | Use averager for the constraint temperature read. |
| `config_example.yaml` | Document the new `General` key. |
| `docs/configuration/sections/general.md` | Document the new key. |
| `tests/` | New `test_temp_probe_averager.py`; constructor-signature updates where needed. |

## 6. Testing

New `tests/test_temp_probe_averager.py`:
- Disabled (0 / null) ⇒ `get_temperature` returns exactly the point-in-time value.
- Enabled ⇒ averages multiple in-window samples (e.g. readings 20, 30, 40 ⇒ 30).
- A brief spike is damped: steady 25s with one 60 spike yields an average well under the
  constraint threshold (the core bug scenario).
- Samples outside the window are pruned (old high readings drop out over time).
- De-dup: repeated identical `LastReadingTime` does not add duplicate samples.
- Probe currently unavailable (`None`) with no samples ⇒ `None`.
- Re-`initialise()` after a config change toggles enabled/disabled correctly.

Existing suites (`test_output_manager`, `test_smart_device_view`, etc.) continue to pass;
update any constructor calls affected by the new parameter. Full `pytest`, `mypy --strict`,
and `ruff check` must be clean.

## 7. Out of scope / decisions

- **Time-weighting.** A simple arithmetic mean of in-window samples is used, not a
  time-weighted integral. With a regular polling interval the difference is negligible and
  the simpler form is easier to reason about and test. (Can revisit if needed.)
- **Per-probe window overrides.** Not requested; a single global window applies to all
  probes.
- **Persistence across restarts.** The rolling buffer is in-memory only. After a restart the
  window re-fills within `TempProbeAveragingWindow` seconds; no state-file changes.
