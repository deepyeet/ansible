#!/usr/bin/env python3
"""S10 first stage (IMPROVEMENT-PLAN Phase E): gates
prompt revision for enriched exemplar text, validated on
the production-episode fixtures.

ONE change vs production: GATE_SYSTEM gains a paragraph
instructing the judge to consume the enriched fields
(scene, distinctive_features, clothing detail in
people.description, verbatim visible_text) when present
in the exemplar analyses, and to treat an absent field as
no evidence either way. The name-evidence rules - the
signage / famous_landmark basis requirements and every
other paragraph - are byte-identical to production. The
revision is applied by monkeypatching gates.GATE_SYSTEM
inside THIS experiment process only; production code is
untouched, and the gate decision key (which covers the
system prompt) makes the experiment's questions distinct
from every banked gate verdict.

Method mirrors validate_gates.py (B35): the REAL
gates.apply_gate runs on a fresh copy of the production
manifest in this experiment's own exp-dir, so the
measured outcome is the full production path (verdict,
code-side floors and guards, and the name verification
apply_gate itself invokes). Sample: every gated episode
in ground-truth-production-episodes.json (outcome
album_new or month_bucket; the merged row is not a gate
sample), plus the direct name_verify fixtures.

Hard fixtures:
- ep94 must not registry-match or be named
  '2026 Cha Birthday'.
- verify_specific_name('2026 Metaversary', ep63) and
  ('2026 Rattlesnake Lake', ep64) must NOT be evidenced.

Run on the container from ~/album_sorter_dev:
  ~/album-venv/bin/python exp_gates_s10.py \
      --prod-manifest /mnt/pixel/.sorter-work/manifest.sqlite3 \
      --work-dir /mnt/pixel/.sorter-work \
      --assets-dir /mnt/pixel/.sorter-work/assets \
      --ground-truth exp/ground-truth-production-episodes.json \
      --exp-dir exp-gates-s10 [--tier flex]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from album_sorter import gates
from album_sorter.config import Config
from album_sorter.manifest import Manifest

ENRICHED_EVIDENCE_PARAGRAPH = """
3. EVIDENCE IN THE EXEMPLAR ANALYSES: an exemplar analysis
   may carry structured detail beyond its prose
   description - a "scene" object (setting, environment,
   lighting), "distinctive_features", clothing and
   appearance detail inside the people description, and
   "visible_text" quoted verbatim from the photo. When
   those fields are present, treat them as first-class
   evidence for both jobs above: scene and
   distinctive_features say what the episode actually is,
   and quoted visible_text is text visible in a photo.
   When a field is absent, judge on the prose alone -
   a missing field is never evidence of anything.
"""

VERIFY_FIXTURES = [
    (63, "2026 Metaversary"),
    (64, "2026 Rattlesnake Lake"),
]


def revised_system() -> str:
    """Production GATE_SYSTEM + the one paragraph,
    inserted before the 'Return JSON only' spec so the
    output contract and the naming paragraphs stay
    byte-identical."""
    marker = "\nReturn JSON only:"
    assert marker in gates.GATE_SYSTEM
    return gates.GATE_SYSTEM.replace(
        marker, ENRICHED_EVIDENCE_PARAGRAPH + marker, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod-manifest", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--assets-dir", required=True)
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--exp-dir", required=True)
    ap.add_argument("--tier", default="flex")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    gt_rows = json.loads(Path(args.ground_truth).read_text())
    sample = [r for r in gt_rows
              if r["outcome"] in ("album_new", "month_bucket")]
    print(f"sample: {len(sample)} gated episodes")
    rev = revised_system()
    assert rev != gates.GATE_SYSTEM
    if args.dry_run:
        print("dry run: prompt revision builds OK "
              f"(+{len(rev) - len(gates.GATE_SYSTEM)} chars)")
        return 0
    gates.GATE_SYSTEM = rev          # this process only;
    # the decision key covers the prompt, so no banked
    # gate verdict can replay under the revised question

    exp_dir = Path(args.exp_dir)
    exp_dir.mkdir(parents=True, exist_ok=True)
    results_path = exp_dir / "gates-s10-results.jsonl"
    done = set()
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if not r.get("error"):
                    done.add((r["episode_id"], r["kind"]))
    out = results_path.open("a")

    copy_path = exp_dir / "copy-gates-s10.sqlite3"
    if not copy_path.exists():
        shutil.copyfile(args.prod_manifest, copy_path)
    cfg = Config(done_dir=Path("/mnt/pixel/3_Done"),
                 work_dir=Path(args.work_dir),
                 sorted_dir=Path("/mnt/pixel/4_Sorted"),
                 service_tier=args.tier)
    man = Manifest(copy_path)
    registry = gates.load_registry(cfg)
    print(f"registry: {len(registry)} albums", flush=True)
    from album_sorter.gemini_client import make_client
    from album_sorter.usage import attach_manifest
    client = make_client(Path(args.assets_dir))
    attach_manifest(client, man)

    for r in sample:
        eid = r["episode_id"]
        if (eid, "gate") in done:
            continue
        rec = {"arm": "s10-gates", "episode_id": eid,
               "kind": "gate",
               "banked_outcome": r["outcome"],
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
        print(f"  ep{eid}: {rec.get('outcome')} "
              f"(banked {r['outcome']}) "
              f"match={rec.get('registry_match')} "
              f"name={rec.get('album_name')}", flush=True)

    for eid, claimed in VERIFY_FIXTURES:
        kind = f"verify:{claimed}"
        if (eid, kind) in done:
            continue
        rec = {"arm": "s10-gates", "episode_id": eid,
               "kind": kind}
        try:
            ep = man.get_episode(eid)
            items = man.query(
                "SELECT * FROM items WHERE episode_id = ? "
                "ORDER BY capture_time, md5", (eid,))
            ok, evidence, _inc = gates.verify_specific_name(
                cfg, man, client, ep, items, claimed)
            rec["evidenced"] = bool(ok)
            rec["evidence"] = evidence
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
        out.write(json.dumps(rec) + "\n")
        out.flush()
        print(f"  verify '{claimed}' on ep{eid}: "
              f"evidenced={rec.get('evidenced')}", flush=True)
    out.close()
    score(results_path)
    return 0


def score(results_path: Path) -> None:
    rows, fixtures = [], []
    for line in results_path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("error"):
            continue
        (rows if r["kind"] == "gate" else fixtures).append(r)
    print("\n=== scoring (S10 gates) ===")
    agree = sum(1 for r in rows
                if r["outcome"] == r["banked_outcome"])
    name_agree = sum(1 for r in rows
                     if r["outcome"] == r["banked_outcome"]
                     and (r.get("album_name")
                          == r.get("banked_album")))
    print(f"outcome agreement {agree}/{len(rows)} "
          f"= {agree / max(1, len(rows)):.1%} | outcome+name "
          f"agreement {name_agree}/{len(rows)}")
    matches = sorted({(r["episode_id"], r["registry_match"])
                      for r in rows if r.get("registry_match")})
    print(f"registry matches produced: {matches}")
    for r in rows:
        if r["outcome"] != r["banked_outcome"]:
            print(f"  DISAGREE ep{r['episode_id']}: s10 "
                  f"{r['outcome']} ({r.get('album_name')}) vs "
                  f"banked {r['banked_outcome']} "
                  f"({r.get('banked_album')})")
    for r in fixtures:
        flag = "OK" if not r["evidenced"] else "FIXTURE FAILURE"
        print(f"fixture [{flag}] {r['kind']} on "
              f"ep{r['episode_id']}: evidenced={r['evidenced']}")
    for r in rows:
        if r["episode_id"] == 94:
            bad = (r.get("registry_match")
                   == "2026 Cha Birthday"
                   or r.get("album_name") == "2026 Cha Birthday")
            print(f"fixture [{'FAILURE' if bad else 'OK'}] "
                  f"Cha ep94: outcome={r['outcome']} "
                  f"album={r.get('album_name')} "
                  f"match={r.get('registry_match')}")


if __name__ == "__main__":
    raise SystemExit(main())
