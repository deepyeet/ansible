# Project Overview: Automated Torrenting Stack

This directory contains a multi-service Docker Compose stack designed for automated, secure torrenting. The architecture is centered around a VPN gateway (`gluetun`) through which all other services route their traffic.

The primary purpose is to run a Transmission torrent client behind a VPN and use other custom services to automate interactions with private trackers.

## Service Architecture

The stack is defined in `compose.yaml` and consists of the following key services:

*   ### `gluetun`
    The networking core of the stack. It establishes a WireGuard VPN connection and provides the network for other services. It manages a forwarded port, executes the `gluetun/update-port.sh` script on port changes, and exposes an authenticated HTTP control server for external monitoring.

*   ### `transmission`
    The main torrent client (`linuxserver/transmission`). It is configured to run behind the `gluetun` service. Its listening port is dynamically managed by `gluetun/update-port.sh`.

*   ### `ptp-archiver`
    A custom-built service that periodically fetches new `.torrent` files from the PassThePopcorn (PTP) tracker.
    > **For a detailed breakdown of this service's internal scripts and logic, see `ptp-archiver/GEMINI.md`.**

*   ### `mam-updater`
    A custom-built service that periodically updates the MyAnonamouse (MAM) tracker with the current public IP.
    > **For a detailed breakdown of this service's internal scripts and logic, see `mam-updater/GEMINI.md`.**

*   ### `transmission-healthcheck`
    This is the master healthcheck for the core torrenting infrastructure. It is a containerized Python service that runs on the same network as `gluetun` and performs a comprehensive, data-rich check to ensure:
    1.  The `gluetun` control server is responsive and the VPN is connected.
    2.  The forwarded port from `gluetun` is correctly synchronized with `transmission`.
    3.  The `transmission` client reports the port as `OPEN` via an external check.
    4.  There are no critical system-level errors on any torrents.
    It sends a high-density status message to a single Healthchecks.io endpoint. For failures, it uses a prioritized, content-aware truncation logic to ensure the most critical diagnostic information (like torrent names or mismatched port numbers) is always present in the alert.

## Stack Operations

This Docker Compose stack is designed to be deployed and managed by the `torrenter` Ansible role, not run manually with `docker-compose`.

### Ansible-Managed Deployment
The `torrenter` role handles the entire lifecycle of this stack:
1.  **Templating:** It generates a `compose.yaml` file from `templates/compose.yaml.j2`.
2.  **Secret Injection:** It creates a temporary `.env` file for the `docker-compose` command to use, populating it with secrets fetched securely from the Ansible vault. This file is managed by Ansible and should not be created manually.
3.  **Service Management:** It uses the `community.docker.docker_compose_v2` module to start, stop, and update the stack, ensuring it's always in the desired state.

### Development & Testing
For development purposes, you can run the stack manually, but you must provide your own `.env` file in this directory with the required secrets.

A minimal `.env` file would look like this:
```
# .env
WIREGUARD_PRIVATE_KEY=...
TRANSMISSION_USER=...
TRANSMISSION_PASS=...
PTP_USER=...
PTP_KEY=...
MAM_ID=...
GLUETUN_USER=...
GLUETUN_PASS=...
HC_URL_PTP=...
HC_URL_MAM=...
HC_URL_TRANSMISSION=...
```

To build and run the entire stack in detached mode for testing:
```bash
docker compose -f compose.yaml up -d --build
```

## Monitoring

The stack uses [Healthchecks.io](https://healthchecks.io) for monitoring:
- **`ptp-archiver`**: Monitors its own fetch cycles.
- **`mam-updater`**: Monitors the health of the IP update process.
- **`transmission-healthcheck`**: This is the master healthcheck for the core torrenting infrastructure. It is a containerized Python service that runs on the same network as `gluetun` and performs a comprehensive, data-rich check to ensure:
    1.  The `gluetun` control server is responsive and the VPN is connected.
    2.  The forwarded port from `gluetun` is correctly synchronized with `transmission`.
    3.  The `transmission` client reports the port as `OPEN` via an external check.
    4.  There are no critical system-level errors on any torrents.
    It sends a high-density status message to a single Healthchecks.io endpoint, including VPN status, port status, active torrent count, and current download/upload speeds.