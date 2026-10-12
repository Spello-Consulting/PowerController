#!/bin/bash

set -euo pipefail

# Default parameters. Override with command-line arguments if needed.
RESOLUTION_MINUTES="${RESOLUTION_MINUTES:-30}"
LOOKBACK_MINUTES="${LOOKBACK_MINUTES:-60}"
LOOKAHEAD_MINUTES="${LOOKAHEAD_MINUTES:-360}"
OUTPUT_FILE="${OUTPUT_FILE:-logs/amber_raw_prices.json}"
BASE_URL="${AMBER_API_BASE_URL:-https://api.amber.com.au/v1}"
REQUEST_TIMEOUT_SECONDS="${AMBER_TIMEOUT_SECONDS:-10}"
AMBER_API_KEY_REFERENCE="${AMBER_API_KEY_REFERENCE:-op://Dev/AmberElectric/APIKey}"
SITE_ID_OVERRIDE="${AMBER_SITE_ID:-}"
ACTIVE_SITE_STATUS="${AMBER_SITE_STATUS:-active}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
    cat <<EOF
Usage: $(basename "${BASH_SOURCE[0]}") [options]

Downloads raw Amber pricing data for a configurable historic/future window and
writes the formatted JSON response to a file.

Options:
  --resolution <5|30>          Price resolution in minutes. Default: $RESOLUTION_MINUTES
  --lookback-minutes <minutes> Minutes of prior data to request. Default: $LOOKBACK_MINUTES
  --lookahead-minutes <minutes> Minutes of future data to request. Default: $LOOKAHEAD_MINUTES
  --output <path>              Output JSON path. Default: $OUTPUT_FILE
  --site-id <id>               Override active-site auto-discovery.
  --help, -h                   Show this help.

Environment overrides:
  RESOLUTION_MINUTES, LOOKBACK_MINUTES, LOOKAHEAD_MINUTES, OUTPUT_FILE,
  AMBER_API_BASE_URL, AMBER_TIMEOUT_SECONDS, AMBER_API_KEY_REFERENCE,
  AMBER_SITE_ID, AMBER_SITE_STATUS
EOF
}

require_command() {
    local command_name="$1"
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Error: required command '$command_name' is not on PATH." >&2
        exit 1
    fi
}

validate_non_negative_integer() {
    local label="$1"
    local value="$2"
    if [[ ! "$value" =~ ^[0-9]+$ ]]; then
        echo "Error: $label must be a non-negative integer. Got '$value'." >&2
        exit 1
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --resolution)
            RESOLUTION_MINUTES="$2"
            shift 2
            ;;
        --lookback-minutes)
            LOOKBACK_MINUTES="$2"
            shift 2
            ;;
        --lookahead-minutes)
            LOOKAHEAD_MINUTES="$2"
            shift 2
            ;;
        --output)
            OUTPUT_FILE="$2"
            shift 2
            ;;
        --site-id)
            SITE_ID_OVERRIDE="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Error: unknown argument '$1'." >&2
            usage >&2
            exit 1
            ;;
    esac
done

require_command op
require_command curl
require_command jq

if [[ "$RESOLUTION_MINUTES" != "5" && "$RESOLUTION_MINUTES" != "30" ]]; then
    echo "Error: resolution must be 5 or 30 minutes." >&2
    exit 1
fi

validate_non_negative_integer "Lookback minutes" "$LOOKBACK_MINUTES"
validate_non_negative_integer "Lookahead minutes" "$LOOKAHEAD_MINUTES"
validate_non_negative_integer "Request timeout" "$REQUEST_TIMEOUT_SECONDS"

previous_intervals=$(( (LOOKBACK_MINUTES + RESOLUTION_MINUTES - 1) / RESOLUTION_MINUTES ))
next_intervals=$(( (LOOKAHEAD_MINUTES + RESOLUTION_MINUTES - 1) / RESOLUTION_MINUTES ))

if (( previous_intervals + next_intervals == 0 )); then
    echo "Error: the total requested interval count must be greater than zero." >&2
    exit 1
fi

if (( previous_intervals + next_intervals > 2048 )); then
    echo "Error: Amber allows at most 2048 intervals per request." >&2
    exit 1
fi

echo "Resolving Amber API key from 1Password..." >&2
AMBER_API_KEY="$(op read "$AMBER_API_KEY_REFERENCE")"
if [[ -z "$AMBER_API_KEY" ]]; then
    echo "Error: resolved AMBER_API_KEY is empty." >&2
    exit 1
fi

headers=(
    -H "accept: application/json"
    -H "Authorization: Bearer ${AMBER_API_KEY}"
)

site_id="$SITE_ID_OVERRIDE"
if [[ -z "$site_id" ]]; then
    echo "Fetching Amber site list..." >&2
    sites_response="$(curl --silent --show-error --fail --max-time "$REQUEST_TIMEOUT_SECONDS" "${headers[@]}" "$BASE_URL/sites")"
    site_id="$(jq -r --arg status "$ACTIVE_SITE_STATUS" 'map(select(.status == $status)) | .[0].id // empty' <<<"$sites_response")"
fi

if [[ -z "$site_id" ]]; then
    echo "Error: no Amber site id was found. Set AMBER_SITE_ID or check your account/site status." >&2
    exit 1
fi

output_path="$OUTPUT_FILE"
if [[ "$output_path" != /* ]]; then
    output_path="$PROJECT_ROOT/$output_path"
fi
mkdir -p "$(dirname "$output_path")"

prices_url="$BASE_URL/sites/$site_id/prices/current?next=$next_intervals&previous=$previous_intervals&resolution=$RESOLUTION_MINUTES"
temp_file="$(mktemp)"

echo "Downloading Amber prices from site '$site_id'..." >&2
curl --silent --show-error --fail --max-time "$REQUEST_TIMEOUT_SECONDS" "${headers[@]}" "$prices_url" \
    | jq --argjson resolution "$RESOLUTION_MINUTES" 'map(select((.duration | tonumber) == $resolution))' > "$temp_file"

mv "$temp_file" "$output_path"

echo "Saved formatted Amber pricing data to $output_path" >&2
echo "Resolution: ${RESOLUTION_MINUTES} min | Previous intervals: ${previous_intervals} | Next intervals: ${next_intervals}" >&2