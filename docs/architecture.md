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

### The three functions

The repo ships three Open WebUI functions, each a single built file at the repo root:

| File | Open WebUI type | Purpose |
|---|---|---|
| [`openclaw_pipe.py`](../openclaw_pipe.py) | [Pipe](https://docs.openwebui.com/features/extensibility/plugin/functions/pipe) (manifold) | The chat integration: almost every [feature](./features/index.md). |
| [`openclaw_status_action.py`](../openclaw_status_action.py) | [Action](https://docs.openwebui.com/features/extensibility/plugin/functions/action) | The Status button on the message toolbar, for on-demand session and usage lookups. It reuses the Pipe's live connection and device identity, so there is no second pairing. |
| [`openclaw_thinking_filter.py`](../openclaw_thinking_filter.py) | [Filter](https://docs.openwebui.com/features/extensibility/plugin/functions/filter) | The per-chat thinking control in the composer. Optional, and it imports nothing but the standard library and pydantic. |

All three are generated from the same source fragments in `src/openclaw_pipe_pkg/`, by `build.py`.

### The tree

```
openclaw-openwebui-integration/
├── openclaw_pipe.py           # Built artifact, paste this into OWUI (Pipe)
├── openclaw_status_action.py  # Built artifact, paste this into OWUI (Action)
├── openclaw_thinking_filter.py # Built artifact, paste this into OWUI (Filter)
├── src/
│   ├── frontmatter.txt         # Pipe docstring + OWUI metadata (title/version/requirements)
│   ├── frontmatter-action.txt  # Same, for the Action
│   ├── frontmatter-filter.txt  # Same, for the Filter
│   └── openclaw_pipe_pkg/     # Source of truth, edit these, not the artifacts above
│       ├── _prelude.py         # Imports shared by every fragment, plus pipe_log()
│       ├── pipe.py              # Pipe class: valves and the pipe() entry point
│       ├── gateway.py            # WebSocket connection, handshake, event dispatch/demux
│       ├── turns.py              # Which Open WebUI message each run writes into
│       ├── writer.py             # Writes runs the agent starts (cron, heartbeat, sub-agent) into the chat
│       ├── action.py              # Status Action class
│       ├── composer.py            # Composer Claw button: in-page JS snippet + emit helper
│       ├── composer_watch.py      # Pipe only: pushes that snippet when a tab opens an OpenClaw chat
│       ├── askuser.py              # "needs input:" interception + question card plumbing
│       ├── emit.py                  # Event emission / tool-card formatting helpers
│       ├── media.py                  # MEDIA: directive handling, OWUI Files API upload, file server
│       ├── models.py                  # Dynamic model discovery, cached list, fallback list
│       ├── history.py                 # Edit/Regenerate/branch notes and the copied messages of a cloned chat
│       ├── identity.py                 # Ed25519 device identity load/generate/persist
│       ├── state.py                     # STATE_DIR read/write helpers
│       ├── thinking.py                  # Thinking levels and clamping, shared by the Pipe and the Filter
│       ├── thinking_filter.py           # Filter class
│       └── __init__.py
├── build.py                    # Concatenates src/ fragments into the artifacts above
├── install.py                  # Automated installer/updater/healthcheck script
├── install_action.py           # Same, for the Status Action
├── install_filter.py           # Same, for the thinking Filter
├── test_*.py                   # Unit + integration test suites (see Development)
├── e2e/                        # End-to-end stack: fake model, real Gateway and Open WebUI, pinned versions
├── gateway-plugins/            # Optional Gateway plugins for a Claude CLI login: rate limits, /claude_login
├── skills/                     # The two agent skills (questions, media)
├── scripts/                    # Auxiliary scripts (OWUI API client, live verifiers, screenshots)
├── docs/                       # These docs; design notes in docs/design/
├── .github/workflows/          # CI (drift guard, tests, artifact load check), E2E, compat watch
├── backups/                    # Local install backups (gitignored, created on first install)
├── README.md                   # Getting started
├── SECURITY.md                 # How to report a vulnerability
├── LICENSE                     # MIT
├── .gitattributes
└── .gitignore
```

### How the fragments become the artifacts

`build.py` concatenates an ordered list of fragments per artifact, behind each artifact's frontmatter file. Several fragments feed more than one artifact:

- **Pipe:** `_prelude`, `identity`, `state`, `turns`, `thinking`, `media`, `emit`, `composer`, `composer_watch`, `askuser`, `gateway`, `writer`, `models`, `history`, `pipe`.
- **Status Action:** `_prelude`, `identity`, `state`, `turns`, `thinking`, `gateway`, `emit`, `composer`, `action`. It reuses the Pipe's connection code verbatim, so its numbers come from the same logic as the Pipe's status line.
- **Thinking filter:** `thinking`, `thinking_filter` only. It skips `_prelude` on purpose: a filter runs on every message, so it does not import `websockets` or `cryptography`.

Build commands and the drift guard: [Development](./development.md#building-the-artifacts).

> `src/frontmatter.txt` is load-bearing: Open WebUI reads the function's title,
> version, and `requirements:` from the leading docstring, and `build.py` emits
> it as the first bytes of the artifact. Valve *documentation* shown in OWUI
> lives there; valve *definitions* live in `pipe.py`.

> `.pipe_device_identity.json` is created by `install.py` to persist the
> device identity across re-installs. It contains a private key, **do not**
> commit or share it.
