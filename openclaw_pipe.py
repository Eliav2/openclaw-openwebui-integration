"""
OpenClaw Gateway Pipe for Open WebUI
=====================================

A self-contained Open WebUI Pipe that connects to an OpenClaw Gateway via its
WebSocket protocol. Supports streaming responses, real-time tool call rendering,
and persistent sessions tied to OWUI conversations.

How it works
------------
1. User selects "OpenClaw Gateway" as the model in OWUI
2. Each message is sent to OpenClaw Gateway via WebSocket
3. Assistant responses are streamed back token-by-token (OWUI streaming)
4. Tool calls are rendered as collapsible <details type="tool_calls"> elements
5. Each OWUI chat gets a stable session key on the OpenClaw side

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
   - DEVICE_IDENTITY: (advanced) persist device identity to avoid re-registration
   - AGENT_ID: OpenClaw agent to route to (default: "main")
7. The pipe will appear as a model in your OWUI model selector
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
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler

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
        "v2", ident["id"], "test", "cli", "operator",
        "operator.read,operator.write", str(ts), token_str, nonce
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
    # Some deployments mangle the PEM newlines — try to recover
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


# ---------------------------------------------------------------------------
MEDIA_DIR = "/tmp/openclaw-pipe-media"
MEDIA_BASE_URL = "http://your-owui-host:18791"  # default; can be overridden via valve

# Simple HTTP file server for media served back to OWUI
# ---------------------------------------------------------------------------

_file_server_started = False


def _resolve_media(text, base_url=None):
    """Convert MEDIA:filename directives to embedded data-URI images.
    Reads the file from the local media directory and returns a base64
    data URI so OWUI can render it inline (OWUI strips external img tags).
    Falls back to URL-based markdown if the file is not found.
    Returns (resolved_text, handled) tuple."""
    if base_url is None:
        base_url = MEDIA_BASE_URL
    prefix = "MEDIA:"
    if prefix not in text:
        return text, False
    # Extract filename
    idx = text.index(prefix)
    after_prefix = text[idx + len(prefix):].strip()
    fname = after_prefix.split()[0] if after_prefix else ""
    if not fname:
        return text, False
    # Try to read from local media directory first
    media_dir = MEDIA_DIR  # /tmp/openclaw-pipe-media
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
            pipe_log(f"  resolved MEDIA: via base64 data URI ({len(raw)} bytes)")
            return result, True
        except Exception as ex:
            pipe_log(f"  base64 fallback failed: {ex}")
    # Fallback: URL-based markdown image
    url = f"{base_url.rstrip('/')}/{fname}"
    rest = after_prefix[len(fname):].strip()
    result = f"![{fname}]({url})"
    if rest:
        result += "\n" + rest
    pipe_log(f"  resolved MEDIA: via URL (file not found locally)")
    return result, True


def _start_file_server(port=18791):
    """Start a minimal HTTP server to serve media files back to OWUI.
    Only starts once per process lifetime."""
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
            """Handle PUT/POST file upload."""
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"empty")
                return
            body = self.rfile.read(length)
            # Sanitize path: only allow single filename, no ".."
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
# Pipe class (Open WebUI entry point)
# ---------------------------------------------------------------------------

_pipe_lock = None


def _get_pipe_lock():
    """Module-level asyncio lock to prevent concurrent pipe() calls."""
    global _pipe_lock
    if _pipe_lock is None:
        _pipe_lock = asyncio.Lock()
    return _pipe_lock


class Pipe:
    """
    Open WebUI Pipe that routes messages through OpenClaw Gateway via WebSocket.

    Valves (configured in OWUI admin panel):
    - GATEWAY_URL: OpenClaw Gateway address (default "localhost:18789")
    - GATEWAY_TOKEN: Gateway API token (required)
    - DEVICE_IDENTITY: (optional) persisted device identity JSON
    - AGENT_ID: target agent (default "main")
    - ENABLE_FILE_SERVER: start media file server (default True)
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
            description="(Advanced) Persisted device identity JSON from first run"
        )
        AGENT_ID: str = Field(
            default="main",
            description="Target agent identifier"
        )
        ENABLE_FILE_SERVER: bool = Field(
            default=True,
            description="Start a minimal HTTP server for media files"
        )
        FILE_SERVER_BASE_URL: str = Field(
            default="http://your-owui-host:18791",
            description="Public URL for the file server (for MEDIA: resolution)"
        )

    def __init__(self):
        self.valves = self.Valves()
        # Cache tool call arguments between start/result events
        self._active_tool_args = {}

    async def pipe(self, body, __event_emitter__,
                   __user__=None, __metadata__=None, __request__=None,
                   __task__=None, __task_body__=None):
        """Main pipe entry point — called by Open WebUI for each user message.

        This is an async generator: each ``yield`` emits a chunk that OWUI
        streams to the frontend in real time.

        Accepts optional __task__ and __task_body__ for OWUI background
        task detection (title, tags, emoji, follow-up generation).
        """
        if self.valves.ENABLE_FILE_SERVER:
            _start_file_server()

        # --- Short-circuit OWUI background task requests (P15) ---
        # OWUI auto-generates titles, tags, emoji, follow-ups, and autocomplete
        # as background pipe calls. These must NOT reach the OpenClaw session
        # (they pollute the conversation and compete with the user's real message).
        if __task__ and __task__ in (
            "title_generation",
            "tags_generation",
            "follow_up_generation",
            "emoji_generation",
            "autocomplete_generation",
            "query_generation",
        ):
            pipe_log(f"Skipping OWUI background task: {__task__}")
            return  # Yield nothing — OWUI handles task results server-side

        # --- Extract user message ---
        messages = body.get("messages", [])
        text = messages[-1]["content"] if messages else ""
        if not text:
            yield "No message"
            return

        pipe_log(f"Messages: {len(messages)}, last role: "
                 f"{messages[-1]['role'] if messages else 'NONE'}")

        # --- Prevent concurrent calls (queued messages = broken session) ---
        lock = _get_pipe_lock()
        if lock.locked():
            yield "Please wait for the previous message to finish..."
            return

        # --- Parse gateway connection parameters ---
        parts = self.valves.GATEWAY_URL.rsplit(":", 1)
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 18789
        token = self.valves.GATEWAY_TOKEN
        if not token:
            yield "No GATEWAY_TOKEN configured"
            return

        # Acquire lock after all early-exit checks
        await lock.acquire()

        # --- Device identity (Ed25519) ---
        ident = None
        if self.valves.DEVICE_IDENTITY:
            ident = _parse_device_identity(self.valves.DEVICE_IDENTITY)
            if ident:
                pipe_log("Using persisted device identity")
            else:
                pipe_log("Failed to parse DEVICE_IDENTITY, generating new one")

        if not ident:
            ident = _generate_device_identity()
            print(
                f"DEVICE_IDENTITY={json.dumps(ident, separators=(',',':'))}",
                flush=True
            )
            pipe_log("New device identity generated — copy it from logs into "
                     "the DEVICE_IDENTITY valve to persist across restarts")

        # --- Connect via WebSocket ---
        try:
            pipe_log(f"Connecting to ws://{host}:{port}")
            ws = await websockets.connect(f"ws://{host}:{port}",
                                          ping_interval=None)

            # Handshake: receive challenge
            challenge = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if challenge.get("event") != "connect.challenge":
                raise GatewayError("Bad handshake — expected connect.challenge")

            c = challenge["payload"]
            signed = _sign_challenge(ident, c["nonce"], c["ts"],
                                     token_str=token)

            # Send connect request
            await ws.send(json.dumps(dict(
                type="req", id="1", method="connect", params=dict(
                    minProtocol=4, maxProtocol=4,
                    client=dict(id="test", version="1",
                                platform="linux", mode="cli"),
                    role="operator",
                    scopes=["operator.read", "operator.write"],
                    auth=dict(token=token),
                    device=signed,
                    locale="en-US",
                    userAgent="owui-pipe/1.0",
                    caps=["agent-events", "tool-events"]
                )
            )))

            resp = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if not resp.get("ok"):
                raise GatewayError(
                    str(resp.get("error", {}).get("message", "connect failed"))
                )
            pipe_log("Connected to Gateway")

            # --- Derive stable session key ---
            # Tie the OpenClaw session to the OWUI chat_id + user_id so that
            # continuing the same OWUI conversation reuses the same session.
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
                pipe_log("WARNING: no chat_id in metadata, generated random:",
                         chat_id)

            session_key = (
                f"agent:{self.valves.AGENT_ID}:openwebui-{user_id}-{chat_id}"
            )
            pipe_log(f"Session key: {session_key}")

            # --- Send the user message ---
            idempotency_key = f"msg-{chat_id}-{time.time()}"
            await ws.send(json.dumps(dict(
                type="req", id="2", method="chat.send", params=dict(
                    sessionKey=session_key,
                    message=text,
                    idempotencyKey=idempotency_key
                )
            )))
            pipe_log("Message sent, waiting for response...")

            # --- Consume events ---
            done = False
            event_count = 0
            text_yielded = False
            our_run_id = None

            while not done and event_count < 500:
                try:
                    msg = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=60)
                    )
                    event_count += 1

                    # Capture runId from chat.send response
                    if msg.get("type") == "res":
                        if msg.get("id") == "2" and not our_run_id:
                            our_run_id = (
                                msg.get("payload", {})
                                   .get("runId")
                            )
                            pipe_log(f"Captured runId: {our_run_id}")
                        continue

                    # Only process agent/chat events
                    if msg.get("type") != "event" or \
                       msg.get("event") not in ("agent", "chat"):
                        continue

                    payload = msg.get("payload", {})
                    stream = payload.get("stream")
                    data = payload.get("data", {})
                    name = data.get("name", "")
                    phase = data.get("phase", "")

                    # --- P16: Filter events that don't belong to our session/run ---
                    # Skip events from other sessions (broadcast to all operator connections)
                    evt_session = payload.get("sessionKey", "")
                    if evt_session and evt_session != session_key:
                        pipe_log(f"  filtered event from other session: "
                                 f"{evt_session[:60]}...")
                        continue

                    # Skip events from other runs within our session
                    evt_run_id = payload.get("runId", "")
                    if evt_run_id and our_run_id and evt_run_id != our_run_id:
                        pipe_log(f"  filtered event from other run: {evt_run_id[:20]}...")
                        continue

                    # Skip heartbeat events
                    if payload.get("isHeartbeat"):
                        pipe_log("  filtered heartbeat event")
                        continue

                    pipe_log(f"Event #{event_count}: stream={stream} "
                             f"phase={phase} name={name}")

                    # --- Assistant text stream ---
                    if stream == "assistant":
                        delta = data.get("delta") or data.get("text") or ""
                        if delta:
                            text_yielded = True
                            # Strip inbound metadata blocks that OpenClaw injects
                            if delta.startswith("Sender (untrusted metadata)"):
                                pipe_log("  filtered metadata block")
                                continue
                            # Convert MEDIA: directives to base64 images
                            if "MEDIA:" in delta:
                                resolved, handled = _resolve_media(
                                    delta,
                                    base_url=self.valves.FILE_SERVER_BASE_URL
                                )
                                if handled:
                                    pipe_log("  resolved MEDIA: directive")
                                    yield resolved
                                    continue
                            yield delta

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

                        elif phase == "result":
                            result = data.get("result", {})
                            result_str = json.dumps(result) if not isinstance(
                                result, str) else result
                            tool_call_id = data.get("toolCallId", "")
                            stored_args = self._active_tool_args.pop(
                                tool_call_id, None
                            )
                            args_str = stored_args if stored_args else json.dumps(
                                data.get("args", {})
                            )
                            pipe_log(f"  Tool result: {name} "
                                     f"({len(result_str)} chars)")

                            # Build OWUI <details type="tool_calls"> element
                            yield (
                                '\n<details type="tool_calls" done="true" '
                                f'id="{html.escape(tool_call_id)}" '
                                f'name="{html.escape(name)}" '
                                f'arguments="{html.escape(args_str[:3000])}" '
                                f'result="{html.escape(result_str[:8000])}" '
                                f'meta="{html.escape(str(data.get("meta",""))[:500])}" '
                                'files="[]" embeds="[]">'
                                f'\n<summary>{html.escape(name)}</summary>'
                                '\n</details>\n'
                            )

                            if __event_emitter__:
                                await __event_emitter__(
                                    {"type": "status", "data": {
                                        "description": f"✅ {name} done",
                                        "done": True
                                    }}
                                )

                    # --- Item events (progress) ---
                    if stream == "item":
                        pipe_log(f"  Item: kind={data.get('kind','')} "
                                 f"status={data.get('status','')} "
                                 f"title={str(data.get('title',''))[:50]}")

                    # --- Lifecycle events ---
                    if stream == "lifecycle":
                        pipe_log(f"  Lifecycle: phase={phase}")
                        if phase == "end":
                            done = True
                        elif phase == "error":
                            error_text = str(data.get("error", "unknown"))
                            yield f"\n\n**Error:** {error_text}"
                            done = True

                except asyncio.TimeoutError:
                    pipe_log("TIMEOUT — no events from Gateway for 60s")
                    break

            await ws.close()
            pipe_log(f"DONE — {event_count} events processed, "
                     f"text yielded: {text_yielded}")

            if not text_yielded:
                yield "(no response)"

        except Exception as e:
            pipe_log("EXCEPTION:", str(e))
            import traceback
            pipe_log(traceback.format_exc()[:500])
            yield f"**Connection error:** {e}"

        finally:
            # Release the concurrent call lock
            if lock.locked():
                lock.release()

