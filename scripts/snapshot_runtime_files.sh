#!/bin/bash

# Change directory to the location of the script
cd "$(dirname "$0")/.."

# List of filenames to snapshot
FILES_TO_SNAPSHOT=("logs/*" "system_state.json" "config.yaml")

# Maximum age of snapshots in hours where we keep all snapshots
MAX_HOURS=72

# Maximum age of snapshots in days where we keep the last snapshot of the day
MAX_DAYS=30

# Create the snapshots directory if it doesn't exist
SNAPSHOT_DIR="snapshots"
mkdir -p "$SNAPSHOT_DIR"

# Get the current date in YYYYMMDD_HHMMSS format
CURRENT_DATETIME=$(date +"%Y%m%d_%H%M%S")

# NEW: keep each snapshot in its own timestamped folder, preserve relative paths
SNAPSHOT_STAMP_DIR="$SNAPSHOT_DIR/$CURRENT_DATETIME"
mkdir -p "$SNAPSHOT_STAMP_DIR"

# Copy files to the snapshots directory, creating parent dirs for relative paths
for PATTERN in "${FILES_TO_SNAPSHOT[@]}"; do
    # Expand the pattern (handle wildcards)
    shopt -s nullglob
    for FILE in $PATTERN; do
        if [[ -f "$FILE" ]]; then
            # Strip leading slash (safety) so absolute paths don't escape SNAPSHOT_DIR
            REL_PATH="${FILE#/}"
            DEST="$SNAPSHOT_STAMP_DIR/$REL_PATH"
            mkdir -p "$(dirname "$DEST")"
            cp -a "$FILE" "$DEST"
            echo "Snapshot created for $FILE -> $DEST"
        fi
    done
    shopt -u nullglob
done

# Cleanup strategy:
# 1. Keep all snapshots from the past MAX_HOURS hours
# 2. For days beyond MAX_HOURS, keep only the last snapshot of each day up to MAX_DAYS
# 3. Delete everything older than MAX_DAYS

# Get current time in seconds since epoch
CURRENT_TIME=$(date +%s)
MAX_HOURS_SECONDS=$((MAX_HOURS * 3600))
MAX_DAYS_SECONDS=$((MAX_DAYS * 86400))

# Helper function to convert timestamp to Unix epoch
get_snapshot_time() {
    local DATE_PART="$1"
    local TIME_PART="$2"
    
    local YEAR="${DATE_PART:0:4}"
    local MONTH="${DATE_PART:4:2}"
    local DAY="${DATE_PART:6:2}"
    local HOUR="${TIME_PART:0:2}"
    local MINUTE="${TIME_PART:2:2}"
    local SECOND="${TIME_PART:4:2}"
    
    # Cross-platform date parsing
    if date --version >/dev/null 2>&1; then
        # GNU date (Linux)
        date -d "$YEAR-$MONTH-$DAY $HOUR:$MINUTE:$SECOND" +%s 2>/dev/null
    else
        # BSD date (macOS)
        date -j -f "%Y-%m-%d %H:%M:%S" "$YEAR-$MONTH-$DAY $HOUR:$MINUTE:$SECOND" +%s 2>/dev/null
    fi
}

# First pass: Delete snapshots older than MAX_DAYS
for SNAPSHOT in "$SNAPSHOT_DIR"/*/; do
    [[ -d "$SNAPSHOT" ]] || continue
    
    SNAPSHOT_NAME=$(basename "$SNAPSHOT")
    
    if [[ $SNAPSHOT_NAME =~ ^([0-9]{8})_([0-9]{6})$ ]]; then
        DATE_PART="${BASH_REMATCH[1]}"
        TIME_PART="${BASH_REMATCH[2]}"
        
        SNAPSHOT_TIME=$(get_snapshot_time "$DATE_PART" "$TIME_PART")
        
        if [[ -z "$SNAPSHOT_TIME" ]]; then
            echo "Warning: Could not parse timestamp for $SNAPSHOT_NAME, skipping"
            continue
        fi
        
        AGE_SECONDS=$((CURRENT_TIME - SNAPSHOT_TIME))
        
        # Delete if older than MAX_DAYS
        if [[ $AGE_SECONDS -gt $MAX_DAYS_SECONDS ]]; then
            echo "Deleting snapshot older than $MAX_DAYS days: $SNAPSHOT_NAME"
            rm -rf "$SNAPSHOT"
        fi
    fi
done

# Second pass: For days beyond MAX_HOURS, keep only the last snapshot per day
# Build a list of all snapshot directories sorted in reverse (newest first)
SNAPSHOTS_SORTED=$(ls -1d "$SNAPSHOT_DIR"/*/ 2>/dev/null | sort -r)

# Track which days we've seen (simple string matching, works in bash 3.2+)
SEEN_DAYS=""

for SNAPSHOT in $SNAPSHOTS_SORTED; do
    [[ -d "$SNAPSHOT" ]] || continue
    
    SNAPSHOT_NAME=$(basename "$SNAPSHOT")
    
    if [[ $SNAPSHOT_NAME =~ ^([0-9]{8})_([0-9]{6})$ ]]; then
        DATE_PART="${BASH_REMATCH[1]}"
        TIME_PART="${BASH_REMATCH[2]}"
        
        SNAPSHOT_TIME=$(get_snapshot_time "$DATE_PART" "$TIME_PART")
        
        if [[ -z "$SNAPSHOT_TIME" ]]; then
            continue
        fi
        
        AGE_SECONDS=$((CURRENT_TIME - SNAPSHOT_TIME))
        
        # Only process snapshots between MAX_HOURS and MAX_DAYS
        if [[ $AGE_SECONDS -gt $MAX_HOURS_SECONDS ]] && [[ $AGE_SECONDS -le $MAX_DAYS_SECONDS ]]; then
            # Check if we've already seen this day
            if [[ "$SEEN_DAYS" == *"|$DATE_PART|"* ]]; then
                # We've already kept a snapshot for this day, delete this one
                echo "Deleting non-last snapshot of the day: $SNAPSHOT_NAME"
                rm -rf "$SNAPSHOT"
            else
                # First snapshot we've seen for this day (and it's the newest since we're going in reverse)
                SEEN_DAYS="${SEEN_DAYS}|${DATE_PART}|"
            fi
        fi
    fi
done

# Clean up any empty directories
find "$SNAPSHOT_DIR" -type d -empty -delete

echo "Snapshot cleanup complete:"
echo "  - Kept all snapshots from the past $MAX_HOURS hours"
echo "  - Kept last snapshot of each day for the past $MAX_DAYS days"
echo "  - Deleted snapshots older than $MAX_DAYS days"