# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# What the agent remembers vs what the chat shows (ELI-183).
#
# The Pipe sends only the newest user message, and the agent's session is one
# line of messages. Open WebUI's chat is a tree: Edit sends a new sibling of a
# user message, Regenerate a new sibling of a reply, and the arrows under a
# message switch the branch shown. None of that reaches the agent by itself,
# so its memory and the chat drift apart. A cloned or forked chat is a new chat,
# so it gets a new session that has never seen the copied messages.
#
# Line: for each chat, the message ids the agent was last sent along the
# branch (`HISTORY_TAIL_MAX` newest), kept in the bridge state dir. Each new
# request names its user message and its reply (`user_message_id`,
# `message_id` in Open WebUI's metadata). Walking up from the user message
# through Open WebUI's `chat_message` rows (parent ids) to the first id on the
# line says what changed:
#   - the user message is already on the line: Regenerate. What came after it
#     on the line is gone from the chat;
#   - the line's tail after the meeting point is gone and the user message
#     hangs right under it: Edit, when the replaced user message still exists
#     as its sibling; deleted messages, when it does not;
#   - other messages lie between the meeting point and the user message: the
#     user switched branches.
# Messages the chat gained without the agent losing any (a reply the agent
# started, a cron wake) change nothing. Only then does the message carry a
# short note (`_history_note`), so a normal send never does.
#
# Copy: on the first request of a chat Open WebUI marks as a clone or fork
# (`originalChatId` on the chat) whose session the Gateway does not have, the
# message carries the copied messages once (`_history_seed`), newest first up
# to `HISTORY_SEED_MAX_CHARS`.
#
# Open WebUI can call the Pipe twice for one reply (ELI-82); the second call
# must send the same text, so the line keeps the last reply id and what was
# put before its message.
# ---------------------------------------------------------------------------

HISTORY_TAIL_MAX = 64
# About 8k tokens. Older copied messages are left out whole; the note says how
# many.
HISTORY_SEED_MAX_CHARS = 32_000
HISTORY_QUOTE_CHARS = 60
HISTORY_LINE_DIR = "history-line"
HISTORY_LINE_KEEP_S = 180 * 86400
_history_line_pruned = False

_DETAILS_BLOCK = re.compile(r"<details\b[^>]*>.*?</details>", re.S | re.I)


def _history_line_path(state_dir: str, chat_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", chat_id)[:120]
    return os.path.join(state_dir, HISTORY_LINE_DIR, f"{safe}.json")


def _history_line_read(state_dir: str, chat_id: str) -> dict:
    data = _read_json_file(_history_line_path(state_dir, chat_id))
    if not isinstance(data, dict) or not isinstance(data.get("tail"), list):
        return {"tail": []}
    return data


def _history_line_write(state_dir: str, chat_id: str, data: dict) -> None:
    """Replace the chat's line under its lock (two Open WebUI workers may
    answer the same chat). Nothing is awaited while the lock is held."""
    global _history_line_pruned
    path = _history_line_path(state_dir, chat_id)
    try:
        import fcntl
    except ImportError:
        fcntl = None
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path + ".lock", "a") as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            _write_json_file(path, data)
    except Exception as ex:
        pipe_log(f"[history] could not save the line of chat {chat_id[:8]}: {ex}")
        return
    if not _history_line_pruned:
        _history_line_pruned = True
        cutoff = time.time() - HISTORY_LINE_KEEP_S
        folder = os.path.dirname(path)
        for name in os.listdir(folder):
            full = os.path.join(folder, name)
            try:
                if os.path.getmtime(full) < cutoff:
                    os.remove(full)
            except OSError:
                pass


class _OwuiHistoryRows:
    """The few reads of one chat that the line needs, from Open WebUI's
    `chat_message` rows (one per message) and one field of the chat row, so
    the chat itself is never loaded (ELI-115: about 14x its JSON per read)."""

    def __init__(self, chat_id: str):
        self.chat_id = chat_id

    async def _row(self, message_id: str, *columns):
        from sqlalchemy import select
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.chat_messages import ChatMessage

        async with get_async_db_context() as db:
            return (await db.execute(
                select(*[getattr(ChatMessage, c) for c in columns])
                .where(ChatMessage.id == f"{self.chat_id}-{message_id}"))).first()

    async def parent(self, message_id: str) -> tuple:
        """(the row exists, its parent id)."""
        row = await self._row(message_id, "parent_id")
        return (row is not None, row[0] if row is not None else None)

    async def text(self, message_id: str) -> str | None:
        row = await self._row(message_id, "content")
        return None if row is None else _coerce_text(row[0])

    async def origin(self) -> str | None:
        """The chat this one was cloned or forked from, if any."""
        from sqlalchemy import select
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.chats import Chat

        async with get_async_db_context() as db:
            row = (await db.execute(select(Chat.chat["originalChatId"].as_string())
                                    .where(Chat.id == self.chat_id))).first()
        return row[0] if row and row[0] else None


async def _history_change(tail: list, user_id: str, reply_id: str, rows,
                          user_parent: str | None = None) -> dict:
    """Compare a new request with the chat's line. Returns {kind, replaced,
    shown, tail}: `kind` is None (nothing the agent knew is gone),
    "regenerate", "edit", "deleted" or "branch"; `replaced` the line's ids no
    longer on the chat's branch, `shown` the ids now between them and the user
    message; `tail` the new line. `user_parent` stands in for the user
    message's parent if its row is not there yet."""
    if user_id in tail:
        meet, walked = tail.index(user_id), []
    else:
        meet, walked = -1, [user_id]
        found, cur = await rows.parent(user_id)
        if not found:
            cur = user_parent
        while cur and len(walked) <= HISTORY_TAIL_MAX:
            if cur in tail:
                meet = tail.index(cur)
                break
            walked.append(cur)
            found, cur = await rows.parent(cur)
            if not found:
                break
    replaced = tail[meet + 1:]
    shown = list(reversed(walked[1:]))
    new_tail = (tail[:meet + 1] + list(reversed(walked)) + [reply_id])[-HISTORY_TAIL_MAX:]
    if not replaced:
        kind = None
    elif user_id in tail:
        kind = "regenerate"
    elif shown:
        kind = "branch"
    else:
        # Edit keeps the original message as a sibling: same parent.
        found, parent = await rows.parent(replaced[0])
        anchor = tail[meet] if meet >= 0 else None
        kind = "edit" if found and parent == anchor else "deleted"
    return {"kind": kind, "replaced": replaced, "shown": shown, "tail": new_tail}


def _history_quote(text: str | None) -> str:
    text = " ".join((text or "").split())
    if len(text) > HISTORY_QUOTE_CHARS:
        text = text[:HISTORY_QUOTE_CHARS - 1].rstrip() + "…"
    return json.dumps(text, ensure_ascii=False)


def _messages(n: int) -> str:
    return f"{n} message" + ("" if n == 1 else "s")


async def _history_note(change: dict, rows) -> str:
    """The short note put before the message when the chat no longer shows
    what the agent remembers (about 50 tokens: counts plus two short quotes)."""
    kind, replaced, shown = change["kind"], change["replaced"], change["shown"]
    if kind == "regenerate":
        after = f" and the {_messages(len(replaced) - 1)} after it" if len(replaced) > 1 else ""
        return (f"[Open WebUI: the user asked for a new answer to the message below. "
                f"Your earlier answer to it{after} is no longer in the chat; answer it again.]")
    if kind == "edit":
        old = _history_quote(await rows.text(replaced[0]))
        after = f", and the {_messages(len(replaced) - 1)} after it are" if len(replaced) > 1 else " is"
        return (f"[Open WebUI: the user edited their message {old}. The message below replaces it; "
                f"the original{after} no longer in the chat.]")
    if kind == "deleted":
        gone = ("the last message you remember was" if len(replaced) == 1
                else f"the last {_messages(len(replaced))} you remember were")
        return (f"[Open WebUI: {gone} deleted from the chat. "
                f"The message below follows what is left.]")
    first_old = _history_quote(await rows.text(replaced[0]))
    first_new = _history_quote(await rows.text(shown[0]))
    return (f"[Open WebUI: the user switched to another branch of the chat. {_messages(len(replaced))} "
            f"from {first_old} on are no longer shown; it now shows {_messages(len(shown))} from "
            f"{first_new} on, then the message below.]")


def _history_seed(messages: list) -> str:
    """The copied messages of a cloned chat, for its first message: newest
    first while they fit in `HISTORY_SEED_MAX_CHARS`, tool-call details left
    out. Empty when there is nothing to copy."""
    entries = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _DETAILS_BLOCK.sub("", _coerce_text(m.get("content"))).strip()
        if text:
            entries.append(f"{'User' if role == 'user' else 'Assistant'}: {text}")
    if not entries:
        return ""
    kept, used = [], 0
    for entry in reversed(entries):
        if used + len(entry) + 2 > HISTORY_SEED_MAX_CHARS:
            if not kept:   # the newest alone is too long: keep its end
                kept.append("…" + entry[-(HISTORY_SEED_MAX_CHARS - 1):])
            break
        kept.append(entry)
        used += len(entry) + 2
    left_out = len(entries) - len(kept)
    cut = f"; the oldest {_messages(left_out)} are left out to fit" if left_out else ""
    return ("[Open WebUI: this chat is a copy of another chat. You have not seen its earlier "
            f"messages, so here they are{cut}:]\n\n" + "\n\n".join(reversed(kept))
            + "\n\n[End of the copied messages. The user's new message follows.]")


async def _gateway_has_session(conn, session_key: str) -> bool:
    try:
        lst = await conn.send_request("sessions.list", {"search": session_key[-36:], "limit": 10},
                                      timeout=15)
    except Exception as ex:
        pipe_log(f"[history] session lookup for {session_key[-12:]} failed: {ex!r}")
        return False
    return any(r.get("key") == session_key for r in lst.get("sessions") or [])


async def _history_prefix(conn, state_dir: str, chat_id: str | None, session_key: str,
                          metadata: dict, messages: list) -> str:
    """What to put before this request's message so the agent's memory matches
    the chat (see above): "" on a normal send. Updates the chat's line."""
    user_id = metadata.get("user_message_id")
    reply_id = metadata.get("message_id")
    if not (chat_id and user_id and reply_id):
        return ""   # not a user's turn in a saved chat (e.g. a scheduled turn)
    line = _history_line_read(state_dir, chat_id)
    if line.get("reply") == reply_id:
        return line.get("prefix") or ""   # Open WebUI's second call for this reply
    rows = _owui_history_rows(chat_id)
    user_parent = ((metadata.get("user_message") or {}).get("parentId")
                   if isinstance(metadata.get("user_message"), dict) else None)
    prefix = ""
    try:
        change = await _history_change(line["tail"], user_id, reply_id, rows, user_parent)
        if change["kind"]:
            prefix = await _history_note(change, rows) + "\n\n"
            pipe_log(f"[history] chat {chat_id[:8]}: {change['kind']}, {len(change['replaced'])} "
                     f"replaced, {len(change['shown'])} shown")
        elif not line["tail"] and len(messages) > 1 and await rows.origin() \
                and not await _gateway_has_session(conn, session_key):
            seed = _history_seed(messages[:-1])
            if seed:
                prefix = seed + "\n\n"
                pipe_log(f"[history] chat {chat_id[:8]}: a copy, seeding {len(seed)} chars")
    except Exception as ex:
        pipe_log(f"[history] chat {chat_id[:8]}: could not compare with the line: {ex!r}")
        return ""
    _history_line_write(state_dir, chat_id, {"tail": change["tail"], "reply": reply_id,
                                             "prefix": prefix})
    return prefix


def _owui_history_rows(chat_id: str):
    return _OwuiHistoryRows(chat_id)
