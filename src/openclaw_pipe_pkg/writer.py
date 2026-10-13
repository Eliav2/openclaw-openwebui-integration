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
  const running = () => !!document.querySelector('button[aria-label="Stop"], #stop-response-button');
  document.addEventListener('click', (e) => {
    const b = e.target && e.target.closest ? e.target.closest('button') : null;
    if (!b) return;
    const label = b.getAttribute('aria-label') || '';
    if (label === 'Send now') mark('send_now');
    else if (label === 'Stop' || b.id === 'stop-response-button') mark('stop');
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
# never answers (it is not a wait for anything to happen).
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
                 thinking: str | None = None, wake: dict | None = None):
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
        self.opened = False
        self.task_id: str | None = None
        self.parent_id: str | None = None
        self.runs = [run_id]            # every run written into M: the first, then resumes / recoveries
        self.resumes = 0                # resume budget, counted on M (MAX_TURN_RESUMES)
        self.pending_resume = False     # a resume the Gateway refused; sent on the next hello-ok
        self.closed = False             # nothing more to write (ended, or M is someone else's)
        self.sent_output: list = []     # the `output` open tabs hold
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
        self.parent_id = claim["parent"]
        if claim["how"] == "new":
            await _introduce_turn_message(self.chat_id, self.user_id, claim["messages"],
                                          self.message_id)
        from open_webui.tasks import create_task

        self.task_id, _ = await create_task(_owui_redis(), self._hold(), id=self.chat_id)
        await _set_turn_meta(self.chat_id, self.message_id, {"task_id": self.task_id})
        self.opened = True
        try:
            # Case 8: lets a cancel of this task ask the page whether it was
            # Stop or Send now. Idempotent in the page.
            await (await self._emitter())({"type": "execute", "data": {"code": _INTENT_HOOK_JS}})
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
        output = self.renderer.output
        emit = await self._emitter()
        for ev in _relay_output_events(self.sent_output, output):
            await emit({"type": "response:completion", "data": ev})
        boundary = _output_item_boundary(self.sent_output, output)
        self.sent_output = copy.deepcopy(output)
        from open_webui.tasks import save_response_stream

        await save_response_stream(_owui_redis(), self.task_id, self.chat_id, self.message_id,
                                   self.renderer.text, self.sent_output)
        if boundary:
            await self._persist(done=False)

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
        self.run_id = run_id

    def _adopt_recoveries_from(self, messages: list) -> None:
        """Restart recoveries that rule E maps to M and whose events never
        reached this socket (they ran while it was down, case 4)."""
        for run, tool in _history_user_turns(messages):
            if (tool == RECOVERY_SOURCE_TOOL and run not in self.runs
                    and run_message_id(self.session_key, self.chat_id, run, messages) == self.message_id):
                self.runs.append(run)

    async def end(self, row: dict, stop_notice_before: bool = False) -> None:
        """The current run settled; `row` is its settled session row (DESIGN
        2.5, 2.7). Keep M open for a recovery or a busy session, resume an
        outside kill, or finalize."""
        if self.closed:
            return
        decision = next_continuation(row, stop_notice_before, self.resumes)
        if decision in ("gateway", "wait"):
            pipe_log(f"  turn writer: {self.message_id and self.message_id[:8]}... stays open "
                     f"({decision}) after run {self.run_id[:24]}...")
            return
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
        await self.finalize(note)

    async def on_hello(self) -> None:
        """A new socket: send a resume the closing Gateway refused."""
        if self.pending_resume and not self.closed:
            await self._send_resume()

    async def _send_resume(self) -> None:
        shown = (self.renderer.visible_text or "").strip()
        if shown:
            directive = (
                "[system] Your previous reply was cut off partway through. The user can "
                "already see the following partial text, and it cannot be edited or "
                f"retracted:\n\n---\n{shown[-1500:]}\n---\n\n"
                "Continue that reply seamlessly from exactly where it stops. Do not repeat "
                "any of it, do not restart the answer, and do not mention the interruption.")
        else:
            directive = (
                "[system] Your previous run was cut off before you produced any output. "
                "Answer the most recent message now, from scratch. Do not mention the "
                "interruption.")
        key = pipe_run_id(self.chat_id, self.message_id, "resume")
        try:
            resp = await self.conn.send_request("chat.send", _owui_chat_send_params(
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
                per_run = [_history_span_messages(hist, r) for r in self.runs] if hist else [None]
                if all(m is not None for m in per_run):
                    rebuilt = _history_renderer([m for msgs in per_run for m in msgs])
                    if rebuilt.output:
                        self.renderer = rebuilt
            if not self.opened:
                if not (self._showable() or note) or not await self.open(takeover=self.takeover):
                    return
            output = [i for i in self.renderer.finished_output()
                      if not _is_sentinel_message_item(i)]
            text = self.renderer.text.strip()
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

    def _rebuild_wake(self, hist: dict | None) -> None:
        """Repair lost events from the wake's window of the transcript. A wake
        has no closing message to prove its span complete (only the first wake
        into a session gets one), so the transcript replaces what streamed only
        when it holds at least as much: it never takes content away."""
        if not hist or self.wake.get("at") is None or self.wake.get("end") is None:
            return
        rebuilt = _history_renderer(wake_span(hist.get("messages"), self.wake["at"],
                                              self.wake["end"]))
        if (len(rebuilt.output) >= len(self.renderer.output)
                and len(rebuilt.text) >= len(self.renderer.text) and rebuilt.output):
            self.renderer = rebuilt

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
            await self.conn.send_request(
                "chat.abort", {"sessionKey": self.session_key, "runId": self.run_id}, timeout=15)
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


async def _turn_route_event(conn: "_GatewayConnection", payload: dict) -> bool:
    """Queue `payload` for its run's writer when the run is a turn this
    connection writes: an OWUI session key and no live request consuming it.
    A run whose request ended before it is handed over (DESIGN 2.6). True
    when handled here."""
    sess, run = payload.get("sessionKey") or "", payload.get("runId") or ""
    if not (getattr(conn, "writes_turns", False) and sess and run) or conn._retired:
        return False
    conn.note_session_id(sess, payload.get("sessionId"))
    parsed = conn.parse_owui_session_key(sess)
    if not parsed or conn.consumers_for_event(payload):
        return False
    if run.startswith(ACTION_RUN_PREFIX):
        return True  # the Status Action's own runs render nothing
    _note_heartbeat_run(conn, payload)
    if (sess, run) in conn._turn_skip_runs:
        return True
    if conn.was_delivered_live(sess, run):
        # A request showed this run to the end, or a Stop / Send now closed
        # its message: a late event reopens nothing.
        return True
    pump = conn._turn_pumps.get((sess, run))
    if pump is None:
        pump = conn._turn_pumps[(sess, run)] = _RunPump()
        target = conn._run_targets.get(f"{sess}:{run}")
        if target is not None:
            # DESIGN 2.6: the request streaming this run ended before it. The
            # writer continues in the same message from the shadow renderer,
            # which already holds this event.
            pump.handover = {"message_id": target["message_id"],
                             "renderer": conn._run_renderers.get(f"{sess}:{run}")}
            conn.unregister_run_target(sess, run)
        spawn_background(_turn_pump_loop(conn, sess, run, parsed, pump))
        spawn_background(conn.ensure_message_subscription(sess))
        if target is not None:
            return True
    if payload.get("state") in ("final", "error", "aborted"):
        pump.stop_notice_before = conn._stop_notice_seen
    pump.queue.put_nowait(("event", payload))
    return True


def _note_heartbeat_run(conn: "_GatewayConnection", payload: dict) -> None:
    """Condition B: a heartbeat run never gets a message. Its agent events
    carry isHeartbeat (server-chat-CbnqM5f6.mjs:791, 823-826); its session.tool
    copies do not, so the run is remembered."""
    sess, run = payload.get("sessionKey"), payload.get("runId")
    if payload.get("isHeartbeat") is not True or not (sess and run):
        return
    conn._turn_skip_runs[(sess, run)] = True
    if len(conn._turn_skip_runs) > 500:
        for stale in list(conn._turn_skip_runs)[:250]:
            conn._turn_skip_runs.pop(stale, None)


def _turn_settled(conn: "_GatewayConnection", session_key: str, row: dict) -> None:
    """The session went idle: every run pumped on it has ended. Its settled
    row decides each writer's next step (DESIGN 2.5, 2.7)."""
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
        if handed is not None:
            pump.writer = _TurnWriter(conn, session_key=sess, chat_id=chat_id, user_id=user_id,
                                      run_id=run, message_id=handed["message_id"], takeover=True,
                                      renderer=handed["renderer"], thinking=handed.get("thinking"))
            conn._turn_writers[(chat_id, handed["message_id"])] = pump.writer
            await pump.writer.feed(None)
        elif re.fullmatch(_UUID_RE, run) and run == await conn.session_id_for(sess):
            pump.wake = True
        else:
            pump.writer = await _turn_writer_for(conn, sess, run, user_id, chat_id)
        while True:
            kind, item = await pump.queue.get()
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


async def _wake_bind(conn: "_GatewayConnection", sess: str, run: str, user_id: str,
                     chat_id: str, pump: _RunPump, at_ms: float) -> None:
    """Give the wake its message: the one its own admitted turn names. Until
    that turn is committed (the embedded runtime), the events wait."""
    try:
        messages = (await conn.session_history(sess, limit=50, max_chars=200)).get("messages")
    except Exception as ex:
        pipe_log(f"  turn writer: chat.history for a wake failed: {ex!r}")
        return
    bound = conn._wake_turns_bound.setdefault(sess, set())
    turn = wake_turn_at(messages, at_ms, bound)
    if turn is None:
        return
    bound.add(turn["key"])
    mid = turn_message_id(sess, turn["key"], chat_id)
    pump.writer = _TurnWriter(conn, session_key=sess, chat_id=chat_id, user_id=user_id,
                              run_id=run, message_id=mid, wake=dict(turn))
    conn._turn_writers[(chat_id, mid)] = pump.writer
    pipe_log(f"  turn writer: wake turn {turn['key'][:8]}... (cron job {turn['job'][:8]}...) "
             f"is message {mid[:8]}...")


async def _wake_step(conn: "_GatewayConnection", sess: str, run: str, user_id: str,
                     chat_id: str, pump: _RunPump, kind: str, item) -> bool:
    """One queue item of a cron wake. A session row never ends a wake: the
    row stays idle while it runs, and a row pushed for another run (a message
    queued behind it) says nothing about it. Its own terminal event does.
    True when the wake is over and the pump can go."""
    if kind == "settled":
        return False
    if kind == "wake-ended":
        await _wake_finish(conn, sess, run, pump, item)
        return True
    if pump.writer is None:
        pump.held.append(item)
        await _wake_bind(conn, sess, run, user_id, chat_id, pump, _event_ms(item))
        if pump.writer is not None:
            held, pump.held = pump.held, []
            for ev in held:
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
        pipe_log(f"  turn writer: a wake on {sess[-12:]} ended before its turn was found; "
                 f"{len(pump.held)} event(s) not shown")
    else:
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
        if pump.writer.wake["job"] not in running:
            pump.queue.put_nowait(("wake-ended", time.time() * 1000))


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
    """Read the row of every session with an open pump (except `skip`, known
    active) and end the ones that went idle while their settle was missed."""
    idle = {sess for (sess, _run) in conn._turn_pumps if sess not in skip}
    for sess in idle:
        try:
            lst = await conn.send_request("sessions.list", {"search": sess[-36:], "limit": 10},
                                          timeout=15)
        except Exception as ex:
            pipe_log(f"  turn writer: row read after hello failed: {ex!r}")
            continue
        row = next((r for r in (lst.get("sessions") or []) if r.get("key") == sess), None)
        if row is not None and not row.get("hasActiveRun"):
            _turn_settled(conn, sess, row)
