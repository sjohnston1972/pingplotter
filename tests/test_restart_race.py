"""
Regression test for the device-restart race (issue #6/#7/#8):

    Editing a device (PATCH -> collector.restart_device) or clicking
    "Ping now" must never leave a device with zero live monitoring threads.

The old `restart_device()` did `stop_device(); time.sleep(0.5); start_device()`.
`start_device()` bails out if the outgoing thread is still `is_alive()`. If the
outgoing thread is busy running a probe (not idle in `stop_event.wait()`) at
the moment of restart, a fixed 0.5s sleep is not a reliable bound on how long
it takes to finish, so `start_device()` can see it still alive and refuse to
start a replacement -- the device is left with no thread and never recovers.

These tests stub out all real I/O (network probes, disk-backed storage,
alerting) so they run in a few seconds with no network access and no
dependency on real ping/traceroute binaries.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import collector


def _make_device(device_id=1, interval_sec=5):
    return {
        "id": device_id,
        "name": f"device-{device_id}",
        "host": "example.invalid",
        "probe_type": "icmp",
        "interval_sec": interval_sec,
        "packet_size": None,
        "df_bit": False,
        "enabled": True,
        "thresholds": {},
    }


@pytest.fixture(autouse=True)
def isolate_collector(monkeypatch):
    """Stub every side-effecting dependency collector.py touches, and reset
    its module-level state before/after each test so tests never depend on
    real network probes, real files, or leftover threads from a prior test.
    """
    devices = {}

    monkeypatch.setattr(collector, "load_devices", lambda: list(devices.values()))
    monkeypatch.setattr(collector, "save_result", lambda *a, **k: None)
    monkeypatch.setattr(collector, "save_trace_run", lambda *a, **k: None)
    monkeypatch.setattr(collector, "check_and_alert", lambda *a, **k: None)
    monkeypatch.setattr("baseline.is_anomaly", lambda *a, **k: False)

    collector._threads.clear()
    collector._stop_flags.clear()
    collector._status.clear()
    collector._streak.clear()
    collector._last_route.clear()

    yield devices

    # Stop anything still running so tests don't leak threads into each other.
    for device_id in list(collector._stop_flags.keys()):
        collector.stop_device(device_id)
    for t in list(collector._threads.values()):
        if t.is_alive():
            t.join(timeout=2.0)
    collector._threads.clear()
    collector._stop_flags.clear()
    collector._status.clear()
    collector._streak.clear()
    collector._last_route.clear()


def _wait_until(predicate, timeout=3.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_restart_leaves_a_live_thread_when_probe_is_slow(isolate_collector, monkeypatch):
    """Reproduces the exact race: the outgoing thread is busy inside a slow
    probe (not idle in stop_event.wait()) at the moment restart_device() is
    called. Against the pre-fix implementation this leaves the device with
    no running thread at all, permanently.
    """
    devices = isolate_collector
    devices[1] = _make_device(1, interval_sec=5)

    def slow_probe(*a, **k):
        # Simulate a real, slow network probe: the thread is executing this
        # call (not blocked in stop_event.wait()) when restart is triggered.
        time.sleep(1.0)
        return True, 1.0

    monkeypatch.setattr(collector, "_ping_once", slow_probe)

    collector.start_device(1)
    assert _wait_until(lambda: 1 in collector._threads and collector._threads[1].is_alive())

    # Give the thread a moment to be inside the slow probe, not in wait().
    time.sleep(0.1)
    old_thread = collector._threads[1]

    collector.restart_device(1)

    # Make sure the OLD thread has actually finished (it was ~0.9s into a 1.0s
    # sleep when restart was called) before checking anything -- otherwise a
    # still-alive stale thread could be mistaken for a successful restart.
    old_thread.join(timeout=3.0)
    assert not old_thread.is_alive(), "test setup bug: old thread did not finish in time"

    # Whatever the outgoing thread was doing, monitoring must recover: there
    # must now be a *different*, live thread for device 1.
    current_thread = collector._threads.get(1)
    assert current_thread is not None, "device 1 has no thread recorded after restart_device() -- monitoring died"
    assert current_thread is not old_thread, "restart_device() never replaced the outgoing thread"
    assert current_thread.is_alive(), "device 1's replacement thread is not alive -- monitoring died"


def test_repeated_restart_always_keeps_exactly_one_live_thread(isolate_collector, monkeypatch):
    """Exercises the actual race repeatedly (not once): restart a device many
    times in a row and assert, after every single restart, that exactly one
    live thread is running and the previous thread has actually terminated
    (no duplicate loops racing each other).
    """
    devices = isolate_collector
    devices[1] = _make_device(1, interval_sec=0.05)

    probe_calls = {"count": 0}

    def counting_probe(*a, **k):
        probe_calls["count"] += 1
        return True, 1.0

    monkeypatch.setattr(collector, "_ping_once", counting_probe)

    collector.start_device(1)
    assert _wait_until(lambda: 1 in collector._threads and collector._threads[1].is_alive())

    seen_threads = set()
    ITERATIONS = 25
    for i in range(ITERATIONS):
        old_thread = collector._threads.get(1)
        collector.restart_device(1)
        new_thread = collector._threads.get(1)

        assert new_thread is not None, f"iteration {i}: no thread object recorded after restart"
        assert new_thread.is_alive(), f"iteration {i}: thread for device 1 is not alive after restart"
        assert new_thread is not old_thread, f"iteration {i}: restart did not create a new thread"

        # The outgoing thread must actually be gone (or gone shortly), so we
        # never have two loops racing for the same device.
        if old_thread is not None:
            old_thread.join(timeout=2.0)
            assert not old_thread.is_alive(), f"iteration {i}: old thread for device 1 is still running"

        seen_threads.add(new_thread.ident)

        # A little run time between restarts, like real usage (edits aren't
        # usually back-to-back with zero delay).
        time.sleep(0.02)

    # Every restart produced a genuinely distinct thread.
    assert len(seen_threads) == ITERATIONS

    # Monitoring is still actually producing results afterwards, at roughly
    # the expected single-loop rate (not silently dead, not double-counting
    # because two loops are alive at once).
    before = probe_calls["count"]
    assert _wait_until(lambda: probe_calls["count"] > before, timeout=2.0), (
        "no further probes were recorded after the last restart -- monitoring stopped"
    )
    time.sleep(0.3)
    with collector._lifecycle_lock:
        alive_threads = [t for t in collector._threads.values() if t.is_alive()]
    assert len(alive_threads) == 1, f"expected exactly 1 live thread for device 1, found {len(alive_threads)}"


def test_ping_now_does_not_disturb_the_running_loop(isolate_collector, monkeypatch):
    """probe_now() (used by /ping-now) must record an immediate result without
    restarting or killing the device's scheduled loop thread.
    """
    devices = isolate_collector
    devices[1] = _make_device(1, interval_sec=5)

    monkeypatch.setattr(collector, "_ping_once", lambda *a, **k: (True, 2.0))

    collector.start_device(1)
    assert _wait_until(lambda: 1 in collector._threads and collector._threads[1].is_alive())
    running_thread = collector._threads[1]

    ok = collector.probe_now(1)
    assert ok is True

    # Same thread object, still alive -- probe_now must not touch the loop.
    assert collector._threads[1] is running_thread
    assert running_thread.is_alive()

    status = collector.get_device_status(1)
    assert status is not None
    assert status["last_latency"] == 2.0
