"""
main.py - PingPlotter entry point
"""
import os
import uvicorn
import collector
import storage
import speedtest_runner
import digest as digest_mod

storage.init_storage()
collector.start_all()

s = storage.load_settings()
st_interval = s.get("speedtest_interval_minutes", 60)
if st_interval > 0:
    speedtest_runner.start(st_interval)

digest_interval = s.get("digest_interval_hours", 0)
if digest_interval > 0:
    digest_mod.start(digest_interval)

if __name__ == "__main__":
    # Bind to localhost by default - opt in to network exposure with
    # PINGPLOTTER_HOST (e.g. PINGPLOTTER_HOST=0.0.0.0). Pair a non-localhost
    # bind with PINGPLOTTER_TOKEN (see api.py) so the API isn't wide open.
    host = os.environ.get("PINGPLOTTER_HOST", "127.0.0.1")
    port = int(os.environ.get("PINGPLOTTER_PORT", "8000"))
    uvicorn.run("api:app", host=host, port=port, reload=False)
