"""Gate tests: dumping-ground albums are never matches, and
notability (not item count) decides new albums (v1.4)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.gates import apply_gate, is_bucket_name, registry_names_for_years
from album_sorter.gemini_client import StubClient
from album_sorter.manifest import Manifest


def test_bucket_names_detected():
    for name in ("2026 Snaps", "2008 Law Snaps", "2011 No EXIF Law Snaps",
                 "2024 December", "2025 January"):
        assert is_bucket_name(name), name


def test_event_names_not_buckets():
    for name in ("2026 Monster Jam", "2025 Cherry Blossoms",
                 "2024 Baby Estelle", "__Misc", "Medical"):
        assert not is_bucket_name(name), name


def test_registry_filter_excludes_buckets():
    reg = [{"name": "2026 Snaps"}, {"name": "2026 Monster Jam"},
           {"name": "2025 Snaps"}, {"name": "2026 December"}]
    names = registry_names_for_years(reg, {2026})
    assert names == ["2026 Monster Jam"]


def _gate_outcome(gate_json, n_items):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        man = Manifest(tmp / "m.sqlite3")
        eid = man.create_episode("an episode", [])
        for i in range(n_items):
            man.upsert_item(md5=f"{eid}-{i}", done_name=f"f{i}.jpg",
                            capture_time="2026-07-15T12:00:00",
                            capture_time_raw="2026-07-15T12:00:00",
                            episode_id=eid, status="grouped", category="PHOTO")
        man.recompute_episode_span(eid)
        client = StubClient(lambda model, system, parts: gate_json)
        outcome = apply_gate(man, Config(), client, [], eid)
        return outcome, man.get_episode(eid)


def test_notable_small_episode_becomes_album():
    # 40 photos of a Japan trip: an album (count is not the rule)
    outcome, ep = _gate_outcome(
        {"registry_match": None, "match_confidence": 0.0,
         "notability": "notable", "notability_confidence": 0.9,
         "notability_reason": "a trip abroad", "proposed_name": "Japan Trip"}, 40)
    assert outcome == "album_new" and ep["album_name"] == "2026 Japan Trip"


def test_routine_huge_episode_stays_month_bucket():
    # 300 photos of kids eating dinner: not an album at any count
    outcome, ep = _gate_outcome(
        {"registry_match": None, "match_confidence": 0.0,
         "notability": "routine", "notability_confidence": 0.95,
         "notability_reason": "everyday meals at home",
         "proposed_name": "Kids Dinner"}, 300)
    assert outcome == "month_bucket" and ep["gate_character"] == "routine"


def test_borderline_notability_flagged_not_album():
    outcome, ep = _gate_outcome(
        {"registry_match": None, "match_confidence": 0.0,
         "notability": "notable", "notability_confidence": 0.5,
         "notability_reason": "a nice hike, maybe an outing",
         "proposed_name": "Lake Hike"}, 41)
    assert outcome == "month_bucket" and ep["gate_character"] == "borderline"
    assert ep["notability_reason"] == "a nice hike, maybe an outing"


def test_notable_fragment_is_flagged_not_album():
    # a 2-photo fragment of a trip (hotel window view): notable
    # content, but a fragment is not an occasion - borderline,
    # not a 2-photo album (live finding, 2026-10-15)
    outcome, ep = _gate_outcome(
        {"registry_match": None, "match_confidence": 0.0,
         "notability": "notable", "notability_confidence": 0.9,
         "notability_reason": "travel and hotel stay",
         "proposed_name": "Las Vegas Trip"}, 2)
    assert outcome == "month_bucket" and ep["gate_character"] == "borderline"


def test_registry_exact_name_still_wins():
    # proposed name exactly equals a registry album for the year:
    # code-side name safety resolves it to a registry match even
    # when the model reports no match and calls it routine
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        man = Manifest(tmp / "m.sqlite3")
        eid = man.create_episode("monster trucks", [])
        for i in range(12):
            man.upsert_item(md5=f"r-{i}", done_name=f"r{i}.jpg",
                            capture_time="2026-01-15T12:00:00",
                            capture_time_raw="2026-01-15T12:00:00",
                            episode_id=eid, status="grouped", category="PHOTO")
        man.recompute_episode_span(eid)
        client = StubClient(lambda model, system, parts: {
            "registry_match": None, "match_confidence": 0.0,
            "notability": "routine", "notability_confidence": 0.9,
            "notability_reason": "", "proposed_name": "Monster Jam"})
        registry = [{"name": "2026 Monster Jam", "items": 50}]
        outcome = apply_gate(man, Config(), client, registry, eid)
        assert outcome == "album_registry"
        assert man.get_episode(eid)["album_name"] == "2026 Monster Jam"
