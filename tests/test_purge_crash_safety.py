"""
Regression test for crash-safe purge rewrites (issue #16):

    `purge_old_data` used to open the target CSV directly in "w" mode and
    write the kept rows into it. Opening in "w" mode truncates the file to
    zero bytes immediately; if the process died partway through writing the
    replacement rows, the file was left truncated/corrupt with no way to
    recover the original contents.

The rewrite now writes to a temp file in the same directory and swaps it
into place with `os.replace` (atomic on the same filesystem, on both POSIX
and Windows), so a crash mid-write can never leave a partial file behind.

`test_purge_survives_a_crash_mid_rewrite` simulates a process crash
partway through writing the replacement rows (raising after only some rows
have been written) and asserts the on-disk file at the *original* path is
still fully intact and parseable - not truncated, not half-written.
"""
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import storage


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
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


def _seed_old_rows(path: Path, n_rows: int = 2, age_days: int = 60):
    base = datetime.utcnow() - timedelta(days=age_days)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "latency_ms", "success", "jitter_ms"])
        for i in range(n_rows):
            w.writerow([(base + timedelta(seconds=i)).isoformat(), 10.0, 1, 0.5])


def _read_rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_purge_survives_a_crash_mid_rewrite(isolated_storage, monkeypatch):
    """Simulates a process crash partway through purge's rewrite (raising
    after some rows have been written to the temp file) and asserts the
    file that matters to users -- the original CSV path -- is left fully
    intact and parseable, never truncated/corrupted.

    Verified by hand against the pre-#16 implementation (open(csv_path,
    "w") and write directly into it): under the same simulated crash, the
    original file ends up truncated to only the rows written before the
    crash (half the data silently gone). Against this fix, the original
    file is completely untouched because the crash only ever corrupts the
    throwaway temp file, which is cleaned up.
    """
    device_id = 3
    path = storage._result_file(device_id)
    _seed_old_rows(path, n_rows=2, age_days=60)
    now = datetime.utcnow()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        # Rows that must survive the purge (recent).
        for i in range(20):
            w.writerow([(now - timedelta(minutes=i)).isoformat(), float(i), 1, 0.1])

    original_rows = _read_rows(path)
    assert len(original_rows) == 22

    real_writerows = csv.DictWriter.writerows

    def crashing_writerows(self, rows):
        rows = list(rows)
        # Write roughly half, flush so it actually hits disk, then blow up --
        # simulates the process dying mid-rewrite.
        half = rows[: len(rows) // 2]
        real_writerows(self, half)
        raise RuntimeError("simulated crash mid-rewrite")

    monkeypatch.setattr(csv.DictWriter, "writerows", crashing_writerows)

    with pytest.raises(RuntimeError, match="simulated crash mid-rewrite"):
        storage.purge_old_data(retention_days=30)

    # The original file must be untouched: same row count, same content,
    # still parseable -- not truncated, not half-written.
    rows_after = _read_rows(path)
    assert rows_after == original_rows, (
        "original CSV was modified/corrupted by a crash mid-rewrite; "
        f"expected {len(original_rows)} original rows, found {rows_after}"
    )

    # No stray temp file left behind in the results directory.
    leftovers = [p for p in path.parent.iterdir() if p != path]
    assert leftovers == [], f"crash left a stray temp file behind: {leftovers}"


def test_purge_still_replaces_file_correctly_on_success(isolated_storage):
    """Sanity check that the temp-file + os.replace machinery still produces
    correct output (and the correct rows_purged count) on the normal,
    non-crashing path."""
    device_id = 4
    path = storage._result_file(device_id)
    now = datetime.utcnow()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "latency_ms", "success", "jitter_ms"])
        w.writerow([(now - timedelta(days=90)).isoformat(), 5.0, 1, 0.1])
        w.writerow([(now - timedelta(days=1)).isoformat(), 7.0, 1, 0.1])

    rows_purged = storage.purge_old_data(retention_days=30)

    assert rows_purged == 1
    rows = _read_rows(path)
    assert len(rows) == 1
    assert rows[0]["latency_ms"] == "7.0"
    # No stray temp file left behind.
    leftovers = [p for p in path.parent.iterdir() if p != path]
    assert leftovers == []
