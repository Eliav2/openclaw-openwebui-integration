# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The turn writer (ELI-100, DESIGN-agent-turns.md 2.3-2.5)
# ---------------------------------------------------------------------------
# One writer per run this connection renders outside a live `pipe()` request:
# agent turns (cron wake, sessions_send, sub-agent announce, restart recovery)
# and user turns whose request ended before the run. It owns exactly one
# message, the one turn identity names, and registers itself with Open
# WebUI's task registry so the page treats the chat as generating and its
# Stop button reaches it.


async def _set_turn_meta(chat_id: str, message_id: str, meta: dict) -> bool:
    """Merge `meta` into message `message_id` under the chat's row lock."""
    from open_webui.models.chats import Chats
    from sqlalchemy.orm.attributes import flag_modified

    async with Chats._chat_transaction(chat_id) as (db, chat):
        data = chat.chat or {}
        history = data.get("history") or {}
        msg = (history.get("messages") or {}).get(message_id)
        if msg is None:
            return False
        msg["meta"] = {**(msg.get("meta") or {}), **meta}
        chat.chat = {**data, "history": history}
        flag_modified(chat, "chat")
        return True


async def _introduce_turn_message(chat_id: str, user_id: str, messages: dict, current_id: str) -> None:
    """Show a newly claimed message in every open tab of the chat without a
    reload: the shared `chat:messages` event to room `chat:<id>`, which the
    page merges and moves its view to (Open WebUI emits the same for its own
    turns; measured live as M1)."""
    from open_webui.socket.main import sio

    await sio.emit("events", {
        "chat_id": chat_id, "message_id": current_id, "user_id": user_id, "shared": True,
        "data": {"type": "chat:messages", "data": {"messages": messages, "currentId": current_id}},
    }, room=f"chat:{chat_id}")


def _owui_redis():
    """Open WebUI's redis client, or None in a single-process install
    (`create_task` handles both)."""
    app = getattr(sys.modules.get("open_webui.main"), "app", None)
    return getattr(getattr(app, "state", None), "redis", None)


TURN_CUT_OFF_NOTE = ("_(This reply was cut off: the run was interrupted again after "
                     f"{MAX_TURN_RESUMES} attempts to continue it.)_")


def _run_prompt_text(span: list | None) -> str:
    """The needs-input prompt a run's text ends with, as the renderer shows
    it (`_prompt_tail`); "" when there is none."""
    return _prompt_tail(_last_item_text(_history_renderer(span))) if span else ""


def _last_item_text(renderer: "_TurnRenderer") -> str:
    """The text of the run's last message item, the only one a closing
    question can be in; fences never span items (review 2af1 F4). "" when
    the run ended on a tool call."""
    last = renderer.output[-1] if renderer.output else None
    if last is None or last.get("type") != "message":
        return ""
    return ((last.get("content") or [{}])[0].get("text")) or ""


def _prompt_tail(text: str, start: int = 0) -> str:
    """`text` from its last needs-input trigger line at or after `start` on,
    the prompt the inline reply would ask; "" when there is none. A trigger
    line in a code fence is a quote (ELI-107)."""
    if not text:
        return ""
    last = None
    while True:
        at = _marker_line_index(text[start:], _fence_open_after(text[:start]))
        if at is None:
            break
        last = start + at
        start = text.find("\n", last) + 1
        if not start:
            break
    return text[last:] if last is not None else ""


RESUME_DIRECTIVE_PREFIX = "[system] Your previous "


# A request longer than this is quoted as its head and tail (review 2af1 F2).
RESUME_REQUEST_MAX = 6000
RESUME_REQUEST_HEAD = 2000
# OpenClaw's inbound metadata blocks a stored user turn can start with
# ("Conversation info (untrusted metadata):" and a fenced JSON body).
_INBOUND_META_RE = re.compile(
    r"\A\s*[A-Z][^\n]{0,80}\(untrusted metadata\):[ \t]*\n```[^\n]*\n.*?\n```[ \t]*(?:\n+|\Z)", re.S)


def _strip_inbound_meta(text: str) -> str:
    """`text` without the metadata blocks in front of it (review 2af1 F5)."""
    while True:
        m = _INBOUND_META_RE.match(text or "")
        if not m:
            return text
        text = text[m.end():]


def _quote_block(text: str) -> str:
    """`text` between two delimiter lines that occur in it as no line: a run
    of dashes longer than any dash-only line of its own (review 2af1 F2b)."""
    dashes = [len(x.strip()) for x in text.split("\n") if x.strip() and set(x.strip()) == {"-"}]
    delim = "-" * max([3] + [n + 1 for n in dashes])
    return f"{delim}\n{text}\n{delim}"


def _clip_middle(text: str, limit: int = RESUME_REQUEST_MAX, head: int = RESUME_REQUEST_HEAD) -> str:
    """`text` within `limit` characters: its head and tail, the question a
    long request usually ends with included, and a line for what is left
    out (review 2af1 F2)."""
    if len(text) <= limit:
        return text
    tail = limit - head
    return f"{text[:head]}\n[... {len(text) - limit} characters omitted ...]\n{text[-tail:]}"


def _resume_directive(shown: str | None, request: str | None = None,
                      attachments: int = 0) -> str:
    """The message a resume sends: continue from what the user already sees.

    A resume follows an outside kill (the owner rule), which also kills any
    tool call still running. Its result then reads like the command failing
    (`Exit code 137`), and a resume that does not say why stopped there and
    asked the user what to do (M3 window, 2026-10-13: 4 of 4).

    `request`: the user's message the run was answering. The killed run's
    claude-cli session is not continued: the resume runs in a new one that
    holds only this message (M3 window run 2, 2026-10-14: "I don't have the
    rest of your sequence"), so the request travels in it. `attachments`:
    how many images or files it carried, which do not travel (F3)."""
    cause = (
        "The Gateway restarted or your process was killed from outside while you "
        "were working. A tool call that was still running was killed by that, so "
        "its result (such as exit code 137 or 143) says nothing about the command "
        "itself: rerun it if the request still needs it.")
    request = (request or "").strip()
    if request:
        cause += (" The request you were answering, which this session may no longer "
                  f"hold, was:\n\n{_quote_block(_clip_middle(request))}\n")
    if attachments:
        cause += (f" The request also carried {attachments} attachment"
                  f"{'s' if attachments > 1 else ''} (an image or a file) that this session "
                  "cannot see; if the request needs it, ask the user to send it again.")
    shown = (shown or "").strip()
    if shown:
        return (
            f"[system] Your previous reply was cut off partway through. {cause} The "
            "user can already see the following partial text, and it cannot be edited "
            f"or retracted:\n\n{_quote_block(shown[-1500:])}\n\n"
            "Continue that reply seamlessly from exactly where it stops and finish the "
            "request. Do not repeat any of it, do not restart the answer, and do not "
            "mention the interruption.")
    return (
        f"[system] Your previous run was cut off before you produced any output. {cause} "
        "Answer the most recent message now and finish the request. Do not mention "
        "the interruption.")


def _is_sentinel_message_item(item: dict) -> bool:
    if item.get("type") != "message":
        return False
    text = "".join(c.get("text", "") for c in item.get("content") or [])
    return text.strip() in _SILENT_SENTINELS


def _turn_note_item(note: str) -> dict:
    return {"type": "message", "id": f"msg_note_{uuid.uuid4().hex[:12]}", "status": "completed",
            "role": "assistant", "content": [{"type": "output_text", "text": note}]}


def _output_item_boundary(sent: list, current: list) -> bool:
    """True when `current` has a new item, or an item other than a growing
    message text changed (a tool result, a finished card)."""
    if len(current) != len(sent):
        return True
    for i, (old, new) in enumerate(zip(sent, current)):
        if old == new:
            continue
        growing_text = (i == len(current) - 1 and new.get("type") == "message"
                        and old.get("id") == new.get("id"))
        if not growing_text:
            return True
    return False


# Open WebUI cancels a response identically for Stop and for Send now (both
# call stopTasksByChatId; the message queue exists only in the browser), so
# the backend can never tell them apart. The page can: this hook, installed
# once per page through an `execute` event, records per chat which one the
# user pressed. Labels are OWUI's English strings (they are localized, so a
# non-English UI falls back to the timer); Escape is OWUI's Stop shortcut,
# and Enter in the composer while a response runs is an interrupt when the
# message queue is off (with it on, a later Stop or Send now click
# overwrites it).
_INTENT_HOOK_JS = r"""
if (!window.__ocIntentHook) {
  window.__ocIntentHook = true;
  window.__ocIntents = {};
  const chat = () => (location.pathname.match(/\/c\/([^/?#]+)/) || [])[1] || null;
  const mark = (kind) => { const c = chat(); if (c) window.__ocIntents[c] = { kind, t: Date.now() }; };
  // The Stop button's aria-label is translated; its icon is not (R15).
  const STOP_ICON = 'M2.25 12c0-5.385 4.365-9.75 9.75-9.75s9.75';
  const isStop = (b) => b.getAttribute('aria-label') === 'Stop' || b.id === 'stop-response-button'
    || [...b.querySelectorAll('svg path')].some((p) => (p.getAttribute('d') || '').startsWith(STOP_ICON));
  const running = () => [...document.querySelectorAll('button')].some(isStop);
  document.addEventListener('click', (e) => {
    const b = e.target && e.target.closest ? e.target.closest('button') : null;
    if (!b) return;
    // Send now needs no mark: an unknown intent already keeps writing.
    if (b.getAttribute('aria-label') === 'Send now') mark('send_now');
    else if (isStop(b)) mark('stop');
  }, true);
  document.addEventListener('keydown', (e) => {
    if (!running()) return;
    if (e.key === 'Escape') { mark('stop'); return; }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.target && e.target.closest
        && e.target.closest('#chat-input')) mark('send_now');
  }, true);
}
return true;
"""


# Open WebUI holds a message sent while a reply runs in a queue above the
# composer, in the browser only, until the reply ends or Send now is clicked.
# In an OpenClaw chat with AUTO_SEND_WHILE_RUNNING on, this hook clicks Send
# now for the user as soon as a queued message appears, so the message lands
# in the chat at once. The click is marked as Send now for the intent query,
# so the run is kept, never aborted. The button is found by its icon, which
# Open WebUI draws only there and does not translate. One click at a time: a
# click is in flight until a new user message is the chat's last (Open WebUI
# ignores a Send now while it is still submitting the previous one).
_AUTO_SEND_HOOK_JS = r"""
if (!window.__ocAutoSendHook) {
  window.__ocAutoSendHook = true;
  window.__ocAutoSend = window.__ocAutoSend || {};
  const SEND_NOW_ICON = 'M4.5 10.5 12 3m0 0 7.5 7.5M12 3v18';
  const chat = () => (location.pathname.match(/\/c\/([^/?#]+)/) || [])[1] || null;
  const lastUser = () => {
    const all = document.querySelectorAll('.user-message[id^="message-"]');
    return all.length ? all[all.length - 1].id : null;
  };
  let inFlight = null;
  const sendQueued = () => {
    const c = chat();
    if (!c || !window.__ocAutoSend[c]) return;
    if (inFlight && inFlight.chat === c && inFlight.lastUser === lastUser()) return;
    inFlight = null;
    const path = document.querySelector(`button:not([disabled]) path[d="${SEND_NOW_ICON}"]`);
    const b = path && path.closest('button');
    if (!b) return;
    inFlight = { chat: c, lastUser: lastUser() };
    window.__ocIntents = window.__ocIntents || {};
    window.__ocIntents[c] = { kind: 'send_now', t: Date.now() };
    b.click();
  };
  new MutationObserver(sendQueued).observe(document.body, {
    childList: true, subtree: true, attributes: true, attributeFilter: ['disabled'] });
  window.__ocAutoSendScan = sendQueued;
}
"""


def _page_hooks_js(chat_id: str | None, auto_send: bool) -> str:
    """The page hooks an OpenClaw chat's tab gets: the Stop / Send now intent
    hook and the auto-send hook, with this chat's auto-send setting."""
    code = "(function () {" + _INTENT_HOOK_JS + "})();\n(function () {" + _AUTO_SEND_HOOK_JS + "})();\n"
    if chat_id:
        code += ("window.__ocAutoSend[" + json.dumps(chat_id) + "] = " + ("true" if auto_send else "false")
                 + ";\nwindow.__ocAutoSendScan();\n")
    return code


def _auto_send_on(valves) -> bool:
    return bool(getattr(valves, "AUTO_SEND_WHILE_RUNNING", True))


def _intent_query_js(chat_id: str) -> str:
    """Read and clear what the page recorded for `chat_id` (see
    `_INTENT_HOOK_JS`). "none" means the hook is there but saw no click, so
    the cancel came from elsewhere; null means no hook at all."""
    return (
        "const c = " + json.dumps(chat_id) + ";"
        "const all = window.__ocIntents;"
        "if (!all) return null;"
        "const i = all[c]; delete all[c];"
        "return (i && Date.now() - i.t < 60000) ? i.kind : 'none';"
    )


# The page's answer to an intent query is an RPC reply; this bounds a tab that
# never answers, as any RPC is bounded (not a wait for anything; ELI-95).
_INTENT_QUERY_TIMEOUT_S = 3.0


def _intent_answer(resp) -> str | None:
    if isinstance(resp, str):
        return resp.strip() or None
    if isinstance(resp, dict) and "error" not in resp:
        for key in ("value", "result", "text"):
            if isinstance(resp.get(key), str):
                return resp[key].strip() or None
    return None


async def _page_cancel_intent(user_id: str, chat_id: str, message_id: str) -> str | None:
    """Which button cancelled the writer's task: "stop", "send_now", or None
    (no tab of this user shows the chat, no hook, or no click recorded). Asks
    every tab showing the chat (OWUI's SESSION_POOL[sid]["chat_ids"]); a
    Stop anywhere wins."""
    try:
        from open_webui.socket import main as socket_main
    except Exception:
        return None
    pool = getattr(socket_main, "SESSION_POOL", None) or {}
    sids = [sid for sid, sess in list(pool.items())
            if isinstance(sess, dict) and sess.get("id") == user_id
            and chat_id in (sess.get("chat_ids") or [])]
    answers = set()
    for sid in sids:
        try:
            resp = await socket_main.sio.call("events", {
                "chat_id": chat_id, "message_id": message_id,
                "data": {"type": "execute", "data": {"code": _intent_query_js(chat_id)}},
            }, to=sid, timeout=_INTENT_QUERY_TIMEOUT_S)
        except Exception as ex:
            pipe_log(f"  turn writer: intent query to {sid[:8]}... failed: {ex!r}")
            continue
        answers.add(_intent_answer(resp))
    for kind in ("stop", "send_now"):
        if kind in answers:
            return kind
    return None


class _TurnWriter:
    """Writes run `run_id` into message `message_id` of chat `chat_id`."""

    def __init__(self, conn: "_GatewayConnection", *, session_key: str, chat_id: str,
                 user_id: str, run_id: str, message_id: str | None,
                 takeover: bool = False, renderer: "_TurnRenderer | None" = None,
                 thinking: str | None = None, wake: dict | None = None, shown: str = ""):
        self.conn = conn
        self.session_key, self.chat_id, self.user_id = session_key, chat_id, user_id
        self.run_id, self.message_id = run_id, message_id
        # A pipe run's message already exists (Open WebUI's stub): the writer
        # takes it over instead of inserting one (DESIGN 2.6).
        self.takeover = takeover
        self.renderer = renderer if renderer is not None else _TurnRenderer()
        # A user turn's level (ELI-85): its resume continues the same reply.
        self.thinking = thinking
        # A cron wake (ELI-103): {key, at, job} of its turn, and `end` once its
        # terminal arrived. Its transcript span is a time window, not a run id.
        self.wake = wake
        # What earlier replies showed of a run M continues (an adopted run,
        # ELI-147): the transcript rebuild of M starts after it.
        self.shown = {run_id: shown} if shown else {}
        self.opened = False
        self.task_id: str | None = None
        # every run written into M, in transcript order (_adopt_recoveries_from)
        self.runs = [run_id]
        # Where each run's streamed part starts in the renderer, in adoption
        # order: (run, output index, text length). See `_from_transcript`.
        self.run_marks: list = [(run_id, 0, 0)]
        self.resumes = 0                # resume budget, counted on M (MAX_TURN_RESUMES)
        self.pending_resume = False     # a resume the Gateway refused; sent on the next hello-ok
        self.request: str | None = None  # the user's message M answers, quoted by a resume
        self.request_attachments = 0    # its images or files, which a resume cannot carry
        self.awaiting_row = False       # kept open for a Gateway recovery (R2)
        self.closed = False             # nothing more to write (ended, or M is someone else's)
        self.sent_output: list = []     # the `output` open tabs hold
        if renderer is not None:
            # A handed renderer is what the request showed: open tabs hold
            # those items, but under Open WebUI's ids, so an `added` would
            # splice in a second copy. Unknown items make the first diff
            # replace each one in place, by index (ELI-158).
            self.sent_output = [{"type": "_shown_by_request"} for _ in renderer.output]
        self._media_failed: set = set()  # MEDIA directives that did not resolve
        self._emit = None
        self._done = asyncio.Event()
        # Two pumps feed one writer when a recovery continues M; only one may
        # claim it.
        self._open_lock = asyncio.Lock()

    async def open(self, takeover: bool = False) -> bool:
        """Claim the message, introduce it to open tabs and register the
        writer's task. False when there is nothing to open (a run that renders
        nothing) or another live writer holds the message."""
        async with self._open_lock:
            return await self._open(takeover)

    async def _open(self, takeover: bool) -> bool:
        if self.opened:
            return True
        if not self.message_id:
            return False
        _register_turn_writer(self.conn.writer_id)
        claim = await _claim_turn_message(self.chat_id, self.user_id, self.message_id,
                                          self.run_id, self.conn.writer_id, takeover=takeover)
        if not claim["claimed"]:
            pipe_log(f"  turn writer: message {self.message_id[:8]}... is held by another writer")
            return False
        if claim["how"] == "new":
            await _introduce_turn_message(self.chat_id, self.user_id, claim["messages"],
                                          self.message_id)
        from open_webui.tasks import create_task

        self.task_id, _ = await create_task(_owui_redis(), self._hold(), id=self.chat_id)
        meta = {"task_id": self.task_id}
        if self.wake is not None:
            # What a successor needs to adopt this message (ELI-103 step 4).
            meta["openclaw_wake"] = {k: self.wake.get(k) for k in ("key", "at", "job")}
        await _set_turn_meta(self.chat_id, self.message_id, meta)
        self.opened = True
        try:
            # Case 8: lets a cancel of this task ask the page whether it was
            # Stop or Send now. Idempotent in the page.
            await (await self._emitter())({"type": "execute", "data": {
                "code": _page_hooks_js(self.chat_id, _auto_send_on(self.conn._valves()))}})
        except Exception as ex:
            pipe_log(f"  turn writer: intent hook install failed: {ex!r}")
        pipe_log(f"  turn writer: opened {self.message_id[:8]}... ({claim['how']}) for run "
                 f"{self.run_id[:24]}... in chat {self.chat_id[:8]}...")
        return True

    def _showable(self) -> bool:
        """Condition B: a tool call start, or text that is not a silent
        sentinel or a prefix of one. Whichever comes first."""
        if any(i.get("type") == "function_call" for i in self.renderer.output):
            return True
        return _relay_content_is_showable(self.renderer.visible_text or "")

    async def feed(self, payload: dict | None) -> None:
        """One event of the run: grow the renderer, open the message on the
        first showable item, then stream what changed. None streams what the
        renderer already holds (a handover's shadow renderer)."""
        if self.closed:
            return
        try:
            if payload is not None:
                self.renderer.feed(payload)
        except Exception as ex:
            pipe_log(f"  turn writer: renderer feed failed: {ex}")
        if not self.opened:
            if not self._showable():
                return
            if not await self.open(takeover=self.takeover):
                self.closed = True
                return
        await self._stream()

    async def _emitter(self):
        if self._emit is None:
            from open_webui.socket.main import get_event_emitter

            # update_db=False: the writer owns this message's DB writes.
            self._emit = await get_event_emitter(
                {"chat_id": self.chat_id, "message_id": self.message_id,
                 "user_id": self.user_id}, update_db=False)
        return self._emit

    async def _stream(self) -> None:
        """Emit the output change as Responses events (Open WebUI forwards a
        throttled shared copy to the chat room), keep the response stream
        current for a tab that opens the chat mid-turn, and write the DB on
        item boundaries (a new item, a tool result), not per text delta."""
        output, text = await self._view(final=False)
        emit = await self._emitter()
        for ev in _relay_output_events(self.sent_output, output):
            await emit({"type": "response:completion", "data": ev})
        boundary = _output_item_boundary(self.sent_output, output)
        self.sent_output = copy.deepcopy(output)
        from open_webui.tasks import save_response_stream

        await save_response_stream(_owui_redis(), self.task_id, self.chat_id, self.message_id,
                                   text, self.sent_output)
        if boundary:
            await self._persist(done=False, output=output, text=text)

    def _rewrite(self, text: str) -> str:
        for raw, shown in self.conn.turn_rewrites(self.chat_id, self.message_id or ""):
            if raw not in text:
                continue
            # An image this writer recorded for one directive is not pasted
            # over the same directive quoted in code (ELI-111).
            if _media_directives(raw, True) == [raw]:
                text = _replace_directive(text, raw, shown)
            else:
                text = text.replace(raw, shown)
        # A prompt nobody answered here shows as the inline reply's fallback
        # does: without its internal directive line (the writer side of
        # ELI-58), a quote in a code fence kept (ELI-107).
        return _strip_needs_input_lines(text)

    def _note_transcript_answers(self, messages: list) -> None:
        """An answered ask-user prompt is a card, rebuilt from the transcript
        when the inline reply's own rewrite is gone (an Open WebUI restart,
        a known gap of PR #20): the prompt ends the text of the run before
        the answer run, and the answer is the answer run's own user turn. A
        rewrite the reply recorded is kept as it was."""
        if not self.message_id:
            return
        recorded = {raw for raw, _ in self.conn.turn_rewrites(self.chat_id, self.message_id)}
        hist, prev = {"messages": messages}, None
        for run, _tool in _history_user_turns(messages):
            if run_message_id(self.session_key, self.chat_id, run, messages) != self.message_id:
                continue
            parsed = parse_pipe_run_id(run)
            if prev is not None and parsed and parsed["kind"] == "input":
                prompt = _run_prompt_text(_history_span_messages(hist, prev))
                answer = next((_strip_inbound_meta((_history_item(m) or {}).get("text") or "").strip()
                               for m in messages if _history_user_run_id(m) == run), "")
                if prompt and answer and prompt not in recorded:
                    self.conn.note_turn_rewrite(self.chat_id, self.message_id, prompt,
                                                _ask_user_detail_block(prompt, answer))
            prev = run

    async def _view(self, *, final: bool) -> tuple[list, str]:
        """(output, text) of M as the inline reply shows a run (review 2af1
        R4): the rewrites it recorded (an image for a `MEDIA:` directive, a
        card for an answered ask-user prompt) applied to the renderer, a
        directive that first came after the handover resolved here, and,
        while the run streams, a directive whose filename is still arriving
        held back, as the inline reply holds it."""
        output = self.renderer.finished_output() if final else copy.deepcopy(self.renderer.output)
        items = [i for i in output if i.get("type") == "message" and i.get("content")]
        # Each message item is read on its own, as the inline reply reads
        # it: an item ends a directive, so the text after a tool card does
        # not run into the filename before it (ELI-147).
        for item in items:
            await self._resolve_media(self._rewrite(item["content"][0].get("text") or ""),
                                      final or item is not output[-1])
        def show(text: str, tail: bool) -> str:
            text = self._rewrite(text)
            return _advance_media_buffer("", text)[0] if tail and not final else text
        for n, item in enumerate(output):
            if item in items:
                part = item["content"][0]
                part["text"] = show(part.get("text") or "", n == len(output) - 1)
        if not items:
            return output, show(self.renderer.text, True)
        return output, "".join(i["content"][0]["text"] for i in items)

    async def _resolve_media(self, text: str, final: bool) -> None:
        """Upload each complete `MEDIA:` directive the inline reply never saw,
        once, the way it does (`_resolve_media_text`: Open WebUI's files, else
        the file server), and record the result as a rewrite."""
        if "MEDIA:" not in text:
            return
        for directive in _media_directives(text, final):
            if directive in self._media_failed:
                continue
            try:
                shown, handled = await _resolve_media_text(
                    directive, valves=self.conn._valves(), __event_emitter__=await self._emitter())
            except Exception as ex:
                pipe_log(f"  turn writer: {directive[:60]} did not resolve: {ex!r}")
                shown, handled = directive, False
            if handled and shown != directive:
                self.conn.note_turn_rewrite(self.chat_id, self.message_id, directive, shown)
            else:
                self._media_failed.add(directive)

    async def _persist(self, *, done: bool, output: list | None = None,
                       text: str | None = None) -> None:
        from open_webui.models.chats import Chats

        try:
            await Chats.upsert_message_to_chat_by_id_and_message_id(
                self.chat_id, self.message_id,
                {"content": self.renderer.text if text is None else text,
                 "output": output if output is not None else copy.deepcopy(self.renderer.output),
                 "done": done})
        except Exception as ex:
            pipe_log(f"  turn writer: persist failed: {ex}")

    def adopt(self, run_id: str) -> None:
        """A run that continues M (a pipe resume, or a restart recovery that
        rule E maps here) is written into the same message."""
        if run_id not in self.runs:
            self.runs.append(run_id)
            self.run_marks.append((run_id, len(self.renderer.output), len(self.renderer.text)))
            self.renderer.run_boundary = True
        self.run_id = run_id

    def _adopt_recoveries_from(self, messages: list) -> None:
        """Runs written into M whose events never reached this writer: restart
        recoveries that rule E maps to M (they ran while the socket was down,
        case 4), and pipe runs naming M, such as the question run an ask-user
        answer run continues after an Open WebUI restart left no target
        (review 2af1 R9). M's runs are put in transcript order."""
        order = []
        for run, tool in _history_user_turns(messages):
            if run in self.runs or (
                    (tool == RECOVERY_SOURCE_TOOL or parse_pipe_run_id(run) is not None)
                    and run_message_id(self.session_key, self.chat_id, run, messages) == self.message_id):
                order.append(run)
        self.runs = [*order, *(r for r in self.runs if r not in order)]

    async def end(self, row: dict, stop_notice_before: bool = False) -> None:
        """The current run settled; `row` is its settled session row (DESIGN
        2.5, 2.7). Keep M open for a recovery or a busy session, resume an
        outside kill, or finalize."""
        if self.closed:
            return
        decision = next_continuation(row, stop_notice_before, self.resumes)
        if decision in ("gateway", "wait"):
            # The pump stays (R2): the recovery run takes M over (rule E), or a
            # later row leaves `running`. A recovery the Gateway gives up on
            # tombstones the row to failed (main-session-restart-recovery-
            # CPJjd8da.mjs:1150), which is that row.
            self.awaiting_row = True
            pipe_log(f"  turn writer: {self.message_id and self.message_id[:8]}... stays open "
                     f"({decision}) after run {self.run_id[:24]}...")
            return
        self.awaiting_row = False
        if decision == "resume":
            self.resumes += 1
            self.pending_resume = True
            await self._send_resume()
            return
        note = None
        if turn_owner(row, stop_notice_before) == "pipe":
            note = TURN_CUT_OFF_NOTE
        elif (row or {}).get("status") in ("failed", "timeout") and row.get("lastRunError"):
            note = f"**Error:** {row['lastRunError']}"
        if note is None and await self._ask_question():
            return
        await self.finalize(note)

    async def _ask_question(self) -> bool:
        """A run that ends with a question asks it as the inline reply does:
        Open WebUI's question card on the tab viewing the chat (ELI-107
        decision 1, option 1). One card call, bounded by its own timeout, so
        M waits for no tab. The answer is sent as an `input` run that this
        writer adopts, and the question shows as its Ask User card. False
        when nothing was sent: no question, no tab on the chat, a cancel, a
        timeout, or a refused send; M then ends with the question as text."""
        if self.closed or not self.opened:
            return False
        # Only a question in the run's last message item (fences are per
        # item), after the last one already answered in M: the text of an
        # answer run follows its question's raw text.
        text = _last_item_text(self.renderer)
        start = 0
        for raw, _ in self.conn.turn_rewrites(self.chat_id, self.message_id):
            at = text.rfind(raw)
            if at >= 0:
                start = max(start, at + len(raw))
        prompt = _prompt_tail(text, start)
        if not prompt or not _is_user_input_prompt(prompt):
            return False
        if await self._ask_card(prompt):
            return True
        # Not answered here: M ends with the question as the inline reply's
        # fallback shows it (a JSON form as readable text, ELI-142).
        self.conn.note_turn_rewrite(self.chat_id, self.message_id, prompt,
                                    _ask_user_fallback_text(prompt))
        return False

    async def _ask_card(self, prompt: str) -> bool:
        """The card for `prompt` on the tab viewing the chat, and its answer
        sent; False when nothing was sent."""
        payload = _native_ask_user_payload(prompt)
        if payload is None:
            return False
        try:
            response = await _call_native_card_on_live_session(
                self.user_id, self.chat_id, self.message_id, payload)
        except Exception as ex:
            pipe_log(f"  turn writer: question card failed: {ex!r}")
            return False
        if response is None:
            pipe_log(f"  turn writer: no tab views chat {self.chat_id[:8]}...; the question stays text")
            return False
        if self.closed or not (isinstance(response, dict) and response.get("status") == "answered"):
            return False
        answer = _format_native_answers(payload["data"]["questions"], response.get("answers") or {})
        if not answer:
            return False
        key = pipe_run_id(self.chat_id, self.message_id, "input")
        try:
            resp = await _run_request(self.conn, RUN_SEND, _owui_chat_send_params(
                session_key=self.session_key, message=answer, idempotency_key=key,
                owui_chat_id=self.chat_id, owui_user_id=self.user_id,
                thinking=self.thinking), timeout=30)
        except Exception as ex:
            pipe_log(f"  turn writer: sending the answer failed ({ex}); the question stays text")
            return False
        self.conn.note_turn_rewrite(self.chat_id, self.message_id, prompt,
                                    _ask_user_detail_block(prompt, answer))
        self.adopt(resp.get("runId") or key)
        pipe_log(f"  turn writer: answer sent for {self.message_id[:8]}... as {self.run_id[:32]}...")
        await self._stream()
        return True

    async def on_hello(self) -> None:
        """A new socket: send a resume the closing Gateway refused."""
        if self.pending_resume and not self.closed:
            await self._send_resume()

    async def _run_request(self) -> tuple[str | None, int]:
        """(text, attachments) of the user's message M answers: the user turn
        of the first run written into M, in transcript order, that is not
        itself a resume. Every run naming M counts, so M's own first run (a
        pipe run or an agent turn) is found even when this writer was made
        for a later run of M, a redeploy's or an Open WebUI restart's (review
        2af1 F1). (None, 0) when the transcript cannot be read (the resume
        then goes without it)."""
        try:
            hist = await self.conn.session_history(self.session_key, limit=RUN_HISTORY_LIMIT)
        except Exception as ex:
            pipe_log(f"  turn writer: chat.history for the resume failed: {ex!r}")
            return None, 0
        messages = (hist or {}).get("messages") or []
        for m in messages:
            run = _history_user_run_id(m)
            if not run or not (run in self.runs or run_message_id(
                    self.session_key, self.chat_id, run, messages) == self.message_id):
                continue
            # The user's answer to M's question and a resume are no request
            # (review 2af1 N2): only M's reply run or an agent turn is.
            if (parse_pipe_run_id(run) or {}).get("kind") in ("input", "resume"):
                continue
            content = m.get("content")
            blocks = content if isinstance(content, list) else []
            text = content if isinstance(content, str) else "".join(
                str(b.get("text") or "") for b in blocks
                if isinstance(b, dict) and b.get("type") == "text")
            text = _strip_inbound_meta(text)
            # Content blocks, or (how the Gateway stores an Open WebUI image
            # turn) string content with the images in `__openclaw.media` (N1).
            media = (m.get("__openclaw") or {}).get("media")
            attachments = max(sum(1 for b in blocks if isinstance(b, dict) and b.get("type") != "text"),
                              len(media) if isinstance(media, list) else 0)
            if text.lstrip().startswith(RESUME_DIRECTIVE_PREFIX):
                continue
            if text.strip() or attachments:
                return text, attachments
        return None, 0

    async def _send_resume(self) -> None:
        if not self.request and not self.request_attachments:
            self.request, self.request_attachments = await self._run_request()
        directive = _resume_directive(self.renderer.visible_text, self.request,
                                      self.request_attachments)
        key = pipe_run_id(self.chat_id, self.message_id, "resume")
        try:
            resp = await _run_request(self.conn, RUN_SEND, _owui_chat_send_params(
                session_key=self.session_key, message=directive, idempotency_key=key,
                owui_chat_id=self.chat_id, owui_user_id=self.user_id,
                thinking=self.thinking), timeout=30)
        except Exception as ex:
            pipe_log(f"  turn writer: resume refused ({ex}); sending it on the next hello")
            return
        self.pending_resume = False
        self.adopt(resp.get("runId") or key)
        pipe_log(f"  turn writer: resumed {self.message_id[:8]}... as {self.run_id[:32]}... "
                 f"({self.resumes}/{MAX_TURN_RESUMES})")

    async def finalize(self, note: str | None = None) -> None:
        """Write M's last state, `done: true`: from the transcript when every
        run written into M has closed there, else what streamed. A silent
        sentinel is never written; a sentinel-only run that showed tool cards
        keeps them (condition B)."""
        if self.closed:
            return
        self.closed = True
        try:
            try:
                hist = await self.conn.session_history(self.session_key, limit=RUN_HISTORY_LIMIT)
            except Exception as ex:
                hist = None
                pipe_log(f"  turn writer: chat.history failed at the end: {ex!r}")
            if self.wake is not None:
                self._rebuild_wake(hist)
            else:
                if hist:
                    self._adopt_recoveries_from(hist.get("messages") or [])
                if hist:
                    self._note_transcript_answers(hist.get("messages") or [])
                rebuilt = self._from_transcript(hist) if hist else None
                if rebuilt is not None:
                    self.renderer = rebuilt
                if not (self._showable() or note):
                    await self._show_silent_announce(hist)
            if not self.opened:
                if not (self._showable() or note) or not await self.open(takeover=self.takeover):
                    return
            output, text = await self._view(final=True)
            output = [i for i in output if not _is_sentinel_message_item(i)]
            text = text.strip()
            if text in _SILENT_SENTINELS:
                text = ""
            if note:
                text = f"{text}\n\n{note}" if text else note
                output.append(_turn_note_item(note))
            await self._persist(done=True, output=output, text=text)
            emit = await self._emitter()
            await emit({"type": "chat:completion", "data": {"done": True, "content": text,
                                                             "output": output}})
            await emit({"type": "chat:active", "data": {"active": False}})
            tools = sum(1 for i in output if i.get("type") == "function_call")
            pipe_log(f"  turn writer: finalized {self.message_id[:8]}... ({len(text)} chars, "
                     f"{tools} tool card(s), {len(self.runs)} run(s))")
        finally:
            self._done.set()

    async def _show_silent_announce(self, hist: dict | None) -> None:
        """A sub-agent's completion the parent answered with silence (NO_REPLY)
        still shows the child's result, as one card (review 2af1 R10): its
        label from the parent's spawn call, its result from the child's own
        transcript, or what was kept of it before a `cleanup: "delete"` (its
        chat final, or the transcript read as the announce run started).
        Nothing when the child's result cannot be had."""
        child = next(filter(None, (parse_announce_run_id(r) for r in self.runs)), None)
        if child is None:
            return
        result = await _child_result(self.conn, child)
        if not result:
            return
        label = None
        for m in (hist or {}).get("messages") or []:
            blocks = m.get("content") if isinstance(m.get("content"), list) else []
            calls = {b.get("id"): b for b in blocks if isinstance(b, dict) and b.get("type") == "toolcall"}
            for b in blocks:
                if (isinstance(b, dict) and b.get("type") == "tool_result"
                        and child["child_session"] in json.dumps(b.get("content"))
                        and b.get("tool_use_id") in calls):
                    label = _spawn_title(calls[b["tool_use_id"]].get("arguments") or {})
        tcid = f"subagent-{child['child_run']}"
        args_str = json.dumps({"label": label or child["child_session"]})
        call = _tool_call_started_event("Sub-agent", tcid, args_str)["item"]
        call["status"] = "completed"
        self.renderer = _TurnRenderer()
        self.renderer.output = [call, _tool_call_result_event(tcid, result)["item"]]

    def release(self) -> None:
        """This writer's connection was retired by a newer deploy: end the Open
        WebUI task now and write nothing more. A task left registered keeps the
        chat "generating" and makes orphan repair skip the chat (live 2026-10-13,
        task 03904655 of retired 62b9927 held efe80f6a). M stays unfinished
        with this writer recorded, which no longer counts as live, so the
        successor adopts it (`_adopt_turn_message`) and finishes it once its
        run is over."""
        if self.closed:
            return
        self.closed = True
        self._done.set()

    def _rebuild_wake(self, hist: dict | None) -> None:
        """Repair lost events from the wake's window of the transcript. A wake
        has no closing message to prove its span complete (only the first wake
        into a session gets one), so the transcript replaces what streamed only
        when it holds at least as much: it never takes content away."""
        if not hist or self.wake.get("at") is None or self.wake.get("end") is None:
            return
        rebuilt = _history_renderer(wake_span(hist.get("messages"), self.wake["at"],
                                              self.wake["end"]))
        # Every card that streamed must be in it: the transcript may lack a
        # wake's tool calls entirely (gate 2026-10-13: no import of the wake's
        # CLI session with a user turn queued behind it), and an item count
        # takes a text item for a card.
        if (self._holds_streamed_calls(rebuilt, "wake's")
                and len(rebuilt.output) >= len(self.renderer.output)
                and len(rebuilt.text) >= len(self.renderer.text) and rebuilt.output):
            self.renderer = rebuilt

    def _from_transcript(self, hist: dict) -> "_TurnRenderer | None":
        """M rebuilt from the transcript wherever the transcript holds it.

        A run counts as covered when its span holds every call it streamed.
        A killed or aborted claude-cli run is never covered: its calls and
        text are never imported (graceful window 2026-10-14; run 64028b9f).
        The transcript is used from the first run k after which every run is
        covered; what streamed before run k's start is kept as it was, and
        the rebuild follows it with the live stream's break. So a resume
        after a killed run keeps the killed run's preamble and card, and a
        recovery the writer joined late still gets its start back (crash
        window 2026-10-14: its first text and card ran before the pipe was
        back). None (keep what streamed) while a run has not closed, or when
        the transcript covers nothing."""
        spans = [_history_span_messages(hist, r) for r in self.runs]
        if any(s is None for s in spans):
            return None
        out, text = self.renderer.output, self.renderer.text
        starts = {run: (o, t) for run, o, t in self.run_marks}
        order = sorted(self.run_marks, key=lambda m: m[1])

        def streamed(run):
            if run not in starts:
                return []
            o = starts[run][0]
            end = min((m[1] for m in order if m[1] > o), default=len(out))
            owner = [m for m in order if m[1] == o][-1][0]   # adopted last at this point
            return out[o:end] if owner == run else []

        def covered(run, span):
            rebuilt = _history_renderer(span)
            calls = {i.get("id") for i in rebuilt.output if i.get("type") == "function_call"}
            seg = streamed(run)
            return ({i.get("id") for i in seg if i.get("type") == "function_call"} <= calls
                    and (bool(rebuilt.output) or not seg))
        k = len(self.runs)
        while k > 0 and covered(self.runs[k - 1], spans[k - 1]):
            k -= 1
        if k == len(self.runs):
            pipe_log(f"  turn writer: the transcript covers none of {self.message_id[:8]}...'s "
                     f"{len(self.runs)} run(s); keeping what streamed")
            return None
        rest = _history_renderer([m for s in spans[k:] for m in s])
        shown = self.shown.get(self.runs[k], "")
        if shown and not rest.drop_shown(shown):
            pipe_log(f"  turn writer: run {self.runs[k][:24]}... does not start with what earlier "
                     f"replies showed; keeping what streamed in {self.message_id[:8]}...")
            return None
        if not rest.output:
            return None
        cut = min((starts[r] for r in self.runs[k:] if r in starts), default=(len(out), len(text)))
        if k == 0 or cut == (0, 0):
            return rest
        prefix_out, prefix_text = copy.deepcopy(out[:cut[0]]), text[:cut[1]]
        rest_out = rest.finished_output()
        sep = _message_boundary_separator(prefix_text, rest.text) \
            if prefix_text.strip() and rest.text else ""
        if sep:
            first = next(i for i in rest_out if i.get("type") == "message")
            first["content"][0]["text"] = sep + first["content"][0]["text"]
        composed = _TurnRenderer()
        composed.output = prefix_out + rest_out
        composed.text = composed.visible_text = prefix_text + sep + rest.text
        pipe_log(f"  turn writer: {self.message_id[:8]}... keeps {len(prefix_out)} streamed item(s) of "
                 f"{k} run(s) the transcript lacks, then {len(self.runs) - k} run(s) from it")
        return composed

    def _holds_streamed_calls(self, rebuilt, whose: str) -> bool:
        """Whether a transcript rebuild holds every tool call that streamed
        into M. A transcript can lack them all: a wake's CLI session not yet
        imported (gate 2026-10-13), or an aborted claude-cli run, whose tool
        calls are never imported (run 64028b9f, 2026-10-14)."""
        streamed = {o.get("id") for o in self.renderer.output if o.get("type") == "function_call"}
        missing = streamed - {o.get("id") for o in rebuilt.output if o.get("type") == "function_call"}
        if missing:
            pipe_log(f"  turn writer: the transcript lacks {len(missing)} of the {whose} "
                     f"{len(streamed)} streamed call(s); keeping what streamed")
        return not missing

    async def _stopped(self) -> None:
        """Open WebUI cancelled the writer's task. Stop and Send now both do
        that (DESIGN case 8), so the page is asked which: Stop aborts the run
        and finalizes what M has; Send now or no answer keeps writing M to the
        run's end, and the queued message runs after it (case 7)."""
        intent = await _page_cancel_intent(self.user_id, self.chat_id, self.message_id)
        pipe_log(f"  turn writer: task of {self.message_id[:8]}... cancelled, page intent "
                 f"{intent or 'unknown'}")
        if intent != "stop" or self.closed:
            return
        try:
            await _run_request(
                self.conn, RUN_ABORT, {"sessionKey": self.session_key, "runId": self.run_id}, timeout=15)
        except Exception as ex:
            pipe_log(f"  turn writer: chat.abort failed: {ex}")
        await self.finalize()

    async def _hold(self) -> None:
        """The Open WebUI task: alive exactly while this writer owns the run.
        Cancelled by the page's Stop (POST /api/tasks/chat/<id>/stop)."""
        try:
            await self._done.wait()
        except asyncio.CancelledError:
            if not self.closed:
                spawn_background(self._stopped())
            raise


# ---------------------------------------------------------------------------
# Routing: which writer gets a run's events (DESIGN 2.1, 2.2)
# ---------------------------------------------------------------------------
# The socket's receive loop also reads RPC replies, so it must never await an
# RPC itself. Each routed run gets a queue and a pump task: the pump resolves
# the run's message (rule E reads the transcript once for a non-pipe run) and
# then feeds the run's events and its settle in order.


def _media_directives(text: str, final: bool) -> list[str]:
    """Each complete `MEDIA:<file>` directive in `text`, as written, by the
    inline reply's grammar (`_media_scan`): outside code (ELI-111), and the
    last one complete once something follows its filename, or when the run
    is over."""
    out = []
    for start, end, _fname in _media_scan(text, final=final)[0]:
        if text[start:end] not in out:
            out.append(text[start:end])
    return out


def _replace_directive(text: str, raw: str, shown: str) -> str:
    """`text` with `shown` for each occurrence of the directive `raw` that
    the inline reply's grammar resolves, none quoted in code (ELI-111), and
    the text after it on a line of its own, as the inline reply saves it
    (`_replace_media`; ELI-117)."""
    hits = [d for d in _media_scan(text)[0] if text[d[0]:d[1]] == raw]
    return _replace_media(text, hits, lambda _fname: shown) if hits else text


async def _transcript_view(conn: "_GatewayConnection", *, session_key: str, chat_id: str,
                           user_id: str, message_id: str, runs: list, messages: list,
                           rendered: "_TurnRenderer") -> tuple[list, str]:
    """(output, text) of M rebuilt from the transcript as the writer finalizes
    it (`_TurnWriter._view`): an answered ask-user prompt as its card, one
    nobody answered without its directive line, a `MEDIA:` directive as its
    image. `content` is that text, so it always agrees with the items. The
    ELI-94 reconcile wrote the bare rebuild and dropped an answered card
    (ask-rebuild, 2026-10-14, chat d0f0c6ff)."""
    view = _TurnWriter(conn, session_key=session_key, chat_id=chat_id, user_id=user_id,
                       run_id=runs[-1], message_id=message_id, renderer=rendered)
    view.runs = list(runs)
    view._note_transcript_answers(messages)
    output, text = await view._view(final=True)
    output = [i for i in output if not _is_sentinel_message_item(i)]
    text = text.strip()
    return output, "" if text in _SILENT_SENTINELS else text


def _history_span_messages(history: dict, run_id: str) -> list | None:
    """One run's messages in `chat.history`, or None while it may still be
    writing. A closed run ends at its `cli-assistant:<runId>`; a run cut off
    with no closing turn (a crash, an outside kill) ends where the next
    admitted turn (a recovery, a resume) begins."""
    closed = _history_run_messages(history, run_id)
    if closed is not None:
        return closed
    messages = (history or {}).get("messages")
    if not isinstance(messages, list):
        return None
    starts = [i for i, m in enumerate(messages) if _history_user_run_id(m)]
    for n, i in enumerate(starts):
        if _history_user_run_id(messages[i]) == run_id and n + 1 < len(starts):
            return messages[i + 1:starts[n + 1]]
    return None


class _RunPump:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.writer: "_TurnWriter | None" = None
        self.handover: dict | None = None  # {message_id, renderer} of a request that ended early
        self.stop_notice_before = False
        self.wake = False               # a cron wake: runId is the sessionId (ELI-103)
        self.held: list = []            # a wake's events before its turn is known
        self.heartbeat = False          # a wake-id run the Gateway flagged isHeartbeat


def _turn_route_now(conn: "_GatewayConnection", payload: dict) -> bool:
    """Queue `payload` for its run's writer when the run is a turn this
    connection writes: an OWUI session key and no live request consuming it.
    A run whose request ended before it is handed over (DESIGN 2.6). True
    when handled here. Never awaits: held events drain through it in order
    (`drain_held`)."""
    sess, run = payload.get("sessionKey") or "", payload.get("runId") or ""
    if not (getattr(conn, "writes_turns", False) and sess and run) or conn._retired:
        return False
    conn.note_session_id(sess, payload.get("sessionId"))
    parsed = conn.parse_owui_session_key(sess)
    if not parsed:
        _note_child_result(conn, payload)
        return False
    if conn.consumers_for_event(payload) or _served_by_retired(payload):
        return False  # a live reply, here or on an earlier load's connection, writes it
    if run.startswith(ACTION_RUN_PREFIX):
        return True  # the Status Action's own runs render nothing
    if (sess, run) in conn._held_for_live:
        conn.drain_held(sess, run)  # the earlier ones first (review 2af1 N7)
    if conn.live_reply_owns(sess, run):
        conn.hold_for_live(sess, run, payload)
        return True
    _note_heartbeat_run(conn, payload)
    if (sess, run) in conn._turn_skip_runs:
        return True
    if conn.was_delivered_live(sess, run):
        # A request showed this run to the end, or a Stop / Send now closed
        # its message: a late event reopens nothing.
        return True
    if not _turn_kind_known(conn, sess, run, payload):
        return True
    return _turn_queue(conn, sess, run, parsed, payload)


def _turn_queue(conn: "_GatewayConnection", sess: str, run: str, parsed: tuple,
                payload: dict) -> bool:
    pump = conn._turn_pumps.get((sess, run))
    if pump is None:
        pump = conn._turn_pumps[(sess, run)] = _RunPump()
        target = conn._run_targets.get(f"{sess}:{run}")
        if target is not None:
            # DESIGN 2.6: the request streaming this run ended before it. The
            # writer continues in the same message from the shadow renderer,
            # which already holds this event.
            pump.handover = conn.handover_of(sess, run)
            conn.unregister_run_target(sess, run)
        spawn_background(_turn_pump_loop(conn, sess, run, parsed, pump))
        spawn_background(conn.ensure_message_subscription(sess))
        child = parse_announce_run_id(run)
        if child is not None:
            # Read before the Gateway deletes a `cleanup: "delete"` child,
            # which it does once this announcement is delivered.
            spawn_background(_child_result(conn, child))
        if target is not None:
            return True
    if payload.get("state") in ("final", "error", "aborted"):
        pump.stop_notice_before = conn._stop_notice_seen
    pump.queue.put_nowait(("event", payload))
    return True


def _note_child_result(conn: "_GatewayConnection", payload: dict) -> None:
    """Keep the result of a sub-agent an Open WebUI session spawned, from its
    own chat final (`spawnedBy` = the parent, server-chat-CbnqM5f6.mjs
    emitChatTerminal): a `cleanup: "delete"` child is gone by the time its
    announce run ends (subagent-announce-BIG31XbZ.mjs:413), and a silent
    announce shows this result (review 2af1 R10)."""
    if payload.get("state") != "final" or not conn.parse_owui_session_key(payload.get("spawnedBy") or ""):
        return
    text = (_history_item(payload.get("message")) or {}).get("text") or ""
    if text.strip():
        _keep_child_result(conn, payload["sessionKey"], payload["runId"], text.strip())


def _keep_child_result(conn: "_GatewayConnection", child_session: str, child_run: str,
                       text: str) -> None:
    conn._child_results[(child_session, child_run)] = text
    if len(conn._child_results) > 200:
        for stale in list(conn._child_results)[:100]:
            conn._child_results.pop(stale, None)


async def _child_result(conn: "_GatewayConnection", child: dict) -> str | None:
    """The result of the sub-agent run an announce run names: kept from its
    chat final, else read from its transcript (and kept), else None."""
    key = (child["child_session"], child["child_run"])
    if key in conn._child_results:
        return conn._child_results[key]
    try:
        child_hist = await conn.session_history(child["child_session"], limit=20)
    except Exception as ex:
        pipe_log(f"  turn writer: child transcript of {child['child_session'][-12:]} "
                 f"unreadable: {ex!r}")
        return None
    found, result = _history_text_for_run(child_hist, child["child_run"])
    if not (found and result):
        return None
    _keep_child_result(conn, *key, result)
    return result


def _note_heartbeat_run(conn: "_GatewayConnection", payload: dict) -> None:
    """Condition B: a heartbeat run never gets a message. Its agent events
    carry isHeartbeat (server-chat-CbnqM5f6.mjs:791, 823-826); its chat events
    and session.tool copies do not, so the run is remembered, and what it sent
    before its first flagged event is dropped (`_turn_kind_known`)."""
    sess, run = payload.get("sessionKey"), payload.get("runId")
    if payload.get("isHeartbeat") is not True or not (sess and run):
        return
    if run == conn._session_ids.get(sess):
        return  # a wake's id: never skip it for good (handled in _wake_step)
    held = conn._turn_unkinded.pop((sess, run), None)
    if held:
        pipe_log(f"  turn writer: heartbeat run {run[:24]}...: {len(held)} earlier event(s) not shown")
    conn._turn_skip_runs[(sess, run)] = True
    if len(conn._turn_skip_runs) > 500:
        for stale in list(conn._turn_skip_runs)[:250]:
            conn._turn_skip_runs.pop(stale, None)


# Bounds on runs held unclassified: a run is classified by its first agent
# event, so anything past these is a leak, not a wait.
_UNKINDED_RUNS_MAX = 64
_UNKINDED_EVENTS_MAX = 500


def _classifies(payload: dict) -> bool:
    """An event that tells a heartbeat run from any other: an agent event of
    its own (not a session.tool copy, which the receive loop rebuilds without
    the flag, and not a tool event, the stream those copies share), or the
    run's end, which comes after its flagged lifecycle end."""
    stream = payload.get("stream")
    return (bool(stream) and stream != "tool") or _is_terminal_event(payload)


def _turn_kind_known(conn: "_GatewayConnection", sess: str, run: str, payload: dict) -> bool:
    """Review 2af1 R7: an agent run's events may reach the writer only once the
    run is known not to be a heartbeat. Until then they are held in order;
    the run's first classifying event releases them ahead of itself, its
    first flagged one drops them (`_note_heartbeat_run`), and its session's
    settled row releases them too (`_turn_settled`). A pipe run (the pipe
    started it) and a cron wake (`_wake_step` judges it) need no wait."""
    if (sess, run) in conn._turn_kinded or parse_pipe_run_id(run) is not None \
            or run == conn._session_ids.get(sess):
        return True
    if not _classifies(payload):
        held = conn._turn_unkinded.setdefault((sess, run), [])
        if len(held) < _UNKINDED_EVENTS_MAX:
            held.append(payload)
        while len(conn._turn_unkinded) > _UNKINDED_RUNS_MAX:
            stale = next(iter(conn._turn_unkinded))
            pipe_log(f"  turn writer: run {stale[1][:24]}... never classified: released")
            _release_unkinded(conn, *stale)
        return False
    _release_unkinded(conn, sess, run)
    return True


def _release_unkinded(conn: "_GatewayConnection", sess: str, run: str) -> None:
    conn._turn_kinded[(sess, run)] = True
    if len(conn._turn_kinded) > 500:
        for stale in list(conn._turn_kinded)[:250]:
            conn._turn_kinded.pop(stale, None)
    for ev in conn._turn_unkinded.pop((sess, run), []):
        _turn_route_now(conn, ev)


def _hand_over_run(conn: "_GatewayConnection", session_key: str, run_id: str, *,
                   thinking: str | None = None, read_row: bool = False,
                   reply: dict | None = None) -> bool:
    """An interrupted run's request ends without continuing it: the turn
    writer takes its message over (DESIGN 2.6) and the run's settled row
    decides who continues it (2.7), so a reply never resumes a run the
    Gateway recovers. `read_row`: the row is read now, for a handover
    that can come after the post-hello resync. `reply`: a request that
    hands over at an event gap gives its own state, {message_id,
    renderer, events}: what it showed, and the events queued behind the
    gap, which the writer takes next; with `no_resume` the writer never
    resumes the run (a run Send now kept, ELI-158). The run's target is then not
    needed (the run's final reaching the request drops it). False when
    there is no message to hand over."""
    key = f"{session_key}:{run_id}"
    target = conn._run_targets.get(key)
    parsed = conn.parse_owui_session_key(session_key)
    if ((target is None and reply is None) or not parsed or not conn.writes_turns
            or conn._retired or (session_key, run_id) in conn._turn_pumps):
        return False
    pump = conn._turn_pumps[(session_key, run_id)] = _RunPump()
    if reply is None:
        pump.handover = conn.handover_of(session_key, run_id, thinking)
    else:
        pump.handover = {"message_id": reply["message_id"], "renderer": reply["renderer"],
                         "thinking": thinking or (target or {}).get("thinking"), "shown": "",
                         "no_resume": bool(reply.get("no_resume"))}
        for payload in reply["events"]:
            pump.queue.put_nowait(("event", payload))
    pump.stop_notice_before = conn._stop_notice_seen
    conn.unregister_run_target(session_key, run_id)
    spawn_background(_turn_pump_loop(conn, session_key, run_id, parsed, pump))
    spawn_background(conn.ensure_message_subscription(session_key))
    if read_row:
        spawn_background(_turn_resync(conn, skip=set(conn.active_run_snapshot)))
    pipe_log(f"  handed run {run_id[:24]}... over to the turn writer "
             f"(message {pump.handover['message_id'][:8]}...)")
    return True


def _turn_settled(conn: "_GatewayConnection", session_key: str, row: dict) -> None:
    """The session went idle: every run pumped on it has ended. Its settled
    row decides each writer's next step (DESIGN 2.5, 2.7). A run still held
    unclassified is no heartbeat (its flagged lifecycle end would have come
    first) and is released ahead of the row."""
    for (sess, run) in [k for k in conn._turn_unkinded if k[0] == session_key]:
        _release_unkinded(conn, sess, run)
    for (sess, _run), pump in list(conn._turn_pumps.items()):
        if sess == session_key:
            pump.queue.put_nowait(("settled", row))


async def _turn_writer_for(conn: "_GatewayConnection", sess: str, run: str,
                           user_id: str, chat_id: str) -> "_TurnWriter | None":
    # Two runs can name one message (a recovery and the run it recovers). The
    # one resolved second must find the first one's writer, not open its own,
    # so resolving and registering is one step per connection.
    async with conn._turn_resolve_lock:
        return await _turn_writer_for_locked(conn, sess, run, user_id, chat_id)


async def _turn_writer_for_locked(conn, sess, run, user_id, chat_id):
    messages = None
    if parse_pipe_run_id(run) is None:
        # Not a pipe run: a restart recovery writes into the message of the run
        # it recovers (rule E), which only the transcript names.
        try:
            messages = (await conn.session_history(sess, limit=50, max_chars=200)).get("messages")
        except Exception as ex:
            pipe_log(f"  turn writer: chat.history for rule E failed: {ex!r}")
    mid = run_message_id(sess, chat_id, run, messages)
    if mid is None:
        return None
    writer = conn._turn_writers.get((chat_id, mid))
    if writer is not None and not writer.closed:
        writer.adopt(run)
        return writer
    # A pipe run names Open WebUI's stub for its turn: after an Open WebUI
    # restart no request or target is left, and the writer takes the stub
    # over unless another live writer holds it.
    writer = _TurnWriter(conn, session_key=sess, chat_id=chat_id, user_id=user_id,
                         run_id=run, message_id=mid,
                         takeover=parse_pipe_run_id(run) is not None)
    conn._turn_writers[(chat_id, mid)] = writer
    return writer


async def _turn_pump_loop(conn: "_GatewayConnection", sess: str, run: str,
                          parsed: tuple, pump: _RunPump) -> None:
    user_id, chat_id = parsed
    try:
        handed = pump.handover
        if pump.writer is not None:
            pass  # adopted from a gone writer's message (_adopt_turn_message)
        elif handed is not None:
            pump.writer = _TurnWriter(conn, session_key=sess, chat_id=chat_id, user_id=user_id,
                                      run_id=run, message_id=handed["message_id"], takeover=True,
                                      renderer=handed["renderer"], thinking=handed.get("thinking"),
                                      shown=handed.get("shown", ""))
            conn._turn_writers[(chat_id, handed["message_id"])] = pump.writer
            if handed.get("no_resume"):
                # Kept by Send now (ELI-158): the user's new message is next
                # and goes out when the run settles. A resume would race it
                # into the same session (ELI-159), so an outside kill ends M.
                pump.writer.resumes = MAX_TURN_RESUMES
            await pump.writer.feed(None)
        elif re.fullmatch(_UUID_RE, run) and run == await conn.session_id_for(sess):
            pump.wake = True
        else:
            pump.writer = await _turn_writer_for(conn, sess, run, user_id, chat_id)
        while True:
            kind, item = await pump.queue.get()
            if kind == "retired":
                return
            if pump.wake:
                if await _wake_step(conn, sess, run, user_id, chat_id, pump, kind, item):
                    return
                continue
            writer = pump.writer
            if kind == "event":
                if writer is not None:
                    await writer.feed(item)
                continue
            # Only the pump of the writer's current run ends it: an interrupted
            # run's pump also gets the session's settle once its recovery has
            # taken M over, and the recovery's own pump may still hold events.
            if writer is not None and writer.run_id == run:
                await writer.end(item, stop_notice_before=pump.stop_notice_before)
                if writer.closed:
                    conn._turn_writers.pop((chat_id, writer.message_id), None)
                elif writer.awaiting_row and writer.run_id == run:
                    continue  # R2: the next row of the session decides again
            return
    except Exception as ex:
        pipe_log(f"  turn writer: pump for run {run[:24]}... failed: {ex!r}")
    finally:
        if conn._turn_pumps.get((sess, run)) is pump:
            conn._turn_pumps.pop((sess, run), None)


def _is_terminal_event(payload: dict) -> bool:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return payload.get("state") in ("final", "error", "aborted") or (
        payload.get("stream") == "lifecycle" and data.get("phase") in ("end", "error"))


def _event_ms(payload: dict) -> float:
    """When the Gateway emitted the event: agent events carry `ts`
    (agent-events enrichAgentEvent); a chat event has none, and the receive
    time on this host stands in (the Gateway runs on the same clock)."""
    ts = payload.get("ts")
    return float(ts) if isinstance(ts, (int, float)) else time.time() * 1000


# chat.history returns at most this many messages (CHAT_HISTORY_MAX_ENTRIES,
# chat-history-constants-C-H8nkgi.mjs:3).
CHAT_HISTORY_MAX = 1000


async def _wake_bind(conn: "_GatewayConnection", sess: str, run: str, user_id: str,
                     chat_id: str, pump: _RunPump, at_ms: float) -> tuple[str, float | None]:
    """Give the wake its message: the one its own admitted turn names.
    Returns ("bound", the job run's start), ("wait", None) while a cron job
    of this session runs but its turn is not readable yet (the embedded
    runtime commits it after the first events), or ("stray", None): nothing
    runs a wake into this session, so the event is not a wake's (review
    2af1 N2)."""
    try:
        jobs = (await conn.send_request("cron.list", {"includeDisabled": True}, timeout=15)).get("jobs")
    except Exception as ex:
        pipe_log(f"  turn writer: cron.list for a wake failed: {ex!r}")
        return "wait", None
    starts = [(j.get("state") or {}).get("runningAtMs") for j in jobs or []
              if j.get("sessionTarget") == f"session:{sess}"]
    running_start = min((s for s in starts if isinstance(s, (int, float))), default=None)
    bound = conn._wake_turns_bound.setdefault(sess, set())
    # The turn is committed after its job started: read back until the window
    # holds a message older than that start (review 2af1: a fixed 50-message
    # window lost a wake that had written more before a reconnect).
    limit, turn = 50, None
    while True:
        try:
            messages = (await conn.session_history(sess, limit=limit, max_chars=200)).get("messages") or []
        except Exception as ex:
            pipe_log(f"  turn writer: chat.history for a wake failed: {ex!r}")
            return "wait", None
        turn = wake_turn_at(messages, at_ms, bound)
        oldest = min((m.get("timestamp") for m in messages
                      if isinstance(m.get("timestamp"), (int, float))), default=None)
        if (turn is not None or running_start is None or len(messages) < limit
                or limit >= CHAT_HISTORY_MAX or (oldest is not None and oldest < running_start)):
            break
        limit = min(limit * 4, CHAT_HISTORY_MAX)
    start = await _wake_job_start(conn, turn, at_ms, jobs) if turn is not None else None
    if start is None:
        return ("wait", None) if running_start is not None else ("stray", None)
    bound.add(turn["key"])
    mid = turn_message_id(sess, turn["key"], chat_id)
    pump.writer = _TurnWriter(conn, session_key=sess, chat_id=chat_id, user_id=user_id,
                              run_id=run, message_id=mid, wake=dict(turn))
    conn._turn_writers[(chat_id, mid)] = pump.writer
    pipe_log(f"  turn writer: wake turn {turn['key'][:8]}... (cron job {turn['job'][:8]}...) "
             f"is message {mid[:8]}...")
    return "bound", start


async def _wake_job_start(conn: "_GatewayConnection", turn: dict, at_ms: float,
                          jobs: list | None) -> float | None:
    """When the cron job run that wrote wake turn `turn` started, if the event
    at `at_ms` is that run's; else None. The job must be running since before
    the turn was written (`state.runningAtMs`), or have a finished run whose
    window holds both the turn and the event (`cron.runs`, kept after a
    one-shot job is deleted). Anything else that runs with runId = sessionId
    (a node agent command, a compaction) is not a wake and opens no message
    (live 2026-10-13: runningAtMs 1791863926518 precedes the turn's commit
    1791863928462). An RPC failure answers None: the events wait."""
    job, at = turn.get("job"), turn.get("at") or 0
    running = next(((j.get("state") or {}).get("runningAtMs") for j in jobs or []
                    if j.get("id") == job), None)
    if running and running <= at:
        return running
    run = await _wake_job_run(conn, job, at)
    if run is not None and at_ms <= (run.get("ts") or 0):
        return run.get("runAtMs") or 0
    return None


async def _wake_job_run(conn: "_GatewayConnection", job: str, turn_at: float) -> dict | None:
    """The finished `cron.runs` entry of `job` whose run wrote a turn at
    `turn_at`: started before it, finished after it."""
    try:
        runs = (await conn.send_request("cron.runs", {"id": job, "limit": 20}, timeout=15)).get("entries")
    except Exception as ex:
        pipe_log(f"  turn writer: cron.runs for wake job {str(job)[:8]}... failed: {ex!r}")
        return None
    done = [e for e in runs or [] if e.get("action") == "finished"
            and (e.get("runAtMs") or 0) <= turn_at <= (e.get("ts") or 0)]
    return min(done, key=lambda e: e.get("ts") or 0, default=None)


async def _wake_step(conn: "_GatewayConnection", sess: str, run: str, user_id: str,
                     chat_id: str, pump: _RunPump, kind: str, item) -> bool:
    """One queue item of a cron wake. A session row never ends a wake: the
    row stays idle while it runs, and a row pushed for another run (a message
    queued behind it) says nothing about it. Its own terminal event does.
    True when the wake is over and the pump can go."""
    if kind == "settled":
        return False
    if kind == "event" and (pump.heartbeat or item.get("isHeartbeat") is True):
        # Condition B by the run's resolved identity, not by its id: the skip
        # table is keyed by run id, which a wake shares with every other wake
        # (ELI-103 f). Nothing more of this run is shown; its terminal frees
        # the id for the next wake.
        pump.heartbeat = True
        pump.held.clear()
        if pump.writer is not None and not pump.writer.opened:
            pump.writer.closed = True
        if _is_terminal_event(item):
            await _wake_finish(conn, sess, run, pump, _event_ms(item))
            return True
        return False
    if kind == "wake-ended":
        await _wake_finish(conn, sess, run, pump, item)
        return True
    if pump.writer is None:
        pump.held.append(item)
        state, start = await _wake_bind(conn, sess, run, user_id, chat_id, pump, _event_ms(item))
        if state == "stray":
            # Nothing runs a wake into this session: these events are no
            # wake's, and holding them would hand them to the next wake, which
            # reuses this run id (review 2af1 N2). The next event is judged anew.
            pipe_log(f"  turn writer: {len(pump.held)} sessionId event(s) on {sess[-12:]} with no "
                     "running cron wake: not shown")
            pump.held.clear()
            return True
        if pump.writer is not None:
            held, pump.held = pump.held, []
            for ev in held:
                if _event_ms(ev) >= start:  # older ones predate this wake's run
                    await pump.writer.feed(ev)
    else:
        await pump.writer.feed(item)
    if _is_terminal_event(item):
        await _wake_finish(conn, sess, run, pump, _event_ms(item))
        return True
    return False


async def _wake_finish(conn: "_GatewayConnection", sess: str, run: str, pump: _RunPump,
                       end_ms: float) -> None:
    writer = pump.writer
    if writer is None:
        if not pump.heartbeat:
            pipe_log(f"  turn writer: a sessionId run on {sess[-12:]} ended with no running "
                     f"cron wake turn; {len(pump.held)} event(s) not shown")
    else:
        if not writer.closed:  # a heartbeat that already showed something is finished too
            writer.wake["end"] = end_ms
            await writer.finalize()
        conn._turn_writers.pop((writer.chat_id, writer.message_id), None)
    # The next wake reuses this run id: nothing remembered for it may carry over.
    conn._turn_skip_runs.pop((sess, run), None)
    conn._delivered_live.pop(f"{sess}:{run}", None)


async def _wake_resync(conn: "_GatewayConnection") -> None:
    """After a reconnect: end the wakes whose terminal this socket missed. The
    row cannot say (it is idle during a wake); the cron job can: it carries
    `runningAtMs` while it runs and a one-shot job is deleted once it ran."""
    wakes = [(k, p) for k, p in conn._turn_pumps.items()
             if p.wake and p.writer is not None and p.writer.wake]
    if not wakes:
        return
    try:
        jobs = (await conn.send_request("cron.list", {"includeDisabled": True}, timeout=15)).get("jobs")
    except Exception as ex:
        pipe_log(f"  turn writer: cron.list after hello failed: {ex!r}")
        return
    running = {j.get("id") for j in jobs or [] if (j.get("state") or {}).get("runningAtMs")}
    for (_sess, _run), pump in wakes:
        wake = pump.writer.wake
        if wake["job"] not in running:
            # The wake ended at its job run's finish, not now: a run queued
            # behind it may have written since (review 2af1 N1). Unknown
            # (None): the transcript is not used, the message keeps what it
            # streamed.
            run = await _wake_job_run(conn, wake["job"], wake.get("at") or 0)
            pump.queue.put_nowait(("wake-ended", (run or {}).get("ts")))


async def _adopt_turn_message(conn: "_GatewayConnection", *, chat_id: str, user_id: str,
                              session_key: str, message_id: str, meta: dict) -> str:
    """Take over message `message_id`, which a turn writer of a retired or
    dead connection left unfinished. Its meta names the run (and a wake's
    turn). Later events of the run reach the pump made here; a run already
    over is finished now. Returns "adopted" or "skip"."""
    run = meta.get("openclaw_run")
    if not run or (session_key, run) in conn._turn_pumps or conn._retired:
        return "skip"
    pump = conn._turn_pumps[(session_key, run)] = _RunPump()
    wake = meta.get("openclaw_wake") if isinstance(meta.get("openclaw_wake"), dict) else None
    if wake and wake.get("key"):
        pump.wake = True
        conn._wake_turns_bound.setdefault(session_key, set()).add(wake["key"])
    pump.writer = _TurnWriter(conn, session_key=session_key, chat_id=chat_id, user_id=user_id,
                              run_id=run, message_id=message_id, takeover=True,
                              wake=dict(wake) if pump.wake else None)
    conn._turn_writers[(chat_id, message_id)] = pump.writer
    spawn_background(_turn_pump_loop(conn, session_key, run, (user_id, chat_id), pump))
    spawn_background(conn.ensure_message_subscription(session_key))
    pipe_log(f"  turn writer: adopted {message_id[:8]}... (run {run[:24]}...) from writer "
             f"{str(meta.get('openclaw_writer'))[:12]}...")
    if pump.wake:
        await _wake_resync(conn)
        return "adopted"
    try:
        lst = await conn.send_request("sessions.list", {"search": session_key[-36:], "limit": 10},
                                      timeout=15)
    except Exception as ex:
        pipe_log(f"  turn writer: row read for an adopted message failed: {ex!r}")
        return "adopted"
    row = next((r for r in (lst.get("sessions") or []) if r.get("key") == session_key), None)
    if row is not None and not row.get("hasActiveRun"):
        pump.queue.put_nowait(("settled", row))
    return "adopted"


async def _turn_after_hello(conn: "_GatewayConnection") -> None:
    """After a reconnect: send resumes the closing Gateway refused, and end
    the runs whose session is no longer active though no settle reached this
    socket (it was down). Sessions still active wait for their own settle."""
    for writer in list(conn._turn_writers.values()):
        if writer.pending_resume:
            await writer.on_hello()
    await _turn_resync(conn, skip=set(conn.active_run_snapshot))
    await _wake_resync(conn)


async def _turn_resync(conn: "_GatewayConnection", skip: set = frozenset()) -> None:
    """Read the row of every session with an open pump or an inline reply
    (except `skip`, known active) and end the ones that went idle while their
    settle was missed. Inline replies get the row busy or idle: one that lost
    its own final and settle in the gap looks for its closer even while a
    later run keeps the session busy (review 2af1 N3)."""
    idle = {sess for (sess, _run) in [*conn._turn_pumps, *conn._turn_unkinded] if sess not in skip}
    idle |= {c.session_key for c in list(conn._consumers.values()) if c.session_key not in skip}
    for sess in idle:
        read_at = time.monotonic()
        try:
            lst = await conn.send_request("sessions.list", {"search": sess[-36:], "limit": 10},
                                          timeout=15)
        except Exception as ex:
            pipe_log(f"  turn writer: row read after hello failed: {ex!r}")
            continue
        row = next((r for r in (lst.get("sessions") or []) if r.get("key") == sess), None)
        if row is None:
            continue
        if not row.get("hasActiveRun"):
            _turn_settled(conn, sess, row)
        conn.row_to_consumers(sess, row, resync=True, read_at=read_at, outside_loop=True)


def _history_run_messages(history: dict, run_id: str) -> list | None:
    """One run's messages in `chat.history`: everything after the user
    message that started it, or after the previous run's closing message
    (a run with no user turn, e.g. a sub-agent announce), through its own
    closing message (`_run_closed_by`). None when the run has not closed
    yet."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list) or not run_id:
        return None
    for end in range(len(messages) - 1, -1, -1):
        message = messages[end]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if run_id not in _idempotency_keys(message) and _run_closed_by(message) != run_id:
            continue
        start = end
        while start > 0 and (messages[start - 1] or {}).get("role") != "user" \
                and not _run_closed_by(messages[start - 1]):
            start -= 1
        return messages[start:end + 1]
    return None


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
        if closing in _idempotency_keys(m):
            return {"run_id": run_id, "messages": turn[:j + 1], "closed": True, "by": by}
    return {"run_id": run_id, "messages": turn, "closed": end < len(messages), "by": by}


def _owui_message_ts(message: dict) -> float | None:
    ts = message.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return ts / 1000 if ts > 1e11 else float(ts)  # ms or s
    return None


def _owui_process_started_at() -> float | None:
    """When this Open WebUI process started (epoch seconds), from /proc: its
    start in clock ticks after boot plus the boot time. None when /proc
    cannot say."""
    try:
        with open("/proc/self/stat") as fh:
            # Field 22; the command name (field 2) may hold spaces, so count
            # from the closing parenthesis.
            ticks = int(fh.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as fh:
            btime = next(int(line.split()[1]) for line in fh if line.startswith("btime "))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def _stub_predates_process(stub: dict) -> bool:
    """True when `stub` was written before this Open WebUI process started,
    so no request in this process can be on its way to fill it. Open WebUI
    stamps whole seconds: a stub from the start's own second is not
    counted."""
    started, ts = _owui_process_started_at(), _owui_message_ts(stub)
    return started is not None and ts is not None and ts < int(started)


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


async def _session_list_active(conn: "_GatewayConnection", session_key: str,
                               *, missing: bool | None = False) -> bool | None:
    """`sessions.list` `hasActiveRun` for one session (counts a sub-agent
    yield gap as active, unlike `sessions.describe`). `missing` when the
    Gateway lists no such session, None when it can't be asked."""
    try:
        lst = await conn.send_request("sessions.list", {}, timeout=15)
    except Exception as ex:
        pipe_log(f"  sessions.list for {session_key[-36:][:8]}... failed: {ex}")
        return None
    row = next((r for r in (lst.get("sessions") or []) if r.get("key") == session_key), None)
    return bool(row.get("hasActiveRun")) if row else missing


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
        # Open WebUI stamps the task id of the request that will fill a stub
        # on it just before it starts that task. With none, no request has
        # taken the stub yet, and its request may still be on its way: after
        # a restart, that request's own model listing starts this sweep
        # (ELI-156). Unless the stub is older than this process: then its
        # request died with the previous one, before it took the stub.
        task_id = (stub.get("meta") or {}).get("task_id")
        if not task_id and not _stub_predates_process(stub):
            return "skip"
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
        if (current.get("meta") or {}).get("task_id") != task_id:
            return "skip"  # a new request took it while the transcript was read
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
        for k in _idempotency_keys(m):
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


class _TurnHooks:
    """What `_GatewayConnection` calls into the turn writer with (`_TURNS`)."""
    route_now = staticmethod(_turn_route_now)
    after_hello = staticmethod(_turn_after_hello)
    resync = staticmethod(_turn_resync)
    settled = staticmethod(_turn_settled)
    note_heartbeat = staticmethod(_note_heartbeat_run)
    hand_over = staticmethod(_hand_over_run)


_TURNS = _TurnHooks
