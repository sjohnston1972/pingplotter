"""Gotify integration checks; all delivery stays on disposable local servers."""
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import alerts
import api
import storage


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "SETTINGS_FILE", tmp_path / "settings.json")
    return storage.save_settings


@pytest.fixture
def gotify():
    state = SimpleNamespace(requests=[], status=200)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state.requests.append({
                "path": self.path,
                "headers": self.headers,
                "json": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
            })
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Location", state.url + "/redirected")
            self.end_headers()
            # Even a server reflecting the token must not leak it into errors.
            self.wfile.write(json.dumps({"error": "private-app-token"}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def api_request(settings, monkeypatch):
    """Exercise actual routes and authentication without an extra HTTP client dependency."""
    monkeypatch.setattr(api, "API_TOKEN", "local-api-token")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(api.app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"

        def request(path="/settings", payload=None, authorized=True, method=None):
            headers = {"Content-Type": "application/json"}
            if authorized:
                headers["Authorization"] = "Bearer local-api-token"
            body = None if payload is None else json.dumps(payload).encode()
            req = urllib.request.Request(base + "/api" + path, data=body, headers=headers, method=method)
            try:
                response = urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response)

        try:
            yield request
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize("path,priority", [("", 5), ("/", 0), ("/proxy/gotify/", 10)])
def test_wire_format_and_proxy_path(settings, gotify, path, priority):
    settings({"gotify_url": gotify.url + path, "gotify_token": "private-app-token", "gotify_priority": priority})
    assert alerts._send_gotify("Router — Ausfall", "Gerät reagiert nicht") is True
    [request] = gotify.requests
    assert request["path"] == path.rstrip("/") + "/message"
    assert request["headers"]["X-Gotify-Key"] == "private-app-token"
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["json"] == {"title": "Router — Ausfall", "message": "Gerät reagiert nicht", "priority": priority}


def test_https_normalization_and_optional_defaults(settings):
    assert alerts._validate_gotify_settings({"gotify_url": " https://push.example.com/gotify/ "}) == (
        "https://push.example.com/gotify", "", 5)
    assert storage.load_settings()["gotify_priority"] == 5
    assert alerts._send_gotify("title", "message") is False


@pytest.mark.parametrize("patch", [
    {"gotify_url": "ftp://example.com"}, {"gotify_url": "https://"},
    {"gotify_url": "http://user:private-app-token@example.com"},
    {"gotify_url": "http://example.com:99999"}, {"gotify_url": "http://example.com/?token=secret"},
    {"gotify_url": "http://example.com/#fragment"}, {"gotify_url": "http://exam ple.com"},
    {"gotify_url": None}, {"gotify_token": None}, {"gotify_token": "key\nheader"},
    {"gotify_priority": -1}, {"gotify_priority": 11}, {"gotify_priority": 5.5},
    {"gotify_priority": True}, {"gotify_priority": "5"}, {"gotify_priority": None},
])
def test_invalid_settings_are_rejected_atomically(api_request, patch):
    status, response = api_request(payload=patch)
    assert status == 400
    assert "private-app-token" not in json.dumps(response)
    assert storage.load_settings()["gotify_url"] == ""
    assert storage.load_settings()["gotify_priority"] == 5


def test_secret_lifecycle_and_test_api(api_request, gotify):
    assert api_request("/settings/test-gotify", payload={}, authorized=False)[0] == 401
    assert api_request("/settings/test-gotify", payload={})[0] == 400
    status, body = api_request(payload={"gotify_url": gotify.url + "/proxy/", "gotify_token": "private-app-token"})
    assert status == 200
    assert body["gotify_token"] == "********" and body["gotify_token_set"] is True
    assert body["gotify_url"] == gotify.url + "/proxy"
    assert "private-app-token" not in json.dumps(api_request())
    api_request(payload={"gotify_token": "********", "gotify_token_set": False, "gotify_priority": 0})
    assert storage.load_settings()["gotify_token"] == "private-app-token"
    assert "gotify_token_set" not in storage.load_settings()
    assert api_request("/settings/test-gotify", payload={}) == (200, {"ok": True})
    assert gotify.requests[-1]["json"]["priority"] == 0
    api_request(payload={"gotify_token": "replacement"})
    assert storage.load_settings()["gotify_token"] == "replacement"
    status, body = api_request(payload={"gotify_token": ""})
    assert status == 200 and body["gotify_token"] == "" and body["gotify_token_set"] is False
    assert api_request("/settings/test-gotify", payload={})[0] == 400
    assert len(gotify.requests) == 1


@pytest.mark.parametrize("status", [401, 500, 307])
def test_delivery_errors_are_sanitized_and_redirects_not_followed(api_request, settings, gotify, status):
    settings({"gotify_url": gotify.url, "gotify_token": "private-app-token"})
    gotify.status = status
    code, body = api_request("/settings/test-gotify", payload={})
    assert code == 502
    assert body == {"detail": f"Gotify returned HTTP {status}"}
    assert len(gotify.requests) == 1


def test_invalid_saved_config_test_returns_400(api_request, settings):
    settings({"gotify_url": "invalid", "gotify_token": "private-app-token"})
    assert api_request("/settings/test-gotify", payload={})[0] == 400


def test_timeout_and_network_errors_are_sanitized(settings, monkeypatch):
    settings({"gotify_url": "https://example.com", "gotify_token": "private-app-token"})

    class Offline:
        def open(self, request, timeout):
            assert timeout == 5
            raise TimeoutError("private-app-token")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Offline())
    with pytest.raises(RuntimeError) as error:
        alerts._send_gotify("title", "message")
    assert "private-app-token" not in str(error.value)


@pytest.fixture
def channels(monkeypatch):
    calls = []
    for name in ("_send_slack", "_send_discord", "_send_teams", "_send_email", "log_alert", "_send_webhook", "_check_correlation"):
        monkeypatch.setattr(alerts, name, lambda *args, channel=name: calls.append(channel))
    monkeypatch.setattr(alerts, "_last_alert", {})
    monkeypatch.setattr(storage, "is_in_maintenance", lambda device_id: False)
    return calls


DEVICE = {"id": 1, "name": "Router", "host": "127.0.0.1", "thresholds": {"latency_ms": 100, "latency_ms_warn": 50, "jitter_ms": 20, "jitter_ms_warn": 10}}


def test_all_device_alerts_plain_text_cooldown_and_maintenance(settings, gotify, channels, monkeypatch):
    settings({"gotify_url": gotify.url, "gotify_token": "private-app-token"})
    for _ in range(2):
        alerts.check_and_alert(DEVICE, 150, False, 30, route_changed=True)
        alerts.check_and_alert(DEVICE, 75, True, 15)
        alerts.fire_anomaly_alert(DEVICE, 250, {"mean": 10, "stddev": 2})
    titles = [r["json"]["title"] for r in gotify.requests]
    assert titles == [f"PingPlotter: {kind} — Router" for kind in (
        "Down", "High latency", "High jitter", "Route changed", "High latency warn", "High jitter warn", "Anomaly")]
    assert all("*" not in r["json"]["message"] and "`" not in r["json"]["message"] and ":red_circle:" not in r["json"]["message"] for r in gotify.requests)
    assert channels.count("log_alert") == 7
    monkeypatch.setattr(alerts, "_last_alert", {})
    monkeypatch.setattr(storage, "is_in_maintenance", lambda device_id: True)
    alerts.check_and_alert(DEVICE, 150, False, 30, route_changed=True)
    alerts.fire_anomaly_alert(DEVICE, 250, {"mean": 10, "stddev": 2})
    assert len(gotify.requests) == 7 and alerts._last_alert == {}


def test_gotify_failure_does_not_interrupt_channels_or_logging(channels, monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError("private-app-token")

    monkeypatch.setattr(alerts, "_send_gotify", fail)
    alerts.check_and_alert(DEVICE, None, False, None)
    assert channels == ["_send_slack", "_send_discord", "_send_teams", "_send_email", "log_alert", "_send_webhook", "_check_correlation"]
    output = capsys.readouterr().out
    assert "Gotify send failed" in output and "private-app-token" not in output


def test_digest_does_not_send_gotify(channels, monkeypatch):
    def unexpected(*args):
        pytest.fail("Digests must not be forwarded to Gotify")

    monkeypatch.setattr(alerts, "_send_gotify", unexpected)
    monkeypatch.setattr(api.digest_mod, "build_digest", lambda hours: {})
    monkeypatch.setattr(api.digest_mod, "format_digest_text", lambda digest: "Daily summary")
    assert api.send_digest()["ok"] is True
    assert channels == ["_send_email", "_send_slack"]
