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
  - Claude · Opus 4.8: patches the session model to OPUS_MODEL
  - Claude · Sonnet 5: patches the session model to SONNET_MODEL
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
MEDIA_BASE_URL = "https://localhost:18791"
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
    before = text[:idx]
    after_prefix = text[idx + len(prefix):].strip()
    fname = after_prefix.split()[0] if after_prefix else ""
    if not fname:
        return text, False
    # Prefer HTTPS URL over base64 data URI (base64 breaks OWUI streaming parser).
    url = f"{base_url.rstrip('/')}/{fname}"
    rest = after_prefix[len(fname):].strip()
    result = before + f"![{fname}]({url})"
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

    # _upload_owui_file uses blocking urllib — must run off the event loop.
    # This call goes to OWUI's own API (often 127.0.0.1:8080, i.e. OWUI
    # calling itself) from *inside* the async handler for the very request
    # that's driving this pipe run. Calling it directly would block the
    # single asyncio event loop thread, and OWUI can't service its own
    # incoming HTTP request while its own loop is blocked waiting on it —
    # a guaranteed self-deadlock that only resolves via timeout. Running it
    # in a thread lets the event loop keep serving requests concurrently.
    file_obj = await asyncio.to_thread(_upload_owui_file, fpath, base_url, token)
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


async def _emit_status(__event_emitter__, description, *, done=False):
    """Send an OWUI status event when the current pipe call supports events."""
    if not __event_emitter__:
        return
    await __event_emitter__(
        {"type": "status", "data": {"description": description, "done": done}}
    )


async def _emit_message_snapshot(__event_emitter__, content):
    """Persist the in-flight assistant message content for OWUI reloads.

    Pipe yielded content is still the final source of truth, but OWUI only saves
    that content when the pipe completes. A short-name `replace` event updates
    the message content in the DB while the run is still active, so navigating
    away and back can rehydrate the partial response.
    """
    if not __event_emitter__ or not content:
        return
    await __event_emitter__(
        {"type": "replace", "data": {"content": content}}
    )


def _is_user_input_prompt(text: str) -> bool:
    """Return True for OpenClaw/Codex blocking user-input prompts."""
    normalized = (text or "").lstrip()
    return (
        normalized.startswith("Codex needs input:")
        or normalized.startswith("OpenClaw needs input:")
    )


def _modal_payload_from_user_input_prompt(prompt_text: str) -> dict:
    """Build an OWUI modal payload from OpenClaw's "needs input:" prompt text."""
    lines = [line.strip() for line in (prompt_text or "").splitlines()]
    lines = [line for line in lines if line]
    if lines and lines[0].endswith("needs input:"):
        lines = lines[1:]

    title = "OpenClaw needs input"
    if lines and len(lines[0]) <= 80 and not re.match(r"^\d+\.", lines[0]):
        title = lines[0]
        lines = lines[1:]

    message = "\n".join(lines).strip() or "Please answer so the run can continue."
    is_secret = any(
        marker in (prompt_text or "").lower()
        for marker in ("secret", "password", "may show your reply")
    )
    data = {
        "title": title,
        "message": message,
        "placeholder": "Reply with a number or your answer",
    }
    if is_secret:
        data["type"] = "password"
    return {"type": "input", "data": data}


def _normalize_event_call_response(response) -> str:
    """Extract text from common OWUI __event_call__ return shapes.
    Returns empty string for errors, None, or unrecognized shapes.
    """
    if response is None:
        return ""
    if isinstance(response, str):
        return response.strip()
    if isinstance(response, dict):
        # Error responses
        if "error" in response:
            return ""
        for key in ("value", "text", "content", "message", "response"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # Confirm type with options
        value = response.get("option")
        if isinstance(value, str) and value.strip():
            return value.strip()
        if response.get("confirmed") is True:
            return "yes"
        if response.get("confirmed") is False:
            return "no"
    return ""


async def _ask_user_input_modal(
    __event_call__,
    prompt_text: str,
    *,
    timeout_s: float = 60,
) -> str | None:
    """Ask the user through OWUI's modal input API when available."""
    if not __event_call__ or not _is_user_input_prompt(prompt_text):
        return None
    response = await asyncio.wait_for(
        __event_call__(_modal_payload_from_user_input_prompt(prompt_text)),
        timeout=timeout_s,
    )
    answer = _normalize_event_call_response(response)
    return answer or None


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


def _preview_recovery_text(preview: dict, session_key: str, user_text: str) -> str | None:
    """Return assistant text saved after this user message, if preview has it."""
    previews = preview.get("previews")
    if not isinstance(previews, list):
        return None

    entry = None
    for candidate in previews:
        if isinstance(candidate, dict) and candidate.get("key") == session_key:
            entry = candidate
            break
    if not entry:
        return None

    items = entry.get("items")
    if not isinstance(items, list):
        return None

    normalized_user_text = (user_text or "").strip()
    if not normalized_user_text:
        return None

    last_matching_user_index = None
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if item.get("role") != "user":
            continue
        if str(item.get("text", "")).strip() == normalized_user_text:
            last_matching_user_index = idx

    if last_matching_user_index is None:
        return None

    for item in items[last_matching_user_index + 1:]:
        if not isinstance(item, dict):
            continue
        if item.get("role") != "assistant":
            continue
        text = str(item.get("text", "")).strip()
        if text:
            return text
    return None


def _owui_session_key(agent_id: str, user_id: str, chat_id: str) -> str:
    return f"agent:{agent_id}:openwebui-{user_id}-{chat_id}"


def _owui_chat_send_params(
    session_key: str,
    message: str,
    idempotency_key: str,
    owui_chat_id: str | None,
    owui_user_id: str | None,
) -> dict:
    params = dict(
        sessionKey=session_key,
        message=message,
        idempotencyKey=idempotency_key,
    )
    if owui_chat_id:
        metadata = {
            "chat_id": owui_chat_id,
            "source": "openwebui",
        }
        if owui_user_id and owui_user_id != "unknown":
            metadata["user_id"] = owui_user_id
        params["systemProvenanceReceipt"] = "\n".join([
            "Conversation info (untrusted metadata):",
            "```json",
            json.dumps(metadata, ensure_ascii=False, indent=2),
            "```",
        ])
    return params


def _resolved_model_key(patch_resp: dict) -> str | None:
    resolved = patch_resp.get("resolved", {})
    provider = resolved.get("modelProvider")
    model = resolved.get("model")
    if provider and model:
        return f"{provider}/{model}"
    return None


def _model_patch_matches(model_override: str | None, patch_resp: dict) -> bool:
    if model_override is None:
        return True
    return _resolved_model_key(patch_resp) == model_override


def _coerce_text(value) -> str:
    """Extract text from common stream payload shapes."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("delta", "text", "content", "message"):
            text = _coerce_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        parts = [_coerce_text(item) for item in value]
        return "".join(part for part in parts if part)
    return ""


def _item_assistant_text(data: dict) -> str:
    """Return visible assistant text carried by item/preamble events."""
    kind = str(data.get("kind", "")).strip().lower()
    if kind not in ("assistant", "message", "output", "preamble"):
        return ""
    for key in ("delta", "text", "content", "message"):
        text = _coerce_text(data.get(key)).strip()
        if text:
            return text
    return ""


def _item_delta_text(item_text: str, last_item_text: str, assistant_stream_text: str) -> str:
    """Compute the new text (if any) an `item` event adds beyond what's
    already been shown, either by a prior `item` event or by the regular
    `assistant` delta stream.

    Some item events of kind "message"/"output" echo the *complete* final
    assistant text (not just a genuine tool-preamble) — without this check
    that full text gets yielded a second time on top of what the assistant
    stream already delivered, duplicating the whole final message.
    """
    baseline = last_item_text or assistant_stream_text
    if not baseline:
        return item_text
    if item_text == baseline:
        return ""
    if item_text.startswith(baseline):
        return item_text[len(baseline):]
    return item_text


# ---------------------------------------------------------------------------
# Persistent Gateway Connection (singleton)
# ---------------------------------------------------------------------------

@dataclass
class _Consumer:
    """An active run consumer — its ``asyncio.Queue`` receives events."""
    session_key: str
    run_id: str
    queues: list[asyncio.Queue] = field(default_factory=list)


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
        """Register an event consumer queue for a (session, run) pair.

        Returns a new queue attached to the consumer. Multiple queues per
        consumer are supported for broadcast (used by steering).
        """
        key = f"{session_key}:{run_id}"
        consumer = self._consumers.get(key)
        if consumer is None:
            consumer = _Consumer(session_key=session_key, run_id=run_id)
            self._consumers[key] = consumer
            pipe_log(f"  registered consumer: {key[:60]}...")
        q = asyncio.Queue(maxsize=500)
        consumer.queues.append(q)
        return q

    def unregister_consumer(self, session_key: str, run_id: str, queue: asyncio.Queue | None = None):
        """Remove a consumer queue.

        If queue is given, only that queue is removed (broadcast mode).
        If queue is None, the entire consumer (all queues) is removed.
        """
        key = f"{session_key}:{run_id}"
        consumer = self._consumers.get(key)
        if consumer is None:
            return
        if queue:
            try:
                consumer.queues.remove(queue)
            except ValueError:
                pass
        if not consumer.queues or queue is None:
            del self._consumers[key]
        pipe_log(f"  unregistered consumer: {key[:60]}...")

    def active_run_id_for_session(self, session_key: str) -> str | None:
        """Return the sole active run id for a session, if one is registered."""
        matches = [
            consumer.run_id for consumer in self._consumers.values()
            if consumer.session_key == session_key
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def consumers_for_event(self, payload: dict) -> list[_Consumer]:
        """Return consumers that should receive a Gateway event payload."""
        evt_session = payload.get("sessionKey", "")
        evt_run_id = payload.get("runId", "")
        if evt_session and evt_run_id:
            consumer = self._consumers.get(f"{evt_session}:{evt_run_id}")
            return [consumer] if consumer else []
        if evt_session and not evt_run_id:
            matches = [
                consumer for consumer in self._consumers.values()
                if consumer.session_key == evt_session
            ]
            return matches if len(matches) == 1 else []
        return []

    async def session_preview(self, session_key: str, limit: int = 8,
                              max_chars: int = 4000) -> dict:
        """Fetch a bounded transcript preview for timeout recovery."""
        return await self.send_request(
            "sessions.preview",
            dict(keys=[session_key], limit=limit, maxChars=max_chars),
            timeout=10
        )

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

    async def send_stop(self, session_key: str):
        """Send the stronger /stop command for runtimes where chat.abort is incomplete."""
        try:
            resp = await self.send_request(
                "chat.send",
                dict(
                    sessionKey=session_key,
                    message="/stop",
                    deliver=False,
                    idempotencyKey=f"stop-{uuid.uuid4()}",
                ),
                timeout=10,
            )
            pipe_log(f"  sent /stop fallback: {resp}")
        except asyncio.TimeoutError:
            pipe_log("  /stop fallback timed out")
        except Exception as e:
            pipe_log(f"  /stop fallback failed: {e}")

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
                consumers = self.consumers_for_event(payload)
                if consumers:
                    self._event_count += 1
                    if not payload.get("runId"):
                        pipe_log("  dispatched session-only event to sole consumer")
                    for q in consumers[0].queues:
                        await q.put(msg)
                    continue
                if payload.get("sessionKey") and not payload.get("runId"):
                    pipe_log("  dropped ambiguous or unmatched session-only event")

                # If nothing matches at all, drop it.
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
    - SEND_STOP_ON_CANCEL: send /stop after chat.abort when OWUI cancels
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
        SEND_STOP_ON_CANCEL: bool = Field(
            default=True,
            description="After OWUI cancels a stream, send /stop because chat.abort may not stop active tool subprocesses"
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
            default="https://localhost:18791",
            description="Public URL for the file server (for MEDIA: resolution)"
        )
        CHATGPT_MODEL: str = Field(
            default="openai/gpt-5.5",
            description="OpenClaw model override used by the ChatGPT manifold model"
        )
        OPUS_MODEL: str = Field(
            default="anthropic/claude-opus-4-8",
            description="OpenClaw model override used by the Opus 4.8 manifold model"
        )
        SONNET_MODEL: str = Field(
            default="anthropic/claude-sonnet-5",
            description="OpenClaw model override used by the Sonnet 5 manifold model"
        )
        GLM_MODEL: str = Field(
            default="openrouter/z-ai/glm-5.2",
            description="OpenClaw model override used by the GLM 5.2 manifold model"
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
            {"id": "opus", "name": "Claude · Opus 4.8"},
            {"id": "sonnet", "name": "Claude · Sonnet 5"},
            {"id": "glm", "name": "GLM 5.2 · OpenRouter"},
        ]

    def _selected_preset(self, body):
        model = str(body.get("model", ""))
        suffix = model.rsplit(".", 1)[-1].rsplit("/", 1)[-1]
        if suffix == "chatgpt":
            return "chatgpt"
        if suffix == "opus":
            return "opus"
        if suffix == "sonnet":
            return "sonnet"
        if suffix == "glm":
            return "glm"
        return "default"

    def _model_override_for_preset(self, preset):
        if preset == "chatgpt":
            return self.valves.CHATGPT_MODEL.strip() or None
        if preset == "opus":
            return self.valves.OPUS_MODEL.strip() or None
        if preset == "sonnet":
            return self.valves.SONNET_MODEL.strip() or None
        if preset == "glm":
            return self.valves.GLM_MODEL.strip() or None
        return None

    async def pipe(self, body, __event_emitter__, __event_call__=None,
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

        await _emit_status(__event_emitter__, "Thinking...", done=False)
        pipe_log(f"Messages: {len(messages)}, last role: "
                 f"{messages[-1]['role'] if messages else 'NONE'}")

        # --- Get the persistent connection ---
        try:
            conn = await _get_gateway_connection(lambda: self.valves)
        except GatewayError as e:
            await _emit_status(__event_emitter__, "", done=True)
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

        owui_origin_chat_id = chat_id
        owui_origin_user_id = user_id

        if not chat_id:
            chat_id = f"owui-{uuid.uuid4().hex[:12]}"
            pipe_log("WARNING: no chat_id in metadata, generated random:", chat_id)

        session_key = _owui_session_key(self.valves.AGENT_ID, user_id, chat_id)
        pipe_log(f"Session key: {session_key}")
        self._current_session_key = session_key

        try:
            patch_resp = await conn.send_request(
                "sessions.patch",
                dict(key=session_key, model=model_override),
                timeout=10
            )
            if not _model_patch_matches(model_override, patch_resp):
                resolved_model = _resolved_model_key(patch_resp)
                raise GatewayError(
                    "model override did not apply "
                    f"(wanted {model_override or 'agent default'}, "
                    f"got {resolved_model or 'agent default'})"
                )
            if model_override:
                pipe_log(f"Applied model override: {model_override}")
            else:
                pipe_log("Cleared model override; using agent default")
        except Exception as e:
            await _emit_status(__event_emitter__, "", done=True)
            yield f"**Model selection error:** could not apply `{model_override or 'agent default'}`: {e}"
            return

        # --- Actual steering: inject message into active run, don't wait ---
        active_run_id = conn.active_run_id_for_session(session_key)
        if active_run_id:
            pipe_log(f"Active run exists ({active_run_id[:20]}...); steering message into it")
            await _emit_status(
                __event_emitter__,
                "Steering into current response...",
                done=False,
            )

            # Send the steering message immediately — the gateway will inject it
            # at the next model boundary (steer mode is the gateway's default).
            try:
                idempotency_key = f"msg-{chat_id}-{time.time()}"
                send_resp = await conn.send_request(
                    "chat.send",
                    _owui_chat_send_params(
                        session_key=session_key,
                        message=text,
                        idempotency_key=idempotency_key,
                        owui_chat_id=owui_origin_chat_id,
                        owui_user_id=owui_origin_user_id,
                    ),
                    timeout=30
                )
            except asyncio.TimeoutError:
                await _emit_status(__event_emitter__, "", done=True)
                yield "**Timeout:** Gateway did not respond to chat.send"
                return
            except Exception as e:
                await _emit_status(__event_emitter__, "", done=True)
                yield f"**Error sending message:** {e}"
                return

            steer_run_id = send_resp.get("runId", "unknown")
            self._current_run_id = steer_run_id
            pipe_log(f"Steer response runId: {steer_run_id[:20] if steer_run_id != 'unknown' else 'unknown'}")

            # Register a broadcast consumer on the SAME run so this bubble
            # gets a copy of all subsequent events (the original bubble's
            # consumer keeps running with its own queue).
            if steer_run_id != "unknown":
                queue = conn.register_consumer(session_key, steer_run_id)
            else:
                await _emit_status(__event_emitter__, "", done=True)
                yield f"**Steer accepted but no runId returned.**"
                return

            # Emit a steering acknowledgment compactly
            yield "⤷ _Steered into the ongoing response_\n\n"

            # Enter the event consumption loop with this bubble's queue
            our_run_id = steer_run_id
        else:
            # --- Normal path: no active run, send fresh ---
            idempotency_key = f"msg-{chat_id}-{time.time()}"
            try:
                send_resp = await conn.send_request(
                    "chat.send",
                    _owui_chat_send_params(
                        session_key=session_key,
                        message=text,
                        idempotency_key=idempotency_key,
                        owui_chat_id=owui_origin_chat_id,
                        owui_user_id=owui_origin_user_id,
                    ),
                    timeout=30
                )
            except asyncio.TimeoutError:
                await _emit_status(__event_emitter__, "", done=True)
                yield "**Timeout:** Gateway did not respond to chat.send"
                return
            except Exception as e:
                await _emit_status(__event_emitter__, "", done=True)
                yield f"**Error sending message:** {e}"
                return
            our_run_id = send_resp.get("runId", "unknown")
            self._current_run_id = our_run_id
            pipe_log(f"Captured runId: {our_run_id}")
            queue = conn.register_consumer(session_key, our_run_id)

        # --- Consume events ---
        done = False
        event_count = 0
        text_yielded = False
        aborted = False
        first_event_arrived = False
        last_item_text = ""
        assistant_stream_text = ""
        visible_message_text = ""
        last_snapshot_text = ""
        last_snapshot_time = 0.0
        snapshot_interval_s = 1.0
        snapshot_min_delta_chars = 250
        wait_started_time = time.time()
        last_activity_time = time.time()
        idle_probe_s = 30
        no_text_deadman_s = 180
        last_describe_check = 0.0
        describe_check_interval = 45
        max_events_without_text = 5000

        async def recover_from_preview() -> str | None:
            try:
                preview = await conn.session_preview(session_key)
                return _preview_recovery_text(preview, session_key, text)
            except Exception as ex:
                pipe_log(f"  preview recovery failed: {ex}")
                return None

        def record_visible_chunk(chunk: str):
            nonlocal visible_message_text
            if chunk:
                visible_message_text += chunk

        async def maybe_emit_snapshot(*, force: bool = False):
            nonlocal last_snapshot_text, last_snapshot_time
            if not visible_message_text or visible_message_text == last_snapshot_text:
                return
            now = time.time()
            if (
                not force
                and last_snapshot_text
                and now - last_snapshot_time < snapshot_interval_s
                and len(visible_message_text) - len(last_snapshot_text) < snapshot_min_delta_chars
            ):
                return
            await _emit_message_snapshot(__event_emitter__, visible_message_text)
            last_snapshot_text = visible_message_text
            last_snapshot_time = now

        async def maybe_answer_user_input(prompt_text: str) -> bool:
            if not _is_user_input_prompt(prompt_text):
                return False
            try:
                answer = await _ask_user_input_modal(__event_call__, prompt_text)
            except Exception as ex:
                pipe_log(f"  user input modal failed; falling back to chat prompt: {ex}")
                return False
            if not answer:
                pipe_log("  user input modal returned empty answer or error")
                return False
            pipe_log("  user input answered via OWUI modal")
            await _emit_status(__event_emitter__, "Sending answer...", done=False)
            idempotency_key = f"user-input-{chat_id}-{time.time()}"
            try:
                await conn.send_request(
                    "chat.send",
                    _owui_chat_send_params(
                        session_key=session_key,
                        message=answer,
                        idempotency_key=idempotency_key,
                        owui_chat_id=owui_origin_chat_id,
                        owui_user_id=owui_origin_user_id,
                    ),
                    timeout=30,
                )
            except Exception as ex:
                pipe_log(f"  sending answer back failed: {ex}")
            await _emit_status(__event_emitter__, "Answer sent; continuing...", done=False)
            return True

        try:
            while not done:
                try:
                    recv_timeout = idle_probe_s if first_event_arrived else 60
                    msg = await asyncio.wait_for(queue.get(), timeout=recv_timeout)
                except asyncio.TimeoutError:
                    timeout_desc = (
                        f"{idle_probe_s}s"
                        if first_event_arrived
                        else "60s"
                    )
                    pipe_log(f"TIMEOUT — no events on queue for {timeout_desc}")
                    if not first_event_arrived:
                        yield "**Timeout:** Gateway accepted the message but emitted no run events."
                        text_yielded = True
                        break

                    recovered = await recover_from_preview()
                    if recovered and not text_yielded:
                        pipe_log("  recovered assistant text from sessions.preview")
                        text_yielded = True
                        record_visible_chunk(recovered)
                        yield recovered
                        await maybe_emit_snapshot(force=True)
                        done = True
                        break

                    # A quiet queue for idle_probe_s does NOT mean the run is
                    # done — a long tool call or a stretch of agent reasoning
                    # with no intermediate events looks identical from here.
                    # Only `sessions.describe` (the Gateway's own run status)
                    # is authoritative; never close on silence alone, whether
                    # or not text has already streamed.
                    idle_elapsed = time.time() - last_activity_time
                    if not text_yielded:
                        elapsed = time.time() - wait_started_time
                        if elapsed < no_text_deadman_s:
                            pipe_log(
                                "  idle but no assistant text yet; "
                                "continuing to wait for terminal event"
                            )
                            await _emit_status(
                                __event_emitter__,
                                "Waiting for final answer...",
                                done=False,
                            )
                    else:
                        pipe_log(
                            f"  idle for {idle_elapsed:.0f}s after streaming text; "
                            "verifying the run is actually done before closing"
                        )
                        await _emit_status(
                            __event_emitter__,
                            "Still working...",
                            done=False,
                        )

                    # Periodically check if the Gateway still has an active run
                    if time.time() - last_describe_check >= describe_check_interval:
                        last_describe_check = time.time()
                        try:
                            desc = await conn.send_request(
                                "sessions.describe",
                                dict(key=session_key),
                                timeout=8
                            )
                            session_row = desc.get("session")
                            if session_row is None:
                                pipe_log("  sessions.describe: session not found")
                                if text_yielded:
                                    done = True
                                    break
                            elif session_row.get("status") in ("done", "failed", "cancelled"):
                                pipe_log("  sessions.describe: session is done/failed/cancelled, checking preview")
                                recovered2 = await recover_from_preview()
                                if recovered2 and not text_yielded:
                                    pipe_log("  recovered assistant text after describe probe")
                                    text_yielded = True
                                    record_visible_chunk(recovered2)
                                    yield recovered2
                                    await maybe_emit_snapshot(force=True)
                                done = True
                                break
                            else:
                                pipe_log(
                                    f"  sessions.describe: status="
                                    f"{session_row.get('status','unknown')} — "
                                    "still active, keep waiting"
                                )
                        except Exception as ex:
                            pipe_log(f"  sessions.describe probe failed: {ex}")
                            # Belt-and-suspenders: if we can't even confirm
                            # status and have been idle far longer than the
                            # normal deadman budget, give up rather than hang
                            # forever (should be rare — WS reconnect handles
                            # true connection loss separately).
                            if text_yielded and idle_elapsed > no_text_deadman_s:
                                pipe_log("  describe probe unreachable and idle too long; closing")
                                done = True
                                break

                    continue

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
                    raw_delta = data.get("delta")
                    if raw_delta:
                        delta = raw_delta
                    else:
                        # Some providers (observed with claude-cli-backed
                        # Opus/Sonnet overrides) send a final catch-all event
                        # with no `delta` but a `text` field holding the full
                        # cumulative reply rather than a fresh chunk. Treating
                        # it as always-new re-sent the whole message a second
                        # time. Diff it against what's already been streamed,
                        # same as the item-event dedup below.
                        raw_text = data.get("text") or ""
                        delta = (
                            _item_delta_text(raw_text, "", assistant_stream_text)
                            if raw_text
                            else ""
                        )
                    if delta:
                        # Filter Sender metadata
                        if (
                            "Sender (untrusted metadata)" in delta
                            or "UnTrustedMetadata" in delta
                        ):
                            pipe_log("  filtered metadata block")
                            continue
                        if await maybe_answer_user_input(delta):
                            last_activity_time = time.time()
                            continue
                        text_yielded = True
                        # No snapshot here, regardless of whether tool blocks
                        # exist earlier in the message. A `replace` event sets
                        # the full message content; if it lands on (or near)
                        # the complete final text right before the generator
                        # naturally finishes, OWUI double-saves — the
                        # `replace` writes it once, then the generator's own
                        # accumulated streamed yields write the same content
                        # again on top. This isn't only a risk for the literal
                        # last chunk: since these snapshots are throttled by
                        # time/char thresholds rather than tied to "a tool
                        # block was just added", they keep firing throughout
                        # any plain-text tail after a tool call and will
                        # eventually land close enough to the end to trigger
                        # the same duplicate. The one snapshot that's actually
                        # safe is the forced one taken right when a tool
                        # block itself is yielded (still partial content by
                        # definition, since the block was just added) — see
                        # the `stream == "tool"` / phase == "result" handler.
                        # See P17 / P19 / P23 / P24 / ELI-9 for the history of
                        # this recurring bug.
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
                                record_visible_chunk(resolved)
                                yield resolved
                                await maybe_emit_snapshot()
                                last_activity_time = time.time()
                                continue
                        assistant_stream_text += delta
                        record_visible_chunk(delta)
                        yield delta
                        last_activity_time = time.time()

                # --- Assistant text carried by item/preamble events ---
                if stream == "item":
                    item_text = _item_assistant_text(data)
                    if item_text:
                        if await maybe_answer_user_input(item_text):
                            last_item_text = item_text
                            last_activity_time = time.time()
                            continue
                        item_delta = _item_delta_text(
                            item_text, last_item_text, assistant_stream_text
                        )
                        last_item_text = item_text
                        if item_delta:
                            text_yielded = True
                            pipe_log("  yielded text from item event")
                            record_visible_chunk(item_delta)
                            yield item_delta
                            last_activity_time = time.time()

                # --- Tool call events ---
                if stream == "tool":
                    if phase == "start":
                        tool_call_id = data.get("toolCallId", "")
                        args = json.dumps(data.get("args", {}))
                        if tool_call_id:
                            self._active_tool_args[tool_call_id] = args
                        pipe_log(f"  Tool start: {name}")
                        await _emit_status(
                            __event_emitter__,
                            f"Running {name}...",
                            done=False,
                        )
                        last_activity_time = time.time()

                    elif phase == "result":
                        result = data.get("result", {})
                        result_str = json.dumps(result) if not isinstance(result, str) else result
                        tool_call_id = data.get("toolCallId", "")
                        stored_args = self._active_tool_args.pop(tool_call_id, None)
                        args_str = stored_args or json.dumps(data.get("args", {}))
                        pipe_log(f"  Tool result: {name} ({len(result_str)} chars)")
                        tool_block = (
                            '\n<details type="tool_calls" done="true" '
                            f'id="{html.escape(tool_call_id)}" '
                            f'name="{html.escape(name)}" '
                            f'arguments="{html.escape(args_str[:3000])}" '
                            f'result="{html.escape(result_str[:8000])}" '
                            f'meta="{html.escape(str(data.get("meta",""))[:500])}" '
                            'files="[]" embeds="[]">'
                            f'\n<summary>{html.escape(name)}</summary>\n</details>\n'
                        )
                        record_visible_chunk(tool_block)
                        yield tool_block
                        await maybe_emit_snapshot(force=True)
                        await _emit_status(
                            __event_emitter__,
                            f"{name} done",
                            done=True,
                        )
                        last_activity_time = time.time()

                # --- Item events (progress) ---
                if stream == "item":
                    pipe_log(f"  Item: kind={data.get('kind','')} "
                             f"status={data.get('status','')} "
                             f"title={str(data.get('title',''))[:50]}")
                    last_activity_time = time.time()

                if not text_yielded and event_count >= max_events_without_text:
                    recovered = await recover_from_preview()
                    if recovered:
                        pipe_log("  recovered assistant text after event cap")
                        text_yielded = True
                        record_visible_chunk(recovered)
                        yield recovered
                        await maybe_emit_snapshot(force=True)
                    else:
                        timeout_text = (
                            "\n\n**Timeout:** The run emitted too many progress "
                            "events without assistant text. The pipe kept the "
                            "run from ending as `(no response)`, but the Gateway "
                            "did not provide visible output."
                        )
                        record_visible_chunk(timeout_text)
                        yield timeout_text
                        await maybe_emit_snapshot(force=True)
                        text_yielded = True
                    done = True

                if (
                    not done
                    and first_event_arrived
                    and text_yielded
                    and time.time() - last_activity_time > idle_probe_s
                ):
                    pipe_log(
                        "  idle probe: text already streamed and no activity for "
                        f"{idle_probe_s}s, self-closing"
                    )
                    done = True

        except asyncio.CancelledError:
            # OWUI stop button → abort the gateway run
            aborted = True
            pipe_log("Generator cancelled — sending chat.abort")
            await maybe_emit_snapshot(force=True)
            await _emit_status(__event_emitter__, "Stopped", done=True)
            await conn.abort(session_key, our_run_id)
            if self.valves.SEND_STOP_ON_CANCEL:
                pipe_log("Generator cancelled — sending /stop fallback")
                await conn.send_stop(session_key)
            raise  # Re-raise to signal proper cancellation

        finally:
            # No forced snapshot here: by the time this runs on a normal
            # completion, OWUI has already built the final message from the
            # streamed yields (the documented source of truth). A forced
            # `replace` snapshot at this exact moment double-writes the same
            # content, producing an exact duplicate with no separator. The
            # throttled snapshots inside the loop above cover reload-recovery
            # during an active run; the CancelledError branch above still
            # forces one for the abort case, which is legitimate since the
            # stream is cut short there.
            await _emit_status(__event_emitter__, "", done=True)
            conn.unregister_consumer(session_key, our_run_id, queue=queue)
            self._current_session_key = None
            self._current_run_id = None

        pipe_log(f"DONE — {event_count} events processed, "
                 f"text yielded: {text_yielded}")

        if not aborted and not text_yielded:
            recovered = await recover_from_preview()
            if recovered:
                pipe_log("  recovered assistant text at final fallback")
                record_visible_chunk(recovered)
                yield recovered
                await maybe_emit_snapshot(force=True)
            else:
                no_response_text = (
                    "\n\n**No visible response:** The run ended without assistant "
                    "text. Check OpenClaw logs for the missing final output event."
                )
                record_visible_chunk(no_response_text)
                yield no_response_text
                await maybe_emit_snapshot(force=True)
