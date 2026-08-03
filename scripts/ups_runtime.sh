#!/bin/bash

# APC UPS monitoring script
# Gets battery charge and runtime from APC UPS using upsc command
# Write the data to a JSON file for use by other scripts or monitoring tools

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
JSON_FILE=""

# Load .env file if it exists
if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env"
    set +a
fi

# Parse command line arguments
UPS_ID=""
LOOP_MODE=false
SIMULATE_MODE=false
SIMULATE_CHARGE="${SIMULATE_CHARGE:-97}"
SIMULATE_RUNTIME="${SIMULATE_RUNTIME:-3215}"
SIMULATE_STATE="${SIMULATE_STATE:-discharging}"
INTERVAL=30

while [[ $# -gt 0 ]]; do
    case $1 in
        --loop)
            LOOP_MODE=true
            if [[ -n $2 && $2 =~ ^[0-9]+$ ]]; then
                INTERVAL=$2
                shift
            fi
            shift
            ;;
        --simulate)
            SIMULATE_MODE=true
            # Check if charge value provided on command line
            if [[ -n $2 && $2 =~ ^[0-9]+$ ]]; then
                SIMULATE_CHARGE=$2
                shift
            fi
            # Check if runtime value provided on command line
            if [[ -n $2 && $2 =~ ^[0-9]+$ ]]; then
                SIMULATE_RUNTIME=$2
                shift
            fi
            # Check if state value provided on command line
            if [[ -n $2 && ($2 == "charged" || $2 == "charging" || $2 == "discharging") ]]; then
                SIMULATE_STATE=$2
                shift
            fi
            # If values weren't provided on command line, they'll use env vars or defaults
            shift
            ;;
        --ups)
            if [[ -n $2 ]]; then
                UPS_ID="$2"
                shift
                shift
            else
                echo "Error: --ups requires a UPS identifier (e.g. apc@localhost)"
                exit 1
            fi
            ;;
        --output)
            if [[ -n $2 ]]; then
                JSON_FILE="$2"
                shift
                shift
            else
                echo "Error: --output requires a file path"
                exit 1
            fi
            ;;
        *)
            echo "Usage: $0 --ups UPS_ID [--loop INTERVAL] [--output FILE] [--simulate [CHARGE] [RUNTIME]]"
            echo "  --ups UPS_ID                     UPS identifier for upsc (e.g. apc@localhost) [required]"
            echo "  --loop INTERVAL                  Run continuously, updating every INTERVAL seconds"
            echo "  --output FILE                    Specify the path to the JSON output file (default: console output)"
            echo "  --simulate [CHARGE] [RUNTIME] [STATE]     Simulate UPS data instead of reading from upsc (for testing)"
            echo "                                   CHARGE, RUNTIME, and STATE are optional; if not provided, uses SIMULATE_CHARGE,"
            echo "                                   SIMULATE_RUNTIME, and SIMULATE_STATE environment variables (or defaults: 97, 3215, discharging)"
            exit 1
            ;;
    esac
done

# Validate required arguments
if [[ -z "$UPS_ID" && "$SIMULATE_MODE" = false ]]; then
    echo "Error: --ups UPS_ID is required (e.g. --ups apc@localhost)"
    exit 1
fi

# Function to get UPS data and write to JSON
write_ups_status() {
    if [ "$SIMULATE_MODE" = true ]; then
        # Simulate UPS data
        local charge=$SIMULATE_CHARGE
        local runtime=$SIMULATE_RUNTIME
        local state=$SIMULATE_STATE
    else
        # Get battery charge (percent)
        local charge=$(upsc "$UPS_ID" battery.charge 2>/dev/null)
        
        # Get battery runtime (seconds)
        local runtime=$(upsc "$UPS_ID" battery.runtime 2>/dev/null)

        # Get battery state (charging/discharging)
        # APC UPS ups.status returns:
        #   "OL" (On Line) when the UPS is running on main power and fully charged.
        #   "OL CHRG" (On Line) when the UPS is on main power and charging.
        #   "OB DISCHRG" (On Battery) when the UPS is running on battery power (discharging)
        local ups_status=$(upsc "$UPS_ID" ups.status 2>/dev/null)
        if [[ "$ups_status" == "OL" ]]; then
            if [[ "$charge" =~ ^[0-9]+$ ]] && (( charge < 99 )); then
                state="charging"
            else
                state="charged"
            fi
        elif [[ "$ups_status" == "OL CHRG"* ]]; then
            state="charging"
        elif [[ "$ups_status" == "OB DISCHRG"* ]]; then
            state="discharging"
        elif [[ "$ups_status" == "OB" ]]; then
            state="discharging"
        else
            state="unknown"
        fi

        # Replace empty values with null for valid JSON
        [[ -z "$charge" ]] && charge="null"
        [[ -z "$runtime" ]] && runtime="null"
        [[ -z "$state" ]] && state=""
    fi
    
    # Get current local timestamp
    local timestamp=$(date +"%Y-%m-%d %H:%M:%S")
    
    # Generate JSON
    local json_output=$(cat <<EOF
{
  "timestamp": "$timestamp",
  "battery_state": "$state",
  "battery_charge_percent": $charge,
  "battery_runtime_seconds": $runtime
}
EOF
)
    
    # Write JSON to file or console
    if [ -n "$JSON_FILE" ]; then
        echo "$json_output" > "$JSON_FILE"
    else
        echo "$json_output"
    fi
}

# Run once or loop based on mode
if [ "$LOOP_MODE" = true ]; then
    if [ -n "$JSON_FILE" ]; then
        echo "Starting UPS monitoring (writing to $JSON_FILE every ${INTERVAL}s)" >&2
    else
        echo "Starting UPS monitoring (writing to console every ${INTERVAL}s)" >&2
    fi
    echo "Press Ctrl+C to stop" >&2
    
    while true; do
        write_ups_status
        if [ -n "$JSON_FILE" ]; then
            echo "$(date): Updated UPS status" >&2
        fi
        sleep "$INTERVAL"
    done
else
    # Run once and exit
    write_ups_status
    if [ -n "$JSON_FILE" ]; then
        echo "UPS status written to $JSON_FILE" >&2
    fi
fi
