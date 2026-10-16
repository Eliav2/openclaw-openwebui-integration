#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "click>=8.1",
#     "rich>=13",
# ]
# ///
"""
Install/update the OpenClaw Status companion Action Function in Open WebUI.

A spec for install.py's installer core (ELI-146) plus the two steps only the
Action has: it copies its five connection valves from the Pipe's (it reuses
the Pipe's paired device identity, so it never asks for them or pairs again),
and its smoke test triggers it over the actions HTTP endpoint. It must be run
from a clone: it imports install.py.

Usage:
    uv run install_action.py install
    uv run install_action.py status
"""

from __future__ import annotations

import time

from install import FUNCTION_ID as PIPE_FUNCTION_ID
from install import ROOT, FunctionSpec, fail, function_cli, get_valves, info, post_valves

FUNCTION_ID = "openclaw_status_action"
# Valves mirrored verbatim from the Pipe's own valves.
VALVE_KEYS = ("GATEWAY_URL", "GATEWAY_TOKEN", "DEVICE_IDENTITY", "STATE_DIR", "AGENT_ID")


def mirror_pipe_valves(client) -> dict:
    """Copy the connection valves from the Pipe. The Pipe must be installed
    first: the Action is its companion, not standalone."""
    pipe_valves = get_valves(client, PIPE_FUNCTION_ID)
    if not pipe_valves or not pipe_valves.get("GATEWAY_TOKEN"):
        raise SystemExit(
            f"Pipe function ({PIPE_FUNCTION_ID}) valves not found or incomplete -- "
            "install the Pipe first (see install.py). The Action is a companion "
            "to it and reuses its device identity/connection."
        )
    desired = {k: pipe_valves[k] for k in VALVE_KEYS if k in pipe_valves}
    current = get_valves(client, FUNCTION_ID)
    # Open WebUI replaces the whole valve set with what is posted, so the
    # Action's own valves (CONTROL_UI_URL) go along unchanged or they reset.
    changed = post_valves(client, FUNCTION_ID, current, {**current, **desired})
    info(f"Mirrored valves from Pipe: {', '.join(changed)}" if changed
         else "Valves already mirror the Pipe's")
    return desired


def smoke_test(client) -> bool:
    """Trigger the Action for a synthetic chat. It has no OpenClaw session,
    so a non-"ok" status is expected; what counts is the Action's own
    response shape rather than an exception or an HTML error page."""
    stamp = int(time.time())
    status, payload = client.request(
        "POST", f"/api/chat/actions/{FUNCTION_ID}",
        {"model": f"{PIPE_FUNCTION_ID}.default", "chat_id": f"install-smoke-{stamp}",
         "id": f"install-smoke-msg-{stamp}", "session_id": "install-smoke"},
        timeout=30,
    )
    if status != 200 or not isinstance(payload, dict) or "status" not in payload:
        fail(f"Smoke test: unexpected response: {payload}")
        return False
    info(f"Smoke test reached the Action (status={payload.get('status')!r})")
    return True


def valves_match_pipe(client) -> dict:
    mine, pipe = get_valves(client, FUNCTION_ID), get_valves(client, PIPE_FUNCTION_ID)
    return {"valves_match_pipe": all(mine.get(k) == pipe.get(k) for k in VALVE_KEYS)}


SPEC = FunctionSpec(
    FUNCTION_ID, "OpenClaw Status", "action", "Action",
    "Live OpenClaw session/usage status -- companion Action to the Gateway Pipe",
    artifact=ROOT / "openclaw_status_action.py", build_artifact="ACTION",
    valves=mirror_pipe_valves, smoke=smoke_test, status_checks=valves_match_pipe,
)
cli = function_cli(SPEC)

if __name__ == "__main__":
    cli()
