# PingPlotter

A self-hosted network monitoring dashboard. Probes hosts continuously, stores results locally as flat CSV files, and serves a single-page web UI with live charts, topology graphs, alerting, and reports — no database required.

<img width="2539" height="1276" alt="image" src="https://github.com/user-attachments/assets/1e36183b-e1d9-4755-b0da-b0463ff300be" />

---

## Features

### Monitoring
- **Five probe types:** ICMP ping, HTTP HEAD, TCP connect, DNS resolution, and MTR-style traceroute
- **Per-device configuration:** custom interval, packet size, alert thresholds (latency, jitter, packet loss, host-down)
- **Device groups** and **maintenance windows** to suppress false alerts during planned downtime
- **MTU discovery** — binary-searches the maximum unfragmented packet size to a host

### Dashboard tabs
| Tab | What it shows |
|---|---|
| **Monitor** | Live latency sparklines, status indicators, drill-down charts with Chart.js |
| **Compare** | Overlay latency/jitter/loss for multiple devices on one chart |
| **Map** | Leaflet.js map plotting traceroute hop geolocations |
| **Topology** | D3.js force graph of shared network paths across all traceroute devices, with per-hop labels (`hop# · ip`) and hover stats (avg latency, jitter, packet loss) |
| **Speedtest** | Periodic bandwidth/latency benchmarks via speedtest-cli |
| **Reports** | SLA summary, uptime %, incident list, heatmap, latency histogram, CSV export |
| **Settings** | Alert channels, speedtest schedule, digest email, data retention |

### Alerting
- **Slack**, **Discord**, **Microsoft Teams**, **email** (SMTP) — all configurable from the UI
- Per-device thresholds for latency, jitter, and packet loss
- 5-minute per-device cooldown to suppress alert storms
- Test-fire buttons in settings to verify webhook/email config

### Intelligence
- **Baseline learning** — captures rolling avg/jitter per device, used for anomaly context
- **Route-change detection** — alerts when traceroute hop IPs change between runs
- **Daily digest** — scheduled email summary of uptime and incident counts
- **Data retention purge** — configurable, keeps storage lean

### Data model
- All data in `data/` as flat CSV files and a single `devices.json` — no database
- Server-Sent Events (`/api/sse`) for live push updates to the UI
- Full REST API with FastAPI auto-docs at `/docs`

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, uvicorn |
| Probing | stdlib `socket`/`urllib` for ICMP/TCP/HTTP/DNS; system `traceroute` for MTR |
| Storage | Flat CSV per device + JSON config (thread-safe with `threading.Lock`) |
| Frontend | Vanilla JS, single HTML file (`static/index.html`) |
| Charts | Chart.js 4.4 + chartjs-plugin-annotation |
| Topology graph | D3.js v7 force simulation |
| Map | Leaflet.js 1.9 + OpenStreetMap tiles |
| Geo lookup | ip-api.com (free, no key required) |
| Speedtest | speedtest-cli |
| Alerts | slack-sdk; Discord/Teams/email via stdlib `urllib`/`smtplib` |

---

## Architecture

```
main.py
├── storage.init_storage()       # create data/ dirs, seed devices.json
├── collector.start_all()        # spawn one thread per device
├── speedtest_runner.start()     # optional background speedtest thread
├── digest.start()               # optional daily email digest thread
└── uvicorn → api.py             # FastAPI app, serves static/ and REST API
```

### Collector (`collector.py`)
One `threading.Thread` per device. Each thread sleeps `interval_sec`, runs the appropriate probe, appends a row to `data/results/device_N.csv`, and fires alerts if thresholds are exceeded. Traceroute results go to a separate `data/traces/device_N.csv`.

### Storage (`storage.py`)
Pure file I/O with a single `threading.Lock` for write safety. Key functions:

| Function | Returns |
|---|---|
| `load_trace_stats(device_id, runs)` | Per-hop stats (avg/min/max/jitter/loss) across last N traceroute runs |
| `load_uptime_stats(device_id, hours)` | Uptime %, incident list |
| `load_heatmap(device_id, hours)` | Hour-of-day × day-of-week loss matrix |
| `load_latency_histogram(device_id, hours, buckets)` | Latency distribution buckets |
| `load_sla(device_id, hours, target_ms)` | SLA compliance % |

### API (`api.py`)
FastAPI app. Thin layer over storage — most endpoints just call a storage function and return JSON. Exceptions: SSE endpoint streams events to connected browsers; MTU discovery runs a background binary-search probe; geo lookup caches ip-api.com responses in memory.

### Frontend (`static/index.html`)
Single HTML file (~2400 lines). No build step. All JS is vanilla; library dependencies loaded from CDN. Tab state is managed with `switchTab()`, which lazy-loads data on first visit. Charts are Chart.js instances stored in a `charts` map and destroyed/recreated on each reload to avoid canvas reuse errors.

#### Topology graph (D3.js)
`loadTopology()` fetches traceroute stats for all devices, builds a node/edge map (nodes keyed by IP), then calls `renderTopologyGraph()`.

**Node construction (two-pass):**
- Pass 1 (per-device loop): accumulates `_avg_sum`, `_avg_count`, `_jitter_sum`, `_jitter_count` and `loss_pct` (running max) on each hop node
- Pass 2 (after loop): finalises `avg_ms` and `jitter_ms` as rounded averages, deletes scratch fields

**Rendering:**
- Circles sized by `nodeRadius(d)` — origin (10px), destination (8px), hops scaled by `hop_count` (shared hops appear larger, amber)
- Labels: `"hop# · ip"` for known hops, `"hop#"` for unresponsive ones
- Hover tooltip: Avg / Jitter / Loss for hop nodes; `esc()` applied to all server-sourced strings

---

## Setup

### Requirements
- Python 3.12+
- `traceroute` (Linux/Mac) or admin rights for ICMP (Windows — see note below)
- Optional: `speedtest-cli` for bandwidth tests

### Install

```bash
pip install -r requirements.txt
```

### Run

```bash
python main.py
```

Open **http://localhost:8000** — the UI loads immediately with no further config.

API docs: **http://localhost:8000/docs**

### Windows note
Raw ICMP sockets require administrator privileges on Windows. Run from an elevated terminal, or use TCP/HTTP probe types instead.

---

## Configuration

All configuration is done from the **Settings** tab in the UI. Nothing requires editing files directly.

### Alert channels

| Channel | How to configure |
|---|---|
| Slack | Paste an Incoming Webhook URL — [create one here](https://api.slack.com/messaging/webhooks) |
| Discord | Paste a Discord Webhook URL |
| Teams | Paste a Teams Incoming Webhook URL |
| Email | SMTP host, port, username, password, from/to addresses |

Use the **Test** buttons to verify before saving.

### Speedtest
Set the interval in minutes (0 = disabled). Results appear in the Speedtest tab.

### Digest email
Daily summary sent at a configurable hour. Requires email settings to be configured first.

### Data retention
Set a maximum age in days. Click **Purge** to delete older rows immediately.

---

## REST API

Full interactive docs at `/docs`. Quick reference:

```bash
# List devices
curl http://localhost:8000/api/devices

# Add a device
curl -X POST http://localhost:8000/api/devices \
  -H "Content-Type: application/json" \
  -d '{"name":"Router","host":"192.168.1.1","probe_type":"icmp","interval_sec":10}'

# Get hop stats for a traceroute device
curl http://localhost:8000/api/devices/1/hops

# Get SLA stats
curl "http://localhost:8000/api/devices/1/sla?hours=168&target_ms=100"

# Export results as CSV
curl http://localhost:8000/api/devices/1/export.csv

# Run a speedtest now
curl -X POST http://localhost:8000/api/speedtest/run

# Get live status of all devices
curl http://localhost:8000/api/status
```

---

## Data files

All data lives in `data/` (created automatically on first run):

| File | Contents |
|---|---|
| `data/devices.json` | Device list, groups, maintenance windows, settings |
| `data/results/device_N.csv` | Ping history (timestamp, latency_ms, success, jitter_ms) |
| `data/traces/device_N.csv` | Traceroute history (timestamp, hop, ip, lat1, lat2, lat3) |
| `data/alerts.csv` | Alert history |
| `data/speedtest.csv` | Speedtest history |
| `data/baseline.json` | Per-device rolling avg/jitter baselines |
