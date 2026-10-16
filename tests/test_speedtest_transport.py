"""Both speedtest entrypoints must retrieve metadata over HTTPS."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import speedtest_runner


def test_scheduled_speedtest_requests_https(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps({
            "download": 100_000_000, "upload": 20_000_000, "ping": 17.0,
            "server": {"name": "Düsseldorf"}, "client": {"isp": "Example ISP"},
        }))

    monkeypatch.setattr(speedtest_runner.subprocess, "run", run)
    result = speedtest_runner.run_once()
    assert "--secure" in calls[0]
    assert result["server"] == "Düsseldorf"
    assert result["download_mbps"] == 100.0


def test_live_speedtest_requests_https_and_preserves_events(monkeypatch):
    calls = []
    events = []

    class FakeSpeedtest:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.results = SimpleNamespace(
                ping=17.0, server={"name": "Düsseldorf"}, client={"isp": "Example ISP"},
            )

        def get_best_server(self):
            return self.results.server

        def download(self):
            return 100_000_000

        def upload(self):
            return 20_000_000

    fake_module = SimpleNamespace(Speedtest=FakeSpeedtest, HTTPDownloader=type("Downloader", (), {}),
                                  HTTPUploader=type("Uploader", (), {}))
    monkeypatch.setitem(sys.modules, "speedtest", fake_module)
    result = speedtest_runner.run_once_streaming(lambda phase, data: events.append((phase, data)))
    assert calls[0].get("secure") is True
    assert result["server"] == "Düsseldorf"
    assert result["ping_ms"] == 17.0
    assert [phase for phase, _ in events] == ["init", "ping", "download", "download_done", "upload", "complete"]
    assert events[-1][1] == result
