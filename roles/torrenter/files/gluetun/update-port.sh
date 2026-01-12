#!/bin/sh

# ==============================================================================
# DURABLE TRANSMISSION PORT UPDATER (v2025.3)
# ==============================================================================

# --- CONFIGURATION ---
# We pull these from the Docker Environment (passed via compose.yaml)
# Defaults are provided after ':-' just in case, but Env vars are preferred.
TR_USER="${TR_USER}"
TR_PASS="${TR_PASS}"
TR_HOST="${TR_HOST}"
TR_PORT="${TR_PORT}"
NEW_PORT=$1

# Retry Settings: 60 checks * 5 seconds = 5 Minutes of wait time.
MAX_RETRIES=60 
SLEEP_SEC=5

# --- LOGGING FUNCTION ---
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# --- 1. PRE-FLIGHT CHECKS ---
if [ -z "$NEW_PORT" ]; then
    log "CRITICAL: No port provided by Gluetun. Exiting."
    exit 1
fi

# Ensure standard tools exist
for tool in wget grep awk; do
    if ! command -v $tool > /dev/null; then
        log "CRITICAL: Required tool '$tool' is missing. Exiting."
        exit 1
    fi
done

log "Starting port update to: $NEW_PORT"

# --- 2. WAIT FOR TRANSMISSION ---
attempt=1
while [ $attempt -le $MAX_RETRIES ]; do
    # Capture headers (-S) to stderr, redirect to stdout (2>&1)
    HTTP_CHECK=$(wget --no-check-certificate -q -S --spider \
        --http-user="$TR_USER" --http-password="$TR_PASS" \
        "http://$TR_HOST:$TR_PORT/transmission/rpc" 2>&1)

    # Extract Status Code
    STATUS_CODE=$(echo "$HTTP_CHECK" | awk '/HTTP\// {print $2}' | tail -1)

    if [ -n "$STATUS_CODE" ] && [ "$STATUS_CODE" != "000" ]; then
        log "Transmission is online (HTTP $STATUS_CODE). Proceeding."
        break
    fi

    if [ $((attempt % 5)) -eq 0 ]; then
        log "Waiting for Transmission... (Attempt $attempt/$MAX_RETRIES)"
    fi
    
    sleep $SLEEP_SEC
    attempt=$((attempt + 1))
done

if [ $attempt -gt $MAX_RETRIES ]; then
    log "TIMEOUT: Transmission did not respond after $((MAX_RETRIES * SLEEP_SEC)) seconds."
    exit 1
fi

# --- 3. GET SESSION ID ---
log "Authenticating and requesting Session ID..."

HEADERS=$(wget --no-check-certificate -q -S --spider \
    --http-user="$TR_USER" --http-password="$TR_PASS" \
    "http://$TR_HOST:$TR_PORT/transmission/rpc" 2>&1)

SESSION_ID=$(echo "$HEADERS" | grep -i "X-Transmission-Session-Id" | head -n 1 | awk -F': ' '{print $2}' | tr -d '\r')

if [ -z "$SESSION_ID" ]; then
    log "CRITICAL ERROR: Failed to get Session ID."
    
    # DEBUGGING
    ERROR_CODE=$(echo "$HEADERS" | awk '/HTTP\// {print $2}' | tail -1)
    if [ "$ERROR_CODE" = "401" ]; then
        log "DIAGNOSIS: HTTP 401 Unauthorized. Check your Username/Password in compose.yaml."
    else
        log "DIAGNOSIS: Unknown error (HTTP $ERROR_CODE). Headers dump:"
        echo "$HEADERS"
    fi
    exit 1
fi

log "Session ID acquired: $SESSION_ID"

# --- 4. EXECUTE UPDATE ---
RESPONSE=$(wget -qO- --http-user="$TR_USER" --http-password="$TR_PASS" \
  --header="X-Transmission-Session-Id: $SESSION_ID" \
  --post-data="{\"method\":\"session-set\",\"arguments\":{\"peer-port\":$NEW_PORT}}" \
  "http://$TR_HOST:$TR_PORT/transmission/rpc")

# --- 5. VERIFY ---
if echo "$RESPONSE" | grep -q "success"; then
    log "SUCCESS: Port updated to $NEW_PORT"
else
    log "ERROR: Update failed. Response from Transmission:"
    echo "$RESPONSE"
    exit 1
fi
