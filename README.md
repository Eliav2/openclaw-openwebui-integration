# OpenClaw in Open WebUI 🔌

[![CI](https://github.com/Eliav2/openclaw-openwebui-integration/actions/workflows/ci.yml/badge.svg)](https://github.com/Eliav2/openclaw-openwebui-integration/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

Run your [OpenClaw Gateway](https://github.com/openclaw/openclaw) agents inside
[Open WebUI](https://openwebui.com/), with their tool calls, subagents and
mid-run questions rendered as real UI instead of flattened into text.

A [**Pipe**](https://docs.openwebui.com/features/extensibility/plugin/functions/pipe)
function connects as an
[external app](https://docs.openclaw.ai/gateway/external-apps) speaking the
[Gateway's native v4 WebSocket protocol](https://docs.openclaw.ai/gateway/protocol),
which is what buys the real-time streaming, the native tool-call cards, and the
persistent agent sessions. A companion
[**Action**](https://docs.openwebui.com/features/extensibility/plugin/functions/action)
function adds a live session/usage button to the message toolbar. Two files,
nothing else to run, nothing added to your OpenClaw install.

<p align="center">
  <img src="./docs/img/tool-calls-streaming.jpg" alt="Tool calls streaming live in Open WebUI on a phone, with spinners and green checkmarks, including nested subagents" width="42%">
</p>
<p align="center"><sub>Tool calls stream live as the agent works, including nested subagents. On a phone.</sub></p>

> **You need a running OpenClaw Gateway before any of this is useful.**
> [OpenClaw](https://github.com/openclaw/openclaw) is a self-hosted agent
> runtime: it runs tool-using agents on your own machine and exposes them over a
> WebSocket gateway. This repo only connects an **existing** Gateway to Open
> WebUI's chat UI. It is not itself an agent. If you don't have a Gateway yet,
> set that up first.

---

## 🎯 Why this exists

I built this because I wanted OpenClaw on my phone.

OpenClaw's built-in chat UI was never somewhere I wanted to hold a long
conversation, least of all on mobile. Open WebUI is polished and mobile-first.
It feels like using ChatGPT or Claude, and I wanted that same experience for my
own agents. So I built this, used it daily for a few months, and I'm now
sharing it.

**The other difference is the connection.** The usual ways to reach OpenClaw
from outside, such as WhatsApp or Telegram, go through OpenClaw's public API,
which is deliberately narrow: text in, text out. This integration speaks the
Gateway's **native v4 WebSocket protocol** instead, the same one OpenClaw's own
clients use.

That is the entire reason the rest of this is possible. A text-in, text-out API
can carry the agent's _answer_; only the native protocol carries the agent's
_work_: every tool call as it happens, every subagent it spawns, the questions
it stops to ask you, and the files it emits. The comparison below shows what
that changes in practice; [Features](#-features) has the full list.

One thing worth calling out separately, because it is not about the protocol:
**your history lives in Open WebUI's database.** It survives Gateway restarts
and agent crashes, stays searchable and exportable, and a turn the Gateway
loses mid-flight is still there in the chat.

<p align="center">
  <img src="./docs/img/action-button.png" alt="The OpenClaw Status button in Open WebUI's message toolbar, tooltip showing" width="28%">
</p>
<p align="center"><sub>The companion Action puts this button on every message.</sub></p>

<p align="center">
  <img src="./docs/img/status-modal.jpg" alt="Status dialog showing context usage, 5h and weekly rate limits, and five running subagents" width="40%">
  &nbsp;&nbsp;
  <img src="./docs/img/model-selector.jpg" alt="Open WebUI model dropdown listing models discovered from the Gateway" width="40%">
</p>
<p align="center"><sub>Left: what it opens, fetched live from the Gateway. Right: every model your Gateway knows about, discovered automatically.</sub></p>

<p align="center">
  <img src="./docs/img/subagent-drawer.jpg" alt="Subagent drawer with Overview, Transcript and Tools tabs, showing a running subagent's transcript" width="80%">
</p>
<p align="center"><sub>Open any subagent and read its transcript while it runs.</sub></p>

<p align="center">
  <img src="./docs/img/tool-call-expanded.jpg" alt="An expanded tool call showing its INPUT arguments and OUTPUT result" width="70%">
</p>
<p align="center"><sub>Tap any tool call to see its arguments and result.</sub></p>

<details>
<summary><b>Why not just point Open WebUI at <code>/v1</code>?</b> &mdash; <sub>the official endpoint, and what it does not carry</sub></summary>

You can, and for many people that is the right answer. OpenClaw ships an
official OpenAI-compatible endpoint, and Open WebUI is a documented, CI-tested
client for it. Enable `gateway.http.endpoints.chatCompletions`, point Open WebUI
at `http://your-gateway:18789/v1`, use your Gateway token as the API key, and
pick `openclaw/default`. No install, nothing to maintain. See
[Open WebUI quick setup](https://docs.openclaw.ai/gateway/openai-http-api#open-webui-quick-setup).

The difference is what each one gives you:

|                                                                             | `/v1` endpoint         | This integration                                   |
| --------------------------------------------------------------------------- | ---------------------- | -------------------------------------------------- |
| Setup                                                                       | Change one config flag | Install two functions                              |
| Streams assistant text                                                      | Yes                    | Yes                                                |
| Client-declared OpenAI function tools                                       | Yes                    | Not applicable                                     |
| **The agent's own tool calls,** its shell commands, file edits and searches | Not surfaced           | Rendered live as native tool cards                 |
| **Subagents**                                                               | Not surfaced           | Listed live, with a transcript drawer per subagent |
| **Ask-user dialogs** mid-run                                                | No                     | Yes                                                |
| **Agent-emitted images and files**                                          | No                     | Attached natively                                  |
| **Context and rate-limit usage**                                            | No                     | On demand, live                                    |
| Model switching                                                             | One model id           | Every model your Gateway knows, per conversation   |
| Auth                                                                        | Shared bearer token    | Ed25519 device pairing                             |

Put simply: **`/v1` gives you the agent as a model. This gives you the agent as
an agent.** If you want OpenClaw in a model dropdown, use `/v1`. If you want to
watch it work, use this.

</details>

---

## ⚡ Quickstart

```bash
git clone https://github.com/Eliav2/openclaw-openwebui-integration
cd openclaw-openwebui-integration
uv run install.py install --wizard    # prompts for everything it needs
```

Then approve the device once on your Gateway host:

```bash
openclaw devices list
openclaw devices approve <request-id>
```

Now pick **`OpenClaw · Default`** in Open WebUI's model dropdown and say hello.
Full detail, including a manual path, in [Installation](#-installation). If
something goes wrong, jump to [Troubleshooting](#-troubleshooting).

---

## ✨ Features

Everything here works the moment the Pipe is installed, except the two marked
**needs agent setup**: the integration provides the mechanism, but your agent has
to know the convention. See [Teaching your agent](#-teaching-your-agent-to-use-this).

- **🔴 Real-time streaming**: assistant responses appear token-by-token, not
  all at once at the end
- **🛠️ Native tool-call rendering**: tool calls are yielded as Responses-API
  output items, so OWUI renders them as native two-phase tool cards (spinner
  while running, result on finish) instead of ad-hoc HTML
- **💬 Persistent sessions**: each OWUI conversation gets a stable OpenClaw
  session key (`agent:{AGENT_ID}:openwebui-{user_id}-{chat_id}`), so the agent
  remembers context across messages
- **🧭 Dynamic model selector**: the integration discovers every model the Gateway
  knows about (`models.list`) and lists one entry per model, with an optional
  whitelist and a size cap
- **❓ Ask-user modal** _(needs agent setup)_: when an agent emits a line beginning with
  `OpenClaw needs input:` (or `Codex needs input:`), the integration intercepts it and
  pops a real OWUI input / choice / confirmation dialog mid-run, instead of
  leaking the raw prompt into the chat as text
  (implementation: [`src/openclaw_pipe_pkg/askuser.py`](./src/openclaw_pipe_pkg/askuser.py))
- **📊 Status Action button** _(separate function)_: adds a message-toolbar
  button that fetches live session/usage data from the Gateway on demand,
  reusing the Pipe's connection when one is already open. Ships as
  `openclaw_status_action.py` and installs separately, see
  [Installing the companion Status Action](#installing-the-companion-status-action)
- **🏷️ Auto-title**: generates a chat title after the first exchange on a
  separate agent lane (`title-gen`), so it never queues behind the main
  conversation
- **🔐 Ed25519 device auth**: full WebSocket handshake with challenge/response,
  with a permanent device identity that survives restarts and reinstalls
- **🖼️ Native OWUI media support** _(needs agent setup)_: `MEDIA:` files are uploaded to the
  Open WebUI Files API and attached to the assistant message; a plain file
  server remains as a fallback
- **⚙️ Configurable**: settings live in OWUI valves; device identity/token
  also persist in a state directory so restarts do not force re-pairing

---

## 📦 Two Functions

| File                                                       | OWUI type                                                                                  | Purpose                                                                                                                              |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| [`openclaw_pipe.py`](./openclaw_pipe.py)                   | [Pipe](https://docs.openwebui.com/features/extensibility/plugin/functions/pipe) (manifold) | The chat integration. Everything above                                                                                               |
| [`openclaw_status_action.py`](./openclaw_status_action.py) | [Action](https://docs.openwebui.com/features/extensibility/plugin/functions/action)        | Message-toolbar button for on-demand session/usage lookups; reuses the Pipe's live connection and device identity, no second pairing |

Both are generated from the same `src/openclaw_pipe_pkg/` source fragments.
See [`docs/development.md`](./docs/development.md) if you're contributing.

---

## 📋 Requirements

| Component                   | Requirement                                                      |
| --------------------------- | ---------------------------------------------------------------- |
| Open WebUI                  | ≥ v0.10.2. Developed and tested against v0.10.2 and v0.11.0      |
| OpenClaw Gateway            | v2025+ (WS protocol v4)                                          |
| Python packages inside OWUI | `websockets`, `cryptography`. `pydantic` already ships with OWUI |

> **Open WebUI version:** native tool-card rendering depends on OWUI's
> Responses-API streaming handler. The integration performs no runtime version check,
> so on older releases tool cards degrade silently rather than erroring.

> **`websockets` / `cryptography`:** both functions declare them in the
> `requirements:` frontmatter field, so Open WebUI pip-installs them itself the
> first time the function loads. If your deployment sets
> `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=false` or runs with
> `OFFLINE_MODE`, install the two packages into the OWUI image yourself.

---

## 🚀 Installation

Two paths, same result. `install.py` drives Open WebUI's REST API, so it needs
your OWUI admin email and password. The manual path needs neither.

<details open>
<summary><b>🤖 Automated</b> &mdash; <sub>one command, needs your OWUI admin password</sub></summary>

#### Prerequisites

- A running OpenClaw Gateway, reachable from the machine Open WebUI runs on
- Admin credentials for your Open WebUI instance
- Your OpenClaw Gateway API token. This is the value of `gateway.auth.token`
  in your OpenClaw config; ask whoever runs your Gateway if you don't have it
- [`uv`](https://docs.astral.sh/uv/) on the **machine running the installer**
  (not inside OWUI). If you don't have it:

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

  No `uv`? Plain `pip install cryptography click rich` + `python3 install.py
install` works identically. `uv` just removes that manual step. (A bare
  `python3 install.py` with no subcommand only prints help.)

#### Run the installer

The wizard prompts for everything it needs and explains where to find each
value. Start here:

```bash
uv run install.py install --wizard
```

`uv run` resolves `cryptography`, `click`, and `rich` into an ephemeral
environment, so there is no venv or `pip install` step.

You can also run it without cloning first:

```bash
uv run https://raw.githubusercontent.com/Eliav2/openclaw-openwebui-integration/main/install.py install --wizard
```

In that form there is no `openclaw_pipe.py` next to the script, so the installer
downloads the artifact from this repo before deploying it. Point
`OPENCLAW_PIPE_ARTIFACT_URL` at a fork, a pinned tag, or an internal mirror to
change that.

#### Configuration

Only three values are required. Everything else has a working default:

| Required        |                                                |
| --------------- | ---------------------------------------------- |
| `OWUI_EMAIL`    | Open WebUI admin email                         |
| `OWUI_PASSWORD` | Open WebUI admin password                      |
| `GATEWAY_TOKEN` | `gateway.auth.token` from your OpenClaw config |

| Optional                    | Default                                                                                                    |
| --------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `OWUI_URL`                  | `http://localhost:8080`                                                                                    |
| `GATEWAY_URL`               | `localhost:18789`                                                                                          |
| `AGENT_ID`                  | `main`                                                                                                     |
| `OPENCLAW_BRIDGE_STATE_DIR` | `/data/openclaw-bridge`                                                                                    |
| `OWUI_API_BASE_URL`         | whatever `OWUI_URL` is. Set it only when the pipe must reach OWUI's API at a different address than you do |
| `OWUI_API_KEY`              | empty. Only needed if the request bearer token can't be used for media uploads                             |
| `FILE_SERVER_BASE_URL`      | empty. Only needed if you rely on the legacy media fallback                                                |

Supply them as env vars, or as flags with the same names (`--owui-email`,
`--gateway-token`, ...). `uv run install.py install --help` lists every flag.
Skip the wizard once they are set:

```bash
export OWUI_EMAIL=admin@example.com
export OWUI_PASSWORD=your-password
export GATEWAY_TOKEN=your-gateway-token

uv run install.py install
```

That non-interactive form is what you want for CI, config management, or
reinstalling after an OWUI upgrade.

#### What `install` does

1. Log in to Open WebUI
2. Check whether the Gateway function already exists
3. **If it exists:** update the code in place, preserving every valve including `DEVICE_IDENTITY`
4. **If it's new:** create the function
5. Back up the previous function and valves to `backups/`
6. Ensure it is active and global, without blindly toggling it off first
7. Generate or reuse a permanent device identity (Ed25519 key pair)
8. Restore valves if Open WebUI drops them during the update
9. Run an end-to-end smoke test through `/api/chat/completions`
10. Approve a matching pending pairing request, when the local `openclaw` CLI is available

Step 3 is why re-running this is safe: the function is never deleted and
recreated, so `DEVICE_IDENTITY` survives and you never re-approve the device.

#### Commands

| Command       | What it does                                                                                                                                         |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `install`     | Create or update the Pipe function and its valves, then smoke test                                                                                   |
| `repair`      | Same as `install`. Use after a broken or partial setup                                                                                               |
| `status`      | Print current state, change nothing                                                                                                                  |
| `healthcheck` | Status checks plus an end-to-end smoke test                                                                                                          |
| `skills`      | Install the agent-side skills. Run on the **Gateway host**, needs no OWUI credentials (see [Teaching your agent](#-teaching-your-agent-to-use-this)) |

`install` and `repair` also take `--with-skills` when you happen to be on the
Gateway host, and `--force-skills` to overwrite an existing skill.

</details>

<details>
<summary><b>✋ Manual</b> &mdash; <sub>paste two files in the admin panel, no credentials shared</sub></summary>

An OWUI function is one flat `.py` file. Paste it in the admin panel:

1. Open **Admin Panel** → **Functions** in OWUI
2. Click **"+"** → **"Create a function"** with ID `openclaw_gateway`, type `pipe`
3. Paste the contents of [`openclaw_pipe.py`](./openclaw_pipe.py)
4. Save, then toggle **Active** → **ON** and **Global** → **ON**
5. Set the valves:

   | Valve                 | Description                                                                             |
   | --------------------- | --------------------------------------------------------------------------------------- |
   | `GATEWAY_URL`         | OpenClaw Gateway address, `host:port`, no scheme (default: `localhost:18789`)           |
   | `GATEWAY_TOKEN`       | Your gateway API token                                                                  |
   | `AGENT_ID`            | Which agent to route to (default: `main`)                                               |
   | `DEVICE_IDENTITY`     | Paste from `./.pipe_device_identity.json` after running the script once, or leave empty |
   | `ENABLE_FILE_SERVER`  | `True` (media support)                                                                  |
   | `USE_OWUI_FILES`      | `True` (native OWUI Files API media support)                                            |
   | `OWUI_BASE_URL`       | Open WebUI base URL reachable from the OWUI backend                                     |
   | `SEND_STOP_ON_CANCEL` | `True` (workaround for `chat.abort` not stopping active tool subprocesses)              |

6. Choose `OpenClaw · Default` (or any discovered model) and start chatting

Optionally repeat the same steps for the Status Action, creating an
**Action** function (not a Pipe) with id `openclaw_status_action` and the
contents of [`openclaw_status_action.py`](./openclaw_status_action.py). Turn on
**Global** so the button appears on every model's messages, then set all five of
its valves, `GATEWAY_URL`, `GATEWAY_TOKEN`, `DEVICE_IDENTITY`, `STATE_DIR`,
`AGENT_ID`: to exactly the same values as the Pipe's.

> Do not skip `DEVICE_IDENTITY` here. Without it, a fallback connection from the
> Action registers as a _new_ device and the Gateway raises a second pairing
> request. `uv run install_action.py install` mirrors all five for you.

</details>

---

### Approve the device in the Gateway

A first install pairs a brand-new device, so the Gateway holds it pending until
you approve it. Expect the installer to report **pairing required**; that is the
normal first run, not a failure.

If the installer ran on the Gateway host it approves the request for you. If it
ran anywhere else, approve it yourself, on the Gateway host:

```bash
openclaw devices list      # find the pending request
openclaw devices approve <request-id>
```

**The identity is permanent.** The installer prefers the existing
`DEVICE_IDENTITY` valve, mirrors it into `./.pipe_device_identity.json`, and the
pipe persists it inside `STATE_DIR` on first run. Valve updates are in-place, so
approval lasts across reinstalls and restarts.

---

### Verify it worked

1. Reload Open WebUI. `OpenClaw · Default` should appear in the model picker.
2. Select it and send "hello". Text should stream in token by token.

Automated path only, covers both checks at once:

```bash
uv run install.py healthcheck   # status checks plus an end-to-end smoke test
```

All ✓ and exit 0 means done.

If the smoke test reports **pairing required**, the approval above hasn't landed
yet. Approve the device, then re-run `healthcheck`.

---

### Installing the companion Status Action

`install.py` manages `openclaw_pipe.py`. The Status Action has its own
installer, install the Pipe first, since it is the source of truth for the
shared valves:

```bash
uv run install_action.py install   # create/update the Action, mirror valves from the Pipe, smoke test
uv run install_action.py status    # inspect without changing anything
uv run install_action.py repair    # same as install
```

It reads only `OWUI_URL`, `OWUI_EMAIL`, and `OWUI_PASSWORD`. The five shared
valves, `GATEWAY_URL`, `GATEWAY_TOKEN`, `DEVICE_IDENTITY`, `STATE_DIR`,
`AGENT_ID`: are copied verbatim from the Pipe's valves, so the Action reuses
the Pipe's already-approved device identity and never triggers a second pairing.
It fails fast if the Pipe isn't installed yet.

Two differences from `install.py`: it must be run from a clone (it imports from
`install.py`, so the `uv run <raw-github-url>` form doesn't work), and it has no
`healthcheck` subcommand.

---

### Uninstalling

There's no `uninstall` subcommand; removal is a UI action. In Open WebUI go to
**Admin Panel → Functions** and delete `openclaw_gateway` (and
`openclaw_status_action` if you installed it). Every install first writes the
previous function and its valves to `backups/`, so if you only want to roll back
a bad upgrade, restore the JSON from there rather than deleting.

Deleting the functions does not touch `STATE_DIR`. Remove that directory too if
you want the device identity gone, and revoke the device on the Gateway with
`openclaw devices list` / `openclaw devices remove <id>`.

---

## ⚙️ Configuration reference

Behaviour you can tune after the integration is running. None of it is
needed for a first install.

### Restart-safe state

The integration loads identity in this order:

1. `STATE_DIR/identity.json` (default: `/data/openclaw-bridge/identity.json`)
2. `DEVICE_IDENTITY` valve, then persists it into `STATE_DIR`
3. Generate a new identity only if neither exists

The Gateway device token is saved to `STATE_DIR/device-token.json` after a
successful connection. This keeps OWUI restarts and pipe reloads from creating
new devices or requiring repeated approvals.

> **If `STATE_DIR` isn't writable**, the integration falls back to
> `/tmp/openclaw-bridge` and logs `STATE_DIR unavailable`. `/tmp` usually does
> not survive a container restart, so identity and device token are lost and
> you get a fresh pairing prompt every time. If OWUI keeps asking you to
> re-approve the device, grep the backend log for `STATE_DIR unavailable` and
> point `STATE_DIR` at a real persistent volume. `/data` is not mounted in
> every OWUI deployment.

### OWUI model selector

The integration is a manifold function: `pipes()` calls the Gateway's `models.list`
RPC (falling back to a cached list, then a small hardcoded list, if the
Gateway is unreachable) and returns one selector entry per discovered model,
plus an always-present `OpenClaw · Default` entry. Selecting a model patches
that OWUI conversation's OpenClaw session to that model; switching back to
`Default` clears the override and returns to the agent's configured default.

Relevant valves:

| Valve               | Description                                                                                                 |
| ------------------- | ----------------------------------------------------------------------------------------------------------- |
| `CONFIGURED_MODELS` | Comma-separated whitelist of model keys to show. Empty = show all discovered models.                        |
| `DEFAULT_MODEL`     | Model used when `OpenClaw · Default` is selected. Also doubles as a reference dropdown of known model keys. |
| `MAX_MODELS`        | Safety cap on how many models the selector lists when there's no whitelist (default `30`).                  |

> **Legacy presets.** The `CHATGPT_MODEL`, `OPUS_MODEL`, `SONNET_MODEL`, and
> `GLM_MODEL` valves still produce fixed selector entries, purely so
> conversations that already picked one keep working. Ignore them on a new
> install; dynamic discovery replaces them.

### Auto-generated chat titles

When `AUTO_TITLE` is enabled (default), the integration generates a chat title after
the first exchange, similar to native OWUI behavior. Title generation runs on
a separate agent lane (`TITLE_GEN_AGENT_ID`, default `title-gen`) so it never
queues behind, or pollutes, the main conversation's session.

### Native OWUI media delivery

When the agent emits a `MEDIA:<filename>` directive and the file exists in the
pipe media directory, the integration now tries this path first:

1. Upload the file to `POST /api/v1/files/?process=false`
2. Emit a `files` event so Open WebUI attaches the file to the assistant message
3. Yield same-origin markdown such as
   `![image.png](/api/v1/files/<id>/content)`

This avoids mixed-content blocking when OWUI is opened over HTTPS. If upload
auth is unavailable or the Files API fails, the integration falls back to the legacy
`FILE_SERVER_BASE_URL` behavior.

Relevant valves:

| Valve                  | Description                                                                                                                                                                                                                                                                 |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `USE_OWUI_FILES`       | Enable OWUI Files API upload for `MEDIA:` directives (default: `True`)                                                                                                                                                                                                      |
| `OWUI_BASE_URL`        | Base URL the integration uses to call the OWUI Files API (default: `http://127.0.0.1:8080`). The integration runs inside the OWUI backend, so the loopback default is usually correct.                                                                                      |
| `OWUI_API_KEY`         | Optional API key for uploads; the current request bearer token is preferred (default: empty)                                                                                                                                                                                |
| `FILE_SERVER_BASE_URL` | Legacy fallback base URL for the built-in media file server, used only when `USE_OWUI_FILES` is off or an upload fails (default: `http://localhost:18791`). The **browser** resolves this URL, not OWUI, so the default only works when you browse OWUI from the same host. |
| `SEND_STOP_ON_CANCEL`  | Send `/stop` after `chat.abort` when OWUI cancels a stream (default: `True`)                                                                                                                                                                                                |

---

## 🔧 Troubleshooting

| Symptom                                               | Likely cause                                                                                                                                                                                                                                                                                                                                                   |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Text appears all at once                              | Pipe uses `return` instead of `yield` (check your code)                                                                                                                                                                                                                                                                                                        |
| "No GATEWAY_TOKEN configured"                         | Valve not set, go to Admin → Functions → edit valves                                                                                                                                                                                                                                                                                                           |
| An `**Error:**` line instead of a reply               | OWUI can't reach `GATEWAY_URL`: check network connectivity from the OWUI backend                                                                                                                                                                                                                                                                               |
| Model missing from selector                           | Run `uv run install.py repair`; it ensures the function is active/global and visible in `/api/v1/models`. Also check `CONFIGURED_MODELS` isn't filtering it out.                                                                                                                                                                                               |
| "pairing required"                                    | Run `uv run install.py repair` or approve the matching request with `openclaw devices approve <request-id>`                                                                                                                                                                                                                                                    |
| Image still uses the legacy file server               | OWUI file upload failed and the integration fell back; check `OWUI_BASE_URL`, request auth/API key, and OWUI logs                                                                                                                                                                                                                                              |
| Tool calls not showing                                | The `__event_emitter__` calls fail silently; check OWUI backend logs                                                                                                                                                                                                                                                                                           |
| Device identity not persisting                        | Check `STATE_DIR` and the `DEVICE_IDENTITY` valve; run `uv run install.py healthcheck`                                                                                                                                                                                                                                                                         |
| Stream stops mid-response                             | The integration probes the Gateway after 30s of silence and gives up after 180s with no text; check agent response time and the OWUI backend log                                                                                                                                                                                                               |
| Repeated device-pairing prompts                       | `STATE_DIR` isn't persistent, grep the OWUI backend log for `STATE_DIR unavailable`                                                                                                                                                                                                                                                                            |
| `ModuleNotFoundError: websockets` on load             | OWUI's frontmatter auto-install is off (`ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=false`) or it's in offline mode, install `websockets` and `cryptography` into the OWUI image                                                                                                                                                                              |
| Models are listed as "- example, Gateway not reached" | The integration hasn't reached your Gateway yet, so the selector is showing a built-in example list rather than your models. Pick `OpenClaw · Default` (it always works), send one message to establish the connection, then reload, the real list replaces it. If it persists, the Gateway is genuinely unreachable: check `GATEWAY_URL` and `GATEWAY_TOKEN`. |
| "Could not start a session on agent `<id>`"           | `AGENT_ID` names an agent your Gateway doesn't define. Use `main` unless you configured others.                                                                                                                                                                                                                                                                |
| Restarting mid-turn loses context                     | Known OpenClaw limitation, avoid restarting the Gateway while a run is in flight                                                                                                                                                                                                                                                                               |

Check OWUI's backend logs for `[openclaw-pipe]` prefixed messages.

---

## 🤖 Teaching your agent to use this

Two features need the **agent** to know a convention. The integration provides
the mechanism, but nothing tells your model it exists, so out of the box the
agent will never use them:

| Feature               | The agent must                            | Otherwise                                                   |
| --------------------- | ----------------------------------------- | ----------------------------------------------------------- |
| Ask-user dialog       | Start a line with `OpenClaw needs input:` | It calls its own ask tool, which never reaches you here     |
| Send an image or file | Emit `MEDIA:<bare-filename>`              | An absolute path silently drops, with no image and no error |

Two skills ship with this repo. Install them **on the Gateway host**, which is
where the agent runs and is not always where Open WebUI runs:

```bash
openclaw skills install ./skills/openclaw-owui-ask-user
openclaw skills install ./skills/openclaw-owui-media
```

Or let the installer do it, if you are already on the Gateway host:

```bash
uv run install.py install --with-skills   # during install
uv run install.py skills                  # any time afterwards
```

It detects the local `openclaw` CLI. If the Gateway is on another machine, it
says so and prints the commands to run there instead.

**Updating:** `git pull`, then re-run the same command with `--force-skills`
(or `openclaw skills install ... --force`). Overwriting is opt-in on purpose:
OpenClaw keeps no history for skill installs, so an overwrite is unrecoverable,
and a skill you wrote yourself should never be clobbered by an installer.

If you would rather not install anything, put the two conventions in your
agent's own instructions. The skills are only a convenient carrier.

---

## 📚 Reference docs

The detail that used to live in this file, moved out so the README stays a
getting-started document:

| Doc                                                                    | What it covers                                                                                                                            |
| ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| [`docs/architecture.md`](./docs/architecture.md)                       | How a message travels OWUI → Gateway → agent → back, why the Pipe is an async generator, and the repo's file layout                       |
| [`docs/metadata-contract.md`](./docs/metadata-contract.md)             | The Sender and Conversation info blocks every message carries, their stability guarantees, and how an agent should read them              |
| [`docs/development.md`](./docs/development.md)                         | Editing `src/` fragments, the build and drift guard, running the tests, driving the integration over the OWUI API                         |
| [`docs/rehydration-persistence.md`](./docs/rehydration-persistence.md) | How OWUI persists an assistant message and why the integration emits mid-run snapshots. Read before touching snapshots or streaming       |
| [`docs/ask-user-modal.md`](./docs/ask-user-modal.md)                   | Design spec for a tool-call-based ask-user flow. **Not implemented as written**, the shipped path intercepts `OpenClaw needs input:` text |

---

## 🔒 Security notes

- **`GATEWAY_TOKEN` and `DEVICE_IDENTITY` are secrets.** `DEVICE_IDENTITY` holds
  an Ed25519 **private key**. `install.py` mirrors it to
  `./.pipe_device_identity.json` (gitignored) and backs valves up to `backups/`
  (also gitignored), don't commit or share either.
- **The legacy media file server is unauthenticated.** When
  `ENABLE_FILE_SERVER` is on (the default), the integration serves
  `/tmp/openclaw-pipe-media` over HTTP on port **18791**, bound to `0.0.0.0`
  with `Access-Control-Allow-Origin: *` and no auth. Anyone who can reach that
  port can read files the agent has emitted. It exists only as a fallback for
  the OWUI Files API path (`USE_OWUI_FILES`, on by default and same-origin).
  Keep 18791 off untrusted networks, or set `ENABLE_FILE_SERVER=False` if you
  don't need the fallback.
- **The integration runs inside the OWUI backend** and talks to the Gateway with the
  configured agent's privileges. Anyone who can chat with the integration can drive
  that agent, scope `AGENT_ID` accordingly.

---

## 📜 License

MIT, see [LICENSE](./LICENSE).

---

## 🙏 Acknowledgements

- Based on [cfullelove's gist](https://gist.github.com/cfullelove/7c6fa74e16d0a8f355e6d5ddb6d8e5fb),
  the original proof-of-concept that got this rolling
- [Open WebUI](https://openwebui.com/) for the excellent pipe/function system
- [OpenClaw](https://github.com/openclaw/openclaw) for the WS Gateway Protocol
