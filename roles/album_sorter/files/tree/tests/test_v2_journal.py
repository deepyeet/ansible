"""B35 v2 verdict keys + the per-window grouping journal.

The principle under test (the user's, verbatim): every
token spent is never re-spent once output is obtained.
Derived state (spans, counts) growing around a verdict
must not orphan it; a death mid-grouping must re-pay at
most the in-flight window; pre-v2 banked verdicts must
keep working through their historical keys."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.gemini_client import StubClient
from album_sorter.grouping import run_grouping
from album_sorter.journal import call_extra, decision_key, image_extra
from album_sorter.linkage import VERIFIER_SYSTEM, try_link
from album_sorter.manifest import Manifest
from album_sorter.merge import run_merge
from album_sorter.rollup import run_rollup


def _item(man, md5, when, eid=None, status="grouped"):
    man.upsert_item(md5=md5, done_name=f"{md5}.jpg", capture_time=when,
                    capture_time_raw=when, episode_id=eid,
                    status=status, category="PHOTO",
                    analysis_json=json.dumps({"description": f"photo {md5}"}))


def _episode(man, desc, times, exemplars=("ex0",)):
    eid = man.create_episode(desc, list(exemplars))
    for i, t in enumerate(times):
        _item(man, f"{desc[:2]}-{eid}-{i}", t, eid=eid)
    man.recompute_episode_span(eid)
    return eid


# ---------------------------------------------------------------- linkage

def _linkage_fixture(td):
    cfg = Config(linkage_adjacency_days=10)
    man = Manifest(Path(td) / "m.sqlite3")
    eid = _episode(man, "lake day",
                   ["2026-05-01T10:00:00", "2026-05-02T10:00:00",
                    "2026-05-03T10:00:00"])
    _item(man, "loose-1", "2026-05-10T10:00:00", status="analyzed")
    item = man.query_one("SELECT * FROM items WHERE md5 = 'loose-1'")
    return cfg, man, eid, item


def test_linkage_v2_replays_across_span_growth():
    with tempfile.TemporaryDirectory() as td:
        cfg, man, eid, item = _linkage_fixture(td)
        client = StubClient(lambda m, s, p: {"verdict": "no",
                                             "confidence": 0.9,
                                             "reason": "different place"})
        assert try_link(cfg, man, client, item) is False
        assert len(client.calls) == 1
        rows = man.query("SELECT * FROM decisions WHERE stage = 'linkage'")
        assert len(rows) == 1
        ctx = json.loads(rows[0]["context_json"])
        assert ctx["contained"] is False and ctx["dist_days"] == 7.0
        # the episode grows toward the item (span May 1-9):
        # distance 7 -> 1, same containment - the verdict
        # replays, no new call (v1 would have re-bought it)
        _item(man, "grow-1", "2026-05-09T10:00:00", eid=eid)
        man.recompute_episode_span(eid)
        assert try_link(cfg, man, client, item) is False
        assert len(client.calls) == 1
        # growth swallows the item (containment flips): the
        # question materially changed - re-asked once, and
        # the row self-heals with the new context
        _item(man, "grow-2", "2026-05-12T10:00:00", eid=eid)
        man.recompute_episode_span(eid)
        assert try_link(cfg, man, client, item) is False
        assert len(client.calls) == 2
        rows = man.query("SELECT * FROM decisions WHERE stage = 'linkage'")
        assert len(rows) == 1
        assert json.loads(rows[0]["context_json"])["contained"] is True
        assert try_link(cfg, man, client, item) is False
        assert len(client.calls) == 2


def test_linkage_v1_verdict_still_replays():
    with tempfile.TemporaryDirectory() as td:
        cfg, man, eid, item = _linkage_fixture(td)
        ep = man.get_episode(eid)
        text_v1 = json.dumps({
            "episode_description": ep["description"],
            "episode_span": [ep["span_start"], ep["span_end"]],
            "item_time": item["capture_time"],
            "item_analysis": item["analysis_json"]})
        key1 = decision_key(
            "linkage", cfg.phase1_model_flash, VERIFIER_SYSTEM,
            {"text": text_v1, "item_md5": item["md5"],
             "exemplars": ["ex0"]},
            extra={**image_extra(cfg), **call_extra(cfg, "judgment")})
        man.record_decision(key1, "linkage", "item loose-1 -> episode",
                            {"verdict": "no", "confidence": 0.9})
        client = StubClient(lambda m, s, p: {"verdict": "yes",
                                             "confidence": 0.99})
        assert try_link(cfg, man, client, item) is False
        assert len(client.calls) == 0     # the banked v1 "no" stood


def test_linkage_never_attaches_into_gated_episode():
    with tempfile.TemporaryDirectory() as td:
        cfg, man, eid, item = _linkage_fixture(td)
        man.update_episode(eid, outcome="album_new")
        client = StubClient(lambda m, s, p: {"verdict": "yes",
                                             "confidence": 0.99})
        assert try_link(cfg, man, client, item) is False
        assert len(client.calls) == 0


# ---------------------------------------------------------------- merge

def test_merge_v2_replays_across_growth():
    with tempfile.TemporaryDirectory() as td:
        cfg = Config()
        man = Manifest(Path(td) / "m.sqlite3")
        a = _episode(man, "museum hall",
                     [f"2026-05-01T{h:02d}:00:00" for h in range(10, 15)])
        b = _episode(man, "museum annex",
                     [f"2026-05-01T{h:02d}:00:00" for h in range(15, 20)])
        client = StubClient(lambda m, s, p: {"same": False,
                                             "confidence": 0.95,
                                             "merged_description": "",
                                             "reason": "different wings"})
        stats = run_merge(cfg, man, client)
        assert stats["kept_separate"] == 1 and len(client.calls) == 1
        # modest growth inside the span (count 5 -> 7), placed
        # after the brief's first-3 exemplars so the judged
        # content is unchanged: replay
        _item(man, "grow-a1", "2026-05-01T13:30:00", eid=a)
        _item(man, "grow-a2", "2026-05-01T13:45:00", eid=a)
        man.recompute_episode_span(a)
        stats = run_merge(cfg, man, client)
        assert stats["replayed"] == 1 and len(client.calls) == 1
        # growth beyond the count bound: re-asked once
        for i in range(20):
            _item(man, f"grow-bulk-{i}", "2026-05-01T13:00:00", eid=a)
        man.recompute_episode_span(a)
        stats = run_merge(cfg, man, client)
        assert len(client.calls) == 2
        stats = run_merge(cfg, man, client)
        assert len(client.calls) == 2     # self-healed row replays


# ---------------------------------------------------------------- rollup

def test_rollup_v2_replays_across_growth():
    with tempfile.TemporaryDirectory() as td:
        cfg = Config()
        man = Manifest(Path(td) / "m.sqlite3")
        a = _episode(man, "home morning", ["2026-05-01T08:00:00",
                                           "2026-05-01T08:30:00",
                                           "2026-05-01T09:00:00"])
        b = _episode(man, "home afternoon", ["2026-05-02T09:00:00",
                                             "2026-05-02T10:00:00"])
        client = StubClient(lambda m, s, p: {"is_trip": False,
                                             "confidence": 0.9,
                                             "reason": "local life"})
        stats = run_rollup(cfg, man, client)
        assert stats["kept_separate"] == 1 and len(client.calls) == 1
        # a fourth item, after the brief's first-3 exemplars:
        # judged content unchanged, span drift half a day
        _item(man, "grow-r1", "2026-05-01T09:30:00", eid=a)
        man.recompute_episode_span(a)
        stats = run_rollup(cfg, man, client)
        assert stats["replayed"] == 1 and len(client.calls) == 1


# ---------------------------------------------------------------- grouping

def _drill_handler(model, system, parts):
    records = json.loads(parts[0]["text"])["records"]
    return {"assignments": {str(i): "N1" for i in range(len(records))},
            "new_episodes": [{"temp_id": "N1",
                              "description": "drill episode",
                              "exemplars": [0]}],
            "unassigned": [], "summary_update": "drill"}


def _drill_fixture(tmp):
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
    return cfg, man


def _state(man):
    eps = [(r["episode_id"], r["description"], r["span_start"],
            r["span_end"], r["item_count"])
           for r in man.query("SELECT * FROM episodes ORDER BY episode_id")]
    items = [(r["md5"], r["episode_id"])
             for r in man.query("SELECT * FROM items ORDER BY md5")]
    return eps, items


def test_window_journal_death_replays_completed_windows():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _drill_fixture(tmp / "control")
        run_grouping(cfg, man, StubClient(_drill_handler), cfg.work_dir)
        control = _state(man)

        cfg2, man2 = _drill_fixture(tmp / "drill")

        def dying(model, system, parts):
            records = json.loads(parts[0]["text"])["records"]
            if any(r["file"] == "f155.jpg" for r in records):
                # Process DEATH is a BaseException: B43's
                # per-window isolation catches model-call
                # failures (Exception) and deliberately lets a
                # dying process die - that is the crash this
                # drill has always simulated. (A model failure
                # mid-window no longer raises at all; see
                # test_grouping_isolation.py.)
                raise KeyboardInterrupt("death mid-window")
            return _drill_handler(model, system, parts)

        try:
            run_grouping(cfg2, man2, StubClient(dying), cfg2.work_dir)
            raise AssertionError("the dying run should have raised")
        except KeyboardInterrupt:
            pass
        rows = man2.query(
            "SELECT * FROM decisions WHERE stage = 'grouping_window'")
        assert len(rows) == 1     # window 0 banked; window 1 died in flight

        client = StubClient(_drill_handler)
        stats = run_grouping(cfg2, man2, client, cfg2.work_dir)
        assert stats.get("replayed_windows") == 1
        assert len(client.calls) == 1     # only window 1 was re-asked
        assert _state(man2) == control    # byte-identical outcome, ids too
        # a third run has nothing left to group (the drill
        # handler assigned every item): it returns on the
        # empty stream without touching the client
        def forbidden(model, system, parts):
            raise AssertionError("re-bought a completed grouping")
        stats = run_grouping(cfg2, man2, StubClient(forbidden),
                             cfg2.work_dir)
        assert stats["items"] == 0 and stats["windows"] == 0
