"""
main.py - PingPlotter entry point
"""
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
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=False)
