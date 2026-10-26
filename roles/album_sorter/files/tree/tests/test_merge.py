"""Merge-pass tests: seam-split detection and application."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.manifest import Manifest
from album_sorter.merge import apply_merge, candidate_pairs


def _setup(tmp: Path):
    man = Manifest(tmp / "m.sqlite3")
    return man


def _episode(man, desc, start, end, n_items):
    eid = man.create_episode(desc, [])
    man.update_episode(eid, span_start=start, span_end=end, item_count=n_items)
    for i in range(n_items):
        # item times span the episode: first at start, last at end
        # (episode spans are derived from member item times)
        t = end if (n_items > 1 and i == n_items - 1) else start
        man.upsert_item(md5=f"{eid}-{i}", done_name=f"f{eid}-{i}.jpg",
                        capture_time=t, capture_time_raw=t,
                        episode_id=eid, status="grouped", category="PHOTO")
    man.recompute_episode_span(eid)
    return eid


def test_candidates_only_for_close_spans():
    with tempfile.TemporaryDirectory() as td:
        man = _setup(Path(td))
        a = _episode(man, "museum part 1", "2026-07-29T10:00:00", "2026-07-29T12:00:00", 3)
        b = _episode(man, "museum part 2", "2026-07-29T12:30:00", "2026-07-29T14:00:00", 3)
        _episode(man, "next week hike", "2026-08-05T09:00:00", "2026-08-05T11:00:00", 2)
        pairs = candidate_pairs(man)
        assert pairs == [(a, b)]


def test_apply_merge_moves_items_and_retires():
    with tempfile.TemporaryDirectory() as td:
        man = _setup(Path(td))
        a = _episode(man, "museum part 1", "2026-07-29T10:00:00", "2026-07-29T12:00:00", 3)
        b = _episode(man, "museum part 2", "2026-07-29T12:30:00", "2026-07-29T14:00:00", 4)
        apply_merge(man, a, b, "museum, whole visit")
        items = man.query("SELECT COUNT(*) AS n FROM items WHERE episode_id = ?", (a,))
        assert items[0]["n"] == 7
        elder = man.get_episode(a)
        assert elder["item_count"] == 7 and elder["description"] == "museum, whole visit"
        assert elder["span_start"] == "2026-07-29T10:00:00"
        assert elder["span_end"] == "2026-07-29T14:00:00"
        younger = man.get_episode(b)
        assert younger["outcome"] == "merged" and younger["item_count"] == 0
        assert candidate_pairs(man) == []     # merged episode is out of circulation
