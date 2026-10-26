"""Run merge + rollup against an existing work dir and print
states between passes. Usage: rollup_check.py <work_dir>"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from album_sorter import merge, rollup
from album_sorter.config import Config
from album_sorter.gemini_client import make_client
from album_sorter.manifest import Manifest

BASE = Path(__file__).resolve().parent
work = BASE / sys.argv[1]
cfg = Config(done_dir=BASE / "cal_corpus", work_dir=work, sorted_dir=BASE / "cal_sorted")
man = Manifest(cfg.manifest_path)
client = make_client(cfg.assets_dir)
print("merge:", merge.run_merge(cfg, man, client))
print("chains:", rollup.candidate_chains(man, cfg.rollup_gap_hours, cfg.rollup_max_span_days))
print("rollup:", rollup.run_rollup(cfg, man, client))
for ep in man.all_episodes():
    print(f"  EP{ep['episode_id']} [{ep['item_count']:3}] outcome={ep['outcome']} {(ep['description'] or '')[:90]}")
print("usage:", json.dumps(client.usage_summary()))
