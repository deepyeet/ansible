"""B23: manifest invariant audit catches the dangerous
states (shared destination, missing placed file, outcome
without a name, count drift, unknown on-disk files) and
reports held loose photos without calling them violations."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.audit import audit_manifest
from album_sorter.config import Config
from album_sorter.manifest import Manifest


def _setup(tmp: Path):
    cfg = Config(done_dir=tmp / "done", sorted_dir=tmp / "sorted", work_dir=tmp / "work")
    cfg.sorted_dir.mkdir(parents=True)
    return cfg, Manifest(cfg.manifest_path)


def _placed(man, cfg, md5c, folder, name):
    p = cfg.sorted_dir / folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    man.upsert_item(md5=md5c * 32, done_name=name, status="placed",
                    final_path=str(p), category="PHOTO")
    return p


def test_clean_manifest_passes():
    with tempfile.TemporaryDirectory() as td:
        cfg, man = _setup(Path(td))
        _placed(man, cfg, "a", "2026 July", "a.jpg")
        man.upsert_item(md5="b" * 32, done_name="loose.jpg", category="PHOTO",
                        status="analyzed")
        out = audit_manifest(cfg, man)
        assert out["violations"] == []
        assert out["info"] == {"placed": 1, "loose_photos_held": 1}


def test_each_violation_is_caught():
    with tempfile.TemporaryDirectory() as td:
        cfg, man = _setup(Path(td))
        p = _placed(man, cfg, "a", "2026 July", "same.jpg")
        man.upsert_item(md5="b" * 32, done_name="other.jpg", status="placed",
                        final_path=str(p), category="PHOTO")          # duplicate path
        man.upsert_item(md5="c" * 32, done_name="gone.jpg", status="placed",
                        final_path=str(cfg.sorted_dir / "2026 July" / "gone.jpg"),
                        category="PHOTO")                              # missing file
        ep = man.create_episode("nameless", [])
        man.update_episode(ep, outcome="album_new", album_name=None, item_count=0)
        ep2 = man.create_episode("drifty", [])
        man.update_episode(ep2, outcome="month_bucket", item_count=5)  # actual 0
        (cfg.sorted_dir / "2026 July" / "mystery.jpg").write_bytes(b"?")
        out = audit_manifest(cfg, man)
        text = "\n".join(out["violations"])
        assert "duplicate final_path" in text
        assert "placed file missing" in text
        assert "without album_name" in text
        assert "item_count 5 != actual 0" in text
        assert "unknown to manifest" in text
        assert len(out["violations"]) == 5
