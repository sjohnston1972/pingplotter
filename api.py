"""
api.py - FastAPI REST endpoints
"""
import asyncio
import json
import os
import urllib.request

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional
import collector
import storage

app = FastAPI(title="PingPlotter API", version="1.0")
_geo_cache: dict = {}  # ip -> {country, org, city, region}


# ── Pydantic models ────────────────────────────────────────────────────────────

class DeviceCreate(BaseModel):
    name: str
    host: str
    probe_type: str = "icmp"
    interval_sec: int = 5
    packet_size: Optional[int] = None
    df_bit: bool = False
    enabled: bool = True
    thresholds: Optional[dict] = {}
    notes: Optional[str] = ""
    tag_color: Optional[str] = None
    group_id: Optional[int] = None


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    probe_type: Optional[str] = None
    interval_sec: Optional[int] = None
    packet_size: Optional[int] = None
    df_bit: Optional[bool] = None
    enabled: Optional[bool] = None
    thresholds: Optional[dict] = None
    notes: Optional[str] = None
    tag_color: Optional[str] = None
    group_id: Optional[int] = None


# ── Device endpoints ───────────────────────────────────────────────────────────

@app.get("/api/devices")
def list_devices():
    devices = storage.load_devices()
    statuses = {s["device_id"]: s for s in collector.get_live_status()}
    for d in devices:
        s = statuses.get(d["id"], {})
        d["live"] = s
    return devices


@app.post("/api/devices", status_code=201)
def create_device(payload: DeviceCreate):
    device = storage.add_device(payload.model_dump())
    if device.get("enabled", True):
        collector.start_device(device["id"])
    return device


@app.delete("/api/devices/{device_id}")
def delete_device(device_id: int):
    collector.stop_device(device_id)
    storage.remove_device(device_id)
    return {"ok": True}


@app.patch("/api/devices/{device_id}")
def update_device(device_id: int, payload: DeviceUpdate):
    updates = payload.model_dump(exclude_unset=True)
    storage.update_device(device_id, updates)
    collector.restart_device(device_id)
    return {"ok": True}


@app.post("/api/devices/{device_id}/enable")
def enable_device(device_id: int):
    storage.update_device(device_id, {"enabled": True})
    collector.start_device(device_id)
    return {"ok": True}


@app.post("/api/devices/{device_id}/disable")
def disable_device(device_id: int):
    storage.update_device(device_id, {"enabled": False})
    collector.stop_device(device_id)
    return {"ok": True}


# ── Groups endpoints ─────────────────────────────────────────────────────────

class GroupCreate(BaseModel):
    name: str

@app.get("/api/groups")
def list_groups():
    return storage.load_groups()

@app.post("/api/groups", status_code=201)
def create_group(payload: GroupCreate):
    return storage.add_group(payload.name)

@app.delete("/api/groups/{group_id}")
def delete_group(group_id: int):
    storage.remove_group(group_id)
    return {"ok": True}

@app.patch("/api/groups/{group_id}")
def update_group(group_id: int, payload: GroupCreate):
    groups = storage.load_groups()
    for g in groups:
        if g["id"] == group_id:
            g["name"] = payload.name
    storage.save_groups(groups)
    return {"ok": True}


# ── Maintenance window endpoints ──────────────────────────────────────────────

class MaintenanceCreate(BaseModel):
    label: str
    start: str          # ISO datetime UTC
    end: str            # ISO datetime UTC
    device_id: Optional[int] = None   # None = all devices

@app.get("/api/maintenance")
def list_maintenance():
    return storage.load_maintenance()

@app.post("/api/maintenance", status_code=201)
def create_maintenance(payload: MaintenanceCreate):
    return storage.add_maintenance(payload.model_dump())

@app.delete("/api/maintenance/{window_id}")
def delete_maintenance(window_id: int):
    storage.remove_maintenance(window_id)
    return {"ok": True}


# ── Results & stats endpoints ──────────────────────────────────────────────────

@app.get("/api/devices/{device_id}/results")
def get_results(device_id: int, hours: int = 1):
    return storage.load_results(device_id, hours)


@app.get("/api/devices/{device_id}/summary")
def get_summary(device_id: int, hours: int = 1):
    return storage.load_results_summary(device_id, hours)


@app.get("/api/devices/{device_id}/hops")
def get_hops(device_id: int, runs: int = 50):
    return storage.load_trace_stats(device_id, runs)


@app.get("/api/status")
def get_all_status():
    return collector.get_live_status()


# ── Alert log endpoint ─────────────────────────────────────────────────────────

@app.get("/api/alerts")
def get_alerts(limit: int = 100):
    return storage.load_alerts(limit)


# ── Health check ───────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "devices": len(storage.load_devices())}


# ── Settings endpoints ─────────────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    s = storage.load_settings()
    # Env var overrides stored webhook
    if os.environ.get("SLACK_WEBHOOK_URL"):
        s["slack_webhook_url"] = os.environ["SLACK_WEBHOOK_URL"]
    return s


@app.post("/api/settings")
def update_settings(payload: dict):
    return storage.save_settings(payload)


@app.post("/api/settings/test-slack")
def test_slack():
    s = storage.load_settings()
    url = os.environ.get("SLACK_WEBHOOK_URL") or s.get("slack_webhook_url", "")
    if not url:
        raise HTTPException(status_code=400, detail="No Slack webhook URL configured")
    body = b'{"text": "PingPlotter test message - Slack alerts are working!"}'
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"ok": resp.status == 200}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/settings/test-discord")
def test_discord():
    s = storage.load_settings()
    url = s.get("discord_webhook_url", "")
    if not url:
        raise HTTPException(status_code=400, detail="No Discord webhook URL configured")
    import json
    payload = json.dumps({"content": "✅ PingPlotter test — Discord alerts are working!"}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/settings/test-teams")
def test_teams():
    s = storage.load_settings()
    url = s.get("teams_webhook_url", "")
    if not url:
        raise HTTPException(status_code=400, detail="No Teams webhook URL configured")
    import json
    payload = json.dumps({"@type": "MessageCard", "@context": "http://schema.org/extensions", "text": "✅ PingPlotter test — Teams alerts are working!"}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/settings/test-email")
def test_email():
    from alerts import _send_email
    try:
        _send_email("PingPlotter Test", "PingPlotter email alerts are working!")
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Report endpoint ────────────────────────────────────────────────────────────

@app.get("/api/report")
def get_report(hours: int = 24):
    devices = storage.load_devices()
    statuses = {s["device_id"]: s for s in collector.get_live_status()}
    report = []
    for d in devices:
        summary = storage.load_results_summary(d["id"], hours)
        live = statuses.get(d["id"], {})
        report.append({
            "id": d["id"],
            "name": d["name"],
            "host": d["host"],
            "status": live.get("status", "unknown"),
            **summary,
        })
    return report


# ── Per-device uptime / incidents / heatmap ────────────────────────────────────

@app.get("/api/devices/{device_id}/uptime")
def get_uptime(device_id: int, hours: int = 24):
    return storage.load_uptime_stats(device_id, hours)


@app.get("/api/devices/{device_id}/incidents")
def get_incidents(device_id: int, hours: int = 168):
    return storage.load_incidents(device_id, hours)


@app.get("/api/devices/{device_id}/heatmap")
def get_heatmap(device_id: int):
    return storage.load_heatmap(device_id)


@app.get("/api/devices/{device_id}/baseline")
def get_baseline_stats(device_id: int):
    import baseline as bl
    return bl.get_baseline(device_id) or {"mean": None, "stddev": None, "samples": 0}


# ── Compare endpoint ───────────────────────────────────────────────────────────


# ── DNS reverse lookup endpoint ────────────────────────────────────────────────

@app.get("/api/resolve")
def resolve_ip(ip: str):
    import socket
    try:
        hostname = socket.gethostbyaddr(ip)[0]
    except Exception:
        hostname = None
    return {"ip": ip, "hostname": hostname}


# ── Sparklines endpoint ────────────────────────────────────────────────────────

@app.get("/api/sparklines")
def get_sparklines(minutes: int = 10):
    """Return last N minutes of latency for all devices (for sidebar sparklines)."""
    from datetime import datetime, timedelta
    devices = storage.load_devices()
    result = {}
    for d in devices:
        rows = storage.load_results(d["id"], hours=1)
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        recent = [r["latency_ms"] for r in rows
                  if r["latency_ms"] is not None
                  and datetime.fromisoformat(r["timestamp"]) >= cutoff]
        result[d["id"]] = recent
    return result


# ── MTU discovery endpoint ─────────────────────────────────────────────────────

@app.post("/api/devices/{device_id}/mtu-discover")
def mtu_discover(device_id: int):
    devices = storage.load_devices()
    device = next((d for d in devices if d["id"] == device_id), None)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    result = collector.discover_mtu(device["host"])
    return result


# ── Network events endpoint ────────────────────────────────────────────────────

@app.get("/api/events")
def get_network_events(limit: int = 50):
    return storage.load_network_events(limit)


# ── SSE live-push endpoint ─────────────────────────────────────────────────────

@app.get("/api/sse")
async def sse_events():
    """Server-Sent Events stream: pushes device status every 3 seconds."""
    async def event_stream():
        while True:
            statuses = collector.get_live_status()
            data = json.dumps(statuses)
            yield f"data: {data}\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Histogram / SLA endpoints ──────────────────────────────────────────────────

@app.get("/api/devices/{device_id}/histogram")
def get_histogram(device_id: int, hours: int = 1, buckets: int = 20):
    return storage.load_latency_histogram(device_id, hours, buckets)


@app.get("/api/devices/{device_id}/sla")
def get_sla(device_id: int, days: int = 30):
    return storage.load_sla_report(device_id, days)


# ── CSV export ─────────────────────────────────────────────────────────────────

@app.get("/api/devices/{device_id}/export.csv")
def export_csv(device_id: int, hours: int = 8760):
    import io, csv as _csv
    devices_list = storage.load_devices()
    device = next((d for d in devices_list if d["id"] == device_id), None)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    results = storage.load_results(device_id, hours)
    fields = ["timestamp", "latency_ms", "success", "jitter_ms"]

    def generate():
        buf = io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in results:
            w.writerow(row)
        yield buf.getvalue()

    fname = f"device_{device_id}_{device['name'].replace(' ', '_')}.csv"
    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── Geo lookup ─────────────────────────────────────────────────────────────────

@app.get("/api/geo")
def geo_lookup(ip: str):
    if ip in _geo_cache:
        return _geo_cache[ip]
    if len(_geo_cache) > 2000:
        _geo_cache.clear()
    try:
        with urllib.request.urlopen(f"https://ipwho.is/{ip}", timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        conn = data.get("connection") or {}
        asn = conn.get("asn")
        org_name = conn.get("org") or conn.get("isp")
        org = f"AS{asn} {org_name}" if asn and org_name else org_name or None
        lat = data.get("latitude")
        lon = data.get("longitude")
        result = {
            "ip":      data.get("ip", ip),
            "country": data.get("country_code") or data.get("country"),
            "org":     org,
            "city":    data.get("city"),
            "region":  data.get("region"),
            "loc":     f"{lat},{lon}" if lat is not None and lon is not None else None,
        }
    except Exception:
        result = {"ip": ip, "country": None, "org": None, "city": None, "region": None, "loc": None}
    _geo_cache[ip] = result
    return result


# ── Ping-now endpoint ──────────────────────────────────────────────────────────

@app.post("/api/devices/{device_id}/ping-now")
def ping_now(device_id: int):
    devices_list = storage.load_devices()
    if not any(d["id"] == device_id for d in devices_list):
        raise HTTPException(status_code=404, detail="Device not found")
    collector.restart_device(device_id)
    return {"ok": True}


# ── Retention / purge endpoint ─────────────────────────────────────────────────

@app.post("/api/retention/purge")
def retention_purge():
    s = storage.load_settings()
    rows_purged = storage.purge_old_data(s.get("retention_days", 30))
    return {"ok": True, "rows_purged": rows_purged}


import threading as _threading
import speedtest_runner
import digest as digest_mod

_speedtest_lock = _threading.Lock()

# ── Digest endpoints ──────────────────────────────────────────────────────────

@app.get("/api/digest")
def get_digest(hours: int = 24):
    return digest_mod.build_digest(hours)

@app.post("/api/digest/send")
def send_digest(hours: int = 24):
    from alerts import _send_email, _send_slack
    d = digest_mod.build_digest(hours)
    text = digest_mod.format_digest_text(d)
    subject = f"PingPlotter {hours}h Digest"
    _send_email(subject, text)
    _send_slack(f"```{text[:2900]}```")
    return {"ok": True, "text": text}


# ── Speedtest endpoints ───────────────────────────────────────────────────────

@app.post("/api/speedtest/run")
async def run_speedtest_endpoint():
    """Run a speedtest in a thread pool, return results."""
    import asyncio
    result = await asyncio.to_thread(speedtest_runner.run_once)
    if result:
        storage.save_speedtest(result)
    return result or {"error": "Speedtest failed"}

@app.get("/api/speedtest/results")
def get_speedtest_results(hours: int = 168):
    return storage.load_speedtest(hours)


@app.get("/api/speedtest/stream")
async def speedtest_stream():
    """SSE: streams real-time speedtest progress (download/upload speed every ~300ms)."""
    if not _speedtest_lock.acquire(blocking=False):
        async def _busy():
            yield 'data: {"phase":"error","message":"Speedtest already running"}\n\n'
        return StreamingResponse(_busy(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _emit(phase, payload):
        loop.call_soon_threadsafe(queue.put_nowait, json.dumps({"phase": phase, **payload}))

    def _run():
        try:
            result = speedtest_runner.run_once_streaming(_emit)
            if result:
                storage.save_speedtest(result)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)
            _speedtest_lock.release()

    _threading.Thread(target=_run, daemon=True).start()

    async def event_stream():
        try:
            while True:
                item = await asyncio.wait_for(queue.get(), timeout=120)
                if item is None:
                    break
                yield f"data: {item}\n\n"
        except asyncio.TimeoutError:
            yield 'data: {"phase":"error","message":"Speedtest timed out"}\n\n'

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Static frontend ────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def serve_frontend():
    return FileResponse("static/index.html")
