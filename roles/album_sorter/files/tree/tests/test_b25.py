"""B25 decisions, made by Muse under delegation 2026-10-15:
(1) the GPS veto measures distance to the episode's NEAREST
member photo, not its centroid; (2) a source whose size is
unchanged but whose mtime moved gets one hash check at
placement - a same-size content swap blocks, a mere touch
does not."""
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.linkage import nominate
from album_sorter.manifest import Manifest
from album_sorter.place import place_all


def test_veto_uses_nearest_member_not_centroid():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        eid = man.create_episode("road trip: seattle to vegas", [])
        # two clusters ~1,100 km apart; centroid sits in Utah,
        # >50 km from EVERY member - the old code vetoed all
        pts = [(47.6062, -122.3321, "2026-07-01T09:00:00"),
               (47.6200, -122.3100, "2026-07-01T10:00:00"),
               (36.1699, -115.1398, "2026-07-03T09:00:00"),
               (36.1800, -115.1200, "2026-07-03T10:00:00")]
        for i, (la, lo, t) in enumerate(pts):
            man.upsert_item(md5=f"{i:032x}", done_name=f"m{i}.jpg",
                            capture_time=t, capture_time_raw=t,
                            gps_lat=la, gps_lon=lo,
                            episode_id=eid, status="grouped", category="PHOTO")
        man.recompute_episode_span(eid)
        # a loose photo taken in Vegas during the span
        man.upsert_item(md5="a" * 32, done_name="loose.jpg",
                        capture_time="2026-07-03T11:00:00",
                        capture_time_raw="2026-07-03T11:00:00",
                        gps_lat=36.1750, gps_lon=-115.1300,
                        status="analyzed", category="PHOTO")
        loose = man.get_by_md5("a" * 32)
        assert nominate(Config(), man, loose) == [eid]
        # and a genuinely far photo is still vetoed out
        man.upsert_item(md5="b" * 32, done_name="nyc.jpg",
                        capture_time="2026-07-03T11:00:00",
                        capture_time_raw="2026-07-03T11:00:00",
                        gps_lat=40.7128, gps_lon=-74.0060,
                        status="analyzed", category="PHOTO")
        assert nominate(Config(), man, man.get_by_md5("b" * 32)) == []


def _place_one(tmp: Path, content: bytes, file_mtime: float):
    cfg = Config(done_dir=tmp / "done", sorted_dir=tmp / "sorted", work_dir=tmp / "work")
    cfg.done_dir.mkdir(parents=True)
    (cfg.done_dir / "f.jpg").write_bytes(content)
    man = Manifest(cfg.manifest_path)
    man.upsert_item(md5=hashlib.md5(content).hexdigest(), done_name="f.jpg",
                    clean_name="f.jpg", suffix_verified=1,
                    capture_time="2026-07-04T12:00:00",
                    capture_time_raw="2026-07-04T12:00:00",
                    file_size=len(content), file_mtime=file_mtime,
                    status="triaged", category="MISC")
    return cfg, man


def test_same_size_swap_with_new_mtime_blocks():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _place_one(tmp, b"AAAAAAAAAAAAAAAA", time.time() - 1000)
        (cfg.done_dir / "f.jpg").write_bytes(b"BBBBBBBBBBBBBBBB")  # same size, new mtime
        stats = place_all(cfg, man, cfg.done_dir)
        assert stats["placed"] == 0 and stats["src_changed"] == 1


def test_mere_touch_still_places():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _place_one(tmp, b"same-content-here", time.time() - 1000)
        os.utime(cfg.done_dir / "f.jpg")          # touched, content identical
        stats = place_all(cfg, man, cfg.done_dir)
        assert stats["placed"] == 1 and stats["src_changed"] == 0


def test_untouched_places_normally():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        content = b"pristine"
        mt = (tmp / "x").stat().st_mtime if False else None
        cfg = Config(done_dir=tmp / "done", sorted_dir=tmp / "sorted", work_dir=tmp / "work")
        cfg.done_dir.mkdir(parents=True)
        (cfg.done_dir / "f.jpg").write_bytes(content)
        recorded = (cfg.done_dir / "f.jpg").stat().st_mtime
        man = Manifest(cfg.manifest_path)
        man.upsert_item(md5=hashlib.md5(content).hexdigest(), done_name="f.jpg",
                        clean_name="f.jpg", suffix_verified=1,
                        capture_time="2026-07-04T12:00:00",
                        capture_time_raw="2026-07-04T12:00:00",
                        file_size=len(content), file_mtime=recorded,
                        status="triaged", category="MISC")
        assert place_all(cfg, man, cfg.done_dir)["placed"] == 1
