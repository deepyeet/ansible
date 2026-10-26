"""Asset self-healing: deleted derived files are rebuilt
from the originals; an intact cache is left alone."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.ingest import ingest, rebuild_missing_assets
from album_sorter.manifest import Manifest


def test_deleted_assets_regenerate():
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = Config(done_dir=tmp / "done", work_dir=tmp / "work",
                     sorted_dir=tmp / "sorted")
        cfg.done_dir.mkdir(parents=True)
        Image.new("RGB", (120, 90), (30, 60, 90)).save(cfg.done_dir / "one.jpg", "JPEG")
        Image.new("RGB", (120, 90), (200, 10, 10)).save(cfg.done_dir / "two.jpg", "JPEG")
        man = Manifest(cfg.manifest_path)
        assert ingest(cfg, man)["ingested"] == 2
        assets = list(cfg.assets_dir.rglob("*_a.jpg"))
        assert len(assets) == 2
        # intact cache: nothing to do
        assert rebuild_missing_assets(cfg, man) == {"checked": 2, "rebuilt": 0, "no_source": 0}
        # delete the analysis copy of one item and the
        # thumbnail of the OTHER (paths derived from the
        # manifest digests - rglob order is not stable)
        digests = sorted(r["md5"] for r in man.query("SELECT md5 FROM items"))
        (cfg.assets_dir / digests[0][:2] / f"{digests[0]}_a.jpg").unlink()
        (cfg.assets_dir / digests[1][:2] / f"{digests[1]}_t.jpg").unlink()
        out = rebuild_missing_assets(cfg, man)
        assert out["rebuilt"] == 2 and out["no_source"] == 0
        assert len(list(cfg.assets_dir.rglob("*_a.jpg"))) == 2
        assert len(list(cfg.assets_dir.rglob("*_t.jpg"))) == 2
        # and a source that vanished entirely is reported, not fatal
        one_md5 = man.get_by_done_name("one.jpg")["md5"]
        (cfg.done_dir / "one.jpg").unlink()
        (cfg.assets_dir / one_md5[:2] / f"{one_md5}_a.jpg").unlink()
        out = rebuild_missing_assets(cfg, man)
        assert out["no_source"] == 1 and out["rebuilt"] == 0
