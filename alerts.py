"""
alerts.py - Threshold monitoring and Slack/webhook notifications
"""
import os
import time
import threading
from storage import log_alert, load_settings

# Cooldown tracker: don't spam alerts. Store last alert time per (device_id, alert_type)
_last_alert: dict[tuple, float] = {}
_alert_lock = threading.Lock()
COOLDOWN_SEC = 300  # 5 minutes between repeat alerts for same issue

_recent_alerts: dict[str, list[tuple[float, int, str]]] = {}  # type -> [(time, device_id, name)]
_CORRELATION_WINDOW = 60  # seconds
_CORRELATION_MIN = 2      # minimum devices for a network event
_correlation_lock = threading.Lock()

def _check_correlation(device_id: int, device_name: str, alert_type: str):
    """Track recent alerts; if N devices fire same type within window, log a network event."""
    import storage as st
    now = time.time()
    with _correlation_lock:
        bucket = _recent_alerts.setdefault(alert_type, [])
        bucket.append((now, device_id, device_name))
        bucket[:] = [(t, did, dn) for t, did, dn in bucket if now - t <= _CORRELATION_WINDOW]
        if len(bucket) >= _CORRELATION_MIN:
            ids = [did for _, did, _ in bucket]
            names = [dn for _, _, dn in bucket]
            st.log_network_event(alert_type, ids, names)
            bucket.clear()


def _can_alert(device_id: int, alert_type: str) -> bool:
    key = (device_id, alert_type)
    now = time.time()
    with _alert_lock:
        last = _last_alert.get(key, 0)
        if now - last >= COOLDOWN_SEC:
            _last_alert[key] = now
            return True
    return False


def _send_slack(message: str):
    """Send a message to Slack. Requires SLACK_WEBHOOK_URL env var or settings."""
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        s = load_settings()
        webhook_url = s.get("slack_webhook_url", "")
    if not webhook_url:
        print(f"[ALERT] Slack not configured. Message: {message}")
        return
    try:
        import urllib.request
        import json
        payload = json.dumps({"text": message}).encode()
        req = urllib.request.Request(webhook_url, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[ALERT] Slack send failed: {e}")


def _send_discord(message: str):
    """Send alert to Discord via incoming webhook."""
    s = load_settings()
    url = s.get("discord_webhook_url", "").strip()
    if not url:
        return
    try:
        import urllib.request, json
        clean = message.replace(":red_circle:", "🔴").replace(":large_yellow_circle:", "🟡").replace(":chart_with_upwards_trend:", "📈").replace(":arrows_counterclockwise:", "🔄")
        payload = json.dumps({"content": clean}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[ALERT] Discord send failed: {e}")


def _send_teams(message: str):
    """Send alert to Microsoft Teams via incoming webhook."""
    s = load_settings()
    url = s.get("teams_webhook_url", "").strip()
    if not url:
        return
    try:
        import urllib.request, json
        clean = message.replace(":red_circle:", "🔴").replace(":large_yellow_circle:", "🟡").replace(":chart_with_upwards_trend:", "📈").replace(":arrows_counterclockwise:", "🔄")
        payload = json.dumps({
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "text": clean
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[ALERT] Teams send failed: {e}")


def _send_email(subject: str, body: str):
    """Send alert email via SMTP."""
    s = load_settings()
    if not s.get("email_alerts_enabled"):
        return
    host = s.get("smtp_host", "").strip()
    to_addr = s.get("smtp_to", "").strip()
    if not host or not to_addr:
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = s.get("smtp_from") or s.get("smtp_user", "pingplotter@localhost")
        msg["To"] = to_addr
        port = int(s.get("smtp_port", 587))
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.ehlo()
            if port == 587:
                smtp.starttls()
            user = s.get("smtp_user", "").strip()
            passwd = s.get("smtp_pass", "").strip()
            if user and passwd:
                smtp.login(user, passwd)
            smtp.sendmail(msg["From"], [to_addr], msg.as_string())
    except Exception as e:
        print(f"[ALERT] Email send failed: {e}")


def _send_webhook(device: dict, alert_type: str, value: float, threshold: float):
    """POST JSON to a custom webhook URL from settings, if configured."""
    try:
        s = load_settings()
        url = s.get("webhook_url", "").strip()
        if not url:
            return
        import json, urllib.request
        payload = json.dumps({
            "device_id":   device["id"],
            "device_name": device["name"],
            "host":        device["host"],
            "alert_type":  alert_type,
            "value":       value,
            "threshold":   threshold,
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[ALERT] Custom webhook send failed: {e}")


def _fire_alert(device: dict, alert_type: str, slack_msg: str, value: float = 0, threshold: float = 0):
    """Send Slack message, log the alert, and post to custom webhook."""
    _send_slack(slack_msg)
    _send_discord(slack_msg)
    _send_teams(slack_msg)
    import re
    clean = re.sub(r':[a-z_]+:', '', slack_msg).strip()
    _send_email(f"PingPlotter: {alert_type} — {device['name']}", clean)
    log_alert(device["id"], device["name"], alert_type, value, threshold)
    _send_webhook(device, alert_type, value, threshold)
    _check_correlation(device["id"], device["name"], alert_type)


def check_and_alert(
    device: dict,
    latency: float | None,
    success: bool,
    jitter: float | None,
    route_changed: bool = False,
):
    """Evaluate thresholds for a device and fire alerts if needed."""
    from storage import is_in_maintenance
    if is_in_maintenance(device["id"]):
        return   # silenced during maintenance
    device_id = device["id"]
    name = device["name"]
    thresholds = device.get("thresholds", {})

    # ── Packet loss / host down ────────────────────────────────────────────────
    if not success:
        if _can_alert(device_id, "down"):
            _fire_alert(device, "down",
                f":red_circle: *PingPlotter Alert* — `{name}` ({device['host']}) is *DOWN* (no response)")

    # ── High latency (critical) ────────────────────────────────────────────────
    latency_crit = thresholds.get("latency_ms")
    if latency is not None and latency_crit and latency > latency_crit:
        if _can_alert(device_id, "high_latency"):
            _fire_alert(device, "high_latency",
                f":red_circle: *PingPlotter CRITICAL* — `{name}` high latency: *{latency:.1f}ms* (critical: {latency_crit}ms)",
                latency, latency_crit)

    # ── High latency (warning) ─────────────────────────────────────────────────
    latency_warn = thresholds.get("latency_ms_warn")
    if latency is not None and latency_warn and latency > latency_warn:
        if latency_crit is None or latency <= latency_crit:
            if _can_alert(device_id, "high_latency_warn"):
                _fire_alert(device, "high_latency_warn",
                    f":large_yellow_circle: *PingPlotter Warning* — `{name}` elevated latency: *{latency:.1f}ms* (warning: {latency_warn}ms)",
                    latency, latency_warn)

    # ── High jitter (critical) ─────────────────────────────────────────────────
    jitter_crit = thresholds.get("jitter_ms")
    if jitter is not None and jitter_crit and jitter > jitter_crit:
        if _can_alert(device_id, "high_jitter"):
            _fire_alert(device, "high_jitter",
                f":red_circle: *PingPlotter CRITICAL* — `{name}` high jitter: *{jitter:.1f}ms* (critical: {jitter_crit}ms)",
                jitter, jitter_crit)

    # ── High jitter (warning) ──────────────────────────────────────────────────
    jitter_warn = thresholds.get("jitter_ms_warn")
    if jitter is not None and jitter_warn and jitter > jitter_warn:
        if jitter_crit is None or jitter <= jitter_crit:
            if _can_alert(device_id, "high_jitter_warn"):
                _fire_alert(device, "high_jitter_warn",
                    f":large_yellow_circle: *PingPlotter Warning* — `{name}` elevated jitter: *{jitter:.1f}ms* (warning: {jitter_warn}ms)",
                    jitter, jitter_warn)

    # ── Route change ───────────────────────────────────────────────────────────
    if route_changed:
        if _can_alert(device_id, "route_changed"):
            _fire_alert(device, "route_changed",
                f":arrows_counterclockwise: *PingPlotter* — `{name}` ({device['host']}) route has *changed* (new hop IPs detected)")


def fire_anomaly_alert(device: dict, latency: float, baseline: dict):
    """Fire anomaly alert (statistically unusual latency vs. 7-day baseline)."""
    if not _can_alert(device["id"], "anomaly"):
        return
    mean = baseline["mean"]
    _fire_alert(device, "anomaly",
        f":chart_with_upwards_trend: *PingPlotter Anomaly* — `{device['name']}` latency *{latency:.1f}ms* is anomalous (baseline: {mean:.1f}ms ± {baseline['stddev']:.1f}ms)",
        latency, mean)
