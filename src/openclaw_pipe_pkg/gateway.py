# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------

GATEWAY_SCOPES = ["operator.admin", "operator.read", "operator.write"]
# A connection for users not in OWNER_USERS. Without operator.admin the
# Gateway does not record its turns as the owner's, and the agent withholds
# owner-only tools from them. read: history, sessions, models, usage and the
# event subscriptions. write: send, abort and the model override.
RESTRICTED_SCOPES = ["operator.read", "operator.write"]


def _parse_owner_users(raw: str) -> frozenset:
    """OWNER_USERS as a set of lowercase user ids and emails."""
    return frozenset(p.strip().lower() for p in (raw or "").split(",") if p.strip())


def _is_owner_user(owners: frozenset, user_id, email) -> bool:
    """An empty list makes everyone an owner (the behaviour before the valve)."""
    if not owners:
        return True
    return any(v and str(v).strip().lower() in owners for v in (user_id, email))


# The requests that start, steer or end a run. The Gateway records a turn as
# the owner's when the connection that sends it holds operator.admin, so these
# go out only through `_run_request`, which picks the connection.
RUN_SEND, RUN_ABORT, RUN_PATCH = "chat.send", "chat.abort", "sessions.patch"
# chat.send fields the Gateway accepts only from an operator.admin connection
# ("system provenance fields require admin scope"). A send over the restricted
# connection leaves them out; the conversation metadata block goes in the
# message text instead (see `_non_owner_send_params`).
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


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class GatewayError(Exception):
    """Raised when the Gateway WebSocket handshake fails, or a request is
    refused. `error` is the refusal's error shape ({code, message, details,
    retryable, retryAfterMs}), when the Gateway sent one."""

    def __init__(self, message: str = "", error: dict | None = None):
        super().__init__(message)
        self.error = error or {}


# How long to wait for the Gateway's TCP/WS accept before giving up. Mirrors
# websockets' own open_timeout default; kept explicit so the bound is visible
# and tunable in one place rather than inherited silently.
_GATEWAY_CONNECT_TIMEOUT_S = 10.0

# Largest Gateway frame the socket accepts. websockets defaults to 1 MiB and
# answers a bigger frame by closing the WHOLE connection (1009, "message too
# big"), dropping every session's events: a long `chat.history` is several
# MB, and one large tool result in an agent or `session.message` event is
# enough. Bounded, not None, so a
# runaway frame still can't take the Open WebUI process's memory with it.
GATEWAY_WS_MAX_SIZE = 64 * 1024 * 1024


def _explain_connect_rejection(message: str, *, device_id: str | None = None) -> str:
    """Turn the Gateway's terse connect rejection into something actionable.

    Device approval is a mandatory step of every first install, not an
    exceptional case, yet the raw response is just "pairing required" -- and the
    command that fixes it appears nowhere in the product, only in the README.
    The device id the operator has to match is already in hand here.
    """
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


def _parse_gateway_url(raw: str) -> tuple[str, int]:
    """Parse the GATEWAY_URL valve into (host, port).

    The valve wants a bare ``host:port`` -- but it renders as a text box that
    looks exactly like a URL field, so pasting ``http://host:18789`` is the
    single most likely new-user mistake. A naive ``rsplit(":", 1)`` handles that
    input catastrophically quietly:

      ``http://gw:18789``  -> host ``http://gw``  -> websockets parses the
                              hostname as literally ``http`` on port 80
      ``http://gw``        -> ``int("//gw")`` -> a raw ValueError that escapes
                              pipe()'s ``except GatewayError`` and surfaces as
                              an unattributed error naming no valve

    So: accept a scheme and strip it (being liberal costs three lines and kills
    the whole failure class), and raise a GatewayError naming the valve for
    anything still unparseable, so it lands in the one handler that prints
    nicely into the chat.
    """
    value = (raw or "").strip()
    if not value:
        raise GatewayError(
            "The GATEWAY_URL valve is empty. Set it to your OpenClaw Gateway "
            "as host:port, for example localhost:18789."
        )
    if "://" in value:
        scheme, _, value = value.partition("://")
        if scheme.lower() not in ("ws", "wss", "http", "https"):
            raise GatewayError(
                f"GATEWAY_URL has an unsupported scheme '{scheme}://'. Use a "
                "bare host:port, for example localhost:18789."
            )
    # Drop any path/query a pasted URL dragged along ("host:8443/" -> "host:8443").
    value = value.split("/", 1)[0].split("?", 1)[0]

    if value.startswith("["):
        # Bracketed IPv6, "[::1]:18789". Split on the closing bracket, not the
        # last colon, and keep the brackets -- websockets needs them in the URL.
        addr, sep, rest = value.partition("]")
        if not sep:
            raise GatewayError(
                f"GATEWAY_URL {raw!r} is missing a closing ']' on the IPv6 "
                "address. Expected [address]:port, for example [::1]:18789."
            )
        inner = addr[1:]
        # Brackets mean IPv6 to every URL parser. If the contents are not an
        # IPv6 literal, websockets' urlsplit().hostname raises a bare ValueError
        # -- which is neither OSError nor WebSocketException, so it escapes the
        # connect handler AND pipe()'s except GatewayError. Reject it here.
        # This is reachable by following our own advice: the bare-IPv6 branch
        # below says "wrap it in brackets", and a user who wraps a *hostname*
        # lands exactly on this input.
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
        port_str = rest[1:] if rest.startswith(":") else (rest or "18789")
    elif value.count(":") > 1:
        # More than one colon and no brackets. If it parses as IPv6, say so;
        # otherwise it is something like "host:80:90" or a pasted URL with
        # userinfo, and calling that "an IPv6 address" would be wrong.
        try:
            ipaddress.IPv6Address(value)
        except ValueError:
            raise GatewayError(
                f"GATEWAY_URL {raw!r} has more than one ':' and is not an IPv6 "
                "address, so the port is ambiguous. Expected host:port, for "
                "example localhost:18789."
            )
        # A bare IPv6 literal. rpartition(":") would read "::1" as host ":" on
        # port 1 -- silently wrong on both counts, which is the whole failure
        # class this function exists to remove.
        raise GatewayError(
            f"GATEWAY_URL {raw!r} is an IPv6 address. Wrap it in brackets so "
            "the port is unambiguous, for example [::1]:18789."
        )
    else:
        host, sep, port_str = value.rpartition(":")
        if not sep:
            host, port_str = value, "18789"

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
    return host, port


# Budget for reading back an interrupted turn's tail. `chat.history` honors
# maxChars per message (schema max 500000) up to a 128 KiB per-message byte
# cap; 50000 chars stays under that cap even for 2-byte scripts like Hebrew.
RECOVERY_HISTORY_LIMIT = 40
RECOVERY_HISTORY_MAX_CHARS = 50000


def _event_frame(payload: dict) -> dict:
    """A routed event payload as the receive loop hands frames to an inline
    reply, which reads `msg["payload"]`: a `chat` event has a state and no
    stream, anything else (including a rebuilt session.tool copy) is `agent`."""
    event = "chat" if "state" in payload and "stream" not in payload else "agent"
    return {"type": "event", "event": event, "payload": payload}


def _history_item(message) -> dict | None:
    """Flatten one `chat.history` message to {role, text}. Only `text` blocks
    count: tool calls, tool results and thinking blocks are not part of the
    visible answer."""
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
    """Return the full assistant text saved after this user message, read from
    a `chat.history` response."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list):
        return None
    return _recovery_text_from_items([_history_item(m) for m in messages], user_text)


def _recovery_user_text_matches(item_text: str, user_text: str) -> bool:
    """True if a stored user message is the one this turn sent.

    The transcript can hold it verbatim or behind the Gateway's metadata
    envelope ("Conversation info (untrusted metadata): ...", then a blank
    line, then the text).
    """
    return item_text == user_text or item_text.endswith("\n" + user_text)


def _recovery_text_from_items(items, user_text: str) -> str | None:
    """The turn's final assistant text: the LAST non-empty assistant item after
    the last user item matching `user_text`, stopping at the next user message.

    Last, not first: a tool-using turn stores several assistant messages. On
    the claude-cli runtime the closing one repeats the whole turn's text
    (preambles included); on the embedded runtime it is the final answer. The
    first one is only the opening preamble ("I'll look at ...").
    """
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
    """Read back the assistant text of a turn whose live stream was lost,
    from `chat.history`, which returns the message whole (ELI-74)."""
    try:
        history = await conn.session_history(session_key)
    except Exception as ex:
        pipe_log(f"  history recovery failed: {ex}")
        return None
    text = _history_recovery_text(history, user_text)
    if not text:
        pipe_log("  history recovery found no reply")
    return text


def _owui_session_key(agent_id: str, user_id: str, chat_id: str) -> str:
    return f"agent:{agent_id}:openwebui-{user_id}-{chat_id}"


def _session_thinking_ladder(desc: dict) -> list | None:
    """Pull the thinking levels a session's CURRENT model supports out of a
    `sessions.describe` response.

    `models.list` cannot answer this: it carries a `reasoning` boolean and
    nothing else. `agents.list` does carry a ladder, but the agent's PRIMARY
    model's, which is wrong the moment a session overrides the model. Only
    describe reflects what is actually resolved for this session right now.

    `thinkingLevels` is authoritative: the gateway defines it as the raw
    `{id, label}` objects (`resolveGatewaySessionThinkingProjectionInternal`
    -> `thinkingLevels: metadata.levels`), keyed by the canonical lowercase
    ids (`off`, `minimal`, ...) that LEVEL_RANKS and clamp_to_ladder match
    against. `thinkingOptions` is display-only -- the SAME gateway function
    derives it as `metadata.levels.map(level => level.label)`, i.e. human
    labels ("Off", "Extra High", ...), not ids. Matching a requested level
    against labels silently breaks clamping (nothing in LEVEL_RANKS looks
    like a label), so `thinkingOptions` must never be used for the ladder:
    an unsupported level would fall through the clamp and reach the
    gateway's own hard validation error on every turn (ELI-85). Kept only as a last-resort fallback in case a future gateway build ever
    stops emitting `thinkingLevels` -- an imperfect ladder beats none.

    Returns None (not []) when the field is absent, because "no ladder known"
    and "this model supports nothing" have to lead to different behaviour: the
    first passes the request through to the gateway, the second would clamp
    every request away.
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
    """`provider/model` for a `sessions.describe` row, or None.

    Formatted to match the Gateway's own rejection text verbatim
    ("...is not supported for claude-cli/claude-opus-5.") so a ladder learned
    from a rejection and one observed from describe key the same entry.
    """
    row = row or {}
    provider = str(row.get("modelProvider") or "").strip()
    model = str(row.get("model") or "").strip()
    if not provider or not model:
        return None
    return f"{provider}/{model}"


def _ladder_cache_path(state_dir: str = "") -> str:
    return os.path.join(_state_dir(state_dir), LADDER_CACHE_NAME)


# Guards the ladder cache's read-merge-write sequence below. Pipe and Status
# Action requests can land in different threads of the same OWUI process, and
# without this a slower worker's read-merge-write can finish after a faster
# one's and silently drop the faster worker's update. A unique temp file (see
# _write_json_file) only makes each individual write atomic; it does nothing
# for two workers racing on the read-then-write in between.
_LADDER_CACHE_LOCK = threading.Lock()


def _record_thinking_ladder(levels, model_key=None, authoritative=False,
                             state_dir: str = "") -> None:
    """Fold a ladder we just saw into the cache, both as a union and per model.

    Two different consumers, two different needs, one file:

    * `levels` (union across every model seen) is what the Thinking filter
      offers, because Open WebUI builds a valve dropdown once from the class
      and cannot vary it per selected model.
    * `models[<provider/model>]` is what the SEND path clamps against, and it
      must be exact: clamping against the union lets `high` reach a model
      whose only level is `off`.

    `authoritative` marks a ladder the Gateway stated itself by rejecting a
    send. A merely observed one (from `sessions.describe`) must never overwrite
    it: describe resolves the ladder with no model catalog in scope and so
    reports the generic base profile for catalog-gated models.

    Best-effort by design: a failed write costs a stale dropdown and one more
    rejected turn, and must never affect the message being sent.
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
                # Keep the stated truth; a describe-sourced ladder is not
                # evidence against it.
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
            return
        payload = {"levels": merged, "updated": int(time.time())}
        if models:
            payload["models"] = models
        if not _write_json_file(path, payload):
            return
    if merged != have:
        pipe_log(f"thinking ladder cache updated: {merged}")
    if model_changed:
        pipe_log(
            f"thinking ladder for {model_key}: {known} "
            f"({'stated by gateway' if authoritative else 'observed'})"
        )


def _read_model_thinking_ladder(model_key, state_dir: str = ""):
    """The exact ladder for one model, or None if we have not learned it.

    None and [] mean different things here and the caller relies on it: None is
    "unknown, pass the request through", [] would be "supports nothing", which
    no model is. Entries that somehow persisted empty are treated as unknown.
    """
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
    # Only when actually chosen. chat.send reads an absent `thinking` as "use
    # whatever the agent is configured for", which is a different instruction
    # from any value we could send, "off" very much included.
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


# Provider namespaces that name a *serving runtime* rather than the model's
# canonical API vendor. A model keyed under one of these (e.g.
# "claude-cli/claude-opus-5") resolves, on sessions.patch, to the model's
# canonical provider ("anthropic/claude-opus-5") -- the gateway always reports
# the vendor, not the runtime. Without treating these as equal, any model
# keyed by its runtime can never pass the post-patch equality check and the
# pipe wrongly aborts with "model override did not apply" (opus-5 was the only
# such model; the claude-cli/ key was chosen deliberately to avoid a separate
# compaction bug, so the fix belongs here, not in gateway config).
_RUNTIME_PROVIDER_NAMESPACES = {"claude-cli", "google-gemini-cli", "codex-cli"}


def _model_patch_matches(model_override: str | None, patch_resp: dict) -> bool:
    if model_override is None:
        return True
    if _resolved_model_key(patch_resp) == model_override:
        return True
    # Runtime-namespaced key (claude-cli/<id>) vs canonical vendor readback
    # (anthropic/<id>): same model, different provider label -- treat as applied.
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
    """True if an OWUI message's `content` is a multimodal block list
    that includes at least one image_url block. `content` is a
    plain string for ordinary text-only messages -- only the list shape
    (sent when a user attaches an image) can carry one, so this returns
    False immediately for the common string case."""
    if not isinstance(raw_content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "image_url"
        for block in raw_content
    )


_DATA_URL_RE = re.compile(r"^data:([^;,]*)(?:;[^,]*)?,(.*)$", re.DOTALL)


def _extract_image_attachments(raw_content) -> list:
    """Pull inline images out of an OWUI multimodal `content` block list
    and shape them for the gateway's `chat.send` `attachments` param:
    `{type, mimeType, fileName, content}` with `content` as base64.

    OWUI sends attached/pasted images as `image_url` blocks whose `url` is
    a `data:<mime>;base64,<data>` URI -- there is no plain-HTTP(S)
    `image_url` case to support here, so anything else is skipped rather
    than guessed at.
    """
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
# Queue item `_GatewayConnection._note_gateway_boot` puts in every run queue
# when a reconnect lands on a new Gateway process: the run may be gone, and
# its owner (DESIGN 2.7) is decided from its row, not by the reply.
_RESTART_MARKER_TYPE = "_pipe_restart"
# Queue item carrying a session row to the inline replies of that session:
# `{"type", "row", "resync"}`. A run's own settle row (hasActiveRun false,
# lastRunId = the run) ends its reply even when its final event was lost;
# `resync` marks a row read after a reconnect or a frame gap, where an idle
# session means every admitted run is over; a `queued` row tells a reply
# waiting for its first event why.
# `read_at` (time.monotonic) is when the row was read: a reply trusts an idle
# row only when it was read after its run was admitted.
_ROW_MARKER_TYPE = "_pipe_row"


# How much of the already-received text anchors a cumulative text that does not
# extend it by prefix. Long enough that a match means "this text contains what
# we have", never a coincidence of a short phrase ("OK.", "Done.").
_WHOLE_TEXT_ANCHOR_CHARS = 200
_MIN_WHOLE_TEXT_ANCHOR_CHARS = 64


def _item_delta_text(item_text: str, last_item_text: str, assistant_stream_text: str) -> str:
    """Compute the new text (if any) an `item` event adds beyond what's
    already been shown, either by a prior `item` event or by the regular
    `assistant` delta stream.

    Some item events of kind "message"/"output" echo the *complete* final
    assistant text (not just a genuine tool-preamble) -- without this check
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
    # Not a prefix extension. If the item still contains the tail of what was
    # already received, it is the cumulative text with something missing
    # from the middle or the start (events lost while the Gateway socket was
    # reconnecting): only what follows that tail is new; returning the whole
    # item would post a second full copy of the reply. The missing piece comes back with the turn writer's transcript rebuild
    # (ELI-147), never appended here.
    # Shorter anchors too: the hole may sit inside the longest one.
    for n in (_WHOLE_TEXT_ANCHOR_CHARS, 128, _MIN_WHOLE_TEXT_ANCHOR_CHARS):
        anchor = baseline[-n:]
        if len(anchor) < _MIN_WHOLE_TEXT_ANCHOR_CHARS:
            break
        pos = item_text.rfind(anchor)
        if pos >= 0:
            return item_text[pos + len(anchor):]
    return item_text


def _suppress_already_shown(delta: str, visible_message_text: str) -> str:
    """Final safety net against re-yielding text that's already been shown.

    `_item_delta_text`'s dedup baseline (`assistant_stream_text` or
    `last_item_text`) is a separate accumulator from `visible_message_text`
    and can drift from it -- e.g. a provider's final catch-all event races
    an idle-timeout recovery cycle, or arrives after other text (tool
    blocks, recovered previews) has been recorded into
    `visible_message_text` without also updating `assistant_stream_text`.
    When that happens, `_item_delta_text`'s prefix check fails to match and
    it falls through to returning the *entire* cumulative text unchanged,
    which then gets appended a second time with no separator.

    `visible_message_text` is the true, complete record of everything
    already recorded for this turn, so checking whether `delta` is already
    its tail catches this regardless of which upstream baseline caused the
    false negative -- without touching the primary dedup logic (so a
    genuine partial catch-up, which is never already at the tail, still
    yields normally).
    """
    if delta and visible_message_text.endswith(delta):
        return ""
    return delta


def _attr(value: str) -> str:
    """Escape a string for use as an HTML attribute value in a tool card.

    `html.escape` alone is NOT enough here. OWUI's frontend parses these
    `<details>` attributes with a non-dotAll regex (`/(\\w+)="(.*?)"/g`, chunk
    `_UcCb4Vt.js`), so `.` never matches a newline: any attribute value
    containing a literal newline fails to match and is silently DROPPED -- the
    tool card then renders with INPUT and no OUTPUT at all, with no error.

    `arguments` is always `json.dumps`-ed, so its newlines are already the
    two-char `\\n` escape; a multi-line `result` is passed through raw.

    Encoding the newline as the numeric character reference `&#10;` keeps the
    attribute on one physical line for that regex while still decoding back to
    a real newline when the browser parses the attribute -- the same path that
    already turns `&quot;` into `"` in the rendered card.
    """
    return (
        html.escape(value)
        .replace("\r\n", "&#10;")
        .replace("\n", "&#10;")
        .replace("\r", "&#10;")
    )


# `output_index` for an item that goes at the end of Open WebUI's output
# list. Both of its reducers append an `output_item.added` whose index is at
# or past the end; without one, the backend appends but the chat tab splices
# the item in before the last one (ELI-122). The pipe cannot see that list
# (a tag in the text can add items to it), so it names no exact length.
_OUTPUT_APPEND_INDEX = 1 << 30


def _tool_call_started_event(name: str, tool_call_id: str, args_str: str) -> dict:
    """Announce a tool call as a Responses-API output item (live spinner).

    Yielding a *dict* from the pipe is a supported protocol switch: OWUI
    serializes it verbatim as an SSE `data:` line (`functions.py::process_line`)
    instead of wrapping it in a chat chunk, and any event whose `type` starts
    with `response.` is folded into the backend's OWN output list by
    `handle_responses_streaming_event` (`middleware.py`).

    That matters because the backend's output list is what feeds the live
    `chat:completion` emissions, the final done event AND the DB write -- so a
    tool call announced this way is mutable, unlike yielded markdown, which is
    append-only for the rest of the turn. The frontend renders a `function_call` with no matching
    `function_call_output` as a spinner (`structuredOutput.ts::buildToolCallToken`).

    Ordering is by arrival: the item goes at the end of the output list, after
    the text streamed before it. It says so with `output_index`
    (`_OUTPUT_APPEND_INDEX`): the backend appends an item without one, but
    the chat tab's reducer (`structuredOutput.ts::applyResponseStreamEvent`)
    splices it in BEFORE the last item, and the live card would show above
    the text written before the call until a reload (ELI-122).

    The tool call id goes in `id`, and `call_id` is deliberately ABSENT.
    Open WebUI replaces an added item that shares its `id` or its `call_id`
    with one it holds:

        existing_index = next(idx for idx, existing in enumerate(new_output)
            if (item['id'] and existing['id'] == item['id'])
            or (item['call_id'] and existing['call_id'] == item['call_id']))
        if existing_index is not None: new_output[existing_index] = item

    -- so the `function_call_output` we send next, which must share the call
    id to pair, would match this item on `call_id` and REPLACE it. The card
    is built from the `function_call` item alone (`structuredOutput.ts`
    reads `call_id ?? id` and looks the output up in a by-call_id map), so
    losing it means no card at all.

    Carrying the id in `id` only keeps both items distinct for the dedup
    (`<tid>` vs `fco_<tid>`, and neither has a `call_id` the other shares)
    while every consumer that matters reads `call_id or id`:
    `structuredOutput.ts` for the card, `middleware.py`'s post-stream sweep
    for the "already has a result, don't execute it" guard, and
    `pause_for_tool_approval`, which even backfills `call_id` from `id`.
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


def _tool_call_result_event(tool_call_id: str, result_str: str) -> dict:
    """Complete a tool call announced by `_tool_call_started_event`.

    A `function_call_output` whose `call_id` matches the started item's id is
    what flips the card to done and reveals the Output section. If we never
    send one (run cancelled, pipe torn down), the backend still marks every
    leftover `in_progress` item completed before the final event, so a card
    can't be left spinning forever.

    This item keeps `call_id`; the started item does not carry one at all. See
    `_tool_call_started_event` for why that asymmetry is load-bearing on OWUI
    0.11.1+.
    """
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
    """The text a tool card shows for a tool event's `result`.

    OpenClaw sends a string, a list of content blocks, or the agent-tool
    envelope `{"content": [...], "details": {...}}` (`sanitizeToolResult`).
    The card shows the content: text blocks one per line, an image (its data
    is already stripped) as a short note. `details` is the tool's metadata
    (status, exit code, cwd, ...), not its output; dumping the whole envelope
    put it in every card (ELI-121). Anything else is shown as JSON.
    """
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

# A sub-agent's title is one line on a phone: its spawn label, else its
# task's first sentence cut to this many characters. The Status Action's
# rows and drawer use it too (ELI-155).
SUBAGENT_TITLE_MAX = 60
_FIRST_SENTENCE_RE = re.compile(r"(?<=[.!?])\s|\n")
# How much of a sub-agent's task its spawn card shows as input.
SPAWN_TASK_LINES = 3
SPAWN_TASK_CHARS = 300


def _short_title(text: str) -> str:
    """The first sentence of `text`, cut on a word to SUBAGENT_TITLE_MAX
    characters with an ellipsis: a sub-agent's title when it has no label."""
    first = _FIRST_SENTENCE_RE.split(text.strip(), 1)[0].strip()
    if len(first) <= SUBAGENT_TITLE_MAX:
        return first
    cut = first[:SUBAGENT_TITLE_MAX - 1]
    space = cut.rfind(" ")
    if space >= SUBAGENT_TITLE_MAX // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:(") + "…"


def _is_spawn_tool(name: str) -> bool:
    """`sessions_spawn`, bare (the Gateway's runtime) or as claude-cli names
    the OpenClaw MCP tool (`mcp__openclaw__sessions_spawn`)."""
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


# The tools a model calls a deferred tool through, by its catalog id or name:
# `tool_call` on OpenClaw 2026.10.1 to 10.4, `dispatch_action` from 10.10. The
# Gateway also reports the target as a nested call of its own
# (`parentToolCallId` = the wrapper's id, id `tool_call:<wrapper id>:...`).
DEFERRED_CALL_TOOLS = ("dispatch_action", "tool_call")


def _is_nested_tool_call(data: dict, known: "set | dict") -> bool:
    """A tool event for a call another call made on the model's behalf (the
    target of a deferred-tool wrapper). Its card is the wrapper's: a second
    card would show the same action twice, nested inside the first."""
    if data.get("parentToolCallId"):
        return True
    tcid = str(data.get("toolCallId") or "")
    parts = tcid.split(":")
    return len(parts) > 2 and parts[0] == "tool_call" and parts[1] in known


OPENCLAW_MCP_PREFIX = "mcp__openclaw__"


def _bare_tool_name(name: str) -> str:
    """An OpenClaw tool by the name the Gateway's live events give it. claude-cli
    calls them over MCP as `mcp__openclaw__<name>`, and its imported transcript
    keeps that name while the live events strip it, so a rebuilt card was
    titled differently from the one that streamed (on 2026.10 every claude-cli
    shell command: `exec`)."""
    return name[len(OPENCLAW_MCP_PREFIX):] if name.startswith(OPENCLAW_MCP_PREFIX) else name


def _deferred_call_view(name: str, args_str: str) -> tuple[str, str] | None:
    """A deferred-tool wrapper call as the call it makes: the target's name
    (the last part of a catalog id like `openclaw:core:session_status`) and
    its arguments. None for any other call."""
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
    """The target's result inside a wrapper's `{"tool": ..., "result": ...}`
    envelope. Anything else (an error, external content in its fence) is
    shown as it came."""
    envelope = _json_object(result_str)
    if not isinstance(envelope.get("tool"), dict) or "result" not in envelope:
        return result_str
    return _tool_result_text(envelope["result"])


def _tool_card_view(name: str, args_str: str) -> tuple[str, str]:
    """The title and input a tool card shows. A spawn card is named after the
    sub-agent it started ("🤖 <label>") and shows the head of its task, not
    the whole prompt; any other card shows the call as made (ELI-155)."""
    name = _bare_tool_name(name)
    if not _is_spawn_tool(name):
        return name, args_str
    args = _json_object(args_str)
    task = str(args.get("task") or "")
    return f"🤖 {_spawn_title(args)}", json.dumps({"task": _spawn_task_head(task)} if task.strip() else {})


def _spawn_result_view(args_str: str, result_str: str) -> tuple[str, bool]:
    """A spawn's receipt as one line, and whether the spawn failed. The
    receipt is JSON ({status, childSessionKey, runId, ...} or {status,
    error}); anything else is shown as it came."""
    receipt = _json_object(result_str)
    status = receipt.get("status")
    if not isinstance(status, str):
        return result_str, False
    if status == "accepted":
        return f"Started: {_spawn_title(_json_object(args_str))}", False
    error = str(receipt.get("error") or "").strip()
    return f"Not started ({status})" + (f": {error}" if error else ""), True


def _spawned_child(payload: dict):
    """(parent session key, child session key) of a tool event that is an
    accepted spawn's result, else None: what the child index records. On
    2026.10 the wrapper's nested call carries the spawn itself; the wrapper's
    own envelope is read too."""
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
    return parent, child


def _spawn_call_task(payload: dict) -> str | None:
    """The task a tool event's spawn call was given (the call itself, or the
    one a deferred-tool wrapper makes), from its start event; else None."""
    data = payload.get("data") or {}
    if payload.get("stream") != "tool" or data.get("phase") != "start":
        return None
    name, args_str = _bare_tool_name(data.get("name") or ""), json.dumps(data.get("args") or {})
    view = _deferred_call_view(name, args_str)
    if view:
        name, args_str = view
    if not _is_spawn_tool(name):
        return None
    task = str(_json_object(args_str).get("task") or "").strip()
    return task or None


def _tool_call_error_relabel_event(name: str, tool_call_id: str, args_str: str) -> dict:
    """Re-announce a failed call's `function_call` item with ❌ in its name.

    The card's collapsed row shows `attributes.name` and nothing else about
    outcome: its status icon is chosen by `isDone` alone, with no failure
    branch (`ToolCallDisplay.svelte:136`), so a red icon is unreachable
    without patching OWUI. The name is the only outcome-carrying field we
    control, and it renders through `Markdown`, so an emoji survives.

    The event is the start event again, same id and arguments: both of OWUI's
    reducers replace an `output_item.added` item that has the id of one they
    hold, the backend (`handle_responses_streaming_event`, what is saved) and
    the chat tab (`structuredOutput.ts::applyResponseStreamEvent`, what shows
    while the reply streams). So the mark shows live, on any card, last or
    not. `response.name.done` would reach only the backend's last item, and
    the tab ignores it (ELI-124). (`response.output_item.done` replaces by
    position in the backend, which the pipe cannot know.)
    """
    return _tool_call_started_event(f"{name} {TOOL_ERROR_MARK}", tool_call_id, args_str)


def _tool_error_banner(result_str: str) -> str:
    """Prefix a failed tool's Output with an explicit failure line.

    Used both as the fallback when the card label can't be safely relabeled
    (parallel calls) and alongside a relabel, so the reason is visible once
    the card is expanded rather than only implied by the ❌.
    """
    return f"{TOOL_ERROR_MARK} tool call failed\n\n{result_str}"


def _render_tool_result_block(name: str, tool_call_id: str, args_str: str,
                              result_str: str, meta) -> str:
    """Render a finished tool call as OWUI's collapsible `tool_calls` card.

    The markdown path, used when native tool items are off and by the shadow
    `_TurnRenderer` (which completes a run's OWUI message when the inline turn
    ended early, outside any live stream -- so it has no protocol channel and
    must bake the card into text).
    """
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
    """Reconstructs the visible assistant content of a run from its raw gateway
    event stream, mirroring the inline pipe loop's rendering.

    Purpose: when the inline pipe generator ends BEFORE a long run finishes
    (cancel, or a torn-down request), the turn writer takes the message over
    (ELI-100). This renderer accumulates the COMPLETE content (assistant text
    + every tool block, in order) straight from the same events the
    persistent connection already dispatches, so the run's ORIGINAL OWUI
    message can be finalized to exactly what a non-interrupted turn would
    have stored.

    Scope: assistant text deltas, item-carried text, and tool-result blocks.
    It holds the raw run text: the turn writer shows a resolved `MEDIA:` image
    and an answered ask-user card through the rewrites the inline reply
    recorded (`note_turn_rewrite`).
    """

    def __init__(self):
        self.visible_text = ""
        # The same run the way OWUI 0.12 stores a streamed reply: `text` is the
        # message `content` (assistant text only) and `output` holds the
        # Responses-API items the cards are drawn from. The frontend renders
        # `output` whenever a message has one, so a tool block baked into
        # `content` as markdown is no card there (ELI-92).
        self.text = ""
        self.output: list[dict] = []
        self._assistant_stream_text = ""
        self._last_item_text = ""
        self._active_tool_args: dict[str, str] = {}
        # A result may carry no tool name (claude-cli's `tool_result`).
        self._active_tool_names: dict[str, str] = {}
        self._tool_items: dict[str, dict] = {}
        self._tool_results: set[str] = set()
        # A new run continues the message (resume, recovery, answer run): its
        # first text gets the break the live stream puts between messages.
        # The Gateway inserts it only inside one run.
        self.run_boundary = False

    @property
    def received(self) -> str:
        """All the run text that arrived, shown or not: the dedup baseline."""
        return self._assistant_stream_text

    @property
    def last_item(self) -> str:
        """The last item event's text, the baseline for the next one."""
        return self._last_item_text

    def seed(self, run_text: dict | None) -> None:
        """Continue a run another reply showed: take its dedup baselines, so
        its closing whole-text events are not shown again."""
        if run_text:
            self._assistant_stream_text = run_text.get("received", "")
            self._last_item_text = run_text.get("item", "")

    def drop_shown(self, shown: str) -> bool:
        """Drop the start of the run that `shown` is, which another message
        already holds (an adopted run, ELI-147): its text and the cards
        inside it. False, changing nothing, when the text does not start
        with it."""
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

    def _add(self, chunk: str) -> None:
        if chunk:
            self.visible_text += chunk

    def _add_text(self, chunk: str) -> str:
        """Append `chunk` to the run's text; returns it as added."""
        if not chunk:
            return ""
        if self.run_boundary:
            self.run_boundary = False
            chunk = _message_boundary_separator(self.text, chunk) + chunk
        self._add(chunk)
        self.text += chunk
        last = self.output[-1] if self.output else None
        if last is None or last.get("type") != "message":
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

    def _tool_item(self, name: str, tcid: str, args_str: str, events: list) -> dict:
        """The call's card, opened (and its start event added to `events`)
        the first time the call is seen, by its start or, when that was
        missed, by its result. `name` and `args_str` are as the card shows
        them (`_tool_card_view`)."""
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
        """Answer every call still open with `note` and return those events.
        Open WebUI runs a streamed `function_call` that has no output when
        the stream ends and sends the result back as a new turn; stored, it
        is a spinner forever."""
        events = self._unanswered(note)
        for event in events:
            self._tool_results.add(event["item"]["call_id"])
            self.output.append(copy.deepcopy(event["item"]))
        return events

    def finished_output(self) -> list[dict]:
        """`output` as stored once the run is over: a call the run never
        answered is closed, and every item still in progress is completed,
        as OWUI does before its final write."""
        out = copy.deepcopy(self.output)
        out += [e["item"] for e in self._unanswered(TOOL_NO_RESULT_NOTE)]
        for item in out:
            if item.get("status") == "in_progress":
                item["status"] = "completed"
        return out

    def feed(self, payload: dict) -> list:
        """Grow the run's content by one event. Returns what it added for a
        live stream: the new text (a str) and the Responses events of a tool
        card opened, relabelled as failed, or answered. The inline reply
        shows them, so it and a turn writer show the same text and cards
        (ELI-147)."""
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

        if stream == "tool":
            tcid = data.get("toolCallId", "")
            if _is_nested_tool_call(data, self._active_tool_names.keys() | self._tool_results):
                return events
            if phase == "start":
                if tcid:
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
                    item["status"] = "completed"
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

# Anchor for state that must be shared by *every* `_GatewayConnection` alive in
# this Python process -- not just the current module's singleton. OWUI's function
# loader execs each redeploy into a brand-new module object with no teardown hook
# on the old one, so a connection left running by a previous deploy (a "zombie")
# keeps its own event loop. With private bookkeeping it would not know that the
# live connection already showed a run, and would write that turn into the chat
# a second time. Stashing the bookkeeping on
# `open_webui.socket.main` (OWUI's own stable module, never reloaded by our
# function) makes all connections read and write the *same* dicts. Since
# ELI-100 a retired connection writes no turns at all (`_turn_route_now`).
_SHARED_STATE_ATTR = "_openclaw_gateway_shared_state_v1"


def _shared_gateway_state() -> dict | None:
    """Return the process-global bookkeeping dicts shared across all
    `_GatewayConnection` instances, or None when not running inside OWUI
    (e.g. unit tests), in which case callers keep instance-local state so
    each test connection stays isolated."""
    try:
        import open_webui.socket.main as _owui_socket_main
    except Exception:
        return None
    state = getattr(_owui_socket_main, _SHARED_STATE_ATTR, None)
    if state is None:
        state = {}
        setattr(_owui_socket_main, _SHARED_STATE_ATTR, state)
    # `setdefault` per key (not a one-shot dict literal on the `is None`
    # branch): this same dict persists across redeploys of the function
    # (stashed on OWUI's stable socket module), so a *new* key added in a
    # later version must be backfilled into the already-existing dict --
    # otherwise `__init__` does `_shared["new_key"]` and KeyErrors on the
    # first request after redeploy.
    state.setdefault("delivered_live", {})
    state.setdefault("claimed_runs", {})
    state.setdefault("chat_write_locks", {})
    state.setdefault("turn_rewrites", {})
    return state


# The replies this process is streaming, for the Status Action (ELI-186):
# {(chat_id, message_id): the reply's _TurnRenderer}. Open WebUI saves a
# reply's tool cards (its `output`) only when the reply ends, in the chat and
# in its message row alike, so while it runs only the Pipe has them. The Pipe
# and the Action are separate Function modules: like the Pipe's shared state,
# the map lives on Open WebUI's own stable module, and outside Open WebUI on
# this one.
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
    """A copy of the tool cards and text a running reply shows so far, or
    None when this process streams no such reply."""
    renderer = _live_replies().get((chat_id, message_id))
    output = getattr(renderer, "output", None)
    return copy.deepcopy(output) if isinstance(output, list) else None


class _RunQueue(asyncio.Queue):
    """A run's event queue. Events wait for room (`await put`): a reply read
    slower than its run streams holds the receive loop back instead of losing
    text. Control markers (a gap, a restart, a session row) go in past the
    bound: they are few, a lost one can leave a reply that never ends, and
    the receive loop that puts them must never wait or raise: QueueFull there
    would kill the loop and hang every run on the connection."""

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


# Open WebUI's uvicorn Server, kept while it serves (see `owui_shutting_down`).
_UVICORN_SERVER = None


def remember_owui_server() -> None:
    """Keep a reference to the uvicorn Server that runs Open WebUI.

    uvicorn installs `Server.handle_exit` as the SIGTERM/SIGINT handler while
    it serves, and that sets `should_exit` when a signal arrives. It restores
    the old handlers before asyncio cancels the tasks still running, which is
    when Open WebUI's reply tasks reach the pipe, so the Server is taken from
    the handler earlier: at import and at every request."""
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
    """True once Open WebUI's server got SIGTERM or SIGINT (`ha apps stop`,
    restart, update). Its shutdown cancels every open reply the way a Stop
    does, and no page is left to say which it was (ELI-94)."""
    remember_owui_server()
    return bool(getattr(_UVICORN_SERVER, "should_exit", False))


remember_owui_server()


@dataclass
class _Consumer:
    """An active run consumer -- its ``asyncio.Queue`` receives events."""
    session_key: str
    run_id: str
    queues: list[asyncio.Queue] = field(default_factory=list)


# The turn writer's entry points (writer.py's `_TurnHooks`), which the Pipe
# build registers. None in the Status Action's build: it carries no turn
# writer, and its own connection only makes requests (ELI-149).
_TURNS = None
# The Pipe's handlers for `question.requested` / `question.resolved`
# (askuser.py, ELI-96), set by pipe.py. None in the Status Action, which asks
# nothing; only the connection that writes turns asks, so a question is asked once.
_QUESTION_HOOKS = None


class _GatewayConnection:
    """Singleton persistent WebSocket connection to the Gateway.

    Maintains one WS connection, dispatches events to per-run queues, and
    auto-reconnects on disconnect with exponential backoff.
    """

    def __init__(self, valves_ref, writes_turns: bool = True, scopes: list | None = None,
                 identity_name: str = "identity", requests_only: bool = False):
        # Weak reference to Pipe valves (re-read on each call)
        self._valves = valves_ref
        # The scopes this connection requests and signs. A connection built with
        # RESTRICTED_SCOPES has its own device identity (`identity_name`) and
        # only sends requests (`requests_only`: no subscriptions, it renders
        # nothing); see `_run_request`.
        self.scopes = list(GATEWAY_SCOPES if scopes is None else scopes)
        self._identity_name = identity_name
        self.requests_only = requests_only
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

        # Per-session locks serializing the queue-release -> chat.send -> run
        # registration critical section (`session_send_lock`, ELI-56).
        self._session_send_locks: dict[str, asyncio.Lock] = {}
        # Open `send_section`s: a message waiting for its turn, then sending.
        # A retired connection stays up for them as it does for consumers:
        # closing it would leave the waiter with no settle push, ever (ELI-172).
        self._open_send_sections = 0

        # Cancelled turns parked until they are claimed or released, by
        # session key (`park_cancelled_turn`). An immediate chat.abort on a
        # cancel would kill the turn AND every subagent it spawned, and Send
        # now cancels the same way Stop does.
        self._pending_cancel_aborts: dict[str, dict] = {}

        # Last model applied per session, {session_key: (model, ts)}, and the
        # per-session patch locks (`model_patch_lock`, ELI-59). Each Open
        # WebUI worker process keeps its own.
        self._model_patch_cache: dict[str, tuple[str | None, float]] = {}
        self._model_patch_locks: dict[str, asyncio.Lock] = {}

        # Last thinking-clamp note shown per session, so a standing mismatch
        # between the picked level and the model's ladder is explained ONCE
        # instead of prefixing every single answer with the same italic line.
        self._thinking_note_shown: dict[str, str] = {}

        # Reconnect state
        self._reconnect_attempt = 0
        self._max_backoff = 30  # seconds
        # A turn that needs the connection while the event loop waits out a
        # backoff asks it to try now (`_reconnect_now`) and waits for that
        # attempt's outcome (a future in `_reconnect_waiters`): the event loop
        # is the only reader of the socket, so the turn must not connect one of
        # its own (ELI-176).
        self._reconnect_now = asyncio.Event()
        self._reconnect_waiters: list = []
        # The Gateway sends a `tick` event every `policy.tickIntervalMs` of
        # hello-ok (30 s, server-maintenance-Wf2jQ0wx.mjs:156); its own client
        # drops a socket that sent no frame for twice that ("tick timeout",
        # client-CKDmO6tp.mjs:1447-1459). The event loop does the same.
        self._tick_interval_s = 30.0
        self._stopped = False
        # Set once a newer deploy of this function has taken over (see
        # `retire()`). A retired connection still serves runs it already
        # has consumers for, but never writes turns (`_turn_route_now`) and
        # never reconnects once those runs are gone.
        self._retired = False
        # This connection's identity as a turn writer. A message whose
        # recorded writer is gone (`_turn_writer_alive`: retired here, or its
        # worker process exited) can be taken over (DESIGN 2.3); retire()
        # removes it.
        self.writer_id = turn_writer_id()
        # Only a build with the turn writer writes turns (not the Status Action's).
        self.writes_turns = writes_turns and _TURNS is not None
        self._turn_pumps: dict = {}     # (session_key, run_id) -> _RunPump
        self._turn_writers: dict = {}   # (chat_id, message_id) -> _TurnWriter
        self._turn_skip_runs: dict = {}  # (session_key, run_id) of heartbeat runs
        # (session_key, run_id) -> events of an agent run not yet known to be
        # no heartbeat, and the runs known (`_turn_kind_known`)
        self._turn_unkinded: dict = {}
        self._turn_kinded: dict = {}
        # (child session, child run) -> a sub-agent's result
        self._child_results: dict = {}
        # session_key -> run id of an inline reply with no Open WebUI message
        # waiting for its interrupted run's owner
        self._awaiting_owner: dict = {}
        self._session_ids: dict = {}     # session_key -> sessionId (a cron wake's run id)
        self._wake_turns_bound: dict = {}  # session_key -> wake turn keys given a message
        # session_key -> [{job, start, end}] of the cron job runs into it this
        # socket heard start (`cron` events), newest last (`note_cron_event`)
        self._cron_runs: dict = {}
        self._turn_resolve_lock = asyncio.Lock()
        self._frame_seq: int | None = None
        self._stop_notice_seen = False  # `shutdown` without restartExpectedMs on this socket
        self._shutdown_seen = False     # any `shutdown` on this socket
        # Bumped on every successful connect/reconnect. A consumer that
        # captures this before `chat.send` can later tell "the queue is quiet
        # because the agent is thinking" from "the queue is quiet because the
        # Gateway went away and came back, taking my run with it" -- the
        # Gateway only persists a turn once it COMPLETES, so a restart mid-run
        # destroys it silently and no terminal event is ever emitted.
        self.connection_epoch = 0
        # A reconnect is not a restart. `hello-ok.server.bootId` names the
        # Gateway process: same id after a reconnect = the same Gateway, its
        # runs are still going; a new id = it restarted and took them with it.
        # A reconnect counted as a restart would send a bogus resume turn into
        # live sessions and close their replies. `gateway_restarts` only counts
        # real restarts; consumers compare it, not `connection_epoch`.
        self.gateway_boot_id: str | None = None
        self.gateway_restarts = 0
        self.gateway_version: str | None = None   # hello-ok.server.version
        # hello-ok.features: what this Gateway offers, checked instead of its
        # version number (a feature can land in several release lines).
        self.gateway_methods: frozenset = frozenset()
        self.gateway_events: frozenset = frozenset()

        # Next request id
        self._next_req_id = 1

        # Delivery bookkeeping. When running inside OWUI these dicts are the
        # *same objects* for every `_GatewayConnection` in the process (see
        # `_shared_gateway_state`), so a turn shown live by one connection is
        # seen as delivered by all of them. Outside OWUI
        # (unit tests) each connection gets its own isolated dicts.
        _shared = _shared_gateway_state()

        # Runs the startup stub reconcile already finished, keyed by
        # "claim:<chat>:<stub>", so two connections never finalize one stub twice.
        self._claimed_runs: dict[str, bool] = (
            _shared["claimed_runs"] if _shared is not None else {}
        )

        # `_delivered_live`: by "session_key:run_id", every *final* event
        # dispatched to a live consumer queue (shown to an open tab, which
        # persists it). A duplicate or retried final for that run id after
        # the tab's request closed is caught by identity, never by comparing
        # text (two turns can both end with "OK.").
        # `_held_for_live`: events of a run continuing a message (an ask-user answer) that
        # arrived before its reply registered for it, while that reply still
        # streams the message: never a turn writer's.
        self._held_for_live: dict[tuple[str, str], list] = {}
        self.unreachable_held_drains = 0
        self._delivered_live: dict[str, float] = (
            _shared["delivered_live"] if _shared is not None else {}
        )

        # Per-chat write locks for the read-modify-write paths that are not
        # turn writers (stub reconcile). Shared across connections.
        # Keyed by chat_id.
        self._chat_write_locks: dict[str, asyncio.Lock] = (
            _shared["chat_write_locks"] if _shared is not None else {}
        )

        # Shadow bookkeeping. For a run that a live pipe() call is rendering
        # into a known OWUI message, `_run_targets[key]` holds that message's
        # (chat_id, message_id) and `_run_renderers[key]` a `_TurnRenderer`
        # accumulating the full content from this run's event stream -- so if
        # the inline turn ends before the run does (cancel, close, a torn-down
        # request), a turn writer continues the SAME message from it
        # (`_turn_route_now`, DESIGN 2.6). Instance-local
        # (not shared): only the singleton connection runs pipe() and dispatches
        # its own events; a zombie connection never registers a target.
        self._run_targets: dict[str, dict] = {}
        self._run_renderers: dict[str, "_TurnRenderer"] = {}
        # What each reply's message went out as, by "session_key:message_id"
        # (`note_reply_sent`, ELI-82).
        self._sent_replies: dict[str, dict] = {}
        # What the inline reply showed in place of raw run text, by
        # "chat_id:message_id": [(raw, shown)] for each resolved `MEDIA:`
        # directive and each ask-user prompt (its card, or its fallback text).
        # A turn writer that takes the message over repeats them. Shared, so
        # a successor deploy's writer has them too.
        self._turn_rewrites: dict[str, list] = (
            _shared["turn_rewrites"] if _shared is not None else {}
        )

        # Sessions whose transcript events (`session.tool`) this connection
        # asked for with `sessions.messages.subscribe`. A subscription ends
        # with the socket, so `_subscribe_all` renews every one after each
        # connect. Tool events for one call can arrive twice (as the run's
        # initiator AND as a subscriber); `_tool_events_seen` keeps the first.
        self._message_subscriptions: set[str] = set()
        # OWUI session key -> run ids active when this socket last subscribed
        # (ELI-100 DESIGN 2.1): runs that started before the subscription,
        # restart recovery above all, whose start push this socket never got.
        # An empty list means "active, ids not given": the Gateway omits
        # activeRunIds for claude-cli runs (session-active-runs-BURU1M8_.mjs:90).
        self.active_run_snapshot: dict[str, list[str]] = {}
        self._subscriptions_stale = False
        # One Event per session someone is waiting on, set by the next
        # `sessions.changed` that carries run state for it (ELI-88).
        self._session_change_waiters: dict[str, asyncio.Event] = {}
        # The same, set by the next `session.message` (a transcript append)
        # for the session (ELI-164).
        self._session_message_waiters: dict[str, asyncio.Event] = {}
        self._tool_events_seen: dict[tuple, bool] = {}
        # (session key, tool call id) -> the task of a spawn call that started
        # and has no result yet (`_spawn_call_task`), for the child index.
        self._spawn_tasks: dict[tuple, str] = {}

    async def _subscribe_all(self) -> None:
        """Renew this socket's subscriptions: the session index
        (`sessions.changed`, which carries run start/settle), and transcript
        events for every OWUI session subscribed so far."""
        try:
            # List params make it one request: the Gateway registers this
            # socket for pushes, THEN lists (sessions-subscriptions:21-36), so a
            # run is in this snapshot or its start push comes after it.
            resp = await self.send_request("sessions.subscribe", {"activeOnly": True}, timeout=15)
        except Exception as ex:
            pipe_log(f"  sessions.subscribe failed: {ex}")
        else:
            self._note_active_snapshot(((resp or {}).get("list") or {}).get("sessions") or [])
            self._stop_notice_seen = False
            self._shutdown_seen = False
            if _TURNS:
                spawn_background(_TURNS.after_hello(self))
        for key in list(self._message_subscriptions):
            await self._subscribe_messages(key)
        # A waiter that read run state before the subscription existed would
        # sleep through a settle it was never sent; make every waiter read
        # again now that settles are pushed.
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
            # Refused like a run while a Gateway that just started prepares
            # the agent: given up, the renewed session would get no tool
            # events until the next reconnect.
            await _send_when_prepared(
                self, "sessions.messages.subscribe", {"key": session_key}, timeout=15
            )
            return True
        except Exception as ex:
            pipe_log(f"  sessions.messages.subscribe {session_key[-36:]} failed: {ex}")
            return False

    async def ensure_message_subscription(self, session_key: str) -> None:
        """Receive `session.tool` for `session_key` from now on. Tool events
        reach the connection that STARTED a run as `agent` events; everyone
        else, including this connection after a reconnect and for any run the
        Gateway starts itself (restart recovery, wakes, announces), gets them
        only as `session.tool` on a transcript subscription (ELI-92)."""
        if not session_key or session_key in self._message_subscriptions:
            return
        self._message_subscriptions.add(session_key)
        if not await self._subscribe_messages(session_key):
            self._message_subscriptions.discard(session_key)

    def _first_tool_event(self, payload: dict) -> bool:
        """False for a tool event this connection already dispatched: the
        same call's start/result can arrive both as the initiator's `agent`
        event and as a subscriber's `session.tool`."""
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

    def _spawn_seen(self, payload: dict):
        """(parent, child, task or None) of an accepted spawn's result event,
        else None. The task is the one the call's start event carried: the
        receipt has none, and the child's transcript may not hold it yet."""
        data = payload.get("data") or {}
        call = (payload.get("sessionKey"), data.get("toolCallId"))
        task = _spawn_call_task(payload)
        if task:
            self._spawn_tasks[call] = task
            if len(self._spawn_tasks) > 200:
                for stale in list(self._spawn_tasks)[:100]:
                    self._spawn_tasks.pop(stale, None)
            return None
        if data.get("phase") != "result":
            return None
        task = self._spawn_tasks.pop(call, None)
        spawned = _spawned_child(payload)
        return (*spawned, task) if spawned else None

    def session_change_event(self, session_key: str) -> asyncio.Event:
        """An Event set by the next pushed change to `session_key`'s run state
        (`sessions.changed` with `hasActiveRun`: run started, run settled).

        Take it BEFORE reading the state, then wait on it only if the read
        said busy: a change landing between the read and the wait has already
        set it, so no change can slip through and no polling is needed. A
        socket drop sets every waiter too (the change may be among the lost
        events), so a waiter always re-reads after a gap."""
        ev = self._session_change_waiters.get(session_key)
        if ev is None:
            ev = self._session_change_waiters[session_key] = asyncio.Event()
        return ev

    def _wake_session_waiters(self, session_key: str | None = None) -> None:
        keys = [session_key] if session_key else list(self._session_change_waiters)
        for key in keys:
            ev = self._session_change_waiters.pop(key, None)
            if ev is not None:
                ev.set()
        if session_key is None:
            # A gap or a fresh subscription: an append may be among the lost.
            for key in list(self._session_message_waiters):
                self._session_message_waiters.pop(key).set()

    def session_message_event(self, session_key: str) -> asyncio.Event:
        """An Event set by the next `session.message` for `session_key` (the
        Gateway appended a transcript message), or by a socket gap. Take it
        BEFORE reading the transcript, as with `session_change_event`."""
        ev = self._session_message_waiters.get(session_key)
        if ev is None:
            ev = self._session_message_waiters[session_key] = asyncio.Event()
        return ev

    def _on_session_message(self, payload: dict) -> None:
        key = payload.get("sessionKey") or ""
        ev = self._session_message_waiters.pop(key, None)
        if ev is not None:
            ev.set()
        if _TURNS and key:
            _TURNS.session_message(self, key)

    def _on_shutdown(self, payload: dict) -> None:
        """DESIGN 2.7 F4: the Gateway announces its close before it aborts any
        run. No restartExpectedMs = stop mode, whose aborted runs it never
        recovers; with it, restart mode, whose aborted rows stay `running`."""
        self._shutdown_seen = True
        if payload.get("restartExpectedMs") is None:
            self._stop_notice_seen = True

    def _on_sessions_changed(self, payload: dict) -> None:
        """A session row changed. Waiters on its run state re-read it, and an
        OWUI session whose run just started gets a transcript subscription
        before its first tool call (measured: run start is pushed ~3s ahead of
        the first `session.tool`)."""
        key = payload.get("sessionKey") or payload.get("key") or ""
        self.note_session_id(key, payload.get("sessionId"))
        if key and "hasActiveRun" in payload:
            self._wake_session_waiters(key)
            self.row_to_consumers(key, payload)
        if _TURNS and key and payload.get("hasActiveRun") is False:
            _TURNS.settled(self, key, payload)
        if (payload.get("hasActiveRun") and key
                and key not in self._message_subscriptions
                and self.parse_owui_session_key(key)):
            spawn_background(self.ensure_message_subscription(key))

    def row_to_consumers(self, session_key: str, row: dict, resync: bool = False,
                         read_at: float | None = None, outside_loop: bool = False) -> None:
        """Hand session row `row` to every inline reply of `session_key`.
        `read_at`: when the row was read, if not just now (a resync's read
        started before its reply came back). `outside_loop`: put by a task,
        not the receive loop, which may be waiting to put an earlier event
        into a full queue; the row waits its turn behind it, in a task per queue so one stalled reply holds up no other."""
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
        """Record a cron job run into a session from the Gateway's `cron`
        event: `started` at `runAtMs`, `finished` after `durationMs`. A wake's
        events are known by this run (`cron_run_at`): on 2026.9 a short wake's
        one-shot job is deleted, and leaves `cron.list`, before its first
        event is judged (ELI-181)."""
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
        """The recorded cron job run into `session_key` that `at_ms` falls in,
        the newest such, else None."""
        for run in reversed(self._cron_runs.get(session_key) or []):
            if run["start"] <= at_ms and (run["end"] is None or at_ms <= run["end"]):
                return run
        return None

    def note_session_id(self, session_key: str, session_id) -> None:
        if session_key and isinstance(session_id, str) and session_id:
            self._session_ids[session_key] = session_id

    async def session_id_for(self, session_key: str) -> str | None:
        """The session's sessionId, which is also the run id of every cron wake
        into it. Session rows and lifecycle / session.tool events carry it;
        assistant and chat events do not, so a wake whose first event is one
        of those reads the row."""
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
                            request_task_id: str | None = None) -> None:
        """Remember that this run streams into OWUI message `message_id` in
        `chat_id`, and start a shadow `_TurnRenderer` for it. No-op without both
        ids (e.g. a chat with no stable message id yet). `thinking`: the
        turn's level, which a writer's resume keeps. `request_task_id`: Open
        WebUI's task for the request, whose end a handover's write waits for."""
        if not (chat_id and message_id and run_id):
            return
        key = f"{session_key}:{run_id}"
        self._run_targets[key] = {"chat_id": chat_id, "message_id": message_id,
                                  "thinking": thinking, "request_task_id": request_task_id}
        self._run_renderers[key] = _TurnRenderer()
        # Bound memory: a run that never emits a terminal would otherwise leak
        # its target/renderer forever. Far above any real concurrency.
        if len(self._run_targets) > 100:
            for stale in list(self._run_targets)[:50]:
                self._run_targets.pop(stale, None)
                self._run_renderers.pop(stale, None)

    def handover_of(self, session_key: str, run_id: str, thinking: str | None = None) -> dict:
        """What a turn writer takes over from a run's request (DESIGN 2.6):
        its message, its shadow renderer, its thinking level, and `shown`,
        what earlier replies already showed of an adopted run."""
        key = f"{session_key}:{run_id}"
        target = self._run_targets[key]
        return {"message_id": target["message_id"], "renderer": self._run_renderers.get(key),
                "thinking": thinking or target.get("thinking"), "shown": target.get("shown", ""),
                "request_task_id": target.get("request_task_id")}

    def unregister_run_target(self, session_key: str, run_id: str) -> None:
        key = f"{session_key}:{run_id}"
        self._run_targets.pop(key, None)
        self._run_renderers.pop(key, None)

    def note_reply_sent(self, session_key: str, message_id: str | None,
                        run_id: str, text: str) -> None:
        """Remember that OWUI reply `message_id` sent `text` as `run_id`, so
        a second call for the same reply follows that run instead of
        sending the message again (ELI-82)."""
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
        """The inline reply moved from `run_id` to `next_run_id` (an ask-user
        answer run): the target and its shadow renderer follow it, so a
        handover of the answer run continues the same reply (the writer finds
        the question run in the transcript as a pipe run of the same
        message). The question run was shown to its end: its late events open
        nothing. `shown=False` moves the target only: the answer is not sent
        yet, or its send failed and the target
        moves back."""
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
        """The inline reply showed `shown` where its run said `raw` (a resolved
        `MEDIA:` directive, an ask-user prompt's card). A writer that takes
        the message over shows the same."""
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
        """Return the process-wide `asyncio.Lock` for serializing the
        read-modify-write paths into `chat_id` (stub reconcile),
        creating it on first use."""
        lock = self._chat_write_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_write_locks[chat_id] = lock
        return lock

    # ── Public API ──────────────────────────────────────────────────

    @property
    def reconnecting(self) -> bool:
        """The socket dropped and the event loop is bringing it back: until
        then a request fails without reaching the Gateway (`send_request`).
        The reconnect renews the subscriptions and wakes every
        `session_change_event` waiter (`_subscribe_all`)."""
        return (self._ws is None and not self._stopped and self._event_loop_task is not None
                and not self._event_loop_task.done())

    async def ensure_connected(self):
        """Ensure the WS connection is up; connect/reconnect if needed."""
        if self._ws and self._event_loop_task and not self._event_loop_task.done():
            return
        if self._retired and not self._serving():
            # Reviving a retired connection is how orphans piled up: each one
            # kept writing every agent turn with its own (older) code.
            raise GatewayError(
                "gateway connection retired: a newer deploy of the pipe owns it now"
            )
        if self._event_loop_task and not self._event_loop_task.done() and not self._stopped:
            # The event loop is reconnecting. A socket connected here would not
            # be read until its backoff ends, and its next attempt would replace
            # it: the turn's first request then timed out (ELI-176). Have the
            # loop try now, and take that attempt's outcome.
            waiter = asyncio.get_running_loop().create_future()
            self._reconnect_waiters.append(waiter)
            self._reconnect_now.set()
            await asyncio.wait({waiter, self._event_loop_task}, return_when=asyncio.FIRST_COMPLETED)
            if not waiter.done():
                raise GatewayError("gateway connection stopped while reconnecting")
            error = waiter.result()
            if error is not None:
                raise error if isinstance(error, GatewayError) else GatewayError(str(error))
            return
        async with self._init_lock:
            if self._ws and self._event_loop_task and not self._event_loop_task.done():
                return
            self._stopped = False
            await self._connect_once_started()
            if not self._event_loop_task or self._event_loop_task.done():
                self._event_loop_task = asyncio.create_task(self._event_loop())

    async def send_request(self, method: str, params: dict, timeout: float = 10) -> dict:
        """Send a request and wait for the response."""
        # `self._ws` is briefly None while the event loop is reconnecting
        # (see `_event_loop`/`_reconnect`). Without this guard the `.send`
        # below raises a bare `AttributeError: 'NoneType' object has no
        # attribute 'send'`, which surfaces to the user as a cryptic
        # "**Error sending message:** 'NoneType'...". A GatewayError with a
        # clear reason is caught by the same callers and reads sanely.
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
        """Per-session lock serializing the queue-release -> chat.send -> run
        registration critical section (see `_session_send_locks`). Created
        lazily; asyncio.Lock is created on the running loop the first time it
        is awaited."""
        return self._session_send_locks.setdefault(session_key, asyncio.Lock())

    @contextlib.asynccontextmanager
    async def send_section(self, session_key: str):
        """`session_send_lock`, plus: this connection stays up until the
        section ends, even if a newer deploy retires it meanwhile. The wait
        for a running run to settle happens in here, and the settle is pushed
        to this socket only (ELI-172: a deploy mid-wait closed it with the
        run, and the queued message was never sent)."""
        self._open_send_sections += 1
        try:
            async with self.session_send_lock(session_key):
                yield
        finally:
            self._open_send_sections -= 1
            self._close_if_retired_and_idle()

    MODEL_PATCH_CACHE_TTL_S = 60.0

    def model_patch_lock(self, session_key: str) -> asyncio.Lock:
        """Per-session lock so a burst of same-session messages issues one
        sessions.patch, not N concurrent ones (ELI-59). Lazily created, same
        pattern as session_send_lock."""
        return self._model_patch_locks.setdefault(session_key, asyncio.Lock())

    def model_patch_cached(self, session_key: str, model: str | None) -> bool:
        """True if `model` was successfully applied to this session within the
        cache TTL, so the idempotent sessions.patch can be skipped (ELI-59).
        The TTL bounds the wrong-model window if the session's model is changed
        externally (e.g. /model in the Control UI) between OWUI messages."""
        entry = self._model_patch_cache.get(session_key)
        if entry is None:
            return False
        cached_model, ts = entry
        return cached_model == model and (time.time() - ts) < self.MODEL_PATCH_CACHE_TTL_S

    def record_model_patched(self, session_key: str, model: str | None) -> None:
        """Record a successful sessions.patch for the cache (ELI-59)."""
        self._model_patch_cache[session_key] = (model, time.time())

    def should_announce_thinking_note(self, session_key: str, note: str) -> bool:
        """True the first time a given clamp note applies to a session.

        The level is picked in a filter dropdown and then stays picked, so a
        model that cannot honour it produces the identical note on every
        message. Told once it is useful; repeated above every answer it is
        noise the user has no way to dismiss, which is what it became in
        practice. Any CHANGE (different level, different model, a ladder
        learned from a rejection) is a new note and speaks up again.

        Per connection rather than persisted on purpose: after a redeploy or a
        reconnect the reminder is worth one repeat, and this must never be a
        file whose staleness could silence a genuinely new mismatch.
        """
        if not note:
            return False
        if self._thinking_note_shown.get(session_key) == note:
            return False
        self._thinking_note_shown[session_key] = note
        return True

    def invalidate_model_patch(self, session_key: str) -> None:
        """Drop the cached model for a session (ELI-59) -- called on patch
        failure so the next message retries the RPC from scratch."""
        self._model_patch_cache.pop(session_key, None)

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
        q = _RunQueue(maxsize=500)
        consumer.queues.append(q)
        for ev in self._held_for_live.pop((session_key, run_id), []):
            q.put_nowait(_event_frame(ev))  # the run's first events, held for this reply
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
            self._release_held(session_key)
        pipe_log(f"  unregistered consumer: {key[:60]}...")
        self._close_if_retired_and_idle()

    def _serving(self) -> bool:
        """Work a retired connection still owes: replies streaming here, and
        messages waiting here to send (ELI-172)."""
        return bool(self._consumers or self._open_send_sections)

    def _close_if_retired_and_idle(self) -> None:
        if self._retired and not self._serving():
            pipe_log("  retired gateway connection: last run finished, closing")
            spawn_background(self.disconnect())

    def live_reply_owns(self, session_key: str, run_id: str) -> bool:
        """True when `run_id` continues a message (an ask-user answer, a
        resume) that a live inline reply of another run still streams: its
        events wait for that reply (`register_consumer`) instead of opening a
        second writer on the message."""
        parsed = parse_pipe_run_id(run_id)
        waiting = self._awaiting_owner.get(session_key)
        if waiting and parsed is None and run_id != waiting:
            # A reply with no message waits for its run's owner: any
            # agent run here may be the Gateway's recovery of it. It waits
            # until the reply has read the transcript (rule E).
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
        """A reply ended. The runs held for it stay held until they drain, in
        their order of arrival: into the run's own queue
        if a reply registers for it (`register_consumer`; the ask-user switch
        registers right after this release), before the run's next event
        (`_turn_route_now`), or here, in the background, for a run that sends
        no more."""
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
        """Route the held events of a run no live reply owns any more, in
        order and synchronously: the router never awaits, so nothing routed
        for the run can come between them."""
        if self.live_reply_owns(session_key, run_id):
            return
        held = self._held_for_live.pop((session_key, run_id), None)
        if not held:
            return
        consumer = self._consumers.get(f"{session_key}:{run_id}")
        if consumer is not None:
            # Unreachable: `register_consumer` takes a run's held events
            # before its queue exists. Counted and logged so a path that
            # breaks this shows; the events still reach the reply.
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
        """Record that this run's final event was just dispatched to a live
        consumer queue -- i.e. genuinely shown to an open browser tab, which
        persists it itself via OWUI's own client-side flow. See
        `_delivered_live`'s docstring: a late event of such a run never
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

    def parse_owui_session_key(self, session_key: str) -> tuple[str, str] | None:
        """Reverse `_owui_session_key`: extract (user_id, chat_id) from a
        session key this pipe built, e.g.
        "agent:main:openwebui-<user_id>-<chat_id>". Both ids are fixed-width
        36-char UUIDs, so a positional split resolves the ambiguity of a
        plain "-".split() (UUIDs themselves contain dashes). Returns None for
        session keys not owned by this pipe/agent (e.g. cross-channel bleed).
        """
        agent_id = getattr(self._valves(), "AGENT_ID", None)
        if not agent_id:
            return None
        prefix = f"agent:{agent_id}:openwebui-"
        if not session_key.startswith(prefix):
            return None
        after = session_key[len(prefix):]
        if len(after) < 73 or after[36] != "-":
            return None
        return after[0:36], after[37:73]

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

    async def session_history(self, session_key: str,
                              limit: int = RECOVERY_HISTORY_LIMIT,
                              max_chars: int = RECOVERY_HISTORY_MAX_CHARS,
                              timeout: float = 10) -> dict:
        """Fetch the session's recent messages unclipped, with each turn in
        the role the transcript holds (`_transcript_roles`)."""
        hist = await self.send_request(
            "chat.history",
            dict(sessionKey=session_key, limit=limit, maxChars=max_chars),
            timeout=timeout
        )
        if isinstance(hist, dict) and isinstance(hist.get("messages"), list):
            hist["messages"] = _transcript_roles(hist["messages"])
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
                            waiting: list | None = None) -> None:
        """Hand a cancelled reply's unfinished business to the next message.

        OWUI cancels a response the same way for Stop and for Send now /
        interrupt (which then sends a new message): both call
        stopTasksByChatId, and the message queue lives only in the browser.
        So the cancelled turn is parked first and decided after:

        * `run_id` keeps running, and `queue` stays registered so every event
          the run emits (including ones queued but not yet shown) is buffered
          rather than dropped;
        * `carried` holds user messages the cancelled reply had accepted but
          not yet sent to the Gateway, oldest first;
        * `run_text` holds the run's text dedup baselines up to the cut
          ({"received", "stream", "item"}). The run's closing events repeat
          its WHOLE text; a claimer that starts from empty baselines takes all
          of it for new and appends the entire reply again.

        * `reply` is the cancelled reply's own state, {message_id, renderer}:
          the message it showed the run in, and what it showed there.
        * `waiting` are the replies whose messages are in `carried`
          (`_TurnState.waiting`): the claimer ends each with a note once it
          sent them; a release ends them as not sent (ELI-188).

        `settle` (a coroutine function) asks the browser which button was
        pressed and calls `settle_parked_turn`: Stop releases it now, Send now
        holds it for the follow-up message, whose claim keeps the run in its
        own message (`_keep_in_own_message`). It runs as a background task
        started here, so interrupting the cancelled reply cannot interrupt it
        (ELI-95), and the entry is `resolved` when it returns, which the
        intent query's own bound ensures. Without one, nobody can say (no page
        hook, API use): it is resolved now, and `grace_s` decides.
        `claim_cancelled_turn` takes all of it over; if nothing claims it, the
        run is aborted, the queue released and the carried messages dropped
        (the user stopped them).

        Synchronous on purpose: it runs on the generator's cancel/close path,
        where an await can itself be interrupted, so nothing here may depend
        on one. The Gateway calls and the settle go to background tasks.

        Open WebUI shutting down cancels every open reply too, and that is no
        Stop: the run keeps going, nothing is aborted, and the next process's
        startup reconcile finishes the reply (ELI-90 if the run is still
        active, ELI-94 if it ended while Open WebUI was down).
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
            waiting = list(previous["waiting"]) + [
                r for r in (waiting or []) if r not in previous["waiting"]]
            if previous["queue"] is not None and previous["queue"] is not queue:
                self.unregister_consumer(
                    session_key, previous["run_id"], queue=previous["queue"])
            if previous["run_id"] and previous["run_id"] != run_id:
                spawn_background(self._abort_cancelled_run(
                    session_key, previous["run_id"], previous["send_stop"]))
        if run_id:
            # The cancelled message keeps what it showed (the caller's forced
            # snapshot). The run's tail belongs to whoever claims it: never
            # back-filled into the stopped message, never written as a turn of
            # its own.
            self.unregister_run_target(session_key, run_id)
            self.mark_delivered_live(session_key, run_id)

        entry = {"run_id": run_id, "queue": queue, "carried": list(carried),
                 "send_stop": send_stop, "task": None,
                 "resolved": asyncio.Event(), "run_text": run_text, "reply": reply,
                 "waiting": list(waiting or [])}

        def _release() -> None:
            # Read from the entry: a run kept in its own message is no longer
            # the entry's to stop.
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
                # A follow-up message is already here, waiting for the page's
                # answer: only an explicit Stop may release the run now.
                return
            self._pending_cancel_aborts.pop(session_key, None)
            entry["resolved"].set()
            pipe_log(f"  no new message within {wait_s:g}s of the cancel: "
                     f"stopping run {str(entry['run_id'])[:20]}...")
            entry["release"]()

        entry["task"] = spawn_background(_fire())

    def settle_parked_turn(self, session_key: str, run_id: str | None,
                           intent: str | None, hold_s: float) -> None:
        """Apply what the browser said about the cancel of `run_id`.

        "stop": the user pressed Stop (or Escape), so stop now. "send_now":
        a follow-up message is on its way, so hold the run for it; `hold_s`
        only bounds a browser that dies before sending it. Anything else
        (no hook, no answer) keeps the fallback timer. No-op if the turn was
        already claimed or released.
        """
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
            # The follow-up message keeps the run (`claim_cancelled_turn`), not
            # this answer: Open WebUI tells the tab the reply was cancelled,
            # with its running tool cards drawn done, only after the cancel
            # handling returns, which this answer can beat. A writer opened
            # now would be overwritten. The page sends the follow-up only
            # after Open WebUI finished the cancel.
            pipe_log(f"  cancel was a Send now: holding run {str(run_id)[:20]}... "
                     f"for the follow-up message (up to {hold_s:g}s)")
            self._arm_parked_timer(session_key, entry, hold_s)
        else:
            pipe_log("  cancel intent unknown (no page hook or no answer): "
                     "keeping the fallback window")
        entry["resolved"].set()

    def _keep_in_own_message(self, session_key: str, entry: dict) -> bool:
        """Keep a parked run in the message it was cancelled in (ELI-158):
        the turn writer takes that message over with what the reply showed
        and the run's events buffered since the cancel, and finishes it when
        the run really ends. A running tool card stays running there instead
        of being shown done while the run goes on in the next reply. The
        entry keeps only the messages the reply had not sent. False when
        there is no message to keep it in (an API request) or no run."""
        reply, run_id, queue = entry.get("reply"), entry["run_id"], entry["queue"]
        if not reply or not run_id or not _TURNS:
            return False
        taken = queue.take_all() if queue is not None else []
        events = [m.get("payload") or {} for m in taken if m.get("type") == "event"]
        # Parking marked the run delivered so a late event reopens nothing;
        # its message is open again now, so its events must reach the writer.
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

        Waits for the page's answer about the cancel (`resolved`), so a Stop
        whose answer is still on its way is honored: OWUI follows a Stop with
        the next queued message, which must start fresh rather than continue
        the run the user just stopped. The answer's own query is bounded
        (`_INTENT_QUERY_TIMEOUT_S`), and it always runs to its end in the
        background, so this needs no bound of its own (ELI-95).

        Returns {"run_id", "queue", "carried", "waiting"}; the caller now
        owns the run, its buffered queue, the carried messages and the
        replies waiting for them.
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
        # The follow-up message (after Send now, or inside the fallback
        # window) keeps the run in its own message; the new reply takes only
        # the messages.
        self._keep_in_own_message(session_key, entry)
        return {"run_id": entry["run_id"], "queue": entry["queue"],
                "carried": list(entry["carried"]),
                "waiting": list(entry["waiting"]),
                "run_text": entry.get("run_text")}

    def seed_run_renderer(self, session_key: str, run_id: str,
                          queue: asyncio.Queue,
                          run_text: dict | None = None) -> None:
        """Bring a freshly registered shadow renderer level with an adopted
        run: give it the run's dedup baselines (so the closing whole-text
        events are not appended again) and feed it the events still waiting
        in `queue`, which were dispatched before this reply registered its
        own target. Without this a handover to a turn writer would miss the
        buffered text and duplicate the rest."""
        key = f"{session_key}:{run_id}"
        renderer = self._run_renderers.get(key)
        if renderer is None:
            return
        renderer.seed(run_text)
        # What the earlier replies showed of the run is theirs: a writer that
        # takes this one over rebuilds from after it (ELI-147).
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

    async def retire(self):
        """Step down in favor of a newer deploy's connection.

        Runs that already have a consumer here keep being served until they
        unregister (cutting them would lose the reply), and so does a message
        waiting here to send (`send_section`, ELI-172); everything else stops
        now. Once the last consumer leaves, the connection closes itself and
        `ensure_connected` refuses to bring it back.
        """
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

    # ── Internal ────────────────────────────────────────────────────

    def _mark_event_gap(self) -> None:
        """Put a gap marker into every run queue, in stream order.

        The Gateway has no event replay (sequence numbers are per connection),
        so whatever it sent while this socket was down is gone. The marker
        sits exactly between the last event before the drop and the first one
        after. A reply that reads it hands its message to the turn writer,
        whose transcript rebuild has what was lost (ELI-147).
        """
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
        """Count a Gateway restart only when its boot id changes. Every
        supported Gateway sends one; below the minimum a restart goes
        uncounted."""
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
        host, port = _parse_gateway_url(valves.GATEWAY_URL)
        token = valves.GATEWAY_TOKEN

        if not token:
            raise GatewayError(
                "The GATEWAY_TOKEN valve is empty. Set it in Open WebUI under "
                "Admin Panel > Functions > OpenClaw Gateway > valves. The value "
                "is `gateway.auth.token` in your OpenClaw config."
            )

        # Device identity
        self._ensure_identity(valves)

        # Connect
        pipe_log(f"Connecting to ws://{host}:{port}")
        # Bounded, and every failure converted to GatewayError. The conversion
        # is the load-bearing part: pipe() only has an `except GatewayError`
        # around this, so anything else escapes the generator and OWUI renders
        # it as an unattributed error naming no valve.
        #
        # websockets already defaults to open_timeout=10 and that covers the TCP
        # connect, so this wait_for is a belt-and-braces bound rather than the
        # only one -- keep _GATEWAY_CONNECT_TIMEOUT_S and that default in step
        # if you tune either.
        try:
            ws = await asyncio.wait_for(
                websockets.connect(f"ws://{host}:{port}", ping_interval=None,
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
            # WebSocketException is NOT an OSError subclass, so catching OSError
            # alone still let InvalidStatus/InvalidHandshake escape -- which is
            # exactly what happens when something answers but isn't a Gateway
            # (a reverse proxy returning 502 on the upgrade, an HTTP server on
            # the port). That is a configuration mistake, and it belongs in the
            # chat naming the valve, not as an unattributed traceback.
            raise GatewayError(
                f"Could not connect to the OpenClaw Gateway at {host}:{port} "
                f"({type(ex).__name__}: {ex}). Is the Gateway running, and is "
                "the GATEWAY_URL valve pointing at it?"
            )

        # Handshake: receive challenge
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
                            platform="linux", mode="cli"),
                role="operator",
                scopes=self.scopes,
                auth=auth,
                device=signed,
                locale="en-US",
                userAgent="openclaw-owui-pipe/1.0",
                caps=["tool-events"]
            )
        )))

        resp = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if not resp.get("ok"):
            err = resp.get("error")
            raise GatewayError(
                _explain_connect_rejection(
                    str((err or {}).get("message", "connect failed")),
                    device_id=(self._ident or {}).get("id"),
                ),
                error=err if isinstance(err, dict) else None,
            )

        # Capture device token for future reconnects
        hello_ok = resp.get("payload", {})
        if hello_ok.get("auth", {}).get("deviceToken"):
            self._device_token = hello_ok["auth"]["deviceToken"]
            self._save_device_token(valves)
            pipe_log(f"Device token captured: {self._device_token[:20]}...")

        self._ws = ws
        self._frame_seq = None  # frame seq counts per socket
        tick_ms = (hello_ok.get("policy") or {}).get("tickIntervalMs")
        if isinstance(tick_ms, (int, float)) and tick_ms > 0:
            self._tick_interval_s = tick_ms / 1000
        self._reconnect_attempt = 0
        self.connection_epoch += 1
        self._thinking_note_shown.clear()
        self.gateway_version = (hello_ok.get("server") or {}).get("version")
        features = hello_ok.get("features") or {}
        self.gateway_methods = frozenset(features.get("methods") or ())
        self.gateway_events = frozenset(features.get("events") or ())
        self._note_gateway_boot((hello_ok.get("server") or {}).get("bootId"))
        pipe_log(
            f"Connected to Gateway (persistent) [epoch {self.connection_epoch}]"
        )
        # Subscriptions belong to the socket. Their requests resolve on the
        # event loop, which is not reading yet here, so it renews them. A
        # requests-only connection has none: the main connection renders.
        self._subscriptions_stale = not self.requests_only
        # NOTE: does not start/spawn the event-loop task -- that happens
        # exactly once, in `ensure_connected`. This method is also called
        # from inside `_reconnect`, which runs from within the event-loop
        # task itself; spawning another task here would duplicate it.

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

        # The DEVICE_IDENTITY valve names the main identity only; a restricted
        # connection always has its own.
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
        """Background task: reads WS messages and dispatches them.

        This is the ONLY task that ever calls `self._ws.recv()`. Reconnects
        happen in place (`self._ws = None`, then `_reconnect()` blocks until
        a new socket is ready) rather than by spawning a second event-loop
        task: two tasks would race on `self._ws.recv()`, each collision would
        raise, and each of *those* would spawn yet another task, a runaway
        reconnect storm.
        """
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

                # ── Dropped frames ──
                # Event frames carry a per-socket `seq`; the Gateway advances it
                # without sending when this client is slow and the event is
                # dropIfSlow (server-start-BB-IxTAg.mjs:3205-3207), and says so
                # nowhere else. A dropped frame may be a run's text (repaired
                # like a reconnect gap) or a settle push (open writers re-read
                # their session rows).
                _seq = msg.get("seq")
                if msg.get("type") == "event" and isinstance(_seq, int):
                    if self._frame_seq is not None and _seq != self._frame_seq + 1:
                        pipe_log(f"  frame seq gap: expected {self._frame_seq + 1}, got {_seq}")
                        self._mark_event_gap()
                        if _TURNS:
                            spawn_background(_TURNS.resync(self))
                    self._frame_seq = _seq

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
                                    or "request failed",
                                    error=err if isinstance(err, dict) else None,
                                )
                            )
                        else:
                            fut.set_result(msg.get("payload", {}))
                    continue

                # ── Tick keepalive (silently consume) ──
                if msg.get("event") == "tick" or msg.get("payload", {}).get("isHeartbeat"):
                    if _TURNS:
                        _TURNS.note_heartbeat(self, msg.get("payload") or {})
                    continue

                # ── Event dispatch ──
                # On tool events:
                #
                #   * The connection that STARTS a run gets its tool activity
                #     as `agent` events with payload.stream == "tool".
                #   * Any connection subscribed to the session with
                #     `sessions.messages.subscribe` gets the same payloads as
                #     `session.tool` events, whoever started the run (and no
                #     `agent` tool events). That covers this connection after a reconnect,
                #     and runs the Gateway starts itself (ELI-92).
                #
                # Both are folded into one path below, first copy wins.
                if msg.get("type") == "event" and msg.get("event") == "sessions.changed":
                    self._on_sessions_changed(msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") == "shutdown":
                    self._on_shutdown(msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") in ("question.requested",
                                                                     "question.resolved"):
                    if _QUESTION_HOOKS and self.writes_turns:
                        seen, resolved = _QUESTION_HOOKS
                        if msg["event"] == "question.requested":
                            seen(self, msg.get("payload") or {})
                        else:
                            resolved(msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") == "session.message":
                    self._on_session_message(msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") == "session.tool":
                    # Same payload as an `agent` tool event (stream "tool",
                    # data.phase/name/toolCallId/args/result), so it takes the
                    # same path: inline turn, shadow renderer, turn writer.
                    p = msg.get("payload") or {}
                    msg = {"type": "event", "event": "agent", "payload": {
                        k: p[k] for k in ("runId", "sessionKey", "sessionId", "stream",
                                          "data", "seq", "ts") if k in p}}
                if msg.get("type") == "event" and msg.get("event") == "cron":
                    self.note_cron_event(msg.get("payload") or {})
                    continue
                if msg.get("type") != "event" or \
                   msg.get("event") not in ("agent", "chat"):
                    continue

                payload = msg.get("payload", {})
                if payload.get("stream") == "tool" and not self._first_tool_event(payload):
                    continue
                spawned = self._spawn_seen(payload) if payload.get("stream") == "tool" else None
                if spawned:
                    # The Status dialog's child index (state.py), off the loop.
                    parent, child, task = spawned
                    asyncio.get_running_loop().run_in_executor(None, functools.partial(
                        _subagent_remember, parent, keys=[child], tasks={child: task} if task else None))

                # ── Shadow render ──
                # Feed every event of a run we're tracking into its shadow
                # `_TurnRenderer`, whether or not a live consumer is still
                # attached: the inline turn can end at any point, and the turn
                # writer it hands over to starts from this content.
                _evt_sess = payload.get("sessionKey", "")
                _evt_run = payload.get("runId", "")
                if _evt_sess and _evt_run:
                    _renderer = self._run_renderers.get(f"{_evt_sess}:{_evt_run}")
                    if _renderer is not None:
                        try:
                            _renderer.feed(payload)
                        except Exception as ex:
                            pipe_log(f"  shadow renderer feed failed: {ex}")

                consumers = self.consumers_for_event(payload)

                # ── ELI-100: a run no live request consumes is a turn ──
                if not consumers and _TURNS and _TURNS.route_now(self, payload):
                    continue

                if consumers:
                    if not payload.get("runId"):
                        pipe_log("  dispatched session-only event to sole consumer")
                    if payload.get("state") == "final" and payload.get("sessionKey") and payload.get("runId"):
                        # Genuinely shown to a live tab, which persists it via
                        # OWUI's own client-side flow -- remember this so a
                        # later duplicate/retried final event for the same
                        # run_id never reopens its message.
                        self.mark_delivered_live(payload["sessionKey"], payload["runId"])
                        # The inline turn is alive and took the final itself, so
                        # the parity shadow isn't needed for this run -- drop it.
                        self.unregister_run_target(payload["sessionKey"], payload["runId"])
                    for q in consumers[0].queues:
                        await q.put(msg)
                    continue

                if payload.get("sessionKey") and not payload.get("runId"):
                    pipe_log("  dropped ambiguous or unmatched session-only event")

                # If nothing matches at all, drop it.
                # This efficiently filters cross-session bleed.

            except asyncio.TimeoutError:
                # Not even a tick for two intervals: the socket is dead without
                # having closed (half-open), so every run on it would wait
                # forever. Tear it down and reconnect.
                pipe_log(f"  tick timeout: no frame for {2 * self._tick_interval_s:.0f}s, "
                         "reconnecting")
                transport = getattr(self._ws, "transport", None)
                if transport is not None:
                    transport.abort()
                self._ws = None
                self._mark_event_gap()
            except websockets.exceptions.ConnectionClosed as e:
                pipe_log(
                    "WS disconnected -- reconnecting... "
                    f"(code {getattr(e, 'code', None)}, reason {getattr(e, 'reason', '')!r})"
                )
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
        """`_connect_and_start`, connecting again while a Gateway that has
        just started refuses connections until its startup sidecars run
        ("gateway starting; retry shortly", details.reason "startup-sidecars"),
        after the wait it names, as OpenClaw's own clients do
        (startup-unavailable-D0-EeFjq.mjs, 2026.10.5). Any other failure, or
        one past STARTUP_REFUSAL_WAIT_S, is raised."""
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
        """Reconnect with exponential backoff.

        Blocks (looping in place, in the single `_event_loop` task) until a
        new connection is established or the connection is stopped. Must
        NEVER spawn a new `_event_loop` task -- see the docstring on
        `_event_loop` for why that caused a runaway task/connection storm.
        """
        while not self._stopped:
            self._reconnect_attempt += 1
            delay = min(2 ** (self._reconnect_attempt - 1), self._max_backoff)
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
                # loop and try again

    def _answer_reconnect_waiters(self, error) -> None:
        """Hand a reconnect attempt's outcome to the turns waiting on it."""
        waiters, self._reconnect_waiters = self._reconnect_waiters, []
        self._reconnect_now.clear()   # asked during this attempt: it is the answer
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(error)


# Protocol sentinels (docs/tools/subagents.md:470-431) that mean "no
# user-visible content was produced" -- never real assistant text to show a
# human. An announce-follow-up run can legitimately finish with exactly one
# of these as its only "assistant text", and writing it into a chat leaks
# internal plumbing.
_SILENT_SENTINELS = {"ANNOUNCE_SKIP", "NO_REPLY", "no_reply"}


# How far back a reply looks for its run's own closing message when the
# stream lost text: later runs may already have ended after it.
RUN_TEXT_HISTORY_LIMIT = 60
# Messages to read when rebuilding a whole run from `chat.history`: each tool
# call is a message of its own (measured: 119 calls = 154 messages).
RUN_HISTORY_LIMIT = 600


def _idempotency_keys(message) -> tuple[str, str]:
    """A transcript message's `idempotencyKey`, top level and under
    `__openclaw`, as strings ("" when absent)."""
    if not isinstance(message, dict):
        return "", ""
    meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
    return str(message.get("idempotencyKey") or ""), str(meta.get("idempotencyKey") or "")


def _history_text_for_run(history: dict, run_id: str) -> tuple[bool, str | None]:
    """The closing assistant text one specific run saved, found by its id.

    The Gateway stamps a run's closing assistant message with
    `idempotencyKey = "cli-assistant:<runId>"` (claude-cli runner), so the
    run's own reply is found by identity, not by position. Returns
    (found, text); text is None when the run ended silent.
    """
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


def _history_renderer(messages: list) -> "_TurnRenderer":
    """A `_TurnRenderer` fed one run's `chat.history` messages instead of its
    live events, so a run whose events were lost still ends as the same
    `text` + `output` items (ELI-88/90/92)."""
    r = _TurnRenderer()
    for payload in _history_events(messages):
        r.feed(payload)
    return r


def _history_events(messages: list):
    """`chat.history` messages as the live events that wrote them: the one
    transcript parser, for the chat's rebuild and the sub-agent drawer's run
    (ELI-147).

    claude-cli imports its transcript into the session as it goes: assistant
    messages whose blocks are `text`, `toolcall` {id, name, arguments} and
    `tool_result` {tool_use_id, name, content, is_error}, in order. The run's
    closing message (`cli-assistant:<runId>`) repeats all of its text, so it
    only counts when nothing in its own run carried text (a plain, tool-free
    reply, or imports that carry tool blocks only). It counts in place: an
    asked question's run closes before its answer run starts, so keeping
    only the last closer would lose the question.

    A run on the Gateway's own runtime (GPT, Gemini, DeepSeek) keeps
    OpenClaw's shape: `toolCall` blocks, and each result a `toolResult`
    message {toolCallId, toolName, content, isError} of its own (recorded,
    test_fixture_native_history.json).

    Text after a tool call is joined to the text before it with the separator
    the Gateway's live stream inserts there (`sawToolUseSinceText`), so the
    rebuild equals what streamed (it streams as "\n\n<text>"), and a
    preamble is not glued to the answer."""
    text = ""                 # what the events so far said
    run_text = False          # this run carried text before its closer
    tool_since_text = False   # the Gateway's `sawToolUseSinceText`
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
                lead = _message_boundary_separator(text, block["text"]) if tool_since_text else ""
                text, tool_since_text = text + lead + block["text"], False
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


def _history_tool_result(tool_call_id: str, name, content, is_error) -> dict:
    return {"stream": "tool", "data": {
        "phase": "result", "name": name or "", "toolCallId": tool_call_id,
        "result": "" if content is None else content, "isError": is_error is True}}


def _message_boundary_separator(previous: str, nxt: str) -> str:
    """The newlines the Gateway inserts between two assistant messages of a
    claude-cli run (`missingMessageBoundarySeparator`,
    cli-live-session-registry-CS2DCATp.mjs:375 on 2026.9.3): enough for a
    blank line, counting the newlines both sides already have."""
    if not previous:
        return ""
    trailing = len(previous[-2:]) - len(previous[-2:].rstrip("\n"))
    leading = len(nxt[:2]) - len(nxt[:2].lstrip("\n"))
    return "\n" * max(0, 2 - trailing - leading)


def _is_closing_message(message) -> bool:
    """A run's closing assistant message (`cli-assistant:<runId>`)."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False
    return any(k.startswith("cli-assistant:") for k in _idempotency_keys(message))


def _run_closed_by(message) -> str | None:
    """The run a `chat.history` message closes, or None. A claude-cli run
    closes with its `cli-assistant:<runId>` message. A run on the Gateway's
    own runtime has no such message: every message it writes carries
    `__openclaw.runId` (the `chat.send` run id), and
    its last is an assistant message that did not stop for a tool call."""
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
    """Walk down `childrenIds` from `start_id` to the deepest childless node.

    OWUI's `history.currentId` is only a *view* pointer: the frontend (or an
    interleaved live turn) can reset it back to an ancestor that already has
    children -- e.g. right after an agent turn was appended. Anchoring a
    new message directly on `currentId` in that state appends it as
    a *sibling* of the existing child, which OWUI renders as a 1/2·2/2 variant
    group instead of a linear message. Walking to the true leaf (following the most-recently-appended
    child at each branch) makes every turn writer chain off the real tail.
    Cycle-guarded and depth-capped for safety against malformed history.
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
    """A forwarded user turn as `chat.history` shows it since OpenClaw
    2026.10.2: role "assistant", "Forwarded from ...", keyed as the admitted
    turn `<id>:user`, with the provenance the Gateway projects
    (chat-display-projection.helpers-pbUdgTgb.mjs `hasForwardedMessageProvenance`:
    a `sessions_send` message, or a cron run's prompt)."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False
    key = next((k for k in _idempotency_keys(message) if k), "")
    prov = message.get("provenance") if isinstance(message.get("provenance"), dict) else {}
    forwarded = ((prov.get("kind"), prov.get("sourceTool")) == ("inter_session", "sessions_send")
                 or (prov.get("kind"), prov.get("sourceTool")) == ("internal_system", "cron"))
    return key.endswith(":user") and len(key) > 5 and forwarded


def _transcript_roles(messages: list) -> list:
    """`chat.history` messages with each forwarded user turn back in role
    "user", as the transcript holds it. Read as a reply, a cron wake's prompt
    and its scheduled-run preamble became the wake message's text when a
    claude-cli run was rebuilt from the transcript, and the run it started
    was not found (GH #24, OpenClaw 2026.10.2+). Other messages are kept."""
    return [{**m, "role": "user"} if _is_forwarded_turn(m) else m for m in messages]


def _history_user_run_id(message) -> str | None:
    """The run a transcript user message started: the Gateway keys the user
    message a `chat.send` adds as `<runId>:user` (the run id is the send's
    idempotencyKey). None for any other message, including the copy the
    runtime imports next to it."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    key = next((k for k in _idempotency_keys(message) if k), "")
    return key[:-len(":user")] if key.endswith(":user") and len(key) > 5 else None


# Read-only shim (ELI-100): bridge versions before ELI-100 embedded this in
# the message they wrote for a finished sub-agent task, invisible in rendered
# markdown (an HTML comment) but still present in the raw `content` OWUI
# hands the Action endpoint on click, so old messages keep opening -- the
# one piece of information a toolbar button click doesn't otherwise carry
# (OWUI sends chat_id/message_id/model/content, never a custom taskId).
# `_run_subagent_detail`-style consumers parse it back out with
# `_SUBAGENT_TASK_ID_MARKER_RE` to open straight into that task's drawer
# instead of the general status dialog.
_SUBAGENT_TASK_ID_MARKER_RE = re.compile(r"<!--\s*openclaw:taskId=([^\s>]+?)\s*-->")


# Module-level singleton
_gateway_connection: _GatewayConnection | None = None
_gateway_init_lock = asyncio.Lock()

# Anchor for finding a connection left behind by a *previous* deploy of this
# same function (OWUI's function loader (open_webui/utils/plugin.py)
# execs each redeploy into a brand-new module object with no teardown hook on
# the old one -- a module-level singleton alone resets every deploy and orphans
# the old module's WS/event-loop task forever). Stashing it as an attribute on
# `open_webui.socket.main` -- OWUI's own stable module, never reloaded by our
# function -- lets the next deploy find and `disconnect()` the previous one
# before opening a new connection, so redeploys self-heal without a container
# restart. Falls back to a no-op when not running inside OWUI (e.g. unit tests).
_STALE_CONN_ATTR = "_openclaw_gateway_connection_v1"


def _function_module_prefix(module_name: str) -> str:
    """`function_<id>_<hex>` -> `function_<id>`. OWUI 0.12 names every load of
    a Function this way (utils/plugin.py `load_function_module_by_id`), so the
    prefix tells "an older load of this same Function" apart from the Status
    Action, whose connection must be left alone."""
    head, sep, tail = module_name.rpartition("_")
    if sep and len(tail) == 32 and all(c in "0123456789abcdef" for c in tail):
        return head
    return module_name


def _orphaned_gateway_connections(current) -> list:
    """Live `_GatewayConnection`s created by OLDER loads of this Function.

    The single stashed attribute only ever named the previous deploy's
    connection; one that an in-flight run revived after being reaped dropped
    out of it and lived on. Scanning the heap is the only handle on those.
    """
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


async def _retire_stale_connection(stale) -> None:
    retire = getattr(stale, "retire", None)
    if retire is not None:
        await asyncio.wait_for(retire(), timeout=5)
        return
    # Built before `retire()` existed: its own ensure_connected would revive
    # it, so pin both entry points shut on the instance before closing it.
    async def _refuse(*_a, **_k):
        raise GatewayError("gateway connection retired by a newer deploy")
    stale._stopped = True
    stale.ensure_connected = _refuse
    stale._connect_and_start = _refuse
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
        await _reap_stale_gateway_connection()
        conn = _GatewayConnection(valves_getter)
        await conn.ensure_connected()
        _gateway_connection = conn
        _remember_gateway_connection(conn)
        return conn


# ---------------------------------------------------------------------------
# Owner users (OWNER_USERS valve)
# ---------------------------------------------------------------------------
# The main connection holds operator.admin, so every turn it sends is the
# owner's and gets owner-only tools. For a chat whose user is not in
# OWNER_USERS, every request that starts, steers or ends a run goes over a
# second connection with RESTRICTED_SCOPES and its own device identity. Reading
# and rendering stay on the main connection: they do not make a turn the
# owner's, and the main connection remains the only one that writes into Open
# WebUI (ELI-100).

_restricted_connection: "_GatewayConnection | None" = None
_restricted_init_lock = asyncio.Lock()


async def _get_restricted_connection(valves_getter) -> "_GatewayConnection":
    """The connection for non-owner users' run requests: read + write scopes,
    identity `identity-restricted.json`, requests only."""
    global _restricted_connection
    async with _restricted_init_lock:
        if _restricted_connection is None:
            conn = _GatewayConnection(valves_getter, writes_turns=False, scopes=RESTRICTED_SCOPES,
                                      identity_name="identity-restricted", requests_only=True)
            await conn.ensure_connected()
            _restricted_connection = conn
            return conn
    await _restricted_connection.ensure_connected()
    return _restricted_connection


async def _owui_call(fn, *args):
    out = fn(*args)
    return await out if asyncio.iscoroutine(out) else out


async def _owner_users_setting(conn) -> "str | None":
    """OWNER_USERS as configured, or None when it cannot be read. The Pipe's
    connection carries the Pipe's valves; the Status Action's own connection
    does not, so the setting is read from the Pipe function's saved valves."""
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
    """Whether a run request for `session_key` may go out as the owner's. An
    empty OWNER_USERS keeps the old behaviour (everyone is). Otherwise the
    chat's user (from the session key, with that user's email) must be on the
    list; anything that cannot be decided is not the owner's."""
    raw = await _owner_users_setting(conn)
    if raw is None:
        return False
    owners = _parse_owner_users(raw)
    if not owners:
        return True
    m = _OWUI_KEY_USER.match(session_key or "")
    if not m:
        return False
    user_id = m.group(1)
    if user_id.lower() in owners:
        return True
    return _is_owner_user(owners, user_id, await _user_email(user_id))


def _non_owner_send_params(params: dict) -> dict:
    """A chat.send the restricted connection may make: without the admin-only
    fields, and with the conversation metadata block at the head of the
    message text, which is how the Gateway hands a receipt to the agent
    (`receipt + "\\n\\n" + message`). The block keeps its "untrusted" label,
    and ownership never comes from it: the Gateway decides that from the
    sending connection. A command ("/stop") goes as typed, since the Gateway
    reads commands from the raw text."""
    dropped = [f for f in ADMIN_ONLY_SEND_FIELDS if f in params]
    if not dropped:
        return params
    out = {k: v for k, v in params.items() if k not in dropped}
    receipt = params.get("systemProvenanceReceipt")
    message = out.get("message") or ""
    moved = isinstance(receipt, str) and bool(receipt) and not message.lstrip().startswith("/")
    if moved:
        out["message"] = "\n\n".join(p for p in (receipt, message) if p)
    pipe_log(f"  non-owner chat.send: left out admin-only fields {dropped}"
             + ("; metadata block moved into the message" if moved else ""))
    return out


async def _run_request(conn, method: str, params: dict, *, timeout: float = 10,
                       owner: "bool | None" = None, wait: bool = True):
    """Send a request that starts, steers or ends a run (`RUN_SEND`,
    `RUN_ABORT`, `RUN_PATCH`) on the connection the chat's owner status calls
    for: `conn` for an owner, the restricted connection otherwise. `owner`
    overrides the decision from the session key (a title run, whose session
    is not the chat's). `wait=False` sends without waiting for the answer."""
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
    return await _send_when_prepared(target, method, params, timeout=timeout)


async def _send_when_prepared(conn, method: str, params: dict, *, timeout: float = 10):
    """`conn.send_request`, sent again while the Gateway refuses it only
    because it is still preparing the agent after its start, after the wait
    each refusal names, up to STARTUP_REFUSAL_WAIT_S."""
    deadline = time.monotonic() + STARTUP_REFUSAL_WAIT_S
    told = False
    while True:
        try:
            return await conn.send_request(method, params, timeout=timeout)
        except GatewayError as ex:
            retry_ms = _startup_refusal_retry_ms(ex)
            if retry_ms is None or time.monotonic() + retry_ms / 1000 > deadline:
                raise
            if not told:
                told = True
                pipe_log(f"  {method}: the Gateway is still preparing the agent after its start; "
                         f"sending again as it asks (every {retry_ms} ms, up to {STARTUP_REFUSAL_WAIT_S} s)")
            await asyncio.sleep(retry_ms / 1000)


# A Gateway that has just started refuses runs until it has inspected and
# prepared the agent's database (2026.10.5 and later): `UNAVAILABLE`,
# `retryable`, `retryAfterMs`, details.code below. The refusal comes before
# the request's ack and is not kept in the idempotency cache ("client-retry",
# chat-send-handler-o4Nr7Ej0.mjs:340/428), so the same request, same key, is
# sent again after the time it names. No event marks the end of preparation.
STARTUP_REFUSAL_CODE = "agent-database-inspection-pending"
STARTUP_REFUSAL_WAIT_S = 300


def _gateway_starting_retry_ms(ex: GatewayError) -> int | None:
    """The wait before connecting again to a Gateway that refused the connect
    only because it is still starting, bounded as its own clients bound it
    (resolveGatewayStartupRetryAfterMs: 100 to 2000 ms, 500 by default)."""
    err = getattr(ex, "error", None) or {}
    details = err.get("details") if isinstance(err.get("details"), dict) else {}
    if err.get("code") != "UNAVAILABLE" or err.get("retryable") is not True \
            or details.get("reason") != "startup-sidecars":
        return None
    retry = err.get("retryAfterMs")
    retry = int(retry) if isinstance(retry, (int, float)) else 500
    return min(max(retry, 100), 2000)


def _startup_refusal_retry_ms(ex: GatewayError) -> int | None:
    """The wait the Gateway asks for before sending a refused request again,
    when it refused it only because it is still preparing after its start."""
    err = getattr(ex, "error", None) or {}
    details = err.get("details") if isinstance(err.get("details"), dict) else {}
    if not err.get("retryable") or details.get("code") != STARTUP_REFUSAL_CODE:
        return None
    retry = err.get("retryAfterMs")
    return int(retry) if isinstance(retry, (int, float)) and retry > 0 else 250
