"""Manifest invariant audit (spec Sec. 13 sanity check, B23).

The spec's invariant: every PHOTO item has exactly one
destination. Code checks, all read-only:
- no two placed items share a final_path (a shared path
  would mean one file silently stands for two photos);
- every placed item's file exists at its recorded path;
- every final_path lives under the sorted root;
- album outcomes always carry an album name;
- episode item_count agrees with actual membership;
- files sitting in album folders that no placed item
  claims (a move the manifest missed) are violations.
Loose PHOTO items (analyzed, no episode) are NOT
violations under the current hold-for-context policy -
but they are counted and reported, never invisible.
"""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .manifest import Manifest


def audit_manifest(cfg: Config, man: Manifest) -> dict:
    violations: list[str] = []
    placed = man.query("SELECT * FROM items WHERE status = 'placed'")
    by_path: dict[str, str] = {}
    root = cfg.sorted_dir.resolve()
    claimed: set[str] = set()
    for it in placed:
        fp = it["final_path"] or ""
        if fp in by_path:
            violations.append(
                f"duplicate final_path {fp}: {by_path[fp]} and {it['done_name']}")
        by_path[fp] = it["done_name"]
        claimed.add(fp)
        if not fp or not Path(fp).exists():
            violations.append(f"placed file missing: {it['done_name']} -> {fp}")
        elif root not in Path(fp).resolve().parents:
            violations.append(f"final_path escapes sorted root: {fp}")
    for ep in man.all_episodes():
        if ep["outcome"] in ("album_new", "album_registry") and not ep["album_name"]:
            violations.append(f"episode {ep['episode_id']}: {ep['outcome']} without album_name")
        actual = man.query(
            "SELECT COUNT(*) c FROM items WHERE episode_id = ?",
            (ep["episode_id"],))[0]["c"]
        if ep["outcome"] != "merged" and actual != (ep["item_count"] or 0):
            violations.append(
                f"episode {ep['episode_id']}: item_count {ep['item_count']} != actual {actual}")
    if cfg.sorted_dir.exists():
        for p in cfg.sorted_dir.rglob("*"):
            if p.is_dir() and p.name == "@eaDir":
                continue
            if p.is_file() and not p.name.startswith("_report") \
                    and "@eaDir" not in p.parts and str(p) not in claimed:
                violations.append(f"on-disk file unknown to manifest: {p}")
    loose = man.query(
        "SELECT COUNT(*) c FROM items WHERE category = 'PHOTO' "
        "AND episode_id IS NULL AND status IN ('analyzed','grouped')")[0]["c"]
    return {"violations": violations,
            "info": {"placed": len(placed), "loose_photos_held": loose}}
