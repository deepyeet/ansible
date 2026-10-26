"""Phase 2 step 3 - linkage backstop (spec Section 11).

Candidate nomination is deterministic: span containment, time
adjacency, GPS footprint. A vision verifier (via the client
interface) decides attachment. Embeddings-based retrieval is the
deferred enhancement (spec Section 20).
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from .config import Config
from .exemplars import stored_exemplars
from .gemini_client import GeminiClient, with_retries
from .journal import call_extra, image_extra
from .manifest import Manifest


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def episode_gps_centroid(man: Manifest, episode_id: int):
    rows = man.query(
        "SELECT gps_lat, gps_lon FROM items WHERE episode_id = ? "
        "AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL",
        (episode_id,))
    if not rows:
        return None
    return (sum(r["gps_lat"] for r in rows) / len(rows),
            sum(r["gps_lon"] for r in rows) / len(rows))


def episode_gps_nearest_km(man: Manifest, episode_id: int,
                           lat: float, lon: float) -> float | None:
    """Distance to the episode's NEAREST member photo. The
    veto footprint (B25 decision, 2026-10-15): an episode is
    a set of points, not a center - for a multi-stop trip
    the centroid can sit >50 km from every real photo and
    veto legitimate members; nearest-member cannot."""
    rows = man.query(
        "SELECT gps_lat, gps_lon FROM items WHERE episode_id = ? "
        "AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL",
        (episode_id,))
    if not rows:
        return None
    return min(haversine_km(lat, lon, r["gps_lat"], r["gps_lon"]) for r in rows)


def nominate(cfg: Config, man: Manifest, item,
             stats: dict | None = None) -> list[int]:
    """Candidate episode ids for an unassigned item, ranked by
    timestamp distance to the episode span.

    B34 pruning (deterministic - timestamps and distances,
    never verdicts): the GPS strong-include nominates only
    when the time gap is also within
    linkage_gps_adjacency_days (it used to nominate at any
    distance, so a home selfie was verified against every
    home episode on record), and the ranked list is capped
    at linkage_max_candidates. stats, when given, counts
    nominated vs pruned so the saving is visible in run
    logs."""
    t = datetime.fromisoformat(item["capture_time"])
    out = []
    for ep in man.all_episodes():
        # B35 (state-design sweep): only pending episodes may
        # receive members. Without this filter a post-gate
        # `cli link` could attach items into an episode the
        # gates already decided - silently rewriting a gated
        # album's membership after its outcome was final.
        if ep["outcome"] != "pending":
            continue
        if not ep["span_start"]:
            continue
        s = datetime.fromisoformat(ep["span_start"])
        e = datetime.fromisoformat(ep["span_end"])
        dist_days = 0.0
        if s <= t <= e:
            dist_days = 0.0                       # containment
        elif t < s:
            dist_days = (s - t).total_seconds() / 86400
        else:
            dist_days = (t - e).total_seconds() / 86400
        reason = dist_days == 0.0 or dist_days <= cfg.linkage_adjacency_days
        if item["gps_lat"] is not None:
            km = episode_gps_nearest_km(man, ep["episode_id"],
                                        item["gps_lat"], item["gps_lon"])
            if km is not None:
                if km > cfg.gps_veto_km:
                    reason = False                # GPS veto
                elif km < cfg.gps_strong_km and \
                        dist_days <= cfg.linkage_gps_adjacency_days:
                    reason = True
        if reason:
            out.append((dist_days, ep["episode_id"]))
    ranked = [eid for _d, eid in sorted(out)]
    if stats is not None:
        stats["nominated"] = stats.get("nominated", 0) + len(ranked)
        stats["pruned"] = stats.get("pruned", 0) + \
            max(0, len(ranked) - cfg.linkage_max_candidates)
    return ranked[:cfg.linkage_max_candidates]


VERIFIER_SYSTEM = """You decide whether ONE loose photo belongs to a candidate photo episode
(same place / event thread). Answer JSON only:
{"verdict": "yes"|"no", "confidence": 0.0-1.0, "reason": "<= 20 words"}
Judge place and occasion continuity, not image quality. A plain or
blank-looking photo can still belong if its details match the place."""


def _time_geometry(item_time: str, span_start: str, span_end: str):
    """(distance in days from the item to the span, contained?)
    - the time formula nominate() ranks candidates by."""
    t = datetime.fromisoformat(item_time)
    s = datetime.fromisoformat(span_start)
    e = datetime.fromisoformat(span_end)
    if s <= t <= e:
        return 0.0, True
    if t < s:
        return (s - t).total_seconds() / 86400, False
    return (t - e).total_seconds() / 86400, False


def _drift_ok(row, cfg: Config, item, ep) -> bool:
    """B35 replay re-validation for a v2 linkage verdict:
    the judged geometry (stored in the row's context) must
    still describe the pair - same containment, distance
    within cfg.linkage_span_drift_days. A row without
    context cannot be validated and never replays."""
    raw = row["context_json"]
    if not raw:
        return False
    try:
        ctx = json.loads(raw)
        d0 = float(ctx["dist_days"])
        c0 = bool(ctx["contained"])
    except (ValueError, KeyError, TypeError):
        return False
    d1, c1 = _time_geometry(item["capture_time"],
                            ep["span_start"], ep["span_end"])
    return c1 == c0 and abs(d1 - d0) <= cfg.linkage_span_drift_days


def _img_part(md5, res):
    d = {"image_md5": md5}
    if res:
        d["media_resolution"] = res
    return d


def build_verifier_question(cfg: Config, item, ep, exemplars: list[str],
                            mode: str = "images",
                            exemplar_images: list[str] | None = None):
    """The ONE construction of a linkage verifier question
    (B39) - used by try_link (production) and by
    validate_linkage (the B35/B38 harness), so a validation
    arm measures the question production asks by
    construction, not by a hand-synced copy. Returns
    (prompt_text, key_text, parts, extra); callers hash
    key_text (the B35 v2 identity text, span-free) and
    prompt_text (the v1 fallback text) into decision keys
    with `extra` as the key's extra material.

    mode="images" at any cfg reproduces the historical
    question exactly: prompt_text carries the episode span
    (still shown to the model), extra is image_extra +
    call_extra with nothing added, and parts are the item
    image plus the exemplar images. mode="text" sends the
    prompt text only (B38 arm). mode="sheet" is Phase C
    (B38) and deliberately unbuilt. Any mode other than
    "images" joins the key extra as payload_mode, so
    verdicts bought under one payload shape can never
    replay under another; at "images" the extra is exactly
    the historical one - no new keys.

    exemplars is the question-identity list (the episode's
    first 4; hashed into the decision key). exemplar_images,
    when given, is the list whose images are actually sent
    (the harness's c1 arm sends 2 while the key keeps the
    identity 4); it defaults to exemplars."""
    prompt_text = json.dumps({
        "episode_description": ep["description"],
        "episode_span": [ep["span_start"], ep["span_end"]],
        "item_time": item["capture_time"],
        "item_analysis": item["analysis_json"]})
    key_text = json.dumps({
        "episode_description": ep["description"],
        "item_time": item["capture_time"],
        "item_analysis": item["analysis_json"]})
    extra = {**image_extra(cfg), **call_extra(cfg, "judgment")}
    if mode != "images":
        extra["payload_mode"] = mode
    if mode == "text":
        parts = [{"text": prompt_text}]
    elif mode == "images":
        shown = exemplars if exemplar_images is None \
            else exemplar_images
        parts = [{"text": prompt_text},
                 {"text": "The item in question:"},
                 _img_part(item["md5"], cfg.media_res_item),
                 {"text": "Exemplar images from the episode:"}] + \
                [_img_part(m, cfg.media_res_exemplar) for m in shown]
    elif mode == "sheet":
        raise NotImplementedError(
            "sheet payload mode is Phase C (B38) - not built")
    else:
        raise ValueError(f"unknown verifier payload mode: {mode!r}")
    return prompt_text, key_text, parts, extra


def try_link(cfg: Config, man: Manifest, client: GeminiClient, item,
             stats: dict | None = None) -> bool:
    """Attempt to attach one unassigned item. Returns True if attached.

    B32 journaled every verdict under a hash of the exact
    question - including the episode's span, which is
    derived state that changes every time the episode
    grows, so ~14% of verdicts per growth round became
    unreachable and were re-bought (measured, B35).

    B35 v2: the key covers the question's IDENTITY - item,
    episode description, exemplars, model, prompt, image
    parameters - and the span (still shown to the model;
    the prompt is unchanged) is stored as judged context
    and re-validated deterministically at replay: the pair
    must still be nominated, the episode still pending,
    containment unchanged, and the distance within
    cfg.linkage_span_drift_days. A v2 row that fails
    validation is re-asked once and the row self-heals
    (replace). Pre-v2 (v1) rows are still honored via a
    fallback lookup of the historical key, which can only
    hit when the span is byte-identical to the judged one
    - nothing banked is ever orphaned."""
    from .gemini_client import coerce_float
    from .journal import decision_key, prompt_hash
    if item["episode_id"] is not None or \
            item["status"] not in ("analyzed", "embedded"):
        return False
    for eid in nominate(cfg, man, item, stats):
        ep = man.get_episode(eid)
        if ep is None or ep["outcome"] != "pending":
            continue    # a gated/merged episode never gains members
        # B46: the identity list comes from the stored
        # grouping snapshot via exemplars.stored_exemplars -
        # grouping's exemplars are model-chosen and not
        # reproducible from membership, so this site's SOURCE
        # is the documented exception to membership freshness
        # (see album_sorter/exemplars.py).
        exemplars = stored_exemplars(ep)
        # spec 11.3: the verifier sees the item's analysis copy
        # AND up to 4 exemplar images (B22 - previously it got
        # exemplar md5 STRINGS as text and judged blind). The
        # question is built by build_verifier_question - the
        # one construction the validation harness shares (B39).
        prompt_text, key_text, parts, extra = \
            build_verifier_question(cfg, item, ep, exemplars)
        key2 = decision_key(
            "linkage", cfg.phase1_model_flash, VERIFIER_SYSTEM,
            {"text": key_text, "item_md5": item["md5"],
             "exemplars": exemplars},
            extra=extra)
        row = man.get_decision(key2)
        stale_v2 = False
        if row is not None and not _drift_ok(row, cfg, item, ep):
            stale_v2 = True     # re-ask once; the row self-heals
            row = None
        if row is None and not stale_v2:
            # v1 fallback: the historical key embedded the
            # span in the hashed text, so it hits only when
            # the current span is the judged span exactly
            key1 = decision_key(
                "linkage", cfg.phase1_model_flash, VERIFIER_SYSTEM,
                {"text": prompt_text, "item_md5": item["md5"],
                 "exemplars": exemplars},
                extra=extra)
            row = man.get_decision(key1)
        if row is not None:
            v = json.loads(row["result_json"])
            fresh = None
            if stats is not None:
                stats["replayed"] = stats.get("replayed", 0) + 1
        else:
            from .gemini_client import EmptyResponse, UnparseableOutput
            from .usage import usage_context
            try:
                with usage_context(stage="linkage",
                                   subject=f"item {item['md5']} -> episode {eid}",
                                   decision_key=key2):
                    fresh = with_retries(lambda: client.generate_json(
                        cfg.phase1_model_flash, VERIFIER_SYSTEM, parts,
                        thinking_level=cfg.thinking_judgment,
                        service_tier=cfg.service_tier))
            except (UnparseableOutput, EmptyResponse):
                # F-3 (B35 review): a semantic failure is one
                # question's failure, not the pass's - the
                # billed call is ledgered and spilled, nothing
                # is journaled, and the pair is simply asked
                # again on a later run. Transport failures and
                # the spend cap still stop the pass.
                if stats is not None:
                    stats["failed_questions"] = \
                        stats.get("failed_questions", 0) + 1
                continue
            v = fresh.data
            if stats is not None:
                stats["called"] = stats.get("called", 0) + 1
            dist, contained = _time_geometry(
                item["capture_time"], ep["span_start"], ep["span_end"])
            judged_ctx = {"span": [ep["span_start"], ep["span_end"]],
                          "dist_days": dist, "contained": contained}
        conf = coerce_float(v.get("confidence"), 0.0)
        if v.get("verdict") == "yes" and conf >= cfg.verifier_attach_threshold:
            with man.transaction():
                if fresh is not None:
                    man.record_decision(
                        key2, "linkage", f"item {item['md5']} -> episode {eid}", v,
                        model=cfg.phase1_model_flash,
                        prompt_hash=prompt_hash(VERIFIER_SYSTEM),
                        tokens_in=fresh.tokens_in, tokens_out=fresh.tokens_out,
                        tokens_thoughts=fresh.thoughts,
                        context=judged_ctx, replace=stale_v2)
                man.update_item(item["md5"], episode_id=eid,
                                assign_confidence=conf,
                                assign_method="linkage", status="grouped")
                man.recompute_episode_span(eid)
            return True
        if fresh is not None:
            man.record_decision(
                key2, "linkage", f"item {item['md5']} -> episode {eid}", v,
                model=cfg.phase1_model_flash,
                prompt_hash=prompt_hash(VERIFIER_SYSTEM),
                tokens_in=fresh.tokens_in, tokens_out=fresh.tokens_out,
                tokens_thoughts=fresh.thoughts,
                context=judged_ctx, replace=stale_v2)
    return False
