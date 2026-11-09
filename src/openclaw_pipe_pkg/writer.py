# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The turn writer (ELI-100, docs/design/agent-turns.md 2.3-2.5)
# ---------------------------------------------------------------------------
# One writer per run rendered outside a live `pipe()` request. It owns one
# message and registers with Open WebUI's task registry so the page shows the
# chat as generating and its Stop button reaches it.


# Opens tried at a run's end while Open WebUI's DB is locked (ELI-167). Each
# waits out sqlite's busy_timeout (5 s), so 6 is about 30 s.
OPEN_AT_END_LOCKED_TRIES = 6
# M's final write waits this long for the handed-over request to end.
FINAL_STATUS_REQUEST_WAIT_S = 30
# A cancel's tail on a reply's status lines: "Stopped" from `park`, then the
# empty closing line.
_CANCEL_STATUS_TAIL = ("Stopped", "")


def _final_status_history(history: list | None, lines: list, stopped: bool) -> tuple[list, list]:
    """M's final status lines (cancel tail dropped) and the ones it adds."""
    kept = [h for h in (history or []) if isinstance(h, dict)]
    while kept and kept[-1].get("done") is not False and kept[-1].get("description") in _CANCEL_STATUS_TAIL:
        kept.pop()
    if stopped:
        new = [{"description": "Stopped", "done": True}]
    else:
        new = [{"description": line, "done": True} for line in lines] or [{"description": "", "done": True}]
    return kept + new, new


# Message meta: the user's Stop ended this reply. Nothing adds to it afterwards
# (`_reconcile_chat_ended_runs`). An Open WebUI shutdown cancels a reply the
# same way, but its run goes on (ELI-94).
STOPPED_META = "openclaw_stopped"


async def _mark_stopped(chat_id: str, message_id: str, request=None) -> None:
    """Mark the reply stopped before any `done: true` save, so no restart finds
    it finished and unmarked. Marked again after `request`'s own save."""
    await _write_stop_mark(chat_id, message_id)
    if request is not None and not request.done():
        async def after_request():
            await asyncio.wait({request})
            await _write_stop_mark(chat_id, message_id)
        spawn_background(after_request())


async def _write_stop_mark(chat_id: str, message_id: str) -> None:
    try:
        await _set_turn_meta(chat_id, message_id, {STOPPED_META: True})
    except Exception as ex:
        pipe_log(f"  reply {message_id[:8]}...: marking it stopped failed: {ex!r}")


def _ended_by_stop(message: dict) -> bool:
    return bool((message.get("meta") or {}).get(STOPPED_META))


def _db_locked(ex: BaseException) -> bool:
    """sqlite's `database is locked`, raw or wrapped by SQLAlchemy."""
    seen = set()
    while ex is not None and id(ex) not in seen:
        seen.add(id(ex))
        if "database is locked" in str(ex):
            return True
        ex = ex.__cause__ or ex.__context__
    return False


async def _set_turn_meta(chat_id: str, message_id: str, meta: dict) -> bool:
    """Merge `meta` into message `message_id` under the chat's row lock, never
    cut short by a cancel (ELI-205)."""
    return await _uncancellable(_set_turn_meta_now(chat_id, message_id, meta))


async def _set_turn_meta_now(chat_id: str, message_id: str, meta: dict) -> bool:
    from open_webui.models.chats import Chats
    from sqlalchemy.orm.attributes import flag_modified

    async with Chats._chat_transaction(chat_id) as (db, chat):
        if chat is None:
            return False
        data = chat.chat or {}
        history = data.get("history") or {}
        msg = (history.get("messages") or {}).get(message_id)
        if msg is None:
            return False
        msg["meta"] = {**(msg.get("meta") or {}), **meta}
        chat.chat = {**data, "history": history}
        flag_modified(chat, "chat")
        return True


async def _introduce_turn_message(chat_id: str, user_id: str, messages: dict, current_id: str | None,
                                  message_id: str | None = None) -> None:
    """Show a newly claimed message in every open tab via the shared
    `chat:messages` event, as Open WebUI does for its own turns."""
    from open_webui.socket.main import sio

    await sio.emit("events", {
        "chat_id": chat_id, "message_id": message_id or current_id, "user_id": user_id, "shared": True,
        "data": {"type": "chat:messages", "data": {"messages": messages, "currentId": current_id}},
    }, room=f"chat:{chat_id}")


def _turn_message_patch(message_id: str, parent: str | None, message: dict) -> dict:
    """The `chat:messages` nodes for `message`. The parent gets only the link:
    the page merges childrenIds as a set and keeps its other fields."""
    nodes = {message_id: message}
    if parent:
        nodes[parent] = {"id": parent, "childrenIds": [message_id]}
    return nodes


def _owui_redis():
    """Open WebUI's redis client, or None in a single-process install."""
    app = getattr(sys.modules.get("open_webui.main"), "app", None)
    return getattr(getattr(app, "state", None), "redis", None)


async def _save_response_stream(task_id, chat_id, message_id, text: str, output: list) -> None:
    """Open WebUI's mid-turn copy of a message. 0.12.3 dropped the `content`
    argument (the GET rebuilds text from `output`)."""
    import inspect
    from open_webui.tasks import save_response_stream

    if "content" in inspect.signature(save_response_stream).parameters:
        await save_response_stream(_owui_redis(), task_id, chat_id, message_id, text, output)
    else:
        await save_response_stream(_owui_redis(), task_id, chat_id, message_id, output)


async def _clear_response_stream(task_id: str | None) -> None:
    """Drop the mid-turn copy once the row is saved whole."""
    if not task_id:
        return
    try:
        from open_webui.tasks import clear_response_stream

        await clear_response_stream(_owui_redis(), task_id)
    except Exception as ex:
        pipe_log(f"  turn writer: clearing the response stream failed: {ex!r}")


# Open WebUI's event names are a closed catalog, so consumers tell the Pipe's
# `chat.finished` from the middleware's by `source`.
CHAT_FINISHED_SOURCE = "openclaw-pipe"
# Claims, one per reply, so a reply is published once however many paths
# finish it (a request can run twice, in any worker). Kept off the chat row,
# whose every write rewrites the whole chat: Open WebUI's Redis when it has
# one (its workers share it), else a set on this worker (Open WebUI without
# Redis runs one worker). Keyed by the reply's message id, which never
# repeats, and never released by time: a claim goes only with its reply's
# message row (_drop_chat_finished_claims_of_deleted_replies). A path that can
# outlive a restart without Redis checks the reply itself (a writer taking
# over a finished message does not publish it).
CHAT_FINISHED_CLAIMS_KEY = "openclaw:chat_finished"
_CHAT_FINISHED_CLAIMS_ATTR = "_openclaw_chat_finished_claims"
_chat_finished_claims_local: set = set()
# A Redis error is logged once per process, not per reply.
_chat_finished_redis_error_logged = False


def _chat_finished_claims() -> set:
    """This worker's claims, on Open WebUI's socket module so a Pipe reload
    keeps them."""
    try:
        import open_webui.socket.main as _owui_socket_main
    except Exception:
        return _chat_finished_claims_local
    claims = getattr(_owui_socket_main, _CHAT_FINISHED_CLAIMS_ATTR, None)
    if claims is None:
        claims = set()
        setattr(_owui_socket_main, _CHAT_FINISHED_CLAIMS_ATTR, claims)
    return claims


def _chat_finished_redis_key() -> str:
    from open_webui.env import REDIS_KEY_PREFIX
    return f"{REDIS_KEY_PREFIX}:{CHAT_FINISHED_CLAIMS_KEY}"


async def _claim_chat_finished(chat_id: str, message_id: str) -> bool:
    """Whether this caller is the reply's one publisher: an atomic test-and-set
    (Redis HSETNX across workers, else one set on this event loop)."""
    global _chat_finished_redis_error_logged
    claim = f"{chat_id}:{message_id}"
    redis = _owui_redis()
    if redis is not None:
        try:
            return bool(await redis.hsetnx(_chat_finished_redis_key(), claim, "1"))
        except Exception as ex:
            # Errs toward publishing: this process's claim still stops its own
            # repeats, while two processes may each publish once. A duplicate
            # notification is better than a reply that never notifies.
            if not _chat_finished_redis_error_logged:
                _chat_finished_redis_error_logged = True
                pipe_log(f"  chat.finished claim: Redis failed ({ex!r}); claiming in this process")
    claims = _chat_finished_claims()
    if claim in claims:
        return False
    claims.add(claim)
    return True


async def _drop_chat_finished_claims_of_deleted_replies() -> None:
    """Garbage collection only, run once per Pipe load: drop the claims of
    replies whose message row is gone (the chat or message was deleted), as no
    path can finish those again."""
    try:
        redis = _owui_redis()
        if redis is not None:
            key = _chat_finished_redis_key()
            claims = [c.decode() if isinstance(c, bytes) else c for c in await redis.hkeys(key)]
        else:
            claims = list(_chat_finished_claims())
        gone = []
        for claim in claims:
            chat_id, _, message_id = claim.rpartition(":")
            if await _finished_message(chat_id, message_id) is None:
                gone.append(claim)
        if not gone:
            return
        if redis is not None:
            await redis.hdel(key, *gone)
        else:
            _chat_finished_claims().difference_update(gone)
    except Exception as ex:
        pipe_log(f"  dropping chat.finished claims of deleted replies failed: {ex!r}")


# Open WebUI's output item and content part that carry a reply's text.
OUTPUT_MESSAGE_ITEM = "message"
OUTPUT_TEXT_PART = "output_text"


def _output_reply_text(output) -> str:
    """The text of a reply's `message` items, as their cards show it."""
    texts = []
    for item in output or []:
        if isinstance(item, dict) and item.get("type") == OUTPUT_MESSAGE_ITEM:
            text = "".join(p.get("text") or "" for p in item.get("content") or []
                           if isinstance(p, dict) and p.get("type") == OUTPUT_TEXT_PART).strip()
            if text:
                texts.append(text)
    return "\n\n".join(texts)


async def _finished_message(chat_id: str, message_id: str):
    """The reply's own chat_message row, not the whole chat."""
    from open_webui.models.chat_messages import ChatMessages
    return await ChatMessages.get_message_by_id(f"{chat_id}-{message_id}")


async def _publish_chat_finished(chat_id: str, message_id: str, user_id: str,
                                 text: str | None = None) -> None:
    """Publish `chat.finished` for a reply saved whole, shaped like the
    middleware's. Its own fires at request end, too early for a handed-over
    reply and never for an agent turn. Once per reply."""
    app = getattr(sys.modules.get("open_webui.main"), "app", None)
    if app is None or not message_id:
        return
    try:
        from open_webui.events import EVENTS, publish_event
        from open_webui.models.chats import Chats
        from open_webui.models.users import Users
        from open_webui.utils.chat_id import is_saved_chat_id
        from starlette.requests import Request

        if not is_saved_chat_id(chat_id):
            return
        row = await _finished_message(chat_id, message_id)
        if row is None or row.done is not True or not await _claim_chat_finished(chat_id, message_id):
            return
        if text is None:
            # A reply of cards only has no content: its text is in its output.
            text = _coerce_text(row.content) or _output_reply_text(getattr(row, "output", None))
        title = await Chats.get_chat_title_by_id(chat_id) or ""
        user = await Users.get_user_by_id(user_id)
        try:
            from open_webui.models.config import Config
            base = (await Config.get("webui.url") or "").rstrip("/")
        except Exception:
            base = ""
        # Sinks read `request.app`; an agent turn has no request.
        request = Request({"type": "http", "app": app, "method": "POST", "path": "/",
                           "headers": [], "query_string": b""})
        await publish_event(
            request, EVENTS.CHAT_FINISHED,
            actor=user or {"id": user_id}, subject_id=chat_id, subject_type="chat",
            source=CHAT_FINISHED_SOURCE,
            data={"user_id": user_id, "chat_id": chat_id, "message_id": message_id,
                  "model_id": row.model_id, "title": title,
                  "url": f"{base}/c/{chat_id}", "message": text},
            message=title or "Chat finished")
    except Exception as ex:
        pipe_log(f"  turn writer: publishing chat.finished for {message_id[:8]}... failed: {ex!r}")


def _owui_request_task(task_id: str | None):
    """Open WebUI's asyncio task for a chat request (Pipe call, then its
    end-of-stream save). None once ended or unknown."""
    if not task_id:
        return None
    return (getattr(sys.modules.get("open_webui.tasks"), "tasks", None) or {}).get(task_id)

# The Gateway's error for a claude-cli turn with no final text. A
# `sessions_send` turn often ends silent on purpose and still fails (ELI-336).
EMPTY_RESPONSE_ERROR = "CLI backend returned an empty response."

TURN_CUT_OFF_NOTE = ("_(This reply was cut off: the run was interrupted again after "
                     f"{MAX_TURN_RESUMES} attempts to continue it.)_")


def _run_prompt_text(span: list | None) -> str:
    """The needs-input prompt a run's text ends with, or ""."""
    return _prompt_tail(_last_item_text(_history_renderer(span))) if span else ""


def _last_item_text(renderer: "_TurnRenderer") -> str:
    """The last message item's text (fences never span items), or ""."""
    last = renderer.output[-1] if renderer.output else None
    if last is None or last.get("type") != "message":
        return ""
    return ((last.get("content") or [{}])[0].get("text")) or ""


def _prompt_tail(text: str, start: int = 0) -> str:
    """`text` from its last needs-input trigger line on, or "". A trigger in
    a code fence is a quote (ELI-107)."""
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


# A request longer than this is quoted as its head and tail.
RESUME_REQUEST_MAX = 6000
RESUME_REQUEST_HEAD = 2000
# OpenClaw's inbound metadata blocks at the start of a stored user turn.
_INBOUND_META_RE = re.compile(
    r"\A\s*[A-Z][^\n]{0,80}\(untrusted metadata\):[ \t]*\n```[^\n]*\n.*?\n```[ \t]*(?:\n+|\Z)", re.S)


def _strip_inbound_meta(text: str) -> str:
    """`text` without the metadata blocks in front of it."""
    while True:
        m = _INBOUND_META_RE.match(text or "")
        if not m:
            return text
        text = text[m.end():]


def _quote_block(text: str) -> str:
    """`text` between dash delimiters longer than any dash-only line in it."""
    dashes = [len(x.strip()) for x in text.split("\n") if x.strip() and set(x.strip()) == {"-"}]
    delim = "-" * max([3] + [n + 1 for n in dashes])
    return f"{delim}\n{text}\n{delim}"


def _clip_middle(text: str, limit: int = RESUME_REQUEST_MAX, head: int = RESUME_REQUEST_HEAD) -> str:
    """`text` clipped to head and tail, keeping the closing question."""
    if len(text) <= limit:
        return text
    tail = limit - head
    return f"{text[:head]}\n[... {len(text) - limit} characters omitted ...]\n{text[-tail:]}"


def _resume_directive(shown: str | None, request: str | None = None,
                      attachments: int = 0) -> str:
    """The message a resume sends. It explains the kill (a killed tool reads
    as exit 137) and carries `request`, since the resume runs in a new
    claude-cli session. Attachments do not travel."""
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


def _keep_pipe_notes(streamed: list, renderer: "_TurnRenderer") -> None:
    """Put the Pipe's own notes back after a transcript rebuild (ELI-229)."""
    output = renderer.output
    have = {i.get("id") for i in output}
    for at, item in enumerate(streamed):
        if not str(item.get("id") or "").startswith(_NOTE_ITEM_PREFIX) or item.get("id") in have:
            continue
        before = next((p.get("id") for p in reversed(streamed[:at]) if p.get("id") in have), None)
        pos = next((n + 1 for n, i in enumerate(output) if i.get("id") == before), len(output)) \
            if before else 0
        output.insert(pos, copy.deepcopy(item))
        have.add(item.get("id"))


# Notes for a reply interrupted before it sent its message (ELI-188).
REPLY_SENT_WITH_NEXT_NOTE = ("_(This message was sent together with your next one: "
                             "the answer is in the next reply.)_")
REPLY_NOT_SENT_NOTE = "_(Stopped: this message was not sent to the agent.)_"
# The Gateway (2026.10.10+) steers a message sent mid-run into that run.
STEER_PENDING_STATUS = "Sent while the agent works: it reads it at its next step…"
REPLY_SEND_FAILED_NOTE = ("_(This message was to go together with your next one, "
                          "which could not be sent: see the next reply.)_")


async def _close_reply_status(ref: dict) -> None:
    """Close a status line left open in the saved message (ELI-200), after the
    request's own save. The saved state decides: on a Stop two concurrent
    writes race and the status one can be lost."""
    request = _owui_request_task(ref.get("request_task_id"))
    if request is not None and not request.done():
        await asyncio.wait({request})
    from open_webui.models.chats import Chats
    from open_webui.socket.main import get_event_emitter

    message_id = ref["message_id"]
    try:
        saved = await Chats.get_message_by_id_and_message_id(ref["chat_id"], message_id) or {}
        last = (saved.get("statusHistory") or [None])[-1]
        if not isinstance(last, dict) or last.get("done") is not False:
            return
        emit = await get_event_emitter({"chat_id": ref["chat_id"], "message_id": message_id,
                                        "user_id": ref.get("user_id")}, update_db=True)
        await emit({"type": "status", "data": {"description": "", "done": True}})
        pipe_log(f"  reply {message_id[:8]}... closed: its status line was left open")
    except Exception as ex:
        pipe_log(f"  reply {message_id[:8]}...: closing its status failed: {ex!r}")


async def _close_unsent_reply(conn: "_GatewayConnection", ref: dict, note: str) -> None:
    """End a reply cancelled before it sent its message, with `note`. Waits
    for the request to end first, or Open WebUI's own save lands over this."""
    request = _owui_request_task(ref.get("request_task_id"))
    if request is not None and not request.done():
        await asyncio.wait({request})
    from open_webui.models.chats import Chats
    from open_webui.socket.main import get_event_emitter

    chat_id, message_id = ref["chat_id"], ref["message_id"]
    try:
        emit = await get_event_emitter({"chat_id": chat_id, "message_id": message_id,
                                        "user_id": ref.get("user_id")}, update_db=True)
        await emit({"type": "status", "data": {"description": "", "done": True}})
        async with conn._chat_write_lock(chat_id):
            current = await Chats.get_message_by_id_and_message_id(chat_id, message_id) or {}
            text = (current.get("content") or "").strip()
            text = f"{text}\n\n{note}" if text else note
            output = list(current.get("output") or []) + [_turn_note_item(note)]
            await _owui_upsert_message(
                chat_id, message_id, {"content": text, "output": output, "done": True})
        await emit({"type": "chat:completion", "data": {"done": True, "content": text,
                                                         "output": output}})
        pipe_log(f"  reply {message_id[:8]}... ended: {note}")
    except Exception as ex:
        pipe_log(f"  reply {message_id[:8]}...: closing note failed: {ex!r}")


def _settle_text(text: str) -> str:
    return "".join((text or "").split())


def _settle_misplaced(saved: list, parts: list) -> bool:
    """The saved replies hold the transcript's parts with only the split
    points moved: in order, each one a piece of the run's text that starts
    no later than where the ones before reach, together all of it. A piece
    may repeat what an earlier reply shows (after a late steer the first
    reply runs on into the next answers). Whitespace-blind."""
    if saved == parts or len(saved) != len(parts):
        return False
    whole, end = "".join(parts), 0  # `end`: how far the replies so far reach
    for was in saved:
        start = whole.rfind(was, 0, end + len(was))  # the latest start at or before `end`
        if start < 0:
            return False
        end = max(end, start + len(was))
    return end == len(whole)


def _steer_chain(history: dict, refs: list) -> list:
    """`refs` in the transcript's order: the host run's reply, then the reply
    of each message steered into it. A message whose steer came after the
    run ended (2026.10.10 publishes the steer target with no order to the
    rest) is in no reply's chain; the transcript still shows it, so it
    joins here. `refs` as they are when the transcript does not show them all."""
    messages = (history or {}).get("messages")
    if not refs or not isinstance(messages, list):
        return refs
    host, by_message = refs[0]["span_run"], {r["message_id"]: r for r in refs[1:]}
    # A turn after the host's last closing message is a turn of its own, even
    # before its answer is stored.
    closers = [n for n, m in enumerate(messages) if _run_closed_by(m) == host]
    chain = [refs[0]]
    for n, message in enumerate(messages[:closers[-1] if closers else len(messages)]):
        run = _history_user_run_id(message)
        ref = _settle_ref(run) if run else None
        if ref is not None and _history_steered_into(history, n) == host:
            chain.append(by_message.pop(ref["message_id"], None) or ref)
    return refs if by_message else chain


# The Pipe's notes that a steered message's answer is in another reply: no
# run text, and wrong once the settle gives the reply its answer.
_STEER_NOTES = (REPLY_SENT_WITH_NEXT_NOTE, REPLY_SEND_FAILED_NOTE)


def _settle_shown(content: str) -> str:
    """A saved reply's text as the settle compares it: no steer notes."""
    for note in _STEER_NOTES:
        content = (content or "").replace(note, "")
    return _settle_text(content)


# A tool card in a message's `output` (`_tool_call_started_event`).
_TOOL_CARD_TYPE = "function_call"


def _tool_card_ids(output: list) -> set:
    """The tool cards in `output`, by `call_id or id` (how consumers read them)."""
    return {i.get("call_id") or i.get("id") for i in output or ()
            if isinstance(i, dict) and i.get("type") == _TOOL_CARD_TYPE}


async def _settle_waits(conn: "_GatewayConnection", refs: list) -> None:
    """Until no one still saves a reply of `refs`: a turn writer (also while it
    finalizes), or an Open WebUI request in its chat, which saves at its end."""
    for writer in (conn._turn_writers.get((r["chat_id"], r["message_id"])) for r in refs):
        if writer is not None and writer.saving():
            await writer.wait_done()
    try:
        from open_webui.tasks import list_task_ids_by_item_id, tasks
        for chat_id in dict.fromkeys(r["chat_id"] for r in refs):
            for task_id in await list_task_ids_by_item_id(_owui_redis(), chat_id):
                task = tasks.get(task_id)
                if task is not None and not task.done() and task is not asyncio.current_task():
                    await asyncio.wait({task})
    except ImportError:
        pass  # no Open WebUI tasks to wait for


async def _settle_steered_replies(conn: "_GatewayConnection", session_key: str, refs: list) -> None:
    """Re-split the replies around a steer from the transcript once the run
    ended. On claude-cli the Gateway forwards the answer before it commits
    the steered message, so the switch can land late. Only the split moves."""
    refs = [r for r in refs if r and r.get("chat_id") and r.get("message_id") and r.get("span_run")]
    if len(refs) < 2:
        return
    for ref in refs:
        request = _owui_request_task(ref.get("request_task_id"))
        if request is not None and not request.done():
            await asyncio.wait({request})
    from open_webui.models.chats import Chats
    from open_webui.socket.main import get_event_emitter

    try:
        while True:
            # A reply is read once it is saved, and the transcript after that:
            # a turn writer ends when the session settled.
            await _settle_waits(conn, refs)
            hist = await conn.session_history(session_key, limit=RUN_HISTORY_LIMIT)
            chain = _steer_chain(hist, refs)
            if chain == refs:
                break
            refs = chain  # replies the transcript added: their writers too
        spans = [_history_span_messages(hist, r["span_run"]) for r in refs]
        if any(s is None for s in spans):
            pipe_log(f"  replies around a steer left as they are: the transcript closes no span of "
                     f"{', '.join(r['span_run'][:24] + '...' for r, s in zip(refs, spans) if s is None)}")
            return
        parts = [_history_renderer(s) for s in spans]
        saved = [await Chats.get_message_by_id_and_message_id(r["chat_id"], r["message_id"]) or {}
                 for r in refs]
        texts = [(m.get("content") or "") for m in saved]
        shown, truth = [_settle_shown(t) for t in texts], [_settle_text(p.text) for p in parts]
        if not _settle_misplaced(shown, truth):
            if shown != truth:
                pipe_log(f"  replies around a steer left as they are: {len(refs)} replies hold more "
                         "than the transcript's text of the run")
            return
        lost = set().union(*(_tool_card_ids(m.get("output")) for m in saved)) - set().union(
            *(_tool_card_ids(p.finished_output()) for p in parts))
        if lost:
            pipe_log(f"  replies around a steer left as they are: the transcript lacks "
                     f"{len(lost)} tool card(s) they show")
            return
        user_id = (conn.parse_owui_session_key(session_key) or (None, None))[0]
        for ref, part, was, message in zip(refs, parts, shown, saved):
            if was == _settle_text(part.text):
                continue
            # The Pipe's own notes are in no transcript (ELI-229).
            _keep_pipe_notes(list(message.get("output") or []), part)
            text, output = part.text.strip(), part.finished_output()
            maps = await _embed_fields(conn, output, message.get("embeds"))
            async with conn._chat_write_lock(ref["chat_id"]):
                await _owui_upsert_message(ref["chat_id"], ref["message_id"],
                                           {"content": text, "output": output, "done": True, **maps})
            emit = await get_event_emitter({"chat_id": ref["chat_id"], "message_id": ref["message_id"],
                                            "user_id": user_id}, update_db=False)
            if maps:
                await emit({"type": "embeds", "data": {**maps, "replace": True}})
            await emit({"type": "chat:completion", "data": {"done": True, "content": text, "output": output}})
            pipe_log(f"  reply {ref['message_id'][:8]}... settled from the transcript around a steer")
    except Exception as ex:
        pipe_log(f"  settling the replies around a steer failed: {ex!r}")


def _end_unsent_replies(conn: "_GatewayConnection", refs: list, *, sent: bool,
                        note: str | None = None) -> None:
    """End each reply in `refs` in the background (`_close_unsent_reply`)."""
    note = note or (REPLY_SENT_WITH_NEXT_NOTE if sent else REPLY_NOT_SENT_NOTE)
    for ref in refs:
        spawn_background(_close_unsent_reply(conn, ref, note))


# Open WebUI cancels identically for Stop and Send now (both call
# stopTasksByChatId), so only the page can tell them apart. This hook records
# per chat which one the user pressed. Labels are localized, so a non-English
# UI falls back to the timer.
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
  // One click answers every task it cancelled (ELI-198), so a query does not
  // clear it: a new message, sent with nothing running, does.
  document.addEventListener('submit', (e) => {
    const f = e.target;
    const input = document.querySelector('#chat-input');
    if (running() || !f || !input || !f.contains(input)) return;
    const c = chat();
    if (c) delete window.__ocIntents[c];
  }, true);
}
return true;
"""


# Open WebUI queues a message sent mid-reply in the browser only. With
# AUTO_SEND_WHILE_RUNNING on, this hook clicks Send now as soon as one
# appears, marked as Send now so the run is kept. The button is found by its
# untranslated icon. One click at a time: Open WebUI ignores a Send now while
# it is still submitting the previous one.
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
    """The intent and auto-send page hooks, with this chat's setting."""
    code = "(function () {" + _INTENT_HOOK_JS + "})();\n(function () {" + _AUTO_SEND_HOOK_JS + "})();\n"
    if chat_id:
        code += ("window.__ocAutoSend[" + json.dumps(chat_id) + "] = " + ("true" if auto_send else "false")
                 + ";\nwindow.__ocAutoSendScan();\n")
    return code


def _auto_send_on(valves) -> bool:
    return bool(getattr(valves, "AUTO_SEND_WHILE_RUNNING", True))


def _intent_query_js(chat_id: str) -> str:
    """Read the page's recorded intent. The record is not cleared: one Stop
    cancels every task of the chat and each asks (ELI-198). "none": hook but
    no click; null: no hook."""
    return (
        "const c = " + json.dumps(chat_id) + ";"
        "const all = window.__ocIntents;"
        "if (!all) return null;"
        "const i = all[c];"
        "return (i && Date.now() - i.t < 60000) ? i.kind : 'none';"
    )


# RPC bound for a tab that never answers (ELI-95).
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
    """Which button cancelled the writer's task: "stop", "send_now" or None.
    Asks every tab showing the chat; a Stop anywhere wins."""
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
                 thinking: str | None = None, wake: dict | None = None, shown: str = "",
                 request: "asyncio.Future | None" = None):
        self.conn = conn
        self.session_key, self.chat_id, self.user_id = session_key, chat_id, user_id
        self.run_id, self.message_id = run_id, message_id
        # A pipe run's message already exists (Open WebUI's stub) (DESIGN 2.6).
        self.takeover = takeover
        self.renderer = renderer if renderer is not None else _TurnRenderer()
        self.thinking = thinking  # ELI-85: a resume keeps the user's level
        # A cron wake (ELI-103): its transcript span is a time window.
        self.wake = wake
        # What earlier replies showed of an adopted run (ELI-147).
        self.shown = {run_id: shown} if shown else {}
        self.opened = False
        self._claim: dict | None = None  # a locked open resumes after it
        self.task_id: str | None = None
        self.runs = [run_id]
        # (run, output index, text length) per run. See `_from_transcript`.
        self.run_marks: list = [(run_id, 0, 0)]
        # Lifecycle `startedAt` (ms) bounds a run's transcript span.
        self.run_started: dict[str, float] = {}
        self.resumes = 0                # resume budget, counted on M (MAX_TURN_RESUMES)
        self.pending_resume = False     # a resume the Gateway refused; sent on the next hello-ok
        self.request: str | None = None  # the user's message M answers, quoted by a resume
        self.request_attachments = 0    # its images or files, which a resume cannot carry
        self.awaiting_row = False       # kept open for a Gateway recovery
        self.closed = False
        self.finalizing = False  # closed, its message still being saved
        # M's message was steered into another run (2026.10.10+): its own run
        # gets only a `chat` final with no message.
        self.steered_message = False
        # Images of a steered reply are that run's (`finish_final_images`).
        self.steered_into: str | None = None
        # A later run works: M's end leaves the active state to its reply.
        self.newer_run = False
        self.sent_output: list = []     # the `output` open tabs hold
        self.sent_text = ""
        # A handover owes M one write after the request's `done: true` save,
        # since the orphan reconcile adopts only `done: false` (ELI-115).
        self.handover_owed = renderer is not None
        self.request_task = request
        # Taken over from a gone writer by its meta (`_adopt_turn_message`).
        self.adopted = False
        if renderer is not None:
            # Tabs hold these items under Open WebUI's ids: unknown items make
            # the first diff replace them in place, not duplicate (ELI-158).
            self.sent_output = [{"type": "_shown_by_request"} for _ in renderer.output]
        self._media_failed: set = set()  # MEDIA directives that did not resolve
        # Media as the inline reply keeps them (ELI-328).
        self.gateway_media: set[str] = set()
        self.event_media: list[str] = []
        self.images_made = 0
        # The reply's maps: page by map id, and the ids open tabs were shown (`_show_maps`).
        self._map_seen: dict = {}
        self._maps_shown: set[str] = set()
        self._emit = None
        self._done = asyncio.Event()
        # A running push of M never lands after its final one (`_push_running`).
        self._push_lock = asyncio.Lock()
        # Two pumps can feed one writer; only one may claim it.
        self._open_lock = asyncio.Lock()
        self._emit_lock = asyncio.Lock()

    async def open(self, takeover: bool = False) -> bool:
        """Claim, introduce and register the message. False when another live
        writer holds it or there is nothing to open."""
        async with self._open_lock:
            return await self._open(takeover)

    async def _open(self, takeover: bool) -> bool:
        if self.opened:
            return True
        if not self.message_id:
            return False
        _register_turn_writer(self.conn.writer_id)
        # Each step once: a locked DB (ELI-167) can stop the open after the
        # claim committed, and a retry must not read it as another writer's.
        if self._claim is None:
            claim = await _claim_turn_message(self.chat_id, self.user_id, self.message_id,
                                              self.run_id, self.conn.writer_id, takeover=takeover)
            if not claim["claimed"]:
                if claim["how"] == "gone":
                    pipe_log(f"  turn writer: chat {self.chat_id[:8]}... is gone; run "
                             f"{self.run_id[:24]}... is not written")
                else:
                    pipe_log(f"  turn writer: message {self.message_id[:8]}... is held by another writer")
                return False
            self._claim = claim
            if claim["how"] == "new":
                await _introduce_turn_message(self.chat_id, self.user_id, claim["messages"],
                                              self.message_id)
        claim = self._claim
        from open_webui.tasks import create_task

        if self.task_id is None:
            self.task_id, _ = await create_task(_owui_redis(), self._hold(), id=self.chat_id)
        meta = {"task_id": self.task_id}
        if self.wake is not None:
            # For a successor to adopt this message (ELI-103 step 4).
            meta["openclaw_wake"] = {k: self.wake.get(k) for k in ("key", "at", "job")}
        await _set_turn_meta(self.chat_id, self.message_id, meta)
        self.opened = True
        try:
            # Case 8: lets a cancel ask the page Stop or Send now. Idempotent.
            await (await self._emitter())({"type": "execute", "data": {
                "code": _page_hooks_js(self.chat_id, _auto_send_on(self.conn._valves()))}})
        except Exception as ex:
            pipe_log(f"  turn writer: intent hook install failed: {ex!r}")
        pipe_log(f"  turn writer: opened {self.message_id[:8]}... ({claim['how']}) for run "
                 f"{self.run_id[:24]}... in chat {self.chat_id[:8]}...")
        return True

    async def hold_open(self) -> None:
        """M's steered message is not yet taken in and its request ended: open
        M now, not done, so it does not read finished and empty meanwhile."""
        if self.opened or not await self.open(takeover=self.takeover):
            return
        await self._push_running()
        # The DB says so once the request's own save is in (ELI-115).
        self.handover_owed = True
        await self._stream()

    async def _push_running(self) -> None:
        """Push M not done to the chat's tabs, after its request ends: that end
        marks M done in a tab, though its run goes on here."""
        if self.request_task is not None and not self.request_task.done():
            await asyncio.wait({self.request_task}, timeout=FINAL_STATUS_REQUEST_WAIT_S)
        async with self._push_lock:
            if self.closed:
                return   # being finalized: its final push shows M done, with its lines
            message = {**self._claim["messages"][self.message_id], "content": self.sent_text,
                       "output": copy.deepcopy(self.sent_output), "done": False}
            await _introduce_turn_message(
                self.chat_id, self.user_id,
                _turn_message_patch(self.message_id, self._claim.get("parent"), message), self.message_id)

    async def show_running(self) -> None:
        """A taken-over M reads running in a tab until it is finalized, never
        done without its final lines (ELI-371, ELI-377)."""
        if self.opened or await self.open(takeover=self.takeover):
            try:
                await self._push_running()
            except Exception as ex:
                pipe_log(f"  turn writer: running push of {self.message_id[:8]}... failed: {ex!r}")

    async def working(self) -> None:
        """M's message went into the running run: show it as working."""
        if self.opened and not self.closed:
            line = {"description": "Thinking...", "done": False}
            emit = await self._emitter()
            await emit({"type": "status", "data": line})
            # Saved too, for a tab that loads the chat now.
            from open_webui.models.chats import Chats
            async with self.conn._chat_write_lock(self.chat_id):
                saved = await Chats.get_message_by_id_and_message_id(self.chat_id, self.message_id) or {}
                await _owui_upsert_message(self.chat_id, self.message_id, {
                    "statusHistory": [*(saved.get("statusHistory") or []), line]})

    def _showable(self) -> bool:
        """Condition B: a tool call, or text that is no silent sentinel."""
        if any(i.get("type") == "function_call" for i in self.renderer.output):
            return True
        return _relay_content_is_showable(self.renderer.visible_text or "")

    async def feed(self, payload: dict | None) -> None:
        """Feed one event, open on the first showable item, stream the change.
        None streams what the renderer already holds."""
        if self.closed:
            return
        try:
            if payload is not None:
                data = payload.get("data") or {}
                if payload.get("stream") == "lifecycle" and data.get("phase") == "start" \
                        and isinstance(data.get("startedAt"), (int, float)) and payload.get("runId"):
                    self.run_started.setdefault(payload["runId"], data["startedAt"])
                if payload.get("stream") == "assistant":
                    for url in _event_media_urls(data):
                        if _is_gateway_media_name(url):
                            self.gateway_media.add(url)
                        elif url not in self.event_media:
                            self.event_media.append(url)
                elif (payload.get("stream") == CODEX_ITEM_STREAM and data.get("phase") == "completed"
                      and data.get("type") == "imageGeneration"):
                    self.images_made += 1
                elif (payload.get("state") == "final" and not payload.get("message")
                      and payload.get("runId") == self.run_id and not self.renderer.output
                      and steers_mid_run_messages(self.conn.gateway_version)):
                    self.steered_message = True
                self.renderer.feed(payload)
        except Exception as ex:
            pipe_log(f"  turn writer: renderer feed failed: {ex}")
        if not self.opened:
            if not self._showable():
                return
            try:
                opened = await self.open(takeover=self.takeover)
            except Exception as ex:
                if not _db_locked(ex):
                    raise
                # The run's next event tries again (ELI-167).
                pipe_log(f"  turn writer: open of {self.message_id[:8]}... waits: Open WebUI's "
                         f"database is locked; the run's next event tries again")
                return
            if not opened:
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
        """Emit the output change and keep the response stream current.
        Mid-turn the DB is written only once, at a handover."""
        output, text = await self._view(final=False)
        emit = await self._emitter()
        async with self._emit_lock:
            for ev in _relay_output_events(self.sent_output, output):
                await emit({"type": "response:completion", "data": ev})
            self.sent_output = copy.deepcopy(output)
            self.sent_text = text
        await _save_response_stream(self.task_id, self.chat_id, self.message_id, text, self.sent_output)
        await self._show_maps()
        if self.handover_owed and (self.request_task is None or self.request_task.done()):
            self.handover_owed = False
            await self._persist(done=False, output=output, text=text)

    async def _saved_embeds(self) -> list:
        """The `embeds` M holds in the DB: a live view, or a map from before a takeover."""
        from open_webui.models.chats import Chats
        try:
            saved = await Chats.get_message_by_id_and_message_id(self.chat_id, self.message_id) or {}
        except Exception as ex:
            pipe_log(f"  turn writer: reading M's embeds failed: {ex!r}")
            return []
        return saved.get("embeds") or []

    def _map_pages(self, output: list) -> list[str]:
        """The pages of the maps in `output` that were fetched."""
        return [self._map_seen[i] for i in _map_ids(output) if self._map_seen.get(i)]

    async def _embed_list(self, output: list) -> list[str]:
        """M's whole `embeds`: what it holds, then this reply's map pages."""
        return _merge_embeds(await self._saved_embeds(), self._map_pages(output))

    async def _show_maps(self) -> None:
        """Fetch the reply's maps; when one is new, show open tabs the whole list and save it in M,
        so a reload mid-reply shows it too. A tab replaces its `embeds` with an event's, and
        update_db=False means Open WebUI saves nothing of the event. A failure leaves the map new,
        for the next event to try again."""
        await _map_embeds(self.conn, self.renderer.output, self._map_seen)
        ids = [i for i in _map_ids(self.renderer.output) if self._map_seen.get(i)]
        if all(i in self._maps_shown for i in ids):
            return
        try:
            embeds = await self._embed_list(self.renderer.output)
            emit = await self._emitter()
            async with self._emit_lock:
                await emit({"type": "embeds", "data": {"embeds": embeds, "replace": True}})
            async with self.conn._chat_write_lock(self.chat_id):
                await _owui_upsert_message(self.chat_id, self.message_id, {"embeds": embeds})
        except Exception as ex:
            pipe_log(f"  turn writer: map embed of {self.message_id[:8]}... failed: {ex!r}")
            return
        self._maps_shown.update(ids)

    def _introduced(self) -> bool:
        """M is a message this writer inserted and introduced to open tabs."""
        return bool(self._claim and self._claim.get("how") == "new" and self._claim.get("parent"))

    def _rewrite(self, text: str) -> str:
        for raw, shown in self.conn.turn_rewrites(self.chat_id, self.message_id or ""):
            if raw not in text:
                continue
            # Not over the same directive quoted in code (ELI-111).
            if _media_directives(raw, True) == [raw]:
                text = _replace_directive(text, raw, shown)
            else:
                text = text.replace(raw, shown)
        # Drop an unanswered prompt's directive line (ELI-58), not in a fence (ELI-107).
        return _strip_needs_input_lines(text)

    def _note_transcript_answers(self, messages: list) -> None:
        """Rebuild answered ask-user cards from the transcript when the inline
        rewrite is gone (an Open WebUI restart, PR #20). Recorded ones stay."""
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
        """(output, text) of M as the inline reply shows it: rewrites applied,
        new directives resolved, a half-arrived directive held back mid-run."""
        output = self.renderer.finished_output() if final else copy.deepcopy(self.renderer.output)
        items = [i for i in output if i.get("type") == "message" and i.get("content")]
        # Per item: an item ends a directive (ELI-147).
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
        """Upload each new complete `MEDIA:` directive once; record a rewrite."""
        if "MEDIA:" not in text:
            return
        for directive in _media_directives(text, final):
            if directive in self._media_failed:
                continue
            fname = directive[len(_MEDIA_TRIGGER):].strip()
            if _is_gateway_media_name(fname):
                # Its image is the run's stored block, shown as M ends (ELI-328).
                self.gateway_media.add(fname)
                self.conn.note_turn_rewrite(self.chat_id, self.message_id, directive, "")
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
                       text: str | None = None, status_history: list | None = None) -> bool:
        """Write M; whether it saved. A failed upsert returns None (ELI-90)."""
        message = {"content": self.renderer.text if text is None else text,
                   "output": output if output is not None else copy.deepcopy(self.renderer.output),
                   "done": done}
        if status_history is not None:
            message["statusHistory"] = status_history
        if self._map_pages(message["output"]):
            message["embeds"] = await self._embed_list(message["output"])
        try:
            row = await _owui_upsert_message(self.chat_id, self.message_id, message)
        except Exception as ex:
            pipe_log(f"  turn writer: persist failed: {ex}")
            return False
        if row is None:
            pipe_log(f"  turn writer: persist failed: chat {self.chat_id[:8]}... not saved")
            return False
        return True

    async def _persist_final(self, output: list, text: str, stopped: bool,
                             run_usage: dict | None = None) -> tuple[list, list, bool]:
        """M's `done: true` write with its final status lines (ELI-210), after
        the handed-over request ends. Returns M's whole status history, the new
        lines and whether the row reads back finished. `run_usage`: for a row
        with no totals (ELI-177)."""
        from open_webui.models.chats import Chats

        if self.request_task is not None and not self.request_task.done():
            await asyncio.wait({self.request_task}, timeout=FINAL_STATUS_REQUEST_WAIT_S)
            if not self.request_task.done():
                pipe_log(f"  turn writer: chat {self.chat_id[:8]}... message {self.message_id[:8]}...: "
                         f"request still running after {FINAL_STATUS_REQUEST_WAIT_S} s; "
                         f"its own status write may land after this one")
        lines = [] if stopped else await _build_usage_status_lines(
            self.conn, self.session_key, run_usage=run_usage)
        # Before the lock: a map the stream never reached (a rebuilt reply) is fetched here.
        await _map_embeds(self.conn, output, self._map_seen)
        async with self.conn._chat_write_lock(self.chat_id):
            try:
                saved = await Chats.get_message_by_id_and_message_id(self.chat_id, self.message_id) or {}
            except Exception as ex:
                saved = {}
                pipe_log(f"  turn writer: reading M's status lines failed: {ex!r}")
            history, new = _final_status_history(saved.get("statusHistory"), lines, stopped)
            whole = await self._persist(done=True, output=output, text=text, status_history=history)
            if whole:
                try:
                    row = await Chats.get_message_by_id_and_message_id(self.chat_id, self.message_id) or {}
                    whole = row.get("done") is True
                except Exception as ex:
                    whole = False
                    pipe_log(f"  turn writer: reading M back failed: {ex!r}")
        return history, new, whole

    async def _push_final(self, text: str, output: list, status_history: list) -> bool:
        """Push M finished to the chat's room with its whole status history,
        the one source of its final lines in a tab: a tab never shows M done
        without them (ELI-371), and a tab replaces M's history with the push's
        while it appends each status event. Only an inserted M carries the
        parent link and moves the view: a taken-over M can have later
        messages. Returns whether the push went out."""
        if not self._claim or self.message_id not in self._claim["messages"]:
            return False
        message = {**self._claim["messages"][self.message_id],
                   "content": text, "output": output, "done": True, "statusHistory": status_history}
        pages = self._map_pages(output)
        if pages:
            message["embeds"] = _merge_embeds(message.get("embeds"), pages)
        new = self._introduced()
        try:
            await _introduce_turn_message(
                self.chat_id, self.user_id,
                _turn_message_patch(self.message_id, self._claim["parent"] if new else None, message),
                self.message_id if new else None, self.message_id)
        except Exception as ex:
            pipe_log(f"  turn writer: final push failed: {ex!r}")
            return False
        return True

    def adopt(self, run_id: str) -> None:
        """Write a run that continues M (resume or recovery) into M."""
        self.awaiting_row = False       # the recovery took M over
        if run_id not in self.runs:
            self.runs.append(run_id)
            self.run_marks.append((run_id, len(self.renderer.output), len(self.renderer.text)))
            self.renderer.run_boundary = True
        self.run_id = run_id

    def _adopt_recoveries_from(self, messages: list) -> None:
        """Add M's runs whose events never arrived (case 4), in transcript order."""
        order = []
        for run, tool in _history_user_turns(messages):
            if run in self.runs or (
                    (tool == RECOVERY_SOURCE_TOOL or parse_pipe_run_id(run) is not None)
                    and run_message_id(self.session_key, self.chat_id, run, messages) == self.message_id):
                order.append(run)
        self.runs = [*order, *(r for r in self.runs if r not in order)]

    async def end(self, row: dict, stop_notice_before: bool = False,
                  after_restart: bool = False) -> None:
        """The run settled (DESIGN 2.5, 2.7): keep M open, resume, or finalize."""
        if self.closed:
            return
        decision = next_continuation(row, stop_notice_before, self.resumes, after_restart)
        if decision in ("gateway", "wait"):
            # A recovery the Gateway gives up on tombstones the row to failed
            # (main-session-restart-recovery-CPJjd8da.mjs:1150).
            self.awaiting_row = True
            pipe_log(f"  turn writer: {self.message_id and self.message_id[:8]}... stays open "
                     f"({decision}) after run {self.run_id[:24]}...")
            return
        self.awaiting_row = False
        last = (row or {}).get("lastRunId")
        if decision == "resume" and last and last not in self.runs:
            # Only the named run's writer resumes: a second resume fails (ELI-159).
            decision = "finalize"
        if decision == "resume":
            self.resumes += 1
            self.pending_resume = True
            await self._send_resume()
            return
        note = None
        if turn_owner(row, stop_notice_before) == "pipe":
            note = TURN_CUT_OFF_NOTE
        elif (row or {}).get("status") in ("failed", "timeout") and row.get("lastRunError"):
            if not (row["lastRunError"].strip() == EMPTY_RESPONSE_ERROR and await self._run_is_inter_session()):
                request = self.request or (await self._run_request())[0]
                note = _plain_error(ERROR_RUN, row["lastRunError"], request)
        if note is None and await self._ask_question():
            return
        if not self._showable():
            await self._await_steered_answer()
        await self.finalize(note)

    async def _await_steered_answer(self) -> None:
        """Wait for the transcript to hold the answer of the run M's message
        was steered into. The idle push can come before the Gateway stores that
        answer, or commits the message. Ends on the answer, a failed run, or a
        restart without the run."""
        if not (self.takeover and steers_mid_run_messages(self.conn.gateway_version)
                and (parse_pipe_run_id(self.run_id) or {}).get("kind") == "reply"):
            return
        await _await_steered_answer(self.conn, self.session_key, self.run_id,
                                    f"turn writer: {self.message_id[:8]}...", steered=self.steered_message)

    async def _run_is_inter_session(self) -> bool:
        """True when this run answers another session's `sessions_send`."""
        try:
            hist = await self.conn.session_history(self.session_key, limit=RUN_HISTORY_LIMIT)
        except Exception as ex:
            pipe_log(f"  turn writer: chat.history failed reading the run's source: {ex!r}")
            return False
        for m in (hist or {}).get("messages") or []:
            if _history_user_run_id(m) == self.run_id:
                return (m.get("provenance") or {}).get("kind") == "inter_session"
        return False

    async def _ask_question(self) -> bool:
        """Ask a closing question as a card on the chat's tab (ELI-107) and
        adopt the answer's `input` run. False when nothing was sent; M then
        ends with the question as text."""
        if self.closed or not self.opened:
            return False
        # Only after the last question already answered in M.
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
        # A JSON form shows as readable text (ELI-142).
        self.conn.note_turn_rewrite(self.chat_id, self.message_id, prompt,
                                    _ask_user_fallback_text(prompt))
        return False

    async def _ask_card(self, prompt: str) -> bool:
        """Show the card for `prompt` and send its answer; False if not sent."""
        payload = _native_ask_user_payload(prompt)
        if payload is None:
            return False
        try:
            response = await _call_on_chat_tab(
                self.user_id, self.chat_id, self.message_id, payload, _NATIVE_CARD_TIMEOUT_MS / 1000)
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
        """(text, attachments) of the user's message M answers: the first
        non-resume user turn naming M. (None, 0) when unreadable."""
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
            if (parse_pipe_run_id(run) or {}).get("kind") in ("input", "resume"):
                continue
            content = m.get("content")
            blocks = content if isinstance(content, list) else []
            text = content if isinstance(content, str) else "".join(
                str(b.get("text") or "") for b in blocks
                if isinstance(b, dict) and b.get("type") == "text")
            text = _strip_inbound_meta(text)
            # The Gateway stores an Open WebUI image turn's images in `__openclaw.media`.
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

    def steered_away(self) -> dict:
        """Dedup baselines of M's run, for the steered message's reply."""
        return {"received": self.renderer.received, "stream": self.renderer.received,
                "item": self.renderer.last_item}

    async def finalize(self, note: str | None = None, stopped: bool = False,
                       steered: bool = False) -> None:
        """Write M `done: true`, from the transcript when it can, else what
        streamed. `steered`: M ends with what streamed, since the run goes on."""
        if self.closed:
            return
        self.closed = self.finalizing = True
        try:
            try:
                hist = None if steered else await _history_once_connected(
                    self.conn, self.session_key, f"turn writer: {self.message_id[:8]}...",
                    limit=RUN_HISTORY_LIMIT)
            except Exception as ex:
                hist = None
                pipe_log(f"  turn writer: chat.history failed at the end: {ex!r}")
            streamed = list(self.renderer.output)
            if steered:
                self.renderer.flush()
            elif self.wake is not None:
                self._rebuild_wake(hist)
                self.renderer.flush()
            else:
                if hist:
                    self._adopt_recoveries_from(hist.get("messages") or [])
                if hist:
                    self._note_transcript_answers(hist.get("messages") or [])
                rebuilt = self._from_transcript(hist) if hist else None
                if rebuilt is not None:
                    self.renderer = rebuilt
                self.renderer.flush()
                if (hist and steers_mid_run_messages(self.conn.gateway_version)
                        and _history_steered_host(hist, self.run_id) is not None):
                    # Also when its lone final came after the settle.
                    self.steered_message = True
                if self.steered_message and not self._showable():
                    # The later message's reply has the answer.
                    note = note or REPLY_SENT_WITH_NEXT_NOTE
                elif not self._showable():
                    await self._show_silent_announce(hist)
            _keep_pipe_notes(streamed, self.renderer)
            # A run whose reply is only an image has no text to show.
            blocks, missing = ([], []) if steered else await self._stored_media(hist)
            if not self.opened:
                # Adopted, with nothing new: it ends as it stands only when the
                # transcript was read; unread, a later load may still rebuild it.
                if not (self._showable() or note or blocks or missing or self.event_media
                        or (self.adopted and hist)) or not await self._open_at_end():
                    return
            output, text = await self._view(final=True)
            if self.adopted and hist and not self._showable():
                # Nothing came for it here: it ends with what its gone writer
                # showed, not left unfinished for every load to adopt again.
                shown = self._claim["messages"].get(self.message_id) or {}
                output, text = copy.deepcopy(shown.get("output") or []), shown.get("content") or ""
            output = [i for i in output if not _is_sentinel_message_item(i)]
            text = text.strip()
            if text in _SILENT_SENTINELS:
                text = ""
            images = (await self._media_text(blocks, missing)).strip()
            if images:
                text = f"{text}\n\n{images}" if text else images
                output.append(_turn_note_item(images))
            if note:
                text = f"{text}\n\n{note}" if text else note
                output.append(_turn_note_item(note))
            run_usage = next(filter(None, (_history_usage_for_run(hist, r) for r in reversed(self.runs))),
                             None) if hist else None
            history, status, saved = await self._persist_final(output, text, stopped, run_usage)
            # ELI-340: drop the mid-turn copy, then push M finished: a tab
            # loading across the save shows M cut, and keeps the push over its
            # fetch. A failed save keeps the copy, the only whole one left.
            pushed = False
            if saved:
                await _clear_response_stream(self.task_id)
                async with self._push_lock:
                    pushed = await self._push_final(text, output, history)
                # A finished reply this writer took over was published when it
                # first finished: the run continues into it, with no second publish.
                if (not stopped and not self._claim.get("was_done")
                        and getattr(self.conn._valves(), "PUBLISH_CHAT_FINISHED", False)):
                    await _publish_chat_finished(self.chat_id, self.message_id, self.user_id, text)
            else:
                pipe_log(f"  turn writer: chat {self.chat_id[:8]}... message {self.message_id[:8]}...: "
                         f"FINAL SAVE FAILED; keeping the mid-turn copy, M is not sent finished")
            emit = await self._emitter()
            # Without the push, the final status lines go as status events,
            # before the completion that shows M done (ELI-371). With it, a tab
            # already has them: a status event would add each one again.
            for line in status if not pushed else ():
                await emit({"type": "status", "data": line})
            await emit({"type": "chat:completion", "data": {"done": True, "content": text,
                                                             "output": output}})
            if not self.newer_run:
                await emit({"type": "chat:active", "data": {"active": False}})
            tools = sum(1 for i in output if i.get("type") == "function_call")
            pipe_log(f"  turn writer: finalized {self.message_id[:8]}... ({len(text)} chars, "
                     f"{tools} tool card(s), {len(self.runs)} run(s))")
        finally:
            self._done.set()

    async def _stored_media(self, hist: dict | None) -> tuple[list, list]:
        """(image blocks, unstored file names) of M's runs. The Gateway stores
        images after the run's final, so re-read until they arrive (ELI-328)."""
        if self.steered_into:
            return [], []
        for _s, _e, fname in _media_scan(self.renderer.text)[0]:
            if _is_gateway_media_name(fname):
                self.gateway_media.add(fname)
        names = {os.path.basename(p) for p in self.gateway_media}
        expected = self.images_made + len(self.gateway_media)

        def stored(h):
            blocks, errors = [], []
            for run in self.runs:
                blocks += _history_image_blocks_for_run(h, run)
                errors += [e for e in _history_media_errors_for_run(h, run)
                           if os.path.basename(e) in names]
            return blocks, errors
        last = [stored(hist)]

        async def read(fresh):
            if fresh:
                last[0] = stored(await self.conn.session_history(
                    self.session_key, limit=RUN_HISTORY_LIMIT))
            return last[0]
        try:
            _got, complete = await self.conn.read_until_stored(
                self.session_key, read, lambda got: len(got[0]) + len(got[1]) >= expected,
                _RUN_IMAGES_MAX_WAIT_S)
            if not complete:
                pipe_log(f"  turn writer: WARNING: the runs' images not all stored after "
                         f"{_RUN_IMAGES_MAX_WAIT_S:.0f} s; written without the rest")
        except Exception as ex:
            pipe_log(f"  turn writer: chat.history for the run's images failed: {ex!r}")
        blocks, errors = last[0]
        if expected:
            pipe_log(f"  turn writer: runs made {self.images_made} image(s) and named "
                     f"{len(self.gateway_media)} file(s); {len(blocks)} stored")
        return blocks, _unstored_gateway_media(sorted(self.gateway_media),
                                               len(blocks) - self.images_made, errors)

    async def _media_text(self, blocks: list, missing: list) -> str:
        """M's images: stored blocks, then the events' `mediaUrls`."""
        try:
            emit = await self._emitter()
            text = await _gateway_images_text(blocks, valves=self.conn._valves(),
                                              __event_emitter__=emit, missing=missing) \
                if blocks or missing else ""
            if self.event_media:
                shown, _handled = await _resolve_media_text(
                    "\n".join(f"MEDIA:{u}" for u in self.event_media) + "\n",
                    valves=self.conn._valves(), __event_emitter__=emit)
                text = f"{text}\n\n{shown}"
        except Exception as ex:
            pipe_log(f"  turn writer: the run's images could not be shown: {ex!r}")
            return ""
        return text

    async def _open_at_end(self) -> bool:
        """Retry a locked open after the run ended (no event left to retry on).
        A DB still locked is reported, not dropped silently (ELI-167)."""
        last = None
        for _ in range(OPEN_AT_END_LOCKED_TRIES):
            try:
                return await self.open(takeover=self.takeover)
            except Exception as ex:
                if not _db_locked(ex):
                    raise
                last = ex
        pipe_log(f"  turn writer: ERROR: run {self.run_id[:24]}... NOT WRITTEN to chat "
                 f"{self.chat_id[:8]}... (message {self.message_id[:8]}...): Open WebUI's database "
                 f"stayed locked for {OPEN_AT_END_LOCKED_TRIES} tries after the run ended ({last!r})")
        try:
            await (await self._emitter())({"type": "notification", "data": {
                "type": "error",
                "content": "OpenClaw: a reply could not be saved to this chat because Open WebUI's "
                           "database stayed locked. See the pipe log."}})
        except Exception as ex:
            pipe_log(f"  turn writer: notification failed: {ex!r}")
        return False

    async def _show_silent_announce(self, hist: dict | None) -> None:
        """Show a sub-agent result the parent met with silence as one card."""
        child = next(filter(None, (parse_announce_run_id(r) for r in self.runs)), None)
        if child is None:
            return
        result = await _child_result(self.conn, child)
        if not result:
            return
        # The spawn call naming the child, in either tool shape, direct or
        # through a deferred-tool wrapper (2026.10).
        label, args = None, {}
        for ev in _history_events((hist or {}).get("messages") or []):
            data = ev.get("data") or {}
            if ev.get("stream") != "tool":
                continue
            if data.get("phase") == "start":
                call = (_bare_tool_name(data.get("name") or ""), json.dumps(data.get("args") or {}))
                name, args_str = _deferred_call_view(*call) or call
                if _is_spawn_tool(name):
                    args[data.get("toolCallId")] = _json_object(args_str)
            elif (data.get("toolCallId") in args
                  and child["child_session"] in json.dumps(data.get("result"))):
                label = _spawn_title(args[data["toolCallId"]])
        tcid = f"subagent-{child['child_run']}"
        args_str = json.dumps({"label": label or child["child_session"]})
        call = _tool_call_started_event("Sub-agent", tcid, args_str)["item"]
        call["status"] = "completed"
        self.renderer = _TurnRenderer()
        self.renderer.output = [call, _tool_call_result_event(tcid, result)["item"]]
        if not self.opened:
            # A message of its own (ELI-217): 2026.10.5 retries a silent
            # delivery under the same run id; a reply must not land over the card.
            self.conn._turn_writers.pop((self.chat_id, self.message_id), None)
            self.message_id = turn_message_id(self.session_key, tcid, self.chat_id)
            self.conn._turn_writers[(self.chat_id, self.message_id)] = self

    def release(self) -> None:
        """Retired by a newer deploy: end the task (a registered one keeps the
        chat "generating"). M stays unfinished for the successor to adopt."""
        if self.closed:
            return
        self.closed = True
        self._done.set()

    def _rebuild_wake(self, hist: dict | None) -> None:
        """Repair lost events from the wake's transcript window. A wake has no
        closing message, so the rebuild is used only if it holds at least as much."""
        if not hist or self.wake.get("at") is None or self.wake.get("end") is None:
            return
        rebuilt = _history_renderer(wake_span(hist.get("messages"), self.wake["at"],
                                              self.wake["end"]))
        # The transcript may lack a wake's tool calls (not imported while a
        # user turn is queued behind it).
        if (self._holds_streamed_calls(rebuilt, "wake's")
                and len(rebuilt.output) >= len(self.renderer.output)
                and len(rebuilt.text) >= len(self.renderer.text) and rebuilt.output):
            self.renderer = rebuilt

    def _from_transcript(self, hist: dict) -> "_TurnRenderer | None":
        """M rebuilt from the transcript for the trailing runs it covers (holds
        every streamed call); earlier streamed content is kept. A killed
        claude-cli run is never imported. None while a run is open or none is
        covered."""
        spans = [_history_span_messages(hist, r, self.run_started.get(r)) for r in self.runs]
        if self.steered_into and self.runs == [self.run_id] and spans[0] is not None:
            # What streamed may be a later message's answer: the span decides.
            return _history_renderer(spans[0])
        if any(s is None for s in spans):
            pipe_log(f"  turn writer: the transcript does not show {self.message_id[:8]}...'s "
                     f"{len(self.runs)} run(s) closed; keeping what streamed")
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
        """Whether a rebuild holds every tool call that streamed into M."""
        streamed = {o.get("id") for o in self.renderer.output if o.get("type") == "function_call"}
        missing = streamed - {o.get("id") for o in rebuilt.output if o.get("type") == "function_call"}
        if missing:
            pipe_log(f"  turn writer: the transcript lacks {len(missing)} of the {whose} "
                     f"{len(streamed)} streamed call(s); keeping what streamed")
        return not missing

    async def _stopped(self) -> None:
        """The task was cancelled; ask the page which button (case 8). Only a
        Stop aborts and finalizes; otherwise keep writing (case 7)."""
        intent = await _page_cancel_intent(self.user_id, self.chat_id, self.message_id)
        pipe_log(f"  turn writer: task of {self.message_id[:8]}... cancelled, page intent "
                 f"{intent or 'unknown'}")
        if intent != "stop" or self.closed:
            return
        wake = bool(self.wake and self.wake.get("job"))
        # An agent-started run first, by its id: on 2026.10.5 chat.abort says
        # `aborted` but stops only the running command.
        aborted = parse_pipe_run_id(self.run_id) is None and await self._abort_agent_run()
        if not aborted:
            try:
                resp = await _run_request(
                    self.conn, RUN_ABORT, {"sessionKey": self.session_key, "runId": self.run_id}, timeout=15)
                aborted = bool((resp or {}).get("aborted"))
            except Exception as ex:
                pipe_log(f"  turn writer: chat.abort failed: {ex}")
        if not aborted and wake:
            await self._cancel_wake_run()
        await _mark_stopped(self.chat_id, self.message_id, self.request_task)
        await self.finalize(stopped=True)

    async def _abort_agent_run(self) -> bool:
        """`sessions.abort` by run id: stops that run only, keeping the queue
        (2026.10.x). On 2026.9 it finds no run."""
        try:
            res = await self.conn.send_request("sessions.abort", {"key": self.session_key, "runId": self.run_id},
                                               timeout=15)
        except Exception as ex:
            pipe_log(f"  turn writer: sessions.abort for the wake's run failed: {ex}")
            return False
        aborted = (res or {}).get("status") == "aborted"
        if aborted:
            pipe_log(f"  turn writer: run {self.run_id[:24]}... aborted by its id")
        return aborted

    async def _cancel_wake_run(self) -> None:
        """Stop a cron wake's run on 2026.9 via its job's task (ELI-192):
        chat.abort finds no run under the sessionId."""
        job = self.wake["job"]
        try:
            resp = await self.conn.send_request("tasks.list", {"sessionKey": self.session_key, "limit": 100},
                                                timeout=10)
        except Exception as ex:
            pipe_log(f"  turn writer: tasks.list for the wake's run failed: {ex}")
            return
        tasks = [t for t in (resp or {}).get("tasks") or []
                 if t.get("runtime") == "cron" and t.get("sourceId") == job
                 and t.get("status") in ("queued", "running")
                 and self.session_key in (t.get("childSessionKey"), t.get("sessionKey"))]
        if not tasks:
            pipe_log(f"  turn writer: no running task of wake job {job[:8]}...: its run is not stopped")
        for task in tasks:
            try:
                res = await self.conn.send_request("tasks.cancel", {"taskId": task["taskId"]}, timeout=15)
                pipe_log(f"  turn writer: wake job {job[:8]}... run cancelled: "
                         f"{bool((res or {}).get('cancelled'))} {(res or {}).get('reason') or ''}")
            except Exception as ex:
                pipe_log(f"  turn writer: tasks.cancel for wake job {job[:8]}... failed: {ex}")

    def saving(self) -> bool:
        """It may still save its message: open, or closed and finalizing."""
        return not self._done.is_set() and (not self.closed or self.finalizing)

    async def wait_done(self) -> None:
        """Until this writer is done with its message."""
        await self._done.wait()

    async def _hold(self) -> None:
        """The Open WebUI task, alive while this writer owns the run."""
        try:
            await self._done.wait()
        except asyncio.CancelledError:
            if not self.closed:
                spawn_background(self._stopped())
            raise


# ---------------------------------------------------------------------------
# Routing: which writer gets a run's events (DESIGN 2.1, 2.2)
# ---------------------------------------------------------------------------
# The receive loop also reads RPC replies, so it must never await an RPC.
# Each routed run gets a queue and a pump task that feeds events in order.


def _media_directives(text: str, final: bool) -> list[str]:
    """Each complete `MEDIA:` directive in `text`, outside code (ELI-111)."""
    out = []
    for start, end, _fname in _media_scan(text, final=final)[0]:
        if text[start:end] not in out:
            out.append(text[start:end])
    return out


def _replace_directive(text: str, raw: str, shown: str) -> str:
    """`text` with `shown` for each `raw` outside code (ELI-111, ELI-117)."""
    hits = [d for d in _media_scan(text)[0] if text[d[0]:d[1]] == raw]
    return _replace_media(text, hits, lambda _fname: shown) if hits else text


async def _transcript_view(conn: "_GatewayConnection", *, session_key: str, chat_id: str,
                           user_id: str, message_id: str, runs: list, messages: list,
                           rendered: "_TurnRenderer") -> tuple[list, str]:
    """(output, text) of M from the transcript, as the writer finalizes it."""
    view = _TurnWriter(conn, session_key=session_key, chat_id=chat_id, user_id=user_id,
                       run_id=runs[-1], message_id=message_id, renderer=rendered)
    view.runs = list(runs)
    view._note_transcript_answers(messages)
    output, text = await view._view(final=True)
    output = [i for i in output if not _is_sentinel_message_item(i)]
    text = text.strip()
    return output, "" if text in _SILENT_SENTINELS else text


def _history_span_messages(history: dict, run_id: str, started_ms: float | None = None) -> list | None:
    """One run's messages, or None while it may still write. A run cut off
    with no closing turn ends where the next admitted turn begins."""
    closed = _history_run_messages(history, run_id, started_ms)
    if closed is not None:
        # After a steered-in message comes that message's answer.
        cut = next((n for n, m in enumerate(closed) if _history_user_run_id(m) not in (None, run_id)), None)
        return closed if cut is None else closed[:cut]
    steered = _history_steered_span(history, run_id)
    if steered is not None:
        return steered
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
        self.restarts_before = 0        # conn.gateway_restarts when the pump began
        self.terminal = asyncio.Event()  # the run's terminal event was queued
        # Sent more than a lone message-less final (`_steer`).
        self.started = False
        self.heartbeat = False          # a wake-id run the Gateway flagged isHeartbeat
        # A cleanly ended run a later message was steered into (`_turn_next_run`).
        self.ended_clean: str | None = None
        # Its writer's reply when steered (`_steer`): the replies the run was
        # split into before it, and its own span when not the pump's run.
        self.steered_before: list | None = None
        self.span_run: str | None = None
        self.task: "asyncio.Task | None" = None  # its `_turn_pump_loop`, which a retire ends


def _turn_route_now(conn: "_GatewayConnection", payload: dict) -> bool:
    """Queue `payload` for its run's writer when no live request consumes it
    (DESIGN 2.6). True when handled. Never awaits (`drain_held` order)."""
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
    if owui_shutting_down():
        # The next startup's reconcile finishes them (ELI-90, ELI-94); a writer
        # opened now would save done:false and die (ELI-173).
        return True
    if run.startswith(ACTION_RUN_PREFIX):
        return True  # the Status Action's own runs render nothing
    if (sess, run) in conn._held_for_live:
        conn.drain_held(sess, run)  # the earlier ones first
    if conn.live_reply_owns(sess, run):
        conn.hold_for_live(sess, run, payload)
        return True
    _note_heartbeat_run(conn, payload)
    if (sess, run) in conn._turn_skip_runs:
        return True
    if conn.was_delivered_live(sess, run):
        # A late event reopens nothing.
        return True
    if not _turn_kind_known(conn, sess, run, payload):
        return True
    return _turn_queue(conn, sess, run, parsed, payload)


def _turn_queue(conn: "_GatewayConnection", sess: str, run: str, parsed: tuple,
                payload: dict) -> bool:
    pump = conn._turn_pumps.get((sess, run))
    if pump is None:
        pump = conn._turn_pumps[(sess, run)] = _RunPump()
        pump.restarts_before = conn.gateway_restarts
        target = conn._run_targets.get(f"{sess}:{run}")
        if target is not None:
            # DESIGN 2.6: continue from the shadow renderer, which holds this event.
            pump.handover = conn.handover_of(sess, run)
            pump.started = _handover_started(pump.handover)
            conn.unregister_run_target(sess, run)
        pump.task = spawn_background(_turn_pump_loop(conn, sess, run, parsed, pump))
        spawn_background(conn.ensure_message_subscription(sess))
        child = parse_announce_run_id(run)
        if child is not None:
            # Read before the Gateway deletes a `cleanup: "delete"` child.
            spawn_background(_child_result(conn, child))
        if target is not None:
            return True
    if payload.get("state") in ("final", "error", "aborted"):
        pump.stop_notice_before = conn._stop_notice_seen
    if _is_terminal_event(payload):
        pump.terminal.set()
    if not (payload.get("state") == "final" and not payload.get("message")):
        pump.started = True
    pump.queue.put_nowait(("event", payload))
    if _is_error_event(payload) and parse_pipe_run_id(run) is None:
        # ELI-217: on 2026.10.5 an erroring agent-started run gets no settle
        # push, so its error events re-read the row.
        spawn_background(_turn_reread_row(conn, sess))
    return True


def _handover_started(handover: dict) -> bool:
    """The handed-over run showed something, so it is not waiting on a steer."""
    renderer = handover.get("renderer")
    return bool(getattr(renderer, "received", "") or getattr(renderer, "output", None))


def _is_error_event(payload: dict) -> bool:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return payload.get("state") == "error" or (
        payload.get("stream") == "lifecycle" and data.get("phase") == "error")


def _note_child_result(conn: "_GatewayConnection", payload: dict) -> None:
    """Keep a spawned sub-agent's result from its chat final
    (server-chat-CbnqM5f6.mjs emitChatTerminal): a `cleanup: "delete"` child
    is gone by its announce's end (subagent-announce-BIG31XbZ.mjs:413)."""
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
    """The announced sub-agent run's result: kept, else from its transcript."""
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
    """Condition B: a heartbeat run gets no message. Only its agent events
    carry isHeartbeat (server-chat-CbnqM5f6.mjs:791, 823-826), so remember it."""
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


# Bounds on runs held unclassified; past these is a leak, not a wait.
_UNKINDED_RUNS_MAX = 64
_UNKINDED_EVENTS_MAX = 500


def _classifies(payload: dict) -> bool:
    """An event that tells a heartbeat run apart: a non-tool agent event
    (session.tool copies lack the flag) or the run's end."""
    stream = payload.get("stream")
    return (bool(stream) and stream != "tool") or _is_terminal_event(payload)


def _turn_kind_known(conn: "_GatewayConnection", sess: str, run: str, payload: dict) -> bool:
    """Hold an agent run's events in order until it is known not to be a
    heartbeat. Pipe runs and cron wakes need no wait."""
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
    """Hand an interrupted run's message to the turn writer (DESIGN 2.6); its
    settled row decides who continues (2.7). `read_row`: read it now, in
    case the resync already passed. `reply`: a request's own state at an
    event gap; `no_resume` for a run Send now kept (ELI-158)."""
    key = f"{session_key}:{run_id}"
    target = conn._run_targets.get(key)
    parsed = conn.parse_owui_session_key(session_key)
    if ((target is None and reply is None) or not parsed or not conn.writes_turns
            or conn._retired or (session_key, run_id) in conn._turn_pumps):
        return False
    pump = conn._turn_pumps[(session_key, run_id)] = _RunPump()
    pump.restarts_before = conn.gateway_restarts
    if reply is None:
        pump.handover = conn.handover_of(session_key, run_id, thinking)
        pump.started = _handover_started(pump.handover)
    else:
        pump.handover = {"message_id": reply["message_id"], "renderer": reply["renderer"],
                         "thinking": thinking or (target or {}).get("thinking"), "shown": "",
                         "no_resume": bool(reply.get("no_resume")),
                         "request_task_id": (target or {}).get("request_task_id"),
                         "steer_pending": bool(reply.get("steer_pending")
                                               or (target or {}).get("steer_pending"))}
        pump.started = _handover_started(pump.handover)
        for payload in reply["events"]:
            pump.queue.put_nowait(("event", payload))
            pump.started = pump.started or not (payload.get("state") == "final"
                                                and not payload.get("message"))
    if target and target.get("span_run") not in (None, run_id):
        # A steered reply keeps its own span and the replies before it (`_steer`).
        pump.span_run, pump.steered_before = target["span_run"], list(target.get("before") or [])
    pump.stop_notice_before = conn._stop_notice_seen
    conn.unregister_run_target(session_key, run_id)
    pump.task = spawn_background(_turn_pump_loop(conn, session_key, run_id, parsed, pump))
    spawn_background(conn.ensure_message_subscription(session_key))
    if read_row:
        spawn_background(_turn_resync(conn, skip=set(conn.active_run_snapshot)))
    pipe_log(f"  handed run {run_id[:24]}... over to the turn writer "
             f"(message {pump.handover['message_id'][:8]}...)")
    return True


def _turn_settled(conn: "_GatewayConnection", session_key: str, row: dict) -> None:
    """The session went idle: its row decides each writer (DESIGN 2.5, 2.7).
    A run still unclassified is no heartbeat and is released first."""
    for (sess, run) in [k for k in conn._turn_unkinded if k[0] == session_key]:
        _release_unkinded(conn, sess, run)
    pumps = [run for (sess, run) in conn._turn_pumps if sess == session_key]
    if pumps:
        pipe_log(f"  session settled (status {row.get('status')!r}, last run "
                 f"{str(row.get('lastRunId'))[-24:]}): {len(pumps)} turn writer run(s)")
    for (sess, _run), pump in list(conn._turn_pumps.items()):
        if sess == session_key:
            pump.queue.put_nowait(("settled", row))


def _turn_next_run(conn: "_GatewayConnection", payload: dict) -> None:
    """A new run's event means earlier runs are over: end cleanly ended
    steered runs from the transcript. 2026.10.10 pushes no idle after a
    follow-up run (ELI-215). A terminal event starts no run."""
    if _is_terminal_event(payload):
        return
    sess, run_id = payload.get("sessionKey"), payload.get("runId")
    for (s, run), pump in list(conn._turn_pumps.items()):
        if s == sess and pump.ended_clean and pump.ended_clean != run_id:
            pump.ended_clean = None
            if pump.writer is not None:
                pump.writer.newer_run = True
            pipe_log(f"  next run {str(run_id)[:24]}... started: run {run[:24]}... ends from the transcript")
            pump.queue.put_nowait(("settled", None))


async def _await_recovery_turn(conn: "_GatewayConnection", sess: str, run: str,
                               pump: _RunPump) -> None:
    """Rule E needs a recovery run's `:user` turn, which 2026.10 commits about
    0.7 s after the first event: wait for the next append or the run's end."""
    if parse_pipe_run_id(run) is not None:
        return
    while not pump.terminal.is_set():
        if not any(w.session_key == sess and not w.closed and (w.awaiting_row or w.takeover)
                   for w in list(conn._turn_writers.values())):
            return
        appended = conn.session_message_event(sess)
        try:
            messages = (await conn.session_history(sess, limit=50, max_chars=200)).get("messages")
        except Exception as ex:
            pipe_log(f"  turn writer: chat.history for run {run[:24]}... failed: {ex!r}")
            return
        if any(r == run for r, _tool in _history_user_turns(messages)):
            return
        waits = [asyncio.ensure_future(appended.wait()), asyncio.ensure_future(pump.terminal.wait())]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()


async def _turn_writer_for(conn: "_GatewayConnection", sess: str, run: str,
                           user_id: str, chat_id: str) -> "_TurnWriter | None":
    # Two runs can name one message, so resolve and register in one step.
    async with conn._turn_resolve_lock:
        return await _turn_writer_for_locked(conn, sess, run, user_id, chat_id)


async def _turn_writer_for_locked(conn, sess, run, user_id, chat_id):
    messages = None
    if parse_pipe_run_id(run) is None:
        # Rule E: only the transcript names a recovery's message.
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
    # A pipe run takes over Open WebUI's stub (no request left after a restart).
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
            request = _owui_request_task(handed.get("request_task_id"))
            pump.writer = _TurnWriter(conn, session_key=sess, chat_id=chat_id, user_id=user_id,
                                      run_id=run, message_id=handed["message_id"], takeover=True,
                                      renderer=handed["renderer"], thinking=handed.get("thinking"),
                                      shown=handed.get("shown", ""), request=request)
            if request is not None and not request.done():
                # Stream again after its save, to write the handover.
                request.add_done_callback(lambda _t: pump.queue.put_nowait(("event", None)))
            conn._turn_writers[(chat_id, handed["message_id"])] = pump.writer
            if handed.get("no_resume"):
                # Kept by Send now (ELI-158): a resume would race the new
                # message (ELI-159).
                pump.writer.resumes = MAX_TURN_RESUMES
            await pump.writer.feed(None)
            if handed.get("steer_pending"):
                await pump.writer.hold_open()
            else:
                spawn_background(pump.writer.show_running())
        elif re.fullmatch(_UUID_RE, run) and run == await conn.session_id_for(sess):
            pump.wake = True
        else:
            await _await_recovery_turn(conn, sess, run, pump)
            pump.writer = await _turn_writer_for(conn, sess, run, user_id, chat_id)
        while True:
            kind, item = await pump.queue.get()
            if pump.wake:
                if await _wake_step(conn, sess, run, user_id, chat_id, pump, kind, item):
                    return
                continue
            writer = pump.writer
            if kind == _PUMP_STEERED_IN:
                if writer is not None:
                    writer.steered_into = item.get("run")
                    writer.renderer.seed(item.get("run_text"))
                    await writer.working()
                    pipe_log(f"  turn writer: {writer.message_id[:8]}... shows run "
                             f"{str(item.get('run'))[:24]}... from its message's commit")
                continue
            if kind == "steered":
                # A later message was steered in: its reply continues from M.
                try:
                    item.set_result({**writer.steered_away(), "earlier": {
                        "chat_id": writer.chat_id, "message_id": writer.message_id,
                        "request_task_id": None, "span_run": run}} if writer is not None else None)
                    if writer is not None:
                        await writer.finalize(steered=True)
                        conn._turn_writers.pop((chat_id, writer.message_id), None)
                finally:
                    if not item.done():
                        item.set_result(None)
                return
            if kind == "event":
                if writer is not None:
                    await writer.feed(item)
                if not (writer is not None and writer.run_id == run and _ends_on_own_final(run, item)):
                    continue
                # 2026.10.5 pushes no settle for a Gateway-started run (ELI-215),
                # so its clean final ends it.
                item = None
            # Only the pump of the writer's current run ends it.
            if writer is not None and writer.run_id == run:
                unjudged = (conn.gateway_restarts > pump.restarts_before and not pump.terminal.is_set()
                            and judges_cut_runs_after_listening(conn.gateway_version))
                await writer.end(item, stop_notice_before=pump.stop_notice_before,
                                 after_restart=unjudged)
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
            # Wakes share this run id: what queued after the end is the next
            # one's. A retired connection starts none (`_end_turn_pumps`).
            while pump.wake and not conn._retired and not pump.queue.empty():
                kind, item = pump.queue.get_nowait()
                if kind == "event" and item is not None:
                    _turn_queue(conn, sess, run, parsed, item)


def _ends_on_own_final(run: str, payload) -> bool:
    """An agent-started run's chat final: over whatever the row says."""
    return (isinstance(payload, dict) and payload.get("state") == "final"
            and parse_pipe_run_id(run) is None)


def _is_terminal_event(payload: dict) -> bool:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return payload.get("state") in ("final", "error", "aborted") or (
        payload.get("stream") == "lifecycle" and data.get("phase") in ("end", "error"))


def _event_ms(payload: dict) -> float:
    """The event's `ts`, else receive time (the Gateway shares this clock)."""
    ts = payload.get("ts")
    return float(ts) if isinstance(ts, (int, float)) else time.time() * 1000


# chat.history returns at most this many messages (CHAT_HISTORY_MAX_ENTRIES,
# chat-history-constants-C-H8nkgi.mjs:3).
CHAT_HISTORY_MAX = 1000


async def _wake_bind(conn: "_GatewayConnection", sess: str, run: str, user_id: str,
                     chat_id: str, pump: _RunPump, at_ms: float) -> tuple[str, float | None]:
    """Bind the wake to its turn's message: ("bound", job start), ("wait",
    None) until the turn is committed, or ("stray", None) when no wake runs."""
    try:
        jobs = (await conn.send_request("cron.list", {"includeDisabled": True}, timeout=15)).get("jobs")
    except Exception as ex:
        pipe_log(f"  turn writer: cron.list for a wake failed: {ex!r}")
        return "wait", None
    starts = [(j.get("state") or {}).get("runningAtMs") for j in jobs or []
              if j.get("sessionTarget") == f"session:{sess}"]
    running_start = min((s for s in starts if isinstance(s, (int, float))), default=None)
    # A heard job run counts even once a one-shot job left cron.list.
    heard = conn.cron_run_at(sess, at_ms)
    if heard is not None:
        running_start = heard["start"]
    bound = conn._wake_turns_bound.setdefault(sess, set())
    # Read back until the window reaches before the job start.
    limit, turn = 50, None
    while True:
        try:
            messages = (await conn.session_history(sess, limit=limit, max_chars=200)).get("messages") or []
        except Exception as ex:
            pipe_log(f"  turn writer: chat.history for a wake failed: {ex!r}")
            return "wait", None
        turn = (wake_turn_of_run(messages, heard["job"], heard["start"], bound) if heard is not None
                else wake_turn_at(messages, at_ms, bound))
        oldest = min((m.get("timestamp") for m in messages
                      if isinstance(m.get("timestamp"), (int, float))), default=None)
        if (turn is not None or running_start is None or len(messages) < limit
                or limit >= CHAT_HISTORY_MAX or (oldest is not None and oldest < running_start)):
            break
        limit = min(limit * 4, CHAT_HISTORY_MAX)
    if heard is not None:
        start = heard["start"] if turn is not None else None
    else:
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
    """Start of the cron job run that wrote `turn`, if the event at `at_ms` is
    that run's; else None. Other runs with runId = sessionId (a compaction)
    are no wake."""
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
    """The finished `cron.runs` entry of `job` spanning `turn_at`."""
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
    """One queue item of a cron wake; True when it is over. Only its own
    terminal ends a wake: the row stays idle while it runs."""
    if kind == "settled":
        return False
    if kind == "event" and (pump.heartbeat or item.get("isHeartbeat") is True):
        # Condition B per pump, not per run id: wakes share it (ELI-103 f).
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
            # Holding them would hand them to the next wake (same run id).
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
    elif not writer.closed:  # a heartbeat that already showed something is finished too
        writer.wake["end"] = end_ms
        # Off this pump: the next wake (same run id) must not wait on a card.
        spawn_background(_wake_close(conn, writer, ask=not pump.heartbeat))
    else:
        conn._turn_writers.pop((writer.chat_id, writer.message_id), None)
    # The next wake reuses this run id: nothing remembered for it may carry over.
    conn._turn_skip_runs.pop((sess, run), None)
    conn._delivered_live.pop(f"{sess}:{run}", None)


async def _wake_close(conn: "_GatewayConnection", writer: "_TurnWriter", *, ask: bool) -> None:
    """End a wake's message: ask a closing question (ELI-180), else finalize.
    Stays registered until closed so the answer run finds it."""
    try:
        if not (ask and await writer._ask_question()):
            await writer.finalize()
    except Exception as ex:
        pipe_log(f"  turn writer: ending wake message {writer.message_id[:8]}... failed: {ex!r}")
    finally:
        if writer.closed:
            conn._turn_writers.pop((writer.chat_id, writer.message_id), None)


async def _wake_resync(conn: "_GatewayConnection") -> None:
    """After a reconnect, end wakes whose terminal was missed. The row is idle
    during a wake, so the cron job's `runningAtMs` decides."""
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
            # It ended at the job run's finish, not now: a queued run may have
            # written since. None keeps what streamed.
            run = await _wake_job_run(conn, wake["job"], wake.get("at") or 0)
            pump.queue.put_nowait(("wake-ended", (run or {}).get("ts")))


async def _adopt_turn_message(conn: "_GatewayConnection", *, chat_id: str, user_id: str,
                              session_key: str, message_id: str, meta: dict) -> str:
    """Take over a message a retired or dead writer left unfinished, by the
    run its meta names. Returns "adopted" or "skip"."""
    run = meta.get("openclaw_run")
    if not run or (session_key, run) in conn._turn_pumps or conn._retired:
        return "skip"
    pump = conn._turn_pumps[(session_key, run)] = _RunPump()
    pump.restarts_before = conn.gateway_restarts
    wake = meta.get("openclaw_wake") if isinstance(meta.get("openclaw_wake"), dict) else None
    if wake and wake.get("key"):
        pump.wake = True
        conn._wake_turns_bound.setdefault(session_key, set()).add(wake["key"])
    pump.writer = _TurnWriter(conn, session_key=session_key, chat_id=chat_id, user_id=user_id,
                              run_id=run, message_id=message_id, takeover=True,
                              wake=dict(wake) if pump.wake else None)
    pump.writer.adopted = True
    conn._turn_writers[(chat_id, message_id)] = pump.writer
    pump.task = spawn_background(_turn_pump_loop(conn, session_key, run, (user_id, chat_id), pump))
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
    """After a reconnect: send refused resumes and end runs whose settle was missed."""
    for writer in list(conn._turn_writers.values()):
        if writer.pending_resume:
            await writer.on_hello()
    await _turn_resync(conn, skip=set(conn.active_run_snapshot))
    await _wake_resync(conn)


async def _turn_resync(conn: "_GatewayConnection", skip: set = frozenset()) -> None:
    """Re-read rows of sessions with a pump or inline reply and end the idle
    ones. Inline replies get the row even when busy: one may have lost its
    final in the gap while a later run runs. While the socket is coming back
    it reads nothing: that reconnect's hello runs this again (ELI-369)."""
    if conn.reconnecting:
        return
    idle = {sess for (sess, _run) in [*conn._turn_pumps, *conn._turn_unkinded] if sess not in skip}
    idle |= {c.session_key for c in list(conn._consumers.values()) if c.session_key not in skip}
    for sess in idle:
        read_at = time.monotonic()
        try:
            lst = await conn.send_request("sessions.list", {"search": sess[-36:], "limit": 10},
                                          timeout=15)
        except Exception as ex:
            pipe_log(f"  turn writer: row read for a resync failed: {ex!r}")
            continue
        row = next((r for r in (lst.get("sessions") or []) if r.get("key") == sess), None)
        if row is None:
            continue
        if not row.get("hasActiveRun"):
            _turn_settled(conn, sess, row)
        conn.row_to_consumers(sess, row, resync=True, read_at=read_at, outside_loop=True)


def _history_run_messages(history: dict, run_id: str, started_ms: float | None = None) -> list | None:
    """One closed run's messages, back to the previous run's closing message;
    None until it closes. `started_ms` cuts a silent previous run's leftovers
    (ELI-227). The walk goes past steered-in messages (ELI-271) and drops a
    wake prompt that sorts inside the run."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list) or not run_id:
        return None
    for end in range(len(messages) - 1, -1, -1):
        message = messages[end]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if run_id not in _idempotency_keys(message) and _run_closed_by(message) != run_id:
            continue
        start, queued = end, set()
        while start > 0 and _run_closed_by(messages[start - 1]) in (None, run_id) \
                and not _stamped_before(messages[start - 1], started_ms):
            if (messages[start - 1] or {}).get("role") == "user":
                if _history_queued_prompt(messages, start - 1, run_id):
                    queued.add(start - 1)
                # Past messages steered into it (2026.10.10).
                elif not (_history_steered_into(history, start - 1) == run_id
                          or (_history_imported_copy(messages, start - 1)
                              and _history_steered_into(history, start - 2) == run_id)):
                    break
            start -= 1
        return [m for i, m in enumerate(messages[start:end + 1], start) if i not in queued]
    return None


def _history_ran(history: dict, run_id: str) -> bool:
    """`run_id` wrote a message of its own: it ran, not steered into another."""
    return any(isinstance(m, dict) and m.get("role") == "assistant"
               and (run_id in _idempotency_keys(m) or _run_closed_by(m) == run_id)
               for m in (history or {}).get("messages") or [])


def _history_imported_copy(messages: list, index: int) -> bool:
    """The unkeyed copy claude-cli imports right after a `chat.send` message."""
    m = messages[index] if 0 <= index < len(messages) else None
    return (isinstance(m, dict) and m.get("role") == "user" and not _history_user_run_id(m)
            and index > 0 and bool(_history_user_run_id(messages[index - 1])))


def _history_steered_into(history: dict, index: int) -> str | None:
    """The run the user message at `index` was steered into (2026.10.10+):
    its own run never wrote, and the last run before it did."""
    messages = (history or {}).get("messages") or []
    own = _history_user_run_id(messages[index]) if 0 <= index < len(messages) else None
    if not own or _history_ran(history, own) or _history_wake_prompt(messages[index]):
        return None
    for k in range(index - 1, -1, -1):
        m = messages[k]
        rid = _history_user_run_id(m)
        if rid:
            if _history_ran(history, rid):
                closers = {_run_closed_by(x) for x in messages[k + 1:index]} - {None}
                return rid if closers <= {rid} else None
            continue
        if isinstance(m, dict) and m.get("role") == "user" and not _history_imported_copy(messages, k):
            return None
    return None


def _history_wake_prompt(message: dict) -> bool:
    """A cron wake's prompt, never steered: on 2026.10.10 it runs after the
    busy run but sorts inside it by fire time. Pipe messages never count."""
    own = _history_user_run_id(message)
    return bool(own) and not parse_pipe_run_id(own) and bool(_cron_turn_job(message))


def _history_next_turn(message: dict) -> bool:
    """A later send that ends a steered message's answer (not a wake prompt)."""
    return bool(_history_user_run_id(message)) and not _history_wake_prompt(message)


def _history_steered_host(history: dict, run_id: str) -> str | None:
    """The run `run_id`'s message was steered into, or None."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list) or not run_id or _history_ran(history, run_id):
        return None
    start = next((n for n, m in enumerate(messages) if _history_user_run_id(m) == run_id), None)
    return _history_steered_into(history, start) if start is not None else None


def _history_steered_answer_pending(history: dict, run_id: str, uncommitted: bool = False,
                                    steered: bool = False) -> bool:
    """`run_id`'s steered message has no answer in the transcript yet and no
    later turn began. `uncommitted`/`steered` also count an uncommitted or
    not yet hosted message as pending."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list):
        return False
    start = next((n for n, m in enumerate(messages) if _history_user_run_id(m) == run_id), None)
    if start is None:
        return uncommitted
    if any(_history_next_turn(m) for m in messages[start + 1:]):
        return False
    if _history_steered_host(history, run_id) is None:
        return steered and not _history_ran(history, run_id) and not any(
            isinstance(m, dict) and m.get("role") == "assistant" for m in messages[start + 1:])
    return _history_steered_span(history, run_id) is None


def _history_reaches_run(history: dict, run_id: str) -> bool:
    """Whether a transcript read goes back to `run_id`'s message: it holds
    it, or its oldest message (ms) is no older than the run id's send time."""
    sent = _run_id_sent_at(run_id)
    messages = (history or {}).get("messages") or []
    if sent is None or any(_history_user_run_id(m) == run_id for m in messages):
        return True
    stamps = [m["timestamp"] for m in messages
              if isinstance(m, dict) and isinstance(m.get("timestamp"), (int, float))]
    return not stamps or min(stamps) / 1000 <= sent


def _history_later_user_turn(history: dict, run_id: str) -> bool:
    """Whether a read that does not reach `run_id`'s message holds another
    user turn: committed after its send, so it is not still waiting to be
    (a session's messages commit in order). A compaction alone leaves none."""
    return any(_history_user_run_id(m) not in (None, run_id)
               for m in (history or {}).get("messages") or [])


def _history_steered_span(history: dict, run_id: str) -> list | None:
    """The answer to a steered message: the host run's messages after it, up
    to the next turn or the host's closing message. None if not steered or
    the host has not closed."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list) or not run_id or _history_ran(history, run_id):
        return None
    start = next((n for n, m in enumerate(messages) if _history_user_run_id(m) == run_id), None)
    host = _history_steered_into(history, start) if start is not None else None
    if host is None:
        return None
    closers = [n for n in range(start + 1, len(messages)) if _run_closed_by(messages[n]) == host]
    if not closers:
        return None
    end = next((n for n in range(start + 1, len(messages)) if _history_next_turn(messages[n])),
               len(messages))
    last = max((n for n in closers if n < end), default=end - 1)
    span = [m for m in messages[start + 1:min(end, last + 1)] if not _history_wake_prompt(m)]
    own = [m for m in span if not any(k.startswith("cli-assistant:") for k in _idempotency_keys(m))]
    # The closing copy replaces Claude Code's own record when texts match
    # (cli-session-history mergeCliHistoryRow).
    answered = any(m.get("role") == "assistant" and _history_renderer([m]).text.strip() for m in own
                   if isinstance(m, dict))
    return own if answered else span


def _history_steered_tail(history: dict, run_id: str) -> list | None:
    """What follows `run_id`'s message up to the next turn, without the host's
    closing copy (the whole run): its answer when the run it went into never
    closed it. None without the message."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list):
        return None
    start = next((n for n, m in enumerate(messages) if _history_user_run_id(m) == run_id), None)
    if start is None:
        return None
    end = next((n for n in range(start + 1, len(messages)) if _history_next_turn(messages[n])), len(messages))
    return [m for m in messages[start + 1:end] if not _history_wake_prompt(m)
            and not any(k.startswith("cli-assistant:") for k in _idempotency_keys(m))]


def _history_steer_target(history: dict, run_id: str) -> str | None:
    """The run `run_id`'s message was steered into, as its commit stored it
    (`__openclaw.steerTargetRunId`, set once the runtime took it in)."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list):
        return None
    message = next((m for m in messages if _history_user_run_id(m) == run_id), None)
    return ((message or {}).get("__openclaw") or {}).get("steerTargetRunId")


def _history_later_turn(history: dict, run_id: str) -> bool:
    """A turn began after `run_id`'s message."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list):
        return False
    start = next((n for n, m in enumerate(messages) if _history_user_run_id(m) == run_id), None)
    return start is not None and any(_history_next_turn(m) for m in messages[start + 1:])


async def _history_once_connected(conn, session_key: str, who: str, **params) -> dict:
    """chat.history; a read the socket's reconnect failed waits for the
    connection to be back (`ensure_connected`, an event) and reads again.
    Raises what the read raised otherwise, or why the connection is not
    coming back (retired)."""
    while True:
        try:
            return await conn.session_history(session_key, **params)
        except Exception as ex:
            if not conn.reconnecting:
                raise
            pipe_log(f"  {who}: chat.history waits for the Gateway connection: {ex!r}")
        await conn.ensure_connected()


async def _await_steered_answer(conn, session_key: str, run_id: str, who: str, *,
                                steered: bool = False, host: str | None = None) -> dict | None:
    """Wait for the transcript to hold the answer of the run `run_id`'s
    message was steered into. The run's terminal and its idle push can come
    before the Gateway commits the message. Ends on the answer, a failed run,
    or a restart without the run, with the last transcript read (None if none).
    `host`: the run it went into, when the steer was seen here. With it, or
    with the steer target its commit stored, it also ends once the session
    settled: a claude-cli run stops counting as active only after it stored
    its answer (2026.10.10: `finishCliAttempt` awaits Claude Code's flush and
    the closing copy before the run resolves, and `deferredLifecycle.complete()`
    clears the active run after that), so the settled transcript holds the
    answer if there is one. A message sent before the oldest one a read
    holds is not in it to wait for: it ends once the session settled. A read
    during a reconnect waits for the connection and reads again."""
    restarts = conn.gateway_restarts
    last = None
    while True:
        appended = conn.session_message_event(session_key)
        changed = conn.session_change_event(session_key)
        pump = conn._turn_pumps.get((session_key, run_id))
        # A terminal means no commit, except the lone steered final: the
        # commit can come after it and the idle row.
        ended = pump is not None and pump.terminal.is_set() and not steered
        try:
            hist = await _history_once_connected(conn, session_key, who, limit=RUN_HISTORY_LIMIT)
        except Exception as ex:
            pipe_log(f"  {who}: chat.history failed waiting for a steered answer: {ex!r}")
            return last
        last = hist
        pending = _history_steered_answer_pending(hist, run_id, uncommitted=not ended, steered=steered)
        if host and not pending:
            # On claude-cli the transcript names the host only once it closed.
            pending = _history_steered_span(hist, run_id) is None and not _history_later_turn(hist, run_id)
        if not pending:
            return hist
        # A commit lands at the end, so a read that does not go back to the
        # message's send time can show it only if it was never committed.
        unreached = not _history_reaches_run(hist, run_id)
        if (unreached and _history_later_user_turn(hist, run_id)
                and await _session_list_active(conn, session_key) is False):
            pipe_log(f"  {who}: its message was sent before the oldest one the transcript read "
                     "holds, a later message is in, and the session settled; it ends with what it shows")
            return hist
        target = host or _history_steer_target(hist, run_id)
        into = target or _history_steered_host(hist, run_id)
        if into and conn.runs_failed.get(f"{session_key}:{into}"):
            pipe_log(f"  {who}: the run its message went into "
                     f"ended {conn.runs_failed[f'{session_key}:{into}']} before answering it")
            return hist
        if conn.gateway_restarts > restarts and await _session_list_active(conn, session_key) is False:
            pipe_log(f"  {who}: the Gateway restarted without the "
                     "run its message went into; it ends without the answer")
            return hist
        if target and await _session_list_active(conn, session_key) is False:
            # The run it went into ended and the session settled: the
            # transcript holds all it will, closed or not.
            try:
                hist = await conn.session_history(session_key, limit=RUN_HISTORY_LIMIT)
            except Exception as ex:
                pipe_log(f"  {who}: chat.history failed after the session settled: {ex!r}")
            pipe_log(f"  {who}: the session settled with its steered message's answer not closed "
                     "in the transcript")
            return hist
        pipe_log(f"  {who} waits for its steered message's answer in the transcript")
        waits = [asyncio.ensure_future(appended.wait())]
        if target or unreached:
            waits.append(asyncio.ensure_future(changed.wait()))
        if pump is not None and not pump.terminal.is_set():
            waits.append(asyncio.ensure_future(pump.terminal.wait()))
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()


def _history_queued_prompt(messages: list, index: int, run_id: str) -> bool:
    """Whether the message at `index` is a wake prompt sorted inside another
    run: 2026.10.10 stores it at fire time, and claude-cli imports the run's
    messages with their own times around it."""
    own = _history_user_run_id(messages[index])
    if own == run_id or not _history_wake_prompt(messages[index]):
        return False
    for message in reversed(messages[:index]):
        if _run_closed_by(message) not in (None, run_id):
            return False
        if _history_user_run_id(message):
            return True
    return False


def _stamped_before(message: dict, started_ms: float | None) -> bool:
    ts = (message or {}).get("timestamp")
    return started_ms is not None and isinstance(ts, (int, float)) and ts < started_ms


def _relay_output_events(sent: list[dict], current: list[dict]) -> list[dict]:
    """Responses events taking a tab's output from `sent` to `current`, as
    OWUI's `applyResponseStreamEvent` folds them. `added` needs an explicit
    `output_index`, or it is spliced in before the last item."""
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
    """True once text is non-empty and can no longer be a silent sentinel,
    so a sentinel-only run never flashes an empty bubble."""
    t = text.strip()
    if not t or t in _SILENT_SENTINELS:
        return False
    if any(s.startswith(t) for s in _SILENT_SENTINELS):
        return False
    return True


async def _relay_emit_event(user_id: str, chat_id: str, message_id: str | None,
                            data: dict) -> bool:
    """Emit one `events` envelope to the user room. False on failure."""
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
# Only the streaming request sets a reply stub `done: True`. After an Open
# WebUI restart mid-turn the stub stays "running" forever, so the first load
# finishes it from the Gateway transcript.

ORPHAN_STUB_NOTE = (
    "*This reply was cut off before it finished (Open WebUI restarted or "
    "lost the run), and no reply for it could be recovered from OpenClaw. "
    "Send the message again if you still need an answer.*"
)
ORPHAN_STUB_SILENT_NOTE = "*The run for this message stopped without leaving a reply in the transcript.*"


def _run_id_sent_at(run_id: str) -> float | None:
    """The send time at the end of a pipe run id."""
    try:
        return float(run_id.rsplit("-", 1)[1]) if run_id.startswith("msg-") else None
    except (IndexError, ValueError):
        return None


def _stub_turn(history: dict, stub_id: str, user_text: str,
               stub_ts: float | None = None, steers: bool = False) -> dict | None:
    """The transcript turn a reply stub waited for: {"run_id", "messages",
    "closed", "by"} or None. Found by message id in the run id, else by user
    text: the first match sent after `stub_ts`, since short texts recur."""
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
    if steers and _history_steered_host(history, run_id) is not None:
        span = _history_steered_span(history, run_id)
        return {"run_id": run_id, "messages": span or [], "closed": span is not None, "by": by}
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
    """This process's start time (epoch seconds) from /proc, or None."""
    try:
        with open("/proc/self/stat") as fh:
            # Field 22; the command name may hold spaces, so count from ")".
            ticks = int(fh.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as fh:
            btime = next(int(line.split()[1]) for line in fh if line.startswith("btime "))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def _stub_predates_process(stub: dict) -> bool:
    """True when `stub` predates this process, so no request can fill it.
    Stamps are whole seconds, so the start's own second does not count."""
    started, ts = _owui_process_started_at(), _owui_message_ts(stub)
    return started is not None and ts is not None and ts < int(started)


async def _owui_chat_has_live_task(chat_id: str) -> bool:
    """True while Open WebUI runs a task for `chat_id`, or when unsure."""
    try:
        from open_webui.main import app
        from open_webui.tasks import list_task_ids_by_item_id
        return bool(await list_task_ids_by_item_id(getattr(app.state, "redis", None), chat_id))
    except Exception as ex:
        pipe_log(f"  orphan stub: cannot read Open WebUI tasks ({ex}); leaving chat {chat_id[:8]}... alone")
        return True


async def _session_list_active(conn: "_GatewayConnection", session_key: str,
                               *, missing: bool | None = False) -> bool | None:
    """`sessions.list` `hasActiveRun` for one session (unlike
    `sessions.describe`, counts a sub-agent yield gap). Searched by key: an
    unfiltered list holds only the newest 100 rows. `missing` when no such
    session; None when unknown."""
    try:
        lst = await conn.send_request(
            "sessions.list", {"search": session_key, "limit": _SESSION_READ_LIMIT}, timeout=15)
    except Exception as ex:
        pipe_log(f"  sessions.list for {session_key[-36:][:8]}... failed: {ex}")
        return None
    rows = lst.get("sessions") or []
    row = next((r for r in rows if r.get("key") == session_key), None)
    if row:
        return bool(row.get("hasActiveRun"))
    total = lst.get("totalCount")
    if len(rows) >= _SESSION_READ_LIMIT or (total is not None and total > len(rows)):
        pipe_log(f"  sessions.list for {session_key[-36:][:8]}...: {total or len(rows)} rows match, "
                 "none is the session; its state is unknown")
        return None
    return missing


async def _wait_for_session_idle(conn: "_GatewayConnection", session_key: str, is_active,
                                 on_busy=None) -> bool | None:
    """Wait until the Gateway says `session_key` has no run, with no ceiling
    (ELI-88): a ceiling drops the message. Only the flag decides, not
    `endedAt`. The event is taken before the read so no push slips between.
    Returns whether it was ever busy, or None when unknown."""
    ever_busy = False
    while True:
        changed = conn.session_change_event(session_key)
        active = await is_active()
        if active is None:
            return None
        if not active:
            return ever_busy
        if not ever_busy:
            ever_busy = True
            if on_busy is not None:
                await on_busy()
        await changed.wait()


# A long transcript takes the Gateway over the default 10 s RPC bound.
ORPHAN_HISTORY_TIMEOUT_S = 60


def _stub_session_key(conn: "_GatewayConnection", agent_id: str, user_id: str, chat_id: str,
                      stub_id: str) -> str:
    """The session of a stub's run, including another model's column (ELI-329)."""
    column = 0
    try:
        state_dir = _state_dir(getattr(conn._valves(), "STATE_DIR", ""))
        column = _history_column_of(state_dir, chat_id, [stub_id]) or 0
    except Exception as ex:
        pipe_log(f"  orphan stub {stub_id[:8]}...: no column ({ex!r}); using the chat's session")
    return _owui_session_key(agent_id, user_id, chat_id, column)


async def _reconcile_stub(conn: "_GatewayConnection", agent_id: str,
                          chat_id: str, stub_id: str, *, wait: bool = True) -> str:
    """Finish an orphaned reply stub from the transcript, waiting on the
    pushed settle for a live run. Returns "done", "wait" or "skip"."""
    from open_webui.models.chats import Chats
    waiting = False
    while True:
        chat = await Chats.get_chat_by_id(chat_id)
        msgs = ((chat.chat or {}).get("history") or {}).get("messages") or {} if chat else {}
        stub = msgs.get(stub_id) or {}
        if stub.get("role") != "assistant" or stub.get("done") is not False:
            return "skip"  # gone or finished meanwhile
        if _ended_by_stop(stub):
            # Stopped by the user: closed as it is, never rebuilt.
            async with conn._chat_write_lock(chat_id):
                if await _owui_upsert_message(chat_id, stub_id, {"done": True}) is None:
                    return "skip"
            pipe_log(f"  orphan stub {stub_id[:8]}... in chat {chat_id[:8]}...: stopped by the user, "
                     "closed as it was")
            return "done"
        writer = (stub.get("meta") or {}).get("openclaw_writer")
        if writer:
            # A turn writer's message: adopt it once that writer is gone.
            if _turn_writer_alive(writer):
                return "skip"
            return await _adopt_turn_message(
                conn, chat_id=chat_id, user_id=chat.user_id, message_id=stub_id,
                session_key=_stub_session_key(conn, agent_id, chat.user_id, chat_id, stub_id),
                meta=stub.get("meta") or {})
        # No task id: its request may still be on its way, since that
        # request's model listing starts this sweep (ELI-156). Unless the stub
        # predates this process.
        task_id = (stub.get("meta") or {}).get("task_id")
        if not task_id and not _stub_predates_process(stub):
            return "skip"
        if await _owui_chat_has_live_task(chat_id):
            return "skip"  # a live request owns it
        session_key = _stub_session_key(conn, agent_id, chat.user_id, chat_id, stub_id)
        changed = conn.session_change_event(session_key)
        try:
            hist = await conn.session_history(session_key, limit=RUN_HISTORY_LIMIT,
                                              timeout=ORPHAN_HISTORY_TIMEOUT_S)
        except Exception as ex:
            pipe_log(f"  orphan stub {stub_id[:8]}...: chat.history failed: {ex!r}")
            return "skip"
        parent = msgs.get(stub.get("parentId")) or {}
        user_text = _coerce_text(parent.get("content")) if parent.get("role") == "user" else ""
        turn = _stub_turn(hist, stub_id, user_text, _owui_message_ts(stub),
                          steers=steers_mid_run_messages(conn.gateway_version))
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
    # A stub with content is rebuilt only from a run matched by id.
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
            fields.update(content=text, output=output,
                          **await _embed_fields(conn, output, stub.get("embeds")))
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
        # Open WebUI returns None silently when the write fails.
        if await _owui_upsert_message(
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
    if getattr(conn._valves(), "PUBLISH_CHAT_FINISHED", False):
        await _publish_chat_finished(chat_id, stub_id, chat.user_id)
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
    """(chat_id, message_id) of this pipe's unfinished assistant messages.
    `chat_message` ids are `<chat_id>-<message_id>`."""
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
    # Claims of replies deleted since (garbage collection, not correctness).
    await _drop_chat_finished_claims_of_deleted_replies()
    if stubs:
        pipe_log(f"  orphan stub reconcile: {len(stubs)} unfinished OpenClaw "
                 "repl(y/ies) in Open WebUI")
    # One at a time: SQLite rejects concurrent chat writes.
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
    """[(run_id, closing ms)] of closed runs: claude-cli by its
    `cli-assistant:<runId>` key, an embedded run by the run id itself."""
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
    """A settled run's span, else everything after its user turn."""
    span = _history_span_messages(history, run_id)
    if span is not None:
        return span
    messages = (history or {}).get("messages")
    for i, m in enumerate(messages if isinstance(messages, list) else []):
        if _history_user_run_id(m) == run_id:
            return messages[i + 1:]
    return None


async def _reconcile_ended_while_down(conn: "_GatewayConnection", agent_id: str) -> None:
    """ELI-94 (agent-turns.md case 4): a graceful stop saves a reply `done:
    true`, and its run closes while Open WebUI is down. Chats whose session
    `updatedAt` is newer than their `updated_at` get those runs rebuilt."""
    from open_webui.models.chats import Chats
    lst = await _list_all_sessions(conn)
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
    """Rebuild finished replies whose run closed after `written` and that hold
    less than the transcript. `lastRunId` counts as closed at `settled_ms`
    (the embedded runtime writes no closer). Returns how many."""
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
        maps = await _embed_fields(conn, output, None)
        async with conn._chat_write_lock(chat_id):
            chat = await Chats.get_chat_by_id(chat_id)
            msgs = ((chat.chat or {}).get("history") or {}).get("messages") or {} if chat else {}
            current = msgs.get(mid) or {}
            if current.get("role") != "assistant" or current.get("done") is not True:
                continue  # missing, or still generating: the ELI-90 pass owns done:false
            if _ended_by_stop(current):
                continue  # what the user stopped is what it shows
            held_cards = sum(1 for i in current.get("output") or [] if i.get("type") == "function_call")
            cards = sum(1 for i in output if i.get("type") == "function_call")
            held_text = _coerce_text(current.get("content")).strip()
            if cards < held_cards or (cards == held_cards and len(text) <= len(held_text)):
                continue  # holds as much as the transcript: never rewritten
            if maps:
                maps = {"embeds": _merge_embeds(current.get("embeds"), maps["embeds"])}
            if await _owui_upsert_message(
                    chat_id, mid, {"content": text, "output": output, "done": True, **maps}) is None:
                pipe_log(f"  ended-while-down {mid[:8]}... in chat {chat_id[:8]}...: "
                         "Open WebUI did not save the write (ELI-94)")
                continue
            conn.mark_delivered_live(session_key, run)
        await _relay_emit_event(chat.user_id, chat_id, mid,
                                {"type": "chat:completion", "data": {"output": output}})
        if maps:
            await _relay_emit_event(chat.user_id, chat_id, mid,
                                    {"type": "embeds", "data": {**maps, "replace": True}})
        if getattr(conn._valves(), "PUBLISH_CHAT_FINISHED", False):
            await _publish_chat_finished(chat_id, mid, chat.user_id)
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
    """Once per Function load (a deploy is harmless: live replies are skipped)."""
    global _orphan_reconcile_task
    if _orphan_reconcile_task is not None:
        return
    try:
        import open_webui.models.chat_messages  # noqa: F401  (inside Open WebUI only)
    except Exception:
        return
    _orphan_reconcile_task = spawn_background(_reconcile_orphan_stubs(valves_getter))


def _turn_session_message(conn: "_GatewayConnection", sess: str) -> None:
    """Re-read the row on an append while a message waits for a recovery: a
    give-up tombstones it unpushed (9.3 main-session-restart-recovery-
    CPJjd8da.mjs:1135-1153, 10.10 -DnTvOdiE.mjs:1040-1062)."""
    if any(w.session_key == sess and not w.closed and w.awaiting_row
           for w in list(conn._turn_writers.values())):
        spawn_background(_turn_reread_row(conn, sess))


async def _turn_reread_row(conn: "_GatewayConnection", sess: str) -> None:
    try:
        lst = await conn.send_request("sessions.list", {"search": sess[-36:], "limit": 10}, timeout=15)
    except Exception as ex:
        pipe_log(f"  turn writer: row read after an append failed: {ex!r}")
        return
    row = next((r for r in (lst.get("sessions") or []) if r.get("key") == sess), None)
    if row is not None and not row.get("hasActiveRun"):
        _turn_settled(conn, sess, row)


class _TurnHooks:
    """What `_GatewayConnection` calls into the turn writer with (`_TURNS`)."""
    route_now = staticmethod(_turn_route_now)
    after_hello = staticmethod(_turn_after_hello)
    resync = staticmethod(_turn_resync)
    settled = staticmethod(_turn_settled)
    next_run = staticmethod(_turn_next_run)
    note_heartbeat = staticmethod(_note_heartbeat_run)
    hand_over = staticmethod(_hand_over_run)
    session_message = staticmethod(_turn_session_message)
    end_unsent_replies = staticmethod(_end_unsent_replies)
    settle_steered_replies = staticmethod(_settle_steered_replies)


_TURNS = _TurnHooks
