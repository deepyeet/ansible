# pixel_backup_gang

Packages storage helpers from `master-hax/pixel-backup-gang`. The captured nine
helpers match upstream tag `0.0.8` when rendered with the original paths; the
original installation revision is unknown. Changes are adopted selectively.
`pbg_config` is the storage profile; it does not include NAS topology or host hashes.

These are mount helpers, not an uploader or a Magisk module. On this phone,
the separate farm boot hook invokes `mount_ext4.sh` after Android boots. It
mounts ext4 at `drive_mount`, then exposes the drive's `the_binding` subtree
through Android 10's sdcardfs at `bind_path`. Google Photos sees shared storage.
The NAS uploads into that exposed subtree.

The mount helper has explicit `set -e` so failures propagate even when invoked
through `sh`, and scopes recursive ownership changes to `the_binding` (as in
upstream commit `f2c3df5`). A failed mount leaves any already completed steps in
place for diagnosis; the helper does not attempt automatic unmount or rollback.
SELinux remains permissive pending the [migration plan](../../playbooks/PHOTO_BACKUP_SELINUX.md).
The custom `my_drive_id.txt` belongs to the farm runtime that reads it.

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
- `pbg_files`: complete desired list of destination, template, UID, GID and mode.
  Supplied explicitly by the play from host inventory.

`main` manages the helper directory and rendered source files through the
repository's Python-free `android_file` action. `plan` predicts those changes
without writes; `apply` consumes that plan. The pipeline uses `plan` then `apply`
once; standalone `main` performs both. `validate` checks inputs and rendering
locally. Missing files are installed in management mode; symlinks fail closed.

Changing sources requires `pbg_maintenance_ready`, supplied by the play after
observing the paused/idle controller. Check mode and matching resources need no
maintenance window. This role never mounts storage or executes helpers.
The repository action plugin is required when transferring this role.

`tasks/prerequisites.yml` can run before helpers exist. Its named assertions
explain the root/global-namespace requirement, ext4/sdcardfs kernel support,
Magisk BusyBox, persistent SSH authorization, module autostart and commands.
It reads key-file metadata, never key contents. It does not establish that the
NAS's particular key is authorized.

See [pipeline setup and recovery](../../playbooks/PHOTO_BACKUP.md) for the
standalone prerequisite command, boot sequence, manual setup, source management and separate activation.
