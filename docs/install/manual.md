Install the Pipe by pasting one file into Open WebUI's admin panel and setting a few valves. No script and no credentials shared; you end up where the installer would have left you.

Applies to: Pipe

Valves: [`GATEWAY_URL`](../configuration.md#gateway_url), [`GATEWAY_TOKEN`](../configuration.md#gateway_token), [`AGENT_ID`](../configuration.md#agent_id), [`DEVICE_IDENTITY`](../configuration.md#device_identity), [`STATE_DIR`](../configuration.md#state_dir), [`ENABLE_FILE_SERVER`](../configuration.md#enable_file_server), [`USE_OWUI_FILES`](../configuration.md#use_owui_files), [`OWUI_BASE_URL`](../configuration.md#owui_base_url), [`SEND_STOP_ON_CANCEL`](../configuration.md#send_stop_on_cancel)

## Before you start

- A running OpenClaw Gateway, reachable from the Open WebUI backend.
- Your OpenClaw Gateway API token: the value of `gateway.auth.token` in your OpenClaw config.
- Admin access to Open WebUI in a browser.
- Open WebUI must be able to pip-install `websockets` and `cryptography` the first time the function loads. If yours can't (offline, or frontmatter installs turned off), see [Python packages inside Open WebUI](./installer.md#python-packages-inside-open-webui).

## Paste the Pipe

An Open WebUI function is one flat `.py` file. Paste it in the admin panel:

1. Open **Admin Panel** → **Functions** in Open WebUI.
2. Click **+** → **Create a function** with ID `openclaw_gateway`, type `pipe`.
3. Paste the contents of [`openclaw_pipe.py`](../../openclaw_pipe.py).
4. Save, then toggle **Active** → **ON** and **Global** → **ON**.

Use the ID `openclaw_gateway` exactly. The Status Action and the installers look the Pipe up by that ID.

## Set the valves

Open the function's valves in **Admin Panel** → **Functions** and set:

| Valve | What to set |
|---|---|
| [`GATEWAY_URL`](../configuration.md#gateway_url) | OpenClaw Gateway address, `host:port`, no scheme (default: `localhost:18789`). It is resolved by the Open WebUI backend, not by your browser |
| [`GATEWAY_TOKEN`](../configuration.md#gateway_token) | Your Gateway API token |
| [`AGENT_ID`](../configuration.md#agent_id) | Which agent to route to (default: `main`) |
| [`DEVICE_IDENTITY`](../configuration.md#device_identity) | Leave empty. The Pipe generates an identity on first connect and keeps it in `STATE_DIR`. Set it only to import an identity that is already approved |
| [`STATE_DIR`](../configuration.md#state_dir) | A persistent, writable directory inside the Open WebUI container (default: `/data/openclaw-bridge`). See [restart-safe state](./pairing.md#restart-safe-state) |
| [`ENABLE_FILE_SERVER`](../configuration.md#enable_file_server) | `True` (media support; the default) |
| [`USE_OWUI_FILES`](../configuration.md#use_owui_files) | `True` (native Open WebUI Files API media support; the default) |
| [`OWUI_BASE_URL`](../configuration.md#owui_base_url) | Open WebUI base URL reachable from the Open WebUI backend (default: `http://127.0.0.1:8080`, right for a standard single-container install) |
| [`SEND_STOP_ON_CANCEL`](../configuration.md#send_stop_on_cancel) | `True` (workaround for `chat.abort` not stopping active tool subprocesses; the default) |

Only `GATEWAY_URL` and `GATEWAY_TOKEN` usually need a change. These are the same valves the installer sets. Every other valve, with its default: [Configuration](../configuration.md).

## Approve the device

The first time the Pipe connects, the Gateway holds the new device until you approve it. Until then a chat answers **pairing required**. On the Gateway host:

```bash
openclaw devices list
openclaw devices approve <request-id>
```

If no request is pending yet, send one message in a chat first: the Pipe connects on first use. More in [device pairing](./pairing.md).

## Start chatting

Reload Open WebUI, choose **OpenClaw · Default** (or any discovered model) and start chatting. The reply should stream in token by token.

## The Status Action and the thinking filter, by hand

Both are optional and install the same way, as their own functions:

- [Status button](./status-action.md#install-by-hand): an **Action** with ID `openclaw_status_action`, whose five connection valves must match the Pipe's.
- [Thinking control](./thinking-filter.md#install-by-hand): a **Filter** with ID `openclaw_thinking`, attached per model.

## Next

- [Agent skills](./agent-skills.md): teach your agent the two conventions the integration listens for.
- [Update and uninstall](./update-uninstall.md#update-by-hand): updating by hand, and removing it.
