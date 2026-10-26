"""Finish probe - $0. Measures, at decision-key level,
exactly which calls a finish run on this manifest would
still have to BUY versus replay from the journal, under
the CURRENT default config. Static upper bound: it does
not simulate attaches (a "yes" mid-run changes later
nominations), so the fresh counts are the most the run
could ask, never more.

Usage: python3 finish_probe.py <manifest-copy.sqlite3>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from album_sorter.config import Config
from album_sorter.journal import call_extra, decision_key, image_extra
from album_sorter.linkage import VERIFIER_SYSTEM, _drift_ok, nominate
from album_sorter.manifest import Manifest
from album_sorter.merge import (
    MERGE_SYSTEM, _episode_brief, brief_identity, candidate_pairs,
    merge_ctx_ok)
from album_sorter.rollup import (
    ROLLUP_SYSTEM, _rollup_ctx_ok, candidate_chains)

# planning prices from the validation plan (visible tokens
# + thinking allowance), per call; the gate price was
# corrected 2026-10-23 from measured payload reconstruction
# (~1,430 tok in/call, not the ~9,000 originally assumed)
PRICE = {"linkage": 0.0060, "merge": 0.0022, "rollup": 0.0063,
         "gate": 0.0045, "name_verify": 0.0050}


def main(path: str) -> int:
    cfg = Config()
    man = Manifest(Path(path))
    extra = {**image_extra(cfg), **call_extra(cfg, "judgment")}
    out = {}

    # ---- linkage: mirror the runner's loose set and
    # try_link's exact key construction
    loose = man.query(
        "SELECT * FROM items WHERE category = 'PHOTO' "
        "AND episode_id IS NULL AND status = 'analyzed' "
        "ORDER BY capture_time, done_name")
    fresh = replayed = pairs = 0
    for item in loose:
        for eid in nominate(cfg, man, item):
            pairs += 1
            ep = man.get_episode(eid)
            if ep is None or ep["outcome"] != "pending":
                replayed += 1     # try_link skips it entirely
                continue
            exemplars = json.loads(ep["exemplars"] or "[]")[:4]
            prompt_text = json.dumps({
                "episode_description": ep["description"],
                "episode_span": [ep["span_start"], ep["span_end"]],
                "item_time": item["capture_time"],
                "item_analysis": item["analysis_json"]})
            key_text = json.dumps({
                "episode_description": ep["description"],
                "item_time": item["capture_time"],
                "item_analysis": item["analysis_json"]})
            row = man.get_decision(decision_key(
                "linkage", cfg.phase1_model_flash, VERIFIER_SYSTEM,
                {"text": key_text, "item_md5": item["md5"],
                 "exemplars": exemplars}, extra=extra))
            hit = row is not None and _drift_ok(row, cfg, item, ep)
            if not hit and row is None:
                hit = man.get_decision(decision_key(
                    "linkage", cfg.phase1_model_flash, VERIFIER_SYSTEM,
                    {"text": prompt_text, "item_md5": item["md5"],
                     "exemplars": exemplars}, extra=extra)) is not None
            if hit:
                replayed += 1
            else:
                fresh += 1
    out["linkage"] = {"loose_items": len(loose), "pairs": pairs,
                      "replayed": replayed, "fresh_upper_bound": fresh}

    # ---- merge
    mf = mr = 0
    for elder_id, younger_id in candidate_pairs(man):
        ba, bb = _episode_brief(man, elder_id), _episode_brief(man, younger_id)
        row = man.get_decision(decision_key(
            "merge", cfg.phase1_model_flash, MERGE_SYSTEM,
            {"episode_a": brief_identity(ba),
             "episode_b": brief_identity(bb)},
            extra=call_extra(cfg, "judgment")))
        hit = row is not None and merge_ctx_ok(row, ba, bb)
        if not hit and row is None:
            hit = man.get_decision(decision_key(
                "merge", cfg.phase1_model_flash, MERGE_SYSTEM,
                {"episode_a": ba, "episode_b": bb},
                extra=call_extra(cfg, "judgment"))) is not None
        if hit:
            mr += 1
        else:
            mf += 1
    out["merge"] = {"pairs": mf + mr, "replayed": mr, "fresh": mf}

    # ---- rollup
    rf = rr = 0
    for chain in candidate_chains(man, cfg.rollup_gap_hours,
                                   cfg.rollup_max_span_days):
        briefs = [_episode_brief(man, eid) for eid in chain]
        row = man.get_decision(decision_key(
            "rollup", cfg.phase1_model_flash, ROLLUP_SYSTEM,
            {"episodes": [brief_identity(b) for b in briefs]},
            extra=call_extra(cfg, "judgment")))
        hit = row is not None and _rollup_ctx_ok(row, briefs)
        if not hit and row is None:
            hit = man.get_decision(decision_key(
                "rollup", cfg.phase1_model_flash, ROLLUP_SYSTEM,
                {"episodes": briefs},
                extra=call_extra(cfg, "judgment"))) is not None
        if hit:
            rr += 1
        else:
            rf += 1
    out["rollup"] = {"chains": rf + rr, "replayed": rr, "fresh": rf}

    # ---- gates: no banked verdicts exist, so every
    # pending episode is one fresh gate call; name_verify
    # fires only for episodes the gate gives a specific
    # name - unknowable pre-run, bounded by the same count
    pending = man.query(
        "SELECT COUNT(*) AS n FROM episodes WHERE outcome = 'pending'")
    gate_rows = man.query(
        "SELECT COUNT(*) AS n FROM decisions WHERE stage = 'gate'")
    out["gates"] = {"pending_episodes": pending[0]["n"],
                    "banked_gate_verdicts": gate_rows[0]["n"],
                    "fresh": pending[0]["n"]}

    total = (out["linkage"]["fresh_upper_bound"] * PRICE["linkage"]
             + mf * PRICE["merge"] + rf * PRICE["rollup"]
             + out["gates"]["fresh"] * PRICE["gate"])
    out["planning_usd"] = {
        "finish_at_standard_rates": round(total, 2),
        "name_verify_extra_bound": round(
            out["gates"]["fresh"] * PRICE["name_verify"], 2),
        "finish_on_flex": round(total / 2, 2)}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
