#!/usr/bin/env python
"""
bench_baseline.py - Standalone benchmark for issue #14.

Demonstrates the per-probe cost of `baseline.get_baseline()` before and
after the #12/#13 fixes, against a large, realistic results CSV (default
100,000 rows spanning 7 days - matching a device on the default 5s probe
interval).

Run standalone (no pytest, no server):

    python bench_baseline.py
    python bench_baseline.py --rows 200000 --calls 500

It measures three things:

  1. BEFORE - the original algorithm: every call re-reads and re-parses the
     entire CSV via `storage.load_results(id, hours=168)` (this is exactly
     what `baseline.get_baseline` did before issue #12/#13).

  2. AFTER (cold recompute) - the current implementation's recompute path
     with the TTL cache forced to miss on every call (so it's an apples-to
     -apples comparison of "cost of a recompute", not just "cost of a cache
     hit"). This isolates the #13 fix: recompute is now O(1) against the
     rolling accumulator instead of an O(n) file scan.

  3. AFTER (steady state) - realistic repeated calls exactly as
     `collector._probe_and_record` makes them (TTL cache left alone). This
     is what per-probe cost actually looks like in production after #12.

The script is self-contained: it generates its data under a temp directory
(never touches a real `data/` tree) and prints the results; nothing here is
wired into CI or pytest.
"""
import argparse
import csv
import random
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import storage  # noqa: E402
import baseline  # noqa: E402


def generate_results_csv(path: Path, n_rows: int, span_hours: float = 168.0):
    """Write n_rows rows evenly spread across span_hours, ending "now", with
    a stable-ish latency distribution (mirrors real steady monitoring)."""
    random.seed(1234)
    start = datetime.utcnow() - timedelta(hours=span_hours)
    step = timedelta(hours=span_hours) / max(n_rows, 1)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "latency_ms", "success", "jitter_ms"])
        ts = start
        for i in range(n_rows):
            latency = round(max(1.0, random.gauss(20.0, 3.0)), 3)
            w.writerow([ts.isoformat(), latency, 1, round(random.uniform(0, 2), 3)])
            ts += step


def original_get_baseline(device_id: int):
    """The pre-#12/#13 implementation, verbatim: a full load_results(168h)
    scan and O(n) mean/stddev computation on every call. Used only for the
    BEFORE measurement."""
    import math
    rows = storage.load_results(device_id, hours=baseline.BASELINE_HOURS)
    lats = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    if len(lats) < 30:
        return None
    mean = sum(lats) / len(lats)
    variance = sum((x - mean) ** 2 for x in lats) / len(lats)
    stddev = math.sqrt(variance)
    return {"mean": round(mean, 2), "stddev": round(stddev, 2), "samples": len(lats)}


def time_calls(fn, n_calls: int) -> list[float]:
    samples = []
    for _ in range(n_calls):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return samples


def fmt(samples: list[float]) -> str:
    avg = statistics.mean(samples) * 1000
    med = statistics.median(samples) * 1000
    p95 = sorted(samples)[int(len(samples) * 0.95)] * 1000 if len(samples) > 1 else avg
    total = sum(samples) * 1000
    return f"avg={avg:.4f} ms  median={med:.4f} ms  p95={p95:.4f} ms  total={total:.1f} ms over {len(samples)} calls"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", type=int, default=100_000, help="rows in the generated results CSV")
    ap.add_argument("--calls", type=int, default=200, help="number of get_baseline calls to time per phase")
    args = ap.parse_args()

    device_id = 1
    tmp_dir = Path(tempfile.mkdtemp(prefix="pp_bench_baseline_"))
    try:
        results_dir = tmp_dir / "results"
        results_dir.mkdir()
        storage.DATA_DIR = tmp_dir
        storage.RESULTS_DIR = results_dir
        storage.TRACES_DIR = tmp_dir / "traces"
        storage.TRACES_DIR.mkdir()

        csv_path = storage._result_file(device_id)
        print(f"Generating {args.rows:,} rows spanning 7 days at {csv_path} ...")
        t0 = time.perf_counter()
        generate_results_csv(csv_path, args.rows)
        print(f"  done in {time.perf_counter() - t0:.2f}s ({csv_path.stat().st_size / 1e6:.1f} MB)\n")

        print(f"=== BEFORE (issue #3 baseline): full-file scan every call, {args.calls} calls ===")
        before_samples = time_calls(lambda: original_get_baseline(device_id), args.calls)
        print(f"  {fmt(before_samples)}\n")

        # Reset baseline module state so the AFTER measurements start cold,
        # exactly like a freshly-started process.
        baseline._cache.clear()
        baseline._windows.clear()

        print(f"=== AFTER, cold recompute every call (#13 fix, cache forced to miss), {args.calls} calls ===")

        def after_cold_recompute():
            baseline._cache.clear()  # force _compute() to run every time
            baseline.get_baseline(device_id)

        after_cold_samples = time_calls(after_cold_recompute, args.calls)
        print(f"  {fmt(after_cold_samples)}")
        print(
            "  (first call includes the one-time cold-start file read that seeds the rolling "
            f"window: {after_cold_samples[0] * 1000:.4f} ms; "
            f"remaining {len(after_cold_samples) - 1} calls avg "
            f"{statistics.mean(after_cold_samples[1:]) * 1000:.4f} ms -- true O(1) recompute cost)\n"
        )

        print(f"=== AFTER, steady state as collector.py actually calls it (#12 TTL cache live), {args.calls} calls ===")
        after_steady_samples = time_calls(lambda: baseline.get_baseline(device_id), args.calls)
        print(f"  {fmt(after_steady_samples)}\n")

        before_avg = statistics.mean(before_samples) * 1000
        after_cold_avg = statistics.mean(after_cold_samples) * 1000
        after_steady_avg = statistics.mean(after_steady_samples) * 1000

        print("=== Summary ===")
        print(f"rows in history CSV:               {args.rows:,}")
        print(f"BEFORE avg per call:                {before_avg:.4f} ms  (full-file scan every time)")
        print(f"AFTER  avg per call (cold recompute): {after_cold_avg:.4f} ms  ({before_avg / after_cold_avg if after_cold_avg else float('inf'):.0f}x faster)")
        print(f"AFTER  avg per call (steady state):   {after_steady_avg:.4f} ms  ({before_avg / after_steady_avg if after_steady_avg else float('inf'):.0f}x faster)")

        default_interval_sec = 5
        probes_per_minute = 60 / default_interval_sec
        print(f"\nAt the default {default_interval_sec}s probe interval ({probes_per_minute:.0f} probes/min per device):")
        print(f"  BEFORE: ~{before_avg * probes_per_minute:.1f} ms/min spent just re-parsing the CSV for anomaly checks")
        print(f"  AFTER:  ~{after_steady_avg * probes_per_minute:.4f} ms/min in steady state")

        # Scaling check: recompute cost (post-fix, steady-state calls) at a
        # much smaller history size, to show it does NOT grow with history
        # size the way BEFORE's full-file scan did.
        print("\n=== Scaling check: does per-call cost grow with history size? ===")
        small_rows = max(1000, args.rows // 50)
        small_csv = tmp_dir / "results" / "device_2.csv"
        generate_results_csv(small_csv, small_rows)
        baseline._cache.clear()
        baseline._windows.clear()
        small_samples = time_calls(lambda: baseline.get_baseline(2), args.calls)
        small_avg = statistics.mean(small_samples) * 1000
        print(f"  {small_rows:,}-row history, AFTER steady state avg: {small_avg:.4f} ms")
        print(f"  {args.rows:,}-row history, AFTER steady state avg: {after_steady_avg:.4f} ms")
        print("  (both should be near-identical and near-zero -- flat with history size, "
              "unlike BEFORE's per-row-scaling full scan)")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
