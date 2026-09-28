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
