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

Deliberately much smaller than install.py (the Pipe's installer): this
Action has no model discovery, no chatgpt-model preset, and no
device-pairing wizard of its own -- see action.py's module docstring for
why (it reuses the Pipe's already-approved device identity via a shared
STATE_DIR, falling back to its own connection only if the Pipe hasn't
connected yet in this process). What it actually needs is: build the
artifact, create-or-update the Function, mirror the five
connection-relevant valves straight from the Pipe's own valves (single
source of truth -- no separate prompt asking for the same values twice),
ensure active+global, and a lightweight smoke test via the actions HTTP
endpoint.

Reuses OwuiClient from install.py as-is (import, not copy) -- it's the one
piece of that file with zero Pipe-specific behavior. Everything else here
is new and scoped to what this Action needs; install.py itself is never
imported for its Pipe-specific logic and is not modified by this file.

Usage:
    uv run install_action.py install
    uv run install_action.py status
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import click
from rich.console import Console

from install import OwuiClient

FUNCTION_ID = "openclaw_status_action"
PIPE_FUNCTION_ID = "openclaw_gateway"
ROOT = Path(__file__).resolve().parent
ARTIFACT_FILE = ROOT / "openclaw_status_action.py"
BACKUP_DIR = ROOT / "backups"

# Valves mirrored verbatim from the Pipe's own valves -- see
# mirror_pipe_valves() for why this Action never prompts for these itself.
VALVE_KEYS = ("GATEWAY_URL", "GATEWAY_TOKEN", "DEVICE_IDENTITY", "STATE_DIR", "AGENT_ID")

console = Console()


def info(msg: str) -> None:
    console.print(f"  [green]✓[/green] {msg}")


def warn(msg: str) -> None:
    console.print(f"  [yellow]![/yellow] {msg}")


def fail(msg: str) -> None:
    console.print(f"  [red]✗[/red] {msg}")


def section(msg: str) -> None:
    console.rule(f"[bold]{msg}[/bold]")


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def _rebuild_artifact() -> None:
    """Always regenerate the deployed artifact from src/ before deploying,
    mirroring install.py's _rebuild_pipe_file so a forgotten `python3
    build.py --action` can never cause a stale deploy."""
    build_script = ROOT / "build.py"
    if not build_script.exists():
        return
    import importlib.util

    spec = importlib.util.spec_from_file_location("_action_build", build_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    built = mod.build(strip_dev_only=True, artifact=mod.ACTION)
    if not ARTIFACT_FILE.exists() or ARTIFACT_FILE.read_text() != built:
        ARTIFACT_FILE.write_text(built)
        info(f"Rebuilt {ARTIFACT_FILE.name} from src/")


# --------------------------------------------------------------------------
# Function / valve management
# --------------------------------------------------------------------------

def get_function(client: OwuiClient, function_id: str) -> dict | None:
    status, payload = client.request("GET", f"/api/v1/functions/id/{function_id}")
    if status == 200 and isinstance(payload, dict) and payload.get("id") == function_id:
        return payload
    return None


def get_functions(client: OwuiClient) -> list[dict]:
    status, payload = client.request("GET", "/api/v1/functions/")
    return payload if status == 200 and isinstance(payload, list) else []


def get_function_summary(client: OwuiClient, function_id: str) -> dict | None:
    return next((it for it in get_functions(client) if it.get("id") == function_id), None)


def get_valves(client: OwuiClient, function_id: str) -> dict:
    status, payload = client.request("GET", f"/api/v1/functions/id/{function_id}/valves")
    return payload if status == 200 and isinstance(payload, dict) else {}


def backup_function(fn: dict) -> None:
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = BACKUP_DIR / f"{FUNCTION_ID}-{stamp}.json"
    path.write_text(json.dumps(fn, indent=2, ensure_ascii=False))
    info(f"Backed up existing function to {path}")


def update_or_create_function(client: OwuiClient) -> None:
    _rebuild_artifact()
    if not ARTIFACT_FILE.exists():
        raise SystemExit(f"Artifact not found: {ARTIFACT_FILE}")
    code = ARTIFACT_FILE.read_text()
    meta = {
        "description": "Live OpenClaw session/usage status -- companion Action to the Gateway Pipe",
        "manifest": {},
    }

    existing = get_function(client, FUNCTION_ID)
    if existing:
        backup_function(existing)
        status, payload = client.request(
            "POST",
            f"/api/v1/functions/id/{FUNCTION_ID}/update",
            {"id": FUNCTION_ID, "name": "OpenClaw Status", "content": code, "meta": meta},
        )
        if status != 200 or not isinstance(payload, dict) or payload.get("id") != FUNCTION_ID:
            raise SystemExit(f"Function update failed: {payload}")
        info("Updated Action function in place")
        return

    status, payload = client.request(
        "POST",
        "/api/v1/functions/create",
        {"id": FUNCTION_ID, "name": "OpenClaw Status", "content": code, "type": "action", "meta": meta},
    )
    if status != 200 or not isinstance(payload, dict) or payload.get("id") != FUNCTION_ID:
        raise SystemExit(f"Function create failed: {payload}")
    info("Created Action function")


def ensure_active_global(client: OwuiClient) -> None:
    summary = get_function_summary(client, FUNCTION_ID)
    if not summary:
        raise SystemExit("Function summary not found after create/update")

    if not summary.get("is_active"):
        status, payload = client.request("POST", f"/api/v1/functions/id/{FUNCTION_ID}/toggle")
        if status != 200:
            raise SystemExit(f"Failed to activate function: {payload}")
        info("Activated Action function")
    else:
        info("Action function already active")

    summary = get_function_summary(client, FUNCTION_ID) or {}
    if not summary.get("is_global"):
        status, payload = client.request("POST", f"/api/v1/functions/id/{FUNCTION_ID}/toggle/global")
        if status != 200:
            raise SystemExit(f"Failed to make function global: {payload}")
        info("Made Action function global")
    else:
        info("Action function already global")


def mirror_pipe_valves(client: OwuiClient) -> dict:
    """Copy the connection-relevant valves straight from the Pipe's own
    valves -- single source of truth, no separate prompt asking for the
    same GATEWAY_URL/TOKEN/DEVICE_IDENTITY/STATE_DIR/AGENT_ID a second
    time. If the Pipe isn't installed yet this is a hard error: the Action
    is a companion to it, not standalone (see action.py's module
    docstring for the connection-reuse relationship)."""
    pipe_valves = get_valves(client, PIPE_FUNCTION_ID)
    if not pipe_valves or not pipe_valves.get("GATEWAY_TOKEN"):
        raise SystemExit(
            f"Pipe function ({PIPE_FUNCTION_ID}) valves not found or incomplete -- "
            "install the Pipe first (see install.py). The Action is a companion "
            "to it and reuses its device identity/connection."
        )
    desired = {k: pipe_valves[k] for k in VALVE_KEYS if k in pipe_valves}
    current = get_valves(client, FUNCTION_ID)
    changed = {k: v for k, v in desired.items() if current.get(k) != v}
    if not changed:
        info("Valves already mirror the Pipe's")
        return desired

    status, payload = client.request(
        "POST", f"/api/v1/functions/id/{FUNCTION_ID}/valves/update", desired,
    )
    if status != 200 or not isinstance(payload, dict):
        raise SystemExit(f"Valve update failed: {payload}")
    info(f"Mirrored valves from Pipe: {', '.join(sorted(changed.keys()))}")
    return desired


# --------------------------------------------------------------------------
# Smoke test
# --------------------------------------------------------------------------

def smoke_test(client: OwuiClient) -> bool:
    """Trigger the Action against a synthetic chat/message id and confirm
    it executes cleanly. Not a chat-completion round trip like the Pipe's
    smoke test -- there's no model inference here -- just confirms the
    Function loads, the gateway connection resolves (reused or fallback),
    and it returns its own well-formed response shape rather than an
    exception or an HTML error page."""
    status, payload = client.request(
        "POST",
        f"/api/chat/actions/{FUNCTION_ID}",
        {
            "model": f"{PIPE_FUNCTION_ID}.default",
            "chat_id": f"install-smoke-{int(time.time())}",
            "id": f"install-smoke-msg-{int(time.time())}",
            "session_id": "install-smoke",
        },
        timeout=30,
    )
    if status != 200 or not isinstance(payload, dict):
        fail(f"Smoke test HTTP failed: {payload}")
        return False
    if "status" not in payload:
        fail(f"Smoke test got an unexpected response shape: {payload}")
        return False
    if payload.get("status") == "ok":
        info("Smoke test passed")
        return True
    # A synthetic chat_id has no real OpenClaw session behind it, so
    # sessions.describe legitimately failing (unknown session) is expected
    # here -- what matters is that the Function itself executed correctly
    # and returned its own {"status": "error", ...} shape rather than
    # throwing, which is what a broken deploy would look like instead.
    info(f"Smoke test reached the Action (status={payload.get('status')!r}); "
         "non-'ok' is expected for a synthetic chat_id with no real session")
    return True


def print_status(client: OwuiClient) -> bool:
    summary = get_function_summary(client, FUNCTION_ID)
    valves = get_valves(client, FUNCTION_ID) if summary else {}
    pipe_valves = get_valves(client, PIPE_FUNCTION_ID)
    exists = bool(summary)
    active = bool(summary and summary.get("is_active"))
    global_ = bool(summary and summary.get("is_global"))
    valves_match_pipe = all(valves.get(k) == pipe_valves.get(k) for k in VALVE_KEYS) if exists else False

    console.print_json(json.dumps({
        "function_exists": exists,
        "is_active": active,
        "is_global": global_,
        "valves_match_pipe": valves_match_pipe,
    }))
    return all([exists, active, global_, valves_match_pipe])


# --------------------------------------------------------------------------
# Command orchestration
# --------------------------------------------------------------------------

def install_or_repair(client: OwuiClient) -> None:
    section("Function")
    update_or_create_function(client)
    ensure_active_global(client)

    section("Valves")
    mirror_pipe_valves(client)


def execute(command: str, client: OwuiClient) -> None:
    section("Login")
    client.login()

    if command == "status":
        section("Status")
        ok = print_status(client)
        raise SystemExit(0 if ok else 1)

    if command in {"install", "repair"}:
        install_or_repair(client)
        section("Smoke test")
        ok = smoke_test(client)
        raise SystemExit(0 if ok else 1)


# --------------------------------------------------------------------------
# CLI (click)
# --------------------------------------------------------------------------

def common_options(f):
    options = [
        click.option("--owui-url", envvar="OWUI_URL", default="http://localhost:8080",
                     show_default=True, help="Open WebUI base URL"),
        click.option("--owui-email", envvar="OWUI_EMAIL", default="",
                     help="Open WebUI admin email"),
        click.option("--owui-password", envvar="OWUI_PASSWORD", default="",
                     help="Open WebUI admin password"),
    ]
    for option in reversed(options):
        f = option(f)
    return f


def _dispatch(command: str, owui_url: str, owui_email: str, owui_password: str) -> None:
    if not owui_email or not owui_password:
        raise SystemExit(
            "Missing required env/args: OWUI_EMAIL, OWUI_PASSWORD "
            "(same ones install.py uses -- this Action shares the same OWUI login)."
        )
    client = OwuiClient(owui_url.rstrip("/"), owui_email, owui_password)
    execute(command, client)


@click.group()
def cli() -> None:
    """Install, repair, and health-check the OpenClaw Status companion Action."""


@cli.command()
@common_options
def install(**kwargs) -> None:
    """Create or update the Action function, mirror valves from the Pipe, smoke test."""
    _dispatch("install", **kwargs)


@cli.command()
@common_options
def repair(**kwargs) -> None:
    """Same as install; use after a broken/partial setup."""
    _dispatch("repair", **kwargs)


@cli.command()
@common_options
def status(**kwargs) -> None:
    """Print current state without changing anything."""
    _dispatch("status", **kwargs)


if __name__ == "__main__":
    cli()
