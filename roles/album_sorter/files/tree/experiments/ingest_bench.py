"""B3 timing check: ingest the calibration corpus fresh with
N workers and report wall time. Usage: ingest_bench.py <workers>"""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from album_sorter.config import Config
from album_sorter.ingest import ingest
from album_sorter.manifest import Manifest

BASE = Path(__file__).resolve().parent
workers = int(sys.argv[1])
root = Path(sys.argv[2]) if len(sys.argv) > 2 else BASE
work = root / f"bench_work_{workers}"
shutil.rmtree(work, ignore_errors=True)
cfg = Config(done_dir=BASE / "cal_corpus", work_dir=work,
             sorted_dir=root / "bench_sorted", ingest_workers=workers)
man = Manifest(cfg.manifest_path)
t0 = time.time()
stats = ingest(cfg, man)
print(f"workers={workers}: {stats} in {time.time() - t0:.1f}s", flush=True)
