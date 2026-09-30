# pixel_backup_gang

Packages the captured storage helpers from `master-hax/pixel-backup-gang`, including
the local mount-helper modifications. Do not replace them with current upstream.
`pbg_config` is the storage profile; it does not include NAS topology or host hashes.

The nine source templates preserve the existing helper bundle. The custom
`my_drive_id.txt` belongs to the farm runtime that reads it.

## Inputs and behavior

- `pbg_config`: `helpers_dir`, `drive_mount`, `bind_path`; see the typed
  [argument schema](meta/argument_specs.yml).
- `pbg_adoption_sources`: complete reviewed list of destination, template,
  SHA256, UID, GID and mode. Supplied by host inventory, never discovered or updated.

`tasks/main.yml` validates inputs and exact rendering, then reads source hashes,
metadata and Android/Magisk capabilities. `tasks/validate.yml` runs the same
preflight entirely on the controller. Neither entry point installs or executes
helpers. Missing sources, symlinks and drift fail without repair. Raw reads avoid
Python/module staging on Android and work with or without `--check`.

`vars/main.yml` describes package contents; it contains no installation hashes.
The small source probe is kept within this role so it can be transferred without
a hidden dependency on another role. Fresh provisioning is a separate future batch.
