# OpenClaw Gateway Pipe for Open WebUI 🔌

A self-contained [Open WebUI](https://openwebui.com/) **Pipe** that connects to
[OpenClaw Gateway](https://github.com/openclaw/openclaw) via its native WebSocket
protocol — giving you real-time streaming, tool call rendering, and persistent
agent sessions inside OWUI's chat interface.

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
- **🖼️ Media support** — built-in HTTP file server for serving images/media back
  to OWUI
- **⚙️ Configurable** — all settings live in OWUI valves (no config files)

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

# Run the installer
python3 install.py
```

The script will:
1. Log in to Open WebUI
2. Delete any existing pipe with the same ID
3. Create the pipe function
4. Enable it (active + global)
5. Generate a **permanent device identity** (Ed25519 key pair)
6. Set all valves (including `DEVICE_IDENTITY`)
7. Print the device ID and guide you through Gateway approval

### Approve the device in the Gateway

When using the pipe for the first time, your OpenClaw Gateway will prompt
for device approval. On the Gateway host, run:

```bash
openclaw devices list      # find the pending request
openclaw devices approve <request-id>
```

**The identity is permanent.** Even if the pipe function is deleted and
recreated, the installer re-uses the same key pair (`./.pipe_device_identity.json`),
so approval is a **one-time** step.

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
| Tool calls not showing | The `__event_emitter__` calls fail silently; check OWUI backend logs |
| Device identity not persisting | Copy the printed `DEVICE_IDENTITY` from logs into the valve |
| Stream stops mid-response | WS timeout (60s default); check agent response time |

Check OWUI's backend logs for `[openclaw-pipe]` prefixed messages.

---

## 📁 File Layout

```
openclaw-openwebui-pipe/
├── openclaw_pipe.py    # The pipe — paste this into OWUI
├── install.py          # Automated installer script
├── README.md           # This file
├── LICENSE             # MIT
└── .gitignore          # Ignores .pipe_device_identity.json
```

> `.pipe_device_identity.json` is created by `install.py` to persist the
> device identity across re-installs. It contains a private key — **do not**
> commit or share it.

---

## 📜 License

MIT — see [LICENSE](./LICENSE).

---

## 🙏 Acknowledgements

- Based on [cfullelove's gist](https://gist.github.com/cfullelove/7c6fa74e16d0a8f355e6d5ddb6d8e5fb)
  — the original proof-of-concept that got this rolling
- [Open WebUI](https://openwebui.com/) for the excellent pipe/function system
- [OpenClaw](https://github.com/openclaw/openclaw) for the WS Gateway Protocol
