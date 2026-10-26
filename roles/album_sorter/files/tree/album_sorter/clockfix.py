"""Clock correction (spec Phase 2 step 0) - pure logic.

Mechanism: for photos carrying both a GPS UTC stamp and a camera
local stamp of the same instant, offset = gps_utc - local_reading
absorbs timezone setting + timezone error + drift in one number.
Per device, offsets should be constant; the median is applied to
every device-clock reading. A mid-batch clock reset shows up as
two tight offset clusters and is handled as a step change.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class OffsetSample:
    item_time: datetime          # raw local reading (sort key)
    offset_seconds: float        # gps_utc - local


@dataclass
class DeviceCorrection:
    # list of (segment_start_time, offset_seconds); empty = unanchored
    segments: list[tuple[datetime, float]]
    n_samples: int
    mad_minutes: float

    def offset_at(self, t: datetime) -> Optional[float]:
        off = None
        for start, o in self.segments:
            if t >= start:
                off = o
        return off

    def correct(self, t: datetime) -> Optional[datetime]:
        off = self.offset_at(t)
        return None if off is None else t + timedelta(seconds=off)


def _mad(values: list[float]) -> float:
    med = statistics.median(values)
    return statistics.median([abs(v - med) for v in values])


def estimate(samples: list[OffsetSample], min_samples: int = 3,
             max_mad_minutes: float = 10.0,
             cluster_gap_minutes: float = 30.0) -> DeviceCorrection:
    """Estimate a device's correction from its offset samples.

    Returns segments: one for a constant offset, two when a step
    change (clock reset) splits the samples into tight clusters.
    Unanchored (too few samples or too scattered) -> no segments.
    """
    if len(samples) < min_samples:
        return DeviceCorrection([], len(samples), 0.0)
    samples = sorted(samples, key=lambda s: s.item_time)
    offsets = [s.offset_seconds for s in samples]
    mad_min = _mad(offsets) / 60.0
    if mad_min <= max_mad_minutes:
        med = statistics.median(offsets)
        return DeviceCorrection([(samples[0].item_time, med)], len(samples), mad_min)

    # Try a two-cluster split at the largest offset jump in time order.
    jumps = [(abs(offsets[i + 1] - offsets[i]), i) for i in range(len(offsets) - 1)]
    jump, idx = max(jumps)
    if jump / 60.0 >= cluster_gap_minutes:
        left, right = offsets[: idx + 1], offsets[idx + 1:]
        if len(left) >= 2 and len(right) >= 2:
            if _mad(left) / 60.0 <= max_mad_minutes and _mad(right) / 60.0 <= max_mad_minutes:
                return DeviceCorrection(
                    [(samples[0].item_time, statistics.median(left)),
                     (samples[idx + 1].item_time, statistics.median(right))],
                    len(samples), max(_mad(left), _mad(right)) / 60.0)
    return DeviceCorrection([], len(samples), mad_min)


def apply_clock_corrections(cfg, man) -> dict:
    """Step-0 driver (B21): estimate each device's clock offset
    from items carrying both a GPS UTC stamp and a local
    reading, then rewrite those devices' capture_time from
    capture_time_raw. Per spec 8.2 the estimated offset applies
    device-wide: a segment's offset extends to items captured
    before the first sample (a constant clock error predates
    its evidence). Rewriting always starts from the RAW time,
    so re-runs are idempotent. Placed items are untouched -
    their times are history, not working data.
    Device key: the EXIF device string, falling back to the
    Software tag (screenshots and some editors only set one).
    """
    from datetime import datetime as _dt

    def parse(s):
        if not s:
            return None
        try:
            return _dt.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None

    rows = man.query(
        "SELECT md5, device, software, capture_time_raw, capture_time, "
        "gps_utc, status FROM items WHERE status != 'placed'")
    by_device: dict[str, list] = {}
    for r in rows:
        key = (r["device"] or r["software"] or "").strip()
        if key:
            by_device.setdefault(key, []).append(r)
    stats = {"devices": 0, "anchored": 0, "items_corrected": 0}
    for key, items in by_device.items():
        stats["devices"] += 1
        samples = []
        for r in items:
            raw, gps = parse(r["capture_time_raw"]), parse(r["gps_utc"])
            if raw and gps:
                samples.append(OffsetSample(raw, (gps - raw).total_seconds()))
        corr = estimate(samples)
        if not corr.segments:
            continue
        stats["anchored"] += 1
        first_off = corr.segments[0][1]
        for r in items:
            raw = parse(r["capture_time_raw"])
            if raw is None:
                continue
            off = corr.offset_at(raw)
            if off is None:
                off = first_off           # device-wide (spec 8.2)
            new = raw + timedelta(seconds=off)
            if r["capture_time"] != new.isoformat():
                man.update_item(r["md5"], capture_time=new.isoformat())
                stats["items_corrected"] += 1
    return stats
