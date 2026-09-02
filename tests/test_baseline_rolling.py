"""
Regression tests for issue #13: avoid a full-file scan when recomputing the
baseline.

Even with the TTL cache from issue #12, every cache-miss recompute still
called `storage.load_results(device_id, hours=168)`, parsing the whole
history CSV. `baseline.py` now keeps a per-device rolling window
(`_RollingStats`, an O(1) running sum/sum-of-squares) that is updated
incrementally via `record_latency()` on every probe (see
`collector._probe_and_record`), and `get_baseline` recomputes mean/stddev
from that accumulator in O(1) instead of re-reading the file. The only
full-file read left is a one-time per-device "cold start" seed.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import baseline


@pytest.fixture(autouse=True)
def reset_baseline_state():
    baseline._cache.clear()
    baseline._windows.clear()
    yield
    baseline._cache.clear()
    baseline._windows.clear()


def _seed_rows(n=100, latency=20.0):
    return [{"timestamp": f"2026-01-01T00:00:{i:02d}", "latency_ms": latency,
              "success": True, "jitter_ms": 0.0} for i in range(n)]


def test_cold_start_seeds_once_then_never_reads_the_file_again(monkeypatch):
    """First touch of a device seeds the rolling window from disk (one
    read). Every subsequent probe/recompute for that device, no matter how
    many, must not call load_results again."""
    calls = {"count": 0}

    def counting_load_results(device_id, hours=1):
        calls["count"] += 1
        return _seed_rows()

    monkeypatch.setattr(baseline, "load_results", counting_load_results)

    # First touch: seeds from disk.
    baseline.record_latency(1, 21.0)
    assert calls["count"] == 1

    # Many subsequent probes and baseline recomputes (bypassing the TTL
    # cache by clearing it each time) must not touch load_results again.
    for i in range(500):
        baseline.record_latency(1, 20.0 + (i % 5))
        baseline._cache.clear()  # force a recompute path every time
        baseline.get_baseline(1)

    assert calls["count"] == 1, (
        f"expected exactly one cold-start file read, got {calls['count']} - "
        "recompute is scanning the file instead of using the rolling accumulator"
    )


def test_recompute_cost_does_not_scale_with_window_size(monkeypatch):
    """A cache-miss recompute must be O(1) with respect to history size:
    computing stats for a huge window should take about the same wall time
    as for a small one (the running sum/sumsq accumulator, not an O(n) scan)."""
    import time as time_mod

    monkeypatch.setattr(baseline, "load_results", lambda device_id, hours=1: [])

    small = baseline._RollingStats()
    small.seed([20.0] * 100)

    large = baseline._RollingStats()
    large.seed([20.0] * 100_000)

    # Warm up (avoid first-call overhead skewing timing).
    small.stats()
    large.stats()

    t0 = time_mod.perf_counter()
    for _ in range(2000):
        small.stats()
    small_elapsed = time_mod.perf_counter() - t0

    t0 = time_mod.perf_counter()
    for _ in range(2000):
        large.stats()
    large_elapsed = time_mod.perf_counter() - t0

    # Generous margin: an O(n) scan of 100k items 2000 times would be
    # orders of magnitude slower than 2000 O(1) lookups. An O(1)
    # implementation should show no meaningful growth.
    assert large_elapsed < small_elapsed * 5 + 0.05, (
        f"stats() appears to scale with window size: small={small_elapsed:.4f}s "
        f"large={large_elapsed:.4f}s"
    )


def test_rolling_stats_matches_full_window_computation(monkeypatch):
    """Statistics from the O(1) accumulator must match a plain full-window
    mean/stddev computation (within rounding) for a stable stream."""
    import random
    random.seed(42)
    values = [20.0 + random.uniform(-3, 3) for _ in range(500)]

    stats = baseline._RollingStats()
    stats.seed(values)
    n, mean, variance = stats.stats()

    expected_mean = sum(values) / len(values)
    expected_variance = sum((x - expected_mean) ** 2 for x in values) / len(values)

    assert n == len(values)
    assert mean == pytest.approx(expected_mean, rel=1e-9)
    assert variance == pytest.approx(expected_variance, rel=1e-9)


def test_record_latency_and_is_anomaly_agree_on_a_clear_outlier(monkeypatch):
    monkeypatch.setattr(baseline, "load_results", lambda device_id, hours=1: [])

    for _ in range(50):
        baseline.record_latency(3, 20.0)

    baseline._cache.clear()
    assert baseline.is_anomaly(3, 20.5) is False
    baseline._cache.clear()
    assert baseline.is_anomaly(3, 500.0) is True
