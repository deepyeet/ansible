# Project Overview: Transmission Healthcheck

This directory contains a single-purpose Python service that acts as the master healthcheck for the torrenting infrastructure. It is designed to run on the same Docker network as `gluetun` and `transmission`, performing deep, interconnected checks to validate that the entire stack is functioning correctly.

## Architecture and Logic

The core of the service is `check.py`, a Python script that runs in an infinite loop. On each iteration, it performs a series of critical checks:

1.  **Gluetun Status**: It contacts the `gluetun` control server to verify that the VPN is `running` and to retrieve the current forwarded port number.
2.  **Transmission Status**: It connects to the `transmission` RPC server to get its configured `peer-port`.
3.  **Port Synchronization Check**: It ensures the port `gluetun` provides is the same one `transmission` is using. A mismatch (`E:MISMATCH`) indicates a critical failure in the port update mechanism.
4.  **External Port Check**: It uses an external service (`portcheck.transmissionbt.com`) to confirm that the `peer-port` is genuinely `OPEN` to the internet. A `CLOSED` port will prevent new peers from connecting.
5.  **Torrent Integrity Check**: It queries all torrents and checks for system-level errors (i.e., `error: 3`). These errors often point to data corruption or filesystem issues (`E:SYS_ERR`).

## Alerting and Error Reporting

The service is designed for robust, unattended monitoring via [Healthchecks.io](https://healthchecks.io).

### Success Message

A successful check sends a dense, informative status update:
`OK | P:58993(OPEN) VPN:running | DL/UL:2170880/16397 | Act:1331`

-   `P`: The current peer port and its status (`OPEN`).
-   `VPN`: The status of the VPN connection.
-   `DL/UL`: Current download and upload speed in bytes/sec.
-   `Act`: The number of active torrents.

### Failure Messages

Failure messages are designed to be as informative as possible within Healthchecks.io's 100-character limit, using a prioritized truncation strategy.

-   **System Error**: Provides torrent-specific details, prioritizing the torrent ID and name over less critical info.
    -   `E:SYS_ERR T:123(My.Torrent.Name): Please Verify Local Data...`

-   **Port Mismatch**: Clearly shows the conflicting port numbers from each service.
    -   `E:MISMATCH(G:58993|T:12345)`

-   **Port Check Error**: Details unexpected responses or exceptions from the external port checking service.
    -   `E:PORTCHECK_UNEXPECTED_RESP(Malformed Response)` or `E:PORTCHECK_DOWN(ConnectionError)`

-   **Other Errors**: All other defined errors (e.g., `E:CLOSED`) are also sent with their relevant diagnostic context.

This intelligent alerting ensures that the root cause of a failure is immediately obvious from the alert itself, minimizing the need for manual debugging.

## Configuration

The service is configured entirely through environment variables, passed from the `compose.yaml` file:

-   `TR_HOST`, `TR_PORT`, `TR_USER`, `TRANSMISSION_PASS`: Credentials for the Transmission RPC.
-   `GLUETUN_HOST`, `GLUETUN_PORT`, `GLUETUN_USER`, `GLUETUN_PASS`: Credentials for the Gluetun control server.
-   `HC_URL`: The URL for the Healthchecks.io endpoint.
-   `CHECK_INTERVAL_SECONDS`: The delay between each check cycle (defaults to 300).
