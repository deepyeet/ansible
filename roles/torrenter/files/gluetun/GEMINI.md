# Project Overview: Gluetun Service Components

This directory contains components related to the `gluetun` service, which acts as the VPN gateway for the parent Docker Compose stack. These components help integrate `gluetun` with other services and enable external monitoring.

## Service Integration

The `gluetun` service is configured in the parent `compose.yaml` to be the networking core of the stack. Two key integration points are managed from this directory:

### 1. Dynamic Port Updates (`update-port.sh`)

This is a shell script that is executed as a hook by `gluetun` whenever the VPN's forwarded port changes.

*   **Purpose**: To dynamically update the listening port in the `transmission` service to match the new port forwarded by the VPN. This is essential for maintaining proper torrent connectivity.
*   **Execution**: The script is triggered by the `VPN_PORT_FORWARDING_UP_COMMAND` environment variable set on the `gluetun` service in `compose.yaml`. The new port is passed as a command-line argument.
*   **Logic**: It's a durable script that waits for the Transmission RPC to be available, authenticates, and then sends the command to update the `peer-port`.

### 2. Control Server for Monitoring

The `gluetun` service is configured to run an authenticated HTTP control server. This is the primary method for the external `transmission-healthcheck` service to get authoritative information about the VPN's status.

*   **Purpose**: To expose an API endpoint that allows external services to query the current forwarded port, VPN status, and public IP address.
*   **Configuration**: This is configured in the parent Ansible role's `compose.yaml.j2` template using environment variables:
    *   `HTTP_CONTROL_SERVER_ADDRESS=:8000`: Enables the server on port 8000.
    *   `HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE`: A JSON string that configures basic authentication using the `{{ gluetun_user }}` and `{{ gluetun_password }}` Ansible variables, which are securely passed from the vault.
*   **Usage**: The `transmission-healthcheck` service queries the `/v1/portforward` and `/v1/vpn/status` endpoints of this server to perform its comprehensive health checks.

## Development Conventions

*   **`update-port.sh`**:
    *   **Not Run Manually**: This script is only ever executed by the `gluetun` container itself.
    *   **Configuration**: It is configured via environment variables (`TRANSMISSION_USER`, `TRANSMISSION_PASS`) that are set on the `gluetun` service in the parent `compose.yaml.j2` template, with values supplied by Ansible.
*   **Control Server**:
    *   **Authentication**: The control server is secured with basic authentication. Credentials (`gluetun_user`, `gluetun_password`) are managed in the Ansible vault and injected as environment variables at deploy time.
    *   **Maintainability**: Using the `HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE` environment variable is the simplest and most direct way to configure authentication, as per the official documentation.