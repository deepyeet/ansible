"""Calibration run (container, detached): ~200-item sample,
Phase 1 once, then grouping twice - Pro and Flash - in branched
work dirs, gates on both, JSON dumps for comparison.
  nohup ~/album-venv/bin/python calibrate.py > calibrate.log 2>&1 &
"""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from album_sorter import gates, grouping, ingest as ing, merge, phase1, rollup
from album_sorter.config import Config
from album_sorter.gemini_client import make_client
from album_sorter.manifest import Manifest

BASE = Path(__file__).resolve().parent


def branch(cfg, man, model: str, tag: str) -> dict:
    client = make_client(cfg.assets_dir)
    cfg.grouping_model = model
    t0 = time.time()
    stats = grouping.run_grouping(cfg, man, client, cfg.work_dir)
    stats["merge"] = merge.run_merge(cfg, man, client)
    stats["rollup"] = rollup.run_rollup(cfg, man, client)
    seconds = time.time() - t0
    registry = gates.load_registry(cfg)
    outcomes = {}
    for ep in man.all_episodes():
        outcomes[ep["episode_id"]] = gates.apply_gate(man, cfg, client, registry, ep["episode_id"])
    episodes = [{"episode_id": ep["episode_id"], "description": ep["description"],
                 "span": [ep["span_start"], ep["span_end"]], "count": ep["item_count"],
                 "outcome": outcomes.get(ep["episode_id"]), "album": ep["album_name"]}
                for ep in man.all_episodes()]
    assign = {r["md5"]: r["episode_id"] for r in
              man.query("SELECT md5, episode_id FROM items WHERE episode_id IS NOT NULL")}
    names = {r["md5"]: r["done_name"] for r in man.query("SELECT md5, done_name FROM items")}
    dump = {"model": model, "seconds": round(seconds, 1), "grouping": stats,
            "episodes": episodes, "assignments": assign, "names": names,
            "usage": client.usage_summary()}
    (BASE / f"cal_{tag}.json").write_text(json.dumps(dump, indent=1))
    return dump


def main() -> None:
    work = BASE / "cal_work"
    if work.exists():
        shutil.rmtree(work)
    cfg = Config(done_dir=BASE / "cal_corpus", work_dir=work,
                 sorted_dir=BASE / "cal_sorted")
    man = Manifest(cfg.manifest_path)
    client = make_client(cfg.assets_dir)
    print("ingest:", ing.ingest(cfg, man), flush=True)
    t0 = time.time()
    print("phase1:", phase1.run_phase1(cfg, man, client), flush=True)
    print(f"phase1 seconds: {time.time() - t0:.0f}", flush=True)
    print("usage phase1:", json.dumps(client.usage_summary()), flush=True)
    cats = man.query("SELECT category, COUNT(*) AS n FROM items GROUP BY category")
    print("categories:", {r["category"]: r["n"] for r in cats}, flush=True)

    results = {}
    for model, tag in (("gemini-3.1-pro-preview", "pro"),
                       ("gemini-3.8-flash", "flash")):
        bdir = BASE / f"cal_work_{tag}"
        if bdir.exists():
            shutil.rmtree(bdir)
        shutil.copytree(work, bdir)
        bcfg = Config(done_dir=cfg.done_dir, work_dir=bdir, sorted_dir=cfg.sorted_dir)
        bman = Manifest(bcfg.manifest_path)
        results[tag] = branch(bcfg, bman, model, tag)
        print(f"{tag}: episodes={len(results[tag]['episodes'])} "
              f"seconds={results[tag]['seconds']} usage={json.dumps(results[tag]['usage'])}",
              flush=True)
    print("CALIBRATE DONE", flush=True)


if __name__ == "__main__":
    main()
