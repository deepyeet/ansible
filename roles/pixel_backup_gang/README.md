# pixel_backup_gang

Packages the captured storage helpers from `master-hax/pixel-backup-gang`, including
the local mount-helper modifications. Do not replace them with current upstream.
`pbg_config` is the storage profile; it does not include NAS topology or host hashes.

These are mount helpers, not an uploader or a Magisk module. On this phone,
the separate farm boot hook invokes `mount_ext4.sh` after Android boots. It
mounts ext4 at `drive_mount`, then exposes the drive's `the_binding` subtree
through Android 10's sdcardfs at `bind_path`. Google Photos sees shared storage.
The NAS uploads into that exposed subtree.

The nine source templates preserve the existing helper bundle. The custom
`my_drive_id.txt` belongs to the farm runtime that reads it.

| Helper | Place in this installation |
| --- | --- |
| `mount_ext4.sh` | Called by farm startup; mounts the disk and its Android binding |
| `start_global_shell.sh` | Manual namespace helper; unnecessary for the observed global SSH shell |
| `find_device.sh`, `show_devices.sh` | Manual discovery helpers; startup does its own live UUID lookup |
| `remount_vfat.sh`, `unmount.sh` | Alternative/manual storage operations; outside the ext4 boot path |
| `run_as_termux.sh` | Optional Termux integration; unused by the farm |
| `enable_tcp_debugging.sh`, `disable_tcp_debugging.sh` | Optional ADB maintenance; unrelated to Magisk SSH |

## Inputs and behavior

- `pbg_config`: `helpers_dir`, `drive_mount`, `bind_path`; see the typed
  [argument schema](meta/argument_specs.yml).
- `pbg_adoption_sources`: complete reviewed list of destination, template,
  SHA256, UID, GID and mode. Supplied by host inventory, never discovered or updated.

`tasks/main.yml` validates inputs and exact rendering, checks Android/Magisk
prerequisites, then reads source hashes and metadata. `tasks/validate.yml` runs
source preflight entirely on the controller. Neither entry point installs or executes
helpers. Missing sources, symlinks and drift fail without repair. Raw reads avoid
Python/module staging on Android and work with or without `--check`.

`vars/main.yml` describes package contents; it contains no installation hashes.
The small source probe is kept within this role so it can be transferred without
a hidden dependency on another role. Fresh provisioning is a separate future batch.

`tasks/prerequisites.yml` can run before helpers exist. Its named assertions
explain the root/global-namespace requirement, ext4/sdcardfs kernel support,
Magisk BusyBox, persistent SSH authorization, module autostart and commands.
It reads key-file metadata, never key contents. It does not establish that the
NAS's particular key is authorized.

See [pipeline setup and recovery](../../playbooks/PHOTO_BACKUP.md) for the
standalone prerequisite command, boot sequence, manual setup and missing bootstrap.
