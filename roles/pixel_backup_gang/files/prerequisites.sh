#!/bin/sh
# Emit only named booleans. Ansible owns the assertions and recovery guidance.
# No helper execution, mount changes, module installation or file staging.
set -eu
emit() {
    key=$1; shift
    printf '%s: ' "$key"
    if "$@" >/dev/null 2>&1; then echo true; else echo false; fi
}
emit root_shell test "$(id -u)" = 0
emit android_10 test "$(getprop ro.build.version.release)" = 10
case "$(getprop ro.product.device)" in
    sailfish|marlin) echo 'original_pixel: true' ;;
    *) echo 'original_pixel: false' ;;
esac
emit boot_completed test "$(getprop sys.boot_completed)" = 1
emit global_mount_namespace test "$(readlink /proc/self/ns/mnt)" = "$(readlink /proc/1/ns/mnt)"
emit ext4_supported grep -qw ext4 /proc/filesystems
emit sdcardfs_supported grep -qw sdcardfs /proc/filesystems
# /sbin is absent from this installation's SSH PATH. Do not use `magisk` alone.
emit magisk_available /sbin/magisk -v
emit magisk_busybox_available /data/adb/magisk/busybox true
emit ssh_module_installed test -f /data/adb/modules/ssh/module.prop
emit ssh_module_enabled test ! -e /data/adb/modules/ssh/disable
emit ssh_module_kept test ! -e /data/adb/modules/ssh/remove
emit ssh_overlay_enabled test ! -e /data/adb/modules/ssh/skip_mount
emit ssh_boot_service_present test -f /data/adb/modules/ssh/service.sh
emit ssh_autostart_enabled test ! -e /data/ssh/no-autostart
emit ssh_authorized_keys_present test -s /data/ssh/root/.ssh/authorized_keys
emit ssh_authorized_keys_private test "$(stat -c '%u:%g:%a' /data/ssh/root/.ssh/authorized_keys 2>/dev/null || true)" = 0:0:600
emit ssh_binary_available test -x /system/bin/sshd
emit rsync_binary_available test -x /system/bin/rsync
for x in sha256sum stat readlink blkid mount nsenter mkdir chmod chown setenforce am; do
    emit "command_$x" command -v "$x"
done
