"""Phase 1 - merged classify + analyze (spec Section 6).

ONE vision call per item returns category + confidence and, for
PHOTO items, the full analysis record in the same response.
Deterministic guards are applied to the model output by code
(confidence floor -> REVIEW; PHOTO without a label -> retry once).

Calls are independent, so the driver runs them with bounded
concurrency (workers call the API; the main thread alone writes
the manifest). 200 sequential calls would take ~15 minutes;
8 workers bring it to ~2.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import Config
from .gemini_client import GeminiClient, with_retries
from .manifest import Manifest
from .metadata import soft_hints

# B41 (IMPROVEMENT-PLAN S1): the phase-1 label schema
# version. v2 enriches the label so the text layer can carry
# the evidence downstream stages match on (scene gist,
# distinctive features, clothing detail); v1 labels in
# banked manifests simply lack the fields and the "v"
# stamp (absence = v1). A coverage-ranked color list was
# part of v2 as first built and was dropped before the
# schema ever shipped: the Phase D audit judged it 23%
# incorrect - the model lists salient accent colors, not
# coverage-dominant ones (B41).
# The stamp is written by run_phase1 at persistence, not
# trusted to the model, and readers treat the label as
# opaque text - additive fields only, never a rename.
PHASE1_SCHEMA_VERSION = 2

SYSTEM = """You classify and describe one family media item in a single pass.

Return JSON only:
{
  "category": "PHOTO" | "MISC" | "MEDICAL" | "REVIEW",
  "confidence": 0.0-1.0,
  "junk_subtype": "screenshot"|"receipt"|"document"|"meme"|"product"|"accidental"|null,
  "reasoning": "<= 25 words",
  "label": null OR {
    "v": 2,
    "description": "2-3 sentences",
    "objects": ["..."],
    "people": {"count": 0, "description": "..."},
    "place_description": "...",
    "place_name_guess": null,
    "wider_location_guess": null,
    "event_description": null,
    "visible_text": null,
    "scene": {"setting": "indoor|outdoor|vehicle|transit or short free text",
              "environment": "short free text, e.g. 'snowy lakeside'",
              "lighting": "short free text, e.g. 'bright daylight'"},
    "distinctive_features": ["<= 3 short strings: what makes this photo different from a generic scene of its kind"]
  }
}

Rules:
- PHOTO = a real photograph worth keeping. MISC = screenshots, memes,
  receipts, product shots, accidental shots, messaging-app junk.
  MEDICAL = clinical photos (wounds, rashes, scans, documents about
  treatment). REVIEW = you genuinely cannot decide.
- Populate "label" ONLY when category is PHOTO; keep it null otherwise.
- People: describe only - counts, apparent ages, clothing colors and
  one distinguishing detail per person or group, activity.
  NEVER name or identify individuals.
- visible_text: quote signage exactly as written when it is
  readable; null when there is no readable text.
- scene, distinctive_features: record only what is plainly
  visible. These fields are evidence later stages match on,
  so a plain generic answer beats a specific guess.
- Terse prose, complete fields: short sentences, but populate
  every label field the photo supports. The label is a record,
  not an essay.
"""

MAX_WORKERS = 8

# After this many failed runs an item is parked (left
# 'ingested', counted in the run stats, never re-bought):
# a deterministically failing item - a safety block that
# will not change, a corrupt asset - otherwise costs one
# call on EVERY run for the life of the manifest (B35
# sweep). Parking decides nothing about the item; it
# stops re-asking a question that has never produced an
# answer. REVIEW handling stays a human's call.
MAX_PHASE1_ATTEMPTS = 3


def apply_guards(raw: dict, floor: float) -> dict:
    """Deterministic post-processing of one raw model result."""
    from .gemini_client import coerce_float
    out = dict(raw)
    cat = out.get("category")
    conf = coerce_float(out.get("confidence"), 0.0)
    out["confidence"] = conf
    if cat not in ("PHOTO", "MISC", "MEDICAL", "REVIEW"):
        out.update(category="ERROR", confidence=conf)
        return out
    if cat != "REVIEW" and conf < floor:
        out["reasoning"] = (out.get("reasoning") or "") + " [demoted: low confidence]"
        out["category"] = "REVIEW"
    return out


def _classify_one(cfg: Config, client: GeminiClient, row) -> tuple[str, dict, int, int]:
    """Worker: API calls only, no manifest writes.
    Returns (md5, guarded_data, tokens_in, tokens_out)."""
    meta_block = {
        "filename": row["done_name"], "timestamp": row["capture_time_raw"],
        "device": row["device"], "time_confidence": row["time_confidence"],
        "hints": soft_hints(row["done_name"], type("M", (), {"software": ""})()),
    }
    parts = [{"text": json.dumps(meta_block)},
             {"image_md5": row["md5"]}]  # transport resolves to the analysis copy
    # B33: usage attribution is set here, inside the worker
    # thread that makes the calls - a contextvar set by the
    # submitting thread would not be seen by the pool
    from .usage import usage_context
    with usage_context(stage="phase1", subject=row["md5"]):
        res = with_retries(lambda: client.generate_json(cfg.phase1_model, SYSTEM, parts,
                                         max_output_tokens=cfg.phase1_max_output_tokens,
                                         service_tier=cfg.service_tier))
        data = apply_guards(res.data, cfg.triage_confidence_floor)
        tin, tout = res.tokens_in, res.tokens_out
        if data["category"] == "PHOTO" and not data.get("label"):
            # F5 salvage (no-re-spend audit; B35 review F-4):
            # call #1's classification is PAID FOR and valid -
            # the retry may contribute ONLY its label field,
            # and if the retry itself fails, the item proceeds
            # on call #1's result instead of deferring and
            # re-buying the whole classification next run.
            # (The classification is persisted by the caller
            # from this return value; a process kill between
            # the two calls is bounded by the F6 drain window,
            # accepted under the single-writer design.)
            from .gemini_client import BudgetExceeded
            try:
                res2 = with_retries(lambda: client.generate_json(
                    cfg.phase1_model, SYSTEM, parts,
                    max_output_tokens=cfg.phase1_max_output_tokens,
                    service_tier=cfg.service_tier))
                tin += res2.tokens_in
                tout += res2.tokens_out
                label2 = apply_guards(
                    res2.data, cfg.triage_confidence_floor).get("label")
                if label2:
                    data = {**data, "label": label2}
            except BudgetExceeded:
                raise   # the cap stops the run, salvage or not
            except Exception:
                pass    # proceed on call #1's result, label-less
    return row["md5"], data, tin, tout


def run_phase1(cfg: Config, man: Manifest, client: GeminiClient,
               limit: int | None = None) -> dict:
    stats = {"processed": 0, "photo": 0, "errors": 0, "deferred": 0,
             "parked": 0}
    run_id = man.start_run("phase1", {"model": cfg.phase1_model})
    rows = man.items_by_status("ingested")
    live = [r for r in rows
            if (r["phase1_attempts"] or 0) < MAX_PHASE1_ATTEMPTS]
    stats["parked"] = len(rows) - len(live)
    rows = live
    if limit is not None:
        rows = rows[:limit]
    # B41 batch-boundary rule: a manifest's analyses belong
    # to the schema version the batch STARTED under. The
    # first phase-1 write stamps that version into manifest
    # meta; a later run under a different schema with items
    # still ingested would mix schemas inside one batch -
    # never an accident, so warn loudly in the run report
    # (stats) and on stderr, and leave the original stamp
    # standing (it records what the batch began as).
    if rows:
        stamp = man.get_meta("phase1_schema_version")
        if stamp is None:
            man.set_meta("phase1_schema_version",
                         str(PHASE1_SCHEMA_VERSION))
        elif stamp != str(PHASE1_SCHEMA_VERSION):
            import sys as _sys
            stats["schema_warning"] = (
                f"phase1 schema mismatch: this manifest's batch "
                f"started under schema v{stamp}, the current "
                f"schema is v{PHASE1_SCHEMA_VERSION}, and "
                f"{len(rows)} ingested items remain - continuing "
                f"mixes analysis schemas in one batch "
                f"(batch-boundary rule, B41); this is the "
                f"operator's explicit choice, never automatic")
            print(f"WARNING: {stats['schema_warning']}",
                  file=_sys.stderr)
    base = {r["md5"]: (r["tokens_in"], r["tokens_out"], r["error"],
                       r["phase1_attempts"] or 0) for r in rows}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_classify_one, cfg, client, r): r["md5"] for r in rows}
        for fut in as_completed(futures):
            md5 = futures[fut]
            try:
                md5, data, tin, tout = fut.result()
            except Exception as e:
                # one poison item must not wedge the whole
                # batch (F8) - but a failed CALL is not a
                # verdict (B32): a safety block, an SDK
                # failure, or a systemic outage (402, network)
                # leaves the item in 'ingested' with the error
                # recorded, so the next run retries it. The
                # old behavior wrote a terminal triaged ERROR,
                # which filed a transient API failure as a
                # permanent REVIEW item and no resume could
                # ever revisit it.
                man.update_item(
                    md5, error=f"phase1: {type(e).__name__}: {e}",
                    phase1_attempts=base[md5][3] + 1)
                stats["processed"] += 1
                stats["deferred"] += 1
                continue
            cat = data["category"]
            b_tin, b_tout, b_err, _b_att = base[md5]
            label = data.get("label") if cat == "PHOTO" else None
            if label:
                # B41: the v stamp is written by code at the
                # one persistence site, not trusted to the
                # model - a label this code writes IS a
                # current-schema label by construction.
                label = {**label, "v": PHASE1_SCHEMA_VERSION}
            man.update_item(
                md5, category=None if cat == "ERROR" else cat,
                category_confidence=data.get("confidence"),
                category_source="model",
                analysis_json=json.dumps(label) if label else None,
                status="triaged" if cat != "PHOTO" else "analyzed",
                tokens_in=b_tin + tin, tokens_out=b_tout + tout,
                # a successful retry clears a deferred failure's
                # error (B32); only a fresh ERROR verdict sets one
                error="phase1: unparseable category" if cat == "ERROR" else None)
            stats["processed"] += 1
            stats["photo"] += int(cat == "PHOTO")
            stats["errors"] += int(cat == "ERROR")
    man.finish_run(run_id, str(stats))
    if rows and stats["deferred"] == len(rows) and any(
            base[r["md5"]][3] == 0 for r in rows):
        # every call failed AND at least one failure is an
        # item's FIRST: this is a systemic failure (credits,
        # outage, bad key), not a batch of poison items.
        # Abort loudly instead of letting the run proceed -
        # and report success - on an empty state. A run
        # whose only failures are repeat failures of known
        # items carries no systemic signal (with parking,
        # a lone poison item would otherwise trip this guard
        # on every run until it parks).
        raise RuntimeError(
            f"phase1: all {len(rows)} attempted items failed to classify "
            f"(systemic API failure?); items left in 'ingested' for retry")
    return stats
