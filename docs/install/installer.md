`install.py` installs, updates, repairs and health-checks the Pipe through Open WebUI's REST API, so it needs your Open WebUI admin email and password. If you'd rather not give a script those, use the [manual install](./manual.md): same result.

Applies to: Pipe

Valves: [`GATEWAY_URL`](../configuration.md#gateway_url), [`GATEWAY_TOKEN`](../configuration.md#gateway_token), [`AGENT_ID`](../configuration.md#agent_id), [`DEVICE_IDENTITY`](../configuration.md#device_identity), [`STATE_DIR`](../configuration.md#state_dir), [`OWUI_BASE_URL`](../configuration.md#owui_base_url), [`OWUI_API_KEY`](../configuration.md#owui_api_key), [`FILE_SERVER_BASE_URL`](../configuration.md#file_server_base_url), [`ENABLE_FILE_SERVER`](../configuration.md#enable_file_server), [`USE_OWUI_FILES`](../configuration.md#use_owui_files), [`SEND_STOP_ON_CANCEL`](../configuration.md#send_stop_on_cancel)

## Prerequisites

- A running OpenClaw Gateway, reachable from the machine Open WebUI runs on.
- Admin credentials for your Open WebUI instance.
- Your OpenClaw Gateway API token. This is the value of `gateway.auth.token` in your OpenClaw config; ask whoever runs your Gateway if you don't have it.
- [`uv`](https://docs.astral.sh/uv/) on the **machine running the installer** (not inside Open WebUI). The installer needs Python 3.10 or newer, and only talks to Open WebUI: it never connects to the Gateway itself.

If you don't have `uv`, on the machine that will run the installer:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

No `uv`? In the repo checkout, this works identically; `uv` just removes the manual step:

```bash
pip install cryptography click rich
python3 install.py install
```

A bare `python3 install.py` with no subcommand only prints help.

## Run the installer

The wizard prompts for everything it needs and says where to find the Gateway token. In the repo checkout:

```bash
uv run install.py install --wizard
```

A real run, the Pipe part:

```text
──────────────────────────────────── Login ─────────────────────────────────────
  ✓ Logged into OWUI as Sam
─────────────────────────────────── Function ───────────────────────────────────
  ✓ Created pipe function
  ✓ Activated pipe function
  ✓ Made pipe function global
──────────────────────────────────── Valves ────────────────────────────────────
  ! Generated new device identity: 133c1b884175ef2906a0...
  ! This new identity must be approved once by OpenClaw.
  ✓ Updated valves: AGENT_ID, DEVICE_IDENTITY, ENABLE_FILE_SERVER,
GATEWAY_TOKEN, GATEWAY_URL, OWUI_BASE_URL, SEND_STOP_ON_CANCEL, STATE_DIR,
USE_OWUI_FILES
─────────────────────────────── Model discovery ────────────────────────────────
  ✓ Model openclaw_gateway.default and at least one dynamic model are visible
────────────────────────────────── Smoke test ──────────────────────────────────
  ✓ Smoke test passed
─────────────────────────────── Route smoke test ───────────────────────────────
  ✓ Route smoke test passed (anthropic/claude-haiku-5-5)
```

`uv run` resolves `cryptography`, `click` and `rich` into an ephemeral environment, so there is no venv or `pip install` step. `-w` is short for `--wizard`.

The wizard asks for the Open WebUI base URL, admin email and password, the Gateway address (`host:port`), the Gateway token and the agent id. It only prompts for the email, password and token when they are not already set.

## Run it without cloning

On any machine with `uv`:

```bash
uv run https://raw.githubusercontent.com/Eliav2/openclaw-openwebui-integration/main/install.py install --wizard
```

In that form there is no `openclaw_pipe.py` next to the script, so the installer downloads the artifact from this repo before deploying it. Point `OPENCLAW_PIPE_ARTIFACT_URL` at a fork, a pinned tag, or an internal mirror to change that. If the download fails, or returns something that is not the Pipe, the installer stops and tells you to run it from a clone.

The download only happens when there is no repo at all next to the script. A partial checkout with the artifact missing fails loudly instead of deploying `main` from the internet.

What the no-clone form can't do: `skills` (it needs the `skills/` folder), `install_action.py` and `install_filter.py` (they import `install.py`). For those, use a clone.

## Configuration

Only three values are required. Everything else has a working default.

| Required | Flag | |
|---|---|---|
| `OWUI_EMAIL` | `--owui-email` | Open WebUI admin email |
| `OWUI_PASSWORD` | `--owui-password` | Open WebUI admin password |
| `GATEWAY_TOKEN` | `--gateway-token` | `gateway.auth.token` from your OpenClaw config |

| Optional | Flag | Default |
|---|---|---|
| `OWUI_URL` | `--owui-url` | `http://localhost:8080` |
| `GATEWAY_URL` | `--gateway-url` | `localhost:18789` |
| `AGENT_ID` | `--agent-id` | `main` |
| `OPENCLAW_BRIDGE_STATE_DIR` | `--state-dir` | `/data/openclaw-bridge` (a directory inside the Open WebUI container; sets the `STATE_DIR` valve) |
| `OWUI_API_BASE_URL` | `--owui-api-base-url` | whatever `OWUI_URL` is. Set it only when the Pipe must reach Open WebUI's API at a different address than you do (sets the `OWUI_BASE_URL` valve) |
| `OWUI_API_KEY` | `--owui-api-key` | empty. Only needed if the request bearer token can't be used for media uploads |
| `FILE_SERVER_BASE_URL` | `--file-server-base-url` | empty. Only needed if you rely on the legacy media fallback |

Flags with no env var:

| Flag | Default | What it does |
|---|---|---|
| `--wizard`, `-w` | off | Prompt interactively for any missing required values |
| `--auto-approve` / `--no-auto-approve` | on | Approve a matching pending OpenClaw device when a smoke test reports "pairing required". Only works where the `openclaw` CLI is installed |

Supply the values as env vars or as flags. `uv run install.py install --help` lists every flag. Skip the wizard once they are set, in the repo checkout:

```bash
export OWUI_EMAIL=admin@example.com
export OWUI_PASSWORD=your-password
export GATEWAY_TOKEN=your-gateway-token

uv run install.py install
```

That non-interactive form is what you want for CI, config management, or reinstalling after an Open WebUI upgrade.

## Python packages inside Open WebUI

The Pipe and the Status Action need `websockets` and `cryptography` inside Open WebUI; `pydantic` already ships with it. Both functions declare the two packages in the `requirements:` frontmatter field, so Open WebUI pip-installs them itself the first time the function loads. The thinking filter needs neither: it imports only the standard library and pydantic.

If your deployment sets `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=false` or runs with `OFFLINE_MODE`, install `websockets` and `cryptography` into the Open WebUI image yourself. Otherwise the function fails to load with `ModuleNotFoundError: websockets`.

Supported Open WebUI and OpenClaw versions: [Versions](../versions.md).

## What `install` does

1. Log in to Open WebUI.
2. Get the Pipe's code. In a clone it rebuilds `openclaw_pipe.py` from `src/` first, so a stale artifact is never deployed; with no clone it downloads it (see above).
3. Check whether the Pipe function (`openclaw_gateway`) already exists.
4. **If it exists:** back up the function and its valves to `backups/`, then update the code in place.
5. **If it's new:** create the function.
6. Make sure it is active and global, switching on only what is off. It never blindly toggles it off first.
7. Set the valves. It restores any valve Open WebUI dropped during the update, and keeps every valve it does not manage (see [Valves the installer sets](#valves-the-installer-sets)).
8. Pick the device identity (an Ed25519 key pair): the existing `DEVICE_IDENTITY` valve first, then `./.pipe_device_identity.json`, and only if neither exists, a new one. It is written to both.
9. Check that **OpenClaw · Default** and at least one discovered model are visible in Open WebUI's model list, and stop if not.
10. Run an end-to-end smoke test through `/api/chat/completions`: it asks the agent to reply with a fixed word. If the reply says **pairing required** and the local `openclaw` CLI is available, it approves the matching pending pairing request and tries again.
11. Run a route smoke test: it picks one discovered (non-default) model and asks the agent which model it is running on, to prove that picking a model in Open WebUI reaches the agent.

The command exits 0 only when both smoke tests pass. The smoke tests are two real messages to your agent, in throwaway chats.

Step 4 is why re-running this is safe: the function is never deleted and recreated, so `DEVICE_IDENTITY` survives and you never re-approve the device.

## Valves the installer sets

Every run of `install` or `repair` writes these valves from your env vars, flags and defaults, even when you changed them in the admin panel since:

- `GATEWAY_URL`, `GATEWAY_TOKEN`, `AGENT_ID`, `STATE_DIR`, `OWUI_BASE_URL`
- `ENABLE_FILE_SERVER`, `USE_OWUI_FILES` and `SEND_STOP_ON_CANCEL`, all set to `True`
- `OWUI_API_KEY` and `FILE_SERVER_BASE_URL`, only when you pass them

So pass the same values on every run. A re-run without `GATEWAY_URL` sets it back to `localhost:18789`; one without `--state-dir` sets `STATE_DIR` back to `/data/openclaw-bridge`. If you turned `ENABLE_FILE_SERVER` off, turn it off again after an update.

`DEVICE_IDENTITY` and every other valve (`DEFAULT_MODEL`, `MAX_MODELS`, `AUTO_TITLE` and the rest) keep their current values. All valves: [Configuration](../configuration.md).

## Commands

| Command | What it does |
|---|---|
| `install` | Create or update the Pipe function and its valves, then smoke test |
| `repair` | Same as `install`. Use after a broken or partial setup |
| `status` | Print current state as JSON, change nothing. Exits 0 only when every field is good |
| `healthcheck` | `status` plus both smoke tests |
| `skills` | Install the agent-side skills. Run it on the **Gateway host**; it needs no Open WebUI credentials. `--force` overwrites an existing skill. See [agent skills](./agent-skills.md) |

`status` reports `function_exists`, `is_active`, `is_global`, `model_visible`, `has_gateway_url`, `has_gateway_token`, `has_device_identity`, `device_id` and `device_paired`. `device_paired` asks the local `openclaw` CLI, so it is only ever `true` on the Gateway host. On any other machine `status` and `healthcheck` exit 1 even when everything works; read the smoke test lines instead.

`install` and `repair` also accept `--with-skills` (install the agent-side skills after the Pipe) and `--force-skills` (overwrite an existing skill). On the current version the installer exits before it reaches the skills step, so run `uv run install.py skills` on its own instead.

## The companion installers

- `install_action.py` installs the Status Action and mirrors the five shared valves from the Pipe: [Status button](./status-action.md).
- `install_filter.py` installs the thinking filter: [thinking control](./thinking-filter.md).

Both take only `OWUI_URL`, `OWUI_EMAIL` and `OWUI_PASSWORD`, have `install`, `repair` and `status` but no `healthcheck`, and must run from a clone.

## After the install

- The Gateway holds a new device until you approve it: [device pairing](./pairing.md).
- Updating, rolling back and uninstalling: [update and uninstall](./update-uninstall.md).
