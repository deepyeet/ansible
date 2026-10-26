"""Placement safety tests: nothing is placed before its
destination is determined, and a crashed placement reconciles
instead of stranding. Run via tests/run_tests.py (which imports
this module too if present) or pytest."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.manifest import Manifest
from album_sorter.place import place_all, plan_placements


def _setup(tmp: Path):
    cfg = Config(done_dir=tmp / "done", sorted_dir=tmp / "sorted", work_dir=tmp / "work")
    cfg.done_dir.mkdir(parents=True)
    man = Manifest(cfg.manifest_path)
    return cfg, man


def _add(man, name, content, **kw):
    import hashlib
    digest = hashlib.md5(content).hexdigest()
    src = Path(kw.pop("src_dir")) / name
    src.write_bytes(content)
    fields = dict(md5=digest, done_name=name, clean_name=name, suffix_verified=0,
                  capture_time="2026-07-04T12:00:00", capture_time_raw="2026-07-04T12:00:00",
                  time_source="exif_dto", time_confidence="high", status="triaged")
    fields.update(kw)
    man.upsert_item(**fields)
    return digest


def test_ungrouped_photo_is_not_placed():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _setup(tmp)
        from datetime import datetime, timedelta
        recent = (datetime.now() - timedelta(days=5)).isoformat()
        _add(man, "photo.jpg", b"img", src_dir=cfg.done_dir, category="PHOTO", status="analyzed",
             capture_time=recent, capture_time_raw=recent)
        _add(man, "junk.jpg", b"junk", src_dir=cfg.done_dir, category="MISC")
        stats = place_all(cfg, man, cfg.done_dir)
        assert stats["placed"] == 1 and stats["not_ready"] == 1
        assert (cfg.done_dir / "photo.jpg").exists()          # untouched
        assert (cfg.sorted_dir / "__Misc" / "junk.jpg").exists()


def test_dry_run_moves_nothing_and_writes_nothing():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _setup(tmp)
        from datetime import datetime, timedelta
        recent = (datetime.now() - timedelta(days=5)).isoformat()
        d_photo = _add(man, "photo.jpg", b"img", src_dir=cfg.done_dir, category="PHOTO", status="analyzed",
                       capture_time=recent, capture_time_raw=recent)
        _add(man, "junk.jpg", b"junk", src_dir=cfg.done_dir, category="MISC")
        plan = plan_placements(cfg, man, cfg.done_dir)
        assert plan["dry_run"] is True
        assert plan["counts"]["move"] == 1 and plan["counts"]["not_ready"] == 1
        # nothing moved, no folders created, statuses untouched
        assert (cfg.done_dir / "junk.jpg").exists()
        assert not cfg.sorted_dir.exists()
        assert man.get_by_md5(d_photo)["status"] == "analyzed"
        # and the real run afterwards agrees with the plan
        stats = place_all(cfg, man, cfg.done_dir)
        assert stats["placed"] == plan["counts"]["move"]


def test_dry_run_detects_duplicate_target():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _setup(tmp)
        _add(man, "junk.jpg", b"junk", src_dir=cfg.done_dir, category="MISC")
        target = cfg.sorted_dir / "__Misc" / "junk.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"junk")     # identical content already there
        plan = plan_placements(cfg, man, cfg.done_dir)
        assert plan["counts"]["dupe_left"] == 1 and plan["counts"]["move"] == 0
        assert (cfg.done_dir / "junk.jpg").exists()


def test_placing_row_reconciles_after_crash():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _setup(tmp)
        digest = _add(man, "moved.jpg", b"data", src_dir=cfg.done_dir, category="MISC")
        # simulate: file was renamed, then the process died
        target = cfg.sorted_dir / "__Misc" / "moved.jpg"
        target.parent.mkdir(parents=True)
        (cfg.done_dir / "moved.jpg").rename(target)
        man.update_item(digest, final_path=str(target), status="placing")
        stats = place_all(cfg, man, cfg.done_dir)
        assert stats["reconciled"]["confirmed_placed"] == 1
        assert man.get_by_md5(digest)["status"] == "placed"
        assert target.exists()


def test_pre_place_backup_is_mirrored_off_volume():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _setup(tmp)
        cfg.backup_dir = tmp / "pixel-share" / "manifest-backups"
        _add(man, "junk.jpg", b"junk", src_dir=cfg.done_dir, category="MISC")
        stats = place_all(cfg, man, cfg.done_dir)
        assert stats["placed"] == 1
        mirrors = list(cfg.backup_dir.glob("manifest*.bak-*-pre-place"))
        assert len(mirrors) == 1
        # the mirror is a real copy of the manifest, not a placeholder
        import sqlite3
        rows = sqlite3.connect(str(mirrors[0])).execute(
            "SELECT COUNT(*) FROM items").fetchone()[0]
        assert rows == 1


def test_duplicate_content_is_left_in_place():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _setup(tmp)
        _add(man, "dupe.jpg", b"same-bytes", src_dir=cfg.done_dir, category="MISC")
        target = cfg.sorted_dir / "__Misc" / "dupe.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"same-bytes")
        stats = place_all(cfg, man, cfg.done_dir)
        assert stats["dupes_left"] == 1
        assert (cfg.done_dir / "dupe.jpg").exists()           # source NOT removed
