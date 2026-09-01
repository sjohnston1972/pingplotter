"""
collector.py - Background ping engine, one thread per device
"""
import threading
import time
import platform
import subprocess
import socket
import re
import urllib.request
from datetime import datetime
from storage import save_result, save_trace_run, load_devices, load_results, log_alert
from alerts import check_and_alert

_threads: dict[int, threading.Thread] = {}
_stop_flags: dict[int, threading.Event] = {}
_status: dict[int, dict] = {}  # live status cache
_status_lock = threading.Lock()
_last_route: dict[int, list] = {}  # device_id -> list of hop IPs from last traceroute run
_streak: dict[int, int] = {}  # device_id -> consecutive failure count
_lifecycle_lock = threading.Lock()  # guards start_device's check-then-launch section


def _parse_hop_windows(line: str) -> dict | None:
    m = re.match(r'^\s*(\d+)\s+(.*)', line)
    if not m:
        return None
    hop_num, rest = int(m.group(1)), m.group(2)
    lats = []
    for token in re.findall(r'[<]?(\d+)\s*ms|\*', rest):
        lats.append(float(token) if token else None)
    ip_m = re.search(r'(\d+\.\d+\.\d+\.\d+)', rest)
    ip = ip_m.group(1) if ip_m else None
    while len(lats) < 3:
        lats.append(None)
    return {"hop": hop_num, "ip": ip, "latencies": lats[:3]}


def _parse_hop_linux(line: str) -> dict | None:
    m = re.match(r'^\s*(\d+)\s+(.*)', line)
    if not m:
        return None
    hop_num, rest = int(m.group(1)), m.group(2).strip()
    if re.match(r'^[*\s]+$', rest):
        return {"hop": hop_num, "ip": None, "latencies": [None, None, None]}
    ip_m = re.match(r'(\S+)', rest)
    ip = ip_m.group(1) if ip_m else None
    lats = [float(x) for x in re.findall(r'([\d.]+)\s*ms', rest)]
    while len(lats) < 3:
        lats.append(None)
    return {"hop": hop_num, "ip": ip, "latencies": lats[:3]}


def _dest_latency(hops: list[dict]) -> float | None:
    for h in reversed(hops):
        valid = [l for l in h["latencies"] if l is not None]
        if valid:
            return round(sum(valid) / len(valid), 2)
    return None


def _run_traceroute(host: str, packet_size: int | None = None) -> tuple[bool, list[dict], float | None]:
    system = platform.system().lower()
    if system == "windows":
        cmd = ["tracert", "-d", "-h", "30", "-w", "1000", host]
        parse_fn = _parse_hop_windows
    else:
        cmd = ["traceroute", "-n", "-m", "30", "-w", "2", host]
        if packet_size is not None:
            cmd.append(str(packet_size))
        parse_fn = _parse_hop_linux
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, [], None
    hops = []
    for line in result.stdout.splitlines():
        h = parse_fn(line)
        if h:
            hops.append(h)
    dest_lat = _dest_latency(hops)
    return dest_lat is not None, hops, dest_lat


def _probe_http(host: str, timeout: int = 5) -> tuple[bool, float | None]:
    """HTTP HEAD probe. Measures time to first response."""
    url = host if host.startswith(("http://", "https://")) else "http://" + host
    try:
        req = urllib.request.Request(url, method="HEAD")
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=timeout):
            return True, round((time.perf_counter() - t0) * 1000, 2)
    except urllib.error.HTTPError as e:
        # Server responded — it's reachable even if 4xx/5xx
        return True, round((time.perf_counter() - t0) * 1000, 2)
    except Exception:
        return False, None


def _probe_tcp(host: str, timeout: int = 3) -> tuple[bool, float | None]:
    """TCP connect probe. host must be 'hostname:port'."""
    if ":" not in host:
        return False, None
    addr, port_str = host.rsplit(":", 1)
    try:
        port = int(port_str)
    except ValueError:
        return False, None
    t0 = time.perf_counter()
    try:
        sock = socket.create_connection((addr, port), timeout=timeout)
        latency = round((time.perf_counter() - t0) * 1000, 2)
        sock.close()
        return True, latency
    except Exception:
        return False, None


def _probe_dns(host: str, timeout: int = 3) -> tuple[bool, float | None]:
    """DNS resolution probe. Measures getaddrinfo() time."""
    t0 = time.perf_counter()
    try:
        socket.getaddrinfo(host, None)
        return True, round((time.perf_counter() - t0) * 1000, 2)
    except Exception:
        return False, None


def _ping_once(host: str, packet_size: int | None = None, df_bit: bool = False) -> tuple[bool, float | None]:
    """Send a single ping. Returns (success, latency_ms)."""
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", "1000"]
        if packet_size is not None:
            cmd += ["-l", str(packet_size)]
        if df_bit:
            cmd += ["-f"]
        cmd.append(host)
    else:
        cmd = ["ping", "-c", "1", "-W", "1"]
        if packet_size is not None:
            cmd += ["-s", str(packet_size)]
        if df_bit:
            cmd += ["-M", "do"]
        cmd.append(host)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        output = result.stdout
        if system == "windows":
            match = re.search(r"Average = (\d+)ms", output) or re.search(r"time[=<](\d+)ms", output)
        else:
            match = re.search(r"time[=<]([\d.]+) ms", output)
        if result.returncode == 0 and match:
            return True, float(match.group(1))
        return False, None
    except (subprocess.TimeoutExpired, Exception):
        return False, None


def _probe_and_record(device_id: int, device: dict, last_latency: float | None) -> float | None:
    """Run a single probe for `device`, persist/alert on the result, and return
    the latency to use as `last_latency` on the next call (for jitter calc).

    Shared by `_device_loop` (the scheduled loop) and `probe_now` (an
    out-of-band immediate probe), so both paths behave identically.
    """
    host = device["host"]
    probe_type = device.get("probe_type", "icmp")
    packet_size = device.get("packet_size") or None
    df_bit = bool(device.get("df_bit", False))
    ts = datetime.utcnow().isoformat()
    route_changed = False
    if probe_type == "http":
        success, latency = _probe_http(host)
    elif probe_type == "tcp":
        success, latency = _probe_tcp(host)
    elif probe_type == "dns":
        success, latency = _probe_dns(host)
    elif probe_type == "traceroute":
        success, hops, latency = _run_traceroute(host, packet_size)
        save_trace_run(device_id, ts, hops)
        current_ips = [h.get("ip") for h in hops if h.get("ip")]
        prev_ips = _last_route.get(device_id)
        if prev_ips is not None and current_ips != prev_ips:
            route_changed = True
        _last_route[device_id] = current_ips
    else:
        success, latency = _ping_once(host, packet_size, df_bit)

    # Calculate jitter (difference from last reading)
    jitter = None
    if latency is not None and last_latency is not None:
        jitter = abs(latency - last_latency)
    if latency is not None:
        last_latency = latency

    save_result(device_id, latency, success, jitter)

    # Update streak counter
    if not success:
        _streak[device_id] = _streak.get(device_id, 0) + 1
    else:
        _streak[device_id] = 0

    # Update live status cache
    with _status_lock:
        _status[device_id] = {
            "device_id": device_id,
            "name": device["name"],
            "host": host,
            "last_ping": datetime.utcnow().isoformat(),
            "last_latency": latency,
            "last_success": success,
            "last_jitter": jitter,
            "status": "up" if success else "down",
            "streak": _streak.get(device_id, 0),
        }

    # Check alert thresholds
    check_and_alert(device, latency, success, jitter, route_changed=route_changed)

    # Anomaly detection
    if success and latency is not None:
        import baseline as bl
        if bl.is_anomaly(device_id, latency):
            from alerts import fire_anomaly_alert
            fire_anomaly_alert(device, latency, bl.get_baseline(device_id))

    return last_latency


def _device_loop(device_id: int, stop_event: threading.Event):
    last_latency: float | None = None

    while not stop_event.is_set():
        devices = load_devices()
        device = next((d for d in devices if d["id"] == device_id), None)
        if device is None:
            break

        interval = device.get("interval_sec", 5)
        last_latency = _probe_and_record(device_id, device, last_latency)

        stop_event.wait(interval)


def probe_now(device_id: int) -> bool:
    """Run a single immediate, out-of-band probe for device_id.

    This does NOT touch the device's scheduled loop/thread at all -- it just
    runs one probe synchronously and records it, reusing the same probe
    dispatch and side effects (save_result/status/alerts) as the loop. Used by
    `/ping-now` so an immediate reading never risks killing monitoring.

    Returns False if the device no longer exists.
    """
    devices = load_devices()
    device = next((d for d in devices if d["id"] == device_id), None)
    if device is None:
        return False
    status = get_device_status(device_id)
    last_latency = status.get("last_latency") if status else None
    _probe_and_record(device_id, device, last_latency)
    return True


def start_device(device_id: int):
    """Start a monitoring thread for device_id if one isn't already running.

    Guarded by `_lifecycle_lock` so two concurrent callers (e.g. an edit and a
    ping-now landing at the same moment) can never both pass the check and
    each launch a thread, which would leave two live loops for one device.

    The "already running" check only looks at whether the *current* thread's
    own stop_event is unset -- a thread whose stop_event has already been set
    (i.e. one mid-shutdown from a restart) never blocks a new thread from
    starting, even if it is still technically alive() while it finishes an
    in-flight probe.
    """
    with _lifecycle_lock:
        existing_thread = _threads.get(device_id)
        existing_stop = _stop_flags.get(device_id)
        if (
            existing_thread is not None
            and existing_thread.is_alive()
            and existing_stop is not None
            and not existing_stop.is_set()
        ):
            return  # a live thread for this device is already running
        stop_event = threading.Event()
        _stop_flags[device_id] = stop_event
        t = threading.Thread(target=_device_loop, args=(device_id, stop_event), daemon=True)
        _threads[device_id] = t
        t.start()


def stop_device(device_id: int) -> threading.Thread | None:
    """Signal device_id's loop to stop.

    Returns the thread that was told to stop (if any) so callers such as
    `restart_device` can join it before starting a replacement.
    """
    with _lifecycle_lock:
        stop_event = _stop_flags.get(device_id)
        thread = _threads.get(device_id)
    if stop_event is not None:
        stop_event.set()
    return thread


def restart_device(device_id: int, join_timeout: float = 10.0):
    """Stop the device's current loop and start a fresh one, guaranteeing a
    live thread is running when this returns (modulo the device having been
    deleted out from under it).

    Setting the stop event wakes a loop that's idle in `stop_event.wait()`
    almost immediately, so in the common case the join below returns quickly
    and the handoff is clean (no overlap). If the outgoing thread is instead
    busy running a slow probe (e.g. traceroute) and doesn't exit within
    join_timeout, start_device() is still called -- its guard only blocks on
    a thread whose OWN stop_event is unset, and this thread's stop_event was
    just set, so the stale thread can never prevent a new one from starting.
    The stale thread simply finishes its in-flight probe and exits on its own
    next time it checks its stop_event.
    """
    old_thread = stop_device(device_id)
    if old_thread is not None and old_thread.is_alive():
        old_thread.join(timeout=join_timeout)
    start_device(device_id)


def start_all():
    for device in load_devices():
        if device.get("enabled", True):
            start_device(device["id"])


def get_live_status() -> list[dict]:
    with _status_lock:
        return list(_status.values())


def get_device_status(device_id: int) -> dict | None:
    with _status_lock:
        return _status.get(device_id)


def discover_mtu(host: str) -> dict:
    """Binary search for path MTU using DF-bit pings. Returns {mtu_bytes, payload_size, tested_at}."""
    low, high = 576, 1500
    found = low
    while low <= high:
        mid = (low + high) // 2
        payload = mid - 28  # IP header (20) + ICMP header (8)
        success, _ = _ping_once(host, packet_size=payload, df_bit=True)
        if success:
            found = mid
            low = mid + 1
        else:
            high = mid - 1
    return {
        "mtu_bytes": found,
        "payload_size": found - 28,
        "tested_at": datetime.utcnow().isoformat(),
    }
