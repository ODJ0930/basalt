#!/bin/bash
# Starts the basalt server in the background on the pack from download.sh, with an API key (needed once a tunnel makes it public).
# The key is written to /content/basalt_api_key.txt; the server log goes to /content/basalt-server.log.
set -euo pipefail
SRC=${SRC:-/content/basalt}
PACK=${PACK:-/mnt/models/DeepSeek-V4.1-Flash-1017-UD-IQ1_M.basalt}
PORT=${PORT:-8090}  # Colab itself listens on 8080
CONTEXT=${CONTEXT:-262144}  # Qwen3.8 Flash-Next native length; DeepSeek takes up to 1,048,576
VISION=${VISION:-off}       # image input: off, dynamic (Qwen packs carry the vision projector; DeepSeek has none)
KV=${KV:-}                  # empty = engine default (Flash-Next fp16, DeepSeek fp8); Flash-Next also int8 / q4_0 / k8v4
KEYFILE=/content/basalt_api_key.txt
[ -f "$PACK" ] || { echo "no pack at $PACK: wait for download.sh" >&2; exit 1; }
[ -x "$SRC/bin/basalt" ] || { echo "no $SRC/bin/basalt: wait for build.sh" >&2; exit 1; }
[ -s "$KEYFILE" ] || python3 -c "import secrets; print(secrets.token_urlsafe(24))" > "$KEYFILE"
KV_JSON=""; [ -n "$KV" ] && KV_JSON=", \"kv\": \"$KV\""
# with images on, treat every request as remote (the tunnel is a forwarder): data: URLs only, no local files or fetched URLs
VISION_JSON=""; IMG_JSON=""
if [ "$VISION" != off ]; then VISION_JSON=", \"vision\": \"$VISION\""; IMG_JSON=", \"image-remote-only\": true"; fi
cat > /content/basalt_config.json <<EOF
{
  "version": 2,
  "models": [{ "model": "$PACK", "gpu": "0"$VISION_JSON }],
  "engine": { "max-context": $CONTEXT$KV_JSON },
  "server": { "port": $PORT, "allowed-host": ["*"]$IMG_JSON }
}
EOF
# a restart (new context or KV): stop the running server first and wait for it to free the GPU
if pkill -f "[s]erve.server --engine-bin"; then
  for _ in $(seq 60); do pgrep -f "[b]in/basalt --serve" > /dev/null || break; sleep 1; done
  echo "stopped the running server"
fi
cd "$SRC"
setsid nohup bin/basalt serve --config /content/basalt_config.json --api-key "$(cat "$KEYFILE")" \
  > /content/basalt-server.log 2>&1 < /dev/null &
echo "basalt started (pid $!), port $PORT; log: /content/basalt-server.log"
