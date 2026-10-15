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


# After a Send now the page sends its follow-up message, which claims the run
# at once (`claim_cancelled_turn`). This bounds only a follow-up that never
# comes: the tab was closed, crashed or reloaded in between. Nothing signals
# that: the Gateway knows nothing of the browser, and Open WebUI's socket
# disconnect is not it either, since a socket drop reconnects with a new sid
# while the page and its queued message survive (ELI-95).
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
        OWNER_USERS: str = Field(
            default="",
            description="Open WebUI user ids or emails, comma-separated, whose "
                "chats act with full owner rights on the Gateway. Leave empty "
                "and every user acts as the owner (the old behaviour). When set, "
                "everyone else talks to the Gateway over a separate connection "
                "with read and write access only, so the agent withholds "
                "owner-only tools (automations, outbound messaging, sessions "
                "admin, terminal) from their turns. Use it whenever someone "
                "other than the Gateway's owner can sign in to Open WebUI."
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
        # A bound, not a wait: a client that cannot say Stop or Send now (no
        # page hook, API use) sends no signal at all (ELI-95).
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
                          owui_origin_chat_id, bearer_token, session_key=""):
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
            # The title run carries this chat's text, so it goes out with the
            # chat's owner status (OWNER_USERS), not the owner's.
            owner = await _sender_is_owner(conn, session_key)
            title = await self._generate_title_text(conn, user_text, assistant_preview, owner=owner)
            if not title:
                return
            await self._set_owui_chat_title(owui_origin_chat_id, title, bearer_token)
            pipe_log(f"Auto-title: set to '{title}'")
        except Exception as e:
            pipe_log(f"Auto-title: failed (non-fatal): {e}")

    async def _generate_title_text(self, conn, user_msg: str,
                                    assistant_msg: str, owner: bool = True) -> str | None:
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
                resp = await _run_request(
                    conn, RUN_SEND,
                    dict(sessionKey=title_session, message=prompt, idempotencyKey=run_id),
                    timeout=15, owner=owner,
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

    async def pipe(self, body, __event_emitter__, __event_call__=None,
                   __user__=None, __metadata__=None, __request__=None,
                   __task__=None, __task_body__=None):
        """Main pipe entry point -- called by Open WebUI for each user message.

        Thin wrapper around _pipe_impl so dev-only deploy-coordination hooks
        (ELI-24) can bracket every turn without re-indenting the whole body.
        """
        remember_owui_server()
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
                    settle=functools.partial(
                        _settle_cancel_intent, turn.conn, turn.session_key,
                        turn.run_id, turn.chat_id, __event_call__),
                )
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
        state = _PipeTurn(
            self, body, __event_emitter__, __event_call__, __user__, __metadata__,
            __request__, __task__, __task_body__, _initial_visible_text, _send_texts, turn)
        async with contextlib.aclosing(state.run()) as items:
            async for item in items:
                yield item


class _PipeTurn:
    """One reply of `Pipe._pipe_turn`, in four phases run in order (ELI-147):
    `prepare`, `admit`, `consume` and `finish`. A phase that ends the reply
    early sets `ended`. The closures the single function had are methods,
    and the locals they or two phases shared are attributes."""

    def __init__(self, pipe, body, __event_emitter__, __event_call__, __user__,
                 __metadata__, __request__, __task__, __task_body__,
                 _initial_visible_text, _send_texts, turn):
        self.pipe = pipe
        self.body = body
        self.__event_emitter__ = __event_emitter__
        self.__event_call__ = __event_call__
        self.__user__ = __user__
        self.__metadata__ = __metadata__
        self.__request__ = __request__
        self.__task__ = __task__
        self.__task_body__ = __task_body__
        self._initial_visible_text = _initial_visible_text
        self._send_texts = _send_texts
        self.turn = turn
        self.ended = False

    async def run(self):
        for phase in (self.prepare, self.admit, self.consume, self.finish):
            async with contextlib.aclosing(phase()) as items:
                async for item in items:
                    yield item
            if self.ended:
                return

    async def prepare(self):
        """The message, the connection, the session key, a cancelled reply's
        run to take over, and the model patch."""
        if self.pipe.valves.ENABLE_FILE_SERVER:
            _start_file_server()
        preset = self.pipe._selected_preset(self.body)
        self.model_override = self.pipe._model_override_for_preset(preset)

        # Per-chat thinking level (ELI-85). `reasoning_effort` is the single
        # source of truth: Open WebUI's own Advanced Params control writes it,
        # and so does the companion Thinking filter's toggle. Reading the field
        # rather than a filter-specific one means either control works, and the
        # bridge keeps working for anyone who never installs the filter.
        # Absent means absent: no `thinking` is sent and the agent's configured
        # level applies. Validation against the model's real ladder happens
        # below, once describe has told us what this session resolves to.
        self.requested_thinking = self.body.get("reasoning_effort") or None
        if self.requested_thinking is not None:
            self.requested_thinking = str(self.requested_thinking).strip().lower() or None
        self.session_thinking_ladder = None
        self.session_model_key = None
        self.session_runtime = None

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
        if self.__task__ and self.__task__ in (
            "title_generation",
            "tags_generation",
            "follow_up_generation",
            "emoji_generation",
            "autocomplete_generation",
            "query_generation",
            "image_prompt_generation",
            "function_calling",
        ):
            pipe_log(f"Skipping OWUI background task: {self.__task__}")
            self.ended = True
            return

        # --- Extract user message ---
        # OWUI sends `content` as a plain string for text-only messages, but
        # as a list of content blocks (`{"type": "text", ...}` /
        # `{"type": "image_url", ...}`) whenever the user attaches an image.
        # Coercing through _coerce_text (already used elsewhere for gateway
        # event payloads with the same block shape) extracts and joins any
        # text blocks and safely no-ops on plain strings.
        messages = self.body.get("messages", [])
        raw_content = messages[-1]["content"] if messages else ""
        self.text = _coerce_text(raw_content)
        has_image = _content_has_image(raw_content)
        self.image_attachments = _extract_image_attachments(raw_content) if has_image else []
        if not self.text and not self.image_attachments:
            if has_image:
                yield (
                    "**Couldn't read that image** -- only base64-embedded "
                    "images are supported right now. Please describe what's "
                    "in the image in words and send that instead."
                )
            else:
                yield "No message"
            self.ended = True
            return

        if has_image and not self.image_attachments:
            # An image block was present but couldn't be turned into an
            # attachment (e.g. not a base64 data URL) -- text still goes
            # through, but say so rather than silently dropping the image.
            yield (
                "_(Note: that image couldn't be relayed to the agent -- "
                "only the text below was sent.)_\n\n"
            )

        _ensure_orphan_reconcile(lambda: self.pipe.valves)
        await _emit_status(self.__event_emitter__, "Thinking...", done=False)
        if self.pipe.valves.COMPOSER_STATUS_BUTTON and not self._initial_visible_text:
            # Cheap and idempotent: installs the composer Status button in the
            # tabs showing this chat (a no-op where it is already there). Once
            # per turn: the thinking-retry recursion skips it.
            _ensure_composer_watcher(lambda: self.pipe.valves)
            await _emit_composer_button(
                self.__event_emitter__,
                session_id=(self.__metadata__ or {}).get("session_id"),
                chat_id=(self.__metadata__ or {}).get("chat_id"),
                model_prefixes=_composer_note_model(self.body.get("model")),
            )
        pipe_log(f"Messages: {len(messages)}, last role: "
                 f"{messages[-1]['role'] if messages else 'NONE'}")

        # --- Get the persistent connection ---
        try:
            self.conn = await _get_gateway_connection(lambda: self.pipe.valves)
        except GatewayError as e:
            await _emit_status(self.__event_emitter__, "", done=True)
            yield f"**Gateway connection error:** {e}"
            self.ended = True
            return


        # --- Derive stable session key ---
        if self.__metadata__:
            self.chat_id = (
                self.__metadata__.get("chat_id")
                or self.__metadata__.get("session_id")
                or self.__metadata__.get("conversation_id")
            )
            user_id = self.__metadata__.get("user_id", "unknown")
        else:
            self.chat_id = None
            user_id = "unknown"

        self.owui_origin_chat_id = self.chat_id
        self.owui_origin_user_id = user_id

        if not self.chat_id:
            self.chat_id = f"owui-{uuid.uuid4().hex[:12]}"
            pipe_log("WARNING: no chat_id in metadata, generated random:", self.chat_id)

        self.session_key = _owui_session_key(self.pipe.valves.AGENT_ID, user_id, self.chat_id)
        pipe_log(f"Session key: {self.session_key}")

        if self.turn is None:
            self.turn = _TurnState()
        self.turn.conn = self.conn
        self.turn.session_key = self.session_key
        self.turn.chat_id = self.owui_origin_chat_id
        self.turn.unsent = list(self._send_texts) if self._send_texts else [self.text]
        if self.owui_origin_chat_id and self.__event_emitter__:
            # Lets a later cancel ask the page whether it was Stop or Send
            # now. Idempotent in the page; fire-and-forget.
            try:
                await self.__event_emitter__(
                    {"type": "execute", "data": {"code": _INTENT_HOOK_JS}})
            except Exception as ex:
                pipe_log(f"  intent hook install failed: {ex!r}")
        # What actually went out in this turn's chat.send, for the
        # thinking-rejection retry to send again.
        self.sent_texts = list(self.turn.unsent)

        # A cancel of this session's previous response that arrived just
        # before this message is OWUI's "Send now" / interrupt, not a Stop:
        # take over what it left (see `park_cancelled_turn`): its run, still
        # running with its events buffered, and any message it had not sent
        # yet. Claimed this early because the model patch below can take
        # seconds and the grace window is short.
        self.adopted_run_id = None
        self.adopted_queue = None
        self.adopted_run_text = None
        claimed = await self.conn.claim_cancelled_turn(self.session_key)
        if claimed:
            if claimed["carried"]:
                self.turn.unsent = claimed["carried"] + self.turn.unsent
                pipe_log(
                    f"  carrying {len(claimed['carried'])} unsent message(s) "
                    "from the cancelled reply"
                )
            self.adopted_run_id = claimed["run_id"]
            self.adopted_queue = claimed["queue"]
            if self.adopted_run_id:
                self.adopted_run_text = claimed.get("run_text")
                self.turn.run_id = self.adopted_run_id
                self.turn.queue = self.adopted_queue
                self.turn.run_text = self.adopted_run_text
                pipe_log(
                    f"  new message right after a cancel: keeping run "
                    f"{self.adopted_run_id[:20]}... alive and continuing it in this "
                    "reply before sending the message"
                )

        # Apply the model. ELI-59: a burst's patches are all redundant -- same
        # session, same model -- so (a) a per-session lock collapses them to one
        # in-flight RPC, and (b) a TTL cache of the last applied model lets the
        # waiters (and the next ~60s of same-model messages) skip the RPC
        # entirely. Failure invalidates the cache so nothing sticks. A failure
        # is reported, not retried: the July retry never saved a reply (every
        # failure since, a model not allowed or a Gateway timeout, failed
        # twice; ELI-131).
        patch_err = None
        async with self.conn.model_patch_lock(self.session_key):
            if self.conn.model_patch_cached(self.session_key, self.model_override):
                pipe_log(
                    f"  model patch skipped (cached: "
                    f"{self.model_override or 'agent default'})"
                )
            else:
                try:
                    patch_resp = await _run_request(
                        self.conn, RUN_PATCH,
                        dict(key=self.session_key, model=self.model_override),
                        timeout=10
                    )
                    if not _model_patch_matches(self.model_override, patch_resp):
                        resolved_model = _resolved_model_key(patch_resp)
                        raise GatewayError(
                            "model override did not apply "
                            f"(wanted {self.model_override or 'agent default'}, "
                            f"got {resolved_model or 'agent default'})"
                        )
                    if self.model_override:
                        pipe_log(f"Applied model override: {self.model_override}")
                    else:
                        pipe_log("Cleared model override; using agent default")
                    self.conn.record_model_patched(self.session_key, self.model_override)
                except Exception as e:
                    patch_err = e
                    pipe_log(f"  [diag] model patch failed: {type(e).__name__}: {e!r}")
                if patch_err is not None:
                    self.conn.invalidate_model_patch(self.session_key)
        if patch_err is not None:
            await _emit_status(self.__event_emitter__, "", done=True)
            yield _explain_session_patch_failure(
                patch_err,
                model_override=self.model_override,
                agent_id=self.pipe.valves.AGENT_ID,
            )
            self.ended = True
            return

        # Tool cards for this session's runs whoever started them, including
        # this reply's own run after a reconnect (see `ensure_message_subscription`).
        await self.conn.ensure_message_subscription(self.session_key)

    async def admit(self):
        """The run this reply streams: a second Open WebUI call follows the one
        it already sent (ELI-82), a taken-over run continues, else the message
        waits for the session to be idle and is sent."""
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

        # Open WebUI can call the pipe twice for one reply (ELI-82: 31 of
        # 1150 replies from 2026-09-26 to 2026-10-14, the second call up to
        # ten minutes later). Sending the message again runs it twice, and
        # the second answer, written last, replaces the first in the same
        # message. So a second call with the same text follows the run the
        # first one started and ends with that run's reply, rebuilt from the
        # transcript once the run is over.
        reply_id = (self.__metadata__ or {}).get("message_id")
        prior = (self.conn.reply_sent(self.session_key, reply_id)
                 if self._send_texts is None and not self.adopted_run_id else None)
        if prior and prior["text"] == "\n\n".join(t for t in self.turn.unsent if t):
            pipe_log(f"  this reply already sent its message as run "
                     f"{prior['run_id'][:20]}... (a second call from Open WebUI); "
                     "following that run instead of sending it again")
            found = None
            while True:
                changed = self.conn.session_change_event(self.session_key)
                try:
                    hist = await self.conn.session_history(self.session_key, limit=RUN_HISTORY_LIMIT)
                except Exception as ex:
                    pipe_log(f"  chat.history for the sent run failed: {ex!r}")
                    break
                found = _stub_turn(hist, reply_id, "")
                if found is None or found["closed"] or not await self._session_run_active():
                    break
                await changed.wait()
            if found is not None:
                for item in _history_renderer(found["messages"]).finished_output():
                    if item.get("type") == "message":
                        yield (item.get("content") or [{}])[0].get("text", "")
                    else:
                        yield {"type": "response.output_item.added",
                               "output_index": _OUTPUT_APPEND_INDEX, "item": item}
                await _emit_status(self.__event_emitter__, "", done=True)
                self.ended = True
                return
            pipe_log("  the sent run is not in the transcript; sending the message")

        if (self.adopted_run_id and self.adopted_queue is None
                and not await self._session_run_active()):
            # It finished during the cancel/re-send gap and nothing buffered
            # its events: nothing to continue. (With a parked queue the run's
            # tail and final are waiting in it, so it is always adopted.)
            pipe_log("  adopted run already ended; sending the message normally")
            self.adopted_run_id = None
            self.adopted_run_text = None
            self.turn.run_id = None
            self.turn.run_text = None
        if self.adopted_run_id:
            # Stream the rest of the adopted run into THIS reply. The message
            # itself is sent afterwards, as its own run, by the recursive call
            # at the end of this turn: sending it now would steer into the
            # live run, and the claude-cli runtime has no steering hook (the
            # gateway acks "started", then drops the message once the run
            # ends -- measured 2026-10-11).
            self.our_run_id = self.adopted_run_id
            self.resolved_thinking = None
            self.pending_clamp_note = ""
            if self.adopted_queue is not None:
                self.queue = self.adopted_queue
            else:
                self.queue = self.conn.register_consumer(self.session_key, self.our_run_id)
            self.turn.queue = self.queue
            await _emit_status(
                self.__event_emitter__,
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
            async with self.conn.session_send_lock(self.session_key):
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
                    changed = self.conn.session_change_event(self.session_key)
                    if not await self._session_run_active():
                        break
                    if not ever_active:
                        ever_active = True
                        pipe_log("Session has an active run (gateway-authoritative); "
                                 "queueing behind it")
                        await _emit_status(
                            self.__event_emitter__,
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
                idempotency_key = (pipe_run_id(self.chat_id, reply_id) if reply_id
                                   else f"msg-{self.chat_id}-{time.time()}")
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
                _state_dir_valve = getattr(self.pipe.valves, "STATE_DIR", "")
                effective_ladder = (_read_model_thinking_ladder(self.model_override, _state_dir_valve)
                                    or _read_model_thinking_ladder(self.session_model_key, _state_dir_valve)
                                    or self.session_thinking_ladder)
                self.resolved_thinking, thinking_note = clamp_to_ladder(
                    self.requested_thinking, effective_ladder)
                # Held until `visible_message_text` exists below (it isn't defined
                # yet at this point in the turn) so the note that streams here also
                # ends up in the forced terminal snapshot -- otherwise it shows
                # while streaming and is gone after a reload.
                self.pending_clamp_note = ""
                if thinking_note:
                    pipe_log(f"thinking: {thinking_note}")
                    # Logged every time, shown once: the dropdown keeps the level
                    # selected, so an unchanged mismatch would otherwise stamp the
                    # same italic line above every answer in the chat.
                    if self.conn.should_announce_thinking_note(self.session_key, thinking_note):
                        self.pending_clamp_note = f"_{thinking_note}_\n\n"
                        yield self.pending_clamp_note
                # Listen before sending: the run's first events can arrive
                # before chat.send returns, and with no consumer they would go
                # to the turn writer, which takes this reply's message over
                # while the reply still streams (review 2af1 R12). The run id
                # is the idempotency key.
                self.queue = self.conn.register_consumer(self.session_key, idempotency_key)
                try:
                    send_resp = await _run_request(
                        self.conn, RUN_SEND,
                        _owui_chat_send_params(
                            session_key=self.session_key,
                            message="\n\n".join(t for t in self.turn.unsent if t),
                            idempotency_key=idempotency_key,
                            owui_chat_id=self.owui_origin_chat_id,
                            owui_user_id=self.owui_origin_user_id,
                            attachments=self.image_attachments,
                            thinking=self.resolved_thinking,
                        ),
                        timeout=30
                    )
                except asyncio.TimeoutError:
                    self.conn.unregister_consumer(self.session_key, idempotency_key, queue=self.queue)
                    await _emit_status(self.__event_emitter__, "", done=True)
                    yield "**Timeout:** Gateway did not respond to chat.send"
                    self.ended = True
                    return
                except Exception as e:
                    self.conn.unregister_consumer(self.session_key, idempotency_key, queue=self.queue)
                    await _emit_status(self.__event_emitter__, "", done=True)
                    yield f"**Error sending message:** {e}"
                    self.ended = True
                    return
                self.our_run_id = send_resp.get("runId") or idempotency_key
                pipe_log(f"Captured runId: {self.our_run_id}")
                if self.our_run_id != idempotency_key:
                    self.conn.unregister_consumer(self.session_key, idempotency_key, queue=self.queue)
                    self.queue = self.conn.register_consumer(self.session_key, self.our_run_id)
                self.sent_texts = list(self.turn.unsent)
                self.conn.note_reply_sent(self.session_key, reply_id, self.our_run_id,
                                     "\n\n".join(t for t in self.sent_texts if t))
                self.turn.unsent = []
                self.turn.run_id = self.our_run_id
                self.turn.queue = self.queue
                self.turn.run_text = None
        # Tell the persistent connection which OWUI message this run streams
        # into, so a turn writer continues that message from a shadow renderer
        # if this inline turn ends before the run does (cancel, close, a
        # torn-down request). No-op without a stable message id. See
        # _turn_route_now (writer.py) and _TurnRenderer (gateway.py).
        self.owui_message_id = (self.__metadata__ or {}).get("message_id")
        self.conn.register_run_target(
            self.session_key, self.our_run_id, self.owui_origin_chat_id, self.owui_message_id,
            thinking=self.resolved_thinking,
        )
        # When the Gateway had admitted this reply's run (chat.send returned;
        # an adopted run already ran). An idle row read after it means the
        # run is over; one read before may show the previous run's settle
        # (review 2af1 N4).
        self.admitted_at = float("-inf") if self.adopted_run_id else time.monotonic()
        # A gap or a resync since: this run's own final and settle may be
        # lost, so another run's settle row may be the first sign it ended.
        self.signals_lost = False
        if self.adopted_run_id and self.adopted_queue is not None:
            self.conn.seed_run_renderer(self.session_key, self.our_run_id, self.queue,
                                   run_text=self.adopted_run_text)
        # Extracted once up front (not just at the terminal snapshot/auto-title
        # step further below) so the mid-run REST snapshot gate has a token to
        # use throughout the streaming loop, not only after it ends.
        self.bearer_token = _extract_request_bearer(self.__request__)

    async def consume(self):
        """The run's events, until its end, a handover or a cancel."""
        # --- Consume events ---
        self.done = False
        self.event_count = 0
        self.text_yielded = False
        self.error_shown = ""  # the run's "**Error:**" line, once shown
        self.aborted = False
        self.first_event_arrived = False
        # The Gateway acks every chat.send with "started"; one that it queued
        # behind a run (or steered into one) instead gets a lone `chat` final
        # with no message as its first and only event.
        self.enqueued_not_started = False
        # The run's text and tool cards, read by the renderer a turn writer
        # uses (ELI-147). Its `received` text is everything the run sent,
        # including what the text hooks still hold, so it is the baseline a
        # cumulative event is diffed against (ELI-80).
        self.renderer = _TurnRenderer()
        # The text this reply has shown, raw: the code state the text hooks
        # read. Continuing a run another reply started, both carry over (see
        # `park_cancelled_turn`), while what THIS reply shows,
        # `visible_message_text`, starts empty.
        self.assistant_stream_text = ""
        if self.adopted_run_text:
            self.renderer.seed(self.adopted_run_text)
            self.assistant_stream_text = self.adopted_run_text.get("stream", "")
        self.visible_message_text = self._initial_visible_text + self.pending_clamp_note
        # Text the hooks hold back: a possible needs-input prompt, a MEDIA:
        # directive whose filename is still arriving.
        self.pending_prompt_text = ""
        self.pending_media_text = ""
        self.last_snapshot_text = ""
        self.last_snapshot_time = 0.0
        self.snapshot_interval_s = 1.0
        self.snapshot_min_delta_chars = 250
        queued_shown = False
        # An interrupted run is not continued here: its message goes to the
        # turn writer, whose settled row decides who continues it (DESIGN 2.7).
        self.handed_over = False
        # A reply with no Open WebUI message (an API request) has nothing to
        # hand over: it applies the owner rule itself, in its own stream
        # (review 2af1 R14). Set while it waits for the interrupted run's row.
        self.awaiting_owner = False
        self.owner_notice_before = False
        self.owner_resumes = 0
        self.owner_run = None

        adopt_new_run = True
        try:
            while adopt_new_run:
                adopt_new_run = False
                # A new run's text is a new message item (review 2af1 F4).
                self.fence_item_start = len(self.assistant_stream_text)
                try:
                    while not self.done:
                        # No silence timer: a quiet queue is a long tool call or
                        # model thinking as often as a dead run. The run's own
                        # signals end the reply: its terminal event, its settle
                        # row (below), a Gateway restart (handed over).
                        msg = await self.queue.get()

                        if msg.get("type") == _ROW_MARKER_TYPE and self.awaiting_owner:
                            step = await self.owner_step(msg.get("row") or {})
                            if step is None:
                                continue
                            if step[0] == "end":
                                if step[1]:
                                    note = f"\n\n{step[1]}" if self.visible_message_text.strip() else step[1]
                                    self.record_visible_chunk(note)
                                    yield note
                                    self.text_yielded = True
                                self.done = True
                            else:
                                # The reply continues in the run that continues
                                # this one: its events (held until now) and its end.
                                _kind, new_run, new_queue = step
                                self.conn.unregister_consumer(self.session_key, self.our_run_id, queue=self.queue)
                                self.queue, self.our_run_id = new_queue, new_run
                                self.turn.run_id, self.turn.queue = self.our_run_id, self.queue
                                self.admitted_at, self.signals_lost = time.monotonic(), False
                            self.awaiting_owner = False
                            self.conn.stop_awaiting_owner(self.session_key, self.owner_run)
                            continue

                        if msg.get("type") == _ROW_MARKER_TYPE:
                            row = msg.get("row") or {}
                            if row.get("status") == "queued" and not self.first_event_arrived \
                                    and not queued_shown:
                                queued_shown = True
                                await _emit_status(
                                    self.__event_emitter__,
                                    "Waiting -- your message is queued behind an "
                                    "active response...",
                                    done=False,
                                )
                            # Read after this run was admitted, an idle session
                            # means it is over, whichever run settled last. That
                            # is the end signal once its own may have been lost
                            # in a gap (review 2af1 N3). A row read before the
                            # admission shows nothing about it (N4).
                            admitted = msg.get("read_at", time.monotonic()) >= self.admitted_at
                            if admitted and msg.get("resync"):
                                self.signals_lost = True
                            tail = None
                            if row.get("hasActiveRun") is False and (
                                    row.get("lastRunId") == self.our_run_id
                                    or (admitted and self.signals_lost)):
                                # The run settled while its terminal event never
                                # reached this reply (dropped frame, socket gap).
                                # Its transcript has what was lost.
                                pipe_log(f"  run settled (row status {row.get('status')}, "
                                         f"last run {str(row.get('lastRunId'))[:24]}) without its final")
                                tail = await self.settled_tail(row)
                            elif row.get("hasActiveRun") and msg.get("resync") and admitted:
                                # After a gap the session is busy, maybe with a
                                # later run: this run's closer says it ended.
                                tail = await self.settled_tail(row, closer_only=True)
                                if tail is not None:
                                    pipe_log("  run closed in the transcript during a gap")
                            if tail is not None:
                                if tail:
                                    self.record_visible_chunk(tail)
                                    yield tail
                                    self.text_yielded = True
                                    await self.maybe_emit_snapshot(force=True)
                                self.done = True
                            continue

                        if msg.get("type") == _RESTART_MARKER_TYPE:
                            # A new Gateway process: the run may be recovered
                            # by it (a crash leaves its row `running`) or need
                            # a resume. Its row decides, in the turn writer,
                            # unless the reply `keeps_its_run`.
                            if not self.keeps_its_run and _hand_over_run(self.conn, self.session_key,
                                    self.our_run_id, thinking=self.resolved_thinking, read_row=True):
                                self.handed_over = self.done = True
                            elif self.awaiting_owner:
                                spawn_background(self.conn.push_row(self.session_key))
                            elif not self.owui_message_id:
                                self.await_owner()
                            else:
                                pipe_log("  Gateway restarted; no message to hand over, waiting")
                            continue

                        if msg.get("type") == _GAP_MARKER_TYPE:
                            # Events were lost on the Gateway socket. The turn
                            # writer rebuilds the message from the run's
                            # transcript, cards included, so the reply hands
                            # it over (ELI-147), with a copy of what it showed
                            # (it closes its own open cards next) and the
                            # events queued behind the gap. Its consumer goes
                            # before any await, so nothing later lands here.
                            # A reply that `keeps_its_run` keeps streaming, as
                            # does one with no message (an API request). What a
                            # gap lost there stays lost.
                            self.signals_lost = True
                            queued = []
                            if not self.keeps_its_run and self.owui_message_id:
                                queued = self.queue.take_all()
                                if _hand_over_run(
                                        self.conn, self.session_key, self.our_run_id,
                                        thinking=self.resolved_thinking, read_row=True,
                                        reply={"message_id": self.owui_message_id,
                                               "renderer": copy.deepcopy(self.renderer),
                                               "events": [m.get("payload") or {} for m in queued
                                                          if m.get("type") == "event"]}):
                                    self.conn.unregister_consumer(self.session_key, self.our_run_id,
                                                                  queue=self.queue)
                                    self.handed_over = self.done = True
                                    continue
                            for m in queued:
                                self.queue.put_control(m)
                            pipe_log("  event gap (Gateway socket dropped): "
                                     "what it lost stays lost in this reply")
                            continue

                        self.event_count += 1
                        self.first_event_arrived = True
                        payload = msg.get("payload", {})
                        stream = payload.get("stream")
                        data = payload.get("data", {})
                        name = data.get("name", "")
                        phase = data.get("phase", "")
                        state = payload.get("state", "")
                        if (self.event_count == 1 and state == "final" and msg.get("event") == "chat"
                                and not payload.get("message")):
                            self.enqueued_not_started = True

                        # --- P16: Double-check session/run match ---
                        evt_session = payload.get("sessionKey", "")
                        evt_run_id = payload.get("runId", "")
                        if evt_session and evt_session != self.session_key:
                            pipe_log(f"  queue delivered wrong session: {evt_session[:40]}...")
                            continue
                        if evt_run_id and evt_run_id != self.our_run_id:
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
                        # message to hand over, or in a reply that
                        # `keeps_its_run`, it ends here like any error.
                        if _EXTERNAL_KILL_RE.search(err) or (aborted_run and self.conn._shutdown_seen):
                            if not self.keeps_its_run and _hand_over_run(self.conn, self.session_key,
                                    self.our_run_id, thinking=self.resolved_thinking):
                                self.handed_over = self.done = True
                                continue
                            if not self.owui_message_id:
                                self.await_owner()
                                continue
                        if stream == "lifecycle" and phase == "end":
                            self.done = True
                            pipe_log("  lifecycle end -> done")
                        elif err:
                            self.error_shown = f"\n\n**Error:** {err}"
                            self.record_visible_chunk(self.error_shown)
                            yield self.error_shown
                            self.done = True
                            pipe_log("  lifecycle error -> done")
                        if state in ("final", "cancelled", "error", "aborted") or aborted_run:
                            self.done = True
                            pipe_log(f"  payload state='{state}' aborted={aborted_run} -> done")
                        # A run the Gateway refused before it started (an
                        # inline `/model` naming a model that is not allowed)
                        # says why only here (ELI-139). A thinking-level
                        # rejection is left to the retry below.
                        run_error = str(payload.get("errorMessage") or "").strip() \
                            if state == "error" else ""
                        if (run_error and not self.error_shown
                                and _thinking_rejection_retry_body(
                                    self.body, run_error, self.resolved_thinking) is None):
                            self.error_shown = f"\n\n**Error:** {run_error}"
                            self.record_visible_chunk(self.error_shown)
                            yield self.error_shown

                        # --- Run text and tool cards ---
                        # One renderer reads them, the one a turn writer uses
                        # (ELI-147).
                        if stream in ("assistant", "item", "tool"):
                            added = self.renderer.feed(payload)
                            for text in (a for a in added if isinstance(a, str)):
                                async for chunk in self.show_text(text):
                                    yield chunk

                        # --- Tool call events ---
                        # The cards come from `_TurnRenderer`, the same code a
                        # turn writer shows them with (ELI-147): each event is
                        # a Responses item Open WebUI folds into its own output
                        # list, so a card is mutable and is never part of the
                        # message text.
                        if stream == "tool":
                            events = [a for a in added if isinstance(a, dict)]
                            if phase == "start":
                                pipe_log(f"  Tool start: {name}")
                                if events and self.pending_media_text:
                                    # The message item ends here, and with it
                                    # any code span a held directive waited on
                                    # (ELI-111): it shows before the card.
                                    held, self.pending_media_text = self.pending_media_text, ""
                                    resolved = await self.settle_held_media(held)
                                    self.assistant_stream_text += held
                                    self.record_visible_chunk(resolved)
                                    yield resolved
                                    self.text_yielded = True
                                for event in events:
                                    yield event
                                if events:
                                    # Text after a tool call is a new message item.
                                    self.fence_item_start = len(self.assistant_stream_text)
                                await _emit_status(
                                    self.__event_emitter__,
                                    f"Running {name}...",
                                    done=False,
                                )

                            elif phase == "result":
                                pipe_log(
                                    f"  Tool result: {name} "
                                    f"({len(_tool_result_text(data.get('result', {})))} chars)"
                                    f"{' [error]' if data.get('isError') is True else ''}"
                                )
                                for event in events:
                                    yield event
                                await _emit_status(
                                    self.__event_emitter__,
                                    f"{name} done",
                                    done=True,
                                )

                        # --- Item events (progress) ---
                        if stream == "item":
                            pipe_log(f"  Item: kind={data.get('kind','')} "
                                     f"status={data.get('status','')} "
                                     f"title={str(data.get('title',''))[:50]}")

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
                    self.aborted = True
                    grace_s = float(getattr(self.pipe.valves, "INTERRUPT_GRACE_SECONDS", 0) or 0)
                    self.turn.parked = True
                    self.conn.park_cancelled_turn(
                        self.session_key, self.our_run_id, self.queue, self.turn.unsent, grace_s,
                        send_stop=self.pipe.valves.SEND_STOP_ON_CANCEL,
                        run_text={"received": self.renderer.received,
                                  "stream": self.assistant_stream_text,
                                  "item": self.renderer.last_item},
                        settle=functools.partial(
                            _settle_cancel_intent, self.conn, self.session_key, self.our_run_id,
                            self.owui_origin_chat_id, self.__event_call__),
                    )
                    pipe_log(
                        f"Generator {type(stop).__name__}: run parked "
                        f"{grace_s:g}s with {len(self.turn.unsent)} unsent "
                        "message(s), in case a new message follows"
                        if grace_s > 0 else
                        f"Generator {type(stop).__name__}: sending chat.abort"
                    )
                    try:
                        await self.maybe_emit_snapshot(force=True)
                        await _emit_status(self.__event_emitter__, "Stopped", done=True)
                    except (Exception, asyncio.CancelledError) as ex:
                        pipe_log(f"  stop snapshot/status skipped: {ex!r}")
                    raise

                else:
                    # Normal completion (no cancellation).
                    if self.pending_prompt_text:
                        # The buffered plain-text tail either fully resolved into a
                        # needs-input trigger (see `_could_be_user_input_prefix`
                        # above -- buffering only continues past the ambiguous
                        # prefix stage while it keeps matching) or the run ended
                        # mid-buffer without ever diverging. Either way, resolve it
                        # now that the full text is available. `maybe_answer_user_input`
                        # re-checks the trigger itself and returns False for a
                        # partial/non-match, in which case fall back to showing it
                        # as plain text so nothing is silently dropped.
                        answer_res = await self.maybe_answer_user_input(self.pending_prompt_text)
                        if answer_res.handled:
                            # Answer delivered. Re-enter the main loop (via the outer
                            # `while adopt_new_run`) to consume the follow-up -- whether
                            # the run resumed in place (steer) or a new run was spawned
                            # -- so the continuation reuses the same needs-input
                            # re-detection, dedup and snapshot handling as any other
                            # run instead of a separate, drift-prone copy of the loop.
                            self.text_yielded = True
                            detail_block = ""
                            if answer_res.answer:
                                detail_block = _ask_user_detail_block(
                                    answer_res.prompt_text or self.pending_prompt_text, answer_res.answer
                                )
                                self.record_visible_chunk(detail_block)
                                yield detail_block
                                await self.maybe_emit_snapshot(force=True)
                            self.conn.note_turn_rewrite(self.owui_origin_chat_id, self.owui_message_id,
                                                   self.pending_prompt_text, detail_block)
                            self.pending_prompt_text = ""
                            if answer_res.new_run_id and answer_res.new_run_id != self.our_run_id:
                                pipe_log(f"  continuing into new run: {answer_res.new_run_id[:20]}...")
                                # The answer run continues this reply (R9).
                                self.switch_to_answer_run(answer_res)
                                self.conn.unregister_consumer(self.session_key, self.our_run_id, queue=self.queue)
                                self.queue = answer_res.queue or self.conn.register_consumer(
                                    self.session_key, answer_res.new_run_id)
                                self.our_run_id = answer_res.new_run_id
                            else:
                                pipe_log("  continuing in the same run after modal answer")
                            self.done = False
                            adopt_new_run = True
                        else:
                            # Modal not handled (cancelled / dismissed / gave up
                            # on reconnect). Show the prompt as a fallback, but
                            # never leak the literal "OpenClaw needs input:"
                            # directive line as visible text.
                            fallback = _ask_user_fallback_text(self.pending_prompt_text)
                            self.conn.note_turn_rewrite(self.owui_origin_chat_id, self.owui_message_id,
                                                   self.pending_prompt_text, fallback)
                            if fallback:
                                self.record_visible_chunk(fallback)
                                yield fallback
                                self.text_yielded = True
                            self.pending_prompt_text = ""

                    if self.pending_media_text:
                        # The run ended with a MEDIA: directive still buffered --
                        # either its filename never got a trailing whitespace
                        # before the stream closed, or "MEDIA:" itself was still
                        # incomplete. Nothing more is coming, so it's safe to
                        # resolve now; `_resolve_media_text` still falls back to
                        # showing it as plain text if resolution fails, so
                        # nothing is silently dropped.
                        resolved = await self.settle_held_media(self.pending_media_text)
                        self.record_visible_chunk(resolved)
                        yield resolved
                        self.text_yielded = True
                        self.pending_media_text = ""

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
                    if not adopt_new_run:
                        note = ("(interrupted -- the reply continues when the run resumes)"
                                if self.handed_over else TOOL_NO_RESULT_NOTE)
                        for event in self.renderer.close_open_calls(note):
                            pipe_log(f"  closing orphaned tool call {event['item']['call_id']}")
                            yield event

        finally:
            if self.awaiting_owner:
                self.conn.stop_awaiting_owner(self.session_key, self.owner_run)
            # A parked queue now belongs to whoever claims the run; releasing
            # it here would drop the events it is buffering.
            if not self.turn.parked:
                if self.done and not self.aborted and self.our_run_id and not self.handed_over:
                    # The run ended in this reply. Its trailing `chat` final
                    # lands after this consumer is gone and must not read as
                    # "the reply ended before its run" (a handover would
                    # rewrite the message OWUI is about to save) or as a run
                    # nobody showed.
                    self.conn.mark_delivered_live(self.session_key, self.our_run_id)
                    self.conn.unregister_run_target(self.session_key, self.our_run_id)
                self.conn.unregister_consumer(self.session_key, self.our_run_id, queue=self.queue)
                self.turn.run_id = None
                self.turn.queue = None
            status_lines = []
            if not self.aborted and self.text_yielded and not self.adopted_run_id and not self.handed_over:
                status_lines = await _build_usage_status_lines(self.conn, self.session_key)
            if self.adopted_run_id and not self.aborted:
                await _emit_status(
                    self.__event_emitter__, "Sending your message…", done=False)
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
                    await _emit_status(self.__event_emitter__, line, done=True)
            else:
                await _emit_status(self.__event_emitter__, "", done=True)

    @property
    def keeps_its_run(self) -> bool:
        """The message holds more than this run: a Send now takeover (it owes
        the user's message) or a continuation of an earlier reply's message.
        The turn writer rebuilds from the run alone, so no handover (ELI-153)."""
        return bool(self.adopted_run_id or self._initial_visible_text)

    async def finish(self):
        """After the run: the messages an adopted run held back, a queued message
        taken back, a lost tail, the terminal snapshot, the title, and the
        recovery or thinking retry of a reply with no text."""
        pipe_log(f"DONE -- {self.event_count} events processed, "
                 f"text yielded: {self.text_yielded}")
        if self.adopted_run_id and not self.aborted:
            # The adopted run is over; now send the messages this reply owes
            # (its own, plus any carried over from cancelled replies), in the
            # same reply. The recursive call does the normal queue + send (it
            # also waits out a subagent-yield gap if the run resumes), and
            # owns the terminal snapshot, usage lines and auto-title. Seeding
            # it with what streamed so far keeps that snapshot complete.
            send_texts = list(self.turn.unsent)
            pipe_log(
                f"  adopted run {self.adopted_run_id[:20]}... finished; sending "
                f"{len(send_texts)} queued message(s)"
            )
            separator = "\n\n---\n\n" if self.visible_message_text.strip() else ""
            if separator:
                self.record_visible_chunk(separator)
                yield separator
            # From here the recursive reply owns them, and parks them itself
            # if it is cancelled.
            self.turn.unsent = []
            async with contextlib.aclosing(self.pipe._pipe_impl(
                self.body, self.__event_emitter__,
                __event_call__=self.__event_call__, __user__=self.__user__,
                __metadata__=self.__metadata__, __request__=self.__request__,
                __task__=self.__task__, __task_body__=self.__task_body__,
                _initial_visible_text=self.visible_message_text,
                _send_texts=send_texts,
            )) as continuation:
                async for item in continuation:
                    yield item
            return
        if self.handed_over:
            # The turn writer owns the message now (DESIGN 2.6): no save of
            # this request's part into it, and no recovery or retry of a
            # reply that showed nothing (the 98891cbb shape, ELI-147).
            return
        if (self.enqueued_not_started and not self.text_yielded and not self.aborted
                and not self.adopted_run_id and self.session_runtime == "claude-cli"
                and self.our_run_id and self.sent_texts):
            # A run started between the idle read and the send (a sub-agent's
            # parent resuming, a wake), and the Gateway queued this message
            # behind it. claude-cli cannot run a queued turn: it fails with
            # "Reply operation has no active tool authority snapshot" once the
            # active run ends (the followup path binds no tool authority,
            # ELI-87), so the message would be lost. Take it back while it is
            # queued, wait for that run to settle, and send it again.
            try:
                await _run_request(
                    self.conn, RUN_ABORT, {"sessionKey": self.session_key, "runId": self.our_run_id},
                    timeout=15,
                )
                took_back = True
            except Exception as ex:
                took_back = False
                pipe_log(f"  could not take back queued message: {ex}")
            if took_back:
                pipe_log(
                    f"  message was queued behind a run that started first "
                    f"(run {str(self.our_run_id)[:20]}...); took it back, resending once idle"
                )
                self.conn.unregister_run_target(self.session_key, self.our_run_id)
                async with contextlib.aclosing(self.pipe._pipe_impl(
                    self.body, self.__event_emitter__,
                    __event_call__=self.__event_call__, __user__=self.__user__,
                    __metadata__=self.__metadata__, __request__=self.__request__,
                    __task__=self.__task__, __task_body__=self.__task_body__,
                    _initial_visible_text=self.visible_message_text,
                    _send_texts=self.sent_texts,
                )) as again:
                    async for item in again:
                        yield item
                return

        if not self.text_yielded:
            pipe_log(
                f"  [diag] PHANTOM (no text): our_run_id={str(self.our_run_id)[:40]} "
                f"first_event_arrived={self.first_event_arrived} aborted={self.aborted}"
            )

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
        if not self.aborted and self.text_yielded:
            await self.maybe_emit_snapshot(force=True)

        # Auto-title: generate title after first exchange (best-effort, non-blocking)
        if not self.aborted and self.text_yielded:
            spawn_background(
                self.pipe._auto_title(
                    self.body, self.conn, self.visible_message_text,
                    self.owui_origin_chat_id, self.bearer_token, session_key=self.session_key,
                )
            )

        if not self.aborted and not self.text_yielded and self.error_shown:
            # The error is the reply: nothing to recover, and no
            # "no reply text" note under it.
            await self.maybe_emit_snapshot(force=True)
        elif not self.aborted and not self.text_yielded:
            recovered = await self.recover_from_transcript()
            retry_body = _thinking_rejection_retry_body(
                self.body, recovered, self.resolved_thinking)
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
                        model_key=rejection["model"] or self.session_model_key,
                        authoritative=True,
                        state_dir=getattr(self.pipe.valves, "STATE_DIR", ""),
                    )
                pipe_log(
                    f"thinking: Gateway rejected level {self.resolved_thinking!r} "
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
                        f"{supported}, so {self.resolved_thinking!r} was dropped "
                        f"for this message. Remembered for next time._\n\n"
                    )
                else:
                    retry_note = (
                        f"_This model does not support thinking level "
                        f"{self.resolved_thinking!r}; sending without it instead._"
                        f"\n\n"
                    )
                self.record_visible_chunk(retry_note)
                yield retry_note
                async with contextlib.aclosing(self.pipe._pipe_impl(
                    retry_body, self.__event_emitter__,
                    __event_call__=self.__event_call__, __user__=self.__user__,
                    __metadata__=self.__metadata__, __request__=self.__request__,
                    __task__=self.__task__, __task_body__=self.__task_body__,
                    _initial_visible_text=self.visible_message_text,
                    _send_texts=self.sent_texts,
                )) as retry:
                    async for item in retry:
                        yield item
                return
            if recovered:
                pipe_log("  recovered assistant text at final fallback")
                self.record_visible_chunk(recovered)
                yield recovered
                await self.maybe_emit_snapshot(force=True)
            else:
                # No assistant text and preview recovery came up empty. Surface
                # the run's REAL terminal state (from the gateway) instead of a
                # scary generic "missing output event" placeholder.
                reason = None
                try:
                    desc = await self.conn.send_request(
                        "sessions.describe", dict(key=self.session_key), timeout=8)
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
                self.record_visible_chunk(no_response_text)
                yield no_response_text
                await self.maybe_emit_snapshot(force=True)

    async def _session_run_active(self) -> bool:
        # Fast path: describe exposes a persisted `status`. When it names a
        # live run state we're done. But describe.status flips to 'done'
        # during a subagent-yield gap (the run is only suspended and will
        # resume -- ELI-56), so a non-active status is NOT conclusive:
        # confirm against sessions.list's authoritative hasActiveRun before
        # treating the session as free to send into.
        try:
            desc = await self.conn.send_request(
                "sessions.describe", dict(key=self.session_key), timeout=8
            )
            row = desc.get("session") or {}
            status = row.get("status")
            self.session_runtime = (row.get("agentRuntime") or {}).get("id") or self.session_runtime
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
            self.session_model_key = _session_model_key(row)
            ladder = _session_thinking_ladder(desc)
            if ladder:
                self.session_thinking_ladder = ladder
                _record_thinking_ladder(
                    ladder, model_key=self.session_model_key,
                    state_dir=getattr(self.pipe.valves, "STATE_DIR", ""))
            if row.get("activeRunId") or status in _ACTIVE_RUN_STATES:
                return True
        except Exception as ex:
            # Describe failed: fall back to the list probe below, and if
            # that also fails, to the (per-worker, possibly-blind) registry.
            pipe_log(f"  active-check describe failed: {ex}")
            status = None
        list_active = await _session_list_active(self.conn, self.session_key, missing=None)
        if list_active is not None:
            active = _session_active_from_signals(status, list_active)
            if active and status not in _ACTIVE_RUN_STATES:
                pipe_log(
                    f"  active-check: describe.status={status!r} but "
                    "list.hasActiveRun=True (subagent-yield gap; queueing)"
                )
            return active
        return self.conn.active_run_id_for_session(self.session_key) is not None

    def await_owner(self) -> None:
        self.awaiting_owner, self.owner_notice_before = True, self.conn._stop_notice_seen
        self.owner_run = self.conn._awaiting_owner[self.session_key] = self.our_run_id
        pipe_log(f"  run {self.our_run_id[:24]}... interrupted, no message to hand over: "
                 "its row decides here")
        spawn_background(self.conn.push_row(self.session_key))

    async def owner_step(self, row: dict):
        """One row while the reply waits for its run's owner (DESIGN 2.7):
        ("switch", run, queue) to continue in the Gateway's recovery run
        or a resume sent here, ("end", note) to finish, None to wait."""
        decision = next_continuation(row, self.owner_notice_before, self.owner_resumes)
        if decision == "gateway":
            # The Gateway recovers it as a run whose turn names this one
            # (rule E); until that turn is committed, the next row looks again.
            try:
                messages = (await self.conn.session_history(self.session_key, limit=50,
                                                       max_chars=200)).get("messages") or []
            except Exception as ex:
                pipe_log(f"  chat.history for the recovery failed: {ex!r}")
                return None
            rec = next((r for r, _t in _history_user_turns(messages)
                        if recovery_source_run(messages, r) == self.our_run_id), None)
            if rec is None:
                return None
            pipe_log(f"  following the Gateway's recovery run {rec[:24]}...")
            return ("switch", rec, self.conn.register_consumer(self.session_key, rec))
        if decision == "wait":
            return None
        if decision == "resume":
            key = f"msg-{self.chat_id}-resume-{time.time()}"
            resume_queue = self.conn.register_consumer(self.session_key, key)
            try:
                resp = await _run_request(self.conn, RUN_SEND, _owui_chat_send_params(
                    session_key=self.session_key, message=_resume_directive(self.visible_message_text, self.text),
                    idempotency_key=key, owui_chat_id=self.owui_origin_chat_id,
                    owui_user_id=self.owui_origin_user_id, thinking=self.resolved_thinking), timeout=30)
            except Exception as ex:
                # The Gateway is closing: its next hello reads the row again.
                self.conn.unregister_consumer(self.session_key, key, queue=resume_queue)
                pipe_log(f"  resume refused ({ex}); trying again on the next row")
                return None
            self.owner_resumes += 1
            new_run = resp.get("runId") or key
            if new_run != key:
                self.conn.unregister_consumer(self.session_key, key, queue=resume_queue)
                resume_queue = self.conn.register_consumer(self.session_key, new_run)
            pipe_log(f"  resumed as {new_run[:32]}... ({self.owner_resumes}/{MAX_TURN_RESUMES})")
            return ("switch", new_run, resume_queue)
        if turn_owner(row, self.owner_notice_before) == "pipe":
            return ("end", TURN_CUT_OFF_NOTE)
        if (row or {}).get("status") in ("failed", "timeout") and row.get("lastRunError"):
            return ("end", f"**Error:** {row['lastRunError']}")
        return ("end", None)

    async def recover_from_transcript(self) -> str | None:
        """The run's text read back: by its run id, else by its user text,
        which only a run seen here may use. With nothing of it seen, the
        session's last reply may be another run's (review 2af1 N4)."""
        try:
            hist = await self.conn.session_history(self.session_key, limit=RUN_TEXT_HISTORY_LIMIT)
            found, whole = _history_text_for_run(hist, self.our_run_id)
            if found and whole:
                return whole
        except Exception as ex:
            pipe_log(f"  recovery by run id: chat.history failed: {ex!r}")
        if not self.first_event_arrived:
            pipe_log("  recovery: nothing of this run seen, no text by its id")
            return None
        return await _recover_turn_text(self.conn, self.session_key, self.text)

    async def settled_tail(self, row: dict, closer_only: bool = False) -> str | None:
        """What this reply still owes once its run settled with no final
        event here: the run's text after what arrived (its closing message
        in the transcript), or, for a run that showed nothing, why.
        `closer_only`: the session is busy, maybe with a later run, so
        only this run's own closer says it ended; None without one."""
        try:
            hist = await self.conn.session_history(self.session_key, limit=RUN_TEXT_HISTORY_LIMIT)
        except Exception as ex:
            pipe_log(f"  settled run: chat.history failed: {ex!r}")
            hist = None
        found, whole = _history_text_for_run(hist, self.our_run_id) if hist else (False, None)
        if found and whole:
            if whole.startswith(self.renderer.received):
                return whole[len(self.renderer.received):]
            return "" if self.text_yielded else whole
        if closer_only:
            return "" if found else None
        if self.text_yielded:
            return ""
        status = row.get("status")
        if status in ("cancelled", "killed"):
            return "**Stopped.**"
        if status in ("failed", "timeout"):
            err = row.get("lastRunError")
            return f"**Error:** {err}" if err else "**Failed:** the run did not complete."
        return await self.recover_from_transcript() or ""

    def record_visible_chunk(self, chunk: str):
        if chunk:
            self.visible_message_text += chunk

    async def maybe_emit_snapshot(self, *, force: bool = False):
        if not self.visible_message_text or self.visible_message_text == self.last_snapshot_text:
            return
        now = time.time()
        if (
            not force
            and self.last_snapshot_text
            and now - self.last_snapshot_time < self.snapshot_interval_s
            and len(self.visible_message_text) - len(self.last_snapshot_text) < self.snapshot_min_delta_chars
        ):
            return
        await _emit_message_snapshot(self.__event_emitter__, self.visible_message_text)
        self.last_snapshot_text = self.visible_message_text
        self.last_snapshot_time = now

    async def maybe_answer_user_input(self, prompt_text: str) -> UserInputResult:
        """Ask the user via an OWUI modal and deliver their answer.

        See UserInputResult for the meaning of the return value. Crucially,
        a delivered answer that steers the *same* run (no new runId) still
        returns handled=True, so the caller suppresses the raw prompt text
        instead of leaking it into the chat.
        """
        if not _is_user_input_prompt(prompt_text):
            return UserInputResult(False, None)
        reconnect_user_id = (
            self.owui_origin_user_id
            if self.owui_origin_user_id and self.owui_origin_user_id != "unknown"
            else None
        )
        try:
            answer = await _ask_user_input_modal(
                self.__event_call__,
                prompt_text,
                owui_user_id=reconnect_user_id,
                owui_chat_id=self.owui_origin_chat_id,
                owui_message_id=(self.__metadata__ or {}).get("message_id"),
                owui_session_id=(self.__metadata__ or {}).get("session_id"),
                __event_emitter__=self.__event_emitter__,
            )
        except Exception as ex:
            pipe_log(f"  user input modal failed; falling back to chat prompt: {ex}")
            answer = None
        if answer is None:
            pipe_log("  user input modal cancelled or empty answer")
            return UserInputResult(False, None)
        pipe_log("  user input answered via OWUI modal")
        await _emit_status(self.__event_emitter__, "Sending answer...", done=False)
        # The answer continues this reply: its run id names it (ELI-100 2.2).
        idempotency_key = (pipe_run_id(self.chat_id, self.owui_message_id, "input") if self.owui_message_id
                           else f"user-input-{self.chat_id}-{time.time()}")
        # Listen before sending, as for the first send (R12): the answer
        # run's first events can arrive before chat.send returns, and with
        # no consumer and no target they would open a second writer on
        # this reply's message (review 2af1 N6).
        answer_queue = self.conn.register_consumer(self.session_key, idempotency_key)
        self.conn.continue_run_target(self.session_key, self.our_run_id, idempotency_key, shown=False)

        def unlisten():
            self.conn.unregister_consumer(self.session_key, idempotency_key, queue=answer_queue)
            self.conn.continue_run_target(self.session_key, idempotency_key, self.our_run_id, shown=False)
        try:
            send_resp = await _run_request(
                self.conn, RUN_SEND,
                _owui_chat_send_params(
                    session_key=self.session_key,
                    message=answer,
                    idempotency_key=idempotency_key,
                    owui_chat_id=self.owui_origin_chat_id,
                    owui_user_id=self.owui_origin_user_id,
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
            self.admitted_at, self.signals_lost = time.monotonic(), False
        await _emit_status(self.__event_emitter__, "Answer sent; continuing...", done=False)
        if new_run_id:
            pipe_log(f"  answer sent, new runId: {new_run_id[:20]}...")
        else:
            pipe_log("  answer sent, steering into active run (no new runId)")
        return UserInputResult(True, new_run_id, prompt_text=prompt_text, answer=answer,
                               queue=answer_queue)

    def switch_to_answer_run(self, answer_res: UserInputResult) -> None:
        """The reply continues in the answer run (R9). Its target moved
        before the send when its consumer was registered then (N6); the
        question run was shown to its end either way."""
        if answer_res.queue is not None:
            self.conn.mark_delivered_live(self.session_key, self.our_run_id)
        else:
            self.conn.continue_run_target(self.session_key, self.our_run_id, answer_res.new_run_id)

    async def show_text(self, text: str):
        """New run text from the renderer, assistant or item alike, through
        the reply's text hooks: held while it may open a needs-input prompt
        (resolved when the run ends), held while a `MEDIA:` filename is still
        arriving, and a complete directive shown as its image."""
        # A fence the shown text of this message item left open makes a
        # marker a quote (ELI-107), and a marker only opens a line; held text
        # always starts outside a fence. A fence never spans items.
        shown = self.assistant_stream_text[self.fence_item_start:] + self.pending_media_text
        text, self.pending_prompt_text = _advance_input_prompt_buffer(
            self.pending_prompt_text, text, _fence_open_after(shown),
            at_line_start=not shown or shown.endswith("\n"))
        if not text:
            return
        # A directive split across deltas is held until its filename is
        # complete (`_advance_media_buffer`); one quoted in code is text
        # (ELI-111), by the shown text of this item.
        text, self.pending_media_text = _advance_media_buffer(
            self.pending_media_text, text, self.assistant_stream_text[self.fence_item_start:])
        if not text:
            return
        self.text_yielded = True
        shown_as = text
        if "MEDIA:" in text:
            resolved, handled = await _resolve_media_text(
                text,
                valves=self.pipe.valves,
                __request__=self.__request__,
                __event_emitter__=self.__event_emitter__,
                ctx=self.assistant_stream_text[self.fence_item_start:],
            )
            if handled:
                pipe_log("  resolved MEDIA: directive")
                # A writer that takes this reply over shows the same image
                # (review 2af1 R4).
                self.conn.note_turn_rewrite(self.owui_origin_chat_id, self.owui_message_id,
                                            text, resolved)
                shown_as = resolved
        # The raw text sets the code state of what follows (ELI-111).
        self.assistant_stream_text += text
        self.record_visible_chunk(shown_as)
        yield shown_as
        # No `replace` snapshot per plain delta: a mid-stream one freezes the
        # visible stream (P23/P25). Open WebUI keeps the in-flight text itself.
        if shown_as is not text:
            await self.maybe_emit_snapshot()

    async def settle_held_media(self, held: str) -> str:
        """Text `_advance_media_buffer` held, once its message item ends:
        resolved as a whole, a directive quoted in code left as text
        (ELI-111), and recorded for a writer that takes the reply over."""
        resolved, handled = await _resolve_media_text(
            held,
            valves=self.pipe.valves,
            __request__=self.__request__,
            __event_emitter__=self.__event_emitter__,
            ctx=self.assistant_stream_text[self.fence_item_start:],
        )
        if handled:
            self.conn.note_turn_rewrite(self.owui_origin_chat_id, self.owui_message_id, held, resolved)
        return resolved
