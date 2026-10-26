"""Run the (v1.4 notability) gate over an existing work dir's
episodes and print outcomes. Usage: gate_check.py <work_dir>"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from album_sorter import gates
from album_sorter.config import Config
from album_sorter.gemini_client import make_client
from album_sorter.manifest import Manifest

BASE = Path(__file__).resolve().parent
work = BASE / sys.argv[1]
cfg = Config(done_dir=BASE / "cal_corpus", work_dir=work, sorted_dir=BASE / "cal_sorted")
man = Manifest(cfg.manifest_path)
registry = gates.load_registry(cfg)
client = make_client(cfg.assets_dir)
for ep in man.all_episodes():
    if ep["outcome"] == "merged":
        continue
    out = gates.apply_gate(man, cfg, client, registry, ep["episode_id"])
    ep2 = man.get_episode(ep["episode_id"])
    print(f"EP{ep['episode_id']} [{ep['item_count']:3}] -> {out:14} "
          f"{ep2['album_name'] or '':22} [{ep2['gate_character']}] {ep2['notability_reason'] or ''} :: "
          f"{(ep['description'] or '')[:60]}")
print("usage:", json.dumps(client.usage_summary()))
