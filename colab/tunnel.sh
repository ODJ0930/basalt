#!/bin/bash
# A Cloudflare quick tunnel to the basalt server; prints the public URL and the API key.
set -euo pipefail
PORT=${PORT:-8090}  # Colab itself listens on 8080
[ -x /content/cloudflared ] || { wget -q -O /content/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64; chmod +x /content/cloudflared; }
pkill -f "cloudflared tunnel" 2>/dev/null || true
setsid nohup /content/cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > /content/cloudflared.log 2>&1 < /dev/null &
for _ in $(seq 60); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /content/cloudflared.log | head -1 || true)
  [ -n "$URL" ] && break
  sleep 1
done
[ -n "${URL:-}" ] || { echo "no tunnel URL; see /content/cloudflared.log" >&2; exit 1; }
echo "$URL" > /content/basalt_public_url.txt
echo "OpenAI base URL:  $URL/v1"
echo "Anthropic URL:    $URL/v1/messages"
echo "API key:          $(cat /content/basalt_api_key.txt)"
