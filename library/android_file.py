"""Documentation for the controller-side android_file action plugin."""
DOCUMENTATION = r'''
module: android_file
short_description: Manage farm source files on a Python-free rooted Android host
description:
  - Uses root SSH shell commands without installing Python or transferring modules.
  - Matching resources and check mode only read remote state.
  - Files use checksum comparison, private staging and atomic same-directory rename.
  - Rejects symlinks and unsupported types. Does not follow or recursively repair trees.
  - Supports only farm source paths under /data/local/tmp and /data/adb/service.d.
  - Use no_log for secret content. File diffs and backups are deliberately omitted.
options:
  allow_changes:
    type: bool
    default: true
    description: Reject real writes unless a maintenance window has been observed. Check mode still predicts changes.
  path:
    type: path
    required: true
  state:
    type: str
    choices: [file, directory]
    default: file
  content:
    type: str
    description: Rendered UTF-8 text, required for files, maximum 64 KiB.
  owner:
    type: str
    required: true
    description: Numeric UID.
  group:
    type: str
    required: true
    description: Numeric GID.
  mode:
    type: str
    required: true
    description: Quoted octal permissions, without special bits.
'''
EXAMPLES = r'''
- name: Install a rendered Android script
  android_file:
    path: /data/local/tmp/pixel-backup-gang/pixel_health.sh
    content: "{{ lookup('ansible.builtin.template', 'pixel_health.sh.j2', rstrip=False) }}"
    owner: '0'
    group: '0'
    mode: '0755'
  no_log: true
  diff: false
'''
