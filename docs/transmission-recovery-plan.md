Prepared on 2026-09-28 for implementation in `<repo-root>`.

The objective is to keep Transmission reachable through its VPN without routine manual repair. Implement and test the changes described here when the user requests implementation. This document is a plan, not authorization to deploy or inject failures into the live Pi. During the investigation, no application configuration, running service, or repository source code was changed.

**The incident was a failed renewal followed by missing recovery.** A successful VPN connection and a working inbound forwarded port are separate conditions. On September 23, Proton's port-mapping endpoint rejected a renewal. The tunnel remained usable, but inbound forwarding disappeared. The existing setup detected this condition and reported an error indefinitely; it did not have a deployed application recovery action.

The retained logs establish the following timeline. Times below are America/Los_Angeles (PDT); the monitor's own log timestamps were UTC.

| Time | Evidence |
| --- | --- |
| September 22, 01:16 | Port renewal timed out; Gluetun's connectivity check also failed and triggered a VPN reconnect. |
| September 22, 01:19 | Forwarding returned on port `56782`; the update hook successfully set Transmission to that port. |
| September 23, 23:00:47 | The monitor last reported port `56782` open. |
| September 23, 23:01:29 | A renewal to `10.2.0.1:5351` failed with UDP `connection refused`. Gluetun removed the firewall allowance, emptied the port file, and logged one new forwarding start. No subsequent forwarding success is in the retained logs. |
| September 23, 23:05:49 onward | The monitor reported `E:GLUETUN_PARSE` every five minutes. |
| September 28 inspection | Gluetun API: `port=0`, VPN `running`; Transmission peer port: `56782`; Gluetun's native healthcheck exits successfully, but the custom Docker healthcheck fails because the port file is empty. |

This explains why the earlier interruption recovered and this one did not: the earlier event affected the connectivity check which drives Gluetun's reconnect behavior. The later event left that check passing. Long periods of successful operation did not exercise the missing recovery path. We cannot identify why the provider rejected this particular renewal from local logs alone, or promise to prevent all provider interruptions.

There is also a matching retry defect in the installed Gluetun version. In v3.41.1, a forwarding restart that fails can lose its retry path and wait for a subsequent VPN event. This is a strong explanation for the single `starting` log followed by silence, but the failed restart's underlying error was not logged, so that exact internal execution is inferred. The practical response is to upgrade to the fixed stable release and retain our own application recovery. [Installed-version retry loop](https://github.com/passteque/gluetun/blob/v3.41.1/internal/portforward/loop.go), [v3.41.2 fixes](https://github.com/passteque/gluetun/releases/tag/v3.41.2), [v3.41.3 regression fix](https://github.com/passteque/gluetun/releases/tag/v3.41.3).

**The confirmed defects in our setup are sufficient to explain the prolonged outage.**

- The deployed monitor uses `if not all([gluetun_port, gluetun_vpn_status])`. Integer `0` is a valid indication that there is no forwarded port, but this code calls it a parse failure. More fundamentally, the deployed monitor has no VPN restart implementation at all; fixing that condition alone cannot restore service.
- Docker's `restart: unless-stopped` handles a process stopping; it does not turn a failed Docker healthcheck into a restart. The Gluetun container stayed running. [Docker restart behavior](https://docs.docker.com/engine/containers/start-containers-automatically/).
- The monitor depends on both Gluetun and Transmission being `service_healthy`. A newly created recovery monitor therefore cannot start when the dependency it needs to repair is unhealthy. This did not stop the already-running monitor in this incident, but is a startup and deployment trap. [Compose startup conditions](https://docs.docker.com/compose/how-tos/startup-order/).
- The watchdog, root heartbeat, UPS monitor, backup pings, and other application checks address different failures. None of those deployed mechanisms repairs a missing VPN forward. A host heartbeat can remain green during this failure.

**Preserve the starting work and distinguish it from the deployed state.** Repository HEAD during inspection was `7bdc61ea305234ac7a62fa3171942e39b7f91cf5`. These two files were already modified before this investigation:

- `roles/torrenter/files/transmission-healthcheck/check.py`
- `roles/torrenter/templates/compose.yaml.j2`

Those edits add a three-check port-zero threshold and a stop/start API function. Build on their intent; do not discard them as agent-generated changes. The Pi's host copy and the running monitor's `/app/check.py` both exactly match the committed version, SHA-256 `aa45bf04f6786582aca2cc8882e313990cb0f423feb0648e09594cd971bbc09e`. This proves the draft is not deployed, but does not establish why it was never deployed.

The draft is incomplete: PUT responses are unchecked; restart success is not verified; a stop followed by a failed start can leave the VPN down; the counter only resets after an entirely successful application check; and there is no cooldown, startup handling, mismatch repair, or fallback when the monitor itself cannot act. Do not deploy it unchanged.

**Use two bounded recovery mechanisms with distinct jobs.** Keep the existing application monitor inside the VPN namespace to check external reachability and perform small repairs. Add one host systemd timer as the fallback for a persistently broken stack or a stuck monitor. No upstream Gluetun changes are part of this work.

| Mechanism | Responsibility | Target timing |
| --- | --- | --- |
| Gluetun v3.41.3 | Maintain the provider's port lease and retry transient forwarding errors. | Native behavior. |
| Existing Transmission monitor, improved | Match Transmission's port to Gluetun; reconnect the VPN after confirmed forwarding failures; publish health and recovery results. | Check every 300 seconds; act on the third consecutive relevant failure, approximately 10–15 minutes after failure, plus bounded request time. |
| New host recovery timer | Observe container state and monitor progress; recreate the five services that share the VPN namespace if application recovery remains unsuccessful. | Check every 60 seconds; require 30 minutes of persistent recoverable failure; at most one stack recovery per hour. |
| Existing Healthchecks.io Transmission check | Tell the owner when the application fails, what repair was attempted, and whether it actually recovered. | Failure remains visible throughout unsuccessful recovery. |

The normal path should be a provider retry or an API reconnect. The host fallback deals with failures such as an unresponsive API, a hung monitor, or a broken container. It must not reboot the Pi, restart the Docker daemon, alter disk mounts, or manipulate torrent data. All five application services are configured to share Gluetun's network namespace; matching live namespace identifiers were verified for Gluetun, Transmission, and the monitor. Recreating just Gluetun can strand its consumers, so a stack recovery must coordinate those services.

Use these explicit defaults, exposed through the torrenter role rather than scattered literals. Existing environment names can be retained where shown. Start the new host timer only after the monitor publishing its state has been deployed and verified.

| Ansible default | Monitor environment / host setting | Value |
| --- | --- | --- |
| `torrenter_recovery_enabled` | `RECOVERY_ENABLED`; timer enabled/running | `true` |
| `torrenter_check_interval_seconds` | `CHECK_INTERVAL_SECONDS` | `300` |
| `torrenter_port_zero_restart_threshold` | `PORT_ZERO_RESTART_THRESHOLD` | `3` |
| `torrenter_closed_port_restart_threshold` | `CLOSED_PORT_RESTART_THRESHOLD` | `3` |
| `torrenter_recovery_grace_seconds` | `RECOVERY_GRACE_SECONDS` | `180` |
| `torrenter_vpn_recovery_timeout_seconds` | `VPN_RECOVERY_TIMEOUT_SECONDS` | `180` |
| `torrenter_vpn_recovery_cooldown_seconds` | `VPN_RECOVERY_COOLDOWN_SECONDS` | `1800` |
| `torrenter_monitor_stale_seconds` | Host stale-record threshold | `900` |
| `torrenter_stack_failure_seconds` | Host persistent-fault threshold | `1800` |
| `torrenter_stack_recovery_cooldown_seconds` | Host attempt cooldown | `3600` |

Use `./recovery-state:/state` for the monitor's bind mount, with files `monitor.json` and `vpn-recovery.json`. Create this directory independently of the copied image source and with ownership allowing the monitor to write; root on the host must be able to read it. Validate configuration types and positive numeric ranges on startup. Host-only settings can be supplied as systemd command arguments; credentials must never be arguments.

Implement the work in this order, keeping each step reviewable.

1. **Update the application configuration and remove startup obstacles.**

   Change the Gluetun version default from `v3.41.1` to `v3.41.3` in `roles/torrenter/templates/compose.yaml.j2`, or put the same pinned value in a new role defaults file and reference it consistently. v3.41.3 was the latest stable release checked on 2026-09-28. Do not use `latest` or v3.41.2: the latter introduced a forwarding hook deadlock corrected by v3.41.3.

   Set the monitor's Gluetun and Transmission dependencies to `condition: service_started`. Its own code must tolerate startup, missing ports, and temporarily unavailable APIs. Retain the existing Docker healthchecks as observations for operators and the host timer. Keep the Transmission image and its data mounts as they are. The existing Transmission settings already disable random peer ports and Transmission's own router port forwarding.

   Retain the immediate Gluetun port-update hook, with bounded execution. Use `/usr/bin/timeout 30 /gluetun/update-port.sh {{PORT}}` as the rendered UP_COMMAND and retain the literal Gluetun placeholder through Jinja. Verify `timeout` and GNU `wget` in the target image during testing; they are present in the current image. Add `--timeout=5 --tries=1` to its requests, validate the supplied port as 1–65535, and remove the log that prints the Transmission session ID. The monitor repairs a missed update later, so the hook must not delay VPN startup for minutes. Do not increase the hook's retry budget.

2. **Make control API authentication explicit.**

   The current `HTTP_CONTROL_SERVER_AUTH_BASIC_USER/PASS` settings are not read by this Gluetun version. There is no mounted auth file; unauthenticated GET requests to both status endpoints currently succeed. The installed version also declares the VPN PUT route public by default in its source. Authentication failure is therefore not the cause of the current outage; relying on those defaults is an avoidable recovery dependency. [Versioned authentication configuration](https://github.com/passteque/gluetun/blob/v3.41.1/internal/configuration/settings/server.go), [route defaults](https://github.com/passteque/gluetun/blob/v3.41.1/internal/server/middlewares/auth/settings.go).

   Add `roles/torrenter/templates/gluetun-auth.toml.j2`. Define one basic-auth role using existing `GLUETUN_USER` and `GLUETUN_PASS`, allowing exactly `GET /v1/portforward`, `GET /v1/vpn/status`, and `PUT /v1/vpn/status`. Template it with `no_log: true`, `diff: false`, mode `0600`, and ownership readable by Gluetun's effective UID. Current runtime UID/GID for its files is 1000; verify this against the target image. Bind-mount it read-only at `/gluetun/auth/config.toml` and remove the unsupported environment settings. Escape values using an actual string serializer, then parse the rendered TOML in a test with quotes, backslashes, and special characters in dummy credentials. Do not print real credentials in Compose output or Ansible diffs.

   Keep the existing published control port for compatibility in this change; narrowing its exposure can be considered separately. Auth-file changes must cause a coordinated Gluetun/consumer recreation because a running server does not reload this file merely because Ansible updated it. No new unauthenticated control routes.

3. **Turn the existing monitor into a small, testable recovery loop.**

   Keep `check.py` as the entrypoint. Extract configuration loading from import time and expose one check/reconciliation step plus a recovery helper. Inject the HTTP clients, clock, and sleep function so tests never use real credentials, network services, or actual five-minute sleeps. Preserve the existing torrent error reporting. Avoid introducing a general orchestration framework or more dependencies for this logic.

   Apply this decision table before unrelated torrent errors or notification results can interrupt network repair:

   | Observation | Required behavior |
   | --- | --- |
   | Gluetun port missing, null, boolean, wrong type, negative, or above 65535 | Report a schema/configuration error. Do not infer port zero and do not cycle the VPN. Tolerate unrelated extra JSON fields, including `ports`. |
   | VPN running, integer port zero | Report `NO_PORT`, increment only the consecutive no-port counter, reset the closed-port counter. On the third such observation, attempt one VPN reconnect if allowed by cooldown. |
   | Valid positive Gluetun port | Reset the no-port counter immediately, even if a later torrent check or notification fails. |
   | Gluetun port differs from Transmission's peer port | Re-read Gluetun to avoid applying a stale port, send Transmission `session-set`, check its RPC result, and verify by readback. Limit reconciliation to two attempts per check. No VPN reconnect for a simple mismatch. |
   | Both ports match; external checker returns the exact value `1` | Network healthy. Reset the closed-port counter. Report success only if the remaining application checks also pass. |
   | Both ports match; external checker returns the exact value `0` | Report `CLOSED`; reconnect only after three consecutive closed observations for the same port. Skip this count during the cycle that changed a port or initiated recovery. |
   | External checker times out, rejects the request, or returns unexpected content | Report `PORTCHECK_UNKNOWN`, not `CLOSED`. Reset the closed-port counter. Do not reconnect solely because the checker is unavailable. |
   | Gluetun or Transmission API rejects credentials | Report `CONFIG_ERROR`; do not try to fix credentials by restarting services. |
   | API connection fails or times out | Report unavailable, reset consecutive port failure counters, and let the host fallback handle a persistent outage. Do not treat it as port zero. |
   | Torrent filesystem errors with healthy networking | Preserve the existing error report; do not reset the VPN or stack to repair a filesystem problem. |

   Use a 180-second grace period after monitor startup and after initiating a VPN recovery. During grace, collect observations, publish state, and repair positive port mismatches, but do not accumulate port-zero or external-closed counts. A VPN `starting` or `stopping` state is a transition, not evidence that connectivity is working. Continued transition failure must remain visible to the host fallback after grace expires.

   All HTTP calls need explicit timeouts and checked statuses. Transmission RPC also needs `result == "success"`; HTTP 200 alone is insufficient. Preserve the 409 session-token refresh with at most one retry and apply the same RPC validation on both paths. Repair port mismatch before testing external reachability so an unavailable checker cannot block a local repair.

   Keep external HTTP checking inside the shared VPN network namespace. Calling the existing port-check service from the host would test the host's public address instead. Do not replace the provider's port with a fixed Docker-published peer port.

4. **Make a VPN reconnect an action with a verified outcome.**

   Use Gluetun's existing `PUT /v1/vpn/status` control API, first requesting `stopped`, then `running`. Check every HTTP response and poll GET status to observe the transition. A request returning HTTP 200 or an `already ...` outcome is not proof that the desired final state was reached. Use a monotonic total action deadline of 180 seconds, polls at most every 3 seconds, and per-request connect/read timeouts capped by remaining time.

   Record the attempt before the first stop request. Persist `last_vpn_cycle_at` atomically in a small JSON file under `/state`, mounted from `./recovery-state`. Enforce a 1800-second cooldown between stop/start cycles, including failed attempts and monitor restarts. Counters can stay in memory and restart at zero. Use UTC timestamps for persisted records and monotonic time for in-process deadlines. A future persisted timestamp must impose at most one cooldown rather than suppressing repair forever after a clock correction. If a cooldown record cannot be saved, report that failure and do not issue a stop request.

   After any stop request, including a timeout with uncertain outcome, make a bounded effort to return the VPN to running. Re-read status and send start once it is stopped; do not assume a timed-out stop failed or a start while `stopping` succeeded. If the action deadline expires, report failure and let subsequent checks continue ensuring a stopped VPN is started. A start-only attempt is allowed during the stop/start cooldown and must not issue another stop. Rate-limit start-only requests to at most once per 60 seconds. With recovery enabled, desired VPN state is running; document maintenance mode for intentional stops.

   Use one `RECOVERY_ENABLED` setting to disable all monitor mutations, including peer-port changes. It must still report health when disabled. Map the same Ansible setting to enabling/disabling the host timer. Never announce `RECOVERED` until a fresh observation shows positive matching ports and a successful external port check. Distinguish `RECOVERY_ATTEMPTED`, `RECOVERY_FAILED`, `RECOVERY_PENDING`, and verified recovery in logs and Healthchecks messages.

5. **Publish progress for the host fallback and improve failure reporting.**

   Atomically write `/state/monitor.json` at check start and completion. Include a schema version, `updated_at` in UTC epoch seconds, phase (`checking`, `recovering`, or `complete`), network status, a recoverable-failure boolean, a concise reason code, both observed ports if available, the external result (`open`, `closed`, `unknown`, or `not_checked`), and the last VPN-cycle timestamp. Do not include credentials, session IDs, tracker URLs, or torrent names. Keep the cooldown file separate so an interrupted status write cannot reset it.

   The host must distinguish progress from application health: a fresh `checking` record is not success, and refreshing a failure record must not reset the host's persistent-failure timer. During grace or a bounded recovery, publish `pending`, which must not clear a previously established failure history. Record network health independently of torrent errors and Healthchecks delivery status.

   Keep the existing Transmission Healthchecks endpoint. Send failure messages that prioritize the reason, repair attempted, and result within the existing length limit. Check notification response statuses and sanitize exception logging so failed requests cannot expose the secret ping URL. Notification failures must not prevent local repair or state publication, reset counters, or cause a stack restart. All network checks must also work with notifications disabled or pointed at a local test server.

   The investigation did not inspect Healthchecks.io dashboard schedules, grace periods, integrations, or delivery. During an authorized rollout, verify the existing Transmission check expects a five-minute cadence, has suitable grace (at least the bounded recovery duration), and delivers failure/recovery notifications. The Pi heartbeat is not a substitute for this application check. Do not add new contact destinations as part of the code change.

6. **Add the host fallback without coupling it to a healthy VPN.**

   Add a host Python script using only the standard library, a oneshot systemd service, and a timer. Suggested repository files are `roles/torrenter/files/recovery/recover.py`, `roles/torrenter/templates/torrenter-recovery.service.j2`, `roles/torrenter/templates/torrenter-recovery.timer.j2`, and `roles/torrenter/tasks/recovery.yml`. Add the configuration defaults in `roles/torrenter/defaults/main.yml`.

   Install the script as `/usr/local/libexec/torrenter-recovery`. Run the service as root with a fixed Compose project path passed as an argument, a fixed service allowlist, `StateDirectory=torrenter-recovery`, and `RuntimeDirectory=torrenter-recovery`. Persist only failure onset and last stack-recovery time under `/var/lib/torrenter-recovery`. Use a file lock to prevent overlapping/manual invocations. Configure the timer with `OnBootSec=5min` and `OnUnitInactiveSec=60s`. It must not depend on Gluetun's health or start the Docker daemon to satisfy a dependency. Use an overall script action deadline of 480 seconds and `TimeoutStartSec=600`; reserve the final 120 seconds of the script budget for startup attempts after a partial failure. Bound observation/validation subprocesses to 15 seconds and individual mutation calls to at most 90 seconds or the remaining budget, whichever is shorter.

   Read Docker state using `docker inspect` with selected fields and the monitor's status file. A recoverable fault is an unhealthy/stopped/missing required container, a monitor record missing or stale for 900 seconds, or a fresh monitor record indicating a persistent networking failure from step 3. Required services are Gluetun, Transmission, and the monitor; include MAM and PTP as namespace consumers in recovery but do not interpret their tracker-side errors as reasons to reset the VPN. Treat malformed monitor state as unavailable/stale, with grace, rather than as healthy. Fresh configuration/authentication errors or external-checker-only failures are report-only; they must not cause restarts merely through the monitor record.

   An eligible fault must persist for 1800 seconds before stack recovery; its onset is stored on the host and survives timer runs and container replacements. Use the earliest continuous fault onset even if the reason changes among recoverable network failures. A fresh `checking`/`recovering` phase preserves the previous onset but cannot on its own authorize an action. Always re-evaluate current eligibility immediately before recovery. A completed healthy observation clears the onset. A completed report-only condition with otherwise healthy required containers also clears recovery eligibility without claiming application success: for example, a previous no-port failure followed by only an external checker outage must not trigger a delayed restart. Docker daemon unavailable, the data disk not mounted, or an invalid Compose configuration are escalation conditions: log clearly and perform no stack mutations.

   Check `/mnt/expansion` is actually mounted before any recovery; do not start containers against an unmounted directory. Require a finite successful Compose validation before stopping services. Enforce a 3600-second cooldown between stack-recovery attempts, recorded before mutations. This bounds repeated disruption during a provider outage; keep observing and reporting during cooldown. A disk/malformed-state problem must not silently bypass this cooldown.

   Stop the monitor first to avoid competing API changes, then stop the other four services. Recreate Gluetun first, then recreate Transmission and all three consumers using the current existing images and fixed names. Equivalent commands, each with a subprocess deadline and checked return code:

   ```text
   docker compose -f <project>/compose.yaml stop transmission-healthcheck
   docker compose -f <project>/compose.yaml stop mam-updater ptp-archiver transmission gluetun
   docker compose -f <project>/compose.yaml up -d --no-deps --force-recreate --no-build --pull never gluetun
   docker compose -f <project>/compose.yaml up -d --no-deps --force-recreate --no-build --pull never transmission transmission-healthcheck mam-updater ptp-archiver
   ```

   Explicitly starting consumers with `--no-deps` avoids waiting on the very health condition being repaired. Confirm the new containers share Gluetun's new namespace. If a stop or recreate call fails or times out, attempt bounded startup of the same services and record the failed stage; do not leave a stopped stack without attempting to start it. Never use `down -v`, volume pruning, pulls/builds, a Docker daemon restart, or a Pi reboot in this automatic path. An hourly attempt limit applies even when recovery fails. The next fresh application observations determine whether the attempt worked.

   Make script logic callable with fake Docker, time, filesystem, and subprocess adapters. No shell evaluation of data read from JSON. A deliberately disabled timer is maintenance mode; an enabled timer owns a desired running torrent stack. Document disabling it and stopping the monitor before intentional maintenance, and restoring both afterwards.

7. **Make deployment repeatable and verify the running result.**

   Integrate auth templating, state directories, monitor configuration, and systemd installation into the existing torrenter role. Keep the authoritative templates in Ansible. Add a narrow `torrenter_stack` task tag to the application deployment and recovery tasks, using `apply: tags` on dynamic includes where needed. Verify `--list-tasks` and a check-mode run select no disk formatting, Docker package upgrades, or unrelated roles. Do not assume adding a role-wide tag safely excludes that role's dependencies.

   Address the deployment gap explicitly: `COPY check.py` bakes code into an image. Updating the host file or restarting an old container is not sufficient. Build with cache on every application deployment, or base the build decision on a comparison with the running container's code as well as copied files. Do not rely solely on whether the current Ansible run changed a host file: a previous interrupted deployment may already have copied it. Verify the deployed `/app/check.py` hash against the intended file before declaring success.

   Pull the pinned replacement Gluetun image and build the monitor before stopping running services. Recreate all namespace consumers when Gluetun's image/configuration/auth changes. Use the coordinated order in step 6 for an approved maintenance rollout. For a monitor-only update, rebuild/recreate only that service. Do not put unconditional forced recreation in ordinary idempotent runs. Gate starting the host timer until deployment is complete; stop an existing timer during deployment and restore its intended state with an Ansible `always` path. Record/report deployment failures rather than claiming success from host-file changes.

   Tooling found on the controller: Python 3.12.3, ansible-core 2.16.3, and system-installed community.docker 3.7.0. The Pi has Docker 29.1.3 and Compose 5.0.1. The collection is not currently pinned by `requirements.yml`. Verify module compatibility as a deployment prerequisite. In particular, `wait`/`wait_timeout` were added after 3.7.0. Do not install community.docker 5.3.0 onto this core version without also preparing a compatible isolated Ansible environment; its published requirement is core >=2.17. Any toolchain update should be explicit and pinned, not an incidental system package upgrade. [Module reference](https://docs.ansible.com/projects/ansible/latest/collections/community/docker/docker_compose_v2_module.html), [5.3.0 core requirement](https://github.com/ansible-collections/community.docker/blob/5.3.0/meta/runtime.yml).

   Preserve existing secrets, images for unrelated services, mounts, torrent data, and the midnight backup. The backup profile is excluded from stack recovery. The root Pi heartbeat, watchdog configuration management, and broad host/update improvements are separate follow-up work and should not expand this patch.

**The test suite must demonstrate recovery behavior, not just successful API responses.** Use `unittest` and local HTTP stubs with dummy credentials. Tests must never import a module that starts the production loop or sends a real Healthchecks ping. Suggested location: `tests/torrenter/`, outside the image's copied application files.

| Test group | Required cases and assertions |
| --- | --- |
| Parsing and counter regressions | Zero is `NO_PORT`, missing/null/invalid are errors; reject booleans; accept extra `ports` field. `0, 0, positive, 0` does not trigger recovery even if the positive iteration has a torrent/notification failure. A transport or schema error breaks a consecutive streak. |
| VPN recovery | Three running/zero checks initiate one stop/start; no action inside grace/cooldown or with recovery disabled. HTTP 401/500, malformed replies, timeouts, delayed stop completion, and `already stopping` cannot be reported as success. Always attempt start after a possibly successful stop. A subsequent iteration handles a VPN left stopped. |
| Rate limits and persistence | Cooldown survives monitor restart; corrupted/unwritable state prevents an unsafe stop and reports a clear error; clock reversal cannot create a restart storm or an indefinite lockout. Start-only recovery does not produce another stop. |
| Port reconciliation | A positive mismatch is updated and verified before external checks. RPC 409 retries once; HTTP 200 with RPC failure is rejected. A changing Gluetun port is re-read and never applied as a known stale value. Persistent mismatch remains a failure. |
| External reachability | Open, confirmed closed, timeout, HTTP error, unexpected body. Only three consecutive confirmed closes on stable matching ports can initiate a reconnect; one timeout or port change resets that count. A checker outage alone never triggers the host fallback. |
| Reporting and liveness | Notifications failing cannot block repair. State writes are atomic, bounded, and contain no secrets. A fresh failure or `checking` record does not count as network success. Verified recovery requires an actual open-port result. |
| Host fallback | Healthy => no mutation; persistent failures => exactly the allowlisted ordered commands. Threshold/cooldown survive repeated invocations. Stale monitor and missing container are detected. An earlier recoverable fault followed by checker-only failure does not trigger a delayed restart. A missing mount, unavailable Docker, or invalid Compose prevents mutation. Concurrent invocations cannot overlap. Partial failure reserves time for startup and reports failure. |
| Deployment contracts | Render with dummy inventory/secrets; parse YAML and TOML; preserve literal `{{PORT}}`; monitor starts without healthy dependencies; auth/state mounts correct; backup excluded. Changing auth or Gluetun requires consumer recreation. A second unchanged deployment keeps container identities. An interrupted copy-then-deploy scenario still updates the running monitor code. |

Run offline tests with `python3 -m unittest discover -s tests/torrenter -v`. Validate shell syntax for the hook and run the appropriate Ansible syntax/template checks with scratch temporary directories. Validate rendered Compose using `docker compose config --quiet` with dummy environment values. Do not expose a fully interpolated production Compose file. If the local Docker daemon is unavailable, report that limitation and use an explicitly authorized isolated test environment rather than silently using the live torrent stack.

An isolated integration test should use local fake Gluetun/Transmission/port-check/notification endpoints to replay: healthy -> port zero -> failed reconnect -> successful reconnect with a different port -> Transmission updated -> external port open. Assert action order, cooldown, and final verified recovery. Separately simulate a stuck monitor and verify host fallback using a fake Docker executable; this test must not control real containers.

**Deployment and production acceptance are a later, explicitly requested step.** Once implementation and offline tests pass, prepare the exact diff and rollout commands. A production rollout changes the active VPN and requires the user's go-ahead under the current instruction to plan only. Save a private rollback copy of the rendered configuration, retain the old Gluetun image and old monitor image ID, and record baseline code hashes, container IDs, and mount/namespace state. Do not copy secrets into the repository or the plan.

For the approved rollout, use only the narrowly tagged torrent application tasks on `pi4_2020`, with shared SSH connection settings appropriate to this session. Verify selection before applying. Rebuild the monitor and recreate the affected namespace consumers, then require all of the following:

- The running Gluetun image is v3.41.3, the running monitor hash matches the source, and the host timer is enabled with the intended configuration.
- The control endpoints reject missing/wrong credentials and accept correct credentials. Test mutations only during the approved recovery exercise.
- VPN state is running, the forwarded port is positive, Transmission reports that same peer port, and a port check originating inside the VPN returns open. Observe success across at least three normal five-minute checks and subsequent lease renewals, rather than accepting a one-time API response.
- Monitor and host records/logs show successful verification, not only that commands ran. Confirm required containers share the current Gluetun network namespace and the data mount remains present.
- Healthchecks displays the expected application state; independently verify notification delivery if the user authorizes a notification test. Dashboard delivery is not inferable from local code alone.
- A second unchanged Ansible application run does not recreate healthy containers. During an approved maintenance exercise, verify one monitor-driven recovery and one host fallback in an isolated stack or controlled live window, and restore normal intervals afterward. Do not fabricate an outage by deleting the live port file: it is an output artifact, not the provider's lease.

Acceptance means a lost forward can recover without an operator, false/unknown external checks do not cause restarts, and unsuccessful repair remains visible. It cannot mean uninterrupted service during a provider-wide outage. The expected recurring operational task is occasional pinned image updates and checking genuine failure alerts, not manually repairing forwarding.

Rollback consists of disabling the new timer, restoring the saved templates and prior image selection, and recreating the same five services in coordinated order while preserving volumes. It restores the old behavior, including its lack of recovery, so report that limitation. A failed rollback/startup must leave a clear error and an explicit attempted startup, never an unreported stopped stack.

The implementation handoff can be issued as: “Implement the local Ansible and monitoring changes in `docs/transmission-recovery-plan.md`, preserving the existing edits. Run the offline and isolated tests, and prepare a reviewed rollout and rollback. Do not deploy to the Pi or induce live outages until I request that step.”
