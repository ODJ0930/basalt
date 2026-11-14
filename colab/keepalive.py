"""Keeps a Colab runtime busy and the basalt server and tunnel up. Run it as the notebook's last cell and leave it running:

    !python3 /content/basalt/colab/keepalive.py

A running cell is what keeps Colab from treating the runtime as idle. Every minute it checks the server and, when a tunnel was
opened (/content/basalt_public_url.txt), the public URL: a server that exited is started again from the same config and key,
and a tunnel that stopped answering is reopened (a quick tunnel gets a NEW URL, printed here). Ctrl+C / the stop button ends
only this watcher; the server and tunnel keep running.
"""
import json
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

SERVER_LOG = "/content/basalt-server.log"
URL_FILE = Path("/content/basalt_public_url.txt")
START = ('cd /content/basalt && setsid nohup bin/basalt serve --config /content/basalt_config.json '
         '--api-key "$(cat /content/basalt_api_key.txt)" > /content/basalt-server.log 2>&1 < /dev/null &')
MAX_RESTARTS = 3          # per hour, so a server that crashes on start does not loop


def now():
    return datetime.now().strftime("%H:%M")


def running(pattern):
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def loaded():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8090/health", timeout=15) as r:
            return bool(json.load(r).get("loaded"))
    except Exception:
        return False


def answers(url):
    try:
        with urllib.request.urlopen(url + "/health", timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


restarts = []
tunnel_misses = 0
print(f"{now()} keepalive on: checks every minute, a status line every 10 minutes", flush=True)
try:
    for minute in range(10 ** 9):
        if not running("[s]erve.server"):
            restarts = [t for t in restarts if time.time() - t < 3600]
            if len(restarts) < MAX_RESTARTS:
                print(f"\n{now()} the server is gone; starting it again (log: {SERVER_LOG})", flush=True)
                subprocess.run(["bash", "-c", START])
                restarts.append(time.time())
            elif minute % 10 == 0:
                print(f"\n{now()} the server keeps exiting; not restarting. See: tail -50 {SERVER_LOG}", flush=True)
        if URL_FILE.exists():
            if running("[c]loudflared tunnel") and answers(URL_FILE.read_text().strip()):
                tunnel_misses = 0
            else:
                tunnel_misses += 1
                if tunnel_misses >= 3:   # three minutes without an answer: reopen
                    print(f"\n{now()} the tunnel stopped answering; reopening it", flush=True)
                    subprocess.run(["bash", "/content/basalt/colab/tunnel.sh"], capture_output=True)
                    if URL_FILE.exists():
                        print(f"{now()} NEW web panel URL: {URL_FILE.read_text().strip()}/  (same API key)", flush=True)
                    tunnel_misses = 0
        if minute % 10 == 0:
            server = "loaded" if loaded() else ("starting" if running("[s]erve.server") else "down")
            tunnel = URL_FILE.read_text().strip() if URL_FILE.exists() else "none"
            print(f"{now()} server {server}, tunnel {tunnel}", flush=True)
        time.sleep(60)
except KeyboardInterrupt:
    print(f"\n{now()} keepalive stopped (the server and tunnel keep running)")
