# OpenClaw ↔ Open WebUI Integration 🔌

Bidirectional integration between [OpenClaw Gateway](https://github.com/openclaw/openclaw)
and [Open WebUI](https://openwebui.com/). The primary component is a **Pipe** function
that connects via OpenClaw's native WebSocket protocol — giving you real-time
streaming, tool call rendering, and persistent agent sessions inside OWUI's chat
interface.

> No separate proxy, no Node.js middleware, no Python subprocess. Just a single
> Python file you paste into OWUI's admin panel.

<p align="center">
  <img src="./owui-screenshot.svg" alt="OpenClaw Gateway Pipe in action — streaming text, tool call rendering, and model selection inside Open WebUI" width="90%">
</p>

---

## ✨ Features

- **🔴 Real-time streaming** — assistant responses appear token-by-token, not
  all at once at the end
- **🛠️ Tool call rendering** — tools show up as native collapsible
  `<details type="tool_calls">` blocks in the chat
- **💬 Persistent sessions** — each OWUI conversation gets a stable OpenClaw
  session key (`agent:main:openwebui-{user_id}-{chat_id}`), so the agent
  remembers context across messages
- **🔐 Ed25519 device auth** — full WebSocket handshake with challenge/response
- **🖼️ Native OWUI media support** — `MEDIA:` files are uploaded to the
  Open WebUI Files API and attached to the assistant message; the old file
  server remains as a fallback
- **⚙️ Configurable** — settings live in OWUI valves; device identity/token
  also persist in a state directory so restarts do not force re-pairing

---

## ⚠️ Known Limitations (v0.5)

This version works well for basic chat but has known issues being tracked for v1:

| Issue | Description | Status |
|-------|-------------|--------|
| **Concurrent messages** | Sending a message while the agent is still responding now steers into the active run (no lock, no blocking, no garbling). | ✅ Fixed — persistent WS + steering |
| **No stop button** | Pressing stop in OWUI now sends `chat.abort` to the Gateway. | ✅ Fixed — CancelledError triggers abort |
| **60s idle timeout** | Long agent runs no longer cut off — tick keepalive keeps the shared connection alive. | ✅ Fixed — persistent connection + tick handler |
| **Per-message reconnect** | WS handshake + crypto every message is gone. | ✅ Fixed — singleton connection |
| **Session bleed** | Events from other chats/surfaces can appear mid-response. | ✅ Fixed — event dispatcher demuxes by sessionKey/runId |
| **Title/tag pollution** | OWUI background tasks (auto-title, tags, follow-up suggestions) pollute the OpenClaw session. | ✅ Fixed — task short-circuit (Phase 0) |
| **Sender metadata** | "Sender (untrusted metadata)" block visible on every message; sender is hardcoded `id:"test"`. | Pending — Gateway schema limits client.id enum; needs further investigation |
| **Image serving** | Images use base64 data-URI or a separate file server URL. Mixed-content blocked on HTTPS OWUI. | ✅ Fixed — OWUI Files API upload + file-server fallback |
| **Restart context loss** | Restarting the pipe mid-turn loses the in-flight state (OpenClaw limitation). | Workaround — avoid restarting mid-run |

---

## 📋 Requirements

| Component | Version |
|-----------|---------|
| Open WebUI | ≥ v0.9 (tested on v0.10.x) |
| OpenClaw Gateway | v2025+ (WS protocol v4) |
| Python (in OWUI) | websockets, cryptography |
| | (pydantic ships with OWUI) |

> **If your OWUI deployment doesn't have `websockets` / `cryptography`:**
> You may need to install them in the OWUI container or use a custom image.
> Most standard OWUI Docker images come with these already.

---

## 🚀 Installation (automated)

### Prerequisites

- Python 3.8+ with `cryptography` package on the **machine running this script**
  (`pip install cryptography`)
- Admin credentials for your Open WebUI instance
- Your OpenClaw Gateway API token

### Run the installer

```bash
# Install cryptography if you don't have it
pip install cryptography

# Set your environment variables
export OWUI_URL=http://your-owui-host:8080
export OWUI_EMAIL=admin@example.com
export OWUI_PASSWORD=your-password
export GATEWAY_URL=your-owui-host:18789
export GATEWAY_TOKEN=your-gateway-token
export AGENT_ID=main
export OWUI_API_BASE_URL=http://your-owui-host:8080

# Install or update the pipe in place, then run a smoke test
python3 install.py install

# Repair a broken install without deleting the function or valves
python3 install.py repair

# Inspect current state without changing anything
python3 install.py status

# Run status checks plus an end-to-end smoke test
python3 install.py healthcheck
```

The script will:
1. Log in to Open WebUI
2. Check if the pipe function already exists
3. **If exists:** update the code in-place (preserving all valves including `DEVICE_IDENTITY`)
4. **If new:** create the pipe function
5. Back up the existing function and valves to `backups/`
6. Ensure it is active + global without blindly toggling it off
7. Generate or reuse a **permanent device identity** (Ed25519 key pair)
8. Restore valves if Open WebUI drops them during a function update
9. Run an end-to-end smoke test through `/api/chat/completions`
10. If a matching pairing request is pending, approve it automatically when the
    local `openclaw` CLI is available

> **v2+ no longer deletes and recreates the function**, which means your
> valve settings (especially `DEVICE_IDENTITY`) survive re-installation.
> The old `delete+create` cycle that wiped `DEVICE_IDENTITY` and forced
> re-approval is gone.

### Restart-safe state

The pipe loads identity in this order:

1. `STATE_DIR/identity.json` (default: `/data/openclaw-bridge/identity.json`)
2. `DEVICE_IDENTITY` valve, then persists it into `STATE_DIR`
3. Generate a new identity only if neither exists

The Gateway device token is saved to `STATE_DIR/device-token.json` after a
successful connection. This keeps OWUI restarts and pipe reloads from creating
new devices or requiring repeated approvals.

### Native OWUI media delivery

When the agent emits a `MEDIA:<filename>` directive and the file exists in the
pipe media directory, the pipe now tries this path first:

1. Upload the file to `POST /api/v1/files/?process=false`
2. Emit a `files` event so Open WebUI attaches the file to the assistant message
3. Yield same-origin markdown such as
   `![image.png](/api/v1/files/<id>/content)`

This avoids mixed-content blocking when OWUI is opened over HTTPS. If upload
auth is unavailable or the Files API fails, the pipe falls back to the legacy
`FILE_SERVER_BASE_URL` behavior.

Relevant valves:

| Valve | Description |
|-------|-------------|
| `USE_OWUI_FILES` | Enable OWUI Files API upload for `MEDIA:` directives (default: `True`) |
| `OWUI_BASE_URL` | Base URL used by the pipe to call the OWUI Files API |
| `OWUI_API_KEY` | Optional API key for uploads; the current request bearer token is preferred |
| `FILE_SERVER_BASE_URL` | Legacy fallback URL for the pipe file server |

### Approve the device in the Gateway

When using the pipe for the first time, your OpenClaw Gateway will prompt
for device approval. On the Gateway host, run:

```bash
openclaw devices list      # find the pending request
openclaw devices approve <request-id>
```

**The identity is permanent.** The installer prefers the existing
`DEVICE_IDENTITY` valve, mirrors it into `./.pipe_device_identity.json`, and the
pipe persists it inside `STATE_DIR` on first run. Valve updates are in-place, so
approval lasts across reinstalls and restarts.

### Manual installation (alternative)

If you can't run the script, install manually:

1. Open **Admin Panel** → **Functions** in OWUI
2. Click **"+"** → **"Create a function"** with ID `openclaw_gateway`, type `pipe`
3. Paste the contents of [`openclaw_pipe.py`](./openclaw_pipe.py)
4. Save, then toggle **Active** → **ON** and **Global** → **ON**
5. Set the valves:

   | Valve | Description |
   |-------|-------------|
   | `GATEWAY_URL` | OpenClaw Gateway address |
   | `GATEWAY_TOKEN` | Your gateway API token |
   | `AGENT_ID` | Which agent to route to (default: `main`) |
   | `DEVICE_IDENTITY` | Paste from `./.pipe_device_identity.json` after running the script once, or leave empty |
   | `ENABLE_FILE_SERVER` | `True` (media support) |
   | `USE_OWUI_FILES` | `True` (native OWUI Files API media support) |
   | `OWUI_BASE_URL` | Open WebUI base URL reachable from the OWUI backend |

6. Choose "OpenClaw Gateway" as your model and start chatting

---

## 🧠 How It Works

```
┌──────────────────────┐       WS (v4)        ┌──────────────────┐
│   Open Web UI        │ ◄──────────────────► │  OpenClaw        │
│                      │   stream=assistant   │  Gateway         │
│  ┌────────────────┐  │   stream=tool        │                  │
│  │ openclaw_pipe  │──┤   stream=item        │  ┌────────────┐  │
│  │ (OWUI Pipe)    │  │   stream=lifecycle   │  │ Agent(s)   │  │
│  └────────────────┘  │                      │  └────────────┘  │
│                      │                      │                  │
│  OWUI renders:       │                      └──────────────────┘
│   • Text streaming   │
│   • Tool call cards  │
│   • Status updates   │
│   • Media links      │
└──────────────────────┘
```

The pipe:
1. Receives the user's message from OWUI
2. Opens a WebSocket connection to the Gateway
3. Performs the Ed25519 challenge/response handshake
4. Sends the message with a stable session key
5. **Yields** each event chunk back to OWUI:
   - `stream="assistant"` → text delta → **yielded** for streaming
   - `stream="tool"` → tool result → **yielded** as `<details>` HTML
   - `stream="lifecycle"` → end signal → **done**

Because the pipe is an **async generator** (uses `yield` instead of `return`),
OWUI streams each chunk to the frontend in real time.

---

## 🔧 Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| Text appears all at once | Pipe uses `return` instead of `yield` (check your code) |
| "No GATEWAY_TOKEN configured" | Valve not set — go to Admin → Functions → edit valves |
| "Connection error" in chat | OWUI can't reach `GATEWAY_URL` — check network connectivity |
| Model missing from selector | Run `python3 install.py repair`; it ensures the function is active/global and visible in `/api/v1/models` |
| "pairing required" | Run `python3 install.py repair` or approve the matching request with `openclaw devices approve <request-id>` |
| Image still uses `:18791` | OWUI file upload failed and the pipe fell back; check `OWUI_BASE_URL`, request auth/API key, and OWUI logs |
| Tool calls not showing | The `__event_emitter__` calls fail silently; check OWUI backend logs |
| Device identity not persisting | Check `STATE_DIR` and the `DEVICE_IDENTITY` valve; run `python3 install.py healthcheck` |
| Stream stops mid-response | WS timeout (60s default); check agent response time |

Check OWUI's backend logs for `[openclaw-pipe]` prefixed messages.

---

## 📁 File Layout

```
openclaw-openwebui-integration/
├── openclaw_pipe.py    # The pipe — paste this into OWUI
├── install.py          # Automated installer script
├── backups/            # Local install backups (ignored by git)
├── README.md           # This file
├── LICENSE             # MIT
└── .gitignore          # Ignores .pipe_device_identity.json
```

> `.pipe_device_identity.json` is created by `install.py` to persist the
> device identity across re-installs. It contains a private key — **do not**
> commit or share it.

---

## 🧪 Testing the pipe via API

You can send messages through the pipe as if using the OWUI frontend by calling
the internal `/api/chat/completions` endpoint:

```bash
# 1. Sign in to get a token
TOKEN=$(curl -s -X POST http://localhost:8080/api/v1/auths/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"***"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 2. Send a message through the pipe
curl -s -X POST http://localhost:8080/api/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openclaw_gateway",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": false
  }'
```

This triggers the full pipeline — pipe function → Gateway → agent → response —
and the conversation is saved to OWUI chat history automatically.

---

## 📜 License

MIT — see [LICENSE](./LICENSE).

---

## 🙏 Acknowledgements

- Based on [cfullelove's gist](https://gist.github.com/cfullelove/7c6fa74e16d0a8f355e6d5ddb6d8e5fb)
  — the original proof-of-concept that got this rolling
- [Open WebUI](https://openwebui.com/) for the excellent pipe/function system
- [OpenClaw](https://github.com/openclaw/openclaw) for the WS Gateway Protocol
