#!/usr/bin/env python3
"""B35 validation harness: gates + name_verify (plan §2 a4).

Method: run the REAL gates.apply_gate on per-arm copies
of the production manifest, so the measured outcome is
the full production path - verdict, code-side floors and
guards, and the name verification apply_gate itself
invokes for specific names. Ground truth: the recorded
production outcomes in ground-truth-production-
episodes.json (62 gated episodes; the 1 merged row is
not a gate sample).

Arms: control (default thinking; stratified 24) and
a4-low (thinking=low; all 62). The fixture episodes are
forced into the control subset: ep94 (Cha), ep63 (Meta
office), ep64 (Lake Keechelus).

Named fixtures (hard zero, both arms):
- ep94 must not registry-match '2026 Cha Birthday'.
- Direct verify_specific_name calls: '2026 Metaversary'
  against ep63 and '2026 Rattlesnake Lake' against ep64
  must NOT be evidenced.

Scoring (plan §2 a4): outcome agreement >= 95% for the
treatment arm and within 2pp of control; zero new
registry matches under treatment that control does not
also produce (listed for manual read, not auto-passed).
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from album_sorter import gates
from album_sorter.config import Config
from album_sorter.manifest import Manifest

SEED = 20261023
FIXTURE_EPS = {94, 63, 64}
VERIFY_FIXTURES = [
    (63, "2026 Metaversary"),
    (64, "2026 Rattlesnake Lake"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod-manifest", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--assets-dir", required=True)
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--exp-dir", required=True)
    ap.add_argument("--tier", default="standard")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    gt_rows = json.loads(Path(args.ground_truth).read_text())
    sample = [r for r in gt_rows
              if r["outcome"] in ("album_new", "month_bucket")]
    by_outcome = {"album_new": [], "month_bucket": []}
    for r in sample:
        by_outcome[r["outcome"]].append(r)
    rng = random.Random(SEED)
    control_ids = set(FIXTURE_EPS)
    for outcome, rows in by_outcome.items():
        pool = [r["episode_id"] for r in rows
                if r["episode_id"] not in control_ids]
        rng.shuffle(pool)
        control_ids.update(pool[:12 - sum(
            1 for r in rows if r["episode_id"] in FIXTURE_EPS
            and r["outcome"] == outcome)])
    print(f"sample: {len(sample)} gated episodes; control "
          f"subset: {len(control_ids)} (fixtures forced in)")
    if args.dry_run:
        return 0

    exp_dir = Path(args.exp_dir)
    results_path = exp_dir / "gates-results.jsonl"
    done = set()
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if not r.get("error"):
                    done.add((r["arm"], r["episode_id"],
                              r["kind"]))
    out = results_path.open("a")

    arms = [("control", None, control_ids),
            ("a4-low", "low", {r["episode_id"] for r in sample})]
    for arm_name, thinking, ep_ids in arms:
        copy_path = exp_dir / f"copy-gates-{arm_name}.sqlite3"
        if not copy_path.exists():
            shutil.copyfile(args.prod_manifest, copy_path)
        cfg = Config(done_dir=Path("/mnt/pixel/3_Done"),
                     work_dir=Path(args.work_dir),
                     sorted_dir=Path("/mnt/pixel/4_Sorted"),
                     thinking_judgment=thinking,
                     service_tier=args.tier)
        man = Manifest(copy_path)
        registry = gates.load_registry(cfg)
        print(f"arm {arm_name}: registry {len(registry)} albums",
              flush=True)
        from album_sorter.gemini_client import make_client
        from album_sorter.usage import attach_manifest
        client = make_client(Path(args.assets_dir))
        attach_manifest(client, man)

        for r in sample:
            eid = r["episode_id"]
            if eid not in ep_ids:
                continue
            if (arm_name, eid, "gate") in done:
                continue
            rec = {"arm": arm_name, "episode_id": eid,
                   "kind": "gate", "banked_outcome": r["outcome"],
                   "banked_album": r.get("album_name")}
            try:
                outcome = gates.apply_gate(man, cfg, client,
                                           registry, eid)
                ep = man.get_episode(eid)
                rec["outcome"] = outcome
                rec["album_name"] = ep["album_name"]
                raw = sqlite3.connect(
                    f"file:{copy_path}?mode=ro", uri=True)
                rows = raw.execute(
                    "SELECT result_json FROM decisions WHERE "
                    "stage = 'gate' AND subject = ?",
                    (f"episode {eid}",)).fetchall()
                raw.close()
                if rows:
                    stored = json.loads(rows[-1][0])
                    g = stored.get("verdict", stored)
                    rec["registry_match"] = g.get("registry_match")
                    rec["proposed_name"] = g.get("proposed_name")
                    rec["name_verification"] = stored.get(
                        "name_verification")
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"[:300]
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"  {arm_name} ep{eid}: "
                  f"{rec.get('outcome')} "
                  f"(banked {r['outcome']}) "
                  f"match={rec.get('registry_match')}",
                  flush=True)

        # Direct name_verify fixtures at this arm's config
        for eid, claimed in VERIFY_FIXTURES:
            if (arm_name, eid, f"verify:{claimed}") in done:
                continue
            rec = {"arm": arm_name, "episode_id": eid,
                   "kind": f"verify:{claimed}"}
            try:
                ep = man.get_episode(eid)
                items = man.query(
                    "SELECT * FROM items WHERE episode_id = ? "
                    "ORDER BY capture_time, md5", (eid,))
                ok, evidence, _inconclusive = \
                    gates.verify_specific_name(
                        cfg, man, client, ep, items, claimed)
                rec["evidenced"] = bool(ok)
                rec["evidence"] = evidence
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"[:300]
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"  {arm_name} verify '{claimed}' on ep{eid}: "
                  f"evidenced={rec.get('evidenced')}", flush=True)
    out.close()
    score(results_path)
    return 0


def score(results_path: Path) -> None:
    arms: dict[str, list[dict]] = {}
    fixtures: list[dict] = []
    if not results_path.exists():
        return
    for line in results_path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("error"):
            continue
        if r["kind"] == "gate":
            arms.setdefault(r["arm"], []).append(r)
        else:
            fixtures.append(r)
    print("\n=== scoring (plan §2 a4 gates) ===")
    matches: dict[str, set] = {}
    for name, rows in sorted(arms.items()):
        if not rows:
            continue
        agree = sum(1 for r in rows
                    if r["outcome"] == r["banked_outcome"])
        matches[name] = {
            (r["episode_id"], r["registry_match"])
            for r in rows if r.get("registry_match")}
        print(f"{name}: outcome agreement {agree}/{len(rows)} "
              f"= {agree / len(rows):.1%} | registry matches: "
              f"{sorted(matches[name])}")
    if "a4-low" in matches and "control" in matches:
        new = matches["a4-low"] - matches["control"]
        print(f"new registry matches under a4-low that "
              f"control does not produce: {sorted(new)} "
              f"(gate: none without a manual read)")
    for r in fixtures:
        flag = "OK" if not r["evidenced"] else "FIXTURE FAILURE"
        print(f"fixture [{flag}] {r['arm']} {r['kind']} on "
              f"ep{r['episode_id']}: evidenced={r['evidenced']}")
    cha = [r for r in arms.get("a4-low", []) + arms.get("control", [])
           if r["episode_id"] == 94]
    for r in cha:
        bad = r.get("registry_match") == "2026 Cha Birthday" or \
            r.get("album_name") == "2026 Cha Birthday"
        print(f"fixture [{'FAILURE' if bad else 'OK'}] Cha "
              f"ep94 under {r['arm']}: outcome={r['outcome']} "
              f"album={r.get('album_name')} "
              f"match={r.get('registry_match')}")


if __name__ == "__main__":
    raise SystemExit(main())
