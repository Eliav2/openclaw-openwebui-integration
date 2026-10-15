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
    """Raised when the Gateway WebSocket handshake fails."""
    pass


# How long to wait for the Gateway's TCP/WS accept before giving up. Mirrors
# websockets' own open_timeout default; kept explicit so the bound is visible
# and tunable in one place rather than inherited silently.
_GATEWAY_CONNECT_TIMEOUT_S = 10.0

# Largest Gateway frame the socket accepts. websockets defaults to 1 MiB and
# answers a bigger frame by closing the WHOLE connection (1009, "message too
# big"), dropping every session's events: a 952-message `chat.history` is
# 3.1 MB, and one large tool result in an agent or `session.message` event is
# enough (measured 2026-10-12, repro_ws_max_size.py). Bounded, not None, so a
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


def _preview_recovery_text(preview: dict, session_key: str, user_text: str) -> str | None:
    """Return assistant text saved after this user message, if preview has it.

    Lossy: the Gateway clips every `sessions.preview` item server-side (800
    chars on 2026.9.x, 2000 before) whatever `maxChars` asks for. Only the
    fallback for when `_history_recovery_text` finds nothing (ELI-74).
    """
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

    return _recovery_text_from_items(entry.get("items"), user_text)


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
    """Flatten one `chat.history` message to the {role, text} shape of a
    `sessions.preview` item. Only `text` blocks count: tool calls, tool results
    and thinking blocks are not part of the visible answer."""
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
    a `chat.history` response.

    This replaces `sessions.preview` as the recovery source (ELI-74): the
    preview is clipped server-side, so a long reply recovered from it landed
    in OWUI cut mid-sentence with a trailing "...". `chat.history` returns the
    message whole.
    """
    messages = (history or {}).get("messages")
    if not isinstance(messages, list):
        return None
    return _recovery_text_from_items([_history_item(m) for m in messages], user_text)


def _recovery_user_text_matches(item_text: str, user_text: str) -> bool:
    """True if a stored user message is the one this turn sent.

    The transcript can hold it verbatim or behind the Gateway's metadata
    envelope ("Conversation info (untrusted metadata): ...", then a blank
    line, then the text). `sessions.preview` strips that envelope;
    `chat.history` does not.
    """
    return item_text == user_text or item_text.endswith("\n" + user_text)


def _recovery_text_from_items(items, user_text: str) -> str | None:
    """The turn's final assistant text: the LAST non-empty assistant item after
    the last user item matching `user_text`, stopping at the next user message.

    Last, not first: a tool-using turn stores several assistant messages. On
    the claude-cli runtime the closing one repeats the whole turn's text
    (preambles included); on the embedded runtime it is the final answer. The
    first one is only the opening preamble ("I'll look at ..."), which is what
    recovery used to return.
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
    """Read back the assistant text of a turn whose live stream was lost.

    `chat.history` first, because it returns the message whole. The old
    `sessions.preview`-only path persisted long replies clipped mid-sentence
    with a trailing "..." (ELI-74): the Gateway caps each preview item
    server-side, so no `maxChars` value fixes it. The preview stays as a
    fallback for a Gateway that refuses `chat.history`.
    """
    try:
        history = await conn.session_history(session_key)
        text = _history_recovery_text(history, user_text)
        if text:
            return text
        pipe_log("  history recovery found no reply; trying sessions.preview")
    except Exception as ex:
        pipe_log(f"  history recovery failed: {ex}; trying sessions.preview")
    try:
        preview = await conn.session_preview(session_key)
        return _preview_recovery_text(preview, session_key, user_text)
    except Exception as ex:
        pipe_log(f"  preview recovery failed: {ex}")
        return None


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
    like a label), so `thinkingOptions` must never be used for the ladder --
    checking it first, as this used to, let an unsupported level fall
    through the clamp and reach the gateway's own hard validation error
    (ELI-85 field mixup, caused a live chat to hard-fail on every turn).
    Kept only as a last-resort fallback in case a future gateway build ever
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
      must be exact. Clamping against the union is what let `high` reach a
      model whose only level is `off`.

    `authoritative` marks a ladder the Gateway stated itself by rejecting a
    send. A merely observed one (from `sessions.describe`) must never overwrite
    it: describe resolves the ladder with no model catalog in scope and so
    reports the generic base profile for catalog-gated models. Trusting the
    weaker source second would undo the fix on the very next turn.

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
    (P39) that includes at least one image_url block. `content` is a
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
    (P39) and shape them for the gateway's `chat.send` `attachments` param:
    `{type, mimeType, fileName, content}` with `content` as base64.

    OWUI sends attached/pasted images as `image_url` blocks whose `url` is
    a `data:<mime>;base64,<data>` URI (confirmed via OWUI's own request
    shape, not just this bridge's tests) -- there is no plain-HTTP(S)
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
# waiting for its first event why (review 2af1 timers).
# `read_at` (time.monotonic) is when the row was read: a reply trusts an idle
# row only when it was read after its run was admitted (review 2af1 N4).
_ROW_MARKER_TYPE = "_pipe_row"
# Text on each side of a gap used to find where a repaired chunk goes.
_GAP_ANCHOR_CHARS = 120


def _align_whole_text(whole: str, received: str, gaps) -> tuple[str, list] | None:
    """Line up what a run actually sent (`whole`) with what reached us.

    `received` is the run's text as it arrived, `gaps` the offsets in it
    where a gap marker sat (events lost on a socket drop). Between gaps the
    received text is contiguous, so each piece is found in `whole` in order;
    whatever `whole` has between pieces is what was lost there. Returns
    (tail, missing): `tail` is what follows the last received piece (new
    text), `missing` a list of (offset in `received`, lost text). None when
    a piece is not in `whole`: then it is not the same text and nothing is
    repaired.
    """
    if not whole or not received:
        return None
    cuts = sorted({g for g in gaps or () if 0 <= g <= len(received)})
    bounds = [0, *cuts, len(received)]
    j = 0
    missing = []
    for a, b in zip(bounds, bounds[1:]):
        piece = received[a:b]
        if not piece:
            continue
        pos = whole.find(piece, j)
        if pos < 0:
            return None
        if pos > j:
            missing.append((a, whole[j:pos]))
        j = pos + len(piece)
    return whole[j:], missing


def _insert_at_anchor(text: str, chunk: str, before: str, after: str) -> str | None:
    """`text` with `chunk` put in front of `before`, or right after `after`."""
    if before:
        i = text.find(before)
        if i >= 0:
            return text[:i] + chunk + text[i:]
    if after:
        i = text.rfind(after)
        if i >= 0:
            i += len(after)
            return text[:i] + chunk + text[i:]
    return None


def _gap_repair_fields(message: dict, chunk: str, before: str, after: str) -> dict | None:
    """The DB fields that put `chunk` back into an OWUI message, or None.

    OWUI 0.12 renders and saves a reply from its `output` items (the content
    string is derived), so the chunk goes into the text part that holds the
    anchor; `content` is kept in step when present.
    """
    fields: dict = {}
    output = copy.deepcopy(message.get("output") or [])
    placed = False
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                new = _insert_at_anchor(part.get("text") or "", chunk, before, after)
                if new is not None:
                    part["text"] = new
                    placed = True
                    break
        if placed:
            break
    if placed:
        fields["output"] = output
    content = message.get("content")
    if isinstance(content, str) and content:
        new_content = _insert_at_anchor(content, chunk, before, after)
        if new_content is not None:
            fields["content"] = new_content
    if output and not placed:
        return None  # the screen renders `output`; a content-only fix would not show
    return fields or None


def _gap_repair_registry() -> dict:
    """Repairs waiting for a reply that is still streaming, by
    "<chat_id>:<message_id>". Shared across redeploys like the other
    bookkeeping, so a repair registered by one module instance is applied by
    the sink whichever instance is current."""
    state = _shared_gateway_state()
    if state is None:
        return _LOCAL_GAP_REPAIRS
    return state.setdefault("pending_gap_repairs", {})


_LOCAL_GAP_REPAIRS: dict = {}


async def _apply_gap_repair(conn, repair: dict) -> bool:
    """Put a lost chunk back into a reply OWUI has finished saving, in the DB
    and in every open tab (a `chat:completion` carrying only `output` replaces
    that one message's items, without a reload and without marking it done).
    """
    chat_id, message_id = repair["chat_id"], repair["message_id"]
    try:
        from open_webui.models.chats import Chats
    except Exception as ex:
        pipe_log(f"  gap repair unavailable (not inside OWUI process?): {ex}")
        return False
    try:
        async with conn._chat_write_lock(chat_id):
            chat = await Chats.get_chat_by_id(chat_id)
            messages = ((chat.chat.get("history") or {}).get("messages") or {}) if chat else {}
            message = messages.get(message_id)
            if not message:
                pipe_log(f"  gap repair: message {message_id[:8]}... not found; "
                         f"{len(repair['chunk'])} chars stay missing")
                return False
            fields = _gap_repair_fields(message, repair["chunk"],
                                        repair.get("before", ""), repair.get("after", ""))
            if not fields:
                pipe_log(f"  gap repair: no anchor in message {message_id[:8]}...; "
                         f"{len(repair['chunk'])} chars stay missing")
                return False
            await Chats.upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, fields)
        live = {"output": fields["output"]} if "output" in fields else {"content": fields["content"]}
        await _relay_emit_event(repair["user_id"], chat_id, message_id,
                                {"type": "chat:completion", "data": live})
        pipe_log(f"  gap repair: put {len(repair['chunk'])} lost chars back into "
                 f"message {message_id[:8]}...")
        return True
    except Exception as ex:
        pipe_log(f"  gap repair failed: {ex}")
        return False


def _schedule_gap_repair(conn, repair: dict, *, streaming: bool) -> None:
    """Apply now, or once OWUI has saved the reply that is still streaming.

    A reply that stopped streaming is already saved: a finished one before
    `chat.finished`, a stopped one before OWUI's stop endpoint returns (it
    awaits the cancelled task), which is before the next reply could start.
    The one still streaming is saved from its `output` when the stream ends,
    which would overwrite a repair made now, so that repair waits for
    `chat.finished` (`_GapRepairSink`) or for the reply that adopts its run.
    """
    if not streaming:
        spawn_background(_apply_gap_repair(conn, repair))
        return
    key = f"{repair['chat_id']}:{repair['message_id']}"
    _gap_repair_registry().setdefault(key, []).append(repair)
    _ensure_gap_repair_sink()
    pipe_log(f"  gap repair for message {repair['message_id'][:8]}... waits for its final save")


def _take_gap_repairs(chat_id: str, message_id: str) -> list:
    return _gap_repair_registry().pop(f"{chat_id}:{message_id}", [])


async def _on_chat_finished(data: dict) -> None:
    repairs = _take_gap_repairs(str(data.get("chat_id")), str(data.get("message_id")))
    if not repairs:
        return
    conn = _gateway_connection
    if conn is None:
        return
    for repair in repairs:
        await _apply_gap_repair(conn, repair)


class _GapRepairSink:
    """OWUI event sink: `chat.finished` fires after the reply's final save
    (`publish_chat_finished_event`, after the upsert in middleware.py). It
    stays registered across redeploys, so it calls whichever handler the
    current module instance stored, never its own (stale) code."""

    openclaw_gap_repair_sink = True

    async def handle_event(self, app, event, request=None) -> None:
        if getattr(event, "event", None) != "chat.finished":
            return
        state = _shared_gateway_state() or {}
        handler = state.get("gap_repair_on_finished") or _on_chat_finished
        try:
            await handler(getattr(event, "data", None) or {})
        except Exception as ex:
            pipe_log(f"  gap repair sink failed: {ex}")


def _ensure_gap_repair_sink() -> None:
    try:
        import open_webui.events as owui_events
    except Exception:
        return
    state = _shared_gateway_state()
    if state is not None:
        state["gap_repair_on_finished"] = _on_chat_finished
    sinks = getattr(owui_events, "EVENT_SINKS", None)
    if isinstance(sinks, list) and not any(
        getattr(s, "openclaw_gap_repair_sink", False) for s in sinks
    ):
        sinks.append(_GapRepairSink())


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
    # reconnecting): only what follows that tail is new. Returning the whole
    # item here posted a second full copy of a 4000-line reply (2026-10-12).
    # The missing piece is put back by the gap repair, not appended here.
    # Shorter anchors too: the hole may sit inside the longest one.
    for n in (_WHOLE_TEXT_ANCHOR_CHARS, 128, _MIN_WHOLE_TEXT_ANCHOR_CHARS):
        anchor = baseline[-n:]
        if len(anchor) < _MIN_WHOLE_TEXT_ANCHOR_CHARS:
            break
        pos = item_text.rfind(anchor)
        if pos >= 0:
            return item_text[pos + len(anchor):]
    return item_text


def _catch_all_delta(raw_text: str, received_text: str, visible_text: str) -> str:
    """New text carried by a cumulative catch-all assistant event.

    Some providers end a turn with an event that has no `delta` but a `text`
    field holding the WHOLE reply. It has to be diffed against what already
    arrived, or the entire message is treated as new.

    The diff is against `received_text`, everything the gateway sent us, and
    not only against what was shown. Those two differ: text held back by the
    ask-user buffer was received but never yielded. Diffing against shown text
    alone made the final catch-all look entirely new, so the whole needs-input
    block was appended to that buffer a second time and the dialog rendered the
    prompt twice with double the options (ELI-80). The visible-text pass stays
    as a second net for the drift case it was originally written for (P27).
    """
    if not raw_text:
        return ""
    delta = _item_delta_text(raw_text, "", received_text)
    return _suppress_already_shown(delta, visible_text)


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
    which then gets appended a second time with no separator (P27,
    reproduced live 2026-07-10).

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

    That was invisible for `arguments` (always `json.dumps`-ed, so its newlines
    are already the two-char `\\n` escape) but hit almost every multi-line tool
    `result`, which is passed through raw (2026-07-25).

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
    append-only for the rest of the turn (see the two-phase `<details>` attempt
    that this replaced). The frontend renders a `function_call` with no matching
    `function_call_output` as a spinner (`structuredOutput.ts::buildToolCallToken`).

    Ordering is by arrival: the item goes at the end of the output list, after
    the text streamed before it. It says so with `output_index`
    (`_OUTPUT_APPEND_INDEX`): the backend appends an item without one, but
    the chat tab's reducer (`structuredOutput.ts::applyResponseStreamEvent`)
    splices it in BEFORE the last item, so the live card showed above the
    text written before the call until a reload (ELI-122).

    The tool call id goes in `id`, and `call_id` is deliberately ABSENT. OWUI
    0.11.0 appended every added item unconditionally, so the obvious shape
    (`id="fc_<tid>"` + `call_id="<tid>"`) was safe. 0.11.1 rewrote that branch
    to dedup first:

        existing_index = next(idx for idx, existing in enumerate(new_output)
            if (item['id'] and existing['id'] == item['id'])
            or (item['call_id'] and existing['call_id'] == item['call_id']))
        if existing_index is not None: new_output[existing_index] = item

    -- so the `function_call_output` we send next, which must share the call
    id to pair, matched this item on `call_id` and REPLACED it. The card is
    built from the `function_call` item alone (`structuredOutput.ts`: `Ye()`
    reads `call_id ?? id` and looks the output up in a by-call_id map), so
    losing it means no card at all: no chevron, no Output, nothing. Every
    chat from the 0.11.1 upgrade onward persisted `fc=0 fco=N`.

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
    not. The `response.name.done` used before only reached the backend, and
    only its last item: the tab ignores that event, so the ❌ appeared when
    the reply finished (ELI-124), and a call followed by anything else got
    the banner alone. (`response.output_item.done` replaces by position in
    the backend, which the pipe cannot know.)
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

    Purpose (2026-07-19; handover since ELI-100): when the inline pipe generator ends BEFORE a
    long run finishes (idle self-close, cancel, or a torn-down request), the
    tail used to survive only as a truncated, tool-block-less *detached*
    bubble read from a length-capped `sessions.preview`. This renderer instead
    accumulates the COMPLETE content (assistant text + every tool block, in
    order) straight from the same events the persistent connection already
    dispatches, so the run's ORIGINAL OWUI message can be finalized to exactly
    what a non-interrupted turn would have stored.

    Scope: assistant text deltas, item-carried text, and tool-result blocks.
    It holds the raw run text: the turn writer shows a resolved `MEDIA:` image
    and an answered ask-user card through the rewrites the inline reply
    recorded (`note_turn_rewrite`, review 2af1 R4).
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
        self._tool_items: dict[str, dict] = {}
        self._tool_results: set[str] = set()
        # A new run continues the message (resume, recovery, answer run): its
        # first text gets the break the live stream puts between messages.
        # The Gateway inserts it only inside one run (graceful window
        # 2026-10-14: "...foreground.USER-DONE" across the resume).
        self.run_boundary = False

    def _add(self, chunk: str) -> None:
        if chunk:
            self.visible_text += chunk

    def _add_text(self, chunk: str) -> None:
        if not chunk:
            return
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

    def _tool_item(self, name: str, tcid: str, args_str: str) -> dict:
        item = self._tool_items.get(tcid)
        if item is None:
            item = _tool_call_started_event(name, tcid, args_str)["item"]
            self._tool_items[tcid] = item
            self.output.append(item)
        return item

    def finished_output(self) -> list[dict]:
        """`output` as stored once the run is over: OWUI marks every item
        still in progress completed before its final write, and so do we."""
        out = copy.deepcopy(self.output)
        for item in out:
            if item.get("status") == "in_progress":
                item["status"] = "completed"
        return out

    def feed(self, payload: dict) -> None:
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
                delta = _suppress_already_shown(delta, self.visible_text)
            if delta:
                if (
                    "Sender (untrusted metadata)" in delta
                    or "UnTrustedMetadata" in delta
                ):
                    return
                self._assistant_stream_text += delta
                self._add_text(delta)

        if stream == "item":
            item_text = _item_assistant_text(data)
            if item_text:
                item_delta = _item_delta_text(
                    item_text, self._last_item_text, self._assistant_stream_text
                )
                self._last_item_text = item_text
                item_delta = _suppress_already_shown(item_delta, self.visible_text)
                self._add_text(item_delta)

        if stream == "tool":
            tcid = data.get("toolCallId", "")
            if phase == "start":
                if tcid:
                    args_str = json.dumps(data.get("args", {}))
                    self._active_tool_args[tcid] = args_str
                    self._tool_item(name, tcid, args_str)
            elif phase == "result":
                if tcid and tcid in self._tool_results:
                    return
                result = data.get("result", {})
                result_str = _tool_result_text(result)
                args_str = self._active_tool_args.pop(tcid, None) or json.dumps(
                    data.get("args", {})
                )
                self._add(_render_tool_result_block(
                    name, tcid, args_str, result_str, data.get("meta", "")
                ))
                if tcid:
                    self._tool_results.add(tcid)
                    item = self._tool_item(name, tcid, args_str)
                    item["status"] = "completed"
                    if data.get("isError") is True:
                        item["name"] = f"{name} {TOOL_ERROR_MARK}"
                        result_str = _tool_error_banner(result_str)
                    self.output.append(_tool_call_result_event(tcid, result_str)["item"])


# ---------------------------------------------------------------------------
# Persistent Gateway Connection (singleton)
# ---------------------------------------------------------------------------

# Anchor for state that must be shared by *every* `_GatewayConnection` alive in
# this Python process -- not just the current module's singleton. OWUI's function
# loader execs each redeploy into a brand-new module object with no teardown hook
# on the old one, so a connection left running by a previous deploy (a "zombie")
# keeps its own event loop. With private bookkeeping it would not know that the
# live connection already showed a run, and wrote that turn into the chat a
# second time (P33, root-caused 2026-07-11). Stashing the bookkeeping on
# `open_webui.socket.main` (OWUI's own stable module, never reloaded by our
# function) makes all connections read and write the *same* dicts. Since
# ELI-100 a retired connection writes no turns at all (`_turn_route_event`).
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
    # first request after redeploy (chat_write_locks hit exactly this,
    # 2026-07-13).
    state.setdefault("delivered_live", {})
    state.setdefault("claimed_runs", {})
    state.setdefault("chat_write_locks", {})
    state.setdefault("turn_rewrites", {})
    return state


class _RunQueue(asyncio.Queue):
    """A run's event queue. Events wait for room (`await put`): a reply read
    slower than its run streams holds the receive loop back instead of losing
    text. Control markers (a gap, a restart, a session row) go in past the
    bound: they are few, a lost one can leave a reply that never ends, and
    the receive loop that puts them must never wait or raise (review 2af1 N5:
    QueueFull there killed the loop and every run on the connection hung)."""

    def put_control(self, item) -> None:
        bound, self._maxsize = self._maxsize, 0
        try:
            self.put_nowait(item)
        finally:
            self._maxsize = bound


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
        # registration critical section. Two concurrent messages that release
        # from the queue at the same instant would otherwise both send into the
        # same idle window; the gateway steer-merges the second into the first,
        # which then renders nothing ("no reply text"). Serializing the send so
        # the next waiter observes the just-started run (via sessions.list
        # hasActiveRun) closes that thundering-herd race. ELI-56.
        self._session_send_locks: dict[str, asyncio.Lock] = {}

        # Deferred aborts for cancelled turns, {session_key: {"run_id", "task"}}.
        # OWUI 0.12 has no "send while running" that keeps the running turn:
        # both its interrupt path and the queued message's "Send now" call
        # stopTasksByChatId and then submit the new message ~0.5-0.8s later
        # (measured live 2026-10-11). An immediate chat.abort on that cancel
        # kills the turn AND every subagent it spawned (the gateway's
        # chat.abort cascades through abortControlledSubagents). So the abort
        # waits a short grace window, and a new message for the same session
        # inside it claims the run instead of letting it die. A plain Stop
        # (no follow-up message) still aborts once the window passes.
        self._pending_cancel_aborts: dict[str, dict] = {}

        # ELI-59: last model applied per session, {session_key: (model, ts)}.
        # A burst of near-simultaneous messages on one session all issue the
        # SAME idempotent sessions.patch; under that contention the RPC can
        # transiently time out and that message's answer is replaced by a
        # "Model selection error". Caching the last applied value (with a
        # short TTL -- the session model can drift externally, e.g. /model in
        # the Control UI) lets same-model calls skip the RPC entirely, and the
        # per-session patch lock collapses a burst to ONE in-flight patch.
        # OWUI caveat: multiple worker processes each keep their own cache --
        # imperfect, but strictly fewer redundant patches than before.
        self._model_patch_cache: dict[str, tuple[str | None, float]] = {}
        self._model_patch_locks: dict[str, asyncio.Lock] = {}

        # Last thinking-clamp note shown per session, so a standing mismatch
        # between the picked level and the model's ladder is explained ONCE
        # instead of prefixing every single answer with the same italic line.
        self._thinking_note_shown: dict[str, str] = {}

        # Reconnect state
        self._reconnect_attempt = 0
        self._max_backoff = 30  # seconds
        # The Gateway sends a `tick` event every `policy.tickIntervalMs` of
        # hello-ok (30 s, server-maintenance-Wf2jQ0wx.mjs:156); its own client
        # drops a socket that sent no frame for twice that ("tick timeout",
        # client-CKDmO6tp.mjs:1447-1459). The event loop does the same.
        self._tick_interval_s = 30.0
        self._stopped = False
        # Set once a newer deploy of this function has taken over (see
        # `retire()`). A retired connection still serves runs it already
        # has consumers for, but never writes turns (`_turn_route_event`) and
        # never reconnects once those runs are gone.
        self._retired = False
        # ELI-100: this connection's identity as a turn writer. A message whose
        # recorded writer is gone (`_turn_writer_alive`: retired here, or its
        # worker process exited) can be taken over (DESIGN 2.3); retire()
        # removes it.
        self.writer_id = turn_writer_id()
        # The Status Action's own connection only makes RPCs: it never writes turns.
        self.writes_turns = writes_turns
        self._turn_pumps: dict = {}     # (session_key, run_id) -> _RunPump
        self._turn_writers: dict = {}   # (chat_id, message_id) -> _TurnWriter
        self._turn_skip_runs: dict = {}  # (session_key, run_id) of heartbeat runs
        # (session_key, run_id) -> events of an agent run not yet known to be
        # no heartbeat, and the runs known (review 2af1 R7, `_turn_kind_known`)
        self._turn_unkinded: dict = {}
        self._turn_kinded: dict = {}
        # (child session, child run) -> a sub-agent's result (review 2af1 R10)
        self._child_results: dict = {}
        # session_key -> run id of an inline reply with no Open WebUI message
        # waiting for its interrupted run's owner (review 2af1 R14)
        self._awaiting_owner: dict = {}
        self._session_ids: dict = {}     # session_key -> sessionId (a cron wake's run id)
        self._wake_turns_bound: dict = {}  # session_key -> wake turn keys given a message
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
        # Treating every reconnect as a restart sent a bogus "gateway
        # restarted, resume" turn into live sessions and closed their OWUI
        # replies while the real run kept going (2026-10-12, a 1s WebSocket
        # drop under host memory pressure). `gateway_restarts` only counts
        # real restarts; consumers compare it, not `connection_epoch`.
        self.gateway_boot_id: str | None = None
        self.gateway_restarts = 0

        # Next request id
        self._next_req_id = 1

        # Counter for the event loop
        self._event_count = 0

        # Delivery bookkeeping. When running inside OWUI these dicts are the
        # *same objects* for every `_GatewayConnection` in the process (see
        # `_shared_gateway_state`), so a turn shown live by one connection is
        # seen as delivered by all of them (P33, 2026-07-11). Outside OWUI
        # (unit tests) each connection gets its own isolated dicts.
        _shared = _shared_gateway_state()

        # Runs the startup stub reconcile already finished, keyed by
        # "claim:<chat>:<stub>", so two connections never finalize one stub twice.
        self._claimed_runs: dict[str, bool] = (
            _shared["claimed_runs"] if _shared is not None else {}
        )

        # Records, by "session_key:run_id", every *final* event that was
        # actually dispatched to a live consumer queue (i.e. genuinely shown
        # to an open browser tab, which persists it itself via OWUI's normal
        # client-side flow). If the Gateway later re-emits a duplicate/retried
        # final event for that same run_id after the tab's request has
        # closed, this identity check catches it precisely -- unlike a
        # content-equality check, it can't false-positive just because two
        # unrelated turns happen to end with the same short text (e.g. "OK.",
        # "Done."), and unlike the debounce timer alone, it doesn't depend on
        # how long the session stays quiet afterward.
        # Events of a run continuing a message (an ask-user answer) that
        # arrived before its reply registered for it, while that reply still
        # streams the message: never a turn writer's (review 2af1 N6).
        self._held_for_live: dict[tuple[str, str], list] = {}
        self.unreachable_held_drains = 0
        self._delivered_live: dict[str, float] = (
            _shared["delivered_live"] if _shared is not None else {}
        )

        # Per-chat write locks for the read-modify-write paths that are not
        # turn writers (gap repair, stub reconcile). Shared across connections.
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
        # (`_turn_route_event`, DESIGN 2.6). Instance-local
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
        # A turn writer that takes the message over repeats them (review 2af1
        # R4). Shared, so a successor deploy's writer has them too.
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
        self._tool_events_seen: dict[tuple, bool] = {}

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
            spawn_background(_turn_after_hello(self))
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
            await self.send_request(
                "sessions.messages.subscribe", {"key": session_key}, timeout=15
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

    def _on_shutdown(self, payload: dict) -> None:
        """DESIGN 2.7 F4: the Gateway announces its close before it aborts any
        run. No restartExpectedMs = stop mode, whose aborted runs it never
        recovers; with it, restart mode, whose aborted rows stay `running`."""
        self._shutdown_seen = True
        if payload.get("restartExpectedMs") is None:
            self._stop_notice_seen = True

    def hand_over_run(self, session_key: str, run_id: str, *,
                      thinking: str | None = None, read_row: bool = False) -> bool:
        """An interrupted run's request ends without continuing it: the turn
        writer takes its message over (DESIGN 2.6) and the run's settled row
        decides who continues it (2.7), so a reply never resumes a run the
        Gateway recovers. `read_row`: the row is read now, for a handover
        that can come after the post-hello resync. False when there is no
        message to hand over."""
        key = f"{session_key}:{run_id}"
        target = self._run_targets.get(key)
        parsed = self.parse_owui_session_key(session_key)
        if (target is None or not parsed or not self.writes_turns or self._retired
                or (session_key, run_id) in self._turn_pumps):
            return False
        pump = self._turn_pumps[(session_key, run_id)] = _RunPump()
        pump.handover = {"message_id": target["message_id"],
                         "renderer": self._run_renderers.get(key),
                         "thinking": thinking or target.get("thinking")}
        pump.stop_notice_before = self._stop_notice_seen
        self.unregister_run_target(session_key, run_id)
        spawn_background(_turn_pump_loop(self, session_key, run_id, parsed, pump))
        spawn_background(self.ensure_message_subscription(session_key))
        if read_row:
            spawn_background(_turn_resync(self, skip=set(self.active_run_snapshot)))
        pipe_log(f"  handed run {run_id[:24]}... over to the turn writer "
                 f"(message {target['message_id'][:8]}...)")
        return True

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
        if key and payload.get("hasActiveRun") is False:
            _turn_settled(self, key, payload)
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
        into a full queue; the row waits its turn behind it (review 2af1
        N9), in a task per queue so one stalled reply holds up no other."""
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
                            thinking: str | None = None) -> None:
        """Remember that this run streams into OWUI message `message_id` in
        `chat_id`, and start a shadow `_TurnRenderer` for it. No-op without both
        ids (e.g. a chat with no stable message id yet). `thinking`: the
        turn's level, which a writer's resume keeps (review 2af1 R6)."""
        if not (chat_id and message_id and run_id):
            return
        key = f"{session_key}:{run_id}"
        self._run_targets[key] = {"chat_id": chat_id, "message_id": message_id,
                                  "thinking": thinking}
        self._run_renderers[key] = _TurnRenderer()
        # Bound memory: a run that never emits a terminal would otherwise leak
        # its target/renderer forever. Far above any real concurrency.
        if len(self._run_targets) > 100:
            for stale in list(self._run_targets)[:50]:
                self._run_targets.pop(stale, None)
                self._run_renderers.pop(stale, None)

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
        handover of the answer run continues the same reply (review 2af1 R9;
        the writer finds the question run in the transcript as a pipe run of
        the same message). The question run was shown to its end: its late
        events open nothing. `shown=False` moves the target only: the answer
        is not sent yet (review 2af1 N6), or its send failed and the target
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
        the message over shows the same (review 2af1 R4)."""
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
        read-modify-write paths into `chat_id` (gap repair, stub reconcile),
        creating it on first use."""
        lock = self._chat_write_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_write_locks[chat_id] = lock
        return lock

    # ── Public API ──────────────────────────────────────────────────

    async def ensure_connected(self):
        """Ensure the WS connection is up; connect/reconnect if needed."""
        if self._ws and self._event_loop_task and not self._event_loop_task.done():
            return
        if self._retired and not self._consumers:
            # Reviving a retired connection is how orphans piled up: each one
            # kept writing every agent turn with its own (older) code.
            raise GatewayError(
                "gateway connection retired: a newer deploy of the pipe owns it now"
            )
        async with self._init_lock:
            if self._ws and self._event_loop_task and not self._event_loop_task.done():
                return
            self._stopped = False
            await self._connect_and_start()
            if not self._event_loop_task or self._event_loop_task.done():
                self._loop = asyncio.get_running_loop()
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
        lock = self._session_send_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_send_locks[session_key] = lock
        return lock

    MODEL_PATCH_CACHE_TTL_S = 60.0

    def model_patch_lock(self, session_key: str) -> asyncio.Lock:
        """Per-session lock so a burst of same-session messages issues one
        sessions.patch, not N concurrent ones (ELI-59). Lazily created, same
        pattern as session_send_lock."""
        lock = self._model_patch_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._model_patch_locks[session_key] = lock
        return lock

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
            q.put_nowait(_event_frame(ev))  # the run's first events, held for this reply (N6)
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
        if self._retired and not self._consumers:
            pipe_log("  retired gateway connection: last run finished, closing")
            spawn_background(self.disconnect())

    def live_reply_owns(self, session_key: str, run_id: str) -> bool:
        """True when `run_id` continues a message (an ask-user answer, a
        resume) that a live inline reply of another run still streams: its
        events wait for that reply (`register_consumer`) instead of opening a
        second writer on the message (review 2af1 N6)."""
        parsed = parse_pipe_run_id(run_id)
        waiting = self._awaiting_owner.get(session_key)
        if waiting and parsed is None and run_id != waiting:
            # A reply with no message waits for its run's owner (R14): any
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
        their order of arrival (review 2af1 N7, N8): into the run's own queue
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
            _turn_route_now(self, ev)

    def mark_delivered_live(self, session_key: str, run_id: str):
        """Record that this run's final event was just dispatched to a live
        consumer queue -- i.e. genuinely shown to an open browser tab, which
        persists it itself via OWUI's own client-side flow. See
        `_delivered_live`'s docstring: a late event of such a run never
        reopens its message (`_turn_route_event`)."""
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

    async def session_preview(self, session_key: str, limit: int = 8,
                              max_chars: int = 4000) -> dict:
        """Fetch a bounded transcript preview. Every item comes back clipped
        server-side (see `_preview_recovery_text`), so do not use this where
        the whole message matters."""
        return await self.send_request(
            "sessions.preview",
            dict(keys=[session_key], limit=limit, maxChars=max_chars),
            timeout=10
        )

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
                            run_text: dict | None = None, settle=None) -> None:
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
          of it for new and appends the entire reply again (seen live
          2026-10-12: 2327..4000 followed by 1..4000).

        `settle` (a coroutine function) asks the browser which button was
        pressed and calls `settle_parked_turn`: Stop releases it now, Send now
        holds it for the follow-up message. It runs as a background task
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
                 "resolved": asyncio.Event(), "run_text": run_text}

        def _release() -> None:
            if entry["queue"] is not None:
                self.unregister_consumer(session_key, run_id, queue=entry["queue"])
            if entry["carried"]:
                pipe_log(f"  stopped: dropping {len(entry['carried'])} unsent "
                         "message(s)")
            if run_id:
                spawn_background(
                    self._abort_cancelled_run(session_key, run_id, send_stop))

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
            pipe_log(f"  cancel was a Send now: holding run {str(run_id)[:20]}... "
                     f"for the follow-up message (up to {hold_s:g}s)")
            self._arm_parked_timer(session_key, entry, hold_s)
        else:
            pipe_log("  cancel intent unknown (no page hook or no answer): "
                     "keeping the fallback window")
        entry["resolved"].set()

    async def claim_cancelled_turn(self, session_key: str) -> dict | None:
        """Take over the turn `park_cancelled_turn` parked, if any.

        Waits for the page's answer about the cancel (`resolved`), so a Stop
        whose answer is still on its way is honored: OWUI follows a Stop with
        the next queued message, which must start fresh rather than continue
        the run the user just stopped. The answer's own query is bounded
        (`_INTENT_QUERY_TIMEOUT_S`), and it always runs to its end in the
        background, so this needs no bound of its own (ELI-95).

        Returns {"run_id", "queue", "carried"}; the caller now owns the run,
        its buffered queue and the carried messages.
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
        return {"run_id": entry["run_id"], "queue": entry["queue"],
                "carried": list(entry["carried"]),
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
        renderer = self._run_renderers.get(f"{session_key}:{run_id}")
        if renderer is None:
            return
        if run_text:
            renderer._assistant_stream_text = run_text.get("received", "")
            renderer._last_item_text = run_text.get("item", "")
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
        unregister (cutting them would lose the reply); everything else stops
        now. Once the last consumer leaves, the connection closes itself and
        `ensure_connected` refuses to bring it back.
        """
        self._retired = True
        _retire_turn_writer(self.writer_id)
        for writer in list(self._turn_writers.values()):
            writer.release()
        for pump in list(self._turn_pumps.values()):
            pump.queue.put_nowait(("retired", None))
        if self._consumers:
            pipe_log(
                f"  retired gateway connection: serving {len(self._consumers)} "
                "in-flight run(s) before closing"
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
        after, which is where the run's text has a hole; the consumer turns
        it into an offset and `_align_whole_text` puts the text back once the
        run's whole text is known.
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
        """Count a Gateway restart only when its boot id changes."""
        first = self.connection_epoch <= 1
        if boot_id is None:
            # TEMPORARY fallback for a Gateway that sends no bootId (older
            # than 2026.9.x): a reconnect may be a restart, so count it as
            # one, which is the behaviour before boot ids existed.
            if not first:
                self.gateway_restarts += 1
                pipe_log("  reconnected; Gateway sent no bootId, treating it as a restart")
                self._mark_gateway_restart()
            return
        if self.gateway_boot_id is not None and boot_id != self.gateway_boot_id:
            self.gateway_restarts += 1
            pipe_log(f"  Gateway restarted (bootId {self.gateway_boot_id[:8]} -> {boot_id[:8]})")
            self._mark_gateway_restart()
        elif not first:
            pipe_log(f"  reconnected to the same Gateway (bootId {boot_id[:8]}); runs continue")
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
            raise GatewayError(
                _explain_connect_rejection(
                    str(resp.get("error", {}).get("message", "connect failed")),
                    device_id=(self._ident or {}).get("id"),
                )
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
        task -- spawning a second task here previously caused a runaway
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
                        spawn_background(_turn_resync(self))
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
                                    or "request failed"
                                )
                            )
                        else:
                            fut.set_result(msg.get("payload", {}))
                    continue

                # ── Tick keepalive (silently consume) ──
                if msg.get("event") == "tick" or msg.get("payload", {}).get("isHeartbeat"):
                    _note_heartbeat_run(self, msg.get("payload") or {})
                    continue

                # ── Event dispatch ──
                # On tool events (re-measured 2026-10-12, gateway 2026.9.x;
                # the 2026-07-30 ELI-26 measurement predates `session.tool`):
                #
                #   * The connection that STARTS a run gets its tool activity
                #     as `agent` events with payload.stream == "tool".
                #   * Any connection subscribed to the session with
                #     `sessions.messages.subscribe` gets the same payloads as
                #     `session.tool` events, whoever started the run (measured:
                #     observer got 3 start + 3 result, zero `agent` tool
                #     events). That covers this connection after a reconnect,
                #     and runs the Gateway starts itself (ELI-92).
                #
                # Both are folded into one path below, first copy wins.
                if msg.get("type") == "event" and msg.get("event") == "sessions.changed":
                    self._on_sessions_changed(msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") == "shutdown":
                    self._on_shutdown(msg.get("payload") or {})
                    continue
                if msg.get("type") == "event" and msg.get("event") == "session.tool":
                    # Same payload as an `agent` tool event (stream "tool",
                    # data.phase/name/toolCallId/args/result), so it takes the
                    # same path: inline turn, shadow renderer, turn writer.
                    p = msg.get("payload") or {}
                    msg = {"type": "event", "event": "agent", "payload": {
                        k: p[k] for k in ("runId", "sessionKey", "sessionId", "stream",
                                          "data", "seq", "ts") if k in p}}
                if msg.get("type") != "event" or \
                   msg.get("event") not in ("agent", "chat"):
                    continue

                payload = msg.get("payload", {})
                if payload.get("stream") == "tool" and not self._first_tool_event(payload):
                    continue

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
                if not consumers and await _turn_route_event(self, payload):
                    continue

                if consumers:
                    self._event_count += 1
                    if not payload.get("runId"):
                        pipe_log("  dispatched session-only event to sole consumer")
                    if payload.get("state") == "final" and payload.get("sessionKey") and payload.get("runId"):
                        # Genuinely shown to a live tab, which persists it via
                        # OWUI's own client-side flow -- remember this so a
                        # later duplicate/retried final event for the same
                        # run_id never reopens its message (P33).
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
                # This efficiently filters cross-session bleed (P16).

            except asyncio.TimeoutError:
                # Not even a tick for two intervals: the socket is dead without
                # having closed (half-open), so every run on it would wait
                # forever (review 2af1 R13). Tear it down and reconnect.
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


# Protocol sentinels (docs/tools/subagents.md:470-431) that mean "no
# user-visible content was produced" -- never real assistant text to show a
# human. An announce-follow-up run can legitimately finish with exactly one
# of these as its only "assistant text", and writing it into a chat leaks
# internal plumbing (P33, 2026-07-11: `ANNOUNCE_SKIP` landed as literal
# message text).
_SILENT_SENTINELS = {"ANNOUNCE_SKIP", "NO_REPLY", "no_reply"}


# How far back a reply looks for its run's own closing message when the
# stream lost text: later runs may already have ended after it.
RUN_TEXT_HISTORY_LIMIT = 60
# Messages to read when rebuilding a whole run from `chat.history`: each tool
# call is a message of its own (measured: 119 calls = 154 messages).
RUN_HISTORY_LIMIT = 600


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
        meta = message.get("__openclaw")
        keys = {message.get("idempotencyKey"),
                meta.get("idempotencyKey") if isinstance(meta, dict) else None}
        if not keys & wanted:
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
    `text` + `output` items (ELI-88/90/92).

    claude-cli imports its transcript into the session as it goes: assistant
    messages whose blocks are `text`, `toolcall` {id, name, arguments} and
    `tool_result` {tool_use_id, content, is_error}, in order. The run's closing
    message (`cli-assistant:<runId>`) repeats all of its text, so it only
    counts when nothing in its own run carried text (a plain, tool-free
    reply, or imports that carry tool blocks only). It counts in place: an
    asked question's run closes before its answer run starts, and the
    question was lost when only the last closer was kept (ask-rebuild,
    2026-10-14, chat d0f0c6ff).

    Text after a tool call is joined to the text before it with the separator
    the Gateway's live stream inserts there (`sawToolUseSinceText`), so the
    rebuild equals what streamed (it streams as "\n\n<text>").
    Plain concatenation glued a preamble to the answer ("...one at a time.
    USER-DONE" with no break, restart window 2026-10-13)."""
    r = _TurnRenderer()
    run_text = False          # this run carried text before its closer
    tool_since_text = False   # the Gateway's `sawToolUseSinceText`
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
        keys = (str(message.get("idempotencyKey") or ""), str(meta.get("idempotencyKey") or ""))
        if any(k.startswith("cli-assistant:") for k in keys):
            text = ((_history_item(message) or {}).get("text") or "").strip()
            if not run_text and text and text not in _SILENT_SENTINELS:
                lead = _message_boundary_separator(r.text, text)
                tool_since_text = False
                r.feed({"stream": "assistant", "data": {"delta": lead + text}})
            run_text = False
            continue
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                lead = _message_boundary_separator(r.text, block["text"]) if tool_since_text else ""
                tool_since_text = False
                run_text = run_text or bool(block["text"].strip())
                r.feed({"stream": "assistant", "data": {"delta": lead + block["text"]}})
            elif kind == "toolcall" and block.get("id"):
                tool_since_text = True
                r.feed({"stream": "tool", "data": {
                    "phase": "start", "name": block.get("name", ""),
                    "toolCallId": block["id"], "args": block.get("arguments") or {}}})
            elif kind == "tool_result" and block.get("tool_use_id"):
                result = block.get("content")
                if isinstance(result, list):
                    result = "".join(str(b.get("text") or "") for b in result if isinstance(b, dict))
                r.feed({"stream": "tool", "data": {
                    "phase": "result", "name": block.get("name", ""),
                    "toolCallId": block["tool_use_id"], "result": result or "",
                    "isError": block.get("is_error") is True}})
    return r


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
    meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
    return any(str(k or "").startswith("cli-assistant:")
               for k in (message.get("idempotencyKey"), meta.get("idempotencyKey")))


def _history_run_messages(history: dict, run_id: str) -> list | None:
    """One run's messages in `chat.history`: everything after the user
    message that started it, or after the previous run's closing message
    (a run with no user turn, e.g. a sub-agent announce), through its own
    closing message (`cli-assistant:<runId>`). None when the run has not
    closed yet."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list) or not run_id:
        return None
    wanted = {run_id, f"cli-assistant:{run_id}"}
    for end in range(len(messages) - 1, -1, -1):
        message = messages[end]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
        if not {message.get("idempotencyKey"), meta.get("idempotencyKey")} & wanted:
            continue
        start = end
        while start > 0 and (messages[start - 1] or {}).get("role") != "user" \
                and not _is_closing_message(messages[start - 1]):
            start -= 1
        return messages[start:end + 1]
    return None


def _deepest_leaf_id(messages: dict, start_id: str | None) -> str | None:
    """Walk down `childrenIds` from `start_id` to the deepest childless node.

    OWUI's `history.currentId` is only a *view* pointer: the frontend (or an
    interleaved live turn) can reset it back to an ancestor that already has
    children -- e.g. right after an agent turn was appended. Anchoring a
    new message directly on `currentId` in that state appends it as
    a *sibling* of the existing child, which OWUI renders as a 1/2·2/2 variant
    group instead of a linear message (observed 2026-07-13: a `Sub-agent
    finished` message and the next agent message both parented to the same
    node). Walking to the true leaf (following the most-recently-appended
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


def _relay_output_events(sent: list[dict], current: list[dict]) -> list[dict]:
    """Responses stream events that take a tab's `message.output` from
    `sent` to `current`, in the shapes OWUI's `applyResponseStreamEvent`
    (structuredOutput.ts) folds: a new item is `output_item.added` with an
    explicit `output_index` (without one it is spliced in BEFORE the last
    item), grown message text is an `output_text.delta` on the item's id
    (which also extends `message.content`), and any other change to an
    existing item is `output_item.done`, which replaces it."""
    events: list[dict] = []
    for i, item in enumerate(current):
        old = sent[i] if i < len(sent) else None
        is_message = item.get("type") == "message"
        text = (item.get("content") or [{}])[0].get("text", "") if is_message else ""
        if old is None:
            added = copy.deepcopy(item)
            if is_message:
                added["content"] = [{"type": "output_text", "text": ""}]
            events.append({"type": "response.output_item.added",
                           "output_index": i, "item": added})
            if text:
                events.append({"type": "response.output_text.delta", "item_id": item["id"],
                               "output_index": i, "content_index": 0, "delta": text})
            continue
        if is_message and old.get("id") == item.get("id"):
            old_text = (old.get("content") or [{}])[0].get("text", "")
            if text != old_text and text.startswith(old_text):
                events.append({"type": "response.output_text.delta", "item_id": item["id"],
                               "output_index": i, "content_index": 0,
                               "delta": text[len(old_text):]})
                continue
        if item != old:
            events.append({"type": "response.output_item.done",
                           "output_index": i, "item": copy.deepcopy(item)})
    return events


def _relay_content_is_showable(text: str) -> bool:
    """True once the accumulated visible content is safe to surface as a real
    bubble: non-empty, not a silent sentinel, and not still a prefix that could
    grow into a sentinel-only run. Delaying introduce until this holds means a
    sentinel-only wake (`NO_REPLY`/`ANNOUNCE_SKIP`) never flashes an empty
    bubble -- the same suppression the post-hoc path gets from
    `_last_assistant_text_from_preview`, applied to a stream we watch grow.
    """
    t = text.strip()
    if not t or t in _SILENT_SENTINELS:
        return False
    if any(s.startswith(t) for s in _SILENT_SENTINELS):
        return False
    return True


async def _relay_emit_event(user_id: str, chat_id: str, message_id: str | None,
                            data: dict) -> bool:
    """Fire one OWUI socket `events` envelope to the user room (same transport
    as `_emit_live_bootstrap_reload`). Returns False on any failure so callers
    can log context; a failed emit never raises into the event loop."""
    if not message_id:
        return False
    try:
        from open_webui.socket.main import sio
    except Exception as ex:
        pipe_log(f"  socket emit unavailable (not inside OWUI process?): {ex}")
        return False
    try:
        await sio.emit(
            "events",
            {"chat_id": chat_id, "message_id": message_id, "data": data},
            room=f"user:{user_id}",
        )
        return True
    except Exception as ex:
        pipe_log(f"  socket emit failed: {ex}")
        return False


# ── Orphaned reply stubs (ELI-90) ──
# Open WebUI writes a reply's assistant message (`done: False`) before the
# pipe sends anything, and only the request that streams the reply ever sets
# `done: True`. When Open WebUI restarts mid-turn (three OOM kills on
# 2026-10-12 16:21-16:25 UTC), that request is gone: the run finishes in the
# Gateway, but the chat keeps an empty bubble that looks like it is still
# running, forever. On the first load after the restart these stubs are
# matched to their runs in the Gateway transcript and finished from it.

ORPHAN_STUB_NOTE = (
    "*This reply was cut off before it finished (Open WebUI restarted or "
    "lost the run), and no reply for it could be recovered from OpenClaw. "
    "Send the message again if you still need an answer.*"
)
ORPHAN_STUB_SILENT_NOTE = "*The run for this message stopped without leaving a reply in the transcript.*"


def _is_forwarded_turn(message) -> bool:
    """A forwarded user turn as `chat.history` shows it since OpenClaw
    2026.10.2: role "assistant", "Forwarded from ...", keyed as the admitted
    turn `<id>:user`, with the provenance the Gateway projects
    (chat-display-projection.helpers-pbUdgTgb.mjs `hasForwardedMessageProvenance`:
    a `sessions_send` message, or a cron run's prompt)."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False
    meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
    key = str(message.get("idempotencyKey") or meta.get("idempotencyKey") or "")
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
    meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
    key = str(message.get("idempotencyKey") or meta.get("idempotencyKey") or "")
    return key[:-len(":user")] if key.endswith(":user") and len(key) > 5 else None


def _run_id_sent_at(run_id: str) -> float | None:
    """The send time the pipe writes at the end of its run ids
    (`msg-<chat>-[<message>-]<time.time()>`)."""
    try:
        return float(run_id.rsplit("-", 1)[1]) if run_id.startswith("msg-") else None
    except (IndexError, ValueError):
        return None


def _stub_turn(history: dict, stub_id: str, user_text: str,
               stub_ts: float | None = None) -> dict | None:
    """The transcript turn an OWUI reply stub was waiting for:
    {"run_id", "messages", "closed", "by"}, or None when the transcript has
    none.

    The pipe puts the OWUI message id into the run id (`msg-<chat>-<message>-
    <time>`), so the turn is found by identity (`by` "id"). A run sent
    before that is found by its user text (`by` "text"): with `stub_ts` (the
    stub's creation time) the first matching run sent at or after it, since
    the pipe sends after Open WebUI creates the stub and the same short text
    ("works?") recurs; without it the newest match. `messages` runs to the
    run's closing message (`cli-assistant:<runId>`) or the next turn;
    `closed` says whether the run is over by the transcript alone."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list):
        return None
    starts = [i for i, m in enumerate(messages) if _history_user_run_id(m)]
    pick, by = None, "id"
    if stub_id:
        by_id = [i for i in starts if f"-{stub_id}-" in _history_user_run_id(messages[i])]
        pick = by_id[-1] if by_id else None
    text = (user_text or "").strip()
    if pick is None and text:
        by, by_text = "text", [i for i in starts if _recovery_user_text_matches(
            ((_history_item(messages[i]) or {}).get("text") or "").strip(), text)]
        if stub_ts is not None:
            # 2s of slack: Open WebUI stamps whole seconds.
            by_text = [i for i in by_text
                       if (_run_id_sent_at(_history_user_run_id(messages[i])) or 0) >= stub_ts - 2]
            pick = by_text[0] if by_text else None
        else:
            pick = by_text[-1] if by_text else None
    if pick is None:
        return None
    run_id = _history_user_run_id(messages[pick])
    end = next((i for i in starts if i > pick), len(messages))
    turn = messages[pick + 1:end]
    closing = f"cli-assistant:{run_id}"
    for j, m in enumerate(turn):
        meta = m.get("__openclaw") if isinstance(m, dict) and isinstance(m.get("__openclaw"), dict) else {}
        if isinstance(m, dict) and closing in (m.get("idempotencyKey"), meta.get("idempotencyKey")):
            return {"run_id": run_id, "messages": turn[:j + 1], "closed": True, "by": by}
    return {"run_id": run_id, "messages": turn, "closed": end < len(messages), "by": by}


def _owui_message_ts(message: dict) -> float | None:
    ts = message.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return ts / 1000 if ts > 1e11 else float(ts)  # ms or s
    return None


async def _owui_chat_has_live_task(chat_id: str) -> bool:
    """True while Open WebUI runs a task for `chat_id` (a reply being
    generated by this process). Answers True when it cannot tell, so a
    reply that is still streaming is never overwritten."""
    try:
        from open_webui.main import app
        from open_webui.tasks import list_task_ids_by_item_id
        return bool(await list_task_ids_by_item_id(getattr(app.state, "redis", None), chat_id))
    except Exception as ex:
        pipe_log(f"  orphan stub: cannot read Open WebUI tasks ({ex}); leaving chat {chat_id[:8]}... alone")
        return True


async def _session_list_active(conn: "_GatewayConnection", session_key: str) -> bool | None:
    """`sessions.list` `hasActiveRun` for one session (counts a sub-agent
    yield gap as active, unlike `sessions.describe`). False when the Gateway
    lists no such session, None when it can't be asked."""
    try:
        lst = await conn.send_request("sessions.list", {}, timeout=15)
    except Exception as ex:
        pipe_log(f"  sessions.list for {session_key[-36:][:8]}... failed: {ex}")
        return None
    row = next((r for r in (lst.get("sessions") or []) if r.get("key") == session_key), None)
    return bool(row and row.get("hasActiveRun"))


# A long session's whole transcript (952 messages for 0abe9bde) takes the
# Gateway well over the default 10s RPC bound to serialize.
ORPHAN_HISTORY_TIMEOUT_S = 60


async def _reconcile_stub(conn: "_GatewayConnection", agent_id: str,
                          chat_id: str, stub_id: str, *, wait: bool = True) -> str:
    """Finish one orphaned reply stub from its run in the transcript. A run
    still going is waited for on the pushed settle (`sessions.changed`), not
    on a timer, or with `wait=False` reported as "wait"; a stub a live turn
    writer takes over meanwhile is the writer's (DESIGN 2.6). Returns
    "done", "wait" or "skip"."""
    from open_webui.models.chats import Chats
    waiting = False
    while True:
        chat = await Chats.get_chat_by_id(chat_id)
        msgs = ((chat.chat or {}).get("history") or {}).get("messages") or {} if chat else {}
        stub = msgs.get(stub_id) or {}
        if stub.get("role") != "assistant" or stub.get("done") is not False:
            return "skip"  # gone or finished meanwhile
        writer = (stub.get("meta") or {}).get("openclaw_writer")
        if writer:
            # A turn writer's message: its own writer finishes it, or, once
            # that writer is gone (a retired deploy, a dead process), this
            # connection adopts it. Another request's task in the chat is no
            # reason to leave it.
            if _turn_writer_alive(writer):
                return "skip"
            return await _adopt_turn_message(
                conn, chat_id=chat_id, user_id=chat.user_id, message_id=stub_id,
                session_key=_owui_session_key(agent_id, chat.user_id, chat_id),
                meta=stub.get("meta") or {})
        if await _owui_chat_has_live_task(chat_id):
            return "skip"  # a live request owns it
        session_key = _owui_session_key(agent_id, chat.user_id, chat_id)
        changed = conn.session_change_event(session_key)
        try:
            hist = await conn.session_history(session_key, limit=RUN_HISTORY_LIMIT,
                                              timeout=ORPHAN_HISTORY_TIMEOUT_S)
        except Exception as ex:
            pipe_log(f"  orphan stub {stub_id[:8]}...: chat.history failed: {ex!r}")
            return "skip"
        parent = msgs.get(stub.get("parentId")) or {}
        user_text = _coerce_text(parent.get("content")) if parent.get("role") == "user" else ""
        turn = _stub_turn(hist, stub_id, user_text, _owui_message_ts(stub))
        if turn is None or turn["closed"]:
            break  # no run to wait for, or it is over
        active = await _session_list_active(conn, session_key)
        if active is None:
            return "skip"
        if not active:
            break
        if not wait:
            return "wait"
        if not waiting:
            waiting = True
            pipe_log(f"  orphan stub {stub_id[:8]}... in chat {chat_id[:8]}...: "
                     "its run is still going; finishing it when the run settles")
        await changed.wait()

    run_id = (turn or {}).get("run_id")
    # What the stub already shows is never replaced on a guess: a stub with
    # content (older pipes streamed into it, a writer persisted into it) is
    # rebuilt only from a run matched by identity, else just closed.
    has_content = bool(_coerce_text(stub.get("content")).strip() or stub.get("output"))
    fields: dict = {"done": True}
    output: list | None = None
    text = ""
    if turn and (turn["by"] == "id" or not has_content):
        rendered = _history_renderer(turn["messages"])
        if not rendered.text.strip() and not rendered.output and not has_content:
            rendered._add_text(ORPHAN_STUB_SILENT_NOTE)
        if rendered.output:
            output = rendered.finished_output()
            text = rendered.text.strip()
            fields.update(content=text, output=output)
    elif not has_content:
        rendered = _TurnRenderer()
        rendered._add_text(ORPHAN_STUB_NOTE)
        output, text = rendered.finished_output(), rendered.text
        fields.update(content=text, output=output)
    async with conn._chat_write_lock(chat_id):
        chat = await Chats.get_chat_by_id(chat_id)
        msgs = ((chat.chat or {}).get("history") or {}).get("messages") or {} if chat else {}
        current = msgs.get(stub_id) or {}
        if current.get("done") is not False:
            return "skip"
        if _turn_writer_alive((current.get("meta") or {}).get("openclaw_writer")):
            return "skip"  # a turn writer took it over (DESIGN 2.6) and finishes it
        claim = f"{session_key}:{run_id}" if run_id else None
        if claim:
            if claim in conn._claimed_runs:
                return "skip"  # another connection finished this run's stub
            conn._claimed_runs[claim] = True
        # Open WebUI answers None, logging nothing, when the write fails (its
        # transaction raised and was rolled back).
        if await Chats.upsert_message_to_chat_by_id_and_message_id(
                chat_id, stub_id, fields) is None:
            if claim:
                conn._claimed_runs.pop(claim, None)
            pipe_log(f"  orphan stub {stub_id[:8]}... in chat {chat_id[:8]}...: "
                     "Open WebUI did not save the write (ELI-90)")
            return "skip"
        if run_id:
            conn.mark_delivered_live(session_key, run_id)
    if output is not None:
        await _relay_emit_event(chat.user_id, chat_id, stub_id,
                                {"type": "chat:completion", "data": {"output": output}})
    await _relay_emit_event(chat.user_id, chat_id, stub_id,
                            {"type": "chat:active", "data": {"active": False}})
    cards = sum(1 for i in output or [] if i.get("type") == "function_call")
    if output is None:
        how = "closed, content kept"
    elif run_id:
        how = f"filled from run {run_id[:48]} by {turn['by']}"
    else:
        how = "no run in the transcript, note written"
    pipe_log(f"  orphan stub {stub_id[:8]}... in chat {chat_id[:8]}...: {how} "
             f"({len(text)} chars, {cards} tool card(s)) (ELI-90)")
    return "done"


async def _orphan_stub_ids(prefixes: list[str]) -> list[tuple[str, str]]:
    """(chat_id, message_id) of every unfinished assistant message a model of
    this pipe wrote. Open WebUI keeps a row per message in `chat_message`
    whose id is `<chat_id>-<message_id>`."""
    from sqlalchemy import or_, select
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.chat_messages import ChatMessage
    async with get_async_db_context() as db:
        rows = (await db.execute(
            select(ChatMessage.chat_id, ChatMessage.id)
            .where(ChatMessage.role == "assistant")
            .where(ChatMessage.done.is_(False))
            .where(or_(*[ChatMessage.model_id.startswith(p) for p in prefixes]))
        )).all()
    out = []
    for chat_id, composite in rows:
        prefix = f"{chat_id}-"
        out.append((chat_id, composite[len(prefix):] if composite.startswith(prefix) else composite))
    return out


_orphan_reconcile_task: "asyncio.Task | None" = None


async def _reconcile_orphan_stubs(valves_getter) -> None:
    try:
        conn = await _get_gateway_connection(valves_getter)
        agent_id = getattr(valves_getter(), "AGENT_ID", "") or "main"
        stubs = await _orphan_stub_ids(
            _composer_model_prefixes(own_id=_composer_own_function_id()))
    except Exception as ex:
        pipe_log(f"  orphan stub reconcile could not start: {ex}")
        return
    if stubs:
        pipe_log(f"  orphan stub reconcile: {len(stubs)} unfinished OpenClaw "
                 "repl(y/ies) in Open WebUI")
    # One at a time: Open WebUI's SQLite rejected most of 45 concurrent chat
    # writes (2026-10-12). Only a stub whose run is still going gets its own
    # task, to wait for that run's settle.
    counts: dict = {}
    for chat_id, stub_id in stubs:
        status = await _reconcile_stub_logged(conn, agent_id, chat_id, stub_id, wait=False)
        counts[status] = counts.get(status, 0) + 1
        if status == "wait":
            spawn_background(_reconcile_stub_logged(conn, agent_id, chat_id, stub_id))
    if stubs:
        pipe_log(f"  orphan stub reconcile: {counts}")
    try:
        await _reconcile_ended_while_down(conn, agent_id)
    except Exception as ex:
        pipe_log(f"  ended-while-down reconcile failed: {ex!r}")


def _history_closed_runs(messages: list) -> list:
    """[(run_id, closing timestamp ms)] of the runs `chat.history` shows as
    closed: a claude-cli run by its `cli-assistant:<runId>` message, an
    embedded run by an assistant message keyed with the run id itself."""
    started = {run for run, _tool in _history_user_turns(messages)}
    out = []
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        meta = m.get("__openclaw") if isinstance(m.get("__openclaw"), dict) else {}
        for k in (m.get("idempotencyKey"), meta.get("idempotencyKey")):
            k = str(k or "")
            run = k[len("cli-assistant:"):] if k.startswith("cli-assistant:") else (k if k in started else "")
            if run and isinstance(m.get("timestamp"), (int, float)):
                out.append((run, m["timestamp"]))
                break
    return out


def _settled_run_span(history: dict, run_id: str) -> list | None:
    """The messages of a run the session row reports settled (no active run,
    `lastRunId`): its span up to its closer or the next admitted turn, else,
    as the newest turn, everything after its user turn."""
    span = _history_span_messages(history, run_id)
    if span is not None:
        return span
    messages = (history or {}).get("messages")
    for i, m in enumerate(messages if isinstance(messages, list) else []):
        if _history_user_run_id(m) == run_id:
            return messages[i + 1:]
    return None


async def _reconcile_ended_while_down(conn: "_GatewayConnection", agent_id: str) -> None:
    """ELI-94, DESIGN-agent-turns.md case 4. A graceful Open WebUI stop saves
    the reply it cancels `done: true` with what it had; the run goes on and
    closes while Open WebUI is down, so no event reaches this process and
    the ELI-90 pass (done:false only) skips it. Two recorded timestamps say
    which chats missed something: the session row's `updatedAt` (bumped when
    a run ends) newer than the chat's `updated_at`. In those, a run that
    closed in a later second than the chat's last write is rebuilt from the
    transcript into its message (identity rule, rule E for recoveries)."""
    from open_webui.models.chats import Chats
    lst = await conn.send_request("sessions.list", {}, timeout=15)
    prefix = f"agent:{agent_id}:openwebui-"
    fixed = 0
    for row in lst.get("sessions") or []:
        key = row.get("key") or ""
        if not key.startswith(prefix) or row.get("hasActiveRun"):
            continue  # not ours, or a run still going: the handover (2.6) owns it
        after = key[len(prefix):]
        if len(after) != 73 or after[36] != "-":
            continue
        user_id, chat_id = after[:36], after[37:]
        updated = row.get("updatedAt")
        if not isinstance(updated, (int, float)):
            continue
        chat = await Chats.get_chat_by_id(chat_id)
        if chat is None or chat.user_id != user_id:
            continue
        written = int(getattr(chat, "updated_at", 0) or 0)
        if int(updated // 1000) <= written:
            continue
        fixed += await _reconcile_chat_ended_runs(conn, key, chat_id, written,
                                                  session_id=row.get("sessionId"),
                                                  last_run=row.get("lastRunId"), settled_ms=updated)
    if fixed:
        pipe_log(f"  ended-while-down reconcile: completed {fixed} repl(y/ies) (ELI-94)")


async def _reconcile_chat_ended_runs(conn: "_GatewayConnection", session_key: str,
                                     chat_id: str, written: int, *,
                                     session_id: str | None, last_run: str | None = None,
                                     settled_ms: float | None = None) -> int:
    """Rebuild every finished reply in `chat_id` whose run closed after the
    chat was last written (`written`, whole seconds) and that holds less than
    the transcript. A run closed by its transcript closer, or the session
    row's `lastRunId`, settled at the row's `updatedAt` (`settled_ms`; the
    embedded runtime writes no closer). Returns how many were written."""
    from open_webui.models.chats import Chats
    if await _owui_chat_has_live_task(chat_id):
        return 0  # a reply is streaming: its own request finishes it
    hist = await conn.session_history(session_key, limit=RUN_HISTORY_LIMIT,
                                      timeout=ORPHAN_HISTORY_TIMEOUT_S)
    messages = (hist or {}).get("messages") or []
    closed = _history_closed_runs(messages)
    if last_run and settled_ms and last_run not in {r for r, _ms in closed}:
        closed.append((last_run, settled_ms))
    targets: dict = {}
    for run, closed_ms in closed:
        # A wake's closer is keyed by the sessionId and deduplicated (ELI-103).
        if run == session_id or int(closed_ms // 1000) <= written:
            continue
        mid = run_message_id(session_key, chat_id, run, messages)
        if mid:
            targets.setdefault(mid, run)
    fixed = 0
    for mid, run in targets.items():
        runs = [r for r, _tool in _history_user_turns(messages)
                if r != session_id and run_message_id(session_key, chat_id, r, messages) == mid]
        if run not in runs:
            runs.append(run)
        spans = [_settled_run_span(hist, r) if r == last_run else _history_span_messages(hist, r)
                 for r in runs]
        if any(s is None for s in spans):
            continue  # a run written into it has not closed
        rendered = _history_renderer([m for s in spans for m in s])
        output, text = await _transcript_view(
            conn, session_key=session_key, chat_id=chat_id,
            user_id=session_key.rsplit("openwebui-", 1)[-1][:36], message_id=mid,
            runs=runs, messages=messages, rendered=rendered)
        if not output:
            continue
        async with conn._chat_write_lock(chat_id):
            chat = await Chats.get_chat_by_id(chat_id)
            msgs = ((chat.chat or {}).get("history") or {}).get("messages") or {} if chat else {}
            current = msgs.get(mid) or {}
            if current.get("role") != "assistant" or current.get("done") is not True:
                continue  # missing, or still generating: the ELI-90 pass owns done:false
            held_cards = sum(1 for i in current.get("output") or [] if i.get("type") == "function_call")
            cards = sum(1 for i in output if i.get("type") == "function_call")
            held_text = _coerce_text(current.get("content")).strip()
            if cards < held_cards or (cards == held_cards and len(text) <= len(held_text)):
                continue  # holds as much as the transcript: never rewritten
            if await Chats.upsert_message_to_chat_by_id_and_message_id(
                    chat_id, mid, {"content": text, "output": output, "done": True}) is None:
                pipe_log(f"  ended-while-down {mid[:8]}... in chat {chat_id[:8]}...: "
                         "Open WebUI did not save the write (ELI-94)")
                continue
            conn.mark_delivered_live(session_key, run)
        await _relay_emit_event(chat.user_id, chat_id, mid,
                                {"type": "chat:completion", "data": {"output": output}})
        pipe_log(f"  ended-while-down {mid[:8]}... in chat {chat_id[:8]}...: completed from "
                 f"{len(runs)} run(s) ({len(text)} chars, {cards} tool card(s), "
                 f"held {held_cards}) (ELI-94)")
        fixed += 1
    return fixed


async def _reconcile_stub_logged(conn, agent_id, chat_id, stub_id, *, wait=True) -> str:
    try:
        return await _reconcile_stub(conn, agent_id, chat_id, stub_id, wait=wait)
    except Exception as ex:
        pipe_log(f"  orphan stub {stub_id[:8]}... in chat {chat_id[:8]}...: reconcile failed: {ex!r}")
        return "failed"


def _ensure_orphan_reconcile(valves_getter) -> None:
    """Once per load of this Function inside Open WebUI (a restart reloads
    it; so does a deploy, which is harmless: a reply still streaming has a
    live task and is skipped)."""
    global _orphan_reconcile_task
    if _orphan_reconcile_task is not None:
        return
    try:
        import open_webui.models.chat_messages  # noqa: F401  (inside Open WebUI only)
    except Exception:
        return
    _orphan_reconcile_task = spawn_background(_reconcile_orphan_stubs(valves_getter))


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
# same function (P33/P36: OWUI's function loader (open_webui/utils/plugin.py)
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
    return await target.send_request(method, params, timeout=timeout)
