"""Crash-resume drill (backlog B10): a grouping run that dies
mid-way must corrupt nothing, and a re-run must converge.

Grouping's actual crash semantics, asserted here: item
assignments are written in one final pass, so a crash leaves
ZERO partial assignments; episode rows created by completed
windows linger span-less and inert (the ledger slice requires
a span, so they can never poison a later run).
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.gemini_client import StubClient
from album_sorter.grouping import run_grouping
from album_sorter.manifest import Manifest

N_ITEMS = 160   # two windows at size 150 / stride 75


def _setup(tmp: Path):
    cfg = Config(done_dir=tmp / "done", work_dir=tmp / "work",
                 sorted_dir=tmp / "sorted")
    man = Manifest(cfg.manifest_path)
    for i in range(N_ITEMS):
        man.upsert_item(
            md5=f"{i:032x}", done_name=f"f{i:03}.jpg", category="PHOTO",
            status="analyzed", capture_time=f"2026-07-01T10:{i // 60:02d}:{i % 60:02d}",
            capture_time_raw=f"2026-07-01T10:{i // 60:02d}:{i % 60:02d}",
            analysis_json=json.dumps({"description": f"photo {i}"}))
    return cfg, man


def _valid_handler(model, system, parts):
    records = json.loads(parts[0]["text"])["records"]
    n = len(records)
    return {"assignments": {str(i): "N1" for i in range(n)},
            "new_episodes": [{"temp_id": "N1", "description": "drill episode",
                              "exemplars": [0]}],
            "unassigned": [], "summary_update": "drill"}


def test_grouping_crash_then_resume():
    with tempfile.TemporaryDirectory() as td:
        cfg, man = _setup(Path(td))
        calls = {"n": 0}

        def crashing(model, system, parts):
            calls["n"] += 1
            if calls["n"] >= 2:
                # A process crash is a BaseException: B43's
                # per-window isolation catches model-call
                # failures (Exception - including an API dying
                # mid-run, which is now one window's failure,
                # not a crash) and deliberately lets a dying
                # process die. That death is what this drill
                # has always simulated.
                raise KeyboardInterrupt("simulated crash mid-run")
            return _valid_handler(model, system, parts)

        try:
            run_grouping(cfg, man, StubClient(crashing), cfg.work_dir)
            raise AssertionError("the crash should propagate")
        except KeyboardInterrupt:
            pass
        # atomicity: no item was assigned by the dead run
        assigned = man.query("SELECT COUNT(*) AS n FROM items WHERE episode_id IS NOT NULL")
        assert assigned[0]["n"] == 0
        # resume with a healthy client: converges completely
        stats = run_grouping(cfg, man, StubClient(_valid_handler), cfg.work_dir)
        assert stats["windows"] == 2
        rows = man.query("SELECT episode_id FROM items")
        assert all(r["episode_id"] for r in rows)
        valid_ids = {ep["episode_id"] for ep in man.all_episodes() if ep["span_start"]}
        assert {r["episode_id"] for r in rows} <= valid_ids
