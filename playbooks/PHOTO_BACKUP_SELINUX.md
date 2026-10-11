# Pixel SELinux migration plan

Status: planned, not applied. The current helper still calls `setenforce 0`.
This migration covers the existing USB/ext4 Pixel pipeline, with no OS, Magisk,
module or Photos upgrades. Normal source management must not toggle enforcement,
relabel a live disk, restart workers or reboot the phone.

## Observed baseline — 2026-09-30

- Pixel `sailfish`, Android build `QP1A.191005.007.A3`; SELinux `Permissive`.
- `/system/bin/chcon` exists; write behavior has not been tested.
- Backing directory `/mnt/my_drive/the_binding`: `u:object_r:unlabeled:s0`.
- Shared view `/sdcard/the_binding`, including `.staging` and `incoming`:
  `u:object_r:sdcardfs:s0`. The shared view is a different filesystem layer;
  its label need not equal the backing ext4 label.
- Runtime status currently requires permissive mode via `legacy_selinux_policy`.
- NAS transfers to `.staging`, then renames files into `incoming` on the same
  filesystem. Photos scanning exclusion and automated cleanup correctness are
  still unverified. Enforcing SELinux does not resolve either question.

Upstream [f4c648b](https://github.com/master-hax/pixel-backup-gang/commit/f4c648bf2bcdb1488da39949c702b3ac1a1261d6)
replaced global permissive mode with `media_rw_data_file` labels.
[f2c3df5](https://github.com/master-hax/pixel-backup-gang/commit/f2c3df5a7f95b639c07b4afeda95657fa8efbe7e)
then restricted relabeling and ownership changes to the photo subtree.
Our mount helper already incorporates the ownership restriction and explicit
`set -e`; preserve both during this migration.

## 1. Establish recovery and collect evidence

Before any enforcement change, verify physical recovery access through an
independent local root shell, such as USB ADB with Magisk authorization. Do not
assume ADB is enabled or that an existing SSH ControlMaster proves a new login
will work. Record a recovery procedure for this installed Magisk/SSH version.
Do not use remote SSH as the only recovery path.

Record the installed helper and boot-hook hashes, root/worker/Photos process
contexts, mount tables and relevant directory labels. Inspect kernel command
line, Magisk module policy and startup scripts for other permissive settings.
Report matched locations without dumping credentials, account names or entire
private scripts. No automatic module removal or policy edits.

Collect timestamped AVC denials from `dmesg` and available `logcat` buffers
during representative operation in permissive mode. Keep raw logs private and
outside Git. Interpret the process context, target label, object class and
operation for each relevant denial. Absence of logged denials is not sufficient
evidence: logging can be rate limited. Review policy changes individually;
do not install blanket rules generated from all denials. See
[Android SELinux validation](https://source.android.com/docs/security/features/selinux/validate).

Gate: recovery access is verified, intended root/Photos/worker contexts are
recorded, and competing permissive settings are understood. Global Enforcing
does not establish that every individual domain is enforcing; record Magisk's
privileged-domain exceptions and ensure Photos is not exempted as a workaround.

## 2. Prepare coordinated code changes

| File | Required change |
| --- | --- |
| `roles/pixel_backup_gang/templates/mount_ext4.sh.j2` | Replace `setenforce 0` with `chcon -R u:object_r:media_rw_data_file:s0 "$drive_mount_dir"/the_binding`. Preserve explicit error handling and subtree ownership. |
| `roles/pixel_backup_gang/files/prerequisites.sh` | Require `chcon` and `getenforce`; remove the routine helper dependency on `setenforce`. |
| `roles/pixel_google_photos_runtime/templates/checks/runtime-probe.sh.j2` | Replace `legacy_selinux_policy` with an Enforcing assertion; inspect backing labels separately from sdcardfs labels. |
| Runtime observation/activation tasks and role documentation | Update consumers of the renamed observation, diagnostics and recovery instructions. Locate every reference before editing. |
| `tests/photo_backup/test_mount_ext4.py` and configuration tests | Cover relabel failure, scoped relabeling, absence of routine permissive commands and the new status expectations. |

Before labeling, verify the unique intended UUID/device/ext4 mount in PID 1's
namespace, and that `the_binding` is a real directory on it, not a symlink or a
nested mount. The helper must verify the expected mount before post-mount writes;
add this check for direct boot-hook execution as well as explicit activation.
Refuse an already mounted/partially mounted layout rather than stack mounts.
Inspect the installed `chcon` recursion behavior before relying on it around
symlinks. Scope migration labeling to the photo subtree, never the whole drive.

Use named read-only assertions for required directory labels. Do not recursively
scan a large photo tree on every Ansible run. During the migration, verify
recursive labeling completed, sample existing media, and inspect newly received
files before and after promotion. If newly received files get unsuitable labels,
stop and design the creation/labeling fix from the observed denial; do not add
a recurring relabel loop or weaken policy to hide it.

Enforcing becomes the intended final runtime state. Keep temporary migration
steps explicit; avoid a permanent selector that silently accepts either mode.
Removing `setenforce 0` does not change the current boot's permissive state.
No routine management task should silently run `setenforce 1` to compensate.

## 3. Prepare the maintenance window

1. Disable the DSM Pixel Upload task manually and let the manager finish.
   Require the existing scheduler/process/lock maintenance checks to pass.
2. Confirm current photo data has an independent copy before testing cleanup.
   Prepare distinctive disposable photos/video with recorded checksums and
   independent originals. The existing Free up space action can affect all
   backed-up media, so an isolated test file does not bound its deletion scope.
3. Stop Photos and farm workers through a reviewed maintenance procedure after
   recording their exact identities. The current activation play does not
   provide a stop operation. Do not use broad process-name kills or unmount a
   drive with writers still running. Keep SSH/recovery access available.
4. Save the pre-migration sources and metadata outside Git if they contain
   secrets. Retain Vault encryption; no plaintext secret snapshots in the repo.
5. Measure subtree size/file count and allow time for recursive relabeling.
   Do not reuse the current activation timeout without checking this workload.

Gate: transfers and cleanup are quiescent, photo copies are safe, and a human
can recover locally. This is a scheduled runtime transition, not a no-op apply.

## 4. Label and test the running system

Deploy reviewed sources using the existing maintenance-gated management play.
For the already mounted disk, use a separately reviewed one-time labeling step
after the mount/source/path checks above; do not rerun the mount helper over the
live mounts. Capture initial labels before the operation. A failed relabel must
stop the migration and leave the scheduler disabled.

With physical recovery ready, switch to enforcement in the explicit maintenance
session and verify it. Test a NEW SSH connection with multiplexing disabled,
including the NAS admin's actual SSH/rsync path; an existing connection alone is
insufficient. Verify root commands needed for source management and observation.

Start the required processes once using a reviewed procedure. Do not invoke
`farm-startup.sh` against an already mounted farm; it tries to mount again.
Inspect new AVC denials while exercising:

- A disposable transfer into `.staging`, label inspection there, rename into
  `incoming`, and label/content checks after the rename.
- Photos recognizing and reading the media, successful cloud backup and an
  independent content check. Local disappearance is not a cloud receipt.
- Photos deleting only confirmed backed-up media through reviewed UI behavior.
  Verify partial staging content is excluded; keep unattended operation paused
  if this existing assumption cannot be established.
- Watchdog heartbeat on the external disk, USB power controls, health reporting
  and the actual cleanup input events used by this installation.

Gate: no unresolved denials affecting required behavior; intended backing
labels and Enforcing mode hold; transfer/backup/deletion and fresh SSH work.
Check mode cannot establish these properties.

## 5. Verify boot persistence and resume

With the NAS schedule still disabled and recovery access present, perform one
controlled reboot. Confirm boot completes, SELinux stays Enforcing, the intended
disk is mounted exactly once, Photos sees the mount, and exactly one watchdog
and health worker run. Confirm new SSH connections and health reports again.
Run status and management check mode; expect healthy state and zero source drift.
Repeat a disposable transfer after boot to confirm creation labels persist.

Exercise missing-drive/mount-failure handling in the local command harness or a
spare setup. Do not unplug live production storage to simulate failure.
Resume the DSM schedule only when all gates pass and the operator has reviewed
the cleanup/scanning results. Observe at least one complete scheduled cycle.

## Rollback

Keep the DSM schedule disabled throughout rollback. If enforcement breaks
required behavior, use the verified recovery shell to return to permissive
mode (`setenforce 0`), then verify access and retain relevant denial evidence.
If SSH is lost, use physical recovery; repeated remote retries are not recovery.

Restore the pre-migration helper and matching runtime checks using the saved
version, retaining the simple fail-fast and scoped-ownership fixes. A live
permissive switch alone is not a persistent rollback because the new helper no
longer requests permissive mode on boot. Diagnose any partial mounts before a
controlled restart; the activation play deliberately refuses partial layouts.

Do not recursively reset labels to `unlabeled`, run blanket `restorecon`, change
owners on unrelated disk contents, or reformat the disk. Valid media labels can
remain while permissive mode is restored. Any need to restore individual labels
must use the captured metadata and a reviewed path-specific procedure.
Re-establish the previous healthy mount/worker/transfer state before resuming
the schedule. Record any failed acceptance gate; do not mark migration complete.
