# Project Overview: MAM IP Updater

This directory contains a single-purpose Docker service responsible for keeping a user's IP address updated with the MyAnonamouse (MAM) private tracker.

This service is a crucial component of the parent Docker Compose stack. Because all traffic is routed through the `gluetun` VPN service, the public IP address can change. This updater ensures that the MAM tracker is always aware of the current IP, which is a common security requirement for private trackers.

## Architecture and Key Files

*   **`Dockerfile`**: Defines the service's runtime environment. It uses a minimal `alpine:3.18` base image, sets the working directory to `/app`, and installs necessary dependencies (`bash`, `curl`, `jq`). The `mam-updater.sh` script is copied into the image and made executable.

*   **`mam-updater.sh`**: The core logic of the service. It's a bash script that runs in an infinite loop, now located at `/app/mam-updater.sh` inside the container. It performs the following actions:
    1.  **Get Public IP**: Determines the current public IP address by querying external services.
    2.  **Check Cache**: Compares the current IP against a cached IP stored in `/data/last_ip`.
    3.  **Authenticate & Update**: If the IP has changed, it uses a stored session cookie (`/data/mam.cookies`) to send an update request to the MAM API.
    4.  **Error Handling**: It includes robust logic to handle API rate limits, session expirations, and other potential errors.
    5.  **Health Monitoring**: Integrates with Healthchecks.io.

*   **`data/` directory**: This directory is mounted as a volume from the host to `/data` inside the container. It is used to persist state between container restarts.
    *   **`last_ip` & `mam.cookies`**: These are state files generated and managed exclusively by the script. They should not be manually edited.

## Building and Running

This service is not intended to be run standalone and is managed by the `compose.yaml` file in the parent directory.

*   **Build**: The image is built automatically when you run `docker-compose build` or `docker-compose up --build`.

*   **Run**: The service is started with the rest of the stack:
    ```bash
    # From the parent directory
    docker-compose up -d
    ```

## Development Conventions

*   **Script Location**: The main script (`mam-updater.sh`) is built into the Docker image at `/app/mam-updater.sh`. This ensures the image is self-contained.
*   **Persistent Data**: The `/data` volume is used to store state files (`last_ip`, `mam.cookies`) that need to persist across container restarts.
*   **Configuration**: The service is configured via environment variables passed from the `compose.yaml` file.
*   **Health Monitoring**: The script reports its status to a Healthchecks.io endpoint.
*   **Dependencies**: The service relies on minimal, common tools (`bash`, `curl`, `jq`), making it lightweight and portable.
