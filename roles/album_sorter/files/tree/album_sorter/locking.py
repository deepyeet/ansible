"""Single-instance run lock (backlog B1).

The manifest is a single-writer SQLite store; two overlapping
sorter runs (a scheduled cycle plus a manual one) would race
on it. pixel_manager has the same discipline via its lockfile;
this gives the sorter its own. The lock is an flock on
<work_dir>/sorter.lock - held for the whole pipeline run and
released automatically if the process dies.
"""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def run_lock(work_dir: Path):
    work_dir.mkdir(parents=True, exist_ok=True)
    fh = open(work_dir / "sorter.lock", "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise AlreadyRunning(
            f"another sorter run holds the lock in {work_dir}")
    try:
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
