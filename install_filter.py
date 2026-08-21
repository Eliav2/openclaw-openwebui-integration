#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "click>=8.1",
#     "rich>=13",
# ]
# ///
"""
Install/update the OpenClaw Thinking companion Filter Function in Open WebUI.

Mirrors install_action.py's shape (same OwuiClient, same login/CLI plumbing)
but is smaller still: the Filter has no connection valves to mirror from the
Pipe (see thinking_filter.py's module docstring -- it only ever writes
``reasoning_effort`` and reads the ladder cache off disk) and, unlike the
Action, must NOT be forced global. Open WebUI's own Filter docs and this
project's install instructions (see src/frontmatter-filter.txt) are explicit
that a global filter would write ``reasoning_effort`` into every chat with
every model, including non-OpenClaw ones -- so this installer only ensures
the Function exists, is up to date, and is active, and leaves attaching it
to specific OpenClaw models as a manual step (Workspace > Models > (model) >
Filters), same as the README already documents doing by hand.

Reuses OwuiClient from install.py as-is (import, not copy), same as
install_action.py does. install.py itself is never imported for its
Pipe-specific logic and is not modified by this file.

Usage:
    uv run install_filter.py install
    uv run install_filter.py status
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import click
from rich.console import Console

from install import OwuiClient

FUNCTION_ID = "openclaw_thinking"
ROOT = Path(__file__).resolve().parent
ARTIFACT_FILE = ROOT / "openclaw_thinking_filter.py"
BACKUP_DIR = ROOT / "backups"

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
    mirroring install_action.py's _rebuild_artifact so a forgotten `python3
    build.py --filter` can never cause a stale deploy."""
    build_script = ROOT / "build.py"
    if not build_script.exists():
        return
    import importlib.util

    spec = importlib.util.spec_from_file_location("_filter_build", build_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    built = mod.build(strip_dev_only=True, artifact=mod.FILTER)
    if not ARTIFACT_FILE.exists() or ARTIFACT_FILE.read_text() != built:
        ARTIFACT_FILE.write_text(built)
        info(f"Rebuilt {ARTIFACT_FILE.name} from src/")


# --------------------------------------------------------------------------
# Function management
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


def backup_function(fn: dict) -> None:
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = BACKUP_DIR / f"{FUNCTION_ID}-{stamp}.json"
    path.write_text(json.dumps(fn, indent=2, ensure_ascii=False))
    info(f"Backed up existing function to {path}")


def update_or_create_function(client: OwuiClient) -> str:
    _rebuild_artifact()
    if not ARTIFACT_FILE.exists():
        raise SystemExit(f"Artifact not found: {ARTIFACT_FILE}")
    code = ARTIFACT_FILE.read_text()
    meta = {
        "description": (
            "Per-chat thinking / reasoning-effort control for the OpenClaw "
            "Gateway pipe -- companion Filter to the Gateway Pipe"
        ),
        "manifest": {},
    }

    existing = get_function(client, FUNCTION_ID)
    if existing:
        backup_function(existing)
        status, payload = client.request(
            "POST",
            f"/api/v1/functions/id/{FUNCTION_ID}/update",
            {"id": FUNCTION_ID, "name": "OpenClaw Thinking", "content": code, "meta": meta},
        )
        if status != 200 or not isinstance(payload, dict) or payload.get("id") != FUNCTION_ID:
            raise SystemExit(f"Function update failed: {payload}")
        info("Updated Filter function in place")
        return code

    status, payload = client.request(
        "POST",
        "/api/v1/functions/create",
        {"id": FUNCTION_ID, "name": "OpenClaw Thinking", "content": code, "type": "filter", "meta": meta},
    )
    if status != 200 or not isinstance(payload, dict) or payload.get("id") != FUNCTION_ID:
        raise SystemExit(f"Function create failed: {payload}")
    info("Created Filter function")
    return code


def ensure_active(client: OwuiClient) -> None:
    """Activate the Function if needed. Deliberately does not touch
    is_global: this Filter must stay attached per-model (see module
    docstring), never toggled global by an installer."""
    summary = get_function_summary(client, FUNCTION_ID)
    if not summary:
        raise SystemExit("Function summary not found after create/update")

    if not summary.get("is_active"):
        status, payload = client.request("POST", f"/api/v1/functions/id/{FUNCTION_ID}/toggle")
        if status != 200:
            raise SystemExit(f"Failed to activate function: {payload}")
        info("Activated Filter function")
    else:
        info("Filter function already active")

    if summary.get("is_global"):
        warn(
            "Filter is global -- it will write reasoning_effort into every chat "
            "with every model. Consider Admin Panel > Functions > toggle Global "
            "off, then attach it per-model instead (Workspace > Models > (model) "
            "> Filters)."
        )


# --------------------------------------------------------------------------
# Smoke test
# --------------------------------------------------------------------------

def smoke_test(client: OwuiClient, uploaded_code: str) -> bool:
    """Filters have no HTTP trigger endpoint like Actions do (no chat
    completion and no /api/chat/actions equivalent to call directly), so the
    strongest available check without driving a real chat is: re-fetch the
    Function and confirm the server has exactly the content we uploaded and
    the type Open WebUI needs to render the toggle."""
    fn = get_function(client, FUNCTION_ID)
    if not fn:
        fail("Filter function not found after install")
        return False
    if fn.get("type") != "filter":
        fail(f"Filter function has unexpected type={fn.get('type')!r} (expected 'filter')")
        return False
    if fn.get("content") != uploaded_code:
        fail("Filter function content on the server does not match the uploaded artifact")
        return False
    info("Smoke test passed (function type and content verified)")
    return True


def print_status(client: OwuiClient) -> bool:
    summary = get_function_summary(client, FUNCTION_ID)
    fn = get_function(client, FUNCTION_ID) if summary else None
    exists = bool(summary)
    active = bool(summary and summary.get("is_active"))
    global_ = bool(summary and summary.get("is_global"))
    content_current = bool(fn and ARTIFACT_FILE.exists() and fn.get("content") == ARTIFACT_FILE.read_text())

    console.print_json(json.dumps({
        "function_exists": exists,
        "is_active": active,
        "is_global": global_,
        "content_current": content_current,
    }))
    return all([exists, active, content_current])


# --------------------------------------------------------------------------
# Command orchestration
# --------------------------------------------------------------------------

def install_or_repair(client: OwuiClient) -> str:
    section("Function")
    code = update_or_create_function(client)
    ensure_active(client)
    return code


def execute(command: str, client: OwuiClient) -> None:
    section("Login")
    client.login()

    if command == "status":
        section("Status")
        ok = print_status(client)
        raise SystemExit(0 if ok else 1)

    if command in {"install", "repair"}:
        code = install_or_repair(client)
        section("Smoke test")
        ok = smoke_test(client, code)
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
            "(same ones install.py uses -- this Filter shares the same OWUI login)."
        )
    client = OwuiClient(owui_url.rstrip("/"), owui_email, owui_password)
    execute(command, client)


@click.group()
def cli() -> None:
    """Install, repair, and health-check the OpenClaw Thinking companion Filter."""


@cli.command()
@common_options
def install(**kwargs) -> None:
    """Create or update the Filter function, ensure it's active, smoke test."""
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
