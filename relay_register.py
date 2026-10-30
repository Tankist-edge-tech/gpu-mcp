#!/usr/bin/env python3
"""
Keeps the Worker relay pointed at this box's current Cloudflare quick-tunnel URL.

Every INTERVAL seconds: read the newest https://*.trycloudflare.com URL from the tunnel log;
if it changed (or REFRESH seconds passed), POST it to $RELAY_URL/__relay/set with the admin key
in the x-admin-key header (never in a URL). Retries forever; safe to run under a supervisor.

Env: RELAY_URL (e.g. https://gpu-mcp-relay.<sub>.workers.dev), RELAY_ADMIN_KEY,
     TUNNEL_LOG (default /tmp/tunnel.log), INTERVAL (default 10), REFRESH (default 300)
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

RELAY = os.environ.get("RELAY_URL", "").rstrip("/")
KEY = os.environ.get("RELAY_ADMIN_KEY", "")
LOG = os.environ.get("TUNNEL_LOG", "/tmp/tunnel.log")
INTERVAL = float(os.environ.get("INTERVAL", "10"))
REFRESH = float(os.environ.get("REFRESH", "300"))
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def say(msg):
    print(f"{time.strftime('%F %T', time.gmtime())}Z relay: {msg}", flush=True)


def current_tunnel():
    try:
        with open(LOG, errors="replace") as f:
            found = URL_RE.findall(f.read())
        return found[-1] if found else None
    except OSError:
        return None


def register(url):
    req = urllib.request.Request(
        f"{RELAY}/__relay/set", method="POST",
        data=json.dumps({"url": url}).encode(),
        headers={"x-admin-key": KEY, "content-type": "application/json",
                 "user-agent": "gpu-box-relay/1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def main():
    if not RELAY or not KEY:
        sys.exit("RELAY_URL and RELAY_ADMIN_KEY must be set")
    last_url, last_ok = None, 0.0
    say(f"watching {LOG} -> {RELAY}")
    while True:
        url = current_tunnel()
        if url and (url != last_url or time.time() - last_ok > REFRESH):
            try:
                register(url)
                if url != last_url:
                    say(f"registered {url}")
                last_url, last_ok = url, time.time()
            except urllib.error.HTTPError as e:
                say(f"register failed: HTTP {e.code} {e.read()[:120]!r}")
            except Exception as e:
                say(f"register failed: {e!r}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
