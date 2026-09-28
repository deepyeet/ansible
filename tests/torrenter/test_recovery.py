"""Behavioral tests for the two layers of torrent recovery."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    import sys
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


monitor_module = load("torrenter_monitor_test", "roles/torrenter/files/transmission-healthcheck/check.py")
host_module = load("torrenter_host_test", "roles/torrenter/files/recovery/recover.py")


class Clock:
    def __init__(self):
        self.now = 10_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        config = monitor_module.Config(
            "localhost", 9091, "tr-user", "tr-pass", "localhost", 8000,
            "gl-user", "gl-pass", "", Path(self.temp.name),
            zero_threshold=3, closed_threshold=3, grace=0, cooldown=1800,
        )
        self.monitor = monitor_module.Monitor(
            config, clock=self.clock, monotonic=self.clock, sleep=self.clock.advance,
        )
        self.vpn = "running"
        self.forwarded = 0
        self.peer = 45000
        self.external = "open"
        self.transitions = []
        self.monitor.vpn_status = lambda: self.vpn
        self.monitor.forward = lambda: self.forwarded
        self.monitor._port = lambda: self.peer
        self.monitor._external = lambda _: self.external
        self.monitor._rpc = lambda method, args=None: (
            {"activeTorrentCount": 1} if method == "session-stats" else {"torrents": []}
        )

        def put_vpn(desired, deadline):
            self.transitions.append(desired)
            self.vpn = desired

        self.monitor._put_vpn = put_vpn

    def test_zero_port_cycles_after_threshold_and_observes_cooldown(self):
        self.assertEqual([self.monitor.check() for _ in range(2)], ["NO_PORT", "NO_PORT"])
        self.assertEqual(self.transitions, [])
        self.assertEqual(self.monitor.check(), "NO_PORT")
        self.assertEqual(self.transitions, ["stopped", "running"])
        self.assertEqual(json.loads((Path(self.temp.name) / "monitor.json").read_text())["reason"],
                         "VPN_CYCLE_ATTEMPTED")
        self.assertEqual(json.loads((Path(self.temp.name) / "monitor.json").read_text())["last_vpn_cycle_at"],
                         self.clock.now)
        self.assertEqual([self.monitor.check() for _ in range(3)], ["NO_PORT"] * 3)
        self.assertEqual(self.transitions, ["stopped", "running"])
        self.assertEqual(json.loads((Path(self.temp.name) / "monitor.json").read_text())["reason"],
                         "VPN_CYCLE_COOLDOWN")

    def test_forward_returns_and_mismatch_is_reconciled(self):
        self.assertEqual(self.monitor.check(), "NO_PORT")
        self.forwarded = 46000
        self.monitor._rpc = lambda method, args=None: (
            setattr(self, "peer", args["peer-port"]) or {}
            if method == "session-set" else
            {"activeTorrentCount": 1} if method == "session-stats" else {"torrents": []}
        )
        with self.assertLogs(monitor_module.LOG, level="INFO") as captured:
            self.assertEqual(self.monitor.check(), "HEALTHY")
        self.assertIn("PORT_REPAIR_START old=45000 new=46000", "\n".join(captured.output))
        self.assertIn("PORT_REPAIR_VERIFIED old=45000 new=46000", "\n".join(captured.output))
        self.assertEqual(self.peer, self.forwarded)
        self.assertEqual(self.monitor.zero_count, 0)

    def test_closed_port_cycles_but_unknown_checker_does_not(self):
        self.forwarded = self.peer
        self.external = "unknown"
        self.assertEqual([self.monitor.check() for _ in range(3)], ["PORTCHECK_UNKNOWN"] * 3)
        self.assertEqual(self.transitions, [])
        self.external = "closed"
        self.assertEqual([self.monitor.check() for _ in range(3)], ["CLOSED"] * 3)
        self.assertEqual(self.transitions, ["stopped", "running"])

    def test_future_cooldown_is_clamped_and_cannot_permit_immediate_cycle(self):
        monitor_module.atomic_json(Path(self.temp.name) / "vpn-recovery.json",
                                   {"last_vpn_cycle_at": self.clock.now + 7200})
        self.assertFalse(self.monitor._cycle_allowed())
        self.assertEqual(self.monitor.last_cycle_at, self.clock.now)
        self.clock.advance(1800)
        self.assertTrue(self.monitor._cycle_allowed())

    def test_corrupt_cooldown_state_fails_closed_without_vpn_mutation(self):
        (Path(self.temp.name) / "vpn-recovery.json").write_text("not JSON")
        self.assertEqual(self.monitor.check(), "STATE_ERROR")
        self.assertEqual(self.transitions, [])

    def test_existing_cooldown_is_visible_after_monitor_restart(self):
        monitor_module.atomic_json(Path(self.temp.name) / "vpn-recovery.json",
                                   {"last_vpn_cycle_at": self.clock.now})
        self.assertEqual(self.monitor.check(), "NO_PORT")
        record = json.loads((Path(self.temp.name) / "monitor.json").read_text())
        self.assertEqual(record["last_vpn_cycle_at"], self.clock.now)

    def test_invalid_port_and_config_error_are_reported_without_cycle(self):
        self.forwarded = "not a port"
        self.monitor.forward = lambda: monitor_module.Monitor.forward(self.monitor)
        self.monitor._api = lambda _: {"port": self.forwarded}
        self.assertEqual(self.monitor.check(), "CONFIG_ERROR")
        self.assertEqual(self.transitions, [])

    def test_stopped_vpn_starts_without_forward_endpoint_and_is_rate_limited(self):
        self.vpn = "stopped"
        self.monitor.forward = lambda: self.fail("Port endpoint must not gate VPN startup")
        self.assertEqual(self.monitor.check(), "VPN_DOWN")
        self.assertEqual(self.transitions, ["running"])
        self.vpn = "stopped"
        self.assertEqual(self.monitor.check(), "VPN_DOWN")
        self.assertEqual(self.transitions, ["running"])
        self.assertEqual(json.loads((Path(self.temp.name) / "monitor.json").read_text())["reason"],
                         "VPN_START_WAIT")


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        root = Path(self.temp.name)
        self.monitor_path = root / "monitor.json"
        self.host = host_module.Recovery(root, root, self.monitor_path, root,
                                         failure=1800, cooldown=3600,
                                         clock=self.clock, monotonic=self.clock,
                                         ismount=lambda _: True)
        self.host.command = lambda *args, **kwargs: "ok"
        self.host.container_state = lambda _: {"Running": True, "Health": {"Status": "healthy"}}
        self.actions = []
        self.host.compose = lambda *args, **kwargs: self.actions.append(args)

        def recover(before_disruption=None):
            if before_disruption is not None and not before_disruption():
                return False
            self.actions.append(("recover",))
            return True

        self.host.recover = recover

    def publish(self, status, recoverable, phase="complete"):
        host_module.atomic_json(self.monitor_path, {
            "schema": 1, "updated_at": self.clock.now, "phase": phase,
            "network_status": status, "recoverable": recoverable,
        })

    def test_persistent_fault_triggers_once_then_cooldown(self):
        self.publish("NO_PORT", True)
        self.assertEqual(self.host.check(), "waiting")
        self.clock.advance(1800)
        self.publish("NO_PORT", True)
        self.assertEqual(self.host.check(), "attempted")
        self.assertEqual(self.actions, [("config", "--quiet"), ("recover",)])
        self.assertEqual(self.host.check(), "cooldown")

    def test_pending_monitor_recovery_delays_host_action(self):
        self.publish("NO_PORT", True)
        self.assertEqual(self.host.check(), "waiting")
        self.clock.advance(1900)
        self.publish("RECOVERY_PENDING", True, "recovering")
        self.assertEqual(self.host.check(), "pending")
        self.assertEqual(self.actions, [])

    def test_fresh_config_error_does_not_recreate_unhealthy_gluetun(self):
        self.host.container_state = lambda name: {
            "Running": True,
            "Health": {"Status": "unhealthy" if name == "gluetun" else "healthy"},
        }
        self.publish("CONFIG_ERROR", False)
        self.assertEqual(self.host.check(), "report_only")
        self.assertEqual(self.actions, [])

    def test_missing_mount_prevents_all_docker_actions(self):
        self.host.ismount = lambda _: False
        with self.assertRaisesRegex(host_module.RecoveryError, "mount"):
            self.host.check()
        self.assertEqual(self.actions, [])

    def test_stale_monitor_state_needs_persistent_failure(self):
        self.assertEqual(self.host.check(), "waiting")
        self.clock.advance(1800)
        self.assertEqual(self.host.check(), "attempted")
        self.assertEqual(self.actions[-1], ("recover",))

    def test_recovered_during_compose_preflight_cancels_stack_recovery(self):
        self.publish("NO_PORT", True)
        self.assertEqual(self.host.check(), "waiting")
        self.clock.advance(1800)
        self.publish("NO_PORT", True)

        def compose(*args, **kwargs):
            self.actions.append(args)
            if args == ("config", "--quiet"):
                self.publish("HEALTHY", False)

        self.host.compose = compose
        self.assertEqual(self.host.check(), "canceled")
        self.assertEqual(self.actions, [("config", "--quiet")])
        self.assertIsNone(self.host.load_state()["failure_since"])
        self.assertIsNone(self.host.load_state()["last_stack_recovery_at"])

    def test_recovered_after_monitor_stop_restores_only_monitor(self):
        self.publish("NO_PORT", True)
        self.assertEqual(self.host.check(), "waiting")
        self.clock.advance(1800)
        self.publish("NO_PORT", True)
        self.host.recover = lambda before_disruption=None: host_module.Recovery.recover(
            self.host, before_disruption
        )

        def compose(*args, **kwargs):
            self.actions.append(args)
            if args == ("stop", "transmission-healthcheck"):
                self.publish("HEALTHY", False)

        self.host.compose = compose
        self.assertEqual(self.host.check(), "canceled")
        self.assertEqual(self.actions, [
            ("config", "--quiet"),
            ("stop", "transmission-healthcheck"),
            ("up", "-d", "--no-deps", "--no-build", "--pull", "never", "transmission-healthcheck"),
        ])
        self.assertIsNone(self.host.load_state()["last_stack_recovery_at"])

    def test_report_only_error_after_preflight_cancels_recovery(self):
        self.publish("NO_PORT", True)
        self.assertEqual(self.host.check(), "waiting")
        self.clock.advance(1800)
        self.publish("NO_PORT", True)

        def compose(*args, **kwargs):
            self.actions.append(args)
            if args == ("config", "--quiet"):
                self.publish("CONFIG_ERROR", False)

        self.host.compose = compose
        self.assertEqual(self.host.check(), "canceled")
        self.assertEqual(self.actions, [("config", "--quiet")])

    def test_lost_data_mount_after_monitor_stop_does_not_restart_data_services(self):
        self.publish("NO_PORT", True)
        self.assertEqual(self.host.check(), "waiting")
        self.clock.advance(1800)
        self.publish("NO_PORT", True)
        mounted = [True]
        self.host.ismount = lambda _: mounted[0]
        self.host.recover = lambda before_disruption=None: host_module.Recovery.recover(
            self.host, before_disruption
        )

        def compose(*args, **kwargs):
            self.actions.append(args)
            if args == ("stop", "transmission-healthcheck"):
                mounted[0] = False

        self.host.compose = compose
        self.assertEqual(self.host.check(), "canceled")
        self.assertEqual(self.actions, [
            ("config", "--quiet"),
            ("stop", "transmission-healthcheck"),
            ("up", "-d", "--no-deps", "--no-build", "--pull", "never", "transmission-healthcheck"),
        ])
        self.assertIsNone(self.host.load_state()["last_stack_recovery_at"])

    def test_stack_recovery_uses_ordered_allowlist(self):
        self.host.verify_namespaces = lambda: self.actions.append(("verify",))
        host_module.Recovery.recover(self.host)
        self.assertEqual(self.actions, [
            ("stop", "transmission-healthcheck"),
            ("stop", "mam-updater", "ptp-archiver", "transmission", "gluetun"),
            ("up", "-d", "--no-deps", "--force-recreate", "--no-build", "--pull", "never", "gluetun"),
            ("up", "-d", "--no-deps", "--force-recreate", "--no-build", "--pull", "never",
             "transmission", "transmission-healthcheck", "mam-updater", "ptp-archiver"),
            ("verify",),
        ])

    def test_failed_recreate_attempts_to_restore_all_services(self):
        def compose(*args, **kwargs):
            self.actions.append(args)
            if "--force-recreate" in args and "gluetun" in args:
                raise host_module.RecoveryError("fake Docker failure")

        self.host.compose = compose
        with self.assertRaisesRegex(host_module.RecoveryError, "fake Docker failure"):
            host_module.Recovery.recover(self.host)
        self.assertEqual(self.actions[-2:], [
            ("up", "-d", "--no-deps", "--no-build", "--pull", "never", "gluetun"),
            ("up", "-d", "--no-deps", "--no-build", "--pull", "never",
             "transmission", "transmission-healthcheck", "mam-updater", "ptp-archiver"),
        ])

    def test_docker_failure_keeps_stage_and_exit_code_without_output(self):
        import subprocess

        def runner(argv, **_):
            return subprocess.CompletedProcess(argv, 17, "", "SECRET_SHOULD_NOT_APPEAR")

        host = host_module.Recovery(self.temp.name, self.temp.name, self.monitor_path,
                                    self.temp.name, runner=runner)
        with self.assertRaises(host_module.RecoveryError) as caught:
            host.compose("config", "--quiet")
        self.assertIn("rc=17", str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
