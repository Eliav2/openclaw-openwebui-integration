# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Channels: one OpenClaw session per Open WebUI channel thread.
#
# Open WebUI calls a model in a channel only for an @mention or a reply to the
# model's message (routers/channels.py `model_response_handler`), and answers
# in a thread on that message (`channels.model_response_mode`, default
# "thread"). With that:
#
# - The thread is the conversation: its session is
#   `agent:<AGENT_ID>:openwebui:channel:<channel id>:thread:<root message id>`.
# - The mention that opens a thread sends the channel's messages since the
#   agent last saw the channel, and the mention itself, as one block of
#   `[HH:MM] Name (user id): text` lines. A mention in a thread the agent
#   follows sends the thread's messages since its last turn.
# - The agent then follows the thread: a watcher reads the thread's new
#   messages (no mention needed). A short judge on the Gateway's utility model
#   (`sessions.companion.ask`) decides whether one is meant for the agent; if
#   so, or when there is no judge, a full turn runs and may answer `[skip]`,
#   which posts nothing. Messages it does not answer reach it as context with
#   its next turn.
# - Every channel turn goes over the same Gateway connection whoever sent it
#   (`_sender_is_owner`): owner and non-owner turns alternating in one session
#   would change its tool list, and claude-cli then starts the session over.
#
# State (cursors, followed threads) is one JSON file in STATE_DIR, read and
# written under an flock, so several Open WebUI workers share it. A follow-up
# turn is claimed with its `idempotencyKey` (`chanfollow-<message id>`): of
# several watchers that send it, only the one the Gateway answers `started`
# posts the reply.
# ---------------------------------------------------------------------------

_CHANNEL_CHAT_PREFIX = "channel:"
_CHANNEL_SKIP = "[skip]"
_CHANNEL_STATE_FILE = "channel-threads.json"
_CHANNEL_POLL_S = 2.0
_CHANNEL_JUDGE_TIMEOUT_S = 10.0
_CHANNEL_IDLE_WAIT_S = 1800.0   # a follow-up waits at most this long for the thread's run to end
_CHANNEL_RUN_TIMEOUT_S = 1800.0  # and for its own run's end
_CHANNEL_JUDGE_TAIL = 12
_CHANNEL_JUDGE_CONTEXT_CHARS = 15000
_CHANNEL_WATCH_ATTR = "_openclaw_channel_watcher_v1"
_CHANNEL_MENTION_RE = re.compile(r"<@([A-Z]):([^|>]+)(?:\|[^>]+)?>")

_CHANNEL_JUDGE_QUESTION = (
    "Should Claw, the assistant in this shared thread, answer the NEW message(s) in the selected "
    "text? YES if they ask the assistant something, name it, or follow up on its last answer with "
    "a question. NO if the people talk to each other, acknowledge or react. If unsure, NO. "
    "Reply exactly YES or NO."
)
_CHANNEL_FOLLOW_NOTE = (
    "You follow this channel thread. Answer the newest message(s) above only if they are meant "
    "for you: they ask you something, name you, or follow up on your last answer. If the people "
    "are talking to each other, acknowledging or reacting, reply with exactly [skip] and nothing "
    "else: nothing is posted then. When unsure, reply [skip]."
)
_CHANNEL_MENTION_NOTE = "You were mentioned in the last message above: answer it."


def _channel_id_of(chat_id) -> "str | None":
    """The channel id when Open WebUI calls the Pipe for a channel mention
    (`chat_id = "channel:<id>"`), else None."""
    if isinstance(chat_id, str) and chat_id.startswith(_CHANNEL_CHAT_PREFIX):
        return chat_id[len(_CHANNEL_CHAT_PREFIX):] or None
    return None


def _channel_session_key(agent_id: str, channel_id: str, root_id: str) -> str:
    return f"agent:{agent_id}:openwebui:channel:{channel_id}:thread:{root_id}"


def _parse_channel_session_key(key) -> "tuple[str, str, str] | None":
    """(agent id, channel id, thread root id) of a channel thread's session."""
    m = _CHANNEL_KEY_RE.match(key or "")
    return (m.group(1), m.group(2), m.group(3)) if m else None


def _channel_mentions_a_model(content) -> bool:
    return any(kind == "M" for kind, _id in _CHANNEL_MENTION_RE.findall(content or ""))


def _channel_trigger(msg: dict, *, followed, bot_message_ids=()) -> "str | None":
    """What a new channel message asks of the agent. The one place this is
    decided, so a later "answer when addressed by name" changes only here.

    - None: the agent's own message, or an empty one.
    - "answer": a mention of a model, or a reply to the agent's message.
      Open WebUI sends these to the model itself (this Pipe's or another
      one), so the watcher leaves them alone.
    - "judge": a message in a thread the agent follows: maybe for it.
    - "context": anything else: the agent sees it with its next turn there.
    """
    if (msg.get("meta") or {}).get("model_id") or not (msg.get("content") or "").strip():
        return None
    if _channel_mentions_a_model(msg.get("content")) or (
            msg.get("reply_to_id") and msg.get("reply_to_id") in bot_message_ids):
        return "answer"
    if msg.get("parent_id") and msg.get("parent_id") in followed:
        return "judge"
    return "context"


def _channel_plain(content: str) -> str:
    """Mentions as their label (`@OpenClaw · Default`), as people read them."""
    return _CHANNEL_MENTION_RE.sub(
        lambda m: "@" + (m.group(0).split("|", 1)[1][:-1] if "|" in m.group(0) else m.group(2)),
        content or "")


def _channel_line(msg: dict, names: dict) -> str:
    when = time.strftime("%H:%M", time.localtime((msg.get("created_at") or 0) / 1e9))
    uid = msg.get("user_id") or "unknown"
    meta = msg.get("meta") or {}
    if meta.get("model_id"):
        who = f"{meta.get('model_name') or meta.get('model_id')} (you)"
    else:
        who = f"{names.get(uid) or 'Unknown'} ({uid})"
    return f"[{when}] {who}: {_channel_plain(msg.get('content') or '').strip()}"


def _channel_block(msgs: list, names: dict, limit: int, title: str) -> str:
    """The messages as one block, oldest first, at most `limit` of them; the
    first line says how many earlier ones were left out."""
    shown = msgs[-limit:] if limit > 0 else []
    dropped = len(msgs) - len(shown)
    lines = [f"{title} ({len(shown)}):"]
    if dropped:
        lines.append(f"({dropped} earlier messages not shown)")
    lines += [_channel_line(m, names) for m in shown]
    return "\n".join(lines)


def _channel_reply_text(text) -> "str | None":
    """What to post for a follow-up turn's final text: None for `[skip]` (or
    nothing), else the text without a leading `[skip]`."""
    t = (text or "").strip()
    if t.startswith(_CHANNEL_SKIP):
        t = t[len(_CHANNEL_SKIP):].strip()
    return t or None


# --- state -----------------------------------------------------------------

def _channel_state_path(state_dir) -> str:
    return os.path.join(state_dir, _CHANNEL_STATE_FILE)


@contextlib.contextmanager
def _channel_state(state_dir):
    """The channel state, read and written back under an flock:
    {"channels": {cid: {"cursor": ns}},
     "threads": {root: {"channel", "cursor", "judged", "active", "model_id", "model_name",
                        "function"}}}"""
    path = _channel_state_path(state_dir)
    with open(path + ".lock", "a") as lock:
        try:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        except ImportError:
            pass
        data = _read_json_file(path) or {}
        data.setdefault("channels", {})
        data.setdefault("threads", {})
        yield data
        _write_json_file(path, data)


def _channel_followed(state_dir, idle_hours: float, now_ns: "int | None" = None) -> dict:
    """The followed threads; drops those idle longer than `idle_hours`."""
    now_ns = now_ns or time.time_ns()
    cutoff = now_ns - int(idle_hours * 3600e9)
    with _channel_state(state_dir) as st:
        for root in [r for r, t in st["threads"].items() if (t.get("active") or 0) < cutoff]:
            st["threads"].pop(root, None)
            pipe_log(f"[channels] thread {root[:8]} idle for {idle_hours:g} h: no longer followed")
        return {r: dict(t) for r, t in st["threads"].items()}


# --- Open WebUI reads and writes ------------------------------------------------

async def _channel_rows(channel_id: str, *, parent_id=None, top_level=False,
                        after_ns: int = 0, upto_ns: "int | None" = None, parents=None) -> list:
    """Channel messages as dicts, oldest first: the thread of `parent_id`, or
    the top-level timeline, or replies to any of `parents`; newer than
    `after_ns` (and not newer than `upto_ns`). One query through Open WebUI's
    own engine (SQLite or Postgres)."""
    from sqlalchemy import select
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.messages import Message

    cols = (Message.id, Message.user_id, Message.parent_id, Message.reply_to_id,
            Message.content, Message.meta, Message.created_at)
    q = select(*cols).where(Message.channel_id == channel_id, Message.created_at > after_ns)
    if parents is not None:
        q = q.where(Message.parent_id.in_(list(parents)))
    elif top_level:
        q = q.where(Message.parent_id.is_(None))
    else:
        q = q.where(Message.parent_id == parent_id)
    if upto_ns is not None:
        q = q.where(Message.created_at <= upto_ns)
    async with get_async_db_context() as db:
        rows = (await db.execute(q.order_by(Message.created_at))).all()
    return [dict(id=r[0], user_id=r[1], parent_id=r[2], reply_to_id=r[3], content=r[4] or "",
                 meta=r[5] if isinstance(r[5], dict) else {}, created_at=r[6] or 0) for r in rows]


async def _channel_message(message_id: str) -> "dict | None":
    from sqlalchemy import select
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.messages import Message
    async with get_async_db_context() as db:
        r = (await db.execute(select(Message.id, Message.parent_id, Message.created_at, Message.meta,
                                     Message.channel_id).where(Message.id == message_id))).first()
    return dict(id=r[0], parent_id=r[1], created_at=r[2] or 0,
                meta=r[3] if isinstance(r[3], dict) else {}, channel_id=r[4]) if r else None


async def _channel_names(user_ids) -> dict:
    ids = sorted({u for u in user_ids if u})
    if not ids:
        return {}
    from sqlalchemy import select
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.users import User
    async with get_async_db_context() as db:
        rows = (await db.execute(select(User.id, User.name).where(User.id.in_(ids)))).all()
    return {r[0]: r[1] for r in rows}


async def _channel_post_reply(channel_id: str, root_id: str, user_id: str, model_id: str,
                              model_name: str, text: str) -> "str | None":
    """Post the agent's reply in the thread through Open WebUI's own message
    path (insert, the live `events:channel` updates, membership checks)."""
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.messages import MessageForm
    from open_webui.models.users import Users
    from open_webui.routers.channels import new_message_handler
    user = await _owui_call(Users.get_user_by_id, user_id)
    if user is None:
        pipe_log(f"[channels] reply in thread {root_id[:8]}: user {user_id[:8]} not found; not posted")
        return None
    form = MessageForm(content=text, parent_id=root_id, data={},
                       meta={"model_id": model_id, "model_name": model_name, "done": True})
    async with get_async_db_context() as db:
        message, _channel = await new_message_handler(None, channel_id, form, user, db)
    return getattr(message, "id", None)


# --- the mention turn -------------------------------------------------------------

async def _channel_mention_context(state_dir: str, channel_id: str, reply_message_id, model_id: str,
                                   limit: int) -> dict:
    """For a mention Open WebUI sent: the thread root (the parent of the
    reply bubble Open WebUI made), the turn's text, and `commit()` to run once
    the Gateway took the turn (advances the cursors, follows the thread)."""
    reply = await _channel_message(reply_message_id) if reply_message_id else None
    root = (reply or {}).get("parent_id") or reply_message_id
    upto = (reply or {}).get("created_at") or time.time_ns()
    model_name = ((reply or {}).get("meta") or {}).get("model_name") or model_id
    with _channel_state(state_dir) as st:
        thread = dict(st["threads"].get(root) or {})
        chan_cursor = (st["channels"].get(channel_id) or {}).get("cursor", 0)
    if reply_message_id and thread.get("reply_id") == reply_message_id and thread.get("text"):
        # Open WebUI called the Pipe again for the same reply: the same text,
        # so the Pipe follows the run the first call started.
        return {"root": root, "text": thread["text"], "commit": lambda: None, "messages": 0}
    if thread:
        msgs = await _channel_rows(channel_id, parent_id=root, after_ns=thread.get("cursor", 0), upto_ns=upto)
        title = "Thread messages since your last turn"
    else:
        timeline = await _channel_rows(channel_id, top_level=True, after_ns=chan_cursor, upto_ns=upto)
        replies = await _channel_rows(channel_id, parent_id=root, upto_ns=upto)
        msgs = sorted(timeline + replies, key=lambda m: m["created_at"])
        title = "Channel messages since you last looked"
    # Not the agent's own replies: its session already has them.
    msgs = [m for m in msgs if m["id"] != reply_message_id and (m["content"] or "").strip()
            and not (m.get("meta") or {}).get("model_id")]
    names = await _channel_names(m["user_id"] for m in msgs)
    text = _channel_block(msgs, names, limit, title) + "\n\n" + _CHANNEL_MENTION_NOTE

    def commit():
        with _channel_state(state_dir) as st:
            t = st["threads"].setdefault(root, {})
            t.update(channel=channel_id, model_id=model_id, model_name=model_name,
                     function=_composer_own_function_id(), reply_id=reply_message_id, text=text,
                     cursor=max(t.get("cursor", 0), upto), judged=max(t.get("judged", 0), upto),
                     active=time.time_ns())
            if not thread:
                c = st["channels"].setdefault(channel_id, {})
                c["cursor"] = max(c.get("cursor", 0), upto)
        pipe_log(f"[channels] following thread {root[:8]} in channel {channel_id[:8]}")

    return {"root": root, "text": text, "commit": commit, "messages": len(msgs)}


# --- the judge and the follow-up turn -----------------------------------------------

async def _channel_judge(conn, session_key: str, tail: str, *, owner: bool) -> "bool | None":
    """YES/NO from the Gateway's utility model (`sessions.companion.ask`), or
    None when there is no judge right now (no utility model, rate limited,
    an error, a timeout). Never retried: a refusal means the full turn
    decides instead, so the judge never competes with the Control UI's Side
    chat for its shared rate limit."""
    target = conn if owner else await _get_restricted_connection(conn._valves)
    try:
        r = await asyncio.wait_for(target.send_request(
            "sessions.companion.ask",
            {"sessionKey": session_key, "question": _CHANNEL_JUDGE_QUESTION,
             "selectionContext": tail[-_CHANNEL_JUDGE_CONTEXT_CHARS:]},
            timeout=_CHANNEL_JUDGE_TIMEOUT_S), _CHANNEL_JUDGE_TIMEOUT_S + 1)
    except Exception as ex:
        pipe_log(f"[channels] judge unavailable ({str(ex)[:120]}); the full turn decides")
        return None
    answer = str((r or {}).get("answer") or "").strip().upper()
    if answer.startswith("YES"):
        return True
    if answer.startswith("NO"):
        return False
    pipe_log(f"[channels] judge gave no YES/NO ({answer[:40]!r}); the full turn decides")
    return None


async def _channel_run(conn, session_key: str, message: str, idempotency_key: str,
                       *, owner: bool, still_needed=None) -> "tuple[str, str | None]":
    """Run one follow-up turn and wait for its final text. Returns
    ("started", text) for the watcher that got the turn, ("taken", None)
    when another one already sent this key, ("covered", None) when a turn
    that ran meanwhile (a mention) already carried the messages, and
    ("failed", None) otherwise."""
    # As a chat message does: wait for the session's run to end before
    # sending, under the per-session send lock. A send into a running turn
    # (a mention in the same thread) is merged into it and never ends.
    async with conn.send_section(session_key):
        waited = time.monotonic()
        while True:
            changed = conn.session_change_event(session_key)
            active = await _session_list_active(conn, session_key, missing=False)
            if active is None:
                pipe_log("[channels] follow-up: the Gateway cannot say whether the thread is busy; not sent")
                return "failed", None
            if not active:
                break
            left = _CHANNEL_IDLE_WAIT_S - (time.monotonic() - waited)
            if left <= 0:
                pipe_log("[channels] follow-up: the thread stayed busy; not sent")
                return "failed", None
            try:
                await asyncio.wait_for(changed.wait(), left)
            except asyncio.TimeoutError:
                pass
        if still_needed is not None and not still_needed():
            pipe_log("[channels] follow-up: a turn meanwhile already carried these messages")
            return "covered", None
        queue = conn.register_consumer(session_key, idempotency_key)
        run_id = idempotency_key
        try:
            resp = await _run_request(conn, RUN_SEND, dict(
                sessionKey=session_key, message=message, idempotencyKey=idempotency_key),
                timeout=30, owner=owner)
        except Exception as ex:
            conn.unregister_consumer(session_key, run_id, queue=queue)
            pipe_log(f"[channels] follow-up send failed: {ex}")
            return "failed", None
        # Only the first sender of a key gets "started"; another gets
        # "in_flight" (or the finished run's status) for the same run.
        if (resp or {}).get("status") != "started":
            conn.unregister_consumer(session_key, run_id, queue=queue)
            pipe_log(f"[channels] follow-up {idempotency_key[:24]} is {(resp or {}).get('status')}: not mine")
            return "taken", None
        if resp.get("runId") and resp["runId"] != run_id:
            conn.unregister_consumer(session_key, run_id, queue=queue)
            run_id = resp["runId"]
            queue = conn.register_consumer(session_key, run_id)
    try:
        return await asyncio.wait_for(_channel_run_text(conn, session_key, run_id, queue),
                                      _CHANNEL_RUN_TIMEOUT_S)
    except asyncio.TimeoutError:
        pipe_log(f"[channels] follow-up {run_id[:24]} sent no end in {_CHANNEL_RUN_TIMEOUT_S:.0f} s; not posted")
        return "failed", None
    finally:
        conn.unregister_consumer(session_key, run_id)


async def _channel_run_text(conn, session_key: str, run_id: str, queue) -> "tuple[str, str | None]":
    """The run's final text from its events, as `_generate_title_text` reads
    a title run."""
    text = ""
    while True:
        msg = await queue.get()
        kind = msg.get("type")
        if kind == _RESTART_MARKER_TYPE:
            return "failed", None
        if kind == _ROW_MARKER_TYPE:
            row = msg.get("row") or {}
            if row.get("hasActiveRun") is False and (msg.get("resync") or row.get("lastRunId") == run_id):
                hist = await conn.session_history(session_key, limit=10)
                found, whole = _history_text_for_run(hist, run_id)
                return ("started", whole or text) if (found or text) else ("failed", None)
            continue
        payload = msg.get("payload") or {}
        data = payload.get("data") or {}
        if payload.get("stream") == "assistant":
            text += data.get("delta") or ""
        if payload.get("stream") == "lifecycle" and data.get("phase") == "error":
            return "failed", None
        if payload.get("state") in ("error", "aborted"):
            return "failed", None
        if payload.get("state") == "final":
            final = _coerce_text(payload.get("message")) if payload.get("message") else ""
            return "started", final or text


async def _channel_follow_up(conn, valves, state_dir: str, root: str, thread: dict, pending: list,
                             *, use_judge: bool = True, post=None) -> str:
    """Handle a followed thread's new messages (after the debounce): judge,
    then maybe a full turn, then maybe a post. Returns what happened:
    "judged-no", "skip", "posted", "taken" or "failed"."""
    post = post or _channel_post_reply
    channel_id = thread["channel"]
    agent_id = getattr(valves, "AGENT_ID", "main") or "main"
    key = _channel_session_key(agent_id, channel_id, root)
    owner = await _sender_is_owner(conn, key)
    newest = pending[-1]
    since = await _channel_rows(channel_id, parent_id=root, after_ns=thread.get("cursor", 0),
                                upto_ns=newest["created_at"])
    since = [m for m in since if (m["content"] or "").strip() and not (m.get("meta") or {}).get("model_id")]
    names = await _channel_names(m["user_id"] for m in since)
    verdict = None
    if use_judge:
        recent = await _channel_rows(channel_id, parent_id=root, upto_ns=newest["created_at"])
        tail_msgs = [m for m in recent if (m["content"] or "").strip()][-_CHANNEL_JUDGE_TAIL:]
        names.update(await _channel_names(m["user_id"] for m in tail_msgs))
        new_ids = {m["id"] for m in pending}
        old = [_channel_line(m, names) for m in tail_msgs if m["id"] not in new_ids]
        new = [_channel_line(m, names) for m in pending]
        tail = "Thread so far:\n" + "\n".join(old) + "\n\nNEW message(s):\n" + "\n".join(new)
        verdict = await _channel_judge(conn, key, tail, owner=owner)

    def mark(**fields):
        with _channel_state(state_dir) as st:
            t = st["threads"].get(root)
            if t is not None:
                for k, v in fields.items():
                    t[k] = max(t.get(k, 0), v)

    if verdict is False:
        mark(judged=newest["created_at"], active=newest["created_at"])
        return "judged-no"
    limit = int(getattr(valves, "CHANNEL_CONTEXT_LIMIT", 50) or 50)
    text = _channel_block(since, names, limit, "Thread messages since your last turn") + "\n\n" + _CHANNEL_FOLLOW_NOTE
    def still_needed():
        with _channel_state(state_dir) as st:
            return (st["threads"].get(root) or {}).get("cursor", 0) < newest["created_at"]

    status, final = await _channel_run(conn, key, text, f"chanfollow-{newest['id']}", owner=owner,
                                       still_needed=still_needed)
    if status in ("taken", "covered"):
        mark(judged=newest["created_at"])
        return status
    if status == "failed":
        mark(judged=newest["created_at"])
        return "failed"
    mark(cursor=newest["created_at"], judged=newest["created_at"], active=newest["created_at"])
    reply = _channel_reply_text(final)
    if reply is None:
        pipe_log(f"[channels] thread {root[:8]}: [skip], nothing posted")
        return "skip"
    await post(channel_id, root, newest["user_id"], thread.get("model_id") or "", thread.get("model_name") or "", reply)
    pipe_log(f"[channels] thread {root[:8]}: answered a follow-up")
    return "posted"


# --- the watcher ----------------------------------------------------------------

class _ChannelWatcher:
    """Reads the followed threads' new messages every `_CHANNEL_POLL_S` while
    at least one thread is followed, and stops when none is (the next mention
    starts it again). One per Pipe function (`_ensure_channel_watcher`)."""

    def __init__(self, valves_getter, function_id=None, conn_getter=None, now=time.time_ns):
        self.valves_getter = valves_getter
        self.function_id = function_id
        self.conn_getter = conn_getter or (lambda: _get_gateway_connection(self.valves_getter))
        self.now = now
        self.busy = set()

    function_state = _ComposerWatcher.function_state

    async def tick(self) -> "dict | None":
        """One pass. Returns the followed threads, or None when there are none."""
        valves = self.valves_getter()
        state_dir = _state_dir(getattr(valves, "STATE_DIR", ""))
        idle = float(getattr(valves, "CHANNEL_THREAD_IDLE_HOURS", 24) or 24)
        debounce_ns = int(float(getattr(valves, "CHANNEL_THREAD_DEBOUNCE_S", 15) or 0) * 1e9)
        # Only this Pipe function's threads: another copy of the Pipe (its own
        # AGENT_ID) follows the threads its model was mentioned in.
        followed = {r: t for r, t in _channel_followed(state_dir, idle, self.now()).items()
                    if t.get("function") == self.function_id}
        if not followed:
            return None
        by_channel = {}
        for root, t in followed.items():
            by_channel.setdefault(t.get("channel"), {})[root] = t
        for channel_id, threads in by_channel.items():
            if not channel_id:
                continue
            after = min(t.get("judged", 0) for t in threads.values())
            rows = await _channel_rows(channel_id, parents=threads.keys(), after_ns=after)
            # A reply to the agent's message is Open WebUI's to send (as is a
            # mention): leave it to that turn.
            bot_ids = {m["id"] for m in rows if (m.get("meta") or {}).get("model_id")}
            for ref in {m.get("reply_to_id") for m in rows} - bot_ids - {None, ""}:
                ref_row = await _channel_message(ref)
                if ref_row and (ref_row.get("meta") or {}).get("model_id"):
                    bot_ids.add(ref)
            for root, t in threads.items():
                pending = [m for m in rows if m["parent_id"] == root and m["created_at"] > t.get("judged", 0)
                           and _channel_trigger(m, followed=threads,
                                                bot_message_ids=bot_ids) == "judge"]
                if not pending or root in self.busy:
                    continue
                if self.now() - pending[-1]["created_at"] < debounce_ns:
                    continue  # more may come: one turn for the burst
                self.busy.add(root)
                spawn_background(self._handle(valves, state_dir, root, t, pending))
        return followed

    async def _handle(self, valves, state_dir, root, thread, pending):
        try:
            conn = await self.conn_getter()
            await _channel_follow_up(conn, valves, state_dir, root, thread, pending)
        except Exception as ex:
            pipe_log(f"[channels] follow-up in thread {root[:8]} failed: {ex!r}")
        finally:
            self.busy.discard(root)

    async def run(self):
        pipe_log(f"[channels] watcher started ({self.function_id or 'this Pipe'})")
        while True:
            state = await self.function_state()
            if state == "gone":
                pipe_log("[channels] Pipe function deleted: watcher stops")
                return
            followed = None
            if state != "inactive":
                try:
                    followed = await self.tick()
                except Exception as ex:
                    pipe_log(f"[channels] watcher pass failed: {ex!r}")
                    followed = True
            if not followed and not self.busy:
                pipe_log("[channels] no thread followed: watcher stops")
                return
            await asyncio.sleep(_CHANNEL_POLL_S)


_channel_watcher_task = None


def _channel_watch_slot(function_id) -> str:
    return f"{_CHANNEL_WATCH_ATTR}_{function_id}" if function_id else _CHANNEL_WATCH_ATTR


def _ensure_channel_watcher(valves_getter):
    """Start the watcher when a thread is followed and none runs for this Pipe
    function. Its task sits in a slot (per function id) on Open WebUI's own
    socket module, so a new load of the Pipe cancels the previous load's
    watcher and two copies of the Pipe keep their own."""
    global _channel_watcher_task
    try:
        if _channel_watcher_task is not None and not _channel_watcher_task.done():
            return _channel_watcher_task
        try:
            import open_webui.socket.main as _owui_socket_main
        except Exception:
            return None
        state_dir = _state_dir(getattr(valves_getter(), "STATE_DIR", ""))
        if not (_read_json_file(_channel_state_path(state_dir)) or {}).get("threads"):
            return None
        function_id = _composer_own_function_id()
        slot = _channel_watch_slot(function_id)
        stale = getattr(_owui_socket_main, slot, None)
        if stale is not None and stale is not _channel_watcher_task:
            try:
                stale.cancel()
                pipe_log("[channels] cancelled the previous load's watcher")
            except Exception:
                pass
        watcher = _ChannelWatcher(valves_getter, function_id=function_id)
        _channel_watcher_task = asyncio.get_running_loop().create_task(watcher.run())
        setattr(_owui_socket_main, slot, _channel_watcher_task)
        return _channel_watcher_task
    except Exception as ex:
        pipe_log(f"[channels] could not start the watcher: {ex}")
        return None
