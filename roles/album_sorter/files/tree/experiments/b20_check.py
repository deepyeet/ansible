"""B20 live validation: re-run Flash grouping on the
calibration items with same-run ledger visibility, and see
whether the Arte Museum seam split still happens at grouping
time (baseline: Flash produced 36+40 halves, healed only by
the merge pass)."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from album_sorter import grouping, merge
from album_sorter.config import Config
from album_sorter.gemini_client import make_client
from album_sorter.manifest import Manifest

BASE = Path(__file__).resolve().parent

work = BASE / "b20_work"
shutil.rmtree(work, ignore_errors=True)
shutil.copytree(BASE / "cal_work_flash", work)
cfg = Config(done_dir=BASE / "cal_corpus", work_dir=work,
             sorted_dir=BASE / "b20_sorted", grouping_model="gemini-3.8-flash")
man = Manifest(cfg.manifest_path)
man.execute("UPDATE items SET episode_id = NULL, status = 'analyzed' "
            "WHERE category = 'PHOTO'")
man.execute("DELETE FROM episodes")
client = make_client(cfg.assets_dir)
print("grouping:", grouping.run_grouping(cfg, man, client, cfg.work_dir), flush=True)
for ep in man.all_episodes():
    print(f"  ep{ep['episode_id']}: n={ep['item_count']} "
          f"{(ep['description'] or '')[:80]}")
print("merge:", merge.run_merge(cfg, man, client), flush=True)
for ep in man.all_episodes():
    if ep["outcome"] != "merged":
        print(f"  POST-MERGE ep{ep['episode_id']}: n={ep['item_count']} "
              f"{(ep['description'] or '')[:80]}")
print("usage:", client.usage_summary())
