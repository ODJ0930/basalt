#!/bin/bash
# Public URL for the basalt server; prints the URL and the API key.
# Named tunnel (fixed URL): a Cloudflare tunnel token in CF_TUNNEL_TOKEN or /content/cf_tunnel_token, and its public hostname
# (e.g. https://llm.example.com) in CF_PUBLIC_URL or /content/cf_public_url. The hostname's service must be http://localhost:8090.
# Without a token: a quick tunnel with a random trycloudflare.com URL that changes on every run.
set -euo pipefail
PORT=${PORT:-8090}  # Colab itself listens on 8080
[ -z "${CF_TUNNEL_TOKEN:-}" ] && [ -s /content/cf_tunnel_token ] && CF_TUNNEL_TOKEN=$(cat /content/cf_tunnel_token)
[ -z "${CF_PUBLIC_URL:-}" ] && [ -s /content/cf_public_url ] && CF_PUBLIC_URL=$(cat /content/cf_public_url)
[ -x /content/cloudflared ] || { wget -q -O /content/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64; chmod +x /content/cloudflared; }
pkill -f "cloudflared tunnel" 2>/dev/null || true
: > /content/cloudflared.log
URL=""
if [ -n "${CF_TUNNEL_TOKEN:-}" ]; then
  [ -n "${CF_PUBLIC_URL:-}" ] || { echo "CF_TUNNEL_TOKEN is set but CF_PUBLIC_URL (the tunnel's hostname) is not" >&2; exit 1; }
  URL=${CF_PUBLIC_URL%/}
  case "$URL" in http*) ;; *) URL="https://$URL" ;; esac
  # the token goes in the environment, not on the command line
  TUNNEL_TOKEN=$CF_TUNNEL_TOKEN setsid nohup /content/cloudflared tunnel --no-autoupdate run \
    > /content/cloudflared.log 2>&1 < /dev/null &
  for _ in $(seq 60); do grep -q "Registered tunnel connection" /content/cloudflared.log && break; sleep 1; done
  grep -q "Registered tunnel connection" /content/cloudflared.log || {
    echo "the named tunnel did not connect; see /content/cloudflared.log" >&2; tail -5 /content/cloudflared.log >&2; exit 1; }
  echo "Named tunnel connected (fixed URL)"
else
  setsid nohup /content/cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > /content/cloudflared.log 2>&1 < /dev/null &
  for _ in $(seq 60); do
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /content/cloudflared.log | head -1 || true)
    [ -n "$URL" ] && break
    sleep 1
  done
  [ -n "$URL" ] || { echo "no tunnel URL; see /content/cloudflared.log" >&2; exit 1; }
  echo "Quick tunnel (the URL changes on every run; set CF_TUNNEL_TOKEN and CF_PUBLIC_URL for a fixed one)"
fi
echo "$URL" > /content/basalt_public_url.txt
echo "Web panel:        $URL/   (enter the API key in the top bar)"
echo "OpenAI base URL:  $URL/v1"
echo "Anthropic URL:    $URL/v1/messages"
echo "API key:          $(cat /content/basalt_api_key.txt)"
