#!/usr/bin/env python3
"""Last-resort recovery for the Transmission services sharing Gluetun's network."""

import argparse
import fcntl
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path


LOG = logging.getLogger("torrenter-recovery")
REQUIRED = ("gluetun", "transmission", "transmission-healthcheck")
CONSUMERS = ("transmission", "transmission-healthcheck", "mam-updater", "ptp-archiver")
RECOVERABLE = {
    "NO_PORT", "CLOSED", "MISMATCH", "VPN_DOWN", "API_UNREACHABLE",
    "TR_UNREACHABLE", "RECOVERY_FAILED", "MONITOR_ERROR",
}


class RecoveryError(Exception):
    pass


def atomic_json(path, value):
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


class Recovery:
    def __init__(self, project, data_mount, monitor_state, state_dir,
                 stale=900, failure=1800, cooldown=3600,
                 runner=None, clock=None, monotonic=None, ismount=None):
        self.project = Path(project)
        self.data_mount = Path(data_mount)
        self.monitor_state = Path(monitor_state)
        self.state_path = Path(state_dir) / "host-recovery.json"
        self.stale = stale
        self.failure = failure
        self.cooldown = cooldown
        self.runner = runner if runner is not None else subprocess.run
        self.clock = clock if clock is not None else time.time
        self.monotonic = monotonic if monotonic is not None else time.monotonic
        self.ismount = ismount if ismount is not None else os.path.ismount

    def command(self, argv, timeout=15):
        try:
            result = self.runner(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except (subprocess.TimeoutExpired, OSError) as error:
            raise RecoveryError(f"Command unavailable: {argv[0]} {argv[1] if len(argv) > 1 else ''}") from error
        if result.returncode != 0:
            # Docker/Compose output may contain environment or secret values.
            raise RecoveryError(
                f"Command failed: {argv[0]} {argv[1] if len(argv) > 1 else ''} rc={result.returncode}"
            )
        return result.stdout

    def compose(self, *args, timeout=15):
        return self.command(["docker", "compose", "-f", str(self.project / "compose.yaml"), *args], timeout)

    def load_state(self):
        try:
            with self.state_path.open(encoding="utf-8") as input_file:
                state = json.load(input_file)
        except FileNotFoundError:
            return {"failure_since": None, "last_stack_recovery_at": None}
        except (OSError, ValueError) as error:
            raise RecoveryError("Host recovery state unreadable") from error
        if not isinstance(state, dict):
            raise RecoveryError("Host recovery state malformed")
        for key in ("failure_since", "last_stack_recovery_at"):
            value = state.get(key)
            if value is not None and (type(value) not in (int, float) or value < 0):
                raise RecoveryError("Host recovery state malformed")
        return {key: state.get(key) for key in ("failure_since", "last_stack_recovery_at")}

    def save_state(self, state):
        try:
            atomic_json(self.state_path, state)
        except OSError as error:
            raise RecoveryError("Host recovery state cannot be saved") from error

    def container_state(self, name):
        try:
            raw = self.command(["docker", "inspect", "--format", "{{json .State}}", name])
        except RecoveryError:
            return None
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise RecoveryError("Docker returned invalid container state") from error
        if not isinstance(value, dict):
            raise RecoveryError("Docker returned invalid container state")
        return value

    def read_monitor(self):
        try:
            with self.monitor_state.open(encoding="utf-8") as input_file:
                record = json.load(input_file)
        except (FileNotFoundError, OSError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("schema") != 1:
            return None
        updated = record.get("updated_at")
        if type(updated) not in (int, float) or updated < 0:
            return None
        return record

    def fault(self, ignore_monitor=False):
        self.command(["docker", "info", "--format", "{{.ServerVersion}}"])
        states = {name: self.container_state(name) for name in REQUIRED}
        bad = []
        for name, state in states.items():
            if ignore_monitor and name == "transmission-healthcheck":
                continue
            if state is None or not state.get("Running"):
                bad.append(f"{name}:stopped")
            elif state.get("Health") and state["Health"].get("Status") == "unhealthy":
                bad.append(f"{name}:unhealthy")
        record = self.read_monitor()
        now = self.clock()
        if record is None or record["updated_at"] > now + 60 or now - record["updated_at"] > self.stale:
            bad.append("monitor:stale")
            return True, ",".join(bad), False
        if record.get("phase") in ("checking", "recovering"):
            return bool(bad), ",".join(bad) or "monitor:pending", True
        status = record.get("network_status")
        if status in ("CONFIG_ERROR", "STATE_ERROR", "PORTCHECK_UNKNOWN", "SYS_ERR"):
            # Fresh, actionable configuration and checker faults need an alert,
            # not a disruptive stack recreation based on Gluetun health alone.
            bad = [item for item in bad if not item.endswith(":unhealthy")]
            if not bad:
                return False, f"report_only:{status}", False
        if status in RECOVERABLE and record.get("recoverable") is True:
            bad.append(f"network:{status}")
        return bool(bad), ",".join(bad), False

    def verify_namespaces(self):
        names = ("gluetun", *CONSUMERS)
        identities = []
        for name in names:
            state = self.container_state(name)
            if state is None or not state.get("Running") or type(state.get("Pid")) is not int:
                raise RecoveryError(f"{name} did not start")
            try:
                identities.append(os.readlink(f"/proc/{state['Pid']}/ns/net"))
            except OSError as error:
                raise RecoveryError(f"Cannot inspect {name} network namespace") from error
        if len(set(identities)) != 1:
            raise RecoveryError("Torrent services do not share Gluetun's network")

    def recover(self, before_disruption=None):
        deadline = self.monotonic() + 480

        def run_step(args):
            remaining = deadline - self.monotonic() - 120  # Reserve time to restore a stopped stack.
            if remaining <= 0:
                raise RecoveryError("Recovery budget exhausted")
            stage = f"{args[0]} {','.join(item for item in args if item in (('gluetun',) + CONSUMERS))}"
            LOG.warning("Stack recovery stage: %s", stage)
            try:
                self.compose(*args, timeout=min(90, remaining))
            except RecoveryError as error:
                raise RecoveryError(f"Stage {stage}: {error}") from error

        stop_monitor = ("stop", "transmission-healthcheck")
        stop_others = ("stop", "mam-updater", "ptp-archiver", "transmission", "gluetun")
        start_gluetun = ("up", "-d", "--no-deps", "--force-recreate", "--no-build", "--pull", "never", "gluetun")
        start_consumers = ("up", "-d", "--no-deps", "--force-recreate", "--no-build", "--pull", "never", *CONSUMERS)
        try:
            run_step(stop_monitor)
            if before_disruption is not None and not before_disruption():
                self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never",
                             "transmission-healthcheck", timeout=60)
                LOG.info("Stack recovery canceled after monitor stop; monitor restarted")
                return False
            for step in (stop_others, start_gluetun, start_consumers):
                run_step(step)
        except RecoveryError as error:
            LOG.error("Stack recovery interrupted: %s", error)
            for service_group in (("gluetun",), CONSUMERS):
                remaining = min(60, deadline - self.monotonic())
                if remaining <= 0:
                    break
                try:
                    self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", *service_group,
                                 timeout=remaining)
                except RecoveryError as start_error:
                    LOG.error("Could not restore %s: %s", ",".join(service_group), start_error)
            raise
        self.verify_namespaces()
        LOG.warning("Stack recreated; waiting for fresh application health checks")
        return True

    def check(self):
        if not self.ismount(self.data_mount):
            raise RecoveryError("Torrent data mount is absent")
        state = self.load_state()
        fault, reason, pending = self.fault()
        now = self.clock()
        if not fault:
            if not pending and state["failure_since"] is not None:
                state["failure_since"] = None
                self.save_state(state)
            return "pending" if pending else "report_only" if reason.startswith("report_only:") else "healthy"
        if state["failure_since"] is None:
            state["failure_since"] = now
            self.save_state(state)
            LOG.warning("Recovery candidate started: %s at %.0f", reason, now)
        elif state["failure_since"] > now:
            state["failure_since"] = now
            self.save_state(state)
        if pending:
            LOG.info("Recovery pending: %s", reason)
            return "pending"
        if now - state["failure_since"] < self.failure:
            return "waiting"
        previous = state["last_stack_recovery_at"]
        if previous is not None and previous > now:
            state["last_stack_recovery_at"] = now
            self.save_state(state)
            return "cooldown"
        if previous is not None and now - previous < self.cooldown:
            return "cooldown"
        self.compose("config", "--quiet")
        if not self.ismount(self.data_mount):
            raise RecoveryError("Torrent data mount disappeared during preflight")
        fault, reason, pending = self.fault()
        if not fault or pending:
            if not fault and not pending:
                state["failure_since"] = None
                self.save_state(state)
            LOG.info("Stack recovery canceled during preflight: %s", reason or "healthy")
            return "canceled"

        def before_disruption():
            if not self.ismount(self.data_mount):
                LOG.error("Torrent data mount disappeared before stack disruption")
                return False
            current_fault, current_reason, current_pending = self.fault(ignore_monitor=True)
            if not current_fault or current_pending:
                if not current_fault and not current_pending:
                    state["failure_since"] = None
                    self.save_state(state)
                LOG.info("Stack recovery canceled after monitor stop: %s", current_reason or "healthy")
                return False
            state["last_stack_recovery_at"] = self.clock()
            self.save_state(state)
            LOG.warning("Recovering torrent stack: %s", current_reason)
            return True

        return "attempted" if self.recover(before_disruption) else "canceled"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--data-mount", required=True)
    parser.add_argument("--monitor-state", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--lock-dir", default="/run/torrenter-recovery")
    parser.add_argument("--stale", type=int, default=900)
    parser.add_argument("--failure", type=int, default=1800)
    parser.add_argument("--cooldown", type=int, default=3600)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if min(args.stale, args.failure, args.cooldown) <= 0:
        parser.error("time limits must be positive")
    state_dir = Path(args.state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_dir = Path(args.lock_dir)
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (lock_dir / "recovery.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info("Another recovery invocation is active")
            return
        try:
            result = Recovery(args.project, args.data_mount, args.monitor_state, state_dir,
                              args.stale, args.failure, args.cooldown).check()
            if result in ("attempted", "canceled"):
                LOG.info("Check result: %s", result)
        except RecoveryError as error:
            LOG.error("Recovery check failed: %s", error)
            raise SystemExit(1) from error


if __name__ == "__main__":
    main()
