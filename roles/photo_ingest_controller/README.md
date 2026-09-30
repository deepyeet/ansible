# photo_ingest_controller

Consumes `photo_ingest` (state/queue policy), `photo_ingest_endpoint` (receiver
contract) and a vaulted health URL at rendering time. It never looks up a peer
or selects a phone implementation itself: the play performs that binding.

The captured shell algorithm uses legacy internal names such as `PIXEL_IP`.
Keeping those names preserves source bytes; the role interface does not require
a Pixel. Its bounded compatibility is SSH/rsync plus the `rsync_drop_v1` layout
and disappearance-based completion assumption. No HTTP/cloud-receipt interface
is implemented. DSM scheduling/ownership are a deployment adapter outside this
role's transfer contract; broad OS compatibility has not been tested.

## Inputs and behavior

- `photo_ingest`: queue/state directories, SSH key path and timing policy.
- `photo_ingest_endpoint`: address, data SSH identity/port, root, cleanup argv,
  protocol and completion semantics.
- `photo_ingest_healthcheck_url`: explicit value supplied from Vault.
- `photo_ingest_adoption_sources`: reviewed manager destination/hash/UID/GID/mode.

All inputs have a typed [argument schema](meta/argument_specs.yml). `tasks/main.yml`
validates them, renders the captured manager in memory and reads its deployed
hash and metadata. `tasks/validate.yml` performs only the local preflight.
Changed or absent sources fail; no task creates, replaces or normalizes them.
The role never runs the manager, rsync, remote cleanup or healthcheck calls.

The play resolves the peer and the DSM adapter inspects manually configured
accounts, storage and scheduling. The controller role itself needs neither
`hostvars` nor knowledge of the receiver's implementation roles.

`rsync_drop_v1` requires promotion on the same filesystem, staging excluded from
the uploader, and cleanup that removes only backed-up files. These are documented
assumptions of the preserved algorithm. Configuration validation checks the
endpoint's declared fields; it cannot establish those behaviors. The installation
records their observed or unverified status separately in its host variables.
