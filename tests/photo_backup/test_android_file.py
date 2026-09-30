"""Exercise the actual Android file action on disposable local files, never SSH."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('ANSIBLE_LOCAL_TEMP', '/tmp/codex-photo-test-ansible')
from ansible.errors import AnsibleActionFail

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('android_file_action', ROOT / 'action_plugins/android_file.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AndroidFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='android-file-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.parent = self.root / 'data/local/tmp/farm'
        self.parent.mkdir(parents=True)
        self.dest = self.parent / 'health.sh'
        self.calls = []

    def run_action(self, content='fixture\n', check=False, allow=True, state='file', before_write=None):
        action = object.__new__(module.ActionModule)
        action._task = SimpleNamespace(args={
            'path': '/data/local/tmp/farm/health.sh', 'state': state,
            'owner': str(os.getuid()), 'group': str(os.getgid()),
            'mode': '0600', 'allow_changes': allow,
            **({'content': content} if state == 'file' else {}),
        }, check_mode=check)
        action._connection = SimpleNamespace(transport='ssh')
        action._play_context = SimpleNamespace(become=False)

        def execute(command, **kwargs):
            self.calls.append((command, kwargs.get('in_data')))
            if len(self.calls) == 2 and before_write:
                before_write()
            # Execute the action's generated shell unchanged apart from these
            # platform substitutions: a virtual /data tree, local mv and UID.
            command = command.replace('/data/adb/magisk/busybox mv -fT', '/bin/mv -fT')
            command = command.replace('/data/', str(self.root / 'data') + '/')
            command = command.replace('[ "$(id -u)" = 0 ]', f'[ "$(id -u)" = {os.getuid()} ]')
            result = subprocess.run(['/bin/sh', '-c', command], input=kwargs.get('in_data'),
                                    capture_output=True, timeout=5)
            return dict(rc=result.returncode, stdout=result.stdout.decode(), stderr=result.stderr.decode())

        action._low_level_execute_command = execute
        with patch('ansible.plugins.action.ActionBase.run', return_value={}):
            return action.run(task_vars={})

    def test_matching_file_is_untouched_without_maintenance(self):
        self.dest.write_text('fixture\n')
        self.dest.chmod(0o600)
        before = self.dest.stat()
        result = self.run_action(allow=False)
        after = self.dest.stat()
        self.assertFalse(result['changed'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((before.st_ino, before.st_mtime_ns, before.st_ctime_ns),
                         (after.st_ino, after.st_mtime_ns, after.st_ctime_ns))
        self.assertEqual(list(self.parent.iterdir()), [self.dest])

    def test_missing_file_check_mode_reports_change_without_staging(self):
        result = self.run_action(check=True, allow=False)
        self.assertTrue(result['changed'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_unplanned_drift_cannot_write_outside_maintenance(self):
        self.dest.write_text('external change\n')
        self.dest.chmod(0o600)
        before = self.dest.stat()
        with self.assertRaises(AnsibleActionFail):
            self.run_action(allow=False)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.dest.read_text(), 'external change\n')
        self.assertEqual(self.dest.stat().st_ctime_ns, before.st_ctime_ns)

    def test_authorized_install_and_update_publish_exact_bytes_atomically(self):
        for content in ['first payload\n', 'replacement\n']:
            old_inode = self.dest.stat().st_ino if self.dest.exists() else None
            result = self.run_action(content=content)
            self.assertTrue(result['changed'])
            self.assertFalse(result.get('failed', False))
            self.assertEqual(self.dest.read_text(), content)
            self.assertEqual(self.dest.stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(self.dest.stat().st_ino, old_inode)
            self.assertEqual(list(self.parent.iterdir()), [self.dest])
            self.calls.clear()
        # The payload travels through stdin; it must not appear in command argv.
        self.run_action(content='separate stdin payload\n')
        self.assertNotIn('separate stdin payload', self.calls[-1][0])
        self.assertEqual(self.calls[-1][1], b'separate stdin payload\n')

    def test_empty_file_install_does_not_wait_for_stdin(self):
        result = self.run_action(content='')
        self.assertFalse(result.get('failed', False))
        self.assertEqual(self.dest.read_bytes(), b'')

    def test_symlink_destination_is_rejected_without_touching_target(self):
        target = self.parent / 'other'
        target.write_text('preserve\n')
        self.dest.symlink_to(target)
        with self.assertRaises(AnsibleActionFail):
            self.run_action()
        self.assertEqual(target.read_text(), 'preserve\n')
        self.assertTrue(self.dest.is_symlink())

    def test_change_between_inspection_and_write_aborts(self):
        self.dest.write_text('old\n')
        result = self.run_action(before_write=lambda: self.dest.write_text('concurrent\n'))
        self.assertTrue(result['failed'])
        self.assertEqual(result['rc'], 43)
        self.assertEqual(self.dest.read_text(), 'concurrent\n')
        self.assertEqual(list(self.parent.iterdir()), [self.dest])


if __name__ == '__main__':
    unittest.main()
