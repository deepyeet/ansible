"""Trip rollup tests: chain nomination and fuse application."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.gemini_client import StubClient
from album_sorter.manifest import Manifest
from album_sorter.rollup import apply_rollup, candidate_chains, run_rollup


def _episode(man, desc, start, end, n_items):
    eid = man.create_episode(desc, [])
    man.update_episode(eid, span_start=start, span_end=end, item_count=n_items)
    for i in range(n_items):
        t = end if (n_items > 1 and i == n_items - 1) else start
        man.upsert_item(md5=f"{eid}-{i}", done_name=f"f{eid}-{i}.jpg",
                        capture_time=t, capture_time_raw=t,
                        episode_id=eid, status="grouped", category="PHOTO")
    man.recompute_episode_span(eid)
    return eid


def _vegas(man):
    a = _episode(man, "venetian evening", "2026-07-28T19:00:00", "2026-07-28T21:00:00", 3)
    b = _episode(man, "hotel window", "2026-07-29T11:00:00", "2026-07-29T11:05:00", 2)
    c = _episode(man, "museum", "2026-07-29T15:00:00", "2026-07-29T17:00:00", 4)
    return a, b, c


def test_chains_link_close_episodes_only():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        a, b, c = _vegas(man)
        _episode(man, "much later", "2026-08-20T10:00:00", "2026-08-20T11:00:00", 2)
        chains = candidate_chains(man, gap_hours=36.0, max_span_days=30.0)
        assert chains == [[a, b, c]]


def test_apply_rollup_fuses_chain():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        a, b, c = _vegas(man)
        apply_rollup(man, [a, b, c], "the whole vegas trip")
        elder = man.get_episode(a)
        assert elder["item_count"] == 9 and elder["description"] == "the whole vegas trip"
        assert elder["span_start"] == "2026-07-28T19:00:00"
        assert elder["span_end"] == "2026-07-29T17:00:00"
        assert man.get_episode(b)["outcome"] == "merged"
        assert man.get_episode(c)["outcome"] == "merged"


def test_run_rollup_respects_judgment():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        _vegas(man)
        client = StubClient(lambda model, system, parts: {
            "is_trip": False, "confidence": 0.9,
            "reason": "just consecutive days at home"})
        stats = run_rollup(Config(), man, client)
        assert stats["chains_checked"] == 1 and stats["trips_formed"] == 0
        assert all(ep["outcome"] == "pending" for ep in man.all_episodes())
