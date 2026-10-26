"""Concurrency validation: placement vs a concurrent writer
in the destination folder.

Simulates another process writing into the sorted tree WHILE
place_all runs:
- it pre-creates a file at the LAST item's planned target
  (that item must be refused, never overwrite);
- it drops unrelated files during the run (no interference);
- every writer file is created exclusively ('x' mode), like
  any sane writer, so the writer itself cannot overwrite
  either - the invariant checked is that NO file's content
  changes after creation, on either side.

Usage: conc_test.py <scratch_root>
"""
from __future__ import annotations

import hashlib
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from album_sorter.config import Config
from album_sorter.manifest import Manifest
from album_sorter.place import place_all

N = 300


def main() -> int:
    root = Path(sys.argv[1])
    done = root / "done"
    done.mkdir(parents=True, exist_ok=True)
    cfg = Config(done_dir=done, sorted_dir=root / "sorted", work_dir=root / "work")
    man = Manifest(cfg.manifest_path)
    contents = {}
    for i in range(N):
        content = f"item-{i:03d}".encode() * 4
        name = f"f{i:03d}.jpg"
        (done / name).write_bytes(content)
        contents[name] = content
        man.upsert_item(md5=hashlib.md5(content).hexdigest(), done_name=name,
                        clean_name=name, suffix_verified=1,
                        capture_time="2026-07-04T12:00:00",
                        capture_time_raw="2026-07-04T12:00:00",
                        file_size=len(content), status="triaged", category="MISC")
    misc = cfg.sorted_dir / "__Misc"
    race_target = misc / f"f{N - 1:03d}.jpg"
    writer_files: dict[Path, bytes] = {}

    def writer():
        misc.mkdir(parents=True, exist_ok=True)
        # the ambush: the last item's exact target, created
        # before placement reaches it
        data = b"concurrent-writer-was-here"
        with open(race_target, "xb") as f:
            f.write(data)
        writer_files[race_target] = data
        for k in range(40):
            p = misc / f"concurrent_{k:02d}.jpg"
            d = f"unrelated-{k}".encode()
            try:
                with open(p, "xb") as f:
                    f.write(d)
                writer_files[p] = d
            except FileExistsError:
                pass
            time.sleep(0.005)

    t = threading.Thread(target=writer)
    t.start()
    stats = place_all(cfg, man, cfg.done_dir)
    t.join()

    errors = []
    # the ambushed item: refused, source intact, writer file intact
    if race_target.read_bytes() != writer_files[race_target]:
        errors.append("RACE TARGET WAS OVERWRITTEN")
    if not (done / f"f{N - 1:03d}.jpg").exists():
        errors.append("ambushed item's source vanished")
    # refused either at plan time (dupe_left - the planner saw
    # the writer's file and diverted) or at execution time
    # (blocked - the re-check caught it); both are the safe
    # outcome, which one depends on the interleaving
    if stats["blocked"] + stats["dupes_left"] < 1:
        errors.append(f"expected >= 1 refused, got stats {stats}")
    # every writer file unchanged
    for p, d in writer_files.items():
        if p.read_bytes() != d:
            errors.append(f"writer file changed: {p.name}")
    # every other item placed with its content intact
    placed = list(misc.glob("f*.jpg"))
    placed = [p for p in placed if p.name != race_target.name]
    if len(placed) != N - 1:
        errors.append(f"expected {N - 1} placed items, found {len(placed)}")
    for p in placed:
        if p.read_bytes() != contents[p.name]:
            errors.append(f"placed content corrupted: {p.name}")
    if stats["verify_failed"]:
        errors.append(f"verify_failed={stats['verify_failed']}")
    print("stats:", stats)
    print("writer files intact:", len(writer_files))
    if errors:
        print("FAIL:", *errors, sep="\n  ")
        return 1
    print("CONCURRENCY VALIDATION PASSED: no overwrites, raced item refused, "
          "all contents intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
