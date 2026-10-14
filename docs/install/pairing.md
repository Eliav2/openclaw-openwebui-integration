The Pipe signs in to the Gateway as a device with its own Ed25519 key, and you approve that device once on the Gateway host. After that the identity and its token survive Open WebUI restarts, function reloads and reinstalls.

Applies to: Pipe, Action

Valves: [`DEVICE_IDENTITY`](../configuration.md#device_identity), [`STATE_DIR`](../configuration.md#state_dir)

## How pairing works

The WebSocket handshake is a full challenge/response against an Ed25519 key pair generated on first run. The first time a new key connects, the Gateway holds it as a pending pairing request. Once you approve it, the Gateway issues a device token, and the Pipe uses both from then on.

`DEVICE_IDENTITY` holds a private key. Treat it, and the files below, as secrets: see [Security](../security.md).

## Approve the device in the Gateway

A first install pairs a brand-new device, so the Gateway holds it pending until you approve it. Expect the installer to report **pairing required**; that is the normal first run, not a failure.

If the installer ran on the Gateway host, it approves the request for you: it finds the pending request for its own device with the local `openclaw` CLI. If it ran anywhere else, approve it yourself, on the Gateway host:

```bash
openclaw devices list
openclaw devices approve <request-id>
```

`openclaw devices list` shows pending requests and paired devices; take `<request-id>` from the pending one. Then, in the repo checkout, re-run the smoke test:

```bash
uv run install.py healthcheck
```

If the smoke test still reports **pairing required**, the approval hasn't landed yet. Approve the device, then re-run `healthcheck`.

`uv run install.py repair` also approves the pending request when it runs on the Gateway host.

## The identity is permanent

The installer prefers the existing `DEVICE_IDENTITY` valve, mirrors it into `./.pipe_device_identity.json` next to `install.py`, and the Pipe persists it inside `STATE_DIR` on first run. Valve updates are in place, so approval lasts across reinstalls and restarts.

The Status Action reuses the same identity: it borrows the Pipe's live connection, or falls back to the Pipe's identity in `STATE_DIR`. That only works when its `DEVICE_IDENTITY` and `STATE_DIR` valves match the Pipe's; `install_action.py` copies them for you. See [Status button](./status-action.md).

## Restart-safe state

The Pipe loads its identity in this order:

1. `STATE_DIR/identity.json` (default: `/data/openclaw-bridge/identity.json`)
2. The `DEVICE_IDENTITY` valve, which it then persists into `STATE_DIR`
3. Generate a new identity, only if neither exists

When it generates a new identity, the Pipe also prints it to the Open WebUI backend log as a `DEVICE_IDENTITY=...` line, so you can paste it into the valve as a second copy. That line contains the private key.

The Gateway device token is saved to `STATE_DIR/device-token.json` after a successful connection. This keeps Open WebUI restarts and Pipe reloads from creating new devices or requiring repeated approvals.

`STATE_DIR` is a directory inside the Open WebUI container. The Pipe uses its `STATE_DIR` valve, or the `OPENCLAW_BRIDGE_STATE_DIR` environment variable when the valve is empty, or `/data/openclaw-bridge`. The installer sets the valve from its `--state-dir` flag.

## If STATE_DIR isn't writable

The Pipe falls back to `/tmp/openclaw-bridge` and logs `STATE_DIR unavailable`, rather than failing. `/tmp` usually does not survive a container restart, so the identity and device token are lost and you get a fresh pairing prompt every time.

If Open WebUI keeps asking you to re-approve the device:

1. Grep the Open WebUI backend log for `STATE_DIR unavailable`.
2. Point `STATE_DIR` at a real persistent volume. `/data` is not mounted in every Open WebUI deployment.
3. If you use the Status Action, set its `STATE_DIR` to the same value (or re-run `uv run install_action.py install`).

## A second device for other users

If you set [`OWNER_USERS`](../configuration.md#owner_users), chats from users not on that list reach the Gateway over a second connection with its own device identity, `STATE_DIR/identity-restricted.json`, and its own token file, `STATE_DIR/device-token-restricted.json`. On a loopback Gateway the second device pairs silently; on a remote one, approve it like the first, with `openclaw devices list` and `openclaw devices approve <request-id>` on the Gateway host. The `DEVICE_IDENTITY` valve only ever holds the main identity. More in [owner users](../features/owner-users.md).

## Starting over with a new device

To revoke the device and pair a fresh one, see [update and uninstall](./update-uninstall.md#revoke-the-device).
