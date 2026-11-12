# Issue #119 — Mobile view summary page

**Status:** Draft plan for review
**Issue:** [Spello-Consulting/PowerController#119](https://github.com/Spello-Consulting/PowerController/issues/119)
**Classification:** Major change (feature branch + tests + docs + PR)

---

## 1. Goal

Add a compact, phone-optimised **summary page** reachable from the web app home page. It shows,
at a glance and ideally without scrolling on an iPhone:

- **Temperature** for every probe in `TempProbeLogging` (no "last reading" time; show `N/A`
  when the reading is stale — older than `TempProbeLogging.LastReadingWithinMinutes`).
- **On/Off state** of every visible output (those **not** `HideFromWebApp`), as a coloured
  status dot — **green** when on, **black** when off — matching the Viewer's
  `.status-indicator--active` / `.status-indicator--inactive`.
- The **current electricity price** next to each output, on that output's own pricing basis
  (the value already made consistent in issue #117).

Font sizes scale up to fill the screen while keeping everything on one page.

## 2. What the data already gives us (no model changes needed)

`PowerController.get_webapp_data()` ([`src/controller.py:164`](../src/controller.py#L164))
already returns everything required:

- `global.TempProbeData` — list of `{name, temperature, last_logged_time}`. **Staleness is
  already applied upstream**: when a reading is older than `LastReadingWithinMinutes`,
  `probe["Temperature"]` is set to `None` ([`src/controller.py:1437`](../src/controller.py#L1437)).
  So the mobile page simply renders `temperature is None` → `N/A`. No new backend logic.
- `outputs` — already **excludes** `HideFromWebApp` outputs (their `get_webapp_data()` returns
  `{}`, which the controller skips). Each entry already carries `name`, `is_on`,
  `current_price` (e.g. `"13.9 c/kWh"`) and `current_price_basis`.

This keeps the change almost entirely in the view/template/JS layer.

## 3. Proposed change

### 3.1 New route — `src/webapp.py`
Add `GET /mobile` alongside the existing `/`, `/system`, `/config` routes. Same access-key
check (`_validate_access_key`) and the same snapshot/503 handling as `/`. It renders
`mobile.html` with: `app_label`, `global_data` (for `TempProbeData`), `outputs`,
`page_auto_refresh`, `access_key`.

### 3.2 Navigation button — `templates/home.html`
Add a button/link on the home page to open `/mobile` (carrying `?key=` when set). Placed in
the home page content near the top so it reads as "go to the phone view". (The header nav in
`base.html` keeps System/Config as-is.)

### 3.3 New template — `templates/mobile.html`
A **standalone** page (not extending `base.html`) so it can use the whole viewport with no
nav/footer chrome stealing vertical space. It includes:
- the same pre-paint theme script as `base.html` (respects the saved light/dark/system theme),
- a slim header: app label + a small **Full view** link back to `/` (and the theme toggle),
- a **Probes** block: each probe as `Name … value °C` (or `N/A`),
- an **Outputs** block: each output as `● Name … <current price>`, the dot green/black by
  `is_on`,
- `styles.css` plus a small `mobile.js`.

### 3.4 Styling — `static/styles.css`
Add a `.mobile-*` section reusing existing theme tokens, plus the two indicator colours from
the Viewer (`--indicator-on: #00c853`, `--indicator-off: #000` light / `#55606f` dark) and the
`.status-indicator` / `--active` / `--inactive` classes (same names as the Viewer for
consistency). Layout: `100dvh` flex column, the two blocks sharing the height; **fluid type**
via `clamp()`/`vmin` so values grow to fill the screen on a phone.

> Note on `clamp()`: the Viewer's `temp_probes` page avoids modern CSS for an old iPad (Gen 2).
> This new page is iPhone-targeted, so `clamp()` is proposed — see open question **Q3**.

### 3.5 Live updates — `static/mobile.js`
Small dedicated script (the existing `app.js` is bound to the home-page card layout). It opens
the same `/ws` socket, and on each `state_update` updates each output's dot + price and each
probe's value, plus the footer/refresh affordance. Honours `PageAutoRefresh` like `app.js`.

### 3.6 Files touched
- `src/webapp.py` — new `/mobile` route.
- `templates/mobile.html` — new standalone template.
- `templates/home.html` — "Mobile view" button.
- `static/styles.css` — mobile layout + status-indicator classes.
- `static/mobile.js` — new live-update script for the mobile page.
- `tests/test_webapp.py` — new tests (see §5).
- `docs/configuration/sections/website.md` — document the page.

## 4. Open questions (my recommendation in **bold**)

- **Q1. Route name.** **`/mobile`** (vs `/summary`).

  > /mobile is fine
- **Q2. Show the price basis label too?** The issue asks only for the price. **Show the price
  only** to stay uncluttered; I can add a tiny basis label under it if you want.
  
  > Don't show price basis
- **Q3. `clamp()`/modern CSS OK here?** Target is iPhone, so **yes**. Say if this page must
  also support the old iPad and I'll use static sizing + a small JS fit-to-screen instead.
  
  > Yes, modern iPhone is the target
- **Q4. Button wording/placement.** **"Mobile view" link at the top of the home page.** Happy
  to put it in the header nav instead so it shows on every page.
  
  > Agreed

## 5. Tests (pytest, in `tests/test_webapp.py`)
- `GET /mobile` returns 200 (open) / 403 (wrong key) / 200 (correct key); 503 when no data.
- Rendered HTML contains each visible output's name and `current_price`, and a
  `status-indicator--active` for an on output / `--inactive` for an off output.
- A `HideFromWebApp` output does **not** appear (already excluded from the snapshot).
- Temp probe with `temperature=None` renders `N/A`; a probe with a value renders the value.

## 6. Docs
- `docs/configuration/sections/website.md`: add a short "Mobile summary page" subsection
  (what `/mobile` shows, that stale probe readings show `N/A`, green/black output dots, and the
  per-output current price).

## 7. Workflow (per global conventions)
1. Feature branch `issue-119-mobile-summary-page`.
2. Implement §3, add tests (§5), update docs (§6).
3. `ruff check`, `mypy --strict`, `pytest` all clean.
4. Push, open PR against `main`, notify for review. Do **not** merge.

## 8. Out of scope
- Any change to how prices or staleness are computed (issue #117 already settled pricing;
  staleness is already applied in the controller).
- Control actions on the mobile page (it is view-only).
