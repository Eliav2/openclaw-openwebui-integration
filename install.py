#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "cryptography>=41",
#     "click>=8.1",
#     "rich>=13",
# ]
# ///
"""
Install, repair, and health-check the OpenClaw Gateway Pipe in Open WebUI.

The installer is intentionally restart-safe:
- update in place; never delete/recreate an existing function
- preserve existing valves, especially DEVICE_IDENTITY
- enable the function only when it is inactive; no blind toggles
- run an end-to-end smoke test before claiming success
- optionally approve the matching OpenClaw pairing request automatically

Zero-setup usage (uv resolves cryptography/click/rich automatically):

    uv run https://raw.githubusercontent.com/Eliav2/openclaw-openwebui-integration/main/install.py install --wizard

From a local clone:

    uv run install.py install
    # or, with deps installed manually: python3 install.py install
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, fields
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt


FUNCTION_ID = "openclaw_gateway"
DEFAULT_MODEL_ID = f"{FUNCTION_ID}.default"
ROOT = Path(__file__).resolve().parent
PIPE_FILE = ROOT / "openclaw_pipe.py"
LOCAL_IDENTITY_FILE = ROOT / ".pipe_device_identity.json"
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
# Config
# --------------------------------------------------------------------------

@dataclass
class Config:
    owui_url: str
    owui_email: str
    owui_password: str
    gateway_url: str
    gateway_token: str
    agent_id: str
    state_dir: str
    owui_api_base_url: str
    owui_api_key: str
    chatgpt_model: str
    file_server_base_url: str
    auto_approve: bool = True
    dev_bundle: bool = False
    confirm_downgrade_from_dev_bundle: bool = False


def require_config(cfg: Config, *, need_gateway: bool) -> None:
    missing = []
    if not cfg.owui_email:
        missing.append("OWUI_EMAIL")
    if not cfg.owui_password:
        missing.append("OWUI_PASSWORD")
    if need_gateway and not cfg.gateway_token:
        missing.append("GATEWAY_TOKEN")
    if missing:
        raise SystemExit(
            "Missing required env/args: " + ", ".join(missing) + "\n"
            "Set OWUI_URL, OWUI_EMAIL, OWUI_PASSWORD, GATEWAY_URL, "
            "GATEWAY_TOKEN, AGENT_ID, or re-run with --wizard."
        )


def run_wizard(cfg: Config, *, need_gateway: bool) -> Config:
    console.print(
        Panel(
            "OpenClaw ↔ Open WebUI bridge — setup wizard\n"
            "Press Enter to accept a default shown in brackets.",
            style="bold cyan",
        )
    )
    cfg.owui_url = Prompt.ask("Open WebUI base URL", default=cfg.owui_url)
    if not cfg.owui_email:
        cfg.owui_email = Prompt.ask("Open WebUI admin email")
    if not cfg.owui_password:
        cfg.owui_password = Prompt.ask("Open WebUI admin password", password=True)
    if need_gateway:
        cfg.gateway_url = Prompt.ask(
            "OpenClaw gateway address (host:port)", default=cfg.gateway_url
        )
        if not cfg.gateway_token:
            console.print(
                "  [dim]Find this under gateway.auth.token in your OpenClaw "
                "config, or ask your OpenClaw admin.[/dim]"
            )
            cfg.gateway_token = Prompt.ask("OpenClaw gateway token", password=True)
    cfg.agent_id = Prompt.ask("OpenClaw agent id", default=cfg.agent_id)
    return cfg


# --------------------------------------------------------------------------
# OWUI API client
# --------------------------------------------------------------------------

class OwuiClient:
    def __init__(self, base_url: str, email: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.token: str | None = None

    def request(self, method: str, path: str, data: dict | None = None,
                timeout: int = 30, require_json: bool = True) -> tuple[int, object]:
        body = json.dumps(data).encode() if data is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
                if not require_json:
                    return resp.status, text
                return resp.status, json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {"detail": text.strip()}
            return exc.code, payload
        except Exception as exc:
            return 0, {"detail": str(exc)}

    def login(self) -> None:
        status, payload = self.request(
            "POST",
            "/api/v1/auths/signin",
            {"email": self.email, "password": self.password},
        )
        if status != 200 or not isinstance(payload, dict):
            raise SystemExit(f"OWUI login failed: {payload}")
        self.token = payload.get("token") or payload.get("access_token")
        if not self.token:
            raise SystemExit(f"OWUI login did not return a token: {payload}")
        info(f"Logged into OWUI as {payload.get('name') or self.email}")


# --------------------------------------------------------------------------
# Device identity
# --------------------------------------------------------------------------

def generate_device_identity() -> dict:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    import base64
    import hashlib

    pk = ed25519.Ed25519PrivateKey.generate()
    pub = pk.public_key()
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "id": hashlib.sha256(raw).hexdigest(),
        "publicKey": base64.urlsafe_b64encode(raw).decode().rstrip("="),
        "privateKey": pk.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
    }


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception as exc:
        warn(f"Could not read {path}: {exc}")
        return None


def save_json_private(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def parse_identity(raw: object) -> dict | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        ident = raw
    else:
        try:
            ident = json.loads(str(raw))
        except json.JSONDecodeError:
            return None
    if ident.get("id") and ident.get("privateKey") and ident.get("publicKey"):
        return ident
    return None


def choose_identity(existing_valves: dict) -> dict:
    valve_identity = parse_identity(existing_valves.get("DEVICE_IDENTITY"))
    if valve_identity:
        save_json_private(LOCAL_IDENTITY_FILE, valve_identity)
        info(f"Using DEVICE_IDENTITY from existing valves: {valve_identity['id'][:20]}...")
        return valve_identity

    file_identity = parse_identity(load_json(LOCAL_IDENTITY_FILE))
    if file_identity:
        info(f"Using local device identity: {file_identity['id'][:20]}...")
        return file_identity

    ident = generate_device_identity()
    save_json_private(LOCAL_IDENTITY_FILE, ident)
    warn(f"Generated new device identity: {ident['id'][:20]}...")
    warn("This new identity must be approved once by OpenClaw.")
    return ident


# --------------------------------------------------------------------------
# Function / valve management
# --------------------------------------------------------------------------

def get_function(client: OwuiClient) -> dict | None:
    status, payload = client.request("GET", f"/api/v1/functions/id/{FUNCTION_ID}")
    if status == 200 and isinstance(payload, dict) and payload.get("id") == FUNCTION_ID:
        return payload
    return None


def get_functions(client: OwuiClient) -> list[dict]:
    status, payload = client.request("GET", "/api/v1/functions/")
    if status == 200 and isinstance(payload, list):
        return payload
    return []


def get_function_summary(client: OwuiClient) -> dict | None:
    for item in get_functions(client):
        if item.get("id") == FUNCTION_ID:
            return item
    return None


def get_valves(client: OwuiClient) -> dict:
    status, payload = client.request("GET", f"/api/v1/functions/id/{FUNCTION_ID}/valves")
    if status == 200 and isinstance(payload, dict):
        return payload
    return {}


def model_exists(client: OwuiClient) -> bool:
    status, payload = client.request("GET", "/api/v1/models")
    if status != 200 or not isinstance(payload, dict):
        return False
    ids = {m.get("id") for m in payload.get("data", [])}
    has_default = DEFAULT_MODEL_ID in ids
    has_any_other = any(
        id.startswith(f"{FUNCTION_ID}.") and id != DEFAULT_MODEL_ID
        for id in ids
    )
    return has_default and has_any_other


def backup_function(fn: dict, valves: dict | None = None) -> None:
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = BACKUP_DIR / f"{FUNCTION_ID}-{stamp}.json"
    path.write_text(json.dumps(fn, indent=2, ensure_ascii=False))
    os.chmod(path, 0o600)
    info(f"Backed up existing function to {path}")
    if valves is not None:
        valves_path = BACKUP_DIR / f"{FUNCTION_ID}-{stamp}-valves.json"
        valves_path.write_text(json.dumps(valves, indent=2, ensure_ascii=False))
        os.chmod(valves_path, 0o600)
        info(f"Backed up existing valves to {valves_path}")


def _resolve_pipe_file(cfg: Config) -> Path:
    """openclaw_pipe.py is the artifact real users install (committed to git).
    openclaw_pipe.dev.py is internal-only (ELI-24 cross-agent deploy
    coordination), gitignored, and only ever deployed with --dev-bundle."""
    return (ROOT / "openclaw_pipe.dev.py") if cfg.dev_bundle else PIPE_FILE


ARTIFACT_URL = os.environ.get(
    "OPENCLAW_PIPE_ARTIFACT_URL",
    "https://raw.githubusercontent.com/Eliav2/openclaw-openwebui-integration"
    "/main/openclaw_pipe.py",
)


def _fetch_pipe_file() -> str:
    """Download openclaw_pipe.py for the no-clone install path.

    ``uv run https://.../install.py install`` hands us a lone temp copy of this
    script, so there is no artifact and no src/ tree beside it -- the deploy
    would otherwise die on "Pipe file not found" only AFTER the wizard has
    already collected the OWUI password and gateway token. Fetch the artifact
    from the same repo instead. Override the source with
    OPENCLAW_PIPE_ARTIFACT_URL (a fork, a pinned tag, or a local file server).
    """
    info(f"No local openclaw_pipe.py; fetching it from {ARTIFACT_URL}")
    try:
        with urllib.request.urlopen(ARTIFACT_URL, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except Exception as exc:
        raise SystemExit(
            f"Could not download the pipe artifact from {ARTIFACT_URL}: {exc}\n"
            "Run the installer from a clone instead:\n"
            "  git clone https://github.com/Eliav2/openclaw-openwebui-integration\n"
            "  cd openclaw-openwebui-integration && uv run install.py install"
        ) from exc
    # Cheapest honest check that we got the artifact and not a 404 page or an
    # HTML error body served with a 200.
    if "class Pipe" not in body:
        raise SystemExit(
            f"{ARTIFACT_URL} did not return the pipe artifact "
            f"(no top-level `class Pipe` in {len(body)} bytes). "
            "If you overrode OPENCLAW_PIPE_ARTIFACT_URL, check it points at the "
            "raw openclaw_pipe.py."
        )
    info(f"Fetched openclaw_pipe.py ({len(body.splitlines())} lines)")
    # Returned as text, not written to disk: the caller only needs the source,
    # and a write/read round trip would re-encode through the locale encoding
    # while the download decoded explicitly as UTF-8. The artifact contains
    # non-ASCII (·, Hebrew, Arabic), so that round trip is a latent corruption.
    return body


def _rebuild_pipe_file(pipe_file: Path, *, dev: bool) -> None:
    """Always regenerate the deployed artifact from src/ before deploying, so
    a forgotten ``python3 build.py`` step can never cause a stale deploy.

    When only the built artifact is present (e.g. remote ``uv run install.py``
    with no src/ tree at all), there is nothing to build and we deploy
    whatever is already there.
    """
    src_pkg = ROOT / "src" / "openclaw_pipe_pkg"
    build_script = ROOT / "build.py"
    if not (src_pkg.exists() and build_script.exists()):
        return
    import importlib.util

    spec = importlib.util.spec_from_file_location("_pipe_build", build_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    built = mod.build(strip_dev_only=not dev)
    if not pipe_file.exists() or pipe_file.read_text() != built:
        pipe_file.write_text(built)
        info(f"Rebuilt {pipe_file.name} from src/openclaw_pipe_pkg/")


def _assert_not_silently_downgrading_from_dev_bundle(cfg: Config) -> None:
    """Refuse to deploy the plain artifact over a live pipe that's currently
    running the dev bundle, unless explicitly confirmed.

    Forgetting --dev-bundle is an easy mistake (it defaults to False), and
    the consequence isn't cosmetic: it silently replaces the coordinated
    dev pipe with the plain stripped one, deleting the ELI-24 deploy-
    coordination mechanism from the live instance and restoring the
    original ELI-23 hazard (a mid-turn deploy kills that turn's own
    generator) with zero warning. A live pipe only responds on
    /__devcoord__/status if it's currently running the dev bundle, so that
    response is a reliable signal this deploy would be a downgrade.
    """
    if cfg.dev_bundle or cfg.confirm_downgrade_from_dev_bundle:
        return
    base = _devcoord_base_url(cfg)
    try:
        _devcoord_request(f"{base}/__devcoord__/status", timeout=3.0)
    except Exception:
        return  # live pipe isn't running the dev bundle -- nothing to downgrade
    raise SystemExit(
        "Refusing to deploy: the live pipe is currently running the dev bundle "
        "(which adds deploy coordination), but this deploy would overwrite it "
        "with the plain public artifact -- silently removing that coordination "
        "and restoring the hazard it exists to prevent: a deploy landing "
        "mid-turn kills that turn's own generator, with no warning.\n"
        "If you meant to deploy the dev bundle, add --dev-bundle.\n"
        "If you really want to downgrade to the plain artifact on purpose, "
        "re-run with --confirm-downgrade-from-dev-bundle."
    )


def update_or_create_function(client: OwuiClient, cfg: Config) -> dict:
    _assert_not_silently_downgrading_from_dev_bundle(cfg)
    pipe_file = _resolve_pipe_file(cfg)
    _rebuild_pipe_file(pipe_file, dev=cfg.dev_bundle)
    running_without_a_clone = not (ROOT / "src").exists() and not (ROOT / ".git").exists()
    if not pipe_file.exists() and not cfg.dev_bundle and running_without_a_clone:
        # No clone: running as `uv run <raw-url> install`, where __file__ is a
        # lone temp copy. Gate on "there is no repo here" rather than merely
        # "the artifact is missing" -- a partial checkout (sparse, blob-filtered,
        # or someone who deleted the artifact) would otherwise silently deploy
        # HEAD-of-main from the internet instead of failing loudly.
        # Never for --dev-bundle, which is internal-only and never published.
        return _deploy_pipe_code(client, _fetch_pipe_file())
    if not pipe_file.exists():
        raise SystemExit(
            f"Pipe file not found: {pipe_file}\n"
            "Run `python3 build.py --dev` first, or drop --dev-bundle."
        )
    return _deploy_pipe_code(client, pipe_file.read_text())


def _deploy_pipe_code(client: OwuiClient, pipe_code: str) -> dict:
    """Create or update the OWUI function from already-resolved pipe source.

    Split out so the no-clone path (fetched artifact, never written into the
    working directory) and the normal path share one deploy implementation.
    """
    existing = get_function(client)
    preserved_valves = get_valves(client) if existing else {}

    if existing:
        backup_function(existing, preserved_valves)
        status, payload = client.request(
            "POST",
            f"/api/v1/functions/id/{FUNCTION_ID}/update",
            {
                "id": FUNCTION_ID,
                "name": "OpenClaw Gateway",
                "content": pipe_code,
                "meta": {
                    "description": "OpenClaw Gateway Pipe - restart-safe persistent WS bridge",
                    "manifest": {},
                },
            },
        )
        if status != 200 or not isinstance(payload, dict) or payload.get("id") != FUNCTION_ID:
            raise SystemExit(f"Function update failed: {payload}")
        info("Updated pipe function in place")
        return preserved_valves

    status, payload = client.request(
        "POST",
        "/api/v1/functions/create",
        {
            "id": FUNCTION_ID,
            "name": "OpenClaw Gateway",
            "content": pipe_code,
            "type": "pipe",
            "meta": {
                "description": "OpenClaw Gateway Pipe - restart-safe persistent WS bridge",
                "manifest": {},
            },
        },
    )
    if status != 200 or not isinstance(payload, dict) or payload.get("id") != FUNCTION_ID:
        raise SystemExit(f"Function create failed: {payload}")
    info("Created pipe function")
    return preserved_valves


def ensure_active_global(client: OwuiClient) -> None:
    summary = get_function_summary(client)
    if not summary:
        raise SystemExit("Function summary not found after create/update")

    if not summary.get("is_active"):
        status, payload = client.request("POST", f"/api/v1/functions/id/{FUNCTION_ID}/toggle")
        if status != 200:
            raise SystemExit(f"Failed to activate function: {payload}")
        info("Activated pipe function")
    else:
        info("Pipe function already active")

    summary = get_function_summary(client) or {}
    if not summary.get("is_global"):
        status, payload = client.request("POST", f"/api/v1/functions/id/{FUNCTION_ID}/toggle/global")
        if status != 200:
            raise SystemExit(f"Failed to make function global: {payload}")
        info("Made pipe function global")
    else:
        info("Pipe function already global")


def update_valves(client: OwuiClient, cfg: Config, preserved_valves: dict | None = None) -> dict:
    current = get_valves(client)
    preserved_valves = preserved_valves or {}
    existing = {**preserved_valves, **current}
    if preserved_valves and set(preserved_valves) - set(current):
        missing = ", ".join(sorted(set(preserved_valves) - set(current)))
        warn(f"Function update dropped valves; restoring: {missing}")
    ident = choose_identity(existing)
    ident_json = json.dumps(ident, separators=(",", ":"))

    required = {
        "GATEWAY_URL": cfg.gateway_url,
        "GATEWAY_TOKEN": cfg.gateway_token,
        "AGENT_ID": cfg.agent_id,
        "ENABLE_FILE_SERVER": True,
        "DEVICE_IDENTITY": ident_json,
        "STATE_DIR": cfg.state_dir,
        "USE_OWUI_FILES": True,
        "SEND_STOP_ON_CANCEL": True,
        "OWUI_BASE_URL": cfg.owui_api_base_url,
        "CHATGPT_MODEL": cfg.chatgpt_model,
    }
    if cfg.owui_api_key:
        required["OWUI_API_KEY"] = cfg.owui_api_key
    if cfg.file_server_base_url:
        required["FILE_SERVER_BASE_URL"] = cfg.file_server_base_url

    desired = {**existing, **required}
    changed = {k: v for k, v in desired.items() if current.get(k) != v}
    if not changed:
        info("Valves already up to date")
        return desired

    status, payload = client.request(
        "POST",
        f"/api/v1/functions/id/{FUNCTION_ID}/valves/update",
        desired,
    )
    if status != 200 or not isinstance(payload, dict):
        raise SystemExit(f"Valve update failed: {payload}")
    info(f"Updated valves: {', '.join(sorted(changed.keys()))}")
    return desired


# --------------------------------------------------------------------------
# OpenClaw device pairing helpers
# --------------------------------------------------------------------------

def run_openclaw_json(*args: str) -> dict | None:
    if not shutil.which("openclaw"):
        return None
    try:
        proc = subprocess.run(
            ["openclaw", *args, "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        warn(f"openclaw {' '.join(args)} failed: {exc}")
        return None
    if proc.returncode != 0:
        warn(proc.stderr.strip() or proc.stdout.strip())
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def approve_pending_device(device_id: str) -> bool:
    if not shutil.which("openclaw"):
        warn("openclaw CLI not found; approve the pending device manually")
        return False
    devices = run_openclaw_json("devices", "list")
    if not devices:
        return False
    for req in devices.get("pending", []):
        if req.get("deviceId") == device_id:
            request_id = req.get("requestId") or req.get("id")
            if not request_id:
                continue
            proc = subprocess.run(
                ["openclaw", "devices", "approve", request_id],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0:
                info(f"Approved pending OpenClaw device {device_id[:20]}...")
                return True
            warn(proc.stderr.strip() or proc.stdout.strip())
            return False
    warn(f"No pending OpenClaw request found for device {device_id[:20]}...")
    return False


def is_device_paired(device_id: str) -> bool:
    devices = run_openclaw_json("devices", "list")
    if not devices:
        return False
    return any(dev.get("deviceId") == device_id for dev in devices.get("paired", []))


# --------------------------------------------------------------------------
# Smoke tests
# --------------------------------------------------------------------------

def smoke_test(client: OwuiClient, *, repair_pairing: bool = False,
               valves: dict | None = None) -> bool:
    status, payload = client.request(
        "POST",
        "/api/chat/completions",
        {
            "model": DEFAULT_MODEL_ID,
            "messages": [
                {
                    "role": "user",
                    "content": "Config smoke test: reply with exactly OK_CONFIG_TEST",
                }
            ],
            "stream": False,
            "metadata": {
                "chat_id": f"install-smoke-{int(time.time())}",
                "user_id": "install-smoke",
            },
        },
        timeout=90,
    )
    if status != 200 or not isinstance(payload, dict):
        fail(f"Smoke test HTTP failed: {payload}")
        return False

    content = (
        payload.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    if "OK_CONFIG_TEST" in content:
        info("Smoke test passed")
        return True

    if "pairing required" in content and repair_pairing and valves:
        ident = parse_identity(valves.get("DEVICE_IDENTITY"))
        if ident and approve_pending_device(ident["id"]):
            return smoke_test(client, repair_pairing=False, valves=valves)

    fail(f"Smoke test failed: {content[:300]}")
    return False


def route_smoke_test(client: OwuiClient) -> bool:
    """Verify the model-override route works for a dynamically discovered
    (non-default) model. Replaces the old hardcoded-"chatgpt" preset test,
    which no longer applies now that presets are discovered dynamically
    instead of hardcoded (see ELI-11)."""
    status, payload = client.request("GET", "/api/v1/models")
    if status != 200 or not isinstance(payload, dict):
        fail(f"Route smoke test: could not list models: {payload}")
        return False
    ids = [m.get("id") for m in payload.get("data", [])]
    candidate = next(
        (i for i in ids if i.startswith(f"{FUNCTION_ID}.") and i != DEFAULT_MODEL_ID),
        None,
    )
    if not candidate:
        warn("Route smoke test skipped: no non-default model currently registered")
        return True
    expected_key = candidate.split(".", 1)[1]

    status, payload = client.request(
        "POST",
        "/api/chat/completions",
        {
            "model": candidate,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Use session_status for current session, then answer "
                        "with only the exact model line."
                    ),
                }
            ],
            "stream": False,
            "metadata": {
                "chat_id": f"install-route-smoke-{int(time.time())}",
                "user_id": "install-route-smoke",
            },
        },
        timeout=120,
    )
    if status != 200 or not isinstance(payload, dict):
        fail(f"Route smoke HTTP failed: {payload}")
        return False

    content = (
        payload.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    if expected_key in content:
        info(f"Route smoke test passed ({expected_key})")
        return True

    fail(f"Route smoke failed for {expected_key}: {content[:500]}")
    return False


def print_status(client: OwuiClient) -> bool:
    summary = get_function_summary(client)
    valves = get_valves(client) if summary else {}
    exists = bool(summary)
    active = bool(summary and summary.get("is_active"))
    global_ = bool(summary and summary.get("is_global"))
    model = model_exists(client)
    ident = parse_identity(valves.get("DEVICE_IDENTITY"))
    paired = bool(ident and is_device_paired(ident["id"]))

    console.print_json(json.dumps({
        "function_exists": exists,
        "is_active": active,
        "is_global": global_,
        "model_visible": model,
        "has_gateway_url": bool(valves.get("GATEWAY_URL")),
        "has_gateway_token": bool(valves.get("GATEWAY_TOKEN")),
        "has_device_identity": bool(ident),
        "device_id": ident.get("id") if ident else None,
        "device_paired": paired,
    }))
    return all([exists, active, global_, model, ident, paired])


# --------------------------------------------------------------------------
# Cross-agent deploy coordination (ELI-24, --dev-bundle only)
#
# install.py has no filesystem access to the pipe's STATE_DIR (it talks to
# OWUI purely over HTTP, possibly from a different host entirely), so it
# reaches the in-flight-turn signal via the dev-only routes on the pipe's
# own media file server instead (see src/openclaw_pipe_pkg/media.py,
# DEV-ONLY block). Those routes only exist when the running pipe was
# deployed from openclaw_pipe.dev.py, hence this whole mechanism is a no-op
# unless --dev-bundle is set.
# --------------------------------------------------------------------------

DEVCOORD_PORT = 18791


def _devcoord_base_url(cfg: Config) -> str:
    host = urllib.parse.urlparse(cfg.owui_url).hostname or "localhost"
    return f"http://{host}:{DEVCOORD_PORT}"


def _devcoord_request(url: str, *, method: str = "GET", timeout: float = 3.0):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


@contextlib.contextmanager
def _deploy_lock():
    """Serialize concurrent install.py repair invocations on this host --
    two agents hot-swapping the same function back-to-back is its own hazard
    independent of in-flight turns."""
    lock_path = ROOT / ".deploy.lock"
    with open(lock_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextlib.contextmanager
def devcoord_deploy_guard(cfg: Config, *, alert_after_s: float = 120.0,
                          alert_interval_s: float = 120.0, poll_interval_s: float = 1.0):
    """Request the deploy, then wait -- with no upper bound -- for the
    running pipe to report zero in-flight turns before yielding to let the
    caller swap the function.

    Deliberately does NOT force-deploy on a timeout: any turn still running
    at swap time gets killed outright by OWUI's own hot-reload (the ELI-23
    root cause), so an auto-force after some magic number of seconds would
    just trade "deploy waited a bit longer" for "a user's in-flight reply
    vanished" -- not acceptable. Past alert_after_s, and then every
    alert_interval_s after that, this prints a loud warning instead of
    forcing, so a genuinely stuck turn becomes a human decision (go
    investigate/kill it, or Ctrl-C the deploy) rather than something a timer
    silently kills.
    """
    if not cfg.dev_bundle:
        yield
        return

    base = _devcoord_base_url(cfg)
    with _deploy_lock():
        try:
            _devcoord_request(f"{base}/__devcoord__/deploy-pending", method="POST")
        except Exception as ex:
            warn(f"devcoord: could not reach running pipe ({ex}); deploying without coordination")
            yield
            return

        drained = False
        start = time.time()
        next_alert = start + alert_after_s
        while True:
            try:
                _, body = _devcoord_request(f"{base}/__devcoord__/status")
                inflight = json.loads(body).get("inflight", 0)
            except Exception as ex:
                warn(f"devcoord: status check failed ({ex}); proceeding without waiting further")
                break
            if inflight == 0:
                drained = True
                break
            now = time.time()
            if now >= next_alert:
                warn(
                    f"devcoord: still waiting on {inflight} in-flight turn(s) after "
                    f"{int(now - start)}s -- NOT force-deploying. If this is a "
                    "genuinely stuck turn, investigate/kill it manually or Ctrl-C "
                    "this deploy."
                )
                next_alert = now + alert_interval_s
            time.sleep(poll_interval_s)

        if drained:
            info("devcoord: no in-flight turns, deploying")

        try:
            yield
        finally:
            try:
                _devcoord_request(f"{base}/__devcoord__/deploy-pending", method="DELETE")
            except Exception as ex:
                warn(f"devcoord: could not clear deploy-pending flag ({ex})")


def install_or_repair(client: OwuiClient, cfg: Config) -> dict:
    section("Function")
    with devcoord_deploy_guard(cfg):
        preserved_valves = update_or_create_function(client, cfg)
    ensure_active_global(client)

    section("Valves")
    valves = update_valves(client, cfg, preserved_valves=preserved_valves)

    section("Model discovery")
    if model_exists(client):
        info(f"Model {DEFAULT_MODEL_ID} and at least one dynamic model are visible")
    else:
        raise SystemExit(
            f"Model {DEFAULT_MODEL_ID} and at least one dynamic model "
            "are not both visible after enabling"
        )
    return valves


# --------------------------------------------------------------------------
# Command orchestration
# --------------------------------------------------------------------------

def execute(command: str, cfg: Config) -> None:
    need_gateway = command in {"install", "repair", "healthcheck"}
    require_config(cfg, need_gateway=need_gateway)
    client = OwuiClient(cfg.owui_url, cfg.owui_email, cfg.owui_password)

    section("Login")
    client.login()

    if command == "status":
        section("Status")
        ok = print_status(client)
        raise SystemExit(0 if ok else 1)

    if command in {"install", "repair"}:
        valves = install_or_repair(client, cfg)
        section("Smoke test")
        ok = smoke_test(client, repair_pairing=cfg.auto_approve, valves=valves)
        section("Route smoke test")
        route_ok = route_smoke_test(client)
        raise SystemExit(0 if ok and route_ok else 1)

    if command == "healthcheck":
        section("Status")
        status_ok = print_status(client)
        section("Smoke test")
        valves = get_valves(client)
        smoke_ok = smoke_test(client, repair_pairing=cfg.auto_approve, valves=valves)
        section("Route smoke test")
        route_ok = route_smoke_test(client)
        raise SystemExit(0 if status_ok and smoke_ok and route_ok else 1)


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
        click.option("--gateway-url", envvar="GATEWAY_URL", default="localhost:18789",
                     show_default=True, help="OpenClaw gateway address (host:port)"),
        click.option("--gateway-token", envvar="GATEWAY_TOKEN", default="",
                     help="OpenClaw gateway API token"),
        click.option("--agent-id", envvar="AGENT_ID", default="main",
                     show_default=True, help="OpenClaw agent id to route to"),
        click.option("--state-dir", envvar="OPENCLAW_BRIDGE_STATE_DIR",
                     default="/data/openclaw-bridge", show_default=True,
                     help="Pipe state directory inside the OWUI container"),
        click.option("--owui-api-base-url", envvar="OWUI_API_BASE_URL", default="",
                     help="Base URL the pipe uses to call OWUI's own API "
                          "(defaults to --owui-url)"),
        click.option("--owui-api-key", envvar="OWUI_API_KEY", default="",
                     help="Optional OWUI API key for native media uploads"),
        click.option("--chatgpt-model", envvar="CHATGPT_MODEL", default="openai/gpt-5.5",
                     show_default=True, help="OpenClaw model for the ChatGPT selector entry"),
        click.option("--file-server-base-url", envvar="FILE_SERVER_BASE_URL", default="",
                     help="Legacy media fallback base URL"),
        click.option("--auto-approve/--no-auto-approve", default=True,
                     help="Approve a matching pending OpenClaw device when a "
                          "smoke test reports 'pairing required'"),
        click.option("--dev-bundle", envvar="OPENCLAW_DEV_BUNDLE", is_flag=True, default=False,
                     help="Internal-only (ELI-24): deploy openclaw_pipe.dev.py "
                          "(includes cross-agent deploy coordination) instead of "
                          "the public openclaw_pipe.py. Never use for a real install."),
        click.option("--confirm-downgrade-from-dev-bundle", is_flag=True, default=False,
                     help="Required to deploy the plain artifact (i.e. without "
                          "--dev-bundle) over a live pipe that's currently running "
                          "the dev bundle -- without this, that deploy is refused "
                          "since it would silently delete the ELI-24 deploy-"
                          "coordination mechanism from the live instance."),
        click.option("--wizard", "-w", is_flag=True, default=False,
                     help="Prompt interactively for any missing required values"),
    ]
    for option in reversed(options):
        f = option(f)
    return f


def _dispatch(command: str, **kwargs) -> None:
    wizard = kwargs.pop("wizard")
    cfg = Config(**kwargs)
    if wizard:
        cfg = run_wizard(cfg, need_gateway=command in {"install", "repair", "healthcheck"})
    cfg.owui_url = cfg.owui_url.rstrip("/")
    cfg.owui_api_base_url = (cfg.owui_api_base_url or cfg.owui_url).rstrip("/")
    execute(command, cfg)


@click.group()
def cli() -> None:
    """Install, repair, and health-check the OpenClaw Gateway Pipe in Open WebUI."""


@cli.command()
@common_options
def install(**kwargs) -> None:
    """Create or update the pipe function, valves, and run a smoke test."""
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


@cli.command()
@common_options
def healthcheck(**kwargs) -> None:
    """Status checks plus an end-to-end smoke test."""
    _dispatch("healthcheck", **kwargs)


if __name__ == "__main__":
    cli()
