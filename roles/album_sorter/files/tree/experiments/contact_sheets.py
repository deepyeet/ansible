#!/usr/bin/env python3
"""Stage 2 Step 1 (spec v0.9): window + contact sheets.

Deterministic, local, zero API calls. Takes a folder of photos, sorts them
by corrected time (EXIF DateTimeOriginal, falling back to file mtime),
assigns a stable global index, splits them into calendar-month windows,
and renders 5x5 contact sheets with each cell labeled with the item's
global index and timestamp. Emits a metadata table (CSV + JSON) per
window: index, timestamp, device, GPS where present.

Clock correction (Step 0) is applied upstream: pass --offsets JSON of
{"device_model": seconds} to shift timestamps per device before sorting.
Items shifted across a month boundary land in the corrected month's
window, which is the point of correcting before windowing.

Usage:
    python3 contact_sheets.py INPUT_DIR OUTPUT_DIR [--offsets offsets.json]
                              [--overlap-days N] [--cell PX]

Sheets are JPEGs named sheet_0001.jpg... inside OUTPUT_DIR/YYYY-MM/.
Overlap items (pulled in from adjacent months when --overlap-days > 0)
are rendered after the window's own items and flagged overlap=true in
the metadata table; the group-by model sees them for context and the
stitching step (Step 4) dedupes by global index.
"""

import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL.ExifTags import GPSTAGS, TAGS

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
GRID = 5
LABEL_H = 28


def _gps_decimal(gps):
    def conv(vals):
        try:
            d, m, s = (float(v) for v in vals)
            return d + m / 60.0 + s / 3600.0
        except Exception:
            return None

    lat = conv(gps.get(2)) if gps.get(2) else None
    lon = conv(gps.get(4)) if gps.get(4) else None
    if lat is None or lon is None:
        return None, None
    if gps.get(1) == "S":
        lat = -lat
    if gps.get(3) == "W":
        lon = -lon
    return lat, lon


def read_item(path):
    """Return dict with timestamp (naive local), device, gps for one image."""
    ts = None
    device = None
    lat = lon = None
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if exif:
                device = exif.get(272)  # Model
                dt = None
                try:
                    ifd = exif.get_ifd(0x8769)
                    dt = ifd.get(36867) or ifd.get(36868)
                except Exception:
                    pass
                dt = dt or exif.get(306)
                if dt:
                    try:
                        ts = datetime.strptime(str(dt).strip(), "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        ts = None
                try:
                    gps_ifd = exif.get_ifd(0x8825)
                    if gps_ifd:
                        gps = {GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
                        lat, lon = _gps_decimal(gps)
                except Exception:
                    pass
    except Exception:
        pass
    if ts is None:
        ts = datetime.fromtimestamp(path.stat().st_mtime)
    return {
        "path": str(path),
        "filename": path.name,
        "timestamp": ts,
        "device": device or "",
        "lat": lat,
        "lon": lon,
    }


def collect(input_dir, offsets):
    items = []
    for p in sorted(Path(input_dir).rglob("*")):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            items.append(read_item(p))
    for it in items:
        shift = offsets.get(it["device"], 0)
        it["timestamp_raw"] = it["timestamp"]
        it["timestamp"] = it["timestamp"] + timedelta(seconds=shift)
        it["clock_shift_s"] = shift
    items.sort(key=lambda x: (x["timestamp"], x["filename"]))
    for i, it in enumerate(items):
        it["index"] = i
    return items


def render_sheet(chunk, out_path, cell):
    cols, rows = GRID, GRID
    sheet = Image.new("RGB", (cols * cell, rows * (cell + LABEL_H)), "black")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=16)
    except TypeError:
        font = ImageFont.load_default()
    for slot, it in enumerate(chunk):
        r, c = divmod(slot, cols)
        x, y = c * cell, r * (cell + LABEL_H)
        try:
            with Image.open(it["path"]) as im:
                im = im.convert("RGB")
                im.thumbnail((cell, cell))
                ox = x + (cell - im.width) // 2
                oy = y + (cell - im.height) // 2
                sheet.paste(im, (ox, oy))
        except Exception:
            draw.rectangle([x, y, x + cell, y + cell], fill=(60, 0, 0))
        label = f"#{it['index']} {it['timestamp'].strftime('%Y-%m-%d %H:%M')}"
        if it.get("overlap"):
            label += " (ov)"
        draw.rectangle([x, y + cell, x + cell, y + cell + LABEL_H], fill=(30, 30, 30))
        draw.text((x + 6, y + cell + 6), label, fill=(255, 255, 120), font=font)
    sheet.save(out_path, quality=88)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input_dir")
    ap.add_argument("output_dir")
    ap.add_argument("--offsets", default=None, help="JSON {device_model: seconds}")
    ap.add_argument("--overlap-days", type=int, default=0)
    ap.add_argument("--cell", type=int, default=320)
    args = ap.parse_args()

    offsets = {}
    if args.offsets:
        offsets = json.loads(Path(args.offsets).read_text())

    items = collect(args.input_dir, offsets)
    if not items:
        print("no images found", file=sys.stderr)
        return 1

    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    windows = {}
    for it in items:
        key = it["timestamp"].strftime("%Y-%m")
        windows.setdefault(key, []).append(it)

    summary = {"total_items": len(items), "windows": {}}
    per_sheet = GRID * GRID
    for key in sorted(windows):
        own = windows[key]
        chunk_items = list(own)
        if args.overlap_days > 0:
            start = own[0]["timestamp"].replace(day=1)
            nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            lo, hi = start - timedelta(days=args.overlap_days), nxt + timedelta(days=args.overlap_days)
            seen = {i["index"] for i in own}
            for it in items:
                if it["index"] not in seen and lo <= it["timestamp"] < hi:
                    dup = dict(it)
                    dup["overlap"] = True
                    chunk_items.append(dup)
            chunk_items.sort(key=lambda x: x["timestamp"])
        for it in chunk_items:
            it.setdefault("overlap", False)

        wdir = out_root / key
        wdir.mkdir(parents=True, exist_ok=True)
        n_sheets = math.ceil(len(chunk_items) / per_sheet)
        for s in range(n_sheets):
            render_sheet(chunk_items[s * per_sheet:(s + 1) * per_sheet],
                         wdir / f"sheet_{s + 1:04d}.jpg", args.cell)

        with open(wdir / "metadata.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["index", "timestamp", "filename", "device", "lat", "lon",
                        "clock_shift_s", "overlap", "path"])
            for it in chunk_items:
                w.writerow([it["index"], it["timestamp"].isoformat(sep=" "),
                            it["filename"], it["device"],
                            "" if it["lat"] is None else f"{it['lat']:.6f}",
                            "" if it["lon"] is None else f"{it['lon']:.6f}",
                            it["clock_shift_s"], str(it["overlap"]).lower(), it["path"]])
        with open(wdir / "metadata.json", "w") as f:
            json.dump([{**it,
                        "timestamp": it["timestamp"].isoformat(sep=" "),
                        "timestamp_raw": it["timestamp_raw"].isoformat(sep=" ")}
                       for it in chunk_items], f, indent=1, default=str)
        summary["windows"][key] = {"own_items": len(own),
                                   "rendered_items": len(chunk_items),
                                   "sheets": n_sheets}

    with open(out_root / "summary.json", "w") as f:
        json.dump(summary, f, indent=1)
    total_sheets = sum(w["sheets"] for w in summary["windows"].values())
    print(f"{len(items)} items, {len(windows)} windows, {total_sheets} sheets "
          f"-> {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
