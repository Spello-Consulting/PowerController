# Issue #117 — Current Price differs for different outputs on the webapp

**Status:** Draft plan for review
**Issue:** [Spello-Consulting/PowerController#117](https://github.com/Spello-Consulting/PowerController/issues/117)
**Classification:** Major change (feature branch + tests + docs + PR)
**Chosen direction:** Keep each output's Current Price on its **own pricing basis**, but make
same-basis outputs **consistent**, fix the non-positive-price bugs, and **label the basis**
in the webapp so it's no longer ambiguous.

---

## 1. Problem

On the webapp, outputs show different **Current Price** values. Two independent causes were
confirmed (one from the issue text, one from the Sydney-prod screenshot).

### Cause 1 — Current Price basis is mode-dependent and unlabelled
Two outputs that *look* identical in the UI price off different sources:

| Output | Mode | Basis actually used | Current Price shown |
|---|---|---|---|
| Pool Pump | `BestPrice`, channel `general` | live Amber general price | `13.9 ¢/kWh` |
| Solar Pump | `Schedule`, schedule `Pool Solar` | schedule slot price (`configs/prod_sydney.yaml:641`, 10:00–16:00 = 15) | `15.0 ¢/kWh` |

Both are "smart device" outputs, so the UI gives no hint that one is Amber-priced and the
other schedule-priced. Each value is individually correct for its config; the field name
"Current Price" just implies a single grid price. (Solar Pump sets `AmberChannel: general`
but in `Schedule` mode that channel is **not** used for pricing.)

### Cause 2 — Three duplicated price paths disagree on non-positive prices
"Current Price" is produced by three independent per-output-type methods with no shared
helper. For a normal positive price they agree; they diverge when the Amber price is ≤ 0
(daytime solar export — "outputs off"):

| Output type | Function | Source | Handling of ≤ 0 price | Shown when ≤ 0 |
|---|---|---|---|---|
| Smart device | [`OutputManager._get_current_price()`](../src/outputs.py#L1311) | `pricing.get_current_price(channel)` (first slot of `today_forecast_data`) | `0` → scheduler fallback; negative → returned as-is | **"N/A"** (negative fails `> 0` guard at [`outputs.py:436`](../src/outputs.py#L436)) |
| Meter | [`MeterOutput._get_price()`](../src/meter_output.py#L244) | `pricing.get_price(now, channel)` (raw slot at now) | any `≤ 0` **rejected** → schedule → else `default_price` | **`default_price`** (32.0) / schedule |
| TeslaMate | [`TeslaMateOutput.get_webapp_data()`](../src/teslamate_output.py#L395) | — (ignores its own `_get_price`) | n/a | **hard-coded "N/A"** |

Underlying data is identical (`today_forecast_data` is derived from `raw_price_data`,
[`pricing.py:461`](../src/pricing.py#L461)) — the divergence is purely in the per-type
fallback/threshold logic.

### Secondary smells
- Treating a genuine 0/negative Amber price as "invalid" and substituting 32c is wrong —
  negative wholesale prices are real.
- The display convention `value if value > 0 else "N/A"` conflates "no data", "free", and
  "negative", applied inconsistently across types.

## 2. Goal / acceptance criteria

1. Two outputs with the **same pricing basis** (same mode + same channel, or same schedule)
   show the **same** Current Price on a given refresh.
2. Non-positive Amber prices (zero / negative during solar export) are displayed honestly
   and consistently, not replaced by the 32c default or shown as "N/A".
3. A single shared definition of "current display price" is used by all three output types.
4. The webapp shows **which basis** each Current Price comes from (e.g. "Amber general",
   "Pool Solar schedule", "Default price"), so mode-driven differences are self-explaining.
5. Cost-accrual behaviour unchanged (display-only change) — see decision B.

## 3. Proposed change

### 3.1 One canonical current-price resolver
Replace the three copies with a single shared computation. Two parts:

**(a) `PricingManager` gains a display helper** that distinguishes "no data" from a real
non-positive price:
```python
def get_current_display_price(self, channel_id: AmberChannel = AmberChannel.GENERAL) -> float | None:
    """Current Amber price for UI display (may be 0 or negative); None if no data for channel."""
```
Today both `get_current_price` and `get_price(now)` collapse "missing" and "zero" to `0.0`;
this ambiguity is the core of Cause 2.

**(b) A shared output-level resolver** (lightweight free function — see decision C) that
all three `get_webapp_data()` paths call, returning both the price and its basis label:
```
# inputs: pricing, scheduler, device_mode, amber_channel, schedule, schedule_name, default_price
if device_mode == BEST_PRICE:
    price = pricing.get_current_display_price(amber_channel)   # may be 0, negative, or None
    if price is not None:
        return price, f"Amber {amber_channel}"
if schedule is not None:
    return scheduler.get_current_price(schedule), f"{schedule_name} schedule"
return default_price, "Default price"
```
Rules applied identically everywhere:
- A real Amber price of 0/negative is **kept** (not treated as invalid).
- Fallback to schedule/default happens **only** when the Amber data is absent (`None`).
- A Schedule-mode output still reports its schedule price (Cause 1 preserved by design) —
  but now **labelled**, and same-schedule outputs agree.

### 3.2 Consistent display formatting
One formatter so every output renders the same way; 0/negative shown as a number
(`"0.0 ¢/kWh"`, `"-3.0 ¢/kWh"`), `"N/A"` reserved for genuinely-missing data. Applied at:
- [`outputs.py:436`](../src/outputs.py#L436), [`meter_output.py:326`](../src/meter_output.py#L326),
  [`teslamate_output.py:395`](../src/teslamate_output.py#L395) (TeslaMate wired to the resolver so it matches).

### 3.3 Webapp basis label
- Add `current_price_basis` to each output's webapp data dict.
- Render it in [`templates/home.html`](../templates/home.html) next to Current Price (both
  the ~L80 and ~L148 blocks) — a small muted label or a `title=`/tooltip on the value.
- Update the AJAX refresh JS that targets `[data-field="current_price"]` to also update the
  basis label/tooltip.

### 3.4 Files touched
- `src/pricing.py` — `get_current_display_price` (+ None-vs-zero disambiguation).
- new shared resolver (location per decision C) — single source of truth + basis label.
- `src/outputs.py`, `src/meter_output.py`, `src/teslamate_output.py` — webapp price via the
  resolver + formatter; expose `current_price_basis`.
- `templates/home.html` (+ its refresh JS) — render the basis label/tooltip.

## 4. Decisions

**Resolved:** "Current Price" stays on each output's **own basis**, made consistent and
**labelled** (user decision, this session).

Still to confirm (my recommendations in **bold**):
- **A. Non-positive price policy.** **Show the real current price including zero/negative;
  fall back to schedule/default only when Amber data is truly missing.** (Required for
  goal #1/#2.)
  
  > Agreed 
- **B. Scope.** **Display path only this PR.** `MeterOutput._get_price` /
  `TeslaMateOutput._get_price` also feed run-history **cost accrual**
  ([`meter_output.py:202`](../src/meter_output.py#L202)); leave that unchanged and note a
  follow-up, unless you want them aligned now.
  
  > Align now, do both
- **C. Helper placement.** **Light shared free function** (`pricing.py` or `helpers.py`)
  rather than a new base class — the three output classes currently share no base.
  
  > Agree
- **D. Label wording.** Proposed: `"Amber general"`, `"<Schedule name> schedule"`,
  `"Default price"`. Confirm text / whether it's an inline label vs tooltip.
  
  > Agree

> Note: I have changed the Sydney production config so that the "Solar Pump" output now uses AMber pricing (`Mode: BestPrice`)

## 5. Tests (pytest)
- `pricing.get_current_display_price`: real value for positive, **zero**, **negative**;
  `None` for no-data / invalid channel.
- Resolver parity: a `BestPrice/general` smart-device call and a `BestPrice/general` meter
  call return the **same** price + basis for the same instant (the heart of Cause 2).
- Resolver basis: `Schedule` mode returns the schedule price + `"… schedule"` label;
  two outputs on the same schedule agree (Cause 1 made consistent).
- Fallback only on `None`; schedule vs `default_price` chosen correctly.
- Webapp data: `current_price` + `current_price_basis` correct across a smart device, a
  meter, and a TeslaMate output; low/zero/negative prices render as numbers, not "N/A".

## 6. Docs
- Update the webapp field docs (`docs/configuration/sections/viewer_website.md` /
  `website.md` as applicable): Current Price is the live price on the output's **own basis**
  (Amber channel, schedule, or default), may be zero/negative, and the basis is labelled.

## 7. Workflow (per global conventions)
1. Feature branch `issue-117-current-price-consistency`.
2. Implement §3, add tests (§5), update docs (§6).
3. `ruff check`, `mypy --strict`, `pytest` all clean.
4. Push, open PR against `main`, notify for review. Do **not** merge.

## 8. Out of scope
- Run-plan / Forecast Price logic (expected to differ per output).
- Amber refresh/forecast pipeline beyond the None-vs-zero disambiguation.
- Cost-accrual alignment (unless decision B says otherwise).
