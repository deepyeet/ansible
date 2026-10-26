"""Live smoke test (container): ingest -> Phase 1 -> grouping ->
gates, with real Gemini calls and exact usage accounting.
Spend discipline: 15 items, one grouping window. Run detached:
  nohup ~/album-venv/bin/python smoke.py > smoke.log 2>&1 &
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from album_sorter import gates, grouping, ingest as ing, phase1
from album_sorter.config import Config
from album_sorter.gemini_client import make_client
from album_sorter.manifest import Manifest

cfg = Config()
man = Manifest(cfg.manifest_path)
client = make_client(cfg.assets_dir)
print("client:", type(client).__name__, flush=True)
print("ingest:", ing.ingest(cfg, man), flush=True)
print("phase1:", phase1.run_phase1(cfg, man, client), flush=True)
print("usage after phase1:", json.dumps(client.usage_summary()), flush=True)
for r in man.query("SELECT done_name, category, category_confidence FROM items ORDER BY capture_time"):
    print("  P1:", r["done_name"][:40], "->", r["category"], r["category_confidence"], flush=True)
lab = man.query_one("SELECT analysis_json FROM items WHERE analysis_json IS NOT NULL LIMIT 1")
print("sample label:", (lab["analysis_json"] or "")[:400], flush=True)
print("grouping:", grouping.run_grouping(cfg, man, client, cfg.work_dir), flush=True)
registry = gates.load_registry(cfg)
for ep in man.all_episodes():
    print("gate:", ep["episode_id"],
          gates.apply_gate(man, cfg, client, registry, ep["episode_id"]), flush=True)
for ep in man.all_episodes():
    print("  EP:", ep["episode_id"], ep["album_name"], "|", (ep["description"] or "")[:160], flush=True)
print("usage FINAL:", json.dumps(client.usage_summary()), flush=True)
print("SMOKE DONE", flush=True)
