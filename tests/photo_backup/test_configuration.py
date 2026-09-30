"""Configuration and read-only status contracts; live SSH is never used."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
# Ansible's controller scratch must stay outside the repository and SSH homes.
os.environ.setdefault('ANSIBLE_LOCAL_TEMP', '/tmp/codex-photo-test-ansible')
from ansible.parsing.dataloader import DataLoader
from ansible.parsing.vault import VaultLib, VaultSecret
from ansible.template import Templar

ROLES = {
    'pixel_backup_gang': 'pbg',
    'pixel_google_photos_runtime': 'pixel_runtime',
    'photo_ingest_controller': 'photo_ingest',
}


def read_yaml(path):
    return yaml.safe_load((ROOT / path).read_text())


def render(path, variables):
    """Use installed Ansible templating for the actual read-only probe source."""
    return Templar(loader=DataLoader(), variables=variables).template(
        (ROOT / path).read_text(), preserve_trailing_newlines=True)


class LocalPlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='photo-config-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.calls = self.work / 'ssh-calls'
        stub = self.work / 'ssh-unavailable'
        stub.write_text('#!/bin/sh\nprintf "UNEXPECTED_SSH\\n" >> "$PHOTO_TEST_SSH_CALLS"\nexit 78\n')
        stub.chmod(0o700)
        self.env = dict(os.environ, ANSIBLE_CONFIG=str(ROOT / 'ansible.cfg'),
                        ANSIBLE_SSH_EXECUTABLE=str(stub),
                        PHOTO_TEST_SSH_CALLS=str(self.calls), ANSIBLE_NOCOLOR='1',
                        ANSIBLE_DISPLAY_ARGS_TO_STDOUT='false')
        self.env.pop('ANSIBLE_LOG_PATH', None)

    def run_play(self, extras=None, limit=None, play='playbooks/photo-backup-validate.yml',
                 extra_args=(), inventory=None):
        args = ['ansible-playbook', play]
        if extras is not None:
            path = self.work / 'extra.yml'
            path.write_text(yaml.safe_dump(extras))
            args.extend(['-e', '@' + str(path)])
        if inventory is not None:
            path = self.work / 'inventory.yml'
            path.write_text(yaml.safe_dump(inventory))
            args.extend(['-i', str(path)])
        if limit:
            args.extend(['--limit', limit])
        result = subprocess.run(args + list(extra_args), cwd=ROOT, env=self.env,
                                capture_output=True, text=True, timeout=60)
        self.assertFalse(self.calls.exists(), 'A local/negative case attempted SSH')
        return result

    def rejected(self, extras, limit=None):
        result = self.run_play(extras, limit=limit)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('failed=1', result.stdout)

    def test_current_files_render_without_ssh(self):
        result = self.run_play(play='playbooks/photo-backup-validate.yml')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('changed=0'), 2)
        vault = VaultLib([(None, VaultSecret((ROOT / '.vault_pass').read_bytes().strip()))])
        # Ansible reports absolute controller task paths. A local checkout may
        # happen to live under the same account name as the vaulted SSH login.
        reported = (result.stdout + result.stderr).replace(str(ROOT), '<repository>')
        for path in ['group_vars/all/vault.yml', 'group_vars/photo_receivers/vault.yml',
                     'group_vars/photo_controllers/vault.yml']:
            values = yaml.safe_load(vault.decrypt((ROOT / path).read_bytes()))
            for value in values.values():
                self.assertTrue(not value or value not in reported,
                                'Vault value appeared in local validation output')

    def test_inventory_become_override_fails_before_ssh(self):
        self.rejected({'ansible_become': True})

    def test_receiver_parameter_edits_are_valid_desired_configuration(self):
        config = read_yaml('host_vars/pixel1/vars.yml')
        config['pixel_runtime_config']['health_interval'] += 1
        result = self.run_play(config)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_controller_parameter_edits_are_valid_desired_configuration(self):
        config = read_yaml('host_vars/ds223j/vars.yml')
        config['photo_ingest']['ingest_age'] += 1
        result = self.run_play(config)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_absent_or_incomplete_file_definitions_fail_before_ssh(self):
        self.rejected({'photo_managed_files': {}}, limit='pixel1')
        baseline = read_yaml('host_vars/pixel1/files.yml')
        baseline['photo_managed_files']['pixel_backup_gang'].pop()
        self.rejected(baseline, limit='pixel1')

    def test_unsafe_or_undeclared_manifest_paths_fail_before_ssh(self):
        for path in ['/tmp/other.sh', '/data/local/tmp/../unexpected',
                     '/data/local/tmp/bad;command']:
            with self.subTest(path=path):
                baseline = read_yaml('host_vars/pixel1/files.yml')
                baseline['photo_managed_files']['pixel_backup_gang'][0]['dest'] = path
                self.rejected(baseline, limit='pixel1')

    def test_missing_peer_fails_with_receiver_only_limit(self):
        self.rejected({'photo_pipelines': {'family_photos': {
            'controller': 'ds223j', 'receiver': 'missing'}}}, limit='pixel1')

    def test_controller_only_limit_checks_pixel_contract(self):
        config = read_yaml('host_vars/pixel1/vars.yml')
        config['photo_receiver']['root'] = '/wrong/root'
        self.rejected(config, limit='ds223j')

    def test_receiver_only_limit_checks_cleanup_coherence(self):
        config = read_yaml('host_vars/pixel1/vars.yml')
        config['photo_receiver']['cleanup_argv'] = ['/other/cleanup']
        self.rejected(config, limit='pixel1')

    def test_incompatible_protocol_or_completion_fail_before_ssh(self):
        for key, value in [('protocol', 'http_api'), ('completion', 'cloud_receipt')]:
            config = read_yaml('host_vars/pixel1/vars.yml')
            config['photo_receiver'][key] = value
            self.rejected(config)

    def test_pipeline_name_can_change_without_a_host_backreference(self):
        result = self.run_play({'photo_pipelines': {
            'renamed_pipeline': {'controller': 'ds223j', 'receiver': 'pixel1'}}},
            play='playbooks/photo-backup-validate.yml')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_controller_cannot_belong_to_two_pipelines(self):
        result = self.run_play({'photo_pipelines': {
            'first': {'controller': 'ds223j', 'receiver': 'pixel1'},
            'second': {'controller': 'ds223j', 'receiver': 'another_phone'}}})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("map(attribute='controller') | unique", result.stdout)

    def test_direct_runtime_inputs_must_agree_with_storage(self):
        for field, value in [('scripts_dir', '/data/local/tmp/other'),
                             ('drive_mount', '/mnt/other')]:
            with self.subTest(field=field):
                config = read_yaml('host_vars/pixel1/vars.yml')
                config['pixel_runtime_config'][field] = value
                self.rejected(config, limit='ds223j')

    def test_shared_receiver_and_undeclared_controller_are_rejected(self):
        for add_pipeline in [False, True]:
            inventory = read_yaml('inventory.yml')
            # The temporary inventory has no adjacent group_vars/host_vars.
            # Supply nonsecret declarations so this exercises topology itself.
            photo_group = inventory['all']['children']['photo_backup']
            photo_group['vars'] = read_yaml('group_vars/photo_backup/vars.yml')
            pixel_host = photo_group['children']['photo_receivers']['hosts']['pixel1']
            pixel_host.update(read_yaml('host_vars/pixel1/vars.yml'))
            pixel_host.update(read_yaml('host_vars/pixel1/files.yml'))
            group = inventory['all']['children']['photo_backup']['children']['photo_controllers']
            group['hosts']['another_nas'] = {'ansible_host': '192.0.2.10'}
            extras = None
            if add_pipeline:
                extras = {'photo_pipelines': {
                    'family_photos': {'controller': 'ds223j', 'receiver': 'pixel1'},
                    'another_pipeline': {'controller': 'another_nas', 'receiver': 'pixel1'}}}
            result = self.run_play(extras, limit='pixel1', inventory=inventory)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('Require one controller and receiver per pipeline', result.stdout)
            self.assertIn('evaluated_to', result.stdout)

    def test_missing_secret_fails_locally_and_suppresses_value(self):
        result = self.run_play({'vault_pixel_backup_healthcheck_url': ''}, limit='pixel1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('censored', result.stdout)

    def test_site_limit_excludes_existing_media_and_wsl_hosts(self):
        result = self.run_play(play='site.yml', limit='photo_backup', extra_args=['--list-hosts'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('pi4_2020', result.stdout)
        self.assertNotIn('jujupc', result.stdout)
        self.assertIn('pixel1', result.stdout)
        self.assertIn('ds223j', result.stdout)

    def test_alternate_endpoint_reuses_actual_controller_template(self):
        vars_ = {'photo_ingest': {
                     'base_dir': '/srv/photos', 'state_dir': '/srv/backup',
                     'ssh_key_path': '/srv/backup/key', 'ingest_age': 4,
                     'ingest_min_date': '2020-01-01', 'rsync_timeout': 60,
                     'cleanup_interval': 28800},
                 'photo_ingest_endpoint': {
                     'protocol': 'rsync_drop_v1', 'address': '192.0.2.20',
                     'root': '/media/photo_drop', 'ssh_user': 'backup', 'ssh_port': 2222,
                     'completion': 'local_absence_after_cleanup',
                     'cleanup_argv': ['/opt/uploader/cleanup']},
                 'photo_ingest_healthcheck_url': 'https://example.invalid/fixture-health'}
        source = render('roles/photo_ingest_controller/templates/pixel_manager.sh.j2', vars_)
        for text in ['PIXEL_IP="192.0.2.20"', 'PIXEL_ROOT="/media/photo_drop"',
                     'backup@$PIXEL_IP', 'ssh -p 2222', '$SSH_CMD "/opt/uploader/cleanup"']:
            self.assertIn(text, source)
        self.assertNotIn('root@$PIXEL_IP', source)
        self.assertNotIn('reset_and_free.sh', source)


class RuntimeProbeTests(unittest.TestCase):
    def test_runtime_and_scheduler_probe_shell_syntax(self):
        probes = [
            ('roles/pixel_google_photos_runtime/templates/checks/runtime-probe.sh.j2',
             read_yaml('host_vars/pixel1/vars.yml')),
            ('playbooks/templates/photo-backup-synology-probe.sh.j2', read_yaml('host_vars/ds223j/vars.yml')),
        ]
        for path, variables in probes:
            result = subprocess.run(['/bin/sh', '-n'], input=render(path, variables),
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)


class RepositoryContractTests(unittest.TestCase):
    def test_local_and_plan_entrypoints_have_identical_role_inputs(self):
        for component in ['receiver', 'controller']:
            live = read_yaml(f'playbooks/tasks/photo-backup-{component}-plan.yml')
            local = read_yaml(f'playbooks/tasks/photo-backup-{component}-validate.yml')
            self.assertEqual(len(live), len(local))
            for before, after in zip(live, local):
                if 'ansible.builtin.include_role' in before:
                    self.assertEqual(before['ansible.builtin.include_role']['tasks_from'], 'plan')
                    self.assertEqual(after['ansible.builtin.include_role']['tasks_from'], 'validate')
                    self.assertEqual(before['vars'], after['vars'])

    def test_status_graph_has_only_controller_actions_and_readonly_raw(self):
        allowed = {'assert', 'debug', 'include_role', 'include_tasks',
                   'import_tasks', 'import_playbook', 'raw'}
        visited = set()

        def inspect(path):
            path = path.resolve()
            if path in visited:
                return
            visited.add(path)
            walk(yaml.safe_load(path.read_text()), path)

        def walk(node, path):
            if isinstance(node, list):
                for value in node:
                    walk(value, path)
            elif isinstance(node, dict):
                for key, value in node.items():
                    if key.startswith('ansible.builtin.'):
                        action = key.split('.')[-1]
                        self.assertIn(action, allowed, str(path.relative_to(ROOT)))
                        if action == 'raw':
                            self.assertIs(node['changed_when'], False)
                            self.assertIs(node['check_mode'], False)
                            self.assertIs(node['become'], False)
                        elif action in ['import_tasks', 'include_tasks', 'import_playbook']:
                            inspect(path.parent / value)
                        elif action == 'include_role':
                            inspect(ROOT / 'roles' / value['name'] / 'tasks' /
                                    (value.get('tasks_from', 'main') + '.yml'))
                    walk(value, path)

        inspect(ROOT / 'playbooks/photo-backup-status.yml')
        for path in (ROOT / 'playbooks').glob('photo-backup*.yml'):
            for play in yaml.safe_load(path.read_text()):
                if 'hosts' in play:
                    self.assertIs(play['gather_facts'], False)
                    self.assertIs(play['become'], False)

    def test_observation_shell_contains_no_mutation_or_network_commands(self):
        paths = list((ROOT / 'playbooks/templates').glob('*probe*'))
        for role in ROLES:
            paths.extend((ROOT / 'roles' / role / 'templates/checks').glob('*.j2'))
            paths.extend((ROOT / 'roles' / role / 'files').glob('*.sh'))
        forbidden = ['chmod ', 'chown ', 'mkdir ', 'touch ', 'rm ', 'reboot',
                     'curl -', 'rsync -', 'mount -', ' --run ', ' > /', ' >> ']
        for path in paths:
            # Comments can name forbidden actions while explaining the boundary.
            code = '\n'.join(line for line in path.read_text().splitlines() if not line.startswith(('#', 'for x in ')))
            for token in forbidden:
                self.assertNotIn(token, code, str(path.relative_to(ROOT)))

    def test_roles_have_no_host_fingerprints_or_peer_discovery(self):
        for role in ROLES:
            for path in (ROOT / 'roles' / role).rglob('*'):
                if path.is_file() and not path.name.startswith('.') and '__pycache__' not in path.parts:
                    for token in ['192.168.1.160', '192.168.1.253', 'photo_pipeline_id',
                                  'photo_managed_files', 'hostvars[']:
                        self.assertNotIn(token, path.read_text(), str(path.relative_to(ROOT)))

    def test_group_vaults_contain_only_their_health_url(self):
        vault = VaultLib([(None, VaultSecret((ROOT / '.vault_pass').read_bytes().strip()))])
        for group, key in [('photo_receivers', 'vault_pixel_backup_healthcheck_url'),
                           ('photo_controllers', 'vault_synology_pixel_healthcheck_url')]:
            content = (ROOT / 'group_vars' / group / 'vault.yml').read_bytes()
            self.assertTrue(content.startswith(b'$ANSIBLE_VAULT;'))
            values = yaml.safe_load(vault.decrypt(content))
            self.assertEqual(set(values), {key})
            # Exact values are covered by the source render checks. Never print them.
            self.assertTrue(isinstance(values[key], str) and values[key].startswith('https://'))

    def test_repository_contains_no_plaintext_health_tokens_or_personal_login(self):
        vault = VaultLib([(None, VaultSecret((ROOT / '.vault_pass').read_bytes().strip()))])
        login = yaml.safe_load(vault.decrypt((ROOT / 'group_vars/all/vault.yml').read_bytes()))['secret_unix_name'].encode()
        result = subprocess.run(['rg', '--files', '--hidden', '-g', '!.git/**', '-g', '!.vault_pass',
                                 '-g', '!**/__pycache__/**'], cwd=ROOT, capture_output=True, text=True, check=True)
        for name in result.stdout.splitlines():
            content = (ROOT / name).read_bytes()
            self.assertNotRegex(content, rb'https://hc-ping\.com/[0-9a-fA-F-]{36}', name)
            self.assertFalse(login in content, 'Plaintext login found in ' + name)


if __name__ == '__main__':
    unittest.main()
