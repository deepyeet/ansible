#!/bin/bash

# Configuration / Defaults
DATA_DIR="${DATA_DIR:-/data}"
CACHE_FILE="${DATA_DIR}/last_ip"
COOKIE_FILE="${DATA_DIR}/mam.cookies"
ID_FILE="${DATA_DIR}/mam.id"
API_URL="https://t.myanonamouse.net/json/dynamicSeedbox.php"
USER_AGENT="Mozilla/5.0 (Compatible; PrivateTrackerUpdater/2.0)"

# Timers (Seconds) - Sourced from Env Vars with fallback to old defaults
INTERVAL_NORMAL=${MAM_INTERVAL_NORMAL:-60}
INTERVAL_ERROR=${MAM_INTERVAL_ERROR:-60}
INTERVAL_RATELIMIT=${MAM_INTERVAL_RATELIMIT:-900} 

# Logging Helper
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Healthcheck Helper
hc_ping() {
    # $1: endpoint (e.g., /start, /fail, /log, or empty for success)
    # $2: diagnostic message
    local endpoint="$1"
    local message="$2"
    # Silently run in background to not block execution
    curl -fsS -m 10 --retry 3 --data-raw "${message:0:100}" "$HC_URL$endpoint" > /dev/null 2>&1 &
}

# Dependency Check
for cmd in curl jq; do
    if ! command -v $cmd &> /dev/null; then
        log "CRITICAL: '$cmd' is missing. Install it and restart."
        hc_ping "/fail" "CRITICAL: command '$cmd' is missing."
        exit 1
    fi
done

# ------------------------------------------------------------------------------
# Core Logic
# ------------------------------------------------------------------------------

get_mam_id() {
    # 1. Try file first (allows changing ID without restarting container)
    if [ -f "$ID_FILE" ] && [ -s "$ID_FILE" ]; then
        tr -d '[:space:]' < "$ID_FILE"
        return 0
    fi
    # 2. Fallback to Env Var
    if [ -n "$MAM_ID" ]; then
        echo "$MAM_ID"
        return 0
    fi
    return 1
}

get_public_ip() {
    # Rotate providers to avoid single point of failure
    local providers=("https://ip.me" "https://ifconfig.co/ip" "https://icanhazip.com")
    for url in "${providers[@]}"; do
        ip=$(curl -s --max-time 5 "$url" | tr -d '[:space:]')
        if [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            echo "$ip"
            return 0
        fi
    done
    return 1
}

authenticate() {
    local mam_id=$(get_mam_id)
    if [ -z "$mam_id" ]; then
        log "ERROR: No MAM_ID found in Environment or $ID_FILE. Waiting..."
        hc_ping "/fail" "FAIL: MAM_ID is not set."
        return 1
    fi

    log "Authenticating with Session ID..."
    hc_ping "/log" "Authenticating with new session..."
    # -c (write jar), -b (read custom string)
    response=$(curl -s -A "$USER_AGENT" -c "$COOKIE_FILE" -b "mam_id=$mam_id" "$API_URL")
    
    # Check if we got valid JSON back
    if ! echo "$response" | jq -e . >/dev/null 2>&1; then
        log "AUTH ERROR: Server returned invalid JSON: ${response:0:100}..."
        hc_ping "/fail" "FAIL: Auth returned invalid JSON."
        return 1
    fi

    success=$(echo "$response" | jq -r '.Success')
    if [ "$success" == "true" ]; then
        log "Session valid. Cookie stored."
        hc_ping "/log" "OK: Authentication successful."
        return 0
    else
        msg=$(echo "$response" | jq -r '.msg')
        if [ "$msg" == "Last change too recent" ]; then
            log "AUTH FAILED: Rate Limit Hit. Sleeping 15m..."
            hc_ping "/fail" "FAIL: Auth rate-limited."
            sleep $INTERVAL_RATELIMIT
            return 1
        fi
        log "AUTH FAILED: $msg"
        hc_ping "/fail" "FAIL: Auth failed - $msg"
        return 1
    fi
}

# ------------------------------------------------------------------------------
# Main Loop
# ------------------------------------------------------------------------------

log "Service started. Monitoring IP changes..."

while true; do
    hc_ping "/start" # Signal start of a new check cycle
    
    # 1. Fetch IP
    current_ip=$(get_public_ip)
    if [ -z "$current_ip" ]; then
        log "WARN: Unable to determine public IP. Retrying in $INTERVAL_ERROR sec..."
        hc_ping "/fail" "FAIL: Could not determine public IP."
        sleep $INTERVAL_ERROR
        continue
    fi

    # 2. Check Cache
    cached_ip=""
    if [ -f "$CACHE_FILE" ]; then
        cached_ip=$(cat "$CACHE_FILE")
    fi

    if [ "$current_ip" == "$cached_ip" ]; then
        # No change, sleep quietly but send a success ping to show we are alive
        hc_ping "" "OK: IP has not changed from $current_ip."
        sleep $INTERVAL_NORMAL
        continue
    fi

    log "CHANGE DETECTED: ${cached_ip:-None} -> $current_ip"

    # 3. Update Request
    # Use cookie file (-b) and update it (-c)
    response=$(curl -s -A "$USER_AGENT" -b "$COOKIE_FILE" -c "$COOKIE_FILE" "$API_URL")

    # 4. Parsing & Error Handling
    if ! echo "$response" | jq -e . >/dev/null 2>&1; then
        log "API ERROR: Invalid JSON received. Retrying... Raw: ${response:0:50}"
        hc_ping "/fail" "FAIL: API returned invalid JSON."
        sleep $INTERVAL_ERROR
        continue
    fi

    success=$(echo "$response" | jq -r '.Success')
    msg=$(echo "$response" | jq -r '.msg // empty')

    if [ "$success" == "true" ]; then
        log "SUCCESS: $msg"
        echo "$current_ip" > "$CACHE_FILE"
        hc_ping "" "OK: $msg ($current_ip)"
        sleep $INTERVAL_NORMAL

    else
        # Handle Specific Error Codes
        case "$msg" in
            "No Change"|"Completed")
                # Sometimes success is false but message implies it's fine
                log "STATE OK: $msg"
                echo "$current_ip" > "$CACHE_FILE"
                hc_ping "" "OK: $msg ($current_ip)"
                sleep $INTERVAL_NORMAL
                ;;

            "Last change too recent")
                log "RATE LIMIT: $msg. Pausing for 15 mins."
                hc_ping "/fail" "FAIL: Rate Limit Hit."
                sleep $INTERVAL_RATELIMIT
                ;;

            *"Session Cookie"*|*"Invalid session"*|*"session type"*)
                log "SESSION EXPIRED: $msg. Re-authenticating..."
                hc_ping "/log" "Session expired. Re-authenticating..."
                rm -f "$COOKIE_FILE"
                if authenticate; then
                    log "Re-auth successful. Retrying immediately."
                    sleep 5 # Give it a moment before the next loop
                else
                    log "Re-auth failed. Retrying in $INTERVAL_ERROR sec."
                    sleep $INTERVAL_ERROR
                fi
                ;;

            *)
                log "UNKNOWN ERROR: $msg. Retrying..."
                hc_ping "/fail" "FAIL: Unknown error - $msg"
                sleep $INTERVAL_ERROR
                ;;
        esac
    fi
done
