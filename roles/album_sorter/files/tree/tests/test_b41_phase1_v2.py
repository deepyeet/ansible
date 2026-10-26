"""B41 (IMPROVEMENT-PLAN S1): enriched phase-1 schema v2.

History: v2 as first built also carried a dominant_colors
field; the Phase D field-accuracy audit judged it 23%
incorrect (the model lists salient accent colors, not
coverage-dominant ones) and it was DROPPED before the
schema ever shipped - pinned below by its absence from
the prompt and from the grouping projection.

Pins: (1) the new label fields (scene,
distinctive_features) pass through apply_guards unchanged;
(2) run_phase1 stamps "v": 2 into the persisted label by
code, whatever the model emitted; (3) the batch-boundary
machinery - the manifest's phase1_schema_version meta stamp
is written on first write, never overwritten, and a
mismatching stamp with items still ingested warns loudly
in the run stats; (4) grouping's analysis projection
carries the new fields and the grouping fingerprint
reacts to PAYLOAD_VERSION; (5) the output-token cap is
the raised B41 value.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter import grouping, phase1
from album_sorter.config import Config
from album_sorter.gemini_client import StubClient
from album_sorter.manifest import Manifest
from album_sorter.windows import _analysis_projection, item_record

V2_LABEL = {
    "description": "Two kids at a snowy lakeside.",
    "objects": ["sled", "pine trees"],
    "people": {"count": 2,
               "description": "two children in red and blue parkas"},
    "place_description": "snow-covered lakeshore",
    "place_name_guess": None,
    "wider_location_guess": None,
    "event_description": None,
    "visible_text": None,
    "scene": {"setting": "outdoor", "environment": "snowy lakeside",
              "lighting": "bright daylight"},
    "distinctive_features": ["red sled with a rope handle"],
}


def _photo_handler(model, system, parts):
    return {"category": "PHOTO", "confidence": 0.95,
            "label": dict(V2_LABEL)}


def _one_item_manifest(tmp: Path, stamp: str | None = None) -> Manifest:
    man = Manifest(tmp / "m.sqlite3")
    man.upsert_item(md5="p1", done_name="p1.jpg",
                    capture_time="2026-07-15T12:00:00",
                    capture_time_raw="2026-07-15T12:00:00",
                    status="ingested")
    if stamp is not None:
        man.set_meta("phase1_schema_version", stamp)
    return man


def test_v2_fields_pass_through_guards_unchanged():
    raw = {"category": "PHOTO", "confidence": 0.95,
           "label": dict(V2_LABEL, v=2)}
    out = phase1.apply_guards(raw, 0.6)
    assert out["label"] == raw["label"]
    assert out["label"]["scene"]["environment"] == "snowy lakeside"
    assert out["label"]["distinctive_features"] == [
        "red sled with a rope handle"]


def test_run_phase1_stamps_v_and_schema_meta():
    # the handler's label carries NO "v" - the stamp is
    # code's job at persistence, not the model's
    assert "v" not in V2_LABEL
    with tempfile.TemporaryDirectory() as td:
        man = _one_item_manifest(Path(td))
        stats = phase1.run_phase1(Config(), man,
                                  StubClient(_photo_handler))
        assert stats["photo"] == 1 and "schema_warning" not in stats
        row = man.query("SELECT * FROM items WHERE md5 = 'p1'")[0]
        label = json.loads(row["analysis_json"])
        assert label["v"] == phase1.PHASE1_SCHEMA_VERSION == 2
        assert label["scene"]["setting"] == "outdoor"
        assert man.get_meta("phase1_schema_version") == "2"


def test_schema_stamp_mismatch_warns_and_stamp_stands():
    with tempfile.TemporaryDirectory() as td:
        man = _one_item_manifest(Path(td), stamp="1")
        stats = phase1.run_phase1(Config(), man,
                                  StubClient(_photo_handler))
        assert "schema_warning" in stats
        assert "v1" in stats["schema_warning"]
        # the stamp records what the batch STARTED under;
        # a mismatching run must not rewrite history
        assert man.get_meta("phase1_schema_version") == "1"


def test_matching_stamp_no_warning():
    with tempfile.TemporaryDirectory() as td:
        man = _one_item_manifest(Path(td), stamp="2")
        stats = phase1.run_phase1(Config(), man,
                                  StubClient(_photo_handler))
        assert "schema_warning" not in stats
        assert man.get_meta("phase1_schema_version") == "2"


def test_no_items_no_stamp_no_warning():
    # an empty run must not claim a schema version for a
    # batch that has not started
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        stats = phase1.run_phase1(Config(), man,
                                  StubClient(_photo_handler))
        assert "schema_warning" not in stats
        assert man.get_meta("phase1_schema_version") is None


def test_projection_carries_new_fields():
    proj = _analysis_projection(dict(V2_LABEL))
    assert proj["scene"] == V2_LABEL["scene"]
    assert proj["distinctive_features"] == ["red sled with a rope handle"]
    # the dropped field is not projected even when a label
    # (e.g. an experiment's) still carries it: the drop is
    # enforced at the projection, not just the prompt
    stray = dict(V2_LABEL, dominant_colors=["white", "blue"])
    assert "dominant_colors" not in _analysis_projection(stray)
    # and a v1 label (fields absent) projects exactly as before
    v1 = {k: v for k, v in V2_LABEL.items()
          if k not in ("scene", "distinctive_features")}
    proj1 = _analysis_projection(v1)
    assert "scene" not in proj1 and "distinctive_features" not in proj1


def test_grouping_fingerprint_reacts_to_payload_version():
    with tempfile.TemporaryDirectory() as td:
        man = _one_item_manifest(Path(td))
        man.update_item("p1", category="PHOTO", status="analyzed",
                        analysis_json=json.dumps(V2_LABEL))
        cfg = Config()
        fp_now = grouping.stream_fingerprint(cfg, man)
        saved = grouping.PAYLOAD_VERSION
        try:
            grouping.PAYLOAD_VERSION = 2
            fp2 = grouping.stream_fingerprint(cfg, man)
        finally:
            grouping.PAYLOAD_VERSION = saved
        # pinned literal moves with each payload bump:
        # v4 was B41's; B44 bumped to 5 (visible_text dedupe)
        assert grouping.PAYLOAD_VERSION == 5
        assert fp_now != fp2


def test_output_cap_raised_for_v2():
    assert Config().phase1_max_output_tokens == 1200


def test_system_prompt_is_v2():
    # dominant_colors was dropped after its audit failed;
    # the prompt must not ask for it (or name it at all)
    assert "dominant_colors" not in phase1.SYSTEM
    assert "distinctive_features" in phase1.SYSTEM
    assert "Terse prose, complete fields" in phase1.SYSTEM
    assert "quote signage exactly as written" in phase1.SYSTEM
    assert "Be terse." not in phase1.SYSTEM


def test_subject_verify_matches_on_pair_identity_only():
    # B41 Phase D: the s1-enriched arm's questions recompute
    # to keys that exist nowhere (the item text changed), so
    # sample membership rests on journal subject rows naming
    # the exact (item, episode) pair with a matching result
    import sqlite3
    import validate_linkage as vl
    assert "s1-enriched" in vl.ARMS
    assert vl.ARMS["s1-enriched"]["payload_mode"] == "images"
    assert vl.ARMS["s1-enriched"]["cfg_overrides"] == {}
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "m.sqlite3"
        man = Manifest(path)
        man.record_decision(
            "deadbeef", "linkage", "item m1 -> episode 3",
            {"verdict": "no", "confidence": 0.95},
            model="gemini-3.8-flash", prompt_hash="x")
        raw = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        raw.row_factory = sqlite3.Row
        q = {"md5": "m1", "episode_id": 3,
             "banked_verdict": "no", "banked_conf": 0.95}
        assert vl.subject_verify(raw, q) == "subject"
        # a different pair, or the same pair with a result
        # that does not match ground truth, does not verify
        assert vl.subject_verify(raw, {**q, "episode_id": 4}) is None
        assert vl.subject_verify(raw, {**q, "banked_conf": 0.5}) is None
        assert vl.subject_verify(raw, {**q, "banked_verdict": "yes"}) is None
