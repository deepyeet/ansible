# album_sorter

Deploys the photo album sorter (the `album_sorter` Python package and
its tooling) to the `ds223j-muse` container on the Synology DS223j,
and keeps its Python virtualenv in sync. This role is source and
environment management only.

## Host layout: two mounts

The container has two mounts that matter, and the role's paths follow
the split:

- `/home/muse` — a mount in its own right (verified with `findmnt`):
  source `/volume1/@homes@[/muse]`, fstype ecryptfs, on the 7.0T
  volume. It persists on NAS storage. **Code and environment live
  here**: the working tree (`~/album_sorter_dev`) and the virtualenv
  (`~/album-venv`) that this role manages.
- `/mnt/pixel` — the only mount under `/mnt`: a read-write bind of
  `/dev/vg1/volume_1` (the `pixel` share; the same underlying volume
  as the homes store). **Photo data lives here**: the pipeline trees
  (`0_Inbox`, `1_Queue`, `2_Uploading`, `3_Done`, `Staging`), the
  sorted output (`4_Sorted`), and the sorter's work directories
  (`.sorter-work*`, holding the manifests). The role never touches
  this mount; the sorter reaches it at runtime through its own
  configuration (environment / `Config` paths), not through anything
  deployed here.

## What it manages

Under `files/tree/`, mirrored to `album_sorter_home`
(`/home/muse/album_sorter_dev`) on the container:

- `album_sorter/` — the package (the sorter itself).
- `run_pipeline.py` — the batch entry point.
- `validate_*.py`, `score_s2.py` — the validation harnesses and paired
  scorer used by the gated-adoption workflow (BACKLOG B35/B38).
- `tests/` — the unit suite (`tests/run_tests.py`).
- One deliberate gap: `reference/album-registry-photos.csv`, the
  runtime data file the package reads by default
  (`Config.registry_csv`), is **not tracked yet** — see the excluded
  list below. The copy already on the container stays in place,
  unmanaged, and the sorter keeps reading it from there.
- `requirements.txt` — the two real third-party dependencies
  (google-genai, pillow), pinned to the container's installed
  versions. The openai/anthropic SDKs that `providers.py` imports
  lazily (Phase F, B45) are not installed yet and are not listed.
- `experiments/` — one-off experiment, probe, check and operator
  scripts from the development tree (`exp_*.py`, `*_check.py`,
  `calibrate.py`, `fuse_albums.py`, `rename_album.py`, ...), kept
  under version control because they encode the validation
  methodology, but clearly separated from the deployable tree: nothing
  in the package imports them. `engine.jpg`/`picnic.jpg` are the
  fixture images `embedding_sample.py` reads.
- `README.md` — the sorter's own documentation from the dev tree.

The role also ensures `~/album-venv` exists and matches
`requirements.txt`, and that the `exp/` scratch directory exists.
The venv is managed with plain `python3 -m venv` / `pip install -r`
commands rather than ansible's pip module: the module requires the
`packaging` library on the target interpreter, which neither the
container's system python nor the venv carries (found on the first
live check run, 2026-10-26).

## What it deliberately does not manage

- **Runtime data**: no `*.sqlite3` (manifests, usage ledger), no
  `exp/` contents, no `*.jsonl`, logs or `__pycache__`. The manifests
  and photo trees live on the `/mnt/pixel` mount (see Host layout);
  the usage ledger lives in the working tree on the container. None
  of it is in this repo.
- **Secrets**: no API keys anywhere in this repo. The Gemini key is
  the container's `.env`/environment and stays there.
- **Excluded dev-tree material**: `sample_corpus/` and `demo/`
  (~310 MB of test media and demo output), `cal_*.json` (generated
  calibration output), `fix_cha.py` (one-off data surgery for a
  single, already-applied album correction), and the retired
  implementation under `reference/`.
- **The registry CSV** (`reference/album-registry-photos.csv`): it is
  real library data — album names from the user's own photo library,
  including the user's name in an album title — and is held out of
  git until the user decides how it should be handled (redacted data
  copy, or data kept out of the repo permanently). Until then the
  repo is the source for the code, not for that one data file.

## Non-goals

- **No manager integration.** The NAS photo-ingest manager
  (`photo_ingest_controller`) does not know this role exists; nothing
  here wires the sorter into it.
- **No scheduled or activated runs.** There is no cron, systemd unit
  or DSM task. The sorter is started by hand on the container, as
  today. Deploying this role never runs the sorter.
- **No activation playbook**, for the same reason: there is nothing
  to activate yet.

## Deploy

Inventory host `ds223j_muse` (group `album_sorter`) reaches the
container the same way `~/workspace/bin/nas-ssh` does: Tailscale SSH
to 100.126.164.83 as user `muse`, through the tailnet tunnel proxy.
The proxy helper reads the `tunnel_proxy` environment variable, so
export it first (the same derivation `nas-ssh` performs):

```sh
export tunnel_proxy="${HTTPS_PROXY%:*}:3130"
ansible-playbook playbooks/album-sorter.yml            # or site.yml --tags album_sorter
ansible-playbook playbooks/album-sorter.yml --check --diff   # plan only
```

The play runs unprivileged (`become: false`): user `muse` owns the
whole deployment. File ownership/mode on the container are not managed
(the NAS-backed home presents its own modes); content and the venv
are the contract.

## Interim edit loop

Until the repo copy becomes the working tree, development still
happens in `~/workspace/photo-pipeline/dev`:

1. Edit and test there (`python3 tests/run_tests.py`).
2. Mirror the changed files into `roles/album_sorter/files/tree/`
   (same relative paths) and commit. One standing difference to
   preserve when mirroring: personal names appearing in dev-tree
   comments and docstrings are redacted to "the user" in the repo
   copies — prose only; the code is otherwise identical.
3. Deploy with the playbook above, or keep using
   `~/workspace/bin/sync-to-container` for a quick container refresh —
   both write the same tree; the playbook additionally converges the
   venv.

When the flows are next revisited, the intent is for step 2 to
disappear: edit in the repo, deploy with ansible.
