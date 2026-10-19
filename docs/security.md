# Security

What to protect when you run this integration: the secrets it keeps, the optional media file server on port 18791, the agent privileges every chat user gets, and the scripts it runs in your browser.

Applies to: Pipe, Action
Valves: `GATEWAY_TOKEN`, `DEVICE_IDENTITY`, `STATE_DIR`, `OWUI_API_KEY`, `ENABLE_FILE_SERVER`, `USE_OWUI_FILES`, `FILE_SERVER_BASE_URL`, `AGENT_ID`, `OWNER_USERS`, `COMPOSER_STATUS_BUTTON`

## Secrets

Treat these as secrets:

- **[`GATEWAY_TOKEN`](./configuration.md#gateway_token)** is your Gateway API token (`gateway.auth.token` in your OpenClaw config).
- **[`DEVICE_IDENTITY`](./configuration.md#device_identity)** holds an Ed25519 **private key**: the device identity the Gateway approved. Whoever has it connects as that approved device.
- **[`OWUI_API_KEY`](./configuration.md#owui_api_key)**, if you set it, is an Open WebUI API key.

Where copies of them end up:

| Where | What | Notes |
|---|---|---|
| The function's valves in Open WebUI | All of the above | Anyone who can open the function's valves in the admin panel can read them. The Status Action keeps its own copy of the shared valves. |
| `STATE_DIR` (default `/data/openclaw-bridge`) | `identity.json` (the key pair), `device-token.json` (the token the Gateway issued), and with `OWNER_USERS` set, `identity-restricted.json` and its token | Written with mode `0600` in a `0700` directory, inside the Open WebUI container. If `STATE_DIR` is not writable, they go to `/tmp/openclaw-bridge` instead. |
| `.pipe_device_identity.json` in your repo checkout | The device identity | Written by `install.py` (mode `0600`) so a reinstall keeps the same device. Gitignored. |
| `backups/` in your repo checkout | The previous function and its valves, from every install | Mode `0600`, gitignored. The valves files hold the token and the identity. |

Do not commit, paste or share any of them. Leave them out of bug reports.

A secret the agent requests with OpenClaw's `secrets` tool is entered in a masked dialog and goes from it straight to the Gateway's secret store: the Pipe never writes, emits or logs it, and only an owner's chat is asked. See [Secrets the agent requests](./features/questions.md#secrets-the-agent-requests).

If you think a device identity leaked, revoke it and pair a new one. On the Gateway host:

```bash
openclaw devices list
openclaw devices remove <id>
```

Then delete `STATE_DIR/identity.json` and `STATE_DIR/device-token.json`, clear the `DEVICE_IDENTITY` valve, and run `uv run install.py install` again to pair a fresh device. [SECURITY.md](../SECURITY.md) has the same steps. More on pairing and identity: [Device pairing](./install/pairing.md).

## The media file server

When the agent sends a file (`MEDIA:<filename>`), the Pipe first uploads it through Open WebUI's own Files API ([`USE_OWUI_FILES`](./configuration.md#use_owui_files), on by default). That path is same-origin and uses Open WebUI's own login: the request's login token, or [`OWUI_API_KEY`](./configuration.md#owui_api_key) if set.

As a fallback, the Pipe also runs a small HTTP file server, and that server is **unauthenticated**. With [`ENABLE_FILE_SERVER`](./configuration.md#enable_file_server) on (the default), the Pipe starts it the first time it handles a chat in an Open WebUI process:

- it listens on port **18791** on all interfaces (`0.0.0.0`); the port is fixed;
- it serves the folder `/tmp/openclaw-pipe-media`, with `Access-Control-Allow-Origin: *` and no auth;
- anyone who can reach the port can list and read every file the agent has emitted;
- it also accepts uploads: a `PUT` or `POST` of a plain file name writes that file into the folder. This is how an agent on another host can hand the Pipe a file, and it means anyone who can reach the port can write files there too;
- a redeploy shuts down the previous server first, so a reinstall never leaves a stale one holding the port.

The browser loads fallback links from [`FILE_SERVER_BASE_URL`](./configuration.md#file_server_base_url) (default `http://localhost:18791`), so the fallback only works where your browser can reach that address.

To stay safe:

- keep port 18791 off untrusted networks (do not publish it from the container, or firewall it);
- or set `ENABLE_FILE_SERVER` to off if you do not need the fallback. If your agent runs on another host and puts files there with `PUT`, it then needs another way to place them in the Pipe's media folder, such as a shared volume. Every `install.py install` or `repair` turns it back on ([valves the installer sets](./install/installer.md#valves-the-installer-sets)), so set it off again after one.

How media works: [Chat features](./features/chat.md).

## The agent's privileges

The integration runs inside the Open WebUI backend and holds the Gateway credentials. It talks to the Gateway with the configured agent's privileges, on a connection that asks for the `operator.admin`, `operator.read` and `operator.write` scopes.

So **anyone who can chat with the Pipe can drive that agent**, with everything that agent may do: run commands, read and write files, use its tools. Scope [`AGENT_ID`](./configuration.md#agent_id) accordingly: point it at an agent whose tools and workspace you are happy for every Open WebUI user to use.

By default every Open WebUI user also acts as the Gateway's owner. If people other than you can sign in, set [`OWNER_USERS`](./configuration.md#owner_users): see [Owner users](./features/owner-users.md).

## Scripts in your browser

The Status dialog and the composer's Claw button are JavaScript that the Status Action and the Pipe send into your open Open WebUI page, through Open WebUI's `execute` event. That code runs unsandboxed in the page, with your session, and calls only your own Open WebUI (same origin). It is part of this repo, so you can read it in `src/openclaw_pipe_pkg/action.py` and `src/openclaw_pipe_pkg/composer.py`. Set [`COMPOSER_STATUS_BUTTON`](./configuration.md#composer_status_button) to off to stop delivering the composer button.

## Reporting a vulnerability

Report privately through GitHub, as [SECURITY.md](../SECURITY.md) describes. Please do not open a public issue for a security problem. SECURITY.md also lists the accepted risks above: reports that only restate them are not vulnerabilities, reports that bypass a stated mitigation are.
