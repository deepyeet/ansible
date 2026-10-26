"""Phase 2 step 2c - trip rollup (v1.5, user decision
2026-10-15: "roll trips up into one album").

Sibling episodes that are stops on one trip (a Venetian evening,
a hotel view, a museum visit) should become ONE album for the
trip, not one album per stop. This pass runs after the seam
merge (step 2b) and before the gates:

- nomination (code): chains of pending episodes linked by
  start-to-previous-end gaps of <= rollup_gap_hours, chain span
  <= rollup_max_span_days. Nomination is deliberately generous -
  home days chain too.
- judgment (one Flash text call per chain): is this one journey
  away from the home base, or local life on consecutive days?
- apply (code): on a confident yes, all episodes in the chain
  fuse into the elder (merge.apply_merge), carrying the trip
  description; the gate then judges and names the fused episode
  as a single unit.
"""
from __future__ import annotations

import json
from datetime import datetime

from .config import Config
from .gemini_client import GeminiClient, with_retries
from .manifest import Manifest
from .merge import _count_ok, _episode_brief, apply_merge, brief_identity

# B35: a stored rollup verdict replays while every member's
# span stays within this drift of the judged span (and its
# count within the shared brief bound) - the same horizon
# family as linkage's span drift.
ROLLUP_SPAN_DRIFT_DAYS = 14.0


def _rollup_ctx_ok(row, briefs: list[dict]) -> bool:
    """Re-validate a v2 rollup verdict's judged context:
    same member count, every member's span start/end
    within ROLLUP_SPAN_DRIFT_DAYS, counts within bounds.
    A row without context never replays."""
    raw = row["context_json"]
    if not raw:
        return False
    try:
        ctx = json.loads(raw)
        if len(ctx["spans"]) != len(briefs):
            return False
        bound = ROLLUP_SPAN_DRIFT_DAYS * 86400
        for (s0, e0), b, judged_count in zip(
                ctx["spans"], briefs, ctx["counts"]):
            if abs((datetime.fromisoformat(b["span"][0])
                    - datetime.fromisoformat(s0)).total_seconds()) > bound:
                return False
            if abs((datetime.fromisoformat(b["span"][1])
                    - datetime.fromisoformat(e0)).total_seconds()) > bound:
                return False
            if not _count_ok(judged_count, b["item_count"]):
                return False
        return True
    except (ValueError, KeyError, TypeError, IndexError):
        return False

ROLLUP_SYSTEM = """You look at a CHAIN of photo episodes from consecutive days and
decide whether they are stops on ONE TRIP - a single journey
away from the family's home base (travel with overnight stays
or a coherent multi-stop journey arc) - or unrelated episodes
that merely happen near each other in time.

Local life is NOT a trip: episodes at home, day outings from
home, local errands and everyday activities stay separate even
on consecutive days. Hotels, resorts, unfamiliar destinations,
and a multi-day arc away from home indicate a trip.

Return JSON only:
{"is_trip": true|false, "confidence": 0.0-1.0,
 "trip_name": "<short plain name WITHOUT year prefix, e.g.
   'Las Vegas Trip', if a trip>",
 "merged_description": "<one description of the whole trip,
   if a trip>",
 "reason": "<= 25 words"}"""


def candidate_chains(man: Manifest, gap_hours: float, max_span_days: float) -> list[list[int]]:
    eps = [ep for ep in man.all_episodes()
           if ep["span_start"] and ep["outcome"] == "pending"]
    eps.sort(key=lambda ep: ep["span_start"])
    chains: list[list[dict]] = []
    cur: list[dict] = []
    cur_max_end: datetime | None = None
    for ep in eps:
        if cur:
            # the gap is measured from the chain's LATEST end,
            # not the last member's (F15): a short episode nested
            # inside a longer one's span must not break the chain
            start = datetime.fromisoformat(ep["span_start"])
            chain_start = datetime.fromisoformat(cur[0]["span_start"])
            gap = (start - cur_max_end).total_seconds() / 3600.0
            span = (datetime.fromisoformat(ep["span_end"]) - chain_start).days
            if gap <= gap_hours and span <= max_span_days:
                cur.append(ep)
                cur_max_end = max(cur_max_end, datetime.fromisoformat(ep["span_end"]))
                continue
            if len(cur) >= 2:
                chains.append(cur)
            cur = [ep]
            cur_max_end = datetime.fromisoformat(ep["span_end"])
        else:
            cur = [ep]
            cur_max_end = datetime.fromisoformat(ep["span_end"])
    if len(cur) >= 2:
        chains.append(cur)
    return [[ep["episode_id"] for ep in chain] for chain in chains]


def apply_rollup(man: Manifest, chain: list[int], merged_description: str) -> None:
    elder = chain[0]
    for younger in chain[1:]:
        apply_merge(man, elder, younger, "")
    if merged_description:
        man.update_episode(elder, description=merged_description)


def run_rollup(cfg: Config, man: Manifest, client: GeminiClient) -> dict:
    stats = {"chains_checked": 0, "trips_formed": 0, "kept_separate": 0,
             "replayed": 0}
    run_id = man.start_run("rollup", {"gap_hours": cfg.rollup_gap_hours,
                                      "max_span_days": cfg.rollup_max_span_days,
                                      "confidence": cfg.rollup_confidence})
    from .gemini_client import coerce_float
    from .journal import decision_key, prompt_hash
    for chain in candidate_chains(man, cfg.rollup_gap_hours, cfg.rollup_max_span_days):
        briefs = [_episode_brief(man, eid) for eid in chain]
        payload = {"episodes": briefs}
        parts = [{"text": json.dumps(payload)}]
        # B32 journaled the verdict under the full briefs;
        # B35 v2 keys the chain's IDENTITY (member ids,
        # descriptions, exemplar content) and re-validates
        # the judged spans/counts at replay (_rollup_ctx_ok),
        # so linkage growth no longer orphans chain verdicts
        # (the audit found 4 of 7 dead). The prompt is
        # unchanged; pre-v2 rows stay reachable through the
        # historical key while briefs are unchanged.
        from .journal import call_extra
        extra = call_extra(cfg, "judgment")
        key = decision_key(
            "rollup", cfg.phase1_model_flash, ROLLUP_SYSTEM,
            {"episodes": [brief_identity(b) for b in briefs]},
            extra=extra)
        stats["chains_checked"] += 1
        row = man.get_decision(key)
        stale = False
        if row is not None and not _rollup_ctx_ok(row, briefs):
            stale = True
            row = None
        if row is None and not stale:
            key1 = decision_key("rollup", cfg.phase1_model_flash,
                                ROLLUP_SYSTEM, payload, extra=extra)
            row = man.get_decision(key1)
        if row is not None:
            v = json.loads(row["result_json"])
            stats["replayed"] += 1
            fresh = None
        else:
            from .gemini_client import EmptyResponse, UnparseableOutput
            from .usage import usage_context
            try:
                with usage_context(stage="rollup", subject=f"chain {chain}",
                                   decision_key=key):
                    fresh = with_retries(lambda: client.generate_json(
                        cfg.phase1_model_flash, ROLLUP_SYSTEM, parts,
                        thinking_level=cfg.thinking_judgment,
                        service_tier=cfg.service_tier))
            except (UnparseableOutput, EmptyResponse):
                # F-3 (B35 review): isolate the chain; the
                # pass continues, nothing is journaled
                stats["failed_chains"] = stats.get("failed_chains", 0) + 1
                continue
            v = fresh.data
            judged_ctx = {"spans": [b["span"] for b in briefs],
                          "counts": [b["item_count"] for b in briefs]}
        if v.get("is_trip") and coerce_float(v.get("confidence"), 0.0) >= cfg.rollup_confidence:
            with man.transaction():
                if fresh is not None:
                    man.record_decision(
                        key, "rollup", f"chain {chain}", v,
                        model=cfg.phase1_model_flash,
                        prompt_hash=prompt_hash(ROLLUP_SYSTEM),
                        tokens_in=fresh.tokens_in, tokens_out=fresh.tokens_out,
                        tokens_thoughts=fresh.thoughts,
                        context=judged_ctx, replace=stale)
                apply_rollup(man, chain, v.get("merged_description") or "")
            stats["trips_formed"] += 1
        else:
            if fresh is not None:
                man.record_decision(
                    key, "rollup", f"chain {chain}", v,
                    model=cfg.phase1_model_flash,
                    prompt_hash=prompt_hash(ROLLUP_SYSTEM),
                    tokens_in=fresh.tokens_in, tokens_out=fresh.tokens_out,
                    tokens_thoughts=fresh.thoughts,
                    context=judged_ctx, replace=stale)
            stats["kept_separate"] += 1
    man.finish_run(run_id, str(stats))
    return stats
