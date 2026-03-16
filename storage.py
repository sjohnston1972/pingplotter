"""
storage.py - CSV-based persistence for ping results and device config
"""
import csv
import json
import math
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path("data")
DEVICES_FILE = DATA_DIR / "devices.json"
RESULTS_DIR = DATA_DIR / "results"
TRACES_DIR = DATA_DIR / "traces"

_TRACE_ROW_LIMIT = 3000  # ~100 runs at 30 hops each

_lock = threading.Lock()


def init_storage():
    DATA_DIR.mkdir(exist_ok=True)
    RESULTS_DIR.mkdir(exist_ok=True)
    TRACES_DIR.mkdir(exist_ok=True)
    if not DEVICES_FILE.exists():
        save_devices([])


# ── Device management ──────────────────────────────────────────────────────────

def load_devices() -> list[dict]:
    if not DEVICES_FILE.exists():
        return []
    with open(DEVICES_FILE) as f:
        return json.load(f)


def save_devices(devices: list[dict]):
    with _lock:
        with open(DEVICES_FILE, "w") as f:
            json.dump(devices, f, indent=2)


def add_device(device: dict) -> dict:
    devices = load_devices()
    # Assign a simple ID
    existing_ids = {d["id"] for d in devices}
    new_id = 1
    while new_id in existing_ids:
        new_id += 1
    device["id"] = new_id
    devices.append(device)
    save_devices(devices)
    return device


def remove_device(device_id: int):
    devices = [d for d in load_devices() if d["id"] != device_id]
    save_devices(devices)


def update_device(device_id: int, updates: dict):
    devices = load_devices()
    for d in devices:
        if d["id"] == device_id:
            d.update(updates)
    save_devices(devices)


GROUPS_FILE = DATA_DIR / "groups.json"

def load_groups() -> list[dict]:
    if not GROUPS_FILE.exists():
        return []
    with open(GROUPS_FILE) as f:
        return json.load(f)

def save_groups(groups: list[dict]):
    with _lock:
        with open(GROUPS_FILE, "w") as f:
            json.dump(groups, f, indent=2)

def add_group(name: str) -> dict:
    groups = load_groups()
    existing_ids = {g["id"] for g in groups}
    new_id = 1
    while new_id in existing_ids:
        new_id += 1
    g = {"id": new_id, "name": name}
    groups.append(g)
    save_groups(groups)
    return g

def remove_group(group_id: int):
    groups = [g for g in load_groups() if g["id"] != group_id]
    save_groups(groups)
    # Unassign devices in this group
    devices = load_devices()
    for d in devices:
        if d.get("group_id") == group_id:
            d.pop("group_id", None)
    save_devices(devices)


MAINTENANCE_FILE = DATA_DIR / "maintenance.json"

def load_maintenance() -> list[dict]:
    if not MAINTENANCE_FILE.exists():
        return []
    with open(MAINTENANCE_FILE) as f:
        return json.load(f)

def save_maintenance(windows: list[dict]):
    with _lock:
        with open(MAINTENANCE_FILE, "w") as f:
            json.dump(windows, f, indent=2)

def add_maintenance(window: dict) -> dict:
    windows = load_maintenance()
    existing_ids = {w["id"] for w in windows}
    new_id = 1
    while new_id in existing_ids:
        new_id += 1
    window["id"] = new_id
    windows.append(window)
    save_maintenance(windows)
    return window

def remove_maintenance(window_id: int):
    windows = [w for w in load_maintenance() if w["id"] != window_id]
    save_maintenance(windows)

def is_in_maintenance(device_id: int) -> bool:
    """Return True if device_id (or all devices) is currently in a maintenance window."""
    now = datetime.utcnow()
    for w in load_maintenance():
        try:
            # Strip trailing Z so fromisoformat works on Python < 3.11
            start = datetime.fromisoformat(w["start"].rstrip("Z"))
            end   = datetime.fromisoformat(w["end"].rstrip("Z"))
        except ValueError:
            continue
        if start <= now <= end:
            if w.get("device_id") is None or w["device_id"] == device_id:
                return True
    return False


# ── Result storage ─────────────────────────────────────────────────────────────

def _result_file(device_id: int) -> Path:
    return RESULTS_DIR / f"device_{device_id}.csv"


def _ensure_csv(path: Path):
    if not path.exists():
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "latency_ms", "success", "jitter_ms"])


def save_result(device_id: int, latency_ms: float | None, success: bool, jitter_ms: float | None):
    path = _result_file(device_id)
    with _lock:
        _ensure_csv(path)
        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.utcnow().isoformat(),
                round(latency_ms, 3) if latency_ms is not None else "",
                int(success),
                round(jitter_ms, 3) if jitter_ms is not None else ""
            ])


def load_results(device_id: int, hours: int = 1) -> list[dict]:
    path = _result_file(device_id)
    if not path.exists():
        return []
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["timestamp"])
                if ts >= cutoff:
                    rows.append({
                        "timestamp": row["timestamp"],
                        "latency_ms": float(row["latency_ms"]) if row["latency_ms"] else None,
                        "success": bool(int(row["success"])),
                        "jitter_ms": float(row["jitter_ms"]) if row["jitter_ms"] else None,
                    })
            except (ValueError, KeyError):
                continue
    return rows


def _percentile(sorted_data: list, pct: float) -> float | None:
    if not sorted_data:
        return None
    k = (len(sorted_data) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_data) - 1)
    return round(sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (k - lo), 2)


def load_results_summary(device_id: int, hours: int = 1) -> dict:
    results = load_results(device_id, hours)
    if not results:
        return {"count": 0, "loss_pct": 0, "avg_latency": None, "max_latency": None,
                "min_latency": None, "avg_jitter": None, "p50": None, "p95": None, "p99": None}
    total = len(results)
    successes = [r for r in results if r["success"]]
    latencies = sorted([r["latency_ms"] for r in successes if r["latency_ms"] is not None])
    jitters = [r["jitter_ms"] for r in successes if r["jitter_ms"] is not None]
    loss_pct = round((1 - len(successes) / total) * 100, 1) if total else 0
    return {
        "count": total,
        "loss_pct": loss_pct,
        "avg_latency": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "max_latency": round(max(latencies), 2) if latencies else None,
        "min_latency": round(min(latencies), 2) if latencies else None,
        "avg_jitter": round(sum(jitters) / len(jitters), 2) if jitters else None,
        "p50": _percentile(latencies, 50),
        "p95": _percentile(latencies, 95),
        "p99": _percentile(latencies, 99),
    }


def load_latency_histogram(device_id: int, hours: int = 1, buckets: int = 20) -> list[dict]:
    results = load_results(device_id, hours)
    latencies = [r["latency_ms"] for r in results if r["latency_ms"] is not None]
    if not latencies:
        return []
    lo, hi = min(latencies), max(latencies)
    if lo == hi:
        hi = lo + 1.0
    width = (hi - lo) / buckets
    counts = [0] * buckets
    for v in latencies:
        idx = min(int((v - lo) / width), buckets - 1)
        counts[idx] += 1
    return [
        {
            "start": round(lo + i * width, 2),
            "end":   round(lo + (i + 1) * width, 2),
            "count": counts[i],
            "label": f"{round(lo + i * width, 1)}",
        }
        for i in range(buckets)
    ]


def load_sla_report(device_id: int, days: int = 30) -> list[dict]:
    """Per-day uptime breakdown for the last N days."""
    from collections import defaultdict
    path = _result_file(device_id)
    if not path.exists():
        return []
    cutoff = datetime.utcnow() - timedelta(days=days)
    day_buckets: dict = defaultdict(lambda: {"up": 0, "total": 0})
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["timestamp"])
                if ts < cutoff:
                    continue
                key = ts.strftime("%Y-%m-%d")
                day_buckets[key]["total"] += 1
                if int(row["success"]):
                    day_buckets[key]["up"] += 1
            except (ValueError, KeyError):
                continue
    result = []
    for i in range(days):
        key = (datetime.utcnow() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        b = day_buckets.get(key, {"up": 0, "total": 0})
        result.append({
            "date": key,
            "uptime_pct": round(b["up"] / b["total"] * 100, 2) if b["total"] else None,
            "up": b["up"],
            "total": b["total"],
        })
    return result


# ── Traceroute hop storage ─────────────────────────────────────────────────────

def _trace_file(device_id: int) -> Path:
    return TRACES_DIR / f"device_{device_id}.csv"


def _trim_trace_file(path: Path):
    """Keep only the last _TRACE_ROW_LIMIT data rows. Called inside _lock."""
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        rows = list(reader)
    if len(rows) <= _TRACE_ROW_LIMIT:
        return
    rows = rows[-_TRACE_ROW_LIMIT:]
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def save_trace_run(device_id: int, timestamp: str, hops: list[dict]):
    path = _trace_file(device_id)
    with _lock:
        exists = path.exists()
        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            if not exists:
                writer.writerow(["timestamp", "hop", "ip", "lat1", "lat2", "lat3"])
            for h in hops:
                lats = h.get("latencies", [None, None, None])
                writer.writerow([
                    timestamp,
                    h["hop"],
                    h.get("ip") or "",
                    round(lats[0], 3) if lats[0] is not None else "",
                    round(lats[1], 3) if len(lats) > 1 and lats[1] is not None else "",
                    round(lats[2], 3) if len(lats) > 2 and lats[2] is not None else "",
                ])
        _trim_trace_file(path)


def load_trace_stats(device_id: int, runs: int = 50) -> dict:
    from collections import defaultdict
    path = _trace_file(device_id)
    empty = {"latest": {"timestamp": None, "hops": []}, "stats": []}
    if not path.exists():
        return empty

    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    if not rows:
        return empty

    # Collect run timestamps in insertion order, keep last N
    seen = {}
    for r in rows:
        seen[r["timestamp"]] = True
    all_ts = list(seen.keys())
    recent_ts = set(all_ts[-runs:])
    recent_rows = [r for r in rows if r["timestamp"] in recent_ts]

    # Latest run
    latest_ts = all_ts[-1]
    latest_hops = sorted(
        [r for r in rows if r["timestamp"] == latest_ts],
        key=lambda r: int(r["hop"])
    )
    latest = {
        "timestamp": latest_ts,
        "hops": [
            {
                "hop": int(r["hop"]),
                "ip": r["ip"] or None,
                "latencies": [
                    float(r["lat1"]) if r["lat1"] else None,
                    float(r["lat2"]) if r["lat2"] else None,
                    float(r["lat3"]) if r["lat3"] else None,
                ],
            }
            for r in latest_hops
        ],
    }

    # Aggregate stats per hop
    hop_data = defaultdict(lambda: {"ips": [], "lats": [], "sent": 0, "lost": 0})
    for r in recent_rows:
        h = int(r["hop"])
        hop_data[h]["ips"].append(r["ip"] or "")
        for key in ("lat1", "lat2", "lat3"):
            hop_data[h]["sent"] += 1
            if r[key]:
                hop_data[h]["lats"].append(float(r[key]))
            else:
                hop_data[h]["lost"] += 1

    stats = []
    for hop_num in sorted(hop_data.keys()):
        d = hop_data[hop_num]
        lats = d["lats"]
        non_empty_ips = [ip for ip in d["ips"] if ip]
        ip = max(set(non_empty_ips), key=non_empty_ips.count) if non_empty_ips else None
        loss_pct = round(d["lost"] / d["sent"] * 100, 1) if d["sent"] else 100.0
        avg = sum(lats) / len(lats) if lats else None
        jitter = round(math.sqrt(sum((x - avg) ** 2 for x in lats) / len(lats)), 2) if lats and len(lats) >= 2 else None
        stats.append({
            "hop": hop_num,
            "ip": ip,
            "sent": d["sent"],
            "lost": d["lost"],
            "loss_pct": loss_pct,
            "last_ms": round(lats[-1], 2) if lats else None,
            "avg_ms": round(avg, 2) if avg is not None else None,
            "min_ms": round(min(lats), 2) if lats else None,
            "max_ms": round(max(lats), 2) if lats else None,
            "jitter_ms": jitter,
        })

    return {"latest": latest, "stats": stats}


SPEEDTEST_FILE = DATA_DIR / "speedtest.csv"

def save_speedtest(result: dict):
    fields = ["timestamp", "download_mbps", "upload_mbps", "ping_ms", "server", "isp"]
    with _lock:
        write_header = not SPEEDTEST_FILE.exists()
        with open(SPEEDTEST_FILE, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if write_header:
                w.writeheader()
            w.writerow(result)

def load_speedtest(hours: int = 168) -> list[dict]:
    if not SPEEDTEST_FILE.exists():
        return []
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    rows = []
    with open(SPEEDTEST_FILE, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["timestamp"])
                if ts >= cutoff:
                    rows.append({
                        "timestamp": row["timestamp"],
                        "download_mbps": float(row["download_mbps"]),
                        "upload_mbps": float(row["upload_mbps"]),
                        "ping_ms": float(row["ping_ms"]),
                        "server": row.get("server", ""),
                        "isp": row.get("isp", ""),
                    })
            except (ValueError, KeyError):
                continue
    return rows


# ── Alert log ──────────────────────────────────────────────────────────────────

ALERT_LOG = DATA_DIR / "alerts.csv"
SETTINGS_FILE = DATA_DIR / "settings.json"

_SETTINGS_DEFAULTS = {
    "slack_webhook_url": "",
    "cooldown_sec": 300,
    "default_interval_sec": 5,
    "retention_days": 30,
    "webhook_url": "",
    "discord_webhook_url": "",
    "teams_webhook_url": "",
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_from": "",
    "smtp_to": "",
    "email_alerts_enabled": False,
    "speedtest_interval_minutes": 60,
    "digest_interval_hours": 0,
}


def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return dict(_SETTINGS_DEFAULTS)
    with open(SETTINGS_FILE) as f:
        data = json.load(f)
    return {**_SETTINGS_DEFAULTS, **data}


def save_settings(updates: dict):
    current = load_settings()
    current.update(updates)
    with _lock:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(current, f, indent=2)
    return current


def load_uptime_stats(device_id: int, hours: int = 24) -> dict:
    results = load_results(device_id, hours)
    if not results:
        return {"uptime_pct": 100.0, "up": 0, "down": 0, "total": 0}
    total = len(results)
    up = sum(1 for r in results if r["success"])
    down = total - up
    return {
        "uptime_pct": round(up / total * 100, 3),
        "up": up,
        "down": down,
        "total": total,
    }


def load_incidents(device_id: int, hours: int = 168) -> list[dict]:
    results = sorted(load_results(device_id, hours), key=lambda r: r["timestamp"])
    incidents = []
    current = None
    for r in results:
        if not r["success"]:
            if current is None:
                current = {"start": r["timestamp"], "end": r["timestamp"], "count": 1}
            else:
                current["end"] = r["timestamp"]
                current["count"] += 1
        else:
            if current is not None:
                try:
                    dur = (datetime.fromisoformat(current["end"]) - datetime.fromisoformat(current["start"])).total_seconds()
                except Exception:
                    dur = 0
                current["duration_sec"] = round(dur, 1)
                incidents.append(current)
                current = None
    if current is not None:
        try:
            dur = (datetime.fromisoformat(current["end"]) - datetime.fromisoformat(current["start"])).total_seconds()
        except Exception:
            dur = 0
        current["duration_sec"] = round(dur, 1)
        incidents.append(current)
    return incidents[-50:]


def load_heatmap(device_id: int) -> list[dict]:
    from collections import defaultdict
    results = load_results(device_id, hours=168)
    buckets = defaultdict(list)
    for r in results:
        try:
            ts = datetime.fromisoformat(r["timestamp"])
            key = (ts.weekday(), ts.hour)
            if r["latency_ms"] is not None:
                buckets[key].append(r["latency_ms"])
        except Exception:
            continue
    cells = []
    for day in range(7):
        for hour in range(24):
            lats = buckets.get((day, hour), [])
            cells.append({
                "day": day,
                "hour": hour,
                "avg_ms": round(sum(lats) / len(lats), 1) if lats else None,
                "count": len(lats),
            })
    return cells


def purge_old_data(retention_days: int) -> int:
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    rows_purged = 0
    for csv_path in list(RESULTS_DIR.glob("device_*.csv")) + list(TRACES_DIR.glob("device_*.csv")):
        kept = []
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                try:
                    ts = datetime.fromisoformat(row["timestamp"])
                    if ts >= cutoff:
                        kept.append(row)
                    else:
                        rows_purged += 1
                except (ValueError, KeyError):
                    kept.append(row)
        with _lock:
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(kept)
    return rows_purged

def log_alert(device_id: int, device_name: str, alert_type: str, value: float, threshold: float):
    with _lock:
        exists = ALERT_LOG.exists()
        with open(ALERT_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            if not exists:
                writer.writerow(["timestamp", "device_id", "device_name", "alert_type", "value", "threshold"])
            writer.writerow([
                datetime.utcnow().isoformat(),
                device_id, device_name, alert_type,
                round(value, 2), round(threshold, 2)
            ])


def load_alerts(limit: int = 100) -> list[dict]:
    if not ALERT_LOG.exists():
        return []
    rows = []
    with open(ALERT_LOG, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    return rows[-limit:]


EVENTS_FILE = DATA_DIR / "events.json"
_EVENTS_MAX = 200

def log_network_event(alert_type: str, device_ids: list[int], device_names: list[str]):
    events = []
    if EVENTS_FILE.exists():
        with open(EVENTS_FILE) as f:
            try:
                events = json.load(f)
            except Exception:
                events = []
    events.append({
        "ts": datetime.utcnow().isoformat(),
        "type": alert_type,
        "device_ids": device_ids,
        "device_names": device_names,
        "count": len(device_ids),
    })
    events = events[-_EVENTS_MAX:]
    with _lock:
        with open(EVENTS_FILE, "w") as f:
            json.dump(events, f, indent=2)

def load_network_events(limit: int = 50) -> list[dict]:
    if not EVENTS_FILE.exists():
        return []
    with open(EVENTS_FILE) as f:
        try:
            events = json.load(f)
        except Exception:
            return []
    return list(reversed(events))[:limit]
