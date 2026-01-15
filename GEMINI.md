# --- PRIVACY AND SECURITY WARNING ---
**This is a public repository. Under no circumstances should any Personally Identifiable Information (PII), including usernames, real names, or any other private data, be committed to this repository.** All secrets must be managed via Ansible Vault. All user-specific identifiers in variable files should use placeholders that are substituted by vaulted secrets.

---

# PART 1: PROJECT DOCUMENTATION

### 1.0 Agent Protocol

**As the Gemini coding agent, after every task, you must always update the `GEMINI.md` files prior to committing the repository.** This ensures the context documents remain synchronized with the state of the codebase, providing accurate and detailed information for subsequent tasks.

---

## GEMINI CONTEXT & ARCHITECTURAL DOCUMENT: Ansible Pi-Config

**DOCUMENT VERSION:** 6.0
**PURPOSE:** To provide a high-fidelity context for an AI agent, detailing project state, architectural decisions, and operational protocols for a Raspberry Pi server managed by Ansible.

---

### 1.1 Project Philosophy & Executive Summary

This repository implements a **"Monolith" Configuration-as-Code** paradigm for a specific Raspberry Pi hardware unit (`pi4_2020`) acting as the "Homelab Primary".

---

### 1.2 Operational Runbook

1.  **Prerequisites:** `ansible` must be installed on the control node (which is also the managed node).
2.  **Install Dependencies:**
    ```bash
    ansible-galaxy role install -r requirements.yml -p vendor/roles
    ansible-galaxy collection install -r requirements.yml -p vendor/collections
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
| **`pi_base`** | Local | **Platform Base Layer.** Handles OS-level "Day 1" setup: `log2ram`, `watchdog`, `nfs-common`, SSH hardening, and `apt` cache updates. **Depends on `rsync_base` to provide the core rsync daemon platform.** Ensures the hardware doesn't crash. |
| **`user_friendly`** | Local | **System-wide Experience Layer.** Installs a static list of common, user-agnostic terminal packages (htop, curl, eza, fd-find, ripgrep, zoxide), build tools (`build-essential`, `cmake`), debug tools (`gdb`, `strace`), and the latest Google Gemini CLI. It also installs `atuin` from a pre-compiled binary and sets the system timezone, acting as a baseline for an interactive system. |
| **`users`** | Local | **User Account Management.** Creates user accounts, sets up their `authorized_keys` for SSH access, and assigns groups. This role handles all user creation logic that requires root privileges. |
| **`dotfiles`** | Local | **User-specific Configuration.** Clones a user's dotfiles repository from Git and uses `stow` to symlink them into place. This role runs with user-level privileges and uses a per-user deploy key for secure git cloning. |
| **`neovim`** | Local | **Neovim Installer.** Installs Neovim from the `unstable` PPA, including Python support packages. |
| **`physical_disks`** | Local | **Physical Storage Layer.** Mounts locally attached physical disks (e.g., USB drives) based on data defined in `host_vars`. Ensures persistent storage is available. |
| **`network_mounts`** | Local | **Network Storage Layer.** Mounts network shares (NFS/SMB) based on a dictionary of available shares and an enabled-list defined in `group_vars`. |
| **`backup_target`** | Local | **Rsync Module Plugin.** Configures a specific rsync module (e.g., a backup target) by dropping config files into the conf.d directory. **Depends on `rsync_base` to provide the core rsync daemon platform.** Owns the systemd dependency for its specific module. |
| **`rsync_base`** | Local | **Rsync Daemon Platform.** Installs the `rsync` package, deploys the global `/etc/rsyncd.conf` with modular includes, creates necessary directories, and manages the `rsync` service. Provides the foundational platform for rsync-based services. |
| **`docker_stack`** | Local | **Generic Docker Compose Deployment.** Deploys Docker Compose stacks based on a list of definitions in `group_vars`. Each definition includes the compose file content, environment variables, and any static files, allowing for flexible, data-driven application deployment without creating a new role for each stack. |
| **`torrenter`** | Local | **Application Layer (The "Hero" Role).** Installs the Transmission stack, manages the VPN (Gluetun), and **owns** its own maintenance logic. It depends on `physical_disks` for mounting its data volumes. Tasks are broken down into smaller, included files (`systemd_docker_dependency.yml`, `docker_stack.yml`, `cron_jobs.yml`) for clarity. |
| **`geerlingguy.docker`**| External | **Dependency Layer.** Managed via `meta/main.yml` inside `torrenter` and `docker_stack`. It is never called directly in `site.yml`. |
| **`geerlingguy.nodejs`**| External | **Dependency Layer.** Managed via `meta/main.yml` inside `user_friendly`. |
| **`ntd.nut`** | External | **NUT Client.** Configures the Network UPS Tools (NUT) client to monitor a remote UPS server (e.g., a Synology NAS). |

### 1.3.1 Data-Driven Docker Deployments (`docker_stack`)

To eliminate boilerplate and reduce role duplication, a generic `docker_stack` role has been introduced. This role moves the responsibility of defining a Docker Compose application from a dedicated role's `tasks` and `templates` into `group_vars`.

**Key Features:**

*   **Data-Centric:** Instead of creating a new role for each Docker-based application (like the old `cloudflared` role), you now define a stack as a data structure in a `group_vars` file (e.g., `group_vars/monolith/docker_stacks.yml`).
*   **Role Logic:** The `docker_stack` role iterates through a list variable called `docker_stacks`. For each item in the list, it performs the following actions:
    1.  Ensures a project directory exists.
    2.  Uses `ansible.builtin.copy` with content derived from a `template` lookup to generate the `compose.yaml` and `.env` files directly from the variables. This allows the file content to be stored in YAML as multi-line strings while still being processed for Jinja2 expressions.
    3.  Deploys the stack using `community.docker.docker_compose_v2`.
    4.  It intelligently uses the `build: 'always'` flag only when the source files (`compose.yaml` or `.env`) have actually changed.
*   **Variable Structure:** An example stack definition in `group_vars/monolith/docker_stacks.yml`:
    ```yaml
    # group_vars/monolith/docker_stacks.yml
    docker_stacks:
      - name: "cloudflared"
        enabled: true
        project_path: "/home/{{ ansible_user }}/ansible_docker/cloudflared"
        compose_content: |
          version: '3.3'
          services:
            cloudflared:
              # ...
        env_content: |
          CLOUDFLARED_TUNNEL_TOKEN={{ CLOUDFLARED_TUNNEL_TOKEN }}
    ```

This approach makes adding new, simple Docker Compose-based services a matter of adding data to a YAML file, rather than scaffolding an entire new role.

### 1.3.2 Systemd Dependencies

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

The playbook uses a "Platform vs. Plugin" model to manage the `rsync` daemon and its configuration, ensuring a clean separation of concerns for *incoming host-level rsync backups*:

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

Variable management is separated by group, with secrets always isolated in `vault.yml` files.

*   **`inventory.yml`**: Defines the topology only. It maps hosts to groups.
    ```yaml
    all:
      hosts:
        pi4_2020:
          ansible_host: raspberrypi
        jujupc:
          ansible_host: juju-pc
          ansible_connection: local
      children:
        monolith:
          hosts:
            pi4_2020: {}
        wsl:
          hosts:
            jujupc: {}
    ```

*   **`group_vars/all/vars.yml`**: Contains global variables for all hosts. The `managed_users` list is defined here, which drives both the `users` and `dotfiles` roles.
    ```yaml
    # group_vars/all/vars.yml
    timezone: America/Los_Angeles

    managed_users:
      - username: "{{ secret_unix_name }}"
        groups: "sudo"
        shell: /usr/bin/zsh
        authorized_keys:
          - "ssh-ed25519 AAAA..."
        dotfiles:
          repo: "git@github.com:example/dotfiles.git"
          dest: "~/.dotfiles"
          version: "main"
          # This key is used to clone the dotfiles repository.
          # It should be a private key with read access to the repo.
          deploy_key: "{{ vault_git_deploy_key }}"

    nodejs_version: "24.x"
    ```
*   **`group_vars/all/vault.yml`**: Contains the encrypted secrets referenced from `vars.yml`.
    ```yaml
    # The decrypted content of group_vars/all/vault.yml
    secret_unix_name: "yiyang"
    vault_git_deploy_key: |
      -----BEGIN OPENSSH PRIVATE KEY-----
      ...
      -----END OPENSSH PRIVATE KEY-----
    ```

*   **`group_vars/monolith/vars.yml`**: An example of group-specific variables for the `monolith` group. These are merged with and override variables from `all`.
    ```yaml
    # group_vars/monolith/vars.yml
    # Network Mounts
    network_mounts_definitions:
      synology_canvio: { path: "/mnt/synology_canvio", src: "...", fstype: "nfs", opts: "..." }
    network_mounts_enabled:
      - synology_canvio
    ```


#### Hardcoded Mount Points Refactoring

To improve robustness and portability, hardcoded mount paths (`/mnt/expansion`, `/mnt/synology_canvio`) have been extracted into variables defined in `group_vars/monolith/vars.yml`:

*   **`torrenter_data_host_path`**: Specifies the host path for Transmission data (config, downloads, watch directories), used as a bind mount in Docker Compose.
*   **`torrenter_backup_docker_volume_name`**: Specifies the name of the Docker named volume used for off-site backups (e.g., NFS mounts).

The base path for the `torrenter` Docker Compose project and related files is `"/home/{{ ansible_user }}/ansible_docker/transmission"`, as per user preference.

These variables and the preferred base path are now used in:
*   `roles/torrenter/templates/compose.yaml.j2`: For defining Docker named volumes and configuring services to use them, and for building the `backup` service from a custom Dockerfile located at `roles/torrenter/files/backup/Dockerfile`.
*   `roles/torrenter/tasks/main.yml`: The main task file ensures the configuration directory exists.
*   `roles/torrenter/tasks/cron_jobs.yml`: The cron job now triggers a containerized backup via `docker compose run`, with the Docker Compose file and log file paths explicitly set to `"/home/{{ ansible_user }}/ansible_docker/transmission/compose.yaml"` and `"/home/{{ ansible_user }}/ansible_docker/transmission/daily-data-backup.log"` respectively.
*   `roles/torrenter/tasks/docker_stack.yml`: All tasks related to deploying the Docker stack now utilize `"/home/{{ ansible_user }}/ansible_docker/transmission"` for the project source and file destinations.
*   `roles/torrenter/files/daily-data-backup.sh`: This script is now part of the `backup` service Docker image and runs inside a container, expecting paths like `/data` and `/backup_dest` and receiving the Healthchecks.io UUID as an argument.

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

### 1.5.1 Volume Management & Containerized Backup

This section details the `torrenter` role's *outgoing* data backup strategy and its volume management. To enhance portability and streamline management, network-attached storage (NFS) mounts previously handled by the `network_mounts` Ansible role are now managed directly by Docker through named volumes in `docker-compose.yaml`. This change necessitated the containerization of the daily data backup process.  The `torrenter` role depends on the `physical_disks` role to ensure that the host-level `torrenter_data_mount` (e.g., `/mnt/expansion`) is correctly mounted before Docker attempts to bind mount it.

**Key Changes:**

*   **Docker Named Volumes for NFS:** Instead of host-level mounting, NFS shares are now defined as Docker named volumes using the `local` driver with `driver_opts` specifying the NFS server, path, and options (e.g., `addr=...,rw,noatime,bg`). This allows Docker to manage the lifecycle of these mounts directly.
*   **Removal of `network_mounts` Role:** The `network_mounts` role has been removed from `site.yml` for the `monolith` host, as its functionality is now superseded by Docker.
*   **Containerized Backup Service (`backup`):**
    *   A new `backup` service has been added to `docker-compose.yaml`. This service uses a lightweight `alpine/git` image (which includes `rsync`).
    *   It operates using the `daily-data-backup.sh` script, which is built directly into its Docker image. It mounts the source data volume (read-only) and the destination NFS Docker volume.
    *   The script now operates with container-internal paths (`/data`, `/backup_dest`) and no longer performs host-level `mountpoint` checks.
*   **Updated Cron Job:** The host-level cron job in `roles/torrenter/tasks/cron_jobs.yml` no longer directly executes the `daily-data-backup.sh` script. Instead, it triggers the containerized backup using `docker compose -f /path/to/compose.yaml --profile backup run --rm backup`.
*   **Refactored Static File Sync:** The `ansible.posix.synchronize` task in `roles/torrenter/tasks/docker_stack.yml` has been replaced with individual `ansible.builtin.copy` tasks for each static file/directory (`backup`, `gluetun`, `mam-updater`, `ptp-archiver`, `transmission-healthcheck`, `GEMINI.md`). This change addresses an error where the `exclude` parameter was not supported by `ansible.posix.synchronize`. A new `static_docker_files_changed` fact aggregates the `changed` status of these copy tasks, ensuring the Docker stack rebuilds when static files are updated.

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

### 2.1.7 Ansible Engine Quirks

#### Lazy Variable Resolution and `is defined`

Ansible uses a "lazy" or "on-demand" method for templating variables. A complex variable (like a list of dictionaries) is not fully resolved until a task requires its value.

This can lead to a confusing and difficult-to-diagnose behavior:

*   You have a variable, `my_list`, defined in `vars.yml`.
*   `my_list` contains a reference to a secret variable, e.g., `{{ my_secret_value }}`.
*   The variable `my_secret_value` is **not defined** anywhere (e.g., typo in the vault).

When a task like `ansible.builtin.assert: that: - my_list is defined` runs, you would expect it to pass, because `my_list` *is* in the `vars.yml` file. However, the assertion will **fail**.

**Why:** Because Ansible cannot fully resolve the complete structure of `my_list` due to the missing nested variable `my_secret_value`, it treats the entire parent variable (`my_list`) as `AnsibleUndefined`.

This means a simple `is defined` check fails, even though the variable appears to exist. The real error is not that the parent variable is missing, but that one of its components is. Debugging this requires checking that all nested variables within a complex data structure can be resolved.
--- End of Context from: GEMINI.md ---
