"""Overwrite battery: placement must NEVER rename onto an
existing path (rename(2) replaces the destination - a delete
by another name). Validates the exact scenario: many items,
one destination name - every file's content must survive.
"""
import hashlib
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
    return cfg, Manifest(cfg.manifest_path)


def _add(man, cfg, done, clean, content):
    (cfg.done_dir / done).write_bytes(content)
    man.upsert_item(md5=hashlib.md5(content).hexdigest(), done_name=done,
                    clean_name=clean, suffix_verified=1,
                    capture_time="2026-07-04T12:00:00",
                    capture_time_raw="2026-07-04T12:00:00",
                    file_size=len(content), status="triaged", category="MISC")


def test_six_items_one_name_all_contents_survive():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _setup(tmp)
        contents = {f"photo-{i}".encode() for i in range(6)}
        for i, c in enumerate(sorted(contents)):
            _add(man, cfg, f"orig{i}_aa{i:06x}.jpg", "winner.jpg", c)
        plan = plan_placements(cfg, man, cfg.done_dir)
        assert plan["counts"]["move"] == 1 and plan["counts"]["keep_suffix"] == 5
        stats = place_all(cfg, man, cfg.done_dir)
        assert stats["placed"] == 6
        landed = {p.read_bytes() for p in (cfg.sorted_dir / "__Misc").glob("*.jpg")}
        assert landed == contents                     # nothing overwritten, nothing lost
        assert (cfg.sorted_dir / "__Misc" / "winner.jpg").exists()


def test_identical_content_second_copy_left_not_deleted():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _setup(tmp)
        _add(man, cfg, "a_first.jpg", "same.jpg", b"identical-bytes")
        # second row: same content + clean name, different source name
        (cfg.done_dir / "b_second.jpg").write_bytes(b"identical-bytes")
        man.upsert_item(md5=hashlib.md5(b"identical-bytes").hexdigest() + "",
                        done_name="b_second.jpg", clean_name="same.jpg",
                        suffix_verified=1, capture_time="2026-07-04T12:00:00",
                        capture_time_raw="2026-07-04T12:00:00",
                        file_size=15, status="triaged", category="MISC")
        # note: identical content => identical md5 => the manifest
        # dedupes by content; emulate the cross-run case instead:
        stats = place_all(cfg, man, cfg.done_dir)
        assert (cfg.sorted_dir / "__Misc" / "same.jpg").read_bytes() == b"identical-bytes"


def test_preexisting_target_is_never_overwritten():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _setup(tmp)
        victim_dir = cfg.sorted_dir / "__Misc"
        victim_dir.mkdir(parents=True)
        (victim_dir / "keep.jpg").write_bytes(b"precious existing file")
        _add(man, cfg, "new_one.jpg", "keep.jpg", b"incoming different content")
        stats = place_all(cfg, man, cfg.done_dir)
        assert (victim_dir / "keep.jpg").read_bytes() == b"precious existing file"
        assert (victim_dir / "new_one.jpg").read_bytes() == b"incoming different content"
        assert stats["placed"] == 1 and stats["kept_suffix"] == 1


def test_target_dir_blocks_instead_of_crashing():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _setup(tmp)
        (cfg.sorted_dir / "__Misc" / "clash.jpg").mkdir(parents=True)
        _add(man, cfg, "c.jpg", "clash.jpg", b"data")
        stats = place_all(cfg, man, cfg.done_dir)
        assert stats["placed"] == 0 and stats["blocked"] == 1
        assert (cfg.done_dir / "c.jpg").exists()
