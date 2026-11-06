# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# Run states that gate the queue. A blacklist of terminal states is unsafe:
# an idle session reports "idle", which it would count as active.
_ACTIVE_RUN_STATES = ("active", "running", "streaming", "queued")
# Gateway questions (secret requests, ELI-96; the ask_user tool, ELI-216).
_QUESTION_HOOKS = (question_requested, question_resolved)
# Plugin approvals are asked in the chat (approvals.py).
_APPROVAL_HOOKS = (plugin_approval_requested, plugin_approval_resolved)
# Slash commands mirrored into Open WebUI's Prompts (slash_prompts.py).
_SLASH_PROMPTS_HOOK = _slash_prompts_on_metadata_changed


def _session_active_from_signals(describe_status, list_has_active_run) -> bool:
    """A session has a run if `sessions.describe` says a run state or
    `sessions.list` says `hasActiveRun` (ELI-56). describe's status flips to
    'done' during a `sessions_yield` gap, while the Gateway still steer-merges
    a message sent then; `hasActiveRun` stays true.
    """
    if describe_status in _ACTIVE_RUN_STATES:
        return True
    return bool(list_has_active_run)


def _thinking_rejection_retry_body(body, recovered, resolved_thinking):
    """The retry body for a textless turn whose preview-recovered text is the
    Gateway's thinking-level rejection, else None (ELI-85 follow-up). The retry
    drops `reasoning_effort`, so it cannot match twice in a row.
    """
    if not recovered or resolved_thinking is None:
        return None
    if parse_thinking_rejection(recovered) is None:
        return None
    retry_body = dict(body)
    retry_body.pop("reasoning_effort", None)
    return retry_body


# Bounds only a Send now follow-up that never comes (tab closed or reloaded).
# Nothing signals that: a socket drop reconnects with a new sid while the
# page and its queued message survive (ELI-95).
_SEND_NOW_HOLD_S = 120.0

# The agent stream a Codex-runtime run reports its items on, images included.
CODEX_ITEM_STREAM = "codex_app_server.item"
# The Gateway attaches a run's images after its final, then pushes
# `sessions.changed` phase `message` (ELI-322). This bounds a Gateway that never does.
_RUN_IMAGES_MAX_WAIT_S = 30.0


async def _current_load(pipe, request):
    """The current load's Pipe when `pipe` belongs to an older load, else None
    (ELI-102). A turn the old load admitted would otherwise meet its retired
    connection and be lost."""
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


async def _settle_cancel_intent(conn, session_key, run_id, chat_id, event_call,
                                message_id=None, task_id=None):
    """Ask the page whether the cancel was Stop or Send now and settle the
    parked turn. A Stop also marks the reply stopped."""
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
    if intent == "stop" and chat_id and message_id:
        # Before the request's own `done: true` save, which follows.
        await _mark_stopped(chat_id, message_id, _owui_request_task(task_id))
    conn.settle_parked_turn(session_key, run_id, intent, hold_s=_SEND_NOW_HOLD_S)


# Open WebUI 0.12.3 creates a chat as "New Chat" (main.py, not translated).
_OWUI_NEW_CHAT_TITLE = "New Chat"
# What Open WebUI strips before titling a chat from a message (middleware.py
# background_tasks_handler), plus skill mentions (utils/skills.py).
_OWUI_TITLE_STRIP_RE = re.compile(
    r'<details\b[^<>]*>(?:(?!<details\b).)*?<\/details>|!\[[^\[\]]*\]\([^()]*\)', re.S | re.I)
_OWUI_SKILL_ID = r'(?:[a-z0-9_-]+|terminal:[^|>\s]+)'
_OWUI_SKILL_MENTION_RE = re.compile(
    rf'<(?:\$({_OWUI_SKILL_ID})(?:\|([^>]*))?|/({_OWUI_SKILL_ID})\|([^>]*))>')


def _message_text(content) -> str:
    """A message's text: the string, or the text blocks of a block list
    (images and files left out)."""
    if isinstance(content, list):
        return "\n".join(p["text"] for p in content if isinstance(p, dict)
                         and p.get("type") == "text" and isinstance(p.get("text"), str))
    return content if isinstance(content, str) else ""


def _owui_title_from_message(content) -> str:
    """The title Open WebUI would give a chat from its first message (the Pipe
    skips its title task)."""
    text = _OWUI_TITLE_STRIP_RE.sub("", _message_text(content)).strip()
    return _OWUI_SKILL_MENTION_RE.sub(lambda m: m.group(2) or m.group(4) or m.group(0), text)


def _title_is_owui_default(title, chat: dict) -> bool:
    """True while the title is still one Open WebUI set by itself ("New Chat"
    or the first message)."""
    if title in (None, "", _OWUI_NEW_CHAT_TITLE):
        return True
    messages = ((chat or {}).get("history") or {}).get("messages") or {}
    return any(title == _owui_title_from_message(m.get("content"))
               for m in messages.values() if m.get("role") == "user" and not m.get("parentId"))


async def _set_title_unless_chosen(chat_id: str, title: str) -> bool:
    """Title the chat under its row lock unless a title was chosen meanwhile
    (ELI-182). Never cut short by a cancel (ELI-205)."""
    return await _uncancellable(_set_title_unless_chosen_now(chat_id, title))


async def _set_title_unless_chosen_now(chat_id: str, title: str) -> bool:
    from open_webui.models.chats import Chats
    async with Chats._chat_transaction(chat_id) as (_db, chat):
        if chat is None or not _title_is_owui_default(chat.title, chat.chat):
            return False
        title = title.replace("\x00", "")
        chat.title = title
        chat.chat = {**(chat.chat or {}), "title": title}
        return True


@dataclass
class _TurnState:
    """What one reply still owes the user if it is cancelled or closed now.

    `unsent`: accepted user messages not yet sent. `run_id` / `queue`: the run
    being consumed. `waiting`: earlier cancelled replies whose messages are in
    `unsent`, each ended with a note once those are sent or dropped (ELI-188).
    `handed_over`: the turn writer took the message over.
    """
    conn: object = None
    session_key: str | None = None
    chat_id: str | None = None
    reply: dict | None = None
    waiting: list = field(default_factory=list)
    status_open: bool = False
    handed_over: bool = False
    unsent: list = field(default_factory=list)
    attachments: list = field(default_factory=list)
    run_id: str | None = None
    queue: object = None
    run_text: dict | None = None
    parked: bool = False


class Pipe:
    """Open WebUI Pipe that routes messages through the OpenClaw Gateway over
    a persistent WebSocket. Valves: see `Valves` or docs/configuration.md.
    """

    class Valves(BaseModel):
        GATEWAY_URL: str = Field(
            default="localhost:18789",
            description="Required. Your OpenClaw Gateway as host:port, for example "
                "192.168.1.10:18789. Use a wss:// prefix for a Gateway behind "
                "a TLS proxy, for example wss://gateway.example.ts.net (port "
                "443 unless you give one). A ws:// or http:// prefix is "
                "accepted and means the same as none. It must be reachable from the Open WebUI backend, which is not "
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
                "with read and write access only, so their turns lose the "
                "owner-only tools (such as automations and messages on your "
                "other channels). This is not a sandbox: they keep every tool "
                "your OpenClaw tool policy allows non-owner turns, which can "
                "include running commands and reading files. Use it whenever "
                "someone other than the Gateway's owner can sign in to Open WebUI."
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
                "unauthenticated -- leave off if port 18791 is exposed. With "
                "it off, a file that cannot be uploaded shows as an error "
                "in the reply, not as a link that never loads. Turning it off "
                "closes the port at the next model list or message, no "
                "Open WebUI restart needed."
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
        # A bound, not a wait: a client with no page hook sends no signal (ELI-95).
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
        AUTO_SEND_WHILE_RUNNING: bool = Field(
            default=True,
            description="Recommended. A message you send while the agent is "
                "working goes into the chat at once, instead of waiting in Open "
                "WebUI's queue above the composer for you to click Send now. The "
                "running reply is not stopped: the agent finishes its current "
                "work, then answers your message. Off: Open WebUI queues it, "
                "and it is sent when the reply ends or when you click Send now."
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
                "Uses a fast model; best-effort, non-blocking. Never replaces a title "
                "the user chose."
        )
        PUBLISH_CHAT_FINISHED: bool = Field(
            default=False,
            description="Publish Open WebUI's 'chat.finished' event when an OpenClaw reply "
                "is saved, for every reply the Pipe writes (also cron, sub-agent and "
                "other agent turns), with source 'openclaw-pipe'. Event functions "
                "(such as a push notifier), event webhooks and notification "
                "targets receive it. Open WebUI's own 'chat.finished' still fires "
                "when the chat request ends, which for OpenClaw can be before the "
                "reply does, so a consumer that gets both should keep this one. A "
                "user with an Open WebUI notification target (Settings > "
                "Notifications) that has 'chat.finished' on gets both for a reply to a chat message, so two "
                "notifications; a reply an agent started (cron, sub-agent, "
                "sessions_send) has no chat request and gets only this one. The event's data holds the full "
                "reply text in 'message'."
        )
        COMPOSER_STATUS_BUTTON: bool = Field(
            default=True,
            description="Add an OpenClaw 'Claw' button to the composer row of "
                "OpenClaw chats (it opens the same dialog as the Status Action's "
                "message button, so that Action must be installed). Installed "
                "into the page at the start of each turn and when a tab opens an "
                "OpenClaw chat. Turn off to stop delivering it; an already open "
                "tab keeps it until reload."
        )
        CHANNEL_THREAD_DEBOUNCE_S: float = Field(
            default=15,
            description="Channels: after a new message in a thread the agent "
                "follows, wait this many seconds for more before deciding whether "
                "to answer, so a burst of messages gets one reply. 0: decide at once."
        )
        CHANNEL_THREAD_IDLE_HOURS: float = Field(
            default=24,
            description="Channels: stop following a thread after this many hours "
                "without a message. A new mention follows it again."
        )
        CHANNEL_CONTEXT_LIMIT: int = Field(
            default=50,
            description="Channels: at most this many messages go to the agent with "
                "one turn (the newest ones); the turn says how many were left out."
        )
        LIVE_VIEW_URL: str = Field(
            default="",
            description="(Optional.) Show the agent's browser live inside a "
                "reply: when a reply first uses a browser tool, this URL is "
                "added to it as an embed. {chat_id} and {message_id} are "
                "replaced with the chat's and the reply's ids. It needs a "
                "service that serves such a page for the agent's browser. "
                "Empty: off."
        )
        LIVE_VIEW_TOOLS: str = Field(
            default=(r"^browser\b"
                     r"|^(?:\S*openclaw\S*|\$\{?\w+\}?)\s+browser\s+(?:--?\S+\s+)*(?:open|navigate|goto|click|type"
                     r"|press|fill|hover|scroll|screenshot|snapshot|evaluate|tabs?|focus|close|start)\b(?!.*\s--help\b)"
                     r"|^(?:\S*/)?(?:chromium(?:-browser)?|google-chrome(?:-stable)?|chrome)(?=\s|$)(?!.*\s--(?:version|help)\b)"
                     r"|^(?:(?:npx|bunx|pnpm\s+(?:dlx|exec)|bun\s+x)\s+(?:-\S+\s+)*)?playwright"
                     r"\s+(?:test|screenshot|open|codegen)\b"
                     r"|^uv\s+run\b.*--with[ =]playwright\b"),
            description="Which tool calls count as browser use for "
                "LIVE_VIEW_URL: a regular expression, case-insensitive, "
                "searched in the tool's name and in each command of its "
                "`command` argument (from the command word on, so a word "
                "inside a quoted string, a heredoc or a file being written "
                "never counts). The default takes the browser tool, "
                "`openclaw browser` commands (how the agent drives its "
                "browser through exec), Chromium or Chrome started as a "
                "command, Playwright's CLI running its open, codegen or "
                "screenshot command or its runner, and "
                "`uv run --with playwright`. Add your own wrapper scripts "
                "here if your agent starts a browser through one: they are "
                "matched the same way, in command position."
        )
        LIVE_VIEW_USERS: str = Field(
            default="",
            description="Open WebUI user ids or emails, comma-separated, whose "
                "replies get LIVE_VIEW_URL. Empty: everyone's, once "
                "LIVE_VIEW_URL is set."
        )
        CONTROL_LINK_PATTERN: str = Field(
            default="",
            description="(Optional.) A link the agent hands you to take over "
                "from it, such as a page that lets you drive its browser for a "
                "login or a CAPTCHA: a regular expression searched in each "
                "tool result. Its first match, an http(s) URL, is added to the "
                "reply as a \"Take control\" link that opens in a new tab, once "
                "per link and chat. Empty: off."
        )
        SYNC_SLASH_COMMANDS: bool = Field(
            default=False,
            description="Offer the Gateway's slash commands in Open WebUI's / "
                "menu: each becomes a Workspace Prompt that every user can "
                "see, kept in sync with the Gateway. A prompt you made "
                "yourself is never changed: a command with the same name is "
                "skipped. Turning this off deletes the prompts the sync made. "
                "The Gateway still refuses an owner-only command from a user "
                "who is not the owner."
        )
        SLASH_COMMANDS_EXCLUDE: str = Field(
            default="stop,new,reset,model,status,compact",
            description="Slash commands not to offer as prompts, "
                "comma-separated. Of the defaults, Open WebUI runs /status, "
                "/compact and /model itself when you send them alone, so they "
                "never reach OpenClaw; /stop has the Stop button; /new and "
                "/reset would reset the session under an open chat."
        )
        PROMPT_PREFIX: str = Field(
            default="",
            description="Put this in front of every synced prompt's command, "
                "for example oc_ to show /oc_status and keep them together in "
                "the menu. Empty: the command is the Gateway's own name."
        )
        SYNC_INTERVAL_MIN: int = Field(
            default=30,
            description="How often, in minutes, to check the Gateway for "
                "changed commands, at most. A check is one small request, made "
                "in the background after a message. A page load and the "
                "Gateway's own change notice also start one.",
            ge=1,
            le=1440
        )
        TITLE_GEN_AGENT_ID: str = Field(
            default="title-gen",
            description="Agent id used for background chat-title generation. It must "
                "exist on the Gateway (default 'title-gen'); if it does not, titles are "
                "silently skipped. Kept separate from AGENT_ID so title generation never "
                "queues behind your conversation."
        )

    # The DEFAULT_MODEL dropdown is a classmethod: it reads the cache via this.
    _last_state_dir = ""

    def __init__(self):
        self.valves = self.Valves()

    async def pipes(self):
        """One model-selector entry per Gateway model, plus 'Default'."""
        type(self)._last_state_dir = getattr(self.valves, "STATE_DIR", "") or ""
        # The first model list after a deploy: with the valve off, close a
        # file server an earlier load left on the port.
        if not self.valves.ENABLE_FILE_SERVER:
            _stop_file_server()
        # Every page load lists models: the earliest point to start the
        # watchers after an Open WebUI restart or redeploy.
        _ensure_composer_watcher(lambda: self.valves)
        _ensure_orphan_reconcile(lambda: self.valves)
        _ensure_channel_watcher(lambda: self.valves)
        _ensure_slash_prompts(lambda: self.valves, force=True)
        models, source = await _discover_models_with_source(self.valves)

        if len(models) > self.valves.MAX_MODELS:
            models = models[:self.valves.MAX_MODELS]

        # A hardcoded list is a guess: say so in the selector. `id` stays the
        # untouched routing key.
        unverified = source == MODELS_SOURCE_FALLBACK
        entries = []
        for m in models:
            friendly = _friendly_name(m)
            provider = _provider_from_key(m["key"])
            name = f"{friendly} ({provider}) · OpenClaw"
            if unverified:
                name += UNVERIFIED_MODEL_SUFFIX
            entries.append({"id": m["key"], "name": name})

        # Default always works: it clears the override and uses the agent's own
        # model. It carries the warning when discovery came back empty.
        default_name = "OpenClaw · Default"
        if unverified and not entries:
            default_name += UNVERIFIED_MODEL_SUFFIX
        return [{"id": "default", "name": default_name}] + entries

    @classmethod
    def get_model_options(cls):
        """Model options for the DEFAULT_MODEL valve dropdown. Reads the state
        dir the running Pipe last resolved: a classmethod has no valves, and the
        default dir would miss a custom STATE_DIR's cache.
        """
        cache = _read_json_file(
            os.path.join(_state_dir(cls._last_state_dir), "models-cache.json")
        )
        cached = cache.get("models") if cache else None
        models = cached or _FALLBACK_MODELS
        # Before the first connection this is an example list, as in pipes().
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
        # Split on the FIRST dot: model keys can hold dots ("gemini-3.1-pro").
        suffix = model.split(".", 1)[-1]
        if suffix == "default":
            return "default"
        return suffix  # raw model key

    def _model_override_for_preset(self, preset):
        """Return the model string to pass to sessions.patch."""
        if preset == "default":
            return self.valves.DEFAULT_MODEL.strip() or None
        return preset


    async def _auto_title(self, body, conn, visible_message_text, owui_origin_chat_id,
                          session_key="", user_id="", message_id=None):
        """Title the chat after the first exchange, when the chat_id is stable,
        unless a title was chosen. Best effort: failures are only logged.
        """
        if not self.valves.AUTO_TITLE:
            return

        messages = body.get("messages", [])
        if not messages or not owui_origin_chat_id or not visible_message_text:
            return

        # On the second user message the chat_id is stable (OWUI replaces the temp ID).
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        if len(user_msgs) != 2 or len(assistant_msgs) != 1:
            return

        user_text = _message_text(user_msgs[0].get("content"))[:400]
        assistant_preview = _message_text(assistant_msgs[0].get("content"))[:400]

        try:
            from open_webui.models.chats import Chats
            chat = await Chats.get_chat_by_id(owui_origin_chat_id)
            if chat is not None and not _title_is_owui_default(chat.title, chat.chat):
                pipe_log(f"Auto-title: chat {owui_origin_chat_id}: its title was chosen; no title")
                return
            pipe_log(f"Auto-title: generating title for chat {owui_origin_chat_id}")
            # The title run carries this chat's text, so it uses the chat's owner status.
            owner = await _sender_is_owner(conn, session_key)
            title = await self._generate_title_text(conn, user_text, assistant_preview, owner=owner)
            if not title:
                return
            if not await _set_title_unless_chosen(owui_origin_chat_id, title):
                pipe_log(f"Auto-title: chat {owui_origin_chat_id}: its title was chosen meanwhile; kept")
                return
            pipe_log(f"Auto-title: chat {owui_origin_chat_id}: set to '{title}'")
            # As Open WebUI does for its own titles: the tab and sidebar retitle.
            await _relay_emit_event(user_id, owui_origin_chat_id, message_id,
                                    {"type": "chat:title", "data": title})
        except Exception as e:
            pipe_log(f"Auto-title: failed (non-fatal): {e}")

    async def _generate_title_text(self, conn, user_msg: str,
                                    assistant_msg: str, owner: bool = True) -> str | None:
        """A 3-5 word title + emoji from a temp-session run, read from its own
        signals. Runs under TITLE_GEN_AGENT_ID so it never queues behind the
        conversation's CLI process.
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
        if "\n" in title:
            title = title.split("\n")[0].strip()
        if len(title) > 80:
            title = title[:77] + "..."
        return title if title else None

    async def pipe(self, body, __event_emitter__, __event_call__=None,
                   __user__=None, __metadata__=None, __request__=None,
                   __task__=None, __task_body__=None):
        """Main entry point: hands a turn admitted by a superseded load of this
        Function to the current load.
        """
        with log_scope(chat_id=(__metadata__ or {}).get("chat_id")):
            remember_owui_server()
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
            # Compares a timestamp; a due check runs in the background.
            _ensure_slash_prompts(lambda: self.valves)
            # aclosing: close the inner generator now, so its cancel handling runs
            # before the next message arrives.
            async with contextlib.aclosing(self._pipe_impl(
                body, __event_emitter__, __event_call__=__event_call__,
                __user__=__user__, __metadata__=__metadata__, __request__=__request__,
                __task__=__task__, __task_body__=__task_body__,
            )) as turn_gen:
                async for item in turn_gen:
                    yield item

    async def _pipe_impl(self, body, __event_emitter__, __event_call__=None,
                   __user__=None, __metadata__=None, __request__=None,
                   __task__=None, __task_body__=None, _initial_visible_text="",
                   _send_texts=None, _waiting=None, _send_attachments=None):
        """One reply: a cancel or close at any point hands what it still owes to
        the next message (`park_cancelled_turn`). `_pipe_turn` parks for itself
        while streaming; this catches every other point. OWUI ends a reply by
        cancelling its task or closing the generator.

        `_send_texts`, `_waiting`, `_send_attachments`: carried-over messages to
        send instead of this request's text.
        """
        turn = _TurnState(waiting=list(_waiting or []))
        emitter = None
        if __event_emitter__:
            async def emitter(event):
                if isinstance(event, dict) and event.get("type") == "status":
                    turn.status_open = not (event.get("data") or {}).get("done")
                await __event_emitter__(event)
        request = _owui_request_task((__metadata__ or {}).get("task_id"))
        if request is not None and _send_texts is None:
            def _request_ended(_task):
                # Open WebUI ended the request with this reply's status line
                # open and nobody else owns the message, so nothing would close
                # it (ELI-200). See `_close_reply_status`.
                if ((turn.status_open or _task.cancelled()) and not turn.parked
                        and not turn.handed_over and turn.reply):
                    spawn_background(_close_reply_status(turn.reply))
                # A handed-over reply is published by its turn writer instead.
                elif (not _task.cancelled() and _task.exception() is None and not turn.parked
                        and not turn.handed_over and turn.reply
                        and getattr(self.valves, "PUBLISH_CHAT_FINISHED", False)):
                    r = turn.reply
                    spawn_background(_publish_chat_finished(r["chat_id"], r["message_id"], r["user_id"]))
            request.add_done_callback(_request_ended)
        try:
            async with contextlib.aclosing(self._pipe_turn(
                body, emitter, __event_call__=__event_call__,
                __user__=__user__, __metadata__=__metadata__,
                __request__=__request__, __task__=__task__,
                __task_body__=__task_body__,
                _initial_visible_text=_initial_visible_text,
                _send_texts=_send_texts, _send_attachments=_send_attachments, turn=turn,
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
                # This reply never sent its message: the reply that does says so.
                waiting = list(turn.waiting)
                if turn.unsent and turn.reply and turn.reply not in waiting:
                    waiting.append(turn.reply)
                turn.conn.park_cancelled_turn(
                    turn.session_key, turn.run_id, turn.queue, turn.unsent,
                    float(getattr(self.valves, "INTERRUPT_GRACE_SECONDS", 0) or 0),
                    attachments=turn.attachments,
                    send_stop=self.valves.SEND_STOP_ON_CANCEL,
                    run_text=turn.run_text,
                    settle=functools.partial(
                        _settle_cancel_intent, turn.conn, turn.session_key,
                        turn.run_id, turn.chat_id, __event_call__),
                    waiting=waiting,
                )
            raise

    async def _pipe_turn(self, body, __event_emitter__, __event_call__=None,
                   __user__=None, __metadata__=None, __request__=None,
                   __task__=None, __task_body__=None, _initial_visible_text="",
                   _send_texts=None, _send_attachments=None, turn=None):
        """One reply over the shared persistent WS connection.
        `_initial_visible_text` seeds the snapshot for the thinking-rejection
        retry, so its note persists on reload.
        """
        state = _PipeTurn(
            self, body, __event_emitter__, __event_call__, __user__, __metadata__,
            __request__, __task__, __task_body__, _initial_visible_text, _send_texts, turn,
            _send_attachments)
        async with contextlib.aclosing(state.run()) as items:
            async for item in items:
                yield item


class _PipeTurn:
    """One reply of `Pipe._pipe_turn`, in phases `prepare`, `admit`, `consume`
    and `finish` (ELI-147). A phase that ends the reply early sets `ended`."""

    def __init__(self, pipe, body, __event_emitter__, __event_call__, __user__,
                 __metadata__, __request__, __task__, __task_body__,
                 _initial_visible_text, _send_texts, turn, _send_attachments=None):
        # The agent was told an unanswered secret never came (once per reply).
        self.secret_not_received_sent = False
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
        self._send_attachments = _send_attachments
        self.turn = turn
        self.ended = False
        self._our_run_id = None

    @property
    def our_run_id(self):
        return getattr(self, "_our_run_id", None)

    @our_run_id.setter
    def our_run_id(self, run_id):
        # Every way this reply gets a run (send, adopt, steer, resume, answer)
        # names it on the log lines from here on (ELI-368).
        self._our_run_id = run_id
        log_run(run_id)

    async def run(self):
        # A run of its own: a retry or resend inside the same reply never logs
        # the previous run's id before its own is known, and the caller's run
        # id comes back after.
        with log_scope(chat_id=_LOG_CHAT.get()):
            try:
                for phase in (self.prepare, self.admit, self.consume, self.finish):
                    async with contextlib.aclosing(phase()) as items:
                        async for item in items:
                            yield item
                    if self.ended:
                        return
            finally:
                _secret_asker_leave(self)

    async def prepare(self):
        """The message, connection, session key, a cancelled run to take over,
        and the model patch."""
        if self.pipe.valves.ENABLE_FILE_SERVER:
            _start_file_server()
        else:
            _stop_file_server()
        preset = self.pipe._selected_preset(self.body)
        self.model_override = self.pipe._model_override_for_preset(preset)

        # Per-chat thinking level (ELI-85): `reasoning_effort`, written by Open
        # WebUI's Advanced Params and by the Thinking filter. Absent means the
        # agent's own level; it is checked against the ladder after describe.
        self.requested_thinking = self.body.get("reasoning_effort") or None
        if self.requested_thinking is not None:
            self.requested_thinking = str(self.requested_thinking).strip().lower() or None
        self.session_thinking_ladder = None
        self.session_model_key = None
        self.session_runtime = None
        # (model key, ladder) last filed: the queue wait probes every 1.5 s.
        self._ladder_recorded = None

        # Skip OWUI background tasks. image_prompt_generation and
        # function_calling have a silent OWUI fallback for an empty answer;
        # moa_response_generation does not, so it is not skipped.
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

        # Continue Response (ELI-184): OpenClaw cannot continue a finished run,
        # and forwarding the reply would send the agent its own words as the
        # user's. So nothing is sent; a toast says why.
        messages = self.body.get("messages", [])
        if messages and messages[-1].get("role") == "assistant":
            pipe_log("Continue Response: the reply is complete, nothing sent to the agent")
            if self.__event_emitter__:
                await self.__event_emitter__({"type": "notification", "data": {
                    "type": "info",
                    "content": "Continue isn't supported for OpenClaw agents: the reply is "
                               "already complete. Send a message to ask for more."}})
            self.ended = True
            return

        # OWUI sends content as a string, or as blocks when an image is attached.
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
            # The image could not become an attachment: say so, text still goes.
            yield (
                "_(Note: that image couldn't be relayed to the agent -- "
                "only the text below was sent.)_\n\n"
            )

        _ensure_orphan_reconcile(lambda: self.pipe.valves)
        # Follows the channel threads again after an Open WebUI restart.
        _ensure_channel_watcher(lambda: self.pipe.valves)
        await _emit_status(self.__event_emitter__, "Thinking...", done=False)
        self.channel_id = _channel_id_of((self.__metadata__ or {}).get("chat_id"))
        self.channel_commit = None
        if (self.pipe.valves.COMPOSER_STATUS_BUTTON and not self._initial_visible_text
                and not self.channel_id):
            # Installs the composer Status button in this chat's tabs; idempotent.
            _ensure_composer_watcher(lambda: self.pipe.valves)
            await _emit_composer_button(
                self.__event_emitter__,
                session_id=(self.__metadata__ or {}).get("session_id"),
                chat_id=(self.__metadata__ or {}).get("chat_id"),
                chat_model=self.body.get("model"),
                model_prefixes=_composer_note_model(self.body.get("model")),
            )
        pipe_log(f"Messages: {len(messages)}, last role: "
                 f"{messages[-1]['role'] if messages else 'NONE'}")

        try:
            self.conn = await _get_gateway_connection(lambda: self.pipe.valves)
        except GatewayError as e:
            await _emit_status(self.__event_emitter__, "", done=True)
            yield f"**Gateway connection error:** {e}"
            self.ended = True
            return


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

        # Several models for one message: each reply gets its own session, so
        # they neither queue behind each other nor share an override (ELI-329).
        state_dir = _state_dir(getattr(self.pipe.valves, "STATE_DIR", ""))
        if self.channel_id:
            # A channel mention: the thread's session, and the text the agent
            # has not seen there yet (see channels.py).
            self.column = 0
            ctx = await _channel_mention_context(
                state_dir, self.channel_id, (self.__metadata__ or {}).get("message_id"),
                self.body.get("model") or "",
                int(getattr(self.pipe.valves, "CHANNEL_CONTEXT_LIMIT", 50) or 50))
            self.channel_root, self.channel_commit = ctx["root"], ctx["commit"]
            if not self._send_texts:
                self.text = ctx["text"]
                pipe_log(f"Channel mention: thread {ctx['root'][:8]}, {ctx['messages']} message(s) sent")
            self.session_key = _channel_session_key(self.pipe.valves.AGENT_ID, self.channel_id,
                                                    self.channel_root)
        else:
            self.column = await _reply_column(state_dir, self.owui_origin_chat_id, self.body.get("model"),
                                        self.__metadata__ or {})
            self.session_key = _owui_session_key(self.pipe.valves.AGENT_ID, user_id, self.chat_id,
                                                 self.column)
        pipe_log(f"Session key: {self.session_key}")

        if self.turn is None:
            self.turn = _TurnState()
        self.turn.conn = self.conn
        self.turn.session_key = self.session_key
        self.turn.chat_id = self.owui_origin_chat_id
        # When the chat no longer shows what the agent remembers (Edit,
        # Regenerate, a branch switch, a copy), the message says so (ELI-183).
        history_prefix = "" if self._send_texts or self.channel_id else await _history_prefix(
            self.conn, state_dir, self.owui_origin_chat_id, self.session_key,
            self.__metadata__ or {}, messages, self.column, self.body.get("model"))
        self.turn.unsent = (list(self._send_texts) if self._send_texts
                            else [history_prefix + _voice_note(self.__metadata__) + self.text])
        self.turn.attachments = (list(self._send_attachments or []) if self._send_texts
                                 else list(self.image_attachments))
        meta = self.__metadata__ or {}
        if self.owui_origin_chat_id and meta.get("message_id") and not self.channel_id:
            self.turn.reply = {"chat_id": self.owui_origin_chat_id, "user_id": user_id,
                               "message_id": meta["message_id"],
                               "request_task_id": meta.get("task_id")}
        if self.owui_origin_chat_id and self.__event_emitter__ and not self.channel_id:
            # Page hooks: a later cancel can ask whether it was Stop or Send now,
            # and AUTO_SEND_WHILE_RUNNING sends a queued message at once.
            try:
                await self.__event_emitter__(
                    {"type": "execute", "data": {"code": _page_hooks_js(
                        self.owui_origin_chat_id, _auto_send_on(self.pipe.valves))}})
            except Exception as ex:
                pipe_log(f"  intent hook install failed: {ex!r}")
        # What went out, for the thinking-rejection retry to send again.
        self.sent_texts = list(self.turn.unsent)
        self.sent_attachments = list(self.turn.attachments)

        # A cancel of the previous response just before this message is a
        # "Send now", not a Stop: take over its run and unsent messages
        # (`park_cancelled_turn`). Claimed early: the model patch can take
        # seconds and the grace window is short.
        self.adopted_run_id = None
        self.adopted_queue = None
        self.adopted_run_text = None
        claimed = await self.conn.claim_cancelled_turn(self.session_key)
        if claimed:
            self.turn.waiting = claimed.get("waiting", []) + self.turn.waiting
            if claimed["carried"]:
                self.turn.unsent = claimed["carried"] + self.turn.unsent
                self.turn.attachments = claimed.get("attachments", []) + self.turn.attachments
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

        # An image over the Gateway's cap is refused whole: shrink it once, after
        # the claim (which must not wait behind the fit), so every later send
        # carries the copy that fits. The whole list is checked, since a claimed
        # turn carries a cancelled reply's images. Only an oversized image leaves
        # the event loop, one fit at a time (each holds the decoded photo).
        if not self._send_texts and _images_over_cap(
                self.turn.attachments, self.conn.gateway_max_image_bytes):
            try:
                async with _image_fit_lock:
                    fitted = await asyncio.to_thread(
                        _fit_image_attachments, self.turn.attachments,
                        self.conn.gateway_max_image_bytes)
            except ImageTooLargeError as e:
                await _emit_status(self.__event_emitter__, "", done=True)
                yield str(e)
                # The messages claimed for this send will not go out with it.
                self.end_waiting_replies_unsent()
                self.ended = True
                return
            # The retry copy and this message's list hold the originals: swap in the fits.
            fitted_for = {id(a): f for a, f in zip(self.turn.attachments, fitted)}
            self.sent_attachments = [fitted_for.get(id(a), a) for a in self.sent_attachments]
            self.image_attachments = [fitted_for.get(id(a), a) for a in self.image_attachments]
            self.turn.attachments = fitted

        # Apply the model (ELI-59): a per-session lock and a TTL cache collapse a
        # burst's identical patches. A failure is reported, not retried: it
        # fails the same way twice (ELI-131).
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
                user_text=self.text,
            )
            self.ended = True
            return

        # Tool cards for this session's runs whoever started them (`ensure_message_subscription`).
        await self.conn.ensure_message_subscription(self.session_key)

    async def admit(self):
        """The run this reply streams: a duplicate Open WebUI call's (ELI-82), a
        taken-over run, or a fresh send once the session is idle."""
        # A message that arrives during a run becomes its own run, never a
        # steer-merge: that returns a phantom runId with no terminal, and the
        # bubble renders nothing. Whether a run is active is the Gateway's call
        # (sessions.describe): OWUI's worker processes each see only their own runs.

        # Open WebUI can call the pipe twice for one reply (ELI-82), up to ten
        # minutes apart. A second call with the same text follows the first
        # call's run instead of running it twice.
        reply_id = (self.__metadata__ or {}).get("message_id")
        prior = (self.conn.reply_sent(self.session_key, reply_id)
                 if self._send_texts is None and not self.adopted_run_id else None)
        if prior and prior["text"] == "\n\n".join(t for t in self.turn.unsent if t):
            pipe_log(f"  this reply already sent its message as run "
                     f"{prior['run_id'][:20]}... (a second call from Open WebUI); "
                     "following that run instead of sending it again")
            found = None
            steers = steers_mid_run_messages(self.conn.gateway_version)
            while True:
                changed = self.conn.session_change_event(self.session_key)
                try:
                    hist = await self.conn.session_history(self.session_key, limit=RUN_HISTORY_LIMIT)
                except Exception as ex:
                    pipe_log(f"  chat.history for the sent run failed: {ex!r}")
                    break
                found = _stub_turn(hist, reply_id, "", steers=steers)
                if found is None or found["closed"]:
                    break
                if not await self._session_run_active():
                    # The closing message can land after the read above: read once more.
                    try:
                        hist = await self.conn.session_history(self.session_key, limit=RUN_HISTORY_LIMIT)
                        found = _stub_turn(hist, reply_id, "", steers=steers) or found
                    except Exception as ex:
                        pipe_log(f"  chat.history after the run ended failed: {ex!r}")
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
            # It ended in the cancel/re-send gap with nothing buffered: send normally.
            pipe_log("  adopted run already ended; sending the message normally")
            self.adopted_run_id = None
            self.adopted_run_text = None
            self.turn.run_id = None
            self.turn.run_text = None
        if self.adopted_run_id:
            # Stream the adopted run's rest here; this message is sent afterwards
            # as its own run. claude-cli has no steering hook: the Gateway acks
            # "started", then drops a steered message.
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
            # The per-session lock covers wait + send + registration, so the next
            # waiter sees our run active instead of racing into the same idle
            # window, where the Gateway steer-merges it (ELI-56). Never prepend
            # `/queue followup`: the Gateway leaks it verbatim from a device send.
            # Messages waiting behind one run go out together (`hand_on`).
            joins = self._send_texts is None and bool(self.turn.reply) and not self.channel_id
            if joins:
                self.conn.send_waiters(self.session_key).append(self)
            try:
                async for item in self.wait_and_send(reply_id):
                    yield item
            finally:
                if joins:
                    self.conn.drop_send_waiter(self.session_key, self)
            if self.ended:
                return
        self.register_target()

    def end_waiting_replies_unsent(self) -> None:
        """The send carrying `turn.waiting`'s messages failed: those replies say so."""
        _end_unsent_replies(self.conn, self.turn.waiting, sent=False, note=REPLY_SEND_FAILED_NOTE)
        self.turn.waiting = []

    def hand_on(self) -> bool:
        """Hand this reply's message to the session's newest waiting reply,
        which sends both as one turn (ELI-188). Not when that reply asks for
        another thinking level: one send has one level."""
        self.conn.drop_send_waiter(self.session_key, self)
        waiters = self.conn.send_waiters(self.session_key)
        mine = self.turn.reply["message_id"]
        later = next((w for w in reversed(waiters) if w.turn.reply["message_id"] != mine), None)
        if later is None or any(w.requested_thinking != self.requested_thinking for w in waiters):
            return False
        at_text = getattr(later, "handed_texts", 0)
        at_image = getattr(later, "handed_images", 0)
        later.turn.unsent[at_text:at_text] = self.turn.unsent
        later.turn.attachments[at_image:at_image] = self.turn.attachments
        later.handed_texts = at_text + len(self.turn.unsent)
        later.handed_images = at_image + len(self.turn.attachments)
        later.turn.waiting = later.turn.waiting + self.turn.waiting + [self.turn.reply]
        pipe_log(f"  {len(self.turn.unsent)} queued message(s) go with a later one "
                 f"(reply {later.turn.reply['message_id'][:8]}...)")
        self.turn.unsent, self.turn.attachments, self.turn.waiting = [], [], []
        return True

    def steers_into_active_run(self) -> bool:
        """Whether this message is steered into the session's running run
        (2026.10.10+). Text only, since the Gateway keeps images for a later
        turn; never in a channel thread; only into this chat's own run at the
        same model and thinking level."""
        return (steers_mid_run_messages(self.conn.gateway_version)
                and not self.turn.attachments and not self.channel_id
                and self.conn.user_turn_run_active(self.session_key, self.chat_id)
                and self.conn.sent_settings.get(self.session_key)
                == (self.model_override, self.requested_thinking))

    async def wait_and_send(self, reply_id):
        """Wait for the run to settle, then send as a fresh run (ELI-56) or hand on."""
        async with self.conn.send_section(self.session_key):
            # Wait on `sessions.changed` pushes, re-reading state after each.
            # No poll and no ceiling (ELI-88): a ceiling would release a message
            # into a live run, where claude-cli drops it.
            self.to_be_steered = False

            async def active():
                # Steerable: the Gateway steers it in at the next tool boundary.
                busy = await self._session_run_active()
                if busy and self.steers_into_active_run():
                    pipe_log("Session has an active run; sending now, to be steered into it")
                    self.to_be_steered = True
                    return False
                return busy

            async def queued():
                pipe_log("Session has an active run (gateway-authoritative); "
                         "queueing behind it")
                await _emit_status(
                    self.__event_emitter__,
                    "Queued: waiting for the current run to finish…",
                    done=False,
                )
            ever_active = await _wait_for_session_idle(
                self.conn, self.session_key, active, on_busy=queued)
            if ever_active and not self.to_be_steered:
                pipe_log("  active run settled; sending queued message as its own run")
            if self in self.conn.send_waiters(self.session_key) and self.hand_on():
                await _emit_status(self.__event_emitter__, "", done=True)
                self.ended = True
                return

            # The OWUI message id in the run id ties a reply orphaned by a
            # restart to its run (ELI-90, `_stub_turn`).
            def new_run_key():
                return (pipe_run_id(self.chat_id, reply_id) if reply_id
                        else f"msg-{self.chat_id}-{time.time()}")
            idempotency_key = new_run_key()
            # Clamp the level down to what this model supports, with a note.
            # A ladder the Gateway stated by a rejection beats describe's, which
            # overreports for catalog-gated models. Look up by `model_override`
            # (the routing key) first: the rejection names that key, and the
            # vendor key alone missed it every turn.
            _state_dir_valve = getattr(self.pipe.valves, "STATE_DIR", "")
            effective_ladder = (_read_model_thinking_ladder(self.model_override, _state_dir_valve)
                                or _read_model_thinking_ladder(self.session_model_key, _state_dir_valve)
                                or self.session_thinking_ladder)
            self.resolved_thinking, thinking_note = clamp_to_ladder(
                self.requested_thinking, effective_ladder)
            # Held until `visible_message_text` exists, so the note persists on reload.
            self.pending_clamp_note = ""
            if thinking_note:
                pipe_log(f"thinking: {thinking_note}")
                # Logged every time, shown once: the dropdown keeps the level.
                if self.conn.should_announce_thinking_note(self.session_key, thinking_note):
                    self.pending_clamp_note = f"_{thinking_note}_\n\n"
                    yield self.pending_clamp_note
            # Listen before sending: early events with no consumer would go to
            # the turn writer, which would take over the message mid-stream.
            self.queue = self.conn.register_consumer(self.session_key, idempotency_key)
            self.conn.sent_settings[self.session_key] = (self.model_override, self.requested_thinking)
            send_params = _owui_chat_send_params(
                session_key=self.session_key,
                message="\n\n".join(t for t in self.turn.unsent if t),
                idempotency_key=idempotency_key,
                owui_chat_id=self.owui_origin_chat_id,
                owui_user_id=self.owui_origin_user_id,
                attachments=_numbered_attachments(
                    self.turn.attachments, combined=len(self.turn.unsent) > 1),
                thinking=self.resolved_thinking,
            )
            # One wait budget for the send and its resend, which take one route.
            send_started = time.monotonic()
            try:
                for resend in (False, True):
                    try:
                        send_resp = await _run_request(self.conn, RUN_SEND, send_params,
                                                       timeout=30, started=send_started)
                        break
                    except GatewayError as ex:
                        if resend or not is_unpublished_input(ex):
                            raise
                        # Committed but never published, and its key is spent (ELI-359): once
                        # more under a new key, which runs it once. A second refusal is shown.
                        self.conn.unregister_consumer(self.session_key, idempotency_key, queue=self.queue)
                        idempotency_key = new_run_key()
                        self.queue = self.conn.register_consumer(self.session_key, idempotency_key)
                        pipe_log(f"  chat.send: {ex}; sending once more as {idempotency_key}")
                        send_params = {**send_params, "idempotencyKey": idempotency_key}
            except asyncio.TimeoutError:
                self.conn.unregister_consumer(self.session_key, idempotency_key, queue=self.queue)
                await _emit_status(self.__event_emitter__, "", done=True)
                yield _plain_error(ERROR_TIMEOUT, "Gateway did not respond to chat.send", self.text)
                self.end_waiting_replies_unsent()
                self.ended = True
                return
            except Exception as e:
                self.conn.unregister_consumer(self.session_key, idempotency_key, queue=self.queue)
                await _emit_status(self.__event_emitter__, "", done=True)
                yield _plain_error(ERROR_SEND, f"Error sending message: {e}", self.text)
                self.end_waiting_replies_unsent()
                self.ended = True
                return
            self.our_run_id = send_resp.get("runId") or idempotency_key
            pipe_log(f"Captured runId: {self.our_run_id}")
            if self.our_run_id != idempotency_key:
                self.conn.unregister_consumer(self.session_key, idempotency_key, queue=self.queue)
                self.queue = self.conn.register_consumer(self.session_key, self.our_run_id)
            self.sent_texts = list(self.turn.unsent)
            self.sent_attachments = list(self.turn.attachments)
            self.conn.note_reply_sent(self.session_key, reply_id, self.our_run_id,
                                 "\n\n".join(t for t in self.sent_texts if t))
            self.turn.unsent = []
            self.turn.attachments = []
            if getattr(self, "to_be_steered", False):
                await _emit_status(self.__event_emitter__, STEER_PENDING_STATUS, done=False)
            if self.channel_commit is not None:
                # The Gateway has the turn: commit the thread's cursors.
                self.channel_commit()
                self.channel_commit = None
                _ensure_channel_watcher(lambda: self.pipe.valves)
            _end_unsent_replies(self.conn, self.turn.waiting, sent=True)
            self.turn.waiting = []
            self.turn.run_id = self.our_run_id
            self.turn.queue = self.queue
            self.turn.run_text = None

    def register_target(self):
        """After admission: tie the run to this reply's message."""
        # Tell the connection which OWUI message this run streams into, so a
        # turn writer can continue it if this inline turn ends first (see
        # _turn_route_now in writer.py).
        self.owui_message_id = (self.__metadata__ or {}).get("message_id")
        self.conn.register_run_target(
            self.session_key, self.our_run_id, self.owui_origin_chat_id, self.owui_message_id,
            thinking=self.resolved_thinking,
            request_task_id=(self.__metadata__ or {}).get("task_id"),
            steer_pending=bool(getattr(self, "to_be_steered", False)),
        )
        # An idle row read after admission means the run is over; one read
        # before may show the previous run's settle.
        self.admitted_at = float("-inf") if self.adopted_run_id else time.monotonic()
        # After a gap the run's own final may be lost, so another settle may be the first sign.
        self.signals_lost = False
        if self.adopted_run_id and self.adopted_queue is not None:
            self.conn.seed_run_renderer(self.session_key, self.our_run_id, self.queue,
                                   run_text=self.adopted_run_text)
        self.bearer_token = _extract_request_bearer(self.__request__)

    async def consume(self):
        """The run's events, until its end, a handover or a cancel."""
        # A secret the run requests is asked on this reply's socket (ELI-96).
        _secret_asker_join(self)
        self.done = False
        self.event_count = 0
        self.text_yielded = False
        self.error_shown = ""  # the run's "**Error:**" line, once shown
        self.aborted = False
        self.first_event_arrived = False
        # The run's last-call usage, from its `chat` final (ELI-177).
        self.run_usage = None
        # A send the Gateway queued or steered gets a lone `chat` final first.
        self.enqueued_not_started = False
        # The renderer a turn writer uses too (ELI-147). Its `received` text is
        # the baseline cumulative events are diffed against (ELI-80).
        self.renderer = _TurnRenderer()
        # Listed by the Status Action while this reply runs (ELI-186).
        live_key = (self.owui_origin_chat_id, getattr(self, "owui_message_id", None))
        if all(live_key):
            _live_replies()[live_key] = self.renderer
        # Raw shown text, the state the text hooks read. Carried over when
        # continuing another reply's run; `visible_message_text` starts empty.
        self.assistant_stream_text = ""
        if self.adopted_run_text:
            self.renderer.seed(self.adopted_run_text)
            self.assistant_stream_text = self.adopted_run_text.get("stream", "")
        self.visible_message_text = self._initial_visible_text + self.pending_clamp_note
        self.pending_prompt_text = ""
        self.pending_media_text = ""
        # Gateway image blocks show only when no MEDIA: directive did.
        self.media_shown = False
        self.end_history = None
        # Codex images: attached to the stored message after the final (ELI-322).
        self.images_made = 0
        # Paths the reply named: the Gateway stores each as an image block after
        # the final (ELI-328).
        self.gateway_media: set[str] = set()
        self.event_media: set[str] = set()   # bare names and URLs of `mediaUrls`, shown once
        self.last_snapshot_text = ""
        self.last_snapshot_time = 0.0
        self.snapshot_interval_s = 1.0
        self.snapshot_min_delta_chars = 250
        self.queued_shown = False
        # An interrupted run goes to the turn writer, whose settled row decides
        # who continues it (DESIGN 2.7).
        self.handed_over = False
        # A reply with no Open WebUI message (API) applies the owner rule itself.
        self.awaiting_owner = False
        self.owner_notice_before = False
        self.owner_resumes = 0
        self.owner_run = None
        # Background Agent calls keep the run open, so the reply says why (ELI-163).
        self.background_agents = 0
        # The run went on as a later steered message's answer: its run id.
        self.steered_into = None

        self.adopt_new_run = True
        try:
            while self.adopt_new_run:
                self.adopt_new_run = False
                self.fence_item_start = len(self.assistant_stream_text)
                try:
                    while not self.done:
                        # No silence timer: a quiet queue is often a long tool
                        # call. The run's own signals end the reply.
                        msg = await self.queue.get()

                        if msg.get("type") == _ROW_MARKER_TYPE and self.awaiting_owner:
                            async for chunk in self.on_owner_row(msg):
                                yield chunk
                            continue

                        if msg.get("type") == _ROW_MARKER_TYPE:
                            async for chunk in self.on_settle_row(msg):
                                yield chunk
                            continue

                        if msg.get("type") == _RESTART_MARKER_TYPE:
                            self.on_restart()
                            continue

                        if msg.get("type") == _GAP_MARKER_TYPE:
                            self.on_gap()
                            continue

                        if msg.get("_steered"):
                            await self.on_steered(msg["_steered"])
                            continue

                        if msg.get("type") == _STEERED_MARKER_TYPE:
                            # A later message was steered in: the rest is its answer.
                            self.steered_into = msg.get("into")
                            self.done = True
                            pipe_log(f"  a later message was steered into run "
                                     f"{str(self.our_run_id)[:24]}...; this reply ends here")
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

                        evt_session = payload.get("sessionKey", "")
                        evt_run_id = payload.get("runId", "")
                        if evt_session and evt_session != self.session_key:
                            pipe_log(f"  queue delivered wrong session: {evt_session[:40]}...")
                            continue
                        if evt_run_id and evt_run_id != self.our_run_id:
                            pipe_log(f"  queue delivered wrong run: {evt_run_id[:20]}...")
                            continue

                        err = (str(data.get("error", "unknown"))
                               if stream == "lifecycle" and phase == "error" else "")
                        aborted_run = state == "aborted" or data.get("aborted") is True
                        if self.hand_over_after_kill(err, aborted_run):
                            continue
                        async for chunk in self.on_completion(payload, stream, phase, state, err, aborted_run):
                            yield chunk

                        # One renderer, shared with the turn writer (ELI-147).
                        if (stream == CODEX_ITEM_STREAM and phase == "completed"
                                and data.get("type") == "imageGeneration"):
                            self.images_made += 1
                        if stream == COMPACTION_STREAM:
                            pipe_log(f"  Compaction: phase={phase} completed={data.get('completed')}")
                        if stream in ("assistant", "item", "tool", PIPE_NOTE_STREAM, COMPACTION_STREAM):
                            added = self.renderer.feed(payload)
                            for text in (a for a in added if isinstance(a, str)):
                                async for chunk in self.show_text(text):
                                    yield chunk
                        if stream == "assistant":
                            # `MEDIA:` lines the Gateway took out (ELI-328).
                            for url in _event_media_urls(data):
                                if _is_gateway_media_name(url):
                                    self.gateway_media.add(url)
                                elif url not in self.event_media:
                                    self.event_media.add(url)
                                    async for chunk in self.show_text(f"\nMEDIA:{url}\n"):
                                        yield chunk

                        # Tool cards are Responses items OWUI folds into its
                        # output list, never part of the message text (ELI-147).
                        if stream == "tool":
                            events = [a for a in added if isinstance(a, dict)]
                            if phase == "start":
                                pipe_log(f"  Tool start: {name}")
                                await self.maybe_show_live_view(name, data.get("args"))
                                if events and self.pending_media_text:
                                    # A held directive's code span ends here (ELI-111).
                                    held, self.pending_media_text = self.pending_media_text, ""
                                    resolved = await self.settle_held_media(held)
                                    self.assistant_stream_text += held
                                    self.record_visible_chunk(resolved)
                                    yield resolved
                                    self.text_yielded = True
                                for event in events:
                                    yield event
                                if events:
                                    self.fence_item_start = len(self.assistant_stream_text)

                            elif phase == "result":
                                pipe_log(
                                    f"  Tool result: {name} "
                                    f"({len(_tool_result_text(data.get('result', {})))} chars)"
                                    f"{' [error]' if data.get('isError') is True else ''}"
                                )
                                for event in events:
                                    yield event
                                if _is_background_agent_launch(name, _tool_result_text(data.get("result", {})),
                                                               data.get("isError") is True):
                                    self.background_agents += 1
                                    await _emit_status(self.__event_emitter__,
                                                       _background_agents_line(self.background_agents))

                        # The Pipe's own notes (ELI-197), compaction's too (ELI-283).
                        if stream in (PIPE_NOTE_STREAM, COMPACTION_STREAM):
                            for event in (a for a in added if isinstance(a, dict)):
                                yield event
                            self.fence_item_start = len(self.assistant_stream_text)

                        if stream == "item":
                            pipe_log(f"  Item: kind={data.get('kind','')} "
                                     f"status={data.get('status','')} "
                                     f"title={str(data.get('title',''))[:50]}")

                except (asyncio.CancelledError, GeneratorExit) as stop:
                    await self.park(stop)
                    raise

                else:
                    released = self.renderer.flush()
                    if released:
                        async for chunk in self.show_text(released):
                            yield chunk
                    async for chunk in self.finish_held_prompt():
                        yield chunk
                    async for chunk in self.finish_held_media():
                        yield chunk
                    async for chunk in self.finish_final_images():
                        yield chunk
                    async for chunk in self.close_open_cards():
                        yield chunk

        finally:
            if all(live_key) and _live_replies().get(live_key) is self.renderer:
                _live_replies().pop(live_key, None)
            if self.awaiting_owner:
                self.conn.stop_awaiting_owner(self.session_key, self.owner_run)
            # Read the usage line while still holding the consumer: dropping it
            # closes a retired connection and the request never answers (#157 gate).
            status_lines = []
            try:
                if not self.aborted and self.text_yielded and not self.adopted_run_id and not self.handed_over:
                    status_lines = await _build_usage_status_lines(
                        self.conn, self.session_key, run_usage=await self.own_run_usage())
            finally:
                # A parked queue belongs to whoever claims the run.
                if not self.turn.parked:
                    if (self.done and not self.aborted and self.our_run_id and not self.handed_over
                            and not self.steered_into):
                        # The run ended here: its trailing `chat` final must not
                        # read as an early end or an unshown run.
                        self.conn.mark_delivered_live(self.session_key, self.our_run_id)
                        self.conn.unregister_run_target(self.session_key, self.our_run_id)
                    self.conn.unregister_consumer(self.session_key, self.our_run_id, queue=self.queue)
                    self.turn.run_id = None
                    self.turn.queue = None
            if self.adopted_run_id and not self.aborted:
                await _emit_status(
                    self.__event_emitter__, "Sending your message…", done=False)
            elif status_lines:
                # One `status` per fact, since OWUI clamps each to one line. All
                # done=True: done=False made them shimmer before the last one.
                for line in status_lines:
                    await _emit_status(self.__event_emitter__, line, done=True)
            else:
                await _emit_status(self.__event_emitter__, "", done=True)

    async def on_steered(self, steered: dict) -> None:
        """Continue the run this message was steered into. What it sent before
        belongs to the earlier reply, so its closing whole-text events are not
        shown again."""
        run_text = steered.get("run_text")
        if isinstance(run_text, asyncio.Future):
            run_text = await run_text
        run_text = dict(run_text or {})
        earlier = run_text.pop("earlier", None)
        # Steered, its own run stays registered until its lone final.
        await _emit_status(self.__event_emitter__, "Thinking...", done=False)
        # Settled once the run ends (`_settle_steered_replies`).
        self.steered_earlier = steered.get("earlier") or earlier
        self.steered_span_run = self.our_run_id
        self.our_run_id = steered["run"]
        self.turn.run_id = self.our_run_id
        self.conn.register_run_target(
            self.session_key, self.our_run_id, self.owui_origin_chat_id, self.owui_message_id,
            thinking=self.resolved_thinking, request_task_id=(self.__metadata__ or {}).get("task_id"),
            span_run=self.steered_span_run, before=self.steered_replies_before())
        self.conn.seed_run_renderer(self.session_key, self.our_run_id, self.queue, run_text)
        self.renderer.seed(run_text)
        self.assistant_stream_text = run_text.get("stream", "")
        self.fence_item_start = len(self.assistant_stream_text)
        self.event_count = 0

    def steered_replies_before(self) -> list:
        """Earlier replies of the run this message was steered into, in order."""
        earlier = getattr(self, "steered_earlier", None)
        if not earlier:
            return []
        return [*(earlier.get("before") or []), {k: v for k, v in earlier.items() if k != "before"}]

    async def on_owner_row(self, msg):
        """A session row while the reply waits for its interrupted run's owner."""
        step = await self.owner_step(msg.get("row") or {})
        if step is None:
            return
        if step[0] == "end":
            if step[1]:
                note = f"\n\n{step[1]}" if self.visible_message_text.strip() else step[1]
                self.record_visible_chunk(note)
                yield note
                self.text_yielded = True
            self.done = True
        else:
            # Continue in the run that continues this one.
            _kind, new_run, new_queue = step
            self.conn.unregister_consumer(self.session_key, self.our_run_id, queue=self.queue)
            self.queue, self.our_run_id = new_queue, new_run
            self.turn.run_id, self.turn.queue = self.our_run_id, self.queue
            self.admitted_at, self.signals_lost = time.monotonic(), False
        self.awaiting_owner = False
        self.conn.stop_awaiting_owner(self.session_key, self.owner_run)

    async def on_settle_row(self, msg):
        """A session row: the queued status, or the run's end when its final was lost."""
        row = msg.get("row") or {}
        if row.get("status") == "queued" and not self.first_event_arrived \
                and not self.queued_shown:
            self.queued_shown = True
            await _emit_status(
                self.__event_emitter__,
                "Waiting -- your message is queued behind an "
                "active response...",
                done=False,
            )
        # An idle session read after admission means the run is over, whichever
        # run settled last.
        admitted = msg.get("read_at", time.monotonic()) >= self.admitted_at
        if admitted and msg.get("resync"):
            self.signals_lost = True
        tail = None
        if row.get("hasActiveRun") is False and (
                row.get("lastRunId") == self.our_run_id
                or (admitted and self.signals_lost)):
            # Its terminal event never reached this reply; the transcript has it.
            pipe_log(f"  run settled (row status {row.get('status')}, "
                     f"last run {str(row.get('lastRunId'))[:24]}) without its final")
            tail = await self.settled_tail(row)
        elif row.get("hasActiveRun") and msg.get("resync") and admitted:
            # The session is busy, maybe with a later run: only this run's closer says it ended.
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

    def on_restart(self):
        """The Gateway process changed under the run."""
        # A new Gateway process may recover the run or need a resume: its row
        # decides in the turn writer, unless the reply `keeps_its_run`.
        if not self.keeps_its_run and _hand_over_run(self.conn, self.session_key,
                self.our_run_id, thinking=self.resolved_thinking, read_row=True):
            self.handed_over = self.done = self.turn.handed_over = True
        elif self.awaiting_owner:
            spawn_background(self.conn.push_row(self.session_key))
        elif not self.owui_message_id:
            self.await_owner()
        else:
            pipe_log("  Gateway restarted; no message to hand over, waiting")

    def on_gap(self):
        """Events were lost on the Gateway socket."""
        # The turn writer rebuilds the message from the transcript, so hand it
        # over (ELI-147) with what it showed and the queued events. The consumer
        # goes before any await. A reply that `keeps_its_run`, or has no
        # message, keeps streaming and loses what the gap lost.
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
                self.handed_over = self.done = self.turn.handed_over = True
                return
        for m in queued:
            self.queue.put_control(m)
        pipe_log("  event gap (Gateway socket dropped): "
                 "what it lost stays lost in this reply")

    def hand_over_after_kill(self, err, aborted_run) -> bool:
        """True when an outside kill or a shutdown abort took the run out of this reply."""
        # DESIGN 2.7: after an outside kill or a post-shutdown abort, the
        # Gateway or a resume may continue the run, so the turn writer takes the
        # message and the settled row picks the owner. Read the row now: its
        # push can come before this reply reads the kill (ELI-159).
        if _EXTERNAL_KILL_RE.search(err) or (aborted_run and self.conn._shutdown_seen):
            if not self.keeps_its_run and _hand_over_run(self.conn, self.session_key,
                    self.our_run_id, thinking=self.resolved_thinking, read_row=True):
                self.handed_over = self.done = self.turn.handed_over = True
                return True
            if not self.owui_message_id:
                self.await_owner()
                return True
        return False

    async def on_completion(self, payload, stream, phase, state, err, aborted_run):
        """The run's end signals and the error lines they show."""
        if stream == "lifecycle" and phase == "end":
            self.done = True
            pipe_log("  lifecycle end -> done")
        elif err:
            self.error_shown = "\n\n" + _plain_error(ERROR_RUN, err, self.text)
            self.record_visible_chunk(self.error_shown)
            yield self.error_shown
            self.done = True
            pipe_log("  lifecycle error -> done")
        if state == "final" and payload.get("runId") in (None, self.our_run_id):
            usage = (payload.get("message") or {}).get("usage") \
                if isinstance(payload.get("message"), dict) else None
            if _usage_prompt_tokens(usage):
                self.run_usage = usage
        if state in ("final", "cancelled", "error", "aborted") or aborted_run:
            self.done = True
            pipe_log(f"  payload state='{state}' aborted={aborted_run} -> done")
        # A run refused before it started (an inline `/model` not allowed) says
        # why only here (ELI-139).
        run_error = str(payload.get("errorMessage") or "").strip() \
            if state == "error" else ""
        if (run_error and not self.error_shown
                and _thinking_rejection_retry_body(
                    self.body, run_error, self.resolved_thinking) is None):
            self.error_shown = "\n\n" + _plain_error(ERROR_RUN, run_error, self.text)
            self.record_visible_chunk(self.error_shown)
            yield self.error_shown

    async def park(self, stop):
        """OWUI ended this response: park the run."""
        # Stop and Send now both end the response, as a task cancel or a
        # generator close at a yield; both land here. Park first, synchronously:
        # the run goes on with its events buffered, and on Send now continues
        # in this message through the turn writer (ELI-158). The awaits after
        # it may be interrupted themselves.
        self.aborted = True
        grace_s = float(getattr(self.pipe.valves, "INTERRUPT_GRACE_SECONDS", 0) or 0)
        self.turn.parked = True
        self.conn.park_cancelled_turn(
            self.session_key, self.our_run_id, self.queue, self.turn.unsent, grace_s,
            send_stop=self.pipe.valves.SEND_STOP_ON_CANCEL,
            attachments=self.turn.attachments,
            run_text={"received": self.renderer.received,
                      "stream": self.assistant_stream_text,
                      "item": self.renderer.last_item},
            settle=functools.partial(
                _settle_cancel_intent, self.conn, self.session_key, self.our_run_id,
                self.owui_origin_chat_id, self.__event_call__, self.owui_message_id,
                (self.__metadata__ or {}).get("task_id")),
            waiting=self.turn.waiting,
            reply=({"message_id": self.owui_message_id,
                    "renderer": copy.deepcopy(self.renderer)}
                   if self.owui_message_id and not self.keeps_its_run else None),
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

    async def finish_held_prompt(self):
        """The run ended with a needs-input prompt held back: ask it, or show it."""
        if self.pending_prompt_text:
            # The held tail is a full trigger, or the run ended mid-buffer.
            # `maybe_answer_user_input` re-checks; a non-match shows as text.
            answer_res = await self.maybe_answer_user_input(self.pending_prompt_text)
            if answer_res.handled:
                # Answered: the outer `while adopt_new_run` consumes the follow-up
                # with the same loop, not a separate copy.
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
                    self.switch_to_answer_run(answer_res)
                    self.conn.unregister_consumer(self.session_key, self.our_run_id, queue=self.queue)
                    self.queue = answer_res.queue or self.conn.register_consumer(
                        self.session_key, answer_res.new_run_id)
                    self.our_run_id = answer_res.new_run_id
                else:
                    pipe_log("  continuing in the same run after modal answer")
                self.done = False
                self.adopt_new_run = True
            else:
                # Not handled: show the prompt as text, without the directive line.
                fallback = _ask_user_fallback_text(self.pending_prompt_text)
                self.conn.note_turn_rewrite(self.owui_origin_chat_id, self.owui_message_id,
                                       self.pending_prompt_text, fallback)
                if fallback:
                    self.record_visible_chunk(fallback)
                    yield fallback
                    self.text_yielded = True
                self.pending_prompt_text = ""

    async def finish_held_media(self):
        """The run ended with a MEDIA: directive held back: resolve it now."""
        if self.pending_media_text:
            # Nothing more is coming, so resolve it now; failure shows it as text.
            resolved = await self.settle_held_media(self.pending_media_text)
            self.record_visible_chunk(resolved)
            yield resolved
            self.text_yielded = True
            self.pending_media_text = ""

    async def run_end_history(self, fresh: bool = False) -> dict:
        """The session's `chat.history` once the run ended, read once for
        the run's images and its usage line (`fresh`: read again)."""
        if self.end_history is None or fresh:
            # As long as the usage line's other reads (`_build_usage_status_lines`).
            self.end_history = await self.conn.session_history(
                self.session_key, limit=RUN_TEXT_HISTORY_LIMIT, timeout=5)
        return self.end_history

    async def finish_final_images(self):
        """Show the Gateway `image` blocks of the run's stored messages: its
        image tool's (ELI-322) and each path the reply named (ELI-328). Bare-name
        MEDIA: images already showed."""
        if ((self.media_shown and not self.gateway_media) or self.handed_over or not self.our_run_id
                or self.steered_into):
            return
        loop = asyncio.get_running_loop()
        start = loop.time()
        expected = self.images_made + len(self.gateway_media)
        names = {os.path.basename(p) for p in self.gateway_media}

        def stored(hist):
            # A file the Gateway could not store is an `attachment_error` block.
            return (_history_image_blocks_for_run(hist, self.our_run_id),
                    [e for e in _history_media_errors_for_run(hist, self.our_run_id)
                     if os.path.basename(e) in names])
        async def read(fresh):
            return stored(await self.run_end_history(fresh=fresh))
        try:
            (blocks, errors), complete = await self.conn.read_until_stored(
                self.session_key, read, lambda got: len(got[0]) + len(got[1]) >= expected,
                _RUN_IMAGES_MAX_WAIT_S, empty=([], []))
        except Exception as ex:
            pipe_log(f"  run images: chat.history failed: {ex!r}")
            return
        if not complete:
            pipe_log(f"  run images: WARNING: {len(blocks) + len(errors)} of {expected} stored "
                     f"after {_RUN_IMAGES_MAX_WAIT_S:.0f} s; the reply ends without the rest")
        if expected:
            pipe_log(f"  run made {self.images_made} image(s) and named {len(self.gateway_media)} "
                     f"file(s); {len(blocks)} stored after {(loop.time() - start) * 1000:.0f} ms")
        missing = _unstored_gateway_media(sorted(self.gateway_media),
                                          len(blocks) - self.images_made, errors)
        if not blocks and not missing:
            return
        shown = await _gateway_images_text(
            blocks, valves=self.pipe.valves, __request__=self.__request__,
            __event_emitter__=self.__event_emitter__, missing=missing)
        if shown:
            pipe_log(f"  showed {len(blocks)} gateway image block(s), {len(missing)} missing")
            self.record_visible_chunk(shown)
            yield shown
            self.text_yielded = True

    async def close_open_cards(self):
        """Answer every tool card the run left open."""
        # Close every tool card left without a result: OWUI's own sweep does not
        # reliably reach it. A handed-over run's cards must close here too:
        # OWUI's post-stream sweep would run each open `function_call`, get
        # `Error: Tool "Bash" not found.` and send that as a new turn over the
        # writer's reply. The handed renderer keeps the call open.
        if not self.adopt_new_run:
            note = ("(interrupted -- the reply continues when the run resumes)"
                    if self.handed_over else TOOL_NO_RESULT_NOTE)
            for event in self.renderer.close_open_calls(note):
                pipe_log(f"  closing orphaned tool call {event['item']['call_id']}")
                yield event

    @property
    def keeps_its_run(self) -> bool:
        """The message holds more than this run (a Send now takeover, or a
        continued message): the turn writer rebuilds from the run alone, so no
        handover (ELI-153)."""
        return bool(self.adopted_run_id or self._initial_visible_text)

    async def finish(self):
        """After the run: held messages, a queued message taken back, a lost
        tail, the terminal snapshot, the title, and a textless reply's retry."""
        pipe_log(f"DONE -- {self.event_count} events processed, "
                 f"text yielded: {self.text_yielded}")
        if self.steered_into and not self.text_yielded and not self.aborted:
            # A later message was steered in first: the answer is in its reply.
            self.record_visible_chunk(REPLY_SENT_WITH_NEXT_NOTE)
            yield REPLY_SENT_WITH_NEXT_NOTE
            self.text_yielded = True
        if self.adopted_run_id and not self.aborted:
            # The adopted run is over: send the owed messages via the recursive
            # call, seeded with what streamed so far so its snapshot is complete.
            send_texts = list(self.turn.unsent)
            send_attachments = list(self.turn.attachments)
            pipe_log(
                f"  adopted run {self.adopted_run_id[:20]}... finished; sending "
                f"{len(send_texts)} queued message(s)"
            )
            separator = "\n\n---\n\n" if self.visible_message_text.strip() else ""
            if separator:
                self.record_visible_chunk(separator)
                yield separator
            # The recursive reply owns them now.
            waiting, self.turn.waiting = self.turn.waiting, []
            self.turn.unsent = []
            self.turn.attachments = []
            async with contextlib.aclosing(self.pipe._pipe_impl(
                self.body, self.__event_emitter__,
                __event_call__=self.__event_call__, __user__=self.__user__,
                __metadata__=self.__metadata__, __request__=self.__request__,
                __task__=self.__task__, __task_body__=self.__task_body__,
                _initial_visible_text=self.visible_message_text,
                _send_texts=send_texts, _waiting=waiting,
                _send_attachments=send_attachments,
            )) as continuation:
                async for item in continuation:
                    yield item
            return
        if self.handed_over:
            # The turn writer owns the message now (DESIGN 2.6, ELI-147).
            return
        if (self.enqueued_not_started and not self.text_yielded and not self.aborted
                and steers_mid_run_messages(self.conn.gateway_version)):
            # Steered into a run no reply here shows (a wake, another tab's).
            note = "_(Sent to the reply that was already running: the agent answers it there.)_"
            self.record_visible_chunk(note)
            yield note
            self.text_yielded = True
        if (self.enqueued_not_started and not self.text_yielded and not self.aborted
                and not self.adopted_run_id and self.session_runtime == "claude-cli"
                and self.our_run_id and self.sent_texts):
            # A run started between the idle read and the send, and the Gateway
            # queued this message behind it. claude-cli fails a queued turn
            # ("no active tool authority snapshot", ELI-87), so take it back,
            # wait for that run to settle, and send again.
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
                    _send_attachments=self.sent_attachments,
                )) as again:
                    async for item in again:
                        yield item
                return

        if not self.text_yielded:
            pipe_log(
                f"  [diag] PHANTOM (no text): our_run_id={str(self.our_run_id)[:40]} "
                f"first_event_arrived={self.first_event_arrived} aborted={self.aborted}"
            )

        # Terminal snapshot (ELI-57, `save_end_snapshot`): shows when the reply
        # never reaches Open WebUI's own end save, and to a read spanning it
        # (ELI-267). Only at the end: a mid-stream `replace` freezes the stream.
        if not self.aborted and self.text_yielded:
            await self.save_end_snapshot()

        if getattr(self, "steered_span_run", None) and not self.aborted:
            # Only the transcript shows where this message went in on claude-cli.
            spawn_background(_settle_steered_replies(self.conn, self.session_key, [
                *self.steered_replies_before(),
                {"chat_id": self.owui_origin_chat_id, "message_id": self.owui_message_id,
                 "request_task_id": (self.__metadata__ or {}).get("task_id"),
                 "span_run": self.steered_span_run}]))

        if not self.aborted and self.text_yielded:
            spawn_background(
                self.pipe._auto_title(
                    self.body, self.conn, self.visible_message_text, self.owui_origin_chat_id,
                    session_key=self.session_key, user_id=self.owui_origin_user_id,
                    message_id=self.owui_message_id,
                )
            )

        if not self.aborted and not self.text_yielded and self.error_shown:
            # The error is the reply: nothing to recover.
            await self.maybe_emit_snapshot(force=True)
        elif not self.aborted and not self.text_yielded and self.renderer.silenced and any(
                i.get("type") == "function_call" for i in self.renderer.output):
            # Silence after tool calls is on purpose: the cards are the reply (ELI-227).
            pipe_log("  the run answered with silence after its tool cards")
        elif not self.aborted and not self.text_yielded:
            recovered = await self.recover_from_transcript()
            retry_body = _thinking_rejection_retry_body(
                self.body, recovered, self.resolved_thinking)
            if retry_body is not None:
                # Record the ladder the Gateway just named before retrying:
                # describe cannot supply it.
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
                    _send_attachments=self.sent_attachments,
                )) as retry:
                    async for item in retry:
                        yield item
                return
            if recovered:
                pipe_log("  recovered assistant text at final fallback")
                self.record_visible_chunk(recovered)
                yield recovered
                await self.save_end_snapshot()
            else:
                # No text and nothing recovered: show the run's real terminal state.
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
                await self.save_end_snapshot()

    async def _session_run_active(self) -> bool:
        # describe.status flips to 'done' during a subagent-yield gap (ELI-56),
        # so a non-active status is confirmed with sessions.list's hasActiveRun.
        try:
            desc = await self.conn.send_request(
                "sessions.describe", dict(key=self.session_key), timeout=8
            )
            row = desc.get("session") or {}
            status = row.get("status")
            self.session_runtime = (row.get("agentRuntime") or {}).get("id") or self.session_runtime
            # Harvest the ladder from this describe at no extra cost. It is a
            # weak source: with no model catalog it overreports (all 8 levels
            # for claude-cli/claude-opus-5, real ladder ["off"]). Good for the
            # dropdown, not for clamping once the Gateway has ruled.
            self.session_model_key = _session_model_key(row)
            ladder = _session_thinking_ladder(desc)
            if ladder:
                self.session_thinking_ladder = ladder
                if (self.session_model_key, ladder) != self._ladder_recorded:
                    # Marked only once cached, so a failed write is retried.
                    if _record_thinking_ladder(
                            ladder, model_key=self.session_model_key,
                            state_dir=getattr(self.pipe.valves, "STATE_DIR", "")):
                        self._ladder_recorded = (self.session_model_key, ladder)
            # There is no `activeRunId`.
            if row.get("hasActiveRun") or row.get("activeRunIds") or status in _ACTIVE_RUN_STATES:
                return True
        except Exception as ex:
            # Fall back to the list probe, then to the per-worker registry.
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
        if self.conn.reconnecting:
            # Neither probe reached the Gateway, and the registry would say idle
            # for a run the turn writer holds, sending into a dead socket
            # (ELI-199). Wait for the reconnect, then read again.
            pipe_log("  active-check: the Gateway socket is reconnecting; "
                     "waiting for it before deciding")
            await self.conn.ensure_connected()
            return await self._session_run_active()
        return self.conn.active_run_id_for_session(self.session_key) is not None

    def await_owner(self) -> None:
        self.awaiting_owner, self.owner_notice_before = True, self.conn._stop_notice_seen
        self.owner_run = self.conn._awaiting_owner[self.session_key] = self.our_run_id
        pipe_log(f"  run {self.our_run_id[:24]}... interrupted, no message to hand over: "
                 "its row decides here")
        spawn_background(self.conn.push_row(self.session_key))

    async def owner_step(self, row: dict):
        """One row while waiting for the run's owner (DESIGN 2.7): ("switch",
        run, queue), ("end", note), or None to wait."""
        decision = next_continuation(row, self.owner_notice_before, self.owner_resumes)
        if decision == "gateway":
            # Recovered as a run whose turn names this one (rule E); until it is
            # committed, the next row looks again.
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
            return ("end", _plain_error(ERROR_RUN, row["lastRunError"], self.text))
        return ("end", None)

    async def own_run_usage(self) -> dict | None:
        """The run's last-call usage: from its `chat` final, else its transcript
        messages (the final may come after the reply ended)."""
        if self.run_usage or not self.our_run_id:
            return self.run_usage
        try:
            hist = await self.run_end_history()
        except Exception as ex:
            pipe_log(f"  usage line: chat.history failed: {ex!r}")
            return None
        usage = _history_usage_for_run(hist, self.our_run_id)
        if not usage:
            pipe_log("  usage line: the run's messages report no usage; using the session row")
        return usage

    async def recover_from_transcript(self) -> str | None:
        """The run's text read back by run id, else by user text (only for a run
        seen here: otherwise the last reply may be another run's)."""
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
        """What this reply still owes once its run settled with no final here:
        the text after what arrived, or why nothing showed. `closer_only`:
        only this run's own closer counts; None without one."""
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
            return _plain_error(ERROR_RUN if err else ERROR_UNFINISHED, err, self.text)
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

    async def save_end_snapshot(self):
        """Save the reply as it stands before Open WebUI's end save, cards
        included via the live output copy (ELI-267), else text alone."""
        output = await _live_output(self.__request__, self.owui_origin_chat_id, self.owui_message_id)
        if output is None:
            await self.maybe_emit_snapshot(force=True)
            return
        await _owui_upsert_message(self.owui_origin_chat_id, self.owui_message_id,
                                   {"content": self.visible_message_text, "output": output})
        self.last_snapshot_text = self.visible_message_text
        self.last_snapshot_time = time.time()

    async def maybe_answer_user_input(self, prompt_text: str) -> UserInputResult:
        """Ask via an OWUI modal and deliver the answer (see UserInputResult).
        An answer steered into the same run still returns handled=True.
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
            )
        except Exception as ex:
            pipe_log(f"  user input modal failed; falling back to chat prompt: {ex}")
            answer = None
        if answer is None:
            pipe_log("  user input modal cancelled or empty answer")
            # The agent is told once per reply that the secret never came.
            if not _is_secret_prompt(prompt_text) or self.secret_not_received_sent:
                return UserInputResult(False, None)
            self.secret_not_received_sent = True
            answer = SECRET_NOT_RECEIVED_FOR_AGENT
        else:
            pipe_log("  user input answered via OWUI modal")
        await _emit_status(self.__event_emitter__, "Sending answer...", done=False)
        # The answer continues this reply: its run id names it (ELI-100 2.2).
        idempotency_key = (pipe_run_id(self.chat_id, self.owui_message_id, "input") if self.owui_message_id
                           else f"user-input-{self.chat_id}-{time.time()}")
        # Listen before sending: with no consumer, early events would open a
        # second writer on this reply's message.
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
            # Not handled, so the caller shows the prompt text.
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
        """Continue the reply in the answer run."""
        if answer_res.queue is not None:
            self.conn.mark_delivered_live(self.session_key, self.our_run_id)
        else:
            self.conn.continue_run_target(self.session_key, self.our_run_id, answer_res.new_run_id)

    async def maybe_show_live_view(self, tool_name, args):
        """Embed LIVE_VIEW_URL once, at the first browser tool call."""
        if getattr(self, "live_view_shown", False) or not self.__event_emitter__:
            return
        meta = self.__metadata__ or {}
        url = _live_view_url(
            self.pipe.valves, chat_id=self.owui_origin_chat_id, message_id=meta.get("message_id"),
            user_id=(self.__user__ or {}).get("id"), email=(self.__user__ or {}).get("email"),
            tool_name=tool_name, args_str=json.dumps(args or {}))
        if not url:
            return
        self.live_view_shown = True
        try:
            await self.__event_emitter__({"type": "embeds", "data": {"embeds": [url]}})
            pipe_log(f"  live view embed added: {url}")
        except Exception as ex:
            pipe_log(f"  live view embed failed: {ex!r}")

    async def show_text(self, text: str):
        """New run text through the reply's text hooks: held while it may open
        a needs-input prompt or a `MEDIA:` filename is arriving."""
        # In an open fence a marker is a quote (ELI-107). A fence never spans items.
        shown = self.assistant_stream_text[self.fence_item_start:] + self.pending_media_text
        text, self.pending_prompt_text = _advance_input_prompt_buffer(
            self.pending_prompt_text, text, _fence_open_after(shown),
            at_line_start=not shown or shown.endswith("\n"))
        if not text:
            return
        # A split directive is held until complete; one in code is text (ELI-111).
        text, self.pending_media_text = _advance_media_buffer(
            self.pending_media_text, text, self.assistant_stream_text[self.fence_item_start:])
        if not text:
            return
        self.text_yielded = True
        shown_as = text
        if "MEDIA:" in text:
            shown_as = await self.settle_held_media(text)
        self.assistant_stream_text += text
        self.record_visible_chunk(shown_as)
        yield shown_as
        # No `replace` per delta: a mid-stream one freezes the visible stream.
        if shown_as is not text:
            await self.maybe_emit_snapshot()

    async def settle_held_media(self, held: str) -> str:
        """Resolve held `MEDIA:` text once its message item ends; one in code
        stays text (ELI-111). A path directive is dropped: its image is the
        run's stored block (ELI-328)."""
        ctx = self.assistant_stream_text[self.fence_item_start:]
        text, paths = _take_gateway_media(held, ctx)
        self.gateway_media.update(paths)
        resolved, handled = await _resolve_media_text(
            text,
            valves=self.pipe.valves,
            __request__=self.__request__,
            __event_emitter__=self.__event_emitter__,
            ctx=ctx,
        )
        if handled:
            pipe_log("  resolved MEDIA: directive")
            self.media_shown = True
        if resolved != held:
            # A writer that takes this reply over shows the same.
            self.conn.note_turn_rewrite(self.owui_origin_chat_id, self.owui_message_id, held, resolved)
        return resolved
