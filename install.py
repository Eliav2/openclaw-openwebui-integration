#!/usr/bin/env python3
"""
Install, repair, and health-check the OpenClaw Gateway Pipe in Open WebUI.

The installer is intentionally restart-safe:
- update in place; never delete/recreate an existing function
- preserve existing valves, especially DEVICE_IDENTITY
- enable the function only when it is inactive; no blind toggles
- run an end-to-end smoke test before claiming success
- optionally approve the matching OpenClaw pairing request automatically
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


FUNCTION_ID = "openclaw_gateway"
DEFAULT_MODEL_ID = f"{FUNCTION_ID}.default"
CHATGPT_MODEL_ID = f"{FUNCTION_ID}.chatgpt"
ROOT = Path(__file__).resolve().parent
PIPE_FILE = ROOT / "openclaw_pipe.py"
LOCAL_IDENTITY_FILE = ROOT / ".pipe_device_identity.json"
BACKUP_DIR = ROOT / "backups"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


DEFAULTS = {
    "owui_url": env("OWUI_URL", "http://localhost:8080").rstrip("/"),
    "owui_email": env("OWUI_EMAIL", ""),
    "owui_password": env("OWUI_PASSWORD", ""),
    "gateway_url": env("GATEWAY_URL", "localhost:18789"),
    "gateway_token": env("GATEWAY_TOKEN", ""),
    "agent_id": env("AGENT_ID", "main"),
    "state_dir": env("OPENCLAW_BRIDGE_STATE_DIR", "/data/openclaw-bridge"),
    "owui_api_base_url": env("OWUI_API_BASE_URL", env("OWUI_URL", "http://localhost:8080")).rstrip("/"),
    "owui_api_key": env("OWUI_API_KEY", ""),
    "chatgpt_model": env("CHATGPT_MODEL", "openai/gpt-5.5"),
    "file_server_base_url": env("FILE_SERVER_BASE_URL", ""),
}


def info(msg: str) -> None:
    print(f"  [ok] {msg}")


def warn(msg: str) -> None:
    print(f"  [warn] {msg}")


def fail(msg: str) -> None:
    print(f"  [fail] {msg}")


def section(msg: str) -> None:
    print(f"\n== {msg} ==")


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


def require_config(args: argparse.Namespace, *, need_gateway: bool) -> None:
    missing = []
    if not args.owui_email:
        missing.append("OWUI_EMAIL")
    if not args.owui_password:
        missing.append("OWUI_PASSWORD")
    if need_gateway and not args.gateway_token:
        missing.append("GATEWAY_TOKEN")
    if missing:
        raise SystemExit(
            "Missing required env/args: " + ", ".join(missing) + "\n"
            "Set OWUI_URL, OWUI_EMAIL, OWUI_PASSWORD, GATEWAY_URL, "
            "GATEWAY_TOKEN, AGENT_ID."
        )


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
    return DEFAULT_MODEL_ID in ids and CHATGPT_MODEL_ID in ids


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


def update_or_create_function(client: OwuiClient) -> dict:
    if not PIPE_FILE.exists():
        raise SystemExit(f"Pipe file not found: {PIPE_FILE}")
    pipe_code = PIPE_FILE.read_text()
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


def update_valves(
    client: OwuiClient,
    args: argparse.Namespace,
    preserved_valves: dict | None = None,
) -> dict:
    current = get_valves(client)
    preserved_valves = preserved_valves or {}
    existing = {**preserved_valves, **current}
    if preserved_valves and set(preserved_valves) - set(current):
        missing = ", ".join(sorted(set(preserved_valves) - set(current)))
        warn(f"Function update dropped valves; restoring: {missing}")
    ident = choose_identity(existing)
    ident_json = json.dumps(ident, separators=(",", ":"))

    required = {
        "GATEWAY_URL": args.gateway_url,
        "GATEWAY_TOKEN": args.gateway_token,
        "AGENT_ID": args.agent_id,
        "ENABLE_FILE_SERVER": True,
        "DEVICE_IDENTITY": ident_json,
        "STATE_DIR": args.state_dir,
        "USE_OWUI_FILES": True,
        "SEND_STOP_ON_CANCEL": True,
        "OWUI_BASE_URL": args.owui_api_base_url,
        "CHATGPT_MODEL": args.chatgpt_model,
    }
    if args.owui_api_key:
        required["OWUI_API_KEY"] = args.owui_api_key
    if args.file_server_base_url:
        required["FILE_SERVER_BASE_URL"] = args.file_server_base_url

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


def chatgpt_route_smoke_test(client: OwuiClient) -> bool:
    status, payload = client.request(
        "POST",
        "/api/chat/completions",
        {
            "model": CHATGPT_MODEL_ID,
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
                "chat_id": f"install-chatgpt-smoke-{int(time.time())}",
                "user_id": "install-chatgpt-smoke",
            },
        },
        timeout=120,
    )
    if status != 200 or not isinstance(payload, dict):
        fail(f"ChatGPT route smoke HTTP failed: {payload}")
        return False

    content = (
        payload.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    if "Model: openai/gpt-5.5" in content:
        info("ChatGPT route smoke test passed")
        return True

    fail(f"ChatGPT route smoke failed: {content[:500]}")
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

    print(json.dumps({
        "function_exists": exists,
        "is_active": active,
        "is_global": global_,
        "model_visible": model,
        "has_gateway_url": bool(valves.get("GATEWAY_URL")),
        "has_gateway_token": bool(valves.get("GATEWAY_TOKEN")),
        "has_device_identity": bool(ident),
        "device_id": ident.get("id") if ident else None,
        "device_paired": paired,
    }, indent=2))
    return all([exists, active, global_, model, ident, paired])


def install_or_repair(client: OwuiClient, args: argparse.Namespace) -> dict:
    section("Function")
    preserved_valves = update_or_create_function(client)
    ensure_active_global(client)

    section("Valves")
    valves = update_valves(client, args, preserved_valves=preserved_valves)

    section("Model discovery")
    if model_exists(client):
        info(f"Models {DEFAULT_MODEL_ID} and {CHATGPT_MODEL_ID} are visible")
    else:
        raise SystemExit(
            f"Models {DEFAULT_MODEL_ID} and {CHATGPT_MODEL_ID} "
            "are not both visible after enabling"
        )
    return valves


def run(args: argparse.Namespace) -> None:
    need_gateway = args.command in {"install", "repair", "healthcheck"}
    require_config(args, need_gateway=need_gateway)
    client = OwuiClient(args.owui_url, args.owui_email, args.owui_password)

    section("Login")
    client.login()

    if args.command == "status":
        section("Status")
        ok = print_status(client)
        raise SystemExit(0 if ok else 1)

    if args.command in {"install", "repair"}:
        valves = install_or_repair(client, args)
        section("Smoke test")
        ok = smoke_test(client, repair_pairing=args.auto_approve, valves=valves)
        section("ChatGPT route smoke test")
        chatgpt_ok = chatgpt_route_smoke_test(client)
        raise SystemExit(0 if ok and chatgpt_ok else 1)

    if args.command == "healthcheck":
        section("Status")
        status_ok = print_status(client)
        section("Smoke test")
        valves = get_valves(client)
        smoke_ok = smoke_test(client, repair_pairing=args.auto_approve, valves=valves)
        section("ChatGPT route smoke test")
        chatgpt_ok = chatgpt_route_smoke_test(client)
        raise SystemExit(0 if status_ok and smoke_ok and chatgpt_ok else 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install, repair, and health-check the OpenClaw OWUI pipe."
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="install",
        choices=["install", "repair", "status", "healthcheck"],
    )
    parser.add_argument("--owui-url", default=DEFAULTS["owui_url"])
    parser.add_argument("--owui-email", default=DEFAULTS["owui_email"])
    parser.add_argument("--owui-password", default=DEFAULTS["owui_password"])
    parser.add_argument("--gateway-url", default=DEFAULTS["gateway_url"])
    parser.add_argument("--gateway-token", default=DEFAULTS["gateway_token"])
    parser.add_argument("--agent-id", default=DEFAULTS["agent_id"])
    parser.add_argument("--state-dir", default=DEFAULTS["state_dir"])
    parser.add_argument("--owui-api-base-url", default=DEFAULTS["owui_api_base_url"])
    parser.add_argument("--owui-api-key", default=DEFAULTS["owui_api_key"])
    parser.add_argument("--chatgpt-model", default=DEFAULTS["chatgpt_model"])
    parser.add_argument("--file-server-base-url", default=DEFAULTS["file_server_base_url"])
    parser.add_argument(
        "--auto-approve",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Approve a matching pending OpenClaw device when smoke test needs pairing.",
    )
    return parser


if __name__ == "__main__":
    run(build_parser().parse_args())
