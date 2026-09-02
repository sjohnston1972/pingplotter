"""
baseline.py - Rolling baseline statistics and anomaly detection

Performance note (issues #12/#13): `get_baseline` used to call
`load_results(device_id, hours=168)` on every single probe, which opens the
device's CSV and parses every row - O(n) work per probe with n (total
history) only ever growing. With the default 5s interval that's a full
7-day-CSV parse roughly every 5 seconds per device.

Issue #12 added a per-device TTL cache in front of `get_baseline`, so most
probes reuse a cached value instead of recomputing at all. Issue #13 (this
change) replaces the recompute path itself: instead of a full `load_results`
scan, a per-device rolling window of recent latencies is kept in memory with
a running sum/sum-of-squares (`_RollingStats`), updated in O(1) via
`record_latency()` each time collector.py records a probe (mirrors
`storage.save_result`, called from the same call site). `get_baseline`
computes mean/stddev from that O(1) accumulator - so even a cache-miss
recompute no longer scales with total history size.

The only place a full CSV read still happens is a one-time, per-device
"cold start" seed the first time a device is touched in a given process
(e.g. right after a restart), matching the "fall back to a one-time file
read to seed the accumulator" guidance in issue #13. After that, this file
is never re-read for baseline purposes for the lifetime of the process.
"""
import math
import threading
import time
from collections import deque

from storage import load_results

BASELINE_HOURS = 168  # 7 days
Z_THRESHOLD = 3.0     # anomaly if value > mean + Z * stddev

# How long a computed baseline may be reused before being recomputed from the
# rolling accumulator. Recompute itself is O(1) (see _RollingStats.stats), so
# this mainly saves the (tiny) lock + dict-lookup cost on the hot path.
CACHE_TTL_SEC = 180  # 3 minutes

# Upper bound on how many recent latencies are kept per device. This bounds
# memory and keeps the one-time cold-start file read + seed itself bounded
# too, while still comfortably covering 7 days of history at the product's
# default 5s probe interval (~121k samples/week).
MAX_WINDOW_SAMPLES = 150_000

_lock = threading.Lock()
_windows: dict[int, "_RollingStats"] = {}
_cache: dict[int, tuple[float, dict | None]] = {}  # device_id -> (computed_at_monotonic, baseline)


class _RollingStats:
    """Bounded window of recent latencies with an O(1) running sum/sumsq, so
    computing mean/stddev never requires iterating the whole window, let
    alone re-reading a file."""

    __slots__ = ("window", "sum", "sumsq")

    def __init__(self, maxlen: int = MAX_WINDOW_SAMPLES):
        self.window: deque[float] = deque(maxlen=maxlen)
        self.sum = 0.0
        self.sumsq = 0.0

    def seed(self, values: list[float]):
        """Bulk-initialize from a one-time file read. Only ever called once
        per device per process (cold start)."""
        trimmed = values[-self.window.maxlen:] if self.window.maxlen else values
        self.window = deque(trimmed, maxlen=self.window.maxlen)
        self.sum = sum(trimmed)
        self.sumsq = sum(v * v for v in trimmed)

    def append(self, value: float):
        window = self.window
        if window.maxlen is not None and len(window) == window.maxlen:
            evicted = window[0]  # about to be pushed out by append()
            self.sum -= evicted
            self.sumsq -= evicted * evicted
        window.append(value)
        self.sum += value
        self.sumsq += value * value

    def stats(self) -> tuple[int, float, float]:
        """Return (n, mean, variance) in O(1)."""
        n = len(self.window)
        if n == 0:
            return 0, 0.0, 0.0
        mean = self.sum / n
        # Clamp for floating-point noise (sumsq/n - mean**2 can go slightly
        # negative for a near-constant stream).
        variance = max(self.sumsq / n - mean * mean, 0.0)
        return n, mean, variance


def _seed_values(device_id: int) -> list[float]:
    """One-time full read, only used to seed a fresh process's accumulator."""
    rows = load_results(device_id, hours=BASELINE_HOURS)
    return [r["latency_ms"] for r in rows if r["latency_ms"] is not None]


def record_latency(device_id: int, latency: float | None):
    """O(1) hook called once per probe (see collector._probe_and_record,
    right after storage.save_result) to keep the rolling accumulator current
    without ever re-reading the CSV on the hot path.

    On the very first call for a device in this process, there is no
    in-memory window yet, so we seed one from disk. That one-time read
    already picks up the row `save_result` just wrote (it's called first),
    so we do NOT also append here on the seed call - that would double
    count the latest sample.
    """
    if latency is None:
        return
    with _lock:
        stats = _windows.get(device_id)
        if stats is None:
            stats = _RollingStats()
            stats.seed(_seed_values(device_id))
            _windows[device_id] = stats
        else:
            stats.append(latency)


def _compute(device_id: int) -> dict | None:
    with _lock:
        stats = _windows.get(device_id)
        if stats is None:
            stats = _RollingStats()
            stats.seed(_seed_values(device_id))
            _windows[device_id] = stats
        n, mean, variance = stats.stats()
    if n < 30:  # not enough data yet
        return None
    stddev = math.sqrt(variance)
    return {"mean": round(mean, 2), "stddev": round(stddev, 2), "samples": n}


def get_baseline(device_id: int) -> dict | None:
    """Return the device's rolling mean/stddev, reusing a cached value for up
    to CACHE_TTL_SEC before recomputing from the O(1) accumulator."""
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
