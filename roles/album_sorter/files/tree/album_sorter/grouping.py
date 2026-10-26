"""Phase 2 steps 1-2 driver: sequential windows over the batch's
PHOTO items. Each window call gets sheets + item records + the
ledger slice + the running summary; output is strictly validated
(retry once with errors, never silently repaired); votes from
overlapping windows are reconciled by centrality (spec Sec. 10).

A window whose call fails is isolated (B43): the failure is
recorded in the run stats, nothing is journaled for that window,
its items stay unassigned, and the run continues - one window's
failure no longer kills the batch, and a later run re-asks.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import grouping_logic as gl
from .config import Config
from .gemini_client import BudgetExceeded, GeminiClient, with_retries
from .manifest import Manifest
from .windows import (dedupe_window_visible_text, item_record,
                      ledger_slice, render_sheet)

GROUPING_SYSTEM = """You group a window of time-ordered family photos into EPISODES.
An episode is one continuous occasion: a time span at a place - a trip,
an outing, an event, an ordinary stretch at home.

You receive: a running summary, the episode ledger so far (with
exemplar sheets), this window's contact sheets, and per-item records.

Return JSON only:
{"assignments": {"<window index>": "<ledger episode_id>" | "<temp_id>"},
 "new_episodes": [{"temp_id": "N1", "description": "...", "exemplars": [indices]}],
 "unassigned": [indices],
 "summary_update": "<= 150 words"}

Rules:
- Every window index appears exactly once (assignments or unassigned).
- Assignment values are episode references only: an exact ledger
  episode_id from the ledger above, or a temp_id you declare in
  new_episodes. "unassigned" is not an episode and is never an
  assignment value: to leave an item loose, omit it from
  assignments and list its index in "unassigned" instead.
- Prefer extending a ledger episode over creating a near-duplicate.
- Episodes are contiguous in time unless the ledger justifies a return
  to the same place/thread.
- Descriptions are free text: place (kind + specifics + name if
  inferable), event if any, in your own words.
"""


# Window-payload construction version. Bumped whenever the
# payload's shape changes (v2, B35: item records carry a
# slimmed analysis projection; v3, B41: the projection
# carries the enriched phase-1 fields; v4, B41 audit:
# dominant_colors dropped from the projection after its
# field-accuracy audit failed; v5, B44: a window's repeated
# long visible_text is deduped to an identity marker after
# its first record - the RECITATION hazard), because the
# fingerprint must treat a different payload as a
# different question.
PAYLOAD_VERSION = 5


def parse_grouping(data: dict) -> gl.GroupingOutput:
    # Non-integer assignment keys / unassigned entries are
    # VALIDATION failures (retry-with-errors), not parse crashes
    # (F9): keep them as sentinel strings the validator rejects,
    # so the model sees the error and the retry contract holds.
    assignments = {}
    for k, v in (data.get("assignments") or {}).items():
        try:
            assignments[int(k)] = str(v)
        except (TypeError, ValueError):
            assignments[f"__bad_key__{k}"] = str(v)
    return gl.GroupingOutput(
        assignments=assignments,
        new_episodes=data.get("new_episodes") or [],
        unassigned=data.get("unassigned") or [],
        summary_update=data.get("summary_update") or "")


def call_window(client: GeminiClient, parts: list[dict], window_len: int,
                known_episode_ids: set[str],
                model: str = "gemini-3.8-flash",
                thinking_level: str | None = None,
                service_tier: str | None = None) -> tuple[gl.GroupingOutput, bool]:
    """One grouping call for a window, strictly validated. On
    validation errors - or on output that is not valid JSON at
    all - retry exactly once with the errors appended to the
    payload (spec Sec. 10); never silently repair the output.
    Returns (output, retried). Raises ValueError with the final
    error list when the retry also fails."""
    from .gemini_client import UnparseableOutput

    def attempt(payload):
        return parse_grouping(with_retries(
            lambda: client.generate_json(model, GROUPING_SYSTEM, payload,
                                         max_output_tokens=32768,
                                         thinking_level=thinking_level,
                                         service_tier=service_tier)).data)

    try:
        out = attempt(parts)
        errors = gl.validate_grouping(out, window_len, known_episode_ids)
    except UnparseableOutput as e:
        out, errors = None, [f"output was not valid JSON: {e}"]
    if not errors:
        return out, False
    retry_parts = parts + [{"text": json.dumps({
        "validation_errors": errors,
        "instruction": "Your previous output failed strict validation. "
                       "Return a corrected JSON output only: every window "
                       "index accounted for exactly once, references only "
                       "to the listed episodes or your new temp_ids."})}]
    try:
        out = attempt(retry_parts)
    except UnparseableOutput as e:
        raise ValueError(f"grouping output unparseable after one retry: {e}") from e
    errors = gl.validate_grouping(out, window_len, known_episode_ids)
    if errors:
        raise ValueError(f"grouping output failed validation after one retry: {errors}")
    return out, True


def photo_stream(man: Manifest) -> list:
    return man.query(
        "SELECT * FROM items WHERE category = 'PHOTO' AND status IN ('analyzed','embedded') "
        "ORDER BY capture_time, done_name")


def stream_fingerprint(cfg: Config, man: Manifest) -> str:
    """Fingerprint of everything grouping's result depends on
    (B32): the full PHOTO population - every per-item field
    the window payloads carry (md5, done_name, corrected
    capture time, device, GPS, analysis) - plus the
    grouping config, model, prompt, and image parameters.
    Computed over ALL photo items, not just the unassigned
    stream: the stream shrinks precisely because grouping
    succeeded, so a stream-scoped hash would differ after
    every success and the checkpoint could never match.
    Downstream stages (merge, linkage, gates, placement)
    never touch the fingerprinted fields, so their work
    does not invalidate it; a new or re-analyzed item, a
    clock correction, or a metadata correction does."""
    from .journal import call_extra, canonical_json, prompt_hash, sha256_text
    rows = man.query(
        "SELECT md5, done_name, capture_time, device, gps_lat, gps_lon, "
        "analysis_json FROM items WHERE category = 'PHOTO' ORDER BY md5")
    material = {
        "items": [[r["md5"], r["done_name"], r["capture_time"],
                   r["device"], r["gps_lat"], r["gps_lon"],
                   sha256_text(r["analysis_json"] or "")] for r in rows],
        "config": {"window_size": cfg.window_size,
                   "window_stride": cfg.window_stride,
                   "sheet_grid": cfg.sheet_grid,
                   "ledger_time_pad_days": cfg.ledger_time_pad_days,
                   "analysis_px": cfg.analysis_px,
                   "thumb_px": cfg.thumb_px,
                   "poster_fraction": cfg.poster_fraction,
                   "payload_version": PAYLOAD_VERSION,
                   **call_extra(cfg, "grouping")},
        "model": cfg.grouping_model,
        "prompt": prompt_hash(GROUPING_SYSTEM)}
    return sha256_text(canonical_json(material))


def run_grouping(cfg: Config, man: Manifest, client: GeminiClient,
                 work_dir: Path) -> dict:
    rows = photo_stream(man)
    stats = {"windows": 0, "items": len(rows), "failed_windows": 0, "retried_windows": 0}
    if not rows:
        return stats
    # crash hygiene: a grouping run that died mid-way leaves
    # pending episodes with provisional spans but NO items
    # (assignments land in one final pass). Those orphans are
    # not real episodes - remove them so they cannot pollute
    # this run's ledger or the gates. A pending episode with
    # items is legitimate and untouched.
    # B35: episodes created by journaled windows are PROTECTED
    # - a resume replays those windows and adopts the exact
    # episodes; deleting them would force a re-ask of work
    # already paid for.
    # F-5 scope (B35 review): protection covers only the
    # LATEST, incomplete attempt - window rows written after
    # the last completed grouping checkpoint. Rows from
    # completed cycles confer no protection: their item-less
    # episodes are phantoms (a completed run's final pass
    # either placed items in them or they were never real),
    # and leaving them immortal would keep them polluting
    # ledgers and linkage nomination forever.
    _ckpt = man.get_checkpoint("grouping")
    _since = _ckpt["completed"] if _ckpt is not None else None
    protected: set[int] = set()
    for r in man.query(
            "SELECT result_json FROM decisions WHERE stage = 'grouping_window'"
            + (" AND created > ?" if _since else ""),
            (_since,) if _since else ()):
        try:
            protected.update(
                int(v) for v in json.loads(r["result_json"]).get("created", {}).values())
        except (ValueError, TypeError, AttributeError):
            continue
    if protected:
        ph = ",".join("?" * len(protected))
        man.execute(
            "DELETE FROM episodes WHERE outcome = 'pending' AND episode_id NOT IN "
            "(SELECT DISTINCT episode_id FROM items WHERE episode_id IS NOT NULL) "
            f"AND episode_id NOT IN ({ph})",
            tuple(sorted(protected)))
    else:
        man.execute(
            "DELETE FROM episodes WHERE outcome = 'pending' AND episode_id NOT IN "
            "(SELECT DISTINCT episode_id FROM items WHERE episode_id IS NOT NULL)")
    # B32 checkpoint: a completed grouping over an unchanged
    # input fingerprint is not re-bought on a resume. This is
    # what distinguishes "continue the same batch" (skip)
    # from "a new cycle" (any input change re-groups the
    # unassigned stream against the ledger, as designed).
    fingerprint = stream_fingerprint(cfg, man)
    checkpoint = man.get_checkpoint("grouping")
    if checkpoint is not None and checkpoint["fingerprint"] == fingerprint:
        stats["skipped"] = 1
        run_id = man.start_run("grouping", {"window": cfg.window_size,
                                            "stride": cfg.window_stride,
                                            "skipped": "checkpoint"})
        man.finish_run(run_id, str(stats))
        return stats
    run_id = man.start_run("grouping", {"window": cfg.window_size, "stride": cfg.window_stride})
    votes: dict[int, list[gl.Vote]] = {}
    summary = ""
    temp_to_ledger: dict[str, int] = {}
    ranges = gl.windows(len(rows), cfg.window_size, cfg.window_stride)
    # B35 replay determinism: a journaled window's payload
    # was built when episodes created by it (and by later
    # windows of the dead run) did not yet exist. On a
    # resume those episodes stand in the manifest as
    # item-less pending orphans, and ledger_slice would
    # hand them to windows that never saw them - changing
    # every rebuilt payload and defeating the journal.
    # Rule: an item-less episode created by a journaled
    # window whose range starts at or after the current
    # window's start is excluded from the current ledger.
    # Episodes created by EARLIER windows are already
    # re-materialized by their replays when later windows
    # build (and must stay visible - the B20 seam), and
    # episodes holding items are real ledger context from
    # completed cycles, never excluded.
    journal_created: dict[int, int] = {}    # episode_id -> range start
    for r in man.query(
            "SELECT result_json FROM decisions WHERE stage = 'grouping_window'"):
        try:
            rec = json.loads(r["result_json"])
            for lid in rec.get("created", {}).values():
                journal_created[int(lid)] = rec["stream_range"][0]
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    with_items = {r["episode_id"] for r in man.query(
        "SELECT DISTINCT episode_id FROM items "
        "WHERE episode_id IS NOT NULL")}

    def apply_window(out, batch, s, wno, created_hint=None):
        """Apply one window's output to the run state -
        register its new episodes, set provisional spans,
        extend temp_to_ledger, accumulate votes. The ONE
        apply path for live and replayed windows (B35), so
        a replay reconstructs byte-identical state. With
        created_hint (a journaled window's recorded map),
        episodes are re-materialized under their recorded
        ids or adopted where they still stand; without it,
        fresh ids are minted as before. Returns
        (created, provisional) for the journal record."""
        created: dict[str, int] = {}
        provisional: dict[str, dict] = {}
        for ep in out.new_episodes:
            member_md5s = [batch[i]["md5"] for i in batch_indices_of(out, ep["temp_id"])]
            exemplars = [batch[i]["md5"] for i in ep.get("exemplars") or []]
            chosen = exemplars or member_md5s[:3]
            hint = (created_hint or {}).get(str(ep["temp_id"]))
            if hint is not None:
                lid = int(hint)
                if man.get_episode(lid) is None:
                    man.execute(
                        "INSERT INTO episodes (episode_id, description,"
                        " exemplars) VALUES (?, ?, ?)",
                        (lid, ep["description"], json.dumps(chosen)))
            else:
                lid = man.create_episode(ep["description"], chosen)
            created[str(ep["temp_id"])] = lid
            temp_to_ledger[str(ep["temp_id"])] = lid
            # B20: give the new episode a provisional span from
            # this window's assignments NOW, so the next window's
            # ledger slice can see it - without this, an episode
            # created by window N is invisible to window N+1 and
            # the model structurally cannot extend it (the seam
            # splits the merge pass then has to heal). The final
            # pass recomputes exact spans from the reconciled
            # assignments.
            member_idx = batch_indices_of(out, ep["temp_id"])
            if member_idx:
                times = [batch[i]["capture_time"] for i in member_idx]
                man.update_episode(lid, span_start=min(times),
                                   span_end=max(times),
                                   item_count=len(member_idx))
                provisional[str(lid)] = {
                    "span_start": min(times), "span_end": max(times),
                    "item_count": len(member_idx)}
        for idx, ref in out.assignments.items():
            lid_ref = str(temp_to_ledger.get(ref, ref))
            votes.setdefault(s + idx, []).append(
                gl.Vote(lid_ref, gl.centrality(idx, len(batch)), wno))
        return created, provisional

    for wno, (s, e) in enumerate(ranges):
        batch = rows[s:e]
        start = datetime.fromisoformat(batch[0]["capture_time"])
        end = datetime.fromisoformat(batch[-1]["capture_time"])
        ledger = ledger_slice(man, cfg, start, end)
        if journal_created:
            ledger = [ep for ep in ledger
                      if not (ep["episode_id"] in journal_created
                              and journal_created[ep["episode_id"]] >= s
                              and ep["episode_id"] not in with_items)]
        # validation is scoped to what the model was actually
        # shown in THIS window (spec Sec. 10, review F18): the
        # slice's episodes plus this run's in-flight episodes
        known_ids = {str(x["episode_id"]) for x in ledger} | {
            str(v) for v in temp_to_ledger.values()}
        # the text ledger omits exemplar md5s (B35 sweep):
        # the model references episodes by id only - the
        # validator rejects anything else - and the exemplar
        # images arrive as labeled mini-sheet parts. The md5
        # strings were ~500-900 dead tokens per window. The
        # full slice (with md5s) still drives sheet rendering
        # below; only the text projection changes.
        text_ledger = [{k: v for k, v in ep.items() if k != "exemplars"}
                       for ep in ledger]
        text_part = {"text": json.dumps({
            "summary": summary, "ledger": text_ledger,
            "records": dedupe_window_visible_text(
                [item_record(r, i) for i, r in enumerate(batch)])})}
        # B35: a journaled window whose rebuilt payload still
        # hashes to its key replays at zero cost - sheets are
        # not even rendered. A miss (or a failed replay
        # precondition) falls through to the live call below;
        # there is no coded suffix invalidation - if the
        # re-asked window leaves the state chain unchanged,
        # the next window's payload still matches its key.
        key = _window_key(cfg, text_part, batch)
        row = man.get_decision(key)
        record = None
        if row is not None:
            try:
                record = json.loads(row["result_json"])
            except ValueError:
                record = None
        if record is not None and _replayable(man, record, s, e):
            out = _output_from_record(record)
            with man.transaction():
                apply_window(out, batch, s, wno,
                             created_hint=record["created"])
            summary = out.summary_update or summary
            stats["replayed_windows"] = stats.get("replayed_windows", 0) + 1
            stats["windows"] += 1
            continue
        thumbs = [cfg.assets_dir / r["md5"][:2] / f"{r['md5']}_t.jpg" for r in batch]
        labels = [f"#{i} {r['capture_time'][5:16]}" for i, r in enumerate(batch)]
        parts = [text_part]
        # ledger exemplar mini-sheets: the model sees the earlier
        # places again, not just their descriptions
        for ep in ledger:
            ex = (ep.get("exemplars") or [])[:9]
            if not ex:
                continue
            ex_thumbs = [cfg.assets_dir / m[:2] / f"{m}_t.jpg" for m in ex]
            mini = render_sheet(ex_thumbs, [f"E{ep['episode_id']}"] * len(ex), 3,
                                work_dir / f"exemplar_ep{ep['episode_id']}.jpg")
            parts.append({"text": f"Exemplar sheet for ledger episode {ep['episode_id']}:"})
            _sheet = {"image_path": str(mini)}
            if cfg.media_res_sheet:
                _sheet["media_resolution"] = cfg.media_res_sheet
            parts.append(_sheet)
        per = cfg.sheet_grid * cfg.sheet_grid
        for k in range(0, len(batch), per):
            sheet = render_sheet(
                thumbs[k:k + per], labels[k:k + per], cfg.sheet_grid,
                work_dir / f"sheet_w{wno}_{k // per}.jpg")
            parts.append({"text": f"Window contact sheet, items {k}-{min(k + per, len(batch)) - 1}:"})
            _sheet = {"image_path": str(sheet)}
            if cfg.media_res_sheet:
                _sheet["media_resolution"] = cfg.media_res_sheet
            parts.append(_sheet)
        from .usage import usage_context
        try:
            with usage_context(stage="grouping", subject=f"window {wno}",
                               decision_key=key):
                out, retried = call_window(client, parts, len(batch),
                                           known_ids | set(temp_to_ledger),
                                           cfg.grouping_model,
                                           thinking_level=cfg.thinking_grouping,
                                           service_tier=cfg.service_tier)
        except BudgetExceeded:
            raise   # the run's own spend cap stops the RUN, not
                    # one window - linkage's F-3 keeps the same line
        except Exception as err:
            # Per-window isolation (B43), mirroring linkage's
            # F-3: one window's failed call is that window's
            # failure, not the run's. The call surface raises
            # its failures in several unrelated shapes -
            # EmptyResponse (a RECITATION-filter empty, observed
            # live 2026-10-23; with_retries never retries it),
            # ValueError (validation failure, or UnparseableOutput
            # surviving call_window's retry-with-errors), and
            # transport errors that survived with_retries (its
            # exhaustion RuntimeError, or a raw fail-fast SDK
            # error) - so the catch is deliberately broad. The
            # billed call is already ledgered by the recording
            # client; NOTHING is journaled for the window, so
            # its items stay unassigned and a later run re-asks
            # them, while windows before and after proceed and
            # bank normally. The failure lands in the run stats,
            # and the completion checkpoint is withheld below so
            # the next run re-enters instead of skipping. (The
            # exception is bound as `err`, not `e`: `except ...
            # as e` would shadow this window's range end.)
            stats["failed_windows"] += 1
            stats.setdefault("windows_failed", []).append({
                "window": wno, "range": [s, e],
                "first_md5": batch[0]["md5"],
                "error": type(err).__name__, "message": str(err)})
            continue
        if retried:
            stats["retried_windows"] += 1
        # the verdict, its episode creations, and its
        # provisional spans commit together or not at all
        summary_after = out.summary_update or summary
        from .journal import prompt_hash
        with man.transaction():
            created, provisional = apply_window(out, batch, s, wno)
            man.record_decision(
                key, "grouping_window", f"window {wno} [{s}:{e}]",
                _window_record(out, created, provisional, s, e, summary_after),
                model=cfg.grouping_model,
                prompt_hash=prompt_hash(GROUPING_SYSTEM))
        summary = summary_after
        stats["windows"] += 1
    final = gl.reconcile(votes)
    # B32: the final pass, the span recomputes, and the
    # completion checkpoint commit as ONE unit. A crash
    # mid-pass previously left a partially-grouped manifest
    # that the next run streamed differently (the B10 drill
    # only covered a crash between windows); now it leaves
    # either everything or nothing.
    with man.transaction():
        for item_idx, ref in final.items():
            man.update_item(rows[item_idx]["md5"], episode_id=int(ref),
                            assign_method="window", status="grouped")
        for lid in {int(r) for r in final.values()}:
            man.recompute_episode_span(lid)
        # B43: the checkpoint asserts grouping COMPLETED over
        # this fingerprint. A run with failed windows banks
        # its successful windows' assignments but does not set
        # it - otherwise the next run would skip the stage
        # outright and the failed windows' items would never
        # be re-asked.
        if not stats["failed_windows"]:
            man.set_checkpoint("grouping", fingerprint, stats)
        man.finish_run(run_id, str(stats))
    return stats


def batch_indices_of(out: gl.GroupingOutput, temp_id: str) -> list[int]:
    return [i for i, ref in out.assignments.items() if str(ref) == str(temp_id)]


# ---- per-window journal (B35) ----------------------------------------------
# Until B35 a death mid-grouping re-paid every window of the
# stage: only the stage-level checkpoint persisted, at the
# end. Now each completed window is journaled in the same
# transaction as the episodes it creates, and a resume
# replays those windows through the same apply path -
# the stage's death exposure is at most one in-flight
# window. Design: cost-overhaul/state-design.md section 1.

def _window_key(cfg: Config, text_part: dict, batch) -> str:
    """Key for one window's question: the payload as sent
    (parsed, so dict order is normalized; list order -
    window order - stays significant), the slice's item
    md5s (the sheets' identity; records carry no md5), and
    the image/call parameters."""
    from .journal import call_extra, decision_key, image_extra
    material = {"payload": json.loads(text_part["text"]),
                "window_md5s": [r["md5"] for r in batch],
                "sheet_grid": cfg.sheet_grid}
    return decision_key("grouping_window", cfg.grouping_model,
                        GROUPING_SYSTEM, material,
                        extra={**image_extra(cfg),
                               **call_extra(cfg, "grouping")})


def _window_record(out: gl.GroupingOutput, created: dict,
                   provisional: dict, s: int, e: int,
                   summary_after: str) -> dict:
    return {"v": 1, "stream_range": [s, e],
            "output": {
                "assignments": {str(k): v for k, v in out.assignments.items()},
                "new_episodes": out.new_episodes,
                "unassigned": list(out.unassigned),
                "summary_update": out.summary_update},
            "created": created, "provisional": provisional,
            "summary_after": summary_after}


def _output_from_record(record: dict) -> gl.GroupingOutput:
    o = record["output"]
    return gl.GroupingOutput(
        assignments={int(k): v for k, v in o["assignments"].items()},
        new_episodes=o["new_episodes"],
        unassigned=list(o["unassigned"]),
        summary_update=o["summary_update"])


def _replayable(man: Manifest, record: dict, s: int, e: int) -> bool:
    """Preconditions for replaying a journaled window: the
    recorded range is this window's, and every episode the
    window created is either absent (re-materialize with
    its recorded id - AUTOINCREMENT ids are never reused,
    so the id is still reserved for it) or present,
    pending, and carrying the recorded description. A
    created episode that was since merged, gated, or
    edited is a hard miss: the window re-asks rather than
    resurrecting state downstream stages have moved past."""
    if record.get("stream_range") != [s, e]:
        return False
    descriptions = {str(ep["temp_id"]): ep["description"]
                    for ep in record["output"]["new_episodes"]}
    for temp_id, lid in record.get("created", {}).items():
        existing = man.get_episode(int(lid))
        if existing is None:
            continue
        if existing["outcome"] != "pending":
            return False
        if existing["description"] != descriptions.get(str(temp_id)):
            return False
    return True
