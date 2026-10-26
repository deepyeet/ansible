"""B43: per-window failure isolation for grouping.

One window whose model call fails - EmptyResponse (a
RECITATION-filter empty, observed live 2026-10-23),
UnparseableOutput surviving call_window's retry-with-errors,
or a transport error that survived with_retries - is that
window's failure, not the run's: the run completes, the
failure is recorded in the run stats, NOTHING is journaled
for the window, its items stay unassigned, and the completion
checkpoint is withheld so a later run re-asks. Mirrors
linkage's F-3 semantics; the run's own spend cap
(BudgetExceeded) still stops the run.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.gemini_client import (BudgetExceeded, EmptyResponse,
                                        StubClient, UnparseableOutput)
from album_sorter.grouping import run_grouping
from album_sorter.manifest import Manifest


def _fixture(tmp):
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


def _handler(fail_marker=None, fail_exc=None):
    """One new episode per window; raises fail_exc for any
    window whose records include fail_marker. The default
    windows over 160 items are [0:150] and [75:160], so
    f155.jpg is seen by window 1 only."""
    def h(model, system, parts):
        records = json.loads(parts[0]["text"])["records"]
        if fail_marker and any(r["file"] == fail_marker for r in records):
            raise fail_exc
        return {"assignments": {str(i): "N1" for i in range(len(records))},
                "new_episodes": [{"temp_id": "N1",
                                  "description": "isolation episode",
                                  "exemplars": [0]}],
                "unassigned": [], "summary_update": "iso"}
    return h


def _unassigned_md5s(man):
    return {r["md5"] for r in man.query(
        "SELECT md5 FROM items WHERE episode_id IS NULL")}


def _journal_rows(man):
    return man.query(
        "SELECT * FROM decisions WHERE stage = 'grouping_window'")


def test_empty_response_window_isolated_and_reasked_next_run():
    with tempfile.TemporaryDirectory() as td:
        cfg, man = _fixture(Path(td))
        bad = StubClient(_handler(
            "f155.jpg",
            EmptyResponse("empty model response (possibly safety-blocked)")))
        stats = run_grouping(cfg, man, bad, cfg.work_dir)
        # the run COMPLETES: window 0 banked, window 1 recorded
        assert stats["windows"] == 1 and stats["failed_windows"] == 1
        (fail,) = stats["windows_failed"]
        assert fail["window"] == 1 and fail["range"] == [75, 160]
        assert fail["first_md5"] == f"{75:032x}"
        assert fail["error"] == "EmptyResponse"
        # one attempt per window - an empty is a completed,
        # billed generation and is never transport-retried
        assert len(bad.calls) == 2
        # nothing journaled for the failed window
        assert len(_journal_rows(man)) == 1
        # items only window 1 covered stay unassigned
        assert _unassigned_md5s(man) == {f"{i:032x}" for i in range(150, 160)}
        # no completion checkpoint: the stage did not complete,
        # so the next run re-enters instead of skipping
        assert man.get_checkpoint("grouping") is None
        # a healthy run re-asks the leftover stream (exactly
        # one call), completes coverage, and lands the checkpoint
        good = StubClient(_handler())
        stats2 = run_grouping(cfg, man, good, cfg.work_dir)
        assert stats2["failed_windows"] == 0 and len(good.calls) == 1
        assert _unassigned_md5s(man) == set()
        assert len(_journal_rows(man)) == 2
        assert man.get_checkpoint("grouping") is not None


def test_unparseable_window_isolated_after_its_one_retry():
    with tempfile.TemporaryDirectory() as td:
        cfg, man = _fixture(Path(td))
        bad = StubClient(_handler(
            "f155.jpg",
            UnparseableOutput("model output is not valid JSON: Expecting value")))
        stats = run_grouping(cfg, man, bad, cfg.work_dir)
        assert stats["windows"] == 1 and stats["failed_windows"] == 1
        (fail,) = stats["windows_failed"]
        # call_window spent its one retry-with-errors and
        # surfaced the failure as ValueError
        assert fail["error"] == "ValueError"
        assert "unparseable after one retry" in fail["message"]
        # window 0 once + window 1's attempt and its single retry
        assert len(bad.calls) == 3
        assert len(_journal_rows(man)) == 1
        assert _unassigned_md5s(man) == {f"{i:032x}" for i in range(150, 160)}
        assert man.get_checkpoint("grouping") is None


class _FailFast(Exception):
    """SDK-shaped client error: a 4xx code makes with_retries
    fail fast and hand the raw error to the caller."""
    code = 400


def test_transport_error_surviving_retries_is_isolated():
    with tempfile.TemporaryDirectory() as td:
        cfg, man = _fixture(Path(td))
        bad = StubClient(_handler("f155.jpg", _FailFast("bad request")))
        stats = run_grouping(cfg, man, bad, cfg.work_dir)
        assert stats["windows"] == 1 and stats["failed_windows"] == 1
        (fail,) = stats["windows_failed"]
        assert fail["window"] == 1 and fail["error"] == "_FailFast"
        assert len(bad.calls) == 2      # fail-fast: no retries burned
        assert len(_journal_rows(man)) == 1
        assert man.get_checkpoint("grouping") is None


def test_spend_cap_still_stops_the_run():
    with tempfile.TemporaryDirectory() as td:
        cfg, man = _fixture(Path(td))
        bad = StubClient(_handler("f155.jpg", BudgetExceeded("spend cap")))
        try:
            run_grouping(cfg, man, bad, cfg.work_dir)
            raise AssertionError("BudgetExceeded must stop the run")
        except BudgetExceeded:
            pass
        # window 0 banked before the cap stopped window 1
        assert len(_journal_rows(man)) == 1
