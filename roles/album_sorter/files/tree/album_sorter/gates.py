"""Phase 2 step 4 - gates, registry match, naming (spec Section 12).

One text call per episode proposes a registry match / character /
name; CODE applies the outcome rules in order. Registry data:
album-registry-photos.csv harvested from the Pixel 1 Photos DB.
"""
from __future__ import annotations

import csv
import json
import re
from difflib import SequenceMatcher

from .config import Config
from .exemplars import GATE_ANALYSIS_CAP, exemplar_analyses, verify_exemplars
from .gemini_client import BudgetExceeded, EmptyResponse, GeminiClient, UnparseableOutput, with_retries
from .manifest import Manifest

YEAR_PREFIX = re.compile(r"^\d{4}\s+")
# Dumping-ground albums are buckets, not event albums: the yearly
# Snaps line (incl. "Law Snaps" / "No EXIF ..." variants) and the
# new monthly buckets must never be registry MATCHES - routine
# episodes belong in month buckets by rule, not by a model match.
# (Found live in the 2026-10-15 smoke run: the gate matched three
# routine episodes to "2026 Snaps" at >= 0.7 confidence.)
BUCKET_NAME = re.compile(
    r"^\d{4} .*Snaps$"
    r"|^\d{4} (January|February|March|April|May|June|July|August|"
    r"September|October|November|December)$")


def is_bucket_name(name: str) -> bool:
    return bool(BUCKET_NAME.match(name))


def load_registry(cfg: Config) -> list[dict]:
    """The harvested registry is pipe-delimited, no header:
    album name | item count (from the Photos DB harvest)."""
    if not cfg.registry_csv.exists():
        return []
    out = []
    for line in cfg.registry_csv.read_text().splitlines():
        if "|" not in line:
            continue
        name, _, count = line.rpartition("|")
        out.append({"name": name.strip(),
                    "items": int(count) if count.strip().isdigit() else 0})
    return out


def registry_names_for_years(registry: list[dict], years: set[int]) -> list[str]:
    return [r["name"] for r in registry
            if not is_bucket_name(r["name"])
            and any(r["name"].startswith(str(y)) for y in years)]


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


GATE_SYSTEM = """You judge one photo episode for a family's album library. Two jobs:

1. REGISTRY MATCH: does this episode belong to an existing album
   in the registry list provided? A match means this episode is
   the SAME SPECIFIC event or series as that album - not merely
   the same KIND of event. A birthday gathering is not a match
   for a named person's birthday album unless the description
   or exemplars identify THAT person; likewise a beach day is
   not a match for a named beach-trip album. When the episode
   could belong to several albums of the same type, it matches
   none of them. The same recurring place/event in a DIFFERENT
   year is NOT a match.

2. NOTABILITY: if there were no registry, would this family make
   this episode its own album? Judge the OCCASION, never the
   photo count - the item count is context, not a criterion.
   Notable: a trip or a stop on one, a destination outing, a
   celebration, a performance, a milestone - what albums in this
   library are made of ("Japan Trip"). Not notable: ordinary
   home routines, meals, practice sessions, errands, everyday
   kid moments, however many photos they produced ("kids ate
   dinner"). A tiny fragment of a bigger occasion is not itself
   notable.

Return JSON only:
{"registry_match": "<exact registry album name>" | null,
 "match_confidence": 0.0-1.0,
 "notability": "notable" | "routine",
 "notability_confidence": 0.0-1.0,
 "notability_reason": "<= 20 words",
 "proposed_name": "<album name WITHOUT the year prefix>",
 "name_basis": "signage" | "famous_landmark" | "generic",
 "generic_name": "<the same album named with NO specific place or occasion claims - generic activity words only; the fallback if the specific name cannot be evidenced>"}
proposed_name follows the family's style: short, plain
("Cherry Blossoms", "Monster Jam"), no filler words. A specific
place or occasion name is a factual claim: include one ONLY
when the photos themselves evidence it - readable signage
naming the place (basis "signage") or an unmistakable famous
landmark (basis "famous_landmark"). A generic lake, beach,
trail, or office scene does NOT identify a specific place or
occasion; for those use generic words (basis "generic").
Never coin insider terms or infer celebrations (a family
visiting an office is a visit, not an anniversary)."""


VERIFY_SYSTEM = """You verify one factual claim: that a photo
episode deserves the specific album name provided. Answer
evidenced=true ONLY if (a) readable text or signage IN the
images states the name (quote it in 'evidence'), or (b) the
images show the unmistakable famous landmark or venue itself
in frame. People at a generic lake, beach, office, or street
that could be anywhere do NOT evidence a specific name. An
inferred occasion (an anniversary, a celebration type) is
evidenced only by visible text stating it.
Return JSON only:
{"evidenced": true|false, "confidence": 0.0-1.0,
 "evidence": "<= 15 words, quoting the signage when relied on"}"""


# B46: the B44 S5a verify-selection policy lives in
# exemplars.py now (verify_exemplars), the one home for
# exemplar selection; this alias keeps the historical
# import path working.
_verify_exemplars = verify_exemplars


# ---- M2 (B46): gate-time episode brief aggregates --------
# The gate question used to carry only the write-once
# grouping brief (episode description + a few exemplar
# analyses). This block adds CODE-computed aggregates over
# the episode's CURRENT member labels at gate time: counts
# and spans no model wrote and nothing can hallucinate.
# Only fields actually present in the labels are aggregated
# - a mixed v1/v2 episode contributes per-field what exists
# and omits the rest (v1 prose is never coerced into v2
# fields). Deterministic: ranked lists sort by count desc,
# then value asc; the block is size-capped (see
# _AGGREGATE_BUDGET_CHARS) by shedding ranked tail entries.
# The block is payload material, so it re-keys gate
# questions prospectively; name-evidence rules and every
# outcome threshold are untouched.
_AGGREGATE_BUDGET_CHARS = 600
_AGG_TEXT_SAMPLES = 3
_AGG_TEXT_LEN = 60
_AGG_OBJECT_CAP = 5
_AGG_SCENE_CAP = 4


def _member_label(item) -> dict | None:
    raw = item["analysis_json"]
    if not raw:
        return None
    try:
        a = json.loads(raw)
    except ValueError:
        return None
    return a if isinstance(a, dict) else None


def _ranked(counts: dict, cap: int) -> list:
    return [[v, n] for v, n in sorted(
        counts.items(), key=lambda kv: (-kv[1], kv[0]))[:cap]]


def episode_aggregates(items: list) -> dict:
    """Aggregates over the member rows apply_gate already
    fetched (canonical order). See the M2 block comment."""
    agg: dict = {"member_count": len(items)}
    times = [i["capture_time"] for i in items if i["capture_time"]]
    if times:
        agg["span"] = [min(times), max(times)]
    texts: dict = {}
    objects: dict = {}
    scenes: dict = {}
    people: list = []
    for i in items:
        a = _member_label(i)
        if a is None:
            continue
        vt = a.get("visible_text")
        if isinstance(vt, str) and vt.strip():
            texts[vt.strip()] = texts.get(vt.strip(), 0) + 1
        objs = a.get("objects")
        if isinstance(objs, list):
            for o in objs:
                if isinstance(o, str) and o.strip():
                    objects[o.strip()] = objects.get(o.strip(), 0) + 1
        scene = a.get("scene")
        if isinstance(scene, dict):
            setting = scene.get("setting")
            if isinstance(setting, str) and setting.strip():
                scenes[setting.strip()] = scenes.get(setting.strip(), 0) + 1
        ppl = a.get("people")
        if isinstance(ppl, dict):
            c = ppl.get("count")
            if isinstance(c, bool):
                pass
            elif isinstance(c, int):
                people.append(c)
            elif isinstance(c, float) and c.is_integer():
                people.append(int(c))
    if texts:
        agg["visible_text_samples"] = [
            v[:_AGG_TEXT_LEN] for v, _n in _ranked(texts, _AGG_TEXT_SAMPLES)]
    if objects:
        agg["top_objects"] = _ranked(objects, _AGG_OBJECT_CAP)
    if scenes:
        agg["scene_settings"] = _ranked(scenes, _AGG_SCENE_CAP)
    if people:
        agg["people_count_range"] = [min(people), max(people)]
    # size discipline: shed ranked tail entries (objects,
    # then scenes, then text samples) until the block fits;
    # member_count / span / people range are never shed.
    while len(json.dumps(agg)) > _AGGREGATE_BUDGET_CHARS:
        if len(agg.get("top_objects", [])) > 1:
            agg["top_objects"].pop()
        elif len(agg.get("scene_settings", [])) > 1:
            agg["scene_settings"].pop()
        elif len(agg.get("visible_text_samples", [])) > 1:
            agg["visible_text_samples"].pop()
        else:
            # single entries still too long: hard-truncate
            # the sample strings rather than emit an
            # over-budget block
            agg["visible_text_samples"] = [
                s[:32] for s in agg.get("visible_text_samples", [])]
            agg["top_objects"] = [[v[:32], n]
                                  for v, n in agg.get("top_objects", [])]
            agg["scene_settings"] = [[v[:32], n] for v, n in
                                     agg.get("scene_settings", [])]
            break
    return agg


def verify_specific_name(cfg: Config, man: Manifest, client: GeminiClient,
                         ep, items: list, name: str) -> tuple[bool, str]:
    """Image-evidence check for a specific album name (B27).
    Any failure - API, parse, low confidence - returns NOT
    evidenced: a naming check must never kill a gate run,
    and unverified is always the safe answer."""
    from .gemini_client import coerce_float
    from .journal import call_extra, decision_key, image_extra, prompt_hash
    chosen = verify_exemplars(items)
    text = json.dumps({"claimed_name": name,
                       "episode_description": ep["description"]})
    # B32: a completed verification is journaled like every
    # other paid verdict; a FAILED one (API, parse) is not -
    # failure is not disproof, and journaling it as such
    # would make a transient outage a permanent verdict.
    # The failed call still resolves this gate safely to
    # "not evidenced", exactly as before.
    key = decision_key("name_verify", cfg.phase1_model_flash, VERIFY_SYSTEM,
                       {"text": text, "images": [i["md5"] for i in chosen]},
                       extra={**image_extra(cfg),
                              **call_extra(cfg, "judgment")})
    row = man.get_decision(key)
    if row is not None:
        data = json.loads(row["result_json"])
        ok = bool(data.get("evidenced")) and \
            coerce_float(data.get("confidence"), 0.0) >= 0.7
        return ok, str(data.get("evidence") or ""), False
    parts = [{"text": text}]
    for i in chosen:
        d = {"image_md5": i["md5"]}
        if cfg.media_res_exemplar:
            d["media_resolution"] = cfg.media_res_exemplar
        parts.append(d)
    from .usage import usage_context
    try:
        with usage_context(stage="name_verify",
                           subject=f"episode {ep['episode_id']}: {name}",
                           decision_key=key):
            res = with_retries(lambda: client.generate_json(
                cfg.phase1_model_flash, VERIFY_SYSTEM, parts,
                thinking_level=cfg.thinking_judgment,
                service_tier=cfg.service_tier))
    except (UnparseableOutput, EmptyResponse):
        # a semantic failure of THIS question: inconclusive,
        # resolved safely to "not evidenced" as designed -
        # but the caller records it as inconclusive, never
        # as a judged negative (B35 sweep, F-9 provenance)
        return False, "", True
    except Exception as e:
        # Two classes PROPAGATE instead of degrading the
        # name (B35 sweep): our own BudgetExceeded (the
        # spend cap is a control signal, not an API
        # failure), and fail-fast API errors - an integer
        # 4xx code other than 429, the codebase's duck-typed
        # convention (a 402's account is dead; converting
        # it into a permanently degraded album name would
        # commit an outage as if it were a judgment).
        # Everything else - exhausted transport retries,
        # arbitrary client errors - keeps the pinned
        # behavior: a failed verify is safe, not fatal, and
        # lands as inconclusive.
        code = getattr(e, "code", None)
        if isinstance(e, BudgetExceeded) or (
                isinstance(code, int) and 400 <= code < 500
                and code != 429):
            raise
        return False, "", True
    man.record_decision(key, "name_verify",
                        f"episode {ep['episode_id']}: {name}", res.data,
                        model=cfg.phase1_model_flash,
                        prompt_hash=prompt_hash(VERIFY_SYSTEM),
                        tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                        tokens_thoughts=res.thoughts)
    ok = bool(res.data.get("evidenced")) and \
        coerce_float(res.data.get("confidence"), 0.0) >= 0.7
    return ok, str(res.data.get("evidence") or ""), False


def apply_gate(man: Manifest, cfg: Config, client: GeminiClient,
               registry: list[dict], episode_id: int) -> str:
    ep = man.get_episode(episode_id)
    # canonical order (B32): the gate payload is journal key
    # material; an unordered item slice would hash the same
    # question differently run to run
    items = man.query("SELECT * FROM items WHERE episode_id = ? "
                      "ORDER BY capture_time, md5", (episode_id,))
    if not items:
        return "empty"
    years = {int(i["capture_time"][:4]) for i in items if i["capture_time"]}
    names = registry_names_for_years(registry, years)
    # B46: exemplar selection lives in exemplars.py - the
    # episode's CURRENT first-5 members with a non-empty
    # analysis (the historical truthiness filter and cap,
    # sourced from membership at call time).
    exemplar_text = exemplar_analyses(man, episode_id,
                                      GATE_ANALYSIS_CAP, nonempty=True)
    payload = {
        "episode_description": ep["description"],
        "span": [ep["span_start"], ep["span_end"]],
        "item_count": len(items),
        # M2 (B46): code-computed aggregates over the current
        # member labels - additional context only; no outcome
        # rule reads it. Re-keys gate questions prospectively.
        "episode_aggregates": episode_aggregates(items),
        "exemplar_analyses": exemplar_text,
        "registry_names_for_year": names}
    parts = [{"text": json.dumps(payload)}]
    from .journal import call_extra, decision_key, prompt_hash
    # The key covers the payload - which already contains
    # registry_names_for_year, the exact name list the model
    # sees. An earlier form also hashed the whole registry
    # FILE: bytes the model never sees (per-album counts,
    # other years' albums), so any registry re-harvest
    # re-keyed and re-bought every gate on a byte-identical
    # question. Measured bank exposure when removed: zero
    # gate rows in either manifest (production gates predate
    # the journal), so nothing banked was orphaned.
    gate_key = decision_key("gate", cfg.phase1_model_flash, GATE_SYSTEM,
                            payload, extra=call_extra(cfg, "judgment"))
    # B35: the journal is consulted FIRST, like every other
    # stage. Until now apply_gate always bought a fresh
    # verdict and recorded it only in the final transaction
    # (replace=True) - a crash between the paid call and the
    # commit lost the verdict and the next run bought it
    # again. Now a stored verdict replays through the same
    # deterministic outcome rules at zero cost.
    fresh = None
    row = man.get_decision(gate_key)
    if row is not None:
        stored = json.loads(row["result_json"])
        g = stored.get("verdict", stored)
    else:
        from .usage import usage_context
        with usage_context(stage="gate", subject=f"episode {episode_id}",
                           decision_key=gate_key):
            fresh = with_retries(lambda: client.generate_json(
                cfg.phase1_model_flash, GATE_SYSTEM, parts,
                thinking_level=cfg.thinking_judgment,
                service_tier=cfg.service_tier))
        g = fresh.data
        # recorded the moment it is bought, in its own
        # commit - outcome application below cannot lose it
        man.record_decision(
            gate_key, "gate", f"episode {episode_id}", {"verdict": g},
            model=cfg.phase1_model_flash,
            prompt_hash=prompt_hash(GATE_SYSTEM),
            tokens_in=fresh.tokens_in, tokens_out=fresh.tokens_out,
            tokens_thoughts=fresh.thoughts)

    from .gemini_client import coerce_float
    match = g.get("registry_match")
    conf = coerce_float(g.get("match_confidence"), 0.0)
    if match and is_bucket_name(match):
        match, conf = None, 0.0     # buckets are never matches, whatever the model says
    all_names = {r["name"] for r in registry if not is_bucket_name(r["name"])}
    # code-side name safety: exact registry hit the call missed,
    # or a near-dup of a registry name, resolves to the registry
    # name. SCOPED TO THE EPISODE'S OWN YEARS (F1): the year is
    # ~1 char in 20, so an unscoped near-dup check scores a
    # PRIOR year's same-name album at 0.91-0.95 - above the
    # override threshold - and files a recurring event (Cherry
    # Blossoms, Easter, Monster Jam) into last year's album.
    year_names = [n for n in names]

    def full_name(raw: str) -> str | None:
        """Sanitize a model-proposed name into 'YYYY <name>' form
        (F2 path safety, year-prefix handling, sentence-length
        rejection - a 'name' over 70 chars is a description the
        model returned instead of a name, seen live 2026-10-15)."""
        raw = (raw or "").strip()
        if not raw or "/" in raw or "\\" in raw or raw in (".", ".."):
            return None
        if not years:
            return None
        y = min(years)
        if raw.startswith(f"{y} "):
            raw = raw[len(str(y)) + 1:]
        full = f"{y} {raw}"
        return full if len(full) <= 70 else None

    proposed_full = full_name(g.get("proposed_name") or "")
    if proposed_full:
        for rn in year_names:
            if rn.lower() == proposed_full.lower():
                match, conf = rn, max(conf, 0.95)
            elif similar(rn, proposed_full) > 0.9:
                match, conf = rn, max(conf, 0.9)


    # Outcome rules (code, in order). There is NO item-count
    # criterion: notability is the model's judgment of the
    # occasion (Decision 5, superseded 2026-10-15 - "2026 kids
    # ate dinner" is not an album at any count; "2026 Japan
    # Trip" is one at 40 photos).
    notability = g.get("notability")
    nconf = coerce_float(g.get("notability_confidence"), 0.0)
    reason = g.get("notability_reason") or ""

    # B27 naming evidence policy: a specific name is a
    # factual claim about THIS episode. With a declared basis
    # (signage / famous landmark) it must VERIFY against the
    # exemplar images before it may be used; unverified, the
    # episode keeps its album under the generic fallback
    # name. (A name denylist was tried and rejected by the
    # user the same day: places are not wrong in themselves
    # and a blacklist does not scale - evidence is the fix.)
    from .naming import coerce_basis
    basis = coerce_basis(g.get("name_basis"))
    verify_record = None
    if proposed_full and basis != "generic":
        ok, _evidence, _inconclusive = verify_specific_name(
            cfg, man, client, ep, items, proposed_full)
        verify_record = {"claimed_name": proposed_full, "evidenced": ok,
                         "evidence": _evidence}
        if _inconclusive:
            verify_record["inconclusive"] = True
        if not ok:
            generic_full = full_name(g.get("generic_name") or "")
            reason = (f"{reason} [specific name '{proposed_full}' not "
                      f"evidenced in photos; "
                      f"{'generic name used' if generic_full else 'no generic fallback'}]").strip()
            proposed_full = generic_full
            basis = "generic"
    if match and match in all_names and conf >= cfg.gate_registry_floor:
        outcome = "album_registry"
        fields = dict(outcome="album_registry", album_name=match,
                      gate_character="registry", notability_reason=reason,
                      name_basis=basis)
    elif (notability == "notable" and nconf >= cfg.gate_notability_floor
            and len(items) >= cfg.gate_fragment_max_items
            and proposed_full):
        outcome = "album_new"
        fields = dict(outcome="album_new", album_name=proposed_full,
                      gate_character="notable", notability_reason=reason,
                      name_basis=basis)
    elif notability == "notable" and (nconf >= cfg.gate_borderline_floor
                                      or not proposed_full):
        # gray zone: month bucket, but flagged in the report as a
        # possible album for human promotion
        outcome = "month_bucket"
        fields = dict(outcome="month_bucket", gate_character="borderline",
                      notability_reason=reason, name_basis=basis)
    else:
        outcome = "month_bucket"
        fields = dict(outcome="month_bucket", gate_character="routine",
                      notability_reason=reason, name_basis=basis)
    # The outcome lands on the episode (the persisted skip
    # mechanism, as before); a fresh verdict's journal row
    # is then enriched in the same transaction with the
    # outcome and name-verification provenance (B35 - the
    # row was already written when the verdict was bought,
    # so this fold-in is bookkeeping, never a second call).
    with man.transaction():
        man.update_episode(episode_id, **fields)
        if fresh is not None:
            man.update_decision_result(
                gate_key, {"verdict": g, "outcome": outcome,
                           "name_verification": verify_record})
    return outcome
