# pixel_google_photos_runtime

Owns the four local custom sources: farm startup, its drive UUID file, health
reporting, and coordinate-based cleanup. Generated watchdog output is not a
second managed source. `pixel_runtime_config` is declared directly in host
variables, where shared storage paths reference the storage role's inputs.

This is a concrete Google Photos implementation, not an abstract uploader.
Boot/cleanup both know Google Photos, so they remain together. A different
upload mechanism should use a different role which meets a supported receiver
contract. The health URL is an explicit vaulted input at rendering time.

## Inputs and behavior

- `pixel_runtime_config`: storage paths, drive UUID, timing, health thresholds,
  kernel policy and UI coordinates; [argument schema](meta/argument_specs.yml).
- `pixel_runtime_healthcheck_url`: supplied from Vault, never printed or contacted.
- `pixel_runtime_adoption_sources`: reviewed fingerprints for all four sources.

`tasks/main.yml` validates rendering and reads source fingerprints, the mount
canary, global mount table and worker processes. `tasks/validate.yml` is entirely
local. Both are read-only; source drift fails without repair. Runtime observations
are reported separately from source agreement and cloud backup remains unverified.

Boot, health and UI sources are separate templates within one lifecycle role.
Only `farm-startup.sh` owns generated `watchdog_runner.sh`; adoption never writes
or executes either. No reboot, mount, app action, health ping or bootstrap path
exists in this role's default graph.
