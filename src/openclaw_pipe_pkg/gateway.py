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
    attachments: list | None = None,
) -> dict:
    params = dict(
        sessionKey=session_key,
        message=message,
        idempotencyKey=idempotency_key,
    )
    if attachments:
        params["attachments"] = attachments
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


def _render_tool_result_block(name: str, tool_call_id: str, args_str: str,
                              result_str: str, meta) -> str:
    """Render a finished tool call as OWUI's collapsible `tool_calls` card.

    Extracted from the inline pipe loop so the shadow `_TurnRenderer` (which
    completes a run's OWUI message when the inline turn ended early) produces
    byte-identical tool blocks — same escaping, same field caps — instead of a
    second, drift-prone copy of this markup.
    """
    return (
        '\n<details type="tool_calls" done="true" '
        f'id="{html.escape(tool_call_id)}" '
        f'name="{html.escape(name)}" '
        f'arguments="{html.escape(args_str[:3000])}" '
        f'result="{html.escape(result_str[:8000])}" '
        f'meta="{html.escape(str(meta)[:500])}" '
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

    Scope: assistant text deltas, item-carried text, and tool-result blocks —
    the substance of the long autonomous turns this path exists for. MEDIA
    uploads and ask-user modals are intentionally NOT reproduced (they need the
    live browser and don't occur on a self-closed background tail).
    """

    def __init__(self):
        self.visible_text = ""
        self._assistant_stream_text = ""
        self._last_item_text = ""
        self._active_tool_args: dict[str, str] = {}

    def _add(self, chunk: str) -> None:
        if chunk:
            self.visible_text += chunk

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
                self._add(delta)

        if stream == "item":
            item_text = _item_assistant_text(data)
            if item_text:
                item_delta = _item_delta_text(
                    item_text, self._last_item_text, self._assistant_stream_text
                )
                self._last_item_text = item_text
                item_delta = _suppress_already_shown(item_delta, self.visible_text)
                self._add(item_delta)

        if stream == "tool":
            if phase == "start":
                tcid = data.get("toolCallId", "")
                if tcid:
                    self._active_tool_args[tcid] = json.dumps(data.get("args", {}))
            elif phase == "result":
                result = data.get("result", {})
                result_str = json.dumps(result) if not isinstance(result, str) else result
                tcid = data.get("toolCallId", "")
                args_str = self._active_tool_args.pop(tcid, None) or json.dumps(
                    data.get("args", {})
                )
                self._add(_render_tool_result_block(
                    name, tcid, args_str, result_str, data.get("meta", "")
                ))


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
# before treating preview text as deliverable — see that constant's
# docstring. The separate duplicate-identity issue (a live turn in an
# actively-used real chat getting proactively re-delivered) was already
# fixed by `was_delivered_live` and confirmed via timestamps to only have
# happened in the window *before* that fix was deployed, not after. Do not
# re-disable without updating this comment and PLAN.md P33 with the new
# incident.
PROACTIVE_DELIVERY_ENABLED = True


# Anchor for state that must be shared by *every* `_GatewayConnection` alive in
# this Python process — not just the current module's singleton. OWUI's function
# loader execs each redeploy into a brand-new module object with no teardown hook
# on the old one, so a connection left running by a previous deploy (a "zombie")
# keeps its own event loop and its own *private* bookkeeping dicts. That zombie
# still receives the Gateway's broadcast `final` events; with private bookkeeping
# it sees an empty `delivered_live`/`session_last_activity` for the session,
# wrongly concludes "nobody consumed this, the session is idle", and proactively
# re-writes the turn into chat history — even though the *live* connection just
# showed it to the open tab. That is the "*↳ Proactive message* on a live turn"
# / duplicate-branch (2/2) bug (P33, root-caused 2026-07-11). Stashing the
# liveness bookkeeping on `open_webui.socket.main` (OWUI's own stable module,
# never reloaded by our function) makes all connections — current and zombie —
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
    # later version must be backfilled into the already-existing dict —
    # otherwise `__init__` does `_shared["new_key"]` and KeyErrors on the
    # first request after redeploy (chat_write_locks hit exactly this,
    # 2026-07-13).
    state.setdefault("delivered_live", {})
    state.setdefault("delivered_proactive", {})
    state.setdefault("session_last_activity", {})
    state.setdefault("pending_proactive_debounce", set())
    state.setdefault("chat_write_locks", {})
    return state


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

        # Proactive-delivery bookkeeping. When running inside OWUI these four
        # dicts/sets are the *same objects* for every `_GatewayConnection` in
        # the process (see `_shared_gateway_state`), so a turn shown live by
        # one connection is seen as delivered by all of them and a zombie
        # connection left by a previous deploy can no longer re-deliver a live
        # turn as a bogus "*↳ Proactive message*" (P33, 2026-07-11). Outside
        # OWUI (unit tests) each connection gets its own isolated dicts.
        _shared = _shared_gateway_state()

        # Dedup guard for proactive OWUI delivery (P33/ELI-17), keyed by
        # "session_key:run_id" so a retried/duplicated final event — or a
        # second (zombie) connection racing the same idle session — never
        # writes the same message into chat history twice.
        self._delivered_proactive: dict[str, bool] = (
            _shared["delivered_proactive"] if _shared is not None else {}
        )

        # Records, by "session_key:run_id", every *final* event that was
        # actually dispatched to a live consumer queue (i.e. genuinely shown
        # to an open browser tab, which persists it itself via OWUI's normal
        # client-side flow). If the Gateway later re-emits a duplicate/retried
        # final event for that same run_id after the tab's request has
        # closed, this identity check catches it precisely — unlike a
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
        # abandoned — a "final" event landing in that window used to get
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
        # when multiple unmatched "final" events arrive close together —
        # which would otherwise let both watchers independently observe
        # idleness later and each call `_deliver_proactive_owui_message`
        # with a *different* run_id, defeating the per-run dedup guard. Shared
        # across connections so two connections (e.g. current + a zombie) never
        # each spawn their own watcher for the same session.
        self._pending_proactive_debounce: set[str] = (
            _shared["pending_proactive_debounce"] if _shared is not None else set()
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
        # stream — so if the inline turn ends before the run does (idle
        # self-close / cancel / torn-down request), the run's final can finalize
        # the ORIGINAL message to the complete content instead of stranding the
        # tail in a truncated, tool-block-less proactive bubble. Instance-local
        # (not shared): only the singleton connection runs pipe() and dispatches
        # its own events; a zombie connection never registers a target.
        self._run_targets: dict[str, dict] = {}
        self._run_renderers: dict[str, "_TurnRenderer"] = {}

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
        async with self._init_lock:
            if self._ws and self._event_loop_task and not self._event_loop_task.done():
                return
            self._stopped = False
            await self._connect_and_start()
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

    def _touch_session_activity(self, session_key: str):
        self._session_last_activity[session_key] = time.time()
        if len(self._session_last_activity) > 500:
            oldest_first = sorted(self._session_last_activity.items(), key=lambda kv: kv[1])
            for stale_key, _ in oldest_first[:250]:
                self._session_last_activity.pop(stale_key, None)

    def mark_delivered_live(self, session_key: str, run_id: str):
        """Record that this run's final event was just dispatched to a live
        consumer queue — i.e. genuinely shown to an open browser tab, which
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
        active consumer at all — otherwise it risks injecting a duplicate
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
        turn arriving (register) — a few seconds, not the minutes a
        genuinely abandoned session sits idle for. Requiring a sustained
        quiet period turns a race into a simple timing margin: a real
        steering/modal round trip resolves in low single-digit seconds,
        while a proactive wake (cron, heartbeat, bare sessions_send) has no
        pending browser request at all, so it always clears the bar.

        A session_key never seen by this connection (no register/unregister
        recorded — e.g. this is a fresh process, or the previous owner of
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
                if consumers:
                    self._event_count += 1
                    if not payload.get("runId"):
                        pipe_log("  dispatched session-only event to sole consumer")
                    if payload.get("state") == "final" and payload.get("sessionKey") and payload.get("runId"):
                        # Genuinely shown to a live tab, which persists it via
                        # OWUI's own client-side flow — remember this so a
                        # later duplicate/retried final event for the same
                        # run_id is never proactively re-delivered (P33).
                        self.mark_delivered_live(payload["sessionKey"], payload["runId"])
                        # The inline turn is alive and took the final itself, so
                        # the parity shadow isn't needed for this run — drop it.
                        self.unregister_run_target(payload["sessionKey"], payload["runId"])
                    for q in consumers[0].queues:
                        await q.put(msg)
                    continue

                # ── Parity finalize (2026-07-19) ──
                # A final arrived with NO live consumer, but we were tracking
                # this run's OWUI message — i.e. the inline pipe() turn ended
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
                    asyncio.create_task(
                        _finalize_inline_message(self, _evt_sess, _evt_run)
                    )
                    continue

                # ── Proactive delivery for idle OWUI sessions (P33/ELI-17/ELI-19) ──
                # Nobody is live-consuming this *exact* event — no browser tab
                # is mid-request for this precise session+run. That alone is
                # NOT enough to prove the session is idle: this event's run_id
                # can simply not match a *different*, still-active run on the
                # same session (steering handoff, or a bare session-only event
                # with zero/multiple matches) — proactively delivering in that
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
                # shown to a live tab — that scenario doesn't depend on
                # timing at all (the tab may have gone quiet for well over
                # the debounce window by the time the retry arrives), so it
                # needs its own identity-based check, not a longer timer.
                evt_session = payload.get("sessionKey", "")
                evt_run_id = payload.get("runId", "")
                if PROACTIVE_DELIVERY_ENABLED and evt_session and payload.get("state") == "final":
                    if (
                        self.parse_owui_session_key(evt_session)
                        and not self.has_any_consumer_for_session(evt_session)
                        and not self.was_delivered_live(evt_session, evt_run_id)
                        and evt_session not in self._pending_proactive_debounce
                    ):
                        self._pending_proactive_debounce.add(evt_session)
                        asyncio.create_task(
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
                    and evt_session
                    and payload.get("state") == "final"
                    and self.is_subagent_session_key(evt_session)
                    and evt_session not in self._pending_proactive_debounce
                ):
                    self._pending_proactive_debounce.add(evt_session)
                    asyncio.create_task(
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


# Protocol sentinels (docs/tools/subagents.md:470-431) that mean "no
# user-visible content was produced" — never real assistant text to show a
# human. An announce-follow-up run can legitimately finish with exactly one
# of these as its only "assistant text", and naively persisting it as a
# proactive OWUI message leaks internal plumbing into the chat (P33,
# 2026-07-11: `ANNOUNCE_SKIP` landed as literal message text).
_SILENT_SENTINELS = {"ANNOUNCE_SKIP", "NO_REPLY", "no_reply"}


def _last_assistant_text_from_preview(preview: dict, session_key: str) -> str | None:
    """Return the last assistant message text for session_key in a
    `sessions.preview` response (same shape `_preview_recovery_text` reads),
    without requiring a preceding user-text match — a proactive delivery
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
    consumer, runs, then unregisters again) — a single fixed sleep would
    miss that and could still deliver during a live steering round trip. If
    the session never settles within `max_wait_s`, gives up rather than
    delivering into a session whose liveness can't be confirmed; also bails
    immediately if the kill switch is flipped back off mid-wait.
    """
    try:
        waited = 0.0
        while not conn.session_idle_for(session_key, min_idle_s=min_idle_s):
            if not PROACTIVE_DELIVERY_ENABLED:
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
        if conn.was_delivered_live(session_key, run_id):
            # The run got shown to a (re)connected live tab while we were
            # waiting out the debounce window — that tab already persists
            # it via OWUI's own client-side flow, so proactively delivering
            # now would be a pure duplicate.
            pipe_log(f"  proactive delivery: {session_key[:40]}... run "
                      f"{run_id[:20]}... was delivered live during debounce wait, skipping")
            return
        await _deliver_proactive_owui_message(conn, session_key, run_id)
    finally:
        conn._pending_proactive_debounce.discard(session_key)


def _deepest_leaf_id(messages: dict, start_id: str | None) -> str | None:
    """Walk down `childrenIds` from `start_id` to the deepest childless node.

    OWUI's `history.currentId` is only a *view* pointer: the frontend (or an
    interleaved live turn) can reset it back to an ancestor that already has
    children — e.g. right after a proactive message was appended. Anchoring a
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
    to the PRE-proactive leaf — making the proactive message and the new
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
        # Keep only the first node under the parent; chain the rest under the
        # deepest leaf of the previous node's existing subtree (a proactive
        # message may already anchor its own tail — e.g. a later proactive
        # chained under it — so we must extend, not overwrite, its children).
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
        # Point the view at the true tail so the whole linear flow — including
        # the now-inlined proactive message — is what shows.
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
                # passed blob has no 'title' key — preserve it explicitly.
                if "title" not in blob:
                    blob["title"] = getattr(chat, "title", None) or "New Chat"
                await Chats.update_chat_by_id(chat_id, blob)
                pipe_log(f"  healed proactive variant group(s) in chat {chat_id[:8]}...")
    except Exception as ex:
        pipe_log(f"  proactive variant heal failed (non-fatal): {ex}")


async def _append_proactive_message_to_chat(
    conn: "_GatewayConnection", chat_id: str, text: str, *, log_prefix: str
) -> bool:
    """Persist one assistant message onto the tail of an OWUI chat.

    Shared write path for both proactive-delivery functions
    (`_deliver_proactive_owui_message` and its sub-agent sibling), which did
    this identically apart from log wording (~45 duplicated lines each). The
    load-bearing details are all P33-history (2026-07-11/13) and MUST stay
    identical for both callers:
      * anchor on the true childless leaf (`_deepest_leaf_id`), not the bare
        `currentId`, or the message forks a 1/2·2/2 sibling variant;
      * patch the old leaf's `childrenIds` BEFORE upserting the new message —
        `upsert_message_to_chat_by_id_and_message_id` resets `currentId` on
        every call, so doing it after would orphan the new message;
      * inherit the branch's own `model` so OWUI's frontend still resolves an
        `actions` list (the Status button) for the message;
      * hold the per-chat write lock so the two proactive paths (and any
        zombie connection) can't each read the same leaf and append siblings.

    Returns True on a successful persist. Callers own the success log line
    (its detail — user_id, task_id — differs); this owns the
    unavailable/not-found/failed logs, tagged with `log_prefix`.
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

            await Chats.upsert_message_to_chat_by_id_and_message_id(
                chat_id, new_message_id, message_fields,
            )
        return True
    except Exception as ex:
        pipe_log(f"  {log_prefix} failed: {ex}")
        return False


async def _finalize_inline_message(
    conn: "_GatewayConnection", session_key: str, run_id: str
) -> None:
    """Parity path (2026-07-19): when a run's inline pipe() turn ended before
    the run itself finished, write the COMPLETE shadow-rendered content into
    the ORIGINAL OWUI message the turn was streaming into.

    Result is parity with a turn that was never interrupted — same message,
    full assistant text + every tool block, marked done — instead of the tail
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
                # model. This is the parity case — same bubble, full content.
                async with conn._chat_write_lock(chat_id):
                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        chat_id, message_id, {"content": content, "done": True},
                    )
                pipe_log(
                    f"  parity finalize: completed assistant message "
                    f"{message_id[:8]}... in place ({len(content)} chars)"
                )
            else:
                # Original id missing or not an assistant message — never
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
    and runs inside OWUI's own backend process — the exact same in-process
    privilege `_retry_modal_on_reconnect` already relies on to import
    `open_webui.socket.main`. An external caller (e.g. a real OpenClaw
    channel plugin running in OpenClaw's own gateway process) has no
    equivalent access; OWUI does not expose a public API to both write a
    chat message and live-refresh an open tab for it, so v0 scope is
    persistence only — the message appears next time the chat is opened or
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

    try:
        preview = await conn.session_preview(session_key, limit=1, max_chars=8000)
    except Exception as ex:
        pipe_log(f"  proactive delivery: session_preview failed: {ex}")
        return

    text = _last_assistant_text_from_preview(preview, session_key)
    if not text:
        pipe_log("  proactive delivery: no assistant text in preview, skipping")
        return

    text = f"*↳ Proactive message*\n\n{text}"

    if await _append_proactive_message_to_chat(
        conn, chat_id, text, log_prefix="proactive delivery"
    ):
        pipe_log(
            f"  proactive delivery: persisted message into chat {chat_id[:8]}... "
            f"for user {user_id[:8]}... ({len(text)} chars)"
        )


# Embedded in the proactively-delivered message text for a finished
# sub-agent task, invisible in rendered markdown (an HTML comment) but still
# present in the raw `content` OWUI hands the Action endpoint on click — the
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
    (checked against the *parent's* liveness, not the sub-agent's own — see
    the event-loop call site's comment for why), then deliver.

    A nested sub-agent (spawned by another sub-agent, not by an OWUI
    session directly) resolves to a parent that also fails
    `parse_owui_session_key` — there is no OWUI chat to deliver to at all in
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
            if not PROACTIVE_DELIVERY_ENABLED:
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

    try:
        preview = await conn.session_preview(child_session_key, limit=1, max_chars=8000)
    except Exception as ex:
        pipe_log(f"  subagent proactive delivery: session_preview failed: {ex}")
        return

    text = _last_assistant_text_from_preview(preview, child_session_key)
    if not text:
        pipe_log("  subagent proactive delivery: no assistant text in preview, skipping")
        return

    label = f"Sub-agent finished: {task_title}" if task_title else "Sub-agent finished"
    text = f"*↳ {label}*\n\n{text}\n\n<!-- openclaw:taskId={task_id} -->"

    if await _append_proactive_message_to_chat(
        conn, chat_id, text, log_prefix="subagent proactive delivery"
    ):
        pipe_log(
            f"  subagent proactive delivery: persisted message into chat {chat_id[:8]}... "
            f"for user {user_id[:8]}... task {task_id[:8]}... ({len(text)} chars)"
        )


# Module-level singleton
_gateway_connection: _GatewayConnection | None = None
_gateway_init_lock = asyncio.Lock()

# Anchor for finding a connection left behind by a *previous* deploy of this
# same function (P33/P36: OWUI's function loader (open_webui/utils/plugin.py)
# execs each redeploy into a brand-new module object with no teardown hook on
# the old one — a module-level singleton alone resets every deploy and orphans
# the old module's WS/event-loop task forever). Stashing it as an attribute on
# `open_webui.socket.main` — OWUI's own stable module, never reloaded by our
# function — lets the next deploy find and `disconnect()` the previous one
# before opening a new connection, so redeploys self-heal without a container
# restart. Falls back to a no-op when not running inside OWUI (e.g. unit tests).
_STALE_CONN_ATTR = "_openclaw_gateway_connection_v1"


async def _reap_stale_gateway_connection() -> None:
    try:
        import open_webui.socket.main as _owui_socket_main
    except Exception:
        return
    stale = getattr(_owui_socket_main, _STALE_CONN_ATTR, None)
    if stale is None or stale is _gateway_connection:
        return
    try:
        await asyncio.wait_for(stale.disconnect(), timeout=5)
        pipe_log("  reaped stale gateway connection from a previous deploy")
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
