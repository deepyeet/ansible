"""B21: clock-correction driver - per-device offsets from
GPS samples, device-wide application, step changes, and
idempotency; plus ingest persisting gps_utc/software."""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.clockfix import apply_clock_corrections
from album_sorter.config import Config
from album_sorter.manifest import Manifest

H = timedelta(hours=1)


def _cfg_man(tmp: Path):
    cfg = Config(done_dir=tmp / "done", work_dir=tmp / "work",
                 sorted_dir=tmp / "sorted")
    return cfg, Manifest(cfg.manifest_path)


def _add(man, md5c, device, raw: datetime, gps: datetime | None,
         status="ingested"):
    man.upsert_item(md5=md5c * 32, done_name=f"{md5c}.jpg", device=device,
                    capture_time_raw=raw.isoformat(),
                    capture_time=raw.isoformat(),
                    gps_utc=gps.isoformat() if gps else None,
                    status=status)


def test_constant_offset_applies_device_wide():
    with tempfile.TemporaryDirectory() as td:
        cfg, man = _cfg_man(Path(td))
        base = datetime(2026, 7, 1, 12, 0, 0)
        for i in range(4):   # samples: clock reads 7h behind UTC truth
            raw = base + i * H
            _add(man, chr(ord("a") + i), "Pixel A", raw, raw + 7 * H)
        _add(man, "z", "Pixel A", base + 10 * H, None)        # no GPS: still corrected
        _add(man, "y", "Pixel A", base - 30 * 24 * H, None)  # before first sample: corrected (device-wide)
        out = apply_clock_corrections(cfg, man)
        assert out["anchored"] == 1 and out["items_corrected"] == 6
        z = man.get_by_done_name("z.jpg")
        assert z["capture_time"] == (base + 17 * H).isoformat()
        y = man.get_by_done_name("y.jpg")
        assert y["capture_time"] == (base - 30 * 24 * H + 7 * H).isoformat()
        assert apply_clock_corrections(cfg, man)["items_corrected"] == 0   # idempotent


def test_step_change_and_unanchored_and_placed_untouched():
    with tempfile.TemporaryDirectory() as td:
        cfg, man = _cfg_man(Path(td))
        jun = datetime(2026, 6, 1, 12, 0, 0)
        for i in range(3):   # before the reset: +7h
            raw = jun - (3 - i) * 24 * H
            _add(man, chr(ord("a") + i), "Cam B", raw, raw + 7 * H)
        for i in range(3):   # after the reset: -5h
            raw = jun + i * 24 * H
            _add(man, chr(ord("d") + i), "Cam B", raw, raw - 5 * H)
        _add(man, "m", "Cam B", jun - 24 * H, None)
        _add(man, "n", "Cam B", jun + 48 * H, None)
        _add(man, "p", "Cam B", jun + 72 * H, None, status="placed")
        for i in range(2):   # sparse device: unanchored
            raw = jun + i * H
            _add(man, chr(ord("u") + i), "Sparse", raw, raw + 3 * H)
        out = apply_clock_corrections(cfg, man)
        assert out["anchored"] == 1
        assert man.get_by_done_name("m.jpg")["capture_time"] == (jun - 24 * H + 7 * H).isoformat()
        assert man.get_by_done_name("n.jpg")["capture_time"] == (jun + 48 * H - 5 * H).isoformat()
        assert man.get_by_done_name("p.jpg")["capture_time"] == (jun + 72 * H).isoformat()  # placed: untouched
        assert man.get_by_done_name("u.jpg")["capture_time"] == (jun + 0 * H).isoformat()    # sparse: untouched


def test_ingest_persists_gps_utc_and_software():
    import album_sorter.ingest as ing
    from album_sorter.metadata import MediaMeta
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, man = _cfg_man(tmp)
        cfg.done_dir.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (60, 40), (9, 9, 9)).save(cfg.done_dir / "shot.jpg", "JPEG")
        fake = MediaMeta()
        fake.device = "Google Pixel"
        fake.software = "HDR+ 1.0"
        fake.gps_lat, fake.gps_lon = 47.6, -122.3
        fake.gps_utc = datetime(2026, 7, 4, 19, 0, 0, tzinfo=timezone.utc)
        fake.dt_original = datetime(2026, 7, 4, 12, 0, 0)
        orig = ing.extract_image_meta
        ing.extract_image_meta = lambda p: fake
        try:
            assert ing.ingest(cfg, man)["ingested"] == 1
        finally:
            ing.extract_image_meta = orig
        row = man.get_by_done_name("shot.jpg")
        assert row["device"] == "Google Pixel"
        assert row["software"] == "HDR+ 1.0"
        assert row["gps_utc"] == "2026-07-04T19:00:00+00:00"
