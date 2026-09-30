#!/bin/sh
# Read capabilities only; none of the helpers are invoked by adoption.
set -eu
[ "$(id -u)" = 0 ] || exit 40
[ "$(getprop ro.build.version.release)" = 10 ] || exit 41
for x in sha256sum stat readlink blkid rsync curl nohup mount am input sendevent; do
  command -v "$x" >/dev/null || exit 42
done
[ -f /data/adb/modules/ssh/module.prop ] || exit 43
echo PREREQUISITES_OK
