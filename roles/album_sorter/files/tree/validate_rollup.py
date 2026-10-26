#!/usr/bin/env python3
"""B35 validation harness: rollup thinking arms (plan §2 a3).

Base manifest: the pre-finish backup (manifest.bak-
*pre-b35-fingerprint) - the last state where all seven
banked chains and the one real trip chain still exist
unfused with their judged briefs reconstructible.

Questions:
- The 7 banked chains from ground-truth-recat.json (all
  is_trip=False). Rebuilt from the backup's briefs and
  verified against its journal (key, then subject tier).
- The big chain [84, 85, 22, ..., 60]: attempt 3 judged
  its pre-growth briefs False @ 0.95; the finish judged
  the post-growth version True @ 0.95 and it became the
  639-item '2026 Family Vacation' album. Reported
  informationally for both arms - it is evidence about
  run-to-run instability, not a clean positive.

Arms: control (Flash, default thinking), a3-low (Flash,
thinking=low). Applied verdict: is_trip at confidence
>= 0.7 (cfg.rollup_confidence).

Decision rule (plan §2 a3): rollup adopts low ONLY if
(a1)+(a2) pass AND the 7/7 negatives hold AND no
synthetic positive is clearly missed. This harness
measures the 7/7 leg and the big-chain read; the
adoption call itself is made in the validation report.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from album_sorter.config import Config
from album_sorter.journal import call_extra, decision_key
from album_sorter.manifest import Manifest
from album_sorter.merge import _episode_brief, brief_identity
from album_sorter.rollup import ROLLUP_SYSTEM

ARMS = {
    "control": {"thinking": None},
    "a3-low": {"thinking": "low"},
}

BIG_CHAIN = [84, 85, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
             32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43,
             44, 45, 46, 47, 48, 49, 50, 51, 52, 54, 55, 56,
             57, 58, 59, 60]


def applied(result: dict, cfg: Config) -> bool:
    return bool(result.get("is_trip")) and \
        float(result.get("confidence") or 0) >= cfg.rollup_confidence


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest-copy", required=True,
                    help="copy of the pre-b35-fingerprint backup")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--arms", default="control,a3-low")
    ap.add_argument("--tier", default="standard")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stub", action="store_true")
    args = ap.parse_args()

    copy_path = Path(args.manifest_copy).resolve()
    gt = json.loads(Path(args.ground_truth).read_text())
    cfg = Config(work_dir=Path(args.work_dir))
    man = Manifest(copy_path)
    raw = sqlite3.connect(f"file:{copy_path}?mode=ro", uri=True)

    questions = []
    for row in gt["rollup"]:
        chain = json.loads(row["subject"].removeprefix("chain "))
        # The big chain IS one of the seven banked negatives
        # (attempt 3 judged its pre-growth briefs False);
        # the finish later judged the grown version True.
        # It gets both reads in the report - not a second
        # ask of the identical question.
        kind = ("banked-negative+big-chain"
                if chain == BIG_CHAIN else "banked-negative")
        questions.append({"chain": chain, "banked": row["result"],
                          "kind": kind})

    extra = call_extra(cfg, "judgment")
    verified = []
    for q in questions:
        briefs = [_episode_brief(man, eid) for eid in q["chain"]]
        payload = {"episodes": briefs}
        q["parts"] = [{"text": json.dumps(payload)}]
        q["key2"] = decision_key(
            "rollup", cfg.phase1_model_flash, ROLLUP_SYSTEM,
            {"episodes": [brief_identity(b) for b in briefs]},
            extra=extra)
        tier = None
        key1 = decision_key("rollup", cfg.phase1_model_flash,
                            ROLLUP_SYSTEM, payload, extra=extra)
        for key in (q["key2"], key1):
            row = man.get_decision(key)
            if row is not None:
                v = json.loads(row["result_json"])
                if applied(v, cfg) == applied(q["banked"], cfg):
                    tier = "key"
                break
        if tier is None:
            rows = raw.execute(
                "SELECT result_json FROM decisions WHERE "
                "stage = 'rollup' AND subject = ?",
                (f"chain {q['chain']}",)).fetchall()
            for (rj,) in rows:
                if applied(json.loads(rj), cfg) == \
                        applied(q["banked"], cfg):
                    tier = "subject"
                    break
        q["tier"] = tier
        print(f"chain {q['chain'][:3]}... n={len(q['chain'])} "
              f"tier={tier} banked_applied="
              f"{applied(q['banked'], cfg)}")
        if tier:
            verified.append(q)
    print(f"verified questions: {len(verified)}/{len(questions)}")
    if args.dry_run:
        return 0

    results_path = Path(args.results)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if not r.get("error"):
                    done.add((r["arm"], tuple(r["chain"])))

    client = None
    if not args.stub:
        from album_sorter.gemini_client import make_client
        from album_sorter.usage import attach_manifest
        client = make_client(Path(args.work_dir) / "assets")
        attach_manifest(client, man)
    from album_sorter.gemini_client import with_retries
    from album_sorter.usage import usage_context

    out = results_path.open("a")
    for arm_name in [a.strip() for a in args.arms.split(",")]:
        arm = ARMS[arm_name]
        for q in verified:
            if (arm_name, tuple(q["chain"])) in done:
                continue
            rec = {"arm": arm_name, "chain": q["chain"],
                   "kind": q["kind"], "tier": q["tier"],
                   "banked_applied": (applied(q["banked"], cfg)
                                      if q["banked"] else None)}
            try:
                if args.stub:
                    rec["is_trip"] = bool(
                        q["banked"] and q["banked"].get("is_trip"))
                    rec["confidence"] = float(
                        (q["banked"] or {}).get("confidence") or 0)
                    rec["tokens_in"] = rec["tokens_out"] = 0
                    rec["thoughts"] = 0
                else:
                    with usage_context(
                            stage=f"exp-{arm_name}",
                            subject=f"chain {q['chain']}",
                            decision_key=q["key2"]):
                        fresh = with_retries(lambda: client.generate_json(
                            cfg.phase1_model_flash, ROLLUP_SYSTEM,
                            q["parts"],
                            thinking_level=arm["thinking"],
                            service_tier=args.tier))
                    rec["is_trip"] = bool(fresh.data.get("is_trip"))
                    rec["confidence"] = float(
                        fresh.data.get("confidence") or 0)
                    rec["tokens_in"] = fresh.tokens_in
                    rec["tokens_out"] = fresh.tokens_out
                    rec["thoughts"] = fresh.thoughts
                rec["applied"] = applied(rec, cfg)
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"[:300]
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"{arm_name} chain n={len(q['chain'])} "
                  f"({q['kind']}): applied={rec.get('applied')} "
                  f"conf={rec.get('confidence')}", flush=True)
    out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
