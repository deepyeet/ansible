# pixel_tailscale

Deploys and manages a headless, resilient Tailscale daemon on a Magisk-rooted Google Pixel 1 (`pixel1`, Android 10, kernel 3.18 aarch64) used as a photo backup farm receiver.

Android lacks standard GNU/Linux service managers (systemd/openrc), packaging tools (apt/dpkg), and Python runtime. This role deploys official static ARM64 Linux binaries, integrates with Magisk's boot architecture, and resolves Android Bionic identity quirks natively using Python-free Ansible (`ansible.builtin.raw`).

---

## Architecture Overview

```
/data/adb/
├── service.d/
│   └── tailscaled.sh          # Magisk late_start boot hook & supervisor loop
├── modules/tailscale/
│   ├── module.prop            # Magisk module metadata
│   └── system/
│       ├── bin/
│       │   ├── tailscale      # System CLI wrapper overlay
│       │   └── getent         # Fallback getent tool overlay
│       └── etc/
│           ├── passwd         # Root passwd entry overlay
│           └── group          # Root group entry overlay
└── tailscale/
    ├── bin/
    │   ├── tailscaled         # Official static ARM64 daemon
    │   └── tailscale          # Official static ARM64 CLI
    ├── run/
    │   ├── tailscaled.sock    # Local management UNIX domain socket
    │   ├── tailscaled.pid     # Daemon process ID
    │   ├── supervisor.pid     # Supervisor shell loop process ID
    │   └── tailscaled.log     # Bounded runtime log (max 5 MB with boot rotation)
    ├── state/
    │   ├── tailscaled.state   # Node key and node network state
    │   ├── derpmap.cached.json# Cached DERP map
    │   └── ssh/               # Persistent Tailscale SSH host keys
    │       ├── ssh_host_ed25519_key
    │       ├── ssh_host_ecdsa_key
    │       └── ssh_host_rsa_key
    └── etc/                   # Mirrored /system/etc subtree with injected passwd & group
```

---

## Key Operational & Android Compatibility Features

### 1. The `/system/etc` Mirror & Dynamic Linker Coexistence
* **The Problem**: Tailscale SSH (`CGO_ENABLED=0`) relies on Go's `os/user` package to resolve supplementary group IDs for `root`. It attempts `openFile("/etc/group")`. Android uses Bionic libc with internal `aid_*` IDs and has no `/etc/passwd` or `/etc/group`, causing Tailscale SSH to abort with `tailscale: failed to look up user's group ID`.
* **Read-Only `/system`**: On Android 10 SAR (System-as-Root), `/system` is mounted read-only from `dm-verity` (`/dev/root`), preventing direct file creation in `/system/etc`.
* **Symlink Trap (`screencap` failure)**: If `/system/etc/*` files are symlinked into a directory and bind-mounted over `/system/etc`, the symlinks resolve circularly to the mount itself. This hides `/system/etc/ld.config.vndk_lite.txt` from the dynamic linker (`linker64`), which causes binaries like `screencap` to fail loading APEX libraries (`library "libandroidicu.so" not found`).
* **The Solution**: The boot script mirrors the entire static `/system/etc` directory (43 files, ~5.9 MB) onto flash at `/data/adb/tailscale/etc/` using `cp -a`, writes `passwd` and `group`, and bind-mounts the directory over `/system/etc`.
* **Zero Bootloop / Brick Risk**:
  * The mount only occurs *after* `sys.boot_completed == 1`, meaning Android finishes booting completely before the mount occurs.
  * Bind mounts exist solely in the kernel's RAM VFS table. On every reboot, RAM is cleared and Android boots from the untouched factory `system.img` partition.
  * A single `umount /system/etc` immediately restores the stock factory state.

### 2. Tailscale SSH & `--statedir` Flag
* Tailscale SSH requires persistent storage for its host keys (`ssh_host_*_key`).
* On standard Linux, Tailscale defaults to `/var/lib/tailscale`. Android has no `/var/` directory, causing Tailscale to disable SSH advertisement (`unable to get SSH host keys, SSH will appear as disabled for this node: no var root for ssh keys`).
* Passing `--statedir="/data/adb/tailscale/state"` instructs `tailscaled` to store host keys in `/data/adb/tailscale/state/ssh/`.

### 3. Magisk Boot Coordination & Independence
* `tailscaled.sh` resides in `/data/adb/service.d/` (`late_start` mode).
* Runs in parallel with the photo backup startup script (`farm-startup.sh`).
* Both scripts synchronize on `while [ "$(getprop sys.boot_completed)" != "1" ]; do sleep 2; done`.
* Tailscale operates on Wi-Fi (`wlan0`) and userspace networking (`--tun=userspace-networking`), while `farm-startup.sh` mounts the external USB storage drive (`/mnt/my_drive`) and launches Google Photos.

---

## Role Configuration Variables

See [`defaults/main.yml`](defaults/main.yml) for full defaults:

| Variable | Default | Purpose |
| --- | --- | --- |
| `pixel_tailscale_version` | `"1.104.1"` | Official Tailscale static ARM64 release version |
| `pixel_tailscale_tarball_checksum` | (sha256) | Integrity verification for downloaded archive |
| `pixel_tailscale_daemon_checksum` | (sha256) | Integrity verification for `tailscaled` |
| `pixel_tailscale_cli_checksum` | (sha256) | Integrity verification for `tailscale` CLI |
| `pixel_tailscale_base_dir` | `"/data/adb/tailscale"` | Base storage directory on target |
| `pixel_tailscale_tun_mode` | `"userspace-networking"` | Tunnel driver mode |
| `pixel_tailscale_ssh_enabled` | `true` | Advertises and serves Tailscale SSH |
| `pixel_tailscale_tags` | `["tag:internal"]` | Advertised Tailnet tags |
| `pixel_tailscale_authkey` | `""` | Optional pre-authenticated auth key (vaulted) |

---

## Tailscale Access Controls (ACLs)

Because `pixel1` is tagged with `tag:internal`, Tailnet policy requires an explicit SSH grant in your Tailscale Admin Console [Access Controls](https://login.tailscale.com/admin/acls):

```json
  "ssh": [
    {
      "action": "accept",
      "src": [
        "autogroup:admin",
        "deepyeet@github"
      ],
      "dst": [
        "tag:internal"
      ],
      "users": [
        "root",
        "autogroup:nonroot"
      ]
    }
  ]
```

---

## Usage

### Run Dedicated Standalone Playbook
```bash
ansible-playbook playbooks/pixel-tailscale.yml
```

### Run via Site Playbook with Tags
```bash
ansible-playbook site.yml --tags pixel_tailscale
```

### Run Automated Unit Tests
```bash
python3 -m unittest discover tests
```
