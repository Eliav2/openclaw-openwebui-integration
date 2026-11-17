# BUILD FRAGMENT: assembled into openclaw_pipe.py by build.py (see src/frontmatter.txt).
# Messages kept in the chat while the Gateway is unreachable (ELI-504).
#
# A turn whose connect fails (`pending_retryable`) does not error. Its message
# id goes into the chat's `meta.openclaw_pending` (an ordered list) and the
# empty reply is marked `meta.delivery: "pending"`, with what the pipe needs to
# send it again. The chat stays the single source of truth: no queue of our
# own. One worker per process delivers the list's head once the Gateway is
# back, by running the stored turn through the pipe again (`_pending_run`), so
# the answer streams into the waiting bubble. The state is in Open WebUI's DB,
# so an Open WebUI restart or a redeploy loses nothing.
#
#   pending  -> sending  (`_pending_claim_head`: one claimer wins)
#   sending  -> delivered (`_pending_delivered`: chat.send was accepted)
#   sending  -> pending  (`_pending_release`: the Gateway went away again)
#
# Never twice: the claim is a compare-and-set under the chat's row lock, and the
# resend uses the first send's idempotency key (`pipe_run_id(..., at=sent_at)`),
# which the Gateway answers with the run it already has.

PENDING_META_KEY = "openclaw_pending"  # chat.meta: [{"id": message id, "at": send time}]
PENDING_DELIVERY = "delivery"  # message meta
PENDING_STATE_PENDING = "pending"
PENDING_STATE_SENDING = "sending"
PENDING_STATE_DELIVERED = "delivered"
PENDING_STATE_STOPPED = "stopped"
PENDING_STATE_FAILED = "failed"
PENDING_LIVE_STATES = (PENDING_STATE_PENDING, PENDING_STATE_SENDING)
PENDING_PAYLOAD = "pending_payload"  # message meta: the turn to run again
PENDING_SENT_AT = "pending_sent_at"  # message meta: when the user sent it
PENDING_CLAIM = "pending_claim"  # message meta: {"proc", "at"} of the claimer
PENDING_ARMED = "pending_armed"  # message meta: the request's own save is behind us
# Pipe metadata of a replay: the original send time.
PENDING_REPLAY_META = "openclaw_pending_sent_at"

# A claim of this process that nobody finished is taken over after this long
# (a claim of another process is dead, and is taken at once).
PENDING_LEASE_S = 300
PENDING_BACKOFF_FIRST_S = 2.0
PENDING_BACKOFF_MAX_S = 60.0
PENDING_BACKOFF_FACTOR = 2
# A message kept for later holds its whole body (images included).
PENDING_PAYLOAD_MAX_BYTES = 16 * 1024 * 1024
PENDING_LANG_DEFAULT = "en"
PENDING_USER_LANGUAGE_VAR = "{{USER_LANGUAGE}}"
PENDING_STATUS = {
    "en": "Waiting for the gateway…",
    "he": "ממתין לגייטווי…",
}
PENDING_SENT_NOTE = {
    "en": "_(Sent at {when}, while the gateway was down. Delivered now.)_",
    "he": "_(נשלחה ב-{when}, כשהגייטווי היה למטה. נמסרה עכשיו.)_",
}
PENDING_AGENT_NOTE = ("[system] The user wrote the message below at {when}. The Gateway was down, "
                      "so it reaches you late: answer it as written then.\n\n")
# Metadata a client may send with a message it held back itself (ELI-403's browser outbox): when
# the user wrote it. Seconds or milliseconds.
PENDING_SENT_AT_META = "sent_at"
PENDING_MILLISECONDS_FROM = 1e11
PENDING_TIME_FORMAT = "%H:%M"
PENDING_DAY_TIME_FORMAT = "%d/%m %H:%M"
PENDING_SECONDS_PER_DAY = 86400
PENDING_ERROR_TEXT = "**Error delivering your message:** {error}"
# Metadata keys a replay must not carry: they belong to the request that ended.
PENDING_REQUEST_ONLY_META = ("task_id",)
# Not a Gateway failure: an earlier message of the chat still waits, so this one queues behind it.
PENDING_BEHIND_CODE = "earlier-message-waits"


def pending_retryable(ex) -> bool:
    """Whether waiting can fix this connect or send failure."""
    if not isinstance(ex, GatewayError):
        return False
    error = ex.error or {}
    return (error.get("code") in GATEWAY_UNREACHABLE_CODES or error.get("code") == PENDING_BEHIND_CODE
            or error.get("retryable") is True)


def pending_lang(metadata) -> str:
    """`he` or `en`: the UI language of the turn (Open WebUI's `{{USER_LANGUAGE}}`)."""
    variables = (metadata or {}).get("variables") if isinstance(metadata, dict) else None
    lang = (variables or {}).get(PENDING_USER_LANGUAGE_VAR) if isinstance(variables, dict) else None
    code = str(lang or "").split("-", 1)[0].lower()
    return code if code in PENDING_STATUS else PENDING_LANG_DEFAULT


def pending_status_text(lang: str) -> str:
    return PENDING_STATUS.get(lang) or PENDING_STATUS[PENDING_LANG_DEFAULT]


def _pending_when(sent_at: float, now: float | None = None) -> str:
    """The send time, with the day when it was not today."""
    now = time.time() if now is None else now
    same_day = time.strftime("%Y%m%d", time.localtime(sent_at)) == time.strftime("%Y%m%d", time.localtime(now))
    return time.strftime(PENDING_TIME_FORMAT if same_day else PENDING_DAY_TIME_FORMAT,
                         time.localtime(sent_at))


def pending_sent_note(sent_at: float, lang: str, now: float | None = None) -> str:
    return (PENDING_SENT_NOTE.get(lang) or PENDING_SENT_NOTE[PENDING_LANG_DEFAULT]).format(
        when=_pending_when(sent_at, now))


def pending_agent_note(sent_at: float, now: float | None = None) -> str:
    return PENDING_AGENT_NOTE.format(when=_pending_when(sent_at, now))


def pending_sent_time(metadata, now: float | None = None) -> float:
    """When the user wrote the message: the client's `sent_at` when it sends one, else the
    Open WebUI message's own timestamp, else now. Never later than now."""
    now = time.time() if now is None else now
    meta = metadata if isinstance(metadata, dict) else {}
    message = meta.get("user_message") if isinstance(meta.get("user_message"), dict) else {}
    for value in (meta.get(PENDING_SENT_AT_META), message.get("timestamp")):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return min(value / 1000 if value >= PENDING_MILLISECONDS_FROM else float(value), now)
    return now


def _pending_payload(body, metadata, user) -> dict | None:
    """What the pipe needs to run this turn again, as JSON; None when it is too big or
    cannot be stored."""
    def plain(obj):
        return json.loads(json.dumps(obj, default=lambda _o: None))
    try:
        user_valves = (user or {}).get("valves")
        valves = (user_valves if isinstance(user_valves, dict)
                  else {"thinking_default": getattr(user_valves, "thinking_default", None)})
        payload = {"body": plain(body), "metadata": plain(metadata or {}),
                   "user": {"valves": plain(valves)}}
        for key in PENDING_REQUEST_ONLY_META:
            payload["metadata"].pop(key, None)
        if len(json.dumps(payload)) > PENDING_PAYLOAD_MAX_BYTES:
            pipe_log("  pending: the message is too big to keep for later")
            return None
        return payload
    except Exception as ex:
        pipe_log(f"  pending: the message cannot be kept for later: {ex!r}")
        return None


def _pending_owns(stub: dict) -> bool:
    """A waiting reply is not an orphan: the worker delivers it."""
    return (stub.get("meta") or {}).get(PENDING_DELIVERY) in PENDING_LIVE_STATES


_PENDING_SENT: dict = {}


def _pending_sent_event(message_id: str) -> asyncio.Event:
    """Set when `message_id`'s message reached the Gateway (`_pending_delivered`)."""
    return _PENDING_SENT.setdefault(message_id, asyncio.Event())


# ── The state, under the chat's row lock ──

async def _pending_txn(chat_id: str, edit):
    return await _uncancellable(_pending_txn_now(chat_id, edit))


async def _pending_txn_now(chat_id: str, edit):
    """Run `edit(messages, chat_meta) -> (result, {message_id: fields})` in one transaction:
    it may change `chat_meta` in place, and the fields are saved into their messages as
    Open WebUI's own upsert does. None when the chat is not saved."""
    from open_webui.models.chats import Chats
    from open_webui.models.chat_messages import ChatMessages
    from sqlalchemy.orm.attributes import flag_modified

    async with Chats._chat_transaction(chat_id) as (db, chat):
        if chat is None:
            return None
        data = chat.chat or {}
        history = data.get("history") or {}
        chat_meta = dict(chat.meta or {})
        result, saves = edit(history.get("messages") or {}, chat_meta)
        for message_id, fields in saves.items():
            message = Chats.upsert_message_to_history(history, message_id, Chats._clean_null_bytes(fields))
            await ChatMessages.upsert_message(message_id, chat_id, message.get("user_id") or chat.user_id,
                                              message, db=db)
        chat.chat = {**data, "history": history}
        flag_modified(chat, "chat")
        chat.meta = chat_meta
        flag_modified(chat, "meta")
        chat.updated_at = int(time.time())
        return result


def _pending_queue_of(chat_meta: dict) -> list:
    queue = chat_meta.get(PENDING_META_KEY)
    return [e for e in queue if isinstance(e, dict) and e.get("id")] if isinstance(queue, list) else []


def _pending_meta_without(meta: dict, *keys) -> dict:
    return {k: v for k, v in meta.items() if k not in keys}


async def _pending_enqueue(chat_id: str, message_id: str, payload: dict, sent_at: float) -> bool:
    """Keep the message: listed in the chat and marked on its reply, with its payload."""
    def edit(messages, chat_meta):
        message = messages.get(message_id)
        if message is None:
            return False, {}
        queue = _pending_queue_of(chat_meta)
        if not any(e["id"] == message_id for e in queue):
            queue.append({"id": message_id, "at": sent_at})
        chat_meta[PENDING_META_KEY] = queue
        meta = _pending_meta_without(message.get("meta") or {}, PENDING_CLAIM, PENDING_ARMED)
        return True, {message_id: {"meta": {**meta, PENDING_DELIVERY: PENDING_STATE_PENDING,
                                            PENDING_SENT_AT: sent_at, PENDING_PAYLOAD: payload}}}
    try:
        return bool(await _pending_txn(chat_id, edit))
    except Exception as ex:
        pipe_log(f"  pending: keeping {message_id[:8]}... failed: {ex!r}")
        return False


async def _pending_arm(chat_id: str, message_id: str) -> bool:
    """After the request's own end-of-reply save (which writes `done: true`): the reply
    waits (`done: false`) and the worker may claim it."""
    def edit(messages, chat_meta):
        message = messages.get(message_id) or {}
        meta = message.get("meta") or {}
        if meta.get(PENDING_DELIVERY) != PENDING_STATE_PENDING:
            return False, {}
        return True, {message_id: {"done": False, "meta": {**meta, PENDING_ARMED: True}}}
    return bool(await _pending_txn(chat_id, edit))


async def _pending_claim_head(chat_id: str, proc: str, now: float) -> dict | None:
    """The chat's first waiting message, claimed for `proc`: {chat_id, message_id, payload,
    sent_at}, or None (nothing waits, it is not armed yet, or somebody else holds it).
    Entries whose reply is gone or was ended meanwhile are dropped on the way."""
    def edit(messages, chat_meta):
        queue = _pending_queue_of(chat_meta)
        saves: dict = {}
        claim = None
        while queue:
            head = queue[0]["id"]
            message = messages.get(head)
            meta = (message or {}).get("meta") or {}
            if message is None or meta.get(PENDING_DELIVERY) not in PENDING_LIVE_STATES:
                queue.pop(0)
                continue
            if meta.get(PENDING_ARMED) and message.get("done") is True:
                # Finished by the page (a Stop saved it): never sent.
                saves[head] = {"meta": {**_pending_meta_without(meta, PENDING_PAYLOAD, PENDING_CLAIM),
                                        PENDING_DELIVERY: PENDING_STATE_STOPPED}}
                queue.pop(0)
                continue
            held = meta.get(PENDING_CLAIM) or {}
            held_live = (meta.get(PENDING_DELIVERY) == PENDING_STATE_SENDING
                         and held.get("proc") == proc and now - held.get("at", 0) <= PENDING_LEASE_S)
            if meta.get(PENDING_ARMED) and not held_live:
                saves[head] = {"meta": {**meta, PENDING_DELIVERY: PENDING_STATE_SENDING,
                                        PENDING_CLAIM: {"proc": proc, "at": now}}}
                claim = {"chat_id": chat_id, "message_id": head, "payload": meta.get(PENDING_PAYLOAD),
                         "sent_at": meta.get(PENDING_SENT_AT)}
            break
        chat_meta[PENDING_META_KEY] = queue
        return claim, saves
    return await _pending_txn(chat_id, edit)


async def _pending_release(chat_id: str, message_id: str) -> None:
    """The send did not happen (the Gateway is away again): back to waiting."""
    def edit(messages, chat_meta):
        meta = (messages.get(message_id) or {}).get("meta") or {}
        if meta.get(PENDING_DELIVERY) != PENDING_STATE_SENDING:
            return None, {}
        return None, {message_id: {"meta": {**_pending_meta_without(meta, PENDING_CLAIM),
                                            PENDING_DELIVERY: PENDING_STATE_PENDING}}}
    try:
        await _pending_txn(chat_id, edit)
    except Exception as ex:
        pipe_log(f"  pending: releasing {message_id[:8]}... failed: {ex!r}")


async def _pending_delivered(chat_id: str, message_id: str, state: str = PENDING_STATE_DELIVERED) -> None:
    """The message is off the list: the Gateway has it (or, for another `state`, it will not
    be sent again)."""
    def edit(messages, chat_meta):
        chat_meta[PENDING_META_KEY] = [e for e in _pending_queue_of(chat_meta) if e["id"] != message_id]
        message = messages.get(message_id)
        if message is None:
            return None, {}
        meta = _pending_meta_without(message.get("meta") or {}, PENDING_PAYLOAD, PENDING_CLAIM, PENDING_ARMED)
        return None, {message_id: {"meta": {**meta, PENDING_DELIVERY: state}}}
    try:
        await _pending_txn(chat_id, edit)
    except Exception as ex:
        pipe_log(f"  pending: marking {message_id[:8]}... delivered failed: {ex!r}")
    if state == PENDING_STATE_DELIVERED:
        _pending_sent_event(message_id).set()


async def _pending_waiting(chat_id: str) -> bool:
    """Whether an earlier message of this chat still waits: a new one goes behind it."""
    try:
        from sqlalchemy import select
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.chats import Chat
        async with get_async_db_context() as db:
            meta = (await db.execute(select(Chat.meta).where(Chat.id == chat_id))).scalar_one_or_none()
        return bool(_pending_queue_of(meta or {}))
    except Exception as ex:
        pipe_log(f"  pending: reading the chat's list failed: {ex!r}")
        return False


# ── The turn runs again ──

async def _pending_emitter(chat_id: str, message_id: str, user_id: str | None):
    """Open WebUI's own emitter for the message: statuses are saved and shown as in a request."""
    from open_webui.socket.main import get_event_emitter
    return await get_event_emitter(
        {"chat_id": chat_id, "message_id": message_id, "user_id": user_id}, update_db=True)


async def _pending_run(pipe, claim: dict) -> bool:
    """Run the kept turn through the pipe, with no request of Open WebUI's: its text streams into
    the waiting reply, which is finished at the end. The pipe flips `delivery` itself once
    chat.send was accepted (`_pending_delivered`); a turn that did not get that far is released."""
    chat_id, message_id = claim["chat_id"], claim["message_id"]
    payload = claim["payload"] or {}
    metadata = {**(payload.get("metadata") or {}), PENDING_REPLAY_META: claim["sent_at"]}
    user_id = metadata.get("user_id")
    emit = await _pending_emitter(chat_id, message_id, user_id)
    text = ""
    try:
        async with contextlib.aclosing(pipe._pipe_impl(
            payload.get("body") or {}, emit, __event_call__=None, __user__=payload.get("user") or {},
            __metadata__=metadata, __request__=None, __task__=None,
        )) as turn_gen:
            async for chunk in turn_gen:
                if isinstance(chunk, str):
                    text += chunk
                    await emit({"type": "chat:completion", "data": {"content": text}})
    except Exception as ex:
        pipe_log(f"  pending: the kept turn of {message_id[:8]}... failed: {ex!r}")
        if not _pending_sent_event(message_id).is_set():
            text += PENDING_ERROR_TEXT.format(error=ex)
    sent = _pending_sent_event(message_id).is_set()
    _PENDING_SENT.pop(message_id, None)
    if not sent and not text.strip():
        await _pending_release(chat_id, message_id)  # the Gateway went away again
        return False
    if not sent:
        await _pending_delivered(chat_id, message_id, PENDING_STATE_FAILED)  # shown as the error it is
    await _owui_upsert_message(chat_id, message_id, {"content": text, "done": True} if text.strip() else {"done": True})
    await emit({"type": "status", "data": {"description": "", "done": True}})
    await emit({"type": "chat:completion", "data": {"done": True, "content": text}})
    pipe_log(f"  pending: {message_id[:8]}... in chat {chat_id[:8]}...: "
             f"{'delivered' if sent else 'ended with an error'} ({len(text)} chars)")
    return sent


async def _pending_arm_after_request(pipe, ref: dict) -> None:
    """The deferred request's own save comes first, then the reply waits and the worker wakes."""
    request = _owui_request_task(ref.get("request_task_id"))
    if request is not None and not request.done():
        await asyncio.wait({request})
    try:
        if await _pending_arm(ref["chat_id"], ref["message_id"]):
            await _relay_emit_event(ref["user_id"], ref["chat_id"], ref["message_id"],
                                    {"type": "chat:completion", "data": {"content": "", "done": False}})
    except Exception as ex:
        pipe_log(f"  pending: waiting for the gateway, {ref['message_id'][:8]}... not armed: {ex!r}")
        return
    _ensure_pending_delivery(pipe, force=True)


# ── The worker: once per process ──

def _pending_process_token() -> str:
    """Names this Open WebUI process; a claim of another one is dead."""
    try:
        import open_webui.socket.main as holder
    except Exception:
        holder = sys.modules[__name__]
    token = getattr(holder, "_openclaw_pending_proc", None)
    if token is None:
        token = uuid.uuid4().hex
        setattr(holder, "_openclaw_pending_proc", token)
    return token


async def _pending_chats() -> list:
    """Chats with a waiting reply (an unfinished message), once each."""
    stubs = await _orphan_stub_ids(_composer_model_prefixes(own_id=_composer_own_function_id()))
    return list(dict.fromkeys(chat_id for chat_id, _message_id in stubs))


# The delivery run of each chat that has one going. The next message of a chat goes once it
# ended: sent while the first still runs, it would be steered into that run and answered in
# the first one's reply.
_pending_runs: dict = {}


async def _pending_pass(chats: list, proc: str, deliver) -> bool:
    """Deliver each chat's waiting messages in order, until one does not go or one's run is
    still going. True when something still waits."""
    waiting = False
    for chat_id in chats:
        while True:
            running = _pending_runs.get(chat_id)
            if running is not None and not running.done():
                waiting = True
                break
            claim = await _pending_claim_head(chat_id, proc, time.time())
            if claim is None:
                # Nothing to claim: the list is empty, or its head is not armed or held.
                waiting = await _pending_waiting(chat_id) or waiting
                break
            if not await deliver(claim):
                waiting = True
                break
    return waiting


async def _pending_deliver(pipe, claim: dict) -> bool:
    """Run one claimed message; True once the Gateway has it (the run goes on by itself)."""
    sent = asyncio.ensure_future(_pending_sent_event(claim["message_id"]).wait())
    task = spawn_background(_pending_run(pipe, claim))
    _pending_runs[claim["chat_id"]] = task
    task.add_done_callback(lambda _t: (_pending_runs.pop(claim["chat_id"], None), _pending_wake.set()))
    try:
        done, _pending = await asyncio.wait({task, sent}, return_when=asyncio.FIRST_COMPLETED)
        return sent in done or (task in done and task.exception() is None and bool(task.result()))
    finally:
        sent.cancel()


async def _pending_worker(pipe) -> None:
    proc = _pending_process_token()
    delay = PENDING_BACKOFF_FIRST_S
    pipe_log("[pending] delivery worker started")
    while True:
        try:
            chats = await _pending_chats()
            if not chats:
                pipe_log("[pending] nothing waits: worker stops")
                return
            await _get_gateway_connection(lambda: pipe.valves)
            if not await _pending_pass(chats, proc, functools.partial(_pending_deliver, pipe)):
                delay = PENDING_BACKOFF_FIRST_S
                continue
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            pipe_log(f"[pending] gateway not reachable yet ({type(ex).__name__}); retrying in {delay:.0f} s")
        try:
            await asyncio.wait_for(_pending_wake.wait(), delay)
            delay = PENDING_BACKOFF_FIRST_S  # woken: a run ended or a message was kept
        except asyncio.TimeoutError:
            delay = min(delay * PENDING_BACKOFF_FACTOR, PENDING_BACKOFF_MAX_S)
        _pending_wake.clear()


_PENDING_SLOT_ATTR = "_openclaw_pending_worker"
_pending_task = None
_pending_discovered = False
_pending_wake = asyncio.Event()


def _pending_slot(function_id) -> str:
    return f"{_PENDING_SLOT_ATTR}_{function_id}" if function_id else _PENDING_SLOT_ATTR


def _ensure_pending_delivery(pipe, *, force: bool = False):
    """The worker, started once per process: a new load of the function cancels the old
    load's worker (its slot on Open WebUI's socket module). Without `force` it looks once
    per load, which finds what waited through an Open WebUI restart; a deferral forces it."""
    global _pending_task, _pending_discovered
    try:
        if _pending_task is not None and not _pending_task.done():
            _pending_wake.set()
            return _pending_task
        if _pending_discovered and not force:
            return None
        try:
            import open_webui.socket.main as holder
        except Exception:
            return None
        _pending_discovered = True
        slot = _pending_slot(_composer_own_function_id())
        stale = getattr(holder, slot, None)
        if stale is not None and stale is not _pending_task and not stale.done():
            stale.cancel()
            pipe_log("[pending] cancelled the previous load's worker")
        _pending_task = asyncio.get_running_loop().create_task(_pending_worker(pipe))
        setattr(holder, slot, _pending_task)
        return _pending_task
    except Exception as ex:
        pipe_log(f"[pending] could not start the worker: {ex!r}")
        return None
