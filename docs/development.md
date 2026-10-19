# Development

Editing the source fragments, rebuilding the artifacts, and running the tests.

## Building the artifacts

`openclaw_pipe.py`, `openclaw_status_action.py` and
`openclaw_thinking_filter.py` are **built artifacts**, and so is
`docs/configuration.md`. Don't hand-edit them. Edit the fragments under `src/openclaw_pipe_pkg/`, then
rebuild:

```bash
python3 build.py --all        # rebuild every artifact and docs/configuration.md
python3 build.py --check-all  # verify every committed artifact matches src/ (CI drift guard)

python3 build.py              # rebuild openclaw_pipe.py only
python3 build.py --action     # rebuild openclaw_status_action.py only
python3 build.py --check      # drift guard, Pipe only
python3 build.py --check-action  # drift guard, Action only
python3 build.py --filter     # rebuild openclaw_thinking_filter.py only
python3 build.py --check-filter  # drift guard, filter only
```

Each single-artifact invocation builds exactly one file, and several fragments
feed **both** artifacts, so prefer `--all` / `--check-all`. Using the
Pipe-only `--check` after editing `action.py` reports "no drift" while the
committed Action artifact is stale.

Why a hand-written concatenation instead of a bundler: OWUI reads a function
as a single flat `.py` file, using the frontmatter docstring for metadata and
introspecting a top-level `Pipe`/`Action` class directly, most bundlers
break one or both of those.

### When your change breaks streaming

| Symptom | Likely cause |
|---|---|
| Text appears all at once instead of streaming | A code path in the Pipe uses `return` instead of `yield`. The Pipe is an async generator: Open WebUI streams each chunk only as it is yielded (see [Architecture](./architecture.md#how-it-works)). Check your code. |

User-facing problems are in [Troubleshooting](./troubleshooting.md).

### Docs

| Doc | What it covers |
|-----|----------------|
| [`docs/rehydration-persistence.md`](./rehydration-persistence.md) | How Open WebUI 0.12 keeps an assistant message mid-run, so a client reconnecting mid-turn sees the answer so far, and why a done:false message cannot be written. Read this before changing anything about streaming or persistence. |
| [`docs/design/ask-user-modal.md`](./design/ask-user-modal.md) | Design spec for a tool-call-based ask-user flow. **Not implemented as written**: see the banner at the top; the shipped approach intercepts `OpenClaw needs input:` text. |

`scripts/migrate_stale_chat_models.py` repoints chats whose saved model the
Gateway no longer offers. Open WebUI drops an unlisted model id before the
Pipe runs (the browser swaps in the default model, the API answers "Model not
found"), so the Pipe cannot map old ids itself; the stored chat has to change.
It picks each successor from the live model list (same model under another
provider, else the newest model of the same Claude family, else `default`),
is a dry run unless given `--apply`, writes a backup before changing anything,
and `--restore <backup>` undoes it. Re-run it after the Gateway drops models.

### Running the tests

```bash
uv run --with-requirements requirements-dev.txt pytest -q
```

This is green on a fresh clone with no setup. `requirements-dev.txt` pins
pydantic, cryptography and websockets to the versions Open WebUI itself locks,
because the artifacts run inside Open WebUI's interpreter.

Most of the suite (`tests/test_build.py` and every `tests/test_*_unit.py`) is pure unit
tests with no external dependencies. What the suites share lives in two modules pytest
does not collect: `tests/_test_stubs.py` (stand-ins for websockets, cryptography and
pydantic) and `tests/_test_helpers.py` (`run_page` for the fake-DOM page scripts,
`RecordingEmitter`, `action_gateway`, `fake_owui`/`owui_modules`, `FakeSio`). `tests/fixtures/test_fixture_owui_streaming_handler.py`
is not a suite, it's a verbatim copy of Open WebUI's streaming handler, used
as a fixture to pin tool-card rendering against the real thing.

`e2e/test_e2e.py` and `e2e/test_browser_flows.py` skip unless `E2E_STACK=1`: it needs the stack that
`e2e/start_stack.sh` brings up (see E2E below). Two fixture checks in
`tests/test_pipe_unit.py` (`-k installed_dist`) compare the Gateway source lines the
Pipe matches on with an installed OpenClaw: `$OPENCLAW_DIST`, else the global
npm install's. They skip when neither exists; the extractors that rewrite those
fixtures (`scripts/extract_openclaw_*.py --write`) default to the same dist.

### CI

Every push and PR runs `.github/workflows/ci.yml`:

| Job | What it guards |
|-----|----------------|
| Lint | `ruff` with correctness rules only (`ruff.toml`), run on the **built artifacts** rather than the fragments, so an undefined name is caught where Open WebUI would hit it. Also `shellcheck`, `actionlint`, and a parse check of the Gateway plugin. |
| Unit (py3.10 to 3.13) | Drift guard for every artifact, the unit suite (coverage reported on 3.11), and a load check that execs each artifact the way Open WebUI's plugin loader does. |
| OWUI handler fixture is current | `scripts/check_owui_fixture.py` compares the vendored `handle_responses_streaming_event` with Open WebUI's source at the pinned version. A stale copy is how the tool cards once vanished while the suite stayed green. `scripts/check_owui_frontend_fixture.py` does the same for the chat tab's live reducer (`applyResponseStreamEvent`, run with bun by the live-order tests). |
| Secret scan | gitleaks over the full history. |
| E2E | The whole stack, real everywhere except the model (below). |

**E2E** (`.github/workflows/e2e.yml`, `e2e/`): starts a deterministic fake
OpenAI-compatible model (`e2e/fake_llm.py`), a real OpenClaw Gateway pointed at
it (with one test-only plugin, `e2e/agent-tool`: an `Agent` tool that answers
like Claude Code's, which the Gateway's own runtime does not have), and Open
WebUI in Docker; installs the Pipe, Filter and Action with
`uv run install*.py install` exactly as the README tells users to (device
pairing and the installer's smoke test included); then runs `e2e/test_e2e.py`
against it, and the installed-dist fixture checks against the Gateway it
installed. `e2e/test_browser_flows.py` then drives the same stack in Chromium
with Playwright, one test per user flow, asserting on what the page shows (the
browser helpers are `scripts/screenshots/capture.py`'s). Because the model is scripted, replies are
asserted exactly, the model that served a turn is read from the fake model's
request log (the stack's default, `e2e-default`, differs from the routed
`e2e-echo`), and a real tool call (`session_status`) is checked all the
way into the chat Open WebUI persisted, as a `function_call` with its matching
`function_call_output`. A third model, `e2e-think`, is the only reasoning one,
so the thinking level a turn picks shows in that request log as
`reasoning_effort`. Gateway, Open WebUI and model logs are uploaded as an
artifact on every run.

If Open WebUI logs `database is locked` during an E2E run, `e2e/conftest.py`
interrupts the running test and ends the run (exit code 3), naming that test
and the log line: a lock that is never released (ELI-205) would otherwise
fail every later test at its timeout.

The versions it runs against are pinned in `e2e/versions.env`. Bump them in the
PR that makes the code work with a new release, together with the matching
pins in `requirements-dev.txt` and a re-extracted fixture
(`python3 scripts/check_owui_fixture.py --ref <tag> --print-upstream`).

**Compat watch** (`.github/workflows/compat-watch.yml`, daily): the same E2E
stack against `open-webui:latest` and `openclaw@latest`, and the fixture
against upstream's latest release and `main`. A scheduled failure opens or
updates one tracking issue, so a breaking upstream release is visible before
the pins are bumped.

**Screenshots and Demos** (`.github/workflows/screenshots.yml` and
`demos.yml`, by hand): the docs' images, from the E2E stack with the scripted
model serving real-looking scenes (`scripts/screenshots/scenes.json`; the
demos add `scripts/demos/demos.json`). `capture.py` takes the stills;
`scripts/demos/record.py` records each demo in `demos.json` with the DevTools
screencast at 2x, a drawn cursor, a ripple on each click and a badge for each
key, then encodes it with ffmpeg into a light GIF, 960 px wide, at most 15 s and
1 MB. Both fail when a page shows deny-listed text (words that give away a test
stack). A demo waits on what the page shows, never on time: a reply is done when
its text is stable and "Thinking…" is gone. Before committing a GIF, look at
frames from it (`ffmpeg -i x.gif -vf "select=not(mod(n\,40))" -vsync 0 f%02d.png`).

**CodeQL** runs on Python, JavaScript and the workflows themselves.
**Dependabot** updates the GitHub Actions and the test tooling weekly.

To run the E2E stack on your own Linux machine (Docker, Node 24 and uv needed;
it installs `openclaw` globally and writes `~/.openclaw/openclaw.json`, so use a
throwaway VM or container, never a host running a real Gateway):

```bash
set -a; . e2e/versions.env; set +a
bash e2e/start_stack.sh        # last line is an `export ...` for the test env
export OWUI_URL=... OWUI_EMAIL=... OWUI_PASSWORD=... E2E_STACK=1   # paste it
uv run --with-requirements requirements-dev.txt pytest -v e2e/test_e2e.py
```

---

## The docs site

`docs/` is the source of the docs site, [eliav2.github.io/openclaw-openwebui-integration](https://eliav2.github.io/openclaw-openwebui-integration/), built with [VitePress](https://vitepress.dev). Pages stay plain Markdown that also reads on github.com:

- Every page starts with a `# Title`, then its one-line summary.
- Images are `<img src alt width>` tags, as the docs test requires. Callouts are GitHub's `> [!NOTE]` form, which VitePress renders too.
- Link to other pages by their relative `.md` path. A link to a file outside `docs/` (a function, `SECURITY.md`, `skills/`) works on GitHub as it is; the site points it at the file on GitHub.
- No Vue components or VitePress-only syntax in a page. The home page's hero and feature cards live in `docs/.vitepress/config.mts`, so `docs/index.md` stays a plain list on GitHub.
- A new page goes in the sidebar in `docs/.vitepress/config.mts`.

Run it locally with [bun](https://bun.sh):

```bash
bun install
bun run docs:dev     # live preview at http://localhost:5173/openclaw-openwebui-integration/
bun run docs:build   # what CI runs; fails on a dead internal link
```

The build also writes `llms.txt`, `llms-full.txt` and a raw `.md` next to every page, for agents. The `Docs site` workflow builds every pull request that touches the docs and deploys `main` to GitHub Pages.

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
