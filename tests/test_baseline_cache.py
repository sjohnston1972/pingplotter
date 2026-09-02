"""
Regression tests for baseline caching (issue #12):

    `baseline.get_baseline` used to call `storage.load_results(device_id,
    hours=168)` - opening and parsing the device's entire 7-day results CSV
    - on every call. Since `collector._probe_and_record` calls it (via
    `is_anomaly`) after every single probe, this meant a full-file read on
    every probe.

`get_baseline` now caches its computed `{mean, stddev, samples}` per device
for `CACHE_TTL_SEC`. These tests assert that within the TTL, repeated calls
do not touch `storage.load_results` again, and that correctness (values,
`is_anomaly` behaviour, cache expiry) is unaffected.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import baseline


@pytest.fixture(autouse=True)
def reset_baseline_cache():
    """baseline.py keeps module-level cache/accumulator state; reset it
    around every test so tests can't see each other's cached values or
    rolling windows."""
    baseline._cache.clear()
    baseline._windows.clear()
    yield
    baseline._cache.clear()
    baseline._windows.clear()


def _stable_rows(n=100, latency=20.0):
    return [{"timestamp": f"2026-01-01T00:00:{i:02d}", "latency_ms": latency,
              "success": True, "jitter_ms": 0.0} for i in range(n)]


def test_repeated_get_baseline_within_ttl_does_not_reread_the_csv(monkeypatch):
    calls = {"count": 0}

    def counting_load_results(device_id, hours=1):
        calls["count"] += 1
        return _stable_rows()

    monkeypatch.setattr(baseline, "load_results", counting_load_results)
    monkeypatch.setattr(baseline.time, "monotonic", lambda: 1000.0)

    first = baseline.get_baseline(42)
    assert calls["count"] == 1

    for _ in range(20):
        again = baseline.get_baseline(42)
        assert again == first

    assert calls["count"] == 1, (
        f"expected load_results to be called exactly once within the TTL window, "
        f"was called {calls['count']} times"
    )


def test_get_baseline_recomputes_after_ttl_expires(monkeypatch):
    """After the TTL expires, get_baseline must reflect newly-recorded data
    (via the O(1) rolling accumulator, issue #13) rather than keep serving
    the stale cached value forever."""
    current_time = {"t": 1000.0}

    monkeypatch.setattr(baseline, "load_results", lambda device_id, hours=1: _stable_rows(latency=20.0))
    monkeypatch.setattr(baseline.time, "monotonic", lambda: current_time["t"])

    first = baseline.get_baseline(42)
    assert first["mean"] == pytest.approx(20.0)

    # Feed very different values into the accumulator, as collector.py would
    # via record_latency() on every probe.
    for _ in range(50):
        baseline.record_latency(42, 100.0)

    # Still within the TTL: the cached value must be reused, unaffected by
    # the new data that just came in.
    still_cached = baseline.get_baseline(42)
    assert still_cached == first

    current_time["t"] += baseline.CACHE_TTL_SEC + 1
    recomputed = baseline.get_baseline(42)
    assert recomputed["mean"] > first["mean"], (
        "baseline should recompute (and reflect newly recorded data) once the TTL has expired"
    )


def test_get_baseline_and_is_anomaly_values_are_unaffected_by_caching(monkeypatch):
    """Correctness check: caching must not change what get_baseline/is_anomaly
    return for a given dataset."""
    rows = [{"timestamp": f"2026-01-01T00:00:{i:02d}", "latency_ms": 20.0,
              "success": True, "jitter_ms": 0.0} for i in range(50)]
    # A handful of outliers mixed in so mean/stddev are non-trivial.
    for i, v in enumerate([21.0, 19.0, 22.0, 18.0, 20.5]):
        rows[i]["latency_ms"] = v

    monkeypatch.setattr(baseline, "load_results", lambda device_id, hours=1: rows)

    result = baseline.get_baseline(7)
    assert result is not None
    assert result["samples"] == 50
    assert result["mean"] == pytest.approx(20.0, abs=0.2)

    # A value far outside the tight distribution should be flagged anomalous...
    assert baseline.is_anomaly(7, 500.0) is True
    # ...while a normal value should not be.
    assert baseline.is_anomaly(7, 20.0) is False


def test_get_baseline_returns_none_with_insufficient_samples(monkeypatch):
    monkeypatch.setattr(baseline, "load_results", lambda device_id, hours=1: _stable_rows(n=5))
    assert baseline.get_baseline(9) is None
    assert baseline.is_anomaly(9, 1000.0) is False
