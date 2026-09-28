"""Replay a lost VPN forward against local fake HTTP services."""

import base64
import http.server
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "torrenter_http_monitor_test", ROOT / "roles/torrenter/files/transmission-healthcheck/check.py"
)
MONITOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MONITOR
SPEC.loader.exec_module(MONITOR)


class Backend:
    vpn = "running"
    forwarded = 0
    peer = 45000
    actions = None
    fail_stop = False

    def __init__(self):
        self.actions = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body, headers=None):
        body = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, username, password):
        expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        return self.headers.get("Authorization") == expected

    def do_GET(self):
        state = self.server.backend
        if self.path.startswith("/v1/"):
            if not self.authorized("gl-user", "gl-pass"):
                return self.reply(401, {})
            if self.path == "/v1/vpn/status":
                return self.reply(200, {"status": state.vpn})
            if self.path == "/v1/portforward":
                return self.reply(200, {"port": state.forwarded, "ports": []})
        if self.path == "/transmission/rpc":
            if not self.authorized("tr-user", "tr-pass"):
                return self.reply(401, {})
            return self.reply(409, {}, {"X-Transmission-Session-Id": "dummy-token"})
        if self.path.startswith("/check/"):
            return self.reply(200, "1" if state.forwarded == state.peer else "0")
        self.reply(404, {})

    def do_PUT(self):
        state = self.server.backend
        if self.path != "/v1/vpn/status" or not self.authorized("gl-user", "gl-pass"):
            return self.reply(401, {})
        desired = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        state.actions.append(("vpn", desired["status"]))
        if state.fail_stop and desired["status"] == "stopped":
            return self.reply(500, {})
        state.vpn = desired["status"]
        if state.vpn == "running":
            state.forwarded = 47000
        self.reply(200, {"outcome": "success"})

    def do_POST(self):
        state = self.server.backend
        if self.path != "/transmission/rpc" or not self.authorized("tr-user", "tr-pass"):
            return self.reply(401, {})
        if self.headers.get("X-Transmission-Session-Id") != "dummy-token":
            return self.reply(409, {}, {"X-Transmission-Session-Id": "dummy-token"})
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = payload["method"]
        if method == "session-get":
            result = {"peer-port": state.peer}
        elif method == "session-set":
            state.peer = payload["arguments"]["peer-port"]
            state.actions.append(("peer", state.peer))
            result = {}
        elif method == "session-stats":
            result = {"activeTorrentCount": 1}
        elif method == "torrent-get":
            result = {"torrents": []}
        else:
            return self.reply(400, {})
        self.reply(200, {"result": "success", "arguments": result})


class HttpFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = Backend()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.backend = self.backend
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        port = self.server.server_port
        self.old_url = MONITOR.PORT_CHECK_URL
        MONITOR.PORT_CHECK_URL = f"http://127.0.0.1:{port}/check"
        self.addCleanup(setattr, MONITOR, "PORT_CHECK_URL", self.old_url)
        config = MONITOR.Config("127.0.0.1", port, "tr-user", "tr-pass", "127.0.0.1", port,
                                "gl-user", "gl-pass", "", Path(self.temp.name), grace=0)
        self.monitor = MONITOR.Monitor(config)

    def test_lost_forward_reconnects_then_verifies_new_port(self):
        with self.assertLogs(MONITOR.LOG, level="INFO") as captured:
            self.assertEqual([self.monitor.check() for _ in range(3)], ["NO_PORT"] * 3)
            self.assertEqual(self.backend.actions, [("vpn", "stopped"), ("vpn", "running")])
            self.assertEqual(self.monitor.check(), "HEALTHY")
        events = "\n".join(captured.output)
        self.assertIn("NO_PORT ZERO_COUNT_1", events)
        self.assertIn("VPN_CYCLE_START", events)
        self.assertIn("VPN_CYCLE_COMPLETE awaiting_forward_verification", events)
        self.assertIn("PORT_REPAIR_START old=45000 new=47000", events)
        self.assertIn("PORT_REPAIR_VERIFIED old=45000 new=47000", events)
        self.assertIn("VERIFIED_HEALTHY port=47000 peer=47000 external=open", events)
        self.assertEqual(self.backend.actions[-1], ("peer", 47000))
        record = json.loads((Path(self.temp.name) / "monitor.json").read_text())
        self.assertEqual((record["network_status"], record["external"], record["transmission_port"]),
                         ("HEALTHY", "open", 47000))

    def test_failed_stop_is_reported_and_cooled_down(self):
        self.backend.fail_stop = True
        with self.assertLogs(MONITOR.LOG, level="INFO") as captured:
            self.assertEqual([self.monitor.check() for _ in range(3)],
                             ["NO_PORT", "NO_PORT", "RECOVERY_FAILED"])
        self.assertIn("VPN_CYCLE_START", "\n".join(captured.output))
        self.assertIn("RECOVERY_FAILED VPN transition rejected", "\n".join(captured.output))
        self.assertEqual(self.backend.actions, [("vpn", "stopped")])
        record = json.loads((Path(self.temp.name) / "monitor.json").read_text())
        self.assertEqual(record["reason"], "VPN transition rejected")
        self.assertEqual([self.monitor.check() for _ in range(3)], ["NO_PORT"] * 3)
        self.assertEqual(self.backend.actions, [("vpn", "stopped")])


if __name__ == "__main__":
    unittest.main()
