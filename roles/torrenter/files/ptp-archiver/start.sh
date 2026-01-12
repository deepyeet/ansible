#!/bin/bash

# 1. Pre-flight Checks
if [ -z "$PTP_USER" ] || [ -z "$PTP_KEY" ]; then
    echo "FATAL: PTP_USER or PTP_KEY environment variables are missing."
    exit 1
fi

echo "--- Starting PTP Archiver ---"
echo "User: $(whoami) (UID: $(id -u))"

# 2. Resilient Download (Retry Loop)
# We check if the file exists AND has content (size > 0)
if [ ! -s "archiver.py" ]; then
    echo "Archiver script missing or empty. Attempting download..."
    
    count=0
    until [ -s "archiver.py" ]; do
        ((count++))
        echo "Download attempt $count..."
        
        # -f fails on HTTP errors, -s is silent (handled by our echo), -S shows errors
        curl -f -L -H "ApiUser: $PTP_USER" -H "ApiKey: $PTP_KEY" \
             -o archiver.py \
             "https://passthepopcorn.me/archive.php?action=script" || true
        
        if [ -s "archiver.py" ]; then
            echo "Download successful."
        else
            echo "Download failed. Retrying in 30 seconds..."
            sleep 30
        fi
    done
else
    echo "Archiver script already exists. Skipping download."
fi

# 3. Dynamic Config Generation
echo "Regenerating config.ptp with current settings..."

# Initialize the base JSON structure
BASE_JSON=$(jq -n \
  --arg apiKey "$PTP_KEY" \
  --arg apiUser "$PTP_USER" \
  '{ 
    "ApiKey": $apiKey,
    "ApiUser": $apiUser,
    "BaseURL": "https://passthepopcorn.me",
    "Containers": {},
    "Default": { "AfterFetchExec": null, "MaxStalled": 0, "Size": "500G", "WatchDirectory": "/watch" },
    "DownloadURL": "torrents.php?action=download", 
    "FetchSleep": 5, 
    "FetchURL": "archive.php?action=fetch", 
    "UpdateURL": "archive.php?action=script", 
    "VersionURL": "archive.php?action=scriptver"
  }')

# Check for the new, flexible PTP_CONTAINERS variable
if [ -n "$PTP_CONTAINERS" ]; then
    echo "Found PTP_CONTAINERS variable. Generating dynamic container config."
    # Start with the base JSON
    CONFIG_JSON="$BASE_JSON"
    # Read the comma-separated string, replacing commas with spaces for the loop
    for item in $(echo "$PTP_CONTAINERS" | tr ',' ' '); do
        # Split by colon
        NAME=$(echo "$item" | cut -d: -f1)
        SIZE=$(echo "$item" | cut -d: -f2)
        if [ -n "$NAME" ] && [ -n "$SIZE" ]; then
            echo "  - Adding container: $NAME ($SIZE)"
            # Add each container to the JSON object
            CONFIG_JSON=$(echo "$CONFIG_JSON" | jq \
              --arg name "$NAME" \
              --arg size "$SIZE" \
              '.Containers[$name] = { "AfterFetchExec": null, "MaxStalled": 0, "Size": $size, "WatchDirectory": "/watch" }')
        fi
    done
    # Write the final, dynamically generated config
    echo "$CONFIG_JSON" > config.ptp
else
    echo "[$(date)] PTP_CONTAINERS not defined"
    curl -fsS -m 10 --retry 3 --data-raw "ERROR: PTP_CONTAINERS not defined" "$HC_URL/fail"
    exit 1
fi


# 4. The Main Loop
echo "Initialization complete. Entering loop."

while true; do
    echo "------------------------------------------------"
    echo "[$(date)] Starting fetch cycle..."

    echo "Checking for updates..."
    python3 archiver.py update

    # Start Ping
    curl -fsS -m 10 --retry 3 --data-raw "Starting fetch cycle for all containers." "$HC_URL/start"

    # Get container list dynamically
    # Use awk to parse the tab-indented container list, getting the first field (the name)
    CONTAINERS=$(python3 archiver.py list | awk '/\t/ {print $1}')
    
    if [ -z "$CONTAINERS" ]; then
        echo "[$(date)] ERROR: Could not get container list."
        curl -fsS -m 10 --retry 3 --data-raw "ERROR: Could not get container list from archiver.py" "$HC_URL/fail"
        echo "[$(date)] Sleeping 6 hours..."
        sleep 21600
        continue # Skip to next loop iteration
    fi
    echo "[$(date)] Found containers: $CONTAINERS"

    OVERALL_FAILURE=0
    AGGREGATED_STATUS=""

    for container in $CONTAINERS; do
        echo "[$(date)] Fetching for container: $container..."
        OUTPUT=$(python3 archiver.py fetch "$container" 2>&1)
        EXIT_CODE=$?

        echo "Output for $container:"
        echo "$OUTPUT"

        STATUS_ENTRY=""
        if [ $EXIT_CODE -eq 0 ]; then
            STATUS_ENTRY="$container:OK"
        else
            if echo "$OUTPUT" | grep -q "No space left in container"; then
                STATUS_ENTRY="$container:FULL"
            else
                OVERALL_FAILURE=1
                # Extract the Python exception type, e.g., "RuntimeError"
                ERROR_TYPE=$(echo "$OUTPUT" | grep -oE '^[A-Za-z]+Error' | tail -n 1)
                if [ -z "$ERROR_TYPE" ]; then
                    ERROR_TYPE="Unknown"
                fi
                STATUS_ENTRY="$container:ERR($ERROR_TYPE)"
            fi
        fi

        # Append to the aggregated status string, with a comma if not the first entry
        if [ -z "$AGGREGATED_STATUS" ]; then
            AGGREGATED_STATUS="$STATUS_ENTRY"
        else
            AGGREGATED_STATUS="$AGGREGATED_STATUS, $STATUS_ENTRY"
        fi
    done

    # The final diagnostics string is our aggregated status, truncated just in case.
    DIAGNOSTICS=$(echo "$AGGREGATED_STATUS" | head -c 100)

    if [ $OVERALL_FAILURE -eq 0 ]; then
        echo "[$(date)] Fetch cycle completed. Status: $DIAGNOSTICS"
        curl -fsS -m 10 --retry 3 --data-raw "$DIAGNOSTICS" "$HC_URL"
    else
        echo "[$(date)] ERROR: Fetch cycle failed. Status: $DIAGNOSTICS"
        curl -fsS -m 10 --retry 3 --data-raw "ERROR: $DIAGNOSTICS" "$HC_URL/fail"
    fi

    echo "[$(date)] Sleeping 6 hours..."
    sleep 21600
done
