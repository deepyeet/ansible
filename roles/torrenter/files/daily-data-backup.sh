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

# SAFETY CHECK: Exit if Synology is not mounted
if ! mountpoint -q "$DEST_MOUNT"; then
  echo "CRITICAL: Synology backup target not mounted. Aborting to save SD card."
  exit 1
fi

# SAFETY CHECK: Exit if Source Drive is not mounted
if ! mountpoint -q "$SOURCE_MOUNT"; then
  echo "CRITICAL: Source drive $SOURCE_MOUNT not mounted. Aborting."
  exit 1
fi

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
