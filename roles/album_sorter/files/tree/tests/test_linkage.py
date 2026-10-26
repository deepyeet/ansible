"""Linkage tests - the module had none, which is how an
argument-order inconsistency survived until the first live
exercise (B7, 2026-10-15)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.gemini_client import StubClient
from album_sorter.linkage import nominate, try_link
from album_sorter.manifest import Manifest


def _setup(tmp: Path):
    man = Manifest(tmp / "m.sqlite3")
    eid = man.create_episode("museum visit", ["a" * 32])
    for i, t in enumerate(("2026-07-29T10:00:00", "2026-07-29T12:00:00")):
        man.upsert_item(md5=f"{i:032x}", done_name=f"in{i}.jpg",
                        capture_time=t, capture_time_raw=t,
                        episode_id=eid, status="grouped", category="PHOTO")
    man.recompute_episode_span(eid)
    man.upsert_item(md5="f" * 32, done_name="loose.jpg",
                    capture_time="2026-07-29T11:00:00",
                    capture_time_raw="2026-07-29T11:00:00",
                    status="analyzed", category="PHOTO")
    man.upsert_item(md5="e" * 32, done_name="far.jpg",
                    capture_time="2026-09-15T11:00:00",
                    capture_time_raw="2026-09-15T11:00:00",
                    status="analyzed", category="PHOTO")
    return man, eid


def test_nominate_and_try_link_attach():
    with tempfile.TemporaryDirectory() as td:
        man, eid = _setup(Path(td))
        cfg = Config()
        loose = man.get_by_md5("f" * 32)
        assert nominate(cfg, man, loose) == [eid]
        far = man.get_by_md5("e" * 32)
        assert nominate(cfg, man, far) == []
        client = StubClient(lambda m, s, p: {"verdict": "yes", "confidence": 0.95})
        assert try_link(cfg, man, client, loose) is True
        now = man.get_by_md5("f" * 32)
        assert now["episode_id"] == eid and now["assign_method"] == "linkage"
        assert try_link(cfg, man, client, far) is False


def test_try_link_respects_verifier_no():
    with tempfile.TemporaryDirectory() as td:
        man, eid = _setup(Path(td))
        loose = man.get_by_md5("f" * 32)
        client = StubClient(lambda m, s, p: {"verdict": "no", "confidence": 0.9})
        assert try_link(Config(), man, client, loose) is False
        assert man.get_by_md5("f" * 32)["episode_id"] is None
