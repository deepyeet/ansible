"""Phase 4 - per-run report (spec Section 15). An audit artifact:
The user's review surface before his manual archival steps."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .config import Config
from .manifest import Manifest


def write_report(cfg: Config, man: Manifest, out_path: Path) -> Path:
    items = man.query("SELECT * FROM items")
    cats = Counter(i["category"] or "?" for i in items)
    statuses = Counter(i["status"] for i in items)
    tin = sum(i["tokens_in"] for i in items)
    tout = sum(i["tokens_out"] for i in items)
    lines = ["# Album sorter run report", ""]
    lines.append(f"Items: {len(items)}  |  by category: {dict(cats)}  |  by status: {dict(statuses)}")
    lines.append(f"Tokens: {tin} in / {tout} out (cost tracking lands with the live client)")
    # B32: the decision journal is the durable spend record -
    # unlike the client's in-memory counters, it survives a
    # crashed run, so "what did the dead run already buy" is
    # always answerable from the manifest
    journal = man.decision_counts()
    if journal:
        jt = man.query_one(
            "SELECT COALESCE(SUM(tokens_in), 0) AS tin, "
            "COALESCE(SUM(tokens_out), 0) AS tout FROM decisions")
        lines.append(f"Decision journal: {sum(journal.values())} stored verdicts "
                     f"{journal} | journaled tokens: {jt['tin']} in / {jt['tout']} out")
    # B33: the per-call usage ledger covers EVERY stage
    # (grouping included, which the journal does not) and
    # survives a crashed run; costs are estimated from the
    # dated price table in usage.py
    calls = man.api_call_rows()
    if calls:
        from .usage import summarize
        s = summarize(calls)
        lines.append(f"API usage ledger: {s['total']['calls']} calls | "
                     f"{s['total']['tokens_in']} in / {s['total']['tokens_out']} out | "
                     f"est. ${s['total']['est_usd']:.2f}")
        for stage, b in sorted(s["by_stage"].items()):
            lines.append(f"  - {stage}: {b['calls']} calls, "
                         f"{b['tokens_in']} in / {b['tokens_out']} out, "
                         f"est. ${b['est_usd']:.2f}")
    lines += ["", "## Episodes", ""]
    for ep in man.all_episodes():
        lines.append(f"- **{ep['album_name'] or '(no album)'}** [{ep['outcome']}] "
                     f"{ep['item_count']} items, {ep['span_start']} .. {ep['span_end']}")
        lines.append(f"  {ep['description'] or ''}")
    specific = [ep for ep in man.all_episodes()
                if ep["outcome"] == "album_new"
                and (ep["name_basis"] or "generic") != "generic"]
    if specific:
        lines += ["", "## Names to spot-check (specific names verified against the photos)", ""]
        for ep in specific:
            lines.append(f"- **{ep['album_name']}** (basis: {ep['name_basis']})")
    unverified = [ep for ep in man.all_episodes()
                  if ep["outcome"] == "album_new"
                  and "not evidenced in photos" in (ep["notability_reason"] or "")]
    if unverified:
        lines += ["", "## Specific names NOT evidenced (generic fallback used)", ""]
        for ep in unverified:
            lines.append(f"- **{ep['album_name']}** - {ep['notability_reason']}")
    borderline = [ep for ep in man.all_episodes() if ep["gate_character"] == "borderline"]
    if borderline:
        lines += ["", "## Possible albums (borderline notability - placed in month buckets, promote manually if they deserve albums)", ""]
        for ep in borderline:
            lines.append(f"- {ep['item_count']} items, {ep['span_start']} .. {ep['span_end']}: "
                         f"{ep['description'] or ''} [{ep['notability_reason'] or ''}]")
    kept = [i for i in items if i["status"] == "placed" and i["final_path"]
            and Path(i["final_path"]).name != (i["clean_name"] or "")]
    if kept:
        lines += ["", "## Placed with original (suffixed) names - clean-name collisions", ""]
        for i in kept:
            lines.append(f"- {i['done_name']} -> {i['final_path']}")
    unplaced = [i for i in items if i["category"] == "PHOTO"
                and i["status"] in ("grouped", "triaged", "analyzed")]
    if unplaced:
        lines += ["", "## Photos not yet placed", ""]
        for i in unplaced:
            lines.append(f"- {i['done_name']}: status={i['status']} episode={i['episode_id']}")
    lines += ["", "## Needs review", ""]
    for i in items:
        if i["category"] in ("REVIEW", "ERROR") or i["error"]:
            lines.append(f"- {i['done_name']}: category={i['category']} error={i['error'] or ''}")
    lines += ["", "## Low time-confidence items", ""]
    for i in items:
        if i["time_confidence"] == "low":
            lines.append(f"- {i['done_name']} ({i['time_source']})")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    return out_path
