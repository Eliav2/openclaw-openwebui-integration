# Architecture

How a message travels from Open WebUI to your agent and back, and where each
piece of the repo lives.

The integration is an
[external app](https://docs.openclaw.ai/gateway/external-apps): a plain Gateway
protocol client. It imports nothing from OpenClaw's plugin SDK and adds no
upstream footprint, so it can only do what any other v4 client can do.

## How it works

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
│   • Native tool cards│
│   • Status updates   │
│   • Media links      │
└──────────────────────┘
```

The integration:
1. Receives the user's message from OWUI
2. Opens (or reuses) a persistent, singleton WebSocket connection to the
   Gateway, shared across all conversations
3. Performs the Ed25519 challenge/response handshake once, on first use
4. Sends the message with a stable session key
5. **Yields** each event chunk back to OWUI:
   - `stream="assistant"` → text delta → **yielded** for streaming
   - `stream="tool"` → tool call → **yielded** as a native Responses-API
     output item, so OWUI renders a two-phase tool card (spinner while
     running, result on finish) instead of hand-built HTML
   - `stream="lifecycle"` → end signal → **done**

Because the integration is an **async generator** (uses `yield` instead of `return`),
OWUI streams each chunk to the frontend in real time. Once content is yielded
it's frozen on screen for the rest of the run. The integration can still update
what's saved to the OWUI database afterward, but not what's already rendered,
so anything meant to change later (like a tool result replacing a spinner) has
to be sent as a fresh item, not a patch to an old one.

---

## File layout

```
openclaw-openwebui-integration/
├── openclaw_pipe.py           # Built artifact, paste this into OWUI (Pipe)
├── openclaw_status_action.py  # Built artifact, paste this into OWUI (Action)
├── src/
│   ├── frontmatter.txt         # Pipe docstring + OWUI metadata (title/version/requirements)
│   ├── frontmatter-action.txt  # Same, for the Action
│   └── openclaw_pipe_pkg/     # Source of truth, edit these, not the artifacts above
│       ├── _prelude.py         # Imports shared by every fragment, plus pipe_log()
│       ├── pipe.py              # Pipe class: valves and the integration() entry point
│       ├── gateway.py            # WebSocket connection, handshake, event dispatch/demux
│       ├── action.py              # Status Action class
│       ├── askuser.py              # "needs input:" interception + modal plumbing
│       ├── emit.py                  # Event emission / tool-card formatting helpers
│       ├── media.py                  # MEDIA: directive handling, OWUI Files API upload
│       ├── models.py                  # Dynamic model discovery, whitelist, fallback list
│       ├── identity.py                 # Ed25519 device identity load/generate/persist
│       ├── state.py                     # STATE_DIR read/write helpers
│       └── __init__.py
├── build.py                    # Concatenates src/ fragments into the artifacts above
├── install.py                  # Automated installer/updater/healthcheck script
├── install_action.py           # Same, for the Status Action
├── install_filter.py           # Same, for the thinking Filter
├── test_*.py                   # Unit + integration test suites (see Development)
├── scripts/                    # Auxiliary scripts (OWUI API client, live verifiers)
├── docs/                       # Architecture, metadata contract, development, design notes
├── .github/workflows/ci.yml    # Drift guard + tests + artifact load check
├── backups/                    # Local install backups (gitignored, created on first install)
├── README.md                   # Getting started
├── LICENSE                     # MIT
├── .gitattributes
└── .gitignore
```

> `src/frontmatter.txt` is load-bearing: Open WebUI reads the function's title,
> version, and `requirements:` from the leading docstring, and `build.py` emits
> it as the first bytes of the artifact. Valve *documentation* shown in OWUI
> lives there; valve *definitions* live in `pipe.py`.

> `.pipe_device_identity.json` is created by `install.py` to persist the
> device identity across re-installs. It contains a private key, **do not**
> commit or share it.
