# Transmission recovery operations

The deployed monitor checks Gluetun's forwarded port every five minutes. A positive
port is copied to Transmission if needed. Three consecutive observations of port
zero or a closed external port trigger one bounded VPN stop/start cycle, with a
30-minute cooldown. A stopped VPN gets a start-only attempt. The host timer checks
every minute and recreates the five services sharing Gluetun's network namespace
after 30 minutes of persistent recoverable failure, with an hourly cooldown.
The timer does not reboot the Pi or touch torrent data.

Detection, VPN cycle, port repair, and verified health events are in the Pi's
volatile journal. They remain available across container recreation during one
boot and disappear on reboot. This avoids additional SD card log writes. Use
`journalctl -b CONTAINER_NAME=transmission-healthcheck` for monitor events and
`journalctl -b -u torrenter-recovery.service` for host escalation. The monitor's
`recovery-state/monitor.json` is the latest observation, while its cooldown state
and the host's recovery state are small overwritten files used to coordinate
retries. Healthy poll messages are suppressed in the journal, but Healthchecks
still receives a success ping on every poll.

## Monitor statuses and alerting

The monitor (`roles/torrenter/files/transmission-healthcheck/check.py`) publishes
its latest observation to `recovery-state/monitor.json` and pings Healthchecks on
every poll: a success ping only for `HEALTHY`, a `/fail` ping for every other
status. Checks run in a fixed order — Gluetun VPN status, Gluetun forwarded
port, Transmission peer port (a mismatch is repaired in place), external port
check, then per-torrent errors — so a late-stage status implies the earlier
stages passed.

| Status | Meaning | Host recovery (`recover.py`) |
| --- | --- | --- |
| `HEALTHY` | Ports match and are externally open; no torrent local errors | Clears failure state |
| `VPN_DOWN` | VPN stopped/starting/stopping; a stopped VPN gets a start-only attempt | Recoverable |
| `NO_PORT` | Gluetun forwarded port is 0; 3 consecutive observations trigger a VPN cycle | Recoverable |
| `CLOSED` | Forwarded port is externally closed; 3 consecutive observations trigger a VPN cycle | Recoverable |
| `MISMATCH` | Transmission did not adopt the forwarded port during repair | Recoverable |
| `API_UNREACHABLE` | Gluetun control API unreachable or erroring | Recoverable |
| `TR_UNREACHABLE` | Transmission RPC unreachable or rejecting requests | Recoverable |
| `RECOVERY_FAILED` | A VPN stop/start transition failed or timed out | Recoverable |
| `MONITOR_ERROR` | Unexpected exception inside the monitor itself | Recoverable |
| `CONFIG_ERROR` | Bad credentials or invalid values from Gluetun/Transmission | Report-only |
| `STATE_ERROR` | Monitor/recovery state files unreadable or invalid | Report-only |
| `PORTCHECK_UNKNOWN` | External port checker (portcheck.transmissionbt.com) unavailable | Report-only |
| `SYS_ERR` | At least one torrent reports a local error (see below) | Report-only |

Recoverable statuses count toward stack recovery only while the monitor record
marks them `recoverable: true` and the fault persists for
`torrenter_stack_failure_seconds` (see the host timer description above).
Report-only statuses never trigger a VPN cycle or a stack recreation; they exist
so a human sees a fault that automation cannot fix.

Known noise problem (identified 2026-10-18): the per-torrent check runs last
and is not debounced. If any loaded torrent has Transmission RPC `error == 3`
(`TR_STAT_LOCAL_ERROR` in Transmission 4.0.x: local trouble such as disk full,
permissions, or missing files — tracker errors are code 2 and are ignored), the
monitor returns `SYS_ERR TORRENT_ID_<id>` for that poll. A single persistently
erroring torrent therefore sends a Healthchecks `/fail` ping every five
minutes even though the VPN, ports, and daemon are healthy, and the message
carries only the first erroring torrent's integer ID — an identifier that is
not stable across daemon restarts — while the torrent `name` and `errorString`
the monitor already fetched are discarded. With ~1,400 torrents loaded, at
least one local error is a routine condition, not a stack outage.

`torrenter_recovery_enabled: false` disables monitor mutations and the host timer
at the next Ansible deployment. For temporary maintenance on an already deployed
host, stop and disable `torrenter-recovery.timer`, then stop
`transmission-healthcheck` before intentionally stopping Gluetun. Restore the
monitor and timer when maintenance ends. While the timer is enabled, its desired
state is a running torrent stack.

## Review and rollout

This repository change does not deploy itself. Before a live rollout, record the
current Gluetun and monitor image IDs, running container IDs, monitor code hash,
mount status, and network namespace IDs. Keep a private copy of the live
`compose.yaml`, `.env`, and Gluetun auth file outside this repository. Preserve the
old images for rollback. Check that the Healthchecks Transmission monitor expects
five-minute pings and has enough grace for a three-minute recovery attempt.

The narrow target is:

```sh
ansible-playbook -i inventory.yml site.yml --list-tasks --tags torrenter_stack --limit pi4_2020
ansible-playbook -i inventory.yml site.yml --check --tags torrenter_stack --limit pi4_2020
ansible-playbook -i inventory.yml site.yml --tags torrenter_stack --limit pi4_2020
```

The last command changes the active VPN and must be run only during the approved
rollout. The role pulls the pinned Gluetun image and builds helpers before stopping
consumers; it then recreates Gluetun and all namespace consumers together. The
host timer starts only after the monitor is deployed and verified. A failed stack
update attempts to start every consumer again and reports failure.

After deployment, inspect `systemctl status torrenter-recovery.timer`,
`journalctl -u torrenter-recovery.service`, and the monitor log. Confirm the
monitor's `recovery-state/monitor.json` shows a fresh `HEALTHY` record with
matching positive ports and `external: open`. Confirm the five application
containers share Gluetun's network namespace and `/mnt/expansion` remains mounted.
Run the same narrow Ansible command again and confirm container IDs do not change.
An isolated or scheduled maintenance exercise can then verify each recovery layer.

For rollback, stop and disable the host timer first. Restore the private copies
of the previous configuration, select the previous Gluetun and monitor images,
then stop the four consumers, recreate Gluetun, and recreate the consumers in that
order. Preserve all bind mounts and volumes. The prior configuration lacks the
new recovery behavior, so continue monitoring until the replacement is ready.
