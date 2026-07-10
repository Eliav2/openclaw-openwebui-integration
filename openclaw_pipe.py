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

_MEDIA_TRIGGER = "MEDIA:"
_MEDIA_FNAME_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def _looks_like_media_filename(fname: str) -> bool:
    """Return True only if fname plausibly names a real file (has an
    extension). Without this, ordinary prose that happens to use "MEDIA:"
    as a documentation term (e.g. "the MEDIA: fix is proven") gets its next
    word treated as a filename and mangled into a broken image link —
    confirmed live 2026-07-10 when explaining this very feature corrupted
    "the MEDIA: fix" into `![fix](.../fix)`.
    """
    return bool(_MEDIA_FNAME_RE.search(fname))


def _advance_media_buffer(pending: str, delta: str) -> tuple[str, str]:
    """Feed a new assistant-delta chunk into the MEDIA: buffering state.

    Returns (text_to_yield_now, new_pending), mirroring
    `_advance_input_prompt_buffer`. A real streaming provider can deliver
    "MEDIA:filename.png" split across multiple deltas at any point,
    including mid-filename or mid-prefix — resolving `_resolve_media`
    eagerly against a single truncated delta either drops the directive
    entirely (if "MEDIA:" itself is split, so neither chunk contains the
    full prefix) or resolves it against a truncated filename (confirmed
    live 2026-07-10: sending several images in one turn hit both failure
    modes in the same message). Hold back from the first "MEDIA:" (or a
    prefix of it that could still complete) until a whitespace char
    confirms the filename is done.
    """
    candidate = pending + delta
    idx = candidate.find(_MEDIA_TRIGGER)
    if idx == -1:
        for i in range(min(len(_MEDIA_TRIGGER) - 1, len(candidate)), 0, -1):
            if candidate.endswith(_MEDIA_TRIGGER[:i]):
                return candidate[:-i], candidate[-i:]
        return candidate, ""
    after = candidate[idx + len(_MEDIA_TRIGGER):]
    if not re.search(r"\s", after):
        return candidate[:idx], candidate[idx:]
    return candidate, ""


def _resolve_media(text, base_url=None):
    """Convert every MEDIA:filename directive in text to embedded media.

    A single reply can legitimately carry several images (confirmed live
    2026-07-10 sending a 4-screenshot bug repro) — this must loop over every
    occurrence, not just the first, or later directives in the same chunk
    silently pass through as literal "MEDIA:filename" text.
    """
    if base_url is None:
        base_url = MEDIA_BASE_URL
    prefix = "MEDIA:"
    if prefix not in text:
        return text, False
    handled = False
    out = ""
    remaining = text
    while prefix in remaining:
        idx = remaining.index(prefix)
        before = remaining[:idx]
        after_prefix = remaining[idx + len(prefix):].strip()
        fname = after_prefix.split()[0] if after_prefix else ""
        if not fname:
            break
        if not _looks_like_media_filename(fname):
            # Not a real directive — e.g. prose using "MEDIA:" as a term,
            # not a file reference. Leave this occurrence as literal text
            # and keep scanning past it for any genuine directive later on.
            out += before + prefix
            remaining = remaining[idx + len(prefix):]
            continue
        # Prefer HTTPS URL over base64 data URI (base64 breaks OWUI streaming parser).
        url = f"{base_url.rstrip('/')}/{fname}"
        rest = after_prefix[len(fname):].strip()
        out += before + f"![{fname}]({url})"
        handled = True
        if rest:
            out += "\n"
        remaining = rest
    out += remaining
    return (out, True) if handled else (text, False)


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
    """Upload every MEDIA: file in text to OWUI, in order, and attach each to
    the current message.

    A single reply can legitimately carry several images (confirmed live
    2026-07-10 sending a 4-screenshot bug repro) — this must loop over every
    occurrence, not just the first, or later directives in the same chunk
    silently pass through as literal "MEDIA:filename" text. If any single
    directive can't be resolved this way (file missing locally, bad name),
    bail out entirely with handled=False so the caller's fallback
    (`_resolve_media`) handles ALL directives uniformly via the external
    URL instead of leaving a mix of native-upload and unresolved directives
    in one message.
    """
    prefix = "MEDIA:"
    if prefix not in text:
        return text, False

    out = ""
    remaining = text
    uploaded_any = False
    while prefix in remaining:
        idx = remaining.index(prefix)
        before = remaining[:idx]
        after_prefix = remaining[idx + len(prefix):].strip()
        fname = after_prefix.split()[0] if after_prefix else ""
        if not fname or not _looks_like_media_filename(fname):
            # Not a real directive — e.g. prose using "MEDIA:" as a term,
            # not a file reference. Leave this occurrence as literal text
            # and keep scanning past it for any genuine directive later on.
            out += before + prefix
            remaining = remaining[idx + len(prefix):]
            continue
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
        mime = file_obj.get("meta", {}).get("content_type") or mimetypes.guess_type(fname)[0] or ""
        content_url = f"/api/v1/files/{file_id}/content" if file_id else ""
        if content_url and mime.startswith("image/"):
            replacement = f"![{fname}]({content_url})"
        elif content_url:
            replacement = f"[{fname}]({content_url})"
        else:
            replacement = f"`{fname}`"

        rest = after_prefix[len(fname):].strip()
        out += before + replacement
        uploaded_any = True
        if rest:
            out += "\n"
        remaining = rest

    out += remaining
    return (out, True) if uploaded_any else (text, False)


async def _resolve_media_text(text, *, valves, __request__=None, __event_emitter__=None):
    """Resolve a MEDIA: directive, preferring the native OWUI Files API upload
    and falling back to the external file-server URL. Shared by the main
    per-delta resolution site and the end-of-stream final-flush site so both
    stay in sync (see `_advance_media_buffer` for why callers must only pass
    in text whose MEDIA:filename is already known-complete).
    """
    handled = False
    resolved = text
    if valves.USE_OWUI_FILES:
        token = _extract_request_bearer(__request__) or valves.OWUI_API_KEY
        try:
            resolved, handled = await _resolve_media_via_owui(
                text,
                base_url=valves.OWUI_BASE_URL,
                token=token,
                __event_emitter__=__event_emitter__,
            )
        except Exception as ex:
            pipe_log(f"  OWUI file upload failed; falling back: {ex}")
    if not handled:
        resolved, handled = _resolve_media(text, base_url=valves.FILE_SERVER_BASE_URL)
    return resolved, handled


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


_USER_INPUT_TRIGGER_PREFIXES = ("Codex needs input:", "OpenClaw needs input:")


def _is_user_input_prompt(text: str) -> bool:
    """Return True for OpenClaw/Codex blocking user-input prompts."""
    normalized = (text or "").lstrip()
    return any(normalized.startswith(p) for p in _USER_INPUT_TRIGGER_PREFIXES)


def _could_be_user_input_prefix(normalized_text: str) -> bool:
    """Return True while `normalized_text` (already left-stripped) is still
    ambiguous: either a strict prefix of one of the trigger phrases (so more
    streamed text could still complete it), or already a full match.

    Used to decide whether to keep withholding assistant-delta text instead
    of yielding it immediately — real token-by-token streaming (e.g. Claude)
    delivers the trigger phrase a few characters at a time, so checking each
    raw delta in isolation (as `_is_user_input_prompt` does) never matches.
    Only once the buffered text diverges from every trigger prefix do we know
    for sure this message isn't a needs-input prompt.
    """
    if not normalized_text:
        return True
    return any(
        normalized_text.startswith(p) or p.startswith(normalized_text)
        for p in _USER_INPUT_TRIGGER_PREFIXES
    )


def _advance_input_prompt_buffer(pending: str, delta: str) -> tuple[str, str]:
    """Feed a new assistant-delta chunk into the needs-input buffering state.

    Returns `(text_to_yield_now, new_pending)`. `pending` is text already
    withheld because it might still become a needs-input trigger.

    A trigger is only meaningful as the start of a line, so once the
    combined text (`pending + delta`) diverges from every trigger prefix,
    only the text after the LAST newline is worth re-examining as a fresh
    candidate — a real streaming delta doesn't necessarily break exactly at
    a line boundary (e.g. a single chunk can contain the tail of one
    paragraph, the blank-line separator, *and* the start of the next one),
    so checking `delta.endswith("\\n")` at the yield site isn't enough on
    its own (P22 follow-up, 2026-07-08 — caught live: a reply that talked
    normally first and only asked its question in the next paragraph never
    got buffered, because the newline landed mid-delta, not at its edge).
    """
    candidate = pending + delta
    if _could_be_user_input_prefix(candidate.lstrip()):
        return "", candidate
    idx = candidate.rfind("\n")
    if idx == -1:
        return candidate, ""
    before, after = candidate[: idx + 1], candidate[idx + 1 :]
    if after and _could_be_user_input_prefix(after.lstrip()):
        return before, after
    return before + after, ""


def _modal_payload_from_user_input_prompt(prompt_text: str) -> tuple[dict, bool]:
    """Build an OWUI modal payload from OpenClaw's "needs input:" prompt text.

    Returns (payload_dict, is_confirmation) where is_confirmation is True
    if a yes/no confirmation modal was chosen instead of a free-text input.
    """
    lines = [line.strip() for line in (prompt_text or "").splitlines()]
    lines = [line for line in lines if line]
    if lines and lines[0].endswith("needs input:"):
        lines = lines[1:]

    title = "OpenClaw needs input"
    if lines and len(lines[0]) <= 80 and not re.match(r"^\d+\.", lines[0]):
        title = lines[0]
        lines = lines[1:]

    message = "\n".join(lines).strip() or "Please answer so the run can continue."

    text_lower = (prompt_text or "").lower()

    # Detect password / secret input. Kept specific on purpose: a bare "key"
    # substring matches innocent words ("monkey", "which key order?"), so we
    # only trigger on unambiguous secret markers.
    is_secret = any(
        marker in text_lower
        for marker in ("secret", "password", "may show your reply", "api key", "token")
    )

    # Detect confirmation (yes/no) questions — conservatively. Misclassifying a
    # free-text choice as a binary yes/no silently strips the real answer, so we
    # only pick confirmation when the prompt clearly reads as binary:
    #   * an explicit yes/no marker is present, OR
    #   * a strong confirm verb appears AND the prompt ends with "?".
    # An enumerated option list ("1. ... 2. ...") is a CHOICE, never yes/no.
    has_options = bool(re.search(r"(?m)^\s*\d+[.)]\s", prompt_text or ""))
    yn_markers = ("(y/n)", "[y/n]", "y/n?", "yes/no", "(yes/no)")
    confirm_verbs = (
        "confirm", "proceed", "overwrite", "are you sure", "do you want",
        "delete", "remove", "בטוח", "האם", "هل تريد",
    )
    has_yn_marker = any(m in text_lower for m in yn_markers)
    has_confirm_verb = any(w in text_lower for w in confirm_verbs)
    ends_question = message.rstrip().endswith("?") or title.rstrip().endswith("?")
    is_confirmation = (
        not is_secret
        and not has_options
        and (has_yn_marker or (has_confirm_verb and ends_question))
    )

    if is_confirmation:
        # Use yes/no confirmation dialog for quick binary choices
        data = {
            "title": title,
            "message": message,
        }
        return {"type": "confirmation", "data": data}, True

    # Default: free-text input
    data = {
        "title": title,
        "message": message,
        "placeholder": "Reply with a number or your answer",
    }
    if is_secret:
        data["type"] = "password"
    return {"type": "input", "data": data}, False


def _ask_user_detail_block(prompt_text: str, answer: str) -> str:
    """Render an answered ask-user prompt as a native OWUI tool-call block.

    OWUI's frontend only special-cases a handful of `<details type="...">`
    values with a nice icon + collapsible UI (confirmed by grepping the
    compiled frontend bundle): "tool_calls", "reasoning", "code_interpreter".
    There's no dedicated type for Q&A, so we reuse "tool_calls" (labelled as
    an "Ask User" call) to get the same familiar rendering Eliav already
    likes for real tool calls, inserted inline at the point the question
    was asked/answered — instead of showing nothing (previous behavior:
    the raw prompt text was fully suppressed once answered).
    """
    payload, _ = _modal_payload_from_user_input_prompt(prompt_text)
    data = payload.get("data", {})
    title = data.get("title", "Question")
    message = data.get("message", "")
    question_display = f"{title}\n{message}".strip() if message else title
    args_str = json.dumps({"question": question_display})
    call_id = f"ask-user-{uuid.uuid4().hex[:12]}"
    return (
        '\n<details type="tool_calls" done="true" '
        f'id="{html.escape(call_id)}" '
        'name="Ask User" '
        f'arguments="{html.escape(args_str[:3000])}" '
        f'result="{html.escape(answer[:8000])}" '
        'meta="" files="[]" embeds="[]">'
        '\n<summary>❓ Ask User</summary>\n</details>\n'
    )


@dataclass
class UserInputResult:
    """Outcome of trying to answer a needs-input prompt via an OWUI modal.

    handled=False  -> not a prompt, user cancelled, or delivery failed;
                      the caller should show the text as a fallback.
    handled=True, new_run_id=None -> answer delivered, the run resumed in
                      place (steer); keep consuming the current run.
    handled=True, new_run_id="..." -> answer spawned a new run; the caller
                      should switch its consumer to that run.
    """

    handled: bool
    new_run_id: str | None = None
    prompt_text: str | None = None
    answer: str | None = None


def _normalize_event_call_response(response) -> str:
    """Extract text from common OWUI __event_call__ return shapes.
    Returns empty string for errors, None, or unrecognized shapes.
    """
    if response is None:
        return ""
    # Confirmation modals resolve to a bare boolean: True=confirm, False=cancel.
    # (bool is a subclass of int, so this must be handled before dict/str.)
    if isinstance(response, bool):
        return "yes" if response else "no"
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


def _live_session_id_for_user(user_id: str, session_pool) -> str | None:
    """Find a currently-connected OWUI session_id belonging to user_id.

    session_pool is OWUI's own SESSION_POOL dict (sid -> user dict with an
    'id' field), imported live from the running process — see
    _retry_modal_on_reconnect for why this only works inside OWUI itself.
    """
    for sid, session in list(session_pool.items()):
        if session and session.get("id") == user_id:
            return sid
    return None


async def _retry_modal_on_reconnect(
    owui_user_id: str,
    owui_chat_id: str | None,
    owui_message_id: str | None,
    payload: dict,
    *,
    max_wait_s: float,
    poll_interval_s: float,
) -> str | None:
    """Wait for owui_user_id to reconnect, then re-fire the modal directly.

    OWUI's own __event_call__ closure is bound to the session_id that was
    live when the original request started; once that session disconnects
    there is no way to retarget it. But OWUI imposes no execution timeout on
    a running pipe (confirmed in docs.openwebui.com's Events page,
    "Persistence & Browser Disconnection" section: the background task
    keeps running after tab close, only killed by returning/raising, manual
    /api/tasks/stop, or a server restart) — and our pipe module runs inside
    the very same process as the OWUI backend, so we can import its live
    `sio` AsyncServer and SESSION_POOL dict directly and poll for a new
    session_id to appear for this user, then call sio.call() against it
    ourselves, bypassing the stale closure entirely.
    """
    try:
        from open_webui.socket.main import sio, SESSION_POOL
    except Exception as ex:
        pipe_log(f"  reconnect retry unavailable (not running inside OWUI process?): {ex}")
        return None

    deadline = time.monotonic() + max_wait_s
    seen_sids: set[str] = set()
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval_s)
        sid = _live_session_id_for_user(owui_user_id, SESSION_POOL)
        if not sid or sid in seen_sids:
            continue
        seen_sids.add(sid)
        pipe_log(f"  {owui_user_id[:8]}... reconnected (sid {sid[:8]}...); retrying modal")
        try:
            response = await asyncio.wait_for(
                sio.call(
                    "events",
                    {
                        "chat_id": owui_chat_id,
                        "message_id": owui_message_id,
                        "data": payload,
                    },
                    to=sid,
                    timeout=30,
                ),
                timeout=35,
            )
        except Exception as ex:
            pipe_log(f"  retry event_call failed for sid {sid[:8]}...: {ex}")
            continue
        answer = _normalize_event_call_response(response)
        if answer:
            return answer
        # Reconnected but cancelled/empty this time; keep watching in case
        # they reconnect again (e.g. an accidental tab close).
    pipe_log(f"  gave up waiting for {owui_user_id[:8]}... to reconnect after {int(max_wait_s)}s")
    return None


async def _ask_user_input_modal(
    __event_call__,
    prompt_text: str,
    *,
    timeout_s: float = 60,
    owui_user_id: str | None = None,
    owui_chat_id: str | None = None,
    owui_message_id: str | None = None,
    max_wait_s: float = 3600,
    poll_interval_s: float = 5,
    __event_emitter__=None,
) -> str | None:
    """Ask the user through OWUI's modal input API when available.

    If the live call fails (user not connected right now) and owui_user_id
    is given, keep the pipe's own background task alive and poll for the
    user to reconnect (up to max_wait_s total), retrying the modal against
    their fresh session — see _retry_modal_on_reconnect. Without
    owui_user_id, behaves exactly as before: a single attempt, exceptions
    (including TimeoutError) propagate to the caller.
    """
    if not __event_call__ or not _is_user_input_prompt(prompt_text):
        return None
    payload, _ = _modal_payload_from_user_input_prompt(prompt_text)

    try:
        response = await asyncio.wait_for(
            __event_call__(payload),
            timeout=timeout_s,
        )
        answer = _normalize_event_call_response(response)
        if answer:
            return answer
    except asyncio.TimeoutError:
        if not owui_user_id:
            raise

    if not owui_user_id:
        return None

    pipe_log(
        f"  modal not answered live; polling for {owui_user_id[:8]}... "
        f"to reconnect (up to {int(max_wait_s)}s)"
    )
    await _emit_status(
        __event_emitter__,
        "Waiting for your reply — question is pending, reconnect anytime",
        done=False,
    )
    return await _retry_modal_on_reconnect(
        owui_user_id,
        owui_chat_id,
        owui_message_id,
        payload,
        max_wait_s=max_wait_s,
        poll_interval_s=poll_interval_s,
    )


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


def _suppress_already_shown(delta: str, visible_message_text: str) -> str:
    """Final safety net against re-yielding text that's already been shown.

    `_item_delta_text`'s dedup baseline (`assistant_stream_text` or
    `last_item_text`) is a separate accumulator from `visible_message_text`
    and can drift from it — e.g. a provider's final catch-all event races
    an idle-timeout recovery cycle, or arrives after other text (tool
    blocks, recovered previews) has been recorded into
    `visible_message_text` without also updating `assistant_stream_text`.
    When that happens, `_item_delta_text`'s prefix check fails to match and
    it falls through to returning the *entire* cumulative text unchanged,
    which then gets appended a second time with no separator (P27,
    reproduced live 2026-07-10).

    `visible_message_text` is the true, complete record of everything
    already recorded for this turn, so checking whether `delta` is already
    its tail catches this regardless of which upstream baseline caused the
    false negative — without touching the primary dedup logic (so a
    genuine partial catch-up, which is never already at the tail, still
    yields normally).
    """
    if delta and visible_message_text.endswith(delta):
        return ""
    return delta


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
            if not self._event_loop_task or self._event_loop_task.done():
                self._event_loop_task = asyncio.create_task(self._event_loop())

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
        # NOTE: does not start/spawn the event-loop task — that happens
        # exactly once, in `ensure_connected`. This method is also called
        # from inside `_reconnect`, which runs from within the event-loop
        # task itself; spawning another task here would duplicate it.

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
        """Background task: reads WS messages and dispatches them.

        This is the ONLY task that ever calls `self._ws.recv()`. Reconnects
        happen in place (`self._ws = None`, then `_reconnect()` blocks until
        a new socket is ready) rather than by spawning a second event-loop
        task — spawning a second task here previously caused a runaway
        reconnect storm: the old coroutine kept looping after `_reconnect()`
        returned, so two tasks raced on `self._ws.recv()`, each collision
        raised its own exception, and each of *those* spawned yet another
        task (2026-07-10 incident, see PLAN.md P36).
        """
        while not self._stopped:
            try:
                if self._ws is None:
                    await self._reconnect()
                    continue
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
                self._ws = None
            except asyncio.CancelledError:
                break
            except Exception as e:
                pipe_log(f"Event loop error: {e}")
                self._ws = None

    async def _reconnect(self):
        """Reconnect with exponential backoff.

        Blocks (looping in place, in the single `_event_loop` task) until a
        new connection is established or the connection is stopped. Must
        NEVER spawn a new `_event_loop` task — see the docstring on
        `_event_loop` for why that caused a runaway task/connection storm.
        """
        while not self._stopped:
            self._reconnect_attempt += 1
            delay = min(2 ** (self._reconnect_attempt - 1), self._max_backoff)
            pipe_log(f"  reconnect in {delay}s (attempt {self._reconnect_attempt})")
            await asyncio.sleep(delay)
            if self._stopped:
                return
            try:
                await self._connect_and_start()
                return
            except Exception as e:
                pipe_log(f"  reconnect failed: {e}")
                self._ws = None
                # loop and try again


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

# ---------------------------------------------------------------------------
# Dynamic model discovery helpers
# ---------------------------------------------------------------------------

_FALLBACK_MODELS = [
    {"key": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash", "tags": ["configured", "alias:DeepSeek"]},
    {"key": "openai/gpt-5.5", "name": "gpt-5.5", "tags": ["configured"]},
    {"key": "anthropic/claude-opus-4-8", "name": "claude-opus-4-8", "tags": ["configured", "alias:opus"]},
    {"key": "anthropic/claude-sonnet-5", "name": "Claude Sonnet 5", "tags": ["configured", "alias:sonnet-5"]},
    {"key": "openrouter/z-ai/glm-5.2", "name": "GLM 5.2", "tags": ["configured", "alias:openrouter-glm-5.2"]},
]


def _friendly_name(model_entry: dict) -> str:
    """Return a human-friendly name for a model entry.
    
    Priority: alias tag → model name field → last segment of key.
    """
    tags = model_entry.get("tags", [])
    alias = next((t for t in tags if t.startswith("alias:")), None)
    if alias:
        name = alias.split(":", 1)[1]
        return name[0].upper() + name[1:] if name else name
    name = model_entry.get("name", "")
    if name:
        return name
    return model_entry["key"].rsplit("/", 1)[-1]


def _provider_from_key(key: str) -> str:
    """Extract the provider/vendor from a model key like 'anthropic/claude-opus-4-8'."""
    return key.split("/", 1)[0] if "/" in key else ""


def _normalize_model_entry(raw: dict) -> dict:
    """Normalize a raw gateway `models.list` entry into the {key, name, tags}
    shape used elsewhere in this module (matches _FALLBACK_MODELS).

    The gateway's actual response shape is {id, name, provider, alias, ...} —
    there is no combined "key" or "tags" field, so this bridges the two.
    """
    provider = raw.get("provider", "")
    model_id = raw.get("id", "")
    key = f"{provider}/{model_id}" if provider and model_id else (model_id or provider)
    tags = ["configured"] if raw.get("available", True) else []
    alias = raw.get("alias")
    if alias:
        tags.append(f"alias:{alias}")
    return {"key": key, "name": raw.get("name", model_id), "tags": tags}


def _parse_whitelist(text: str) -> set[str]:
    """Parse comma-separated model whitelist into a set."""
    if not text or not text.strip():
        return set()
    return {x.strip() for x in text.split(",") if x.strip()}


async def _discover_models(valves) -> list[dict]:
    """Discover available models from the gateway, cache, or hardcoded fallback.
    
    Tries in order:
    1. Live gateway request (only if connection already up)
    2. Cache file from STATE_DIR
    3. Hardcoded fallback list
    """
    # 1. Try live gateway (fast path only if already connected)
    global _gateway_connection
    conn = _gateway_connection
    if conn and conn._ws and conn._event_loop_task and not conn._event_loop_task.done():
        try:
            resp = await conn.send_request("models.list", {}, timeout=5)
            raw_models = resp.get("models", [])
            if raw_models:
                models = [_normalize_model_entry(m) for m in raw_models]
                _write_json_file(
                    os.path.join(_state_dir(getattr(valves, "STATE_DIR", "")), "models-cache.json"),
                    {"models": models, "cachedAt": time.time()}
                )
                return models
        except Exception as e:
            pipe_log(f"Live model discovery failed: {e}")
    
    # 2. Cache fallback
    cache = _read_json_file(os.path.join(_state_dir(getattr(valves, "STATE_DIR", "")), "models-cache.json"))
    if cache and cache.get("models"):
        pipe_log("Using cached model list")
        return cache["models"]
    
    # 3. Hardcoded fallback
    pipe_log("Using hardcoded fallback model list")
    return _FALLBACK_MODELS


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
        OWUI_API_KEY: str = Field(
            default="",
            description="Optional OWUI API key for file uploads; request bearer token is preferred"
        )
        FILE_SERVER_BASE_URL: str = Field(
            default="https://localhost:18791",
            description="Public URL for the file server (for MEDIA: resolution)"
        )
        CONFIGURED_MODELS: str = Field(
            default="",
            description="Comma-separated list of model keys to show in the selector. "
                "Leave empty to show all discovered models. "
                "See the Default Model valve for a reference list of available keys."
        )
        DEFAULT_MODEL: str = Field(
            default="",
            description="Override the agent's default model when 'Default' preset is selected. "
                "Also serves as a reference list of all available model keys.",
            json_schema_extra={
                "input": {"type": "select", "options": "get_model_options"}
            }
        )
        MAX_MODELS: int = Field(
            default=30,
            description="Maximum number of models to show in the selector when whitelist is empty.",
            ge=1,
            le=100
        )
        CHATGPT_MODEL: str = Field(
            default="openai/gpt-5.5",
            description="[Legacy] OpenClaw model override used by the ChatGPT manifold model. "
                "Still honored for backward compatibility."
        )
        OPUS_MODEL: str = Field(
            default="anthropic/claude-opus-4-8",
            description="[Legacy] OpenClaw model override used by the Opus 4.8 manifold model. "
                "Still honored for backward compatibility."
        )
        SONNET_MODEL: str = Field(
            default="anthropic/claude-sonnet-5",
            description="[Legacy] OpenClaw model override used by the Sonnet 5 manifold model. "
                "Still honored for backward compatibility."
        )
        GLM_MODEL: str = Field(
            default="openrouter/z-ai/glm-5.2",
            description="[Legacy] OpenClaw model override used by the GLM 5.2 manifold model. "
                "Still honored for backward compatibility."
        )
        AUTO_TITLE: bool = Field(
            default=True,
            description="Auto-generate a chat title after the first exchange (like native OWUI). "
                "Uses a fast model; best-effort, non-blocking."
        )

    def __init__(self):
        self.valves = self.Valves()
        self._active_tool_args: dict[str, str] = {}
        # Will be set per pipe() call
        self._current_session_key: str | None = None
        self._current_run_id: str | None = None
        self._connection: _GatewayConnection | None = None

    _LEGACY_PRESET_MAP = {
        "chatgpt": "openai/gpt-5.5",
        "opus": "anthropic/claude-opus-4-8",
        "sonnet": "anthropic/claude-sonnet-5",
        "glm": "openrouter/z-ai/glm-5.2",
    }

    async def pipes(self):
        """Expose multiple OWUI model-selector entries from one pipe.
        
        Dynamically discovers models from the OpenClaw Gateway (or cache)
        and returns one entry per model plus a 'Default' entry.
        """
        models = await _discover_models(self.valves)
        
        # Apply whitelist filter
        whitelist = _parse_whitelist(self.valves.CONFIGURED_MODELS)
        if whitelist:
            models = [m for m in models if m["key"] in whitelist]
        
        # Apply safety cap
        if len(models) > self.valves.MAX_MODELS:
            models = models[:self.valves.MAX_MODELS]
        
        entries = []
        for m in models:
            friendly = _friendly_name(m)
            provider = _provider_from_key(m["key"])
            name = f"{friendly} ({provider}) · OpenClaw"
            entries.append({"id": m["key"], "name": name})
        
        # Always prepend Default at top
        return [{"id": "default", "name": "OpenClaw · Default"}] + entries

    @classmethod
    def get_model_options(cls):
        """Return model options for the DEFAULT_MODEL valve dropdown.
        
        Reads synchronously from the model cache or fallback list.
        """
        cache = _read_json_file(os.path.join(_state_dir(), "models-cache.json"))
        models = cache.get("models", _FALLBACK_MODELS) if cache else _FALLBACK_MODELS
        return [{"value": m["key"], "label": f"{_friendly_name(m)} ({_provider_from_key(m['key'])})"} for m in models]

    def _selected_preset(self, body):
        """Extract the model key or legacy preset name from the OWUI model string."""
        model = str(body.get("model", ""))
        # Split on the FIRST dot only: the function id (e.g. "openclaw_gateway")
        # never contains a dot, but model keys can (e.g. "gemini-3.1-pro-preview").
        # rsplit would wrongly cut inside the version number.
        suffix = model.split(".", 1)[-1]
        if suffix == "default":
            return "default"
        if suffix in self._LEGACY_PRESET_MAP:
            return suffix  # legacy name like "chatgpt" — mapping handled downstream
        return suffix  # raw model key

    def _model_override_for_preset(self, preset):
        """Return the model string to pass to sessions.patch.
        
        For legacy presets, respects user-customized legacy valve values
        (backward compatibility) before falling back to the hardcoded mapping.
        """
        if preset == "default":
            return self.valves.DEFAULT_MODEL.strip() or None
        if preset in self._LEGACY_PRESET_MAP:
            legacy_defaults = {
                "chatgpt": "openai/gpt-5.5",
                "opus": "anthropic/claude-opus-4-8",
                "sonnet": "anthropic/claude-sonnet-5",
                "glm": "openrouter/z-ai/glm-5.2",
            }
            legacy_val = {
                "chatgpt": self.valves.CHATGPT_MODEL,
                "opus": self.valves.OPUS_MODEL,
                "sonnet": self.valves.SONNET_MODEL,
                "glm": self.valves.GLM_MODEL,
            }.get(preset, "")
            if legacy_val and legacy_val.strip() and legacy_val.strip() != legacy_defaults.get(preset, ""):
                return legacy_val.strip()
            return self._LEGACY_PRESET_MAP[preset]
        return preset

    # ── Auto-title helpers ──────────────────────────────────────────

    async def _auto_title(self, body, conn, visible_message_text,
                          owui_origin_chat_id, bearer_token):
        """Generate a chat title and set it via OWUI's REST API.

        Fires after the SECOND successful exchange (first message + response),
        when OWUI's chat_id is stable. The first request uses a temporary ID.
        Best-effort, non-blocking — failures are logged but never surfaced.
        """
        if not self.valves.AUTO_TITLE:
            return

        messages = body.get("messages", [])
        if not messages or not owui_origin_chat_id or not visible_message_text:
            return

        # Fire on the SECOND user message: one full exchange is now in history,
        # and the chat_id is stable (OWUI replaces the temp ID after exchange 1).
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        if len(user_msgs) != 2 or len(assistant_msgs) != 1:
            return

        # Build title from the FIRST exchange, not the current message
        user_text = user_msgs[0].get("content", "")[:400]
        assistant_preview = assistant_msgs[0].get("content", "")[:400]

        pipe_log(f"Auto-title: generating title for chat {owui_origin_chat_id}")

        try:
            title = await self._generate_title_text(conn, user_text, assistant_preview)
            if not title:
                return
            await self._set_owui_chat_title(owui_origin_chat_id, title, bearer_token)
            pipe_log(f"Auto-title: set to '{title}'")
        except Exception as e:
            pipe_log(f"Auto-title: failed (non-fatal): {e}")

    async def _generate_title_text(self, conn, user_msg: str,
                                    assistant_msg: str) -> str | None:
        """Use a lightweight model call to generate a 3-5 word title + emoji.

        Sends a chat.send to a temp session, then polls sessions.preview
        until the model's response is available. Avoids the complexity of
        registering consumers and consuming raw gateway events.
        """
        prompt = (
            "Create a concise title (3-5 words) with a relevant emoji "
            "for this conversation. Output ONLY the title, nothing else "
            "— no quotes, no explanation.\n\n"
            f"User message: {user_msg}\n\n"
            f"Assistant response: {assistant_msg}"
        )

        title_session = f"title-gen-{uuid.uuid4().hex[:12]}"

        # 1. Send the title-gen prompt to a new session
        try:
            await conn.send_request(
                "chat.send",
                dict(
                    sessionKey=title_session,
                    message=prompt,
                    idempotencyKey=f"title-{title_session}",
                ),
                timeout=15,
            )
        except Exception as e:
            pipe_log(f"Auto-title: chat.send failed: {e}")
            return None

        # 2. Poll sessions.preview until the model responds (up to ~20s)
        for attempt in range(7):
            await asyncio.sleep(3)
            try:
                preview = await conn.send_request(
                    "sessions.preview",
                    dict(keys=[title_session], limit=3, maxChars=600),
                    timeout=8,
                )
            except Exception:
                continue

            # Extract the last assistant message from the preview
            previews = preview.get("previews", [])
            if not previews:
                continue
            items = previews[0].get("items", [])
            for item in reversed(items):
                if isinstance(item, dict) and item.get("role") == "assistant":
                    title = str(item.get("text", "")).strip()
                    if title:
                        pipe_log(f"Auto-title: got title from preview (attempt {attempt+1})")
                        return self._clean_title_text(title)

        pipe_log("Auto-title: no assistant response in preview after polling")
        return None

    @staticmethod
    def _clean_title_text(raw: str) -> str | None:
        """Clean up raw model output into a valid title."""
        title = raw.strip('\"\' \n\r')
        # Some models wrap in quotes or add explanatory text
        if "\n" in title:
            title = title.split("\n")[0].strip()
        if len(title) > 80:
            title = title[:77] + "..."
        return title if title else None

    async def _set_owui_chat_title(self, chat_id: str, title: str,
                                    bearer_token: str | None):
        """Set the chat title via OWUI's REST API (POST /api/v1/chats/{id})."""
        token = bearer_token or self.valves.OWUI_API_KEY
        if not token:
            pipe_log("Auto-title: no OWUI auth token available")
            return

        data = json.dumps({"chat": {"title": title}}).encode()
        req = urllib.request.Request(
            f"{self.valves.OWUI_BASE_URL.rstrip('/')}/api/v1/chats/{chat_id}",
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)

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
        had_tool_block = False
        pending_prompt_text = ""
        pending_media_text = ""
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

        async def session_still_active() -> bool:
            """Check the Gateway's own run status (P26).

            A quiet queue before the first event ever arrives does not mean
            the message was lost — it may simply be queued behind another
            active run in the same session (e.g. a long-running agent task).
            Only `sessions.describe` is authoritative; ask it before treating
            60s of silence as a dead run.
            """
            try:
                desc = await conn.send_request(
                    "sessions.describe",
                    dict(key=session_key),
                    timeout=8,
                )
            except Exception as ex:
                pipe_log(f"  initial describe probe failed: {ex}")
                return False
            session_row = desc.get("session")
            if session_row is None:
                return False
            return session_row.get("status") not in ("done", "failed", "cancelled")

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

        async def maybe_answer_user_input(prompt_text: str) -> UserInputResult:
            """Ask the user via an OWUI modal and deliver their answer.

            See UserInputResult for the meaning of the return value. Crucially,
            a delivered answer that steers the *same* run (no new runId) still
            returns handled=True, so the caller suppresses the raw prompt text
            instead of leaking it into the chat.
            """
            if not _is_user_input_prompt(prompt_text):
                return UserInputResult(False, None)
            reconnect_user_id = (
                owui_origin_user_id
                if owui_origin_user_id and owui_origin_user_id != "unknown"
                else None
            )
            try:
                answer = await _ask_user_input_modal(
                    __event_call__,
                    prompt_text,
                    owui_user_id=reconnect_user_id,
                    owui_chat_id=owui_origin_chat_id,
                    owui_message_id=(__metadata__ or {}).get("message_id"),
                    __event_emitter__=__event_emitter__,
                )
            except Exception as ex:
                pipe_log(f"  user input modal failed; falling back to chat prompt: {ex}")
                answer = None
            if answer is None:
                pipe_log("  user input modal cancelled or empty answer")
                return UserInputResult(False, None)
            pipe_log("  user input answered via OWUI modal")
            await _emit_status(__event_emitter__, "Sending answer...", done=False)
            idempotency_key = f"user-input-{chat_id}-{time.time()}"
            try:
                send_resp = await conn.send_request(
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
                # Delivery failed: report not-handled so the caller falls back
                # to showing the prompt text (better than a silent stall).
                pipe_log(f"  sending answer back failed: {ex}")
                return UserInputResult(False, None)
            new_run_id = send_resp.get("runId") or None
            await _emit_status(__event_emitter__, "Answer sent; continuing...", done=False)
            if new_run_id:
                pipe_log(f"  answer sent, new runId: {new_run_id[:20]}...")
            else:
                pipe_log("  answer sent, steering into active run (no new runId)")
            return UserInputResult(True, new_run_id, prompt_text=prompt_text, answer=answer)

        adopt_new_run = True
        try:
            while adopt_new_run:
                adopt_new_run = False
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
                                elapsed = time.time() - wait_started_time
                                if elapsed < no_text_deadman_s and await session_still_active():
                                    pipe_log(
                                        "  no run events yet but session still active "
                                        "(queued behind other work); continuing to wait"
                                    )
                                    await _emit_status(
                                        __event_emitter__,
                                        "Waiting — your message is queued behind an "
                                        "active response...",
                                        done=False,
                                    )
                                    continue
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
                                # Safety net (P27, reproduced live 2026-07-10):
                                # `assistant_stream_text` can drift from
                                # `visible_message_text` and defeat the prefix
                                # check above, which then falls through to
                                # re-yielding the entire cumulative text. See
                                # `_suppress_already_shown`'s docstring.
                                suppressed = _suppress_already_shown(delta, visible_message_text)
                                if delta and not suppressed:
                                    pipe_log(
                                        "  suppressed duplicate catch-all assistant "
                                        f"text ({len(delta)} chars already shown)"
                                    )
                                delta = suppressed
                            if delta:
                                # Filter Sender metadata
                                if (
                                    "Sender (untrusted metadata)" in delta
                                    or "UnTrustedMetadata" in delta
                                ):
                                    pipe_log("  filtered metadata block")
                                    continue
                                # Hold back text while it (or a fresh line within
                                # it) is still ambiguous whether it opens with a
                                # needs-input trigger phrase. Real token-by-token
                                # streaming delivers that phrase a few characters at
                                # a time, so checking each raw delta in isolation
                                # (as used to happen here) essentially never
                                # matches — only the buffered, accumulated text can
                                # be checked reliably. Resolved once the run ends
                                # (see the `else:` clause below) or as soon as the
                                # buffered text diverges from every trigger.
                                flush_text, pending_prompt_text = (
                                    _advance_input_prompt_buffer(pending_prompt_text, delta)
                                )
                                if not flush_text:
                                    last_activity_time = time.time()
                                    continue
                                delta = flush_text

                                # Hold back a MEDIA: directive until its filename is
                                # confirmed complete (terminated by whitespace) — a
                                # real streaming provider can split "MEDIA:filename.png"
                                # across multiple deltas at any point, including
                                # mid-filename or mid-prefix. See _advance_media_buffer.
                                delta, pending_media_text = _advance_media_buffer(
                                    pending_media_text, delta
                                )
                                if not delta:
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
                                    resolved, handled = await _resolve_media_text(
                                        delta,
                                        valves=self.valves,
                                        __request__=__request__,
                                        __event_emitter__=__event_emitter__,
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
                                answer_res = await maybe_answer_user_input(item_text)
                                if answer_res.handled:
                                    if answer_res.answer:
                                        detail_block = _ask_user_detail_block(
                                            answer_res.prompt_text or item_text, answer_res.answer
                                        )
                                        record_visible_chunk(detail_block)
                                        yield detail_block
                                        await maybe_emit_snapshot(force=True)
                                    if answer_res.new_run_id and answer_res.new_run_id != our_run_id:
                                        pipe_log(f"  needs-input handled, switching to run {answer_res.new_run_id[:20]}...")
                                        conn.unregister_consumer(session_key, our_run_id, queue=queue)
                                        queue = conn.register_consumer(session_key, answer_res.new_run_id)
                                        our_run_id = answer_res.new_run_id
                                    done = False
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
                                had_tool_block = True
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

                else:
                    # Normal completion (no cancellation).
                    if pending_prompt_text:
                        # The buffered plain-text tail either fully resolved into a
                        # needs-input trigger (see `_could_be_user_input_prefix`
                        # above — buffering only continues past the ambiguous
                        # prefix stage while it keeps matching) or the run ended
                        # mid-buffer without ever diverging. Either way, resolve it
                        # now that the full text is available. `maybe_answer_user_input`
                        # re-checks the trigger itself and returns False for a
                        # partial/non-match, in which case fall back to showing it
                        # as plain text so nothing is silently dropped.
                        answer_res = await maybe_answer_user_input(pending_prompt_text)
                        if answer_res.handled:
                            # Answer delivered. Re-enter the main loop (via the outer
                            # `while adopt_new_run`) to consume the follow-up — whether
                            # the run resumed in place (steer) or a new run was spawned
                            # — so the continuation reuses the same needs-input
                            # re-detection, dedup and snapshot handling as any other
                            # run instead of a separate, drift-prone copy of the loop.
                            text_yielded = True
                            if answer_res.answer:
                                detail_block = _ask_user_detail_block(
                                    answer_res.prompt_text or pending_prompt_text, answer_res.answer
                                )
                                record_visible_chunk(detail_block)
                                yield detail_block
                                await maybe_emit_snapshot(force=True)
                            pending_prompt_text = ""
                            if answer_res.new_run_id and answer_res.new_run_id != our_run_id:
                                pipe_log(f"  continuing into new run: {answer_res.new_run_id[:20]}...")
                                conn.unregister_consumer(session_key, our_run_id, queue=queue)
                                queue = conn.register_consumer(session_key, answer_res.new_run_id)
                                our_run_id = answer_res.new_run_id
                            else:
                                pipe_log("  continuing in the same run after modal answer")
                            done = False
                            adopt_new_run = True
                            # Fresh activity clock so the idle-probe self-close does not
                            # fire immediately against a stale pre-answer timestamp.
                            last_activity_time = time.time()
                        else:
                            record_visible_chunk(pending_prompt_text)
                            yield pending_prompt_text
                            text_yielded = True
                            pending_prompt_text = ""

                    if pending_media_text:
                        # The run ended with a MEDIA: directive still buffered —
                        # either its filename never got a trailing whitespace
                        # before the stream closed, or "MEDIA:" itself was still
                        # incomplete. Nothing more is coming, so it's safe to
                        # resolve now; `_resolve_media_text` still falls back to
                        # showing it as plain text if resolution fails, so
                        # nothing is silently dropped.
                        resolved, handled = await _resolve_media_text(
                            pending_media_text,
                            valves=self.valves,
                            __request__=__request__,
                            __event_emitter__=__event_emitter__,
                        )
                        record_visible_chunk(resolved)
                        yield resolved
                        text_yielded = True
                        pending_media_text = ""

                    # For pure-text turns, do NOT force a snapshot here: OWUI's own
                    # accumulation of the streamed yields already lands the full
                    # text in `content`, and an extra forced `replace` at this
                    # point double-writes it (P23/P25 — exact duplicate, no
                    # separator).
                    #
                    # Turns that included a tool-call block are different (P27,
                    # 2026-07-08): once a `<details type="tool_calls">` block has
                    # been yielded, OWUI's own end-of-stream save no longer reliably
                    # lands the plain-text tail in `content` at all — it only shows
                    # up in `output` instead. So for tool-block turns only, force
                    # one last `content` snapshot here.
                    #
                    # This was briefly reverted the same day after what looked like
                    # a live duplicate regression, but that turned out to be a false
                    # alarm caused by redeploying the pipe mid-message in the same
                    # live chat used to test it (a self-inflicted artifact of the
                    # testing method, not this code) — confirmed by reproducing the
                    # duplicate-free "output has it once, content misses it"
                    # signature again on an unrelated turn where nothing was
                    # redeployed mid-stream. Restored. Lesson for future sessions:
                    # never redeploy this pipe function while a message in the same
                    # live chat being used to verify it is still streaming.
                    #
                    # Skip while re-entering for a modal follow-up: the snapshot must
                    # only land once the run is truly finished, not between turns.
                    if not adopt_new_run and had_tool_block:
                        await maybe_emit_snapshot(force=True)

        finally:
            await _emit_status(__event_emitter__, "", done=True)
            conn.unregister_consumer(session_key, our_run_id, queue=queue)
            self._current_session_key = None
            self._current_run_id = None

        pipe_log(f"DONE — {event_count} events processed, "
                 f"text yielded: {text_yielded}")

        # Auto-title: generate title after first exchange (best-effort, non-blocking)
        if not aborted and text_yielded:
            bearer_token = _extract_request_bearer(__request__)
            asyncio.create_task(
                self._auto_title(
                    body, conn, visible_message_text,
                    owui_origin_chat_id, bearer_token,
                )
            )

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
