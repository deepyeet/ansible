"""Decision-journal key material (backlog B32).

Resume support rests on one idea: a paid model verdict is a
function of the exact question asked, so the journal key is a
hash of that question - the canonical payload, the model
string, and a hash of the system prompt - never of bare row
ids. Episode ids are unstable (rows are deleted by grouping
hygiene, retired by merges, mutated in place); content is
not. When any part of the question changes - an episode's
description, span, or membership, an item's corrected time
or analysis, the registry contents behind a gate payload,
the prompt text, the model - the key changes and the
question is asked again. When nothing changed, the stored
verdict is replayed and the CURRENT code thresholds are
re-applied to it, so a rule change never re-buys a call.

Payload builders must be canonical (stable ordering) or the
hit rate silently degrades; the failure mode is safe (a
miss re-pays, it never wrongly reuses).

Two boundary clarifications, so the invariant is exactly
what the code enforces - no more:

- The hash covers the payload AS SENT. Where a payload
  itself embeds an id (the merge/rollup episode briefs
  carry episode_id), the id rides along in the key. That
  is harmless inside one manifest - ids are AUTOINCREMENT,
  never reused, and stable across the resumes that matter -
  but it means the honest rule is "no SEMANTIC key is a
  bare id", not "no id ever appears in key material".
- Transport/render constants that live in code (the
  client's temperature, JPEG qualities, contact-sheet cell
  size) are part of the code version, not the key - the
  same status the reconcile/threshold logic has. Prompts
  are hashed because they are long, frequently edited
  strings whose changes are otherwise invisible.

B35 amendment - identity keys with re-validated context:
three stages (linkage, merge, rollup) no longer hash
their full payload. Their payloads embed DERIVED state
(an episode's span and item count, recomputed whenever
membership changes), and hashing derived state orphaned
~14% of verdicts per growth round - a rejection was
re-bought not because the judged content changed but
because a summary of it did. Those keys now cover the
question's identity (the items/episodes, their
descriptions and exemplar content, the model, the
prompt, the image and call parameters). The prompt sent
is unchanged - the model still sees the span - and the
judged derived state is stored in the row's context_json
and re-validated deterministically at replay (the
stage's own drift rules in linkage.py / merge.py /
rollup.py). A row that fails re-validation is re-asked
once and self-heals via replace. Pre-B35 rows remain
reachable through their historical full-payload keys,
which hit only when the payload is byte-identical to
the judged one, so nothing banked is orphaned by the
change. Call-configuration that changes what the model
sees or how it reasons (thinking level, media
resolution) joins the key material when set, via
call_extra; service tier does not (it is transport
priority, like Batch-vs-sync).
"""
from __future__ import annotations

import hashlib
import json


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_hash(system_prompt: str) -> str:
    return sha256_text(system_prompt)


def decision_key(stage: str, model: str, system_prompt: str,
                 payload, extra: dict | None = None) -> str:
    """Hash of one exact model question for `stage`."""
    material = {"stage": stage, "model": model,
                "prompt": prompt_hash(system_prompt),
                "payload": payload, "extra": extra or {}}
    return sha256_text(canonical_json(material))


def image_extra(cfg) -> dict:
    """Derived-image parameters: image-bearing payloads are
    built from assets generated with these, so a change here
    changes the question even though the item md5s do not."""
    return {"analysis_px": cfg.analysis_px, "thumb_px": cfg.thumb_px,
            "poster_fraction": cfg.poster_fraction}


def call_extra(cfg, kind: str) -> dict:
    """B35 call-configuration key material. Thinking level
    and per-part media resolution change what the model
    effectively sees and how it reasons, so once set they
    are part of the question and join the key; at their
    defaults this returns {} and historical keys are
    untouched, so banked verdicts survive the plumbing.
    Service tier is deliberately absent: it is transport
    priority (the same status Batch-vs-sync has), not part
    of the question. kind: "judgment" | "grouping"."""
    out: dict = {}
    thinking = (cfg.thinking_grouping if kind == "grouping"
                else cfg.thinking_judgment)
    if thinking:
        out["thinking"] = thinking
    if kind == "grouping":
        if cfg.media_res_sheet:
            out["media_res_sheet"] = cfg.media_res_sheet
    else:
        if cfg.media_res_item:
            out["media_res_item"] = cfg.media_res_item
        if cfg.media_res_exemplar:
            out["media_res_exemplar"] = cfg.media_res_exemplar
    return out
