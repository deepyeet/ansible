"""Regression tests for the 2026-10-15 code review findings.
Each test names the finding it pins (F1, F2, F5, F8, F11, F14,
F15, F19, Q2)."""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter import grouping_logic as gl
from album_sorter.config import Config
from album_sorter.gates import apply_gate
from album_sorter.gemini_client import StubClient
from album_sorter.ingest import ingest
from album_sorter.manifest import Manifest
from album_sorter.merge import run_merge
from album_sorter.phase1 import run_phase1
from album_sorter.place import place_all, plan_placements
from album_sorter.rollup import candidate_chains


def _gate(man, cfg, gate_json, registry, n_items, when="2026-04-10T12:00:00"):
    eid = man.create_episode("an episode", [])
    for i in range(n_items):
        man.upsert_item(md5=f"{eid}-{i}", done_name=f"f{i}.jpg",
                        capture_time=when, capture_time_raw=when,
                        episode_id=eid, status="grouped", category="PHOTO")
    man.recompute_episode_span(eid)
    client = StubClient(lambda model, system, parts: gate_json)
    return apply_gate(man, cfg, client, registry, eid), man.get_episode(eid)


def test_f1_cross_year_near_dup_does_not_hijack():
    # registry holds LAST year's album only; a 2026 episode of
    # the same recurring event must not be filed into 2025's
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        outcome, ep = _gate(man, Config(),
                            {"registry_match": None, "match_confidence": 0.0,
                             "notability": "notable", "notability_confidence": 0.9,
                             "notability_reason": "annual festival",
                             "proposed_name": "Cherry Blossoms"},
                            [{"name": "2025 Cherry Blossoms"}], 30)
        assert outcome == "album_new" and ep["album_name"] == "2026 Cherry Blossoms"


def test_f1_same_year_registry_match_still_fires():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        outcome, ep = _gate(man, Config(),
                            {"registry_match": None, "match_confidence": 0.0,
                             "notability": "notable", "notability_confidence": 0.9,
                             "notability_reason": "annual festival",
                             "proposed_name": "Cherry Blossoms"},
                            [{"name": "2026 Cherry Blossoms"}], 30)
        assert outcome == "album_registry" and ep["album_name"] == "2026 Cherry Blossoms"


def test_f2_traversal_album_name_rejected():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        outcome, ep = _gate(man, Config(),
                            {"registry_match": None, "match_confidence": 0.0,
                             "notability": "notable", "notability_confidence": 0.95,
                             "notability_reason": "a trip",
                             "proposed_name": "../Escape"},
                            [], 30)
        assert outcome != "album_new"
        assert not ep["album_name"] or ("/" not in ep["album_name"]
                                        and ".." not in ep["album_name"])


def test_f8_phase1_poison_item_isolated():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = Config(done_dir=tmp / "done", work_dir=tmp / "work",
                     sorted_dir=tmp / "sorted")
        man = Manifest(cfg.manifest_path)
        for name in ("good.jpg", "bad.jpg"):
            man.upsert_item(md5=hashlib.md5(name.encode()).hexdigest(),
                            done_name=name, status="ingested", category=None,
                            capture_time="2026-07-01T10:00:00",
                            capture_time_raw="2026-07-01T10:00:00")

        def handler(model, system, parts):
            if '"bad.jpg"' in parts[0]["text"]:
                raise RuntimeError("response blocked")
            return {"category": "PHOTO", "confidence": 0.95,
                    "label": {"description": "a photo", "subjects": [],
                              "setting": "park", "activity": "",
                              "mood": "", "quality_issues": []}}

        stats = run_phase1(cfg, man, StubClient(handler))
        # B32 supersedes the terminal half of F8: a failed CALL
        # defers (the item stays retryable), it is not filed as
        # a permanent ERROR verdict. F8's intent stands - the
        # poison item does not wedge the batch.
        assert stats["processed"] == 2 and stats["deferred"] == 1 and stats["photo"] == 1
        bad = man.get_by_done_name("bad.jpg")
        assert bad["status"] == "ingested" and "blocked" in (bad["error"] or "")
        assert man.get_by_done_name("good.jpg")["status"] == "analyzed"
        # once the API recovers, the next run retries the
        # deferred item and clears its error
        stats = run_phase1(cfg, man, StubClient(
            lambda m, s, p: {"category": "PHOTO", "confidence": 0.95,
                             "label": {"description": "recovered"}}))
        assert stats["deferred"] == 0 and stats["photo"] == 1
        bad = man.get_by_done_name("bad.jpg")
        assert bad["status"] == "analyzed" and not bad["error"]


def test_f5_ingest_retries_after_error_row():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = Config(done_dir=tmp / "done", work_dir=tmp / "work",
                     sorted_dir=tmp / "sorted")
        cfg.done_dir.mkdir(parents=True)
        from PIL import Image
        Image.new("RGB", (64, 64), (10, 20, 30)).save(cfg.done_dir / "shot.jpg", "JPEG")
        man = Manifest(cfg.manifest_path)
        man.upsert_item(md5="err:shot.jpg", done_name="shot.jpg",
                        category="ERROR", status="triaged",
                        error="ffmpeg timed out (transient)")
        stats = ingest(cfg, man)
        assert stats["ingested"] == 1 and stats["skipped"] == 0
        assert man.get_by_done_name("shot.jpg")["md5"] != "err:shot.jpg"


def _place_setup(tmp: Path):
    cfg = Config(done_dir=tmp / "done", sorted_dir=tmp / "sorted", work_dir=tmp / "work")
    cfg.done_dir.mkdir(parents=True)
    return cfg, Manifest(cfg.manifest_path)


def _add(man, cfg, name, clean, content, **kw):
    digest = hashlib.md5(content).hexdigest()
    (cfg.done_dir / name).write_bytes(content)
    fields = dict(md5=digest, done_name=name, clean_name=clean, suffix_verified=1,
                  capture_time="2026-07-04T12:00:00", capture_time_raw="2026-07-04T12:00:00",
                  time_source="exif_dto", time_confidence="high", status="triaged")
    fields.update(kw)
    man.upsert_item(**fields)
    return digest


def test_f11_pending_episode_item_is_not_ready():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _place_setup(tmp)
        eid = man.create_episode("ungated episode", [])   # outcome stays pending
        _add(man, cfg, "p.jpg", "p.jpg", b"img", category="PHOTO",
             status="grouped", episode_id=eid)
        plan = plan_placements(cfg, man, cfg.done_dir)
        assert plan["counts"]["not_ready"] == 1 and plan["counts"]["move"] == 0
        assert place_all(cfg, man, cfg.done_dir)["placed"] == 0


def test_f19_plan_matches_run_on_clean_name_collision():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _place_setup(tmp)
        _add(man, cfg, "a.jpg", "same.jpg", b"content-one", category="MISC")
        _add(man, cfg, "b.jpg", "same.jpg", b"content-two", category="MISC")
        plan = plan_placements(cfg, man, cfg.done_dir)
        assert plan["counts"]["move"] == 1 and plan["counts"]["keep_suffix"] == 1
        stats = place_all(cfg, man, cfg.done_dir)
        assert stats["placed"] == 2 and stats["kept_suffix"] == 1
        assert (cfg.sorted_dir / "__Misc" / "same.jpg").read_bytes() == b"content-one"
        assert (cfg.sorted_dir / "__Misc" / "b.jpg").read_bytes() == b"content-two"


def test_f14_merge_three_fragments_across_passes():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")

        def ep(desc, start, end):
            eid = man.create_episode(desc, [])
            for i, t in enumerate((start, end)):
                man.upsert_item(md5=f"{eid}-{i}", done_name=f"f{eid}-{i}.jpg",
                                capture_time=t, capture_time_raw=t,
                                episode_id=eid, status="grouped", category="PHOTO")
            man.recompute_episode_span(eid)
            return eid

        a = ep("frag a", "2026-07-01T00:00:00", "2026-07-01T01:00:00")
        ep("frag b", "2026-07-01T06:00:00", "2026-07-01T07:00:00")
        c = ep("frag c", "2026-07-01T14:00:00", "2026-07-01T15:00:00")
        client = StubClient(lambda m, s, p: {"same": True, "confidence": 0.95,
                                             "merged_description": "whole outing"})
        stats = run_merge(Config(), man, client)
        assert stats["merged"] == 2
        assert man.get_episode(c)["outcome"] == "merged"
        total = man.query("SELECT COUNT(*) AS n FROM items WHERE episode_id = ?", (a,))
        assert total[0]["n"] == 6


def test_f15_rollup_chain_uses_latest_end():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")

        def ep(desc, start, end):
            eid = man.create_episode(desc, [])
            for i, t in enumerate((start, end)):
                man.upsert_item(md5=f"{eid}-{i}", done_name=f"f{eid}-{i}.jpg",
                                capture_time=t, capture_time_raw=t,
                                episode_id=eid, status="grouped", category="PHOTO")
            man.recompute_episode_span(eid)
            return eid

        container = ep("long stay", "2026-07-01T09:00:00", "2026-07-05T18:00:00")
        nested = ep("day trip inside", "2026-07-02T10:00:00", "2026-07-02T11:00:00")
        nxt = ep("outing", "2026-07-04T09:00:00", "2026-07-04T10:00:00")
        chains = candidate_chains(man, gap_hours=36.0, max_span_days=30.0)
        assert chains == [[container, nested, nxt]]


def test_q2_stride_must_be_below_size():
    try:
        gl.windows(300, 150, 150)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
