"""Experiment (e) from the B35 validation plan - $0.

Counterfactual replay of tighter linkage nomination over
banked data: would a 7-day GPS window (from 14) and/or a
candidate cap of 3 (from 5) have lost any of the 64
attachments the pipeline actually made? Deterministic -
no API calls, runs on a COPY of a manifest.

Method (per the plan): for every item that linkage
attached (assign_method='linkage'), reconstruct the
counterfactual state - the item detached, its episode's
span/footprint recomputed without it - then run nominate()
under each variant and check the winning episode is still
nominated. Hard gate: 64/64 survival for a variant to be
adoptable. Also reports how many of the currently
nominated pairs each variant would eliminate (the saving).

Usage: python3 experiment_nomination.py <manifest-copy.sqlite3>
The copy is mutated during the replay and restored item
by item; never point this at a live manifest.
"""
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from album_sorter.config import Config
from album_sorter.linkage import nominate
from album_sorter.manifest import Manifest

VARIANTS = {
    "current (gps 14d, cap 5)": dict(linkage_gps_adjacency_days=14,
                                     linkage_max_candidates=5),
    "gps 7d only": dict(linkage_gps_adjacency_days=7,
                        linkage_max_candidates=5),
    "cap 3 only": dict(linkage_gps_adjacency_days=14,
                       linkage_max_candidates=3),
    "gps 7d + cap 3": dict(linkage_gps_adjacency_days=7,
                           linkage_max_candidates=3),
}


def main(path: str) -> int:
    man = Manifest(Path(path))
    base = Config()
    attached = man.query(
        "SELECT * FROM items WHERE assign_method = 'linkage' "
        "AND episode_id IS NOT NULL ORDER BY capture_time")
    print(f"attached-by-linkage items: {len(attached)}")

    survival = {name: 0 for name in VARIANTS}
    losses = {name: [] for name in VARIANTS}
    for item in attached:
        winner = item["episode_id"]
        # counterfactual: detach the item, recompute the
        # winner's span/footprint without it
        man.update_item(item["md5"], episode_id=None)
        man.recompute_episode_span(winner)
        loose = man.query_one("SELECT * FROM items WHERE md5 = ?",
                              (item["md5"],))
        for name, knobs in VARIANTS.items():
            cfg = replace(base, **knobs)
            if winner in nominate(cfg, man, loose):
                survival[name] += 1
            else:
                losses[name].append((item["md5"], winner))
        man.update_item(item["md5"], episode_id=winner)
        man.recompute_episode_span(winner)

    total = len(attached)
    for name in VARIANTS:
        gate = "PASS" if survival[name] == total else "FAIL"
        print(f"{name}: {survival[name]}/{total} winners survive [{gate}]")
        for md5, winner in losses[name]:
            print(f"    lost: item {md5} -> episode {winner}")

    # the saving: pairs nominated over the currently loose
    # population, per variant
    loose_items = man.query(
        "SELECT * FROM items WHERE episode_id IS NULL "
        "AND status IN ('analyzed', 'embedded') AND category = 'PHOTO'")
    banked = {}
    for r in man.query(
            "SELECT subject FROM decisions WHERE stage = 'linkage'"):
        banked[r["subject"]] = True
    print(f"\nloose items: {len(loose_items)}")
    for name, knobs in VARIANTS.items():
        cfg = replace(base, **knobs)
        pairs = 0
        banked_pairs = 0
        for it in loose_items:
            cands = nominate(cfg, man, it)
            pairs += len(cands)
            banked_pairs += sum(
                1 for eid in cands
                if f"item {it['md5']} -> episode {eid}" in banked)
        print(f"{name}: {pairs} pairs nominated "
              f"({banked_pairs} of them banked verdicts)")

    ok = all(survival[n] == total for n in VARIANTS
             if n != "current (gps 14d, cap 5)")
    print("\nverdict:", "all tightened variants clear the hard gate"
          if ok else "at least one variant LOSES winners - not adoptable")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
