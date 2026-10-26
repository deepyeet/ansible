#!/usr/bin/env python3
"""B41 Phase D step 2b: field-accuracy audit of the v2
phase-1 fields.

A Flash judge sees each photo (default resolution) next
to the NEW fields of its v2 label and rules per field:
correct / incorrect / uncertain / na, with a one-line
reason required for incorrect. The fields judged are the
ones S1 adds or strengthens - dominant_colors, scene,
distinctive_features, and people_description's clothing
detail (the field the v2 prompt newly demands carry
clothing colors + a distinguishing detail).

Discipline: manifest copies / results files only; the
judged population is the exp_phase1_v2 results (items it
re-described as PHOTO with a label); the subsample is
seeded and deterministic; results append per call and a
rerun answers only what's missing. Usage lands under
stage exp-phase1-judge in the central ledger.

Run on the container from ~/album_sorter_dev:
  ~/album-venv/bin/python exp_phase1_judge.py \
      --phase1-results exp/phase1-v2-results.jsonl \
      --assets-dir /mnt/pixel/.sorter-work-recat/assets \
      --work-dir /mnt/pixel/.sorter-work-recat \
      --results exp/phase1-judge-results.jsonl [--n 100] [--stub]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from album_sorter.config import Config

SEED = 20261023
FIELDS = ("dominant_colors", "scene", "distinctive_features",
          "people_description")

JUDGE_SYSTEM = """You audit the field-level accuracy of an automated photo label. You are given ONE photo and selected fields from its machine-written label. Rule on EACH field separately, judging only what the photo shows.

- dominant_colors: the label claims these are the photo's dominant colors, most coverage first. "correct" = the listed colors are indeed dominant (order roughly by coverage, minor order differences are fine). "incorrect" = a listed color is not prominent in the photo, or a clearly dominant color is missing from the list.
- scene: {"setting", "environment", "lighting"}. "correct" = all three match the photo. "incorrect" = any one is wrong; name which in the reason.
- distinctive_features: claims about what discriminates this photo from a generic scene of its kind. "correct" = every claimed feature is actually visible. "incorrect" = any claimed feature is absent, wrong, or not visible.
- people_description: the label's description of the people, including claimed clothing colors and distinguishing details. "correct" = it matches the people actually visible. If the photo shows no people, return "na".

Use "uncertain" only when the photo genuinely does not let you decide; do not guess. A reason (<= 15 words) is REQUIRED for every "incorrect" verdict and must state what the photo actually shows.

Return JSON only:
{"dominant_colors": {"verdict": "correct"|"incorrect"|"uncertain"|"na", "reason": "..."},
 "scene": {"verdict": ..., "reason": "..."},
 "distinctive_features": {"verdict": ..., "reason": "..."},
 "people_description": {"verdict": ..., "reason": "..."}}"""


def label_fields(label: dict) -> dict:
    out = {}
    for f in ("dominant_colors", "scene", "distinctive_features"):
        if label.get(f) not in (None, "", []):
            out[f] = label[f]
    people = label.get("people")
    if isinstance(people, dict):
        out["people_description"] = {
            "count": people.get("count"),
            "description": people.get("description")}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase1-results", required=True)
    ap.add_argument("--assets-dir", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--tier", default="flex")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--stub", action="store_true")
    args = ap.parse_args()

    labels = {}
    for line in Path(args.phase1_results).read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if not r.get("error") and r.get("label"):
                labels[r["md5"]] = r["label"]
    md5s = sorted(labels)
    rng = random.Random(SEED)
    rng.shuffle(md5s)
    md5s = md5s[:args.n]
    print(f"judging {len(md5s)} items (of {len(labels)} labeled)")

    results_path = Path(args.results)
    done = set()
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if not r.get("error"):
                    done.add(r["md5"])
    todo = [m for m in md5s if m not in done]
    print(f"to judge: {len(todo)}")

    cfg = Config(work_dir=Path(args.work_dir))
    if args.stub:
        from album_sorter.gemini_client import StubClient
        client = StubClient(lambda model, system, parts: {
            f: {"verdict": "correct", "reason": ""}
            for f in FIELDS})
    else:
        from album_sorter.gemini_client import make_client
        client = make_client(Path(args.assets_dir))

    from album_sorter.gemini_client import with_retries
    from album_sorter.usage import usage_context

    def work(md5):
        # Per-item isolation is this script's contract (the
        # F-3 lesson): a semantic failure - an unparseable
        # judge response is the observed case - is ONE
        # item's missing answer, never a dead run. The
        # whole body sits inside the try, and the collector
        # below is guarded too: the first metered run died
        # with a worker exception escaping collection after
        # 4 items, whatever the precise path was.
        rec = {"md5": md5}
        try:
            fields = label_fields(labels[md5])
            rec["fields_present"] = sorted(fields)
            parts = [{"text": json.dumps({"label_fields": fields})},
                     {"image_md5": md5}]
            with usage_context(stage="exp-phase1-judge",
                               subject=md5):
                res = with_retries(lambda: client.generate_json(
                    cfg.phase1_model_flash, JUDGE_SYSTEM, parts,
                    service_tier=args.tier))
            rec["verdicts"] = res.data
            rec["tokens_in"] = res.tokens_in
            rec["tokens_out"] = res.tokens_out
            rec["thoughts"] = res.thoughts
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
        return rec

    fh = results_path.open("a")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(work, m): m for m in todo}
        for fut in as_completed(futs):
            try:
                rec = fut.result()
            except Exception as e:
                rec = {"md5": futs[fut],
                       "error": f"worker: {type(e).__name__}: "
                                f"{e}"[:300]}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
    fh.close()
    summarize(results_path, labels)
    return 0


def summarize(results_path: Path, labels: dict) -> None:
    rows = []
    for line in results_path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if not r.get("error"):
                rows.append(r)
    print(f"\n=== field-accuracy audit: {len(rows)} judged ===")
    for f in FIELDS:
        counts = {"correct": 0, "incorrect": 0, "uncertain": 0,
                  "na": 0, "missing": 0, "malformed": 0}
        for r in rows:
            if f not in (r.get("fields_present") or []):
                counts["missing"] += 1
                continue
            v = (r.get("verdicts") or {}).get(f)
            if not isinstance(v, dict) or \
                    v.get("verdict") not in counts:
                counts["malformed"] += 1
                continue
            counts[v["verdict"]] += 1
        judged = (counts["correct"] + counts["incorrect"]
                  + counts["uncertain"])
        err = (counts["incorrect"] / judged) if judged else 0.0
        print(f"{f}: {counts} | error rate "
              f"{counts['incorrect']}/{judged} = {err:.1%} "
              f"(ship bar: <= ~10%)")
        shown = 0
        for r in rows:
            v = (r.get("verdicts") or {}).get(f)
            if isinstance(v, dict) and \
                    v.get("verdict") == "incorrect" and shown < 5:
                print(f"    e.g. {r['md5'][:8]}: {v.get('reason')}")
                shown += 1


if __name__ == "__main__":
    raise SystemExit(main())
