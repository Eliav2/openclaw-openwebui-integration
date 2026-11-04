#!/usr/bin/env bash
# Web Push E2E stack: a throwaway Open WebUI and the Cloudflare Worker example
# under `wrangler dev`, in front of it. Usage:
#   push/e2e/stack.sh up <run_dir> <pipe_build>    # pipe_build: an openclaw_pipe.py with _publish_chat_finished
#   push/e2e/stack.sh down <run_dir>
# Ports: OWUI 18780, Worker 18787. Needs docker, bun or npx, uv.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd); push=$(dirname "$here")
cmd=$1; run=$2
OWUI_IMAGE=${OWUI_IMAGE:-ghcr.io/open-webui/open-webui:v0.13.0}
name=${E2E_NAME:-owui-webpush-e2e}
if [ "$cmd" = down ]; then
  docker rm -f "$name" >/dev/null 2>&1 || true
  systemctl --user stop "$name-wrangler" 2>/dev/null || true
  systemctl --user reset-failed "$name-wrangler" 2>/dev/null || true
  exit 0
fi
pipe_build=$(realpath "$3")
# Wait for the first line matching $1 that a follower command ($2...) prints;
# the follower is stopped then. The timeout is a safety net, never expected to
# expire.
first_match() {
  local pattern=$1 line; shift
  coproc FOLLOW { exec "$@"; }
  local pid=$FOLLOW_PID
  while read -r -t 300 line <&"${FOLLOW[0]}"; do
    if [[ $line == *$pattern* ]]; then kill "$pid" 2>/dev/null || true; return 0; fi
  done
  kill "$pid" 2>/dev/null || true
  echo "no '$pattern' from: $*" >&2
  return 1
}
mkdir -p "$run/data" "$run/keys" "$run/pipe" "$run/worker/src"
cp "$pipe_build" "$run/pipe/openclaw_pipe.py"
uv run -q --with cryptography python "$here/vapid_keys.py" "$run/keys"
since=$(date +%s)
docker run -d --name "$name" -p 127.0.0.1:18780:8080 \
  -e WEBUI_SECRET_KEY=e2e-secret -e ENABLE_SIGNUP=true -e WEBUI_URL=http://localhost:18787 \
  -v "$run/data:/app/backend/data" -v "$run/keys:/mnt/keys:ro" -v "$run/pipe:/mnt/pipe:ro" \
  "$OWUI_IMAGE" >/dev/null
# Ready on the image's own healthcheck turning healthy (docker's event).
[ "$(docker inspect -f '{{.State.Health.Status}}' "$name")" = healthy ] ||
  first_match healthy docker events --since "$since" --filter "container=$name" --filter "event=health_status: healthy"
# The Worker example, its two scripts and a dev config (no routes: wrangler dev sends every path to it).
cp "$push/examples/cloudflare-worker/src/index.js" "$run/worker/src/index.js"
mkdir -p "$run/web"; cp "$push/web/oc-sw.js" "$push/web/oc-push.js" "$run/web/"
sed -i 's#\.\./\.\./\.\./web/#../web/#' "$run/worker/src/index.js"
rm -rf "$run/worker/web"; mv "$run/web" "$run/worker/web"   # a second `up` on the same run dir would nest it
cat > "$run/worker/wrangler.jsonc" <<JSON
{ "name": "web-push-e2e", "main": "src/index.js", "compatibility_date": "2025-10-01",
  "rules": [{ "type": "Text", "globs": ["**/oc-sw.js", "**/oc-push.js"], "fallthrough": true }],
  "vars": { "VAPID_PUBLIC_KEY": "$(cat "$run/keys/public.b64")", "FUNCTION_ID": "web_push" } }
JSON
: > "$run/wrangler.log"
systemd-run --user --unit "$name-wrangler" --working-directory "$run/worker" --setenv=PATH="$PATH" \
  -p StandardOutput="append:$run/wrangler.log" -p StandardError="append:$run/wrangler.log" \
  bunx wrangler@4.45.0 dev --port 18787 --ip 127.0.0.1 --local-upstream 127.0.0.1:18780 >/dev/null
first_match "Ready on" tail -n +1 -F "$run/wrangler.log"
echo "up: owui http://127.0.0.1:18780 worker http://localhost:18787"
