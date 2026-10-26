"""Derived assets (spec Section 3): analysis copies, thumbnails,
video poster frames. Keyed by MD5 under the work assets dir.
Originals are never modified."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .metadata import VIDEO_EXTS


def _downscale(src: Path, dst: Path, long_edge: int) -> Path:
    from PIL import Image
    if dst.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    # write-then-rename (F13): a worker killed mid-save must not
    # leave a truncated JPEG that every later run treats as valid
    tmp = dst.with_name(dst.name + ".tmp")
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((long_edge, long_edge))
        im.save(tmp, "JPEG", quality=85)
    tmp.replace(dst)
    return dst


def poster_frame(video: Path, work: Path, fraction: float, duration_s: float | None) -> Path:
    dst = work / f"{video.stem}_poster.jpg"
    if dst.exists():
        return dst
    work.mkdir(parents=True, exist_ok=True)
    at = (duration_s or 4.0) * fraction
    tmp = work / f"{video.stem}_poster.tmp.jpg"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", str(video),
         "-frames:v", "1", "-q:v", "3", str(tmp)],
        check=True, timeout=120)
    tmp.replace(dst)
    return dst


def build_assets(md5: str, src: Path, assets_dir: Path,
                 duration_s: float | None, analysis_px: int,
                 thumb_px: int, poster_fraction: float) -> dict[str, Path]:
    """Return {analysis, thumb} paths for an item, creating them."""
    out_dir = assets_dir / md5[:2]
    out_dir.mkdir(parents=True, exist_ok=True)
    base = src
    if src.suffix.lower() in VIDEO_EXTS:
        base = poster_frame(src, out_dir, poster_fraction, duration_s)
    return {
        "analysis": _downscale(base, out_dir / f"{md5}_a.jpg", analysis_px),
        "thumb": _downscale(base, out_dir / f"{md5}_t.jpg", thumb_px),
    }
