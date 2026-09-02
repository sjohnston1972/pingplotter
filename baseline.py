"""
baseline.py - Rolling baseline statistics and anomaly detection

Performance note (issue #12): `get_baseline` used to call
`load_results(device_id, hours=168)` - a full parse of the device's 7-day
results CSV - on *every* probe (via `is_anomaly`, called from
`collector._probe_and_record` after every single probe). With the default
5s interval that's a full-file read+parse roughly every 5 seconds per
device, even though the underlying mean/stddev barely moves between probes.

`get_baseline` now caches its result per device for `CACHE_TTL_SEC`, so
repeated calls within the TTL window reuse the cached `{mean, stddev,
samples}` instead of re-reading the CSV. The recompute path itself is
unchanged for now (still a full `load_results` scan) - issue #13 tracks
replacing that with an O(1) rolling accumulator so even a cache-miss
recompute stops scaling with total history size.
"""
import math
import threading
import time

from storage import load_results

BASELINE_HOURS = 168  # 7 days
Z_THRESHOLD = 3.0     # anomaly if value > mean + Z * stddev

# How long a computed baseline may be reused before being recomputed from a
# fresh full-file read. The baseline changes slowly, so most probes within
# this window reuse the cached value instead of re-reading the CSV.
CACHE_TTL_SEC = 180  # 3 minutes

_lock = threading.Lock()
_cache: dict[int, tuple[float, dict | None]] = {}  # device_id -> (computed_at_monotonic, baseline)


def _compute(device_id: int) -> dict | None:
    """Compute rolling mean and stddev from last 7 days of results."""
    rows = load_results(device_id, hours=BASELINE_HOURS)
    lats = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    if len(lats) < 30:   # not enough data yet
        return None
    mean = sum(lats) / len(lats)
    variance = sum((x - mean) ** 2 for x in lats) / len(lats)
    stddev = math.sqrt(variance)
    return {"mean": round(mean, 2), "stddev": round(stddev, 2), "samples": len(lats)}


def get_baseline(device_id: int) -> dict | None:
    """Return the device's rolling mean/stddev, reusing a cached value for up
    to CACHE_TTL_SEC before recomputing (full-file read, for now) again."""
    now = time.monotonic()
    with _lock:
        cached = _cache.get(device_id)
        if cached is not None and (now - cached[0]) < CACHE_TTL_SEC:
            return cached[1]
    baseline = _compute(device_id)
    with _lock:
        _cache[device_id] = (now, baseline)
    return baseline


def is_anomaly(device_id: int, latency: float) -> bool:
    """Return True if latency is statistically anomalous vs. 7-day baseline."""
    baseline = get_baseline(device_id)
    if baseline is None:
        return False
    threshold = baseline["mean"] + Z_THRESHOLD * baseline["stddev"]
    return latency > threshold and latency > baseline["mean"] * 1.5
