"""B32 resume support: the decision journal, the grouping
checkpoint, transactional applies, and Phase 1 deferral.

The defect these pin: positive stage outcomes always
persisted in the manifest, but negative model verdicts -
merge "kept separate", rollup "not a trip", linkage "no" -
were stored nowhere, so any downstream failure (run 2's
Gemini 402 mid-linkage) re-bought every one on relaunch.
A stored verdict must now replay with ZERO model calls
while its question is unchanged, and must be re-asked the
moment any part of the question changes.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.gates import GATE_SYSTEM, VERIFY_SYSTEM, apply_gate
from album_sorter.gemini_client import StubClient, UnparseableOutput
from album_sorter.grouping import run_grouping
from album_sorter.linkage import try_link
from album_sorter.manifest import Manifest
from album_sorter.merge import run_merge
from album_sorter.phase1 import run_phase1
from album_sorter.rollup import run_rollup


def _episode(man, desc, start, end, n=2):
    eid = man.create_episode(desc, [])
    for i in range(n):
        t = start if i == 0 else end
        man.upsert_item(md5=f"{eid:04x}{i:028x}", done_name=f"e{eid}-{i}.jpg",
                        capture_time=t, capture_time_raw=t,
                        episode_id=eid, status="grouped", category="PHOTO",
                        analysis_json=json.dumps({"description": f"{desc} photo"}))
    man.recompute_episode_span(eid)
    return eid


def test_merge_negative_verdict_not_reasked():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        a = _episode(man, "museum hall", "2026-07-01T10:00:00", "2026-07-01T11:00:00")
        _episode(man, "museum garden", "2026-07-01T12:00:00", "2026-07-01T13:00:00")
        client = StubClient(lambda m, s, p: {"same": False, "confidence": 0.9,
                                             "reason": "different rooms"})
        stats = run_merge(Config(), man, client)
        assert stats["kept_separate"] == 1 and len(client.calls) == 1
        # relaunch: the same question replays from the journal
        stats = run_merge(Config(), man, client)
        assert stats["kept_separate"] == 1 and stats["replayed"] == 1
        assert len(client.calls) == 1
        # a changed episode is a changed question: asked fresh
        man.update_episode(a, description="museum hall, extended visit")
        stats = run_merge(Config(), man, client)
        assert stats["replayed"] == 0 and len(client.calls) == 2


def test_rollup_negative_verdict_not_reasked():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        _episode(man, "home day one", "2026-07-01T10:00:00", "2026-07-01T11:00:00")
        _episode(man, "home day two", "2026-07-02T10:00:00", "2026-07-02T11:00:00")
        client = StubClient(lambda m, s, p: {"is_trip": False, "confidence": 0.95,
                                             "reason": "local life"})
        stats = run_rollup(Config(), man, client)
        assert stats["kept_separate"] == 1 and len(client.calls) == 1
        stats = run_rollup(Config(), man, client)
        assert stats["kept_separate"] == 1 and stats["replayed"] == 1
        assert len(client.calls) == 1


def _linkage_setup(tmp: Path):
    man = Manifest(tmp / "m.sqlite3")
    eid = man.create_episode("museum visit", ["a" * 32])
    for i, t in enumerate(("2026-07-29T10:00:00", "2026-07-29T12:00:00")):
        man.upsert_item(md5=f"{i:032x}", done_name=f"in{i}.jpg",
                        capture_time=t, capture_time_raw=t,
                        episode_id=eid, status="grouped", category="PHOTO")
    man.recompute_episode_span(eid)
    man.upsert_item(md5="f" * 32, done_name="loose.jpg",
                    capture_time="2026-07-29T11:00:00",
                    capture_time_raw="2026-07-29T11:00:00",
                    status="analyzed", category="PHOTO")
    return man, eid


def test_linkage_negative_verdict_not_reasked():
    with tempfile.TemporaryDirectory() as td:
        man, _eid = _linkage_setup(Path(td))
        loose = man.get_by_md5("f" * 32)
        client = StubClient(lambda m, s, p: {"verdict": "no", "confidence": 0.9})
        assert try_link(Config(), man, client, loose) is False
        assert len(client.calls) == 1
        # relaunch after a downstream death: no re-buy
        stats = {}
        assert try_link(Config(), man, client, loose, stats) is False
        assert len(client.calls) == 1 and stats["replayed"] == 1


def test_linkage_replay_applies_current_threshold():
    with tempfile.TemporaryDirectory() as td:
        man, eid = _linkage_setup(Path(td))
        loose = man.get_by_md5("f" * 32)
        client = StubClient(lambda m, s, p: {"verdict": "yes", "confidence": 0.8})
        strict = Config()
        strict.verifier_attach_threshold = 0.9
        # the verdict is bought and journaled, but the strict
        # threshold declines to apply it
        assert try_link(strict, man, client, loose) is False
        assert len(client.calls) == 1
        # the operator relaxes the threshold: the stored
        # verdict now applies WITHOUT a new call
        relaxed = Config()
        relaxed.verifier_attach_threshold = 0.7
        assert try_link(relaxed, man, client, loose) is True
        assert len(client.calls) == 1
        assert man.get_by_md5("f" * 32)["episode_id"] == eid


def _grouping_handler(model, system, parts):
    # like a real grouping: most items land in an episode, a
    # few stay loose (f155-f159) - a leftover stream is what
    # a resume actually faces, and what the checkpoint check
    # (which sits behind the non-empty-stream path) governs
    records = json.loads(parts[0]["text"])["records"]
    assign, unassigned = {}, []
    for i, rec in enumerate(records):
        if rec["file"] in {f"f{n:03}.jpg" for n in range(155, 160)}:
            unassigned.append(i)
        else:
            assign[str(i)] = "N1"
    return {"assignments": assign,
            "new_episodes": [{"temp_id": "N1", "description": "drill episode",
                              "exemplars": [0]}],
            "unassigned": unassigned, "summary_update": "drill"}


def test_grouping_checkpoint_skips_then_invalidates():
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
        client = StubClient(_grouping_handler)
        stats = run_grouping(cfg, man, client, cfg.work_dir)
        assert stats["windows"] == 2 and len(client.calls) == 2
        # resume with unchanged inputs: grouping is skipped
        # outright - the client is never touched
        def forbidden(model, system, parts):
            raise AssertionError("grouping re-bought a completed stage")
        stats = run_grouping(cfg, man, StubClient(forbidden), cfg.work_dir)
        assert stats.get("skipped") == 1
        # a newly analyzed photo changes the fingerprint: the
        # designed regroup of the unassigned stream runs
        man.upsert_item(
            md5=f"{999:032x}", done_name="new.jpg", category="PHOTO",
            status="analyzed", capture_time="2026-07-01T10:30:00",
            capture_time_raw="2026-07-01T10:30:00",
            analysis_json=json.dumps({"description": "late arrival"}))
        stats = run_grouping(cfg, man, client, cfg.work_dir)
        assert not stats.get("skipped") and len(client.calls) == 3


def test_grouping_final_pass_is_atomic():
    """A crash DURING grouping's final pass (the B10 drill
    only covered a crash between windows) must leave zero
    partial assignments and no checkpoint - the pass, the
    span recomputes, and the checkpoint are one transaction."""
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
        original = man.recompute_episode_span

        def boom(episode_id):
            raise RuntimeError("simulated crash mid final pass")

        man.recompute_episode_span = boom
        try:
            run_grouping(cfg, man, StubClient(_grouping_handler), cfg.work_dir)
            raise AssertionError("the crash should propagate")
        except RuntimeError:
            pass
        finally:
            man.recompute_episode_span = original
        assigned = man.query(
            "SELECT COUNT(*) AS n FROM items WHERE episode_id IS NOT NULL")
        assert assigned[0]["n"] == 0
        assert man.get_checkpoint("grouping") is None
        # and a healthy rerun converges completely
        stats = run_grouping(cfg, man, StubClient(_grouping_handler), cfg.work_dir)
        assert stats["windows"] == 2
        assert man.get_checkpoint("grouping") is not None


def test_grouping_fingerprint_invalidated_by_time_and_device():
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
        run_grouping(cfg, man, StubClient(_grouping_handler), cfg.work_dir)

        def forbidden(model, system, parts):
            raise AssertionError("grouping re-bought a completed stage")

        stats = run_grouping(cfg, man, StubClient(forbidden), cfg.work_dir)
        assert stats.get("skipped") == 1

        def loose_handler(model, system, parts):
            n = len(json.loads(parts[0]["text"])["records"])
            return {"assignments": {}, "new_episodes": [],
                    "unassigned": list(range(n)), "summary_update": "loose"}

        # a clock correction rewrites one loose photo's time:
        # the fingerprint changes and grouping runs again
        loose = man.get_by_done_name("f155.jpg")
        man.update_item(loose["md5"], capture_time="2026-07-01T09:55:00")
        client = StubClient(loose_handler)
        stats = run_grouping(cfg, man, client, cfg.work_dir)
        assert not stats.get("skipped") and len(client.calls) == 1
        # settled again - until a metadata correction lands
        stats = run_grouping(cfg, man, StubClient(forbidden), cfg.work_dir)
        assert stats.get("skipped") == 1
        grouped = man.get_by_done_name("f000.jpg")
        man.update_item(grouped["md5"], device="Corrected Camera")
        stats = run_grouping(cfg, man, client, cfg.work_dir)
        # B35: the fingerprint is population-wide, so the
        # stage re-enters - but the device correction touched
        # an item that appears in no window payload (it is
        # grouped, not in the loose stream, and the ledger
        # slice carries no device), so the one window's
        # question is byte-identical and the window journal
        # replays it instead of re-buying it
        assert not stats.get("skipped") and len(client.calls) == 1
        assert stats.get("replayed_windows") == 1
        # a correction to the loose item ITSELF changes its
        # record, hence the payload, hence the key: re-asked
        man.update_item(loose["md5"], device="Corrected Camera")
        stats = run_grouping(cfg, man, client, cfg.work_dir)
        assert not stats.get("skipped") and len(client.calls) == 2


def test_merge_threshold_replay():
    """A journaled "same" verdict below today's confidence
    bar is stored, not applied; relaxing the bar applies
    the STORED verdict with no new call."""
    import album_sorter.merge as merge_mod
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        a = _episode(man, "museum hall", "2026-07-01T10:00:00", "2026-07-01T11:00:00")
        b = _episode(man, "museum hall cont", "2026-07-01T12:00:00", "2026-07-01T13:00:00")
        client = StubClient(lambda m, s, p: {"same": True, "confidence": 0.8,
                                             "merged_description": "whole visit"})
        saved = merge_mod.MERGE_CONFIDENCE
        try:
            merge_mod.MERGE_CONFIDENCE = 0.9
            stats = run_merge(Config(), man, client)
            assert stats["kept_separate"] == 1 and len(client.calls) == 1
            merge_mod.MERGE_CONFIDENCE = 0.7
            stats = run_merge(Config(), man, client)
            assert stats["merged"] == 1 and stats["replayed"] == 1
            assert len(client.calls) == 1
            assert man.get_episode(b)["outcome"] == "merged"
            assert man.get_episode(a)["item_count"] == 4
        finally:
            merge_mod.MERGE_CONFIDENCE = saved


def test_rollup_threshold_replay():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        _episode(man, "resort day one", "2026-07-01T10:00:00", "2026-07-01T11:00:00")
        b = _episode(man, "resort day two", "2026-07-02T10:00:00", "2026-07-02T11:00:00")
        client = StubClient(lambda m, s, p: {"is_trip": True, "confidence": 0.8,
                                            "merged_description": "the trip",
                                            "trip_name": "Resort Trip"})
        strict = Config()
        strict.rollup_confidence = 0.9
        stats = run_rollup(strict, man, client)
        assert stats["kept_separate"] == 1 and len(client.calls) == 1
        relaxed = Config()
        relaxed.rollup_confidence = 0.7
        stats = run_rollup(relaxed, man, client)
        assert stats["trips_formed"] == 1 and stats["replayed"] == 1
        assert len(client.calls) == 1
        assert man.get_episode(b)["outcome"] == "merged"


def test_regate_replays_until_purged():
    """B35 reversal of the B32 gate rule: a gate verdict,
    once bought, is never re-bought for the same question -
    a re-gate replays the stored verdict through the
    outcome rules. The deliberate re-judge path is the
    journal purge (cli decisions purge), after which a
    fresh verdict is bought and recorded."""
    with tempfile.TemporaryDirectory() as td:
        cfg, man, ep = _gate_fixture(Path(td))
        verdicts = [
            {"registry_match": None, "match_confidence": 0.0,
             "notability": "routine", "notability_confidence": 0.9,
             "notability_reason": "ordinary", "proposed_name": "Lake",
             "name_basis": "generic", "generic_name": "Lake"},
            {"registry_match": None, "match_confidence": 0.0,
             "notability": "notable", "notability_confidence": 0.95,
             "notability_reason": "destination outing",
             "proposed_name": "Lake Outing", "name_basis": "generic",
             "generic_name": "Lake Outing"},
        ]
        client = StubClient(lambda m, s, p: verdicts.pop(0))
        assert apply_gate(man, cfg, client, [], ep) == "month_bucket"
        assert len(client.calls) == 1
        rows = man.query("SELECT * FROM decisions WHERE stage = 'gate'")
        assert len(rows) == 1
        assert json.loads(rows[0]["result_json"])["outcome"] == "month_bucket"
        # re-gate the identical question: replay, no new call
        man.update_episode(ep, outcome="pending")
        assert apply_gate(man, cfg, client, [], ep) == "month_bucket"
        assert len(client.calls) == 1
        # the operator's deliberate re-judge: purge, then buy
        man.purge_decisions(stage="gate")
        man.update_episode(ep, outcome="pending")
        assert apply_gate(man, cfg, client, [], ep) == "album_new"
        assert len(client.calls) == 2
        rows = man.query("SELECT * FROM decisions WHERE stage = 'gate'")
        assert len(rows) == 1
        assert json.loads(rows[0]["result_json"])["outcome"] == "album_new"


def test_purge_cli():
    # driven as a subprocess, the way the CLI actually runs:
    # Config's env-based root defaults bind at import time,
    # so an in-process call cannot redirect them
    import os
    import subprocess
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        work = tmp / "work"
        man = Manifest(work / "manifest.sqlite3")
        man.record_decision("k-merge", "merge", "episodes 1+2", {"same": False})
        man.record_decision("k-link", "linkage", "item x -> episode 1",
                            {"verdict": "no"})
        man.record_decision("k-verify", "name_verify", "episode 1: Lake",
                            {"evidenced": False})
        man.set_checkpoint("grouping", "fp123", {"windows": 2})
        env = dict(os.environ,
                   ALBUM_DONE_DIR=str(tmp / "done"),
                   ALBUM_WORK_DIR=str(work),
                   ALBUM_SORTED_DIR=str(tmp / "sorted"))
        root = Path(__file__).resolve().parent.parent

        def run_cli(*argv):
            return subprocess.run(
                [sys.executable, "-m", "album_sorter.cli", *argv],
                cwd=root, env=env, capture_output=True, text=True)

        out = run_cli("decisions")
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout)["decisions_by_stage"]["merge"] == 1
        out = run_cli("purge", "--stage", "merge")
        assert out.returncode == 0, out.stderr
        counts = man.decision_counts()
        assert "merge" not in counts and counts.get("linkage") == 1
        assert man.get_checkpoint("grouping") is not None
        # gate purges name_verify with it (one operator stage)
        out = run_cli("purge", "--stage", "gate")
        assert out.returncode == 0, out.stderr
        assert "name_verify" not in man.decision_counts()
        out = run_cli("purge", "--stage", "grouping")
        assert out.returncode == 0, out.stderr
        assert man.get_checkpoint("grouping") is None
        out = run_cli("purge", "--stage", "bogus")
        assert out.returncode == 2


def test_transaction_rolls_back_journal_and_state_together():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        try:
            with man.transaction():
                man.record_decision("k1", "merge", "episodes 1+2",
                                    {"same": True})
                man.create_episode("doomed", [])
                raise RuntimeError("simulated crash mid-apply")
        except RuntimeError:
            pass
        # neither the verdict nor the state change survived:
        # the journal can never claim work the manifest lacks
        assert man.get_decision("k1") is None
        assert man.all_episodes() == []
        # and a clean transaction commits both
        with man.transaction():
            man.record_decision("k1", "merge", "episodes 1+2", {"same": True})
            man.create_episode("kept", [])
        assert man.get_decision("k1") is not None
        assert len(man.all_episodes()) == 1


def test_phase1_systemic_failure_defers_and_aborts():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = Config(done_dir=tmp / "done", work_dir=tmp / "work",
                     sorted_dir=tmp / "sorted")
        man = Manifest(cfg.manifest_path)
        import hashlib
        for name in ("a.jpg", "b.jpg"):
            man.upsert_item(md5=hashlib.md5(name.encode()).hexdigest(),
                            done_name=name,
                            status="ingested", category=None,
                            capture_time="2026-07-01T10:00:00",
                            capture_time_raw="2026-07-01T10:00:00")

        def down(model, system, parts):
            raise UnparseableOutput("service unavailable")

        try:
            run_phase1(cfg, man, StubClient(down))
            raise AssertionError("an all-fail phase 1 must abort the run")
        except RuntimeError as e:
            assert "all 2 attempted items failed" in str(e)
        # nothing was filed as a terminal verdict: both items
        # remain retryable, with the failure recorded
        for name in ("a.jpg", "b.jpg"):
            row = man.get_by_done_name(name)
            assert row["status"] == "ingested" and row["error"]


class _Fake402(Exception):
    """Transport-shaped credit death: with_retries fail-fasts
    on .code, and stage code must let it abort the pass."""
    code = 402


def test_full_chain_resume_after_linkage_death():
    """The B30 run-2 shape, end to end: grouping, merge and
    rollup complete; linkage judges one loose photo "no",
    then the API dies (402) on the next. The relaunch must
    re-buy NOTHING already decided - grouping skipped by
    checkpoint, the "no" replayed from the journal - and
    spend only on the one unjudged photo and the gates."""
    from album_sorter import gates as gates_mod
    from album_sorter.grouping import GROUPING_SYSTEM
    from album_sorter.linkage import VERIFIER_SYSTEM
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = Config(done_dir=tmp / "done", work_dir=tmp / "work",
                     sorted_dir=tmp / "sorted")
        man = Manifest(cfg.manifest_path)
        for i in range(6):
            man.upsert_item(
                md5=f"{i:032x}", done_name=f"f{i}.jpg", category="PHOTO",
                status="analyzed", capture_time=f"2026-07-01T10:0{i}:00",
                capture_time_raw=f"2026-07-01T10:0{i}:00",
                analysis_json=json.dumps({"description": f"photo {i}"}))

        def grouping_handler(model, system, parts):
            records = json.loads(parts[0]["text"])["records"]
            return {"assignments": {str(i): "N1" for i in range(4)},
                    "new_episodes": [{"temp_id": "N1",
                                      "description": "park outing",
                                      "exemplars": [0]}],
                    "unassigned": [4, 5], "summary_update": "park"}

        gate_out = {"registry_match": None, "match_confidence": 0.0,
                    "notability": "routine", "notability_confidence": 0.9,
                    "notability_reason": "ordinary park time",
                    "proposed_name": "Park", "name_basis": "generic",
                    "generic_name": "Park"}

        def run1_handler(model, system, parts):
            if system == GROUPING_SYSTEM:
                return grouping_handler(model, system, parts)
            if system == VERIFIER_SYSTEM:
                # first loose photo (10:04): a bought "no";
                # second (10:05): the API dies mid-stage,
                # as run 2's 402 did
                if "10:04" in parts[0]["text"]:
                    return {"verdict": "no", "confidence": 0.9}
                # a REAL 402 reaches try_link as an APIError
                # (fail-fast in with_retries), never as
                # UnparseableOutput - F-3 isolates semantic
                # failures per-question, so this drill must
                # simulate death with the transport shape or
                # it tests a death that cannot happen
                raise _Fake402("credits depleted")
            raise AssertionError(f"unexpected system: {system[:30]}")

        client1 = StubClient(run1_handler)
        run_grouping(cfg, man, client1, cfg.work_dir)
        run_merge(cfg, man, client1)
        run_rollup(cfg, man, client1)
        loose = man.query("SELECT * FROM items WHERE category = 'PHOTO' "
                          "AND episode_id IS NULL AND status = 'analyzed' "
                          "ORDER BY capture_time, done_name")
        assert len(loose) == 2
        try:
            for it in loose:
                try_link(cfg, man, client1, it)
            raise AssertionError("the simulated 402 should kill the stage")
        except _Fake402:
            pass
        assert man.decision_counts().get("linkage") == 1

        # relaunch with a healthy, counting client
        counts = {"grouping": 0, "verifier": 0, "gate": 0}

        def run2_handler(model, system, parts):
            if system == GROUPING_SYSTEM:
                counts["grouping"] += 1
                return grouping_handler(model, system, parts)
            if system == VERIFIER_SYSTEM:
                counts["verifier"] += 1
                return {"verdict": "yes", "confidence": 0.95}
            if system == gates_mod.GATE_SYSTEM:
                counts["gate"] += 1
                return dict(gate_out)
            raise AssertionError(f"unexpected system: {system[:30]}")

        client2 = StubClient(run2_handler)
        gstats = run_grouping(cfg, man, client2, cfg.work_dir)
        assert gstats.get("skipped") == 1 and counts["grouping"] == 0
        mstats = run_merge(cfg, man, client2)
        rstats = run_rollup(cfg, man, client2)
        assert mstats["replayed"] == 0 and rstats["replayed"] == 0
        loose = man.query("SELECT * FROM items WHERE category = 'PHOTO' "
                          "AND episode_id IS NULL AND status = 'analyzed' "
                          "ORDER BY capture_time, done_name")
        lstats = {"called": 0, "replayed": 0}
        attached = sum(1 for it in loose
                       if try_link(cfg, man, client2, it, lstats))
        # the journaled "no" replayed for free; exactly one
        # fresh call bought the surviving photo's verdict
        assert lstats["replayed"] == 1 and lstats["called"] == 1
        assert counts["verifier"] == 1 and attached == 1
        for ep in man.all_episodes():
            if ep["outcome"] == "pending":
                gates_mod.apply_gate(cfg=cfg, man=man, client=client2,
                                     registry=[], episode_id=ep["episode_id"])
        assert counts["gate"] == 1
        assert man.get_by_done_name("f5.jpg")["episode_id"] is not None
        assert man.get_by_done_name("f4.jpg")["episode_id"] is None


GATE_OUT = {"registry_match": None, "match_confidence": 0.0,
            "notability": "notable", "notability_confidence": 0.9,
            "notability_reason": "a destination outing",
            "proposed_name": "Rattlesnake Lake",
            "name_basis": "famous_landmark",
            "generic_name": "Lake Outing"}


def _gate_fixture(tmp: Path):
    cfg = Config(done_dir=tmp / "done", work_dir=tmp / "work",
                 sorted_dir=tmp / "sorted")
    man = Manifest(cfg.manifest_path)
    ep = man.create_episode("family outing to a lake with stumps", ["0" * 32])
    for i in range(6):
        man.upsert_item(md5=f"{i:032x}", done_name=f"p{i}.jpg", category="PHOTO",
                        status="grouped", episode_id=ep,
                        capture_time=f"2026-08-12T1{i}:00:00",
                        capture_time_raw=f"2026-08-12T1{i}:00:00",
                        analysis_json='{"description": "lake"}')
    man.update_episode(ep, span_start="2026-08-12T10:00:00",
                       span_end="2026-08-12T15:00:00", item_count=6)
    return cfg, man, ep


def test_gate_journals_verdict_and_failed_verify_is_not_a_verdict():
    with tempfile.TemporaryDirectory() as td:
        cfg, man, ep = _gate_fixture(Path(td))

        def handler(model, system, parts):
            if system == VERIFY_SYSTEM:
                raise UnparseableOutput("verify service down")
            assert system == GATE_SYSTEM
            return dict(GATE_OUT)

        out = apply_gate(man, cfg, StubClient(handler), [], ep)
        row = man.get_episode(ep)
        # the failed verification still resolves safely to the
        # generic fallback (B27 semantics unchanged)...
        assert out == "album_new" and row["album_name"] == "2026 Lake Outing"
        counts = man.decision_counts()
        # ...the gate verdict itself is journaled with its
        # outcome for provenance...
        assert counts.get("gate") == 1
        # ...but the FAILED verification is not journaled as
        # a negative verdict - a later run re-asks it
        assert "name_verify" not in counts


def test_name_verify_negative_verdict_is_journaled_and_replayed():
    with tempfile.TemporaryDirectory() as td:
        cfg, man, ep = _gate_fixture(Path(td))
        calls = {"verify": 0}

        def handler(model, system, parts):
            if system == VERIFY_SYSTEM:
                calls["verify"] += 1
                return {"evidenced": False, "confidence": 0.9,
                        "evidence": "generic shoreline"}
            return dict(GATE_OUT)

        client = StubClient(handler)
        assert apply_gate(man, cfg, client, [], ep) == "album_new"
        assert calls["verify"] == 1
        assert man.decision_counts().get("name_verify") == 1
        # an operator resets the episode for a re-gate: the
        # stored verification replays with no new call
        man.update_episode(ep, outcome="pending", album_name=None)
        assert apply_gate(man, cfg, client, [], ep) == "album_new"
        assert calls["verify"] == 1
        assert man.get_episode(ep)["album_name"] == "2026 Lake Outing"
