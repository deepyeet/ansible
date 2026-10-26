"""One-off album surgery for the production manifest
(2026-10-16, user direction): fuse the Los Cabos cluster
into '2026 Los Cabos' and the Las Vegas cluster into
'2026 Las Vegas' (split BY LOCATION, his granularity), and
rename '2026 Metaversary' to '2026 Meta Office Visit' (it
was a family office visit, not a work anniversary).

Same file discipline as placement: rename-only, never
overwrite an existing path, manifest updated per item,
emptied source folders removed. Items keep their episode
ids; the episodes' album_name (and outcome for month-bucket
episodes) are updated to the fused album so future runs see
the user's decision.
Usage: fuse_albums.py [--yes]   (default: dry-run plan)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from album_sorter.config import Config
from album_sorter import locking
from album_sorter.manifest import Manifest

CABO_TARGET = "2026 Los Cabos"
CABO_EPISODES = [37, 38, 39, 40, 41, 42, 43, 44, 47, 49, 50]  # + keepers 45, 48
VEGAS_TARGET = "2026 Las Vegas"
VEGAS_EPISODES = [51, 52, 54, 55, 56, 57, 58]                  # + keeper 53
RENAME_FROM, RENAME_TO, RENAME_EPISODE = "2026 Metaversary", "2026 Meta Office Visit", 63
EPISODE_FIXES = [(RENAME_EPISODE,
                  "Visiting the Meta office in Bellevue for a family visit, "
                  "drawing on the Facebook Wall, playing on giant sushi "
                  "sculptures, taking multi-generational family sunset "
                  "portraits on the rooftop terrace, and playing piano")]


def move_item(cfg: Config, man: Manifest, item, target_dir: Path,
              stats: dict) -> None:
    src = Path(item["final_path"])
    dst = target_dir / src.name
    if dst.exists():
        import hashlib
        if hashlib.md5(dst.read_bytes()).hexdigest() == item["md5"]:
            stats["dupe_left"] += 1
            return
        alt = target_dir / item["done_name"]
        if alt.exists():
            stats["blocked"] += 1
            return
        dst = alt
        stats["kept_suffix"] += 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    man.update_item(item["md5"], final_path=str(dst))
    stats["moved"] += 1


def main() -> int:
    apply = "--yes" in sys.argv
    import os
    root = Path(os.environ.get("ALBUM_PROD_ROOT", "/mnt/pixel"))
    cfg = Config(done_dir=root / "3_Done", sorted_dir=root / "4_Sorted",
                 work_dir=root / ".sorter-work")
    man = Manifest(cfg.manifest_path)
    plan: list[tuple[str, list[int]]] = [(CABO_TARGET, CABO_EPISODES),
                                         (VEGAS_TARGET, VEGAS_EPISODES)]
    for target, eps in plan:
        rows = man.query(
            "SELECT COUNT(*) c FROM items WHERE status='placed' AND episode_id IN (%s)"
            % ",".join(str(e) for e in eps))
        print(f"{target}: {rows[0]['c']} items to move from episodes {eps}")
    n = man.query("SELECT COUNT(*) c FROM items WHERE status='placed' AND episode_id=?",
                  (RENAME_EPISODE,))[0]["c"]
    print(f"rename {RENAME_FROM!r} -> {RENAME_TO!r}: {n} items (folder rename)")
    if not apply:
        print("DRY RUN - re-run with --yes to apply")
        return 0
    with locking.run_lock(cfg.work_dir):
        man.backup('pre-fuse', cfg.backup_dir)
        stats = {"moved": 0, "dupe_left": 0, "blocked": 0, "kept_suffix": 0}
        for target, eps in plan:
            target_dir = cfg.sorted_dir / target
            for ep in eps:
                for item in man.query('SELECT * FROM items WHERE episode_id=?', (ep,)):
                    if item["status"] == "placed":
                        move_item(cfg, man, item, target_dir, stats)
                man.update_episode(ep, album_name=target, outcome="album_new")
        # the rename: one folder move, prefix-swap final_paths
        old_dir, new_dir = cfg.sorted_dir / RENAME_FROM, cfg.sorted_dir / RENAME_TO
        if old_dir.exists() and not new_dir.exists():
            old_dir.rename(new_dir)
            for item in man.query('SELECT * FROM items WHERE episode_id=?', (RENAME_EPISODE,)):
                fp = item["final_path"]
                if fp:
                    man.update_item(item["md5"],
                                    final_path=str(new_dir / Path(fp).name))
            stats["moved"] += n
        man.update_episode(RENAME_EPISODE, album_name=RENAME_TO)
        for ep_id, desc in EPISODE_FIXES:
            man.update_episode(ep_id, description=desc)
        # remove emptied source album folders
        for name in ("2026 Resort Vacation", "2026 San José del Cabo",
                     "2026 Los Cabos Trip", "2026 Family Photoshoot",
                     "2026 Seven Magic Mountains", "2026 V Theater",
                     "2026 The Venetian", "2026 Immersive Art Exhibit"):
            d = cfg.sorted_dir / name
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                print("removed empty folder:", name)
        print("stats:", stats)
        missing = [i["done_name"] for i in man.query(
            "SELECT * FROM items WHERE status='placed'")
            if not Path(i["final_path"]).exists()]
        print("placed items with missing files:", len(missing))
        return 1 if missing or stats["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
