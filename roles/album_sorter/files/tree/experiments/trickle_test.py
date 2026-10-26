"""Backlog B8: trickle-regime test (container, real API).

Two sequential batches from cal_corpus through the full
pipeline (no placement). The question: does batch 2 CONTINUE
batch 1's episodes via the ledger, duplicate them (merge
should heal), or lose items? Prints episode tables after
each batch for judgment.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from album_sorter import gates, grouping, ingest as ing, merge, phase1, rollup
from album_sorter.config import Config
from album_sorter.gemini_client import make_client
from album_sorter.manifest import Manifest

BASE = Path(__file__).resolve().parent
BATCH = 45


def table(man: Manifest, label: str) -> None:
    print(f"--- episodes after {label} ---")
    for ep in man.all_episodes():
        if ep["outcome"] == "merged":
            continue
        print(f"  ep{ep['episode_id']}: n={ep['item_count']} "
              f"{ep['span_start'][:16]}..{ep['span_end'][:16]} "
              f"outcome={ep['outcome']} album={ep['album_name']} "
              f"{(ep['description'] or '')[:70]}")
    loose = man.query("SELECT COUNT(*) AS n FROM items WHERE category='PHOTO' "
                      "AND episode_id IS NULL AND status != 'placed'")
    total = man.query("SELECT COUNT(*) AS n FROM items")
    print(f"  items total={total[0]['n']} loose_photos={loose[0]['n']}")


def run_phases(cfg: Config, man: Manifest, client) -> None:
    print("ingest:", ing.ingest(cfg, man), flush=True)
    print("phase1:", phase1.run_phase1(cfg, man, client), flush=True)
    print("grouping:", grouping.run_grouping(cfg, man, client, cfg.work_dir), flush=True)
    print("merge:", merge.run_merge(cfg, man, client), flush=True)
    print("rollup:", rollup.run_rollup(cfg, man, client), flush=True)
    for ep in man.all_episodes():
        if ep["outcome"] == "pending":
            gates.apply_gate(man, cfg, client, [], ep["episode_id"])


def main() -> None:
    corpus = sorted((BASE / "cal_corpus").iterdir())
    corpus = [p for p in corpus if p.is_file()]
    work = BASE / "trickle_work"
    shutil.rmtree(work, ignore_errors=True)
    done = BASE / "trickle_done"
    shutil.rmtree(done, ignore_errors=True)
    done.mkdir(parents=True)
    cfg = Config(done_dir=done, work_dir=work, sorted_dir=BASE / "trickle_sorted",
                 grouping_model="gemini-3.8-flash", ingest_workers=3)
    man = Manifest(cfg.manifest_path)
    client = make_client(cfg.assets_dir)

    for p in corpus[:BATCH]:
        shutil.copy2(p, done / p.name)
    run_phases(cfg, man, client)
    table(man, "batch 1")
    ep_ids_b1 = {ep["episode_id"] for ep in man.all_episodes() if ep["outcome"] != "merged"}

    for p in corpus[BATCH:2 * BATCH]:
        shutil.copy2(p, done / p.name)
    run_phases(cfg, man, client)
    table(man, "batch 2")
    grown = []
    for ep in man.all_episodes():
        if ep["episode_id"] in ep_ids_b1 and ep["outcome"] != "merged":
            grown.append((ep["episode_id"], ep["item_count"]))
    print("batch-1 episodes still live after batch 2 (id, items):", grown)
    print("usage:", client.usage_summary())


if __name__ == "__main__":
    main()
