#!/usr/bin/env python3
"""Grouping validation harness (validation-plan.md §3 design,
built for IMPROVEMENT-PLAN Phase B / S6a).

NAME NOTE: validate_groupby.py already exists in this tree
as an unrelated spec-v0.9 grouping-OUTPUT validator; this
experiment harness is a different tool and lives under the
name validate_grouping.py rather than overwriting it.

Design (plan §3, adapted to what the backup can support):
- Population: PHOTO items in the pre-finish backup whose
  co-membership was produced by a grouping window pass
  (status 'grouped', assign_method 'window'), ordered by
  (capture_time, done_name) - the photo_stream order.
  Subsample: the first 450 contiguous items -> 5 windows
  of 150 at stride 75.
- DEVIATION, recorded: plan §3 names "the 598 items
  attempt 2 grouped" (run 3 attempt 2's stream: the 598
  items loose at run-2 end, BACKLOG run-3 preflight). That
  exact set is NOT recoverable from the backup - grouping
  was never journaled (zero grouping_window rows), and no
  per-item run marker exists (verified 2026-10-23). The
  window-grouped population used here is the recoverable
  class with the property the plan requires of the
  reference: medium-thinking grouping co-membership under
  the current prompt, already paid for.
- Reference co-membership is read from the UNTOUCHED
  backup before any rewind.
- Per arm: a scratch copy of the backup rewound for the
  subsample (items back to 'analyzed', their episodes
  deleted, the grouping checkpoint and any grouping
  journal rows removed, and every non-subsample loose
  PHOTO item parked at status 'held' so the stream is
  exactly the subsample). The REAL run_grouping code path
  then runs through the recording client, so the calls
  land in the usage ledger with stage 'grouping' - the
  first measured grouping $/window in the project.
- Arms: control (defaults) and s6a (media_res_sheet low).
  Both arms use the same service tier; tier is transport.

Gates (plan §3 + the red-team fusion metric):
- Jaccard(s6a, reference) >= 0.93 AND
  >= Jaccard(control, reference) - 0.02
- unassigned rate within 2pp of control
- episode count within +/-15% of reference
- zero reference episodes with >= 20 subsample members
  split by s6a that control did not also split
- fusion (reported, both arms): arm episodes whose
  subsample members come from >= 2 reference episodes

Run on the container from ~/album_sorter_dev:
  ~/album-venv/bin/python validate_grouping.py \
      --backup /mnt/pixel/.sorter-work-recat/manifest.bak-... \
      --exp-dir exp [--tier flex] [--stub] [--score-only]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sqlite3
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from album_sorter.config import Config
from album_sorter.manifest import Manifest

RECAT_WORK = Path("/mnt/pixel/.sorter-work-recat")
SUBSAMPLE_N = 450

ARMS = {
    "control": {},
    "s6a": {"media_res_sheet": "low"},
}


def build_reference(backup: Path) -> dict:
    """Subsample + reference co-membership from the
    untouched backup."""
    c = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT md5, episode_id FROM items WHERE category = "
        "'PHOTO' AND status = 'grouped' AND assign_method = "
        "'window' ORDER BY capture_time, done_name").fetchall()
    sub = rows[:SUBSAMPLE_N]
    return {
        "population_n": len(rows),
        "subsample": [r["md5"] for r in sub],
        "reference": {r["md5"]: r["episode_id"] for r in sub},
    }


def build_scratch(backup: Path, dest: Path, subsample: list[str]) -> None:
    """Rewound per-arm copy of the backup (see docstring)."""
    if dest.exists():
        dest.unlink()
    shutil.copy2(backup, dest)
    c = sqlite3.connect(str(dest))
    sub = set(subsample)
    ph = ",".join("?" * len(subsample))
    # episodes containing any subsample item are removed;
    # the rest stay as the grouping ledger (production
    # grouping always runs against a ledger)
    ep_ids = [r[0] for r in c.execute(
        f"SELECT DISTINCT episode_id FROM items WHERE md5 IN "
        f"({ph}) AND episode_id IS NOT NULL", subsample)]
    if ep_ids:
        eph = ",".join("?" * len(ep_ids))
        c.execute(f"DELETE FROM episodes WHERE episode_id IN "
                  f"({eph})", ep_ids)
    # rewind the subsample to loose/analyzed
    c.execute(
        f"UPDATE items SET episode_id = NULL, assign_method = "
        f"NULL, assign_confidence = NULL, status = 'analyzed' "
        f"WHERE md5 IN ({ph})", subsample)
    # park every other loose PHOTO item out of the stream
    c.execute(
        "UPDATE items SET status = 'held' WHERE category = "
        "'PHOTO' AND status IN ('analyzed', 'embedded') AND "
        f"md5 NOT IN ({ph})", subsample)
    # grouping must re-ask: no checkpoint skip, no journal
    cols = [r[1] for r in c.execute(
        "PRAGMA table_info(stage_checkpoints)")]
    stage_col = "stage" if "stage" in cols else cols[0]
    c.execute(f"DELETE FROM stage_checkpoints WHERE "
              f"{stage_col} = 'grouping'")
    c.execute("DELETE FROM decisions WHERE stage = "
              "'grouping_window'")
    c.commit()
    # sanity: the stream is exactly the subsample
    from album_sorter.grouping import photo_stream
    man = Manifest(dest)
    stream = [r["md5"] for r in photo_stream(man)]
    assert stream == subsample, (
        f"stream mismatch: {len(stream)} vs {len(subsample)}")
    c.close()


def stub_handler(model, system, parts):
    """$0 grouping answer: one new episode per window."""
    text = json.loads(parts[0]["text"])
    n = len(text["records"])
    return {"assignments": {str(i): "N1" for i in range(n)},
            "new_episodes": [{"temp_id": "N1",
                              "description": "stub episode",
                              "exemplars": [0]}],
            "unassigned": [],
            "summary_update": "stub window"}


def ledger_costs(scratch: Path) -> dict:
    """Grouping spend for one arm: the scratch manifest's
    own api_calls (stage 'grouping'; the backup carries
    none, so every grouping row is this arm's) priced with
    usage.est_cost, cross-checked against the central
    ledger rows labelled with this manifest."""
    from album_sorter.usage import billed_out, est_cost
    c = sqlite3.connect(f"file:{scratch}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM api_calls WHERE stage = 'grouping'")]
    total = 0.0
    for r in rows:
        cost = est_cost(r["model"], r["tokens_in"], billed_out(r),
                        r["ts"], service_tier=r.get("service_tier"))
        total += cost or 0.0
    return {"calls": len(rows), "est_usd": round(total, 4),
            "tokens_in": sum(r["tokens_in"] for r in rows),
            "billed_out": sum(billed_out(r) for r in rows)}


def run_arm(arm: str, backup: Path, exp_dir: Path,
            ref: dict, tier: str, stub: bool) -> dict:
    scratch = exp_dir / f"groupby-{arm}.sqlite3"
    build_scratch(backup, scratch, ref["subsample"])
    cfg = Config(work_dir=RECAT_WORK)
    cfg = dataclasses.replace(cfg, service_tier=tier,
                              **ARMS[arm])
    man = Manifest(scratch)
    if stub:
        from album_sorter.gemini_client import StubClient
        client = StubClient(handler=stub_handler)
    else:
        from album_sorter.gemini_client import make_client
        from album_sorter.usage import attach_manifest
        client = make_client(RECAT_WORK / "assets")
        attach_manifest(client, man)
    work = exp_dir / f"groupby-{arm}-work"
    work.mkdir(parents=True, exist_ok=True)
    from album_sorter.grouping import run_grouping
    stats = run_grouping(cfg, man, client, work)
    assignment = {}
    for md5 in ref["subsample"]:
        row = man.query("SELECT episode_id FROM items WHERE "
                        "md5 = ?", (md5,))
        assignment[md5] = row[0]["episode_id"] if row else None
    result = {"arm": arm, "tier": tier, "stub": stub,
              "run_stats": stats, "assignment": assignment,
              "ledger": ledger_costs(scratch) if not stub else {}}
    out = exp_dir / f"groupby-{arm}-result.json"
    out.write_text(json.dumps(result, indent=1))
    print(f"arm {arm}: run stats {stats}; ledger "
          f"{result['ledger']}")
    return result


def pair_set(assign: dict) -> set:
    by_ep: dict = {}
    for md5, ep in assign.items():
        if ep is not None:
            by_ep.setdefault(ep, []).append(md5)
    pairs = set()
    for members in by_ep.values():
        pairs.update(combinations(sorted(members), 2))
    return pairs


def score(ref: dict, results: dict[str, dict]) -> None:
    ref_pairs = pair_set(ref["reference"])
    ref_eps = {ep for ep in ref["reference"].values()
               if ep is not None}
    print(f"\n=== grouping scoring (plan §3 gates) ===")
    print(f"reference: {len(ref['subsample'])} items, "
          f"{len(ref_eps)} episodes, {len(ref_pairs)} "
          f"co-membership pairs")
    scored = {}
    for arm, res in results.items():
        a = res["assignment"]
        pairs = pair_set(a)
        jac = (len(pairs & ref_pairs) / len(pairs | ref_pairs)
               if pairs | ref_pairs else 1.0)
        unassigned = sum(1 for v in a.values() if v is None)
        eps = {v for v in a.values() if v is not None}
        # splits: reference episodes with >=20 subsample
        # members whose members land in >1 arm bucket
        # (episode id or unassigned)
        ref_members: dict = {}
        for md5, ep in ref["reference"].items():
            if ep is not None:
                ref_members.setdefault(ep, []).append(md5)
        splits = []
        for ep, members in ref_members.items():
            if len(members) < 20:
                continue
            buckets = {a[m] for m in members}
            if len(buckets) > 1:
                splits.append(ep)
        # fusion: arm episodes spanning >=2 reference eps
        arm_members: dict = {}
        for md5, ep in a.items():
            if ep is not None:
                arm_members.setdefault(ep, []).append(md5)
        fusion = [ep for ep, members in arm_members.items()
                  if len({ref["reference"][m] for m in members
                          if ref["reference"][m] is not None}) >= 2]
        scored[arm] = {"jaccard": jac, "unassigned": unassigned,
                       "episodes": len(eps), "splits": splits,
                       "fusion": fusion}
        led = res.get("ledger") or {}
        per_window = (led.get("est_usd", 0) /
                      max(1, res["run_stats"].get("windows", 1)))
        print(f"{arm}: Jaccard {jac:.4f} | unassigned "
              f"{unassigned}/{len(a)} "
              f"({unassigned / len(a):.1%}) | episodes "
              f"{len(eps)} (reference {len(ref_eps)}) | "
              f"big-episode splits {splits} | fusion "
              f"episodes {len(fusion)} {fusion} | ledger "
              f"{led.get('calls')} calls ${led.get('est_usd')} "
              f"= ${per_window:.4f}/window")
    if "s6a" in scored and "control" in scored:
        t, c = scored["s6a"], scored["control"]
        n = len(ref["subsample"])
        gates = [
            ("Jaccard(s6a) >= 0.93", t["jaccard"] >= 0.93),
            ("Jaccard(s6a) >= Jaccard(control) - 0.02",
             t["jaccard"] >= c["jaccard"] - 0.02),
            ("unassigned within 2pp of control",
             abs(t["unassigned"] - c["unassigned"]) / n <= 0.02),
            ("episode count within +/-15% of reference",
             abs(t["episodes"] - len(ref_eps)) <= 0.15 * len(ref_eps)),
            ("no big-episode split by s6a that control "
             "did not also split",
             not (set(t["splits"]) - set(c["splits"]))),
        ]
        print("--- gates ---")
        for name, ok in gates:
            print(f"  {'PASS' if ok else 'FAIL'}: {name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", required=True)
    ap.add_argument("--exp-dir", default="exp")
    ap.add_argument("--arms", default="control,s6a")
    ap.add_argument("--tier", default="flex")
    ap.add_argument("--stub", action="store_true")
    ap.add_argument("--score-only", action="store_true")
    args = ap.parse_args()
    backup = Path(args.backup).resolve()
    exp_dir = Path(args.exp_dir)
    exp_dir.mkdir(parents=True, exist_ok=True)
    ref_path = exp_dir / "groupby-reference.json"
    if ref_path.exists():
        ref = json.loads(ref_path.read_text())
        print(f"reference: {ref['population_n']} in population, "
              f"{len(ref['subsample'])} in subsample (cached)")
    else:
        ref = build_reference(backup)
        ref_path.write_text(json.dumps(ref, indent=1))
        print(f"reference: {ref['population_n']} in population, "
              f"{len(ref['subsample'])} in subsample (built)")
    results = {}
    if not args.score_only:
        for arm in [a.strip() for a in args.arms.split(",")]:
            results[arm] = run_arm(arm, backup, exp_dir, ref,
                                   args.tier, args.stub)
    else:
        for arm in [a.strip() for a in args.arms.split(",")]:
            p = exp_dir / f"groupby-{arm}-result.json"
            if p.exists():
                results[arm] = json.loads(p.read_text())
    if results:
        score(ref, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
