#!/usr/bin/env python3
"""B35 validation harness: merge arms (plan §2 a2, §5 d)
against banked ground truth.

Same rules as validate_linkage.py: manifest copy only,
payloads rebuilt exactly as merge.run_merge builds them
(_episode_brief from the copy), direct generate_json
(never get_decision), per-answer JSONL with resume,
two-tier verification ("key" = recomputed v2/v1 key in
the journal with matching result; "subject" = journal
row for the exact pair with matching result, for pairs
whose briefs have since moved).

Arms: control (Flash, default thinking), a2-low (Flash,
thinking=low), d-lite-merge (Flash-Lite). All 62 banked
pairs per arm; merge calls are text-only.

Gates (plan §2 a2): agreement >= 95% on the APPLIED
verdict (same=True at confidence >= 0.7); the single
banked True (episodes 52+53 @ 0.88) must remain applied
True - hard gate; zero flips among the 19 conf-1.0
Falses; borderline False flips counted vs control.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from album_sorter.config import Config
from album_sorter.journal import call_extra, decision_key
from album_sorter.manifest import Manifest
from album_sorter.merge import (MERGE_CONFIDENCE, MERGE_SYSTEM,
                                _episode_brief, brief_identity)

ARMS = {
    "control": {"model": "flash", "thinking": None},
    "a2-low": {"model": "flash", "thinking": "low"},
    "d-lite-merge": {"model": "lite", "thinking": None},
}


def applied(result: dict) -> bool:
    return bool(result.get("same")) and \
        float(result.get("confidence") or 0) >= MERGE_CONFIDENCE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest-copy", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--arms", default="control,a2-low,d-lite-merge")
    ap.add_argument("--tier", default="flex")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stub", action="store_true")
    args = ap.parse_args()

    copy_path = Path(args.manifest_copy).resolve()
    if copy_path == Path("/mnt/pixel/.sorter-work-recat/"
                         "manifest.sqlite3"):
        print("refusing: experiments never touch the live "
              "manifest", file=sys.stderr)
        return 2

    gt = json.loads(Path(args.ground_truth).read_text())
    cfg = Config(work_dir=Path(args.work_dir))
    man = Manifest(copy_path)

    sample = []
    for row in gt["merge"]:
        a, b = row["subject"].removeprefix("episodes ").split("+")
        q = {"elder": int(a), "younger": int(b),
             "banked": row["result"],
             "banked_applied": applied(row["result"])}
        sample.append(q)
    print(f"sample: {len(sample)} pairs; banked applied "
          f"True: {sum(q['banked_applied'] for q in sample)}")

    verified, excluded = [], []
    tiers: dict[str, int] = {}
    extra = call_extra(cfg, "judgment")
    for q in sample:
        try:
            brief_a = _episode_brief(man, q["elder"])
            brief_b = _episode_brief(man, q["younger"])
        except Exception:
            excluded.append((q, "brief-failed"))
            continue
        payload = {"episode_a": brief_a, "episode_b": brief_b}
        key2 = decision_key(
            "merge", cfg.phase1_model_flash, MERGE_SYSTEM,
            {"episode_a": brief_identity(brief_a),
             "episode_b": brief_identity(brief_b)}, extra=extra)
        key1 = decision_key("merge", cfg.phase1_model_flash,
                            MERGE_SYSTEM, payload, extra=extra)
        q["key2"] = key2
        q["parts"] = [{"text": json.dumps(payload)}]
        tier = None
        for key in (key2, key1):
            row = man.get_decision(key)
            if row is not None:
                v = json.loads(row["result_json"])
                if applied(v) == q["banked_applied"] and \
                        bool(v.get("same")) == bool(q["banked"].get("same")):
                    tier = "key"
                break
        if tier is None:
            import sqlite3
            raw = sqlite3.connect(f"file:{copy_path}?mode=ro",
                                  uri=True)
            rows = raw.execute(
                "SELECT result_json FROM decisions WHERE "
                "stage = 'merge' AND subject = ?",
                (f"episodes {q['elder']}+{q['younger']}",),
            ).fetchall()
            raw.close()
            for (rj,) in rows:
                v = json.loads(rj)
                if applied(v) == q["banked_applied"] and \
                        bool(v.get("same")) == bool(q["banked"].get("same")):
                    tier = "subject"
                    break
        if tier:
            q["tier"] = tier
            tiers[tier] = tiers.get(tier, 0) + 1
            verified.append(q)
        else:
            excluded.append((q, "no-journal-row"))
    print(f"verified: {len(verified)}/{len(sample)} "
          f"(tiers {tiers}); excluded: {len(excluded)}")
    for q, why in excluded[:8]:
        print(f"  excluded episodes {q['elder']}+{q['younger']}: {why}")
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
                    done.add((r["arm"], r["elder"], r["younger"]))
    print(f"already answered: {len(done)}")

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
        model = (cfg.phase1_model_flash if arm["model"] == "flash"
                 else cfg.phase1_model_lite)
        todo = [q for q in verified
                if (arm_name, q["elder"], q["younger"]) not in done]
        print(f"arm {arm_name}: {len(verified)} questions, "
              f"{len(todo)} to ask", flush=True)
        for q in todo:
            rec = {"arm": arm_name, "elder": q["elder"],
                   "younger": q["younger"], "tier": q["tier"],
                   "banked_applied": q["banked_applied"],
                   "banked_same": bool(q["banked"].get("same")),
                   "banked_conf": float(q["banked"].get("confidence") or 0)}
            try:
                if args.stub:
                    rec["same"] = rec["banked_same"]
                    rec["confidence"] = rec["banked_conf"]
                    rec["tokens_in"] = rec["tokens_out"] = 0
                    rec["thoughts"] = 0
                else:
                    with usage_context(
                            stage=f"exp-{arm_name}",
                            subject=f"episodes {q['elder']}+{q['younger']}",
                            decision_key=q["key2"]):
                        fresh = with_retries(lambda: client.generate_json(
                            model, MERGE_SYSTEM, q["parts"],
                            thinking_level=arm["thinking"],
                            service_tier=args.tier))
                    rec["same"] = bool(fresh.data.get("same"))
                    rec["confidence"] = float(
                        fresh.data.get("confidence") or 0)
                    rec["tokens_in"] = fresh.tokens_in
                    rec["tokens_out"] = fresh.tokens_out
                    rec["thoughts"] = fresh.thoughts
                rec["applied"] = applied(rec)
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"[:300]
            out.write(json.dumps(rec) + "\n")
            out.flush()
    out.close()
    score(results_path)
    return 0


def score(results_path: Path) -> None:
    arms: dict[str, list[dict]] = {}
    if not results_path.exists():
        return
    for line in results_path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if not r.get("error"):
                arms.setdefault(r["arm"], []).append(r)
    print("\n=== scoring (plan §2 a2 gates) ===")
    control = arms.get("control", [])
    control_bl_flips = sum(
        1 for r in control
        if not r["banked_applied"] and r["banked_conf"] < 0.9
        and r["applied"])
    for name, rows in sorted(arms.items()):
        if not rows:
            continue
        agree = sum(1 for r in rows
                    if r["applied"] == r["banked_applied"])
        the_true = [r for r in rows if r["banked_applied"]]
        true_kept = sum(1 for r in the_true if r["applied"])
        conf_false = [r for r in rows
                      if not r["banked_same"]
                      and r["banked_conf"] >= 1.0]
        conf_flips = sum(1 for r in conf_false if r["applied"])
        bl_false = [r for r in rows
                    if not r["banked_applied"]
                    and r["banked_conf"] < 0.9]
        bl_flips = sum(1 for r in bl_false if r["applied"])
        print(f"{name}: n={len(rows)} agreement "
              f"{agree}/{len(rows)} = {agree / len(rows):.1%} "
              f"(gate >= 95%) | banked True kept "
              f"{true_kept}/{len(the_true)} (hard gate) | "
              f"conf-1.0 False flips {conf_flips}/{len(conf_false)} "
              f"(hard gate 0) | borderline False flips "
              f"{bl_flips}/{len(bl_false)} (control: "
              f"{control_bl_flips})")


if __name__ == "__main__":
    raise SystemExit(main())
