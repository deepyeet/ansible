"""Manage small text files/directories over Magisk SSH without remote Python.

The action never stages a module. Inspection and check mode only read. File
payloads travel on SSH stdin, never in argv, and are published by atomic rename.
Use no_log on tasks whose content contains Vault values. No diff is returned.
"""
import hashlib
import posixpath
import re
import shlex

from ansible.errors import AnsibleActionFail
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):
    TRANSFERS_FILES = False
    _supports_check_mode = True
    _supports_async = False

    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        args = self._task.args
        if set(args) - {'path', 'state', 'content', 'owner', 'group', 'mode', 'allow_changes'}:
            raise AnsibleActionFail('Unsupported android_file argument')
        if self._connection.transport != 'ssh' or self._play_context.become:
            raise AnsibleActionFail('android_file requires direct root SSH without become')
        path = args.get('path', '')
        if (not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', path)
                or posixpath.normpath(path) != path or path.endswith('/')
                or not path.startswith(('/data/local/tmp/', '/data/adb/service.d'))):
            raise AnsibleActionFail('Only canonical Android farm source paths are supported')
        # Do not let the service.d prefix accidentally include sibling directories.
        if path.startswith('/data/adb/') and path != '/data/adb/service.d' and not path.startswith('/data/adb/service.d/'):
            raise AnsibleActionFail('Only service.d is managed under /data/adb')
        state = args.get('state', 'file')
        if state not in ('file', 'directory'):
            raise AnsibleActionFail('state must be file or directory')
        values = [str(args.get(k, '')) for k in ('owner', 'group', 'mode')]
        uid, gid, mode = values
        if not all(re.fullmatch(r'[0-9]+', v) for v in (uid, gid)) or not re.fullmatch(r'0?[0-7]{3,4}', mode):
            raise AnsibleActionFail('Explicit numeric owner, group and octal mode are required')
        mode = format(int(mode, 8), 'o')
        if int(mode, 8) > 0o777:
            raise AnsibleActionFail('Special permission bits are not supported')
        content = args.get('content')
        if state == 'file' and not isinstance(content, str):
            raise AnsibleActionFail('File content must be rendered UTF-8 text')
        if state == 'directory' and content is not None:
            raise AnsibleActionFail('Directories do not accept content')
        payload = content.encode('utf-8') if state == 'file' else b''
        if len(payload) > 65536:
            raise AnsibleActionFail('android_file supports source files up to 64 KiB')
        digest = hashlib.sha256(payload).hexdigest()
        expected = f'{uid}|{gid}|{mode}'
        preamble = f'''set -eu
[ "$(id -u)" = 0 ] || exit 40
p={shlex.quote(path)}
parent=${{p%/*}}
# Existing ancestors must be real directories, including /data and /data/adb.
a="$parent"
while [ -n "$a" ] && [ "$a" != / ]; do
  [ ! -L "$a" ] || exit 41
  if [ -e "$a" ]; then [ -d "$a" ] || exit 41; fi
  a=${{a%/*}}
done
snapshot() {{
  [ ! -L "$p" ] || return 42
  if [ ! -e "$p" ]; then echo absent; return; fi
  [ -{'f' if state == 'file' else 'd'} "$p" ] || return 42
  {'h=$(sha256sum "$p"); printf "%s|" "${h%% *}"' if state == 'file' else ':'}
  stat -c '%u|%g|%a' "$p"
}}
'''
        before = self._low_level_execute_command(preamble + 'snapshot', executable='/system/bin/sh', sudoable=False)
        if before['rc']:
            raise AnsibleActionFail('Cannot inspect destination: check root, ancestor paths and file type')
        observed = before['stdout'].strip()
        desired = f'{digest}|{expected}' if state == 'file' else expected
        changed = observed != desired
        result.update(changed=changed, path=path, state=state)
        if not changed or self._task.check_mode:
            return result
        # Planning and applying are separate pipeline phases. A file that starts
        # drifting after an unchanged plan must not bypass the maintenance gate.
        if not boolean(args.get('allow_changes', True), strict=True):
            raise AnsibleActionFail('Source changed outside the observed maintenance window; no write performed')

        # Recheck before writing. This catches concurrent Ansible edits, but the
        # destination directory must still be trusted against hostile root edits.
        script = preamble + f'''
[ "$(snapshot)" = {shlex.quote(observed)} ] || exit 43
[ -d "$parent" ] || exit 44
'''
        if state == 'directory':
            script += f'''
if [ ! -e "$p" ]; then mkdir "$p"; fi
chown {uid}:{gid} "$p"
chmod {mode} "$p"
'''
        else:
            script += f'''
umask 077
t=$(mktemp -d "$parent/.ansible-photo.XXXXXXXX")
trap 'rm -f "$t/payload"; rmdir "$t"' EXIT
trap 'exit 1' HUP INT TERM
{"cat" if payload else ":"} > "$t/payload"
h=$(sha256sum "$t/payload")
[ "${{h%% *}}" = {digest} ] || exit 45
chown {uid}:{gid} "$t/payload"
chmod {mode} "$t/payload"
[ "$(snapshot)" = {shlex.quote(observed)} ] || exit 43
/data/adb/magisk/busybox mv -fT "$t/payload" "$p"
'''
        script += f'\n[ "$(snapshot)" = {shlex.quote(desired)} ] || exit 46\n'
        after = self._low_level_execute_command(script, executable='/system/bin/sh',
                                                in_data=payload if payload else None,
                                                sudoable=False)
        if after['rc']:
            result.update(failed=True, msg='Android file installation failed; inspect destination before retrying', rc=after['rc'])
        return result
