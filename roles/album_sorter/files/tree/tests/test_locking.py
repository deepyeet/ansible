"""Run-lock tests (backlog B1): a second mutating run refuses."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.locking import AlreadyRunning, run_lock


def test_second_acquire_refused_and_reacquire_after_release():
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        with run_lock(work):
            refused = False
            try:
                with run_lock(work):
                    pass
            except AlreadyRunning:
                refused = True
            assert refused
        with run_lock(work):   # released: acquiring again works
            pass
