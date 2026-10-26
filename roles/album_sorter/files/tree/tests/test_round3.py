"""Round-3 tests: same-run ledger visibility (B20), placement
source-integrity (src_changed), overlong gate names."""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.gates import apply_gate
from album_sorter.gemini_client import StubClient
from album_sorter.grouping import run_grouping
from album_sorter.manifest import Manifest
from album_sorter.place import place_all, plan_placements


def test_b20_second_window_extends_first_windows_episode():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = Config(done_dir=tmp / "done", work_dir=tmp / "work",
                     sorted_dir=tmp / "sorted")
        man = Manifest(cfg.manifest_path)
        for i in range(160):
            man.upsert_item(
                md5=f"{i:032x}", done_name=f"f{i:03}.jpg", category="PHOTO",
                status="analyzed",
                capture_time=f"2026-07-01T10:{i // 60:02d}:{i % 60:02d}",
                capture_time_raw=f"2026-07-01T10:{i // 60:02d}:{i % 60:02d}",
                analysis_json=json.dumps({"description": f"photo {i}"}))
        seen_ledgers = []

        def handler(model, system, parts):
            payload = json.loads(parts[0]["text"])
            ledger = payload["ledger"]
            seen_ledgers.append([e["episode_id"] for e in ledger])
            n = len(payload["records"])
            if ledger:
                ref = str(ledger[0]["episode_id"])
                return {"assignments": {str(i): ref for i in range(n)},
                        "new_episodes": [], "unassigned": [],
                        "summary_update": "extending"}
            return {"assignments": {str(i): "N1" for i in range(n)},
                    "new_episodes": [{"temp_id": "N1", "description": "one outing",
                                      "exemplars": [0]}],
                    "unassigned": [], "summary_update": "outing"}

        stats = run_grouping(cfg, man, StubClient(handler), cfg.work_dir)
        assert stats["windows"] == 2
        assert seen_ledgers[0] == [] and seen_ledgers[1] == [1]  # visible in window 2
        eps = [e for e in man.all_episodes()]
        assert len(eps) == 1 and eps[0]["item_count"] == 160


def test_src_changed_blocks_placement():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = Config(done_dir=tmp / "done", sorted_dir=tmp / "sorted",
                     work_dir=tmp / "work")
        cfg.done_dir.mkdir(parents=True)
        man = Manifest(cfg.manifest_path)
        content = b"original-content"
        (cfg.done_dir / "f.jpg").write_bytes(content)
        man.upsert_item(md5=hashlib.md5(content).hexdigest(), done_name="f.jpg",
                        clean_name="f.jpg", suffix_verified=1,
                        capture_time="2026-07-04T12:00:00",
                        capture_time_raw="2026-07-04T12:00:00",
                        file_size=len(content), status="triaged", category="MISC")
        # the file is replaced by a longer re-export after ingest
        (cfg.done_dir / "f.jpg").write_bytes(b"a different, longer re-export")
        plan = plan_placements(cfg, man, cfg.done_dir)
        assert plan["counts"]["src_changed"] == 1 and plan["counts"]["move"] == 0
        stats = place_all(cfg, man, cfg.done_dir)
        assert stats["placed"] == 0 and stats["src_changed"] == 1
        assert (cfg.done_dir / "f.jpg").exists()      # left in place


def test_gate_overlong_proposed_name_degrades():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        eid = man.create_episode("resort stay", [])
        for i in range(20):
            man.upsert_item(md5=f"{eid}-{i}", done_name=f"f{i}.jpg",
                            capture_time="2026-07-29T12:00:00",
                            capture_time_raw="2026-07-29T12:00:00",
                            episode_id=eid, status="grouped", category="PHOTO")
        man.recompute_episode_span(eid)
        long_name = ("Van Gogh Exhibit A resort vacation featuring hotel "
                     "views of a massive pool complex and lazy river")
        client = StubClient(lambda m, s, p: {
            "registry_match": None, "match_confidence": 0.0,
            "notability": "notable", "notability_confidence": 0.95,
            "notability_reason": "a trip", "proposed_name": long_name})
        outcome = apply_gate(man, Config(), client, [], eid)
        ep = man.get_episode(eid)
        assert outcome == "month_bucket" and ep["gate_character"] == "borderline"
        assert ep["album_name"] is None
