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
- `pixel_runtime_healthcheck_url`: supplied from Vault, never printed or contacted.
- `pixel_runtime_adoption_sources`: reviewed fingerprints for all four sources.

`tasks/main.yml` validates rendering, checks platform prerequisites and source
fingerprints, then asserts the actual ext4/sdcardfs mount chain, canary identity,
same-filesystem staging, Photos-process mount visibility and singleton workers.
`tasks/validate.yml` is entirely local. Both are read-only; drift fails without
repair. Passing these checks still does not establish cloud backup.

`tasks/prerequisites.yml` runs before the farm is installed, without a source
manifest or health secret. It checks the connected drive UUID/type, Photos
entry point and permissions, primary-user unlock, display/input profile, and
health/kernel interfaces. The additional screen/input fields in
`pixel_runtime_config` document assumptions of the captured cleanup script;
they do not recalibrate its taps or prove that a Photos menu matches.

Boot, health and UI sources are separate templates within one lifecycle role.
Only `farm-startup.sh` owns generated `watchdog_runner.sh`; adoption never writes
or executes either. No reboot, mount, app action, health ping or bootstrap path
exists in this role's default graph.

The boot hook invokes the storage helper, launches Photos, generates the
watchdog, sets panic policy and starts health reporting. The NAS separately
invokes cleanup; there is no phone cleanup timer. The canary is externally
prepared drive state: none of these scripts creates it. Missing-drive boot
failure occurs before either worker starts.

See [pipeline setup and recovery](../../playbooks/PHOTO_BACKUP.md) for each
script's place in the boot sequence, prerequisites that need manual setup,
and the remaining work before a reset phone can be rebuilt.
