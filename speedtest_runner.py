"""
speedtest_runner.py - Periodic bandwidth testing via speedtest-cli
"""
import subprocess
import json
import threading
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
