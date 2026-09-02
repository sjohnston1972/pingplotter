"""
Regression test for the retention-purge read-modify-write race (issues #4,
#15):

    `purge_old_data` used to read a device's CSV *outside* `_lock`, then
    rewrite it *inside* `_lock`. A `save_result`/`save_trace_run` append
    landing in that unlocked gap was silently overwritten by the purge's
    rewrite (a classic read-modify-write TOCTOU) -- real monitoring history
    could be lost.

`test_purge_never_drops_a_row_written_during_the_race_window` forces the
race deterministically with a `threading.Barrier` rendezvous placed exactly
at the read/write boundary inside `purge_old_data`, rather than relying on
OS thread-scheduling luck to occasionally interleave the two operations.
"""
import builtins
import csv
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import storage


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    """Point storage.py's data paths at a throwaway directory so tests never
    touch (or depend on) a real `data/` tree."""
    data_dir = tmp_path / "data"
    results_dir = data_dir / "results"
    traces_dir = data_dir / "traces"
    results_dir.mkdir(parents=True)
    traces_dir.mkdir(parents=True)
    monkeypatch.setattr(storage, "DATA_DIR", data_dir)
    monkeypatch.setattr(storage, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(storage, "TRACES_DIR", traces_dir)
    monkeypatch.setattr(storage, "DEVICES_FILE", data_dir / "devices.json")
    return data_dir


def _seed_old_rows(path: Path, n_rows: int = 5, age_days: int = 60):
    """Write n_rows rows old enough to be purged by any reasonable
    retention window used in these tests."""
    base = datetime.utcnow() - timedelta(days=age_days)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "latency_ms", "success", "jitter_ms"])
        for i in range(n_rows):
            w.writerow([(base + timedelta(seconds=i)).isoformat(), 10.0, 1, 0.5])


def _read_rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_purge_never_drops_a_row_written_during_the_race_window(isolated_storage, monkeypatch):
    """
    Forces the exact TOCTOU race from issues #4/#15: a row appended by
    `save_result()` while `purge_old_data()` is between reading a file's
    current contents and rewriting it must survive the purge.

    To avoid a test that only *sometimes* catches the race by scheduling
    luck, a `threading.Barrier` is used to force genuine simultaneity: the
    writer thread is released to run `save_result()` at the exact instant
    `purge_old_data` finishes reading the file (both pre-fix and post-fix
    implementations read the file inside a `with open(...) as f: ... for
    row in reader: ...` block, so hooking that block's exit is a stable,
    implementation-shape-agnostic rendezvous point). After the rendezvous,
    the purge thread additionally sleeps briefly before continuing --
    unconditionally, whether or not it currently holds `_lock` -- which
    gives the writer thread a large, deterministic window to complete its
    (tiny, normally sub-millisecond) append *if* the lock is actually free
    at that point. This does not create a deadlock risk for a correct,
    fully-locked implementation: the writer only calls `barrier.wait()`
    *before* attempting to acquire `_lock`, so the purge thread's barrier
    rendezvous never depends on the writer having finished (or even
    started) its append.

    Verified by hand against the pre-fix `purge_old_data` (read outside
    `_lock`, write inside it): this test fails there because the
    concurrently-appended row is wiped out (the purge rewrites the file
    using the `kept` list it built before the append happened). Against
    the fixed implementation (whole read+write under `_lock`), it passes.
    """
    device_id = 1
    path = storage._result_file(device_id)
    _seed_old_rows(path, n_rows=5, age_days=60)

    barrier = threading.Barrier(2)
    real_open = builtins.open

    class _RaceWindowFile:
        """Transparent wrapper around the real read-mode file handle that
        rendezvouses with the writer thread exactly as the `with` block
        around the read finishes (i.e. exactly at the read/write boundary
        inside purge_old_data), then gives the writer a generous window to
        run before letting purge continue."""

        def __init__(self, real_file):
            self._real_file = real_file

        def __enter__(self):
            return self._real_file.__enter__()

        def __exit__(self, exc_type, exc, tb):
            barrier.wait(timeout=5)
            time.sleep(0.2)
            return self._real_file.__exit__(exc_type, exc, tb)

        def __getattr__(self, name):
            return getattr(self._real_file, name)

    def hooked_open(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        if str(file) == str(path) and mode == "r":
            return _RaceWindowFile(f)
        return f

    monkeypatch.setattr(storage, "open", hooked_open, raising=False)

    append_done = threading.Event()

    def writer():
        barrier.wait(timeout=5)  # released exactly as purge finishes reading
        storage.save_result(device_id, 42.0, True, 0.0)
        append_done.set()

    t = threading.Thread(target=writer)
    t.start()

    rows_purged = storage.purge_old_data(retention_days=30)

    t.join(timeout=5)
    assert append_done.is_set(), "writer thread never completed its append -- test setup bug"
    assert rows_purged == 5, "purge should have removed the 5 seeded old rows"

    rows = _read_rows(path)
    latencies = [r["latency_ms"] for r in rows]
    assert "42.0" in latencies, (
        "row appended during the purge's read/write race window was lost; "
        f"surviving rows: {rows}"
    )


def test_purge_removes_old_rows_and_keeps_recent_ones(isolated_storage):
    """Sanity check that the locking change didn't break purge's actual
    filtering behaviour or its rows_purged count."""
    device_id = 2
    path = storage._result_file(device_id)
    now = datetime.utcnow()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "latency_ms", "success", "jitter_ms"])
        w.writerow([(now - timedelta(days=90)).isoformat(), 5.0, 1, 0.1])
        w.writerow([(now - timedelta(days=60)).isoformat(), 6.0, 1, 0.1])
        w.writerow([(now - timedelta(days=1)).isoformat(), 7.0, 1, 0.1])

    rows_purged = storage.purge_old_data(retention_days=30)

    assert rows_purged == 2
    rows = _read_rows(path)
    assert len(rows) == 1
    assert rows[0]["latency_ms"] == "7.0"
