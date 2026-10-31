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

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
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
SKILLS_DIR = ROOT / "skills"

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
    file_server_base_url: str
    auto_approve: bool = True


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
            "OpenClaw ↔ Open WebUI bridge -- setup wizard\n"
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
        console.print(
            "  [dim]Open WebUI connects to this address, so it must work from "
            "where Open WebUI runs. In Docker, localhost is the container: use "
            "host.docker.internal:18789 or the host's LAN address.[/dim]"
        )
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
# Agent-side skills
# --------------------------------------------------------------------------

def install_bundled_skills(force: bool = False) -> bool:
    """Install (or refresh) the bundled agent-side skills via the openclaw CLI.

    These teach the AGENT the two conventions the pipe listens for: the
    "OpenClaw needs input:" marker and the MEDIA: directive. Without them the
    pipe's ask-user and media features are advertised but unreachable, because
    nothing tells the model they exist.

    Skills must land where the AGENT runs, which is the Gateway host. This
    installer runs wherever the Open WebUI admin credentials are, and those are
    not always the same machine. Rather than ask, detect: the openclaw CLI is
    only present on the Gateway host, and that is already how this script
    decides whether it can auto-approve a device (see approve_pending_device).
    """
    if not SKILLS_DIR.is_dir():
        warn(f"no skills directory at {SKILLS_DIR}; run this from a clone")
        return False
    skills = sorted(d for d in SKILLS_DIR.iterdir() if (d / "SKILL.md").is_file())
    if not skills:
        warn(f"no skills found under {SKILLS_DIR}")
        return False
    if not shutil.which("openclaw"):
        warn("openclaw CLI not found here, so the agent-side skills were NOT "
             "installed.")
        console.print(
            "  [dim]That usually means the Gateway runs on another machine. "
            "Clone this repo there and run:[/dim]"
        )
        for d in skills:
            console.print(f"  [dim]  openclaw skills install ./skills/{d.name}[/dim]")
        return False

    ok = True
    for d in skills:
        # No --force by default. `install --force` is documented as overwriting
        # an existing workspace skill folder for the same slug, and OpenClaw
        # keeps NO history for skill installs, so a surprise overwrite of a
        # user-authored skill is unrecoverable. Learned the hard way: this very
        # function destroyed a hand-written skill during development.
        cmd = ["openclaw", "skills", "install", str(d)]
        if force:
            cmd.append("--force")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            info(f"skill installed: {d.name}")
        else:
            ok = False
            fail(f"skill install failed: {d.name}"
                 + ("" if force else "  (already present? re-run with --force to overwrite)"))
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            for line in detail[-3:]:
                console.print(f"    [dim]{line}[/dim]")
    return ok

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

def get_function(client: OwuiClient, function_id: str = FUNCTION_ID) -> dict | None:
    status, payload = client.request("GET", f"/api/v1/functions/id/{function_id}")
    if status == 200 and isinstance(payload, dict) and payload.get("id") == function_id:
        return payload
    return None


def get_functions(client: OwuiClient) -> list[dict]:
    status, payload = client.request("GET", "/api/v1/functions/")
    if status == 200 and isinstance(payload, list):
        return payload
    return []


def get_function_summary(client: OwuiClient, function_id: str = FUNCTION_ID) -> dict | None:
    for item in get_functions(client):
        if item.get("id") == function_id:
            return item
    return None


def get_valves(client: OwuiClient, function_id: str = FUNCTION_ID) -> dict:
    status, payload = client.request("GET", f"/api/v1/functions/id/{function_id}/valves")
    if status == 200 and isinstance(payload, dict):
        return payload
    return {}


def post_valves(client: OwuiClient, function_id: str, current: dict, desired: dict) -> list:
    """Post `desired` as `function_id`'s whole valve set when it differs from
    `current`. Returns the names that changed (none: nothing was posted)."""
    changed = sorted(k for k, v in desired.items() if current.get(k) != v)
    if changed:
        status, payload = client.request(
            "POST", f"/api/v1/functions/id/{function_id}/valves/update", desired)
        if status != 200 or not isinstance(payload, dict):
            raise SystemExit(f"Valve update failed: {payload}")
    return changed


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
    path = BACKUP_DIR / f"{fn.get('id') or FUNCTION_ID}-{stamp}.json"
    path.write_text(json.dumps(fn, indent=2, ensure_ascii=False))
    os.chmod(path, 0o600)
    info(f"Backed up existing function to {path}")
    if valves is not None:
        valves_path = BACKUP_DIR / f"{fn.get('id') or FUNCTION_ID}-{stamp}-valves.json"
        valves_path.write_text(json.dumps(valves, indent=2, ensure_ascii=False))
        os.chmod(valves_path, 0o600)
        info(f"Backed up existing valves to {valves_path}")


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


def _rebuild_pipe_file(pipe_file: Path) -> None:
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
    built = mod.build()
    if not pipe_file.exists() or pipe_file.read_text() != built:
        pipe_file.write_text(built)
        info(f"Rebuilt {pipe_file.name} from src/openclaw_pipe_pkg/")


def update_or_create_function(client: OwuiClient, cfg: Config) -> dict:
    pipe_file = PIPE_FILE
    _rebuild_pipe_file(pipe_file)
    running_without_a_clone = not (ROOT / "src").exists() and not (ROOT / ".git").exists()
    if not pipe_file.exists() and running_without_a_clone:
        # No clone: running as `uv run <raw-url> install`, where __file__ is a
        # lone temp copy. Gate on "there is no repo here" rather than merely
        # "the artifact is missing" -- a partial checkout (sparse, blob-filtered,
        # or someone who deleted the artifact) would otherwise silently deploy
        # HEAD-of-main from the internet instead of failing loudly.
        return deploy_function_code(client, PIPE_SPEC, _fetch_pipe_file())
    if not pipe_file.exists():
        raise SystemExit(
            f"Pipe file not found: {pipe_file}\n"
            "Run `python3 build.py` first."
        )
    return deploy_function_code(client, PIPE_SPEC, pipe_file.read_text())


# --------------------------------------------------------------------------
# One installer core for the three Functions (ELI-146)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FunctionSpec:
    """One Open WebUI Function this repo installs. The Pipe's installer is the
    rest of this file; install_action.py and install_filter.py are a spec each
    plus their own steps, run by `function_cli`."""
    id: str
    name: str
    type: str                      # "pipe", "action" or "filter"
    label: str                     # "Pipe", "Action", "Filter" in messages
    description: str
    artifact: Path | None = None   # the Pipe resolves its own (it may need fetching, no clone)
    build_artifact: str = ""       # build.py's artifact object ("ACTION", "FILTER")
    global_: bool = True           # made global; False: left per-model, warned if global
    global_warning: str = ""
    valves: Callable | None = None         # (client): after create/update
    smoke: Callable | None = None          # (client) -> bool, after the content check
    status_checks: Callable | None = None  # (client) -> {field: must-be-true}


PIPE_SPEC = FunctionSpec(FUNCTION_ID, "OpenClaw Gateway", "pipe", "Pipe",
                         "OpenClaw Gateway Pipe - restart-safe persistent WS bridge")


def deploy_function_code(client: OwuiClient, spec: FunctionSpec, code: str) -> dict:
    """Update the Function in place (after a backup of it and its valves), or
    create it. Never deletes and recreates: valves survive. Returns the valves
    it had before."""
    meta = {"description": spec.description, "manifest": {}}
    existing = get_function(client, spec.id)
    preserved_valves = get_valves(client, spec.id) if existing else {}
    if existing:
        backup_function(existing, preserved_valves)
        status, payload = client.request(
            "POST", f"/api/v1/functions/id/{spec.id}/update",
            {"id": spec.id, "name": spec.name, "content": code, "meta": meta},
        )
        if status != 200 or not isinstance(payload, dict) or payload.get("id") != spec.id:
            raise SystemExit(f"Function update failed: {payload}")
        info(f"Updated {spec.label} function in place")
        return preserved_valves
    status, payload = client.request(
        "POST", "/api/v1/functions/create",
        {"id": spec.id, "name": spec.name, "content": code, "type": spec.type, "meta": meta},
    )
    if status != 200 or not isinstance(payload, dict) or payload.get("id") != spec.id:
        raise SystemExit(f"Function create failed: {payload}")
    info(f"Created {spec.label} function")
    return preserved_valves


def ensure_active(client: OwuiClient, spec: FunctionSpec) -> None:
    """Activate the Function if it is not; make it global only when the spec
    says so (a filter stays attached per model)."""
    summary = get_function_summary(client, spec.id)
    if not summary:
        raise SystemExit("Function summary not found after create/update")

    if not summary.get("is_active"):
        status, payload = client.request("POST", f"/api/v1/functions/id/{spec.id}/toggle")
        if status != 200:
            raise SystemExit(f"Failed to activate function: {payload}")
        info(f"Activated {spec.label} function")
    else:
        info(f"{spec.label} function already active")

    summary = get_function_summary(client, spec.id) or {}
    if not spec.global_:
        if summary.get("is_global") and spec.global_warning:
            warn(spec.global_warning)
    elif not summary.get("is_global"):
        status, payload = client.request("POST", f"/api/v1/functions/id/{spec.id}/toggle/global")
        if status != 200:
            raise SystemExit(f"Failed to make function global: {payload}")
        info(f"Made {spec.label} function global")
    else:
        info(f"{spec.label} function already global")


def rebuild_artifact(spec: FunctionSpec) -> None:
    """Regenerate the spec's artifact from src/ before deploying, as
    _rebuild_pipe_file does for the Pipe, so a forgotten build step can never
    cause a stale deploy."""
    build_script = ROOT / "build.py"
    if not build_script.exists():
        return
    import importlib.util

    module = importlib.util.spec_from_file_location(f"_{spec.type}_build", build_script)
    mod = importlib.util.module_from_spec(module)
    module.loader.exec_module(mod)
    built = mod.build(artifact=getattr(mod, spec.build_artifact))
    if not spec.artifact.exists() or spec.artifact.read_text() != built:
        spec.artifact.write_text(built)
        info(f"Rebuilt {spec.artifact.name} from src/")


def install_function(client: OwuiClient, spec: FunctionSpec) -> bool:
    """install/repair for a spec'd Function: deploy, activate, its valves,
    then confirm the server holds exactly the uploaded code, then its own
    smoke test."""
    section("Function")
    rebuild_artifact(spec)
    if not spec.artifact.exists():
        raise SystemExit(f"Artifact not found: {spec.artifact}")
    code = spec.artifact.read_text()
    deploy_function_code(client, spec, code)
    ensure_active(client, spec)
    if spec.valves:
        section("Valves")
        spec.valves(client)

    section("Smoke test")
    fn = get_function(client, spec.id)
    if not fn or fn.get("type") != spec.type or fn.get("content") != code:
        fail(f"{spec.label} function on the server is missing, of another type, or not the uploaded code")
        return False
    info(f"{spec.label} function type and content verified")
    return spec.smoke(client) if spec.smoke else True


def function_status(client: OwuiClient, spec: FunctionSpec) -> bool:
    summary = get_function_summary(client, spec.id)
    fn = get_function(client, spec.id) if summary else None
    state = {
        "function_exists": bool(summary),
        "is_active": bool(summary and summary.get("is_active")),
        "is_global": bool(summary and summary.get("is_global")),
        "content_current": bool(fn and spec.artifact.exists() and fn.get("content") == spec.artifact.read_text()),
    }
    if spec.status_checks and summary:
        state.update(spec.status_checks(client))
    console.print_json(json.dumps(state))
    return all(v for k, v in state.items() if k != "is_global" or spec.global_)


def _in_host_netns() -> bool:
    """True unless this process provably runs in another network namespace than
    PID 1. Unreadable counts as the host: refusing a sandbox deploy is the safe
    side."""
    try:
        return os.readlink("/proc/self/ns/net") == os.readlink("/proc/1/ns/net")
    except OSError:
        return True


def sandbox_refusal(*, live: bool) -> str | None:
    """Why a deploy must not run, or None (ELI-221).

    A sandbox (e2e-local's run.sh) sets E2E_ROOT and runs in its own network
    namespace, where localhost is the sandbox's Open WebUI. With E2E_ROOT set
    in the host's namespace, the same command reaches the host's live Open
    WebUI instead, so it needs --live."""
    if live or not os.environ.get("E2E_ROOT") or not _in_host_netns():
        return None
    return ("Refusing to deploy: E2E_ROOT is set (a sandbox shell), but this runs in the "
            "host's network namespace, so the target is the host's live Open WebUI. "
            "Run it through the sandbox wrapper, or pass --live to deploy to the host.")


def live_option(f):
    return click.option("--live", is_flag=True, default=False,
                        help="Deploy to the host's Open WebUI even though E2E_ROOT is set "
                             "(a sandbox shell refuses that otherwise)")(f)


def owui_options(f):
    for option in reversed([
        click.option("--owui-url", envvar="OWUI_URL", default="http://localhost:8080",
                     show_default=True, help="Open WebUI base URL"),
        click.option("--owui-email", envvar="OWUI_EMAIL", default="", help="Open WebUI admin email"),
        click.option("--owui-password", envvar="OWUI_PASSWORD", default="", help="Open WebUI admin password"),
    ]):
        f = option(f)
    return f


def function_cli(spec: FunctionSpec):
    """install / repair / status for a spec'd Function, logged in with the
    same OWUI credentials install.py uses."""
    @click.group(help=f"Install, repair, and health-check the {spec.name} {spec.label}.")
    def cli() -> None:
        pass

    def run(command: str, owui_url: str, owui_email: str, owui_password: str, live: bool = False) -> None:
        refusal = command != "status" and sandbox_refusal(live=live)
        if refusal:
            raise SystemExit(refusal)
        if not owui_email or not owui_password:
            raise SystemExit("Missing required env/args: OWUI_EMAIL, OWUI_PASSWORD "
                             "(the same Open WebUI login install.py uses).")
        client = OwuiClient(owui_url.rstrip("/"), owui_email, owui_password)
        section("Login")
        client.login()
        if command == "status":
            section("Status")
            ok = function_status(client, spec)
        else:
            ok = install_function(client, spec)
        raise SystemExit(0 if ok else 1)

    def add(command: str, help_text: str) -> None:
        @cli.command(name=command, help=help_text)
        @owui_options
        @live_option
        def _command(owui_url: str, owui_email: str, owui_password: str, live: bool) -> None:
            run(command, owui_url, owui_email, owui_password, live)

    add("install", f"Create or update the {spec.label} function, then smoke test.")
    add("repair", "Same as install; use after a broken/partial setup.")
    add("status", "Print current state without changing anything.")
    return cli


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
    }
    if cfg.owui_api_key:
        required["OWUI_API_KEY"] = cfg.owui_api_key
    if cfg.file_server_base_url:
        required["FILE_SERVER_BASE_URL"] = cfg.file_server_base_url

    desired = {**existing, **required}
    changed = post_valves(client, FUNCTION_ID, current, desired)
    info(f"Updated valves: {', '.join(changed)}" if changed else "Valves already up to date")
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


def install_or_repair(client: OwuiClient, cfg: Config) -> dict:
    section("Function")
    preserved_valves = update_or_create_function(client, cfg)
    ensure_active(client, PIPE_SPEC)

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
        click.option("--file-server-base-url", envvar="FILE_SERVER_BASE_URL", default="",
                     help="Legacy media fallback base URL"),
        click.option("--auto-approve/--no-auto-approve", default=True,
                     help="Approve a matching pending OpenClaw device when a "
                          "smoke test reports 'pairing required'"),
        click.option("--wizard", "-w", is_flag=True, default=False,
                     help="Prompt interactively for any missing required values"),
        live_option,
    ]
    for option in reversed(options):
        f = option(f)
    return f


def _dispatch(command: str, **kwargs) -> None:
    live = kwargs.pop("live")
    refusal = command != "status" and sandbox_refusal(live=live)
    if refusal:
        raise SystemExit(refusal)
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
@click.option("--force", "force", is_flag=True, default=False,
              help="Overwrite an existing skill of the same name (unrecoverable).")
def skills(force: bool) -> None:
    """Install or refresh the agent-side skills (run on the Gateway host).

    Separate from `install` because the skills belong where the AGENT runs,
    which is not always where Open WebUI runs. Needs no Open WebUI credentials.
    """
    section("Agent-side skills")
    raise SystemExit(0 if install_bundled_skills(force=force) else 1)


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
