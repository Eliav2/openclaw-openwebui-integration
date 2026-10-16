#!/usr/bin/env bash
# Bring up the end-to-end stack on a Linux host with Docker, Node and uv:
#
#   fake model (e2e/fake_llm.py)  <-  OpenClaw Gateway  <-  Open WebUI container
#
# then install the Pipe, Filter and Action exactly the way the README tells a
# user to (`uv run install*.py install`), including device pairing. The
# installer's own smoke test is the first end-to-end assertion: it fails the
# script if a real chat completion does not come back through the Gateway.
#
# Inputs (env): OWUI_IMAGE, OPENCLAW_VERSION (npm spec, e.g. 2026.9.3 or latest),
# E2E_LOG_DIR. Writes the credentials the test suite needs to $GITHUB_ENV when
# running under Actions.
#
# Optional, for the screenshots workflow (scripts/screenshots): STACK_ADMIN_NAME
# and STACK_ADMIN_EMAIL for the admin account, and STACK_CONFIG_PATCH, a JSON5
# patch applied to the Gateway config before it starts (other model names, an
# extra plugin). FAKE_LLM_* variables reach the fake model as they are.
set -euo pipefail

cd "$(dirname "$0")/.."

: "${OWUI_IMAGE:?set OWUI_IMAGE}"
: "${OPENCLAW_VERSION:?set OPENCLAW_VERSION}"
LOG_DIR="${E2E_LOG_DIR:-$PWD/e2e-logs}"
mkdir -p "$LOG_DIR"

# The Open WebUI image is the stack's largest download. It is pulled while the
# fake model, the npm install and the Gateway come up; `docker run` waits for it.
PULL_START=$SECONDS
docker pull --quiet "$OWUI_IMAGE" >"$LOG_DIR/docker-pull.log" 2>&1 &
PULL_PID=$!

OWUI_URL="http://localhost:8080"
OWUI_EMAIL="${STACK_ADMIN_EMAIL:-e2e-admin@example.com}"
OWUI_ADMIN_NAME="${STACK_ADMIN_NAME:-E2E Admin}"
OWUI_PASSWORD="e2e-$(openssl rand -hex 12)"
GATEWAY_TOKEN="e2e-$(openssl rand -hex 16)"
FAKE_LLM_PORT=18999
if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
  echo "::add-mask::$OWUI_PASSWORD"
  echo "::add-mask::$GATEWAY_TOKEN"
fi

wait_for() {  # wait_for <description> <seconds> <command...>
  local what=$1 timeout=$2; shift 2
  local start=$SECONDS
  until "$@" >/dev/null 2>&1; do
    if (( SECONDS - start > timeout )); then
      echo "::error::timed out after ${timeout}s waiting for $what"
      return 1
    fi
    sleep 2
  done
  echo "$what is up after $(( SECONDS - start ))s"
}

echo "::group::Fake model server"
FAKE_LLM_LOG="$LOG_DIR/fake-llm.jsonl"
FAKE_LLM_LOG="$FAKE_LLM_LOG" FAKE_LLM_PORT=$FAKE_LLM_PORT \
  nohup python3 e2e/fake_llm.py >"$LOG_DIR/fake-llm.log" 2>&1 &
wait_for "fake model" 30 curl -fsS "http://127.0.0.1:$FAKE_LLM_PORT/health"
echo "::endgroup::"

echo "::group::OpenClaw Gateway (openclaw@$OPENCLAW_VERSION)"
npm install --global --no-audit --no-fund "openclaw@$OPENCLAW_VERSION" >"$LOG_DIR/npm-install.log" 2>&1 \
  || { cat "$LOG_DIR/npm-install.log"; exit 1; }
openclaw --version
mkdir -p "$HOME/.openclaw"
cat >"$HOME/.openclaw/openclaw.json" <<EOF
{
  "gateway": {
    "mode": "local", "port": 18789, "bind": "loopback",
    "auth": {"mode": "token", "token": "$GATEWAY_TOKEN"}
  },
  "models": {"mode": "merge", "providers": {"fake": {
    "baseUrl": "http://127.0.0.1:$FAKE_LLM_PORT/v1",
    "api": "openai-completions",
    "models": [{"id": "e2e-default", "name": "E2E Default"},
               {"id": "e2e-echo", "name": "E2E Echo"}]
  }}},
  "agents": {
    "defaults": {
      "model": {"primary": "fake/e2e-default"},
      "heartbeat": {"agentId": "main"},
      "systemAgent": {"agentId": "main"}
    },
    "ownership": "explicit",
    "entries": {"main": {"workspace": "$HOME/.openclaw/workspace"}, "title-gen": {}}
  }
}
EOF
if [[ -n "${STACK_CONFIG_PATCH:-}" ]]; then
  openclaw config patch --file "$STACK_CONFIG_PATCH"
fi
openclaw config validate
nohup openclaw gateway run >"$LOG_DIR/gateway.log" 2>&1 &
# The crash test kills this pid (and its children) and nothing else, so it can
# never hit a Gateway the stack did not start.
GATEWAY_PID_FILE="$LOG_DIR/gateway.pid"
echo $! >"$GATEWAY_PID_FILE"
wait_for "OpenClaw Gateway" 120 grep -q "\[gateway\] ready" "$LOG_DIR/gateway.log" \
  || { tail -50 "$LOG_DIR/gateway.log"; exit 1; }
echo "::endgroup::"

echo "::group::Open WebUI ($OWUI_IMAGE)"
WAIT_START=$SECONDS
wait "$PULL_PID" || { cat "$LOG_DIR/docker-pull.log"; echo "::error::docker pull $OWUI_IMAGE failed"; exit 1; }
echo "image pulled after $(( SECONDS - PULL_START ))s, $(( SECONDS - WAIT_START ))s of it waited for here"
# Host networking so "localhost:18789" means the same Gateway from inside the
# container as from the installer, which is the single-host layout the README's
# defaults assume.
docker run -d --name owui --network host \
  -e WEBUI_SECRET_KEY="e2e-$(openssl rand -hex 16)" \
  -e ENABLE_OLLAMA_API=false \
  -e ENABLE_OPENAI_API=false \
  -e OFFLINE_MODE=true \
  "$OWUI_IMAGE" >/dev/null
wait_for "Open WebUI" 300 curl -fsS "$OWUI_URL/health" \
  || { docker logs --tail 100 owui; exit 1; }
curl -fsS "$OWUI_URL/api/version"; echo

# The first account to sign up becomes the admin.
curl -fsS -X POST "$OWUI_URL/api/v1/auths/signup" -H 'Content-Type: application/json' \
  -d "{\"name\":\"$OWUI_ADMIN_NAME\",\"email\":\"$OWUI_EMAIL\",\"password\":\"$OWUI_PASSWORD\"}" >/dev/null
echo "admin account created"
echo "::endgroup::"

export OWUI_URL OWUI_EMAIL OWUI_PASSWORD GATEWAY_TOKEN GATEWAY_URL="localhost:18789"

echo "::group::Install the Pipe (pairs the device, runs the installer smoke test)"
uv run install.py install
echo "::endgroup::"

echo "::group::Install the Thinking Filter"
uv run install_filter.py install
echo "::endgroup::"

echo "::group::Install the Status Action"
uv run install_action.py install
echo "::endgroup::"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    echo "OWUI_URL=$OWUI_URL"
    echo "OWUI_EMAIL=$OWUI_EMAIL"
    echo "OWUI_PASSWORD=$OWUI_PASSWORD"
    echo "E2E_STACK=1"
    echo "E2E_GATEWAY_PID_FILE=$GATEWAY_PID_FILE"
    echo "E2E_FAKE_LLM_LOG=$FAKE_LLM_LOG"
    echo "GATEWAY_TOKEN=$GATEWAY_TOKEN"
    echo "GATEWAY_URL=$GATEWAY_URL"
  } >>"$GITHUB_ENV"
else
  echo "export OWUI_URL=$OWUI_URL OWUI_EMAIL=$OWUI_EMAIL OWUI_PASSWORD=$OWUI_PASSWORD E2E_STACK=1 E2E_GATEWAY_PID_FILE=$GATEWAY_PID_FILE E2E_FAKE_LLM_LOG=$FAKE_LLM_LOG GATEWAY_TOKEN=$GATEWAY_TOKEN GATEWAY_URL=$GATEWAY_URL"
fi
echo "stack ready"
