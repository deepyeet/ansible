"""B46: M1 (one exemplar-selection home, fresh membership)
+ M2 (gate-time episode brief aggregates).

M1: linkage, gates, merge and name_verify all select from
album_sorter/exemplars.py. Membership-derived consumers read
the episode's CURRENT members in canonical capture_time/md5
order with their historical caps and filters; linkage's
identity list keeps the stored grouping snapshot (grouping's
exemplars are model-chosen, not reproducible from
membership - the documented exception).

M2: apply_gate's payload gains an "episode_aggregates"
block computed by code from the current member labels:
present fields only, deterministic ranking, <= 600 chars.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.exemplars import (
    GATE_ANALYSIS_CAP, LINKAGE_IMAGE_CAP, MERGE_ANALYSIS_CAP,
    exemplar_analyses, member_items, stored_exemplars,
    verify_exemplars)
from album_sorter.gates import apply_gate, episode_aggregates
from album_sorter.gemini_client import StubClient
from album_sorter.manifest import Manifest
from album_sorter.merge import _episode_brief


def _man_with_members(specs):
    """specs: list of (md5, minute, label-or-None). Returns
    (tmp, man, eid) - caller keeps tmp alive via man path."""
    td = tempfile.TemporaryDirectory()
    man = Manifest(Path(td.name) / "m.sqlite3")
    eid = man.create_episode("an episode", [])
    for md5, minute, label in specs:
        man.upsert_item(
            md5=md5, done_name=f"{md5}.jpg",
            capture_time=f"2026-07-15T12:{minute:02d}:00",
            capture_time_raw=f"2026-07-15T12:{minute:02d}:00",
            episode_id=eid, status="grouped", category="PHOTO",
            analysis_json=json.dumps(label) if label is not None else None)
    man.recompute_episode_span(eid)
    return td, man, eid


def _label(minute, **kw):
    base = {"description": f"photo {minute}"}
    base.update(kw)
    return base


# ---- M1: determinism + caps -------------------------------

def test_selection_deterministic_same_members_same_picks():
    specs = [(f"m{i}", i, _label(i, visible_text="SIGN" if i % 3 == 0 else None))
             for i in range(1, 9)]
    _td, man, eid = _man_with_members(specs)
    items = member_items(man, eid)
    assert [i["md5"] for i in items] == [f"m{i}" for i in range(1, 9)]
    assert exemplar_analyses(man, eid, GATE_ANALYSIS_CAP, nonempty=True) == \
        exemplar_analyses(man, eid, GATE_ANALYSIS_CAP, nonempty=True)
    assert [i["md5"] for i in verify_exemplars(items)] == \
        [i["md5"] for i in verify_exemplars(items)]
    assert _episode_brief(man, eid) == _episode_brief(man, eid)


def test_per_consumer_caps_respected():
    specs = [(f"m{i}", i, _label(i, visible_text=f"SIGN {i}"))
             for i in range(1, 11)]
    _td, man, eid = _man_with_members(specs)
    assert len(exemplar_analyses(man, eid, GATE_ANALYSIS_CAP,
                                 nonempty=True)) == 5
    assert len(exemplar_analyses(man, eid, MERGE_ANALYSIS_CAP)) == 3
    assert len(verify_exemplars(member_items(man, eid))) == 4
    ep = man.get_episode(eid)
    man.update_episode(eid, exemplars=json.dumps([f"s{i}" for i in range(7)]))
    assert stored_exemplars(man.get_episode(eid)) == \
        [f"s{i}" for i in range(LINKAGE_IMAGE_CAP)]
    assert len(stored_exemplars(man.get_episode(eid), cap=None)) == 7
    assert ep is not None


def test_gate_and_merge_filter_semantics_preserved():
    # gates historically skipped falsy analyses (None AND
    # ""); merge's SQL skipped only NULL. The helper's
    # nonempty flag reproduces each exactly.
    specs = [("m1", 1, None), ("m2", 2, _label(2)), ("m3", 3, _label(3))]
    _td, man, eid = _man_with_members(specs)
    man.execute("UPDATE items SET analysis_json = '' WHERE md5 = 'm2'")
    gate_pick = exemplar_analyses(man, eid, GATE_ANALYSIS_CAP, nonempty=True)
    merge_pick = exemplar_analyses(man, eid, MERGE_ANALYSIS_CAP)
    assert len(gate_pick) == 1 and json.loads(gate_pick[0])["description"] == "photo 3"
    assert merge_pick[0] == "" and len(merge_pick) == 2


# ---- M1: freshness -----------------------------------------

def test_selection_fresh_when_membership_grows():
    specs = [("m2", 2, _label(2)), ("m3", 3, _label(3, visible_text="LATE SIGN"))]
    _td, man, eid = _man_with_members(specs)
    before_gate = exemplar_analyses(man, eid, GATE_ANALYSIS_CAP, nonempty=True)
    before_verify = [i["md5"] for i in verify_exemplars(member_items(man, eid))]
    # a new EARLIER member joins the episode after grouping;
    # no grouping re-run happens
    man.upsert_item(
        md5="m1", done_name="m1.jpg",
        capture_time="2026-07-15T12:01:00",
        capture_time_raw="2026-07-15T12:01:00",
        episode_id=eid, status="grouped", category="PHOTO",
        analysis_json=json.dumps(_label(1, visible_text="EARLY SIGN")))
    after_gate = exemplar_analyses(man, eid, GATE_ANALYSIS_CAP, nonempty=True)
    after_verify = [i["md5"] for i in verify_exemplars(member_items(man, eid))]
    assert json.loads(after_gate[0])["description"] == "photo 1"
    assert len(after_gate) == len(before_gate) + 1
    # verify: both text-bearing members lead, earliest first
    assert after_verify[:2] == ["m1", "m3"] and before_verify[0] == "m3"
    brief = _episode_brief(man, eid)
    assert json.loads(brief["exemplar_analyses"][0])["description"] == "photo 1"


def test_linkage_source_is_the_stored_snapshot_by_design():
    # grouping's stored exemplars are model-chosen; linkage's
    # identity list follows the SNAPSHOT even as membership
    # changes (B46 documented exception, not an oversight).
    _td, man, eid = _man_with_members([("m1", 1, _label(1))])
    man.update_episode(eid, exemplars=json.dumps(["stored-a", "stored-b"]))
    assert stored_exemplars(man.get_episode(eid)) == ["stored-a", "stored-b"]
    man.upsert_item(
        md5="m0", done_name="m0.jpg", capture_time="2026-07-15T12:00:00",
        capture_time_raw="2026-07-15T12:00:00", episode_id=eid,
        status="grouped", category="PHOTO",
        analysis_json=json.dumps(_label(0)))
    assert stored_exemplars(man.get_episode(eid)) == ["stored-a", "stored-b"]


# ---- M2: aggregates ----------------------------------------

def _v1(minute, **kw):
    base = {"description": f"v1 photo {minute}",
            "objects": ["chair"], "people": {"count": 1},
            "visible_text": None}
    base.update(kw)
    return base


def _v2(minute, **kw):
    base = {"v": 2, "description": f"v2 photo {minute}",
            "objects": ["tent"], "people": {"count": 2},
            "visible_text": None,
            "scene": {"setting": "outdoor", "environment": "lakeside",
                      "lighting": "daylight"}}
    base.update(kw)
    return base


def test_aggregates_v1_only_no_scene_key():
    items = [{"capture_time": "2026-07-15T12:01:00",
              "analysis_json": json.dumps(_v1(1, objects=["boat", "chair"],
                                              visible_text="MARINA"))},
             {"capture_time": "2026-07-15T12:02:00",
              "analysis_json": json.dumps(_v1(2, objects=["boat"],
                                              people={"count": 3}))},
             {"capture_time": "2026-07-15T12:03:00", "analysis_json": None}]
    agg = episode_aggregates(items)
    assert agg["member_count"] == 3
    assert agg["span"] == ["2026-07-15T12:01:00", "2026-07-15T12:03:00"]
    assert "scene_settings" not in agg          # v1 has no scene - omitted, never fabricated
    assert agg["top_objects"] == [["boat", 2], ["chair", 1]]
    assert agg["visible_text_samples"] == ["MARINA"]
    assert agg["people_count_range"] == [1, 3]


def test_aggregates_v2_scene_counts():
    items = [{"capture_time": "2026-07-15T12:01:00",
              "analysis_json": json.dumps(_v2(1))},
             {"capture_time": "2026-07-15T12:02:00",
              "analysis_json": json.dumps(_v2(2))},
             {"capture_time": "2026-07-15T12:03:00",
              "analysis_json": json.dumps(_v2(
                  3, scene={"setting": "indoor", "environment": "gym",
                            "lighting": "fluorescent"}))}]
    agg = episode_aggregates(items)
    assert agg["scene_settings"] == [["outdoor", 2], ["indoor", 1]]
    assert agg["top_objects"] == [["tent", 3]]
    assert agg["people_count_range"] == [2, 2]


def test_aggregates_mixed_episode_aggregates_what_exists():
    items = [{"capture_time": "2026-07-15T12:01:00",
              "analysis_json": json.dumps(_v1(1, objects=["dock"]))},
             {"capture_time": "2026-07-15T12:02:00",
              "analysis_json": json.dumps(_v2(2, objects=["dock"]))},
             {"capture_time": "2026-07-15T12:03:00",
              "analysis_json": "{not json"}]
    agg = episode_aggregates(items)
    assert agg["member_count"] == 3            # the malformed label still counts as a member
    assert agg["scene_settings"] == [["outdoor", 1]]   # only the v2 member contributes
    assert agg["top_objects"] == [["dock", 2]]
    assert agg["people_count_range"] == [1, 2]


def test_aggregates_no_labels_member_count_and_span_only():
    items = [{"capture_time": "2026-07-15T12:01:00", "analysis_json": None},
             {"capture_time": "2026-07-15T12:05:00", "analysis_json": None}]
    assert episode_aggregates(items) == {
        "member_count": 2,
        "span": ["2026-07-15T12:01:00", "2026-07-15T12:05:00"]}


def test_aggregates_deterministic_byte_identical():
    def build():
        return [{"capture_time": "2026-07-15T12:01:00",
                 "analysis_json": json.dumps(_v2(1, objects=["b", "a"]))},
                {"capture_time": "2026-07-15T12:02:00",
                 "analysis_json": json.dumps(_v2(2, objects=["a", "b"]))}]
    one = json.dumps(episode_aggregates(build()))
    two = json.dumps(episode_aggregates(build()))
    assert one == two
    # equal counts break ties by value ascending
    assert episode_aggregates(build())["top_objects"] == [["a", 2], ["b", 2]]


def test_aggregates_size_budget_holds():
    items = []
    for i in range(30):
        items.append({
            "capture_time": f"2026-07-15T12:{i:02d}:00",
            "analysis_json": json.dumps(_v2(
                i, objects=[f"object-number-{i:02d}-with-a-long-name"],
                visible_text=f"BANNER TEXT {i:02d} " + "X" * 50,
                scene={"setting": f"setting-{i:02d}",
                       "environment": "e", "lighting": "l"}))})
    agg = episode_aggregates(items)
    assert len(json.dumps(agg)) <= 600
    assert agg["member_count"] == 30
    assert len(agg["top_objects"]) <= 5
    assert len(agg["visible_text_samples"]) <= 3


def test_aggregates_fresh_when_membership_grows():
    specs = [("m1", 1, _v2(1)), ("m2", 2, _v2(2))]
    _td, man, eid = _man_with_members(specs)
    before = episode_aggregates(member_items(man, eid))
    man.upsert_item(
        md5="m3", done_name="m3.jpg", capture_time="2026-07-15T12:03:00",
        capture_time_raw="2026-07-15T12:03:00", episode_id=eid,
        status="grouped", category="PHOTO",
        analysis_json=json.dumps(_v2(3, objects=["kayak"])))
    after = episode_aggregates(member_items(man, eid))
    assert before["member_count"] == 2 and after["member_count"] == 3
    assert ["kayak", 1] in after["top_objects"]


# ---- M2: the gate payload carries the block -----------------

_GATE_JSON = {"registry_match": None, "match_confidence": 0.0,
              "notability": "routine", "notability_confidence": 0.9,
              "notability_reason": "everyday", "proposed_name": "Day",
              "name_basis": "generic", "generic_name": "Day"}


def test_gate_payload_includes_aggregates_and_stays_fresh():
    specs = [("m1", 1, _v2(1)), ("m2", 2, _v2(2))]
    _td, man, eid = _man_with_members(specs)
    seen = []

    def handler(model, system, parts):
        seen.append(json.loads(parts[0]["text"]))
        return dict(_GATE_JSON)

    client = StubClient(handler)
    outcome = apply_gate(man, Config(), client, [], eid)
    assert outcome == "month_bucket"
    agg = seen[0]["episode_aggregates"]
    assert agg["member_count"] == 2 and agg["scene_settings"] == [["outdoor", 2]]
    assert seen[0]["item_count"] == 2
    # membership grows; the next gate asks a NEW question
    # whose aggregates reflect the grown episode
    man.upsert_item(
        md5="m3", done_name="m3.jpg", capture_time="2026-07-15T12:03:00",
        capture_time_raw="2026-07-15T12:03:00", episode_id=eid,
        status="grouped", category="PHOTO",
        analysis_json=json.dumps(_v2(3)))
    man.update_episode(eid, outcome="pending")
    apply_gate(man, Config(), client, [], eid)
    assert len(seen) == 2
    assert seen[1]["episode_aggregates"]["member_count"] == 3
