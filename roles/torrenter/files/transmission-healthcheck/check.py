"""Observe Transmission's VPN port and perform bounded, verified repairs."""

import json
import logging
import os
import hashlib
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import requests


LOG = logging.getLogger("transmission-healthcheck")
PORT_CHECK_URL = "https://portcheck.transmissionbt.com"


class Fault(Exception):
    def __init__(self, code, detail="", recoverable=False):
        self.code = code
        self.detail = detail
        self.recoverable = recoverable
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Config:
    tr_host: str
    tr_port: int
    tr_user: str
    tr_pass: str
    gluetun_host: str
    gluetun_port: int
    gluetun_user: str
    gluetun_pass: str
    hc_url: str
    state_dir: Path
    interval: int = 300
    zero_threshold: int = 3
    closed_threshold: int = 3
    grace: int = 180
    recovery_timeout: int = 180
    cooldown: int = 1800
    recovery_enabled: bool = True

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env

        def positive(name, default):
            value = int(env.get(name, default))
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            return value

        enabled = env.get("RECOVERY_ENABLED", "true").lower()
        if enabled not in ("true", "false"):
            raise ValueError("RECOVERY_ENABLED must be true or false")
        config = cls(
            tr_host=env.get("TR_HOST", "localhost"),
            tr_port=positive("TR_PORT", 9091),
            tr_user=env.get("TR_USER", ""),
            tr_pass=env.get("TR_PASS", ""),
            gluetun_host=env.get("GLUETUN_HOST", "localhost"),
            gluetun_port=positive("GLUETUN_PORT", 8000),
            gluetun_user=env.get("GLUETUN_USER", ""),
            gluetun_pass=env.get("GLUETUN_PASS", ""),
            hc_url=env.get("HC_URL", ""),
            state_dir=Path(env.get("RECOVERY_STATE_DIR", "/state")),
            interval=positive("CHECK_INTERVAL_SECONDS", 300),
            zero_threshold=positive("PORT_ZERO_RESTART_THRESHOLD", 3),
            closed_threshold=positive("CLOSED_PORT_RESTART_THRESHOLD", 3),
            grace=int(env.get("RECOVERY_GRACE_SECONDS", 180)),
            recovery_timeout=positive("VPN_RECOVERY_TIMEOUT_SECONDS", 180),
            cooldown=positive("VPN_RECOVERY_COOLDOWN_SECONDS", 1800),
            recovery_enabled=enabled == "true",
        )
        if config.grace < 0 or config.recovery_timeout < 20:
            raise ValueError("invalid recovery grace or timeout")
        if not all((config.tr_user, config.tr_pass, config.gluetun_user, config.gluetun_pass)):
            raise ValueError("Transmission and Gluetun credentials are required")
        return config


def valid_port(value, allow_zero=False):
    return type(value) is int and (0 if allow_zero else 1) <= value <= 65535


def atomic_json(path, value):
    """Replace a state file without exposing a partially written JSON document."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Monitor:
    def __init__(self, config, http=None, clock=None, monotonic=None, sleep=None):
        self.config = config
        self.http = http if http is not None else requests.Session()
        self.tr_http = requests.Session()
        self.clock = clock if clock is not None else time.time
        self.monotonic = monotonic if monotonic is not None else time.monotonic
        self.sleep = sleep if sleep is not None else time.sleep
        self.started_at = self.monotonic()
        self.grace_until = self.started_at + config.grace
        self.last_start_attempt = float("-inf")
        self.zero_count = 0
        self.closed_count = 0
        self.closed_port = None
        self.gluetun_port = None
        self.transmission_port = None
        self.external = "not_checked"
        self.last_cycle_at = None
        self.last_status = None

    @property
    def vpn_url(self):
        return f"http://{self.config.gluetun_host}:{self.config.gluetun_port}"

    @property
    def tr_url(self):
        return f"http://{self.config.tr_host}:{self.config.tr_port}/transmission/rpc"

    def publish(self, phase, status, reason="", recoverable=False):
        record = {
            "schema": 1,
            "updated_at": self.clock(),
            "phase": phase,
            "network_status": status,
            "reason": reason[:100],
            "recoverable": recoverable,
            "gluetun_port": self.gluetun_port,
            "transmission_port": self.transmission_port,
            "external": self.external,
            "last_vpn_cycle_at": self.last_cycle_at,
        }
        try:
            atomic_json(self.config.state_dir / "monitor.json", record)
        except OSError as error:
            LOG.error("Could not publish monitor state: %s", type(error).__name__)

    def notify(self, event, message=""):
        if not self.config.hc_url:
            return
        url = self.config.hc_url
        if event in ("start", "fail"):
            url += "/" + event
        try:
            response = self.http.post(url, data=message[:100].encode(), timeout=(3, 5))
            response.raise_for_status()
        except requests.RequestException as error:
            # Request exceptions can contain the secret Healthchecks URL.
            LOG.warning("Healthchecks delivery failed: %s", type(error).__name__)

    def _api(self, path):
        try:
            response = self.http.get(
                self.vpn_url + path,
                auth=(self.config.gluetun_user, self.config.gluetun_pass),
                timeout=(3, 5),
            )
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as error:
            if error.response is not None and error.response.status_code in (401, 403):
                raise Fault("CONFIG_ERROR", "Gluetun API denied credentials") from error
            raise Fault("API_UNREACHABLE", "Gluetun API returned an error", True) from error
        except (requests.RequestException, ValueError, TypeError) as error:
            raise Fault("API_UNREACHABLE", "Gluetun API did not respond correctly", True) from error

    def vpn_status(self):
        data = self._api("/v1/vpn/status")
        status = data.get("status") if isinstance(data, dict) else None
        if status not in ("running", "stopped", "starting", "stopping"):
            raise Fault("CONFIG_ERROR", "Invalid Gluetun VPN status")
        return status

    def forward(self):
        data = self._api("/v1/portforward")
        port = data.get("port") if isinstance(data, dict) else None
        if not valid_port(port, allow_zero=True):
            raise Fault("CONFIG_ERROR", "Invalid Gluetun forwarded port")
        return port

    def _rpc(self, method, arguments=None):
        auth = (self.config.tr_user, self.config.tr_pass)
        payload = {"method": method, "arguments": arguments or {}}
        try:
            if "X-Transmission-Session-Id" not in self.tr_http.headers:
                response = self.tr_http.get(self.tr_url, auth=auth, timeout=(3, 5))
                if response.status_code == 401:
                    raise Fault("CONFIG_ERROR", "Transmission denied credentials")
                token = response.headers.get("X-Transmission-Session-Id")
                if not token:
                    raise Fault("TR_UNREACHABLE", "Transmission session token missing", True)
                self.tr_http.headers["X-Transmission-Session-Id"] = token
            for attempt in range(2):
                response = self.tr_http.post(self.tr_url, json=payload, auth=auth, timeout=(3, 20))
                if response.status_code == 409 and attempt == 0:
                    token = response.headers.get("X-Transmission-Session-Id")
                    if not token:
                        raise Fault("TR_UNREACHABLE", "Transmission session token missing", True)
                    self.tr_http.headers["X-Transmission-Session-Id"] = token
                    continue
                if response.status_code in (401, 403):
                    raise Fault("CONFIG_ERROR", "Transmission denied credentials")
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict) or body.get("result") != "success":
                    raise Fault("TR_UNREACHABLE", "Transmission RPC rejected request", True)
                return body.get("arguments", {})
            raise Fault("TR_UNREACHABLE", "Transmission session token expired", True)
        except Fault:
            raise
        except (requests.RequestException, ValueError, TypeError) as error:
            self.tr_http.headers.pop("X-Transmission-Session-Id", None)
            raise Fault("TR_UNREACHABLE", "Transmission RPC unavailable", True) from error

    def _port(self):
        value = self._rpc("session-get", {"fields": ["peer-port"]}).get("peer-port")
        if not valid_port(value):
            raise Fault("CONFIG_ERROR", "Invalid Transmission peer port")
        return value

    def _external(self, port):
        for attempt in range(3):
            try:
                response = self.http.get(f"{PORT_CHECK_URL}/{port}", timeout=(5, 15))
                response.raise_for_status()
                result = response.text.strip()
                if result == "1":
                    return "open"
                if result == "0":
                    return "closed"
                return "unknown"
            except requests.RequestException:
                if attempt < 2:
                    self.sleep(5)
        return "unknown"

    def _cooldown_record(self):
        path = self.config.state_dir / "vpn-recovery.json"
        try:
            with path.open(encoding="utf-8") as input_file:
                record = json.load(input_file)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise Fault("STATE_ERROR", "VPN cooldown state unreadable") from error
        timestamp = record.get("last_vpn_cycle_at") if isinstance(record, dict) else None
        if type(timestamp) not in (float, int) or timestamp < 0:
            raise Fault("STATE_ERROR", "VPN cooldown state invalid")
        return timestamp

    def _cycle_allowed(self):
        previous = self._cooldown_record()
        if previous is None:
            return True
        self.last_cycle_at = previous
        now = self.clock()
        if previous > now:
            # A backward clock correction must not disable recovery indefinitely.
            self._record_cycle(now)
            return False
        elapsed = now - previous
        return elapsed >= self.config.cooldown

    def _record_cycle(self, timestamp=None):
        timestamp = self.clock() if timestamp is None else timestamp
        try:
            atomic_json(self.config.state_dir / "vpn-recovery.json", {
                "last_vpn_cycle_at": timestamp
            })
        except OSError as error:
            raise Fault("STATE_ERROR", "Cannot save VPN recovery cooldown") from error
        self.last_cycle_at = timestamp

    def _put_vpn(self, desired, deadline):
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise Fault("RECOVERY_FAILED", "VPN transition timed out", True)
        try:
            response = self.http.put(
                self.vpn_url + "/v1/vpn/status",
                json={"status": desired},
                auth=(self.config.gluetun_user, self.config.gluetun_pass),
                timeout=(min(3, remaining), min(10, remaining)),
            )
            response.raise_for_status()
            outcome = response.json()
            if not isinstance(outcome, dict) or not isinstance(outcome.get("outcome"), str):
                raise Fault("RECOVERY_FAILED", "VPN transition reply invalid", True)
        except requests.HTTPError as error:
            if error.response is not None and error.response.status_code in (401, 403):
                raise Fault("CONFIG_ERROR", "Gluetun API denied recovery request") from error
            raise Fault("RECOVERY_FAILED", "VPN transition rejected", True) from error
        except (requests.RequestException, ValueError, TypeError) as error:
            raise Fault("RECOVERY_FAILED", "VPN transition request failed", True) from error

    def _wait_vpn(self, desired, deadline):
        while self.monotonic() < deadline:
            if self.vpn_status() == desired:
                return
            self.sleep(min(3, max(0, deadline - self.monotonic())))
        raise Fault("RECOVERY_FAILED", f"VPN did not reach {desired}", True)

    def _ensure_running(self, deadline, start_only=False):
        status = self.vpn_status()
        if status == "running":
            return False
        if status == "stopping":
            self._wait_vpn("stopped", deadline)
            status = "stopped"
        if status == "stopped":
            if start_only and self.monotonic() - self.last_start_attempt < 60:
                return False
            self.last_start_attempt = self.monotonic()
            if start_only:
                LOG.warning("VPN_START_ATTEMPT status=stopped")
            self._put_vpn("running", deadline)
        self._wait_vpn("running", deadline)
        return True

    def _cycle_vpn(self):
        if not self._cycle_allowed():
            LOG.info("VPN_CYCLE_COOLDOWN last_at=%.0f", self.last_cycle_at)
            return "VPN_CYCLE_COOLDOWN"
        self._record_cycle()  # A crash after this point still enforces cooldown.
        deadline = self.monotonic() + self.config.recovery_timeout
        stop_deadline = min(deadline - 60, self.monotonic() + 60)
        self.publish("recovering", "RECOVERY_PENDING", "VPN_CYCLE", True)
        LOG.warning("VPN_CYCLE_START port=%s last_at=%.0f", self.gluetun_port, self.last_cycle_at)
        stop_attempted = False
        primary_error = None
        try:
            stop_attempted = True
            self._put_vpn("stopped", stop_deadline)
            self._wait_vpn("stopped", stop_deadline)
        except Fault as error:
            primary_error = error
        finally:
            if stop_attempted:
                try:
                    self._ensure_running(deadline)
                except Fault as error:
                    primary_error = error
        if primary_error:
            raise primary_error
        self.grace_until = self.monotonic() + self.config.grace
        LOG.warning("VPN_CYCLE_COMPLETE awaiting_forward_verification")
        return "VPN_CYCLE_ATTEMPTED"

    def _reconcile_port(self, port):
        original = self.transmission_port
        LOG.warning("PORT_REPAIR_START old=%s new=%s", original, port)
        for _ in range(2):
            current = self.forward()
            if current != port or current == 0:
                raise Fault("MISMATCH", "Gluetun port changed during update", True)
            self._rpc("session-set", {"peer-port": port})
            self.transmission_port = self._port()
            if self.transmission_port == port:
                LOG.info("PORT_REPAIR_VERIFIED old=%s new=%s", original, port)
                return
        raise Fault("MISMATCH", "Transmission did not adopt forwarded port", True)

    def check(self):
        self.gluetun_port = self.transmission_port = None
        self.external = "not_checked"
        self.publish("checking", "PENDING")
        self.notify("start")
        status = "PENDING"
        reason = ""
        recoverable = False
        try:
            status, reason, recoverable = self._check_network()
        except Fault as error:
            status, reason, recoverable = error.code, error.detail, error.recoverable
            self.zero_count = self.closed_count = 0
        except Exception as error:
            status, reason, recoverable = "MONITOR_ERROR", type(error).__name__, True
            self.zero_count = self.closed_count = 0
            LOG.exception("Unexpected monitor failure")
        self.publish("complete", status, reason, recoverable)
        summary = f"{status} {reason}".strip()
        if status == "HEALTHY":
            if self.last_status != "HEALTHY":
                LOG.info("VERIFIED_HEALTHY port=%s peer=%s external=%s %s",
                         self.gluetun_port, self.transmission_port, self.external, reason)
            self.notify("success", summary)
        else:
            LOG.error(summary)
            self.notify("fail", summary)
        self.last_status = status
        return status

    def _check_network(self):
        c = self.config
        self.last_cycle_at = self._cooldown_record()
        vpn = self.vpn_status()
        if vpn != "running":
            self.zero_count = self.closed_count = 0
            if vpn == "stopped" and c.recovery_enabled:
                self.publish("recovering", "RECOVERY_PENDING", "VPN_START", True)
                started = self._ensure_running(self.monotonic() + c.recovery_timeout, start_only=True)
                if started:
                    self.grace_until = self.monotonic() + c.grace
                return "VPN_DOWN", "VPN_START_ATTEMPTED" if started else "VPN_START_WAIT", True
            return "VPN_DOWN", f"VPN_{vpn.upper()}", True
        port = self.forward()
        self.gluetun_port = port
        in_grace = self.monotonic() < self.grace_until
        if port == 0:
            self.closed_count = 0
            if not in_grace:
                self.zero_count += 1
            if c.recovery_enabled and self.zero_count >= c.zero_threshold:
                self.zero_count = 0
                return "NO_PORT", self._cycle_vpn(), True
            return "NO_PORT", f"ZERO_COUNT_{self.zero_count}", True

        self.zero_count = 0
        self.transmission_port = self._port()
        changed = False
        if self.transmission_port != port:
            self.closed_count = 0
            if not c.recovery_enabled:
                return "MISMATCH", "RECOVERY_DISABLED", True
            self._reconcile_port(port)
            changed = True
        self.external = self._external(port)
        if self.external == "unknown":
            self.closed_count = 0
            return "PORTCHECK_UNKNOWN", "External checker unavailable", False
        if self.external == "closed":
            if self.closed_port != port:
                self.closed_count = 0
            self.closed_port = port
            if not changed and not in_grace:
                self.closed_count += 1
            if c.recovery_enabled and self.closed_count >= c.closed_threshold:
                self.closed_count = 0
                return "CLOSED", self._cycle_vpn(), True
            return "CLOSED", f"CLOSED_COUNT_{self.closed_count}", True
        self.closed_count = 0
        stats = self._rpc("session-stats")
        torrents = self._rpc("torrent-get", {"fields": ["id", "name", "error", "errorString"]})
        errors = [item for item in torrents.get("torrents", []) if item.get("error") == 3]
        if errors:
            return "SYS_ERR", f"TORRENT_ID_{errors[0].get('id')}", False
        return "HEALTHY", f"PORT_{port} ACTIVE_{stats.get('activeTorrentCount', 0)}", False

    def run_forever(self):
        while True:
            self.check()
            self.sleep(self.config.interval)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
    except (ValueError, TypeError) as error:
        LOG.error("Invalid monitor configuration: %s", error)
        raise SystemExit(1) from error
    LOG.info("MONITOR_START code_sha256=%s interval=%s recovery_enabled=%s",
             hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             config.interval, config.recovery_enabled)
    Monitor(config).run_forever()


if __name__ == "__main__":
    main()
