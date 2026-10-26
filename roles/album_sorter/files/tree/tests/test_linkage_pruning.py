"""B34 linkage candidate pruning: the GPS strong-include
used to nominate at ANY time distance (a home selfie was
verified against every home episode on record - 588 of
attempt 2's 696 verdicts were >30 days out, all "no").
Nomination is now bounded by a GPS time window, and the
ranked candidate list is capped."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.linkage import nominate
from album_sorter.manifest import Manifest

HOME = (47.6, -122.1)
FAR_AWAY = (49.0, -123.5)      # ~180 km from HOME


def _episode(man, desc, times, gps=None):
    eid = man.create_episode(desc, [])
    for i, t in enumerate(times):
        fields = dict(md5=f"{eid:08x}{i:024x}", done_name=f"e{eid}m{i}.jpg",
                      capture_time=t, capture_time_raw=t, episode_id=eid,
                      status="grouped", category="PHOTO")
        if gps:
            fields["gps_lat"], fields["gps_lon"] = gps
        man.upsert_item(**fields)
    man.recompute_episode_span(eid)
    return eid


def _loose(man, md5, t, gps=None):
    fields = dict(md5=md5, done_name=f"{md5[:4]}.jpg", capture_time=t,
                  capture_time_raw=t, status="analyzed", category="PHOTO")
    if gps:
        fields["gps_lat"], fields["gps_lon"] = gps
    man.upsert_item(**fields)
    return man.get_by_md5(md5)


def test_gps_strong_no_longer_reaches_across_months():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        home_ep = _episode(man, "home weekend",
                           ["2026-05-01T10:00:00", "2026-05-03T10:00:00"],
                           gps=HOME)
        cfg = Config()
        # same couch, 73 days later: pre-B34 this was a paid
        # verifier call; now it is not even nominated
        selfie = _loose(man, "a" * 32, "2026-07-15T10:00:00", gps=HOME)
        assert nominate(cfg, man, selfie) == []
        # exactly 3 days past the span: the boundary of the
        # plain adjacency window (inclusive <=), nominated
        # with or without GPS
        near = _loose(man, "b" * 32, "2026-05-06T10:00:00", gps=HOME)
        assert nominate(cfg, man, near) == [home_ep]
        # exactly 7 days past, same place: the former
        # blue-wall design case. The GPS extension that
        # reached it was retired by the user 2026-10-23
        # (the (3, 7] slice measured 133 asks, 0 winners);
        # GPS evidence no longer extends the time window
        week = _loose(man, "f" * 32, "2026-05-10T10:00:00", gps=HOME)
        assert nominate(cfg, man, week) == []
        nogps = _loose(man, "c" * 32, "2026-05-10T10:00:00")
        assert nominate(cfg, man, nogps) == []


def test_gps_veto_still_applies_inside_span():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        _episode(man, "home weekend",
                 ["2026-05-01T10:00:00", "2026-05-03T10:00:00"], gps=HOME)
        cfg = Config()
        # timestamp inside the span, but the photo was taken
        # ~180 km away: vetoed, as before B34
        item = _loose(man, "d" * 32, "2026-05-02T10:00:00", gps=FAR_AWAY)
        assert nominate(cfg, man, item) == []


def test_candidate_cap_and_stats():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        for k in range(7):
            _episode(man, f"ep {k}",
                     ["2026-05-01T10:00:00", "2026-05-03T10:00:00"])
        item = _loose(man, "e" * 32, "2026-05-02T10:00:00")
        stats: dict = {}
        got = nominate(Config(), man, item, stats)
        assert len(got) == 3              # linkage_max_candidates (B35 exp. (e))
        assert stats == {"nominated": 7, "pruned": 4}
        # ranked deterministically: equal distances order by
        # episode id, so the cap keeps the earliest episodes
        assert got == sorted(got)
