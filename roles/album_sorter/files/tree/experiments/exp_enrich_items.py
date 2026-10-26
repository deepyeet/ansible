#!/usr/bin/env python3
"""Enrich an explicit md5 list with v2 phase-1 labels into
an existing scratch manifest copy (experiment tooling).

Written for the Phase D continuation (2026-10-23): Phase D
enriched the KEY-verified sample items (180), but the
s1-enriched arm verifies by SUBJECT and its 240 questions
span 193 distinct subject items - 14 subjects (15
questions) still carried v1 labels. This tool tops up any
such gap, and is reused for the groupby-on-enriched
population (450 items into an enriched backup copy).

Discipline mirrors exp_phase1_v2.py: the copy is the only
thing written; calls run under the exp-phase1-v2 usage
stage via that module's classify_v2 (same prompt, same
guards, same F5 salvage); results append to a JSONL the
moment they land and a rerun re-asks only missing items;
labels are applied only after the worker pool drains (the
2026-10-23 write-race lesson). dominant_colors is
stripped before apply - the field was dropped from the
schema after the Phase D audit, and enriched labels must
stay uniform.

Run on the container from ~/album_sorter_dev:
  ~/album-venv/bin/python exp_enrich_items.py \
      --copy exp/recat-enriched.sqlite3 \
      --md5-file exp/topup-md5s.txt \
      --assets-dir /mnt/pixel/.sorter-work-recat/assets \
      --results exp/phase1-v2-topup-results.jsonl [--tier flex]
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import exp_phase1_v2 as ev2
from album_sorter.config import Config
from album_sorter.manifest import Manifest

LIVE = {"/mnt/pixel/.sorter-work-recat/manifest.sqlite3",
        "/mnt/pixel/.sorter-work/manifest.sqlite3"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--copy", required=True)
    ap.add_argument("--md5-file", required=True)
    ap.add_argument("--assets-dir", required=True)
    ap.add_argument("--work-dir",
                    default="/mnt/pixel/.sorter-work-recat")
    ap.add_argument("--results", required=True)
    ap.add_argument("--tier", default="flex")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    copy_path = Path(args.copy).resolve()
    if str(copy_path) in LIVE:
        print("refusing: experiments never touch a live "
              "manifest", file=sys.stderr)
        return 2
    md5s = [m.strip() for m in
            Path(args.md5_file).read_text().splitlines()
            if m.strip()]
    print(f"items to enrich: {len(md5s)}")

    results_path = Path(args.results)
    done = set()
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if not r.get("error") and r.get("label"):
                    done.add(r["md5"])
    todo = [m for m in md5s if m not in done]
    print(f"already done: {len(done)}; to ask: {len(todo)}")

    man = Manifest(copy_path)
    cfg = Config(work_dir=Path(args.work_dir))
    rows = {r["md5"]: r for r in man.query(
        "SELECT * FROM items WHERE md5 IN (%s)"
        % ",".join("?" * len(md5s)), md5s)} if md5s else {}
    missing = [m for m in todo if m not in rows]
    if missing:
        print(f"WARNING: {len(missing)} md5s not in copy: "
              f"{missing[:5]}")
        todo = [m for m in todo if m in rows]

    from album_sorter.gemini_client import make_client
    from album_sorter.usage import attach_manifest
    client = make_client(Path(args.assets_dir))
    attach_manifest(client, man)

    def work(md5):
        try:
            return ev2.classify_v2(cfg, client, rows[md5],
                                   args.tier)
        except Exception as e:
            return {"md5": md5,
                    "error": f"{type(e).__name__}: {e}"[:300]}

    collected = []
    fh = results_path.open("a")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(work, m) for m in todo]
        for fut in as_completed(futs):
            rec = fut.result()
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            collected.append(rec)
    fh.close()

    applied = 0
    for rec in collected:
        if rec.get("error") or not rec.get("label"):
            continue
        rec = dict(rec)
        label = dict(rec["label"])
        label.pop("dominant_colors", None)   # dropped field:
        rec["label"] = label                # keep labels uniform
        ev2.apply_label(man, rec)
        applied += 1
    errs = sum(1 for r in collected if r.get("error"))
    print(f"asked {len(collected)}: applied {applied}, "
          f"errors {errs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
