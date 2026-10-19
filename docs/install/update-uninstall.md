# Update and uninstall

Update by re-running the installers: they change the code in place, keep the device identity and back up what was there. Uninstalling is done in Open WebUI's admin panel, plus revoking the device on the Gateway if you want it gone.

Applies to: Pipe, Action, Filter

Valves: [`DEVICE_IDENTITY`](../configuration.md#device_identity), [`STATE_DIR`](../configuration.md#state_dir)

## Update

In the repo checkout:

```bash
git pull
uv run install.py install
uv run install_action.py install    # if you use the Status Action
uv run install_filter.py install    # if you use the thinking filter
```

Pass the same env vars or flags as on the first install (`OWUI_EMAIL`, `OWUI_PASSWORD`, `GATEWAY_TOKEN`, and `GATEWAY_URL`, `AGENT_ID`, `--state-dir` if you changed them), or add `--wizard`.

Reinstalling updates in place: the function is never deleted and recreated, so `DEVICE_IDENTITY` survives and you never re-approve the device. The valves you set in the admin panel are kept, except the ones the installer manages: those are written again from your env vars, flags and defaults. See [valves the installer sets](./installer.md#valves-the-installer-sets).

Run `install_action.py` after `install.py`, so the Action copies the Pipe's current valves.

You can update while a reply is streaming: it finishes in its own message, once. See [redeploys](../features/reliability.md#redeploys).

Updated the Pipe and use the thinking filter? Reload or re-save the filter once the Pipe has chatted with a new model, so its dropdown picks up the new levels; see [thinking control](./thinking-filter.md#show-your-models-real-levels).

Updated the skills too? See [agent skills](./agent-skills.md#update-them).

## Update without a clone

On any machine with `uv`, run the no-clone command again; it fetches the current `openclaw_pipe.py` from `main`:

```bash
uv run https://raw.githubusercontent.com/Eliav2/openclaw-openwebui-integration/main/install.py install --wizard
```

Point `OPENCLAW_PIPE_ARTIFACT_URL` at a pinned tag or a mirror to install something other than `main`. The Action and the filter installers need a clone.

## Update by hand

Paste the new `openclaw_pipe.py` (or `openclaw_status_action.py`, `openclaw_thinking_filter.py`) over the function's code in **Admin Panel** → **Functions** and save. Then check its valves: Open WebUI can drop valves during an update, which is why the installer restores them for you.

## Backups

Every install writes the previous function to `backups/` first, next to the installer script:

| Installer | Files |
|---|---|
| `install.py` | `openclaw_gateway-<YYYYmmdd-HHMMSS>.json` (the function) and `openclaw_gateway-<YYYYmmdd-HHMMSS>-valves.json` (its valves) |
| `install_action.py` | `openclaw_status_action-<YYYYmmdd-HHMMSS>.json` |
| `install_filter.py` | `openclaw_thinking-<YYYYmmdd-HHMMSS>.json` |

The function backup is Open WebUI's own record of the function; its `content` field is the old code. The valves backup holds every valve, including `GATEWAY_TOKEN` and `DEVICE_IDENTITY`, a private key: the `install.py` backups are written readable by you only. `backups/` and `.pipe_device_identity.json` are gitignored. Don't commit or share either; see [Security](../security.md).

With the no-clone form, `backups/` is created next to uv's temporary copy of the script, which is hard to find. Run from a clone if you want backups you can use.

## Roll back

To roll back a bad upgrade, restore from `backups/` rather than deleting:

- **Code:** open the function in **Admin Panel** → **Functions**, replace its code with the `content` field of the backup, and save. Or, in the repo checkout, check out the earlier version and re-run `uv run install.py install`: it deploys what is checked out, in place.
- **Valves:** set them from the `-valves.json` file in the function's valves.

The device identity is not affected by a rollback, so there is nothing to re-approve.

## Uninstall

There's no `uninstall` subcommand; removal is a UI action. In Open WebUI go to **Admin Panel** → **Functions** and delete `openclaw_gateway` (and `openclaw_status_action` and/or `openclaw_thinking`, if you installed them).

Deleting the functions does not touch `STATE_DIR`. Remove that directory too, inside the Open WebUI container, if you want the device identity gone. It holds `identity.json`, `device-token.json` and, if you used `OWNER_USERS`, `identity-restricted.json` and `device-token-restricted.json`. If the Pipe ever fell back to `/tmp/openclaw-bridge`, that holds a copy too.

In the repo checkout, `.pipe_device_identity.json` and `backups/` also hold the identity. Delete them as well.

## Revoke the device

On the Gateway host:

```bash
openclaw devices list
openclaw devices remove <id>
```

If you used `OWNER_USERS`, there is a second device for the other users; remove it too.

To pair a fresh device afterwards, clear the identity everywhere it is kept (the `DEVICE_IDENTITY` valve, `STATE_DIR/identity.json` with `STATE_DIR/device-token.json`, and `./.pipe_device_identity.json`), run the installer again and approve the new request: see [device pairing](./pairing.md). If any one of them still holds the old identity, the installer or the Pipe reuses it.
