#!/usr/bin/env python3
"""B35 validation harness: linkage verifier arms (plan §2 a1,
§4 c1, §5 d) against banked ground truth.

Design rules (validation-plan.md §1):
- Manifest COPY only. Payloads are rebuilt from the copy
  exactly as linkage.try_link builds them; a banked verdict
  enters the sample only if its recomputed decision key
  (v2, or v1 fallback) exists in the copy's journal and the
  journaled result matches the ground-truth file.
- The harness calls generate_json DIRECTLY (never
  get_decision), so a treatment arm can never replay-suppress
  itself against the journal it is being measured against.
- Every answer is appended to a JSONL results file the
  moment it lands; a rerun answers only questions still
  missing or errored. A transport death costs time, never
  banked answers.
- All arms run on one service tier (default flex): tier is
  transport, not part of the question, so the comparison
  between arms is unaffected and every arm costs half.

Arms:
  control  Flash, default thinking, 4 exemplars (subset 100)
  a1-low   Flash, thinking=low, 4 exemplars (all 240)
  c1-ex2   Flash, default thinking, 2 exemplars (all 240)
  d-lite   Flash-Lite, default thinking, 4 exemplars (240)

Run on the container from ~/album_sorter_dev:
  ~/album-venv/bin/python validate_linkage.py \
      --manifest-copy exp/recat-copy.sqlite3 \
      --work-dir /mnt/pixel/.sorter-work-recat \
      --assets-dir /mnt/pixel/.sorter-work-recat/assets \
      --ground-truth <cost-overhaul>/ground-truth-recat.json \
      --results exp/linkage-results.jsonl [--dry-run|--stub]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from album_sorter.config import Config
from album_sorter.exemplars import stored_exemplars
from album_sorter.journal import decision_key
from album_sorter.linkage import VERIFIER_SYSTEM, build_verifier_question
from album_sorter.manifest import Manifest

SEED = 20261023

# Arm fields: model ("flash"|"lite"), thinking (generate_json
# thinking_level; an arm may omit it and set thinking_judgment
# via cfg_overrides instead), exemplars (images actually
# sent; the decision key always keeps the identity 4),
# subset ("control" | "all"), cfg_overrides (applied to the
# arm's Config via dataclasses.replace - media_res knobs and
# future call config), payload_mode (the builder's mode:
# "images" | "text" | "sheet").
ARMS = {
    "control": {"model": "flash", "thinking": None, "exemplars": 4,
                "subset": "control", "cfg_overrides": {},
                "payload_mode": "images"},
    "a1-low": {"model": "flash", "thinking": "low", "exemplars": 4,
               "subset": "all", "cfg_overrides": {},
               "payload_mode": "images"},
    "c1-ex2": {"model": "flash", "thinking": None, "exemplars": 2,
               "subset": "all", "cfg_overrides": {},
               "payload_mode": "images"},
    "d-lite": {"model": "lite", "thinking": None, "exemplars": 4,
               "subset": "all", "cfg_overrides": {},
               "payload_mode": "images"},
    # B38 arm (iii), IMPROVEMENT-PLAN Phase B / S2: item +
    # exemplar images at media_resolution low (280 tok/part
    # vs the 1,120 default). Knob-only: thinking stays the
    # cfg default (None = provider medium) so resolution is
    # the single variable against control.
    "s2-lowres": {"model": "flash", "thinking": None,
                  "exemplars": 4, "subset": "all",
                  "cfg_overrides": {"media_res_item": "low",
                                    "media_res_exemplar": "low"},
                  "payload_mode": "images"},
    # B41 Phase D / S1: enriched phase-1 text (v2 schema)
    # with the payload otherwise IDENTICAL to control -
    # default-res images, default thinking. The arm runs
    # against the enriched manifest copy (exp_phase1_v2.py)
    # and isolates the text effect: the item analyses in
    # the rebuilt questions come from the copy, so the copy
    # is the variable. Sample membership is verified by
    # SUBJECT (--verify-mode subject): enriched questions
    # recompute to decision keys that exist nowhere, by
    # design - the journal's subject rows are the pair
    # identity that survives the text change.
    "s1-enriched": {"model": "flash", "thinking": None,
                    "exemplars": 4, "subset": "all",
                    "cfg_overrides": {},
                    "payload_mode": "images"},
    # B38 arm (i) on enriched text, IMPROVEMENT-PLAN
    # Phase D continuation / S3-on-enriched: the SAME
    # questions as s1-enriched with payload_mode "text" -
    # the builder sends the prompt text only (episode
    # description + span + item time + the item's enriched
    # v2 analysis); no images at all. Exemplars exist in
    # this payload family only as images (images mode),
    # so the arm isolates the entire image channel
    # against s1-enriched. Subject verification, same as
    # the s1 arm. "exemplars" is inert in text mode (the
    # parts carry no images); kept for symmetry.
    "s3-enriched-text": {"model": "flash", "thinking": None,
                         "exemplars": 4, "subset": "all",
                         "cfg_overrides": {},
                         "payload_mode": "text"},
}


def parse_subject(subject: str) -> tuple[str, int]:
    # "item <md5> -> episode <id>"
    left, right = subject.split(" -> episode ")
    return left.removeprefix("item ").strip(), int(right)


def build_sample(gt_linkage: list[dict]) -> list[dict]:
    """Stratified sample per plan §2 (a1). Deterministic."""
    yes, no_b, no_m, no_t = [], [], [], []
    for row in gt_linkage:
        md5, eid = parse_subject(row["subject"])
        r = row["result"]
        q = {"md5": md5, "episode_id": eid,
             "banked_verdict": r["verdict"],
             "banked_conf": float(r["confidence"])}
        if r["verdict"] == "yes":
            yes.append(q)
        elif float(r["confidence"]) < 0.9:
            no_b.append(q)
        elif float(r["confidence"]) < 1.0:
            no_m.append(q)
        else:
            no_t.append(q)
    rng = random.Random(SEED)
    rng.shuffle(no_m)
    rng.shuffle(no_t)
    sample = (yes + no_b + no_m[:100] + no_t[:72])
    for q in sample:
        if q["banked_verdict"] == "yes":
            q["stratum"] = ("yes-conf" if q["banked_conf"] >= 0.9
                            else "yes-borderline")
        elif q["banked_conf"] < 0.9:
            q["stratum"] = "no-borderline"
        elif q["banked_conf"] < 1.0:
            q["stratum"] = "no-0.9"
        else:
            q["stratum"] = "no-1.0"
    # Control subset (plan §2): all 28 borderline nos, 36
    # yeses (all borderline-band yeses + seeded confident
    # draws), 36 confident nos (18 from each confident band,
    # the same questions the full sample drew).
    yes_b = [q for q in sample if q["stratum"] == "yes-borderline"]
    yes_c = [q for q in sample if q["stratum"] == "yes-conf"]
    rng.shuffle(yes_c)
    control = (no_b + yes_b + yes_c[:36 - len(yes_b)]
               + no_m[:18] + no_t[:18])
    control_ids = {(q["md5"], q["episode_id"]) for q in control}
    for q in sample:
        q["in_control"] = (q["md5"], q["episode_id"]) in control_ids
    return sample


def rebuild_question(cfg: Config, man: Manifest, raw, q: dict,
                     n_exemplars: int, mode: str = "images"):
    """Rebuild the verifier question via the SAME builder
    try_link uses (album_sorter.linkage.
    build_verifier_question, B39) - the harness measures
    the question production asks by construction, not by a
    hand-synced copy. Returns (parts, key2, key1) or None
    when the item or episode is gone from the copy. The
    keys are computed from the builder's texts/extra with
    the identity exemplar list (first 4); n_exemplars
    governs only how many exemplar images the parts carry
    (the c1 arm's question)."""
    item = raw.execute("SELECT * FROM items WHERE md5 = ?",
                       (q["md5"],)).fetchone()
    ep = man.get_episode(q["episode_id"])
    if item is None or ep is None:
        return None
    # B46: the stored-snapshot picks live in exemplars.py -
    # the identity list is stored_exemplars(ep) (cap 4) and
    # the image slice is taken from the full stored list,
    # exactly what try_link's source produces.
    exemplars_all = stored_exemplars(ep, cap=None)
    exemplars = stored_exemplars(ep)
    prompt_text, key_text, parts, extra = build_verifier_question(
        cfg, item, ep, exemplars, mode=mode,
        exemplar_images=exemplars_all[:n_exemplars])
    key2 = decision_key(
        "linkage", cfg.phase1_model_flash, VERIFIER_SYSTEM,
        {"text": key_text, "item_md5": item["md5"],
         "exemplars": exemplars}, extra=extra)
    key1 = decision_key(
        "linkage", cfg.phase1_model_flash, VERIFIER_SYSTEM,
        {"text": prompt_text, "item_md5": item["md5"],
         "exemplars": exemplars}, extra=extra)
    return parts, key2, key1


def key_verify(man: Manifest, raw, key2: str, key1: str,
               q: dict) -> str | None:
    """Verification tier for a sampled pair.

    "key": the recomputed decision key (v2, or v1 when the
    episode's span never moved) exists in the journal and
    its result matches the ground-truth file - the plan's
    rule 1, satisfied exactly.
    "subject": a v1-era row whose span has since moved
    (the key-turnover population). The journal row's
    subject names this exact (item, episode) pair and its
    result matches ground truth, so the banked verdict is
    provably this pair's; only the span - volatile
    geometry the v2 key design deliberately removed from
    question identity - differs from the re-asked payload.
    Scoring reports the tiers separately so any
    sensitivity is visible rather than averaged away.
    None: no journal row accounts for this pair; exclude.
    """
    for key in (key2, key1):
        row = man.get_decision(key)
        if row is not None:
            v = json.loads(row["result_json"])
            if (v.get("verdict") == q["banked_verdict"]
                    and abs(float(v.get("confidence", 0))
                            - q["banked_conf"]) < 1e-9):
                return "key"
            return None
    rows = raw.execute(
        "SELECT result_json FROM decisions WHERE stage = "
        "'linkage' AND subject = ?",
        (f"item {q['md5']} -> episode {q['episode_id']}",),
    ).fetchall()
    for row in rows:
        v = json.loads(row["result_json"])
        if (v.get("verdict") == q["banked_verdict"]
                and abs(float(v.get("confidence", 0))
                        - q["banked_conf"]) < 1e-9):
            return "subject"
    return None


def subject_verify(raw, q: dict) -> str | None:
    """Subject-only verification (--verify-mode subject):
    sample membership rests entirely on the journal's
    subject rows naming this exact (item, episode) pair
    with a result matching ground truth - the "subject"
    tier of key_verify, used as the ONLY tier. For arms
    whose questions are deliberately rebuilt from changed
    inputs (B41: enriched item analyses), the recomputed
    decision keys exist nowhere by construction, so key
    verification would exclude the whole sample for a
    reason that has nothing to do with pair identity."""
    rows = raw.execute(
        "SELECT result_json FROM decisions WHERE stage = "
        "'linkage' AND subject = ?",
        (f"item {q['md5']} -> episode {q['episode_id']}",),
    ).fetchall()
    for row in rows:
        v = json.loads(row["result_json"])
        if (v.get("verdict") == q["banked_verdict"]
                and abs(float(v.get("confidence", 0))
                        - q["banked_conf"]) < 1e-9):
            return "subject"
    return None


def load_done(results_path: Path) -> set[tuple[str, str, int]]:
    done = set()
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if not r.get("error"):
                done.add((r["arm"], r["md5"], r["episode_id"]))
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest-copy", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--assets-dir", required=True)
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--arms", default="control,a1-low,c1-ex2,d-lite")
    ap.add_argument("--tier", default="flex")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--verify-mode", default="key",
                    choices=["key", "subject"],
                    help="'key' (default): decision-key "
                         "verification with subject fallback. "
                         "'subject': pair identity rests on "
                         "journal subject rows only - for "
                         "arms rebuilt from changed inputs "
                         "(B41 s1-enriched) whose keys exist "
                         "nowhere by construction")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stub", action="store_true",
                    help="answer with the banked verdict ($0 "
                         "plumbing smoke; scoring must show "
                         "perfect agreement)")
    ap.add_argument("--score", action="store_true")
    args = ap.parse_args()

    copy_path = Path(args.manifest_copy).resolve()
    if copy_path == Path("/mnt/pixel/.sorter-work-recat/"
                         "manifest.sqlite3"):
        print("refusing: experiments never touch the live "
              "manifest", file=sys.stderr)
        return 2

    gt = json.loads(Path(args.ground_truth).read_text())
    sample = build_sample(gt["linkage"])
    strata: dict[str, int] = {}
    for q in sample:
        strata[q["stratum"]] = strata.get(q["stratum"], 0) + 1
    print(f"sample: {len(sample)} questions; strata {strata}; "
          f"control subset {sum(q['in_control'] for q in sample)}")

    cfg = Config(work_dir=Path(args.work_dir))
    man = Manifest(copy_path)
    raw = sqlite3.connect(f"file:{copy_path}?mode=ro", uri=True)
    raw.row_factory = sqlite3.Row

    verified, excluded = [], []
    tiers: dict[str, int] = {}
    for q in sample:
        built = rebuild_question(cfg, man, raw, q, 4)
        if built is None:
            excluded.append((q, "item-or-episode-missing"))
            continue
        _parts, key2, key1 = built
        tier = (subject_verify(raw, q)
                if args.verify_mode == "subject"
                else key_verify(man, raw, key2, key1, q))
        if tier:
            q["key2"] = key2
            q["tier"] = tier
            tiers[tier] = tiers.get(tier, 0) + 1
            verified.append(q)
        else:
            excluded.append((q, "no-journal-row"))
    print(f"verified: {len(verified)}/{len(sample)} "
          f"(tiers {tiers}); excluded: {len(excluded)}")
    for q, why in excluded[:10]:
        print(f"  excluded {q['md5'][:8]} ep{q['episode_id']} "
              f"{q['stratum']}: {why}")
    if args.dry_run:
        return 0
    if len(verified) < 200:
        print("refusing to spend: key-verified sample too "
              "small to judge the gates", file=sys.stderr)
        return 2

    results_path = Path(args.results)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    done = load_done(results_path)
    print(f"already answered: {len(done)}")

    if args.stub:
        client = None     # stub mode answers from the banked
        # record directly in the loop; no client is touched
    else:
        from album_sorter.gemini_client import make_client
        from album_sorter.usage import attach_manifest
        client = make_client(Path(args.assets_dir))
        attach_manifest(client, man)

    from album_sorter.gemini_client import with_retries
    from album_sorter.usage import usage_context

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def ask(arm_name: str, arm: dict, model: str, q: dict,
            parts: list) -> dict:
        rec = {"arm": arm_name, "md5": q["md5"],
               "episode_id": q["episode_id"],
               "stratum": q["stratum"], "tier": q["tier"],
               "banked_verdict": q["banked_verdict"],
               "banked_conf": q["banked_conf"]}
        try:
            if args.stub:
                # Stub answers from the banked record so the
                # plumbing + scoring path is exercised end
                # to end at $0.
                rec["verdict"] = q["banked_verdict"]
                rec["confidence"] = q["banked_conf"]
                rec["tokens_in"] = rec["tokens_out"] = 0
                rec["thoughts"] = 0
            else:
                with usage_context(
                        stage=f"exp-{arm_name}",
                        subject=f"item {q['md5']} -> episode "
                                f"{q['episode_id']}",
                        decision_key=q["key2"]):
                    fresh = with_retries(lambda: client.generate_json(
                        model, VERIFIER_SYSTEM, parts,
                        thinking_level=arm["thinking"],
                        service_tier=args.tier))
                rec["verdict"] = fresh.data.get("verdict")
                rec["confidence"] = float(
                    fresh.data.get("confidence") or 0)
                rec["tokens_in"] = fresh.tokens_in
                rec["tokens_out"] = fresh.tokens_out
                rec["thoughts"] = fresh.thoughts
        except Exception as e:  # per-question isolation: a
            # dead call is one missing answer, never a dead
            # arm (the 2026-10-23 linkage lesson)
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
        return rec

    out = results_path.open("a")
    for arm_name in [a.strip() for a in args.arms.split(",")]:
        arm = dict(ARMS[arm_name])
        overrides = arm.get("cfg_overrides") or {}
        arm_cfg = (dataclasses.replace(cfg, **overrides)
                   if overrides else cfg)
        if "thinking" not in arm:
            arm["thinking"] = arm_cfg.thinking_judgment
        mode = arm.get("payload_mode", "images")
        model = (cfg.phase1_model_flash if arm["model"] == "flash"
                 else cfg.phase1_model_lite)
        qs = [q for q in verified
              if arm["subset"] == "all" or q["in_control"]]
        todo = [q for q in qs
                if (arm_name, q["md5"], q["episode_id"]) not in done]
        print(f"arm {arm_name}: {len(qs)} questions, "
              f"{len(todo)} to ask", flush=True)
        # Payloads are rebuilt serially (sqlite reads stay
        # on this thread); only the model calls fan out.
        # Flex latency is server-side queueing, so serial
        # asking would take hours for no benefit - the
        # client stack is thread-safe by design (the same
        # recording client carries Phase 1's worker pool).
        jobs = []
        for q in todo:
            built = rebuild_question(
                arm_cfg, man, raw, q, arm["exemplars"], mode=mode)
            if built is None:
                out.write(json.dumps(
                    {"arm": arm_name, "md5": q["md5"],
                     "episode_id": q["episode_id"],
                     "error": "question vanished at ask time"})
                    + "\n")
                out.flush()
                continue
            jobs.append((q, built[0]))
        finished = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(ask, arm_name, arm, model, q,
                                   parts) for q, parts in jobs]
            for fut in as_completed(futures):
                out.write(json.dumps(fut.result()) + "\n")
                out.flush()
                finished += 1
                if finished % 25 == 0:
                    print(f"  {arm_name} {finished}/{len(jobs)}",
                          flush=True)
    out.close()
    score(results_path)
    return 0


def _arm_rows(results_path: Path) -> dict[str, list[dict]]:
    arms: dict[str, list[dict]] = {}
    if not results_path.exists():
        return arms
    for line in results_path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("error"):
            continue
        arms.setdefault(r["arm"], []).append(r)
    return arms


def _is_yes(r: dict) -> bool:
    return r["verdict"] == "yes" and float(r["confidence"]) >= 0.7


def score(results_path: Path) -> None:
    arms = _arm_rows(results_path)
    print("\n=== scoring (plan §2 gates) ===")
    control = arms.get("control", [])
    control_no_flips = sum(
        1 for r in control
        if r["banked_verdict"] == "no" and _is_yes(r))
    for name, rows in sorted(arms.items()):
        if not rows:
            continue
        yes_rows = [r for r in rows if r["banked_verdict"] == "yes"]
        no_rows = [r for r in rows if r["banked_verdict"] == "no"]
        conf_yes = [r for r in yes_rows if r["banked_conf"] >= 0.9]
        recall_conf = sum(1 for r in conf_yes if _is_yes(r))
        recall_all = sum(1 for r in yes_rows if _is_yes(r))
        fa_flips = sum(1 for r in no_rows if _is_yes(r))
        # agreement counts a row as agreeing when the APPLIED
        # verdict (yes at >= 0.7 attaches; anything else does
        # not) matches the banked applied verdict
        agree = 0
        for r in rows:
            banked_applied = (r["banked_verdict"] == "yes"
                              and r["banked_conf"] >= 0.7)
            if _is_yes(r) == banked_applied:
                agree += 1
        spent = sum(r.get("tokens_in", 0) for r in rows)
        print(f"{name}: n={len(rows)} "
              f"yes-recall conf>=0.9: {recall_conf}/{len(conf_yes)} "
              f"(hard gate: all) | overall {recall_all}/"
              f"{len(yes_rows)} | false-attach flips "
              f"{fa_flips}/{len(no_rows)} (gate <=2 and "
              f"<= control {control_no_flips} + 1) | agreement "
              f"{agree}/{len(rows)} = {agree / len(rows):.1%} "
              f"(gate >= 98%) | tokens_in {spent}")
        for tier in ("key", "subject"):
            t_rows = [r for r in rows if r.get("tier") == tier]
            if not t_rows:
                continue
            t_conf = [r for r in t_rows
                      if r["banked_verdict"] == "yes"
                      and r["banked_conf"] >= 0.9]
            t_rec = sum(1 for r in t_conf if _is_yes(r))
            t_fa = sum(1 for r in t_rows
                       if r["banked_verdict"] == "no" and _is_yes(r))
            print(f"  tier {tier}: n={len(t_rows)} "
                  f"conf-yes recall {t_rec}/{len(t_conf)}, "
                  f"false-attach flips {t_fa}")


if __name__ == "__main__":
    raise SystemExit(main())
