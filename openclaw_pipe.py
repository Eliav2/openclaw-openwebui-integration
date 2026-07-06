"""
OpenClaw Gateway Pipe for Open WebUI
=====================================

A self-contained Open WebUI Pipe that connects to an OpenClaw Gateway via its
native WebSocket protocol — with a **persistent** singleton connection shared
by all conversations. No reconnect per message, no global lock, and no 60s
idle suicide.

How it works
------------
1. On the first ``pipe()`` call, a ``_GatewayConnection`` singleton is created:
   one WebSocket connection to the Gateway, kept alive with tick keepalives.
2. Each ``pipe()`` call sends ``chat.send`` over the shared connection and
   receives events via an ``asyncio.Queue`` keyed by session + run.
3. The background event loop dispatches incoming Gateway events to the correct
   queue; unmatched events (other sessions, heartbeats, Sender metadata) are
   filtered out efficiently.
4. On WS disconnect, the connection manager auto-reconnects with exponential
   backoff (1s → 2s → 4s → … → 30s max); all consumers survive reconnect.
5. Tool calls render as native OWUI ``<details type="tool_calls">`` blocks.

Requirements
------------
- Open WebUI v0.9+ (tested on v0.10.x)
- OpenClaw Gateway running and accessible
- Python modules: websockets, cryptography, pydantic
  (Open WebUI ships pydantic; websockets and cryptography may need
   manual install depending on your OWUI deployment)

Installation
------------
1. In Open WebUI, go to Admin Panel > Functions
2. Click "+" and choose "Create a function"
3. Set type to "pipe", id to "openclaw_gateway"
4. Paste the entire contents of this file
5. Save and enable the function
6. Configure the valves:
   - GATEWAY_URL: your OpenClaw Gateway host:port (default: localhost:18789)
   - GATEWAY_TOKEN: your gateway API token
   - DEVICE_IDENTITY: (advanced) fallback/import device identity JSON
   - STATE_DIR: persistent bridge state dir (default: /data/openclaw-bridge)
   - AGENT_ID: OpenClaw agent to route to (default: "main")
7. The pipe appears in OWUI as two selectable models:
   - OpenClaw · Default: uses the agent's configured default model
   - ChatGPT · GPT-5.5: patches the session model to CHATGPT_MODEL
"""

import asyncio
import json
import html
import os
import uuid
import logging
import base64
import time
import threading
import sys
import hashlib
import re
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from dataclasses import dataclass, field

import websockets
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def pipe_log(*args):
    """Log to stdout so it appears in OWUI's backend logs."""
    print(f"[openclaw-pipe] {' '.join(str(a) for a in args)}", flush=True)

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Device Identity helpers
# ---------------------------------------------------------------------------

def _generate_device_identity():
    """Generate a fresh Ed25519 key pair and return a dict with id, publicKey,
    and privateKey (PEM)."""
    pk = ed25519.Ed25519PrivateKey.generate()
    pub = pk.public_key()
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )
    pub_b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    did = hashlib.sha256(raw).hexdigest()
    pem = pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode()
    return dict(id=did, publicKey=pub_b64, privateKey=pem)


def _sign_challenge(ident, nonce, ts, token_str=""):
    """Sign the WebSocket challenge using the device identity."""
    parts = [
        "v2", ident["id"], "webchat", "cli", "operator",
        ",".join(GATEWAY_SCOPES), str(ts), token_str, nonce
    ]
    pk = serialization.load_pem_private_key(
        ident["privateKey"].encode(), password=None, backend=default_backend()
    )
    sig = pk.sign(("|".join(parts)).encode())
    return dict(
        id=ident["id"],
        publicKey=ident["publicKey"],
        signature=base64.urlsafe_b64encode(sig).decode().rstrip("="),
        signedAt=ts,
        nonce=nonce
    )


def _parse_device_identity(raw):
    """Parse DEVICE_IDENTITY JSON, handling unescaped newlines in PEM keys."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        def _fix_pem(m):
            return m.group(1) + m.group(2).replace("\n", "").replace("\r", "") + m.group(3)
        fixed = re.sub(
            r'("privateKey":\s*")(.*?)("[, \\}])',
            _fix_pem, raw, flags=re.DOTALL
        )
        return json.loads(fixed)
    except Exception:
        return None


def _state_dir(path=None):
    """Return the persistent bridge state directory, creating it if possible."""
    root = path or os.environ.get("OPENCLAW_BRIDGE_STATE_DIR") or "/data/openclaw-bridge"
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
        return root
    except Exception as ex:
        fallback = "/tmp/openclaw-bridge"
        os.makedirs(fallback, mode=0o700, exist_ok=True)
        pipe_log(f"STATE_DIR unavailable ({root}: {ex}); using {fallback}")
        return fallback


def _read_json_file(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as ex:
        pipe_log(f"Failed reading {path}: {ex}")
        return None


def _write_json_file(path, data):
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except Exception as ex:
        pipe_log(f"Failed writing {path}: {ex}")
        return False


# ---------------------------------------------------------------------------
# File Server (media delivery)
# ---------------------------------------------------------------------------

MEDIA_DIR = "/tmp/openclaw-pipe-media"
MEDIA_BASE_URL = "http://your-owui-host:18791"
GATEWAY_SCOPES = ["operator.admin", "operator.read", "operator.write"]

_file_server_started = False


def _resolve_media(text, base_url=None):
    """Convert MEDIA:filename directives to embedded media."""
    if base_url is None:
        base_url = MEDIA_BASE_URL
    prefix = "MEDIA:"
    if prefix not in text:
        return text, False
    idx = text.index(prefix)
    after_prefix = text[idx + len(prefix):].strip()
    fname = after_prefix.split()[0] if after_prefix else ""
    if not fname:
        return text, False
    media_dir = MEDIA_DIR
    fpath = os.path.join(media_dir, fname)
    if os.path.isfile(fpath):
        try:
            with open(fpath, "rb") as f:
                raw = f.read()
            b64 = base64.b64encode(raw).decode()
            mime = mimetypes.guess_type(fname)[0] or "image/png"
            data_uri = f"data:{mime};base64,{b64}"
            rest = after_prefix[len(fname):].strip()
            result = f"![{fname}]({data_uri})"
            if rest:
                result += "\n" + rest
            return result, True
        except Exception as ex:
            pipe_log(f"  base64 fallback failed: {ex}")
    url = f"{base_url.rstrip('/')}/{fname}"
    rest = after_prefix[len(fname):].strip()
    result = f"![{fname}]({url})"
    if rest:
        result += "\n" + rest
    return result, True


def _extract_request_bearer(__request__):
    """Return a Bearer token from the current OWUI request, when available."""
    if not __request__:
        return None
    headers = getattr(__request__, "headers", None)
    if not headers:
        return None
    auth = headers.get("authorization") or headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return None


def _multipart_body(field_name, file_path, filename, mime):
    boundary = "----openclawowui" + uuid.uuid4().hex
    with open(file_path, "rb") as f:
        raw = f.read()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return boundary, head + raw + tail


def _upload_owui_file(file_path, base_url, token):
    """Upload one file to OWUI Files API and return the file object."""
    if not token:
        raise RuntimeError("missing OWUI bearer token")
    filename = os.path.basename(file_path)
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    boundary, body = _multipart_body("file", file_path, filename, mime)
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/files/?process=false",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"OWUI file upload failed: {exc.code} {detail}") from exc


async def _resolve_media_via_owui(
    text,
    *,
    base_url,
    token,
    __event_emitter__,
):
    """Upload a MEDIA: file to OWUI and attach it to the current message."""
    prefix = "MEDIA:"
    if prefix not in text:
        return text, False
    idx = text.index(prefix)
    before = text[:idx]
    after_prefix = text[idx + len(prefix):].strip()
    fname = after_prefix.split()[0] if after_prefix else ""
    if not fname:
        return text, False
    if ".." in fname or "/" in fname:
        return text, False
    fpath = os.path.join(MEDIA_DIR, fname)
    if not os.path.isfile(fpath):
        return text, False

    file_obj = _upload_owui_file(fpath, base_url, token)
    if __event_emitter__:
        await __event_emitter__(
            {
                "type": "files",
                "data": {"files": [file_obj]},
            }
        )

    file_id = file_obj.get("id")
    rest = after_prefix[len(fname):].strip()
    mime = file_obj.get("meta", {}).get("content_type") or mimetypes.guess_type(fname)[0] or ""
    content_url = f"/api/v1/files/{file_id}/content" if file_id else ""
    if content_url and mime.startswith("image/"):
        replacement = f"![{fname}]({content_url})"
    elif content_url:
        replacement = f"[{fname}]({content_url})"
    else:
        replacement = f"`{fname}`"
    if rest:
        replacement += "\n" + rest
    return before + replacement, True


def _start_file_server(port=18791):
    """Start a minimal HTTP server for media files. Starts once per process."""
    global _file_server_started
    if _file_server_started:
        return
    directory = "/tmp/openclaw-pipe-media"
    os.makedirs(directory, exist_ok=True)
    Path(directory, "health").write_text("ok")

    class _Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def _handle_upload(self):
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"empty")
                return
            body = self.rfile.read(length)
            path = self.path.strip("/").split("?")[0]
            if not path or ".." in path or "/" in path:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"bad path")
                return
            dest = os.path.join(directory, os.path.basename(path))
            with open(dest, "wb") as f:
                f.write(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"ok:{path}".encode())

        def do_PUT(self):
            self._handle_upload()

        def do_POST(self):
            self._handle_upload()

    try:
        server = HTTPServer(("0.0.0.0", port), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        _file_server_started = True
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class GatewayError(Exception):
    """Raised when the Gateway WebSocket handshake fails."""
    pass


# ---------------------------------------------------------------------------
# Persistent Gateway Connection (singleton)
# ---------------------------------------------------------------------------

@dataclass
class _Consumer:
    """An active run consumer — its ``asyncio.Queue`` receives events."""
    session_key: str
    run_id: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=500))


class _GatewayConnection:
    """Singleton persistent WebSocket connection to the Gateway.

    Maintains one WS connection, dispatches events to per-run queues, and
    auto-reconnects on disconnect with exponential backoff.
    """

    def __init__(self, valves_ref):
        # Weak reference to Pipe valves (re-read on each call)
        self._valves = valves_ref
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._event_loop_task: asyncio.Task | None = None
        self._init_lock = asyncio.Lock()

        # Identity
        self._ident: dict | None = None
        self._device_token: str | None = None

        # Request-response futures (keyed by request id)
        self._pending_reqs: dict[str, asyncio.Future] = {}

        # Run consumers (keyed by f"{session_key}:{run_id}")
        self._consumers: dict[str, _Consumer] = {}

        # Reconnect state
        self._reconnect_attempt = 0
        self._max_backoff = 30  # seconds
        self._stopped = False

        # Next request id
        self._next_req_id = 1

        # Counter for the event loop
        self._event_count = 0

    # ── Public API ──────────────────────────────────────────────────

    async def ensure_connected(self):
        """Ensure the WS connection is up; connect/reconnect if needed."""
        if self._ws and self._event_loop_task and not self._event_loop_task.done():
            return
        async with self._init_lock:
            if self._ws and self._event_loop_task and not self._event_loop_task.done():
                return
            self._stopped = False
            await self._connect_and_start()

    async def send_request(self, method: str, params: dict, timeout: float = 10) -> dict:
        """Send a request and wait for the response."""
        req_id = str(self._next_req_id)
        self._next_req_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending_reqs[req_id] = fut

        try:
            await self._ws.send(json.dumps(dict(
                type="req", id=req_id, method=method, params=params
            )))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_reqs.pop(req_id, None)

    def register_consumer(self, session_key: str, run_id: str) -> asyncio.Queue:
        """Register an event consumer queue for a (session, run) pair."""
        key = f"{session_key}:{run_id}"
        if key not in self._consumers:
            self._consumers[key] = _Consumer(session_key=session_key, run_id=run_id)
            pipe_log(f"  registered consumer: {key[:60]}...")
        return self._consumers[key].queue

    def unregister_consumer(self, session_key: str, run_id: str):
        """Remove a consumer queue."""
        key = f"{session_key}:{run_id}"
        self._consumers.pop(key, None)
        pipe_log(f"  unregistered consumer: {key[:60]}...")

    async def abort(self, session_key: str, run_id: str):
        """Send chat.abort for an active run."""
        try:
            await self._ws.send(json.dumps(dict(
                type="req", id="abort", method="chat.abort", params=dict(
                    sessionKey=session_key,
                    runId=run_id,
                )
            )))
            pipe_log(f"  sent chat.abort for run {run_id[:20]}...")
        except Exception as e:
            pipe_log(f"  abort send failed: {e}")

    async def disconnect(self):
        """Gracefully close the connection."""
        self._stopped = True
        if self._event_loop_task:
            self._event_loop_task.cancel()
            try:
                await self._event_loop_task
            except asyncio.CancelledError:
                pass
            self._event_loop_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ── Internal ────────────────────────────────────────────────────

    async def _connect_and_start(self):
        """Connect, handshake, and start the event loop task."""
        valves = self._valves()
        parts = valves.GATEWAY_URL.rsplit(":", 1)
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 18789
        token = valves.GATEWAY_TOKEN

        if not token:
            raise GatewayError("No GATEWAY_TOKEN configured")

        # Device identity
        self._ensure_identity(valves)

        # Connect
        pipe_log(f"Connecting to ws://{host}:{port}")
        ws = await websockets.connect(f"ws://{host}:{port}", ping_interval=None)

        # Handshake: receive challenge
        challenge = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if challenge.get("event") != "connect.challenge":
            raise GatewayError("Bad handshake — expected connect.challenge")

        c = challenge["payload"]
        signed = _sign_challenge(self._ident, c["nonce"], c["ts"], token_str=token)

        auth = dict(token=token)
        if self._device_token:
            auth["deviceToken"] = self._device_token

        await ws.send(json.dumps(dict(
            type="req", id="1", method="connect", params=dict(
                minProtocol=4, maxProtocol=4,
                client=dict(id="webchat", version="1",
                            platform="linux", mode="cli"),
                role="operator",
                scopes=GATEWAY_SCOPES,
                auth=auth,
                device=signed,
                locale="en-US",
                userAgent="openclaw-owui-pipe/1.0",
                caps=["agent-events", "tool-events"]
            )
        )))

        resp = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if not resp.get("ok"):
            raise GatewayError(
                str(resp.get("error", {}).get("message", "connect failed"))
            )

        # Capture device token for future reconnects
        hello_ok = resp.get("payload", {})
        if hello_ok.get("auth", {}).get("deviceToken"):
            self._device_token = hello_ok["auth"]["deviceToken"]
            self._save_device_token(valves)
            pipe_log(f"Device token captured: {self._device_token[:20]}...")

        self._ws = ws
        self._reconnect_attempt = 0
        pipe_log("Connected to Gateway (persistent)")

        # Start the background event loop
        self._event_loop_task = asyncio.create_task(self._event_loop())

    def _identity_path(self, valves):
        return os.path.join(_state_dir(getattr(valves, "STATE_DIR", "")), "identity.json")

    def _device_token_path(self, valves):
        return os.path.join(_state_dir(getattr(valves, "STATE_DIR", "")), "device-token.json")

    def _load_device_token(self, valves):
        if self._device_token:
            return
        data = _read_json_file(self._device_token_path(valves))
        if isinstance(data, dict) and data.get("deviceToken"):
            self._device_token = data["deviceToken"]
            pipe_log("Using persisted device token")

    def _save_device_token(self, valves):
        if not self._device_token:
            return
        _write_json_file(
            self._device_token_path(valves),
            {"deviceToken": self._device_token, "deviceId": self._ident.get("id")},
        )

    def _ensure_identity(self, valves):
        """Load or generate device identity from stable storage.

        Order matters for restart safety:
        1. state-dir identity file
        2. DEVICE_IDENTITY valve
        3. generate once and persist
        """
        if self._ident:
            return
        identity_path = self._identity_path(valves)
        ident = _read_json_file(identity_path)
        if ident and ident.get("id") and ident.get("privateKey"):
            self._ident = ident
            pipe_log(f"Using device identity from {identity_path}")
            self._load_device_token(valves)
            return

        if valves.DEVICE_IDENTITY:
            ident = _parse_device_identity(valves.DEVICE_IDENTITY)
            if ident and ident.get("id") and ident.get("privateKey"):
                self._ident = ident
                _write_json_file(identity_path, ident)
                pipe_log("Using device identity from valve and persisted it to state dir")
                self._load_device_token(valves)
                return
            pipe_log("Failed to parse DEVICE_IDENTITY valve; generating new identity")

        self._ident = _generate_device_identity()
        _write_json_file(identity_path, self._ident)
        pipe_log(f"Generated new device identity and saved it to {identity_path}")
        print(
            f"DEVICE_IDENTITY={json.dumps(self._ident, separators=(',',':'))}",
            flush=True
        )

    async def _event_loop(self):
        """Background task: reads WS messages and dispatches them."""
        while not self._stopped:
            try:
                msg = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=90))

                # ── Request-response matching ──
                if msg.get("type") == "res":
                    req_id = msg.get("id")
                    fut = self._pending_reqs.get(req_id)
                    if fut and not fut.done():
                        if msg.get("ok") is False:
                            err = msg.get("error", {})
                            fut.set_exception(
                                GatewayError(
                                    err.get("message")
                                    or err.get("code")
                                    or "request failed"
                                )
                            )
                        else:
                            fut.set_result(msg.get("payload", {}))
                    continue

                # ── Tick keepalive (silently consume) ──
                if msg.get("event") == "tick" or msg.get("payload", {}).get("isHeartbeat"):
                    continue

                # ── Event dispatch ──
                if msg.get("type") != "event" or \
                   msg.get("event") not in ("agent", "chat"):
                    continue

                payload = msg.get("payload", {})
                evt_session = payload.get("sessionKey", "")
                evt_run_id = payload.get("runId", "")

                # Find the matching consumer
                if evt_session and evt_run_id:
                    key = f"{evt_session}:{evt_run_id}"
                    consumer = self._consumers.get(key)
                    if consumer:
                        self._event_count += 1
                        await consumer.queue.put(msg)
                        continue

                # If session matches but run_id doesn't (e.g. lifecycle/end
                # for a completed run that was already unregistered), drop it.
                # If nothing matches at all — drop it.
                # This efficiently filters cross-session bleed (P16).

            except asyncio.TimeoutError:
                # No events for 90s but connection is still alive
                continue
            except websockets.exceptions.ConnectionClosed:
                pipe_log("WS disconnected — reconnecting...")
                await self._reconnect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                pipe_log(f"Event loop error: {e}")
                await self._reconnect()

    async def _reconnect(self):
        """Reconnect with exponential backoff."""
        self._reconnect_attempt += 1
        delay = min(2 ** (self._reconnect_attempt - 1), self._max_backoff)
        pipe_log(f"  reconnect in {delay}s (attempt {self._reconnect_attempt})")
        await asyncio.sleep(delay)
        self._ws = None
        try:
            await self._connect_and_start()
        except Exception as e:
            pipe_log(f"  reconnect failed: {e}")
            # Try again with the event loop


# Module-level singleton
_gateway_connection: _GatewayConnection | None = None
_gateway_init_lock = asyncio.Lock()


async def _get_gateway_connection(valves_getter) -> _GatewayConnection:
    """Get or create the singleton Gateway connection."""
    global _gateway_connection
    if _gateway_connection is not None:
        await _gateway_connection.ensure_connected()
        return _gateway_connection
    async with _gateway_init_lock:
        if _gateway_connection is not None:
            await _gateway_connection.ensure_connected()
            return _gateway_connection
        conn = _GatewayConnection(valves_getter)
        await conn.ensure_connected()
        _gateway_connection = conn
        return conn


# ---------------------------------------------------------------------------
# Pipe class (Open WebUI entry point)
# ---------------------------------------------------------------------------

class Pipe:
    """
    Open WebUI Pipe that routes messages through OpenClaw Gateway via
    a persistent WebSocket connection.

    Valves (configured in OWUI admin panel):
    - GATEWAY_URL: OpenClaw Gateway address (default "localhost:18789")
    - GATEWAY_TOKEN: Gateway API token (required)
    - DEVICE_IDENTITY: (optional) persisted device identity JSON
    - AGENT_ID: target agent (default "main")
    - ENABLE_FILE_SERVER: start media file server (default True)
    - USE_OWUI_FILES: upload MEDIA files into OWUI Files API (default True)
    """

    class Valves(BaseModel):
        GATEWAY_URL: str = Field(
            default="localhost:18789",
            description="OpenClaw Gateway address (host:port)"
        )
        GATEWAY_TOKEN: str = Field(
            default="",
            description="OpenClaw Gateway API token"
        )
        DEVICE_IDENTITY: str = Field(
            default="",
            description="(Advanced) Fallback/import device identity JSON"
        )
        STATE_DIR: str = Field(
            default="/data/openclaw-bridge",
            description="Persistent state directory for identity and device token"
        )
        AGENT_ID: str = Field(
            default="main",
            description="Target agent identifier"
        )
        ENABLE_FILE_SERVER: bool = Field(
            default=True,
            description="Start a minimal HTTP server for media files"
        )
        USE_OWUI_FILES: bool = Field(
            default=True,
            description="Upload MEDIA files to OWUI Files API before falling back to file server"
        )
        OWUI_BASE_URL: str = Field(
            default="http://127.0.0.1:8080",
            description="Open WebUI base URL for Files API uploads"
        )
        OWUI_API_KEY: str = Field(
            default="",
            description="Optional OWUI API key for file uploads; request bearer token is preferred"
        )
        FILE_SERVER_BASE_URL: str = Field(
            default="http://your-owui-host:18791",
            description="Public URL for the file server (for MEDIA: resolution)"
        )
        CHATGPT_MODEL: str = Field(
            default="openai/gpt-5.5",
            description="OpenClaw model override used by the ChatGPT manifold model"
        )

    def __init__(self):
        self.valves = self.Valves()
        self._active_tool_args: dict[str, str] = {}
        # Will be set per pipe() call
        self._current_session_key: str | None = None
        self._current_run_id: str | None = None
        self._connection: _GatewayConnection | None = None

    def pipes(self):
        """Expose multiple OWUI model-selector entries from one pipe."""
        return [
            {"id": "default", "name": "OpenClaw · Default"},
            {"id": "chatgpt", "name": "ChatGPT · GPT-5.5"},
        ]

    def _selected_preset(self, body):
        model = str(body.get("model", ""))
        suffix = model.rsplit(".", 1)[-1].rsplit("/", 1)[-1]
        if suffix == "chatgpt":
            return "chatgpt"
        return "default"

    def _model_override_for_preset(self, preset):
        if preset == "chatgpt":
            return self.valves.CHATGPT_MODEL.strip() or None
        return None

    async def pipe(self, body, __event_emitter__,
                   __user__=None, __metadata__=None, __request__=None,
                   __task__=None, __task_body__=None):
        """Main pipe entry point — called by Open WebUI for each user message.

        Uses a shared persistent WS connection; no per-message reconnect,
        no global lock, and no 60s timeout.
        """
        if self.valves.ENABLE_FILE_SERVER:
            _start_file_server()
        preset = self._selected_preset(body)
        model_override = self._model_override_for_preset(preset)

        # --- P15: Short-circuit OWUI background tasks ---
        if __task__ and __task__ in (
            "title_generation",
            "tags_generation",
            "follow_up_generation",
            "emoji_generation",
            "autocomplete_generation",
            "query_generation",
        ):
            pipe_log(f"Skipping OWUI background task: {__task__}")
            return

        # --- Extract user message ---
        messages = body.get("messages", [])
        text = messages[-1]["content"] if messages else ""
        if not text:
            yield "No message"
            return

        pipe_log(f"Messages: {len(messages)}, last role: "
                 f"{messages[-1]['role'] if messages else 'NONE'}")

        # --- Get the persistent connection ---
        try:
            conn = await _get_gateway_connection(lambda: self.valves)
        except GatewayError as e:
            yield f"**Gateway connection error:** {e}"
            return

        self._connection = conn

        # --- Derive stable session key ---
        if __metadata__:
            chat_id = (
                __metadata__.get("chat_id")
                or __metadata__.get("session_id")
                or __metadata__.get("conversation_id")
            )
            user_id = __metadata__.get("user_id", "unknown")
        else:
            chat_id = None
            user_id = "unknown"

        if not chat_id:
            chat_id = f"owui-{uuid.uuid4().hex[:12]}"
            pipe_log("WARNING: no chat_id in metadata, generated random:", chat_id)

        preset_suffix = "" if body.get("model") == "openclaw_gateway" else f"-{preset}"
        session_key = (
            f"agent:{self.valves.AGENT_ID}:"
            f"openwebui-{user_id}-{chat_id}{preset_suffix}"
        )
        pipe_log(f"Session key: {session_key}")
        self._current_session_key = session_key

        if model_override:
            try:
                patch_resp = await conn.send_request(
                    "sessions.patch",
                    dict(key=session_key, model=model_override),
                    timeout=10
                )
                resolved = patch_resp.get("resolved", {})
                resolved_model = "/".join(
                    part for part in (
                        resolved.get("modelProvider"),
                        resolved.get("model"),
                    )
                    if part
                )
                if resolved_model != model_override:
                    raise GatewayError(
                        "model override did not apply "
                        f"(wanted {model_override}, got {resolved_model or 'unknown'})"
                    )
                pipe_log(f"Applied model override: {model_override}")
            except Exception as e:
                yield f"**Model selection error:** could not apply `{model_override}`: {e}"
                return

        # --- Send message and get runId ---
        idempotency_key = f"msg-{chat_id}-{time.time()}"
        try:
            send_resp = await conn.send_request(
                "chat.send",
                dict(sessionKey=session_key, message=text,
                     idempotencyKey=idempotency_key),
                timeout=30
            )
        except asyncio.TimeoutError:
            yield "**Timeout:** Gateway did not respond to chat.send"
            return
        except Exception as e:
            yield f"**Error sending message:** {e}"
            return

        our_run_id = send_resp.get("runId", "unknown")
        self._current_run_id = our_run_id
        pipe_log(f"Captured runId: {our_run_id}")

        # --- Register consumer ---
        queue = conn.register_consumer(session_key, our_run_id)

        # --- Consume events ---
        done = False
        event_count = 0
        text_yielded = False
        aborted = False
        first_event_arrived = False
        last_activity_time = time.time()
        grace_after_last_event_s = 45

        try:
            while not done and event_count < 500:
                try:
                    recv_timeout = grace_after_last_event_s if first_event_arrived else 60
                    msg = await asyncio.wait_for(queue.get(), timeout=recv_timeout)
                except asyncio.TimeoutError:
                    timeout_desc = (
                        f"{grace_after_last_event_s}s"
                        if first_event_arrived
                        else "60s"
                    )
                    pipe_log(f"TIMEOUT — no events on queue for {timeout_desc}")
                    break

                event_count += 1
                first_event_arrived = True
                payload = msg.get("payload", {})
                stream = payload.get("stream")
                data = payload.get("data", {})
                name = data.get("name", "")
                phase = data.get("phase", "")
                state = payload.get("state", "")

                # --- P16: Double-check session/run match ---
                evt_session = payload.get("sessionKey", "")
                evt_run_id = payload.get("runId", "")
                if evt_session and evt_session != session_key:
                    pipe_log(f"  queue delivered wrong session: {evt_session[:40]}...")
                    continue
                if evt_run_id and evt_run_id != our_run_id:
                    pipe_log(f"  queue delivered wrong run: {evt_run_id[:20]}...")
                    continue

                # --- Completion signals (multiple sources) ---
                if stream == "lifecycle" and phase == "end":
                    done = True
                    pipe_log("  lifecycle end -> done")
                elif stream == "lifecycle" and phase == "error":
                    yield f"\n\n**Error:** {data.get('error', 'unknown')}"
                    done = True
                    pipe_log("  lifecycle error -> done")

                if state in ("final", "cancelled", "error"):
                    done = True
                    pipe_log(f"  payload state='{state}' -> done")

                if data.get("aborted") is True:
                    done = True
                    pipe_log("  data.aborted -> done")

                # --- Assistant text stream ---
                if stream == "assistant":
                    delta = data.get("delta") or data.get("text") or ""
                    if delta:
                        # Filter Sender metadata
                        if (
                            "Sender (untrusted metadata)" in delta
                            or "UnTrustedMetadata" in delta
                        ):
                            pipe_log("  filtered metadata block")
                            continue
                        text_yielded = True
                        # MEDIA: resolution
                        if "MEDIA:" in delta:
                            handled = False
                            if self.valves.USE_OWUI_FILES:
                                token = (
                                    _extract_request_bearer(__request__)
                                    or self.valves.OWUI_API_KEY
                                )
                                try:
                                    resolved, handled = await _resolve_media_via_owui(
                                        delta,
                                        base_url=self.valves.OWUI_BASE_URL,
                                        token=token,
                                        __event_emitter__=__event_emitter__,
                                    )
                                except Exception as ex:
                                    pipe_log(f"  OWUI file upload failed; falling back: {ex}")
                            if not handled:
                                resolved, handled = _resolve_media(
                                    delta,
                                    base_url=self.valves.FILE_SERVER_BASE_URL
                                )
                            if handled:
                                pipe_log("  resolved MEDIA: directive")
                                yield resolved
                                last_activity_time = time.time()
                                continue
                        yield delta
                        last_activity_time = time.time()

                # --- Tool call events ---
                if stream == "tool":
                    if phase == "start":
                        tool_call_id = data.get("toolCallId", "")
                        args = json.dumps(data.get("args", {}))
                        if tool_call_id:
                            self._active_tool_args[tool_call_id] = args
                        pipe_log(f"  Tool start: {name}")
                        if __event_emitter__:
                            await __event_emitter__(
                                {"type": "status", "data": {
                                    "description": f"🔧 Running {name}...",
                                    "done": False
                                }}
                            )
                        last_activity_time = time.time()

                    elif phase == "result":
                        result = data.get("result", {})
                        result_str = json.dumps(result) if not isinstance(result, str) else result
                        tool_call_id = data.get("toolCallId", "")
                        stored_args = self._active_tool_args.pop(tool_call_id, None)
                        args_str = stored_args or json.dumps(data.get("args", {}))
                        pipe_log(f"  Tool result: {name} ({len(result_str)} chars)")
                        yield (
                            '\n<details type="tool_calls" done="true" '
                            f'id="{html.escape(tool_call_id)}" '
                            f'name="{html.escape(name)}" '
                            f'arguments="{html.escape(args_str[:3000])}" '
                            f'result="{html.escape(result_str[:8000])}" '
                            f'meta="{html.escape(str(data.get("meta",""))[:500])}" '
                            'files="[]" embeds="[]">'
                            f'\n<summary>{html.escape(name)}</summary>\n</details>\n'
                        )
                        if __event_emitter__:
                            await __event_emitter__(
                                {"type": "status", "data": {
                                    "description": f"✅ {name} done", "done": True
                                }}
                            )
                        last_activity_time = time.time()

                # --- Item events (progress) ---
                if stream == "item":
                    pipe_log(f"  Item: kind={data.get('kind','')} "
                             f"status={data.get('status','')} "
                             f"title={str(data.get('title',''))[:50]}")
                    last_activity_time = time.time()

                if (
                    not done
                    and first_event_arrived
                    and time.time() - last_activity_time > grace_after_last_event_s
                ):
                    pipe_log(
                        "  grace timer: no activity for "
                        f"{grace_after_last_event_s}s, self-closing"
                    )
                    done = True

        except asyncio.CancelledError:
            # OWUI stop button → abort the gateway run
            aborted = True
            pipe_log("Generator cancelled — sending chat.abort")
            await conn.abort(session_key, our_run_id)
            raise  # Re-raise to signal proper cancellation

        finally:
            conn.unregister_consumer(session_key, our_run_id)
            self._current_session_key = None
            self._current_run_id = None

        pipe_log(f"DONE — {event_count} events processed, "
                 f"text yielded: {text_yielded}")

        if not aborted and not text_yielded:
            yield "(no response)"
