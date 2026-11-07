# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------

GATEWAY_SCOPES = ["operator.admin", "operator.read", "operator.write"]
# For users not in OWNER_USERS. Without operator.admin the Gateway does not
# record their turns as the owner's, so owner-only tools are withheld. Not a
# sandbox: tools the Gateway allows non-owner turns stay available.
RESTRICTED_SCOPES = ["operator.read", "operator.write"]
# The Gateway prunes a silently paired device when another silent client with
# the same client id, mode and displayName pairs while it has no open socket.
# A displayName of its own keeps other local "webchat" clients from evicting it.
# The name carries the first characters of the device id (`_display_name`), so
# two installs on one Gateway do not share a name either. The main identity
# needs one too: on a local Gateway it is approved silently like any loopback
# client, and a connect without a name clears the stored one, so the Pipe and the
# Status Action (same identity, same device id) always send the same name.
MAIN_DISPLAY_NAME = "owui-pipe"
RESTRICTED_DISPLAY_NAME = "owui-pipe-restricted"
DISPLAY_NAME_ID_CHARS = 8


def _display_name(base: str, ident: dict | None) -> str:
    """`base` plus the start of the device id, e.g. owui-pipe-e769bc48; `base` alone without an id."""
    device_id = (ident or {}).get("id") or ""
    return f"{base}-{device_id[:DISPLAY_NAME_ID_CHARS]}" if device_id else base


def _parse_owner_users(raw: str) -> frozenset:
    """OWNER_USERS as a set of lowercase user ids and emails."""
    return frozenset(p.strip().lower() for p in (raw or "").split(",") if p.strip())


def _is_owner_user(owners: frozenset, user_id, email) -> bool:
    """An empty list makes everyone an owner (the behaviour before the valve)."""
    if not owners:
        return True
    return any(v and str(v).strip().lower() in owners for v in (user_id, email))


# Run requests go out only through `_run_request`, which picks the connection:
# the sender's scopes decide whether the turn counts as the owner's.
RUN_SEND, RUN_ABORT, RUN_PATCH = "chat.send", "chat.abort", "sessions.patch"
# chat.send fields the Gateway accepts only from an operator.admin connection.
# See `_non_owner_send_params`.
ADMIN_ONLY_SEND_FIELDS = (
    "systemInputProvenance",
    "systemProvenanceReceipt",
    "suppressCommandInterpretation",
    "originatingChannel",
    "originatingTo",
    "originatingAccountId",
    "originatingThreadId",
)
_OWUI_KEY_USER = re.compile(r"^agent:[^:]+:openwebui-([0-9a-fA-F-]{36})-")
# An Open WebUI channel thread's session (channels.py).
_CHANNEL_KEY_RE = re.compile(r"^agent:([^:]+):openwebui:channel:([^:]+):thread:([^:]+)$")


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class GatewayError(Exception):
    """Handshake failure or refused request. `error` is the Gateway's error
    shape ({code, message, details, retryable, retryAfterMs}) when sent."""

    def __init__(self, message: str = "", error: dict | None = None):
        super().__init__(message)
        self.error = error or {}


_GATEWAY_CONNECT_TIMEOUT_S = 10.0

# websockets' 1 MiB default closes the whole connection (1009) on a bigger
# frame, and a long `chat.history` or one large tool result exceeds it.
# Bounded so a runaway frame can't exhaust Open WebUI's memory.
GATEWAY_WS_MAX_SIZE = 64 * 1024 * 1024


def _explain_connect_rejection(message: str, *, device_id: str | None = None) -> str:
    """Turn the Gateway's terse connect rejection into an actionable message."""
    lowered = message.lower()
    if "pairing" in lowered or "not approved" in lowered:
        ident = f"\n\nThis device's id is `{device_id}`." if device_id else ""
        return (
            "This Open WebUI instance is not an approved device on the OpenClaw "
            "Gateway yet.\n\nOn the Gateway host, run:\n\n"
            "    openclaw devices list\n"
            "    openclaw devices approve <request-id>\n"
            f"{ident}\n\nThen send your message again."
        )
    if any(k in lowered for k in ("unauthor", "invalid token", "forbidden", "bad token")):
        return (
            f"The Gateway rejected the credentials ({message}). Check the "
            "GATEWAY_TOKEN valve in Open WebUI under Admin Panel > Functions > "
            "OpenClaw Gateway > valves; it must match `gateway.auth.token` in "
            "your OpenClaw config."
        )
    return message


def _parse_gateway_url(raw: str) -> tuple[str, int, bool]:
    """Parse the GATEWAY_URL valve into (host, port, tls).

    Users paste full URLs, so a scheme is accepted; ``wss``/``https`` set tls.
    Anything unparseable raises GatewayError naming the valve, never a bare
    ValueError that would escape pipe()'s handler.
    """
    value = (raw or "").strip()
    if not value:
        raise GatewayError(
            "The GATEWAY_URL valve is empty. Set it to your OpenClaw Gateway "
            "as host:port, for example localhost:18789."
        )
    tls = False
    if "://" in value:
        scheme, _, value = value.partition("://")
        if scheme.lower() not in ("ws", "wss", "http", "https"):
            raise GatewayError(
                f"GATEWAY_URL has an unsupported scheme '{scheme}://'. Use a "
                "bare host:port, for example localhost:18789."
            )
        tls = scheme.lower() in ("wss", "https")
    default_port = "443" if tls else "18789"
    value = value.split("/", 1)[0].split("?", 1)[0]

    if value.startswith("["):
        # Keep the brackets: websockets needs them in the URL.
        addr, sep, rest = value.partition("]")
        if not sep:
            raise GatewayError(
                f"GATEWAY_URL {raw!r} is missing a closing ']' on the IPv6 "
                "address. Expected [address]:port, for example [::1]:18789."
            )
        inner = addr[1:]
        # A bracketed non-IPv6 value makes websockets raise a bare ValueError
        # that escapes every handler, so reject it here.
        try:
            ipaddress.IPv6Address(inner)
        except ValueError:
            raise GatewayError(
                f"GATEWAY_URL {raw!r} has {inner!r} in brackets, but brackets "
                "are only for IPv6 literals. Use a plain host:port such as "
                f"{inner}:18789, or bracket an actual IPv6 address like "
                "[::1]:18789."
            )
        host = addr + "]"
        port_str = rest[1:] if rest.startswith(":") else (rest or default_port)
    elif value.count(":") > 1:
        try:
            ipaddress.IPv6Address(value)
        except ValueError:
            raise GatewayError(
                f"GATEWAY_URL {raw!r} has more than one ':' and is not an IPv6 "
                "address, so the port is ambiguous. Expected host:port, for "
                "example localhost:18789."
            )
        # rpartition(":") would read "::1" as host ":" on port 1.
        raise GatewayError(
            f"GATEWAY_URL {raw!r} is an IPv6 address. Wrap it in brackets so "
            "the port is unambiguous, for example [::1]:18789."
        )
    else:
        host, sep, port_str = value.rpartition(":")
        if not sep:
            host, port_str = value, default_port

    if not host or host == "[]":
        raise GatewayError(
            f"GATEWAY_URL {raw!r} has no host. Expected host:port, "
            "for example localhost:18789."
        )
    if not (port_str.isascii() and port_str.isdigit()):
        raise GatewayError(
            f"GATEWAY_URL {raw!r} has a non-numeric port {port_str!r}. "
            "Expected host:port, for example localhost:18789."
        )
    port = int(port_str)
    if not 1 <= port <= 65535:
        raise GatewayError(
            f"GATEWAY_URL {raw!r} has port {port}, which is outside 1-65535. "
            "The OpenClaw Gateway default is 18789."
        )
    return host, port, tls


# Budget for reading back an interrupted turn's tail. `chat.history` honors
# maxChars per message (schema max 500000) up to a 128 KiB per-message byte
# cap; 50000 chars stays under that cap even for 2-byte scripts like Hebrew.
RECOVERY_HISTORY_LIMIT = 40
RECOVERY_HISTORY_MAX_CHARS = 50000


def _event_frame(payload: dict) -> dict:
    """Wrap a routed payload as a frame for an inline reply. A `chat` event
    has a state and no stream; anything else is `agent`."""
    event = "chat" if "state" in payload and "stream" not in payload else "agent"
    return {"type": "event", "event": event, "payload": payload}


def _history_item(message) -> dict | None:
    """Flatten one `chat.history` message to {role, text}, text blocks only."""
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        text = str(message.get("text") or "")
    return {"role": message.get("role"), "text": text}


def _history_recovery_text(history: dict, user_text: str) -> str | None:
    """The assistant text saved after this user message in `chat.history`."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list):
        return None
    return _recovery_text_from_items([_history_item(m) for m in messages], user_text)


def _recovery_user_text_matches(item_text: str, user_text: str) -> bool:
    """True if a stored user message is this turn's, verbatim or behind the
    Gateway's metadata envelope."""
    return item_text == user_text or item_text.endswith("\n" + user_text)


def _recovery_text_from_items(items, user_text: str) -> str | None:
    """The LAST non-empty assistant item after the last user item matching
    `user_text`. Last because a tool-using turn stores several assistant
    messages and the first is only a preamble."""
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
        if _recovery_user_text_matches(
            str(item.get("text", "")).strip(), normalized_user_text
        ):
            last_matching_user_index = idx

    if last_matching_user_index is None:
        return None

    found = None
    for item in items[last_matching_user_index + 1:]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if item.get("role") == "user" and text:
            break  # the next turn; tool results are user items with no text
        if item.get("role") == "assistant" and text:
            found = text
    return found


async def _recover_turn_text(conn, session_key: str, user_text: str) -> str | None:
    """Read back a turn's text from `chat.history` after the stream was lost (ELI-74)."""
    try:
        history = await conn.session_history(session_key)
    except Exception as ex:
        pipe_log(f"  history recovery failed: {ex}")
        return None
    text = _history_recovery_text(history, user_text)
    if not text:
        pipe_log("  history recovery found no reply")
    return text


def _owui_session_key(agent_id: str, user_id: str, chat_id: str, column: int = 0) -> str:
    """The chat's session key. Column n > 0 is one model of a multi-model
    chat (ELI-329). In lower case, as the Gateway stores it and names it in its
    events: a temporary chat's id carries its socket's id, which has upper case,
    and a key in another case would never match its run's events."""
    key = f"agent:{agent_id}:openwebui-{user_id}-{chat_id}".lower()
    return f"{key}-m{column}" if column else key


def _session_thinking_ladder(desc: dict) -> list | None:
    """Thinking levels the session's CURRENT model supports, from a
    `sessions.describe` response.

    `thinkingLevels` ids are authoritative. `thinkingOptions` holds display
    labels, which break clamping (ELI-85); it is only a fallback. None means
    "unknown, pass through", unlike [] ("supports nothing").
    """
    row = (desc or {}).get("session") or {}
    levels = row.get("thinkingLevels")
    if isinstance(levels, list) and levels:
        out = [str(lv.get("id")) for lv in levels
               if isinstance(lv, dict) and lv.get("id")]
        if out:
            return out
    opts = row.get("thinkingOptions")
    if isinstance(opts, list) and opts:
        return [str(x).strip().lower() for x in opts]
    return None


def _session_model_key(row) -> str | None:
    """`provider/model` for a `sessions.describe` row, or None. Matches the
    Gateway's rejection text so both ladder sources share a cache key."""
    row = row or {}
    provider = str(row.get("modelProvider") or "").strip()
    model = str(row.get("model") or "").strip()
    if not provider or not model:
        return None
    return f"{provider}/{model}"


def _ladder_cache_path(state_dir: str = "") -> str:
    return os.path.join(_state_dir(state_dir), LADDER_CACHE_NAME)


# Guards the ladder cache's read-merge-write: Pipe and Status Action threads
# in one OWUI process would otherwise drop each other's updates.
_LADDER_CACHE_LOCK = threading.Lock()


def _record_thinking_ladder(levels, model_key=None, authoritative=False,
                             state_dir: str = "") -> bool:
    """Fold a ladder into the cache, as a union and per model.

    `levels` (union) feeds the Thinking filter dropdown, which OWUI cannot
    vary per model. `models[key]` is what sends clamp against. An
    `authoritative` ladder (stated in a Gateway rejection) is never
    overwritten by a describe-observed one, which can be the generic profile.
    Best-effort; returns False only when the write failed.
    """
    known = [lv for lv in (levels or []) if lv in LEVEL_RANKS]
    path = _ladder_cache_path(state_dir)

    with _LADDER_CACHE_LOCK:
        current = _read_json_file(path) or {}

        have = current.get("levels")
        have = [x for x in have if x in LEVEL_RANKS] if isinstance(have, list) else []
        merged = sorted(set(have) | set(known), key=lambda lv: (LEVEL_RANKS[lv], lv))

        models = current.get("models")
        models = dict(models) if isinstance(models, dict) else {}
        model_changed = False
        if model_key:
            prior = models.get(model_key)
            prior = prior if isinstance(prior, dict) else {}
            if prior.get("source") == "gateway" and not authoritative:
                pass
            else:
                entry = {
                    "levels": known,
                    "source": "gateway" if authoritative else "describe",
                    "updated": int(time.time()),
                }
                if (prior.get("levels") != entry["levels"]
                        or prior.get("source") != entry["source"]):
                    models[model_key] = entry
                    model_changed = True

        if merged == have and not model_changed:
            return True
        payload = {"levels": merged, "updated": int(time.time())}
        if models:
            payload["models"] = models
        if not _write_json_file(path, payload):
            return False
    if merged != have:
        pipe_log(f"thinking ladder cache updated: {merged}")
    if model_changed:
        pipe_log(
            f"thinking ladder for {model_key}: {known} "
            f"({'stated by gateway' if authoritative else 'observed'})"
        )
    return True


def _read_model_thinking_ladder(model_key, state_dir: str = ""):
    """The exact ladder for one model, or None if unknown (empty counts as unknown)."""
    if not model_key:
        return None
    cache = _read_json_file(_ladder_cache_path(state_dir)) or {}
    models = cache.get("models")
    if not isinstance(models, dict):
        return None
    entry = models.get(model_key)
    if not isinstance(entry, dict):
        return None
    levels = entry.get("levels")
    if not isinstance(levels, list):
        return None
    known = [lv for lv in levels if lv in LEVEL_RANKS]
    return known or None


def _owui_chat_send_params(
    session_key: str,
    message: str,
    idempotency_key: str,
    owui_chat_id: str | None,
    owui_user_id: str | None,
    attachments: list | None = None,
    thinking: str | None = None,
) -> dict:
    params = dict(
        sessionKey=session_key,
        message=message,
        idempotencyKey=idempotency_key,
    )
    if attachments:
        params["attachments"] = attachments
    # Absent means "agent default", unlike any value, "off" included.
    if thinking:
        params["thinking"] = thinking
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


# Runtime provider namespaces. sessions.patch reports the vendor instead
# ("claude-cli/x" resolves to "anthropic/x"), so the post-patch check must
# treat them as equal.
_RUNTIME_PROVIDER_NAMESPACES = {"claude-cli", "google-gemini-cli", "codex-cli"}


def _model_patch_matches(model_override: str | None, patch_resp: dict) -> bool:
    if model_override is None:
        return True
    if _resolved_model_key(patch_resp) == model_override:
        return True
    if "/" in model_override:
        want_provider, want_model = model_override.split("/", 1)
        got_model = patch_resp.get("resolved", {}).get("model")
        if want_provider in _RUNTIME_PROVIDER_NAMESPACES and want_model == got_model:
            return True
    return False


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


def _content_has_image(raw_content) -> bool:
    """True if an OWUI message's block-list `content` has an image_url block."""
    if not isinstance(raw_content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "image_url"
        for block in raw_content
    )


# Used only when hello-ok has no `policy.attachments.maxImageBytes`: the
# Gateway's own cap, openclaw packages/media-core/src/constants.ts:2
# (MAX_IMAGE_BYTES). chat.send refuses a bigger decoded image outright.
GATEWAY_IMAGE_BYTES_FALLBACK = 6 * 1024 * 1024

def _hello_max_image_bytes(hello_ok: dict) -> int:
    """The image cap hello-ok advertises, else the Gateway's built-in one."""
    policy = hello_ok.get("policy")
    attachments = policy.get("attachments") if isinstance(policy, dict) else None
    cap = attachments.get("maxImageBytes") if isinstance(attachments, dict) else None
    # `True` is an int in Python: it is not a byte count.
    if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
        return cap
    return GATEWAY_IMAGE_BYTES_FALLBACK


_DATA_URL_RE = re.compile(r"^data:([^;,]*)(?:;[^,]*)?,(.*)$", re.DOTALL)


def _extract_image_attachments(raw_content) -> list:
    """Inline images from OWUI `content` blocks as `chat.send` attachments.
    OWUI only sends base64 data URIs; anything else is skipped."""
    if not isinstance(raw_content, list):
        return []
    attachments = []
    for idx, block in enumerate(raw_content):
        if not (isinstance(block, dict) and block.get("type") == "image_url"):
            continue
        image_url = block.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else None
        if not isinstance(url, str):
            continue
        match = _DATA_URL_RE.match(url.strip())
        if not match or ";base64" not in url.split(",", 1)[0]:
            continue
        mime = match.group(1) or "image/png"
        ext = mimetypes.guess_extension(mime) or ".png"
        attachments.append({
            "type": "image",
            "mimeType": mime,
            "fileName": f"image-{idx + 1}{ext}",
            "content": match.group(2),
        })
    return attachments


def _numbered_attachments(attachments: list, *, combined: bool) -> list:
    """Renumber images of several combined messages so names don't collide."""
    if not combined:
        return attachments
    out = []
    for n, a in enumerate(attachments, 1):
        ext = mimetypes.guess_extension(a.get("mimeType") or "") or ".png"
        out.append({**a, "fileName": f"image-{n}{ext}"})
    return out


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


# Queue item `_GatewayConnection._mark_event_gap` puts where events were lost.
_GAP_MARKER_TYPE = "_pipe_gap"
# Put in every run queue when a reconnect lands on a new Gateway process: the
# run's owner (DESIGN 2.7) is then decided from its row.
_RESTART_MARKER_TYPE = "_pipe_restart"
# A session row for its inline replies: `{"type", "row", "resync", "read_at"}`.
# A run's own settle row ends its reply even if the final event was lost.
# `resync` (after a reconnect or gap): an idle session means every admitted
# run is over. An idle row counts only if read after the run was admitted.
_ROW_MARKER_TYPE = "_pipe_row"
# A later message was steered into this run (2026.10.10+): `{"type", "into"}`.
# The reply ends there; the rest goes to the steered message's reply.
_STEERED_MARKER_TYPE = "_pipe_steered"
# Turn pump queue items (`writer._turn_pump_loop`).
_PUMP_EVENT = "event"
_PUMP_STEERED_IN = "steered_in"
_CHAT_FINAL = "final"
_PHASE_END = "end"


def steers_mid_run_messages(version) -> bool:
    """True for a 2026.10.10+ Gateway, which steers mid-run messages into the
    run (claude-cli included). An unknown version counts as older."""
    m = re.match(r"\s*(\d{4})\.(\d{1,2})\.(\d{1,3})", str(version or ""))
    return bool(m) and tuple(int(g) for g in m.groups()) >= (2026, 10, 10)


# Tail length that anchors a cumulative text with a hole in it. Long enough
# that a match is never a coincidence of a short phrase.
_WHOLE_TEXT_ANCHOR_CHARS = 200
_MIN_WHOLE_TEXT_ANCHOR_CHARS = 64
_LINE_BREAKS_RE = re.compile(r"\n+")


def _one_break(text: str) -> str:
    """Collapse line-break runs: a claude-cli closer joins messages with one
    newline where the stream used two (ELI-219)."""
    return _LINE_BREAKS_RE.sub("\n", text)


def _item_delta_text(item_text: str, last_item_text: str, assistant_stream_text: str) -> str:
    """The new text an `item` event adds beyond what was already shown. Some
    items echo the complete final text, which must not be shown twice."""
    baseline = last_item_text or assistant_stream_text
    if not baseline:
        return item_text
    if item_text == baseline:
        return ""
    if item_text.startswith(baseline):
        return item_text[len(baseline):]
    if _one_break(item_text) == _one_break(baseline):
        return ""
    if last_item_text and item_text.endswith("\n" + last_item_text):
        # A claude-cli closer ending with the item already shown (ELI-219);
        # that item can be too short for the anchors below.
        return ""
    # Cumulative text with a hole (events lost in a reconnect): only what
    # follows the known tail is new. The hole is filled by the transcript
    # rebuild (ELI-147). Shorter anchors too: the hole may sit in the longest.
    for n in (_WHOLE_TEXT_ANCHOR_CHARS, 128, _MIN_WHOLE_TEXT_ANCHOR_CHARS):
        anchor = baseline[-n:]
        if len(anchor) < _MIN_WHOLE_TEXT_ANCHOR_CHARS:
            break
        pos = item_text.rfind(anchor)
        if pos >= 0:
            return item_text[pos + len(anchor):]
    return item_text


def _suppress_already_shown(delta: str, visible_message_text: str) -> str:
    """Safety net: drop `delta` if it is already the tail of the visible text.
    `_item_delta_text`'s baseline can drift from `visible_message_text`."""
    if delta and visible_message_text.endswith(delta):
        return ""
    return delta


def _attr(value: str) -> str:
    """Escape a tool card HTML attribute value. Newlines become `&#10;`: OWUI
    parses these attributes with a non-dotAll regex and silently drops a
    value containing a literal newline."""
    return (
        html.escape(value)
        .replace("\r\n", "&#10;")
        .replace("\n", "&#10;")
        .replace("\r", "&#10;")
    )


# `output_index` past the end of Open WebUI's output list. Without one the
# chat tab splices the item in before the last one (ELI-122).
_OUTPUT_APPEND_INDEX = 1 << 30


def _tool_call_started_event(name: str, tool_call_id: str, args_str: str) -> dict:
    """Announce a tool call as a Responses-API output item (live spinner).

    A yielded `response.*` dict lands in OWUI's backend output list, so the
    card stays mutable, unlike yielded markdown. The id goes in `id` and
    `call_id` is deliberately absent: OWUI replaces an added item sharing
    either with one it holds, so the matching `function_call_output` would
    replace this item. Consumers read `call_id or id`.
    """
    return {
        "type": "response.output_item.added",
        "output_index": _OUTPUT_APPEND_INDEX,
        "item": {
            "type": "function_call",
            "id": tool_call_id,
            "name": name,
            "arguments": args_str[:3000],
            "status": "in_progress",
        },
    }


# A Pipe note in a reply (ELI-197), carried as a run event of this made-up
# stream so the live reply and a turn writer render it alike. Its id is
# stable per note, so the same note again replaces it.
PIPE_NOTE_STREAM = "pipe_note"
_NOTE_ITEM_PREFIX = "msg_note_"
# A tool result's takeover link (CONTROL_LINK_PATTERN), as a note (ELI-318).
CONTROL_LINK_NOTE = ("**[Take control ↗]({link})**: the agent is waiting for you to do a step in "
                     "its browser. It opens in a new tab; press End there when you are done.")

# The Gateway's context compaction as a reply note (ELI-283). A size shows
# only when the Gateway sends `tokensBefore`; the session's `totalTokens` is
# stale by then and no stand-in.
COMPACTION_STREAM = "compaction"
COMPACTING_NOTE = "🧹 _Compacting context…_"
COMPACTION_FAILED_NOTE = "🧹 _Context compaction did not complete: the conversation goes on uncompacted._"
COMPACTION_SKIPPED_NOTE = "🧹 _Context compaction skipped: nothing to compact yet._"
COMPACTION_STOPPED_NOTE = "🧹 _Context compaction stopped._"
COMPACTED_BODY = ("Older messages in this conversation were summarized to free up the model's "
                  "context. The agent continues from that summary plus the most recent messages; "
                  "everything stays visible here.")


def _compacted_note(tokens_before) -> str:
    was = f" · was ~{_fmt_tokens(tokens_before)} tokens" if isinstance(tokens_before, int) \
        and tokens_before > 0 else ""
    return (f"<details>\n<summary>🧹 Context compacted{was}</summary>\n\n"
            f"{COMPACTED_BODY}\n</details>")


def _tool_call_result_event(tool_call_id: str, result_str: str) -> dict:
    """Complete a tool call announced by `_tool_call_started_event`. This item
    keeps `call_id`; see the started event for why that one does not."""
    return {
        "type": "response.output_item.added",
        "output_index": _OUTPUT_APPEND_INDEX,
        "item": {
            "type": "function_call_output",
            "id": f"fco_{tool_call_id}",
            "call_id": tool_call_id,
            "output": [{"type": "output_text", "text": result_str[:8000]}],
            "status": "completed",
        },
    }


def _tool_result_text(result) -> str:
    """The text a tool card shows for a tool event's `result`. An envelope's
    `details` is metadata, not output, and is left out (ELI-121)."""
    if isinstance(result, str):
        return result
    blocks = result.get("content") if isinstance(result, dict) else result
    if not isinstance(blocks, list):
        return json.dumps(result)
    lines = []
    for block in blocks:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text" and isinstance(block.get("text"), str):
            lines.append(block["text"])
        elif kind == "image":
            lines.append(f"[{block.get('mimeType') or 'an'} image]")
        else:
            lines.append(block if isinstance(block, str) else json.dumps(block))
    return "\n".join(lines)


TOOL_ERROR_MARK = "❌"
# What a call the run ended without answering shows as its output.
TOOL_NO_RESULT_NOTE = "(no result -- the run ended first)"

# A sub-agent title fits one phone line; the Status Action uses it too (ELI-155).
SUBAGENT_TITLE_MAX = 60
_FIRST_SENTENCE_RE = re.compile(r"(?<=[.!?])\s|\n")
# How much of a sub-agent's task its spawn card shows as input.
SPAWN_TASK_LINES = 3
SPAWN_TASK_CHARS = 300


def _short_title(text: str) -> str:
    """The first sentence of `text`, cut on a word to SUBAGENT_TITLE_MAX."""
    first = _FIRST_SENTENCE_RE.split(text.strip(), 1)[0].strip()
    if len(first) <= SUBAGENT_TITLE_MAX:
        return first
    cut = first[:SUBAGENT_TITLE_MAX - 1]
    space = cut.rfind(" ")
    if space >= SUBAGENT_TITLE_MAX // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:(") + "…"


def _is_spawn_tool(name: str) -> bool:
    """`sessions_spawn`, bare or as a claude-cli MCP tool name."""
    return name == "sessions_spawn" or (name.startswith("mcp__")
                                        and name.endswith("__sessions_spawn"))


def _json_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _spawn_title(args: dict) -> str:
    """A sub-agent's name: its spawn label, else its task's first sentence."""
    task = str(args.get("task") or "").strip()
    return str(args.get("label") or "").strip() or (_short_title(task) if task else "Sub-agent")


def _spawn_task_head(task: str) -> str:
    """The first SPAWN_TASK_LINES lines of a task, at most SPAWN_TASK_CHARS."""
    lines = [line for line in task.strip().splitlines() if line.strip()]
    head = "\n".join(lines[:SPAWN_TASK_LINES])
    if len(head) > SPAWN_TASK_CHARS:
        head = head[:SPAWN_TASK_CHARS - 1].rsplit(" ", 1)[0].rstrip(" ,;:(") + "…"
    elif len(lines) > SPAWN_TASK_LINES:
        head += "\n…"
    return head


# Deferred-tool wrappers: `tool_call` on OpenClaw 2026.10.1-10.4,
# `dispatch_action` from 10.10. The target is also reported as a nested call.
DEFERRED_CALL_TOOLS = ("dispatch_action", "tool_call")


def _is_nested_tool_call(data: dict, known: "set | dict") -> bool:
    """A deferred-tool wrapper's target call. It gets no card of its own."""
    if data.get("parentToolCallId"):
        return True
    tcid = str(data.get("toolCallId") or "")
    parts = tcid.split(":")
    return len(parts) > 2 and parts[0] == "tool_call" and parts[1] in known


OPENCLAW_MCP_PREFIX = "mcp__openclaw__"


def _bare_tool_name(name: str) -> str:
    """Strip `mcp__openclaw__`: live events drop it but claude-cli's imported
    transcript keeps it, so rebuilt cards would be titled differently."""
    return name[len(OPENCLAW_MCP_PREFIX):] if name.startswith(OPENCLAW_MCP_PREFIX) else name


def _deferred_call_view(name: str, args_str: str) -> tuple[str, str] | None:
    """A deferred-tool wrapper call as (target name, target args), else None."""
    if _bare_tool_name(name) not in DEFERRED_CALL_TOOLS:
        return None
    args = _json_object(args_str)
    target = args.get("id") if isinstance(args.get("id"), str) else args.get("name")
    if not isinstance(target, str) or not target.strip():
        return None
    inner = args.get("args")
    if not isinstance(inner, dict):  # a flattened call: the target's arguments beside `id`
        inner = {k: v for k, v in args.items() if k not in ("id", "name")}
    return target.strip().rsplit(":", 1)[-1], json.dumps(inner)


def _deferred_call_result_text(result_str: str) -> str:
    """The target's result inside a wrapper's envelope, else the text as is."""
    envelope = _json_object(result_str)
    if not isinstance(envelope.get("tool"), dict) or "result" not in envelope:
        return result_str
    return _tool_result_text(envelope["result"])


def _tool_card_view(name: str, args_str: str) -> tuple[str, str]:
    """A tool card's title and input. A spawn card shows the sub-agent's
    title and its task's head (ELI-155)."""
    name = _bare_tool_name(name)
    if not _is_spawn_tool(name):
        return name, args_str
    args = _json_object(args_str)
    task = str(args.get("task") or "")
    return f"🤖 {_spawn_title(args)}", json.dumps({"task": _spawn_task_head(task)} if task.strip() else {})


def _spawn_result_view(args_str: str, result_str: str) -> tuple[str, bool]:
    """A spawn's JSON receipt as one line, and whether the spawn failed."""
    receipt = _json_object(result_str)
    status = receipt.get("status")
    if not isinstance(status, str):
        return result_str, False
    if status == "accepted":
        return f"Started: {_spawn_title(_json_object(args_str))}", False
    error = str(receipt.get("error") or "").strip()
    return f"Not started ({status})" + (f": {error}" if error else ""), True


def _spawned_child(payload: dict):
    """(parent key, child key, run id or None) of an accepted spawn's result
    event, else None. A deferred wrapper's envelope is read too."""
    data = payload.get("data") or {}
    if payload.get("stream") != "tool" or data.get("phase") != "result" or data.get("isError") is True:
        return None
    parent = payload.get("sessionKey")
    text = _tool_result_text(data.get("result", {}))
    name = _bare_tool_name(data.get("name") or "")
    if _is_spawn_tool(name):
        receipt = _json_object(text)
    elif name in DEFERRED_CALL_TOOLS:
        receipt = _json_object(_deferred_call_result_text(text))
    else:
        return None
    child = receipt.get("childSessionKey")
    if receipt.get("status") != "accepted" or not isinstance(child, str) or not child or not parent:
        return None
    run_id = receipt.get("runId")
    return parent, child, run_id if isinstance(run_id, str) and run_id else None


def _spawn_call_args(payload: dict) -> dict | None:
    """A spawn call's arguments from its start event (wrapped or not), else None."""
    data = payload.get("data") or {}
    if payload.get("stream") != "tool" or data.get("phase") != "start":
        return None
    name, args_str = _bare_tool_name(data.get("name") or ""), json.dumps(data.get("args") or {})
    view = _deferred_call_view(name, args_str)
    if view:
        name, args_str = view
    return _json_object(args_str) if _is_spawn_tool(name) else None


def _tool_call_error_relabel_event(name: str, tool_call_id: str, args_str: str) -> dict:
    """Re-announce a failed call's item with status "failed" and ❌ in its name.

    OWUI draws the red X only for status "failed" (ELI-278). Re-sending the
    start event with the same id replaces the item in both OWUI reducers, so
    it works live on any card; `response.name.done` does not (ELI-124).
    """
    event = _tool_call_started_event(f"{name} {TOOL_ERROR_MARK}", tool_call_id, args_str)
    event["item"]["status"] = "failed"
    return event


def _tool_error_banner(result_str: str) -> str:
    """Prefix a failed tool's Output with an explicit failure line."""
    return f"{TOOL_ERROR_MARK} tool call failed\n\n{result_str}"


def _render_tool_result_block(name: str, tool_call_id: str, args_str: str,
                              result_str: str, meta) -> str:
    """Render a finished tool call as OWUI's markdown `tool_calls` card, for
    paths with no live protocol channel."""
    return (
        '\n<details type="tool_calls" done="true" '
        f'id="{_attr(tool_call_id)}" '
        f'name="{_attr(name)}" '
        f'arguments="{_attr(args_str[:3000])}" '
        f'result="{_attr(result_str[:8000])}" '
        f'meta="{_attr(str(meta)[:500])}" '
        'files="[]" embeds="[]">'
        f'\n<summary>{html.escape(name)}</summary>\n</details>\n'
    )


class _TurnRenderer:
    """Rebuild a run's visible content (text, tool cards, Pipe notes) from its
    Gateway events, as the inline reply renders it. The turn writer uses it to
    finish a message whose inline reply ended early (ELI-100). It holds raw
    run text; MEDIA and ask-user rewrites come from `note_turn_rewrite`.
    """

    def __init__(self):
        self.visible_text = ""
        # As OWUI 0.12 stores a reply: `text` is `content`, `output` the
        # Responses items. A card baked into `content` is no card (ELI-92).
        self.text = ""
        self.output: list[dict] = []
        self._assistant_stream_text = ""
        # A steered message's answer is a new `itemId` in the same run.
        self._assistant_item_id: str | None = None
        self._last_item_text = ""
        self._active_tool_args: dict[str, str] = {}
        # A result may carry no tool name (claude-cli's `tool_result`).
        self._active_tool_names: dict[str, str] = {}
        self._tool_items: dict[str, dict] = {}
        self._tool_results: set[str] = set()
        # A new run continuing the message needs the message break; the
        # Gateway only inserts it inside one run.
        self.run_boundary = False
        # A possible silent sentinel at a message start (`_add_text`).
        self._held = ""
        # The run's reply was a silent sentinel alone, not lost text.
        self.silenced = False
        self._compactions = 0
        self._compaction_note_id: str | None = None
        self._compaction_before = None

    @property
    def received(self) -> str:
        """All the run text that arrived, shown or not: the dedup baseline."""
        return self._assistant_stream_text

    @property
    def last_item(self) -> str:
        """The last item event's text, the baseline for the next one."""
        return self._last_item_text

    def seed(self, run_text: dict | None) -> None:
        """Take another reply's dedup baselines for the run it showed."""
        if run_text:
            self._assistant_stream_text = run_text.get("received", "")
            self._last_item_text = run_text.get("item", "")

    def drop_shown(self, shown: str) -> bool:
        """Drop the leading `shown` part another message already holds
        (ELI-147). False, changing nothing, if the text doesn't start with it."""
        if not self.text.startswith(shown):
            return False
        cut, pos, kept = len(shown), 0, []
        for item in self.output:
            if item.get("type") == "message":
                text = item["content"][0]["text"]
                start, pos = pos, pos + len(text)
                if pos <= cut:
                    continue
                if start < cut:
                    item = copy.deepcopy(item)
                    item["content"][0]["text"] = text[cut - start:]
            elif pos < cut:
                continue
            kept.append(item)
        self.output, self.text = kept, self.text[cut:]
        self.visible_text = self.text
        return True

    def _mark_new_reply(self, item_id) -> None:
        """A new `itemId` after a message starts a second reply in the run,
        which needs a break or the two run together (ELI-271). Notes are
        looked past."""
        if not isinstance(item_id, str) or not item_id:
            return
        previous, self._assistant_item_id = self._assistant_item_id, item_id
        last = next((i for i in reversed(self.output)
                     if not str(i.get("id", "")).startswith(_NOTE_ITEM_PREFIX)), None)
        if previous and previous != item_id and last is not None and last.get("type") == "message":
            self.run_boundary = True

    def _add(self, chunk: str) -> None:
        if chunk:
            self.visible_text += chunk

    def _add_text(self, chunk: str) -> str:
        """Append `chunk`; returns it as added. A leading silent sentinel is
        held: followed by more text it is dropped (ELI-219), alone it stays out
        (`flush`)."""
        if not chunk:
            return ""
        last = self.output[-1] if self.output else None
        if last is None or last.get("type") != "message" or not last["content"][0]["text"]:
            pending = self._held + chunk
            lead = pending.lstrip()
            if any(s.startswith(lead) for s in _SILENT_SENTINELS):
                self._held = pending
                return ""
            self._held = ""
            for s in _SILENT_SENTINELS:
                if lead.startswith(s) and lead[len(s):len(s) + 1].isspace():
                    pipe_log(f"  dropped a leading {s} before the run's text")
                    pending = lead[len(s):].lstrip()
                    break
            chunk = pending
        return self._append_text(chunk)

    def flush(self) -> str:
        """Release held text unless it is a sentinel; returns it as added."""
        held, self._held = self._held, ""
        if held.strip() in _SILENT_SENTINELS:
            self.silenced = True
        if not held.strip() or held.strip() in _SILENT_SENTINELS:
            return ""
        return self._append_text(held)

    def _append_text(self, chunk: str) -> str:
        if not chunk:
            return ""
        if self.run_boundary:
            self.run_boundary = False
            chunk = _message_boundary_separator(self.text, chunk) + chunk
        self._add(chunk)
        self.text += chunk
        last = self.output[-1] if self.output else None
        if (last is None or last.get("type") != "message"
                or str(last.get("id", "")).startswith(_NOTE_ITEM_PREFIX)):
            last = {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "status": "in_progress",
                "role": "assistant",
                "content": [{"type": "output_text", "text": ""}],
            }
            self.output.append(last)
        last["content"][0]["text"] += chunk
        return chunk

    def _compaction_note(self, run_id: str, data: dict) -> dict | None:
        """The note a compaction event makes; its end replaces its start. The
        id is deterministic so every renderer of the run names it alike."""
        phase = data.get("phase")
        if phase not in ("start", "end"):
            return None
        nid = self._compaction_note_id
        if nid is None:
            self._compactions += 1
            tag = hashlib.sha1(run_id.encode()).hexdigest()[:8]
            nid = f"{_NOTE_ITEM_PREFIX}compaction_{tag}_{self._compactions}"
            self._compaction_before = None
        if phase == "start":
            self._compaction_note_id = nid
            self._compaction_before = data.get("tokensBefore")
            return {"id": nid, "text": COMPACTING_NOTE}
        self._compaction_note_id = None
        if data.get("completed") is True:
            return {"id": nid, "text": _compacted_note(self._compaction_before)}
        return {"id": nid, "text": {"skipped": COMPACTION_SKIPPED_NOTE,
                                    "aborted": COMPACTION_STOPPED_NOTE}.get(data.get("outcome"),
                                                                           COMPACTION_FAILED_NOTE)}

    def _tool_item(self, name: str, tcid: str, args_str: str, events: list) -> dict:
        """The call's card, opened on first sight (start or result)."""
        item = self._tool_items.get(tcid)
        if item is None:
            event = _tool_call_started_event(name, tcid, args_str)
            item = copy.deepcopy(event["item"])
            self._tool_items[tcid] = item
            self.output.append(item)
            events.append(event)
        return item

    def _unanswered(self, note: str) -> list[dict]:
        """A result event for every call with no result yet, saying `note`."""
        return [_tool_call_result_event(tcid, note) for tcid in self._tool_items
                if tcid not in self._tool_results]

    def close_open_calls(self, note: str) -> list[dict]:
        """Answer every open call with `note`. OWUI would execute an unanswered
        streamed `function_call` itself, and stored it spins forever."""
        events = self._unanswered(note)
        for event in events:
            self._tool_results.add(event["item"]["call_id"])
            self.output.append(copy.deepcopy(event["item"]))
        return events

    def finished_output(self) -> list[dict]:
        """`output` as stored after the run: open calls closed, items completed."""
        out = copy.deepcopy(self.output)
        out += [e["item"] for e in self._unanswered(TOOL_NO_RESULT_NOTE)]
        for item in out:
            if item.get("status") == "in_progress":
                item["status"] = "completed"
        return out

    def feed(self, payload: dict) -> list:
        """Feed one event. Returns what it added for a live stream: text (str)
        and Responses card events, so inline and writer match (ELI-147)."""
        events: list = []
        data = payload.get("data", {}) or {}
        stream = payload.get("stream")
        name = data.get("name", "")
        phase = data.get("phase", "")
        if stream == "assistant":
            raw_delta = data.get("delta")
            if raw_delta:
                delta = raw_delta
            else:
                raw_text = data.get("text") or ""
                delta = (
                    _item_delta_text(raw_text, "", self._assistant_stream_text)
                    if raw_text else ""
                )
                if raw_text and not delta:
                    pipe_log("  suppressed duplicate catch-all assistant text")
                delta = _suppress_already_shown(delta, self.visible_text)
            if delta:
                if (
                    "Sender (untrusted metadata)" in delta
                    or "UnTrustedMetadata" in delta
                ):
                    pipe_log("  filtered metadata block")
                    return events
                self._assistant_stream_text += delta
                self._mark_new_reply(data.get("itemId"))
                events.append(self._add_text(delta))

        if stream == "item":
            item_text = _item_assistant_text(data)
            if item_text:
                item_delta = _item_delta_text(
                    item_text, self._last_item_text, self._assistant_stream_text
                )
                self._last_item_text = item_text
                item_delta = _suppress_already_shown(item_delta, self.visible_text)
                if item_delta:
                    events.append(self._add_text(item_delta))

        if stream == COMPACTION_STREAM:
            note = self._compaction_note(payload.get("runId") or "", data)
            if note is None:
                return events
            events.append(self.flush())
            stream, data = PIPE_NOTE_STREAM, note

        if stream == PIPE_NOTE_STREAM:
            nid, text = data.get("id"), data.get("text")
            if nid and text:
                item = {"type": "message", "id": nid, "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": text}]}
                at = next((n for n, i in enumerate(self.output) if i.get("id") == nid), None)
                if at is None:
                    self.output.append(item)
                else:
                    self.output[at] = item
                events.append({"type": "response.output_item.added",
                               "output_index": _OUTPUT_APPEND_INDEX, "item": copy.deepcopy(item)})
            return events

        if stream == "tool":
            tcid = data.get("toolCallId", "")
            if _is_nested_tool_call(data, self._active_tool_names.keys() | self._tool_results):
                return events
            if phase == "start":
                if tcid:
                    events.append(self.flush())
                    args_str = json.dumps(data.get("args", {}))
                    self._active_tool_args[tcid] = args_str
                    self._active_tool_names[tcid] = name
                    card_name, card_args = _tool_card_view(*(_deferred_call_view(name, args_str)
                                                             or (name, args_str)))
                    self._tool_item(card_name, tcid, card_args, events)
            elif phase == "result":
                if tcid and tcid in self._tool_results:
                    return events
                result = data.get("result", {})
                result_str = _tool_result_text(result)
                args_str = self._active_tool_args.pop(tcid, None) or json.dumps(
                    data.get("args", {})
                )
                name = self._active_tool_names.pop(tcid, None) or name
                deferred = _deferred_call_view(name, args_str)
                if deferred:
                    name, args_str = deferred
                    result_str = _deferred_call_result_text(result_str)
                failed = data.get("isError") is True
                if _is_spawn_tool(name) and not failed:
                    result_str, failed = _spawn_result_view(args_str, result_str)
                card_name, card_args = _tool_card_view(name, args_str)
                self._add(_render_tool_result_block(
                    card_name, tcid, card_args, result_str, data.get("meta", "")
                ))
                if tcid:
                    self._tool_results.add(tcid)
                    item = self._tool_item(card_name, tcid, card_args, events)
                    item["status"] = "failed" if failed else "completed"
                    if failed:
                        item["name"] = f"{card_name} {TOOL_ERROR_MARK}"
                        events.append(_tool_call_error_relabel_event(card_name, tcid, card_args))
                        result_str = _tool_error_banner(result_str)
                    event = _tool_call_result_event(tcid, result_str)
                    self.output.append(copy.deepcopy(event["item"]))
                    events.append(event)
        return events


# ---------------------------------------------------------------------------
# Persistent Gateway Connection (singleton)
# ---------------------------------------------------------------------------

# State shared by every `_GatewayConnection` in the process. A redeploy execs
# a new module with no teardown of the old one, so the bookkeeping lives on
# OWUI's stable `open_webui.socket.main` module.
_SHARED_STATE_ATTR = "_openclaw_gateway_shared_state_v1"


def _shared_gateway_state() -> dict | None:
    """The process-global bookkeeping dicts, or None outside OWUI (tests)."""
    try:
        import open_webui.socket.main as _owui_socket_main
    except Exception:
        return None
    state = getattr(_owui_socket_main, _SHARED_STATE_ATTR, None)
    if state is None:
        state = {}
        setattr(_owui_socket_main, _SHARED_STATE_ATTR, state)
    # Per key: the dict outlives redeploys, so new keys must be backfilled.
    state.setdefault("delivered_live", {})
    state.setdefault("claimed_runs", {})
    state.setdefault("chat_write_locks", {})
    state.setdefault("turn_rewrites", {})
    return state


# Streaming replies for the Status Action (ELI-186): {(chat_id, message_id):
# _TurnRenderer}. OWUI saves tool cards only at the reply's end. Lives on
# OWUI's stable module so the separate Action module sees it.
_LIVE_REPLIES_ATTR = "_openclaw_live_replies_v1"
_live_replies_local: dict = {}


def _live_replies() -> dict:
    try:
        import open_webui.socket.main as _owui_socket_main
    except Exception:
        return _live_replies_local
    replies = getattr(_owui_socket_main, _LIVE_REPLIES_ATTR, None)
    if replies is None:
        replies = {}
        setattr(_owui_socket_main, _LIVE_REPLIES_ATTR, replies)
    return replies


def _live_reply_output(chat_id, message_id):
    """A copy of a running reply's output so far, or None."""
    renderer = _live_replies().get((chat_id, message_id))
    output = getattr(renderer, "output", None)
    return copy.deepcopy(output) if isinstance(output, list) else None


class _RunQueue(asyncio.Queue):
    """A run's bounded event queue: a slow reader applies backpressure
    instead of losing text. Control markers bypass the bound, since the
    receive loop must never wait or raise on them."""

    def put_control(self, item) -> None:
        bound, self._maxsize = self._maxsize, 0
        try:
            self.put_nowait(item)
        finally:
            self._maxsize = bound

    def take_all(self) -> list:
        """Everything queued now, in order, without waiting."""
        items = []
        while not self.empty():
            items.append(self.get_nowait())
        return items


def _feeds_writer(consumer: "_Consumer") -> bool:
    """`consumer`'s queues go to a turn writer's pump (`_WriterQueue`)."""
    return bool(consumer.queues) and all(isinstance(q, _WriterQueue) for q in consumer.queues)


def _pump_baselines(pump) -> dict | None:
    """Dedup baselines of a writer pump's run, counting unwritten events."""
    writer = pump.writer
    renderer = writer.renderer if writer is not None else (pump.handover or {}).get("renderer")
    if not isinstance(renderer, _TurnRenderer):
        return None
    shadow = copy.deepcopy(renderer)
    for kind, item in list(pump.queue._queue):
        if kind == _PUMP_EVENT and item:
            shadow.feed(item)
    return {"received": shadow.received, "stream": shadow.received, "item": shadow.last_item}


class _WriterQueue(_RunQueue):
    """Forwards a run's events into a turn writer's pump when the writer owns
    the steered message's reply. Other markers stay here; the writer ends
    from the transcript at the settle."""

    def __init__(self, pump):
        super().__init__()
        self.pump = pump

    def put_nowait(self, item) -> None:
        if not isinstance(item, dict):
            return
        if item.get("_steered"):
            self.pump.queue.put_nowait((_PUMP_STEERED_IN, item["_steered"]))
        elif item.get("type") == _PUMP_EVENT:
            self.pump.queue.put_nowait((_PUMP_EVENT, item.get("payload") or {}))


# Open WebUI's uvicorn Server, kept while it serves (see `owui_shutting_down`).
_UVICORN_SERVER = None


def remember_owui_server() -> None:
    """Keep a reference to the uvicorn Server running Open WebUI. Taken early
    from the SIGTERM handler: uvicorn restores old handlers before cancelling
    tasks."""
    global _UVICORN_SERVER
    if _UVICORN_SERVER is not None:
        return
    try:
        server = getattr(signal.getsignal(signal.SIGTERM), "__self__", None)
    except (ValueError, TypeError):
        return
    if server is not None and hasattr(server, "should_exit"):
        _UVICORN_SERVER = server


def owui_shutting_down() -> bool:
    """True once Open WebUI got SIGTERM/SIGINT. Shutdown cancels replies like
    a Stop does, so this tells them apart (ELI-94)."""
    remember_owui_server()
    return bool(getattr(_UVICORN_SERVER, "should_exit", False))


remember_owui_server()


@dataclass
class _Consumer:
    """An active run consumer; its queues receive the run's events."""
    session_key: str
    run_id: str
    queues: list[asyncio.Queue] = field(default_factory=list)
    # Sent more than a lone `chat` final, so it was not steered elsewhere.
    started: bool = False
    ended: bool = False
    # Its message was steered into run `steered_into`; its own events are
    # dropped. Tracked since a socket drop can lose its lone final.
    steered: bool = False
    steered_into: str = ""
    # Writer pumps that showed this run before a steer took it (`_turn_next_run`).
    held: list = field(default_factory=list)


# Hooks the Pipe build registers; all None in the Status Action build, whose
# connection only makes requests (ELI-149).
# Turn writer entry points (writer.py's `_TurnHooks`).
_TURNS = None
# `question.*` handlers (askuser.py, ELI-96). Only the live turn-writing
# connection asks, so a question is asked once; `question.resolved` also
# reaches a retired one so pre-deploy questions still get cancelled.
_QUESTION_HOOKS = None
# `chat.metadata.changed` handler (slash_prompts.py).
_SLASH_PROMPTS_HOOK = None
# `plugin.approval.*` handlers (approvals.py), gated like `_QUESTION_HOOKS`.
_APPROVAL_HOOKS = None
PLUGIN_APPROVAL_CAP = "plugin-approvals"


class _GatewayConnection:
    """Persistent WebSocket connection to the Gateway: dispatches events to
    per-run queues and reconnects with backoff."""

    # Every connect names the device (`_display_name`). A build without this
    # flag connects nameless, which wipes the name the Gateway stored (a
    # nameless silent record can be pruned); `_retire_stale_connection` keeps
    # such a build from reconnecting.
    sends_display_name = True

    def __init__(self, valves_ref, writes_turns: bool = True, scopes: list | None = None,
                 identity_name: str = "identity", requests_only: bool = False,
                 display_name: str = MAIN_DISPLAY_NAME):
        self._valves = valves_ref
        # A RESTRICTED_SCOPES connection has its own device identity and only
        # sends requests (no subscriptions); see `_run_request`.
        self.scopes = list(GATEWAY_SCOPES if scopes is None else scopes)
        self._identity_name = identity_name
        self.requests_only = requests_only
        self._display_name = display_name
        self._ws: websockets.WebSocketClientProtocol | None = None
        # Last opened endpoint. A connection replaced by a valve change keeps
        # dialling its old one (`_pinned_endpoint`).
        self._endpoint: tuple[str, int, bool] | None = None
        self._pinned_endpoint: tuple[str, int, bool] | None = None
        self._event_loop_task: asyncio.Task | None = None
        self._init_lock = asyncio.Lock()

        self._ident: dict | None = None
        self._device_token: str | None = None

        self._pending_reqs: dict[str, asyncio.Future] = {}

        # Keyed by f"{session_key}:{run_id}".
        self._consumers: dict[str, _Consumer] = {}

        # `session_send_lock` (ELI-56).
        self._session_send_locks: dict[str, asyncio.Lock] = {}
        # A retired connection stays up for open send sections: closing it
        # would leave a waiter with no settle push (ELI-172).
        self._open_send_sections = 0
        # Per session, replies in `send_section` that may combine with a later
        # message, oldest first.
        self._send_waiters: dict[str, list] = {}

        # Parked cancelled turns (`park_cancelled_turn`). An immediate
        # chat.abort would also kill every subagent the turn spawned.
        self._pending_cancel_aborts: dict[str, dict] = {}

        # {session_key: (model, ts)} and `model_patch_lock`s (ELI-59), per worker.
        self._model_patch_cache: dict[str, tuple[str | None, float]] = {}
        self._model_patch_locks: dict[str, asyncio.Lock] = {}

        # Last thinking-clamp note per session, so it is shown once.
        self._thinking_note_shown: dict[str, str] = {}

        self._reconnect_attempt = 0
        self._max_backoff = 30  # seconds
        # A turn asks the event loop to reconnect now and awaits the outcome;
        # the loop is the socket's only reader (ELI-176).
        self._reconnect_now = asyncio.Event()
        self._reconnect_waiters: list = []
        # While turns wait on a refusing Gateway, retry at the first backoff step.
        self._turns_awaiting_gateway = 0
        # hello-ok's `policy.tickIntervalMs`; like the Gateway's own client,
        # drop a socket silent for twice that.
        self._tick_interval_s = 30.0
        self._stopped = False
        # A newer deploy took over (`retire()`): serve existing runs only,
        # write no turns, never reconnect once they end.
        self._retired = False
        # A message whose writer is gone can be taken over (DESIGN 2.3).
        self.writer_id = turn_writer_id()
        self.writes_turns = writes_turns and _TURNS is not None
        self._turn_pumps: dict = {}     # (session_key, run_id) -> _RunPump
        self._turn_writers: dict = {}   # (chat_id, message_id) -> _TurnWriter
        self._turn_skip_runs: dict = {}  # (session_key, run_id) of heartbeat runs
        # Agent-run events not yet known to be no heartbeat (`_turn_kind_known`).
        self._turn_unkinded: dict = {}
        self._turn_kinded: dict = {}
        # (child session, child run) -> a sub-agent's result
        self._child_results: dict = {}
        # session_key -> run id of a message-less inline reply awaiting its
        # interrupted run's owner
        self._awaiting_owner: dict = {}
        self._session_ids: dict = {}     # session_key -> sessionId (a cron wake's run id)
        self._wake_turns_bound: dict = {}  # session_key -> wake turn keys given a message
        # session_key -> [{job, start, end}] cron runs heard (`note_cron_event`)
        self._cron_runs: dict = {}
        self._turn_resolve_lock = asyncio.Lock()
        self._frame_seq: int | None = None
        self._stop_notice_seen = False  # `shutdown` without restartExpectedMs on this socket
        self._shutdown_seen = False     # any `shutdown` on this socket
        # Bumped on every connect.
        self.connection_epoch = 0
        # A reconnect is not a restart: `hello-ok.server.bootId` names the
        # Gateway process, and only a new one means its runs are gone.
        # Counting reconnects would send bogus resume turns.
        self.gateway_boot_id: str | None = None
        self.gateway_restarts = 0
        self.gateway_version: str | None = None   # hello-ok.server.version
        # hello-ok.policy.attachments.maxImageBytes: chat.send refuses an
        # image bigger than this (decoded bytes), so bigger ones are shrunk.
        self.gateway_max_image_bytes = GATEWAY_IMAGE_BYTES_FALLBACK
        # hello-ok.features, checked instead of version numbers.
        self.gateway_methods: frozenset = frozenset()
        self.gateway_events: frozenset = frozenset()

        self._next_req_id = 1

        # Inside OWUI these dicts are shared by every connection in the
        # process (`_shared_gateway_state`); in tests each gets its own.
        _shared = _shared_gateway_state()

        # Stubs the startup reconcile finished, so none is finalized twice.
        self._claimed_runs: dict[str, bool] = (
            _shared["claimed_runs"] if _shared is not None else {}
        )

        # `_delivered_live`: "session_key:run_id" of finals shown to a live
        # tab. A retried final is caught by identity, never by text.
        # `_held_for_live`: events of a continuing run that arrived before
        # its still-streaming reply registered; never a turn writer's.
        self._held_for_live: dict[tuple[str, str], list] = {}
        self.unreachable_held_drains = 0
        self._delivered_live: dict[str, float] = (
            _shared["delivered_live"] if _shared is not None else {}
        )

        # Model and thinking level of each session's last send (`steers_into_active_run`).
        self.sent_settings: dict[str, tuple] = {}
        # "<session>:<run>" -> "aborted" / "error" for a run that ended without an answer.
        self.runs_failed: dict[str, str] = {}

        # Per-chat locks for non-writer read-modify-write paths (stub reconcile).
        self._chat_write_locks: dict[str, asyncio.Lock] = (
            _shared["chat_write_locks"] if _shared is not None else {}
        )

        # For a run a live pipe() renders into a known message: its target
        # (chat_id, message_id) and a shadow `_TurnRenderer`, so a turn writer
        # can finish the same message if the inline turn ends first
        # (DESIGN 2.6). Instance-local: a zombie never registers a target.
        self._run_targets: dict[str, dict] = {}
        self._run_renderers: dict[str, "_TurnRenderer"] = {}
        # `note_reply_sent` (ELI-82), by "session_key:message_id".
        self._sent_replies: dict[str, dict] = {}
        # "chat_id:message_id" -> [(raw, shown)] MEDIA and ask-user rewrites
        # the inline reply made; a takeover writer repeats them. Shared across
        # deploys.
        self._turn_rewrites: dict[str, list] = (
            _shared["turn_rewrites"] if _shared is not None else {}
        )

        # Transcript subscriptions, renewed after each connect. A tool event
        # can arrive twice (initiator and subscriber); `_tool_events_seen`
        # keeps the first.
        self._message_subscriptions: set[str] = set()
        # Runs active at the last subscribe (DESIGN 2.1), whose start push
        # this socket never got. An empty list means "active, ids not given":
        # the Gateway omits activeRunIds for claude-cli runs.
        self.active_run_snapshot: dict[str, list[str]] = {}
        self._subscriptions_stale = False
        # Waiter Events per session: `sessions.changed` (ELI-88), transcript
        # appends (ELI-164), and child session changes (ELI-260).
        self._session_change_waiters: dict[str, asyncio.Event] = {}
        self._session_message_waiters: dict[str, asyncio.Event] = {}
        self._session_child_waiters: dict[str, asyncio.Event] = {}
        # The Gateway's `cron` event (a job added, changed, removed, started
        # or finished), or a socket gap: the waiter, and a count readers keep
        # to tell a cached job list is old.
        # `cron_epoch` names this connection, so a revision never matches
        # another connection's, the Pipe's and the Action's classes included.
        self._cron_waiter: asyncio.Event | None = None
        self.cron_epoch = uuid.uuid4().hex
        self.cron_revision = 0
        self._tool_events_seen: dict[tuple, bool] = {}
        # A waiting command returns its "Take control" link on every poll.
        self._control_links_shown: dict[tuple, bool] = {}
        # Spawn call args awaiting their result, for the child index.
        self._spawn_args: dict[tuple, dict] = {}
        # Child key -> its spawn's run; children whose error is recorded.
        self._spawn_runs: dict[str, str] = {}
        self._child_errors_read: set[str] = set()

    async def _subscribe_all(self) -> None:
        """Renew the session index and transcript subscriptions."""
        try:
            # The Gateway registers for pushes, THEN lists, so a run is in
            # this snapshot or its start push follows.
            resp = await self.send_request("sessions.subscribe", {"activeOnly": True}, timeout=15)
        except Exception as ex:
            pipe_log(f"  sessions.subscribe failed: {ex}")
        else:
            self._note_active_snapshot(((resp or {}).get("list") or {}).get("sessions") or [])
            self._stop_notice_seen = False
            self._shutdown_seen = False
        # Also after a failed subscribe: the turn resync reads rows with sessions.list,
        # and the reads it skipped while the socket was coming back are owed now
        # (ELI-369). A socket that dropped again leaves it to that reconnect's hello.
        if _TURNS:
            spawn_background(_TURNS.after_hello(self))
        for key in list(self._message_subscriptions):
            await self._subscribe_messages(key)
        # A waiter that read state before subscribing could miss a settle.
        self._wake_session_waiters()

    def _note_active_snapshot(self, rows: list) -> None:
        """Keep the active runs of OWUI sessions from a subscribe snapshot."""
        snap = {}
        for row in rows:
            key = str(row.get("key") or "")
            runs = [r for r in (row.get("activeRunIds") or []) if r]
            if (runs or row.get("hasActiveRun")) and self.parse_owui_session_key(key):
                snap[key] = runs
        self.active_run_snapshot = snap
        if snap:
            pipe_log(f"  subscribe snapshot: {len(snap)} OWUI session(s) with an active run")

    async def _subscribe_messages(self, session_key: str) -> bool:
        try:
            # A just-started Gateway refuses it while preparing the agent.
            await _send_when_prepared(
                self, "sessions.messages.subscribe", {"key": session_key}, timeout=15
            )
            return True
        except Exception as ex:
            pipe_log(f"  sessions.messages.subscribe {session_key[-36:]} failed: {ex}")
            return False

    async def ensure_message_subscription(self, session_key: str) -> None:
        """Subscribe to `session.tool` for `session_key`. Only the run's
        initiating socket gets tool events as `agent` events (ELI-92)."""
        if not session_key or session_key in self._message_subscriptions:
            return
        self._message_subscriptions.add(session_key)
        if not await self._subscribe_messages(session_key):
            self._message_subscriptions.discard(session_key)

    def _first_tool_event(self, payload: dict) -> bool:
        """False for a tool event already dispatched via the other path."""
        data = payload.get("data") or {}
        tcid = data.get("toolCallId")
        phase = data.get("phase")
        if not tcid or phase not in ("start", "result"):
            return True
        key = (payload.get("sessionKey"), tcid, phase)
        if key in self._tool_events_seen:
            return False
        self._tool_events_seen[key] = True
        if len(self._tool_events_seen) > 4000:
            for stale in list(self._tool_events_seen)[:2000]:
                self._tool_events_seen.pop(stale, None)
        return True

    def _control_link_note(self, payload: dict) -> dict | None:
        """A "Take control" note for a tool result matching CONTROL_LINK_PATTERN
        (ELI-318), once per link and chat. Takeover pages refuse framing, so
        the link goes in the reply text."""
        data = payload.get("data") or {}
        session = payload.get("sessionKey") or ""
        if payload.get("stream") != "tool" or data.get("phase") != "result" or not payload.get("runId"):
            return None
        pattern = (getattr(self._valves(), "CONTROL_LINK_PATTERN", "") or "").strip()
        if not pattern or not self.parse_owui_session_key(session):
            return None
        try:
            found = re.search(pattern, _tool_result_text(data.get("result", {})))
        except re.error as ex:
            pipe_log(f"  CONTROL_LINK_PATTERN is not a valid regular expression: {ex}")
            return None
        link = found.group(0).strip() if found else ""
        if not re.fullmatch(r"https?://[^\s()<>\[\]]+", link) or (session, link) in self._control_links_shown:
            return None
        self._control_links_shown[(session, link)] = True
        if len(self._control_links_shown) > 2000:
            for stale in list(self._control_links_shown)[:1000]:
                self._control_links_shown.pop(stale, None)
        pipe_log(f"  control link added: {link}")
        nid = f"{_NOTE_ITEM_PREFIX}control_{hashlib.sha256(link.encode()).hexdigest()[:12]}"
        return {"sessionKey": session, "runId": payload["runId"], "stream": PIPE_NOTE_STREAM,
                "data": {"id": nid, "text": CONTROL_LINK_NOTE.format(link=link)}}

    def _spawn_seen(self, payload: dict):
        """(parent, child, task, run fields) of an accepted spawn result, else
        None. Task and label come from the start event; the receipt has
        neither. Run fields keep a deleted child's row (ELI-276)."""
        data = payload.get("data") or {}
        call = (payload.get("sessionKey"), data.get("toolCallId"))
        args = _spawn_call_args(payload)
        if args is not None:
            self._spawn_args[call] = args
            if len(self._spawn_args) > 200:
                for stale in list(self._spawn_args)[:100]:
                    self._spawn_args.pop(stale, None)
            return None
        if data.get("phase") != "result":
            return None
        args = self._spawn_args.pop(call, None) or {}
        spawned = _spawned_child(payload)
        if not spawned:
            return None
        parent, child, run_id = spawned
        task = str(args.get("task") or "").strip() or None
        label = str(args.get("label") or "").strip()
        run = {"runId": run_id, **({"label": label} if label else {})} if run_id else None
        return parent, child, task, run

    def session_change_event(self, session_key: str) -> asyncio.Event:
        """An Event set by the next `sessions.changed` for `session_key` or a
        socket gap. Take it BEFORE reading the state so no change slips
        between the read and the wait."""
        ev = self._session_change_waiters.get(session_key)
        if ev is None:
            ev = self._session_change_waiters[session_key] = asyncio.Event()
        return ev

    def cron_event(self) -> asyncio.Event:
        """An Event set by the next `cron` event or a socket gap. Take it
        BEFORE reading the jobs so no change slips between the read and the wait."""
        if self._cron_waiter is None:
            self._cron_waiter = asyncio.Event()
        return self._cron_waiter

    def _on_cron_changed(self) -> None:
        self.cron_revision += 1
        waiter, self._cron_waiter = self._cron_waiter, None
        if waiter is not None:
            waiter.set()

    def _wake_session_waiters(self, session_key: str | None = None) -> None:
        if session_key is None:
            self._on_cron_changed()     # a gap may have lost a `cron` event too
        keys = [session_key] if session_key else list(self._session_change_waiters)
        for key in keys:
            ev = self._session_change_waiters.pop(key, None)
            if ev is not None:
                ev.set()
        if session_key is None:
            # A gap or a fresh subscription: an append may be among the lost.
            for key in list(self._session_message_waiters):
                self._session_message_waiters.pop(key).set()
            for key in list(self._session_child_waiters):
                self._session_child_waiters.pop(key).set()

    def session_child_event(self, session_key: str) -> asyncio.Event:
        """Like `session_change_event`, for a child session of `session_key`."""
        ev = self._session_child_waiters.get(session_key)
        if ev is None:
            ev = self._session_child_waiters[session_key] = asyncio.Event()
        return ev

    def session_message_event(self, session_key: str) -> asyncio.Event:
        """Like `session_change_event`, for a transcript change: an append, a
        `sessions.changed` with phase `message` (a rewrite), or a gap."""
        ev = self._session_message_waiters.get(session_key)
        if ev is None:
            ev = self._session_message_waiters[session_key] = asyncio.Event()
        return ev

    def _wake_message_waiter(self, session_key: str) -> None:
        ev = self._session_message_waiters.pop(session_key, None)
        if ev is not None:
            ev.set()

    async def read_until_stored(self, session_key: str, read, have, max_wait_s: float, empty=None):
        """`read(fresh)` until `have(result)`, re-reading after each
        transcript change. Returns (last result, whether `have` held).
        `max_wait_s` bounds the whole wait; a read's own error is raised."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait_s
        bound = object()

        async def within(coro):
            # `asyncio.wait` doesn't raise, so a read's own TimeoutError still propagates.
            task = asyncio.ensure_future(coro)
            left = deadline - loop.time()
            if left > 0:
                await asyncio.wait({task}, timeout=left)
            if not task.done():
                task.cancel()
                return bound
            return task.result()

        got = await within(read(False))
        if got is bound:
            return empty, False
        if have(got):
            return got, True
        while True:
            if await within(self.ensure_message_subscription(session_key)) is bound:
                return got, False
            changed = self.session_message_event(session_key)
            fresh = await within(read(True))
            if fresh is bound:
                return got, False
            got = fresh
            if have(got):
                return got, True
            if await within(changed.wait()) is bound:
                return got, False

    def _note_run_failed(self, payload: dict) -> None:
        """Record an aborted or failed run (no answer is stored) and wake its
        transcript waiters (`_await_steered_answer`)."""
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        how = payload.get("state") if payload.get("state") in ("aborted", "error") else (
            "error" if payload.get("stream") == "lifecycle" and data.get("phase") == "error" else None)
        sess, run = payload.get("sessionKey"), payload.get("runId")
        if not (how and sess and run):
            return
        self.runs_failed[f"{sess}:{run}"] = how
        if len(self.runs_failed) > 2000:
            for stale in list(self.runs_failed)[:1000]:
                self.runs_failed.pop(stale, None)
        self._wake_message_waiter(sess)

    def _on_session_message(self, payload: dict) -> None:
        key = payload.get("sessionKey") or ""
        self._wake_message_waiter(key)
        if key and isinstance(payload.get("message"), dict):
            self._on_user_message_committed(key, payload["message"])
        if _TURNS and key:
            _TURNS.session_message(self, key)

    def _on_shutdown(self, payload: dict) -> None:
        """DESIGN 2.7 F4: sent before the Gateway aborts runs. Without
        restartExpectedMs it is a stop, whose aborted runs are never recovered."""
        self._shutdown_seen = True
        if payload.get("restartExpectedMs") is None:
            self._stop_notice_seen = True

    def _on_sessions_changed(self, payload: dict) -> None:
        """A session row changed: wake its waiters, and subscribe a just
        started OWUI session's transcript before its first tool call."""
        key = payload.get("sessionKey") or payload.get("key") or ""
        self.note_session_id(key, payload.get("sessionId"))
        if key:
            # On any change: 2026.10.10 announces a cron wake's idle with
            # `status: done` and no `hasActiveRun` (`reason: cron-binding`).
            self._wake_session_waiters(key)
            if "hasActiveRun" in payload:
                self.row_to_consumers(key, payload)
        if key and payload.get("phase") == "message":
            # A rewrite, not an append: no `session.message` follows.
            self._wake_message_waiter(key)
        parent = payload.get("spawnedBy") or payload.get("parentSessionKey")
        if parent and parent != key:
            ev = self._session_child_waiters.pop(parent, None)
            if ev is not None:
                ev.set()
            if key and _subagent_ended(payload) and self.parse_owui_session_key(parent):
                spawn_background(self._record_child_end(parent, key, {**payload, "key": key}))
        if _TURNS and key and payload.get("hasActiveRun") is False:
            _TURNS.settled(self, key, payload)
        if (payload.get("hasActiveRun") and key
                and key not in self._message_subscriptions
                and self.parse_owui_session_key(key)):
            spawn_background(self.ensure_message_subscription(key))

    async def _record_child_end(self, parent: str, child: str, row: dict) -> None:
        """Keep a chat's child as it ended for the Status dialog, since a
        cleanup "delete" child vanishes (ELI-276). A failed run's own error is
        read too: the row's `lastRunError` is a canned summary (ELI-277)."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, functools.partial(_subagent_remember, parent, rows=[row]))
        if row.get("status") not in ("failed", "timeout") or child in self._child_errors_read:
            return
        run_id = self._spawn_runs.get(child) or ((await loop.run_in_executor(
            None, _subagent_runs, parent)).get(child) or {}).get("runId")
        if not run_id:
            return
        try:
            ended = await self.send_request("agent.wait", {"runId": run_id, "timeoutMs": 0}, timeout=5)
        except Exception as ex:
            pipe_log(f"[subagents] agent.wait {run_id} failed: {ex}")
            return
        if (ended or {}).get("status") == "timeout":
            return   # not over on the run's side yet: the next push asks again
        self._child_errors_read.add(child)
        if len(self._child_errors_read) > 2000:
            self._child_errors_read.clear()
        error = (ended or {}).get("error")
        if isinstance(error, str) and error.strip():
            await loop.run_in_executor(None, functools.partial(
                _subagent_remember, parent, runs={child: {"error": error.strip()[:2000]}}))

    def row_to_consumers(self, session_key: str, row: dict, resync: bool = False,
                         read_at: float | None = None, outside_loop: bool = False) -> None:
        """Hand `row` to every inline reply of `session_key`. `outside_loop`:
        not called by the receive loop, so the row queues behind earlier
        events, one task per queue."""
        marker = {"type": _ROW_MARKER_TYPE, "row": row, "resync": resync,
                  "read_at": time.monotonic() if read_at is None else read_at}
        for key, consumer in list(self._consumers.items()):
            if key.startswith(f"{session_key}:"):
                for q in consumer.queues:
                    if outside_loop:
                        spawn_background(q.put(dict(marker)))
                    else:
                        q.put_control(dict(marker))

    async def push_row(self, session_key: str) -> None:
        """Read the session's row now and hand it to its inline replies."""
        try:
            lst = await self.send_request("sessions.list", {"search": session_key[-36:], "limit": 10},
                                          timeout=15)
        except Exception as ex:
            pipe_log(f"  row read for {session_key[-12:]} failed: {ex!r}")
            return
        row = next((r for r in (lst.get("sessions") or []) if r.get("key") == session_key), None)
        if row is not None:
            self.row_to_consumers(session_key, row, outside_loop=True)

    def stop_awaiting_owner(self, session_key: str, run_id: str) -> None:
        """The reply decided: the agent runs held for it go on (`_release_held`)."""
        if self._awaiting_owner.get(session_key) == run_id:
            del self._awaiting_owner[session_key]
            self._release_held(session_key)

    def note_cron_event(self, payload: dict) -> None:
        """Record a cron run into a session from a `cron` event. Needed because
        a short one-shot wake leaves `cron.list` before its events are judged
        (ELI-181)."""
        job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
        target, job_id, start = job.get("sessionTarget"), payload.get("jobId"), payload.get("runAtMs")
        if not (job_id and isinstance(start, (int, float))):
            return
        run = next((r for runs in self._cron_runs.values() for r in runs
                    if r["job"] == job_id and r["start"] == start), None)
        if run is None:
            if payload.get("action") != "started" or not (
                    isinstance(target, str) and target.startswith("session:")):
                return
            runs = self._cron_runs.setdefault(target[len("session:"):], [])
            run = {"job": job_id, "start": start, "end": None}
            runs.append(run)
            del runs[:-20]
        if payload.get("action") == "finished" and isinstance(payload.get("durationMs"), (int, float)):
            run["end"] = start + payload["durationMs"]

    def cron_run_at(self, session_key: str, at_ms: float) -> dict | None:
        """The newest recorded cron run into `session_key` covering `at_ms`."""
        for run in reversed(self._cron_runs.get(session_key) or []):
            if run["start"] <= at_ms and (run["end"] is None or at_ms <= run["end"]):
                return run
        return None

    def note_session_id(self, session_key: str, session_id) -> None:
        if session_key and isinstance(session_id, str) and session_id:
            self._session_ids[session_key] = session_id

    async def session_id_for(self, session_key: str) -> str | None:
        """The session's sessionId, also every cron wake's run id. Read from
        the row when no event carried it yet."""
        known = self._session_ids.get(session_key)
        if known:
            return known
        try:
            lst = await self.send_request("sessions.list", {"search": session_key[-36:], "limit": 10},
                                          timeout=15)
        except Exception as ex:
            pipe_log(f"  sessionId read for {session_key[-12:]} failed: {ex!r}")
            return None
        for row in lst.get("sessions") or []:
            self.note_session_id(row.get("key") or "", row.get("sessionId"))
        return self._session_ids.get(session_key)

    def register_run_target(self, session_key: str, run_id: str,
                            chat_id: str | None, message_id: str | None,
                            thinking: str | None = None,
                            request_task_id: str | None = None,
                            span_run: str | None = None,
                            steer_pending: bool = False,
                            before: list | None = None) -> None:
        """Record that this run streams into an OWUI message and start its
        shadow `_TurnRenderer`. `request_task_id`: OWUI's request task, which
        a handover write waits for. `span_run`, `steer_pending` and `before`
        describe steered replies (`_settle_steered_replies`)."""
        if not (chat_id and message_id and run_id):
            return
        key = f"{session_key}:{run_id}"
        self._run_targets[key] = {"chat_id": chat_id, "message_id": message_id,
                                  "thinking": thinking, "request_task_id": request_task_id,
                                  "span_run": span_run, "steer_pending": steer_pending,
                                  "before": list(before or [])}
        self._run_renderers[key] = _TurnRenderer()
        # Bound memory for runs that never end.
        if len(self._run_targets) > 100:
            for stale in list(self._run_targets)[:50]:
                self._run_targets.pop(stale, None)
                self._run_renderers.pop(stale, None)

    def handover_of(self, session_key: str, run_id: str, thinking: str | None = None) -> dict:
        """What a turn writer takes over from a run's request (DESIGN 2.6)."""
        key = f"{session_key}:{run_id}"
        target = self._run_targets[key]
        return {"message_id": target["message_id"], "renderer": self._run_renderers.get(key),
                "thinking": thinking or target.get("thinking"), "shown": target.get("shown", ""),
                "request_task_id": target.get("request_task_id"),
                "steer_pending": bool(target.get("steer_pending"))}

    def unregister_run_target(self, session_key: str, run_id: str) -> None:
        key = f"{session_key}:{run_id}"
        self._run_targets.pop(key, None)
        self._run_renderers.pop(key, None)

    def note_reply_sent(self, session_key: str, message_id: str | None,
                        run_id: str, text: str) -> None:
        """Record the run a reply sent, so a repeat call follows it instead of
        resending (ELI-82)."""
        if not (message_id and run_id):
            return
        self._sent_replies[f"{session_key}:{message_id}"] = {"run_id": run_id, "text": text}
        if len(self._sent_replies) > 100:
            for stale in list(self._sent_replies)[:50]:
                self._sent_replies.pop(stale, None)

    def reply_sent(self, session_key: str, message_id: str | None) -> dict | None:
        """{run_id, text} that `note_reply_sent` recorded for this reply."""
        return self._sent_replies.get(f"{session_key}:{message_id}") if message_id else None

    def continue_run_target(self, session_key: str, run_id: str, next_run_id: str,
                            shown: bool = True) -> None:
        """Move the target and renderer to the next run (an ask-user answer).
        `shown` marks the old run delivered; False moves the target only."""
        key = f"{session_key}:{run_id}"
        target = self._run_targets.pop(key, None)
        renderer = self._run_renderers.pop(key, None)
        if shown:
            self.mark_delivered_live(session_key, run_id)
        if target is None:
            return
        nkey = f"{session_key}:{next_run_id}"
        self._run_targets[nkey] = target
        self._run_renderers[nkey] = renderer if renderer is not None else _TurnRenderer()

    def note_turn_rewrite(self, chat_id: str | None, message_id: str | None,
                          raw: str, shown: str) -> None:
        """Record that the inline reply showed `shown` for `raw`, so a
        takeover writer does the same."""
        if not (chat_id and message_id and raw) or raw == shown:
            return
        key = f"{chat_id}:{message_id}"
        rewrites = self._turn_rewrites.setdefault(key, [])
        if (raw, shown) not in rewrites:
            rewrites.append((raw, shown))
        if len(self._turn_rewrites) > 200:
            for stale in list(self._turn_rewrites)[:100]:
                self._turn_rewrites.pop(stale, None)

    def turn_rewrites(self, chat_id: str, message_id: str) -> list:
        return list(self._turn_rewrites.get(f"{chat_id}:{message_id}") or [])

    def _chat_write_lock(self, chat_id: str) -> asyncio.Lock:
        """The process-wide lock for read-modify-writes into `chat_id`."""
        lock = self._chat_write_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_write_locks[chat_id] = lock
        return lock

    # ── Public API ──────────────────────────────────────────────────

    @property
    def reconnecting(self) -> bool:
        """The event loop is bringing a dropped socket back; requests fail until then."""
        return (self._ws is None and not self._stopped and self._event_loop_task is not None
                and not self._event_loop_task.done())

    def endpoint_moved(self) -> bool:
        """GATEWAY_URL now names another endpoint (`_replace_moved_connection`).
        An unparseable valve counts as moved, so the new attempt names it."""
        if self._endpoint is None:
            return False
        try:
            return _parse_gateway_url(self._valves().GATEWAY_URL) != self._endpoint
        except GatewayError:
            return True

    async def ensure_connected(self):
        """Ensure the WS connection is up; connect/reconnect if needed."""
        if self._ws and self._event_loop_task and not self._event_loop_task.done():
            return
        if self._retired and not self._serving():
            # Revived retired connections pile up as orphans running old code.
            raise GatewayError(
                "gateway connection retired: a newer deploy of the pipe owns it now"
            )
        if self._event_loop_task and not self._event_loop_task.done() and not self._stopped:
            # A socket opened here would go unread until the loop's backoff
            # ends (ELI-176), so make the loop try now.
            await self._await_reconnect()
            return
        async with self._init_lock:
            if self._ws and self._event_loop_task and not self._event_loop_task.done():
                return
            self._stopped = False
            await self._connect_once_started()
            if not self._event_loop_task or self._event_loop_task.done():
                self._event_loop_task = asyncio.create_task(self._event_loop())

    async def _await_reconnect(self):
        """Await the event loop's reconnect outcome (ELI-176). A refusal means
        a restarting Gateway, so wait up to GATEWAY_DOWN_WAIT_S; other
        failures raise at once."""
        deadline = time.monotonic() + GATEWAY_DOWN_WAIT_S
        refusal = None
        self._turns_awaiting_gateway += 1
        try:
            self._reconnect_now.set()
            while True:
                waiter = asyncio.get_running_loop().create_future()
                self._reconnect_waiters.append(waiter)
                # The loop's first attempt is made at once; later ones at its first step.
                await asyncio.wait({waiter, self._event_loop_task},
                                   timeout=None if refusal is None else max(0, deadline - time.monotonic()),
                                   return_when=asyncio.FIRST_COMPLETED)
                if not waiter.done():
                    waiter.cancel()
                    if self._event_loop_task.done():
                        raise GatewayError("gateway connection stopped while reconnecting")
                    raise refusal
                error = waiter.result()
                if error is None:
                    return
                error = error if isinstance(error, GatewayError) else GatewayError(str(error))
                if not _gateway_refused(error) or time.monotonic() >= deadline:
                    raise error
                if refusal is None:
                    pipe_log("  the Gateway refuses connections (down or restarting); the message "
                             f"waits for the reconnect, up to {GATEWAY_DOWN_WAIT_S} s")
                refusal = error
        finally:
            self._turns_awaiting_gateway -= 1

    async def send_request(self, method: str, params: dict, timeout: float = 10) -> dict:
        """Send a request and wait for the response."""
        # None while reconnecting; raise a readable error, not AttributeError.
        if self._ws is None:
            raise GatewayError("gateway connection not ready (reconnecting)")
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

    def session_send_lock(self, session_key: str) -> asyncio.Lock:
        """Per-session lock around queue release, chat.send and run registration."""
        return self._session_send_locks.setdefault(session_key, asyncio.Lock())

    @contextlib.asynccontextmanager
    async def send_section(self, session_key: str):
        """`session_send_lock`, keeping this connection up until the section
        ends even if retired: the settle it waits for is pushed only to this
        socket (ELI-172)."""
        self._open_send_sections += 1
        try:
            async with self.session_send_lock(session_key):
                yield
        finally:
            self._open_send_sections -= 1
            self._close_if_retired_and_idle()

    def send_waiters(self, session_key: str) -> list:
        """Replies waiting to send, oldest first. Messages queued behind one
        run go out together as one turn."""
        return self._send_waiters.setdefault(session_key, [])

    def drop_send_waiter(self, session_key: str, waiter) -> None:
        waiters = self._send_waiters.get(session_key)
        if waiters and waiter in waiters:
            waiters.remove(waiter)
            if not waiters:
                del self._send_waiters[session_key]

    MODEL_PATCH_CACHE_TTL_S = 60.0

    def model_patch_lock(self, session_key: str) -> asyncio.Lock:
        """Per-session lock so a message burst issues one sessions.patch (ELI-59)."""
        return self._model_patch_locks.setdefault(session_key, asyncio.Lock())

    def model_patch_cached(self, session_key: str, model: str | None) -> bool:
        """True if `model` was applied within the TTL, so the patch can be
        skipped (ELI-59). The TTL bounds drift from external model changes."""
        entry = self._model_patch_cache.get(session_key)
        if entry is None:
            return False
        cached_model, ts = entry
        return cached_model == model and (time.time() - ts) < self.MODEL_PATCH_CACHE_TTL_S

    def record_model_patched(self, session_key: str, model: str | None) -> None:
        """Record a successful sessions.patch for the cache (ELI-59)."""
        self._model_patch_cache[session_key] = (model, time.time())

    def should_announce_thinking_note(self, session_key: str, note: str) -> bool:
        """True the first time a given clamp note applies to a session. A
        picked level stays picked, so repeating it on every answer is noise.
        Per connection on purpose, never persisted."""
        if not note:
            return False
        if self._thinking_note_shown.get(session_key) == note:
            return False
        self._thinking_note_shown[session_key] = note
        return True

    def invalidate_model_patch(self, session_key: str) -> None:
        """Drop the cached model after a patch failure (ELI-59)."""
        self._model_patch_cache.pop(session_key, None)

    def register_consumer(self, session_key: str, run_id: str) -> asyncio.Queue:
        """Attach a new event queue to the (session, run) consumer."""
        key = f"{session_key}:{run_id}"
        consumer = self._consumers.get(key)
        if consumer is None:
            consumer = _Consumer(session_key=session_key, run_id=run_id)
            self._consumers[key] = consumer
            pipe_log(f"  registered consumer: {key[:60]}...")
        q = _RunQueue(maxsize=500)
        consumer.queues.append(q)
        for ev in self._held_for_live.pop((session_key, run_id), []):
            q.put_nowait(_event_frame(ev))  # the run's first events, held for this reply
        return q

    def unregister_consumer(self, session_key: str, run_id: str, queue: asyncio.Queue | None = None):
        """Remove one queue, or the whole consumer when `queue` is None."""
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
            for other in [k for k, c in self._consumers.items()
                          if c.session_key == session_key and c.steered and c.steered_into == run_id]:
                del self._consumers[other]
            self._release_held(session_key)
            # Its run may never have started (`has_reply`): waiters re-read.
            self._wake_session_waiters(session_key)
        pipe_log(f"  unregistered consumer: {key[:60]}...")
        self._close_if_retired_and_idle()

    def has_reply(self, session_key: str) -> bool:
        """Whether a reply here streams a run of `session_key`, including
        before the Gateway has the run (ELI-260)."""
        return any(c.session_key == session_key and not getattr(c, "steered", False)
                   for c in self._consumers.values())

    def _serving(self) -> bool:
        """Work a retired connection still owes (ELI-172)."""
        return (any(not getattr(c, "steered", False) for c in self._consumers.values())
                or bool(self._open_send_sections))

    def _close_if_retired_and_idle(self) -> None:
        if self._retired and not self._serving():
            pipe_log("  retired gateway connection: last run finished, closing")
            spawn_background(self.disconnect())

    def live_reply_owns(self, session_key: str, run_id: str) -> bool:
        """True when `run_id` continues a message a live inline reply still
        streams; its events wait for that reply, not a second writer."""
        parsed = parse_pipe_run_id(run_id)
        waiting = self._awaiting_owner.get(session_key)
        if waiting and parsed is None and run_id != waiting:
            # Any agent run may be the Gateway's recovery of it (rule E).
            return True
        if parsed is None or parsed["kind"] == "reply":
            return False
        for c in list(self._consumers.values()):
            other = parse_pipe_run_id(c.run_id) if c.session_key == session_key else None
            if (other and c.run_id != run_id and other["chat"] == parsed["chat"]
                    and other["message"] == parsed["message"]):
                return True
        return False

    def hold_for_live(self, session_key: str, run_id: str, payload: dict) -> None:
        held = self._held_for_live.setdefault((session_key, run_id), [])
        if len(held) < 500:
            held.append(payload)

    def _release_held(self, session_key: str) -> None:
        """A reply ended: drain the runs held for it in the background, unless a
        new reply registers for them first (`register_consumer`)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop, no events to route (a sync teardown)
        for (sess, run) in list(self._held_for_live):
            if sess == session_key:
                async def drain(sess=sess, run=run):
                    self.drain_held(sess, run)
                spawn_background(drain())

    def drain_held(self, session_key: str, run_id: str) -> None:
        """Route a run's held events in order, synchronously so nothing else
        for the run can come between them."""
        if self.live_reply_owns(session_key, run_id):
            return
        held = self._held_for_live.pop((session_key, run_id), None)
        if not held:
            return
        consumer = self._consumers.get(f"{session_key}:{run_id}")
        if consumer is not None:
            # Unreachable (`register_consumer` takes them first); logged if it breaks.
            self.unreachable_held_drains += 1
            pipe_log(f"  ERROR unreachable: {len(held)} held event(s) for an already-registered "
                     f"consumer of {run_id[:24]}... (count {self.unreachable_held_drains})")
            for q in consumer.queues:
                for ev in held:
                    q.put_control(_event_frame(ev))
            return
        pipe_log(f"  {len(held)} held event(s) of {run_id[:24]}... released to the turn writer")
        for ev in held:
            _TURNS.route_now(self, ev)  # only the turn writer holds events

    def mark_delivered_live(self, session_key: str, run_id: str):
        """Record that a live tab got this run's final, so a late event never
        reopens its message (`_turn_route_now`)."""
        key = f"{session_key}:{run_id}"
        self._delivered_live[key] = time.time()
        if len(self._delivered_live) > 500:
            oldest_first = sorted(self._delivered_live.items(), key=lambda kv: kv[1])
            for stale_key, _ in oldest_first[:250]:
                self._delivered_live.pop(stale_key, None)

    def was_delivered_live(self, session_key: str, run_id: str) -> bool:
        return f"{session_key}:{run_id}" in self._delivered_live

    def active_run_id_for_session(self, session_key: str) -> str | None:
        """Return the sole active run id for a session, if one is registered."""
        matches = [
            consumer.run_id for consumer in self._consumers.values()
            if consumer.session_key == session_key
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def user_turn_run_active(self, session_key: str, chat_id: str) -> bool:
        """The session's one active run is a Pipe reply run in `chat_id`, the
        only kind the Gateway steers into. Into any other it falls back to its
        follow-up queue, where on 2026.10.10-beta.1 the send never runs."""
        # Started only: a message waiting to be steered has a consumer too.
        runs = {c.run_id for c in self._consumers.values()
                if c.session_key == session_key and c.started and not (c.ended or c.steered)}
        runs |= {run for (s, run), pump in getattr(self, "_turn_pumps", {}).items()
                 if s == session_key and pump.started and not pump.terminal.is_set()}
        parsed = parse_pipe_run_id(next(iter(runs))) if len(runs) == 1 else None
        return bool(parsed and parsed["kind"] == "reply" and parsed["chat"] == chat_id)

    def parse_owui_session_key(self, session_key: str) -> tuple[str, str] | None:
        """(user_id, chat_id) of a key `_owui_session_key` built, else None.
        Split by position: both are 36-char UUIDs containing dashes."""
        agent_id = getattr(self._valves(), "AGENT_ID", None)
        if not agent_id:
            return None
        prefix = f"agent:{agent_id}:openwebui-".lower()   # as `_owui_session_key` builds it
        if not session_key.startswith(prefix):
            return None
        after = session_key[len(prefix):]
        if len(after) < 73 or after[36] != "-":
            return None
        return after[0:36], after[37:73]

    async def dispatch_run_event(self, msg: dict) -> None:
        """Route one run event (or Pipe note, ELI-197): shadow render, then
        live reply or turn writer. A takeover link adds a note (ELI-318)."""
        payload = msg.get("payload", {})
        if payload.get("stream") == "tool" and not self._first_tool_event(payload):
            return
        await self._dispatch_run_event(msg)
        note = self._control_link_note(payload)
        if note is not None:
            await self._dispatch_run_event({"type": "event", "event": "agent", "payload": note})

    async def _dispatch_run_event(self, msg: dict) -> None:
        payload = msg.get("payload", {})
        self._note_run_failed(payload)
        spawned = self._spawn_seen(payload) if payload.get("stream") == "tool" else None
        if spawned:
            # The Status dialog's child index (state.py), off the loop.
            parent, child, task, run = spawned
            if run:
                self._spawn_runs[child] = run["runId"]
                if len(self._spawn_runs) > 2000:
                    for stale in list(self._spawn_runs)[:1000]:
                        self._spawn_runs.pop(stale, None)
            asyncio.get_running_loop().run_in_executor(None, functools.partial(
                _subagent_remember, parent, keys=[child], tasks={child: task} if task else None,
                runs={child: run} if run else None))

        # Shadow render, live consumer or not: a takeover writer starts from it.
        _evt_sess = payload.get("sessionKey", "")
        _evt_run = payload.get("runId", "")
        if _evt_sess and _evt_run:
            _renderer = self._run_renderers.get(f"{_evt_sess}:{_evt_run}")
            if _renderer is not None:
                try:
                    _renderer.feed(payload)
                except Exception as ex:
                    pipe_log(f"  shadow renderer feed failed: {ex}")

        if _TURNS and _evt_sess and _evt_run:
            _TURNS.next_run(self, payload)
        consumers = self.consumers_for_event(payload)

        # ELI-100: a run no live request consumes is a turn.
        if not consumers and _TURNS and _TURNS.route_now(self, payload):
            return

        if consumers and not self._note_run_event(msg, consumers[0]):
            return
        if consumers:
            if not payload.get("runId"):
                pipe_log("  dispatched session-only event to sole consumer")
            if payload.get("state") == "final" and payload.get("sessionKey") and payload.get("runId"):
                self.mark_delivered_live(payload["sessionKey"], payload["runId"])
                self.unregister_run_target(payload["sessionKey"], payload["runId"])
            for q in consumers[0].queues:
                await q.put(msg)
            return

        if payload.get("sessionKey") and not payload.get("runId"):
            pipe_log("  dropped ambiguous or unmatched session-only event")

    def _note_run_event(self, msg: dict, consumer: _Consumer) -> bool:
        """Track whether `consumer`'s run started, ended or was steered. False
        drops the event. A first event that is a message-less `chat` final
        means it was steered and the commit did not switch it (`_steer`)."""
        payload = msg.get("payload") or {}
        terminal = (payload.get("state") in ("final", "error", "aborted")
                    or (payload.get("stream") == "lifecycle"
                        and (payload.get("data") or {}).get("phase") in ("end", "error")))
        if consumer.steered:
            if terminal:
                self._consumers.pop(f"{consumer.session_key}:{consumer.run_id}", None)
            return False
        if (not consumer.started and msg.get("event") == "chat"
                and payload.get("state") == "final" and not payload.get("message")
                and steers_mid_run_messages(self.gateway_version) and self._steer(consumer)):
            self._consumers.pop(f"{consumer.session_key}:{consumer.run_id}", None)
            return False
        consumer.started = True
        consumer.ended = consumer.ended or terminal
        if terminal and (payload.get("state") == _CHAT_FINAL
                         or (payload.get("data") or {}).get("phase") == _PHASE_END):
            # Its writers end at the next run's first event if no settle comes first.
            for p in [*consumer.held, *(q.pump for q in consumer.queues if isinstance(q, _WriterQueue))]:
                p.ended_clean = consumer.run_id
        if consumer.ended and _feeds_writer(consumer):
            # A turn writer shows the rest (`_steer_handed_over`) and ends at the
            # settle; late events open nothing. Its pump learns the run ended, so
            # a message committed before the settle is no steer into it.
            pump = self._turn_pumps.get((consumer.session_key, consumer.run_id))
            if pump is not None:
                pump.terminal.set()
            self.mark_delivered_live(consumer.session_key, consumer.run_id)
            self.unregister_run_target(consumer.session_key, consumer.run_id)
            self._consumers.pop(f"{consumer.session_key}:{consumer.run_id}", None)
            self._wake_session_waiters(consumer.session_key)
            self._close_if_retired_and_idle()
            return False
        return True

    def _on_user_message_committed(self, session_key: str, message: dict) -> None:
        """A user message was committed. On 2026.10.10+ a commit naming a
        steer target comes before the answer's first event, so the steer is
        applied from here."""
        idem = str((message.get("__openclaw") or {}).get("idempotencyKey") or "")
        if not (message.get("role") == "user" and idem.endswith(":user")
                and steers_mid_run_messages(self.gateway_version)):
            return
        # A queued (not steered) message is committed with no target.
        into = (message.get("__openclaw") or {}).get("steerTargetRunId")
        if not into:
            return
        run_id = idem[:-len(":user")]
        consumer = self._consumers.get(f"{session_key}:{run_id}")
        if consumer is not None and not (consumer.started or consumer.steered):
            self._steer(consumer, into=into)
        elif consumer is None and (handed := self._steer_handed_over(session_key, run_id)) is not None:
            self._steer(handed, into=into)

    def _steer_handed_over(self, session_key: str, run_id: str) -> _Consumer | None:
        """A consumer feeding the turn writer that took over a reply still
        waiting to be steered (`_WriterQueue`), else None."""
        pump = getattr(self, "_turn_pumps", {}).get((session_key, run_id))
        if pump is None or pump.started or not (pump.handover or {}).get("steer_pending"):
            return None
        return _Consumer(session_key=session_key, run_id=run_id, queues=[_WriterQueue(pump)])

    def _steer(self, consumer: _Consumer, into: str | None = None) -> bool:
        """Send the session's active run's events from here on to the reply of
        `consumer`'s message, which was steered into it. The previous reply
        ends at this point; the steered one gets the run and its dedup
        baselines. False with no single active run, or not `into`."""
        sess = consumer.session_key
        active = [c for c in self._consumers.values()
                  if c.session_key == sess and c is not consumer and c.started
                  and not (c.ended or c.steered)]
        pumps = [(run, pump) for (s, run), pump in getattr(self, "_turn_pumps", {}).items()
                 if s == sess and pump.started and not pump.wake and not pump.terminal.is_set()]
        if len(active) == 1:
            run = active[0]
            if into and run.run_id != into:
                return False
            if _feeds_writer(run):
                # The writer has the baselines; it stays open until the settle.
                fed = run.queues[0].pump
                writer = fed.writer
                earlier = writer and {"chat_id": writer.chat_id, "message_id": writer.message_id,
                                      "request_task_id": None, "span_run": run.run_id}
                run_text = _pump_baselines(fed)
                run.held.append(fed)
            else:
                earlier = self._run_targets.get(f"{sess}:{run.run_id}")
                earlier = earlier and {k: earlier.get(k) for k in ("chat_id", "message_id", "request_task_id")} | {
                    "span_run": earlier.get("span_run") or run.run_id, "before": earlier.get("before") or []}
                shadow = self._run_renderers.get(f"{sess}:{run.run_id}")
                run_text = ({"received": shadow.received, "stream": shadow.received,
                             "item": shadow.last_item} if shadow is not None else None)
            for q in run.queues:
                q.put_control({"type": _STEERED_MARKER_TYPE, "into": consumer.run_id})
            run.queues = list(consumer.queues)
            run_id = run.run_id
        elif not active and len(pumps) == 1:
            run_id, pump = pumps[0]
            if into and run_id != into:
                return False
            writer = pump.writer
            earlier = writer and {"chat_id": writer.chat_id, "message_id": writer.message_id,
                                  "request_task_id": None, "span_run": run_id}
            run_text = asyncio.get_running_loop().create_future()
            if _feeds_writer(consumer):
                # The old writer ends at the settle from the transcript, which
                # covers event gaps. Baselines now: its pump may be busy.
                run_text = _pump_baselines(pump)
            else:
                pump.queue.put_nowait(("steered", run_text))
            self._consumers[f"{sess}:{run_id}"] = _Consumer(
                session_key=sess, run_id=run_id, queues=list(consumer.queues), started=True,
                held=[pump] if _feeds_writer(consumer) else [])
        else:
            return False
        consumer.steered, consumer.steered_into = True, run_id
        for q in consumer.queues:
            q.put_control({"type": _STEERED_MARKER_TYPE, "_steered": {
                "run": run_id, "run_text": run_text, "earlier": earlier}})
        pipe_log(f"  run {consumer.run_id[:24]}... steered into {run_id[:24]}...; "
                 "its reply continues that run")
        return True

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
                if consumer.session_key == evt_session and not consumer.steered
            ]
            return matches if len(matches) == 1 else []
        return []

    async def session_history(self, session_key: str,
                              limit: int = RECOVERY_HISTORY_LIMIT,
                              max_chars: int = RECOVERY_HISTORY_MAX_CHARS,
                              timeout: float = 10) -> dict:
        """Recent messages, normalized by `_transcript_roles` and
        `_transcript_commit_order`."""
        hist = await self.send_request(
            "chat.history",
            dict(sessionKey=session_key, limit=limit, maxChars=max_chars),
            timeout=timeout
        )
        if isinstance(hist, dict) and isinstance(hist.get("messages"), list):
            hist["messages"] = _transcript_commit_order(_transcript_roles(hist["messages"]))
        return hist

    async def abort(self, session_key: str, run_id: str):
        """Send chat.abort for an active run."""
        try:
            await _run_request(self, RUN_ABORT, dict(sessionKey=session_key, runId=run_id),
                               wait=False)
            pipe_log(f"  sent chat.abort for run {run_id[:20]}...")
        except Exception as e:
            pipe_log(f"  abort send failed: {e}")

    async def _abort_cancelled_run(self, session_key: str, run_id: str,
                                   send_stop: bool) -> None:
        await self.abort(session_key, run_id)
        if send_stop:
            pipe_log("Generator cancelled -- sending /stop fallback")
            await self.send_stop(session_key)

    def park_cancelled_turn(self, session_key: str, run_id: str | None,
                            queue: asyncio.Queue | None, carried: list[str],
                            grace_s: float, send_stop: bool,
                            run_text: dict | None = None, settle=None,
                            reply: dict | None = None,
                            waiting: list | None = None,
                            attachments: list | None = None) -> None:
        """Park a cancelled reply's unfinished business for the next message.

        OWUI cancels the same way for Stop and Send now, so the turn is parked
        and decided after. The run keeps going and `queue` buffers its events;
        `carried`/`attachments` are accepted but unsent messages; `run_text`
        holds dedup baselines, since closing events repeat the whole text;
        `waiting` replies are ended by the claimer or a release (ELI-188).
        `settle` asks the browser which button it was, in a background task
        (ELI-95); without it `grace_s` decides. Unclaimed, the run is aborted.

        Synchronous on purpose: it runs on the cancel path, where an await can
        be interrupted. An OWUI shutdown is no Stop: the run keeps going for
        the next process's reconcile (ELI-90, ELI-94).
        """
        if owui_shutting_down():
            if queue is not None:
                self.unregister_consumer(session_key, run_id, queue=queue)
            pipe_log(f"  Open WebUI is shutting down: run {str(run_id)[:20]}... keeps going "
                     f"({len(carried)} unsent message(s) dropped)")
            return
        previous = self._pending_cancel_aborts.pop(session_key, None)
        if previous is not None:
            if previous["task"] is not None:
                previous["task"].cancel()
            previous["resolved"].set()
            carried = list(previous["carried"]) + list(carried)
            attachments = list(previous["attachments"]) + list(attachments or [])
            waiting = list(previous["waiting"]) + [
                r for r in (waiting or []) if r not in previous["waiting"]]
            if previous["queue"] is not None and previous["queue"] is not queue:
                self.unregister_consumer(
                    session_key, previous["run_id"], queue=previous["queue"])
            if previous["run_id"] and previous["run_id"] != run_id:
                spawn_background(self._abort_cancelled_run(
                    session_key, previous["run_id"], previous["send_stop"]))
        if run_id:
            # The run's tail belongs to the claimer, never the stopped message.
            # A reply still waiting to be steered keeps `steer_pending`.
            target = self._run_targets.get(f"{session_key}:{run_id}") or {}
            if reply is not None and target.get("steer_pending"):
                reply = {**reply, "steer_pending": True}
            self.unregister_run_target(session_key, run_id)
            self.mark_delivered_live(session_key, run_id)

        entry = {"run_id": run_id, "queue": queue, "carried": list(carried),
                 "attachments": list(attachments or []),
                 "send_stop": send_stop, "task": None,
                 "resolved": asyncio.Event(), "run_text": run_text, "reply": reply,
                 "waiting": list(waiting or [])}

        def _release() -> None:
            # Read from the entry: `_keep_in_own_message` may have cleared it.
            if entry["queue"] is not None:
                self.unregister_consumer(session_key, entry["run_id"], queue=entry["queue"])
            if entry["carried"]:
                pipe_log(f"  stopped: dropping {len(entry['carried'])} unsent "
                         "message(s)")
                if _TURNS is not None:
                    _TURNS.end_unsent_replies(self, entry["waiting"], sent=False)
            if entry["run_id"]:
                spawn_background(
                    self._abort_cancelled_run(session_key, entry["run_id"], send_stop))

        entry["release"] = _release
        if grace_s <= 0:
            _release()
            return
        self._pending_cancel_aborts[session_key] = entry
        self._arm_parked_timer(session_key, entry, grace_s)
        if settle is None:
            entry["resolved"].set()
            return

        async def _settle() -> None:
            try:
                await settle()
            finally:
                entry["resolved"].set()
        spawn_background(_settle())

    def _arm_parked_timer(self, session_key: str, entry: dict,
                          wait_s: float) -> None:
        if entry["task"] is not None:
            entry["task"].cancel()

        async def _fire():
            await asyncio.sleep(wait_s)
            if self._pending_cancel_aborts.get(session_key) is not entry:
                return
            if entry.get("claiming"):
                # A follow-up is waiting: only an explicit Stop releases it now.
                return
            self._pending_cancel_aborts.pop(session_key, None)
            entry["resolved"].set()
            pipe_log(f"  no new message within {wait_s:g}s of the cancel: "
                     f"stopping run {str(entry['run_id'])[:20]}...")
            entry["release"]()

        entry["task"] = spawn_background(_fire())

    def settle_parked_turn(self, session_key: str, run_id: str | None,
                           intent: str | None, hold_s: float) -> None:
        """Apply the browser's answer about the cancel: "stop" stops now,
        "send_now" holds the run up to `hold_s` for the follow-up, anything
        else keeps the fallback timer."""
        entry = self._pending_cancel_aborts.get(session_key)
        if entry is None or entry["run_id"] != run_id:
            return
        if intent == "stop":
            self._pending_cancel_aborts.pop(session_key, None)
            if entry["task"] is not None:
                entry["task"].cancel()
            pipe_log(f"  cancel was a Stop: stopping run {str(run_id)[:20]}... now")
            entry["release"]()
        elif intent == "send_now":
            # The follow-up claims the run, not this answer: a writer opened now
            # would be overwritten by OWUI's own cancel handling.
            pipe_log(f"  cancel was a Send now: holding run {str(run_id)[:20]}... "
                     f"for the follow-up message (up to {hold_s:g}s)")
            self._arm_parked_timer(session_key, entry, hold_s)
        else:
            pipe_log("  cancel intent unknown (no page hook or no answer): "
                     "keeping the fallback window")
        entry["resolved"].set()

    def _keep_in_own_message(self, session_key: str, entry: dict) -> bool:
        """Hand a parked run to a turn writer in the message it was cancelled
        in, with the buffered events (ELI-158). False with no message or run."""
        reply, run_id, queue = entry.get("reply"), entry["run_id"], entry["queue"]
        if not reply or not run_id or not _TURNS:
            return False
        taken = queue.take_all() if queue is not None else []
        events = [m.get("payload") or {} for m in taken if m.get("type") == "event"]
        # Undo parking's delivered mark so events reach the writer.
        delivered = self._delivered_live.pop(f"{session_key}:{run_id}", None)
        if not _TURNS.hand_over(self, session_key, run_id,
                                reply={**reply, "events": events, "no_resume": True}):
            for m in taken:
                queue.put_control(m)
            if delivered is not None:
                self._delivered_live[f"{session_key}:{run_id}"] = delivered
            return False
        if queue is not None:
            self.unregister_consumer(session_key, run_id, queue=queue)
        entry.update(run_id=None, queue=None, reply=None, run_text=None)
        return True

    async def claim_cancelled_turn(self, session_key: str) -> dict | None:
        """Take over the turn `park_cancelled_turn` parked, if any.

        Waits for the page's cancel answer, so a Stop still in flight is
        honored; that query has its own bound (ELI-95). The caller then owns
        the returned run, queue, carried messages and waiting replies.
        """
        entry = self._pending_cancel_aborts.get(session_key)
        if entry is None:
            return None
        if not entry["resolved"].is_set():
            entry["claiming"] = True
            await entry["resolved"].wait()
        entry = self._pending_cancel_aborts.pop(session_key, None)
        if entry is None:
            return None
        if entry["task"] is not None:
            entry["task"].cancel()
        # The run stays in its own message; the new reply takes only the messages.
        self._keep_in_own_message(session_key, entry)
        return {"run_id": entry["run_id"], "queue": entry["queue"],
                "carried": list(entry["carried"]),
                "attachments": list(entry["attachments"]),
                "waiting": list(entry["waiting"]),
                "run_text": entry.get("run_text")}

    def seed_run_renderer(self, session_key: str, run_id: str,
                          queue: asyncio.Queue,
                          run_text: dict | None = None) -> None:
        """Bring a new shadow renderer level with an adopted run: its dedup
        baselines plus the events already waiting in `queue`."""
        key = f"{session_key}:{run_id}"
        renderer = self._run_renderers.get(key)
        if renderer is None:
            return
        renderer.seed(run_text)
        # A takeover writer rebuilds from after what earlier replies showed (ELI-147).
        self._run_targets[key]["shown"] = (run_text or {}).get("stream", "")
        for msg in list(getattr(queue, "_queue", ())):
            try:
                renderer.feed(msg.get("payload", {}))
            except Exception as ex:
                pipe_log(f"  shadow renderer seed failed: {ex}")

    async def send_stop(self, session_key: str):
        """Send the stronger /stop command for runtimes where chat.abort is incomplete."""
        try:
            resp = await _run_request(
                self, RUN_SEND,
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

    def _connect_caps(self) -> list:
        """Connect caps. "plugin-approvals" makes the Gateway count this as a
        client that shows approvals, so only the admin turn-writing one sends it."""
        caps = ["tool-events"]
        if _APPROVAL_HOOKS and self.writes_turns and "operator.admin" in self.scopes:
            caps.append(PLUGIN_APPROVAL_CAP)
        return caps

    @property
    def handles_hooks(self) -> bool:
        """Whether this connection hands hook events to the Pipe. Every client
        gets its own copy, so only the live turn writer does (ELI-341)."""
        return self.writes_turns and not self._retired

    async def retire(self):
        """Step down for a newer deploy's connection. Existing consumers and
        waiting sends (ELI-172) are still served; then it closes for good."""
        self._retired = True
        _retire_turn_writer(self.writer_id)
        for writer in list(self._turn_writers.values()):
            writer.release()
        for pump in list(self._turn_pumps.values()):
            pump.queue.put_nowait(("retired", None))
        if self._serving():
            pipe_log(
                f"  retired gateway connection: serving {len(self._consumers)} "
                f"in-flight run(s) and {self._open_send_sections} queued message(s) before closing"
            )
            return
        await self.disconnect()

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
        # A follow waiting on this connection reads again, through a live one.
        self._on_cron_changed()

    # ── Internal ────────────────────────────────────────────────────

    def _mark_event_gap(self) -> None:
        """Put a gap marker into every run queue. The Gateway has no event
        replay; a reply reading it hands over to the turn writer's transcript
        rebuild (ELI-147)."""
        n = 0
        for consumer in list(self._consumers.values()):
            for q in consumer.queues:
                q.put_control({"type": _GAP_MARKER_TYPE})
                n += 1
        if n:
            pipe_log(f"  marked an event gap in {n} run queue(s)")
        self._wake_session_waiters()

    def _mark_gateway_restart(self) -> None:
        """Tell every live reply that its run's Gateway process is gone."""
        for consumer in list(self._consumers.values()):
            for q in consumer.queues:
                q.put_control({"type": _RESTART_MARKER_TYPE})

    def _note_gateway_boot(self, boot_id: str | None) -> None:
        """Count a Gateway restart only when its boot id changes."""
        first = self.connection_epoch <= 1
        if self.gateway_boot_id is not None and boot_id != self.gateway_boot_id:
            self.gateway_restarts += 1
            pipe_log(f"  Gateway restarted (bootId {self.gateway_boot_id[:8]} -> {str(boot_id)[:8]})")
            self._mark_gateway_restart()
        elif not first:
            pipe_log(f"  reconnected to the same Gateway (bootId {str(boot_id)[:8]}); runs continue")
        self.gateway_boot_id = boot_id

    async def _connect_and_start(self):
        """Connect, handshake, and start the event loop task."""
        valves = self._valves()
        host, port, tls = self._pinned_endpoint or _parse_gateway_url(valves.GATEWAY_URL)
        ws_url = f"{'wss' if tls else 'ws'}://{host}:{port}"
        token = valves.GATEWAY_TOKEN

        if not token:
            raise GatewayError(
                "The GATEWAY_TOKEN valve is empty. Set it in Open WebUI under "
                "Admin Panel > Functions > OpenClaw Gateway > valves. The value "
                "is `gateway.auth.token` in your OpenClaw config."
            )

        self._ensure_identity(valves)

        pipe_log(f"Connecting to {ws_url}")
        # Every failure becomes GatewayError: pipe() catches only that.
        try:
            ws = await asyncio.wait_for(
                websockets.connect(ws_url, ping_interval=None,
                                   max_size=GATEWAY_WS_MAX_SIZE),
                timeout=_GATEWAY_CONNECT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            raise GatewayError(
                f"No response from the OpenClaw Gateway at {host}:{port} within "
                f"{_GATEWAY_CONNECT_TIMEOUT_S:.0f}s. Check the GATEWAY_URL valve, "
                "and that the Open WebUI backend can reach that host (Open WebUI "
                "resolves it, not your browser)."
            )
        except (OSError, ValueError, websockets.exceptions.WebSocketException) as ex:
            # WebSocketException is not an OSError (a non-Gateway answering).
            # A refused connect is marked: a reconnecting turn waits through it.
            raise GatewayError(
                f"Could not connect to the OpenClaw Gateway at {host}:{port} "
                f"({type(ex).__name__}: {ex}). Is the Gateway running, and is "
                "the GATEWAY_URL valve pointing at it?"
                + (_container_loopback_hint(host, port, tls) if _refused_connect(ex) else ""),
                error={"code": GATEWAY_REFUSED_CODE} if _refused_connect(ex) else None,
            )

        challenge = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if challenge.get("event") != "connect.challenge":
            raise GatewayError("Bad handshake -- expected connect.challenge")

        c = challenge["payload"]
        signed = _sign_challenge(self._ident, c["nonce"], c["ts"], token_str=token,
                                 scopes=self.scopes)

        auth = dict(token=token)
        if self._device_token:
            auth["deviceToken"] = self._device_token

        await ws.send(json.dumps(dict(
            type="req", id="1", method="connect", params=dict(
                minProtocol=4, maxProtocol=4,
                client=dict(id="webchat", version="1",
                            platform="linux", mode="cli",
                            displayName=_display_name(self._display_name, self._ident)),
                role="operator",
                scopes=self.scopes,
                auth=auth,
                device=signed,
                locale="en-US",
                userAgent="openclaw-owui-pipe/1.0",
                caps=self._connect_caps()
            )
        )))

        resp = await _connect_response(ws, "1")
        if not resp.get("ok"):
            err = resp.get("error")
            pipe_log(f"  connect rejected: {json.dumps(err)[:500]}")
            raise GatewayError(
                _explain_connect_rejection(
                    str((err or {}).get("message", "connect failed")),
                    device_id=(self._ident or {}).get("id"),
                ),
                error=err if isinstance(err, dict) else None,
            )

        hello_ok = resp.get("payload", {})
        if hello_ok.get("auth", {}).get("deviceToken"):
            self._device_token = hello_ok["auth"]["deviceToken"]
            self._save_device_token(valves)
            pipe_log(f"Device token captured: {self._device_token[:20]}...")

        self._ws = ws
        self._endpoint = (host, port, tls)
        self._frame_seq = None  # frame seq counts per socket
        tick_ms = (hello_ok.get("policy") or {}).get("tickIntervalMs")
        if isinstance(tick_ms, (int, float)) and tick_ms > 0:
            self._tick_interval_s = tick_ms / 1000
        self._reconnect_attempt = 0
        self.connection_epoch += 1
        self._thinking_note_shown.clear()
        self.gateway_version = (hello_ok.get("server") or {}).get("version")
        self.gateway_max_image_bytes = _hello_max_image_bytes(hello_ok)
        features = hello_ok.get("features") or {}
        self.gateway_methods = frozenset(features.get("methods") or ())
        self.gateway_events = frozenset(features.get("events") or ())
        self._note_gateway_boot((hello_ok.get("server") or {}).get("bootId"))
        pipe_log(
            f"Connected to Gateway (persistent) [epoch {self.connection_epoch}]"
        )
        # The event loop renews subscriptions: their responses need it reading.
        # Never spawn the event loop here; `_reconnect` calls this from inside it.
        self._subscriptions_stale = not self.requests_only

    def _identity_path(self, valves):
        return os.path.join(_state_dir(getattr(valves, "STATE_DIR", "")), f"{self._identity_name}.json")

    def _device_token_path(self, valves):
        suffix = self._identity_name[len("identity"):]
        return os.path.join(_state_dir(getattr(valves, "STATE_DIR", "")), f"device-token{suffix}.json")

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
        """Load the device identity: state-dir file, then the DEVICE_IDENTITY
        valve, else generate once and persist."""
        if self._ident:
            return
        identity_path = self._identity_path(valves)
        ident = _read_json_file(identity_path)
        if ident and ident.get("id") and ident.get("privateKey"):
            self._ident = ident
            pipe_log(f"Using device identity from {identity_path}")
            self._load_device_token(valves)
            return

        # The valve is the main identity only.
        if self._identity_name == "identity" and getattr(valves, "DEVICE_IDENTITY", ""):
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
        if self._identity_name != "identity":
            return
        print(
            f"DEVICE_IDENTITY={json.dumps(self._ident, separators=(',',':'))}",
            flush=True
        )

    async def _event_loop(self):
        """Read and dispatch WS messages. The ONLY caller of `recv()`; it
        reconnects in place, since a second loop task would race on recv."""
        while not self._stopped:
            try:
                if self._ws is None:
                    await self._reconnect()
                    continue
                if self._subscriptions_stale:
                    self._subscriptions_stale = False
                    spawn_background(self._subscribe_all())
                msg = json.loads(await asyncio.wait_for(self._ws.recv(),
                                                        timeout=2 * self._tick_interval_s))

                # The Gateway silently skips `seq` for dropIfSlow events it
                # dropped on a slow client; treat a skip like a reconnect gap.
                _seq = msg.get("seq")
                if msg.get("type") == "event" and isinstance(_seq, int):
                    if self._frame_seq is not None and _seq != self._frame_seq + 1:
                        pipe_log(f"  frame seq gap: expected {self._frame_seq + 1}, got {_seq}")
                        self._mark_event_gap()
                        if _TURNS:
                            spawn_background(_TURNS.resync(self))
                    self._frame_seq = _seq

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
                                    or "request failed",
                                    error=err if isinstance(err, dict) else None,
                                )
                            )
                        else:
                            fut.set_result(msg.get("payload", {}))
                    continue

                if msg.get("event") == "tick" or msg.get("payload", {}).get("isHeartbeat"):
                    if _TURNS:
                        _TURNS.note_heartbeat(self, msg.get("payload") or {})
                    continue

                # Tool events come as `agent` to the run's starter and as
                # `session.tool` to subscribers (ELI-92); one path, first wins.
                if msg.get("type") == "event" and msg.get("event") == "sessions.changed":
                    self._on_sessions_changed(msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") == "chat.metadata.changed":
                    if _SLASH_PROMPTS_HOOK and self.handles_hooks:
                        _SLASH_PROMPTS_HOOK(msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") == "shutdown":
                    self._on_shutdown(msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") == "question.requested":
                    if _QUESTION_HOOKS and self.handles_hooks:
                        _QUESTION_HOOKS[0](self, msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") == "question.resolved":
                    # Retired connections too: a pre-deploy question lives in
                    # the old module. Each registry pops its own ids.
                    if _QUESTION_HOOKS and self.writes_turns:
                        _QUESTION_HOOKS[1](msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") == "plugin.approval.requested":
                    if _APPROVAL_HOOKS and self.handles_hooks:
                        _APPROVAL_HOOKS[0](self, msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") == "plugin.approval.resolved":
                    # Retired connections too, as for `question.resolved`.
                    if _APPROVAL_HOOKS and self.writes_turns:
                        _APPROVAL_HOOKS[1](msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") == "session.message":
                    self._on_session_message(msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") == "session.tool":
                    # Same payload as an `agent` tool event, so same path.
                    p = msg.get("payload") or {}
                    msg = {"type": "event", "event": "agent", "payload": {
                        k: p[k] for k in ("runId", "sessionKey", "sessionId", "stream",
                                          "data", "seq", "ts") if k in p}}
                if msg.get("type") == "event" and msg.get("event") == "cron":
                    self.note_cron_event(msg.get("payload") or {})
                    self._on_cron_changed()
                    continue
                if msg.get("type") != "event" or \
                   msg.get("event") not in ("agent", "chat"):
                    continue

                await self.dispatch_run_event(msg)

            except asyncio.TimeoutError:
                # No tick for two intervals: a half-open socket.
                pipe_log(f"  tick timeout: no frame for {2 * self._tick_interval_s:.0f}s, "
                         "reconnecting")
                transport = getattr(self._ws, "transport", None)
                if transport is not None:
                    transport.abort()
                self._ws = None
                self._mark_event_gap()
            except websockets.exceptions.ConnectionClosed as e:
                pipe_log(f"WS disconnected -- reconnecting... ({_close_detail(e)})")
                self._ws = None
                self._mark_event_gap()
            except asyncio.CancelledError:
                break
            except Exception as e:
                pipe_log(f"Event loop error: {e!r}")
                self._ws = None
                try:
                    self._mark_event_gap()
                except Exception as ex:  # the loop outlives any one failure
                    pipe_log(f"  event gap marking failed: {ex!r}")

    async def _connect_once_started(self):
        """`_connect_and_start`, retrying while a just-started Gateway refuses
        with reason "startup-sidecars", after the wait it names, up to
        STARTUP_REFUSAL_WAIT_S."""
        deadline = time.monotonic() + STARTUP_REFUSAL_WAIT_S
        told = False
        while True:
            try:
                return await self._connect_and_start()
            except GatewayError as ex:
                retry_ms = _gateway_starting_retry_ms(ex)
                if retry_ms is None or self._stopped or time.monotonic() + retry_ms / 1000 > deadline:
                    raise
                if not told:
                    told = True
                    pipe_log(f"  the Gateway is starting; connecting again as it asks (every {retry_ms} ms)")
                await asyncio.sleep(retry_ms / 1000)

    async def _reconnect(self):
        """Reconnect with backoff, in place inside `_event_loop`. Must never
        spawn another `_event_loop` task."""
        while not self._stopped:
            self._reconnect_attempt += 1
            delay = 1 if self._turns_awaiting_gateway else min(2 ** (self._reconnect_attempt - 1),
                                                               self._max_backoff)
            pipe_log(f"  reconnect in {delay}s (attempt {self._reconnect_attempt})")
            try:
                # A turn waiting for the connection ends the wait (ensure_connected).
                await asyncio.wait_for(self._reconnect_now.wait(), delay)
                pipe_log("  reconnecting now: a message is waiting for the connection")
            except asyncio.TimeoutError:
                pass
            self._reconnect_now.clear()
            if self._stopped:
                self._answer_reconnect_waiters(GatewayError("gateway connection stopped"))
                return
            try:
                await self._connect_once_started()
                self._answer_reconnect_waiters(None)
                return
            except Exception as e:
                pipe_log(f"  reconnect failed: {e}")
                self._ws = None
                self._answer_reconnect_waiters(e)

    def _answer_reconnect_waiters(self, error) -> None:
        """Hand a reconnect attempt's outcome to the turns waiting on it."""
        waiters, self._reconnect_waiters = self._reconnect_waiters, []
        self._reconnect_now.clear()   # asked during this attempt: it is the answer
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(error)


# Protocol sentinels for "no user-visible reply" (docs/tools/subagents.md),
# never shown in a chat.
_SILENT_SENTINELS = {"ANNOUNCE_SKIP", "NO_REPLY", "no_reply"}


# How far back to look for a run's closing message; later runs may follow it.
RUN_TEXT_HISTORY_LIMIT = 60
# For rebuilding a whole run: each tool call is a message of its own.
RUN_HISTORY_LIMIT = 600


def _idempotency_keys(message) -> tuple[str, str]:
    """A message's `idempotencyKey`, top level and under `__openclaw`."""
    if not isinstance(message, dict):
        return "", ""
    meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
    return str(message.get("idempotencyKey") or ""), str(meta.get("idempotencyKey") or "")


def _history_text_for_run(history: dict, run_id: str) -> tuple[bool, str | None]:
    """(found, text) of a run's closing message, by its `cli-assistant:<runId>`
    key. text is None when the run ended silent."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list) or not run_id:
        return False, None
    wanted = {run_id, f"cli-assistant:{run_id}"}
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if not set(_idempotency_keys(message)) & wanted:
            continue
        text = ((_history_item(message) or {}).get("text") or "").strip()
        if not text:
            fallback = message.get("openclawStreamFallback")
            if isinstance(fallback, dict):
                text = str(fallback.get("replacementText") or "").strip()
        return True, (text if text and text not in _SILENT_SENTINELS else None)
    return False, None


def _usage_prompt_tokens(usage) -> int | None:
    """The context a model call filled, as OpenClaw's
    `deriveContextPromptTokens` counts it, or None."""
    if not isinstance(usage, dict):
        return None
    context = usage.get("contextUsage") if isinstance(usage.get("contextUsage"), dict) else {}
    if context.get("state") == "unavailable":
        return None
    if context.get("state") == "available":
        prompt = context.get("promptTokens")
        return prompt if isinstance(prompt, (int, float)) and prompt > 0 else None
    nums = {k: usage.get(k) if isinstance(usage.get(k), (int, float)) else 0
            for k in ("input", "cacheRead", "cacheWrite", "output")}
    prompt = nums["input"] + nums["cacheRead"] + nums["cacheWrite"]
    if prompt > 0:
        return prompt
    total = usage.get("totalTokens", usage.get("total"))
    if isinstance(total, (int, float)) and total - nums["output"] > 0:
        return total - nums["output"]
    return None


def _history_usage_for_run(history: dict, run_id: str) -> dict | None:
    """The `usage` of one run's last model call in `chat.history`, or None."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list) or not run_id:
        return None
    wanted = {run_id, f"cli-assistant:{run_id}"}
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
        ids = {*_idempotency_keys(message), str(meta.get("runId") or ""), str(message.get("runId") or "")}
        if ids & wanted and _usage_prompt_tokens(message.get("usage")):
            return message["usage"]
    return None


def _history_latest_prompt_tokens(history: dict) -> int | None:
    """Prompt tokens of the newest assistant message reporting a usage."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "assistant":
            prompt = _usage_prompt_tokens(message.get("usage"))
            if prompt:
                return prompt
    return None


def _history_renderer(messages: list) -> "_TurnRenderer":
    """A `_TurnRenderer` fed from `chat.history` instead of live events (ELI-88/90/92)."""
    r = _TurnRenderer()
    for payload in _history_events(messages):
        r.feed(payload)
    r.flush()
    return r


def _history_events(messages: list):
    """`chat.history` messages as the live events that wrote them: the one
    transcript parser (ELI-147). Handles both shapes: claude-cli imports
    (`toolcall`/`tool_result` blocks) and the Gateway's own runtime
    (`toolCall` blocks plus `toolResult` messages).

    A claude-cli closer repeats its run's text, so it counts only when the
    run carried none, and in place. Text after a tool call gets the live
    stream's separator (`sawToolUseSinceText`). A `message` tool send's
    duplicate record is shown once (ELI-324)."""
    text = ""                 # what the events so far said
    sent_by_tool: set[str] = set()   # texts the agent's `message` tool already delivered
    run_text = False          # this run carried text before its closer
    tool_since_text = False   # the Gateway's `sawToolUseSinceText`
    reply_ended = False       # a reply of this run ended since the last text
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "toolResult" and message.get("toolCallId"):
            yield _history_tool_result(message["toolCallId"], message.get("toolName"),
                                       message.get("content"), message.get("isError"))
            continue
        if _is_closing_message(message):
            closing = ((_history_item(message) or {}).get("text") or "").strip()
            if not run_text and closing and closing not in _SILENT_SENTINELS:
                delta = _message_boundary_separator(text, closing) + closing
                text, tool_since_text = text + delta, False
                yield {"stream": "assistant", "data": {"delta": delta}}
            run_text = False
            continue
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and block.get("text") and role == "assistant":
                if _is_message_tool_mirror(message):
                    sent_by_tool.add(block["text"].strip())
                elif block["text"].strip() in sent_by_tool:
                    continue
                lead = (_message_boundary_separator(text, block["text"])
                        if tool_since_text or reply_ended else "")
                text, tool_since_text, reply_ended = text + lead + block["text"], False, False
                run_text = run_text or bool(block["text"].strip())
                yield {"stream": "assistant", "data": {"delta": lead + block["text"]}}
            elif kind in ("toolcall", "toolCall") and block.get("id") and role == "assistant":
                tool_since_text = True
                yield {"stream": "tool", "data": {
                    "phase": "start", "name": block.get("name", ""),
                    "toolCallId": block["id"], "args": block.get("arguments") or {}}}
            elif kind == "tool_result" and block.get("tool_use_id"):
                yield _history_tool_result(block["tool_use_id"], block.get("name"),
                                           block.get("content"), block.get("is_error"))
        # More text after a native reply ended answers a steered message (ELI-271).
        reply_ended = reply_ended or bool(_run_closed_by(message))


def _history_tool_result(tool_call_id: str, name, content, is_error) -> dict:
    return {"stream": "tool", "data": {
        "phase": "result", "name": name or "", "toolCallId": tool_call_id,
        "result": "" if content is None else content, "isError": is_error is True}}


def _message_boundary_separator(previous: str, nxt: str) -> str:
    """The newlines the Gateway inserts between two claude-cli assistant
    messages (`missingMessageBoundarySeparator`): enough for a blank line."""
    if not previous:
        return ""
    trailing = len(previous[-2:]) - len(previous[-2:].rstrip("\n"))
    leading = len(nxt[:2]) - len(nxt[:2].lstrip("\n"))
    return "\n" * max(0, 2 - trailing - leading)


def _is_message_tool_mirror(message) -> bool:
    """The transcript copy of a `message` tool send (`<runId>:message-tool:...`)."""
    return any(":message-tool:" in k for k in _idempotency_keys(message))


def _is_closing_message(message) -> bool:
    """A run's closing assistant message (`cli-assistant:<runId>`)."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False
    return any(k.startswith("cli-assistant:") for k in _idempotency_keys(message))


def _run_closed_by(message) -> str | None:
    """The run a message closes, or None: claude-cli's `cli-assistant:<runId>`,
    or a native run's assistant message that did not stop for a tool call."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    key = next((k for k in _idempotency_keys(message) if k.startswith("cli-assistant:")), "")
    if key:
        return key[len("cli-assistant:"):]
    meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
    if meta.get("runId") and message.get("stopReason") not in (None, "toolUse"):
        return str(meta["runId"])
    return None


def _deepest_leaf_id(messages: dict, start_id: str | None) -> str | None:
    """Walk `childrenIds` (newest child) from `start_id` to the leaf.

    OWUI's `history.currentId` is only a view pointer and can point at an
    ancestor; anchoring there would create a sibling variant group.
    """
    node_id = start_id
    seen: set[str] = set()
    for _ in range(10_000):
        if not node_id or node_id in seen:
            break
        seen.add(node_id)
        node = messages.get(node_id) or {}
        children = node.get("childrenIds") or []
        if not children:
            break
        node_id = children[-1]
    return node_id


def _is_forwarded_turn(message) -> bool:
    """A forwarded user turn, which `chat.history` shows as role "assistant"
    since OpenClaw 2026.10.2 (`hasForwardedMessageProvenance`)."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False
    key = next((k for k in _idempotency_keys(message) if k), "")
    prov = message.get("provenance") if isinstance(message.get("provenance"), dict) else {}
    forwarded = ((prov.get("kind"), prov.get("sourceTool")) == ("inter_session", "sessions_send")
                 or (prov.get("kind"), prov.get("sourceTool")) == ("internal_system", "cron"))
    return key.endswith(":user") and len(key) > 5 and forwarded


def _transcript_roles(messages: list) -> list:
    """Put forwarded user turns back in role "user" (GH #24, OpenClaw 2026.10.2+)."""
    return [{**m, "role": "user"} if _is_forwarded_turn(m) else m for m in messages]


def _transcript_commit_order(messages: list) -> list:
    """Move each sent user message to where its run took it in.

    The Gateway stores a message when sent, but claude-cli takes it in later,
    so messages written meanwhile would read as its answer. Its commit time
    (`recordTimestampMs`) is the boundary; its imported copy stays right
    after it. Processed from the last one back.
    """
    out = list(messages)
    for message in reversed([m for m in messages if _committed_after_sent(m) is not None]):
        at = out.index(message)
        committed = _committed_after_sent(message)
        end = at + 1
        while end < len(out) and _commit_ms(out[end]) is not None and (
                _commit_ms(out[end]) < committed
                or (_commit_ms(out[end]) == committed and not _history_user_run_id(out[end]))):
            end += 1
        own = [i for i in range(at + 1, end) if _copy_of(out[i], message)]
        end = own[-1] if own else end
        out.insert(end - 1, out.pop(at))
    return out


def _commit_ms(message) -> float | None:
    """When `message` joined the run: a sent message's commit, else its time."""
    if not isinstance(message, dict):
        return None
    committed = _committed_after_sent(message)
    at = message.get("timestamp")
    return committed if committed is not None else (at if isinstance(at, (int, float)) else None)


def _committed_after_sent(message) -> float | None:
    """A sent message's `recordTimestampMs`, if later than its timestamp."""
    if not _history_user_run_id(message):
        return None
    committed, sent = (message.get("__openclaw") or {}).get("recordTimestampMs"), message.get("timestamp")
    if not isinstance(committed, (int, float)) or not isinstance(sent, (int, float)) or committed <= sent:
        return None
    return committed


def _copy_of(message, sent: dict) -> bool:
    """`message` is the CLI runtime's imported copy of `sent`."""
    if not isinstance(message, dict) or message.get("role") != "user" or _history_user_run_id(message):
        return False
    text = (_history_item(sent) or {}).get("text", "").strip()
    return bool(text) and (_history_item(message) or {}).get("text", "").strip().endswith(text)


def _history_user_run_id(message) -> str | None:
    """The run a `chat.send` user message (`<runId>:user`) started, else None."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    key = next((k for k in _idempotency_keys(message) if k), "")
    return key[:-len(":user")] if key.endswith(":user") and len(key) > 5 else None


# Read-only shim (ELI-100): pre-ELI-100 messages embed a task id as an HTML
# comment, so old messages still open straight into that task's drawer.
_SUBAGENT_TASK_ID_MARKER_RE = re.compile(r"<!--\s*openclaw:taskId=([^\s>]+?)\s*-->")


_gateway_connection: _GatewayConnection | None = None
_gateway_init_lock = asyncio.Lock()

# Stashed on OWUI's stable `open_webui.socket.main` so the next deploy (a new
# module, no teardown hook) can find and retire this one.
_STALE_CONN_ATTR = "_openclaw_gateway_connection_v1"


def _function_module_prefix(module_name: str) -> str:
    """`function_<id>_<hex>` -> `function_<id>`, OWUI 0.12's per-load module
    name, so older loads of this Function are told from the Status Action."""
    head, sep, tail = module_name.rpartition("_")
    if sep and len(tail) == 32 and all(c in "0123456789abcdef" for c in tail):
        return head
    return module_name


def _orphaned_gateway_connections(current) -> list:
    """Live `_GatewayConnection`s of OLDER loads of this Function, found on
    the heap: the stash names only the previous one."""
    import gc
    prefix = _function_module_prefix(__name__)
    if not prefix.startswith("function_"):
        return []
    found = []
    for obj in gc.get_objects():
        cls = type(obj)
        if cls.__name__ != "_GatewayConnection" or obj is current:
            continue
        mod = getattr(cls, "__module__", "") or ""
        if mod == __name__ or _function_module_prefix(mod) != prefix:
            continue
        found.append(obj)
    return found


def _end_replies_of(stale) -> None:
    """A pinned connection's socket is gone while replies still read it. No
    event can reach them now, it has no turn writer to hand them to
    (`_hand_over_run` refuses a retired connection) and the successor leaves a
    run alone while `_served_by_retired` finds its consumer, so each would stay
    generating for good. Each reply gets its run's own end signal and its
    consumer is unregistered: it finishes with what it showed, and this load's
    turn writer writes the rest of the run into the same message (ELI-150)."""
    for consumer in list(getattr(stale, "_consumers", {}).values()):
        end = _event_frame({"sessionKey": consumer.session_key, "runId": consumer.run_id,
                            "stream": "lifecycle", "data": {"phase": "end"}})
        for queue in list(consumer.queues):
            put = getattr(queue, "put_control", queue.put_nowait)
            put(end)
            stale.unregister_consumer(consumer.session_key, consumer.run_id, queue=queue)


def _pin_shut(stale) -> None:
    """Keep an older build's connection from connecting again, but keep its
    open socket read. The connection's own event loop is that socket's only
    reader: it answers the Gateway's pings and carries the runs the connection
    still serves. It ends when the socket drops (a dial is then the only thing
    left to do) or when the connection closes itself once idle
    (`_close_if_retired_and_idle`). Setting `_stopped` here would end the old
    loop after its current `recv()` and leave the socket open and unread, a
    CLOSE-WAIT socket per pin (ELI-369). The old code cannot be changed, so its
    entry points are replaced from this side.

    It admits no new work: `ensure_connected` refuses even with the socket
    open (a retired producer of the old build, such as its channel watcher,
    would otherwise start a run here). When the socket is gone the replies
    still reading it are ended (`_end_replies_of`)."""
    def _refusal() -> GatewayError:
        return GatewayError("gateway connection retired by a newer deploy")

    def _gone() -> GatewayError:
        stale._stopped = True       # a dial was wanted: nothing is left to read
        _end_replies_of(stale)
        answer = getattr(stale, "_answer_reconnect_waiters", None)
        error = _refusal()
        if answer is not None:
            answer(error)
        return error

    async def _ensure_connected(*_a, **_k):
        ws, task = getattr(stale, "_ws", None), getattr(stale, "_event_loop_task", None)
        if ws is not None and task is not None and not task.done():
            raise _refusal()
        raise _gone()

    async def _connect_and_start(*_a, **_k):
        raise _gone()

    async def _reconnect(*_a, **_k):
        _gone()

    stale.ensure_connected = _ensure_connected
    stale._connect_and_start = _connect_and_start
    stale._reconnect = _reconnect


async def _retire_stale_connection(stale) -> None:
    retire = getattr(stale, "retire", None)
    if retire is not None:
        await asyncio.wait_for(retire(), timeout=5)
        if not getattr(stale, "sends_display_name", False):
            # It still serves its runs, but a reconnect by its old code would
            # connect nameless on the shared device and wipe the stored name.
            _pin_shut(stale)
        return
    # Pre-`retire()` build: it serves no run past this, so it closes now; pin
    # its entry points shut so it can't revive.
    stale._stopped = True
    _pin_shut(stale)
    await asyncio.wait_for(stale.disconnect(), timeout=5)


# Earlier loads' connections this load retired. One may still stream a reply
# (`retire`); its run's events reach this load's connection too (ELI-150).
_retired_connections: list = []


def _served_by_retired(payload: dict) -> bool:
    _retired_connections[:] = [c for c in _retired_connections if getattr(c, "_consumers", None)]
    return any(c.consumers_for_event(payload) for c in _retired_connections)


async def _reap_stale_gateway_connection() -> None:
    try:
        import open_webui.socket.main as _owui_socket_main
    except Exception:
        return
    stale_conns = []
    stale = getattr(_owui_socket_main, _STALE_CONN_ATTR, None)
    if stale is not None and stale is not _gateway_connection:
        stale_conns.append(stale)
    try:
        for obj in _orphaned_gateway_connections(_gateway_connection):
            if not any(obj is s for s in stale_conns):
                stale_conns.append(obj)
    except Exception as ex:
        pipe_log(f"  orphan scan failed (non-fatal): {ex}")
    for conn in stale_conns:
        try:
            await _retire_stale_connection(conn)
            _retired_connections.append(conn)
            pipe_log(
                "  reaped stale gateway connection from a previous deploy "
                f"({type(conn).__module__})"
            )
        except Exception as ex:
            pipe_log(f"  failed to reap stale gateway connection (non-fatal): {ex}")


def _remember_gateway_connection(conn: _GatewayConnection) -> None:
    try:
        import open_webui.socket.main as _owui_socket_main
    except Exception:
        return
    setattr(_owui_socket_main, _STALE_CONN_ATTR, conn)


async def _replace_moved_connection(old: _GatewayConnection, make) -> _GatewayConnection:
    """Connect `make()` to the new GATEWAY_URL, then retire `old` (pinned to
    its endpoint). Retiring only after success keeps a mistyped valve from
    breaking the working connection."""
    new = make()
    await new.ensure_connected()
    old._pinned_endpoint = old._endpoint
    try:
        await _retire_stale_connection(old)
        _retired_connections.append(old)
    except Exception as ex:
        pipe_log(f"  failed to retire the connection to the old Gateway endpoint (non-fatal): {ex}")
    pipe_log(f"  GATEWAY_URL changed: now connected to {new._endpoint[0]}:{new._endpoint[1]}"
             f"{' (TLS)' if new._endpoint[2] else ''}")
    return new


async def _get_gateway_connection(valves_getter) -> _GatewayConnection:
    """Get or create the singleton Gateway connection."""
    global _gateway_connection
    conn = _gateway_connection
    if conn is not None and not conn.endpoint_moved():
        await conn.ensure_connected()
        return conn
    async with _gateway_init_lock:
        conn = _gateway_connection
        if conn is None:
            await _reap_stale_gateway_connection()
            conn = _GatewayConnection(valves_getter)
            await conn.ensure_connected()
        elif not conn.endpoint_moved():
            await conn.ensure_connected()
            return conn
        else:
            conn = await _replace_moved_connection(conn, lambda: _GatewayConnection(valves_getter))
        _gateway_connection = conn
        _remember_gateway_connection(conn)
        return conn


# ---------------------------------------------------------------------------
# Owner users (OWNER_USERS valve)
# ---------------------------------------------------------------------------
# Turns sent by the main (operator.admin) connection are the owner's. Run
# requests for non-owner chats go over a RESTRICTED_SCOPES connection; reading
# and rendering stay on the main one, the only writer into OWUI (ELI-100).

_restricted_connection: "_GatewayConnection | None" = None
_restricted_init_lock = asyncio.Lock()


def _new_restricted_connection(valves_getter) -> "_GatewayConnection":
    return _GatewayConnection(valves_getter, writes_turns=False, scopes=RESTRICTED_SCOPES,
                              identity_name="identity-restricted", requests_only=True,
                              display_name=RESTRICTED_DISPLAY_NAME)


async def _get_restricted_connection(valves_getter) -> "_GatewayConnection":
    """The connection for non-owner users' run requests."""
    global _restricted_connection
    async with _restricted_init_lock:
        conn = _restricted_connection
        if conn is None:
            conn = _new_restricted_connection(valves_getter)
            await conn.ensure_connected()
        elif conn.endpoint_moved():
            conn = await _replace_moved_connection(conn, lambda: _new_restricted_connection(valves_getter))
        else:
            await conn.ensure_connected()
        _restricted_connection = conn
        return conn


async def _owui_call(fn, *args):
    out = fn(*args)
    return await out if asyncio.iscoroutine(out) else out


async def _owner_users_setting(conn) -> "str | None":
    """OWNER_USERS, or None when unreadable. The Status Action reads it from
    the Pipe function's saved valves."""
    valves_getter = getattr(conn, "_valves", None)
    raw = getattr(valves_getter() if callable(valves_getter) else None, "OWNER_USERS", None)
    if isinstance(raw, str):
        return raw
    try:
        from open_webui.models.functions import Functions
    except ImportError:
        return ""  # not inside Open WebUI: there is no valve to set
    try:
        found = ""
        for fn in await _owui_call(Functions.get_functions_by_type, "pipe") or []:
            saved = await _owui_call(Functions.get_function_valves_by_id, fn.id) or {}
            if isinstance(saved, dict) and saved.get("OWNER_USERS"):
                found = ",".join(x for x in (found, saved["OWNER_USERS"]) if x)
        return found
    except Exception as ex:
        pipe_log(f"  owner users: could not read the Pipe's valves ({ex!r}); treating as not owner")
        return None


async def _user_email(user_id: str) -> "str | None":
    try:
        from open_webui.models.users import Users
        user = await _owui_call(Users.get_user_by_id, user_id)
        return getattr(user, "email", None) if user else None
    except Exception:
        return None


async def _sender_is_owner(conn, session_key: str) -> bool:
    """Whether a run request for `session_key` may go out as the owner's.
    Undecidable means not the owner's."""
    raw = await _owner_users_setting(conn)
    if raw is None:
        return False
    owners = _parse_owner_users(raw)
    if not owners:
        return True
    if _CHANNEL_KEY_RE.match(session_key or ""):
        # Shared thread: always restricted, so its tool list stays stable.
        return False
    m = _OWUI_KEY_USER.match(session_key or "")
    if not m:
        return False
    user_id = m.group(1)
    if user_id.lower() in owners:
        return True
    return _is_owner_user(owners, user_id, await _user_email(user_id))


# The Gateway reads a message as a command ("/stop", "/model x") when its trimmed
# text starts with this (its chat.send handler).
GATEWAY_COMMAND_PREFIX = "/"


def _is_gateway_command(message: str) -> bool:
    """Whether the Gateway reads `message` as a command rather than a prompt."""
    return message.lstrip().startswith(GATEWAY_COMMAND_PREFIX)


# The commands that start the session afresh (the Gateway's RESET_COMMAND_RE).
GATEWAY_RESET_COMMAND_RE = re.compile(r"^/(new|reset)(?:\s|$)", re.IGNORECASE)


def _is_gateway_reset(message: str) -> bool:
    """Whether `message` is a command that gives the session a fresh start."""
    return bool(GATEWAY_RESET_COMMAND_RE.match(message.strip()))


def _close_frame_text(frame) -> str:
    return f"{frame.code} {frame.reason!r}" if frame is not None else "none"


def _close_detail(e) -> str:
    """Both close frames of a closed connection (ELI-369). websockets reports
    code 1006 whenever no frame came back, also when this side failed the
    connection itself (1009 a frame over max_size, 1007 a text frame not
    UTF-8): only the frame it sent names that. The code itself is left out:
    it is the received frame's, or 1006 when none came, and websockets 13+
    deprecates reading it."""
    return (f"sent {_close_frame_text(getattr(e, 'sent', None))}, "
            f"received {_close_frame_text(getattr(e, 'rcvd', None))}")


def _non_owner_send_params(params: dict) -> dict:
    """A chat.send for the restricted connection: admin-only fields dropped,
    the metadata receipt prepended to the message as the Gateway would. A
    command ("/stop") stays as typed, since commands are read from raw text."""
    dropped = [f for f in ADMIN_ONLY_SEND_FIELDS if f in params]
    if not dropped:
        return params
    out = {k: v for k, v in params.items() if k not in dropped}
    receipt = params.get("systemProvenanceReceipt")
    message = out.get("message") or ""
    moved = isinstance(receipt, str) and bool(receipt) and not _is_gateway_command(message)
    if moved:
        out["message"] = "\n\n".join(p for p in (receipt, message) if p)
    pipe_log(f"  non-owner chat.send: left out admin-only fields {dropped}"
             + ("; metadata block moved into the message" if moved else ""))
    return out


async def _run_request(conn, method: str, params: dict, *, timeout: float = 10,
                       owner: "bool | None" = None, wait: bool = True,
                       started: "float | None" = None):
    """Send a run request on `conn` for an owner, else on the restricted
    connection. `owner` overrides the session-key decision; `started`
    (monotonic) is an earlier send's, whose wait budget this one shares."""
    if owner is None:
        owner = await _sender_is_owner(conn, params.get("sessionKey") or params.get("key") or "")
    target = conn if owner else await _get_restricted_connection(conn._valves)
    if not owner and method == RUN_SEND:
        params = _non_owner_send_params(params)
    if not wait:
        await target._ws.send(json.dumps(dict(type="req", id="abort" if method == RUN_ABORT
                                              else f"run-{uuid.uuid4().hex[:8]}",
                                              method=method, params=params)))
        return None
    return await _send_when_prepared(target, method, params, timeout=timeout, started=started)


async def _send_when_prepared(conn, method: str, params: dict, *, timeout: float = 10,
                              started: "float | None" = None):
    """`conn.send_request`, resent while the Gateway is still preparing the
    agent (up to STARTUP_REFUSAL_WAIT_S), or on any other retryable refusal
    (up to RETRYABLE_REFUSAL_WAIT_S): 2026.10.5 refuses sessions.patch that
    way right after preparation, naming no reason."""
    started = time.monotonic() if started is None else started
    deadline = started + STARTUP_REFUSAL_WAIT_S
    told = set()
    while True:
        try:
            return await conn.send_request(method, params, timeout=timeout)
        except GatewayError as ex:
            retry_ms, why = _startup_refusal_retry_ms(ex), "preparing"
            if retry_ms is None:
                retry_ms, why = _retryable_refusal_retry_ms(ex), "retryable"
                if retry_ms is not None and "retryable-since" not in told:
                    told.add("retryable-since")
                    retryable_deadline = time.monotonic() + RETRYABLE_REFUSAL_WAIT_S
            if retry_ms is None or time.monotonic() + retry_ms / 1000 > (
                    deadline if why == "preparing" else min(deadline, retryable_deadline)):
                raise
            if why not in told:
                told.add(why)
                pipe_log(f"  {method}: the Gateway is still preparing the agent after its start; "
                         f"sending again as it asks (every {retry_ms} ms, up to {STARTUP_REFUSAL_WAIT_S} s)"
                         if why == "preparing" else
                         f"  {method}: refused as retryable ({ex}); sending again in {retry_ms} ms, "
                         f"up to {RETRYABLE_REFUSAL_WAIT_S} s")
            await asyncio.sleep(retry_ms / 1000)


# A just-started Gateway (2026.10.5+) refuses runs with this details.code
# until the agent's database is prepared. The refusal is not cached by
# idempotency key, so the same request is resent; no event marks the end.
STARTUP_REFUSAL_CODE = "agent-database-inspection-pending"
STARTUP_REFUSAL_WAIT_S = 300
RETRYABLE_REFUSAL_WAIT_S = 30
# Wait for a restarting Gateway that refuses connections.
GATEWAY_DOWN_WAIT_S = 120
GATEWAY_REFUSED_CODE = "connection-refused"


async def _connect_response(ws, req_id: str, timeout: float = 10) -> dict:
    """The first `res` frame with our connect id; other frames are skipped."""
    deadline = time.monotonic() + timeout
    while True:
        frame = json.loads(await asyncio.wait_for(ws.recv(), max(0, deadline - time.monotonic())))
        if frame.get("type") == "res" and frame.get("id") == req_id:
            return frame
        pipe_log(f"  frame before the connect response, skipped: type={frame.get('type')!r} "
                 f"event={frame.get('event')!r} id={frame.get('id')!r}")


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _in_container() -> bool:
    """Open WebUI runs in a Docker or Podman container."""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def _container_loopback_hint(host: str, port: int, tls: bool = False) -> str:
    """A hint for a refused loopback address inside a container, where
    localhost is the container itself."""
    if host not in _LOOPBACK_HOSTS and not host.startswith("127."):
        return ""
    if not _in_container():
        return ""
    where = (" Open WebUI runs in a container here. Unless it uses host networking "
             f"(--network host, where this refusal means nothing listens on {host}:{port}), "
             f"{host} inside it is the container itself, not the machine the Gateway runs on.")
    if tls:
        # host.docker.internal would not match the proxy's certificate.
        return (where + " A wss:// address names the TLS proxy: point GATEWAY_URL at a "
                "host name its certificate covers and that the container can reach, with "
                "the proxy listening there.")
    return (where + f" Point GATEWAY_URL at the host (host.docker.internal:{port}, started with "
            "--add-host=host.docker.internal:host-gateway, or the host's LAN address) "
            "and have the Gateway listen there: gateway.bind lan in the OpenClaw config "
            "listens on every interface, so firewall the port on a host the internet can "
            "reach. A Gateway behind tailscale serve keeps its loopback bind: set "
            "GATEWAY_URL to its wss:// address instead.")


def _refused_connect(ex) -> bool:
    """Every resolved address refused the connect. asyncio merges differing
    errors into one "Multiple exceptions:" OSError."""
    if isinstance(ex, ConnectionRefusedError):
        return True
    text = str(ex)
    return (isinstance(ex, OSError) and text.startswith("Multiple exceptions:")
            and text.count("[Errno") == text.count(f"[Errno {errno.ECONNREFUSED}]"))


def _gateway_refused(ex) -> bool:
    """A connect the Gateway's host refused: nothing listens on the port."""
    return isinstance(ex, GatewayError) and ex.error.get("code") == GATEWAY_REFUSED_CODE


def _gateway_starting_retry_ms(ex: GatewayError) -> int | None:
    """Retry wait for a "startup-sidecars" connect refusal, bounded like
    OpenClaw's `resolveGatewayStartupRetryAfterMs`."""
    err = getattr(ex, "error", None) or {}
    details = err.get("details") if isinstance(err.get("details"), dict) else {}
    if err.get("code") != "UNAVAILABLE" or err.get("retryable") is not True \
            or details.get("reason") != "startup-sidecars":
        return None
    retry = err.get("retryAfterMs")
    retry = int(retry) if isinstance(retry, (int, float)) else 500
    return min(max(retry, 100), 2000)


def _startup_refusal_retry_ms(ex: GatewayError) -> int | None:
    """Retry wait for a STARTUP_REFUSAL_CODE refusal, else None."""
    err = getattr(ex, "error", None) or {}
    details = err.get("details") if isinstance(err.get("details"), dict) else {}
    if not err.get("retryable") or details.get("code") != STARTUP_REFUSAL_CODE:
        return None
    retry = err.get("retryAfterMs")
    return int(retry) if isinstance(retry, (int, float)) and retry > 0 else 250


def _retryable_refusal_retry_ms(ex: GatewayError) -> int | None:
    """Retry wait for any other UNAVAILABLE + retryable refusal, else None."""
    err = getattr(ex, "error", None) or {}
    if err.get("code") != "UNAVAILABLE" or err.get("retryable") is not True:
        return None
    retry = err.get("retryAfterMs")
    return int(retry) if isinstance(retry, (int, float)) and retry > 0 else 250


# ELI-359: OpenClaw 2026.10.5 commits a `chat.send` to the session's pending
# inputs, then refuses to publish it when another write lands on the agent
# database in between (session-accessor-BHpQ2Hsr.mjs:542, e.g. the startup
# sub-agent recovery). UNAVAILABLE, not retryable, and the input never runs:
# its key is refused from then on ("Pending input ownership ended; submit a new
# turn"), a new key runs once. Checked with a forced fault on 2026.10.5.
UNPUBLISHED_INPUT_ERROR = "Pending input authority changed before publication"
# The Gateway's error code for a request it could not serve now.
UNAVAILABLE_CODE = "UNAVAILABLE"


def is_unpublished_input(ex: Exception) -> bool:
    """A `chat.send` refused as committed but never published (ELI-359)."""
    err = getattr(ex, "error", None) or {}
    return (isinstance(ex, GatewayError) and err.get("code") == UNAVAILABLE_CODE
            and UNPUBLISHED_INPUT_ERROR in str(err.get("message") or ex))


# `sessions.list` returns at most 100 rows without a `limit`, and has no
# filter by key: `search` is a substring match.
_SESSIONS_PAGE = 500          # rows asked for per page of a full read
_SESSION_READ_LIMIT = 10      # rows asked for when one session is named


async def _list_all_sessions(conn, *, timeout: float = 30, **filters) -> dict:
    """Every `sessions.list` row for `filters`, read page by page."""
    rows: list = []
    while True:
        resp = await conn.send_request(
            "sessions.list", {**filters, "limit": _SESSIONS_PAGE, "offset": len(rows)}, timeout=timeout)
        page = resp.get("sessions") or []
        rows.extend(page)
        total = resp.get("totalCount")
        if not page or (total is not None and len(rows) >= total) or (total is None and len(page) < _SESSIONS_PAGE):
            return {"sessions": rows, "totalCount": total if total is not None else len(rows)}
