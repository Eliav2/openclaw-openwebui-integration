# Development

Editing the source fragments, rebuilding the artifacts, and running the tests.

## Building the artifacts

`openclaw_pipe.py` and `openclaw_status_action.py` are **built artifacts**.
Don't hand-edit them. Edit the fragments under `src/openclaw_pipe_pkg/`, then
rebuild:

```bash
python3 build.py --all        # rebuild BOTH artifacts (DEV-ONLY blocks stripped)
python3 build.py --check-all  # verify both committed artifacts match src/ (CI drift guard)

python3 build.py              # rebuild openclaw_pipe.py only
python3 build.py --action     # rebuild openclaw_status_action.py only
python3 build.py --check      # drift guard, Pipe only
python3 build.py --check-action  # drift guard, Action only
```

Each single-artifact invocation builds exactly one file, and several fragments
feed **both** artifacts, so prefer `--all` / `--check-all`. Using the
Pipe-only `--check` after editing `action.py` reports "no drift" while the
committed Action artifact is stale.

Why a hand-written concatenation instead of a bundler: OWUI reads a function
as a single flat `.py` file, using the frontmatter docstring for metadata and
introspecting a top-level `Pipe`/`Action` class directly, most bundlers
break one or both of those.

### Docs

| Doc | What it covers |
|-----|----------------|
| [`docs/rehydration-persistence.md`](./rehydration-persistence.md) | How Open WebUI persists an assistant message, why `output` wins over `content`, and why the integration emits mid-run snapshots so a client reconnecting mid-turn isn't left staring at an empty message. Read this before changing anything about snapshots or streaming. |
| [`docs/ask-user-modal.md`](./ask-user-modal.md) | Design spec for a tool-call-based ask-user flow. **Not implemented as written**: see the banner at the top; the shipped approach intercepts `OpenClaw needs input:` text. |

`scripts/verify_rehydration.py` is a live verifier for the behavior described in
the first doc. It needs the integration deployed and a real streaming turn, it polls a
chat until the turn is done and reports whether mid-run snapshots landed and
whether the final message duplicated itself.

### Running the tests

```bash
uv run --with pytest --with pydantic --with websockets --with cryptography \
  --with click --with rich pytest -q
```

This is green on a fresh clone with no setup.

Most of the suite (`test_pipe_unit.py`, `test_status_action_unit.py`,
`test_build.py`, `test_devcoord_unit.py`, `test_install_unit.py`) is pure unit
tests with no external dependencies. `test_fixture_owui_streaming_handler.py`
is not a suite, it's a verbatim copy of Open WebUI's streaming handler, used
as a fixture to pin tool-card rendering against the real thing.

`test_pipe.py` and `scripts/test_auto_title.py` are **integration tests**
against a live OWUI. They **skip** unless `OWUI_EMAIL` and `OWUI_PASSWORD` are
set, and skip with a diagnostic if login fails or the integration isn't registered. To
actually run them:

```bash
export OWUI_URL=http://your-owui-host:8080
export OWUI_EMAIL=admin@example.com
export OWUI_PASSWORD=your-password
uv run --with pytest --with pydantic --with websockets --with cryptography \
  --with click --with rich pytest -q test_pipe.py
```

CI (`.github/workflows/ci.yml`) runs the drift guard for both artifacts, the
unit suite, and a load check that execs each artifact the way OWUI's plugin
loader does, on Python 3.10 to 3.13.

---

## Driving the integration over the API

You can send messages through the integration as if using the OWUI frontend by calling
the internal `/api/chat/completions` endpoint:

```bash
# 1. Sign in to get a token
TOKEN=$(curl -s -X POST http://localhost:8080/api/v1/auths/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"***"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 2. Send a message through the integration
curl -s -X POST http://localhost:8080/api/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openclaw_gateway.default",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": false
  }'
```

This triggers the full pipeline (pipe function, Gateway, agent, response)
and the conversation is saved to OWUI chat history automatically.
