#!/usr/bin/env python3
"""$0 probe for the S3-on-enriched arm (Phase D continuation).

Answers two questions from the enriched manifest copy alone:
1. What does a text-mode linkage question actually carry?
   (Rebuild one sample question in both modes through the
   shared B39 builder and print the wire payload structure.)
2. What is the exemplar universe of the 240 questions -
   distinct exemplar md5s, distinct episodes, and how many
   exemplar items already carry v2 labels (as subject items)?

Read-only: opens the copy with mode=ro and never writes.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate_linkage as vl
from album_sorter.config import Config
from album_sorter.manifest import Manifest

COPY = Path("exp/recat-enriched.sqlite3")
GT = Path("exp/ground-truth-recat.json")
WORK = Path("/mnt/pixel/.sorter-work-recat")


def main() -> int:
    gt = json.loads(GT.read_text())
    sample = vl.build_sample(gt["linkage"])
    cfg = Config(work_dir=WORK)
    man = Manifest(COPY)
    raw = sqlite3.connect(f"file:{COPY}?mode=ro", uri=True)
    raw.row_factory = sqlite3.Row

    # --- exemplar universe ---------------------------------
    ex_md5s: set[str] = set()
    ep_ids: set[int] = set()
    subj_md5s: set[str] = set()
    subj_v2 = 0
    for q in sample:
        subj_md5s.add(q["md5"])
        ep = man.get_episode(q["episode_id"])
        ep_ids.add(q["episode_id"])
        ex_md5s.update(json.loads(ep["exemplars"] or "[]")[:4])

    def has_v2(md5: str) -> bool:
        row = raw.execute(
            "SELECT analysis_json FROM items WHERE md5 = ?",
            (md5,)).fetchone()
        if row is None or not row["analysis_json"]:
            return False
        try:
            return json.loads(row["analysis_json"]).get("v") == 2
        except ValueError:
            return False

    subj_v2 = sum(1 for m in subj_md5s if has_v2(m))
    ex_v2 = sum(1 for m in ex_md5s if has_v2(m))
    ex_in_copy = sum(
        1 for m in ex_md5s
        if raw.execute("SELECT 1 FROM items WHERE md5 = ?",
                       (m,)).fetchone())
    print(f"questions: {len(sample)}; distinct subject items: "
          f"{len(subj_md5s)} (v2-labelled: {subj_v2})")
    print(f"distinct episodes: {len(ep_ids)}; distinct exemplar "
          f"items (identity 4/episode): {len(ex_md5s)} "
          f"(present in copy: {ex_in_copy}, v2-labelled: {ex_v2}, "
          f"overlap with subject items: "
          f"{len(ex_md5s & subj_md5s)})")

    # --- one question, both modes --------------------------
    q = sample[0]
    for mode in ("images", "text"):
        built = vl.rebuild_question(cfg, man, raw, q, 4, mode=mode)
        parts = built[0]
        kinds = [("text" if "text" in p else "image")
                 for p in parts]
        text_part = next(p["text"] for p in parts if "text" in p
                         and p["text"].startswith("{"))
        payload = json.loads(text_part)
        print(f"mode={mode}: parts={kinds}")
        print(f"  prompt keys: {sorted(payload)}")
        print(f"  item_analysis keys: "
              f"{sorted(json.loads(payload['item_analysis']))}")
        print(f"  prompt chars: {len(text_part)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
