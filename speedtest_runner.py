"""
speedtest_runner.py - Periodic bandwidth testing via speedtest-cli
"""
import subprocess
import json
import threading
import time
from datetime import datetime

_thread = None
_stop_event = threading.Event()


def run_once() -> dict | None:
    """Run a single speedtest and return results dict."""
    try:
        # speedtest-cli installs as "speedtest-cli", not "speedtest"
        result = subprocess.run(
            ["speedtest-cli", "--json"],
            capture_output=True, text=True, timeout=120
        )
        data = json.loads(result.stdout)
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "download_mbps": round(data["download"] / 1_000_000, 2),
            "upload_mbps": round(data["upload"] / 1_000_000, 2),
            "ping_ms": round(data["ping"], 2),
            "server": data.get("server", {}).get("name", ""),
            "isp": data.get("client", {}).get("isp", ""),
        }
    except Exception as e:
        print(f"[Speedtest] Failed: {e}")
        return None


def run_once_streaming(emit) -> dict | None:
    """Run speedtest using the Python API, streaming live speed via emit(phase, dict).

    Phases emitted:
      ("init", {})
      ("ping", {"ping_ms": float, "server": str})
      ("download", {"speed": float})          -- repeated ~every 300ms
      ("download_done", {"download_mbps": float})
      ("upload", {"speed": float, "download_mbps": float})  -- repeated ~every 300ms
      ("complete", {full result dict})
      ("error", {"message": str})
    """
    try:
        import speedtest as _st
        s = _st.Speedtest()
        emit("init", {})

        s.get_best_server()
        ping = round(s.results.ping, 1)
        server = (s.results.server or {}).get("name", "")
        emit("ping", {"ping_ms": ping, "server": server})

        # ── Download ──────────────────────────────────────────────────────────
        emit("download", {"speed": 0})
        dl_done = threading.Event()

        def _poll_dl():
            prev_b, prev_t, smooth = 0, time.perf_counter(), 0.0
            while not dl_done.is_set():
                time.sleep(0.3)
                cur_b = getattr(s.results, "bytes_received", 0)
                cur_t = time.perf_counter()
                dt = cur_t - prev_t
                delta = cur_b - prev_b
                if dt > 0 and delta > 0:
                    raw = delta * 8 / (dt * 1_000_000)
                    smooth = 0.6 * smooth + 0.4 * raw if smooth else raw
                    emit("download", {"speed": round(smooth, 1)})
                prev_b, prev_t = cur_b, cur_t

        t = threading.Thread(target=_poll_dl, daemon=True)
        t.start()
        dl_bps = s.download()
        dl_done.set()
        t.join(timeout=1)

        dl_mbps = round(dl_bps / 1_000_000, 2)
        emit("download_done", {"download_mbps": dl_mbps})

        # ── Upload ────────────────────────────────────────────────────────────
        emit("upload", {"speed": 0, "download_mbps": dl_mbps})
        ul_done = threading.Event()

        def _poll_ul():
            prev_b, prev_t, smooth = 0, time.perf_counter(), 0.0
            while not ul_done.is_set():
                time.sleep(0.3)
                cur_b = getattr(s.results, "bytes_sent", 0)
                cur_t = time.perf_counter()
                dt = cur_t - prev_t
                delta = cur_b - prev_b
                if dt > 0 and delta > 0:
                    raw = delta * 8 / (dt * 1_000_000)
                    smooth = 0.6 * smooth + 0.4 * raw if smooth else raw
                    emit("upload", {"speed": round(smooth, 1), "download_mbps": dl_mbps})
                prev_b, prev_t = cur_b, cur_t

        t2 = threading.Thread(target=_poll_ul, daemon=True)
        t2.start()
        ul_bps = s.upload()
        ul_done.set()
        t2.join(timeout=1)

        ul_mbps = round(ul_bps / 1_000_000, 2)
        isp = (s.results.client or {}).get("isp", "")

        result = {
            "timestamp": datetime.utcnow().isoformat(),
            "download_mbps": dl_mbps,
            "upload_mbps": ul_mbps,
            "ping_ms": ping,
            "server": server,
            "isp": isp,
        }
        emit("complete", result)
        return result
    except Exception as e:
        print(f"[Speedtest streaming] Failed: {e}")
        emit("error", {"message": str(e)})
        return None


def _loop(interval_minutes: int, stop: threading.Event):
    while not stop.is_set():
        import storage
        result = run_once()
        if result:
            storage.save_speedtest(result)
            print(f"[Speedtest] ↓{result['download_mbps']} ↑{result['upload_mbps']} ping:{result['ping_ms']}ms")
        stop.wait(interval_minutes * 60)


def start(interval_minutes: int = 60):
    global _thread, _stop_event
    _stop_event = threading.Event()
    _thread = threading.Thread(target=_loop, args=(interval_minutes, _stop_event), daemon=True)
    _thread.start()


def stop():
    if _stop_event:
        _stop_event.set()
