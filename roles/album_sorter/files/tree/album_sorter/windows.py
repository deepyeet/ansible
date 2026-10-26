"""Phase 2 step 1 - window assembly (spec Section 9): contact
sheets rendered with Pillow, per-item records, and the scoped
episode-ledger slice (time-adjacent episodes with exemplar
mini-sheets + the running summary)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .manifest import Manifest

CELL = 512
LABEL_H = 28


def render_sheet(thumbs: list[Path], labels: list[str], grid: int, out: Path) -> Path:
    """grid x grid contact sheet; each cell labeled index + time."""
    from PIL import Image, ImageDraw
    per = grid * grid
    thumbs, labels = thumbs[:per], labels[:per]
    sheet = Image.new("RGB", (grid * CELL, grid * (CELL + LABEL_H)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, (tp, lab) in enumerate(zip(thumbs, labels)):
        x, y = (i % grid) * CELL, (i // grid) * (CELL + LABEL_H)
        try:
            with Image.open(tp) as im:
                im = im.convert("RGB")
                im.thumbnail((CELL, CELL))
                sheet.paste(im, (x, y + LABEL_H))
        except Exception:
            pass
        draw.rectangle([x, y, x + CELL, y + LABEL_H], fill=(30, 30, 30))
        draw.text((x + 8, y + 6), lab, fill=(255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=80)
    return out


def ledger_slice(man: Manifest, cfg: Config, start: datetime, end: datetime) -> list[dict]:
    """Episodes intersecting the padded window span, with exemplar
    md5s for mini-sheet rendering. (Similarity-based selection of
    older episodes is deferred with embeddings, spec Section 20.)"""
    pad = timedelta(days=cfg.ledger_time_pad_days)
    out = []
    for ep in man.all_episodes():
        if not ep["span_start"]:
            continue
        s, e = datetime.fromisoformat(ep["span_start"]), datetime.fromisoformat(ep["span_end"])
        if s <= end + pad and e >= start - pad:
            out.append({"episode_id": ep["episode_id"],
                        "description": ep["description"],
                        "span": [ep["span_start"], ep["span_end"]],
                        "item_count": ep["item_count"],
                        "exemplars": json.loads(ep["exemplars"] or "[]")})
    return out


# B35 (payload v2): the window record carries the analysis
# fields grouping can act on - place/event continuity and
# the scene description - not the full phase-1 record. The
# people prose (~27 of ~150 tokens per item, the single
# largest field after the description) is reduced to its
# count; guess fields ride along only when non-empty.
# B41 (payload v3): the v2-schema enrichment fields ride
# along too (colors, scene gist, distinctive features) -
# without this projection extension grouping would never
# see them, however rich phase 1 gets.
_ANALYSIS_KEEP = ("description", "place_description",
                  "event_description", "place_name_guess",
                  "wider_location_guess", "visible_text",
                  "objects", "scene",
                  "distinctive_features")


def _analysis_projection(a: dict) -> dict:
    out = {k: a[k] for k in _ANALYSIS_KEEP
           if a.get(k) not in (None, "", [])}
    people = a.get("people")
    if isinstance(people, dict) and people.get("count"):
        out["people_count"] = people["count"]
    return out


def item_record(row, window_index: int) -> dict:
    rec = {"index": window_index, "file": row["done_name"],
           "time": row["capture_time"], "device": row["device"]}
    if row["gps_lat"] is not None:
        rec["gps"] = [round(row["gps_lat"], 4) if row["gps_lat"] is not None else None,
                      round(row["gps_lon"], 4) if row["gps_lon"] is not None else None]
    if row["analysis_json"]:
        rec["analysis"] = _analysis_projection(json.loads(row["analysis_json"]))
    return rec


# B44: a long visible_text repeated across one window's
# records is a RECITATION-filter hazard. V2 phase-1 labels
# quote signage verbatim, so every photo of the same event
# banner carries the same normalized 100+ char string;
# observed live 2026-10-23, grouping window 0 over v2
# records emptied (finish_reason RECITATION) on every
# attempt while the v1 control probe completed. The
# SAMENESS is the signal grouping needs, not the
# repetition: the string stays verbatim on the first
# record and later repeats become an identity marker
# naming that record's file. Presentation rule over the
# rendered records only - stored labels never change.
_REPEAT_TEXT_MIN = 40


def dedupe_window_visible_text(records: list[dict]) -> list[dict]:
    """First occurrence of a >= 40-char visible_text
    (compared after strip + whitespace collapse) stays
    verbatim; subsequent repeats in the same window are
    replaced with "[same text as <first record's file>]".
    Short repeats and unrepeated text are untouched.
    Deterministic in window record order."""
    seen: dict[str, str] = {}
    out = []
    for rec in records:
        a = rec.get("analysis")
        text = a.get("visible_text") if isinstance(a, dict) else None
        if isinstance(text, str):
            norm = " ".join(text.split())
            if len(norm) >= _REPEAT_TEXT_MIN:
                if norm in seen:
                    rec = {**rec, "analysis": {
                        **a, "visible_text":
                            f"[same text as {seen[norm]}]"}}
                else:
                    seen[norm] = rec["file"]
        out.append(rec)
    return out
