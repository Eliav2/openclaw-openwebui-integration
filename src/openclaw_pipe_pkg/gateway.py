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

# Hard kill switch for P33/ELI-17/ELI-19 proactive delivery. Disabled
# 2026-07-11: even the session-wide idleness gate (has_any_consumer_for_session)
# has a live race at steering-leg boundaries — a run_id can finish and
# unregister while the user is still actively chatting, and each leg's
# distinct run_id defeats the dedup cache. This produced duplicate assistant
# messages in an unrelated, currently-active chat (see PLAN.md P33). Do not
# re-enable without a fix for that race and a live verification that does not
# touch real conversations.
PROACTIVE_DELIVERY_ENABLED = False

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

        # Dedup guard for proactive OWUI delivery (P33/ELI-17), keyed by
        # "session_key:run_id" so a retried/duplicated final event never
        # writes the same message into chat history twice.
        self._delivered_proactive: dict[str, bool] = {}

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
        # genuinely idle, which a few-second leg gap cannot satisfy.
        self._session_last_activity: dict[str, float] = {}

        # Sessions currently being watched by a debounce task waiting for
        # `session_idle_for` to clear (see `_maybe_deliver_proactive_after_debounce`).
        # Prevents spawning a second concurrent watcher for the same session
        # when multiple unmatched "final" events arrive close together —
        # which would otherwise let both watchers independently observe
        # idleness later and each call `_deliver_proactive_owui_message`
        # with a *different* run_id, defeating the per-run dedup guard.
        self._pending_proactive_debounce: set[str] = set()

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
                # for a sustained quiet period, not just at this instant.
                evt_session = payload.get("sessionKey", "")
                if PROACTIVE_DELIVERY_ENABLED and evt_session and payload.get("state") == "final":
                    if (
                        self.parse_owui_session_key(evt_session)
                        and not self.has_any_consumer_for_session(evt_session)
                        and evt_session not in self._pending_proactive_debounce
                    ):
                        self._pending_proactive_debounce.add(evt_session)
                        asyncio.create_task(
                            _maybe_deliver_proactive_after_debounce(
                                self, evt_session, payload.get("runId", "")
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
            if text:
                return text
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
        await _deliver_proactive_owui_message(conn, session_key, run_id)
    finally:
        conn._pending_proactive_debounce.discard(session_key)


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

    try:
        from open_webui.models.chats import Chats
    except Exception as ex:
        pipe_log(f"  proactive delivery unavailable (not running inside OWUI process?): {ex}")
        return

    try:
        chat = await Chats.get_chat_by_id(chat_id)
        if chat is None:
            pipe_log(f"  proactive delivery: chat {chat_id[:8]}... not found")
            return
        history = chat.chat.get("history", {}) or {}
        old_leaf_id = history.get("currentId")
        new_message_id = str(uuid.uuid4())

        # `upsert_message_to_chat_by_id_and_message_id` unconditionally sets
        # `history.currentId = message_id` as a side effect of *every* call
        # (it's OWUI's own generic upsert, not something we control). So the
        # childrenIds patch on the *old* leaf must happen first — otherwise
        # it clobbers currentId back to old_leaf_id right after we set it,
        # and the new message becomes an invisible orphan branch (P33 bug,
        # found 2026-07-11: pipe_log showed successful "persisted" calls but
        # nothing ever appeared in OWUI, because currentId never actually
        # ended up pointing at the new message).
        if old_leaf_id:
            old_leaf = (history.get("messages") or {}).get(old_leaf_id, {})
            children = list(old_leaf.get("childrenIds", []))
            if new_message_id not in children:
                children.append(new_message_id)
                await Chats.upsert_message_to_chat_by_id_and_message_id(
                    chat_id, old_leaf_id, {"childrenIds": children},
                )

        await Chats.upsert_message_to_chat_by_id_and_message_id(
            chat_id,
            new_message_id,
            {
                "role": "assistant",
                "content": text,
                "parentId": old_leaf_id,
                "childrenIds": [],
                "timestamp": int(time.time()),
            },
        )

        pipe_log(
            f"  proactive delivery: persisted message into chat {chat_id[:8]}... "
            f"for user {user_id[:8]}... ({len(text)} chars)"
        )
    except Exception as ex:
        pipe_log(f"  proactive delivery failed: {ex}")


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
