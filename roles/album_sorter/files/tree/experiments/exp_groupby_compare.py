#!/usr/bin/env python3
"""Compare grouping partitions (experiment tooling).

Loads the cached reference plus any arm result JSONs and
reports, per arm: Jaccard vs reference co-membership,
unassigned count, episode count, big-episode splits,
fusion episodes - and pairwise partition-vs-partition
Jaccard + identical-bucket rate between arms. Used for
the groupby-on-enriched run (Phase D continuation):
does richer phase-1 text change the partition relative
to the reference and to the S6a (v1 text, low-res sheets)
partition?

Usage (container):
  ~/album-venv/bin/python exp_groupby_compare.py \
      exp/groupby-control-result.json \
      exp/groupby-s6a-result.json \
      exp-gd/groupby-control-result.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate_grouping as G


def partition_stats(ref: dict, assignment: dict) -> dict:
    ref_pairs = G.pair_set(ref["reference"])
    pairs = G.pair_set(assignment)
    jac = (len(pairs & ref_pairs) / len(pairs | ref_pairs)
           if pairs | ref_pairs else 1.0)
    unassigned = sum(1 for v in assignment.values() if v is None)
    eps = {v for v in assignment.values() if v is not None}
    ref_members: dict = {}
    for md5, ep in ref["reference"].items():
        if ep is not None:
            ref_members.setdefault(ep, []).append(md5)
    splits = [ep for ep, members in ref_members.items()
              if len(members) >= 20
              and len({assignment[m] for m in members}) > 1]
    arm_members: dict = {}
    for md5, ep in assignment.items():
        if ep is not None:
            arm_members.setdefault(ep, []).append(md5)
    fusion = [ep for ep, members in arm_members.items()
              if len({ref["reference"][m] for m in members
                      if ref["reference"][m] is not None}) >= 2]
    return {"jaccard_vs_ref": jac, "unassigned": unassigned,
            "episodes": len(eps), "splits": splits,
            "fusion": fusion, "pairs": pairs}


def main() -> int:
    ref = json.loads(Path("exp/groupby-reference.json")
                     .read_text())
    ref_eps = {ep for ep in ref["reference"].values()
               if ep is not None}
    print(f"reference: {len(ref['subsample'])} items, "
          f"{len(ref_eps)} episodes, "
          f"{len(G.pair_set(ref['reference']))} pairs")
    arms: dict[str, dict] = {}
    for p in sys.argv[1:]:
        res = json.loads(Path(p).read_text())
        label = f"{Path(p).parent.name}/{res['arm']}"
        arms[label] = res["assignment"]
        s = partition_stats(ref, res["assignment"])
        led = res.get("ledger") or {}
        print(f"{label}: Jaccard vs ref "
              f"{s['jaccard_vs_ref']:.4f} | unassigned "
              f"{s['unassigned']} | episodes {s['episodes']} "
              f"| splits {s['splits']} | fusion "
              f"{len(s['fusion'])} {s['fusion']} | ledger "
              f"{led.get('calls')} calls ${led.get('est_usd')}")
    labels = list(arms)
    for i, a in enumerate(labels):
        for b in labels[i + 1:]:
            pa = G.pair_set(arms[a])
            pb = G.pair_set(arms[b])
            jac = (len(pa & pb) / len(pa | pb)
                   if pa | pb else 1.0)
            same = sum(1 for m in ref["subsample"]
                       if arms[a][m] == arms[b][m])
            # episode ids are per-manifest; identical-id rate
            # is meaningless across copies - bucket-structure
            # agreement is the Jaccard above plus a
            # co-membership delta count:
            delta = len(pa ^ pb)
            print(f"pairwise {a} vs {b}: co-membership "
                  f"Jaccard {jac:.4f} | pair delta {delta} "
                  f"(only-a {len(pa - pb)}, only-b "
                  f"{len(pb - pa)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
