#!/bin/bash

set -e

# --- HEALTHCHECK SETUP ---
# Expect Healthchecks.io UUID as the first argument, followed by source and destination
HC_UUID="$1"
SOURCE_MOUNT="$2"
DEST_MOUNT="$3"

if [ -z "$HC_UUID" ] || [ -z "$SOURCE_MOUNT" ] || [ -z "$DEST_MOUNT" ]; then
    echo "ERROR: Healthchecks.io UUID, source mount, and destination mount must be provided as arguments."
    exit 1
fi
HC_URL="https://hc-ping.com/${HC_UUID}"

# This function runs automatically whenever the script exits
function handle_exit {
    # Check the exit code of the last command
    if [ $? -ne 0 ]; then
        echo "CRITICAL: Script failed. Sending failure signal to Healthchecks..."
        curl -fsS -m 10 --retry 5 "$HC_URL/fail"
    fi
}

# Activate the trap to catch ANY exit (crash, manual exit, or success)
trap handle_exit EXIT
# -------------------------

# --- DEPENDENCY CHECK ---
for cmd in curl rsync; do
    if ! command -v $cmd > /dev/null; then
        echo "CRITICAL: Required tool '$cmd' is missing. Exiting."
        exit 1
    fi
done
# ------------------------


# DATA BACKUP SCRIPT
SOURCE_BASE="$SOURCE_MOUNT/transmission"
DEST_BASE="$DEST_MOUNT/transmission"

mkdir -p "$DEST_BASE"

# Send Start Signal
curl -fsS -m 10 --retry 5 "$HC_URL/start"

# 1. Config Backup
rsync -ahHAXv --delete --numeric-ids "$SOURCE_BASE/config" "$DEST_BASE/"

# 2. Downloads Backup
rsync -ahHAXv --delete --numeric-ids --fuzzy --delete-after \
    --include='/downloads' \
    --include='/downloads/keep/***' \
    --include='/downloads/seed/***' \
    --include='/downloads/want/***' \
    --exclude='*' \
    "$SOURCE_BASE/" "$DEST_BASE/"

# Send Success Signal
# Note: If we get here, exit code is 0, so the trap above will do nothing (which is what we want)
curl -fsS -m 10 --retry 5 "$HC_URL"
