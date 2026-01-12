
import os
import sys
import time
import requests
import logging


# --- Custom Exception for Healthchecks ---
class HealthcheckError(Exception):
    """Custom exception allowing for intelligent, prioritized message truncation."""
    def __init__(self, message, diag_context=None, torrent=None, mismatch_ports=None, portcheck_error=None):
        self.message = message
        self.diag_context = diag_context
        self.torrent = torrent
        self.mismatch_ports = mismatch_ports
        self.portcheck_error = portcheck_error
        super().__init__(self.full_message)

    @property
    def full_message(self):
        """The complete, untruncated error message for local logging."""
        if self.portcheck_error:
            detail = self.portcheck_error.get('content') or self.portcheck_error.get('exception')
            return f"{self.message}({detail}) | {self.diag_context}"
        if self.mismatch_ports:
            g = self.mismatch_ports.get('gluetun')
            t = self.mismatch_ports.get('transmission')
            return f"{self.message}(G:{g}|T:{t}) | {self.diag_context}"
        if self.torrent:
            return f"{self.message} T:{self.torrent.get('id')}({self.torrent.get('name')}) - {self.torrent.get('errorString')} | {self.diag_context}"
        if self.diag_context:
            return f"{self.message} | {self.diag_context}"
        return self.message

    @property
    def hc_message(self):
        """A truncated message for Healthchecks.io, prioritizing critical info."""
        full_msg = self.full_message
        if len(full_msg) <= 100:
            return full_msg

        # For port check errors
        if self.portcheck_error:
            p1 = self.message
            detail = self.portcheck_error.get('content') or self.portcheck_error.get('exception')
            p2 = f"({detail})"
            msg = f"{p1}{p2}"
            if len(msg) <= 100:
                return msg
            return msg[:100]

        # For port mismatch errors
        if self.mismatch_ports:
            g = self.mismatch_ports.get('gluetun')
            t = self.mismatch_ports.get('transmission')
            p1 = self.message
            p2 = f"(G:{g}|T:{t})"
            msg = f"{p1}{p2}"
            if len(msg) <= 100:
                return msg
            return msg[:100]

        # For torrent-specific errors
        if self.torrent:
            p1 = self.message
            p2 = f" T:{self.torrent.get('id')}({self.torrent.get('name')})"
            p3 = f": {self.torrent.get('errorString')}"
            
            msg = f"{p1}{p2}{p3}"
            if len(msg) <= 100:
                return msg
            
            available_len = 100 - len(f"{p1}{p2}")
            if available_len > 4:
                p3_trunc = p3[:available_len - 3] + "..."
                return f"{p1}{p2}{p3_trunc}"

            p2_trunc = f" T:{self.torrent.get('id')}"
            available_len = 100 - len(f"{p1}{p2_trunc}")
            if available_len > 4:
                p3_trunc = p3[:available_len - 3] + "..."
                return f"{p1}{p2_trunc}{p3_trunc}"

            return (f"{p1}{p2_trunc}")[:100]

        # For other errors, just truncate the end
        return full_msg[:97] + "..."


# --- CONFIGURATION ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Load configuration from environment variables ---
TR_HOST = os.environ.get("TR_HOST", "localhost")
TR_PORT = int(os.environ.get("TR_PORT", 9091))
TR_USER = os.environ.get("TR_USER")
TR_PASS = os.environ.get("TR_PASS")

GLUETUN_HOST = os.environ.get("GLUETUN_HOST", "localhost")
GLUETUN_PORT = int(os.environ.get("GLUETUN_PORT", 8000))
GLUETUN_USER = os.environ.get("GLUETUN_USER")
GLUETUN_PASS = os.environ.get("GLUETUN_PASS")

HC_URL = os.environ.get("HC_URL")
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 300)) # Default to 5 minutes

# --- Validate that essential variables are set ---
if not all([TR_USER, TR_PASS, GLUETUN_USER, GLUETUN_PASS, HC_URL]):
    logging.error("FATAL: One or more essential environment variables are not set. Exiting.")
    sys.exit(1)

GLUETUN_BASE_URL = f"http://{GLUETUN_HOST}:{GLUETUN_PORT}"
TR_RPC_URL = f"http://{TR_HOST}:{TR_PORT}/transmission/rpc"

def ping_healthchecks(event: str, message: str = ""):
    """Pings the Healthchecks.io endpoint with an event (start, success, fail)."""
    try:
        url = f"{HC_URL}/{event}" if event in ["start", "fail"] else HC_URL
        requests.post(url, data=message.encode('utf-8'), timeout=10)
    except requests.RequestException as e:
        logging.warning(f"Could not ping Healthchecks.io: {e}")

def get_transmission_session_id(session: requests.Session) -> str:
    """Gets the X-Transmission-Session-Id header."""
    try:
        response = session.get(TR_RPC_URL, auth=(TR_USER, TR_PASS), timeout=5)
    except requests.RequestException as e:
        if isinstance(e, requests.exceptions.HTTPError) and e.response.status_code == 409:
            return e.response.headers.get("X-Transmission-Session-Id")
        raise ConnectionError(f"Failed to connect to Transmission to get session ID: {e}") from e

    # This path is unexpected, but if the server returns 200 on first hit, handle it.
    return response.headers.get("X-Transmission-Session-Id")

def run_transmission_rpc(session: requests.Session, method: str, arguments: dict = None) -> dict:
    """Runs a command on the Transmission RPC."""
    if arguments is None:
        arguments = {}
    payload = {"method": method, "arguments": arguments}
    
    # Get session ID first time
    if 'X-Transmission-Session-Id' not in session.headers:
        session_id = get_transmission_session_id(session)
        if not session_id:
            raise ValueError("Could not retrieve Transmission Session ID.")
        session.headers.update({"X-Transmission-Session-Id": session_id})

    try:
        response = session.post(TR_RPC_URL, json=payload, auth=(TR_USER, TR_PASS), timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        # Handle session ID conflict
        if e.response.status_code == 409:
            logging.info("Transmission session ID expired, renewing...")
            session_id = e.response.headers.get("X-Transmission-Session-Id")
            if not session_id:
                 raise ValueError("Could not renew expired Transmission Session ID.")
            session.headers.update({"X-Transmission-Session-Id": session_id})
            
            # Retry the request with the new session ID
            response = session.post(TR_RPC_URL, json=payload, auth=(TR_USER, TR_PASS), timeout=10)
            response.raise_for_status()
            return response.json()
        raise
    except requests.RequestException as e:
        raise ConnectionError(f"Transmission RPC call '{method}' failed: {e}") from e

def check_external_port(port: int) -> tuple[str, dict]:
    """Checks the port status using the external service. Returns (status_bool, error_dict)."""
    try:
        response = requests.get(
            f"https://portcheck.transmissionbt.com/{port}",
            timeout=(5, 15), # (connect, read)
        )
        response.raise_for_status()
        content = response.text.strip()
        if content == "1":
            return True, None
        elif content == "0":
            return False, None
        else:
            return None, {'error': 'E:PORTCHECK_UNEXPECTED_RESP', 'content': content}
    except requests.RequestException as e:
        return None, {'error': 'E:PORTCHECK_DOWN', 'exception': type(e).__name__}

def main():
    """Main checking logic."""
    tr_session = requests.Session()
    
    while True:
        ping_healthchecks("start")
        diagnostic_context = "No Additional Info"
        
        try:
            # 1. Get Gluetun data
            gluetun_auth = (GLUETUN_USER, GLUETUN_PASS)
            gluetun_port_resp = requests.get(f"{GLUETUN_BASE_URL}/v1/portforward", auth=gluetun_auth, timeout=5)
            gluetun_port_resp.raise_for_status()
            gluetun_port_data = gluetun_port_resp.json()
            
            gluetun_vpn_resp = requests.get(f"{GLUETUN_BASE_URL}/v1/vpn/status", auth=gluetun_auth, timeout=5)
            gluetun_vpn_resp.raise_for_status()
            gluetun_vpn_data = gluetun_vpn_resp.json()

            gluetun_port = gluetun_port_data.get("port")
            gluetun_vpn_status = gluetun_vpn_data.get("status")

            if not all([gluetun_port, gluetun_vpn_status]):
                raise HealthcheckError("E:GLUETUN_PARSE | Missing 'port' or 'status' in Gluetun API response")

            # 2. Get Transmission data
            session_data = run_transmission_rpc(tr_session, "session-get", {"fields": ["peer-port"]})
            session_stats = run_transmission_rpc(tr_session, "session-stats")
            torrents_data = run_transmission_rpc(tr_session, "torrent-get", {"fields": ["id", "name", "error", "errorString"]})

            transmission_port = session_data.get("arguments", {}).get("peer-port")
            stats_args = session_stats.get("arguments", {})
            active_count = stats_args.get("activeTorrentCount", "N/A")
            dl_speed = stats_args.get("downloadSpeed", "N/A")
            ul_speed = stats_args.get("uploadSpeed", "N/A")

            if transmission_port is None:
                raise HealthcheckError("E:TR_PARSE | Missing 'peer-port' in Transmission session data")
                
            system_error_torrents = [t for t in torrents_data.get("arguments", {}).get("torrents", []) if t.get("error") == 3]
            system_error_count = len(system_error_torrents)

            # 3. Check external port
            port_status_bool, port_check_error = check_external_port(transmission_port)

            # 4. Build diagnostic context and perform checks
            port_status_str = "OPEN" if port_status_bool else "CLOSED"
            diagnostic_context = f"P:{transmission_port}({port_status_str}) VPN:{gluetun_vpn_status} | DL/UL:{dl_speed}/{ul_speed} | Act:{active_count}"

            if port_check_error:
                raise HealthcheckError(
                    port_check_error['error'],
                    diag_context=diagnostic_context,
                    portcheck_error=port_check_error
                )
            
            if gluetun_port != transmission_port:
                raise HealthcheckError("E:MISMATCH", diag_context=diagnostic_context, mismatch_ports={'gluetun': gluetun_port, 'transmission': transmission_port})

            if not port_status_bool:
                raise HealthcheckError("E:CLOSED", diagnostic_context)

            if system_error_count > 0:
                raise HealthcheckError("E:SYS_ERR", diag_context=diagnostic_context, torrent=system_error_torrents[0])

            # 5. Success
            logging.info(f"OK | {diagnostic_context}")
            ping_healthchecks("success", f"OK | {diagnostic_context}")

        except HealthcheckError as e:
            logging.error(e.full_message)
            ping_healthchecks("fail", e.hc_message)
            # Reset session on failure in case of persistent connection/session issues
            tr_session = requests.Session()
        except Exception as e:
            error_message = str(e)
            logging.error(error_message)
            # For unexpected errors, perform a simple truncation for the healthcheck ping
            ping_healthchecks("fail", error_message[:100])
            # Reset session on failure in case of persistent connection/session issues
            tr_session = requests.Session()

        finally:
            logging.info(f"Check finished. Waiting {CHECK_INTERVAL_SECONDS} seconds...")
            time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
