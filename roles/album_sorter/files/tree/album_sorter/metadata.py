"""Metadata extraction and timestamp resolution (spec Phase 0).

Photos: EXIF via Pillow (production on the container may swap in
exiftool behind this same interface). Video: ffprobe JSON.
Timestamp priority (spec Section 5): EXIF DateTimeOriginal ->
filename pattern -> video creation_time -> mtime.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".dng"}

# EXIF tag ids
TAG_MAKE, TAG_MODEL, TAG_SOFTWARE = 271, 272, 305
EXIF_DT_ORIGINAL, EXIF_OFFSET_ORIGINAL = 36867, 36881
GPS_TAGS = {0: "ver", 1: "lat_ref", 2: "lat", 3: "lon_ref", 4: "lon", 5: "alt_ref",
            6: "alt", 7: "time", 29: "date"}


@dataclass
class MediaMeta:
    device: str = ""
    software: str = ""
    dt_original: Optional[datetime] = None      # naive, camera-local reading
    dt_offset_minutes: Optional[int] = None     # OffsetTimeOriginal, if present
    gps_lat: Optional[float] = None
    gps_lon: Optional[float] = None
    gps_utc: Optional[datetime] = None          # aware, from GPS date+time
    video_creation: Optional[datetime] = None   # aware UTC, from container tags
    duration_s: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None


def _dms_to_deg(dms, ref: str) -> Optional[float]:
    try:
        deg = float(dms[0]) + float(dms[1]) / 60.0 + float(dms[2]) / 3600.0
        return -deg if ref in ("S", "W") else deg
    except Exception:
        return None


def extract_image_meta(path: Path) -> MediaMeta:
    from PIL import Image
    meta = MediaMeta()
    with Image.open(path) as im:
        meta.width, meta.height = im.size
        exif = im.getexif()
        if not exif:
            return meta
        meta.device = " ".join(
            str(exif.get(t, "")).strip() for t in (TAG_MAKE, TAG_MODEL)).strip()
        meta.software = str(exif.get(TAG_SOFTWARE, "")).strip()
        try:
            ifd = exif.get_ifd(0x8769)
        except Exception:
            ifd = {}
        raw_dt = ifd.get(EXIF_DT_ORIGINAL) or exif.get(306)
        if raw_dt:
            try:
                meta.dt_original = datetime.strptime(str(raw_dt)[:19], "%Y:%m:%d %H:%M:%S")
            except ValueError:
                pass
        raw_off = ifd.get(EXIF_OFFSET_ORIGINAL)
        if raw_off:
            m = re.match(r"([+-])(\d{2}):(\d{2})", str(raw_off))
            if m:
                sign = 1 if m.group(1) == "+" else -1
                meta.dt_offset_minutes = sign * (int(m.group(2)) * 60 + int(m.group(3)))
        try:
            gps_ifd = exif.get_ifd(0x8825)
        except Exception:
            gps_ifd = {}
        if gps_ifd:
            gps = {GPS_TAGS.get(k, k): v for k, v in gps_ifd.items()}
            meta.gps_lat = _dms_to_deg(gps.get("lat"), gps.get("lat_ref", "N")) if gps.get("lat") else None
            meta.gps_lon = _dms_to_deg(gps.get("lon"), gps.get("lon_ref", "E")) if gps.get("lon") else None
            if gps.get("date") and gps.get("time"):
                try:
                    t = gps["time"]
                    meta.gps_utc = datetime(
                        int(str(gps["date"])[:4]), int(str(gps["date"])[5:7]), int(str(gps["date"])[8:10]),
                        int(float(t[0])), int(float(t[1])), int(float(t[2])),
                        tzinfo=timezone.utc)
                except Exception:
                    meta.gps_utc = None
    return meta


def extract_video_meta(path: Path) -> MediaMeta:
    meta = MediaMeta()
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60)
    data = json.loads(out.stdout or "{}")
    tags = (data.get("format", {}).get("tags") or {})
    ct = tags.get("creation_time")
    if ct:
        try:
            meta.video_creation = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except ValueError:
            pass
    dur = data.get("format", {}).get("duration")
    meta.duration_s = float(dur) if dur else None
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            meta.width, meta.height = s.get("width"), s.get("height")
            make = (s.get("tags") or {}).get("make", "")
            model = (s.get("tags") or {}).get("model", "")
            meta.device = f"{make} {model}".strip()
            break
    return meta


# ---- filename timestamp patterns -------------------------------------------
_PXL = re.compile(r"PXL_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(\d{3})")
_RECORD = re.compile(r"record-(\d{10,13})")
_SCREENSHOT = re.compile(r"Screenshot_(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})")


def filename_time(name: str) -> Optional[datetime]:
    # every branch is guarded: a name that LOOKS like a pattern
    # but encodes an impossible date (month 13, hour 25, an
    # absurd epoch) must degrade to "no filename time", never
    # raise - an unguarded raise made such files permanent
    # ingest ERRORs (review F5 tail)
    m = _PXL.search(name)
    if m:
        try:
            y, mo, d, h, mi, s, _ms = (int(g) for g in m.groups())
            return datetime(y, mo, d, h, mi, s)
        except ValueError:
            pass
    m = _SCREENSHOT.search(name)
    if m:
        try:
            y, mo, d, h, mi, s = (int(g) for g in m.groups())
            return datetime(y, mo, d, h, mi, s)
        except ValueError:
            pass
    m = _RECORD.search(name)
    if m:
        try:
            ts = int(m.group(1))
            if ts > 10**12:
                ts //= 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except (ValueError, OverflowError, OSError):
            pass
    return None


SCREENSHOT_SOFTWARE = ("screenshot", "snipping", "lightshot", "sharex", "clean shot")
SCREENSHOT_NAME = re.compile(r"(screenshot|screen[_ ]?record|screenrecording)", re.I)
FB_HINT = re.compile(r"(FB_IMG|received_|Messenger|WhatsApp|reddit|download)", re.I)


@dataclass
class ResolvedTime:
    raw: Optional[datetime]
    source: str          # exif_dto / filename / video_meta / mtime
    confidence: str      # high / medium / low


def resolve_time(path: Path, meta: MediaMeta) -> ResolvedTime:
    if meta.dt_original is not None:
        conf = "high" if meta.dt_offset_minutes is not None else "medium"
        return ResolvedTime(meta.dt_original, "exif_dto", conf)
    ft = filename_time(path.name)
    if ft is not None:
        return ResolvedTime(ft, "filename", "medium")
    if meta.video_creation is not None:
        return ResolvedTime(meta.video_creation.replace(tzinfo=None), "video_meta", "medium")
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)
    return ResolvedTime(mtime, "mtime", "low")


def pre_rule_category(name: str, meta: MediaMeta) -> Optional[tuple[str, float, str]]:
    """Hard deterministic triage (spec Phase 0 step 8).
    Returns (category, confidence, source) or None."""
    if meta.software and any(s in meta.software.lower() for s in SCREENSHOT_SOFTWARE):
        return ("MISC", 0.95, "prerule")
    if SCREENSHOT_NAME.search(name):
        return ("MISC", 0.9, "prerule")
    return None


def soft_hints(name: str, meta: MediaMeta) -> list[str]:
    hints = []
    if FB_HINT.search(name):
        hints.append("filename suggests a downloaded/shared image")
    if meta.software:
        hints.append(f"software tag: {meta.software}")
    return hints
