"""Execute the mount helper with fake commands; never mount or alter a device."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]


class MountExt4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='photo-mount-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.calls = self.work / 'calls.jsonl'
        self.script = self.work / 'mount_ext4.sh'
        template = (ROOT / 'roles/pixel_backup_gang/templates/mount_ext4.sh.j2').read_text()
        self.script.write_text(Environment(undefined=StrictUndefined).from_string(template).render(
            pbg_config={'drive_mount': '/mnt/my_drive',
                        'bind_path': '/mnt/runtime/write/emulated/0/the_binding'}))
        # PATH contains only stubs, so a new external command fails safely.
        self.bin = self.work / 'bin'
        self.bin.mkdir()
        stub = self.work / 'command-stub'
        stub.write_text(f'#!{sys.executable}\n' + '''
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['MOUNT_TEST_CALLS'], 'a') as log:
    log.write(json.dumps([name] + args) + '\\n')
step = 'mount_' + args[1] if name == 'mount' else name
if step == os.environ.get('MOUNT_TEST_FAIL'):
    sys.exit(23)
if name == 'readlink':
    print('mnt:[1]')
elif name == 'stat':
    print('tmpfs')
''')
        stub.chmod(0o700)
        for name in ['readlink', 'stat', 'mkdir', 'mount', 'chmod', 'chown', 'setenforce', 'am']:
            (self.bin / name).symlink_to(stub)

    def run_helper(self, failure=''):
        self.calls.write_text('')
        result = subprocess.run(
            ['/bin/sh', str(self.script), '/dev/block/test-device'],
            env=dict(os.environ, PATH=str(self.bin), MOUNT_TEST_CALLS=str(self.calls),
                     MOUNT_TEST_FAIL=failure),
            capture_output=True, text=True, timeout=10)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return result, calls

    def test_failed_ext4_mount_stops_before_backing_directory_writes(self):
        result, calls = self.run_helper('mount_ext4')
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(calls[-1][0:3], ['mount', '-t', 'ext4'])
        # Creating the empty mountpoint is allowed; writing beneath it is not.
        self.assertEqual([c for c in calls if c[0] == 'mkdir'],
                         [['mkdir', '-p', '-v', '/mnt/my_drive']])
        self.assertFalse(any(c[0] in ['chmod', 'chown', 'setenforce', 'am'] for c in calls))
        self.assertNotIn('successfully', result.stdout)

    def test_later_failures_propagate_without_success_or_media_scan(self):
        for failure in ['chmod', 'chown', 'setenforce', 'mount_sdcardfs']:
            with self.subTest(failure=failure):
                result, calls = self.run_helper(failure)
                self.assertEqual(result.returncode, 23, result.stderr)
                last = calls[-1]
                self.assertEqual('mount_' + last[2] if last[0] == 'mount' else last[0], failure)
                self.assertFalse(any(c[0] == 'am' for c in calls))
                self.assertNotIn('successfully', result.stdout)

    def test_success_scopes_ownership_to_photo_subtree(self):
        result, calls = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c for c in calls if c[0] == 'chown'],
                         [['chown', '-R', 'sdcard_rw:sdcard_rw', '/mnt/my_drive/the_binding']])
        self.assertEqual([c[2] for c in calls if c[0] == 'mount'], ['ext4', 'sdcardfs'])
        self.assertEqual(calls[-1][0], 'am')
        self.assertIn('ext4 drive mounted successfully', result.stdout)


if __name__ == '__main__':
    unittest.main()
