# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------

GATEWAY_SCOPES = ["operator.admin", "operator.read", "operator.write"]


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

    Ordering is by arrival, and `response.output_item.added` appends when it
    does not recognise the item, so these interleave correctly with ordinary
    streamed text -- no output_index bookkeeping needed on our side.

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
        "item": {
            "type": "function_call_output",
            "id": f"fco_{tool_call_id}",
            "call_id": tool_call_id,
            "output": [{"type": "output_text", "text": result_str[:8000]}],
            "status": "completed",
        },
    }


TOOL_ERROR_MARK = "❌"


def _tool_call_error_relabel_event(name: str) -> dict:
    """Re-emit a failed call's `function_call` item with ❌ in its name.

    The card's collapsed row shows `attributes.name` and nothing else about
    outcome: its status icon is chosen by `isDone` alone, with no failure
    branch (`ToolCallDisplay.svelte:136`), so a red icon is unreachable
    without patching OWUI. The name is the only outcome-carrying field we
    control, and it renders through `Markdown`, so an emoji survives.

    `response.output_item.done` looks like the natural event for this, but in
    OWUI's `handle_responses_streaming_event` it is dead code: the broader
    `elif event_type.startswith('response.') and event_type.endswith('.done')`
    branch matches first and explicitly skips `output_item` ("handled
    specifically below" -- it isn't reachable). So we instead use that
    branch's own "Generic Field Done" arm, which for an unrecognized
    `response.<field>.done` event does `item[<field>] = data[<field>]` on
    `output[output_index]` (default: last item) and returns a non-None
    metadata dict, which is what actually triggers the broadcast. No
    `output_index` needed: the caller only uses this when the started item is
    still last, which is exactly when the default is correct.

    Unlike the old (dead) `output_item.done` shape, this mutates the existing
    item in place rather than replacing it wholesale, so `arguments`/`status`
    don't need to be repeated here -- they're already set on the item from the
    start event and survive untouched.
    """
    return {
        "type": "response.name.done",
        "name": f"{name} {TOOL_ERROR_MARK}",
    }


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

    Purpose (parity, 2026-07-19): when the inline pipe generator ends BEFORE a
    long run finishes (idle self-close, cancel, or a torn-down request), the
    tail used to survive only as a truncated, tool-block-less *proactive*
    bubble read from a length-capped `sessions.preview`. This renderer instead
    accumulates the COMPLETE content (assistant text + every tool block, in
    order) straight from the same events the persistent connection already
    dispatches, so the run's ORIGINAL OWUI message can be finalized to exactly
    what a non-interrupted turn would have stored.

    Scope: assistant text deltas, item-carried text, and tool-result blocks --
    the substance of the long autonomous turns this path exists for. MEDIA
    uploads and ask-user modals are intentionally NOT reproduced (they need the
    live browser and don't occur on a self-closed background tail).
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

    def _add(self, chunk: str) -> None:
        if chunk:
            self.visible_text += chunk

    def _add_text(self, chunk: str) -> None:
        if not chunk:
            return
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
                result_str = json.dumps(result) if not isinstance(result, str) else result
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

# Re-enabled 2026-07-11 after fixing the ANNOUNCE_SKIP-class bug: an
# internal protocol marker ("ANNOUNCE_SKIP", from the sessions_send
# announce-delivery mechanism) was being proactively delivered into a chat
# as if it were real assistant text, because `_last_assistant_text_from_preview`
# didn't distinguish user-facing text from internal markers that happen to
# flow through the same event stream. Fixed by filtering the documented
# silent sentinels (`_SILENT_SENTINELS`: ANNOUNCE_SKIP, NO_REPLY, no_reply)
# before treating preview text as deliverable -- see that constant's
# docstring. The separate duplicate-identity issue (a live turn in an
# actively-used real chat getting proactively re-delivered) was already
# fixed by `was_delivered_live` and confirmed via timestamps to only have
# happened in the window *before* that fix was deployed, not after. Do not
# re-disable without updating this comment and PLAN.md P33 with the new
# incident.
PROACTIVE_DELIVERY_ENABLED = True


# Anchor for state that must be shared by *every* `_GatewayConnection` alive in
# this Python process -- not just the current module's singleton. OWUI's function
# loader execs each redeploy into a brand-new module object with no teardown hook
# on the old one, so a connection left running by a previous deploy (a "zombie")
# keeps its own event loop and its own *private* bookkeeping dicts. That zombie
# still receives the Gateway's broadcast `final` events; with private bookkeeping
# it sees an empty `delivered_live`/`session_last_activity` for the session,
# wrongly concludes "nobody consumed this, the session is idle", and proactively
# re-writes the turn into chat history -- even though the *live* connection just
# showed it to the open tab. That is the "*↳ Proactive message* on a live turn"
# / duplicate-branch (2/2) bug (P33, root-caused 2026-07-11). Stashing the
# liveness bookkeeping on `open_webui.socket.main` (OWUI's own stable module,
# never reloaded by our function) makes all connections -- current and zombie --
# read and write the *same* dicts, so a turn shown live by any connection is seen
# as delivered by all of them, and proactive delivery only fires for turns no
# connection ever showed live (genuine cron/heartbeat/sessions_send wakes).
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
    state.setdefault("delivered_proactive", {})
    state.setdefault("session_last_activity", {})
    state.setdefault("pending_proactive_debounce", set())
    state.setdefault("proactive_runs", {})
    state.setdefault("last_final_run", {})
    state.setdefault("chat_write_locks", {})
    return state


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
        self._stopped = False
        # Set once a newer deploy of this function has taken over (see
        # `retire()`). A retired connection still serves runs it already
        # has consumers for, but never relays or delivers proactively and
        # never reconnects once those runs are gone.
        self._retired = False
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

        # Proactive-delivery bookkeeping. When running inside OWUI these four
        # dicts/sets are the *same objects* for every `_GatewayConnection` in
        # the process (see `_shared_gateway_state`), so a turn shown live by
        # one connection is seen as delivered by all of them and a zombie
        # connection left by a previous deploy can no longer re-deliver a live
        # turn as a bogus "*↳ Proactive message*" (P33, 2026-07-11). Outside
        # OWUI (unit tests) each connection gets its own isolated dicts.
        _shared = _shared_gateway_state()

        # Dedup guard for proactive OWUI delivery (P33/ELI-17), keyed by
        # "session_key:run_id" so a retried/duplicated final event -- or a
        # second (zombie) connection racing the same idle session -- never
        # writes the same message into chat history twice.
        self._delivered_proactive: dict[str, bool] = (
            _shared["delivered_proactive"] if _shared is not None else {}
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
        self._delivered_live: dict[str, float] = (
            _shared["delivered_live"] if _shared is not None else {}
        )

        # Last time *any* consumer for a session was registered or
        # unregistered (P33/ELI-19 second incident, 2026-07-11). A bare
        # `has_any_consumer_for_session() is False` check is racy at
        # steering-leg / OWUI-request boundaries: a leg can unregister its
        # consumer the instant its HTTP request ends, and the browser tab
        # sends the *next* leg's request (e.g. answering an ask-user modal)
        # a few seconds later under a brand-new run_id. During that gap the
        # session has zero registered consumers yet is not actually
        # abandoned -- a "final" event landing in that window used to get
        # proactively delivered as a duplicate into a chat the user was
        # still actively looking at. Recording the timestamp on every
        # register/unregister lets `session_idle_for` require a minimum
        # quiet period with zero consumers before trusting the session is
        # genuinely idle, which a few-second leg gap cannot satisfy. Shared
        # across connections so a zombie sees the live connection's consumer
        # register/unregister activity and treats the session as busy, not
        # idle.
        self._session_last_activity: dict[str, float] = (
            _shared["session_last_activity"] if _shared is not None else {}
        )

        # Sessions currently being watched by a debounce task waiting for
        # `session_idle_for` to clear (see `_maybe_deliver_proactive_after_debounce`).
        # Prevents spawning a second concurrent watcher for the same session
        # when multiple unmatched "final" events arrive close together --
        # which would otherwise let both watchers independently observe
        # idleness later and each call `_deliver_proactive_owui_message`
        # with a *different* run_id, defeating the per-run dedup guard. Shared
        # across connections so two connections (e.g. current + a zombie) never
        # each spawn their own watcher for the same session.
        self._pending_proactive_debounce: set[str] = (
            _shared["pending_proactive_debounce"] if _shared is not None else set()
        )
        # Every unwatched run a session's watcher holds, oldest first. The one
        # watcher delivers each of them with that run's own text, so a second
        # run ending inside the debounce window is queued, not skipped
        # (2026-10-12).
        self._proactive_runs: dict[str, list[str]] = (
            _shared["proactive_runs"] if _shared is not None else {}
        )
        # The newest run that ended in each session, watched or not. A run
        # whose text can't be found by its id may only fall back to "the
        # session's newest reply" while it is still the newest run.
        self._last_final_run: dict[str, str] = (
            _shared["last_final_run"] if _shared is not None else {}
        )

        # Per-chat write locks serializing the two proactive-delivery paths
        # (`_deliver_proactive_owui_message` and
        # `_deliver_subagent_proactive_owui_message`) against each other for a
        # given chat. Both read the chat's current leaf, then append a new
        # child; without a lock two deliveries landing close together each
        # read the same leaf and append as *siblings*, which OWUI renders as a
        # 1/2·2/2 variant group instead of a linear sequence (2026-07-13).
        # Shared across connections so a zombie connection can't race the live
        # one on the same chat. Keyed by chat_id.
        self._chat_write_locks: dict[str, asyncio.Lock] = (
            _shared["chat_write_locks"] if _shared is not None else {}
        )

        # Parity bookkeeping (2026-07-19). For a run that a live pipe() call is
        # rendering into a known OWUI message, `_run_targets[key]` holds that
        # message's (chat_id, message_id) and `_run_renderers[key]` a
        # `_TurnRenderer` accumulating the full content from this run's event
        # stream -- so if the inline turn ends before the run does (idle
        # self-close / cancel / torn-down request), the run's final can finalize
        # the ORIGINAL message to the complete content instead of stranding the
        # tail in a truncated, tool-block-less proactive bubble. Instance-local
        # (not shared): only the singleton connection runs pipe() and dispatches
        # its own events; a zombie connection never registers a target.
        self._run_targets: dict[str, dict] = {}
        self._run_renderers: dict[str, "_TurnRenderer"] = {}

        # ELI-62 live-relay bookkeeping (slice 2). Keyed by "session_key:run_id",
        # holds a `_RelayState` for every PROACTIVE run currently being streamed
        # out-of-band into an open OWUI tab (see LIVE_STREAM_RELAY_ENABLED). Only
        # populated when the kill switch is on; instance-local because only the
        # singleton connection drives relay emits, and cross-connection dedup is
        # already handled via the shared `_delivered_proactive` claim at
        # introduce time (a zombie sees the claim and never double-introduces).
        self._relay_sessions: dict[str, "_RelayState"] = {}

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
        for key in list(self._message_subscriptions):
            await self._subscribe_messages(key)
        # A relay whose run settled while the socket was down never hears its
        # final or the settle push; ask once, now that pushes flow again.
        for state in [s for s in self._relay_sessions.values() if s.gap]:
            try:
                lst = await self.send_request("sessions.list", {}, timeout=15)
            except Exception as ex:
                pipe_log(f"  relay gap check failed: {ex}")
                break
            rows = {r.get("key"): r for r in (lst.get("sessions") or [])}
            row = rows.get(state.session_key)
            if row is not None and row.get("hasActiveRun") is False:
                spawn_background(_relay_finalize_settled(self, state))
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

    def _on_sessions_changed(self, payload: dict) -> None:
        """A session row changed. Waiters on its run state re-read it, and an
        OWUI session whose run just started gets a transcript subscription
        before its first tool call (measured: run start is pushed ~3s ahead of
        the first `session.tool`)."""
        key = payload.get("sessionKey") or payload.get("key") or ""
        if key and "hasActiveRun" in payload:
            self._wake_session_waiters(key)
        if key and payload.get("hasActiveRun") is False:
            for state in list(self._relay_sessions.values()):
                if state.session_key == key:
                    spawn_background(_relay_finalize_settled(self, state))
        if (payload.get("hasActiveRun") and key
                and key not in self._message_subscriptions
                and self.parse_owui_session_key(key)):
            spawn_background(self.ensure_message_subscription(key))

    def register_run_target(self, session_key: str, run_id: str,
                            chat_id: str | None, message_id: str | None) -> None:
        """Remember that this run streams into OWUI message `message_id` in
        `chat_id`, and start a shadow `_TurnRenderer` for it. No-op without both
        ids (e.g. a chat with no stable message id yet)."""
        if not (chat_id and message_id and run_id):
            return
        key = f"{session_key}:{run_id}"
        self._run_targets[key] = {"chat_id": chat_id, "message_id": message_id}
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

    def _chat_write_lock(self, chat_id: str) -> asyncio.Lock:
        """Return the process-wide `asyncio.Lock` for serializing proactive
        writes into `chat_id`, creating it on first use."""
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
            # kept relaying every proactive run with its own (older) code.
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
        q = asyncio.Queue(maxsize=500)
        consumer.queues.append(q)
        self._touch_session_activity(session_key)
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
        self._touch_session_activity(session_key)
        pipe_log(f"  unregistered consumer: {key[:60]}...")
        if self._retired and not self._consumers:
            pipe_log("  retired gateway connection: last run finished, closing")
            spawn_background(self.disconnect())

    def _touch_session_activity(self, session_key: str):
        self._session_last_activity[session_key] = time.time()
        if len(self._session_last_activity) > 500:
            oldest_first = sorted(self._session_last_activity.items(), key=lambda kv: kv[1])
            for stale_key, _ in oldest_first[:250]:
                self._session_last_activity.pop(stale_key, None)

    def mark_delivered_live(self, session_key: str, run_id: str):
        """Record that this run's final event was just dispatched to a live
        consumer queue -- i.e. genuinely shown to an open browser tab, which
        persists it itself via OWUI's own client-side flow. See
        `_delivered_live`'s docstring for why this is the precise signal to
        gate proactive delivery on."""
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

    def has_any_consumer_for_session(self, session_key: str) -> bool:
        """True if *any* run (not just a specific run_id) currently has a
        live pipe() call registered for this session.

        Used to gate proactive delivery (P33/ELI-17/ELI-19): an event can
        fail to match `consumers_for_event` (e.g. its run_id belongs to a
        just-superseded steering leg, or it's a bare session-only event with
        zero or multiple session matches) while the session is still
        genuinely being serviced by an open browser tab under a *different*
        run_id. Proactive delivery must only fire when the session has no
        active consumer at all -- otherwise it risks injecting a duplicate
        message into a conversation that's live right now (see 2026-07-11
        incident in PLAN.md P33).
        """
        return any(
            consumer.session_key == session_key
            for consumer in self._consumers.values()
        )

    def session_idle_for(self, session_key: str, min_idle_s: float = 120) -> bool:
        """True only if the session has had *zero* registered consumers for
        at least `min_idle_s` seconds (P33/ELI-19, second incident,
        2026-07-11).

        `has_any_consumer_for_session` alone is racy: it can momentarily
        read False in the gap between one leg's HTTP request ending
        (unregister) and the browser's next request for the same logical
        turn arriving (register) -- a few seconds, not the minutes a
        genuinely abandoned session sits idle for. Requiring a sustained
        quiet period turns a race into a simple timing margin: a real
        steering/modal round trip resolves in low single-digit seconds,
        while a proactive wake (cron, heartbeat, bare sessions_send) has no
        pending browser request at all, so it always clears the bar.

        A session_key never seen by this connection (no register/unregister
        recorded -- e.g. this is a fresh process, or the previous owner of
        this session was a now-reaped zombie connection) has no activity to
        race against, so it's treated as idle immediately.
        """
        if self.has_any_consumer_for_session(session_key):
            return False
        last_activity = self._session_last_activity.get(session_key)
        if last_activity is None:
            return True
        return (time.time() - last_activity) >= min_idle_s

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

    def is_subagent_session_key(self, session_key: str) -> bool:
        """True for a sub-agent's own session key, e.g.
        "agent:<agentId>:subagent:<uuid>" (`sessions_spawn`/Task tool
        format, docs/tools/subagents.md). These never have an OWUI tab
        talking to them directly -- only their *requester* (parent) session
        can be an OWUI chat -- so they're never matched by
        `parse_owui_session_key` and need `resolve_subagent_parent_task` to
        find who to deliver to.
        """
        return ":subagent:" in session_key

    async def resolve_subagent_parent_task(self, session_key: str) -> dict | None:
        """Look up the task record for a sub-agent's own session key via
        `tasks.list`.

        The Gateway's tasks.list handler matches `params.sessionKey` against
        a task's requesterSessionKey, childSessionKey, *or* ownerKey (any of
        the three) -- so passing the *child's* session key here still finds
        the task, and the returned record's `sessionKey` field is the
        *parent's* (requester's) session key, not the child's. That's the
        one piece of information `parse_owui_session_key` alone can't get to,
        since a sub-agent session key has no positional relationship to the
        OWUI session that spawned it.

        Crucially, a single sub-agent run produces *two* task records whose
        `childSessionKey` is this session: the spawn/announce task, whose
        `sessionKey` (requesterSessionKey) is the real OWUI parent, AND a
        second `runtime="cli"` execution task that is self-referential --
        its requesterSessionKey is the sub-agent's *own* key. The latter is
        created a few ms later, so tasks.list (newest-first) returns it
        first. Returning the first `childSessionKey` match therefore hands
        back the self-referential record, whose `sessionKey` is the child
        itself -> `parse_owui_session_key` fails and delivery silently
        aborts. So we must skip past any record whose `sessionKey` doesn't
        actually resolve to an OWUI chat and keep looking for the one that
        does. `limit` is generous enough to include both records.
        """
        try:
            resp = await self.send_request(
                "tasks.list", dict(sessionKey=session_key, limit=25), timeout=8,
            )
        except Exception as ex:
            pipe_log(f"  subagent parent lookup failed: {ex}")
            return None
        for task in (resp or {}).get("tasks", []):
            if task.get("childSessionKey") != session_key:
                continue
            parent = task.get("sessionKey")
            if parent and self.parse_owui_session_key(parent):
                return task
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
        """Fetch the session's recent messages unclipped, for timeout recovery."""
        return await self.send_request(
            "chat.history",
            dict(sessionKey=session_key, limit=limit, maxChars=max_chars),
            timeout=timeout
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

    async def _abort_cancelled_run(self, session_key: str, run_id: str,
                                   send_stop: bool) -> None:
        await self.abort(session_key, run_id)
        if send_stop:
            pipe_log("Generator cancelled -- sending /stop fallback")
            await self.send_stop(session_key)

    def park_cancelled_turn(self, session_key: str, run_id: str | None,
                            queue: asyncio.Queue | None, carried: list[str],
                            grace_s: float, send_stop: bool,
                            run_text: dict | None = None) -> None:
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

        The caller then asks the browser which button was pressed and calls
        `settle_parked_turn`: Stop releases it now, Send now holds it for the
        follow-up message. `grace_s` is only the fallback for a client that
        cannot say (no page hook, API use). `claim_cancelled_turn` takes all
        of it over; if nothing claims it, the run is aborted, the queue
        released and the carried messages dropped (the user stopped them).

        Synchronous on purpose: it runs on the generator's cancel/close path,
        where an await can itself be interrupted, so nothing here may depend
        on one. The Gateway calls go to background tasks.
        """
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
            # back-filled into the stopped message, never re-delivered as a
            # proactive bubble.
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

    async def claim_cancelled_turn(self, session_key: str,
                                   wait_s: float = 3.0) -> dict | None:
        """Take over the turn `park_cancelled_turn` parked, if any.

        Waits up to `wait_s` for `settle_parked_turn`, so a Stop whose answer
        is still on its way is honored: OWUI follows a Stop with the next
        queued message, which must start fresh rather than continue the run
        the user just stopped.

        Returns {"run_id", "queue", "carried"}; the caller now owns the run,
        its buffered queue and the carried messages.
        """
        entry = self._pending_cancel_aborts.get(session_key)
        if entry is None:
            return None
        if not entry["resolved"].is_set():
            entry["claiming"] = True
            try:
                await asyncio.wait_for(entry["resolved"].wait(), wait_s)
            except asyncio.TimeoutError:
                pass
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
        own target. Without this its parity finalize would miss the buffered
        text and duplicate the rest."""
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

    async def retire(self):
        """Step down in favor of a newer deploy's connection.

        Runs that already have a consumer here keep being served until they
        unregister (cutting them would lose the reply); everything else stops
        now. Once the last consumer leaves, the connection closes itself and
        `ensure_connected` refuses to bring it back.
        """
        self._retired = True
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
                q.put_nowait({"type": _GAP_MARKER_TYPE})
                n += 1
        if n:
            pipe_log(f"  marked an event gap in {n} run queue(s)")
        for state in self._relay_sessions.values():
            state.gap = True
        self._wake_session_waiters()

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
            return
        if self.gateway_boot_id is not None and boot_id != self.gateway_boot_id:
            self.gateway_restarts += 1
            pipe_log(f"  Gateway restarted (bootId {self.gateway_boot_id[:8]} -> {boot_id[:8]})")
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
        self._reconnect_attempt = 0
        self.connection_epoch += 1
        self._thinking_note_shown.clear()
        self._note_gateway_boot((hello_ok.get("server") or {}).get("bootId"))
        pipe_log(
            f"Connected to Gateway (persistent) [epoch {self.connection_epoch}]"
        )
        # Subscriptions belong to the socket. Their requests resolve on the
        # event loop, which is not reading yet here, so it renews them.
        self._subscriptions_stale = True
        # NOTE: does not start/spawn the event-loop task -- that happens
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
                if msg.get("type") == "event" and msg.get("event") == "session.tool":
                    # Same payload as an `agent` tool event (stream "tool",
                    # data.phase/name/toolCallId/args/result), so it takes the
                    # same path: inline turn, shadow renderer, live relay.
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

                # ── Parity shadow render (2026-07-19) ──
                # Feed every event of a run we're tracking into its shadow
                # `_TurnRenderer`, whether or not a live consumer is still
                # attached: the inline turn can end at any point, and we must
                # already hold the complete content to finalize its message.
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

                # ── ELI-62 live relay (step 4, slice 2) ──
                # When enabled (kill switch default OFF -- see
                # LIVE_STREAM_RELAY_ENABLED), relay a PROACTIVE run's deltas
                # straight into an already-open OWUI tab as they stream,
                # instead of only persisting the finished text post-hoc. Only
                # engages for a run with NO live consumer on a genuinely-idle
                # OWUI session; a run a live pipe() call is already servicing
                # keeps the normal inline path untouched. Fully inert when the
                # switch is off, so default behavior is byte-for-byte identical.
                _relay_key = (
                    f"{_evt_sess}:{_evt_run}" if (_evt_sess and _evt_run) else None
                )
                if payload.get("state") == "final" and _evt_sess and _evt_run:
                    self._last_final_run[_evt_sess] = _evt_run
                if LIVE_STREAM_RELAY_ENABLED and _relay_key in self._relay_sessions:
                    if consumers:
                        # A live consumer appeared for a run we were relaying --
                        # hand the run back to the inline path (finalize our
                        # out-of-band bubble so it isn't left pending) and let
                        # the normal dispatch below take over.
                        await _relay_abort(self, _relay_key)
                    else:
                        await _relay_feed_event(self, _evt_sess, _evt_run, payload)
                        continue
                elif (
                    LIVE_STREAM_RELAY_ENABLED
                    and not self._retired
                    and _relay_key is not None
                    and not consumers
                    and payload.get("state") != "final"
                    and self.parse_owui_session_key(_evt_sess)
                    and not self.has_any_consumer_for_session(_evt_sess)
                    and self.session_idle_for(_evt_sess, min_idle_s=_RELAY_MIN_IDLE_S)
                    and not self.was_delivered_live(_evt_sess, _evt_run)
                    and _relay_key not in self._delivered_proactive
                ):
                    await _relay_begin(self, _evt_sess, _evt_run, payload)
                    continue

                if consumers:
                    self._event_count += 1
                    if not payload.get("runId"):
                        pipe_log("  dispatched session-only event to sole consumer")
                    if payload.get("state") == "final" and payload.get("sessionKey") and payload.get("runId"):
                        # Genuinely shown to a live tab, which persists it via
                        # OWUI's own client-side flow -- remember this so a
                        # later duplicate/retried final event for the same
                        # run_id is never proactively re-delivered (P33).
                        self.mark_delivered_live(payload["sessionKey"], payload["runId"])
                        # The inline turn is alive and took the final itself, so
                        # the parity shadow isn't needed for this run -- drop it.
                        self.unregister_run_target(payload["sessionKey"], payload["runId"])
                    for q in consumers[0].queues:
                        await q.put(msg)
                    continue

                # ── Parity finalize (2026-07-19) ──
                # A final arrived with NO live consumer, but we were tracking
                # this run's OWUI message -- i.e. the inline pipe() turn ended
                # before the run did. Complete the ORIGINAL message with the
                # full shadow-rendered content, in place, instead of letting
                # the tail fall through to the detached/truncated proactive
                # new-bubble path below.
                if (
                    payload.get("state") == "final"
                    and _evt_sess and _evt_run
                    and f"{_evt_sess}:{_evt_run}" in self._run_targets
                ):
                    pipe_log(
                        f"  parity: inline turn ended early; finalizing message "
                        f"for run {_evt_run[:20]}..."
                    )
                    spawn_background(
                        _finalize_inline_message(self, _evt_sess, _evt_run)
                    )
                    continue

                # ── Proactive delivery for idle OWUI sessions (P33/ELI-17/ELI-19) ──
                # Nobody is live-consuming this *exact* event -- no browser tab
                # is mid-request for this precise session+run. That alone is
                # NOT enough to prove the session is idle: this event's run_id
                # can simply not match a *different*, still-active run on the
                # same session (steering handoff, or a bare session-only event
                # with zero/multiple matches) -- proactively delivering in that
                # case injects a duplicate into a live conversation (real
                # incident, 2026-07-11, see PLAN.md P33). Even
                # `has_any_consumer_for_session` alone isn't enough: it's a
                # point-in-time read that can be momentarily False in the gap
                # between one leg's HTTP request ending and the next leg's
                # request arriving a few seconds later (second 2026-07-11
                # incident). So this branch hands off to a debounce watcher
                # (`_maybe_deliver_proactive_after_debounce`) that only
                # delivers once the session has been genuinely consumer-free
                # for a sustained quiet period, not just at this instant. On
                # top of that, `was_delivered_live` gates out a *duplicate or
                # retried* final event for a run that was already genuinely
                # shown to a live tab -- that scenario doesn't depend on
                # timing at all (the tab may have gone quiet for well over
                # the debounce window by the time the retry arrives), so it
                # needs its own identity-based check, not a longer timer.
                evt_session = payload.get("sessionKey", "")
                evt_run_id = payload.get("runId", "")
                if (
                    PROACTIVE_DELIVERY_ENABLED
                    and not self._retired
                    and evt_session
                    and payload.get("state") == "final"
                ):
                    if (
                        self.parse_owui_session_key(evt_session)
                        and not self.has_any_consumer_for_session(evt_session)
                        and not self.was_delivered_live(evt_session, evt_run_id)
                    ):
                        queued = self._proactive_runs.setdefault(evt_session, [])
                        if evt_run_id not in queued:
                            queued.append(evt_run_id)
                        if evt_session not in self._pending_proactive_debounce:
                            self._pending_proactive_debounce.add(evt_session)
                            spawn_background(
                                _maybe_deliver_proactive_after_debounce(
                                    self, evt_session, evt_run_id
                                )
                            )
                        continue

                # ── Proactive delivery for finished sub-agent tasks ──
                # A sub-agent's own session key (`agent:*:subagent:*`) never
                # matches `parse_owui_session_key` -- it's not an OWUI
                # session at all, so the branch above never fires for it and
                # it always reaches here. Resolving *which* OWUI chat (if
                # any) should hear about it requires an RPC
                # (`resolve_subagent_parent_task`), so this can't reuse the
                # sync gate above -- it hands off immediately to a task that
                # does the lookup first, then the same idle-debounce dance,
                # but checked against the *parent's* liveness, not the
                # sub-agent's own (which has no meaning -- no tab ever talks
                # to a sub-agent session directly, so it would always read
                # "idle" and defeat the point of debouncing at all).
                if (
                    PROACTIVE_DELIVERY_ENABLED
                    and not self._retired
                    and evt_session
                    and payload.get("state") == "final"
                    and self.is_subagent_session_key(evt_session)
                    and evt_session not in self._pending_proactive_debounce
                ):
                    self._pending_proactive_debounce.add(evt_session)
                    spawn_background(
                        _maybe_deliver_subagent_proactive(
                            self, evt_session, evt_run_id
                        )
                    )
                    continue

                if payload.get("sessionKey") and not payload.get("runId"):
                    pipe_log("  dropped ambiguous or unmatched session-only event")

                # If nothing matches at all, drop it.
                # This efficiently filters cross-session bleed (P16).

            except asyncio.TimeoutError:
                # No events for 90s but connection is still alive
                continue
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
                pipe_log(f"Event loop error: {e}")
                self._ws = None
                self._mark_event_gap()

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
# of these as its only "assistant text", and naively persisting it as a
# proactive OWUI message leaks internal plumbing into the chat (P33,
# 2026-07-11: `ANNOUNCE_SKIP` landed as literal message text).
_SILENT_SENTINELS = {"ANNOUNCE_SKIP", "NO_REPLY", "no_reply"}


def _last_assistant_text_from_preview(preview: dict, session_key: str) -> str | None:
    """Return the last assistant message text for session_key in a
    `sessions.preview` response (same shape `_preview_recovery_text` reads),
    without requiring a preceding user-text match -- a proactive delivery
    (cron/heartbeat/sessions_send) has no live "user turn" in this pipe to
    anchor against, we just want whatever the run finished saying.
    """
    previews = preview.get("previews")
    if not isinstance(previews, list):
        return None
    entry = next(
        (p for p in previews if isinstance(p, dict) and p.get("key") == session_key),
        None,
    )
    if not entry:
        return None
    items = entry.get("items")
    if not isinstance(items, list):
        return None
    for item in reversed(items):
        if isinstance(item, dict) and item.get("role") == "assistant":
            text = str(item.get("text", "")).strip()
            if text and text not in _SILENT_SENTINELS:
                return text
            if text in _SILENT_SENTINELS:
                return None
    return None


# How far back a proactive delivery reads the session transcript to find the
# whole version of a clipped preview item. The run's closing assistant message
# is normally the newest; a few extra rows cover trailing tool-only messages.
PROACTIVE_HISTORY_LIMIT = 8

# The Gateway marks a clipped preview item with a trailing "..." (800 chars
# per item on 2026.9.x, 2000 before). Below this many chars before the marker
# the text is too short to be a clip, and too weak an anchor to look for.
PREVIEW_CLIP_MARKER = "..."
PREVIEW_CLIP_MIN_PREFIX = 200


def _preview_clip_prefix(text: str) -> str | None:
    """The kept part of a clipped `sessions.preview` item, or None when the
    item does not look clipped (it is then already the whole message)."""
    if not text.endswith(PREVIEW_CLIP_MARKER):
        return None
    prefix = text[: -len(PREVIEW_CLIP_MARKER)].strip()
    return prefix if len(prefix) >= PREVIEW_CLIP_MIN_PREFIX else None


def _history_text_completing_preview(history: dict, clip_prefix: str) -> str | None:
    """The whole text of the `chat.history` message a clipped preview item
    was cut from: the newest assistant message whose text contains the kept
    prefix.

    Anchored on the preview on purpose. `chat.history` hides rows the preview
    keeps (heartbeat prompts, sub-agent completion reports) and drops silent
    replies (NO_REPLY, ANNOUNCE_SKIP), so "the newest assistant text in
    chat.history" after a silent run is an OLDER run's reply, which would be
    re-delivered as a proactive message.
    """
    messages = (history or {}).get("messages")
    if not isinstance(messages, list) or not clip_prefix:
        return None
    for message in reversed(messages):
        item = _history_item(message)
        if not item or item.get("role") != "assistant":
            continue
        text = item["text"].strip()
        if clip_prefix in text:
            return text
    return None


# How far back a proactive delivery looks for the run's own closing message.
# Other runs may have ended after it (the delivery waits for the session to go
# idle), so this reaches further than PROACTIVE_HISTORY_LIMIT.
PROACTIVE_RUN_HISTORY_LIMIT = 60
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
    counts when nothing before it carried text (a plain, tool-free reply)."""
    r = _TurnRenderer()
    closing = None
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
        keys = (str(message.get("idempotencyKey") or ""), str(meta.get("idempotencyKey") or ""))
        if any(k.startswith("cli-assistant:") for k in keys):
            closing = message
            continue
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                r.feed({"stream": "assistant", "data": {"delta": block["text"]}})
            elif kind == "toolcall" and block.get("id"):
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
    if closing is not None and not r.text.strip():
        text = ((_history_item(closing) or {}).get("text") or "").strip()
        if text and text not in _SILENT_SENTINELS:
            r.feed({"stream": "assistant", "data": {"delta": text}})
    return r


def _history_run_messages(history: dict, run_id: str) -> list | None:
    """One run's messages in `chat.history`: everything after the user
    message that started it, through its closing message
    (`cli-assistant:<runId>`). None when the run has not closed yet."""
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
        while start > 0 and (messages[start - 1] or {}).get("role") != "user":
            start -= 1
        return messages[start:end + 1]
    return None


async def _proactive_run_text(conn, session_key: str, log_prefix: str,
                              run_id: str | None = None,
                              out: dict | None = None) -> str | None:
    """The finished run's assistant text for a proactive delivery.

    With `run_id`, the run's own closing message is looked up by identity
    first (`_history_text_for_run`). The delivery waits for the session to go
    idle, and by then a LATER run may have replied: "the session's newest
    reply" was that later run's text, so it was delivered twice and this run's
    reply never (2026-10-12, a message sent while a sub-agent's completion run
    was going). When the run can't be found by id, the newest-reply fallback
    below is only used while this run is still the session's newest run.

    `sessions.preview` (limit 1) decides WHETHER this run left text to show:
    its newest item is the run's last visible row, so a run that ended silent
    or on its own prompt yields nothing. The Gateway clips every preview item
    (800 chars on 2026.9.x) whatever `maxChars` asks for, so a clipped item
    is then completed from `chat.history`, which returns the message whole
    (same source as `_recover_turn_text`, ELI-74). If `chat.history` fails or
    has no matching message, the clipped preview is delivered rather than
    nothing.
    """
    history = None
    if run_id:
        try:
            # Wide enough for the whole run: `out["history"]` also feeds its
            # tool cards (`_proactive_run_output`).
            history = await conn.session_history(session_key, limit=RUN_HISTORY_LIMIT)
        except Exception as ex:
            pipe_log(f"  {log_prefix}: chat.history failed: {ex}")
            history = None
        if out is not None:
            out["history"] = history
        found, run_text = _history_text_for_run(history, run_id)
        if found:
            return run_text
        newest = getattr(conn, "_last_final_run", {}).get(session_key, run_id)
        if newest != run_id:
            pipe_log(
                f"  {log_prefix}: run {run_id[:20]}... has no reply of its own in "
                f"chat.history and run {str(newest)[:20]}... ended after it; "
                "not delivering the newer run's text"
            )
            return None
    try:
        preview = await conn.session_preview(session_key, limit=1, max_chars=8000)
    except Exception as ex:
        pipe_log(f"  {log_prefix}: session_preview failed: {ex}")
        return None
    text = _last_assistant_text_from_preview(preview, session_key)
    clip_prefix = _preview_clip_prefix(text) if text else None
    if not clip_prefix:
        return text
    if history is None:
        try:
            history = await conn.session_history(session_key, limit=PROACTIVE_HISTORY_LIMIT)
        except Exception as ex:
            pipe_log(f"  {log_prefix}: chat.history failed: {ex}; delivering the clipped preview")
            return text
    full = _history_text_completing_preview(history, clip_prefix)
    if full:
        return full
    pipe_log(f"  {log_prefix}: no chat.history message matches the clipped preview; delivering it as is")
    return text


def _proactive_run_output(history: dict | None, run_id: str,
                          label: str) -> list | None:
    """The run's `output` items, rebuilt from `chat.history`, when it used
    tools: OWUI 0.12 draws a message from `output` when it has one, and a run
    delivered after the fact (a sub-agent's parent resuming, a wake) never
    streamed through a reply that could have shown its cards (ELI-92). None
    for a tool-free run, which keeps the plain-text message. `label` leads
    the first text item, as it leads `content`."""
    run_messages = _history_run_messages(history, run_id)
    if not run_messages:
        return None
    output = _history_renderer(run_messages).finished_output()
    if not any(i.get("type") == "function_call" for i in output):
        return None
    if not label:
        pass
    elif output[0].get("type") == "message":
        output[0]["content"][0]["text"] = label + output[0]["content"][0]["text"]
    else:
        output.insert(0, {"type": "message", "id": f"msg_{uuid.uuid4().hex[:24]}",
                          "status": "completed", "role": "assistant",
                          "content": [{"type": "output_text", "text": label.rstrip()}]})
    return output


async def _maybe_deliver_proactive_after_debounce(
    conn: "_GatewayConnection",
    session_key: str,
    run_id: str,
    min_idle_s: float = 120,
    poll_interval_s: float = 5,
    max_wait_s: float = 600,
) -> None:
    """Wait out `session_idle_for`'s quiet-period requirement before handing
    off to `_deliver_proactive_owui_message` (P33/ELI-19, second incident).

    Re-polls rather than sleeping once for `min_idle_s`, because the
    session's idle clock can restart at any point (a new leg registers a
    consumer, runs, then unregisters again) -- a single fixed sleep would
    miss that and could still deliver during a live steering round trip. If
    the session never settles within `max_wait_s`, gives up rather than
    delivering into a session whose liveness can't be confirmed; also bails
    immediately if the kill switch is flipped back off mid-wait.
    """
    try:
        waited = 0.0
        while not conn.session_idle_for(session_key, min_idle_s=min_idle_s):
            if not PROACTIVE_DELIVERY_ENABLED or conn._retired:
                pipe_log("  proactive delivery: disabled mid-debounce, abandoning wait")
                return
            if waited >= max_wait_s:
                pipe_log(
                    f"  proactive delivery: session {session_key[:40]}... never "
                    f"settled idle within {max_wait_s:.0f}s, giving up"
                )
                return
            await asyncio.sleep(poll_interval_s)
            waited += poll_interval_s
        # Every run queued while this watcher waited, oldest first; a run
        # that ends while these are delivered joins the same list.
        queued = conn._proactive_runs.setdefault(session_key, [])
        if run_id not in queued:
            queued.insert(0, run_id)
        while queued:
            rid = queued.pop(0)
            if conn.was_delivered_live(session_key, rid):
                # The run got shown to a (re)connected live tab while we were
                # waiting out the debounce window -- that tab already persists
                # it via OWUI's own client-side flow, so proactively delivering
                # now would be a pure duplicate.
                pipe_log(f"  proactive delivery: {session_key[:40]}... run "
                          f"{rid[:20]}... was delivered live during debounce wait, skipping")
                continue
            await _deliver_proactive_owui_message(conn, session_key, rid)
    finally:
        conn._proactive_runs.pop(session_key, None)
        conn._pending_proactive_debounce.discard(session_key)


def _deepest_leaf_id(messages: dict, start_id: str | None) -> str | None:
    """Walk down `childrenIds` from `start_id` to the deepest childless node.

    OWUI's `history.currentId` is only a *view* pointer: the frontend (or an
    interleaved live turn) can reset it back to an ancestor that already has
    children -- e.g. right after a proactive message was appended. Anchoring a
    new proactive message directly on `currentId` in that state appends it as
    a *sibling* of the existing child, which OWUI renders as a 1/2·2/2 variant
    group instead of a linear message (observed 2026-07-13: a `Sub-agent
    finished` message and the next proactive message both parented to the same
    node). Walking to the true leaf (following the most-recently-appended
    child at each branch) makes every proactive write chain off the real tail.
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


def _is_proactive_message(msg: dict) -> bool:
    """A message this bridge delivered out-of-band (proactive / sub-agent
    finished), recognizable by the `*↳` prefix its content always carries."""
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return False
    head = (msg.get("content") or "")[:48]
    return "*↳ Proactive message*" in head or "*↳ Sub-agent" in head


def _relinearize_proactive_variants(history: dict) -> bool:
    """Heal 1/2·2/2 variant groups an out-of-band proactive delivery created,
    so the proactive message sits in the normal linear flow instead of behind
    a swipe arrow.

    Cause: OWUI can't live-append a new message to an already-open tab, so a
    proactive message is written to the DB while the tab's `currentId` is
    stale. When the user then sends their next message, the frontend parents it
    to the PRE-proactive leaf -- making the proactive message and the new
    message siblings of one node, which OWUI renders as a swipeable 1/2·2/2
    variant group (observed live 2026-07-19: 7 such groups, each an assistant
    node with a proactive child + a user child).

    This re-chains any such group into one linear path: for a node whose
    children are one-or-more (leaf) proactive messages plus AT MOST ONE
    non-proactive continuation, rewire them parent → proactive(oldest→newest) →
    continuation. Genuine user-regeneration variants (2+ non-proactive
    children) are left untouched; a group where a proactive node already has
    children is skipped (already chained), which also makes this idempotent.
    Returns True if anything changed.
    """
    messages = history.get("messages")
    if not isinstance(messages, dict):
        return False
    changed = False
    for parent_id, parent in list(messages.items()):
        if not isinstance(parent, dict):
            continue
        kids = list(parent.get("childrenIds") or [])
        if len(kids) < 2:
            continue
        proactive = [k for k in kids if _is_proactive_message(messages.get(k) or {})]
        others = [k for k in kids if k not in proactive]
        if not proactive or len(others) > 1:
            continue
        proactive.sort(key=lambda k: (messages.get(k) or {}).get("timestamp") or 0)
        chain = proactive + others  # `others` is [] or exactly [one]
        # Everything after chain[0] gets a new parentId. OWUI 0.12's
        # `Chats.update_chat_by_id` keeps the STORED copy of any assistant
        # message whose `done` is False, so re-parenting a still-streaming
        # reply (a live-relay proactive message, or a turn in flight) is
        # silently dropped while the rest of the group moves. Leave the whole
        # group for a later turn's heal, when that message is finished.
        if any(
            (messages.get(k) or {}).get("role") == "assistant"
            and (messages.get(k) or {}).get("done") is False
            for k in chain[1:]
        ):
            continue
        # Keep only the first node under the parent; chain the rest under the
        # deepest leaf of the previous node's existing subtree (a proactive
        # message may already anchor its own tail -- e.g. a later proactive
        # chained under it -- so we must extend, not overwrite, its children).
        parent["childrenIds"] = [chain[0]]
        first = messages.get(chain[0])
        if isinstance(first, dict):
            first["parentId"] = parent_id
        for i in range(len(chain) - 1):
            cur_leaf = _deepest_leaf_id(messages, chain[i])
            nxt = chain[i + 1]
            leaf_node = messages.get(cur_leaf)
            if isinstance(leaf_node, dict):
                leaf_node.setdefault("childrenIds", [])
                if nxt not in leaf_node["childrenIds"]:
                    leaf_node["childrenIds"].append(nxt)
            nxt_node = messages.get(nxt)
            if isinstance(nxt_node, dict):
                nxt_node["parentId"] = cur_leaf
        # Point the view at the true tail so the whole linear flow -- including
        # the now-inlined proactive message -- is what shows.
        history["currentId"] = _deepest_leaf_id(messages, chain[0])
        changed = True
    return changed


async def _heal_proactive_variants(conn: "_GatewayConnection", chat_id: str) -> None:
    """Best-effort: read a chat, re-linearize any proactive-created variant
    group (`_relinearize_proactive_variants`), and persist if changed. Runs at
    the start of each turn so a previous turn's stray variant is healed before
    the user (or a reload) ever has to swipe to find the proactive message.
    Silent no-op outside OWUI or on any failure."""
    if not chat_id:
        return
    try:
        from open_webui.models.chats import Chats
    except Exception:
        return
    try:
        async with conn._chat_write_lock(chat_id):
            chat = await Chats.get_chat_by_id(chat_id)
            if chat is None:
                return
            blob = chat.chat
            history = blob.get("history") if isinstance(blob, dict) else None
            if not isinstance(history, dict):
                return
            if _relinearize_proactive_variants(history):
                # update_chat_by_id resets the title to "New Chat" when the
                # passed blob has no 'title' key -- preserve it explicitly.
                if "title" not in blob:
                    blob["title"] = getattr(chat, "title", None) or "New Chat"
                await Chats.update_chat_by_id(chat_id, blob)
                pipe_log(f"  healed proactive variant group(s) in chat {chat_id[:8]}...")
    except Exception as ex:
        pipe_log(f"  proactive variant heal failed (non-fatal): {ex}")


async def _append_proactive_message_to_chat(
    conn: "_GatewayConnection", chat_id: str, text: str, *, log_prefix: str,
    out: dict | None = None, extra_fields: dict | None = None,
    adopt: dict | None = None,
) -> bool:
    """Persist one assistant message onto the tail of an OWUI chat.

    `adopt` ({"session_key", "run_id", "fields"}): when the chat's leaf is
    the orphaned reply stub of exactly this run (`_orphan_stub_for_run`,
    ELI-90), `fields` are written INTO that stub instead of appending a
    second bubble under it; `out["adopted"]` is then True and both
    `old_leaf_id` and `new_message_id` name the stub, and `out["turn"]` is
    the run's transcript slice.

    Shared write path for both proactive-delivery functions
    (`_deliver_proactive_owui_message` and its sub-agent sibling), which did
    this identically apart from log wording (~45 duplicated lines each). The
    load-bearing details are all P33-history (2026-07-11/13) and MUST stay
    identical for both callers:
      * anchor on the true childless leaf (`_deepest_leaf_id`), not the bare
        `currentId`, or the message forks a 1/2·2/2 sibling variant;
      * patch the old leaf's `childrenIds` BEFORE upserting the new message --
        `upsert_message_to_chat_by_id_and_message_id` resets `currentId` on
        every call, so doing it after would orphan the new message;
      * inherit the branch's own `model` so OWUI's frontend still resolves an
        `actions` list (the Status button) for the message;
      * hold the per-chat write lock so the two proactive paths (and any
        zombie connection) can't each read the same leaf and append siblings.

    Returns True on a successful persist. Callers own the success log line
    (its detail -- user_id, task_id -- differs); this owns the
    unavailable/not-found/failed logs, tagged with `log_prefix`.

    `out`, if given, is filled with `old_leaf_id`/`new_message_id` on success
    (ELI-62: the live-bootstrap step needs the OLD leaf id -- a message id
    already known to any open tab -- as its `execute` event target). Optional
    and additive so the three pre-existing callers that don't pass it keep
    their plain bool return untouched.
    """
    try:
        from open_webui.models.chats import Chats
    except Exception as ex:
        pipe_log(f"  {log_prefix} unavailable (not running inside OWUI process?): {ex}")
        return False

    try:
        async with conn._chat_write_lock(chat_id):
            chat = await Chats.get_chat_by_id(chat_id)
            if chat is None:
                pipe_log(f"  {log_prefix}: chat {chat_id[:8]}... not found")
                return False
            history = chat.chat.get("history", {}) or {}
            old_leaf_id = _deepest_leaf_id(history.get("messages") or {}, history.get("currentId"))
            if adopt and old_leaf_id:
                turn = await _orphan_stub_for_run(
                    conn, chat, old_leaf_id, adopt["session_key"], adopt["run_id"])
                if turn is not None:
                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        chat_id, old_leaf_id, dict(adopt.get("fields") or {}),
                    )
                    pipe_log(f"  {log_prefix}: wrote run {adopt['run_id'][:20]}... into "
                             f"its orphaned reply {old_leaf_id[:8]}... (ELI-90)")
                    if out is not None:
                        out.update(old_leaf_id=old_leaf_id, new_message_id=old_leaf_id,
                                   adopted=True, turn=turn)
                    return True
            new_message_id = str(uuid.uuid4())
            old_leaf = (history.get("messages") or {}).get(old_leaf_id, {}) if old_leaf_id else {}
            model_id = old_leaf.get("model") or next(iter(chat.chat.get("models") or []), None)

            if old_leaf_id:
                children = list(old_leaf.get("childrenIds", []))
                if new_message_id not in children:
                    children.append(new_message_id)
                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        chat_id, old_leaf_id, {"childrenIds": children},
                    )

            message_fields = {
                "role": "assistant",
                "content": text,
                "parentId": old_leaf_id,
                "childrenIds": [],
                "timestamp": int(time.time()),
            }
            if model_id:
                message_fields["model"] = model_id
                message_fields["modelName"] = old_leaf.get("modelName") or model_id
            if extra_fields:
                message_fields.update(extra_fields)

            await Chats.upsert_message_to_chat_by_id_and_message_id(
                chat_id, new_message_id, message_fields,
            )
        if out is not None:
            out["old_leaf_id"] = old_leaf_id
            out["new_message_id"] = new_message_id
        return True
    except Exception as ex:
        pipe_log(f"  {log_prefix} failed: {ex}")
        return False


# ELI-62 PoC kill switch -- default OFF. Flips on the "introduce + bootstrap"
# slice of the live-streaming design only (steps 2-3: persist the message,
# then nudge any open tab to reload so it appears without a manual refresh).
# Token-by-token relay of an in-flight run (step 4) is a separate, larger
# follow-up and is NOT gated by this flag because it doesn't exist yet.
# Do not flip on without reading the docstring on `_emit_live_bootstrap_reload`
# below: it emits straight to open tabs, bypassing OWUI's own event emitter, so
# a mistake here is visible to every connected client, not just one request.
LIVE_STREAM_BOOTSTRAP_ENABLED = True


async def _emit_live_bootstrap_reload(user_id: str, chat_id: str, target_message_id: str | None) -> None:
    """Fire-and-forget nudge: ask any open tab showing `chat_id` to reload so
    a message this bridge just persisted out-of-band (which OWUI's own
    live-refresh logic never picks up -- see `_deliver_proactive_owui_message`'s
    docstring) shows up immediately instead of waiting for the user to
    reopen/refresh the chat by hand.

    Wire protocol confirmed against the actual deployed OWUI backend
    (0.10.2, `open_webui/socket/main.py::get_event_emitter`, checked
    2026-07-25): `sio.emit('events', {chat_id, message_id, data}, room=f
    'user:{user_id}')`. The frontend's `chatEventHandler` (`Chat.svelte`,
    same version, confirmed live) drops the WHOLE event -- before even
    looking at `data.type` -- unless `history.messages[message_id]` already
    exists in that tab's in-memory history. That's why this targets
    `target_message_id` = the OLD leaf (a message id already known to any
    open tab), never the brand-new proactive message id itself, and why a
    missing/None target is a silent no-op rather than an error: an empty
    chat with no prior messages has nothing for an open tab to key off of
    anyway. `chat_id` in the payload also matters -- the handler only acts
    when `event.chat_id === $chatId`, so other chats/tabs/users are
    unaffected even though the emit targets the whole `user:{user_id}` room
    (every tab that user has open, matching OWUI's own room-wide behavior).

    PoC scope: the injected code is the constant
    `location.reload()` -- a full reload, not a targeted DOM patch. Jarring
    but simple and safe; a nicer alternative is explicitly left as a later
    exploration in the design doc, not attempted here.

    `sio.emit` (fire-and-forget), not `sio.call` -- there is no answer to
    wait for, unlike the ask-user modal's `sio.call` in askuser.py.
    """
    if not target_message_id:
        return
    try:
        from open_webui.socket.main import sio
    except Exception as ex:
        pipe_log(f"  live bootstrap unavailable (not inside OWUI process?): {ex}")
        return
    try:
        await sio.emit(
            "events",
            {
                "chat_id": chat_id,
                "message_id": target_message_id,
                "data": {"type": "execute", "data": {"code": "location.reload();"}},
            },
            room=f"user:{user_id}",
        )
        pipe_log(f"  live bootstrap: fired reload nudge for chat {chat_id[:8]}...")
    except Exception as ex:
        pipe_log(f"  live bootstrap emit failed: {ex}")


# ── ELI-62 live relay (step 4, slice 2) ────────────────────────────────────
# Kill switch -- default OFF. When on, a PROACTIVE run (cron/wake/sessions_send
# into an idle OWUI session with no live tab consuming it) is streamed straight
# into any already-open tab showing that chat, token by token, instead of only
# being persisted as finished text after the run ends (slice 1). Supersedes the
# slice-1 post-hoc reload-nudge for the same run: when relay owns a run it
# claims the run's `_delivered_proactive` identity at introduce time, so the
# post-hoc `_deliver_proactive_owui_message` path skips it.
#
# Verified against the OWUI 0.10.2 wire protocol (`message` = append
# at Chat.svelte:650, `replace` = set at :652, both applied for any KNOWN
# message id regardless of initiator; `chat:active:false` drives loadChat
# reconciliation once a pending assistant leaf exists). We emit these directly
# via `sio.emit("events", …)` (same envelope as `_emit_live_bootstrap_reload`),
# which reaches open tabs but does NOT go through OWUI's `get_event_emitter`, so
# it does NOT auto-persist -- this path owns DB writes explicitly (introduce =
# done:false, snapshots + finalize = done:true), exactly like the slice-1 /
# parity paths already do.
#
# Flipped ON in 2aada56 after the live-browser confirmation this comment used to
# be waiting for. The terminal `chat:active` envelope (see `_relay_finalize`) is
# still the least-verified part of the path, but DB persistence guarantees
# reload-correctness regardless, so an imperfect terminal event only costs a
# spinner nicety. Set to False to fall back to slice-1 post-hoc delivery.
LIVE_STREAM_RELAY_ENABLED = True

# A proactive run is only eligible for relay once its session has had zero
# consumers for at least this long -- the same sustained-idle contract the
# post-hoc debounce path uses (`session_idle_for`), so a live steering/modal
# round trip (low single-digit seconds) can never be mistaken for a genuine
# idle wake and we never race a message the user is mid-way through sending.
_RELAY_MIN_IDLE_S = 120

# Minimum seconds between successive full `replace` snapshot emits (which also
# re-persist the in-flight content to the DB for reload parity). `message`
# append deltas still flow every event for smooth streaming; the throttled
# snapshot is the correctness anchor that self-heals any append lost while a
# tab was mid-`location.reload()` from the bootstrap step.
_RELAY_SNAPSHOT_MIN_INTERVAL_S = 1.0


@dataclass
class _RelayState:
    """Per-run state machine for one live-relayed proactive run (ELI-62)."""
    session_key: str
    run_id: str
    user_id: str
    chat_id: str
    renderer: "_TurnRenderer"
    message_id: str | None = None      # None until `introduced`
    old_leaf_id: str | None = None     # bootstrap `execute` target
    introduced: bool = False
    gap: bool = False                  # events were lost while the socket was down
    adopted: bool = False              # writes into the run's orphaned reply stub (ELI-90)
    sent_output: list = field(default_factory=list)  # `output` the tab holds
    last_snapshot_output: list = field(default_factory=list)
    last_snapshot_ts: float = 0.0


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
        pipe_log(f"  live relay unavailable (not inside OWUI process?): {ex}")
        return False
    try:
        await sio.emit(
            "events",
            {"chat_id": chat_id, "message_id": message_id, "data": data},
            room=f"user:{user_id}",
        )
        return True
    except Exception as ex:
        pipe_log(f"  live relay emit failed: {ex}")
        return False


async def _relay_persist_content(conn: "_GatewayConnection", chat_id: str,
                                 message_id: str | None, content: str, *,
                                 done: bool, output: list | None = None) -> None:
    """Write the in-flight/final content into the DB message, under the same
    per-chat write lock the proactive paths use. Keeps reload parity mid-stream
    (a tab reopened while a run streams shows the last snapshot) and is the
    authoritative done:true record at run end -- the terminal socket event is
    only a live-tab nicety on top of this."""
    if not message_id:
        return
    try:
        from open_webui.models.chats import Chats
    except Exception as ex:
        pipe_log(f"  live relay persist unavailable (not inside OWUI process?): {ex}")
        return
    fields: dict = {"content": content, "done": done}
    if output:
        fields["output"] = output
    try:
        async with conn._chat_write_lock(chat_id):
            await Chats.upsert_message_to_chat_by_id_and_message_id(
                chat_id, message_id, fields,
            )
    except Exception as ex:
        pipe_log(f"  live relay persist failed: {ex}")


async def _relay_begin(conn: "_GatewayConnection", session_key: str, run_id: str,
                       payload: dict) -> None:
    """Start tracking a proactive run for live relay and feed it its first
    event. Creates the `_RelayState` (with its own `_TurnRenderer`); the actual
    introduce+bootstrap is deferred to `_relay_feed_event` until the content is
    showable, so a sentinel-only run never introduces a bubble at all."""
    parsed = conn.parse_owui_session_key(session_key)
    if not parsed:
        return
    user_id, chat_id = parsed
    key = f"{session_key}:{run_id}"
    conn._relay_sessions[key] = _RelayState(
        session_key=session_key, run_id=run_id, user_id=user_id,
        chat_id=chat_id, renderer=_TurnRenderer(),
    )
    # Bound memory: a run that somehow never emits a terminal would otherwise
    # leak its state forever. Far above any real concurrency.
    if len(conn._relay_sessions) > 100:
        for stale in list(conn._relay_sessions)[:50]:
            conn._relay_sessions.pop(stale, None)
    pipe_log(
        f"  live relay: begin for chat {chat_id[:8]}... run {run_id[:20]}..."
    )
    await _relay_feed_event(conn, session_key, run_id, payload)


async def _relay_introduce(conn: "_GatewayConnection", state: "_RelayState") -> bool:
    """Persist the assistant message (done:false) at the true chat leaf and
    nudge any open tab to reload onto it. Claims the run's proactive-dedup
    identity FIRST so the post-hoc path (and any zombie connection) never also
    delivers the same run. Returns True once the bubble exists."""
    key = f"{state.session_key}:{state.run_id}"
    if key in conn._delivered_proactive:
        # Another path (post-hoc / a zombie connection) already claimed this
        # run -- abandon relay rather than risk a duplicate bubble.
        conn._relay_sessions.pop(key, None)
        return False
    conn._delivered_proactive[key] = True
    if len(conn._delivered_proactive) > 200:
        for stale_key in list(conn._delivered_proactive)[:100]:
            if stale_key != key:
                conn._delivered_proactive.pop(stale_key, None)

    content = state.renderer.text
    output = copy.deepcopy(state.renderer.output)
    out: dict = {}
    adopt = {"session_key": state.session_key, "run_id": state.run_id,
             "fields": {"content": content, "done": False, "output": output}}
    if not await _append_proactive_message_to_chat(
        conn, state.chat_id, content, log_prefix="live relay introduce", out=out,
        adopt=adopt,
    ):
        # Persist failed -- release the claim so the post-hoc path can still try.
        conn._delivered_proactive.pop(key, None)
        conn._relay_sessions.pop(key, None)
        return False
    state.message_id = out.get("new_message_id")
    state.old_leaf_id = out.get("old_leaf_id")
    state.introduced = True
    if out.get("adopted"):
        # The stub is this run's own reply, cut off by an Open WebUI restart.
        # What streamed since then is only the tail; the transcript holds the
        # run so far, so the stub restarts from that.
        state.adopted = True
        turn = out.get("turn") or {}
        rebuilt = _history_renderer(turn.get("messages") or [])
        if rebuilt.output:
            state.renderer = rebuilt
        elif turn.get("prior_output"):
            # claude-cli writes a run into the transcript only when it ends;
            # until then the stub keeps what it showed before the restart.
            state.renderer.output[0:0] = turn["prior_output"]
        content = state.renderer.text
        output = copy.deepcopy(state.renderer.output)
    state.sent_output = output
    state.last_snapshot_output = output
    state.last_snapshot_ts = time.time()
    # Mark the freshly-appended message as still-generating (the append helper
    # doesn't set `done`); a reload before the first snapshot shows a pending
    # bubble, not a spuriously-complete one. The `output` items go in with it,
    # before the bootstrap reload below, so the reloaded tab starts from them.
    await _relay_persist_content(
        conn, state.chat_id, state.message_id, content, done=False, output=output
    )
    # Bootstrap: make any open tab reload and pick up the pending message so
    # subsequent `message`/`replace` events (which the frontend only applies to
    # KNOWN message ids) actually land. Targets the OLD leaf -- a message id the
    # tab already knows -- exactly like slice 1.
    await _emit_live_bootstrap_reload(state.user_id, state.chat_id, state.old_leaf_id)
    pipe_log(
        f"  live relay: introduced message {str(state.message_id)[:8]}... "
        f"in chat {state.chat_id[:8]}..."
    )
    return True


async def _relay_feed_event(conn: "_GatewayConnection", session_key: str,
                            run_id: str, payload: dict) -> None:
    """Feed one gateway event into a run's relay: grow the shadow renderer,
    introduce the bubble on first showable content, stream the new delta as a
    `message` append (plus a throttled full `replace` snapshot), and finalize
    on the run's terminal event."""
    key = f"{session_key}:{run_id}"
    state = conn._relay_sessions.get(key)
    if state is None:
        return
    is_final = payload.get("state") == "final"
    try:
        state.renderer.feed(payload)
    except Exception as ex:
        pipe_log(f"  live relay renderer feed failed: {ex}")

    content = state.renderer.visible_text or ""

    if not state.introduced:
        if _relay_content_is_showable(content):
            if not await _relay_introduce(conn, state):
                return
        elif is_final:
            # Run ended before any showable content (empty / sentinel-only wake)
            # -- never introduced a bubble, so just drop the state silently.
            conn._relay_sessions.pop(key, None)
            return
        else:
            return  # nothing to show yet; keep accumulating

    if is_final:
        await _relay_finalize(conn, state)
        return

    # Stream what changed as Responses events: text deltas and tool cards
    # appear in the open tab as they happen, the same items OWUI itself
    # streams for a turn it drives (ELI-92).
    output = state.renderer.output
    for ev in _relay_output_events(state.sent_output, output):
        await _relay_emit_event(
            state.user_id, state.chat_id, state.message_id,
            {"type": "response:completion", "data": ev},
        )
    state.sent_output = copy.deepcopy(output)

    # Throttled full re-anchor: a `chat:completion` carrying the whole `output`
    # replaces the tab's copy outright, which self-heals any event lost while
    # the tab was mid-`location.reload()` from the bootstrap step, and the DB
    # write keeps a mid-stream reopen current.
    now = time.time()
    if (
        output != state.last_snapshot_output
        and (now - state.last_snapshot_ts) >= _RELAY_SNAPSHOT_MIN_INTERVAL_S
    ):
        snapshot = copy.deepcopy(output)
        state.last_snapshot_output = snapshot
        state.last_snapshot_ts = now
        await _relay_emit_event(
            state.user_id, state.chat_id, state.message_id,
            {"type": "chat:completion", "data": {"output": snapshot}},
        )
        await _relay_persist_content(
            conn, state.chat_id, state.message_id, state.renderer.text,
            done=False, output=snapshot,
        )


async def _relay_finalize(conn: "_GatewayConnection", state: "_RelayState") -> None:
    """Complete a relayed run: authoritative done:true DB write, a final full
    `replace` so the live tab shows the complete content, then a
    `chat:active:false` event to settle the frontend's generating state. Always
    drops the run's relay state on the way out."""
    key = f"{state.session_key}:{state.run_id}"
    try:
        if not state.introduced or not state.message_id:
            return
        if state.adopted:
            # Adopted mid-run: the renderer saw only the tail live. The closed
            # transcript has the whole run.
            try:
                hist = await conn.session_history(state.session_key, limit=RUN_HISTORY_LIMIT)
                run_messages = _history_run_messages(hist, state.run_id)
            except Exception as ex:
                run_messages = None
                pipe_log(f"  live relay finalize: chat.history failed: {ex}")
            rebuilt = _history_renderer(run_messages or [])
            if rebuilt.output:
                state.renderer = rebuilt
        final_content = state.renderer.text.strip()
        final_output = state.renderer.finished_output()
        await _relay_persist_content(
            conn, state.chat_id, state.message_id, final_content, done=True,
            output=final_output,
        )
        await _relay_emit_event(
            state.user_id, state.chat_id, state.message_id,
            {"type": "chat:completion", "data": {"output": final_output}},
        )
        # Terminal signal: once a pending assistant leaf exists (it does -- we
        # introduced it), OWUI's chatEventHandler treats chat:active:false as a
        # reconciliation trigger (ELI-62 groundwork). Envelope still wants a
        # live-browser confirmation before the kill switch is flipped on; the
        # done:true DB write above already guarantees reload-correctness.
        await _relay_emit_event(
            state.user_id, state.chat_id, state.message_id,
            {"type": "chat:active", "data": {"active": False}},
        )
        tools = sum(1 for i in final_output if i.get("type") == "function_call")
        pipe_log(
            f"  live relay: finalized message {str(state.message_id)[:8]}... "
            f"in chat {state.chat_id[:8]}... ({len(final_content)} chars, "
            f"{tools} tool card(s))"
        )
    except Exception as ex:
        pipe_log(f"  live relay finalize failed: {ex}")
    finally:
        conn._relay_sessions.pop(key, None)


async def _relay_finalize_settled(conn: "_GatewayConnection", state: "_RelayState") -> None:
    """The relayed run's session went idle but the relay never saw the run's
    final: it was lost with the socket (ELI-88, chat 2af17834 19:15: the
    bubble stayed at "Next I'm posting the Linear updates on ELI-89/" with
    done=False forever). Take the run's text and tool calls from
    `chat.history` when it has closed there, else keep what streamed, and
    finalize."""
    key = f"{state.session_key}:{state.run_id}"
    if conn._relay_sessions.get(key) is not state:
        return  # finalized meanwhile
    try:
        hist = await conn.session_history(state.session_key, limit=RUN_HISTORY_LIMIT)
    except Exception as ex:
        hist = None
        pipe_log(f"  live relay settle: chat.history failed: {ex}")
    run_messages = _history_run_messages(hist, state.run_id)
    if run_messages is None:
        # Not closed in the transcript: only an authoritative idle read may
        # end it (a coalesced settle push can trail the next run's start).
        try:
            lst = await conn.send_request("sessions.list", {}, timeout=15)
        except Exception as ex:
            pipe_log(f"  live relay settle: sessions.list failed: {ex}")
            return
        row = next((r for r in (lst.get("sessions") or [])
                    if r.get("key") == state.session_key), None)
        if row is None or row.get("hasActiveRun") is not False:
            return
    if conn._relay_sessions.get(key) is not state:
        return
    if run_messages:
        rebuilt = _history_renderer(run_messages)
        if rebuilt.output:
            state.renderer = rebuilt
    pipe_log(
        f"  live relay: run {state.run_id[:20]}... settled without a final reaching "
        f"us; finalizing from {'chat.history' if run_messages else 'what streamed'}"
    )
    if not state.introduced:
        if not _relay_content_is_showable(state.renderer.visible_text or ""):
            conn._relay_sessions.pop(key, None)
            return
        if not await _relay_introduce(conn, state):
            return
    await _relay_finalize(conn, state)


async def _relay_abort(conn: "_GatewayConnection", key: str) -> None:
    """A live consumer appeared for a run we were relaying -- stop relaying and
    finalize whatever we have so the out-of-band bubble isn't left pending. The
    inline path then renders the run into its own message; a brief duplicate is
    possible in this rare race (relay only starts after sustained idle), which
    is acceptable for a kill-switched-OFF PoC and documented in ELI-62."""
    state = conn._relay_sessions.get(key)
    if state is None:
        return
    if state.introduced and state.message_id:
        await _relay_finalize(conn, state)
    else:
        conn._relay_sessions.pop(key, None)


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
        pipe_log(f"  orphan stub: sessions.list failed: {ex}")
        return None
    row = next((r for r in (lst.get("sessions") or []) if r.get("key") == session_key), None)
    return bool(row and row.get("hasActiveRun"))


async def _orphan_stub_for_run(conn: "_GatewayConnection", chat, stub_id: str,
                               session_key: str, run_id: str) -> dict | None:
    """The transcript turn of `run_id` if OWUI message `stub_id` is its
    orphaned reply stub: an assistant message no Open WebUI task is
    generating, whose turn in the transcript is this run. Else None.

    Unfinished (`done: False`, an OOM kill), or closed while the run went on:
    a graceful Open WebUI stop cancels the reply and saves it `done: True`
    with what it had (2026-10-12: 2 of 6 cards, no text). The second case is
    only taken on an identity match, the run id naming this message, and its
    saved `output` is returned as `turn["prior_output"]`."""
    msgs = ((chat.chat or {}).get("history") or {}).get("messages") or {}
    stub = msgs.get(stub_id) or {}
    if stub.get("role") != "assistant":
        return None
    if stub.get("done") is not False and f"-{stub_id}-" not in (run_id or ""):
        return None  # a finished reply, not this run's: no transcript read
    if await _owui_chat_has_live_task(chat.id):
        return None
    try:
        hist = await conn.session_history(session_key, limit=RUN_HISTORY_LIMIT)
    except Exception as ex:
        pipe_log(f"  orphan stub: chat.history failed: {ex}")
        return None
    parent = msgs.get(stub.get("parentId")) or {}
    user_text = _coerce_text(parent.get("content")) if parent.get("role") == "user" else ""
    turn = _stub_turn(hist, stub_id, user_text, _owui_message_ts(stub))
    if not turn or turn["run_id"] != run_id:
        return None
    if stub.get("done") is not False and turn["by"] != "id":
        return None
    turn["prior_output"] = copy.deepcopy(stub.get("output") or [])
    return turn


# A long session's whole transcript (952 messages for 0abe9bde) takes the
# Gateway well over the default 10s RPC bound to serialize.
ORPHAN_HISTORY_TIMEOUT_S = 60


async def _reconcile_stub(conn: "_GatewayConnection", agent_id: str,
                          chat_id: str, stub_id: str, *, wait: bool = True) -> str:
    """Finish one orphaned reply stub from its run in the transcript. A run
    still going is waited for on the pushed settle (`sessions.changed`), not
    on a timer, or with `wait=False` reported as "wait"; a run that relay or
    proactive delivery claims meanwhile is theirs, and they write into this
    same stub. Returns "done", "wait" or "skip"."""
    from open_webui.models.chats import Chats
    waiting = False
    while True:
        chat = await Chats.get_chat_by_id(chat_id)
        msgs = ((chat.chat or {}).get("history") or {}).get("messages") or {} if chat else {}
        stub = msgs.get(stub_id) or {}
        if stub.get("role") != "assistant" or stub.get("done") is not False:
            return "skip"  # gone or finished meanwhile
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
    # content (older pipes streamed into it, the relay snapshots into it) is
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
        if (msgs.get(stub_id) or {}).get("done") is not False:
            return "skip"
        claim = f"{session_key}:{run_id}" if run_id else None
        if claim:
            if claim in conn._delivered_proactive:
                return "skip"  # live relay or proactive delivery owns this run
            conn._delivered_proactive[claim] = True
        # Open WebUI answers None, logging nothing, when the write fails (its
        # transaction raised and was rolled back).
        if await Chats.upsert_message_to_chat_by_id_and_message_id(
                chat_id, stub_id, fields) is None:
            if claim:
                conn._delivered_proactive.pop(claim, None)
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


async def _finalize_inline_message(
    conn: "_GatewayConnection", session_key: str, run_id: str
) -> None:
    """Parity path (2026-07-19): when a run's inline pipe() turn ended before
    the run itself finished, write the COMPLETE shadow-rendered content into
    the ORIGINAL OWUI message the turn was streaming into.

    Result is parity with a turn that was never interrupted -- same message,
    full assistant text + every tool block, marked done -- instead of the tail
    stranding in a truncated, tool-block-less, detached proactive bubble.

    Safety: only ever overwrites an *assistant* message in place. If the target
    id is missing or isn't an assistant message (must never clobber a user
    turn), it falls back to appending the full content as a new tail message so
    nothing is lost and nothing is corrupted.
    """
    key = f"{session_key}:{run_id}"
    try:
        target = conn._run_targets.get(key)
        renderer = conn._run_renderers.get(key)
        if not target or renderer is None:
            return
        if conn.was_delivered_live(session_key, run_id):
            return  # a live tab already persisted it via OWUI's own flow
        content = (renderer.visible_text or "").strip()
        if not content:
            return
        dedup = f"finalize:{key}"
        if dedup in conn._delivered_proactive:
            return
        conn._delivered_proactive[dedup] = True

        chat_id = target["chat_id"]
        message_id = target["message_id"]
        try:
            from open_webui.models.chats import Chats
        except Exception as ex:
            pipe_log(f"  parity finalize unavailable (not inside OWUI process?): {ex}")
            return

        try:
            chat = await Chats.get_chat_by_id(chat_id)
            if chat is None:
                pipe_log(f"  parity finalize: chat {chat_id[:8]}... not found")
                return
            messages = (chat.chat.get("history", {}) or {}).get("messages", {}) or {}
            existing = messages.get(message_id)
            if existing and existing.get("role") == "assistant":
                # Update the existing assistant message in place: OWUI's upsert
                # merges these fields into it, keeping role/parentId/childrenIds/
                # model. This is the parity case -- same bubble, full content.
                # OWUI 0.12 draws the message from `output` whenever it has
                # one, so the run's items replace it; `content` is text only,
                # as OWUI stores it.
                fields: dict = {"content": content, "done": True}
                if renderer.output:
                    fields = {"content": renderer.text.strip(), "done": True,
                              "output": renderer.finished_output()}
                async with conn._chat_write_lock(chat_id):
                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        chat_id, message_id, fields,
                    )
                pipe_log(
                    f"  parity finalize: completed assistant message "
                    f"{message_id[:8]}... in place ({len(content)} chars, "
                    f"{sum(1 for i in fields.get('output', []) if i.get('type') == 'function_call')}"
                    " tool card(s))"
                )
            else:
                # Original id missing or not an assistant message -- never
                # overwrite it. Append the full content as a new tail message
                # (still complete, with tool blocks) so the turn isn't lost.
                if await _append_proactive_message_to_chat(
                    conn, chat_id, content, log_prefix="parity finalize (fallback append)"
                ):
                    pipe_log(
                        f"  parity finalize: original id absent, appended full "
                        f"content as new tail ({len(content)} chars)"
                    )
        except Exception as ex:
            pipe_log(f"  parity finalize failed: {ex}")
    finally:
        conn.unregister_run_target(session_key, run_id)


async def _deliver_proactive_owui_message(
    conn: "_GatewayConnection", session_key: str, run_id: str
) -> None:
    """Persist a finished OpenClaw turn into OWUI chat history when nobody's
    tab is live-consuming it (P33/ELI-17: cron wake, heartbeat, or a bare
    `sessions_send` with no active `pipe()` call for this session+run).

    This only works because the pipe module is loaded as an OWUI function
    and runs inside OWUI's own backend process -- the exact same in-process
    privilege `_retry_modal_on_reconnect` already relies on to import
    `open_webui.socket.main`. An external caller (e.g. a real OpenClaw
    channel plugin running in OpenClaw's own gateway process) has no
    equivalent access; OWUI does not expose a public API to both write a
    chat message and live-refresh an open tab for it, so v0 scope is
    persistence only -- the message appears next time the chat is opened or
    refreshed, not instantly in an already-open tab.
    """
    parsed = conn.parse_owui_session_key(session_key)
    if not parsed:
        return
    user_id, chat_id = parsed

    dedup_key = f"{session_key}:{run_id}"
    if dedup_key in conn._delivered_proactive:
        return
    conn._delivered_proactive[dedup_key] = True
    if len(conn._delivered_proactive) > 200:
        for stale_key in list(conn._delivered_proactive)[:100]:
            conn._delivered_proactive.pop(stale_key, None)

    text_out: dict = {}
    text = await _proactive_run_text(conn, session_key, "proactive delivery",
                                     run_id=run_id, out=text_out)
    if not text:
        pipe_log("  proactive delivery: no assistant text, skipping")
        return

    # A run whose reply stub an Open WebUI restart orphaned is that stub's
    # answer, not a proactive message: it goes into the stub unlabeled.
    adopt_fields: dict = {"content": text, "done": True}
    plain_output = _proactive_run_output(text_out.get("history"), run_id, "")
    if plain_output:
        adopt_fields["output"] = plain_output

    label = "*↳ Proactive message*\n\n"
    text = f"{label}{text}"
    extra = {"done": True}
    output = _proactive_run_output(text_out.get("history"), run_id, label)
    if output:
        extra["output"] = output

    append_out: dict = {}
    if await _append_proactive_message_to_chat(
        conn, chat_id, text, log_prefix="proactive delivery", out=append_out,
        extra_fields=extra,
        adopt={"session_key": session_key, "run_id": run_id, "fields": adopt_fields},
    ):
        if append_out.get("adopted"):
            output = plain_output
        cards = sum(1 for i in output or [] if i.get("type") == "function_call")
        pipe_log(
            f"  proactive delivery: persisted message into chat {chat_id[:8]}... "
            f"for user {user_id[:8]}... ({len(text)} chars, {cards} tool card(s))"
        )
        if LIVE_STREAM_BOOTSTRAP_ENABLED:
            await _emit_live_bootstrap_reload(user_id, chat_id, append_out.get("old_leaf_id"))


# Embedded in the proactively-delivered message text for a finished
# sub-agent task, invisible in rendered markdown (an HTML comment) but still
# present in the raw `content` OWUI hands the Action endpoint on click -- the
# one piece of information a toolbar button click doesn't otherwise carry
# (OWUI sends chat_id/message_id/model/content, never a custom taskId).
# `_run_subagent_detail`-style consumers parse it back out with
# `_SUBAGENT_TASK_ID_MARKER_RE` to open straight into that task's drawer
# instead of the general status dialog.
_SUBAGENT_TASK_ID_MARKER_RE = re.compile(r"<!--\s*openclaw:taskId=([^\s>]+?)\s*-->")


async def _maybe_deliver_subagent_proactive(
    conn: "_GatewayConnection",
    child_session_key: str,
    run_id: str,
    min_idle_s: float = 120,
    poll_interval_s: float = 5,
    max_wait_s: float = 600,
) -> None:
    """Resolve a finished sub-agent's parent OWUI chat, wait out the same
    idle-debounce contract `_maybe_deliver_proactive_after_debounce` uses
    (checked against the *parent's* liveness, not the sub-agent's own -- see
    the event-loop call site's comment for why), then deliver.

    A nested sub-agent (spawned by another sub-agent, not by an OWUI
    session directly) resolves to a parent that also fails
    `parse_owui_session_key` -- there is no OWUI chat to deliver to at all in
    that case, so this quietly gives up rather than trying to walk further
    up the chain.
    """
    try:
        task = await conn.resolve_subagent_parent_task(child_session_key)
        if not task:
            pipe_log(f"  subagent proactive: no task found for {child_session_key[:40]}...")
            return
        parent_session_key = task.get("sessionKey")
        task_id = task.get("id")
        if not task_id or not parent_session_key or not conn.parse_owui_session_key(parent_session_key):
            return

        waited = 0.0
        while not conn.session_idle_for(parent_session_key, min_idle_s=min_idle_s):
            if not PROACTIVE_DELIVERY_ENABLED or conn._retired:
                pipe_log("  subagent proactive delivery: disabled mid-debounce, abandoning wait")
                return
            if waited >= max_wait_s:
                pipe_log(
                    f"  subagent proactive delivery: parent {parent_session_key[:40]}... "
                    f"never settled idle within {max_wait_s:.0f}s, giving up"
                )
                return
            await asyncio.sleep(poll_interval_s)
            waited += poll_interval_s

        await _deliver_subagent_proactive_owui_message(
            conn, child_session_key, run_id, parent_session_key, task_id,
            task.get("title"),
        )
    finally:
        conn._pending_proactive_debounce.discard(child_session_key)


async def _deliver_subagent_proactive_owui_message(
    conn: "_GatewayConnection",
    child_session_key: str,
    run_id: str,
    parent_session_key: str,
    task_id: str,
    task_title: str | None,
) -> None:
    """Same persistence mechanism as `_deliver_proactive_owui_message`
    (direct write via `Chats.upsert_message_to_chat_by_id_and_message_id`,
    same must-run-inside-OWUI-process constraint), but the *content* source
    and the *delivery target* are different sessions: the sub-agent's own
    transcript for what it said, the parent's chat for where it's shown.
    """
    parsed = conn.parse_owui_session_key(parent_session_key)
    if not parsed:
        return
    user_id, chat_id = parsed

    dedup_key = f"{child_session_key}:{run_id}"
    if dedup_key in conn._delivered_proactive:
        return
    conn._delivered_proactive[dedup_key] = True
    if len(conn._delivered_proactive) > 200:
        for stale_key in list(conn._delivered_proactive)[:100]:
            conn._delivered_proactive.pop(stale_key, None)

    text = await _proactive_run_text(conn, child_session_key, "subagent proactive delivery")
    if not text:
        pipe_log("  subagent proactive delivery: no assistant text, skipping")
        return

    label = f"Sub-agent finished: {task_title}" if task_title else "Sub-agent finished"
    text = f"*↳ {label}*\n\n{text}\n\n<!-- openclaw:taskId={task_id} -->"

    append_out: dict = {}
    if await _append_proactive_message_to_chat(
        conn, chat_id, text, log_prefix="subagent proactive delivery", out=append_out
    ):
        pipe_log(
            f"  subagent proactive delivery: persisted message into chat {chat_id[:8]}... "
            f"for user {user_id[:8]}... task {task_id[:8]}... ({len(text)} chars)"
        )
        if LIVE_STREAM_BOOTSTRAP_ENABLED:
            await _emit_live_bootstrap_reload(user_id, chat_id, append_out.get("old_leaf_id"))


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
