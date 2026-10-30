#!/bin/bash
# Run on the GPU box from the folder that holds server.py (normally /workspace/mcp).
# Starts: MCP server + Cloudflare tunnel + deadline daemon, each under a supervisor loop that
# restarts it if it dies. Running jobs are separate processes and are not affected by restarts.
set -e
cd "$(dirname "$0")"
# pip: Ubuntu's system Python may refuse installs ("externally-managed-environment"); retry with the override
# Debian-packaged libs (e.g. PyJWT) have no pip RECORD file, so a plain upgrade fails: last resort ignores installed copies.
pip_install() {
  python3 -m pip install -q "$@" 2>/dev/null \
    || python3 -m pip install -q --break-system-packages "$@" 2>/dev/null \
    || python3 -m pip install -q --break-system-packages --ignore-installed "$@"
}
pip_install "mcp[cli]<2"

CF_URL=https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
if ! command -v cloudflared >/dev/null; then
  command -v wget >/dev/null || command -v curl >/dev/null || (apt-get update -qq && apt-get install -y -qq curl)
  if command -v wget >/dev/null; then wget -q "$CF_URL" -O /usr/local/bin/cloudflared
  else curl -fsSL "$CF_URL" -o /usr/local/bin/cloudflared; fi
  chmod +x /usr/local/bin/cloudflared
fi

SECRET_FROM_ENV=${MCP_SECRET:+1}
# Set MCP_SECRET yourself (e.g. in vast.ai account env vars) to keep the URL path stable across instances.
export MCP_SECRET=${MCP_SECRET:-$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")}
export MAX_WAIT=${MAX_WAIT:-900}
mkdir -p /workspace/.deadline

supervise() {  # supervise NAME "COMMAND": restart COMMAND forever, log to /tmp/NAME.log
  local name=$1; shift
  setsid nohup bash -c 'while true; do '"$*"'; echo "[supervise] exited, restarting in 3s"; sleep 3; done' \
    "supervise-$name" >> "/tmp/$name.log" 2>&1 < /dev/null &
}

# stop old supervisors first, then their children
pkill -f "supervise-" 2>/dev/null || true
pkill -f "[s]erver.py" 2>/dev/null || true
pkill -f "[c]loudflared tunnel" 2>/dev/null || true
pkill -f "[d]eadline.py daemon" 2>/dev/null || true
pkill -f "[r]elay_register.py" 2>/dev/null || true
sleep 1
rm -f /tmp/tunnel.log

supervise mcp      "MCP_SECRET=$MCP_SECRET MAX_WAIT=$MAX_WAIT python3 server.py"
supervise tunnel   "cloudflared tunnel --url http://127.0.0.1:8765"
supervise deadline "python3 deadline.py daemon"   # idles until a deadline is armed (DEADLINE_MIN or deadline_set)
RELAY_MODE=""
if [ -n "$RELAY_URL" ] && [ -n "$RELAY_ADMIN_KEY" ]; then
  # keeps the Worker relay pointed at the current tunnel URL (key is read from the environment, not the command line)
  supervise relay "python3 relay_register.py"
  RELAY_MODE=1
fi

URL=""
for i in $(seq 1 30); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/tunnel.log 2>/dev/null | tail -1 || true)
  [ -n "$URL" ] && break
  sleep 1
done

echo
if [ -n "$RELAY_MODE" ]; then
  echo "STABLE connector URL (add once, never changes):"
  echo "  $RELAY_URL/$MCP_SECRET/mcp"
  [ -n "$SECRET_FROM_ENV" ] || echo "  !! MCP_SECRET was generated randomly this time -> set MCP_SECRET in your env (and as the Worker secret) to keep this URL stable"
  echo "(box tunnel, auto-registered with the relay: $URL)"
else
  echo "Add this as a custom connector in claude.ai:"
  echo "  $URL/$MCP_SECRET/mcp"
  echo "(no RELAY_URL/RELAY_ADMIN_KEY set -> this URL changes whenever the tunnel restarts)"
  echo "Current tunnel URL: grep -o 'https://[a-z0-9-]*\\.trycloudflare\\.com' /tmp/tunnel.log | tail -1"
fi
