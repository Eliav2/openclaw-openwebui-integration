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


class _TurnWriter:
    """Writes run `run_id` into message `message_id` of chat `chat_id`."""

    def __init__(self, conn: "_GatewayConnection", *, session_key: str, chat_id: str,
                 user_id: str, run_id: str, message_id: str | None):
        self.conn = conn
        self.session_key, self.chat_id, self.user_id = session_key, chat_id, user_id
        self.run_id, self.message_id = run_id, message_id
        self.renderer = _TurnRenderer()
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

    async def open(self, takeover: bool = False) -> bool:
        """Claim the message, introduce it to open tabs and register the
        writer's task. False when there is nothing to open (a run that renders
        nothing) or another live writer holds the message."""
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
        pipe_log(f"  turn writer: opened {self.message_id[:8]}... ({claim['how']}) for run "
                 f"{self.run_id[:24]}... in chat {self.chat_id[:8]}...")
        return True

    def _showable(self) -> bool:
        """Condition B: a tool call start, or text that is not a silent
        sentinel or a prefix of one. Whichever comes first."""
        if any(i.get("type") == "function_call" for i in self.renderer.output):
            return True
        return _relay_content_is_showable(self.renderer.visible_text or "")

    async def feed(self, payload: dict) -> None:
        """One event of the run: grow the renderer, open the message on the
        first showable item, then stream what changed."""
        if self.closed:
            return
        try:
            self.renderer.feed(payload)
        except Exception as ex:
            pipe_log(f"  turn writer: renderer feed failed: {ex}")
        if not self.opened:
            if not self._showable():
                return
            if not await self.open():
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
                owui_chat_id=self.chat_id, owui_user_id=self.user_id), timeout=30)
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
            if hist:
                self._adopt_recoveries_from(hist.get("messages") or [])
            per_run = [_history_span_messages(hist, r) for r in self.runs] if hist else [None]
            if all(m is not None for m in per_run):
                rebuilt = _history_renderer([m for msgs in per_run for m in msgs])
                if rebuilt.output:
                    self.renderer = rebuilt
            if not self.opened:
                if not (self._showable() or note) or not await self.open():
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

    async def _stopped(self) -> None:
        """Open WebUI cancelled the writer's task (the page's Stop): abort the
        run and finalize what M has."""
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
        self.stop_notice_before = False


async def _turn_route_event(conn: "_GatewayConnection", payload: dict) -> bool:
    """Queue `payload` for its run's writer when the run is a turn this
    connection writes: an OWUI session key, no live request consuming it, and
    not an inline turn's own run (that one is handed over, DESIGN 2.6). True
    when handled here."""
    sess, run = payload.get("sessionKey") or "", payload.get("runId") or ""
    if not (getattr(conn, "writes_turns", False) and sess and run) or conn._retired:
        return False
    parsed = conn.parse_owui_session_key(sess)
    if not parsed or conn.consumers_for_event(payload) or f"{sess}:{run}" in conn._run_targets:
        return False
    if run.startswith(ACTION_RUN_PREFIX):
        return True  # the Status Action's own runs render nothing
    pump = conn._turn_pumps.get((sess, run))
    if pump is None:
        pump = conn._turn_pumps[(sess, run)] = _RunPump()
        spawn_background(_turn_pump_loop(conn, sess, run, parsed, pump))
        spawn_background(conn.ensure_message_subscription(sess))
    if payload.get("state") in ("final", "error", "aborted"):
        pump.stop_notice_before = conn._stop_notice_seen
    pump.queue.put_nowait(("event", payload))
    return True


def _turn_settled(conn: "_GatewayConnection", session_key: str, row: dict) -> None:
    """The session went idle: every run pumped on it has ended. Its settled
    row decides each writer's next step (DESIGN 2.5, 2.7)."""
    for (sess, _run), pump in list(conn._turn_pumps.items()):
        if sess == session_key:
            pump.queue.put_nowait(("settled", row))


async def _turn_writer_for(conn: "_GatewayConnection", sess: str, run: str,
                           user_id: str, chat_id: str) -> "_TurnWriter | None":
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
    writer = _TurnWriter(conn, session_key=sess, chat_id=chat_id, user_id=user_id,
                         run_id=run, message_id=mid)
    conn._turn_writers[(chat_id, mid)] = writer
    return writer


async def _turn_pump_loop(conn: "_GatewayConnection", sess: str, run: str,
                          parsed: tuple, pump: _RunPump) -> None:
    user_id, chat_id = parsed
    try:
        pump.writer = await _turn_writer_for(conn, sess, run, user_id, chat_id)
        while True:
            kind, item = await pump.queue.get()
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


async def _turn_after_hello(conn: "_GatewayConnection") -> None:
    """After a reconnect: send resumes the closing Gateway refused, and end
    the runs whose session is no longer active though no settle reached this
    socket (it was down). Sessions still active wait for their own settle."""
    for writer in list(conn._turn_writers.values()):
        if writer.pending_resume:
            await writer.on_hello()
    idle = {sess for (sess, _run) in conn._turn_pumps if sess not in conn.active_run_snapshot}
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
