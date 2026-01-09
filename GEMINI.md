# PART 1: PROJECT DOCUMENTATION

## GEMINI CONTEXT & ARCHITECTURAL DOCUMENT: Ansible Pi-Config

**DOCUMENT VERSION:** 5.0
**PURPOSE:** To provide a high-fidelity context for an AI agent, detailing project state, architectural decisions, and operational protocols for a Raspberry Pi server managed by Ansible.

---

### 1.1 Project Philosophy & Executive Summary

This repository implements a **"Monolith" Configuration-as-Code** paradigm for a specific Raspberry Pi hardware unit (`pi4_2020`) acting as the "Homelab Primary".

---

### 1.2 Operational Runbook

1.  **Prerequisites:** `ansible` must be installed on the control node (which is also the managed node).
2.  **Install Dependencies:**
    ```bash
    ansible-galaxy install -r requirements.yml -p vendor/
    ```

**CRITICAL WORKFLOW RULES:**
* **Encapsulation:** Logic belongs in Roles. Data belongs in Group Vars. Never mix them.
* **Directory Layout:** Always use `group_vars/<group_name>/` directories to separate `vars.yml` (git-tracked) from `vault.yml` (encrypted).
* **Dependencies:** If a role requires Docker, it must declare it in `meta/main.yml`. Do not rely on the playbook to set up prerequisites.

---

### 1.3 System Architecture: Roles

The architecture uses "Self-Contained" roles. Utility roles (like generic wrappers) are banned in favor of domain-specific encapsulation.

| Role | Type | Purpose & Rationale |
| :--- | :--- | :--- |
| **`pi_base`** | Local | **Platform Base Layer.** Handles OS-level "Day 1" setup: `log2ram`, `watchdog`, `nfs-common`, SSH hardening, and `apt` cache updates. Ensures the hardware doesn't crash. |
| **`user_friendly`** | Local | **User Experience Layer.** Installs common user packages (vim, tmux, zsh, etc.) and configures user-specific settings like the default shell and timezone for an interactive user. |
| **`physical_disks`** | Local | **Physical Storage Layer.** Mounts locally attached physical disks (e.g., USB drives) based on data defined in `host_vars`. Ensures persistent storage is available. |
| **`network_mounts`** | Local | **Network Storage Layer.** Mounts network shares (NFS/SMB) based on a dictionary of available shares and an enabled-list defined in `group_vars`. |
| **`backup_target`** | Local | **Rsync Module Plugin.** Configures a specific rsync module (e.g., a backup target) by dropping config files into the conf.d directory provided by `pi_base`. Owns the systemd dependency for its specific module. |
| **`torrenter`** | Local | **Application Layer (The "Hero" Role).** Installs the Transmission stack, manages the VPN (Gluetun), and **owns** its own maintenance logic. Tasks are broken down into smaller, included files (`systemd_docker_dependency.yml`, `docker_stack.yml`, `cron_jobs.yml`) for clarity. |
| **`geerlingguy.docker`**| External | **Dependency Layer.** Managed via `meta/main.yml` inside `torrenter`. It is never called directly in `site.yml`. |
| **`ntd.nut`** | External | **NUT Client.** Configures the Network UPS Tools (NUT) client to monitor a remote UPS server (e.g., a Synology NAS). |

### 1.3.1 Systemd Dependencies

To prevent race conditions where the Docker daemon starts before its required storage volumes are mounted, the `torrenter` role now encapsulates the logic to create a systemd drop-in override for the `docker.service` unit.

**Key Features:**

*   **Role Encapsulation:** The logic is no longer in the main playbook's `pre_tasks`. It now resides in `roles/torrenter/tasks/systemd_docker_dependency.yml` and is included by the role's `main.yml`. This makes the `torrenter` role self-contained.
*   **Execution Order:** The `torrenter` role's `main.yml` is structured to ensure correctness:
    1.  It includes the `systemd_docker_dependency.yml` task file first.
    2.  If the drop-in file is created or changed, it notifies a `Restart docker service` handler.
    3.  A `meta: flush_handlers` task is called immediately after the include. This forces the notified `Restart docker service` handler to run *before* any subsequent tasks, guaranteeing that the Docker daemon is running with the correct dependencies before the role attempts to deploy containers.
*   **Dynamic Unit Resolution:** It uses `ansible.builtin.command` to run `systemd-escape --path --suffix=mount` on the target host for each mount path, ensuring the generated unit names are always correct.
*   **Idempotent Restart:** The `Restart docker service` handler uses `daemon_reload: true` and `state: restarted` to atomically reload systemd and restart Docker, but only when the drop-in file has actually changed.
This setup ensures that the `docker.service` will `Require=` and run `After=` the specified mount units are active.

### 1.3.2 Rsync: Platform vs. Plugin Architecture

The playbook uses a "Platform vs. Plugin" model to manage the `rsync` daemon and its configuration, ensuring a clean separation of concerns:

*   **The Platform (`pi_base` role):**
    *   Installs the `rsync` package.
    *   Deploys a global, skeleton `/etc/rsyncd.conf` that enables the modular `include = /etc/rsyncd.d/*.conf` pattern.
    *   Creates the `/etc/rsyncd.d` and `/etc/rsyncd.secrets.d` directories.
    *   Owns and manages the state of the `rsync` service itself (started, enabled).
    *   **Provides the `Restart rsync service` handler**, acting as the single source of truth for how to restart the service.

*   **The Plugin (`backup_target` role):**
    *   **Consumes** the platform provided by `pi_base`.
    *   Is responsible only for its own specific module.
    *   Drops a module configuration file into `/etc/rsyncd.d/`.
    *   Drops a corresponding secrets file into `/etc/rsyncd.secrets.d/`.
    *   Configures systemd dependencies *for its specific module's mount point*.
    *   It does **not** manage the rsync service directly but **notifies** the `Restart rsync service` handler (provided by `pi_base`) whenever its configuration changes.
---

### 1.4 Data Flow & Variable Management

We utilize the **Directory Layout** strategy to prevent "Split Brain" configurations and ensure secret safety.

* **`inventory.yml`**: Defines the topology and can also contain host-specific variables for simplicity in smaller setups.
    ```yaml
    all:
      hosts:
        pi4_2020:
          ansible_host: 127.0.0.1
          ansible_connection: local
          physical_disks:
            - { path: "/mnt/expansion", uuid: "...", fstype: "ext4", opts: "..." }
      children:
        monolith:
          hosts:
            pi4_2020: {}
    ```
* **`group_vars/monolith/`**: The single source of truth for the group configuration.
    * **`vars.yml`**: Plaintext config (Users, Docker settings, non-sensitive paths). This now also includes definitions and an enable-list for logical network mounts, and all non-sensitive configuration variables for the `backup_target` role.
        ```yaml
        # group_vars/monolith/vars.yml
        # User Experience
        timezone: America/Los_Angeles

        # Network Mounts
        network_mounts_definitions:
          synology_canvio: { path: "/mnt/synology_canvio", src: "...", fstype: "nfs", opts: "..." }
        network_mounts_enabled:
          - synology_canvio
        # Rsync Backup Target (non-sensitive)
        backup_target_rsync_module_name: "synology_target"
        backup_target_rsync_base_path: "/mnt/expansion/synology_vault"
        backup_target_rsync_comment: "Backup Target for Synology"
        backup_target_rsync_uid: "1000"
        backup_target_rsync_gid: "1000"
        backup_target_rsync_user: "synology"
        backup_target_rsync_allowed_hosts: "192.168.0.0/16"
        ```
    * **`vault.yml`**: AES-256 Encrypted secrets (API keys, passwords). This now includes the `backup_target_rsync_password`.
        ```yaml
        # group_vars/monolith/vault.yml
        backup_target_rsync_password: !vault |
          $ANSIBLE_VAULT;1.1;AES256;...
        ```
* **`roles/torrenter/defaults/main.yml`**: Default behavior for the role (e.g., default backup schedule times), which can be overridden by `group_vars`.

#### Hardcoded Mount Points Refactoring

To improve robustness and portability, hardcoded mount paths (`/mnt/expansion`, `/mnt/synology_canvio`) have been extracted into variables defined in `group_vars/monolith/vars.yml`:

*   `torrenter_data_mount`: Specifies the mount point for Transmission data (config, downloads, watch directories).
*   `torrenter_backup_mount`: Specifies the mount point for off-site backups.

These variables are now used in:
*   `roles/torrenter/templates/compose.yaml.j2`: For configuring Docker volumes.
*   `roles/torrenter/files/daily-data-backup.sh`: Passed as arguments to the script, which uses them for its source and destination paths and safety checks.
*   `roles/torrenter/tasks/main.yml`: The cron job for daily backups passes these variables as quoted arguments to the `daily-data-backup.sh` script to ensure correct shell tokenization, even with whitespace in paths.

---

### 1.5 Application Layer: The Docker Stack

The `torrenter` role deploys a Docker Compose stack consisting of several services that work together to provide a secure and automated torrenting solution. The architecture is centered around the `gluetun` VPN gateway, which provides the network for all other services.

* **`gluetun`**: The networking core of the stack. It establishes a WireGuard VPN connection and provides the network for other services. It manages a forwarded port, executes the `gluetun/update-port.sh` script on port changes, and exposes an authenticated HTTP control server for external monitoring.

* **`transmission`**: The main torrent client (`linuxserver/transmission`). It is configured to run behind the `gluetun` service. Its listening port is dynamically managed by `gluetun/update-port.sh`.

* **`ptp-archiver`**: A custom-built service that periodically fetches new `.torrent` files from the PassThePopcorn (PTP) tracker.

* **`mam-updater`**: A custom-built service that periodically updates the MyAnonamouse (MAM) tracker with the current public IP.

* **`transmission-healthcheck`**: This is the master healthcheck for the core torrenting infrastructure. It is a containerized Python service that runs on the same network as `gluetun` and performs a comprehensive, data-rich check to ensure:
    1.  The `gluetun` control server is responsive and the VPN is connected.
    2.  The forwarded port from `gluetun` is correctly synchronized with `transmission`.
    3.  The `transmission` client reports the port as `OPEN` via an external check.
    4.  There are no critical system-level errors on any torrents.
    It sends a high-density status message to a single Healthchecks.io endpoint. For failures, it uses a prioritized, content-aware truncation logic to ensure the most critical diagnostic information (like torrent names or mismatched port numbers) is always present in the alert.

### 1.6 Docker Services Conventions

All custom Docker services in this project follow these conventions to ensure consistency and maintainability:

*   **Dockerfile Naming:** All Dockerfiles are named `Dockerfile` without any suffixes.
*   **Application Directory:** The working directory for all services is `/app`. All application scripts and files are copied into this directory. This makes the images self-contained and independent of volume mounts for code.
*   **Code vs. Data:** Application code is always built into the image. Persistent data is mounted to a top-level directory, typically `/data`, to keep it separate from the application code.
*   **Execution:** Scripts are executed from the `/app` directory.

---
---

# PART 2: ANSIBLE LLM CONTEXT

**AGENT NOTE:** This section contains foundational Ansible documentation and **MUST NOT** be removed. It acts as the "Senior Engineer" constraints for the LLM.

## 2.1 Ansible Key Concepts & Best Practices

### 2.1.1 Inventory & Topology

* **Host Definition:** Define the host *once* in `all.hosts` with its connection details (`ansible_host`). Reference it in groups via `children`. Never copy-paste IP addresses.

### 2.1.2 Variable Precedence & Layout

* **The Directory Rule:** Never use flat files for groups (e.g., `group_vars/all.yml`). Always use directories (`group_vars/all/`) containing:
    * `vars.yml`: Cleartext.
    * `vault.yml`: Encrypted.
    * *Why:* This allows `git` to track changes to config without exposing secrets, and prevents "ignored file" accidents.
* **Inventory vs. Group Vars:**
    * **Inventory:** Connection data only (`ansible_host`, `ansible_user` if specific to one node).
    * **Group Vars:** Configuration data (Application settings, global users).

### 2.1.3 Role Architecture & Encapsulation
* **The "Utility Role" Anti-Pattern:** Do not create roles like `cron_wrapper` or `script_installer`. These create "God Variables" and scatter logic.
* **Self-Contained Roles:** A role should own its entire lifecycle.
    * *Example:* The `torrenter` role installs the software, syncs the backup script, AND schedules the cron job.
* **Role Dependencies (`meta/main.yml`):** If Role A requires Role B, define it in `meta/main.yml`.
    * *Benefit:* `site.yml` remains clean.
    * *Benefit:* Variables for the dependency can be hardcoded in the parent role's metadata (e.g., forcing `docker_install_compose_plugin: true`).

### 2.1.4 Playbook "Check Mode"
* **The "Liar" Warning:** `ansible-playbook --check` is a simulation, not a sandbox.
* **Handler Logic:** Check mode will report `changed` handlers even if they didn't run. It is reporting what *would* happen.
* **Tag Usage:** When refactoring, use `--tags <role_name>` to limit the blast radius of changes.

### 2.1.5 Ansible Vault: Managing Secrets
* **Strategy:** Encrypt the `vault.yml` file entirely. Do not use inline `!vault` strings unless absolutely necessary, as they clutter readable YAML.
* **Decoupling:** By splitting `vars.yml` and `vault.yml`, you allow the codebase to be browsable without constantly needing the vault password.

### 2.1.6 Standard Ansible Concepts (Reference)
* **Idempotency:** The core goal. Running the playbook 100 times should result in 0 changes after the first run.
* **Handlers:** Triggered by `notify`. Run once at the end of the play.
* **Templates (Jinja2):** Use `.j2` extensions. Logic like `{% if %}` belongs here, not in the playbook YAML.
