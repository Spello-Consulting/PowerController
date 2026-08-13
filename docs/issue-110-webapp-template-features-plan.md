# Issue #110 — Copy in features from the template web app

Port three template-webapp features into the PowerController web UI, following the
reference implementation done for LightingControl
(`/Users/nick/dev/LightingControl/docs/webapp-template-features-implementation-notes.md`).

## Goal

Title bar right side becomes, in order:

1. Clock
2. Display-mode button (Day / Night / System, persisted in `localStorage`)
3. **System** button → `/system` page
4. **Config** button → `/config` page

Plus two new pages:

- **`/system`** — platform info + psutil host metrics + **Number of outputs**
  (excludes the template's *Simulation mode* and *Cities loaded* rows).
- **`/config`** — dumps the active YAML config file contents + its path.

## Current state (starting point)

- `templates/` and `static/` live at the **repo root**.
- All routes are inline in `src/webapp.py` (`_register_routes`).
- Static file is `static/styles.css` (note the `s`) + `static/favicon.ico`.
- `src/webapp.py` exposes public `WebAppNotifier` (dataclass) and
  `create_asgi_app(controller, config, logger) -> (FastAPI, WebAppNotifier)`.
- `tests/test_webapp.py` imports `WebAppNotifier` and `create_asgi_app` from `webapp`.
- `config.config_path` (a `Path`) is available on `SCConfigManager`.
- `app_label` = `config.get("General", "Label", default="PowerController")`;
  also present in the snapshot as `global_data.AppLabel`.
- `Website.PageAutoRefresh` already exists in the config schema (min 1, max 3600).
- The home page currently hardcodes a 60 s full-page reload.

## Decisions (PowerController-specific)

- **Keep the web layer at the repo root** (do *not* move under `src/`). The notes'
  `src/` split is optional ("if an app currently keeps them at root… `git mv`"); moving
  is pure churn here and would require touching `main.py`, the dataapi mount paths, and
  deploy configs. We adopt the *routes-vs-factory split* only in spirit: routes stay in
  `webapp.py` but the three new GET routes are added alongside the existing ones. This
  keeps the diff small and the public `create_asgi_app` signature untouched.
  - Rationale: the notes explicitly allow either layout; the value the issue asks for is
    the three features, not the directory move.
- **Keep the static filename `styles.css`** and the existing `favicon.ico` (no rename to
  `style.css`), to avoid breaking the favicon reference and minimise churn.
- **App label**: pass the snapshot's `global_data.AppLabel` into every template context so
  the header title is consistent across all three pages.
- **No simulation mode / no disable flag**: PowerController has neither, so omit the
  template's SIMULATION badge and the *Simulation mode* / *Cities loaded* rows. The only
  app-specific system counter is **Number of outputs** = `len(snapshot["outputs"])`.
- **Page auto-refresh**: drive the home-page reload from `Website.PageAutoRefresh`
  (default 60) instead of the hardcoded 60 s.

## Changes

### 1. Dependency
- `uv add psutil` (updates `pyproject.toml` + `uv.lock`).

### 2. `src/webapp.py`
- Add a `_NoCacheStaticFiles(StaticFiles)` subclass that sets
  `Cache-Control: no-cache`, and mount static through it (so updated
  `styles.css` isn't served stale).
- Add three new HTTP GET routes inside `_register_routes` (keeping the existing
  `/` and `/ws`):
  - **`GET /system`** — validate key; snapshot via `controller.get_webapp_data()`;
    build `system_info` and render `system.html`. Metrics:
    - `platform`: Operating system, Platform, Architecture, Hostname, Python version.
    - `psutil`: Uptime (`time.time() - psutil.boot_time()`, formatted `Xd Yh Zm`),
      Memory used (`virtual_memory().percent`), CPU load. **`psutil.cpu_percent(0.3)`
      runs via `asyncio.to_thread`** (it blocks).
    - **Number of outputs** = `len(snapshot.get("outputs", {}))`.
  - **`GET /config`** — validate key; read `config.config_path.read_text(encoding="utf-8")`
    inside `try/except OSError`; render `config.html` with `config_text` + `config_path`.
  - Update **`GET /`** to also pass `app_label`, `access_key` (from `?key=`), and
    `page_auto_refresh` (from `Website.PageAutoRefresh`, default 60) into the context, and
    render `home.html`.
- Add a `_format_uptime(seconds) -> str` helper (`Xd Yh Zm`).
- No change to the public API: `create_asgi_app` signature and the public
  `WebAppNotifier` name stay as-is (test compatibility preserved).

### 3. Templates (`templates/`)
- **`base.html`** (new) — shared shell:
  - `<head>` with pre-paint theme script (reads `localStorage["theme"]`, default
    `"system"`, sets `data-theme` on `<html>` before first paint).
  - `<header>` with `<h1>` app label (links home, preserving `?key=`) and
    `<nav class="header-right">`: `#clock`, `#theme-toggle` (cycles System → Light → Dark,
    icons 🖥️ / ☀️ / 🌙), `/system` link, `/config` link — each link carries
    `?key={{ access_key }}` when set.
  - `<footer>` with `#conn-status` + `#last-refresh`.
  - `window.__ACCESS_KEY__` / `window.__PAGE_AUTO_REFRESH__` globals, then
    `<script src="/static/app.js">`, then inline clock + theme-toggle scripts.
  - Blocks: `{% block title %}`, `{% block content %}`, `{% block scripts %}`.
- **`index.html` → `home.html`** — `git mv` and reparent to `{% extends "base.html" %}`;
  the existing temp-probes + output-cards body moves into `{% block content %}`. The big
  inline `<script>` (WebSocket client + `setMode`) moves to `static/app.js`. The old
  `<header>`/`<footer>`/clock/reload code is dropped (now provided by base).
- **`system.html`** (new) — extends base; iterate `system_info.items()` into `.info-row`s +
  a back-to-home `.btn` (preserving `?key=`).
- **`config.html`** (new) — extends base; `config_path` in `.config-path`, contents in
  `<pre class="config-dump">`; back-to-home `.btn`.

### 4. Static (`static/`)
- **`styles.css`** — introduce CSS custom properties for the palette in three places
  (light `:root`, `:root[data-theme="dark"]`, and
  `@media (prefers-color-scheme: dark)` for `:root[data-theme="system"]` /
  `:root:not([data-theme])`), then rewrite **every** hardcoded colour (header, cards,
  tables, buttons, badges, reason text, footer) to reference the variables. Add the
  header-nav rules (`.header-right`, `.header-link`, `#theme-toggle`) and the info-page
  rules (`.info-page/.info-card/.info-row/.info-label/.info-value/.config-path/
  .config-dump`, `.btn`, `#conn-status` online/offline). Preserve the existing
  full/compact responsive data-table behaviour.
- **`app.js`** (new) — the WebSocket client extracted from `index.html`:
  connect/reconnect (`1008` close ⇒ stop + "unauthorized"; else "offline" + retry),
  `applySnapshot` (temp probes + output cards, updating `#conn-status` + `#last-refresh`),
  the `setMode` prompt/command flow (unchanged behaviour, incl. revert-minutes prompt),
  and the `page_auto_refresh` reload from `window.__PAGE_AUTO_REFRESH__`. Reads the access
  key from `window.__ACCESS_KEY__`. No-ops harmlessly on `/system` and `/config`.

## Test / verification

- Existing `tests/test_webapp.py` must stay green (imports + `/` + `/ws` behaviour
  unchanged; `WebAppNotifier` and `create_asgi_app` unchanged).
- Add coverage: `GET /system` → 200 and contains "Number of outputs"; `GET /config` → 200
  and contains the config path; both enforce the access key (403 on wrong key).
- `uv run ruff check src/ && uv run ruff format src/` clean.
- `uv run pytest` green.
- Manual: all three pages 200; header shows Clock → toggle → System → Config; toggle
  cycles System → Light → Dark and persists; live WS updates + footer conn-status still
  work; `?key=` preserved across nav when an access key is configured.

## Out of scope

- Moving the web layer under `src/` (see Decisions).
- PowerControllerViewer / water-info (separate issues per the notes).
