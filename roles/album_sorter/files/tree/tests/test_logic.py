"""Unit tests for the deterministic logic (no API, no fixtures
beyond synthetic data). Run: python -m pytest tests/ -q"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter import grouping_logic as gl
from album_sorter.clockfix import OffsetSample, estimate
from album_sorter.ingest import split_suffix
from album_sorter.metadata import filename_time


# ---- clock correction -------------------------------------------------------
def _samples(offsets_h, start=datetime(2026, 1, 1, 12, 0, 0)):
    return [OffsetSample(start + timedelta(hours=5 * i), h * 3600.0)
            for i, h in enumerate(offsets_h)]


def test_clock_constant_offset():
    c = estimate(_samples([8.02, 7.98, 8.0, 8.05, 7.99]))
    assert len(c.segments) == 1
    corrected = c.correct(datetime(2026, 1, 1, 12, 0, 0))
    assert abs((corrected - datetime(2026, 1, 1, 20, 0, 0)).total_seconds()) < 300


def test_clock_step_change_two_clusters():
    s = _samples([8.0, 8.01, 7.99, -11.0, -10.98, -11.02])
    c = estimate(s)
    assert len(c.segments) == 2
    early = c.correct(datetime(2026, 1, 1, 12, 0, 0))
    late = c.correct(datetime(2026, 1, 2, 12, 0, 0))
    assert early.hour == 20 and late.hour == 1


def test_clock_unanchored_too_few_samples():
    c = estimate(_samples([8.0, 8.1]))
    assert c.segments == []


def test_clock_scattered_samples_unanchored():
    c = estimate(_samples([1.0, 5.0, -3.0, 9.0, 0.5]))
    assert c.segments == []


# ---- ingest helpers -----------------------------------------------------------
def test_split_suffix():
    assert split_suffix("PXL_20260101_120000000_ab12cd34.jpg") == ("PXL_20260101_120000000", "ab12cd34")
    assert split_suffix("plain_name.jpg") == ("plain_name", None)


def test_filename_time_pxl():
    t = filename_time("PXL_20261013_162042229_c0e03342.jpg")
    assert t == datetime(2026, 10, 13, 16, 20, 42)


# ---- grouping logic -----------------------------------------------------------
def test_windows_cover_with_overlap():
    ws = gl.windows(400, 150, 75)
    covered = set()
    for s, e in ws:
        covered |= set(range(s, e))
    assert covered == set(range(400))
    assert ws[0] == (0, 150) and ws[-1][1] == 400


def test_validate_grouping_catches_double_count():
    out = gl.GroupingOutput(assignments={0: "1", 1: "1"}, unassigned=[1, 2])
    errors = gl.validate_grouping(out, 3, {"1"})
    assert any("twice" in e for e in errors)


def test_validate_grouping_catches_unknown_episode():
    out = gl.GroupingOutput(assignments={0: "99", 1: "99", 2: "99"})
    errors = gl.validate_grouping(out, 3, {"1"})
    assert any("unknown episode" in e for e in errors)


def test_validate_grouping_ok():
    out = gl.GroupingOutput(assignments={0: "N1", 1: "N1", 2: "1"},
                            new_episodes=[{"temp_id": "N1", "description": "zoo",
                                           "exemplars": [0]}])
    assert gl.validate_grouping(out, 3, {"1"}) == []


def test_reconcile_prefers_central_window():
    votes = {5: [gl.Vote("A", 0.2, 0), gl.Vote("B", 0.9, 1)]}
    assert gl.reconcile(votes)[5] == "B"

