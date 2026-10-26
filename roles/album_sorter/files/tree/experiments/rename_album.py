"""Rename one placed album (folder + manifest), for user
corrections: rename_album.py "Old Name" "New Name" EPISODE_ID
[--desc "old substring" "new substring"]

Rename-only mechanics, same as placement: the folder is
renamed (never copied), every item's final_path is prefix-
swapped in the manifest, the episode's album_name follows.
Refuses if the target folder exists or the source doesn't.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from album_sorter.config import Config
from album_sorter import locking
from album_sorter.manifest import Manifest


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--desc"]
    old, new, ep_id = args[0], args[1], int(args[2])
    desc_pair = None
    if "--desc" in sys.argv:
        i = sys.argv.index("--desc")
        desc_pair = (sys.argv[i + 1], sys.argv[i + 2])
    root = Path(os.environ.get("ALBUM_PROD_ROOT", "/mnt/pixel"))
    cfg = Config(done_dir=root / "3_Done", sorted_dir=root / "4_Sorted",
                 work_dir=root / ".sorter-work")
    man = Manifest(cfg.manifest_path)
    old_dir, new_dir = cfg.sorted_dir / old, cfg.sorted_dir / new
    if not old_dir.is_dir():
        print("source folder missing:", old_dir)
        return 1
    if new_dir.exists():
        print("target already exists:", new_dir)
        return 1
    items = man.query("SELECT * FROM items WHERE episode_id=? AND status='placed'",
                      (ep_id,))
    print(f"{old!r} -> {new!r}: {len(items)} placed items, episode {ep_id}")
    with locking.run_lock(cfg.work_dir):
        man.backup("pre-rename", cfg.backup_dir)
        old_dir.rename(new_dir)
        for item in items:
            man.update_item(item["md5"],
                            final_path=str(new_dir / Path(item["final_path"]).name))
        man.update_episode(ep_id, album_name=new)
        if desc_pair:
            ep = man.get_episode(ep_id)
            man.update_episode(ep_id, description=ep["description"].replace(*desc_pair))
    missing = [i["done_name"] for i in items
               if not (new_dir / Path(i["final_path"]).name).exists()]
    print("missing after rename:", len(missing))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
