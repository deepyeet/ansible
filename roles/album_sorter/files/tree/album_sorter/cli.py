"""CLI entry point. The phases below run independently (each
is a function of manifest state); the full chain in order is
run_pipeline.py. Every phase the runner performs is also a
CLI subcommand (B23):

  python -m album_sorter.cli ingest
  python -m album_sorter.cli phase1     # needs a Gemini client
  python -m album_sorter.cli group      # needs a Gemini client
  python -m album_sorter.cli merge      # needs a Gemini client
  python -m album_sorter.cli rollup     # needs a Gemini client
  python -m album_sorter.cli link       # needs a Gemini client
  python -m album_sorter.cli gate       # needs a Gemini client
  python -m album_sorter.cli place [--dry-run]
  python -m album_sorter.cli audit      # read-only invariant check
  python -m album_sorter.cli report
  python -m album_sorter.cli stats
  python -m album_sorter.cli decisions  # B32 journal/checkpoint state
  python -m album_sorter.cli purge [--stage S]  # forget stored verdicts
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import gates, grouping, ingest as ingest_mod, linkage, merge, phase1, place, rollup
from .audit import audit_manifest
from .config import Config
from .gemini_client import make_client
from .locking import AlreadyRunning, run_lock
from .manifest import Manifest
from .report import write_report
from .usage import attach_manifest

MUTATING = {"ingest", "phase1", "group", "merge", "rollup", "link", "gate", "place", "purge"}


def _client(cfg, man):
    """A client whose completed calls are recorded in this
    manifest's usage ledger as well as the central one (B33).
    When cfg.spend_cap_usd is set, the batch cap applies to
    CLI stage runs exactly as it does to run_pipeline."""
    client = attach_manifest(make_client(cfg.assets_dir), man)
    from .usage import UsageRecordingClient
    if isinstance(client, UsageRecordingClient) and cfg.spend_cap_usd > 0:
        client.budget_usd = cfg.spend_cap_usd
    return client


def _dispatch(args, cfg, man) -> None:
    if args.command == "ingest":
        print(json.dumps(ingest_mod.ingest(cfg, man), indent=2))
    elif args.command == "phase1":
        print(json.dumps(phase1.run_phase1(cfg, man, _client(cfg, man)), indent=2))
    elif args.command == "group":
        print(json.dumps(grouping.run_grouping(cfg, man, _client(cfg, man), cfg.work_dir), indent=2))
    elif args.command == "merge":
        print(json.dumps(merge.run_merge(cfg, man, _client(cfg, man)), indent=2))
    elif args.command == "rollup":
        print(json.dumps(rollup.run_rollup(cfg, man, _client(cfg, man)), indent=2))
    elif args.command == "link":
        client = _client(cfg, man)
        loose = man.query("SELECT * FROM items WHERE category = 'PHOTO' "
                          "AND episode_id IS NULL AND status = 'analyzed' "
                          "ORDER BY capture_time, done_name")
        lstats = {"called": 0, "replayed": 0}
        attached = sum(1 for it in loose
                       if linkage.try_link(cfg, man, client, it, lstats))
        print(json.dumps({"loose": len(loose), "attached": attached,
                          "verifier_calls": lstats["called"],
                          "journal_replays": lstats["replayed"],
                          "candidates_nominated": lstats.get("nominated", 0),
                          "candidates_pruned": lstats.get("pruned", 0)}, indent=2))
    elif args.command == "decisions":
        # B32: what the resume journal holds, and which stage
        # checkpoints are recorded - the operator's window
        # into what a relaunch would replay vs. re-ask
        checkpoints = {r["stage"]: {"fingerprint": r["fingerprint"][:12],
                                     "completed": r["completed"]}
                       for r in man.query("SELECT * FROM stage_checkpoints")}
        print(json.dumps({"decisions_by_stage": man.decision_counts(),
                          "checkpoints": checkpoints}, indent=2))
    elif args.command == "usage":
        # B33: what this manifest's runs actually spent, per
        # stage, from the per-call ledger (survives crashed
        # runs). --central reads the shared ledger instead -
        # every manifest plus dev/calibration clients - for
        # the account-wide "where is the money going" view.
        from .usage import attempt_rows, central_rows, summarize
        if getattr(args, "central", False):
            rows = central_rows()
            by_manifest: dict[str, list] = {}
            for r in rows:
                by_manifest.setdefault(r["manifest"] or "unattributed",
                                       []).append(r)
            # F-7 (B35 review): transport attempts (retries,
            # fail-fast stops) are accounting too - they are
            # how a dying account announces itself. Surface
            # their totals beside the billed calls.
            attempts = attempt_rows()
            by_error: dict[str, int] = {}
            for a in attempts:
                by_error[a["error_class"]] = \
                    by_error.get(a["error_class"], 0) + 1
            print(json.dumps(
                {"central_ledger_calls": len(rows),
                 "by_manifest": {k: summarize(v)
                                   for k, v in sorted(by_manifest.items())},
                 "transport_attempts": {"total": len(attempts),
                                        "by_error": by_error}},
                indent=2))
        else:
            # per-outcome counts make billed-but-failed calls
            # (unparseable, empty, error) visible in the
            # manifest's own view, not just the totals
            call_rows = man.api_call_rows()
            by_outcome: dict[str, int] = {}
            for r in call_rows:
                by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
            print(json.dumps(
                {**summarize(call_rows),
                 "by_outcome": by_outcome,
                 "raw_responses_spilled": len(man.raw_response_rows())},
                indent=2))
    elif args.command == "purge":
        # B32: the supported re-judge path - forget stored
        # verdicts (one stage, or all) so those questions are
        # asked again. Purging also drops the grouping
        # checkpoint when grouping is in scope: its verdicts
        # are the checkpoint. "gate" purges name_verify too -
        # to an operator they are one stage, and a re-gated
        # episode that silently replayed its old name
        # verification would not be the re-judge they asked
        # for.
        stage = getattr(args, "stage", None)
        stages = [stage] if stage else [None]
        if stage == "gate":
            stages.append("name_verify")
        if stage == "grouping":
            # B35: per-window verdicts are grouping's journal
            stages.append("grouping_window")
        removed = sum(man.purge_decisions(s) for s in stages)
        checkpoint_dropped = False
        if stage is None or stage == "grouping":
            man.delete_checkpoint("grouping")
            checkpoint_dropped = True
        print(json.dumps({"stage": stage, "decisions_removed": removed,
                          "grouping_checkpoint_dropped": checkpoint_dropped},
                         indent=2))
    elif args.command == "gate":
        client = _client(cfg, man)
        registry = gates.load_registry(cfg)
        outcomes = {}
        for ep in man.all_episodes():
            if ep["outcome"] == "pending":
                out = gates.apply_gate(cfg=cfg, man=man, client=client,
                                       registry=registry, episode_id=ep["episode_id"])
                outcomes[out] = outcomes.get(out, 0) + 1
        print(json.dumps(outcomes, indent=2))
    elif args.command == "audit":
        result = audit_manifest(cfg, man)
        print(json.dumps(result, indent=2))
        if result["violations"]:
            raise SystemExit(1)
    elif args.command == "place":
        if getattr(args, "dry_run", False):
            print(json.dumps(place.plan_placements(cfg, man, cfg.done_dir), indent=2))
        else:
            print(json.dumps(place.place_all(cfg, man, cfg.done_dir), indent=2))
    elif args.command == "report":
        p = write_report(cfg, man, cfg.sorted_dir / "_report-latest.md")
        print(f"report written: {p}")
    elif args.command == "stats":
        rows = man.query("SELECT status, COUNT(*) AS n FROM items GROUP BY status")
        print(json.dumps({r["status"]: r["n"] for r in rows}, indent=2))


PURGE_STAGES = ("merge", "rollup", "linkage", "gate", "name_verify", "grouping")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="album_sorter")
    ap.add_argument("command", choices=["ingest", "phase1", "group", "merge", "rollup", "link", "gate", "place", "audit", "report", "stats", "decisions", "purge", "usage"])
    ap.add_argument("--dry-run", action="store_true",
                    help="with `place`: compute the plan only, move nothing (backlog B2)")
    ap.add_argument("--central", action="store_true",
                    help="with `usage`: read the shared central ledger "
                         "(all manifests + dev clients) instead of this manifest")
    ap.add_argument("--stage", default=None, choices=PURGE_STAGES,
                    help="with `purge`: only this stage's verdicts "
                         "(gate also purges name_verify)")
    ap.add_argument("--spend-cap", type=float, default=0.0,
                    help="batch API spend cap in USD for stage "
                         "commands (0 = no cap); the counter is "
                         "seeded from this manifest's ledger")
    args = ap.parse_args(argv)
    cfg = Config()
    cfg.spend_cap_usd = args.spend_cap
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    man = Manifest(cfg.manifest_path)

    if args.command in MUTATING:
        # single-instance discipline (backlog B1): a second
        # mutating run refuses instead of racing the manifest.
        # Outcome pings (backlog B4) fire for real attempts.
        from .ping import ping
        try:
            with run_lock(cfg.work_dir):
                _dispatch(args, cfg, man)
        except AlreadyRunning as e:
            print(f"refusing: {e}", file=sys.stderr)
            return 3
        except Exception:
            ping(False)
            raise
        ping(True)
    else:
        _dispatch(args, cfg, man)
    return 0


if __name__ == "__main__":
    sys.exit(main())
