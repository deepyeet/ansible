#!/usr/bin/env python3
"""B41 Phase D step 2a: re-describe the validate_linkage
sample items with the ENRICHED (v2) phase-1 prompt into a
scratch manifest copy, and measure what the enrichment
costs and yields (token stats, schema-valid rate,
label-missing rate vs the banked v1 analyses).

Discipline (same as every harness in this project):
- The SOURCE copy is only read; the enriched copy is a
  fresh file copy of it. Live manifests are never touched
  (the enriched copy path is refused if it names one).
- The sample is the validate_linkage VERIFIED sample
  (key-mode verification against the source copy), so the
  items re-described are exactly the items the linkage
  sample's questions are about.
- Calls mirror phase1._classify_one's construction
  (same meta block, same parts, apply_guards, the F5
  label salvage) but under an experiment usage stage, so
  the central ledger attributes this spend to Phase D.
- Results append to a JSONL the moment they land; a rerun
  re-asks only items still missing or errored.

Run on the container from ~/album_sorter_dev:
  ~/album-venv/bin/python exp_phase1_v2.py \
      --source-copy exp/recat-copy.sqlite3 \
      --out-copy exp/recat-enriched.sqlite3 \
      --work-dir /mnt/pixel/.sorter-work-recat \
      --assets-dir /mnt/pixel/.sorter-work-recat/assets \
      --ground-truth exp/ground-truth-recat.json \
      --results exp/phase1-v2-results.jsonl [--tier flex] [--stub]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from album_sorter import phase1
from album_sorter.config import Config
from album_sorter.manifest import Manifest
from album_sorter.metadata import soft_hints

LIVE = {"/mnt/pixel/.sorter-work-recat/manifest.sqlite3",
        "/mnt/pixel/.sorter-work/manifest.sqlite3"}

STUB_LABEL = {
    "description": "stub photo at a lakeside.",
    "objects": ["stub"],
    "people": {"count": 0, "description": ""},
    "place_description": "stub shore",
    "place_name_guess": None, "wider_location_guess": None,
    "event_description": None, "visible_text": None,
    "dominant_colors": ["blue", "green"],
    "scene": {"setting": "outdoor", "environment": "lakeside",
              "lighting": "daylight"},
    "distinctive_features": ["stub feature"],
}


def verified_sample_items(cfg, man, raw, gt_linkage) -> list[str]:
    """Distinct item md5s of the key-verified linkage
    sample, in sample order (validate_linkage's own
    verification, unmodified)."""
    import validate_linkage as vl
    sample = vl.build_sample(gt_linkage)
    md5s, seen = [], set()
    for q in sample:
        built = vl.rebuild_question(cfg, man, raw, q, 4)
        if built is None:
            continue
        _p, key2, key1 = built
        if vl.key_verify(man, raw, key2, key1, q) is None:
            continue
        if q["md5"] not in seen:
            seen.add(q["md5"])
            md5s.append(q["md5"])
    return md5s


def classify_v2(cfg, client, row, tier: str) -> dict:
    """One item through the v2 prompt, mirroring
    phase1._classify_one (meta block, guards, F5 salvage)
    under the experiment's usage stage."""
    from album_sorter.gemini_client import BudgetExceeded
    from album_sorter.usage import usage_context
    meta_block = {
        "filename": row["done_name"],
        "timestamp": row["capture_time_raw"],
        "device": row["device"],
        "time_confidence": row["time_confidence"],
        "hints": soft_hints(row["done_name"],
                            type("M", (), {"software": ""})()),
    }
    parts = [{"text": json.dumps(meta_block)},
             {"image_md5": row["md5"]}]
    rec = {"md5": row["md5"]}
    with usage_context(stage="exp-phase1-v2", subject=row["md5"]):
        res = client.generate_json(
            cfg.phase1_model, phase1.SYSTEM, parts,
            max_output_tokens=cfg.phase1_max_output_tokens,
            service_tier=tier)
        data = phase1.apply_guards(res.data,
                                   cfg.triage_confidence_floor)
        tin, tout, thoughts = (res.tokens_in, res.tokens_out,
                               res.thoughts)
        if data["category"] == "PHOTO" and not data.get("label"):
            try:
                res2 = client.generate_json(
                    cfg.phase1_model, phase1.SYSTEM, parts,
                    max_output_tokens=cfg.phase1_max_output_tokens,
                    service_tier=tier)
                tin += res2.tokens_in
                tout += res2.tokens_out
                thoughts += res2.thoughts
                label2 = phase1.apply_guards(
                    res2.data, cfg.triage_confidence_floor).get("label")
                if label2:
                    data = {**data, "label": label2}
            except BudgetExceeded:
                raise
            except Exception:
                pass
    rec.update(category=data["category"],
               confidence=data.get("confidence"),
               tokens_in=tin, tokens_out=tout, thoughts=thoughts,
               label=data.get("label"))
    return rec


def recover_from_spill(man_out: Manifest, md5: str,
                       floor: float) -> dict | None:
    """Rebuild an item's record from the recording client's
    raw-response spill in the OUT copy (B35's no-re-spend
    asset): a run whose collector died after the calls were
    paid for (2026-10-23: the manifest write raced the
    recording client's own writes and the main thread died
    on 'database is locked', orphaning ~120 finished
    verdicts in the pool) loses nothing - the spill IS the
    response. Mirrors classify_v2's semantics: first spill
    is the classification call; a second spill contributes
    only its label (the F5 salvage). Token figures are
    summed from the central ledger's rows for this stage +
    subject, so the recovered record accounts exactly what
    was billed. Returns None when no usable spill exists."""
    rows = man_out.query(
        "SELECT raw_text FROM raw_responses WHERE stage = ? "
        "AND subject = ? ORDER BY id", ("exp-phase1-v2", md5))
    if not rows:
        return None
    try:
        data = phase1.apply_guards(json.loads(rows[0]["raw_text"]),
                                   floor)
        if data["category"] == "PHOTO" and not data.get("label") \
                and len(rows) > 1:
            label2 = phase1.apply_guards(
                json.loads(rows[1]["raw_text"]), floor).get("label")
            if label2:
                data = {**data, "label": label2}
    except Exception:
        return None
    from album_sorter.usage import central_rows
    tin = tout = thoughts = 0
    for r in central_rows():
        if r.get("stage") == "exp-phase1-v2" \
                and r.get("subject") == md5:
            tin += r["tokens_in"] or 0
            tout += r["tokens_out"] or 0
            thoughts += r.get("tokens_thoughts") or 0
    return {"md5": md5, "category": data["category"],
            "confidence": data.get("confidence"),
            "tokens_in": tin, "tokens_out": tout,
            "thoughts": thoughts, "label": data.get("label"),
            "recovered": True}


def apply_label(man_out: Manifest, rec: dict) -> None:
    """Write one enriched analysis into the out copy, with
    a bounded retry: the recording client writes into the
    same database from the worker threads while the pool
    runs, and sqlite has no busy-retry here - the caller
    applies labels only after the pool drains, and this
    retry absorbs any residual contention."""
    import sqlite3 as _sq
    import time as _time
    label = {**rec["label"], "v": phase1.PHASE1_SCHEMA_VERSION}
    for attempt in range(6):
        try:
            man_out.update_item(
                rec["md5"], analysis_json=json.dumps(label))
            return
        except _sq.OperationalError:
            if attempt == 5:
                raise
            _time.sleep(0.3 * (attempt + 1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-copy", required=True)
    ap.add_argument("--out-copy", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--assets-dir", required=True)
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--tier", default="flex")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--stub", action="store_true")
    args = ap.parse_args()

    src = Path(args.source_copy).resolve()
    out = Path(args.out_copy).resolve()
    if str(out) in LIVE or str(src) in LIVE:
        print("refusing: experiments never touch a live manifest",
              file=sys.stderr)
        return 2
    if not out.exists():
        shutil.copy(src, out)
        print(f"copied {src.name} -> {out.name}")

    cfg = Config(work_dir=Path(args.work_dir))
    man_src = Manifest(src)
    raw = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    raw.row_factory = sqlite3.Row
    gt = json.loads(Path(args.ground_truth).read_text())
    md5s = verified_sample_items(cfg, man_src, raw, gt["linkage"])
    print(f"verified sample items: {len(md5s)}")
    if args.limit:
        md5s = md5s[:args.limit]

    results_path = Path(args.results)
    done = set()
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if not r.get("error"):
                    done.add(r["md5"])
    todo = [m for m in md5s if m not in done]

    man_out = Manifest(out)
    # Spill recovery pre-pass: any todo item with a usable
    # spilled response in the out copy is already paid for -
    # reconstruct it instead of re-asking (no-re-spend).
    if todo and not args.stub:
        fh0 = results_path.open("a")
        still = []
        for m in todo:
            rec = recover_from_spill(man_out, m,
                                     cfg.triage_confidence_floor)
            if rec is None:
                still.append(m)
                continue
            fh0.write(json.dumps(rec) + "\n")
            fh0.flush()
            if rec.get("label"):
                apply_label(man_out, rec)
            done.add(m)
        fh0.close()
        if len(still) < len(todo):
            print(f"recovered from spill: {len(todo) - len(still)}")
        todo = still
    print(f"to describe: {len(todo)} (already done: "
          f"{len(md5s) - len(todo)})")
    if args.stub:
        from album_sorter.gemini_client import StubClient
        client = StubClient(lambda model, system, parts: {
            "category": "PHOTO", "confidence": 0.95,
            "label": dict(STUB_LABEL)})
    else:
        from album_sorter.gemini_client import make_client
        from album_sorter.usage import attach_manifest
        client = make_client(Path(args.assets_dir))
        attach_manifest(client, man_out)

    rows = {r["md5"]: r for r in man_src.query(
        "SELECT * FROM items WHERE md5 IN (%s)"
        % ",".join("?" * len(md5s)), md5s)} if md5s else {}

    def work(md5):
        if args.stub:
            return {"md5": md5, "category": "PHOTO",
                    "confidence": 0.95, "tokens_in": 0,
                    "tokens_out": 0, "thoughts": 0,
                    "label": dict(STUB_LABEL)}
        try:
            return classify_v2(cfg, client, rows[md5], args.tier)
        except Exception as e:
            return {"md5": md5,
                    "error": f"{type(e).__name__}: {e}"[:300]}

    fh = results_path.open("a")
    collected = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(work, m) for m in todo]
        for fut in as_completed(futs):
            rec = fut.result()
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            collected.append(rec)
    fh.close()
    # Labels are applied ONLY after the pool drains: while
    # it runs, the recording client writes into this same
    # database from the worker threads, and a main-thread
    # update racing those writes died on 'database is
    # locked' in the first metered run (56 of 180 applied;
    # the rest were recovered from the spill, above).
    for rec in collected:
        if not rec.get("error") and rec.get("label"):
            apply_label(man_out, rec)
    summarize(results_path, man_src, md5s)
    return 0


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(p * len(s)))]


def summarize(results_path: Path, man_src: Manifest, md5s) -> None:
    recs = {}
    for line in results_path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if not r.get("error"):
                recs[r["md5"]] = r
    got = [recs[m] for m in md5s if m in recs]
    photos = [r for r in got if r["category"] == "PHOTO"]
    labeled = [r for r in photos if r.get("label")]
    touts = [r["tokens_out"] for r in got]
    v1_missing = v1_touts = 0
    v1_n = 0
    for m in md5s:
        row = man_src.get_by_md5(m)
        if row is None:
            continue
        v1_n += 1
        if row["category"] == "PHOTO" and not row["analysis_json"]:
            v1_missing += 1
        v1_touts += row["tokens_out"] or 0
    print("\n=== phase-1 v2 re-description ===")
    print(f"items: {len(got)}/{len(md5s)} answered "
          f"(errors: {len(md5s) - len(got)})")
    if touts:
        mean = sum(touts) / len(touts)
        print(f"tokens_out (candidates): mean {mean:.0f}, "
              f"p95 {_pct(touts, 0.95)} "
              f"(v1 banked mean for the same items: "
              f"{v1_touts / max(v1_n, 1):.0f})")
    print(f"categories: PHOTO {len(photos)}, "
          f"other {len(got) - len(photos)}")
    print(f"schema-valid (PHOTO with label): {len(labeled)}/"
          f"{len(photos)}; label-missing "
          f"{len(photos) - len(labeled)}"
          f" (v1 banked label-missing: {v1_missing})")
    for f in ("dominant_colors", "scene", "distinctive_features"):
        have = sum(1 for r in labeled if r["label"].get(f))
        print(f"  field {f}: present on {have}/{len(labeled)} labels")


if __name__ == "__main__":
    raise SystemExit(main())
