"""Phase 2 step 2b - post-grouping episode merge pass (v1.3).

Why this exists (calibration finding, 2026-10-15): an episode
straddling a window seam can be proposed twice - once per window -
as two episodes with DISJOINT member sets (the Arte Museum visit:
36 + 40 items). The overlap-union rule cannot catch that, and the
later window may create instead of extending despite the ledger
context. This pass compares finished episodes pairwise:

- candidates (code): spans overlapping, or a gap of <= 12h between
  one episode's end and the next one's start (the seam signature);
- judgment (one Flash text call per pair): same occasion/place,
  split by a processing boundary?
- apply (code): on a confident yes, the younger episode's items
  move to the elder, descriptions merge, the absorbed episode is
  retired with outcome='merged'. Items are never touched on disk;
  this is manifest bookkeeping only, fully reversible.
"""
from __future__ import annotations

import json
from datetime import datetime

from .config import Config
from .exemplars import MERGE_ANALYSIS_CAP, exemplar_analyses
from .gemini_client import GeminiClient, with_retries
from .manifest import Manifest

# Candidate generation window. A tightening to 6h was
# counterfactual-clean over the 62 banked verdicts (14
# pairs in the (6h, 12h] band, all "separate"; the one
# "same" pair's seam gap is <= 1.47h) but was NOT adopted
# (B35 sweep): the bank holds a single positive, and the
# F14 fixture demonstrates the loser class concretely -
# a three-fragment day outing whose fragments sit 7h
# apart merges at 12h and cannot at 6h. The saving was
# ~$0.03/batch; the mechanism risk was not worth three
# cents. Revisit only with more banked positives.
MERGE_GAP_HOURS = 12.0
MERGE_CONFIDENCE = 0.7

MERGE_SYSTEM = """You compare TWO photo episodes from a family album sorter and decide
whether they are actually ONE occasion that a processing boundary
split in two (e.g. a long museum visit cut in half), or two
genuinely different occasions (different places or events, even
if close in time).

Return JSON only:
{"same": true|false, "confidence": 0.0-1.0,
 "merged_description": "<one description covering both, if same>",
 "reason": "<= 25 words"}
Same means: same place AND same continuous occasion. A day trip
with two distinct stops at different places is NOT automatically
one episode; judge by place continuity in the descriptions."""


def candidate_pairs(man: Manifest, gap_hours: float = MERGE_GAP_HOURS) -> list[tuple[int, int]]:
    """Episode id pairs (elder, younger) whose spans overlap or
    are separated by <= gap_hours. Episodes already merged or
    gated are excluded."""
    eps = [ep for ep in man.all_episodes()
           if ep["span_start"] and ep["outcome"] == "pending"]
    eps.sort(key=lambda ep: ep["span_start"])
    pairs = []
    for i, a in enumerate(eps):
        a_end = datetime.fromisoformat(a["span_end"])
        for b in eps[i + 1:]:
            b_start = datetime.fromisoformat(b["span_start"])
            gap = (b_start - a_end).total_seconds() / 3600.0
            if gap <= gap_hours:
                pairs.append((a["episode_id"], b["episode_id"]))
            else:
                break  # sorted by start; later episodes only get further away
    return pairs


def _episode_brief(man: Manifest, episode_id: int) -> dict:
    ep = man.get_episode(episode_id)
    # canonical order (B32): the brief is journal key material
    # as well as prompt content - an unordered slice would
    # make the same question hash differently run to run.
    # B46: the selection itself lives in exemplars.py now -
    # the episode's CURRENT first-3 analyzed members (the
    # historical IS NOT NULL filter, cap MERGE_ANALYSIS_CAP).
    return {"episode_id": episode_id, "description": ep["description"],
            "span": [ep["span_start"], ep["span_end"]],
            "item_count": ep["item_count"],
            "exemplar_analyses": exemplar_analyses(
                man, episode_id, MERGE_ANALYSIS_CAP)}


def brief_identity(brief: dict) -> dict:
    """The content-bearing part of an episode brief (B35):
    what a merge/rollup verdict is ABOUT - the episode's
    identity and the exemplar content it was judged from.
    Span and item_count are derived state; they ride in
    the judged context and are re-validated at replay
    instead of rotting the key (the audit measured 4 merge
    and 4 of 7 rollup verdicts dead from exactly this)."""
    return {"episode_id": brief["episode_id"],
            "description": brief["description"],
            "exemplar_analyses": brief["exemplar_analyses"]}


def pair_geometry(brief_a: dict, brief_b: dict):
    """(gap in hours from a's end to b's start, overlap?) -
    negative gaps are overlaps, the seam signature merge
    candidates are built from."""
    a_end = datetime.fromisoformat(brief_a["span"][1])
    b_start = datetime.fromisoformat(brief_b["span"][0])
    gap = (b_start - a_end).total_seconds() / 3600.0
    return gap, gap <= 0


def _count_ok(judged: int, now: int) -> bool:
    """Membership drift bound for brief re-validation: the
    count may halve or double (plus small-count slack)
    before the judged content is considered stale."""
    return judged > 0 and judged / 2 - 1 <= now <= judged * 2 + 2


def merge_ctx_ok(row, brief_a: dict, brief_b: dict) -> bool:
    """Re-validate a v2 merge verdict's judged context
    against the pair's current geometry: same overlap
    category, gap moved <= 24h, counts within bounds.
    A row without context never replays."""
    raw = row["context_json"]
    if not raw:
        return False
    try:
        ctx = json.loads(raw)
        gap1, ov1 = pair_geometry(brief_a, brief_b)
        if bool(ctx["overlap"]) != ov1:
            return False
        if abs(gap1 - float(ctx["gap_hours"])) > 24.0:
            return False
        return all(_count_ok(j, n) for j, n in zip(
            ctx["counts"], (brief_a["item_count"], brief_b["item_count"])))
    except (ValueError, KeyError, TypeError):
        return False


def apply_merge(man: Manifest, elder_id: int, younger_id: int,
                merged_description: str) -> None:
    """Move the younger episode's items to the elder and retire
    the younger. The elder keeps its id (stable references)."""
    man.execute("UPDATE items SET episode_id = ? WHERE episode_id = ?",
                (elder_id, younger_id))
    if merged_description:
        man.update_episode(elder_id, description=merged_description)
    man.update_episode(younger_id, outcome="merged", item_count=0,
                       span_start=None, span_end=None)
    man.recompute_episode_span(elder_id)


def run_merge(cfg: Config, man: Manifest, client: GeminiClient) -> dict:
    stats = {"pairs_checked": 0, "merged": 0, "kept_separate": 0, "replayed": 0}
    run_id = man.start_run("merge", {"gap_hours": MERGE_GAP_HOURS})
    from .gemini_client import coerce_float
    from .journal import decision_key, prompt_hash
    # Multiple passes (F14): merging A+B grows A's span, which
    # can make a third fragment C adjacent to the MERGED episode
    # even when C was > gap from both originals. Recompute
    # candidates per pass and stop when a pass merges nothing
    # (bounded - fragments beyond 3 are pathological).
    for _pass in range(3):
        merged_this_pass = 0
        retired: set[int] = set()
        for elder_id, younger_id in candidate_pairs(man):
            if elder_id in retired or younger_id in retired:
                continue
            brief_a = _episode_brief(man, elder_id)
            brief_b = _episode_brief(man, younger_id)
            payload = {"episode_a": brief_a, "episode_b": brief_b}
            parts = [{"text": json.dumps(payload)}]
            # B32 journaled the verdict under a hash of the
            # exact question; B35 v2 keys the pair's IDENTITY
            # (brief_identity) and stores the volatile span/
            # count geometry as judged context for replay
            # re-validation (merge_ctx_ok) - linkage growing
            # an episode no longer orphans its merge verdicts.
            # The prompt is unchanged. Pre-v2 rows remain
            # reachable through the historical key, which
            # hits only when both briefs are unchanged.
            from .journal import call_extra
            extra = call_extra(cfg, "judgment")
            key = decision_key(
                "merge", cfg.phase1_model_flash, MERGE_SYSTEM,
                {"episode_a": brief_identity(brief_a),
                 "episode_b": brief_identity(brief_b)},
                extra=extra)
            stats["pairs_checked"] += 1
            row = man.get_decision(key)
            stale = False
            if row is not None and not merge_ctx_ok(row, brief_a, brief_b):
                stale = True
                row = None
            if row is None and not stale:
                key1 = decision_key("merge", cfg.phase1_model_flash,
                                    MERGE_SYSTEM, payload, extra=extra)
                row = man.get_decision(key1)
            if row is not None:
                v = json.loads(row["result_json"])
                stats["replayed"] += 1
                fresh = None
            else:
                from .gemini_client import EmptyResponse, UnparseableOutput
                from .usage import usage_context
                try:
                    with usage_context(stage="merge",
                                       subject=f"episodes {elder_id}+{younger_id}",
                                       decision_key=key):
                        fresh = with_retries(lambda: client.generate_json(
                            cfg.phase1_model_flash, MERGE_SYSTEM, parts,
                            thinking_level=cfg.thinking_judgment,
                            service_tier=cfg.service_tier))
                except (UnparseableOutput, EmptyResponse):
                    # F-3 (B35 review): isolate the pair; the
                    # pass continues, nothing is journaled
                    stats["failed_pairs"] = stats.get("failed_pairs", 0) + 1
                    continue
                v = fresh.data
            if fresh is not None:
                _gap, _ov = pair_geometry(brief_a, brief_b)
                judged_ctx = {"gap_hours": _gap, "overlap": _ov,
                              "counts": [brief_a["item_count"],
                                         brief_b["item_count"]]}
            if v.get("same") and coerce_float(v.get("confidence"), 0.0) >= MERGE_CONFIDENCE:
                with man.transaction():
                    if fresh is not None:
                        man.record_decision(
                            key, "merge", f"episodes {elder_id}+{younger_id}", v,
                            model=cfg.phase1_model_flash,
                            prompt_hash=prompt_hash(MERGE_SYSTEM),
                            tokens_in=fresh.tokens_in, tokens_out=fresh.tokens_out,
                            tokens_thoughts=fresh.thoughts,
                            context=judged_ctx, replace=stale)
                    apply_merge(man, elder_id, younger_id,
                                v.get("merged_description") or "")
                retired.add(younger_id)
                stats["merged"] += 1
                merged_this_pass += 1
            else:
                if fresh is not None:
                    man.record_decision(
                        key, "merge", f"episodes {elder_id}+{younger_id}", v,
                        model=cfg.phase1_model_flash,
                        prompt_hash=prompt_hash(MERGE_SYSTEM),
                        tokens_in=fresh.tokens_in, tokens_out=fresh.tokens_out,
                        tokens_thoughts=fresh.thoughts,
                        context=judged_ctx, replace=stale)
                stats["kept_separate"] += 1
        if not merged_this_pass:
            break
    man.finish_run(run_id, str(stats))
    return stats
