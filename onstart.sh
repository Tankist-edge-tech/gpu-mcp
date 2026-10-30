#!/bin/bash
# vast.ai on-start / first-run script. Expects the 6 project files in /workspace/mcp/.
#
# Env vars (set them in your vast.ai account settings or the template; NOT in chat):
#   HF_TOKEN       fine-grained HF token (create-repos-only scope)
#   CKPT_REPO      e.g. youruser/myproject-ckpt   (private model repo; the script creates it)
#   MCP_SECRET     long random string -> stable connector URL path across instances
#   RELAY_URL / RELAY_ADMIN_KEY   your Cloudflare Worker relay (see relay-worker/README.md): gives a connector
#                  URL that never changes. MCP_SECRET must then be set (same value as the Worker secret).
#   DEADLINE_MIN   minutes until the soft deadline (e.g. 55). Optional; can be armed later by Claude.
#   ON_DEADLINE    stop (default) | destroy | none      ASK_LEAD_MIN=15  FINAL_LEAD_MIN=8
#   GIT_REPO       optional code repo to clone;  KEEP=3 checkpoints on the Hub;  EVERY=600 sync seconds
# CONTAINER_ID / CONTAINER_API_KEY are injected by vast.ai: a per-instance key that can only
# start/stop/destroy THIS instance. Do NOT put your main vast.ai API key on the box.
#
# Training-code convention: if /workspace/.deadline/SAVE_NOW exists -> save a checkpoint now
# (write to .tmp_step_N, rename to step_N), then delete that file.
cd "$(dirname "$0")"

# vast.ai's UI sometimes stores values with surrounding quotes; strip them defensively
for v in HF_TOKEN CKPT_REPO MCP_SECRET RELAY_URL RELAY_ADMIN_KEY DEADLINE_MIN ON_DEADLINE; do
  val="${!v}"; val="${val%\"}"; val="${val#\"}"
  [ -n "$val" ] && export "$v=$val"
done

# Debian-packaged libs (e.g. PyJWT) have no pip RECORD file, so a plain upgrade fails: last resort ignores installed copies.
pip_install() {
  python3 -m pip install -q "$@" 2>/dev/null \
    || python3 -m pip install -q --break-system-packages "$@" 2>/dev/null \
    || python3 -m pip install -q --break-system-packages --ignore-installed "$@"
}
pip_install huggingface_hub
mkdir -p /workspace/checkpoints

if [ -n "$GIT_REPO" ] && [ ! -d /workspace/project ]; then
  git clone "$GIT_REPO" /workspace/project || echo "WARNING: git clone failed"
fi

if [ -n "$HF_TOKEN" ] && [ -n "$CKPT_REPO" ]; then
  # restore the latest checkpoints (no-op on the very first run)
  python3 hf_sync.py pull "$CKPT_REPO" /workspace/checkpoints --type model || true
  # background sync loop
  pkill -f "[h]f_sync.py watch" 2>/dev/null || true
  setsid nohup python3 hf_sync.py watch /workspace/checkpoints "$CKPT_REPO" --type model \
    --keep "${KEEP:-3}" --every "${EVERY:-600}" >> /tmp/sync.log 2>&1 < /dev/null &
else
  echo "WARNING: HF_TOKEN and/or CKPT_REPO not set -> checkpoint sync and final sync are DISABLED"
fi

# read-only check that the per-instance vast.ai key works (also shown in time_left)
python3 deadline.py selftest || echo "WARNING: vast.ai control check failed - auto-stop will NOT work"

# MCP server + tunnel + deadline daemon (prints the connector URL)
bash setup.sh
