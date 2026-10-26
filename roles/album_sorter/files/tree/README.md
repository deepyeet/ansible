# Album sorter — as-built behavior and operations

The sorter files the family's photo backlog into albums. It
reads originals from the NAS share's `3_Done`, decides what
each photo is and which set it belongs to, and MOVES files
into `4_Sorted` — into named albums for notable occasions,
month buckets for everyday photos, and category folders for
the rest. A human reviews the result and uploads
from `4_Sorted`; the sorter never talks to Google Photos.

This README describes the system AS BUILT (2026-10-21,
spec v1.6, suite 143/143). Authority order when documents
disagree: the code wins, then this README, then the spec
(`~/workspace/goals/photo-pipeline-and-album-automation/
files/ALBUM-SPEC-DRAFT.md` — the design intent), then
BACKLOG.md in the same folder (open work and full decision
history). Procedures for running/reviewing/correcting live
in the `local_album_sorter` skill; this file is the
behavior reference the skill points at.

## Where it runs, what it touches

- Code: this folder (`album_sorter/` package + drivers).
  Developed on the Muse VM, executed in the `ds223j-muse`
  container on the Synology NAS (`~/album_sorter_dev`,
  venv `~/album-venv`), where the pixel share is mounted
  at `/mnt/pixel` — local-speed, no network copies.
- Production roots (env `ALBUM_PROD_ROOT=/mnt/pixel` style
  Config): done=`/mnt/pixel/3_Done`, sorted=`/mnt/pixel/
  4_Sorted`, work=`/mnt/pixel/.sorter-work` (manifest
  SQLite, derived-image asset cache, single-instance lock,
  manifest backups — also mirrored off-volume). The runner
  resolves each root from flag > env > root-relative
  default (`--src-dir`/`ALBUM_DONE_DIR`, `--work-dir`/
  `ALBUM_WORK_DIR`, `--sorted-dir`/`ALBUM_SORTED_DIR`), so
  an alternate batch can run from another share folder
  with its OWN work dir/manifest into the same `4_Sorted`
  (B30, e.g. `--src-dir tmp_recategorize --work-dir
  .sorter-work-recat`). The runner refuses a missing src
  dir and any src/work/sorted that coincide or nest.
- The sorter is strictly downstream. The phone pipeline
  (pixel_manager on DSM, running as user `muse`) fills
  `0_Inbox → 1_Queue → 2_Uploading → 3_Done` and never
  looks back; the sorter never writes outside `3_Done`
  (reads originals) and `4_Sorted` / `.sorter-work`
  (writes). It cannot break the upload pipeline.
- Gemini API calls must originate from the container
  (direct egress from the Muse VM times out). API key in
  `~/album_sorter_dev/.env` (mode 600) on the container and
  this folder's `.env` locally; funded with a $16 credit
  (2026-10-15) — measured spend for the full 2,187-item
  backlog run was ~$3.

## The pipeline, stage by stage

Order in `run_pipeline.py` (each stage is also a CLI
subcommand — see Operations):

1. **Ingest.** Scans `3_Done`, hashes every file (MD5 is
   identity), verifies the 8-char filename checksum suffix
   the upstream manager appended (only verified names are
   later stripped), extracts EXIF/ffprobe metadata, and
   derives a small analysis image per item into the asset
   cache (videos: a poster frame at ~25%). Persists
   capture time (raw + resolved), device, software, GPS
   pair (both axes or neither), and `gps_utc`. A file that
   changes size after ingest is re-ingested under its true
   identity; placement refuses a source whose size or
   hash no longer matches its record.
2. **Asset rebuild.** Self-healing sweep: derived assets
   that vanished (cache pruned, crash) are rebuilt from
   originals for unplaced items; a missing original is
   reported, not fatal.
3. **Clock correction.** Per device (EXIF Make+Model,
   Software fallback), offset = median(GPS UTC − local
   reading) over items carrying both stamps; a mid-stream
   clock reset shows up as two tight offset clusters and
   becomes a step change. The offset applies device-wide
   and every rewrite starts from the raw time, so re-runs
   are idempotent. Placed items are never rewritten.
   Devices with <3 GPS samples stay unanchored — until
   GPS-tagged photos enter intake, this stage is a no-op
   on real data (backlog B15).
4. **Phase 1 — classify + analyze (one call per item,
   Flash-Lite).** Category first: PHOTO / MISC / MEDICAL
   (deterministic pre-rules for screenshots, documents,
   collages; the model decides the rest). Photos get a
   rich per-item analysis record (scene, subjects, text
   seen, setting). MISC/MEDICAL leave the photo stream
   here and go to `__Misc` / `Medical` untouched by every
   later stage. A failed CALL is not a verdict (B32): the
   item stays `ingested` with the error recorded and the
   next run retries it; if every item fails, the run
   aborts loudly instead of proceeding on an empty state.
5. **Grouping (Flash — decided B11 on measured cost and
   agreement with Pro).** Time-ordered windows (150 items,
   stride 75) over the analyzed photo stream; the model
   proposes episodes; deterministic code validates,
   reconciles overlaps by centrality, and writes all
   assignments in ONE final pass — now a single
   transaction with the completion checkpoint, so a crash
   mid-pass leaves zero partial assignments; orphan
   pending episodes are garbage-collected on the next
   run. Each window sees a ledger of existing episodes —
   including episodes created earlier in the same run,
   with provisional spans — so an occasion split across
   windows or across runs still forms one episode. A
   completed grouping is checkpointed (see Resume support
   below): an unchanged-input relaunch skips it outright.
6. **Merge pass.** Model-checked pass over episode pairs
   that are close in time and description; seam-split
   episodes fuse. Iterates until stable (max 3 passes).
   Verdicts — including "kept separate" — are journaled
   (see Resume support below).
7. **Trip rollup.** Episodes chained by travel signals and
   short gaps fuse into ONE album per trip ("roll trips up
   into one album" — the user's rule; Los Cabos is one
   456-photo album, not five). Verdicts journaled, as merge.
8. **Linkage.** Loose photos (analyzed, no episode) are
   re-checked against nearby episodes WITH exemplar images
   — the verifier sees up to 4 exemplar photos plus the
   candidate, not just text. Attached or left loose.
   Verdicts journaled, as merge. Candidate nomination is
   deterministic and bounded (B34): span containment or
   ≤3 days adjacency; a GPS strong match (<1 km to the
   nearest member) extends nomination only within 14 days;
   the GPS veto (>50 km) always applies; at most 5
   candidates per item, nearest first.
9. **Gates — one decision per episode.**
   - *Registry match:* the episode joins an existing album
     from the user's own library registry (1,078 albums
     harvested from his Google Photos DB) ONLY when it is
     the same specific event, series, or person. Event-TYPE
     similarity is not a match (a stranger's birthday party
     is not "Cha Birthday" — B28, a real misfiling).
     Legacy dumping grounds (`YYYY Snaps`, month buckets)
     can never match.
   - *Notability:* would this family make this its own
     album? Judged on the OCCASION, never the photo count
     ("2026 kids ate dinner" is not an album at any count;
     a concert is one at 89 photos). Gray-zone episodes go
     to month buckets flagged *borderline* in the report
     for human promotion.
   - *Naming:* see the evidence policy below.
   - Anything else → month bucket (`YYYY <MonthName>`),
     per the item's own corrected month.
10. **Placement (deterministic; the only stage that moves
    files).** See Safety + the sweep rule below.
11. **Audit + report.** The invariant audit runs after
    placement (below); the report lands in `4_Sorted/
    _report-<ts>.md` with per-episode outcomes, borderline
    list, name spot-check sections, token/cost totals.

## Naming: the evidence policy (B27)

A specific album name is a factual claim about the
episode, and claims need evidence from the photos:

- The gate proposes a specific name only with a declared
  basis — `signage` (readable text in the photos names it)
  or `famous_landmark` (the unmistakable landmark itself
  in frame) — plus a generic fallback name built from
  activity words.
- Before a specific name may be used, a verification call
  checks it against the episode's exemplar IMAGES:
  evidenced only if signage quotes it, or the landmark is
  actually in frame. A generic shoreline/office/street
  evidences nothing specific.
- Unverified → the generic fallback name; the episode
  still gets its album, and the rejected name is recorded
  in its reason. A verifier failure (API, parse) also
  resolves to "not evidenced" — never blocks a run.
- The basis is stored per episode; the report lists every
  verified specific name and every unverified fallback for
  one-glance human review.

There is deliberately NO name denylist. One was built and
rejected by the user the same day: a place name is not
wrong in itself (he may visit Rattlesnake Lake for real),
a blacklist does not scale, and it confuses an
episode-specific error with a permanent ban. The failure
that motivates the policy: the model named an outing
"Rattlesnake Lake" from narration alone — the lake was
actually Lake Keechelus — and coined "Metaversary" for a
family visit to an office. Model-inferred names are
claims, not facts.

## Resume support: decision journal + checkpoints (B32)

Built 2026-10-21 after B30 run 2 died mid-linkage on a
Gemini 402: positive stage outcomes always persisted in
the manifest, but NEGATIVE model verdicts were stored
nowhere, so any downstream failure re-bought every "kept
separate" / "not a trip" / "does not belong" on relaunch.

- **Decision journal** (`decisions` table, key logic in
  `album_sorter/journal.py`). Every paid verdict in merge,
  rollup, linkage, gates, and name verification is stored
  under a hash of the exact question: the canonical
  payload, the model string, and a hash of the system
  prompt (plus derived-image parameters for image-bearing
  calls, and the registry file's hash for gates). No
  semantic key is a bare row id — episodes are deleted,
  retired, and mutated in place; content is the identity.
  (Where a payload itself embeds an id, as the merge
  briefs do, the id rides along in the hash — harmless
  inside one manifest, where ids are never reused.
  Transport/render constants in code — temperature, JPEG
  quality, sheet cell size — are part of the code version,
  not the key.)
  On replay, the CURRENT code thresholds are re-applied
  to the stored verdict, so a rule change never re-buys a
  call, and any change to the question (an episode grew,
  a clock correction landed, the prompt or model changed)
  re-asks automatically. First verdicts are sticky by
  design; `cli purge [--stage S]` is the supported
  re-judge path (purging `gate` also purges `name_verify`),
  and stage stats report `replayed` counts so the reuse
  is visible. Gate decisions are journaled for provenance
  (raw verdict, registry hash, tokens) with replace
  semantics — a re-gate overwrites its own record, so the
  journal always describes the verdict that produced the
  current state; their skip mechanism remains the
  persisted outcome.
  A FAILED name verification is never journaled — failure
  is not disproof.
- **Grouping checkpoint** (`stage_checkpoints` table).
  Grouping is the one monolithic stage, so it gets a
  completion record instead: a fingerprint over the full
  PHOTO population (md5, corrected capture time, analysis
  hash) plus grouping config/model/prompt. A relaunch
  whose fingerprint still matches skips grouping
  outright — that is what "resume the same batch" means;
  any new or changed item changes the fingerprint and the
  designed regroup of the unassigned stream runs. A death
  INSIDE grouping still re-buys its windows (the window
  outputs are in-memory); the final assignment pass now
  commits atomically with the checkpoint.
- **Transactions.** `Manifest.transaction()` brackets a
  real unit of work (explicit BEGIN/COMMIT over an
  autocommit connection). Journal writes and the state
  changes they drive commit together — merge applies,
  rollup fusions, linkage attaches, gate outcomes, and
  grouping's final pass can no longer be left
  half-applied. Journal mode stays the SQLite default:
  `backup()` copies the DB file, which would silently
  omit WAL contents.
- The journal doubles as the durable spend record: it
  stores tokens per verdict, so what a crashed run
  already bought is answerable from the manifest (the
  report prints journal totals), unlike the client's
  in-memory usage counters, which die with the process.

## Placement semantics and the sweep

- Placement is a rename within the share: atomic, no bytes
  copied, NOTHING is ever deleted by this package — the
  only mutation of user files is that one `rename()`.
- Never-overwrite is enforced at three layers: plan-time
  collision resolution, sequential lazy execution (each
  item's plan sees prior renames), and an execution-time
  re-check that refuses (`blocked`) if the target appeared
  since planning. An exact-duplicate target leaves the
  source in place (`dupe_left`); a different-content
  collision keeps the checksum suffix (`kept_suffix`).
- Crash protocol: the manifest records `placing` + final
  path before the rename and `placed` after; a stranded
  `placing` row is reconciled on the next run by checking
  which side the file is actually on. Before any placing,
  the manifest is snapshotted (and mirrored off-volume).
- **Leftover sweep, age-gated (B29, spec §13 as amended
  v1.6):** a loose photo enters its month bucket only when
  its corrected capture time is at least
  `sweep_min_age_days` (default 60) old. Younger loose
  photos, timeless ones, and members of ungated episodes
  stay in `3_Done`, in no album. Rationale: the user
  uploads from the sorted tree, so a premature bucket
  would lock the photo out of any album that forms later;
  grouping and linkage reconsider held photos every run.
  Swept items are counted separately in placement stats
  (`swept`, assign_method=gate).
- One destination per item, by construction: a single
  `final_path` per row. Duplication is impossible; the
  audit verifies it.

## Invariant audit (`album_sorter/audit.py`)

Read-only; runs inside the pipeline after placement and as
`cli audit` (exit 1 on violations). Checks: no two placed
items share a `final_path`; every placed file exists;
every `final_path` lives under the sorted root; album
outcomes always carry a name; episode `item_count` agrees
with membership; no file sits in an album folder that the
manifest doesn't claim. Loose held photos are counted and
reported, never silently zero. Production state after the
full backlog run and all corrections: 0 violations over
2,140 placed items.

## Models and cost (measured, not rate-card)

- Phase 1: `gemini-3-flash-lite`-class tier, ~$0.71 per
  1,000 items. Grouping + gates + merge/rollup: Flash
  (`gemini-3.8-flash`), ~$0.35–0.45 per 1,000. Linkage
  does NOT follow a per-1,000 rate: it scales with
  candidates per loose item. Measured on the 2026-10-22
  recat batch (journal-exact): 696 verifier calls for 80
  items, $2.75, every verdict "no" — 84.5% of the pairs
  were >30 days apart, nominated by the then-unbounded
  GPS strong-include. B34 bounds nomination (14-day GPS
  window, 5-candidate cap); project any linkage pass from
  the journal's candidates-per-item, not a flat rate.
  Flash grouping beat Pro on agreement-
  adjusted value: 0.91 co-membership Jaccard, 160 s vs
  1,428 s, $0.074 vs $0.199 per 211 items (B11).
- Spend accounting (B33): every completed API call is
  recorded at call time in the manifest's `api_calls`
  table and in a central `usage-ledger.sqlite3` beside
  the code tree (dev/calibration clients included), with
  stage and subject attribution. `cli usage` reports a
  manifest's spend by stage; `cli usage --central` gives
  the account-wide view. Costs are estimated at report
  time from the dated price table in `usage.py`; Google's
  billing page remains the account authority.
- Embeddings are deliberately absent from v1: the manifest
  keeps nullable columns, and a backfill job (~$0.00013/
  image) is the documented return path IF linkage misses
  ever concentrate in low-time-confidence items. Every
  subsystem must earn its place.

## Operations

```
# full pipeline (dry-run placement unless --place)
~/album-venv/bin/python run_pipeline.py [--place] [--limit N]

# alternate source batch, own manifest, same sorted tree (B30)
~/album-venv/bin/python run_pipeline.py \
    --src-dir tmp_recategorize --work-dir .sorter-work-recat

# individual stages / inspection (dev or container)
python3 -m album_sorter.cli ingest|phase1|group|merge|rollup|link|gate
python3 -m album_sorter.cli place [--dry-run]
python3 -m album_sorter.cli audit|report|stats
python3 -m album_sorter.cli decisions          # B32 journal + checkpoints
python3 -m album_sorter.cli purge [--stage S]  # forget verdicts, force re-judge

# tests (dependency-free runner, auto-discovers test_*.py)
python3 tests/run_tests.py        # 143/143 as of 2026-10-22
```

- Sync code VM → container with `~/workspace/bin/
  sync-to-container <paths...>` (md5-verified copy; never
  hand-copy). Long container jobs run DETACHED (nohup/
  setsid, log to file): SSH tunnels through the egress
  proxy drop idle connections, and processes launched over
  SSH may outlive the client — always check for a running
  process before relaunching.
- One-off correction drivers live beside the package:
  `fuse_albums.py` (fuse episode folders into one album),
  `rename_album.py` (rename an album + fix a description),
  `fix_cha.py` (the B28 correction). All are rename-only,
  lock-held, backup-first, manifest-consistent.
- Mutating CLI commands take the work-dir flock (a second
  run exits 3) and honor `ALBUM_PING_URL` (healthchecks-
  style success/fail ping; unconfigured in production).
- Reports are the review surface: spot-check names,
  borderline episodes, unverified fallbacks, and the
  audit line before uploading from `4_Sorted`.

## Failure modes worth knowing

- Malformed model JSON is a semantic failure, not a
  transport one: it goes through the retry-with-errors
  contract once (the model sees its own validation errors)
  and is never burned on identical retries (the first B5
  attempt died exactly that way; state survived intact
  because grouping writes in one final pass).
- A poison item cannot wedge Phase 1 (per-item isolation).
- NAS storage has slow periods (multi-second fsync
  stalls); SQLite commit latency ~1 s at worst observed.
  Long runs are normal, not hung — check the log, not the
  clock.
- Wrapped non-photo blobs (`9b 7a 38 00` magic, upstream
  source unidentified — B19) arrive via the phone pipeline
  and sit in `3_Done`/incoming limbo; they are not the
  sorter's to fix.

## Layout

- `album_sorter/` — config, manifest, metadata, ingest,
  assets, clockfix, phase1, grouping_logic, grouping,
  windows, linkage, gates, naming, merge, rollup, place,
  audit, report, gemini_client, locking, cli, ping.
- `tests/` — stub-client unit + integration tests
  (no network, no key needed).
- `reference/` — the harvested album registry
  (`album-registry-photos.csv`, 1,078 albums / 169,315
  items from the phone's Photos DB) and WSL-era scripts.
- `sample_corpus/` — 15 real files for local dev; real
  runs belong in the container (the VM disk is small).
- `demo/` — end-to-end stub demo on a corpus copy.
