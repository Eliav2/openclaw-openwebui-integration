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

## 🚀 Installation

### 1. Add the Pipe in OWUI

1. Open **Admin Panel** → **Functions**
2. Click **"+"** → **"Create a function"**
3. Set:
   - **ID:** `openclaw_gateway`
   - **Name:** `OpenClaw Gateway`
   - **Type:** `pipe`
4. Paste the entire contents of [`openclaw_pipe.py`](./openclaw_pipe.py) into
   the code editor
5. Click **Save**

### 2. Configure Valves

After saving, open the **Valves** section and set:

| Valve | Description | Default |
|-------|-------------|---------|
| `GATEWAY_URL` | OpenClaw Gateway address | `localhost:18789` |
| `GATEWAY_TOKEN` | Your gateway API token | *(required)* |
| `AGENT_ID` | Which agent to route to | `main` |
| `DEVICE_IDENTITY` | (Advanced) paste from first-run logs to persist identity | `""` |
| `ENABLE_FILE_SERVER` | Start HTTP server for media files | `True` |

### 3. Enable & Use

1. Toggle the pipe **Active** → **ON**
2. (Optional) Toggle **Global** → **ON** to make it available to all users
3. Go to any chat, open the model selector dropdown, and choose
   **"OpenClaw Gateway"**
4. Start chatting! 🤖

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
├── README.md           # This file
└── LICENSE             # MIT
```

---

## 📜 License

MIT — see [LICENSE](./LICENSE).

---

## 🙏 Acknowledgements

- Based on [cfullelove's gist](https://gist.github.com/cfullelove/7c6fa74e16d0a8f355e6d5ddb6d8e5fb)
  — the original proof-of-concept that got this rolling
- [Open WebUI](https://openwebui.com/) for the excellent pipe/function system
- [OpenClaw](https://github.com/openclaw/openclaw) for the WS Gateway Protocol
