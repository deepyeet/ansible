# Transmission recovery review and resolution — 2026-09-28

This document records the review findings as they were discovered. The subsequent
local changes fixed the deployment checksum, host recovery races, and missing
diagnostic events. The user chose volatile journal logs to avoid additional SD
card writes; the durable incident log proposal below is not being implemented.
The updated code still needs live rollout and verification on the Pi.

## Resolution

- Both Ansible monitor checksum comparisons now preserve the source file's final
  newline. The local Ansible reproduction verified that this matches the actual
  file checksum.
- Deployment stops the host timer and waits for a running or activating recovery
  service to finish before changing configuration or containers.
- Host recovery rechecks the mount and fault after Compose validation and again
  after stopping the monitor. If the fault clears, it restarts the monitor and
  leaves the other services running. Cooldown is committed only immediately before
  stopping the rest of the stack.
- Monitor logs now distinguish failure detection, peer-port repair start and
  verified result, VPN cycle start and completion, and verified healthy state.
  Host recovery logs its action stages and failed command exit status without
  printing arbitrary Docker output. Report-only faults are no longer labelled
  healthy by the host. Repeated healthy status lines are suppressed.
- Container output and host recovery events continue to use the Pi's volatile
  journal. The host lock sits under `/run`, so minute-by-minute timer checks do
  not update an SD card lock file. Current-state JSON and cooldown files remain
  small and persist to coordinate recovery; no persistent incident log was added.

The findings below describe the pre-fix behavior and the evidence for each issue.

## Findings

### P1: Monitor hash verification always rejects the current source

`roles/torrenter/tasks/docker_runtime.yml:62` and `:87` hash the result of
`lookup('file', ...)`. Ansible's default `rstrip=true` removes the trailing newline
from `check.py`, whereas the container's `sha256sum` hashes the actual bytes.

A local Ansible reproduction returned:

| Calculation | SHA-256 |
| --- | --- |
| Actual file bytes | `590f128b21287c9e997e0f8b0167586dc7d53ccad3ed13338384de85c441e991` |
| Current role lookup | `f2445d2d831cbbd103bc9b86cdb37e87597226aac152abf05794ed89a5e71779` |
| Lookup with `rstrip=false` | Same as actual file bytes |

Consequences: the role unnecessarily recreates an unchanged monitor, and its final
assertion fails after deployment. On the first rollout, execution never reaches
the host recovery installation in `tasks/deploy.yml`. Syntax checks and Ansible
check mode do not exercise this assertion because runtime tasks are skipped.

Fix both comparisons with an exact-byte checksum, or `rstrip=false`. Add an
Ansible-level regression test with a newline-terminated file, then verify that an
unchanged second deployment preserves container IDs.

[Ansible file lookup defaults](https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/file_lookup.html)

### P2: Host recovery can act on an obsolete observation

`files/recovery/recover.py:194` reads eligibility once. After the Compose
validation at `:220`, it commits the cooldown and recreates the stack without
checking the monitor or mount again. A deterministic reproduction published
`HEALTHY` during Compose validation: the host still invoked recovery. The same
window can overlap a new monitor VPN recovery attempt.

Recheck mount, container state and current monitor eligibility immediately before
committing to recovery. Stop the monitor and recheck its final observation before
stopping the remaining services, so a just-completed repair cancels escalation.
Tests should cover both a healthy observation and a recovery-in-progress update
arriving during preflight.

Separately, `tasks/deploy.yml:9` stops only the timer. An already-running recovery
service is not joined or stopped by these tasks, and deployment does not acquire
the recovery script's lock. A future deployment can therefore overlap a host
recreation. Quiesce the active recovery invocation before copying configuration or
changing containers; merely preventing the next timer invocation is insufficient.
This second concurrency finding is from code inspection, not a live race test.

### P2: Local incident logs disappear on reboot

Read-only inspection of the Pi found:

- All five running torrent containers use the `journald` driver.
- `/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf` sets
  `Storage=volatile`.
- Journal usage was 70.7 MiB under `/run/log/journal`, on a 760 MiB `/run` tmpfs.
- There are no explicit `RuntimeMaxUse` or retention overrides.
- log2ram covers `/var/log`, has a 256 MiB allocation, and syncs daily.
  `/var/log` and its disk copy each occupied approximately 23 MiB.
- Neither rsyslog nor syslog-ng is installed. `ForwardToSyslog=yes` therefore
  does not provide a durable copy. `/var/log/journal` is empty.
- `journalctl --list-boots` showed only the current boot.

The journal is already size-limited by systemd defaults; this is not an unbounded
Docker JSON log configuration. However, log2ram does not preserve the journal in
`/run`, so a reboot removes the local evidence needed for a future investigation.
Healthchecks may retain delivered short messages, but those omit much of the
incident context and cannot be delivered reliably while connectivity is broken.

The chosen behavior is to keep the journal volatile and size-limited by systemd's
existing limits. This sacrifices logs across a reboot but avoids extra SD writes.
The existing journal can be queried across container recreation during the same
boot. Current-state and cooldown files are overwritten in place and do not grow
with the number of incidents.

Persistent journaling was intentionally left out of the fix.

[Journal storage and limits](https://www.freedesktop.org/software/systemd/man/252/journald.conf.html)
and [Docker journal metadata and queries](https://docs.docker.com/engine/logging/drivers/journald/).

### P2: Repair evidence is incomplete, and the host can log false health

`files/transmission-healthcheck/check.py:356` repairs a mismatched port silently.
A reproduction changed Transmission from 45000 to 46000 and captured only
`HEALTHY PORT_46000 ACTIVE_0`. The original port, mutation and verification were
absent. The next check overwrites `monitor.json`; it is current status, not history.
VPN recovery publishes an intermediate state, but does not log its start before
the requests, so a killed process can lose evidence of the attempted action.

`files/recovery/recover.py:137` correctly avoids restarting for report-only errors,
but `:200` calls those observations `healthy`. A fresh `CONFIG_ERROR` reproduces
`Check result: healthy` in the host path. Generic command failures also discard
the failing stage and exit status, making failed recovery harder to diagnose.

Emit sanitized events for failure transitions, peer-port changes, VPN cycle
start/end/failure, cooldown decisions, stack recovery stages, and verified return
to health. Include UTC timestamp, reason, VPN status, old/new peer ports, external
check result, cooldown deadline, elapsed time, and an incident/attempt identifier.
Report nonrecoverable conditions as `report_only` with their reason. Retain the
command stage and return code without dumping environment or arbitrary output.
Only call recovery successful after a fresh application check confirms matching
positive ports and an open external port. Include the code/image identity in a
startup or deployment event to distinguish old and new implementations.

## What is validated

At review time, all 19 existing tests passed. They cover the original port-zero failure,
closed-port thresholds, VPN stop/start, a failed stop, cooldown persistence,
port reconciliation, checker outages, stale monitor state, ordered host recovery,
and attempted restoration after a failed Docker recreation. The HTTP simulation
ends with matching port 47000 and an externally open result from its fake checker.

Five additional scratch probes identified the original gaps or confirmed safeguards:

1. A health update during host preflight did not cancel recovery (since fixed).
2. A configuration error was labelled healthy by the host (since fixed).
3. A successful port repair had no explicit repair log (since fixed).
4. Disabling monitor recovery prevents VPN and peer-port mutations (pass).
5. A checker-only error clears prior host escalation eligibility (pass).

The updated suite has 24 passing tests, including recovery cancellation at both
preflight points and assertions for logged repair and verification. An Ansible
checksum reproduction confirmed the original deployment defect and the exact-byte
fix.
Scratch reproductions are in `/tmp/codex/torrenter-review/`; assertions in those
probes document observed defects and are not regression tests of desired behavior.

Under normal response times, an established monitor detects failure on the next
five-minute poll and attempts a VPN cycle after three failed observations, roughly
10–15 minutes after onset. Startup grace and request durations add time. Recovery
is verified on a subsequent poll, not just from a successful API request. The host
escalates after 30 minutes of observed persistent eligible failure; a stalled
monitor first has to become stale, so that path is roughly 45 minutes or longer.

Recovery state normally consists of overwritten JSON files and a lock, not an
append-only history. Routine monitor output is about 288 summaries/day; the host
adds up to 1,440 result lines/day plus systemd unit lifecycle messages. This is
modest, but healthy host result messages could be suppressed to preserve useful
journal retention. A size limit remains necessary regardless of expected volume.

## Remaining acceptance for live rollout

- Deploy during the maintenance window, then repeat the narrow deployment
  and confirm no unnecessary container recreation.
- Exercise the real control API and container recreation; verify external port
  reachability, shared network namespaces and persistent cooldown after restart.
- Confirm the journal remains under its volatile storage limit. Log history is
  intentionally reset on reboot.

The Pi is still running Gluetun v3.41.1. No new recovery code was deployed and no
live failure was injected during this review. Fake HTTP and Docker tests do not
establish compatibility or successful recovery with the real v3.41.3 image.
