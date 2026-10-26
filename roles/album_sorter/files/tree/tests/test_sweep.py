"""B29 (user decision, 2026-10-17): the leftover sweep
is age-gated. A loose photo is swept into its own month
bucket only once its capture time is >= sweep_min_age_days
old; younger loose photos, timeless ones, and members of
ungated episodes stay held - placement is one-way and the
user uploads from the sorted tree, so nothing may be
locked into a bucket before late-forming episodes have
had their chance to claim it."""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.manifest import Manifest
from album_sorter.place import place_all, plan_placements


def _setup(tmp: Path, name: str, capture: datetime | None, age_days: int = 0):
    cfg = Config(done_dir=tmp / "done", sorted_dir=tmp / "sorted", work_dir=tmp / "work")
    cfg.done_dir.mkdir(parents=True, exist_ok=True)
    (cfg.done_dir / name).write_bytes(b"img")
    man = Manifest(cfg.manifest_path)
    man.upsert_item(md5=name[0] * 32, done_name=name, clean_name=name,
                    suffix_verified=1, category="PHOTO", status="analyzed",
                    capture_time=capture.isoformat() if capture else None,
                    capture_time_raw=capture.isoformat() if capture else None,
                    file_size=3, file_mtime=(cfg.done_dir / name).stat().st_mtime)
    return cfg, man


def _plan_by_name(cfg, man):
    plan = plan_placements(cfg, man, cfg.done_dir)
    return {e["file"]: e for e in plan["entries"]}, plan["counts"]


def test_old_loose_photo_is_swept_to_its_month():
    with tempfile.TemporaryDirectory() as td:
        old = datetime.now() - timedelta(days=200)
        cfg, man = _setup(Path(td), "old.jpg", old)
        entries, counts = _plan_by_name(cfg, man)
        assert entries["old.jpg"]["action"] == "move"
        import calendar
        assert entries["old.jpg"]["target"].endswith(
            f"{old.year} {calendar.month_name[old.month]}/old.jpg")
        assert counts["swept"] == 1
        stats = place_all(cfg, man, cfg.done_dir)
        assert stats["placed"] == 1 and stats["swept"] == 1
        row = man.get_by_done_name("old.jpg")
        assert row["status"] == "placed" and row["assign_method"] == "gate"


def test_young_and_timeless_loose_photos_stay_held():
    with tempfile.TemporaryDirectory() as td:
        young = datetime.now() - timedelta(days=10)
        cfg, man = _setup(Path(td), "young.jpg", young)
        man.upsert_item(md5="t" * 32, done_name="timeless.jpg", category="PHOTO",
                        status="analyzed")
        entries, counts = _plan_by_name(cfg, man)
        assert entries["young.jpg"]["action"] == "not_ready"
        assert entries["timeless.jpg"]["action"] == "not_ready"
        assert counts["swept"] == 0


def test_old_member_of_pending_episode_is_not_swept():
    with tempfile.TemporaryDirectory() as td:
        old = datetime.now() - timedelta(days=200)
        cfg, man = _setup(Path(td), "member.jpg", old)
        ep = man.create_episode("ungated episode", [])
        man.update_item("m" * 32, episode_id=ep, status="grouped")
        entries, _ = _plan_by_name(cfg, man)
        assert entries["member.jpg"]["action"] == "not_ready"
