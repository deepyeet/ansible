"""Run-outcome pings (backlog B4).

A scheduled sorter that fails silently is indistinguishable
from one that had nothing to do - and pinned model names WILL
eventually retire. Optional healthchecks-style pinging:
ALBUM_PING_URL set -> success GETs the URL, failure GETs
<url>/fail (the pixel_manager pattern). Unset -> no-op.
A failed ping never fails the run.
"""
from __future__ import annotations

import os
import urllib.request


def _default_opener(url: str) -> None:
    with urllib.request.urlopen(url, timeout=10) as r:
        r.read()


def ping(ok: bool, opener=None) -> bool:
    base = os.environ.get("ALBUM_PING_URL", "").rstrip("/")
    if not base:
        return False
    url = base if ok else base + "/fail"
    try:
        (opener or _default_opener)(url)
        return True
    except Exception:
        return False
