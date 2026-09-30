# pixel_google_photos_runtime

Owns the four local custom sources: farm startup, its drive UUID file, health
reporting, and coordinate-based cleanup. Generated watchdog output is not a
second managed source. `pixel_runtime_config` is declared directly in host
variables, where shared storage paths reference the storage role's inputs.

This is a concrete Google Photos implementation, not an abstract uploader.
Boot/cleanup both know Google Photos, so they remain together. A different
upload mechanism should use a different role which meets a supported receiver
contract. The health URL is an explicit vaulted input at rendering time.

| Source | Invoked by | Purpose |
| --- | --- | --- |
| `farm-startup.sh` | Magisk late-start service | Wait for boot and drive, mount storage, launch Photos and workers |
| `my_drive_id.txt` | Startup reads it | Select the filesystem by UUID despite changing USB device names |
| `pixel_health.sh` | Startup, once per boot | Report canary, temperature, battery and free space; fail on missing canary or excess temperature |
| `reset_and_free.sh` | NAS manager | Wake/open Photos and tap Free up space using fixed coordinates |

Generated `watchdog_runner.sh` suppresses USB autosuspend, writes a small
keepalive only when the drive canary exists, and requests a reboot if its
selected block-device node vanishes. It does not establish that disk I/O is
healthy. Startup's kernel panic settings are a separate crash-recovery mechanism.

## Inputs and behavior

- `pixel_runtime_config`: storage paths, drive UUID, timing, health thresholds,
  kernel policy and UI coordinates; [argument schema](meta/argument_specs.yml).
- `pixel_runtime_healthcheck_url`: supplied from Vault; never printed. The running health worker contacts it.
- `pixel_runtime_adoption_sources`: reviewed fingerprints for all four sources.

`main` manages rendered sources, with the executable Magisk hook published
last. Standalone `main` plans then applies. The pipeline uses `plan` and `apply`
separately to inspect prerequisites once. `validate` renders locally. `audit`
checks the historical source baseline and installed mount/worker state. Source
changes require `pixel_runtime_maintenance_ready` from the play's observed
controller state. Matching files need no maintenance window or write.

`activate` is the standalone plan/start entrypoint. The activation play uses
its existing source plan and calls `start`; mount/worker state is checked again
after controller maintenance inspection. It
accepts a healthy farm unchanged, starts a completely stopped farm only with an
idle controller, and refuses partial mounts/duplicate workers. It establishes
missing canary/transfer directories only after verifying the external mount.
A reset/recovery exercise has not yet been performed with this code.

`tasks/prerequisites.yml` runs before the farm is installed, without a source
manifest or health secret. It checks the connected drive UUID/type, Photos
entry point and permissions, primary-user unlock, display/input profile, and
health/kernel interfaces. The additional screen/input fields in
`pixel_runtime_config` document assumptions of the captured cleanup script;
they do not recalibrate its taps or prove that a Photos menu matches.

Boot, health and UI sources are separate templates within one lifecycle role.
Only `farm-startup.sh` owns generated `watchdog_runner.sh`. Default management
installs sources without executing the hook. Activation starts Photos and the
health/watchdog loops, but never invokes the cleanup automation.

The boot hook invokes the storage helper, launches Photos, generates the
watchdog, sets panic policy and starts health reporting. The NAS separately
invokes cleanup; there is no phone cleanup timer. The canary is externally
prepared drive state: the guarded activation task can create it; the captured
boot script itself never does. Missing-drive boot
failure occurs before either worker starts.

See [pipeline setup and recovery](../../playbooks/PHOTO_BACKUP.md) for each
script's place in the boot sequence, prerequisites that need manual setup,
source deployment, explicit activation and remaining manual setup.
