# Security Policy

## Reporting a vulnerability

Report privately through GitHub's
[Report a vulnerability](https://github.com/Eliav2/openclaw-openwebui-integration/security/advisories/new)
form. Please do not open a public issue for a security problem.

Include what you can: affected version, what an attacker gains, and the
smallest reproduction you have. Expect a first reply within a week. This is a
hobby project maintained by one person, so there is no SLA beyond best effort.

## Supported versions

Only the tip of `main` is supported. Fixes ship there; there are no backports.

## What is in scope

The two Open WebUI functions (`openclaw_pipe.py`, `openclaw_status_action.py`),
their source fragments under `src/openclaw_pipe_pkg/`, and the installers
(`install.py`, `install_action.py`).

Out of scope: Open WebUI itself, the OpenClaw Gateway, and the agent you route
to. Report those upstream.

## Handling secrets

Two values this project touches are secrets:

- **`DEVICE_IDENTITY`** is an Ed25519 **private key**. `install.py` mirrors it
  to `./.pipe_device_identity.json` and backs valves up to `backups/`. Both
  paths are gitignored. Do not commit or paste either.
- **`GATEWAY_TOKEN`** is your Gateway API token.

If you think you have leaked a device identity, remove the device on the
Gateway (`openclaw devices list` / `openclaw devices remove <id>`), delete
`STATE_DIR/identity.json` and `STATE_DIR/device-token.json`, clear the
`DEVICE_IDENTITY` valve, and re-run `install.py install` to pair a fresh one.

## Known accepted risks

- **The legacy media file server is unauthenticated by design.** With
  `ENABLE_FILE_SERVER` on (the default), the integration serves
  `/tmp/openclaw-pipe-media` on port **18791**, bound to `0.0.0.0`, with
  `Access-Control-Allow-Origin: *` and no auth. Anyone who can reach that port
  reads any file the agent has emitted. It is a fallback for the Open WebUI
  Files API path (`USE_OWUI_FILES`, on by default, same-origin). Keep 18791 off
  untrusted networks, or set `ENABLE_FILE_SERVER=False`.
- **Anyone who can chat with the pipe can drive the configured agent** with
  that agent's full privileges, because the pipe runs inside the Open WebUI
  backend and holds the Gateway credentials. Scope `AGENT_ID` accordingly.

Reports that only restate these two are not vulnerabilities. Reports that
bypass a stated mitigation are.
