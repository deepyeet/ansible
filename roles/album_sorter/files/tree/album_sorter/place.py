"""Phase 3 - placement + rename (spec Section 14).

Crash-safe protocol per file: write final_path + status=placing to
the manifest, rename(2), then status=placed. Nothing is deleted,
ever; exact-duplicate arrivals stay in place and are reported.
Month buckets are per item (its own corrected month).
"""
from __future__ import annotations

import calendar
from datetime import datetime
from pathlib import Path

from .config import Config
from .manifest import Manifest


def destination_for(cfg: Config, item, episode_outcomes: dict[int, tuple[str, str | None]]) -> Path:
    cat = item["category"]
    if cat == "MISC":
        folder = cfg.sorted_dir / "__Misc"
    elif cat == "MEDICAL":
        folder = cfg.sorted_dir / "Medical"
    elif cat in ("REVIEW", "ERROR") or item["status"] == "triaged" and cat is None:
        folder = cfg.sorted_dir / "REVIEW"
    elif item["episode_id"] and item["episode_id"] in episode_outcomes:
        outcome, album = episode_outcomes[item["episode_id"]]
        if outcome in ("album_registry", "album_new") and album:
            folder = cfg.sorted_dir / album
        else:
            folder = _month_bucket(cfg, item)
    else:
        folder = _month_bucket(cfg, item)
    return folder


def _month_bucket(cfg: Config, item) -> Path:
    t = datetime.fromisoformat(item["capture_time"])
    return cfg.sorted_dir / f"{t.year} {calendar.month_name[t.month]}"


def reconcile_placing(man: Manifest, src_dir: Path) -> dict:
    """Resolve rows stranded at status='placing' by a crash between
    the manifest write and the rename (or just after it). The file
    is in exactly one of the two places; the manifest follows it."""
    out = {"confirmed_placed": 0, "reset_for_retry": 0, "lost": 0}
    for row in man.query("SELECT * FROM items WHERE status = 'placing'"):
        target = Path(row["final_path"]) if row["final_path"] else None
        src = src_dir / row["done_name"]
        if target and target.exists() and not src.exists():
            man.update_item(row["md5"], status="placed")
            out["confirmed_placed"] += 1
        elif src.exists() and not (target and target.exists()):
            man.update_item(row["md5"],
                            status="grouped" if row["episode_id"] else "triaged",
                            final_path=None)
            out["reset_for_retry"] += 1
        else:
            out["lost"] += 1   # both or neither present: report, never guess
    return out


def _plan(cfg: Config, man: Manifest, src_dir: Path, virtual: dict | None = None):
    """Read-only placement computation shared by place_all and
    the dry-run planner. Yields one dict per eligible item:
    {md5, src, target, action} with action in
    move | keep_suffix | dupe_left | missing_src | not_ready | blocked.
    Only destination-determined items are eligible: grouped
    items whose episode has a gate outcome, and triaged
    non-PHOTO items (MISC / MEDICAL / REVIEW / ERROR). A PHOTO
    item that was never grouped - or whose episode is still
    pending a gate decision - is NOT month-bucketed by default:
    running `place` early must not empty 3_Done or dissolve
    ungated episodes (F11).
    `virtual` (dry-run only) maps target paths to the md5 of
    the item an earlier planned move would put there, so the
    plan accounts for intra-batch collisions exactly as the
    real run's disk state does (F19)."""
    from .ingest import md5_of
    virtual = virtual if virtual is not None else {}
    outcomes = {ep["episode_id"]: (ep["outcome"], ep["album_name"])
                for ep in man.all_episodes() if ep["outcome"] != "pending"}
    pending_eps = {ep["episode_id"] for ep in man.all_episodes()
                   if ep["outcome"] == "pending"}
    from datetime import timedelta
    sweep_cutoff = datetime.now() - timedelta(days=cfg.sweep_min_age_days)
    for row in man.query("SELECT * FROM items WHERE status IN ('grouped','triaged','analyzed')"):
        entry = {"md5": row["md5"], "src": src_dir / row["done_name"], "target": None}
        if row["category"] == "PHOTO" and (not row["episode_id"]
                                           or row["episode_id"] in pending_eps):
            # B29 age-gated sweep: a loose photo (no episode)
            # whose capture time is at least sweep_min_age_days
            # old has had its chances - grouping reconsiders
            # loose items every run - and is swept into its own
            # month bucket (spec Sec. 13). Younger loose photos,
            # timeless ones, and members of ungated episodes
            # stay held: placement is a one-way move, and the
            # user uploads from the sorted tree, so a premature
            # bucket would lock the photo out of any album that
            # forms later.
            swept = False
            if not row["episode_id"] and row["capture_time"]:
                try:
                    swept = datetime.fromisoformat(row["capture_time"]) <= sweep_cutoff
                except ValueError:
                    swept = False
            if not swept:
                entry["action"] = "not_ready"
                yield entry
                continue
            entry["swept"] = True
        if (row["file_size"] is not None and entry["src"].exists()
                and entry["src"].stat().st_size != row["file_size"]):
            # the source changed after ingest (a partial copy
            # completed, a re-export landed): its content no
            # longer matches the recorded digest's identity.
            # Never move a file under the wrong identity -
            # leave it for re-ingest to sort out.
            entry["action"] = "src_changed"
            yield entry
            continue
        if (row["file_mtime"] is not None and entry["src"].exists()
                and entry["src"].stat().st_mtime != row["file_mtime"]):
            # same size, new mtime (B25 decision): worth one
            # hash to distinguish a mere touch from a same-size
            # content swap - only the swap blocks placement
            from .ingest import md5_of as _md5
            if _md5(entry["src"]) != row["md5"]:
                entry["action"] = "src_changed"
                yield entry
                continue
        folder = destination_for(cfg, row, outcomes)
        if folder.parent != cfg.sorted_dir:
            # every legitimate destination is a DIRECT child of
            # the sorted root; anything else means a name tried
            # to be a path (F2) - refuse, never traverse
            entry["action"] = "blocked"
            yield entry
            continue
        target_name = row["clean_name"] if row["suffix_verified"] else row["done_name"]
        target = folder / target_name
        action = "move"
        existing_md5 = virtual.get(target)
        if existing_md5 is None and target.exists():
            if not target.is_file():
                action = "blocked"      # a directory wears this name (Q1)
            else:
                existing_md5 = md5_of(target)   # streamed; may be a multi-GB video
        if action == "move" and existing_md5 is not None:
            # collision: identical content -> leave source; else
            # keep the suffix (if that name is also taken, leave)
            if existing_md5 == row["md5"]:
                action = "dupe_left"
            else:
                target = folder / row["done_name"]
                if target in virtual:
                    action = "dupe_left"
                elif target.exists():
                    action = "dupe_left" if target.is_file() else "blocked"
                else:
                    action = "keep_suffix"
        if action not in ("dupe_left", "blocked") and not entry["src"].exists():
            action = "missing_src"
        entry.update(target=target, action=action)
        yield entry


def plan_placements(cfg: Config, man: Manifest, src_dir: Path) -> dict:
    """Report-only mode (backlog B2): what place_all WOULD do,
    computed with zero writes - no moves, no manifest changes,
    no folders created. The probation mode for unattended runs."""
    counts = {"move": 0, "keep_suffix": 0, "dupe_left": 0,
              "missing_src": 0, "not_ready": 0, "blocked": 0, "src_changed": 0,
              "swept": 0}
    entries = []
    virtual: dict = {}
    for e in _plan(cfg, man, src_dir, virtual=virtual):
        counts[e["action"]] += 1
        if e.get("swept") and e["action"] in ("move", "keep_suffix"):
            counts["swept"] += 1
        if e["action"] in ("move", "keep_suffix"):
            virtual[e["target"]] = e["md5"]
        entries.append({"file": e["src"].name, "action": e["action"],
                        "target": str(e["target"]) if e["target"] else None})
    return {"dry_run": True, "counts": counts, "entries": entries}


def place_all(cfg: Config, man: Manifest, src_dir: Path) -> dict:
    man.backup("pre-place", mirror_dir=cfg.backup_dir)
    stats = {"placed": 0, "dupes_left": 0, "kept_suffix": 0, "missing_src": 0,
             "not_ready": 0, "blocked": 0, "src_changed": 0, "verify_failed": 0,
             "swept": 0,
             "reconciled": reconcile_placing(man, src_dir)}
    placed_targets: list[Path] = []
    for e in _plan(cfg, man, src_dir):
        if e["action"] == "not_ready":
            stats["not_ready"] += 1
        elif e["action"] == "dupe_left":
            stats["dupes_left"] += 1
        elif e["action"] == "missing_src":
            stats["missing_src"] += 1
        elif e["action"] == "blocked":
            stats["blocked"] += 1
        elif e["action"] == "src_changed":
            stats["src_changed"] += 1
        else:
            if e["target"].exists():
                # execution-time re-check: rename(2) onto an
                # existing path REPLACES it - that would be a
                # delete. The plan resolved collisions before
                # any rename, and execution is sequential, so a
                # target existing NOW means something outside
                # this run put it there since planning. Refuse:
                # never rename onto an existing path.
                stats["blocked"] += 1
                continue
            e["target"].parent.mkdir(parents=True, exist_ok=True)
            if e.get("swept"):
                stats["swept"] += 1
                man.update_item(e["md5"], assign_method="gate")
            man.update_item(e["md5"], final_path=str(e["target"]), status="placing")
            e["src"].rename(e["target"])
            man.update_item(e["md5"], status="placed")
            stats["placed"] += 1
            placed_targets.append(e["target"])
            if e["action"] == "keep_suffix":
                stats["kept_suffix"] += 1
    # post-move verification (spec 14.5, review F12): everything
    # the manifest now calls placed must actually be there
    for t in placed_targets:
        if not t.exists():
            stats["verify_failed"] += 1
    return stats
