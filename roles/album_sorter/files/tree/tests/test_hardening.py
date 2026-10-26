"""Adversarial hardening tests: malformed model output,
impossible filenames, and the full pipeline chain end to end
with stub clients (the integration seam unit tests can't see).
"""
import hashlib
import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter import grouping_logic as gl
from album_sorter.config import Config
from album_sorter.gemini_client import StubClient
from album_sorter.grouping import call_window
from album_sorter.metadata import filename_time


def test_filename_time_impossible_dates_degrade():
    assert filename_time("PXL_20261340_256070999.jpg") is None      # month 13, hour 25
    assert filename_time("Screenshot_20260230-101010.png") is None  # Feb 30
    assert filename_time("VID_99999999999999999999.mp4") is None     # absurd epoch
    assert filename_time("PXL_20260729_103000000.jpg") is not None   # sane still parses
    assert filename_time("random_name.jpg") is None


def test_grouping_fuzz_never_crashes_or_accepts_garbage():
    rng = random.Random(20261015)

    def garbage(model, system, parts):
        kind = rng.randrange(8)
        n = 5
        if kind == 0:
            return {"assignments": {str(i): "N1" for i in range(n)},
                    "new_episodes": [{"temp_id": "N1", "description": "x",
                                      "exemplars": [0]}],
                    "unassigned": [], "summary_update": ""}
        if kind == 1:   # string indices in unassigned
            return {"assignments": {"0": "N1"}, "new_episodes": [
                {"temp_id": "N1", "description": "x", "exemplars": [0]}],
                "unassigned": ["1", "2", "3", "4"], "summary_update": ""}
        if kind == 2:   # non-numeric assignment key
            return {"assignments": {"zero": "N1"}, "new_episodes": [],
                    "unassigned": [], "summary_update": ""}
        if kind == 3:   # duplicate accounting
            return {"assignments": {str(i): "N1" for i in range(n)},
                    "new_episodes": [{"temp_id": "N1", "description": "x",
                                      "exemplars": [0]}],
                    "unassigned": [0], "summary_update": ""}
        if kind == 4:   # unknown episode reference
            return {"assignments": {str(i): "999" for i in range(n)},
                    "new_episodes": [], "unassigned": [], "summary_update": ""}
        if kind == 5:   # exemplar out of window
            return {"assignments": {str(i): "N1" for i in range(n)},
                    "new_episodes": [{"temp_id": "N1", "description": "x",
                                      "exemplars": [99]}],
                    "unassigned": [], "summary_update": ""}
        if kind == 6:   # everything missing
            return {}
        # kind 7: float keys / weird types
        return {"assignments": {1.5: "N1"}, "new_episodes": None,
                "unassigned": None, "summary_update": None}

    for _ in range(60):
        client = StubClient(garbage)
        try:
            out, _retried = call_window(client, [{"text": "{}"}], 5, set())
            # a returned output must be FULLY valid by construction
            assert gl.validate_grouping(out, 5, set()) == []
        except ValueError:
            pass  # the sanctioned failure mode: rejected after one retry


def test_full_chain_end_to_end_with_stubs():
    """ingest -> phase1 -> grouping -> merge -> rollup -> gates
    -> dry-run -> place, over fabricated JPEGs. Asserts the
    final tree and that every PHOTO lands exactly once."""
    from PIL import Image
    from album_sorter import gates, grouping, ingest as ing, merge, phase1, place, rollup
    from album_sorter.manifest import Manifest

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = Config(done_dir=tmp / "done", work_dir=tmp / "work",
                     sorted_dir=tmp / "sorted")
        cfg.done_dir.mkdir(parents=True)
        # 6 photos across two days (a "trip") + 1 screenshot-ish misc
        names = []
        for i in range(6):
            day = "20260728" if i < 3 else "20260729"
            name = f"PXL_{day}_1000{i}0000_a{i:07x}.jpg"
            Image.new("RGB", (80, 60), (i * 30, 90, 40)).save(cfg.done_dir / name, "JPEG")
            names.append(name)
        Image.new("RGB", (80, 60), (0, 0, 0)).save(cfg.done_dir / "note_x.jpg", "JPEG")

        def phase1_handler(model, system, parts):
            meta = json.loads(parts[0]["text"])
            if meta["filename"].startswith("note"):
                return {"category": "MISC", "confidence": 0.95, "label": None}
            return {"category": "PHOTO", "confidence": 0.97,
                    "label": {"description": "museum hall", "subjects": ["art"],
                              "setting": "museum", "activity": "visiting",
                              "mood": "", "quality_issues": []}}

        def grouping_handler(model, system, parts):
            payload = json.loads(parts[0]["text"])
            recs = payload["records"]
            return {"assignments": {str(i): "N1" for i in range(len(recs))},
                    "new_episodes": [{"temp_id": "N1",
                                      "description": "museum visit over two days",
                                      "exemplars": [0]}],
                    "unassigned": [], "summary_update": "museum"}

        judge = lambda m, s, p: {"same": False, "confidence": 0.9}
        trip = lambda m, s, p: {"is_trip": False, "confidence": 0.9}
        gate = lambda m, s, p: {"registry_match": None, "match_confidence": 0.0,
                                "notability": "notable", "notability_confidence": 0.9,
                                "notability_reason": "a museum trip",
                                "proposed_name": "Museum"}

        def dispatch(model, system, parts):
            if "classify" in system or "sorting assistant" in system:
                return phase1_handler(model, system, parts)
            if "group" in system.lower() and "window" in system.lower() or "episode" in system.lower() and "assignments" in system:
                return grouping_handler(model, system, parts)
            return gate(model, system, parts)

        man = Manifest(cfg.manifest_path)
        assert ing.ingest(cfg, man)["errors"] == 0
        client = StubClient(phase1_handler)
        st = phase1.run_phase1(cfg, man, client)
        assert st["photo"] == 6
        gclient = StubClient(grouping_handler)
        gst = grouping.run_grouping(cfg, man, gclient, cfg.work_dir)
        assert gst["items"] == 6
        merge.run_merge(cfg, man, StubClient(judge))
        rollup.run_rollup(cfg, man, StubClient(trip))
        for ep in man.all_episodes():
            if ep["outcome"] == "pending":
                gates.apply_gate(man, cfg, StubClient(gate), [], ep["episode_id"])
        plan = place.plan_placements(cfg, man, cfg.done_dir)
        assert plan["counts"]["move"] == 7
        stats = place.place_all(cfg, man, cfg.done_dir)
        assert stats["placed"] == 7
        placed = sorted(p.name for p in cfg.sorted_dir.rglob("*.jpg"))
        assert len(placed) == 7
        album_files = list((cfg.sorted_dir / "2026 Museum").glob("*.jpg"))
        assert len(album_files) == 6           # suffix stripped at final placement
        assert (cfg.sorted_dir / "__Misc" / "note_x.jpg").exists()
