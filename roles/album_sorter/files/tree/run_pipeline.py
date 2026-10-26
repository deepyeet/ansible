"""Production runner for the album sorter (backlog B5, B30).

Runs the full pipeline against the NAS pixel share from inside
the ds223j-muse container:

  ALBUM_PROD_ROOT=/mnt/pixel \
  ~/album-venv/bin/python run_pipeline.py [--place] [--limit N]

Directory layout is parameterized, never hardcoded per run.
Each of the three roots resolves, in order, from a CLI flag,
an environment variable, or a default relative to the share
root:

  root    --root        ALBUM_PROD_ROOT   /mnt/pixel
  src     --src-dir     ALBUM_DONE_DIR    <root>/3_Done
  work    --work-dir    ALBUM_WORK_DIR    <root>/.sorter-work
  sorted  --sorted-dir  ALBUM_SORTED_DIR  <root>/4_Sorted

Relative flag/env values resolve against the root. The env
names are the Config package's own (config.py), so the CLI
subcommands and this runner agree on vocabulary. With no
overrides the layout is exactly the historical production one:

  3_Done/       input (read by ingest; placement moves out of it)
  .sorter-work/ manifest + assets + sheets - on the btrfs share
                itself, NOT the ecryptfs home (backlog B3: the
                home mount is the ingest bottleneck)
  4_Sorted/     output

Alternate-source runs (B30): point src at another folder on
the share and give it its OWN work dir, while sorted stays
the production tree - e.g. re-categorizing a legacy batch:

  ~/album-venv/bin/python run_pipeline.py \
      --src-dir tmp_recategorize --work-dir .sorter-work-recat

A separate work dir means a separate manifest: the batch is
analyzed fresh, the production manifest (and the items it
holds in 3_Done) is untouched, and placement into the shared
sorted tree keeps its usual collision rules (never overwrite;
exact dupes left in place). Two runs that share a sorted dir
must never place at the same time - each work dir has its own
lock, so the lock does NOT serialize across work dirs.

Ingest parallelism: --ingest-workers (env ALBUM_INGEST_WORKERS)
sets the ingest process-pool width; the default is 1. Each
worker holds a full-res decode (~113 MB RSS), and on the
DS223j's ~1 GB of RAM three workers plus DSM's own ~560 MB
baseline put the host at its memory ceiling - the 2026-10-18
B30 run swap-thrashed in ingest for ~68 h without finishing.
One worker keeps total demand well under the ceiling; raise
it only on hosts with RAM to spare.

Safety rails: the src dir must exist; src, work and sorted
must be pairwise distinct and none may nest inside another
(a work dir inside src would feed the sorter its own assets;
a sorted dir inside src would file albums into the intake).

Placement is a separate, explicit step: without --place the run
stops after the report with a dry-run placement plan printed.
With --place it moves files for real (manifest-logged,
reversible). Grouping uses Flash (calibration A/B winner);
override with --grouping-model.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from album_sorter import gates, grouping, ingest as ing, linkage, merge, phase1, place, rollup
from album_sorter.config import Config
from album_sorter.gemini_client import make_client
from album_sorter.locking import AlreadyRunning, run_lock
from album_sorter.manifest import Manifest
from album_sorter.ping import ping
from album_sorter.report import write_report


def _resolve(value: str | None, env_name: str, root: Path, default_name: str) -> Path:
    """One directory from flag > env > root-relative default.
    Relative results resolve against the share root."""
    raw = value or os.environ.get(env_name) or default_name
    p = Path(raw)
    return p if p.is_absolute() else root / p


def resolve_layout(args) -> tuple[Path, Path, Path, Path]:
    root = Path(args.root or os.environ.get("ALBUM_PROD_ROOT", "/mnt/pixel"))
    src = _resolve(args.src_dir, "ALBUM_DONE_DIR", root, "3_Done")
    work = _resolve(args.work_dir, "ALBUM_WORK_DIR", root, ".sorter-work")
    sorted_dir = _resolve(args.sorted_dir, "ALBUM_SORTED_DIR", root, "4_Sorted")
    return root, src, work, sorted_dir


def check_layout(src: Path, work: Path, sorted_dir: Path) -> str | None:
    """Return an error string, or None if the layout is sane."""
    if not src.is_dir():
        return f"src dir does not exist: {src}"
    resolved = [(name, p.resolve())
                for name, p in (("src", src), ("work", work), ("sorted", sorted_dir))]
    for i, (name_a, a) in enumerate(resolved):
        for name_b, b in resolved[i + 1:]:
            if a == b:
                return f"{name_a} and {name_b} are the same directory: {a}"
            if a in b.parents or b in a.parents:
                return f"{name_a} ({a}) and {name_b} ({b}) must not nest"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(prog="run_pipeline")
    ap.add_argument("--place", action="store_true",
                    help="actually move files (default: report + dry-run plan only)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap Phase 1 items (testing)")
    ap.add_argument("--grouping-model", default="gemini-3.8-flash",
                    help="grouping tier (calibration A/B winner: Flash)")
    ap.add_argument("--root", default=None,
                    help="share root (env ALBUM_PROD_ROOT, default /mnt/pixel)")
    ap.add_argument("--src-dir", default=None,
                    help="input dir (env ALBUM_DONE_DIR, default <root>/3_Done); "
                         "relative values resolve against the root")
    ap.add_argument("--work-dir", default=None,
                    help="manifest/assets dir (env ALBUM_WORK_DIR, "
                         "default <root>/.sorter-work)")
    ap.add_argument("--sorted-dir", default=None,
                    help="output dir (env ALBUM_SORTED_DIR, default <root>/4_Sorted)")
    ap.add_argument("--ingest-workers", type=int, default=None,
                    help="ingest process-pool width (env ALBUM_INGEST_WORKERS, "
                         "default 1; each worker holds ~113 MB - see docstring)")
    ap.add_argument("--service-tier", default="standard",
                    choices=["standard", "flex"],
                    help="Gemini service tier for this run (B35). "
                         "Transport only - never decision-key "
                         "material - so a tier choice cannot orphan "
                         "banked verdicts. Flex is 50%% of standard "
                         "rates with minute-scale latency.")
    ap.add_argument("--spend-cap", type=float, default=0.0,
                    help="hard API spend cap in USD for this invocation "
                         "(B35; 0 = no cap). Enforced by the recording "
                         "client before each call; a capped run stops "
                         "cleanly and can be resumed.")
    args = ap.parse_args()

    workers = args.ingest_workers
    if workers is None and os.environ.get("ALBUM_INGEST_WORKERS"):
        try:
            workers = int(os.environ["ALBUM_INGEST_WORKERS"])
        except ValueError:
            print("refusing: ALBUM_INGEST_WORKERS is not an integer: "
                  f"{os.environ['ALBUM_INGEST_WORKERS']!r}", file=sys.stderr)
            return 2
    if workers is None:
        workers = 1
    if workers < 1:
        print(f"refusing: --ingest-workers must be >= 1 (got {workers})",
              file=sys.stderr)
        return 2

    root, src, work, sorted_dir = resolve_layout(args)
    error = check_layout(src, work, sorted_dir)
    if error:
        print(f"refusing: {error}", file=sys.stderr)
        return 2
    print(f"layout: root={root} src={src} work={work} sorted={sorted_dir} "
          f"ingest_workers={workers}", flush=True)

    cfg = Config(done_dir=src,
                 work_dir=work,
                 sorted_dir=sorted_dir,
                 grouping_model=args.grouping_model,
                 ingest_workers=workers,
                 service_tier=args.service_tier,
                 spend_cap_usd=args.spend_cap)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    man = Manifest(cfg.manifest_path)
    t0 = time.time()
    try:
        with run_lock(cfg.work_dir):
            from album_sorter.usage import attach_manifest, UsageRecordingClient
            client = attach_manifest(make_client(cfg.assets_dir), man)
            if isinstance(client, UsageRecordingClient) and cfg.spend_cap_usd > 0:
                client.budget_usd = cfg.spend_cap_usd
                print(f"spend cap: ${cfg.spend_cap_usd:.2f} for this batch "
                      f"(${client.spent_usd:.2f} already spent per the ledger; "
                      f"enforced per call by the recording client)", flush=True)
            print("ingest:", ing.ingest(cfg, man), flush=True)
            print("asset-rebuild:", ing.rebuild_missing_assets(cfg, man), flush=True)
            from album_sorter.clockfix import apply_clock_corrections
            print("clockfix:", apply_clock_corrections(cfg, man), flush=True)
            print("phase1:", phase1.run_phase1(cfg, man, client, limit=args.limit), flush=True)
            print("grouping:", grouping.run_grouping(cfg, man, client, cfg.work_dir), flush=True)
            print("merge:", merge.run_merge(cfg, man, client), flush=True)
            print("rollup:", rollup.run_rollup(cfg, man, client), flush=True)
            loose = man.query("SELECT * FROM items WHERE category = 'PHOTO' "
                              "AND episode_id IS NULL AND status = 'analyzed' "
                              "ORDER BY capture_time, done_name")
            lstats = {"called": 0, "replayed": 0}
            attached = sum(1 for it in loose
                           if linkage.try_link(cfg, man, client, it, lstats))
            print(f"linkage: {attached}/{len(loose)} loose photos attached "
                  f"(verifier calls: {lstats['called']}, "
                  f"journal replays: {lstats['replayed']}, "
                  f"candidates nominated: {lstats.get('nominated', 0)}, "
                  f"pruned by B34 cap/window: {lstats.get('pruned', 0)})",
                  flush=True)
            registry = gates.load_registry(cfg)
            outcomes = {}
            for ep in man.all_episodes():
                if ep["outcome"] == "pending":
                    out = gates.apply_gate(cfg=cfg, man=man, client=client,
                                           registry=registry, episode_id=ep["episode_id"])
                    outcomes[out] = outcomes.get(out, 0) + 1
            print("gates:", outcomes, flush=True)
            if args.place:
                print("place:", place.place_all(cfg, man, cfg.done_dir), flush=True)
            else:
                plan = place.plan_placements(cfg, man, cfg.done_dir)
                print("place (dry-run):", json.dumps(plan["counts"]), flush=True)
            from album_sorter.audit import audit_manifest
            audit = audit_manifest(cfg, man)
            print("audit:", json.dumps(audit), flush=True)
            if audit["violations"]:
                print("AUDIT VIOLATIONS PRESENT - see above", flush=True)
            report = write_report(cfg, man, cfg.sorted_dir / f"_report-{int(t0)}.md")
            print("report:", report, flush=True)
            print("usage:", json.dumps(client.usage_summary()), flush=True)
            _sf = getattr(client, "sink_failures", 0)
            if _sf:
                print(f"WARNING: {_sf} usage/spill records failed to "
                      "persist (see sink accounting)", flush=True)
            print(f"total seconds: {time.time() - t0:.0f}", flush=True)
    except AlreadyRunning as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        from album_sorter.gemini_client import BudgetExceeded
        if isinstance(e, BudgetExceeded):
            # B35: an intentional stop, not a crash - every
            # verdict bought so far is journaled and the run
            # resumes where it stopped. Report and exit with
            # a distinct code; the healthcheck still sees a
            # not-completed run (a human decides: raise the
            # cap or leave it parked).
            print(f"stopped: {e}", flush=True)
            print("usage:", json.dumps(client.usage_summary()), flush=True)
            _sf = getattr(client, "sink_failures", 0)
            if _sf:
                print(f"WARNING: {_sf} usage/spill records failed to "
                      "persist (see sink accounting)", flush=True)
            print(f"total seconds: {time.time() - t0:.0f}", flush=True)
            ping(False)
            return 4
        ping(False)
        raise
    ping(True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
