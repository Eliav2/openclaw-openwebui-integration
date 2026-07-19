# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------



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
        TITLE_GEN_AGENT_ID: str = Field(
            default="title-gen",
            description="Agent id used for the background title-generation session. Deliberately "
                "separate from AGENT_ID: title-gen gets its own CLI process/lane, so it no longer "
                "queues behind the main conversation's active work (2026-07-10 regression, P36-adjacent)."
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
            # `_LEGACY_PRESET_MAP` is the single source of truth for each
            # preset's default model — don't re-hardcode it here.
            legacy_val = {
                "chatgpt": self.valves.CHATGPT_MODEL,
                "opus": self.valves.OPUS_MODEL,
                "sonnet": self.valves.SONNET_MODEL,
                "glm": self.valves.GLM_MODEL,
            }.get(preset, "")
            if legacy_val and legacy_val.strip() and legacy_val.strip() != self._LEGACY_PRESET_MAP[preset]:
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

        Runs under TITLE_GEN_AGENT_ID (a separate configured agent from
        AGENT_ID), not the main conversation's agent. A bare/unprefixed
        sessionKey previously defaulted onto the SAME agent as the live
        conversation, so this background call shared its CLI process/queue
        lane — when the main conversation was busy (any heavy tool-call
        turn), title-gen queued behind it and routinely missed this
        function's ~21s polling window, silently dropping the title
        (2026-07-10 regression). A distinct agent id gets its own lane.
        """
        prompt = (
            "Create a concise title (3-5 words) with a relevant emoji "
            "for this conversation. Output ONLY the title, nothing else "
            "— no quotes, no explanation.\n\n"
            f"User message: {user_msg}\n\n"
            f"Assistant response: {assistant_msg}"
        )

        agent_id = self.valves.TITLE_GEN_AGENT_ID.strip() or "title-gen"
        title_session = f"agent:{agent_id}:title-gen-{uuid.uuid4().hex[:12]}"

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
        _devcoord_marker = _devcoord_turn_begin()
        # DEV-ONLY-END
        try:
            async for item in self._pipe_impl(
                body, __event_emitter__, __event_call__=__event_call__,
                __user__=__user__, __metadata__=__metadata__, __request__=__request__,
                __task__=__task__, __task_body__=__task_body__,
            ):
                yield item
        finally:
            # DEV-ONLY-START
            _devcoord_turn_end(_devcoord_marker)
            # DEV-ONLY-END
            pass

    async def _pipe_impl(self, body, __event_emitter__, __event_call__=None,
                   __user__=None, __metadata__=None, __request__=None,
                   __task__=None, __task_body__=None):
        """Uses a shared persistent WS connection; no per-message reconnect,
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
                    "**Couldn't read that image** — only base64-embedded "
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
                "_(Note: that image couldn't be relayed to the agent — "
                "only the text below was sent.)_\n\n"
            )

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

        # DIAG (2026-07-18, ELI-56): log the OWUI message_id + a short content
        # hash of the user text so a *re-fired / duplicate* completion for the
        # same message — the suspected cause of the phantom "1-event, no-text"
        # turns (e.g. on a socket reconnect over the tailscale proxy) — becomes
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

        # --- Concurrency: queue behind an active run, never steer-merge ---
        # A message that arrives while a run is active must become its OWN run,
        # not a steer-merge into the active run. Steer-merging (the gateway's
        # default when a run is active) returns a *phantom* runId that never
        # runs independently and emits no terminal — the injected message just
        # merges into the active run — so a bubble bound to it renders nothing
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
        # gateway still had an active run — the gateway steer-merged it and
        # returned a runId that instantly emits a single 'final' with no text,
        # so the bubble rendered nothing and the real answer leaked out via
        # proactive delivery. (Verified from live pipe logs 2026-07-18.)
        # Whitelist the states that mean "a run is genuinely in progress".
        # Blacklisting terminal states is unsafe: an idle session reports
        # status "idle" (there is also "done"), so a `not in (done, failed,
        # cancelled)` test would treat every idle session as active and hang
        # every message. Only these states gate the queue.
        _ACTIVE_RUN_STATES = ("active", "running", "streaming", "queued")

        async def _session_run_active() -> bool:
            try:
                desc = await conn.send_request(
                    "sessions.describe", dict(key=session_key), timeout=8
                )
                row = desc.get("session") or {}
                if row.get("activeRunId"):
                    return True
                return row.get("status") in _ACTIVE_RUN_STATES
            except Exception as ex:
                # Only on a describe failure do we consult the (per-worker,
                # possibly-blind) local registry as a weak secondary signal.
                pipe_log(f"  active-check describe failed: {ex}")
                return conn.active_run_id_for_session(session_key) is not None

        if await _session_run_active():
            pipe_log("Session has an active run (gateway-authoritative); queueing behind it")
            await _emit_status(
                __event_emitter__,
                "⏳ Queued behind the current response…",
                done=False,
            )
            # NOTE: do NOT prepend a `/queue followup` directive here. On the
            # 2026.7.1 gateway that directive is NOT stripped from a device
            # chat.send — it leaked verbatim into the delivered message (seen
            # in the OpenClaw transcript as "/queue followup <user text>"),
            # polluting the message without achieving native followup queueing.
            queue_wait_started = time.time()
            queue_wait_cap_s = 1800  # safety ceiling; socket keepalive holds the request
            while time.time() - queue_wait_started < queue_wait_cap_s:
                if not await _session_run_active():
                    break
                await asyncio.sleep(1.5)
            pipe_log("  active run ended; sending queued message as its own run")

        # --- Send this message as its own fresh run ---
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
                    attachments=image_attachments,
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

        async def gateway_run_status() -> str | None:
            """Check the Gateway's own run status (P26).

            A quiet queue before the first event ever arrives does not mean
            the message was lost — it may simply be queued behind another
            active run in the same session (e.g. a long-running agent task).
            Only `sessions.describe` is authoritative; ask it before treating
            60s of silence as a dead run. Returns the raw status string
            (e.g. "active", "done", "failed", "cancelled") so callers can
            tell a user-initiated stop apart from a genuinely dead run;
            None means the probe itself failed or the session is unknown.
            """
            try:
                desc = await conn.send_request(
                    "sessions.describe",
                    dict(key=session_key),
                    timeout=8,
                )
            except Exception as ex:
                pipe_log(f"  initial describe probe failed: {ex}")
                return None
            session_row = desc.get("session")
            if session_row is None:
                return None
            return session_row.get("status")

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
                                status = await gateway_run_status()
                                still_active = status is not None and status not in (
                                    "done", "failed", "cancelled",
                                )
                                if elapsed < no_text_deadman_s and still_active:
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
                                # `status` distinguishes a user-initiated stop (Gateway
                                # reports "cancelled") from a run that genuinely never
                                # produced output — the two used to show the same
                                # generic "Timeout" wording, which was misleading when
                                # the run was simply aborted, not stuck (2026-07-11).
                                if status == "cancelled":
                                    yield "**Stopped.**"
                                elif status == "failed":
                                    yield "**Failed:** the run did not complete."
                                else:
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
                                # NOTE: do NOT emit a `replace` snapshot per text
                                # delta here. A mid-stream `replace` carrying the
                                # full accumulated text makes OWUI's own
                                # `_suppress_already_shown` treat the *continuing*
                                # delta stream as already-shown and suppress it —
                                # the visible stream freezes mid-message (regression
                                # 2026-07-18, exactly the P23/P25 hazard). Text-turn
                                # DB back-sync must be done a different way (e.g. a
                                # single terminal snapshot, or replace-only streaming
                                # that never also yields), not per-delta.
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
                                # Same safety net as the assistant-stream catch-all
                                # above, and for the same reason: `last_item_text`/
                                # `assistant_stream_text` can drift from
                                # `visible_message_text`, and this call site has the
                                # exact same `_item_delta_text` fallback that
                                # re-yields the entire text when the prefix check
                                # fails (P27).
                                item_delta = _suppress_already_shown(item_delta, visible_message_text)
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
            status_lines = []
            if not aborted and text_yielded:
                status_lines = await _build_usage_status_lines(conn, session_key)
            if status_lines:
                # One `status` event per fact (context / rate-limit / goal) so
                # each row stays inside OWUI's per-line clamp instead of one
                # long combined line getting cut off. All of them already
                # reflect final data by the time we get here (both RPCs in
                # `_build_usage_status_lines` already completed) — none of
                # this is genuinely still "in progress", so every line is
                # `done=True`. Marking earlier ones `done=False` made them
                # shimmer for an instant before getting replaced by the last
                # line, a visible flash for no reason.
                for line in status_lines:
                    await _emit_status(__event_emitter__, line, done=True)
            else:
                await _emit_status(__event_emitter__, "", done=True)
            conn.unregister_consumer(session_key, our_run_id, queue=queue)
            self._current_session_key = None
            self._current_run_id = None

        pipe_log(f"DONE — {event_count} events processed, "
                 f"text yielded: {text_yielded}")
        if not text_yielded:
            # DIAG (ELI-56): a no-text turn is the phantom. Emit the correlation
            # keys so it can be matched to a duplicate/re-fired completion above
            # (same text_sha / owui_msg_id) and to the run that actually carried
            # the answer (which then leaks out via proactive delivery).
            pipe_log(
                f"  [diag] PHANTOM (no text): our_run_id={str(our_run_id)[:40]} "
                f"text_sha={getattr(self, '_diag_text_sha', None)} "
                f"first_event_arrived={first_event_arrived} aborted={aborted}"
            )

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
