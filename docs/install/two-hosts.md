# Two hosts

Open WebUI and the OpenClaw Gateway can run on different machines. The install is the same; this page says which part runs where, which address each setting needs, and what you do on the Gateway host.

Applies to: Pipe, Action

Valves: [`GATEWAY_URL`](../configuration.md#gateway_url), [`OWUI_BASE_URL`](../configuration.md#owui_base_url), [`ENABLE_FILE_SERVER`](../configuration.md#enable_file_server), [`FILE_SERVER_BASE_URL`](../configuration.md#file_server_base_url), [`OWNER_USERS`](../configuration.md#owner_users)

## What runs where

| Piece | Runs on |
|---|---|
| The Pipe, the Status Action, the thinking filter | Inside the Open WebUI backend |
| The agent, its skills, the `openclaw` CLI, the `claude-cli-usage` and `claude-login` plugins | The Gateway host |
| `install.py`, `install_action.py`, `install_filter.py` | Any machine that can reach Open WebUI's URL |

The installers only talk to Open WebUI's REST API. They never connect to the Gateway; the Pipe does, from inside Open WebUI. So the one network path the integration needs is **from the Open WebUI backend to the Gateway**, on the Gateway's port (`18789` by default).

## The Gateway address

Set [`GATEWAY_URL`](../configuration.md#gateway_url) (installer: `GATEWAY_URL` or `--gateway-url`) to the Gateway as `host:port`, for example `192.168.1.10:18789`. A Gateway behind a TLS proxy takes a `wss://` prefix, see [below](#gateway-behind-a-tls-proxy).

It is resolved by the Open WebUI backend, which is not necessarily the same network as your browser, nor as the machine you run the installer on. The default, `localhost:18789`, only works when the Gateway runs on the same host as the Open WebUI backend.

If the Pipe can't reach that address, a chat shows an `**Error:**` line instead of a reply, and the model picker shows a built-in example list ("Gateway not reached") instead of your models. Check `GATEWAY_URL` and `GATEWAY_TOKEN`, and that the Gateway accepts connections on that address from the Open WebUI host.

### Open WebUI in a container, Gateway on the host

If Open WebUI runs in Docker and the Gateway runs on the machine around it, `localhost` and `127.0.0.1` in `GATEWAY_URL` point at the container itself, so the connection is refused even though the Gateway answers in your browser. The Pipe's error says so when it sees it runs in a container.

1. Start the Open WebUI container with `--add-host=host.docker.internal:host-gateway` (Docker Desktop on macOS and Windows already provides the name).
2. Set `GATEWAY_URL` to `host.docker.internal:18789`, or to the host's LAN address.
3. Set `gateway.bind` to `lan` in the OpenClaw config and restart the Gateway. The default, `loopback`, accepts connections only from the host itself, and a container is not the host.

### Gateway behind a TLS proxy

If a proxy terminates TLS in front of the Gateway (for example `tailscale serve`, or a reverse proxy), give `GATEWAY_URL` a `wss://` prefix: `wss://openclaw.example.ts.net`. Without a port, `wss://` means 443; add one (`wss://host:8443`) when the proxy listens elsewhere. `https://` works the same way.

The Pipe checks the proxy's certificate against the Open WebUI container's trusted CAs, so a public certificate (Let's Encrypt, or the ones Tailscale issues) works as it is. A self-signed one needs its CA in the container, for example through `SSL_CERT_FILE`.

A `ws://` or `http://` prefix, or none, connects without TLS. The Gateway's HTTP media is fetched over the same scheme, `https://` for `wss://`.

## Open WebUI's own address

The installer asks for the Open WebUI URL as you reach it (`OWUI_URL`), and by default also writes it into the Pipe's [`OWUI_BASE_URL`](../configuration.md#owui_base_url) valve. The Pipe uses that valve to call Open WebUI's own API (to attach images and files), and it is resolved by the Open WebUI backend, not by your browser.

If the address you use doesn't work from inside the Open WebUI container, set `OWUI_API_BASE_URL` (or `--owui-api-base-url`) to one that does. The valve's own default, `http://127.0.0.1:8080`, is right for a standard single-container install.

## Install

1. **On any machine that can reach Open WebUI,** run the installer as in the [quick start](./quickstart.md), with the Gateway address from above. In the repo checkout:

   ```bash
   uv run install.py install --wizard
   ```

   The first run ends with **pairing required** in the smoke test. That is expected: the installer can't approve the device from here.

2. **On the Gateway host,** approve the device:

   ```bash
   openclaw devices list
   openclaw devices approve <request-id>
   ```

3. **Back on the first machine,** re-run the smoke test, in the repo checkout:

   ```bash
   uv run install.py healthcheck
   ```

   Look for `✓ Smoke test passed`. Off the Gateway host, the status block shows `"device_paired": false` and the command exits 1 even when everything works, because that check asks the local `openclaw` CLI. The smoke test is the real answer.

If you run the installer on the Gateway host instead, it approves the device for you, as long as it can reach Open WebUI's URL from there.

## On the Gateway host

These belong where the agent runs, and need a clone of this repo there:

- **Agent skills:** in the repo checkout on the Gateway host, `uv run install.py skills`. It needs no Open WebUI credentials. Run anywhere else, it installs nothing and prints the commands to run on the Gateway host. See [agent skills](./agent-skills.md).
- **Rate limits from a Claude CLI login:** the optional `claude-cli-usage` plugin. See [Status button](./status-action.md#rate-limits-from-a-claude-cli-login).

## Images and files from the agent

The agent sends a file by emitting `MEDIA:<bare-filename>`, and the file must exist in the Pipe's media directory, `/tmp/openclaw-pipe-media`, inside the Open WebUI container. OpenClaw and Open WebUI usually share no filesystem, so a path that is valid where the agent runs means nothing to the Pipe.

When [`ENABLE_FILE_SERVER`](../configuration.md#enable_file_server) is on (the default), the Pipe runs a small HTTP server on port `18791` of the Open WebUI host that serves that directory and accepts an HTTP `PUT` or `POST` of a file to `/<bare-filename>`. That is how a file from another machine can reach it. The server has no authentication: read [Security](../security.md) before you open port 18791 to the Gateway host, and keep it off untrusted networks.

[`FILE_SERVER_BASE_URL`](../configuration.md#file_server_base_url) is a different thing: the address the **browser** uses for the legacy media fallback (default `http://localhost:18791`). It only matters when uploads to Open WebUI's Files API are off or fail.

## Other users and a second device

If you set [`OWNER_USERS`](../configuration.md#owner_users), the Pipe opens a second connection with its own device for everyone else. On a loopback Gateway it pairs silently; on a remote one, approve it like the first, on the Gateway host. See [device pairing](./pairing.md#a-second-device-for-other-users).
