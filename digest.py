"""
digest.py - Generate periodic summary digests and send via email/webhook
"""
import threading
import time
from datetime import datetime
import storage


def build_digest(hours: int = 24) -> dict:
    """Build a digest summary for all devices over the last N hours."""
    devices = storage.load_devices()
    rows = []
    for d in devices:
        summary = storage.load_results_summary(d["id"], hours)
        uptime = storage.load_uptime_stats(d["id"], hours)
        incidents = storage.load_incidents(d["id"], hours)
        rows.append({
            "id": d["id"],
            "name": d["name"],
            "host": d["host"],
            "probe_type": d.get("probe_type", "icmp"),
            "avg_latency": summary.get("avg_latency"),
            "p95": summary.get("p95"),
            "loss_pct": summary.get("loss_pct"),
            "uptime_pct": uptime.get("uptime_pct"),
            "incidents": len(incidents),
        })
    alerts = storage.load_alerts(50)
    return {
        "generated_at": datetime.utcnow().isoformat(),
        "period_hours": hours,
        "devices": rows,
        "recent_alerts": alerts[:10],
    }


def format_digest_text(digest: dict) -> str:
    """Format digest as plain text for email."""
    lines = [
        f"PingPlotter Digest — {digest['period_hours']}h summary",
        f"Generated: {digest['generated_at'][:19].replace('T', ' ')} UTC",
        "=" * 60,
    ]
    for d in digest["devices"]:
        uptime = f"{d['uptime_pct']:.1f}%" if d["uptime_pct"] is not None else "—"
        lat = f"{d['avg_latency']:.1f}ms" if d["avg_latency"] else "—"
        p95 = f"{d['p95']:.1f}ms" if d["p95"] else "—"
        loss = f"{d['loss_pct']:.1f}%" if d["loss_pct"] is not None else "—"
        lines.append(f"\n{d['name']} ({d['host']})")
        lines.append(f"  Uptime: {uptime}  Avg: {lat}  P95: {p95}  Loss: {loss}  Incidents: {d['incidents']}")
    if digest["recent_alerts"]:
        lines.append("\n" + "=" * 60)
        lines.append("Recent Alerts:")
        for a in digest["recent_alerts"]:
            lines.append(f"  [{a['timestamp'][:19]}] {a['device_name']} — {a['alert_type']}")
    return "\n".join(lines)


_digest_thread = None
_digest_stop = threading.Event()


def _digest_loop(interval_hours: int, stop: threading.Event):
    while True:
        try:
            from alerts import _send_email, _send_slack
            digest = build_digest(interval_hours)
            text = format_digest_text(digest)
            subject = f"PingPlotter {interval_hours}h Digest — {datetime.utcnow().strftime('%Y-%m-%d')}"
            _send_email(subject, text)
            _send_slack(f"```{text[:2900]}```")  # Slack 3k char limit
        except Exception as e:
            print(f"[Digest] Failed: {e}")
        if stop.wait(interval_hours * 3600):
            break


def start(interval_hours: int = 24):
    global _digest_thread, _digest_stop
    _digest_stop = threading.Event()
    _digest_thread = threading.Thread(target=_digest_loop, args=(interval_hours, _digest_stop), daemon=True)
    _digest_thread.start()
