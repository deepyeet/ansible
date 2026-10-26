"""Phase 0 - ingest (spec Section 5). Deterministic, local, no API.

Scans the done dir, hashes, verifies the checksum suffix, extracts
metadata, resolves timestamps, applies hard pre-rules, builds
derived assets, and writes manifest rows. Idempotent: files already
in the manifest by name are skipped; a hash seen under a new name
is a re-arrival and is logged, never duplicated and never deleted.

Parallelism (backlog B3): the expensive per-file work - hashing,
metadata, and above all the full-res decode in asset building
(~5 s/file single-threaded) - runs in a process pool. Workers
never touch the manifest: the main process dedupes digests (in
sorted order, so first-wins re-arrival semantics match the old
sequential code exactly) and does every manifest write itself.
Asset building runs once per unique new digest, so two copies
of one photo can never race on the same asset directory.
"""
from __future__ import annotations

import hashlib
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import assets as assets_mod
from .config import Config
from .manifest import Manifest
from .metadata import (IMAGE_EXTS, VIDEO_EXTS, extract_image_meta,
                       extract_video_meta, pre_rule_category, resolve_time)

_SUFFIX = re.compile(r"_([0-9a-f]{8})$")


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def split_suffix(name: str) -> tuple[str, str | None]:
    stem = Path(name).stem
    m = _SUFFIX.search(stem)
    if m:
        return stem[: m.start()], m.group(1)
    return stem, None


def _probe(path_str: str) -> dict:
    """Worker: hash + metadata + time resolution for one file.
    Pure function of the file - no manifest access."""
    path = Path(path_str)
    digest = md5_of(path)
    if path.suffix.lower() in VIDEO_EXTS:
        meta = extract_video_meta(path)
    else:
        meta = extract_image_meta(path)
    rt = resolve_time(path, meta)
    return {"path": path_str, "digest": digest, "meta": meta, "rt": rt,
            "size": path.stat().st_size,
            "mtime": path.stat().st_mtime}


def _build(args: tuple) -> str:
    """Worker: build derived assets for one unique digest."""
    digest, path_str, assets_dir, duration_s, analysis_px, thumb_px, poster_fraction = args
    assets_mod.build_assets(digest, Path(path_str), Path(assets_dir),
                            duration_s, analysis_px, thumb_px, poster_fraction)
    return digest


def _error_row(man: Manifest, name: str, e: Exception) -> None:
    man.upsert_item(md5=f"err:{name}", done_name=name,
                    category="ERROR", status="triaged",
                    error=f"{type(e).__name__}: {e}")


def ingest(cfg: Config, man: Manifest) -> dict:
    stats = {"seen": 0, "ingested": 0, "skipped": 0, "rearrivals": 0, "errors": 0}
    run_id = man.start_run("ingest", {"done_dir": str(cfg.done_dir)})

    candidates: list[Path] = []
    for path in sorted(cfg.done_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS | VIDEO_EXTS:
            continue
        stats["seen"] += 1
        existing = man.get_by_done_name(path.name)
        if existing:
            if (existing["file_size"] is not None
                    and path.stat().st_size != existing["file_size"]):
                # the file changed under this name after ingest
                # (a partial copy completed, a re-export landed):
                # the recorded identity is stale - drop the row
                # and ingest the file as it now is. Placement's
                # src_changed check is the other half of this
                # guard. (A same-size content swap is the known
                # residual, backlog B25.)
                man.execute("DELETE FROM items WHERE md5 = ?", (existing["md5"],))
            elif str(existing["md5"]).startswith("err:"):
                # a placeholder from a failed attempt (F5): the
                # failure may have been transient (an ffmpeg
                # timeout under load) - drop the placeholder and
                # let the file ingest fresh instead of poisoning
                # the name forever
                man.execute("DELETE FROM items WHERE md5 = ?", (existing["md5"],))
            else:
                stats["skipped"] += 1
                continue
        candidates.append(path)

    workers = max(1, cfg.ingest_workers)
    probed: dict[str, dict] = {}
    probe_errors: dict[str, Exception] = {}
    if candidates:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_probe, str(p)): p for p in candidates}
            for fut, path in futs.items():
                try:
                    probed[path.name] = fut.result()
                except Exception as e:   # one corrupt file never blocks the batch
                    probe_errors[path.name] = e

    # Dedupe in sorted order: the first name wins a digest; later
    # same-content names and digests already in the manifest are
    # re-arrivals, exactly as the sequential version behaved.
    fresh: list[tuple[Path, dict]] = []
    seen_digests: set[str] = set()
    for path in candidates:
        res = probed.get(path.name)
        if res is None:
            continue  # probe raised; handled below via future errors
        digest = res["digest"]
        if digest in seen_digests or man.get_by_md5(digest):
            stats["rearrivals"] += 1
            continue
        seen_digests.add(digest)
        fresh.append((path, res))

    built: set[str] = set()
    build_errors: dict[str, Exception] = {}
    if fresh:
        jobs = {res["digest"]: (res["digest"], str(path), str(cfg.assets_dir),
                                res["meta"].duration_s, cfg.analysis_px,
                                cfg.thumb_px, cfg.poster_fraction)
                for path, res in fresh}
        # Videos build SEQUENTIALLY: parallel ffmpeg poster seeks
        # on this storage stack timed out at 120 s under contention
        # (bench finding, 2026-10-15 - 3 of 3 long MP4s failed in
        # the parallel pool, all fine sequentially). Images are
        # decode-bound and parallelize cleanly.
        video_digests = {d for d, job in jobs.items()
                         if Path(job[1]).suffix.lower() in VIDEO_EXTS}
        image_jobs = {d: j for d, j in jobs.items() if d not in video_digests}
        if image_jobs:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(_build, job): digest for digest, job in image_jobs.items()}
                for fut, digest in futs.items():
                    try:
                        built.add(fut.result())
                    except Exception as e:
                        build_errors[digest] = e
        for digest in video_digests:
            try:
                built.add(_build(jobs[digest]))
            except Exception as e:
                build_errors[digest] = e

    fresh_paths = {id(p) for p, _ in fresh}
    for path in candidates:
        if path.name in probe_errors:
            stats["errors"] += 1
            _error_row(man, path.name, probe_errors[path.name])
            continue
        res = probed[path.name]
        digest = res["digest"]
        if id(path) not in fresh_paths:
            continue  # re-arrival, already counted
        if digest in build_errors:
            stats["errors"] += 1
            _error_row(man, path.name, build_errors[digest])
            continue
        try:
            meta, rt = res["meta"], res["rt"]
            stem, suffix = split_suffix(path.name)
            verified = suffix is not None and digest[:8] == suffix
            clean = (stem + path.suffix.lower()) if verified else path.name
            raw_iso = rt.raw.isoformat() if rt.raw else None
            pre = pre_rule_category(path.name, meta)
            man.upsert_item(
                md5=digest, done_name=path.name, clean_name=clean,
                suffix_verified=int(verified),
                capture_time_raw=raw_iso, capture_time=raw_iso,
                time_source=rt.source, time_confidence=rt.confidence,
                file_size=res["size"], file_mtime=res["mtime"],
                device=meta.device or None,
                software=meta.software or None,
                gps_utc=meta.gps_utc.isoformat() if meta.gps_utc else None,
                # GPS is a pair or nothing (F7): a half-coordinate
                # (one axis corrupt in EXIF) crashes consumers
                gps_lat=meta.gps_lat if meta.gps_lon is not None else None,
                gps_lon=meta.gps_lon if meta.gps_lat is not None else None,
                category=pre[0] if pre else None,
                category_confidence=pre[1] if pre else None,
                category_source=pre[2] if pre else None,
                status="triaged" if pre else "ingested")
            stats["ingested"] += 1
        except Exception as e:
            stats["errors"] += 1
            _error_row(man, path.name, e)
    man.finish_run(run_id, str(stats))
    return stats


def rebuild_missing_assets(cfg: Config, man: Manifest) -> dict:
    """Self-healing sweep: if asset files were deleted (manual
    cleanup, a partial sync, disk pruning), rebuild them from
    the originals for every item still in the pipeline. Asset
    existence is checked on the filesystem, never trusted
    from manifest state - so the folder can be pruned freely
    and the next run repairs exactly what is missing."""
    stats = {"checked": 0, "rebuilt": 0, "no_source": 0}
    seen: set[str] = set()
    rows = man.query(
        "SELECT * FROM items WHERE status IN ('ingested','analyzed','triaged','grouped')")
    for row in rows:
        digest = row["md5"]
        if digest.startswith("err:") or digest in seen:
            continue
        seen.add(digest)
        stats["checked"] += 1
        shard = cfg.assets_dir / digest[:2]
        if (shard / f"{digest}_a.jpg").exists() and (shard / f"{digest}_t.jpg").exists():
            continue
        src = cfg.done_dir / row["done_name"]
        if not src.exists():
            stats["no_source"] += 1
            continue
        res = _probe(str(src))
        _build((digest, str(src), str(cfg.assets_dir),
                res["meta"].duration_s, cfg.analysis_px,
                cfg.thumb_px, cfg.poster_fraction))
        stats["rebuilt"] += 1
    return stats
