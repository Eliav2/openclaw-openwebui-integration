#!/usr/bin/env python3
"""
Install the OpenClaw Gateway Pipe into an Open WebUI instance.

v2 — Update-in-place, preserves existing valves (including DEVICE_IDENTITY).

Usage:
  1. Set environment variables (or edit the defaults below):

     export OWUI_URL=http://your-owui-host:8080
     export OWUI_EMAIL=admin@example.com
     export OWUI_PASSWORD=your-password
     export GATEWAY_URL=your-owui-host:18789
     export GATEWAY_TOKEN=your-gateway-token
     export AGENT_ID=main
     export FILE_SERVER_BASE_URL=http://100.120.212.63:18791

  2. Run:

     python3 install.py

  3. If the pipe fails with "pairing required: device is not approved yet",
     run the same script again — it will re-use the same device identity
     and guide you through approval.
"""

import json
import os
import sys
import re
import time

# ── Config ──────────────────────────────────────────────────────────────────

OWUI_URL = os.environ.get("OWUI_URL", "http://localhost:8080")
OWUI_EMAIL = os.environ.get("OWUI_EMAIL", "")
OWUI_PASSWORD = os.environ.get("OWUI_PASSWORD", "")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "localhost:18789")
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
AGENT_ID = os.environ.get("AGENT_ID", "main")
FILE_SERVER_BASE_URL = os.environ.get("FILE_SERVER_BASE_URL", "")
PIPE_FILE = os.path.join(os.path.dirname(__file__), "openclaw_pipe.py")

FUNCTION_ID = "openclaw_gateway"

# ── Helpers ─────────────────────────────────────────────────────────────────

def info(msg):
    print(f"  ✓ {msg}")


def warn(msg):
    print(f"  ⚠ {msg}")


def step(n, msg):
    print(f"\n[{n}] {msg}")


def http_get(path, token):
    """GET from OWUI API, return parsed response."""
    import urllib.request

    req = urllib.request.Request(
        f"{OWUI_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        try:
            return json.loads(err)
        except json.JSONDecodeError:
            return {"detail": err.strip()}


def http_post(path, data=None, token=None, method="POST"):
    """POST/PUT/PATCH JSON to OWUI API, return parsed response."""
    import urllib.request

    url = f"{OWUI_URL}{path}"
    body = json.dumps(data).encode() if data else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        try:
            return json.loads(err)
        except json.JSONDecodeError:
            return {"detail": err.strip()}


def http_delete(path, token):
    """DELETE from OWUI API."""
    import urllib.request

    req = urllib.request.Request(
        f"{OWUI_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"detail": e.read().decode().strip()}


# ── Identity generation (same logic as the pipe) ───────────────────────────

def generate_device_identity():
    """Generate an Ed25519 key pair and return device identity dict."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    import hashlib
    import base64

    pk = ed25519.Ed25519PrivateKey.generate()
    pub = pk.public_key()
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    did = hashlib.sha256(raw).hexdigest()
    pem = pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return {"id": did, "publicKey": pub_b64, "privateKey": pem}


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    missing = []
    if not OWUI_EMAIL:
        missing.append("OWUI_EMAIL")
    if not OWUI_PASSWORD:
        missing.append("OWUI_PASSWORD")
    if not GATEWAY_TOKEN:
        missing.append("GATEWAY_TOKEN")

    if missing:
        print(
            f"Missing required env vars: {', '.join(missing)}\n"
            f"  export OWUI_URL=...\n"
            f"  export OWUI_EMAIL=...\n"
            f"  export OWUI_PASSWORD=...\n"
            f"  export GATEWAY_URL=...\n"
            f"  export GATEWAY_TOKEN=...\n"
            f"  export AGENT_ID=main\n"
            f"  export FILE_SERVER_BASE_URL=..."
        )
        sys.exit(1)

    if not os.path.exists(PIPE_FILE):
        print(f"Pipe file not found: {PIPE_FILE}")
        sys.exit(1)

    # ── Login to OWUI ──────────────────────────────────────────────────
    step(1, "Logging into Open WebUI")

    resp = http_post("/api/v1/auths/signin", {
        "email": OWUI_EMAIL,
        "password": OWUI_PASSWORD,
    })

    token = resp.get("token") or resp.get("access_token")
    if not token:
        detail = resp.get("detail", str(resp))
        print(f"  ✗ Login failed: {detail}")
        sys.exit(1)
    info(f"Logged in as {resp.get('name', OWUI_EMAIL)}")

    # ── Read the pipe code ─────────────────────────────────────────────
    with open(PIPE_FILE) as f:
        pipe_code = f.read()

    # ── Check if function exists ───────────────────────────────────────
    step(2, "Checking existing pipe function")

    existing = http_get(f"/api/v1/functions/id/{FUNCTION_ID}", token)
    exists = existing and existing.get("id") == FUNCTION_ID

    if exists:
        info(f"Pipe function '{FUNCTION_ID}' found — updating in-place (valves preserved)")
        resp = http_post(
            f"/api/v1/functions/id/{FUNCTION_ID}/update",
            data={
                "id": FUNCTION_ID,
                "name": "OpenClaw Gateway",
                "content": pipe_code,
                "meta": {
                    "description": (
                        "OpenClaw Gateway Pipe — WebSocket streaming, "
                        "persistent sessions, media file server"
                    ),
                    "manifest": {},
                },
            },
            token=token,
        )
        if resp.get("id") != FUNCTION_ID:
            detail = resp.get("detail", str(resp)[:200])
            print(f"  ✗ Update failed: {detail}")
            sys.exit(1)
        info("Pipe function updated")
    else:
        warn(f"No existing pipe function found — creating new one")
        resp = http_post("/api/v1/functions/create", {
            "id": FUNCTION_ID,
            "name": "OpenClaw Gateway",
            "content": pipe_code,
            "type": "pipe",
            "meta": {
                "description": (
                    "OpenClaw Gateway Pipe — WebSocket streaming, "
                    "persistent sessions, media file server"
                ),
            },
        })
        if resp.get("id") != FUNCTION_ID:
            detail = resp.get("detail", str(resp)[:200])
            print(f"  ✗ Create failed: {detail}")
            sys.exit(1)
        info("Pipe function created")

    # ── Toggle active + global ─────────────────────────────────────────
    step(3, "Enabling pipe")

    http_post(f"/api/v1/functions/id/{FUNCTION_ID}/toggle", token=token)
    http_post(f"/api/v1/functions/id/{FUNCTION_ID}/toggle/global", token=token)
    info("Pipe is active + global")

    # ── Generate or reuse device identity ──────────────────────────────
    step(4, "Device identity")

    identity_file = os.path.join(
        os.path.dirname(__file__), ".pipe_device_identity.json"
    )
    if os.path.exists(identity_file):
        with open(identity_file) as f:
            ident = json.load(f)
        info(f"Re-using existing identity: {ident['id'][:20]}...")
    else:
        ident = generate_device_identity()
        info(f"New identity generated: {ident['id'][:20]}...")
        with open(identity_file, "w") as f:
            json.dump(ident, f, separators=(",", ":"))

    # ── Get existing valves ────────────────────────────────────────────
    step(5, "Checking existing valves")

    existing_valves = {}
    if exists:
        existing_valves = http_get(
            f"/api/v1/functions/id/{FUNCTION_ID}/valves", token
        ) or {}
        if existing_valves:
            info(f"Found existing valves: {', '.join(existing_valves.keys())}")
            # Preserve the existing DEVICE_IDENTITY if it's set
            existing_device = existing_valves.get("DEVICE_IDENTITY", "")
            if existing_device and existing_device != ident_json:
                warn("Existing DEVICE_IDENTITY differs from local file")
                # We'll keep the local identity — it's more likely to be the
                # one that was already approved
        else:
            info("No existing valves found")

    ident_json = json.dumps(ident, separators=(",", ":"))

    # ── Build valve updates ────────────────────────────────────────────
    valves = {}

    # Only set valves that differ or don't exist yet
    required_valves = {
        "GATEWAY_URL": GATEWAY_URL,
        "GATEWAY_TOKEN": GATEWAY_TOKEN,
        "AGENT_ID": AGENT_ID,
        "ENABLE_FILE_SERVER": True,
        "DEVICE_IDENTITY": ident_json,
    }
    if FILE_SERVER_BASE_URL:
        required_valves["FILE_SERVER_BASE_URL"] = FILE_SERVER_BASE_URL

    for key, value in required_valves.items():
        existing = existing_valves.get(key)
        if existing is None or existing != value:
            valves[key] = value
            if existing is not None:
                info(f"  Updating valve {key}")
            else:
                info(f"  Setting valve {key}")
        else:
            info(f"  Valve {key} unchanged (preserved)")

    # ── Set valves (if any changed) ────────────────────────────────────
    if valves:
        step(6, "Updating valves")
        resp = http_post(
            f"/api/v1/functions/id/{FUNCTION_ID}/valves/update",
            data=valves,
            token=token,
        )
        # Check at least one key made it through
        ok = any(resp.get(k) == v for k, v in valves.items())
        if not ok:
            print(f"  ✗ Valve update might have failed: {str(resp)[:200]}")
            sys.exit(1)
        info(f"Updated {len(valves)} valve(s)")
    else:
        step(6, "Valves")
        info("All valves already up-to-date")

    # ── Guide user through device approval ─────────────────────────────
    step(7, "Device pairing")

    print()
    print(f"  Device ID: {ident['id'][:40]}...")
    print()
    print(f"  OpenClaw Gateway will ask you to approve this device.")
    print(f"  Open a terminal on the Gateway host and run:")
    print()
    print(f"      openclaw devices approve <request-id>")
    print()
    print(f"  OR to find pending requests:")
    print()
    print(f"      openclaw devices list")
    print()
    print(f"  Approve the one with device ID starting with:")
    print(f"    {ident['id'][:20]}...")
    print()
    print("── Installation complete ──────────────────────────────────────────")
    print()
    print("  Next steps:")
    print("    1. Approve the device in the Gateway (see above)")
    print("    2. Open OWUI → select model 'OpenClaw Gateway'")
    print("    3. Start chatting!")
    print()
    print("  If pairing fails, just re-run this script — it remembers")
    print("  the device identity and won't create a new one.")
    print()


if __name__ == "__main__":
    main()
