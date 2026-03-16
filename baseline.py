"""
baseline.py - Rolling baseline statistics and anomaly detection
"""
import math
from storage import load_results

BASELINE_HOURS = 168  # 7 days
Z_THRESHOLD = 3.0     # anomaly if value > mean + Z * stddev


def get_baseline(device_id: int) -> dict | None:
    """Compute rolling mean and stddev from last 7 days of results."""
    rows = load_results(device_id, hours=BASELINE_HOURS)
    lats = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    if len(lats) < 30:   # not enough data yet
        return None
    mean = sum(lats) / len(lats)
    variance = sum((x - mean) ** 2 for x in lats) / len(lats)
    stddev = math.sqrt(variance)
    return {"mean": round(mean, 2), "stddev": round(stddev, 2), "samples": len(lats)}


def is_anomaly(device_id: int, latency: float) -> bool:
    """Return True if latency is statistically anomalous vs. 7-day baseline."""
    baseline = get_baseline(device_id)
    if baseline is None:
        return False
    threshold = baseline["mean"] + Z_THRESHOLD * baseline["stddev"]
    return latency > threshold and latency > baseline["mean"] * 1.5
