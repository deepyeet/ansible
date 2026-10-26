"""Run the merge pass against an existing calibration work dir
and print candidate verdicts + before/after episode states.
Usage: python3 merge_check.py <work_dir_name>"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from album_sorter import merge
from album_sorter.config import Config
from album_sorter.gemini_client import make_client
from album_sorter.manifest import Manifest

BASE = Path(__file__).resolve().parent
work = BASE / sys.argv[1]
cfg = Config(done_dir=BASE / "cal_corpus", work_dir=work, sorted_dir=BASE / "cal_sorted")
man = Manifest(cfg.manifest_path)
print("candidates:", [(a, b) for a, b in merge.candidate_pairs(man)])
for ep in man.all_episodes():
    print(f"  before EP{ep['episode_id']} [{ep['item_count']}] {ep['span_start']}..{ep['span_end']} {(ep['description'] or '')[:70]}")
client = make_client(cfg.assets_dir)
print("merge:", merge.run_merge(cfg, man, client))
for ep in man.all_episodes():
    print(f"  after  EP{ep['episode_id']} [{ep['item_count']}] outcome={ep['outcome']} {(ep['description'] or '')[:80]}")
print("usage:", json.dumps(client.usage_summary()))
