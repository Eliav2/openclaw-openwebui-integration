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
# `bash e2e/start_stack.sh down` stops what a start left running (fake model,
# Gateway and its children, the image pull, the Open WebUI container), using
# the pid files in E2E_LOG_DIR. A start that fails halfway tears itself down
# the same way (set E2E_KEEP_ON_FAIL=1 to keep it for a look); in GitHub Actions
# it does not, because the next step reads `docker logs owui` after a failure.
#
# Inputs (env): OWUI_IMAGE, OPENCLAW_VERSION (npm spec, e.g. 2026.9.3 or latest;
# replaced by the installed version once npm has it),
# E2E_LOG_DIR. Writes the credentials the test suite needs to $GITHUB_ENV when
# running under Actions.
#
# Optional, for the screenshots workflow (scripts/screenshots): STACK_ADMIN_NAME
# and STACK_ADMIN_EMAIL for the admin account, and STACK_CONFIG_PATCH, a JSON5
# patch applied to the Gateway config before it starts (other model names, an
# extra plugin). FAKE_LLM_* variables reach the fake model as they are.
set -euo pipefail

cd "$(dirname "$0")/.."

LOG_DIR="${E2E_LOG_DIR:-$PWD/e2e-logs}"
# One pid file per process the stack starts. gateway.pid is also read (and
# rewritten after a relaunch) by the crash tests, so it holds the bare pid.
STACK_PID_FILES=("$LOG_DIR/fake-llm.pid" "$LOG_DIR/gateway.pid" "$LOG_DIR/docker-pull.pid")

tree_pids() {  # tree_pids <pid>: the pid and all its descendants
  local k
  echo "$1"
  for k in $(pgrep -P "$1" 2>/dev/null || true); do tree_pids "$k"; done
}

proc_stat() {  # proc_stat <pid>: "<state> <start ticks>" of a process, nothing if it is gone
  sed 's/^.*) //' "/proc/$1/stat" 2>/dev/null | awk '{print $1, $20}'
}

started_at() {  # started_at <pid>: epoch second the process started, nothing if it is gone
  local st
  st=$(proc_stat "$1")
  [[ -n "$st" ]] || return 0
  awk -v t="${st#* }" -v hz="$(getconf CLK_TCK)" '/^btime/ {print int($2 + t / hz)}' /proc/stat
}

running_as() {  # running_as <pid> <start ticks>: still that process, and not a zombie waiting for its parent
  local st
  st=$(proc_stat "$1")
  [[ -n "$st" && "${st%% *}" != Z && "${st#* }" == "$2" ]]
}

stack_down() {
  local f pid started st pids=() p left id
  declare -A ticks
  command -v pgrep >/dev/null || { echo "::error::pgrep (procps) is needed to find the stack's child processes" >&2; exit 1; }
  for f in "${STACK_PID_FILES[@]}"; do
    [[ -s "$f" ]] || continue
    pid=$(<"$f")
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    # A pid file older than the process means the pid was reused: not ours.
    started=$(started_at "$pid")
    if [[ -n "$started" ]] && (( started <= $(stat -c %Y "$f") + 2 )); then
      mapfile -t -O "${#pids[@]}" pids < <(tree_pids "$pid")
    fi
  done
  if (( ${#pids[@]} )); then
    # Remember each process's start time: a pid the kernel reuses during the wait is not ours to kill.
    for p in "${pids[@]}"; do st=$(proc_stat "$p"); ticks[$p]="${st#* }"; done
    kill -TERM "${pids[@]}" 2>/dev/null || true
    for _ in $(seq 1 10); do
      left=()
      for p in "${pids[@]}"; do running_as "$p" "${ticks[$p]}" && left+=("$p"); done
      (( ${#left[@]} )) || break
      sleep 1
    done
    # Check identity again right before the KILL: the pid could have been reused since the last look.
    for p in "${left[@]}"; do running_as "$p" "${ticks[$p]}" && kill -KILL "$p" 2>/dev/null; done || true
  fi
  # Only the container this start created (its id is recorded at `docker run`), never an `owui` that was there before.
  if [[ -s "$LOG_DIR/owui.id" ]]; then
    id=$(<"$LOG_DIR/owui.id")
    docker rm -f "$id" >/dev/null 2>&1 || true
  fi
  rm -f "${STACK_PID_FILES[@]}" "$LOG_DIR/owui.id"
  echo "stack down (${#pids[@]} processes stopped)"
}

if [[ "${1:-}" == down ]]; then
  stack_down
  exit 0
fi

: "${OWUI_IMAGE:?set OWUI_IMAGE}"
: "${OPENCLAW_VERSION:?set OPENCLAW_VERSION}"
mkdir -p "$LOG_DIR"

STACK_UP=0
on_exit() {
  local rc=$?
  trap - EXIT
  if (( rc != 0 && STACK_UP == 0 )) && [[ -z "${E2E_KEEP_ON_FAIL:-}" && -z "${GITHUB_ACTIONS:-}" ]]; then
    echo "::warning::the stack did not come up (exit $rc); stopping what it started"
    # A signal can land between starting a process and writing its pid file: stop the script's own children too.
    pkill -TERM -P $$ 2>/dev/null || true
    stack_down
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# The Open WebUI image is the stack's largest download. It is pulled while the
# fake model, the npm install and the Gateway come up; `docker run` waits for it.
PULL_START=$SECONDS
docker pull --quiet "$OWUI_IMAGE" >"$LOG_DIR/docker-pull.log" 2>&1 &
PULL_PID=$!
echo "$PULL_PID" >"$LOG_DIR/docker-pull.pid"

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
echo $! >"$LOG_DIR/fake-llm.pid"
wait_for "fake model" 30 curl -fsS "http://127.0.0.1:$FAKE_LLM_PORT/health"
echo "::endgroup::"

echo "::group::OpenClaw Gateway (openclaw@$OPENCLAW_VERSION)"
npm install --global --no-audit --no-fund "openclaw@$OPENCLAW_VERSION" >"$LOG_DIR/npm-install.log" 2>&1 \
  || { cat "$LOG_DIR/npm-install.log"; exit 1; }
openclaw --version
# The Gateway reports OPENCLAW_VERSION as its own version when it is set, ahead
# of its package version, and the Pipe's version gates read that. An npm spec
# (`latest`) there makes them all take their oldest path (ELI-225), so from
# here on it is the version npm installed, for this Gateway and the test steps.
OPENCLAW_VERSION=$(openclaw --version | grep -oE '[0-9]{4}\.[0-9]{1,2}\.[0-9]+(-[0-9A-Za-z.]+)?' | head -1)
: "${OPENCLAW_VERSION:?openclaw --version printed no version}"
export OPENCLAW_VERSION
mkdir -p "$HOME/.openclaw"
# e2e-think is the one reasoning model: the Gateway accepts a thinking level for
# it (off to high on 2026.9) and sends it on as `reasoning_effort`, which the
# fake model's request log shows. The other two take no level at all.
#
# e2e/agent-tool adds an `Agent` tool that answers like Claude Code's.
#
# No heartbeat, dreaming or weekly skill review: a job the Gateway schedules
# itself can come due mid-suite and hold a clean stop's drain for minutes
# (test_the_stack_gateway_schedules_no_work_of_its_own).
cat >"$HOME/.openclaw/openclaw.json" <<EOF
{
  "gateway": {
    "mode": "local", "port": 18789, "bind": "loopback",
    "auth": {"mode": "token", "token": "$GATEWAY_TOKEN"}
  },
  "models": {"mode": "merge", "providers": {"fake": {
    "baseUrl": "http://127.0.0.1:$FAKE_LLM_PORT/v1",
    "api": "openai-completions",
    "models": [{"id": "e2e-default", "name": "E2E Default", "contextWindow": 200000, "input": ["text", "image"]},
               {"id": "e2e-echo", "name": "E2E Echo", "contextWindow": 200000, "input": ["text", "image"]},
               {"id": "e2e-think", "name": "E2E Think", "contextWindow": 200000,
                "reasoning": true, "compat": {"supportsReasoningEffort": true}}]
  }}},
  "agents": {
    "defaults": {
      "model": {"primary": "fake/e2e-default"},
      "heartbeat": {"agentId": "main", "every": "0m"},
      "systemAgent": {"agentId": "main"}
    },
    "ownership": "explicit",
    "entries": {"main": {"workspace": "$HOME/.openclaw/workspace"}, "title-gen": {}}
  },
  "skills": {"workshop": {"autonomous": {"mode": "off"}}},
  "plugins": {"load": {"paths": ["$PWD/e2e/agent-tool"]},
              "entries": {"memory-core": {"config": {"dreaming": {"enabled": false}}},
                          "e2e-agent-tool": {"enabled": true}}}
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
  -e ENABLE_CHANNELS=true \
  "$OWUI_IMAGE" >"$LOG_DIR/owui.id"
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
    echo "OPENCLAW_VERSION=$OPENCLAW_VERSION"  # the installed version, for a Gateway a test restarts
  } >>"$GITHUB_ENV"
else
  echo "export OWUI_URL=$OWUI_URL OWUI_EMAIL=$OWUI_EMAIL OWUI_PASSWORD=$OWUI_PASSWORD E2E_STACK=1 E2E_GATEWAY_PID_FILE=$GATEWAY_PID_FILE E2E_FAKE_LLM_LOG=$FAKE_LLM_LOG GATEWAY_TOKEN=$GATEWAY_TOKEN GATEWAY_URL=$GATEWAY_URL OPENCLAW_VERSION=$OPENCLAW_VERSION"
fi
STACK_UP=1
echo "stack ready"
