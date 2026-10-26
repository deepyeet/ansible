"""Ping tests (backlog B4): URL formation, no-op when unset,
and a broken ping endpoint never propagates."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.ping import ping


def test_ping_urls_and_noop():
    calls = []
    old = os.environ.pop("ALBUM_PING_URL", None)
    try:
        assert ping(True, opener=calls.append) is False   # unset: no-op
        os.environ["ALBUM_PING_URL"] = "https://hc.example/abc/"
        assert ping(True, opener=calls.append) is True
        assert ping(False, opener=calls.append) is True
        assert calls == ["https://hc.example/abc", "https://hc.example/abc/fail"]
        def boom(url):
            raise OSError("endpoint down")
        assert ping(True, opener=boom) is False           # never propagates
    finally:
        if old is None:
            os.environ.pop("ALBUM_PING_URL", None)
        else:
            os.environ["ALBUM_PING_URL"] = old
