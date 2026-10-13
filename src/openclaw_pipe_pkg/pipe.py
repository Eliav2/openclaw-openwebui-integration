# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# Session states that `sessions.describe` reports for a genuinely in-progress
# run. Blacklisting terminal states is unsafe: an idle session reports "idle"
# (and "done"), so a `not in (done, failed, cancelled)` test would treat every
# resting session as active and hang every message. Only these gate the queue.
_ACTIVE_RUN_STATES = ("active", "running", "streaming", "queued")


def _session_active_from_signals(describe_status, list_has_active_run) -> bool:
    """Decide whether a session has an in-progress run from the two gateway
    signals the pipe can observe over RPC (ELI-56).

    `sessions.describe` only exposes a persisted `status`, which flips to
    'done' the instant a turn calls `sessions_yield` to await a subagent -- even
    though the run is merely suspended and WILL resume, and the gateway still
    steer-merges any message sent into that window (→ a phantom "no reply
    text" bubble). `sessions.list` additionally exposes `hasActiveRun`, the
    authoritative live signal derived from the gateway's chatAbortControllers
    map + isEmbeddedAgentRunActive(sessionId); it stays true across the whole
    yield gap. So a session is "active" if EITHER describe says a run state OR
    list says a run is live.
    """
    if describe_status in _ACTIVE_RUN_STATES:
        return True
    return bool(list_has_active_run)


def _owui_owns_inflight_message() -> bool:
    """True when the running Open WebUI (0.12+) owns the in-flight assistant
    message, which makes the pipe's REST mid-run snapshot a no-op.

    Two 0.12 changes, measured live on 2026-10-11 against 0.12.0:
      * `Chats.update_chat_by_id` keeps the STORED copy of any assistant
        message whose stored or incoming `done` is False
        (`models/chats.py::update_chat_by_id`), so the snapshot's
        `history.messages[id]` patch is dropped. The live probe saw every
        snapshot POST return 200 while the DB row stayed at content=0 until
        `done`.
      * The streaming handler keeps in-progress content/output in
        `response_streams` (`middleware.py::save_current_response_stream`)
        and `GET /api/v1/chats/{id}` overlays it onto the message
        (`routers/chats.py::overlay_response_streams`). The same probe read
        the growing text back from that GET every poll, so a client that
        reloads mid-turn is rehydrated by OWUI itself.

    `insert_chat_turn` (the 0.12 server-side turn insertion that came with
    the `done` rule) is the feature marker. Outside OWUI, or on an older
    OWUI, this is False and the snapshot keeps running.
    """
    mod = sys.modules.get("open_webui.models.chats")
    if mod is None:
        try:
            import importlib
            mod = importlib.import_module("open_webui.models.chats")
        except Exception:
            return False
    chats = getattr(mod, "Chats", None)
    return callable(getattr(chats, "insert_chat_turn", None))


def _thinking_rejection_retry_body(body, recovered, resolved_thinking):
    """Decide whether a textless ("PHANTOM") turn's preview-recovered text is
    the Gateway's permanent-brick thinking-level rejection, and build the
    retry body if so (ELI-85 follow-up).

    `clamp_to_ladder` passes a requested level straight through whenever the
    model's real ladder is unknown -- and if the target model can't honour it,
    the Gateway hard-rejects the turn. That rejection never streams as a normal
    event (the run delivers a single contentless `final`), so it only surfaces
    via preview recovery at the caller's final fallback.

    Detection is `parse_thinking_rejection`, the same parser that extracts the
    ladder, so the thing that decides to retry and the thing that learns from
    the retry can never disagree about what a rejection is.

    Returns None when `recovered` isn't that rejection (nothing to retry).
    Otherwise returns a fresh dict body with `reasoning_effort` stripped, so
    resending it can no longer hit the same rejection -- which is also what
    makes recursing on this once safe: the retried call resolves
    `resolved_thinking` to None (`clamp_to_ladder` short-circuits on no
    requested level), so this can't match twice in a row.
    """
    if not recovered or resolved_thinking is None:
        return None
    if parse_thinking_rejection(recovered) is None:
        return None
    retry_body = dict(body)
    retry_body.pop("reasoning_effort", None)
    return retry_body


# A Send now's follow-up message is known to be coming; this only bounds a
# browser that dies between the cancel and the send.
_SEND_NOW_HOLD_S = 120.0


async def _current_load(pipe, request):
    """The Pipe of the load Open WebUI serves now, when `pipe` belongs to an
    older load of this Function; else None (ELI-102).

    A deploy execs the new content into a new module, and the first request
    there retires the old load's connection. A turn the old load admitted but
    had not yet connected (the dev build holds new turns while a deploy is
    pending, 24 min on 2026-10-13) then met the retired connection and was
    lost as "gateway connection retired". Open WebUI's own lookup
    (`get_function_module_by_id`, which also sets the valves) names the
    current load."""
    prefix = _function_module_prefix(__name__)
    if request is None or not prefix.startswith("function_"):
        return None
    try:
        from open_webui.functions import get_function_module_by_id

        current = await get_function_module_by_id(request, prefix[len("function_"):])
    except Exception as ex:
        pipe_log(f"  current load lookup failed, serving this turn here: {ex!r}")
        return None
    if current is None or current is pipe or type(current).__module__ == __name__:
        return None
    return current


async def _settle_cancel_intent(conn, session_key, run_id, chat_id, event_call):
    """Ask the page whether this cancel was Stop or Send now, and settle the
    parked turn accordingly (see `settle_parked_turn`)."""
    intent = None
    if event_call and chat_id:
        try:
            resp = await asyncio.wait_for(
                event_call({"type": "execute",
                            "data": {"code": _intent_query_js(chat_id)}}),
                timeout=_INTENT_QUERY_TIMEOUT_S,
            )
            intent = _normalize_event_call_response(resp) or None
        except (Exception, asyncio.CancelledError) as ex:
            pipe_log(f"  cancel intent query failed: {ex!r}")
    pipe_log(f"  cancel intent from the page: {intent or 'unknown'}")
    conn.settle_parked_turn(session_key, run_id, intent, hold_s=_SEND_NOW_HOLD_S)


async def _gateway_run_status(conn, session_key: str) -> str | None:
    """The Gateway's own run status (P26), for a reply that has been quiet.

    A quiet queue does not mean the run ended: it may be queued behind
    another run, in a long tool call, or in a sub-agent yield gap. Returns
    the `sessions.describe` status ("running", "done", "failed",
    "cancelled", ...), so callers can tell a user stop from a dead run. A
    terminal status is checked against `sessions.list` `hasActiveRun`, which
    counts a yield gap as active where describe already says "done"
    (ELI-56); then it is "running". None when the probe failed or the
    session is unknown."""
    try:
        desc = await conn.send_request("sessions.describe", dict(key=session_key), timeout=8)
    except Exception as ex:
        pipe_log(f"  initial describe probe failed: {ex}")
        return None
    session_row = desc.get("session")
    if session_row is None:
        return None
    status = session_row.get("status")
    if status in ("done", "failed", "cancelled") and await _session_list_active(conn, session_key):
        return "running"
    return status


@dataclass
class _TurnState:
    """What one reply still owes the user if it is cancelled or closed now.

    `unsent` are user messages it accepted but has not yet sent to the
    Gateway (its own, plus any carried over from a cancelled reply).
    `run_id` / `queue` are a run it is consuming, its own or one it adopted,
    and the consumer queue buffering that run's events. `parked` is set once
    the cancel was handed to `park_cancelled_turn`.
    """
    conn: object = None
    session_key: str | None = None
    chat_id: str | None = None
    unsent: list = field(default_factory=list)
    run_id: str | None = None
    queue: object = None
    run_text: dict | None = None
    parked: bool = False


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
    - INTERRUPT_GRACE_SECONDS: how long a cancel waits for a follow-up message
      before aborting the run (default 3; 0 aborts immediately)
    """

    class Valves(BaseModel):
        GATEWAY_URL: str = Field(
            default="localhost:18789",
            description="Required. Your OpenClaw Gateway as host:port -- no "
                "http:// or ws:// prefix. Example: 192.168.1.10:18789. It must "
                "be reachable from the Open WebUI backend, which is not "
                "necessarily the same network as your browser."
        )
        GATEWAY_TOKEN: str = Field(
            default="",
            description="Required. The Gateway API token: the value of "
                "`gateway.auth.token` in your OpenClaw config. Ask whoever runs "
                "the Gateway if you don't manage it yourself."
        )
        DEVICE_IDENTITY: str = Field(
            default="",
            description="(Advanced -- normally leave empty.) Ed25519 device "
                "identity JSON. The pipe generates one on first connect and "
                "persists it in STATE_DIR. Set this only to import an "
                "already-approved identity, or to make the companion Status "
                "Action reuse this exact device instead of raising a second "
                "pairing request."
        )
        STATE_DIR: str = Field(
            default="/data/openclaw-bridge",
            description="Directory inside the Open WebUI container where the "
                "device identity and Gateway token are stored. Must survive "
                "restarts: if it is not writable the pipe silently falls back "
                "to /tmp, and you will be asked to approve the device again "
                "after every restart."
        )
        AGENT_ID: str = Field(
            default="main",
            description="Which OpenClaw agent handles these chats. Leave as "
                "'main' unless your Gateway defines additional named agents."
        )
        ENABLE_FILE_SERVER: bool = Field(
            default=True,
            description="Start a small HTTP server on port 18791 to serve "
                "agent-generated images and files. Only used as a fallback when "
                "USE_OWUI_FILES is off or an upload fails. Note it is "
                "unauthenticated -- leave off if port 18791 is exposed."
        )
        USE_OWUI_FILES: bool = Field(
            default=True,
            description="Recommended. Upload agent-generated images and files "
                "through Open WebUI's own Files API so they attach natively and "
                "work over HTTPS. Falls back to the file server if it fails."
        )
        SEND_STOP_ON_CANCEL: bool = Field(
            default=True,
            description="Recommended. When you press Stop, also send the agent "
                "a /stop, so a tool it is running is actually killed -- aborting "
                "the stream alone can leave the tool running."
        )
        INTERRUPT_GRACE_SECONDS: float = Field(
            default=10.0,
            description="Recommended: 10. Fallback only: the Pipe asks the "
                "page whether a cancel was Stop or Send now and acts on that "
                "at once; this window applies when the page cannot say (a "
                "non-English UI, API clients). Sending a message while the agent is "
                "working (Open WebUI's Send now, or sending with the message "
                "queue off) stops the current response. With this set, the "
                "agent's run is kept alive instead: the new reply first shows "
                "the rest of the running turn, then answers your message. A "
                "plain Stop with no new message still stops the run after this "
                "many seconds. 0 stops immediately, as before."
        )
        OWUI_BASE_URL: str = Field(
            default="http://127.0.0.1:8080",
            description="Open WebUI's own base URL, resolved by the Open WebUI "
                "backend itself and not by your browser. The default is correct "
                "for a standard single-container install; change it only if "
                "Open WebUI cannot reach itself at 127.0.0.1:8080."
        )
        OWUI_API_KEY: str = Field(
            default="",
            description="(Optional.) Only needed if media uploads fail with an "
                "auth error -- the pipe normally reuses your own login token."
        )
        FILE_SERVER_BASE_URL: str = Field(
            default="http://localhost:18791",
            description="Legacy fallback only: base URL of the built-in media file "
                "server, used when USE_OWUI_FILES is off or an upload fails. The "
                "browser resolves this URL, not OWUI, so the default only works when "
                "you browse OWUI from the same host. Point it at an address your "
                "browser can reach if you depend on this fallback."
        )
        DEFAULT_MODEL: str = Field(
            default="",
            description="Model used when you pick 'OpenClaw · Default'. Leave empty to "
                "use the agent's own configured model. The dropdown lists models "
                "discovered from your Gateway; before the first successful "
                "connection it shows a built-in example list that may not match "
                "your Gateway.",
            json_schema_extra={
                "input": {"type": "select", "options": "get_model_options"}
            }
        )
        MAX_MODELS: int = Field(
            default=30,
            description="Maximum number of models to show in the selector.",
            ge=1,
            le=100
        )
        AUTO_TITLE: bool = Field(
            default=True,
            description="Auto-generate a chat title after the first exchange (like native OWUI). "
                "Uses a fast model; best-effort, non-blocking."
        )
        COMPOSER_STATUS_BUTTON: bool = Field(
            default=True,
            description="Add an OpenClaw 'Status' button to the composer row of "
                "OpenClaw chats (it opens the same dialog as the Status Action's "
                "message button, so that Action must be installed). Installed "
                "into the page at the start of each turn and when a tab opens an "
                "OpenClaw chat. Turn off to stop delivering it; an already open "
                "tab keeps it until reload."
        )
        TITLE_GEN_AGENT_ID: str = Field(
            default="title-gen",
            description="Agent id used for background chat-title generation. It must "
                "exist on the Gateway (default 'title-gen'); if it does not, titles are "
                "silently skipped. Kept separate from AGENT_ID so title generation never "
                "queues behind your conversation."
        )

    # Set from the live valves so the DEFAULT_MODEL dropdown (a classmethod with
    # no access to self.valves) reads the same cache pipes() writes.
    _last_state_dir = ""

    def __init__(self):
        self.valves = self.Valves()
        self._active_tool_args: dict[str, str] = {}
        # Will be set per pipe() call
        self._current_session_key: str | None = None
        self._current_run_id: str | None = None
        self._connection: _GatewayConnection | None = None

    async def pipes(self):
        """Expose multiple OWUI model-selector entries from one pipe.
        
        Dynamically discovers models from the OpenClaw Gateway (or cache)
        and returns one entry per model plus a 'Default' entry.
        """
        type(self)._last_state_dir = getattr(self.valves, "STATE_DIR", "") or ""
        # Every page load lists models, so this is the earliest point the
        # composer watcher can start after an Open WebUI restart or redeploy.
        _ensure_composer_watcher(lambda: self.valves)
        # ...and the earliest point to finish replies a restart orphaned.
        _ensure_orphan_reconcile(lambda: self.valves)
        models, source = await _discover_models_with_source(self.valves)

        # Apply safety cap
        if len(models) > self.valves.MAX_MODELS:
            models = models[:self.valves.MAX_MODELS]

        # A hardcoded list is a guess, not a discovery. Say so in the only place
        # the user is looking -- the selector itself -- rather than letting five
        # invented models masquerade as their Gateway's. Suffixing the display
        # name is deliberate: `id` stays the untouched routing key.
        unverified = source == MODELS_SOURCE_FALLBACK
        entries = []
        for m in models:
            friendly = _friendly_name(m)
            provider = _provider_from_key(m["key"])
            name = f"{friendly} ({provider}) · OpenClaw"
            if unverified:
                name += UNVERIFIED_MODEL_SUFFIX
            entries.append({"id": m["key"], "name": name})

        # Always prepend Default at top. It is the one entry that always works
        # in this state: it clears the override and uses the agent's own model,
        # so it needs no Gateway round trip to be correct.
        #
        # If discovery ever comes back empty (fallback list included), say the
        # warning on Default instead of leaving a bare, unexplained selector.
        default_name = "OpenClaw · Default"
        if unverified and not entries:
            default_name += UNVERIFIED_MODEL_SUFFIX
        return [{"id": "default", "name": default_name}] + entries

    @classmethod
    def get_model_options(cls):
        """Return model options for the DEFAULT_MODEL valve dropdown.

        Reads synchronously from the model cache or fallback list.

        Uses the state dir the running Pipe last resolved (recorded in
        _last_state_dir) rather than _state_dir() with no argument. Being a
        classmethod there is no self.valves here, and the no-argument form
        resolves to OPENCLAW_BRIDGE_STATE_DIR or /data/openclaw-bridge -- so for
        anyone who set the STATE_DIR valve it read a path nothing ever writes,
        never found the cache, and therefore labelled every option
        "Gateway not reached" permanently, long after successful discovery.
        """
        cache = _read_json_file(
            os.path.join(_state_dir(cls._last_state_dir), "models-cache.json")
        )
        cached = cache.get("models") if cache else None
        models = cached or _FALLBACK_MODELS
        # Same honesty as pipes(): before the first successful connection this
        # dropdown is a built-in example list, not a reference of what the
        # Gateway actually offers.
        suffix = "" if cached else UNVERIFIED_MODEL_SUFFIX
        return [
            {
                "value": m["key"],
                "label": f"{_friendly_name(m)} ({_provider_from_key(m['key'])}){suffix}",
            }
            for m in models
        ]

    def _selected_preset(self, body):
        """Extract the model key from the OWUI model string."""
        model = str(body.get("model", ""))
        # Split on the FIRST dot only: the function id (e.g. "openclaw_gateway")
        # never contains a dot, but model keys can (e.g. "gemini-3.1-pro-preview").
        # rsplit would wrongly cut inside the version number.
        suffix = model.split(".", 1)[-1]
        if suffix == "default":
            return "default"
        return suffix  # raw model key

    def _model_override_for_preset(self, preset):
        """Return the model string to pass to sessions.patch."""
        if preset == "default":
            return self.valves.DEFAULT_MODEL.strip() or None
        return preset

    # ── Auto-title helpers ──────────────────────────────────────────

    async def _auto_title(self, body, conn, visible_message_text,
                          owui_origin_chat_id, bearer_token):
        """Generate a chat title and set it via OWUI's REST API.

        Fires after the SECOND successful exchange (first message + response),
        when OWUI's chat_id is stable. The first request uses a temporary ID.
        Best-effort, non-blocking -- failures are logged but never surfaced.
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

        Sends a chat.send to a temp session and reads the run from its own
        signals: its assistant text up to its final event, or, when the final
        was lost, its settle row and the transcript (review 2af1 timers). The
        old 7 x 3 s `sessions.preview` poll dropped any title slower than that.

        Runs under TITLE_GEN_AGENT_ID (a separate configured agent from
        AGENT_ID), not the main conversation's agent. A bare/unprefixed
        sessionKey previously defaulted onto the SAME agent as the live
        conversation, so this background call shared its CLI process/queue
        lane and queued behind any heavy turn (2026-07-10 regression). A
        distinct agent id gets its own lane.
        """
        prompt = (
            "Create a concise title (3-5 words) with a relevant emoji "
            "for this conversation. Output ONLY the title, nothing else "
            "-- no quotes, no explanation.\n\n"
            f"User message: {user_msg}\n\n"
            f"Assistant response: {assistant_msg}"
        )

        agent_id = self.valves.TITLE_GEN_AGENT_ID.strip() or "title-gen"
        title_session = f"agent:{agent_id}:title-gen-{uuid.uuid4().hex[:12]}"
        run_id = f"title-{title_session}"
        # Listen before sending: a fast run can end before chat.send returns.
        queue = conn.register_consumer(title_session, run_id)
        try:
            try:
                resp = await conn.send_request(
                    "chat.send",
                    dict(sessionKey=title_session, message=prompt, idempotencyKey=run_id),
                    timeout=15,
                )
            except Exception as e:
                pipe_log(f"Auto-title: chat.send failed: {e}")
                return None
            if resp.get("runId") and resp["runId"] != run_id:
                conn.unregister_consumer(title_session, run_id, queue=queue)
                run_id = resp["runId"]
                queue = conn.register_consumer(title_session, run_id)
            text = ""
            while True:
                msg = await queue.get()
                kind = msg.get("type")
                if kind == _RESTART_MARKER_TYPE:
                    pipe_log("Auto-title: Gateway restarted; no title")
                    return None
                if kind == _ROW_MARKER_TYPE:
                    row = msg.get("row") or {}
                    if row.get("hasActiveRun") is False and (
                            msg.get("resync") or row.get("lastRunId") == run_id):
                        hist = await conn.session_history(title_session, limit=10)
                        found, whole = _history_text_for_run(hist, run_id)
                        return self._clean_title_text(whole or text) if (found or text) else None
                    continue
                payload = msg.get("payload") or {}
                data = payload.get("data") or {}
                if payload.get("stream") == "assistant":
                    text += data.get("delta") or ""
                if payload.get("stream") == "lifecycle" and data.get("phase") == "error":
                    pipe_log(f"Auto-title: title run failed: {data.get('error')}")
                    return None
                if payload.get("state") in ("error", "aborted"):
                    return None
                if payload.get("state") == "final":
                    final = _coerce_text(payload.get("message")) if payload.get("message") else ""
                    return self._clean_title_text(final or text)
        finally:
            conn.unregister_consumer(title_session, run_id)

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

    async def _rest_snapshot_message(self, chat_id: str, message_id: str,
                                      content: str, bearer_token: str | None):
        """Persist the in-flight assistant message via OWUI's REST API.

        Deliberately NOT the __event_emitter__ `replace` event, and
        deliberately NOT `POST /api/v1/chats/{id}/messages/{message_id}`
        either -- that dedicated endpoint calls an `event_emitter` with a
        `chat:message` socket event, which races the live HTTP delta stream
        exactly like `replace` does (P17/P19/P23/P25/P27, 6e5f372). This
        uses the same whole-chat update endpoint `_set_owui_chat_title`
        already calls mid-conversation (`POST /api/v1/chats/{id}` with no
        `message_id` in the path), which was confirmed by reading the live
        OWUI 0.11.0 source (`bridge-rehydration-rest-snapshot-verify`,
        2026-08-26) to never emit any Socket.IO frame -- it writes the DB
        row directly with nothing for the frontend's live-stream handlers
        to even receive.

        Read-modify-write is required because `chat.messages[]` (the flat,
        active-branch array) has no server-side merge -- sending it fully
        replaces the old array, per `routers/chats.py::update_chat_by_id`
        (`updated_chat = {**chat.chat, **form_data.chat}`). `chat.history
        .messages{}` (keyed by message id) merges whole-object-per-id
        server-side (`Chats.merge_history`), so only the one patched
        message needs to be sent there.

        Skipped on OWUI 0.12+ (`_owui_owns_inflight_message`): there the
        write is discarded for an unfinished message and OWUI serves the
        in-flight text to reloading clients on its own.
        """
        token = bearer_token or self.valves.OWUI_API_KEY
        if not token or not chat_id or not message_id:
            return
        if _owui_owns_inflight_message():
            return
        base = self.valves.OWUI_BASE_URL.rstrip('/')
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        def _get_chat():
            req = urllib.request.Request(
                f"{base}/api/v1/chats/{chat_id}", headers=headers, method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))

        try:
            current = await asyncio.to_thread(_get_chat)
        except Exception as ex:
            pipe_log(f"  rest_snapshot: GET failed: {ex}")
            return

        chat_obj = (current or {}).get("chat") or {}
        messages = chat_obj.get("messages")
        history_messages = ((chat_obj.get("history") or {}).get("messages")) or {}

        target = history_messages.get(message_id)
        if target is None and isinstance(messages, list):
            target = next(
                (m for m in messages if isinstance(m, dict) and m.get("id") == message_id),
                None,
            )
        if target is None:
            # Message doesn't exist yet on the OWUI side (e.g. very first
            # chunk of a brand-new chat, before OWUI's own request handler
            # has created the row) -- nothing to patch onto.
            return

        patched = dict(target)
        patched["content"] = content

        patch: dict = {"history": {"messages": {message_id: patched}}}
        if isinstance(messages, list):
            patched_messages = [
                (patched if isinstance(m, dict) and m.get("id") == message_id else m)
                for m in messages
            ]
            patch["messages"] = patched_messages

        data = json.dumps({"chat": patch}).encode()

        def _post_chat():
            req = urllib.request.Request(
                f"{base}/api/v1/chats/{chat_id}",
                data=data, headers=headers, method="POST",
            )
            urllib.request.urlopen(req, timeout=10)

        try:
            await asyncio.to_thread(_post_chat)
        except Exception as ex:
            pipe_log(f"  rest_snapshot: POST failed: {ex}")

    async def pipe(self, body, __event_emitter__, __event_call__=None,
                   __user__=None, __metadata__=None, __request__=None,
                   __task__=None, __task_body__=None):
        """Main pipe entry point -- called by Open WebUI for each user message.

        Thin wrapper around _pipe_impl so dev-only deploy-coordination hooks
        (ELI-24) can bracket every turn without re-indenting the whole body.
        """
        # DEV-ONLY-START
        async def _devcoord_report_wait(waited_s):
            await _emit_status(
                __event_emitter__,
                f"⏳ Deploy in progress, waiting to start ({int(waited_s)}s)...",
                done=False,
            )
        await _devcoord_wait_if_deploy_pending(on_wait=_devcoord_report_wait)
        # DEV-ONLY-END
        current = await _current_load(self, __request__)
        if current is not None:
            pipe_log("Turn admitted by a superseded load of this Function: the current "
                     f"load ({type(current).__module__}) serves it")
            async with contextlib.aclosing(current.pipe(
                body, __event_emitter__, __event_call__=__event_call__,
                __user__=__user__, __metadata__=__metadata__, __request__=__request__,
                __task__=__task__, __task_body__=__task_body__,
            )) as turn_gen:
                async for item in turn_gen:
                    yield item
            return
        # DEV-ONLY-START
        _devcoord_marker = _devcoord_turn_begin()
        _devcoord_beat = spawn_background(_devcoord_heartbeat(_devcoord_marker))
        # DEV-ONLY-END
        try:
            # aclosing: when OWUI closes this generator, close the inner one
            # now rather than whenever it is garbage-collected, so its cancel
            # handling runs before the next message arrives.
            async with contextlib.aclosing(self._pipe_impl(
                body, __event_emitter__, __event_call__=__event_call__,
                __user__=__user__, __metadata__=__metadata__, __request__=__request__,
                __task__=__task__, __task_body__=__task_body__,
            )) as turn_gen:
                async for item in turn_gen:
                    yield item
        finally:
            # DEV-ONLY-START
            _devcoord_beat.cancel()
            _devcoord_turn_end(_devcoord_marker)
            # DEV-ONLY-END
            pass

    async def _pipe_impl(self, body, __event_emitter__, __event_call__=None,
                   __user__=None, __metadata__=None, __request__=None,
                   __task__=None, __task_body__=None, _initial_visible_text="",
                   _send_texts=None):
        """One reply: runs `_pipe_turn`, and makes sure a cancel or close at
        ANY point hands what the reply still owes to the next message (see
        `park_cancelled_turn`) instead of dropping it.

        `_pipe_turn` parks for itself while it is streaming a run, where it
        also snapshots and shows "Stopped". This catches every other point:
        before the message is sent (model patch, queue wait, a yield outside
        the stream loop) and around the adopt / retry recursion. OWUI ends a
        reply either by cancelling its task (CancelledError at an await) or
        by closing the generator (GeneratorExit at a yield); both land here.

        `_send_texts` replaces this request's own text as what to send, for
        the recursions that send messages carried over from earlier replies.
        """
        turn = _TurnState()
        try:
            async with contextlib.aclosing(self._pipe_turn(
                body, __event_emitter__, __event_call__=__event_call__,
                __user__=__user__, __metadata__=__metadata__,
                __request__=__request__, __task__=__task__,
                __task_body__=__task_body__,
                _initial_visible_text=_initial_visible_text,
                _send_texts=_send_texts, turn=turn,
            )) as turn_gen:
                async for item in turn_gen:
                    yield item
        except (asyncio.CancelledError, GeneratorExit) as stop:
            if (not turn.parked and turn.conn is not None and turn.session_key
                    and (turn.unsent or turn.run_id)):
                turn.parked = True
                pipe_log(
                    f"Reply {type(stop).__name__} outside the stream loop: parking "
                    f"{len(turn.unsent)} unsent message(s), run "
                    f"{str(turn.run_id)[:20]}"
                )
                turn.conn.park_cancelled_turn(
                    turn.session_key, turn.run_id, turn.queue, turn.unsent,
                    float(getattr(self.valves, "INTERRUPT_GRACE_SECONDS", 0) or 0),
                    send_stop=self.valves.SEND_STOP_ON_CANCEL,
                    run_text=turn.run_text,
                )
                await _settle_cancel_intent(
                    turn.conn, turn.session_key, turn.run_id, turn.chat_id,
                    __event_call__)
            raise

    async def _pipe_turn(self, body, __event_emitter__, __event_call__=None,
                   __user__=None, __metadata__=None, __request__=None,
                   __task__=None, __task_body__=None, _initial_visible_text="",
                   _send_texts=None, turn=None):
        """Uses a shared persistent WS connection; no per-message reconnect,
        no global lock, and no 60s timeout.

        `_initial_visible_text` seeds the terminal snapshot for the
        thinking-rejection retry recursion below: the retry note belongs to
        the turn that's ending, not the one about to start, but this call's
        own `visible_message_text` is what the forced terminal `replace`
        snapshot persists -- a note recorded only on the caller's copy would
        show while streaming and vanish on reload.
        """
        if self.valves.ENABLE_FILE_SERVER:
            _start_file_server()
        preset = self._selected_preset(body)
        model_override = self._model_override_for_preset(preset)

        # Per-chat thinking level (ELI-85). `reasoning_effort` is the single
        # source of truth: Open WebUI's own Advanced Params control writes it,
        # and so does the companion Thinking filter's toggle. Reading the field
        # rather than a filter-specific one means either control works, and the
        # bridge keeps working for anyone who never installs the filter.
        # Absent means absent: no `thinking` is sent and the agent's configured
        # level applies. Validation against the model's real ladder happens
        # below, once describe has told us what this session resolves to.
        requested_thinking = body.get("reasoning_effort") or None
        if requested_thinking is not None:
            requested_thinking = str(requested_thinking).strip().lower() or None
        session_thinking_ladder = None
        session_model_key = None
        session_runtime = None

        # --- P15: Short-circuit OWUI background tasks ---
        # O10 addendum: image_prompt_generation and function_calling also get
        # skipped here -- both already have a documented, silent OWUI-side
        # fallback for an empty response (raw user message / "no tool
        # needed" respectively -- see PLAN.md O10), so skipping them costs
        # nothing and saves a full agent turn. moa_response_generation is
        # deliberately excluded -- it is a user-triggered "Merge Responses"
        # request whose only output path is the streamed content itself, so
        # a no-op here would show as a visibly empty result rather than a
        # graceful fallback.
        if __task__ and __task__ in (
            "title_generation",
            "tags_generation",
            "follow_up_generation",
            "emoji_generation",
            "autocomplete_generation",
            "query_generation",
            "image_prompt_generation",
            "function_calling",
        ):
            pipe_log(f"Skipping OWUI background task: {__task__}")
            return

        # --- Extract user message ---
        # OWUI sends `content` as a plain string for text-only messages, but
        # as a list of content blocks (`{"type": "text", ...}` /
        # `{"type": "image_url", ...}`) whenever the user attaches an image.
        # Coercing through _coerce_text (already used elsewhere for gateway
        # event payloads with the same block shape) extracts and joins any
        # text blocks and safely no-ops on plain strings.
        messages = body.get("messages", [])
        raw_content = messages[-1]["content"] if messages else ""
        text = _coerce_text(raw_content)
        has_image = _content_has_image(raw_content)
        image_attachments = _extract_image_attachments(raw_content) if has_image else []
        if not text and not image_attachments:
            if has_image:
                yield (
                    "**Couldn't read that image** -- only base64-embedded "
                    "images are supported right now. Please describe what's "
                    "in the image in words and send that instead."
                )
            else:
                yield "No message"
            return

        if has_image and not image_attachments:
            # An image block was present but couldn't be turned into an
            # attachment (e.g. not a base64 data URL) -- text still goes
            # through, but say so rather than silently dropping the image.
            yield (
                "_(Note: that image couldn't be relayed to the agent -- "
                "only the text below was sent.)_\n\n"
            )

        _ensure_orphan_reconcile(lambda: self.valves)
        await _emit_status(__event_emitter__, "Thinking...", done=False)
        if self.valves.COMPOSER_STATUS_BUTTON and not _initial_visible_text:
            # Cheap and idempotent: installs the composer Status button in the
            # tabs showing this chat (a no-op where it is already there). Once
            # per turn: the thinking-retry recursion skips it.
            _ensure_composer_watcher(lambda: self.valves)
            await _emit_composer_button(
                __event_emitter__,
                session_id=(__metadata__ or {}).get("session_id"),
                chat_id=(__metadata__ or {}).get("chat_id"),
                model_prefixes=_composer_note_model(body.get("model")),
            )
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

        # DIAG (2026-07-18, ELI-56): log the OWUI message_id + a short content
        # hash of the user text so a *re-fired / duplicate* completion for the
        # same message -- the suspected cause of the phantom "1-event, no-text"
        # turns (e.g. on a socket reconnect behind a reverse proxy) -- becomes
        # visible: two pipe() invocations with the SAME owui_msg_id / text_sha
        # close together is a smoking gun. Pure logging, no behavior change.
        _diag_md = __metadata__ or {}
        _diag_sha = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]
        self._diag_text_sha = _diag_sha
        pipe_log(
            f"  [diag] owui_msg_id={_diag_md.get('message_id')} "
            f"session_id={str(_diag_md.get('session_id'))[:12]} "
            f"text_sha={_diag_sha} text_preview={text[:48]!r}"
        )

        if turn is None:
            turn = _TurnState()
        turn.conn = conn
        turn.session_key = session_key
        turn.chat_id = owui_origin_chat_id
        turn.unsent = list(_send_texts) if _send_texts else [text]
        if owui_origin_chat_id and __event_emitter__:
            # Lets a later cancel ask the page whether it was Stop or Send
            # now. Idempotent in the page; fire-and-forget.
            try:
                await __event_emitter__(
                    {"type": "execute", "data": {"code": _INTENT_HOOK_JS}})
            except Exception as ex:
                pipe_log(f"  intent hook install failed: {ex!r}")
        # What actually went out in this turn's chat.send, for the
        # thinking-rejection retry to send again.
        sent_texts = list(turn.unsent)

        # A cancel of this session's previous response that arrived just
        # before this message is OWUI's "Send now" / interrupt, not a Stop:
        # take over what it left (see `park_cancelled_turn`): its run, still
        # running with its events buffered, and any message it had not sent
        # yet. Claimed this early because the model patch below can take
        # seconds and the grace window is short.
        adopted_run_id = None
        adopted_queue = None
        adopted_run_text = None
        claimed = await conn.claim_cancelled_turn(session_key)
        if claimed:
            if claimed["carried"]:
                turn.unsent = claimed["carried"] + turn.unsent
                pipe_log(
                    f"  carrying {len(claimed['carried'])} unsent message(s) "
                    "from the cancelled reply"
                )
            adopted_run_id = claimed["run_id"]
            adopted_queue = claimed["queue"]
            if adopted_run_id:
                adopted_run_text = claimed.get("run_text")
                turn.run_id = adopted_run_id
                turn.queue = adopted_queue
                turn.run_text = adopted_run_text
                pipe_log(
                    f"  new message right after a cancel: keeping run "
                    f"{adopted_run_id[:20]}... alive and continuing it in this "
                    "reply before sending the message"
                )

        # Apply the model with one retry. sessions.patch is idempotent (set the
        # session's model to a fixed value), and under a burst of near-
        # simultaneous messages the RPC can transiently time out or return a
        # racing/stale row (observed once at ~8 messages in 3s). A single retry
        # absorbs that blip instead of surfacing a scary "Model selection error"
        # to the user; a genuine misconfig still fails on the second attempt.
        # ELI-59 (fuller fix): the burst's patches are all redundant -- same
        # session, same model -- so (a) a per-session lock collapses them to one
        # in-flight RPC, and (b) a TTL cache of the last applied model lets the
        # waiters (and the next ~60s of same-model messages) skip the RPC
        # entirely. Failure invalidates the cache so nothing sticks.
        patch_err = None
        async with conn.model_patch_lock(session_key):
            if conn.model_patch_cached(session_key, model_override):
                pipe_log(
                    f"  model patch skipped (cached: "
                    f"{model_override or 'agent default'})"
                )
            else:
                for _patch_attempt in range(2):
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
                        conn.record_model_patched(session_key, model_override)
                        patch_err = None
                        break
                    except Exception as e:
                        patch_err = e
                        pipe_log(
                            f"  [diag] model patch attempt {_patch_attempt + 1} failed: "
                            f"{type(e).__name__}: {e!r}"
                        )
                        if _patch_attempt == 0:
                            await asyncio.sleep(0.5)
                if patch_err is not None:
                    conn.invalidate_model_patch(session_key)
        if patch_err is not None:
            await _emit_status(__event_emitter__, "", done=True)
            yield _explain_session_patch_failure(
                patch_err,
                model_override=model_override,
                agent_id=self.valves.AGENT_ID,
            )
            return

        # Tool cards for this session's runs whoever started them, including
        # this reply's own run after a reconnect (see `ensure_message_subscription`).
        await conn.ensure_message_subscription(session_key)

        # --- Concurrency: queue behind an active run, never steer-merge ---
        # A message that arrives while a run is active must become its OWN run,
        # not a steer-merge into the active run. Steer-merging (the gateway's
        # default when a run is active) returns a *phantom* runId that never
        # runs independently and emits no terminal -- the injected message just
        # merges into the active run -- so a bubble bound to it renders nothing
        # ("No visible response"). Instead we wait for the active run to reach a
        # definite terminal, then send this message as a fresh run with its own
        # runId + terminal. This matches OWUI's default "queue" UX and can never
        # strand the bubble. (This is a deterministic wait on the gateway's own
        # run status, not a heuristic timer.)
        # Whether the session already has an in-progress run must be decided
        # AUTHORITATIVELY by the gateway (sessions.describe), NOT by the
        # per-worker `active_run_id_for_session` registry: OWUI runs multiple
        # worker processes, so a run started while another worker handled the
        # previous turn is invisible to this worker's registry. That blind spot
        # let a concurrent message fall through to a fresh chat.send while the
        # gateway still had an active run -- the gateway steer-merged it and
        # returned a runId that instantly emits a single 'final' with no text,
        # so the bubble rendered nothing and the real answer leaked out as a
        # separate message. (Verified from live pipe logs 2026-07-18.)
        async def _list_has_active_run() -> bool | None:
            """Authoritative live run signal from sessions.list (hasActiveRun).
            Returns None if the probe fails or our session isn't in the list."""
            try:
                lst = await conn.send_request("sessions.list", dict(), timeout=8)
            except Exception as ex:
                pipe_log(f"  active-check sessions.list failed: {ex}")
                return None
            for s in (lst.get("sessions") or []):
                if s.get("key") == session_key:
                    return bool(s.get("hasActiveRun"))
            return None

        async def _session_run_active() -> bool:
            # Fast path: describe exposes a persisted `status`. When it names a
            # live run state we're done. But describe.status flips to 'done'
            # during a subagent-yield gap (the run is only suspended and will
            # resume -- ELI-56), so a non-active status is NOT conclusive:
            # confirm against sessions.list's authoritative hasActiveRun before
            # treating the session as free to send into.
            nonlocal session_thinking_ladder, session_model_key, session_runtime
            try:
                desc = await conn.send_request(
                    "sessions.describe", dict(key=session_key), timeout=8
                )
                row = desc.get("session") or {}
                status = row.get("status")
                session_runtime = (row.get("agentRuntime") or {}).get("id") or session_runtime
                # Harvest the thinking ladder from the describe we are already
                # making. This probe runs before every send, so the per-model
                # validation below costs no extra round trip, and piggybacking
                # rather than adding a describe of its own means the ladder can
                # never be fresher or staler than the run state it was read
                # alongside.
                #
                # Describe is a WEAK source and is treated as one. It resolves
                # the ladder with no model catalog in scope, so for a
                # catalog-gated model it reports the generic base profile
                # rather than the truth -- live, it advertised all 8 levels for
                # claude-cli/claude-opus-5, whose real ladder is ["off"]. Good
                # enough to seed the dropdown; not good enough to clamp a send
                # against once the Gateway itself has ruled.
                session_model_key = _session_model_key(row)
                ladder = _session_thinking_ladder(desc)
                if ladder:
                    session_thinking_ladder = ladder
                    _record_thinking_ladder(
                        ladder, model_key=session_model_key,
                        state_dir=getattr(self.valves, "STATE_DIR", ""))
                if row.get("activeRunId") or status in _ACTIVE_RUN_STATES:
                    return True
            except Exception as ex:
                # Describe failed: fall back to the list probe below, and if
                # that also fails, to the (per-worker, possibly-blind) registry.
                pipe_log(f"  active-check describe failed: {ex}")
                status = None
            list_active = await _list_has_active_run()
            if list_active is not None:
                active = _session_active_from_signals(status, list_active)
                if active and status not in _ACTIVE_RUN_STATES:
                    pipe_log(
                        f"  active-check: describe.status={status!r} but "
                        "list.hasActiveRun=True (subagent-yield gap; queueing)"
                    )
                return active
            return conn.active_run_id_for_session(session_key) is not None

        if (adopted_run_id and adopted_queue is None
                and not await _session_run_active()):
            # It finished during the cancel/re-send gap and nothing buffered
            # its events: nothing to continue. (With a parked queue the run's
            # tail and final are waiting in it, so it is always adopted.)
            pipe_log("  adopted run already ended; sending the message normally")
            adopted_run_id = None
            adopted_run_text = None
            turn.run_id = None
            turn.run_text = None
        if adopted_run_id:
            # Stream the rest of the adopted run into THIS reply. The message
            # itself is sent afterwards, as its own run, by the recursive call
            # at the end of this turn: sending it now would steer into the
            # live run, and the claude-cli runtime has no steering hook (the
            # gateway acks "started", then drops the message once the run
            # ends -- measured 2026-10-11).
            our_run_id = adopted_run_id
            self._current_run_id = our_run_id
            resolved_thinking = None
            pending_clamp_note = ""
            if adopted_queue is not None:
                queue = adopted_queue
            else:
                queue = conn.register_consumer(session_key, our_run_id)
            turn.queue = queue
            await _emit_status(
                __event_emitter__,
                "Continuing the current task; your message is next…",
                done=False,
            )
        else:
            # Serialize the queue-release -> chat.send -> run-registration critical
            # section per session. Two messages released from the queue at the same
            # instant would otherwise both send into the same idle window; the
            # gateway steer-merges the second into the first, which then renders
            # nothing ("no reply text"). Holding this lock only across the wait+send
            # (NOT the event loop below) means the next waiter re-checks AFTER our
            # run is registered and observes it active (sessions.list hasActiveRun),
            # so it waits its turn instead of racing us. Confirmed thundering-herd
            # phantom: two Captured runIds 10ms apart under a burst release (ELI-56).
            # NOTE: do NOT prepend a `/queue followup` directive here. On the
            # 2026.7.1 gateway that directive is NOT stripped from a device
            # chat.send -- it leaked verbatim into the delivered message.
            async with conn.session_send_lock(session_key):
                # Wait for the Gateway to push that the run settled
                # (`sessions.changed`, see `session_change_event`), re-reading
                # the authoritative state after each change. No poll, no
                # settle window and no ceiling (ELI-88): the old 30-minute
                # ceiling released a message into a run that was still going,
                # where the Gateway queued it and claude-cli dropped it. A
                # resume that starts right after a settle is caught after the
                # send instead (see `enqueued_not_started`). Stop cancels this
                # wait like any other.
                ever_active = False
                while True:
                    changed = conn.session_change_event(session_key)
                    if not await _session_run_active():
                        break
                    if not ever_active:
                        ever_active = True
                        pipe_log("Session has an active run (gateway-authoritative); "
                                 "queueing behind it")
                        await _emit_status(
                            __event_emitter__,
                            "Queued: waiting for the current run to finish…",
                            done=False,
                        )
                    await changed.wait()
                if ever_active:
                    pipe_log("  active run settled; sending queued message as its own run")

                # --- Send this message as its own fresh run (still holding the lock
                #     so the next waiter observes our run before deciding to send) ---
                # The OWUI message id in the run id ties the run to the reply
                # it streams into, so a reply orphaned by an Open WebUI restart
                # is matched to its run by identity (ELI-90, `_stub_turn`).
                reply_id = (__metadata__ or {}).get("message_id")
                idempotency_key = (pipe_run_id(chat_id, reply_id) if reply_id
                                   else f"msg-{chat_id}-{time.time()}")
                # Fit the requested level to what this session's model actually
                # supports. The dropdown is a union across models, so a level that
                # is real for one model can be unavailable here. Clamping is
                # downward only and always explained: silently answering at a level
                # other than the one asked for is worse than a short note, and
                # silently erroring out is worse still.
                #
                # A ladder the Gateway STATED (by rejecting a send) beats the one
                # describe reported, because describe answers this question without
                # a model catalog and overreports for catalog-gated models. This is
                # what makes the rejection cost one turn per model instead of one
                # turn every turn.
                #
                # Look up by `model_override` (the runtime-namespaced routing key,
                # e.g. "claude-cli/claude-opus-5") before `session_model_key` (the
                # vendor key describe resolves it to, e.g. "anthropic/claude-opus-5"
                # -- see `_RUNTIME_PROVIDER_NAMESPACES`). The Gateway's rejection
                # text names the model by its routing key, so that is where
                # `_record_thinking_ladder` files the authoritative entry; reading
                # back under the vendor key alone missed it every time and repeated
                # the same round-trip rejection (and its note) on every turn.
                _state_dir_valve = getattr(self.valves, "STATE_DIR", "")
                effective_ladder = (_read_model_thinking_ladder(model_override, _state_dir_valve)
                                    or _read_model_thinking_ladder(session_model_key, _state_dir_valve)
                                    or session_thinking_ladder)
                resolved_thinking, thinking_note = clamp_to_ladder(
                    requested_thinking, effective_ladder)
                # Held until `visible_message_text` exists below (it isn't defined
                # yet at this point in the turn) so the note that streams here also
                # ends up in the forced terminal snapshot -- otherwise it shows
                # while streaming and is gone after a reload.
                pending_clamp_note = ""
                if thinking_note:
                    pipe_log(f"thinking: {thinking_note}")
                    # Logged every time, shown once: the dropdown keeps the level
                    # selected, so an unchanged mismatch would otherwise stamp the
                    # same italic line above every answer in the chat.
                    if conn.should_announce_thinking_note(session_key, thinking_note):
                        pending_clamp_note = f"_{thinking_note}_\n\n"
                        yield pending_clamp_note
                # Listen before sending: the run's first events can arrive
                # before chat.send returns, and with no consumer they would go
                # to the turn writer, which takes this reply's message over
                # while the reply still streams (review 2af1 R12). The run id
                # is the idempotency key.
                queue = conn.register_consumer(session_key, idempotency_key)
                try:
                    send_resp = await conn.send_request(
                        "chat.send",
                        _owui_chat_send_params(
                            session_key=session_key,
                            message="\n\n".join(t for t in turn.unsent if t),
                            idempotency_key=idempotency_key,
                            owui_chat_id=owui_origin_chat_id,
                            owui_user_id=owui_origin_user_id,
                            attachments=image_attachments,
                            thinking=resolved_thinking,
                        ),
                        timeout=30
                    )
                except asyncio.TimeoutError:
                    conn.unregister_consumer(session_key, idempotency_key, queue=queue)
                    await _emit_status(__event_emitter__, "", done=True)
                    yield "**Timeout:** Gateway did not respond to chat.send"
                    return
                except Exception as e:
                    conn.unregister_consumer(session_key, idempotency_key, queue=queue)
                    await _emit_status(__event_emitter__, "", done=True)
                    yield f"**Error sending message:** {e}"
                    return
                our_run_id = send_resp.get("runId") or idempotency_key
                self._current_run_id = our_run_id
                pipe_log(f"Captured runId: {our_run_id}")
                if our_run_id != idempotency_key:
                    conn.unregister_consumer(session_key, idempotency_key, queue=queue)
                    queue = conn.register_consumer(session_key, our_run_id)
                sent_texts = list(turn.unsent)
                turn.unsent = []
                turn.run_id = our_run_id
                turn.queue = queue
                turn.run_text = None
        # Tell the persistent connection which OWUI message this run streams
        # into, so a turn writer continues that message from a shadow renderer
        # if this inline turn ends before the run does (cancel, close, a
        # torn-down request). No-op without a stable message id. See
        # _turn_route_event (writer.py) and _TurnRenderer (gateway.py).
        owui_message_id = (__metadata__ or {}).get("message_id")
        conn.register_run_target(
            session_key, our_run_id, owui_origin_chat_id, owui_message_id,
            thinking=resolved_thinking,
        )
        # When the Gateway had admitted this reply's run (chat.send returned;
        # an adopted run already ran). An idle row read after it means the
        # run is over; one read before may show the previous run's settle
        # (review 2af1 N4).
        admitted_at = float("-inf") if adopted_run_id else time.monotonic()
        # A gap or a resync since: this run's own final and settle may be
        # lost, so another run's settle row may be the first sign it ended.
        signals_lost = False
        if adopted_run_id and adopted_queue is not None:
            conn.seed_run_renderer(session_key, our_run_id, queue,
                                   run_text=adopted_run_text)
        # Extracted once up front (not just at the terminal snapshot/auto-title
        # step further below) so the mid-run REST snapshot gate has a token to
        # use throughout the streaming loop, not only after it ends.
        bearer_token = _extract_request_bearer(__request__)

        # --- Consume events ---
        done = False
        event_count = 0
        text_yielded = False
        aborted = False
        first_event_arrived = False
        # The Gateway acks every chat.send with "started"; one that it queued
        # behind a run (or steered into one) instead gets a lone `chat` final
        # with no message as its first and only event.
        enqueued_not_started = False
        last_item_text = ""
        assistant_stream_text = ""
        # Everything the gateway has sent for this turn, including text the
        # ask-user buffer withheld. assistant_stream_text only tracks what was
        # FLUSHED, so it cannot be used to dedup a cumulative catch-all event
        # while a needs-input block is being held back (ELI-80).
        assistant_received_text = ""
        if adopted_run_text:
            # Continuing a run another reply started: its text baselines carry
            # over (see `park_cancelled_turn`), while what THIS reply shows,
            # `visible_message_text`, starts empty.
            assistant_received_text = adopted_run_text.get("received", "")
            assistant_stream_text = adopted_run_text.get("stream", "")
            last_item_text = adopted_run_text.get("item", "")
        # Offsets in `assistant_received_text` where events were lost (a
        # Gateway socket drop, see `_mark_event_gap`), and which OWUI reply
        # showed the run from which offset, so a lost chunk can be put back
        # into the reply it belongs to. Both are run-level and travel with an
        # adopted run.
        run_gaps: list[int] = list((adopted_run_text or {}).get("gaps", []))
        run_segments: list[dict] = list((adopted_run_text or {}).get("segments", []))
        if chat_id and owui_message_id:
            # The reply that showed the run until now has stopped streaming,
            # so OWUI has saved it; repairs that waited for it can go in.
            for seg in run_segments:
                for repair in _take_gap_repairs(chat_id, seg["message_id"]):
                    _schedule_gap_repair(conn, repair, streaming=False)
            run_segments.append({"message_id": owui_message_id,
                                 "start": len(assistant_received_text)})

        def reconcile_whole_text(whole: str) -> str | None:
            """After a gap, line the run's whole text up with what arrived
            and put each lost chunk back into the reply that showed it.
            Returns the text after the last received piece (the only new
            text), or None when there is no gap or `whole` does not line up
            (an item carrying one segment, not the whole run), which leaves
            the gaps pending for the next whole text."""
            if not run_gaps or not chat_id:
                return None
            aligned = _align_whole_text(whole, assistant_received_text, run_gaps)
            if aligned is None:
                return None
            tail, missing = aligned
            run_gaps.clear()
            for offset, chunk in missing:
                seg = next((sg for sg in reversed(run_segments) if sg["start"] <= offset),
                           run_segments[0] if run_segments else None)
                if seg is None:
                    continue
                nxt = next((sg["start"] for sg in run_segments if sg["start"] > seg["start"]),
                           len(assistant_received_text))
                repair = {
                    "user_id": owui_origin_user_id, "chat_id": chat_id,
                    "message_id": seg["message_id"], "chunk": chunk,
                    "before": assistant_received_text[offset:min(offset + _GAP_ANCHOR_CHARS, nxt)],
                    "after": assistant_received_text[max(seg["start"], offset - _GAP_ANCHOR_CHARS):offset],
                }
                pipe_log(f"  event gap: {len(chunk)} chars lost at offset {offset}; "
                         f"repairing message {seg['message_id'][:8]}...")
                _schedule_gap_repair(conn, repair,
                                     streaming=seg["message_id"] == owui_message_id)
            return tail
        visible_message_text = _initial_visible_text + pending_clamp_note
        had_tool_block = False
        # toolCallIds announced to OWUI as `function_call` items that have
        # not been completed by a matching result item yet. Anything left
        # here when the run ends would render as a spinner forever.
        pending_tool_calls: set[str] = set()
        # toolCallId of the `function_call` item yielded most recently, held
        # only while it is still the LAST item in OWUI's output list; anything
        # else we yield clears it. A failed call can only be relabeled (❌ in
        # the card name) while it is last, because the relabel event replaces
        # by index and we send none -- see `_tool_call_error_relabel_event`.
        relabelable_tool_call: str | None = None
        pending_prompt_text = ""
        pending_media_text = ""
        last_snapshot_text = ""
        last_snapshot_time = 0.0
        snapshot_interval_s = 1.0
        snapshot_min_delta_chars = 250
        # REST-API mid-run snapshot (bridge-rehydration-rest-snapshot-verify,
        # confirmed safe 2026-08-26): a parallel, throttled persistence path
        # for the plain assistant-text stream, which lost the `replace`-based
        # snapshot in 6e5f372 and cannot regain it that way -- see
        # REHYDRATION-PLAINTEXT-FIX-PROPOSAL.md for why every replace-based
        # variant of this (P17/P19/P23/P25/P27) is unsafe at any throttle.
        # This instead POSTs straight to the chat's DB row, which never
        # broadcasts to the live client, so it cannot race the delta stream.
        last_rest_snapshot_text = ""
        last_rest_snapshot_time = 0.0
        rest_snapshot_interval_s = 2.5
        rest_snapshot_task: asyncio.Task | None = None
        # OWUI 0.12+ discards this write and rehydrates reloads itself
        # (`_owui_owns_inflight_message`), so don't schedule it at all there.
        rest_snapshot_enabled = not _owui_owns_inflight_message()
        max_events_without_text = 5000
        queued_shown = False
        # An interrupted run is not continued here: its message goes to the
        # turn writer, whose settled row decides who continues it (DESIGN 2.7).
        handed_over = False

        async def recover_from_transcript() -> str | None:
            """The run's text read back: by its run id, else by its user text,
            which only a run seen here may use. With nothing of it seen, the
            session's last reply may be another run's (review 2af1 N4)."""
            try:
                hist = await conn.session_history(session_key, limit=RUN_TEXT_HISTORY_LIMIT)
                found, whole = _history_text_for_run(hist, our_run_id)
                if found and whole:
                    return whole
            except Exception as ex:
                pipe_log(f"  recovery by run id: chat.history failed: {ex!r}")
            if not first_event_arrived:
                pipe_log("  recovery: nothing of this run seen, no text by its id")
                return None
            return await _recover_turn_text(conn, session_key, text)

        async def settled_tail(row: dict, closer_only: bool = False) -> str | None:
            """What this reply still owes once its run settled with no final
            event here: the run's text after what arrived (its closing message
            in the transcript), or, for a run that showed nothing, why.
            `closer_only`: the session is busy, maybe with a later run, so
            only this run's own closer says it ended; None without one."""
            try:
                hist = await conn.session_history(session_key, limit=RUN_TEXT_HISTORY_LIMIT)
            except Exception as ex:
                pipe_log(f"  settled run: chat.history failed: {ex!r}")
                hist = None
            found, whole = _history_text_for_run(hist, our_run_id) if hist else (False, None)
            if found and whole:
                gap_tail = reconcile_whole_text(whole)
                if gap_tail is not None:
                    return gap_tail
                if whole.startswith(assistant_received_text):
                    return whole[len(assistant_received_text):]
                return "" if text_yielded else whole
            if closer_only:
                return "" if found else None
            if text_yielded:
                return ""
            status = row.get("status")
            if status in ("cancelled", "killed"):
                return "**Stopped.**"
            if status in ("failed", "timeout"):
                err = row.get("lastRunError")
                return f"**Error:** {err}" if err else "**Failed:** the run did not complete."
            return await recover_from_transcript() or ""

        def record_visible_chunk(chunk: str):
            nonlocal visible_message_text, relabelable_tool_call
            if chunk:
                visible_message_text += chunk
                # Text either extends OWUI's last output item or appends a new
                # `message` one (`middleware.py:4452`); either way the
                # `function_call` we were holding is no longer last, so an
                # index-less relabel would now overwrite this text instead.
                relabelable_tool_call = None

        async def maybe_emit_snapshot(*, force: bool = False):
            nonlocal last_snapshot_text, last_snapshot_time
            # ELI-82 instrumentation. A forced snapshot is the pipe's last
            # chance to persist trailing text, and it used to be completely
            # silent: on the 2026-08-01 incident the final 3417-char block
            # vanished and there was no way to tell whether this ever ran, let
            # alone with what. Only `force` calls are logged, so the mid-stream
            # cadence stays quiet. Log-only, no behaviour change.
            if force:
                pipe_log(
                    f"  snapshot(force): visible={len(visible_message_text)} "
                    f"last={len(last_snapshot_text)} "
                    f"delta={len(visible_message_text) - len(last_snapshot_text)}"
                )
            if not visible_message_text or visible_message_text == last_snapshot_text:
                if force:
                    pipe_log(
                        "  snapshot(force): SKIPPED "
                        + ("visible_message_text empty" if not visible_message_text
                           else "identical to last snapshot")
                    )
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
            if force:
                pipe_log(f"  snapshot(force): EMITTED {len(visible_message_text)} chars")

        def maybe_start_rest_snapshot():
            """Throttled gate for the REST-API mid-run snapshot.

            Fire-and-forget (background task): a GET+POST round trip inline
            in the streaming path would stall the live delta yield every
            time the throttle opens. Guarded so at most one is in flight at
            once, and stops firing once the turn hits terminal flush (`done`
            True) -- matching this project's established convention
            (19e9fd7/2092e19) of never doing anything new right at the
            natural end of a message, even though this path is confirmed
            safe from the replace-vs-delta race that motivated that
            convention (bridge-rehydration-rest-snapshot-verify, 2026-08-26).
            """
            nonlocal last_rest_snapshot_text, last_rest_snapshot_time, rest_snapshot_task
            if done or not owui_message_id or not rest_snapshot_enabled:
                return
            if not visible_message_text or visible_message_text == last_rest_snapshot_text:
                return
            now = time.time()
            if (
                last_rest_snapshot_text
                and now - last_rest_snapshot_time < rest_snapshot_interval_s
            ):
                return
            if rest_snapshot_task is not None and not rest_snapshot_task.done():
                return
            snapshot_text = visible_message_text
            last_rest_snapshot_text = snapshot_text
            last_rest_snapshot_time = now
            rest_snapshot_task = asyncio.create_task(
                self._rest_snapshot_message(
                    owui_origin_chat_id, owui_message_id, snapshot_text, bearer_token,
                )
            )

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
            # The answer continues this reply: its run id names it (ELI-100 2.2).
            idempotency_key = (pipe_run_id(chat_id, owui_message_id, "input") if owui_message_id
                               else f"user-input-{chat_id}-{time.time()}")
            # Listen before sending, as for the first send (R12): the answer
            # run's first events can arrive before chat.send returns, and with
            # no consumer and no target they would open a second writer on
            # this reply's message (review 2af1 N6).
            answer_queue = conn.register_consumer(session_key, idempotency_key)
            conn.continue_run_target(session_key, our_run_id, idempotency_key, shown=False)

            def unlisten():
                conn.unregister_consumer(session_key, idempotency_key, queue=answer_queue)
                conn.continue_run_target(session_key, idempotency_key, our_run_id, shown=False)
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
                unlisten()
                return UserInputResult(False, None)
            new_run_id = send_resp.get("runId") or None
            if new_run_id != idempotency_key:
                unlisten()  # steered into the live run, or renamed: as before
                answer_queue = None
            else:
                nonlocal admitted_at, signals_lost
                admitted_at, signals_lost = time.monotonic(), False
            await _emit_status(__event_emitter__, "Answer sent; continuing...", done=False)
            if new_run_id:
                pipe_log(f"  answer sent, new runId: {new_run_id[:20]}...")
            else:
                pipe_log("  answer sent, steering into active run (no new runId)")
            return UserInputResult(True, new_run_id, prompt_text=prompt_text, answer=answer,
                                   queue=answer_queue)

        def switch_to_answer_run(answer_res: UserInputResult) -> None:
            """The reply continues in the answer run (R9). Its target moved
            before the send when its consumer was registered then (N6); the
            question run was shown to its end either way."""
            if answer_res.queue is not None:
                conn.mark_delivered_live(session_key, our_run_id)
            else:
                conn.continue_run_target(session_key, our_run_id, answer_res.new_run_id)

        adopt_new_run = True
        try:
            while adopt_new_run:
                adopt_new_run = False
                try:
                    while not done:
                        # No silence timer: a quiet queue is a long tool call or
                        # model thinking as often as a dead run. The run's own
                        # signals end the reply: its terminal event, its settle
                        # row (below), a Gateway restart (handed over).
                        msg = await queue.get()

                        if msg.get("type") == _ROW_MARKER_TYPE:
                            row = msg.get("row") or {}
                            if row.get("status") == "queued" and not first_event_arrived \
                                    and not queued_shown:
                                queued_shown = True
                                await _emit_status(
                                    __event_emitter__,
                                    "Waiting -- your message is queued behind an "
                                    "active response...",
                                    done=False,
                                )
                            # Read after this run was admitted, an idle session
                            # means it is over, whichever run settled last. That
                            # is the end signal once its own may have been lost
                            # in a gap (review 2af1 N3). A row read before the
                            # admission shows nothing about it (N4).
                            admitted = msg.get("read_at", time.monotonic()) >= admitted_at
                            if admitted and msg.get("resync"):
                                signals_lost = True
                            tail = None
                            if row.get("hasActiveRun") is False and (
                                    row.get("lastRunId") == our_run_id
                                    or (admitted and signals_lost)):
                                # The run settled while its terminal event never
                                # reached this reply (dropped frame, socket gap).
                                # Its transcript has what was lost.
                                pipe_log(f"  run settled (row status {row.get('status')}, "
                                         f"last run {str(row.get('lastRunId'))[:24]}) without its final")
                                tail = await settled_tail(row)
                            elif row.get("hasActiveRun") and msg.get("resync") and admitted:
                                # After a gap the session is busy, maybe with a
                                # later run: this run's closer says it ended.
                                tail = await settled_tail(row, closer_only=True)
                                if tail is not None:
                                    pipe_log("  run closed in the transcript during a gap")
                            if tail is not None:
                                if tail:
                                    record_visible_chunk(tail)
                                    yield tail
                                    text_yielded = True
                                    await maybe_emit_snapshot(force=True)
                                done = True
                            continue

                        if msg.get("type") == _RESTART_MARKER_TYPE:
                            # A new Gateway process: the run may be recovered
                            # by it (a crash leaves its row `running`) or need
                            # a resume. Its row decides, in the turn writer.
                            if conn.hand_over_run(session_key, our_run_id,
                                                  thinking=resolved_thinking, read_row=True):
                                handed_over = done = True
                            else:
                                pipe_log("  Gateway restarted; no message to hand over, waiting")
                            continue

                        if msg.get("type") == _GAP_MARKER_TYPE:
                            signals_lost = True
                            run_gaps.append(len(assistant_received_text))
                            pipe_log(f"  event gap at offset {len(assistant_received_text)} "
                                     "(Gateway socket dropped)")
                            continue

                        event_count += 1
                        first_event_arrived = True
                        payload = msg.get("payload", {})
                        stream = payload.get("stream")
                        data = payload.get("data", {})
                        name = data.get("name", "")
                        phase = data.get("phase", "")
                        state = payload.get("state", "")
                        if (event_count == 1 and state == "final" and msg.get("event") == "chat"
                                and not payload.get("message")):
                            enqueued_not_started = True

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
                        err = (str(data.get("error", "unknown"))
                               if stream == "lifecycle" and phase == "error" else "")
                        aborted_run = state == "aborted" or data.get("aborted") is True
                        # DESIGN 2.7: an outside kill of the CLI, or an abort
                        # after a shutdown notice, may be continued by the
                        # Gateway (a `running` row) or by a resume. The reply
                        # decides neither: the turn writer takes the message
                        # and the settled row picks the owner. Without a
                        # message to hand over, it ends here like any error.
                        if ((_EXTERNAL_KILL_RE.search(err) or (aborted_run and conn._shutdown_seen))
                                and conn.hand_over_run(session_key, our_run_id,
                                                       thinking=resolved_thinking)):
                            handed_over = done = True
                            continue
                        if stream == "lifecycle" and phase == "end":
                            done = True
                            pipe_log("  lifecycle end -> done")
                        elif err:
                            yield f"\n\n**Error:** {err}"
                            done = True
                            pipe_log("  lifecycle error -> done")
                        if state in ("final", "cancelled", "error", "aborted") or aborted_run:
                            done = True
                            pipe_log(f"  payload state='{state}' aborted={aborted_run} -> done")

                        # --- Assistant text stream ---
                        if stream == "assistant":
                            raw_delta = data.get("delta")
                            if raw_delta:
                                delta = raw_delta
                                assistant_received_text += raw_delta
                            else:
                                # Some providers (observed with claude-cli-backed
                                # Opus/Sonnet overrides) send a final catch-all event
                                # with no `delta` but a `text` field holding the full
                                # cumulative reply rather than a fresh chunk. Treating
                                # it as always-new re-sent the whole message a second
                                # time. Diff it against what's already been streamed,
                                # same as the item-event dedup below.
                                raw_text = data.get("text") or ""
                                gap_tail = reconcile_whole_text(raw_text) if raw_text else None
                                before = _item_delta_text(
                                    raw_text, "", assistant_received_text
                                ) if raw_text else ""
                                delta = (
                                    _suppress_already_shown(gap_tail, visible_message_text)
                                    if gap_tail is not None
                                    else _catch_all_delta(
                                        raw_text, assistant_received_text, visible_message_text
                                    )
                                )
                                if before and not delta:
                                    pipe_log(
                                        "  suppressed duplicate catch-all assistant "
                                        f"text ({len(before)} chars already received)"
                                    )
                                assistant_received_text += delta
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
                                # matches -- only the buffered, accumulated text can
                                # be checked reliably. Resolved once the run ends
                                # (see the `else:` clause below) or as soon as the
                                # buffered text diverges from every trigger.
                                flush_text, pending_prompt_text = (
                                    _advance_input_prompt_buffer(pending_prompt_text, delta)
                                )
                                if not flush_text:
                                    continue
                                delta = flush_text

                                # Hold back a MEDIA: directive until its filename is
                                # confirmed complete (terminated by whitespace) -- a
                                # real streaming provider can split "MEDIA:filename.png"
                                # across multiple deltas at any point, including
                                # mid-filename or mid-prefix. See _advance_media_buffer.
                                delta, pending_media_text = _advance_media_buffer(
                                    pending_media_text, delta
                                )
                                if not delta:
                                    continue

                                text_yielded = True
                                # No snapshot here, regardless of whether tool blocks
                                # exist earlier in the message. A `replace` event sets
                                # the full message content; if it lands on (or near)
                                # the complete final text right before the generator
                                # naturally finishes, OWUI double-saves -- the
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
                                # definition, since the block was just added) -- see
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
                                        # A writer that takes this reply over
                                        # shows the same image (review 2af1 R4).
                                        conn.note_turn_rewrite(owui_origin_chat_id, owui_message_id,
                                                               delta, resolved)
                                        record_visible_chunk(resolved)
                                        yield resolved
                                        await maybe_emit_snapshot()
                                        continue
                                assistant_stream_text += delta
                                record_visible_chunk(delta)
                                yield delta
                                # NOTE: do NOT emit a `replace` snapshot per text
                                # delta here. A mid-stream `replace` carrying the
                                # full accumulated text makes OWUI's own
                                # `_suppress_already_shown` treat the *continuing*
                                # delta stream as already-shown and suppress it --
                                # the visible stream freezes mid-message (regression
                                # 2026-07-18, exactly the P23/P25 hazard). Mid-run
                                # DB back-sync for this path goes through the REST
                                # snapshot gate instead (`maybe_start_rest_snapshot`,
                                # bridge-rehydration-rest-snapshot-verify), which
                                # writes the chat row directly and never broadcasts
                                # to the live client, so it cannot trigger that
                                # suppression.
                                maybe_start_rest_snapshot()

                        # --- Assistant text carried by item/preamble events ---
                        if stream == "item":
                            item_text = _item_assistant_text(data)
                            if item_text:
                                answer_res = await maybe_answer_user_input(item_text)
                                if answer_res.handled:
                                    detail_block = ""
                                    if answer_res.answer:
                                        detail_block = _ask_user_detail_block(
                                            answer_res.prompt_text or item_text, answer_res.answer
                                        )
                                        record_visible_chunk(detail_block)
                                        yield detail_block
                                        await maybe_emit_snapshot(force=True)
                                    conn.note_turn_rewrite(owui_origin_chat_id, owui_message_id,
                                                           answer_res.prompt_text or item_text,
                                                           detail_block)
                                    if answer_res.new_run_id and answer_res.new_run_id != our_run_id:
                                        pipe_log(f"  needs-input handled, switching to run {answer_res.new_run_id[:20]}...")
                                        # The answer run continues this reply (R9).
                                        switch_to_answer_run(answer_res)
                                        conn.unregister_consumer(session_key, our_run_id, queue=queue)
                                        queue = answer_res.queue or conn.register_consumer(
                                            session_key, answer_res.new_run_id)
                                        our_run_id = answer_res.new_run_id
                                    done = False
                                    last_item_text = item_text
                                    continue
                                gap_tail = reconcile_whole_text(item_text)
                                item_delta = gap_tail if gap_tail is not None else _item_delta_text(
                                    item_text, last_item_text, assistant_stream_text
                                )
                                last_item_text = item_text
                                # Same safety net as the assistant-stream catch-all
                                # above, and for the same reason: `last_item_text`/
                                # `assistant_stream_text` can drift from
                                # `visible_message_text`, and this call site has the
                                # exact same `_item_delta_text` fallback that
                                # re-yields the entire text when the prefix check
                                # fails (P27).
                                item_delta = _suppress_already_shown(item_delta, visible_message_text)
                                # Unlike the assistant-stream path, item events
                                # carry no `_advance_input_prompt_buffer` guard,
                                # so a needs-input prompt that arrives here (and
                                # wasn't handled into a modal above) would leak
                                # its raw "OpenClaw needs input:" marker line.
                                # Strip it defensively.
                                if _is_user_input_prompt(item_delta):
                                    stripped = _strip_input_marker_line(item_delta)
                                    conn.note_turn_rewrite(owui_origin_chat_id, owui_message_id,
                                                           item_delta, stripped)
                                    item_delta = stripped
                                if item_delta:
                                    text_yielded = True
                                    pipe_log("  yielded text from item event")
                                    record_visible_chunk(item_delta)
                                    yield item_delta

                        # --- Tool call events ---
                        if stream == "tool":
                            if phase == "start":
                                tool_call_id = data.get("toolCallId", "")
                                args = json.dumps(data.get("args", {}))
                                if tool_call_id:
                                    self._active_tool_args[tool_call_id] = args
                                pipe_log(f"  Tool start: {name}")
                                if tool_call_id:
                                    # Protocol switch, not text: this dict becomes a
                                    # `function_call` item in OWUI's own output list,
                                    # which renders as a live spinner until its
                                    # matching result item arrives. Deliberately NOT
                                    # recorded into `visible_message_text` -- it is
                                    # not part of the message text, and the snapshot
                                    # path must not duplicate it as markdown.
                                    pending_tool_calls.add(tool_call_id)
                                    yield _tool_call_started_event(name, tool_call_id, args)
                                    # Now the last item in OWUI's output list,
                                    # so a relabel on failure can target it
                                    # without an output_index. A second start
                                    # (parallel calls) displaces the first,
                                    # which then falls back to the banner.
                                    relabelable_tool_call = tool_call_id
                                await _emit_status(
                                    __event_emitter__,
                                    f"Running {name}...",
                                    done=False,
                                )

                            elif phase == "result":
                                result = data.get("result", {})
                                result_str = json.dumps(result) if not isinstance(result, str) else result
                                tool_call_id = data.get("toolCallId", "")
                                # The Gateway already tells us the outcome
                                # (`execute.runtime`'s emitCliToolResult); the
                                # card just never showed it.
                                is_error = data.get("isError") is True
                                stored_args = self._active_tool_args.pop(tool_call_id, None)
                                args_str = stored_args or json.dumps(data.get("args", {}))
                                if (
                                    adopted_run_id and tool_call_id
                                    and tool_call_id not in pending_tool_calls
                                ):
                                    # Started under the reply that was
                                    # interrupted, so this reply has no card
                                    # for the result to complete: open one.
                                    pending_tool_calls.add(tool_call_id)
                                    yield _tool_call_started_event(name, tool_call_id, args_str)
                                    relabelable_tool_call = tool_call_id
                                pipe_log(
                                    f"  Tool result: {name} ({len(result_str)} chars)"
                                    f"{' [error]' if is_error else ''}"
                                )
                                if is_error:
                                    # Log the gate decision, always. Without it a
                                    # skipped relabel is invisible, which is
                                    # indistinguishable from the error branch never
                                    # running at all. That ambiguity is exactly why
                                    # ELI-75 read as a missing feature for months
                                    # while the branch was in fact running and only
                                    # the relabel was being skipped.
                                    pipe_log(
                                        f"  relabel gate: call={tool_call_id!r} "
                                        f"held={relabelable_tool_call!r} "
                                        + ("FIRE" if tool_call_id
                                           and relabelable_tool_call == tool_call_id
                                           else "SKIP")
                                    )
                                    if tool_call_id and relabelable_tool_call == tool_call_id:
                                        # Mutates the started item's `name`
                                        # field in place via OWUI's generic
                                        # "<field>.done" handler: the
                                        # collapsed row becomes "View Result
                                        # from **<name> ❌**", and OWUI
                                        # persists it, so the mark survives a
                                        # reload.
                                        yield _tool_call_error_relabel_event(name)
                                    result_str = _tool_error_banner(result_str)
                                relabelable_tool_call = None
                                if tool_call_id:
                                    # Completes the item yielded at start: the shared
                                    # call_id is what flips that card to done and
                                    # reveals its Output. No snapshot needed -- the
                                    # backend folds this into the output list it
                                    # already persists and re-emits.
                                    pending_tool_calls.discard(tool_call_id)
                                    yield _tool_call_result_event(tool_call_id, result_str)
                                else:
                                    # No toolCallId means there is nothing to pair a
                                    # result item to, so fall back to the markdown
                                    # card -- the same one the shadow renderer emits.
                                    tool_block = _render_tool_result_block(
                                        name, tool_call_id, args_str, result_str,
                                        data.get("meta", ""),
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

                        # --- Item events (progress) ---
                        if stream == "item":
                            pipe_log(f"  Item: kind={data.get('kind','')} "
                                     f"status={data.get('status','')} "
                                     f"title={str(data.get('title',''))[:50]}")

                        if not text_yielded and event_count >= max_events_without_text:
                            recovered = await recover_from_transcript()
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

                except (asyncio.CancelledError, GeneratorExit) as stop:
                    # OWUI ended this response: the Stop button, or Send now /
                    # interrupt, which stop it the same way and then send a
                    # new message. It arrives as a task cancel when we are
                    # awaiting, or as a generator close when we are suspended
                    # at a yield (OWUI busy with the last chunk), which is
                    # most of the time while text streams. Handling only the
                    # cancel left the close path with no abort, no handover
                    # and the reply's own unsent message lost (2026-10-11).
                    #
                    # Park first, synchronously: the run keeps going with its
                    # events buffered, and the next message takes it over
                    # (see `park_cancelled_turn`). The awaits after it may be
                    # interrupted themselves; nothing depends on them.
                    aborted = True
                    grace_s = float(getattr(self.valves, "INTERRUPT_GRACE_SECONDS", 0) or 0)
                    turn.parked = True
                    conn.park_cancelled_turn(
                        session_key, our_run_id, queue, turn.unsent, grace_s,
                        send_stop=self.valves.SEND_STOP_ON_CANCEL,
                        run_text={"received": assistant_received_text,
                                  "stream": assistant_stream_text,
                                  "item": last_item_text,
                                  "gaps": run_gaps,
                                  "segments": run_segments},
                    )
                    pipe_log(
                        f"Generator {type(stop).__name__}: run parked "
                        f"{grace_s:g}s with {len(turn.unsent)} unsent "
                        "message(s), in case a new message follows"
                        if grace_s > 0 else
                        f"Generator {type(stop).__name__}: sending chat.abort"
                    )
                    try:
                        await _settle_cancel_intent(
                            conn, session_key, our_run_id,
                            owui_origin_chat_id, __event_call__)
                        await maybe_emit_snapshot(force=True)
                        await _emit_status(__event_emitter__, "Stopped", done=True)
                    except (Exception, asyncio.CancelledError) as ex:
                        pipe_log(f"  stop snapshot/status skipped: {ex!r}")
                    raise

                else:
                    # Normal completion (no cancellation).
                    if pending_prompt_text:
                        # The buffered plain-text tail either fully resolved into a
                        # needs-input trigger (see `_could_be_user_input_prefix`
                        # above -- buffering only continues past the ambiguous
                        # prefix stage while it keeps matching) or the run ended
                        # mid-buffer without ever diverging. Either way, resolve it
                        # now that the full text is available. `maybe_answer_user_input`
                        # re-checks the trigger itself and returns False for a
                        # partial/non-match, in which case fall back to showing it
                        # as plain text so nothing is silently dropped.
                        answer_res = await maybe_answer_user_input(pending_prompt_text)
                        if answer_res.handled:
                            # Answer delivered. Re-enter the main loop (via the outer
                            # `while adopt_new_run`) to consume the follow-up -- whether
                            # the run resumed in place (steer) or a new run was spawned
                            # -- so the continuation reuses the same needs-input
                            # re-detection, dedup and snapshot handling as any other
                            # run instead of a separate, drift-prone copy of the loop.
                            text_yielded = True
                            detail_block = ""
                            if answer_res.answer:
                                detail_block = _ask_user_detail_block(
                                    answer_res.prompt_text or pending_prompt_text, answer_res.answer
                                )
                                record_visible_chunk(detail_block)
                                yield detail_block
                                await maybe_emit_snapshot(force=True)
                            conn.note_turn_rewrite(owui_origin_chat_id, owui_message_id,
                                                   pending_prompt_text, detail_block)
                            pending_prompt_text = ""
                            if answer_res.new_run_id and answer_res.new_run_id != our_run_id:
                                pipe_log(f"  continuing into new run: {answer_res.new_run_id[:20]}...")
                                # The answer run continues this reply (R9).
                                switch_to_answer_run(answer_res)
                                conn.unregister_consumer(session_key, our_run_id, queue=queue)
                                queue = answer_res.queue or conn.register_consumer(
                                    session_key, answer_res.new_run_id)
                                our_run_id = answer_res.new_run_id
                            else:
                                pipe_log("  continuing in the same run after modal answer")
                            done = False
                            adopt_new_run = True
                        else:
                            # Modal not handled (cancelled / dismissed / gave up
                            # on reconnect). Show the prompt as a fallback, but
                            # never leak the literal "OpenClaw needs input:"
                            # directive line as visible text.
                            fallback = _strip_input_marker_line(pending_prompt_text)
                            conn.note_turn_rewrite(owui_origin_chat_id, owui_message_id,
                                                   pending_prompt_text, fallback)
                            if fallback:
                                record_visible_chunk(fallback)
                                yield fallback
                                text_yielded = True
                            pending_prompt_text = ""

                    if pending_media_text:
                        # The run ended with a MEDIA: directive still buffered --
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
                        if handled:
                            conn.note_turn_rewrite(owui_origin_chat_id, owui_message_id,
                                                   pending_media_text, resolved)
                        record_visible_chunk(resolved)
                        yield resolved
                        text_yielded = True
                        pending_media_text = ""

                    # For pure-text turns, do NOT force a snapshot here: OWUI's own
                    # accumulation of the streamed yields already lands the full
                    # text in `content`, and an extra forced `replace` at this
                    # point double-writes it (P23/P25 -- exact duplicate, no
                    # separator).
                    #
                    # Turns that included a tool-call block are different (P27,
                    # 2026-07-08): once a `<details type="tool_calls">` block has
                    # been yielded, OWUI's own end-of-stream save no longer reliably
                    # lands the plain-text tail in `content` at all -- it only shows
                    # up in `output` instead. So for tool-block turns only, force
                    # one last `content` snapshot here.
                    #
                    # This was briefly reverted the same day after what looked like
                    # a live duplicate regression, but that turned out to be a false
                    # alarm caused by redeploying the pipe mid-message in the same
                    # live chat used to test it (a self-inflicted artifact of the
                    # testing method, not this code) -- confirmed by reproducing the
                    # duplicate-free "output has it once, content misses it"
                    # signature again on an unrelated turn where nothing was
                    # redeployed mid-stream. Restored. Lesson for future sessions:
                    # never redeploy this pipe function while a message in the same
                    # live chat being used to verify it is still streaming.
                    #
                    # Skip while re-entering for a modal follow-up: the snapshot must
                    # only land once the run is truly finished, not between turns.
                    # Not for an adopted run either: this reply keeps streaming
                    # the queued message's answer, and a `replace` now would
                    # freeze those deltas (P23/P25).
                    if (not adopt_new_run and had_tool_block and not adopted_run_id
                            and not handed_over):
                        await maybe_emit_snapshot(force=True)

                    # Resolve any tool call that was announced but never got a
                    # result (run cancelled, gateway dropped it, tool crashed).
                    # Its card is a spinner until something completes it, and
                    # OWUI's own "mark leftover in_progress items completed"
                    # sweep does NOT reliably reach this path -- observed
                    # 2026-07-25, where a delivered call stayed `in_progress`
                    # in the persisted item and only rendered as done because
                    # its result item happened to exist. So close them here
                    # rather than relying on that.
                    #
                    # A handed-over run is not over (its writer completes the
                    # cards from the renderer it was handed), but this
                    # request's output must still close them: Open WebUI's
                    # post-stream sweep executes every `function_call` of the
                    # stream that has no `function_call_output`, gets `Error:
                    # Tool "Bash" not found.` and sends that back as a new
                    # turn, whose stream then overwrites the writer's reply
                    # (M3 crash window, 2026-10-13). Only the yielded items are
                    # closed; the handed renderer keeps the call open.
                    if not adopt_new_run and pending_tool_calls:
                        note = ("(interrupted -- the reply continues when the run resumes)"
                                if handed_over else "(no result -- the run ended first)")
                        for orphan_id in list(pending_tool_calls):
                            pipe_log(f"  closing orphaned tool call {orphan_id}")
                            yield _tool_call_result_event(orphan_id, note)
                        pending_tool_calls.clear()

        finally:
            # A parked queue now belongs to whoever claims the run; releasing
            # it here would drop the events it is buffering.
            if not turn.parked:
                if done and not aborted and our_run_id and not handed_over:
                    # The run ended in this reply. Its trailing `chat` final
                    # lands after this consumer is gone and must not read as
                    # "the reply ended before its run" (a handover would
                    # rewrite the message OWUI is about to save) or as a run
                    # nobody showed.
                    conn.mark_delivered_live(session_key, our_run_id)
                    conn.unregister_run_target(session_key, our_run_id)
                conn.unregister_consumer(session_key, our_run_id, queue=queue)
                turn.run_id = None
                turn.queue = None
            status_lines = []
            if not aborted and text_yielded and not adopted_run_id and not handed_over:
                status_lines = await _build_usage_status_lines(conn, session_key)
            if adopted_run_id and not aborted:
                await _emit_status(
                    __event_emitter__, "Sending your message…", done=False)
            elif status_lines:
                # One `status` event per fact (context / rate-limit / goal) so
                # each row stays inside OWUI's per-line clamp instead of one
                # long combined line getting cut off. All of them already
                # reflect final data by the time we get here (both RPCs in
                # `_build_usage_status_lines` already completed) -- none of
                # this is genuinely still "in progress", so every line is
                # `done=True`. Marking earlier ones `done=False` made them
                # shimmer for an instant before getting replaced by the last
                # line, a visible flash for no reason.
                for line in status_lines:
                    await _emit_status(__event_emitter__, line, done=True)
            else:
                await _emit_status(__event_emitter__, "", done=True)
            self._current_session_key = None
            self._current_run_id = None

        pipe_log(f"DONE -- {event_count} events processed, "
                 f"text yielded: {text_yielded}")
        if adopted_run_id and not aborted:
            # The adopted run is over; now send the messages this reply owes
            # (its own, plus any carried over from cancelled replies), in the
            # same reply. The recursive call does the normal queue + send (it
            # also waits out a subagent-yield gap if the run resumes), and
            # owns the terminal snapshot, usage lines and auto-title. Seeding
            # it with what streamed so far keeps that snapshot complete.
            send_texts = list(turn.unsent)
            pipe_log(
                f"  adopted run {adopted_run_id[:20]}... finished; sending "
                f"{len(send_texts)} queued message(s)"
            )
            separator = "\n\n---\n\n" if visible_message_text.strip() else ""
            if separator:
                record_visible_chunk(separator)
                yield separator
            # From here the recursive reply owns them, and parks them itself
            # if it is cancelled.
            turn.unsent = []
            async with contextlib.aclosing(self._pipe_impl(
                body, __event_emitter__,
                __event_call__=__event_call__, __user__=__user__,
                __metadata__=__metadata__, __request__=__request__,
                __task__=__task__, __task_body__=__task_body__,
                _initial_visible_text=visible_message_text,
                _send_texts=send_texts,
            )) as continuation:
                async for item in continuation:
                    yield item
            return
        if (enqueued_not_started and not text_yielded and not aborted
                and not adopted_run_id and session_runtime == "claude-cli"
                and our_run_id and sent_texts):
            # A run started between the idle read and the send (a sub-agent's
            # parent resuming, a wake), and the Gateway queued this message
            # behind it. claude-cli cannot run a queued turn: it fails with
            # "Reply operation has no active tool authority snapshot" once the
            # active run ends (the followup path binds no tool authority,
            # ELI-87), so the message would be lost. Take it back while it is
            # queued, wait for that run to settle, and send it again.
            try:
                await conn.send_request(
                    "chat.abort", {"sessionKey": session_key, "runId": our_run_id},
                    timeout=15,
                )
                took_back = True
            except Exception as ex:
                took_back = False
                pipe_log(f"  could not take back queued message: {ex}")
            if took_back:
                pipe_log(
                    f"  message was queued behind a run that started first "
                    f"(run {str(our_run_id)[:20]}...); took it back, resending once idle"
                )
                conn.unregister_run_target(session_key, our_run_id)
                async with contextlib.aclosing(self._pipe_impl(
                    body, __event_emitter__,
                    __event_call__=__event_call__, __user__=__user__,
                    __metadata__=__metadata__, __request__=__request__,
                    __task__=__task__, __task_body__=__task_body__,
                    _initial_visible_text=visible_message_text,
                    _send_texts=sent_texts,
                )) as again:
                    async for item in again:
                        yield item
                return

        if not text_yielded:
            # DIAG (ELI-56): a no-text turn is the phantom. Emit the correlation
            # keys so it can be matched to a duplicate/re-fired completion above
            # (same text_sha / owui_msg_id) and to the run that actually carried
            # the answer (which then leaks out as a separate message).
            pipe_log(
                f"  [diag] PHANTOM (no text): our_run_id={str(our_run_id)[:40]} "
                f"text_sha={getattr(self, '_diag_text_sha', None)} "
                f"first_event_arrived={first_event_arrived} aborted={aborted}"
            )

        if run_gaps and not aborted and our_run_id:
            # Events were lost and no whole-text event lined up with them
            # during the run: the run's own closing message in the transcript
            # (`cli-assistant:<runId>`, see `_history_text_for_run`) is the
            # whole text. Its tail, if the lost events were the last ones,
            # belongs at the end of this reply.
            try:
                hist = await conn.session_history(session_key, limit=RUN_TEXT_HISTORY_LIMIT)
            except Exception as ex:
                hist = None
                pipe_log(f"  event gap: chat.history failed: {ex}")
            found, whole = _history_text_for_run(hist, our_run_id)
            tail = (reconcile_whole_text(whole) if found and whole else None) or ""
            if run_gaps:
                pipe_log(f"  {len(run_gaps)} event gap(s) and no whole text lines up for run "
                         f"{str(our_run_id)[:20]}...; the lost text cannot be put back")
            elif tail and not visible_message_text.endswith(tail):
                pipe_log(f"  event gap: {len(tail)} chars lost at the end of the run, appended")
                record_visible_chunk(tail)
                yield tail
                text_yielded = True

        # Terminal back-sync (ELI-57): persist the completed turn's visible text
        # to the OWUI DB with one final `replace` snapshot. OWUI only saves
        # yielded content when the BROWSER writes it back after completion -- if
        # the tab/socket died mid-stream, the SSE yields land nowhere, raise
        # nothing, and the message stays empty in the DB forever (reload shows
        # nothing). Tool turns already snapshot along the way; pure-text turns
        # snapshot nowhere since the per-delta back-sync was reverted (ffd2fdd --
        # a mid-stream `replace` makes the frontend suppress the CONTINUING
        # delta stream, P23/P25). At terminal there are no continuing deltas,
        # so a single force snapshot is freeze-safe, and OWUI's `replace`
        # handler writes the DB unconditionally (sio.emit to an empty room is a
        # no-op, not an error), so it persists even with everything dead.
        # ELI-82 instrumentation: record the terminal decision inputs, not just
        # its effect. `text_yielded` gating this call means a turn can end with
        # a populated visible_message_text and still never snapshot, which is
        # indistinguishable from "snapshotted but OWUI dropped it" unless the
        # inputs are on the record.
        pipe_log(
            f"  terminal: aborted={aborted} text_yielded={text_yielded} "
            f"visible={len(visible_message_text)} snapshotted={len(last_snapshot_text)}"
        )
        if not aborted and text_yielded:
            await maybe_emit_snapshot(force=True)

        # Auto-title: generate title after first exchange (best-effort, non-blocking)
        if not aborted and text_yielded:
            spawn_background(
                self._auto_title(
                    body, conn, visible_message_text,
                    owui_origin_chat_id, bearer_token,
                )
            )

        if not aborted and not text_yielded:
            recovered = await recover_from_transcript()
            retry_body = _thinking_rejection_retry_body(
                body, recovered, resolved_thinking)
            if retry_body is not None:
                # Learn from the rejection before retrying. The Gateway just
                # named this model's real ladder, and it is the only place that
                # truth is stated -- describe cannot supply it. Recording it
                # here is what stops the next turn repeating this round trip.
                rejection = parse_thinking_rejection(recovered)
                learned = None
                if rejection:
                    learned = rejection["levels"]
                    _record_thinking_ladder(
                        learned,
                        model_key=rejection["model"] or session_model_key,
                        authoritative=True,
                        state_dir=getattr(self.valves, "STATE_DIR", ""),
                    )
                pipe_log(
                    f"thinking: Gateway rejected level {resolved_thinking!r} "
                    f"({recovered!r}); retrying once without an explicit level"
                )
                # Say only what was actually observed. The previous wording
                # asserted a cause it never checked ("this chat hadn't talked
                # to it before"), which was false on every turn after the
                # first and sent anyone reading it hunting the wrong bug.
                if learned:
                    supported = ", ".join(repr(lv) for lv in learned)
                    retry_note = (
                        f"_This model only supports thinking level "
                        f"{supported}, so {resolved_thinking!r} was dropped "
                        f"for this message. Remembered for next time._\n\n"
                    )
                else:
                    retry_note = (
                        f"_This model does not support thinking level "
                        f"{resolved_thinking!r}; sending without it instead._"
                        f"\n\n"
                    )
                record_visible_chunk(retry_note)
                yield retry_note
                async with contextlib.aclosing(self._pipe_impl(
                    retry_body, __event_emitter__,
                    __event_call__=__event_call__, __user__=__user__,
                    __metadata__=__metadata__, __request__=__request__,
                    __task__=__task__, __task_body__=__task_body__,
                    _initial_visible_text=visible_message_text,
                    _send_texts=sent_texts,
                )) as retry:
                    async for item in retry:
                        yield item
                return
            if recovered:
                pipe_log("  recovered assistant text at final fallback")
                record_visible_chunk(recovered)
                yield recovered
                await maybe_emit_snapshot(force=True)
            else:
                # No assistant text and preview recovery came up empty. Surface
                # the run's REAL terminal state (from the gateway) instead of a
                # scary generic "missing output event" placeholder.
                reason = None
                try:
                    desc = await conn.send_request(
                        "sessions.describe", dict(key=session_key), timeout=8)
                    row = desc.get("session") or {}
                    status = row.get("status")
                    if status == "failed":
                        reason = "**The response failed.** Please try again."
                    elif status == "cancelled":
                        reason = "**Stopped.**"
                except Exception as ex:
                    pipe_log(f"  terminal-reason describe failed: {ex}")
                no_response_text = "\n\n" + (
                    reason or "_(This turn produced no reply text.)_"
                )
                record_visible_chunk(no_response_text)
                yield no_response_text
                await maybe_emit_snapshot(force=True)
