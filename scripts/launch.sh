#!/bin/bash
: '=======================================================
Application Launcher

Requires Python and UV to be installed

3/4/2026: Change default directory to the script location, and allow --homedir to override it.
3/6/2026: Better logic for finding HomeDir.
1/8/2026: Add support for 1Password op / build .env file
1/8/2026: Re-exec through `op run` to inject secrets into the environment
          in-memory (no plaintext .env written to disk).
20/8/2026: Add APP_ENV guard to ensure .env.target is valid and APP_ENV is set. Does uv sync --extra all in dev environment.
=========================================================='

# set -euo pipefail

PYPROJECT="pyproject.toml"

# Parse --homedir argument from any position; default to the directory containing pyproject.toml
ScriptDir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Absolute path to this script, so we can safely re-exec ourselves regardless of cwd.
SelfPath="$ScriptDir/$(basename "${BASH_SOURCE[0]}")"
HomeDir=""
for ((i=1; i<=$#; i++)); do
  if [ "${!i}" = "--homedir" ]; then
    j=$((i+1))
    if [ $j -le $# ]; then
      HomeDir="${!j}"
      break
    fi
  fi
done

# If --homedir was not provided, locate the directory containing pyproject.toml
if [ -z "$HomeDir" ]; then
  if [ -f "$ScriptDir/$PYPROJECT" ]; then
    HomeDir="$ScriptDir"
  elif [ -f "$ScriptDir/../$PYPROJECT" ]; then
    HomeDir="$(cd "$ScriptDir/.." && pwd)"
  else
    echo "[launcher] Error: Cannot find $PYPROJECT in $ScriptDir or its parent." >&2
    exit 1
  fi
fi

# make sure HomeDir is an absolute path
HomeDir="$(cd "$HomeDir" && pwd)"

# Change to the home directory so uv commands work correctly
cd "$HomeDir" || {
  echo "[launcher] Error: Cannot change to directory $HomeDir" >&2
  exit 1
}

# Stream Python output in real time. Under `op run` (below) the app's stdout is a
# pipe, not a TTY, so Python defaults to block buffering and logs only appear in
# large bursts. Forcing unbuffered output restores the immediate, line-by-line
# console output you get when running `uv run` directly in a terminal.
export PYTHONUNBUFFERED=1

# -------------------------------------------------------------------------
# Secrets: materialise them into the environment via 1Password — never to disk.
#
# If a committed .env.template (containing op:// references) exists, re-exec
# this whole script through `op run`, which resolves those references and
# injects the values into the environment. A sentinel var guards against an
# infinite re-exec loop. Doing this by re-exec (rather than a narrow
# `op run -- uv run app`) makes the injected secrets available to the entire
# launcher — uv sync, logging, etc. — not just the final app process, and no
# plaintext .env is ever written to the filesystem.
# -------------------------------------------------------------------------
# .env.target is a per-deployment pointer (copy or symlink) to one of the tracked
# templates, .env.dev.template or .env.prod.template. It is gitignored so each
# checkout (dev vs prod) selects its own environment without shipping the choice
# in git. The selected template carries APP_ENV, which is verified after injection.
EnvTemplate="$HomeDir/.env.target"

if [ -z "${_LAUNCH_OP_INJECTED:-}" ] && [ -f "$EnvTemplate" ]; then
  # Load the 1Password service-account token explicitly if present — don't rely
  # on shell rc files, since this may run outside an interactive/login shell
  # (cron, @reboot, systemd). On headless Linux/Pi this token authenticates op
  # non-interactively; on an interactive Mac the file legitimately won't exist,
  # and op authenticates via the desktop app integration instead.
  if [ -f "$HOME/.config/op/service-account-token" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$HOME/.config/op/service-account-token"
    set +a
    # With a service-account token, op authenticates directly to 1Password and
    # never needs the desktop app. Disable biometric/app integration so op does
    # NOT probe the 1Password desktop app's container — that access is what
    # triggers the macOS "op would like to access data from other apps" TCC
    # prompt on every run (the grant can't persist under a LaunchAgent). Scoped
    # to this invocation, so interactive dev use of op elsewhere is unaffected.
    export OP_BIOMETRIC_UNLOCK_ENABLED=false    
  fi

  if ! command -v op >/dev/null 2>&1; then
    echo "[launcher] Error: $EnvTemplate found but 1Password CLI (op) is not on PATH." >&2
    exit 1
  fi

  # Hygiene: projects using this 1Password flow don't need an on-disk .env, so
  # remove any stale one (e.g. left over from the old `op inject -o .env` path).
  # Done before the exec, since exec replaces this process and nothing after it
  # would run; at this point op is confirmed present and the template exists.
  if [ -f "$HomeDir/.env" ]; then
    echo "[launcher] Removing on-disk .env — secrets now come from 1Password."
    rm -f "$HomeDir/.env"
  fi

  # Wait for DNS before op runs. Scheduled jobs (launchd/systemd/cron) often
  # fire the instant the machine wakes, before the OS resolver is back, so op's
  # first call fails with "no such host". Probe the OS resolver directly — the
  # same path op resolves through — via fixed-path system tools, so this needs
  # neither python3 nor uv on PATH. macOS: dscacheutil exits 0 even on a miss,
  # so match an address in its output. Linux: getent exits non-zero on a miss.
  op_host="my.1password.com"
  _dns_ready() {
    if [ "$(uname -s)" = "Darwin" ]; then
      /usr/bin/dscacheutil -q host -a name "$1" 2>/dev/null | grep -q ip_address
    else
      getent ahosts "$1" >/dev/null 2>&1
    fi
  }
  for attempt in $(seq 1 6); do
    _dns_ready "$op_host" && break
    echo "[workflow] Network/DNS not ready (attempt $attempt), waiting 5s ..." >&2
    sleep 5
  done

  echo "[launcher] Injecting secrets from $EnvTemplate via 'op run' and re-exec'ing ..."
  export _LAUNCH_OP_INJECTED=1
  exec op run --env-file="$EnvTemplate" -- "$SelfPath" "$@"
fi

# Second pass (secrets already injected above), or no template at all: also load
# a plain .env if present, for local/non-op overrides. This never contains the
# templated secrets — those already live in the environment.
EnvFile="$HomeDir/.env"
if [ -f "$EnvFile" ]; then
  echo "[launcher] Loading environment from $EnvFile ..."
  set -a
  # shellcheck disable=SC1090
  . "$EnvFile"
  set +a
fi

# Environment guard: APP_ENV is injected from the selected .env.target template
# (development or production). Refuse to run if it is missing — that means no
# template was resolved (.env.target absent or dangling); we must never run
# without knowing which environment we're in.
if [ -f "$EnvTemplate" ] || [ -f "$EnvFile" ]; then
  if [ -z "${APP_ENV:-}" ]; then
    echo "[launcher] Error: APP_ENV is not set — refusing to run. Ensure .env.target points to .env.dev.template or .env.prod.template." >&2
    exit 1
  fi
fi
if [ "$APP_ENV" = "development" ]; then
  echo "[launcher] WARNING: APP_ENV=development — running with development settings." >&2
fi

# Get the script name from pyproject.toml
if [ -f "$PYPROJECT" ]; then
  ScriptName=$(grep -E '^launch_path *= *"' "$PYPROJECT" | head -1 | sed -E 's/^launch_path *= *"([^"]+)".*$/\1/')
else
  echo "Error: $PYPROJECT not found."
  exit 1
fi

if [ -z "$ScriptName" ]; then
  echo "Error: launch_path not defined in $PYPROJECT."
  exit 1
fi

# Find uv reliably (systemd often has a minimal PATH)
if command -v uv >/dev/null 2>&1; then
  UVCmd="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
  UVCmd="$HOME/.local/bin/uv"
else
  echo "[launcher] Error: 'uv' not found in PATH or at \$HOME/.local/bin/uv" >&2
  exit 1
fi

# On Raspberry Pi, enforce Python 3.13+ if requested
if [[ $(uname -m) == "armv7l" || $(uname -m) == "aarch64" ]]; then
  if ! "$UVCmd" python pin --resolved 2>/dev/null | grep -Eq '^(3\.1[3-9]|3\.[2-9][0-9]|[4-9])'; then
    echo "[launcher] Error: project must pin Python 3.13+ on Raspberry Pi. Run: uv python pin 3.13" >&2
    exit 1
  fi
fi

# If APP_CONFIG is set and --config hasn't been passed to this script, add it to the uv run command line. This allows systemd service files to set APP_CONFIG without needing to hardcode --config in the service file.
if [ -n "${APP_CONFIG:-}" ]; then
  config_passed=false
  for arg in "$@"; do
    if [[ "$arg" == "--config" ]]; then
      config_passed=true
      break
    fi
  done
  if [ "$config_passed" = false ]; then
    set -- "$@" "--config" "$APP_CONFIG"
  fi
fi

# Make sure deps are synced before starting
if [ "$APP_ENV" = "development" ]; then
  if ! "$UVCmd" sync --extra all; then
    echo "[launcher] uv sync failed — not starting app." >&2
    exit 2
  fi
else
  if ! "$UVCmd" sync; then
    echo "[launcher] uv sync failed — not starting app." >&2
    exit 2
  fi
fi

# Hand off to the app with `exec`, replacing this launcher shell with uv (and,
# under it, the Python process). That way SIGINT/SIGTERM are delivered straight
# to the app rather than to a shell that can't forward them — so `systemctl stop`
# and Ctrl-C both reach main.py's signal handler for a graceful shutdown.
#
# uv's exit status becomes this process's exit status, so systemd's
# Restart=on-failure still sees the real result: a graceful SIGTERM shutdown
# exits 0 (no restart), a crash exits non-zero (systemd restarts it).
echo "[launcher] Starting app with uv run $ScriptName from directory $HomeDir"
echo "[launcher] Command line: $UVCmd run $ScriptName $*"
exec "$UVCmd" run "$ScriptName" "$@"
