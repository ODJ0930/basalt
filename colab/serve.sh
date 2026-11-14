#!/bin/bash
# Starts the basalt server in the background on the pack from download.sh, with an API key (needed once a tunnel makes it public).
# The key is written to /content/basalt_api_key.txt; the server log goes to /content/basalt-server.log.
set -euo pipefail
SRC=${SRC:-/content/basalt}
PACK=${PACK:-/mnt/models/DeepSeek-V4.1-Flash-1017-UD-IQ1_M.basalt}
PORT=${PORT:-8090}  # Colab itself listens on 8080
CONTEXT=${CONTEXT:-32768}
KEYFILE=/content/basalt_api_key.txt
[ -f "$PACK" ] || { echo "no pack at $PACK: wait for download.sh" >&2; exit 1; }
[ -x "$SRC/bin/basalt" ] || { echo "no $SRC/bin/basalt: wait for build.sh" >&2; exit 1; }
[ -s "$KEYFILE" ] || python3 -c "import secrets; print(secrets.token_urlsafe(24))" > "$KEYFILE"
cat > /content/basalt_config.json <<EOF
{
  "version": 2,
  "models": [{ "model": "$PACK", "gpu": "0" }],
  "engine": { "max-context": $CONTEXT },
  "server": { "port": $PORT, "allowed-host": ["*"] }
}
EOF
cd "$SRC"
setsid nohup bin/basalt serve --config /content/basalt_config.json --api-key "$(cat "$KEYFILE")" \
  > /content/basalt-server.log 2>&1 < /dev/null &
echo "basalt started (pid $!), port $PORT; log: /content/basalt-server.log"
