# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# What the agent remembers vs what the chat shows (ELI-183).
#
# The Pipe sends only the newest user message, but OWUI's chat is a tree
# (Edit, Regenerate, branch arrows), so the agent's session drifts from it.
# The "line" stores the message ids last sent per chat. Walking up from the
# new user message's parents to the line tells regenerate, edit, deleted or
# branch, and only then a short note (`_history_note`) goes before the message.
# A clone or fork with no Gateway session gets the copied messages once
# (`_history_seed`). OWUI may call twice for one reply (ELI-82), so the line
# keeps the last reply id and its prefix. With several models at once
# (ELI-329) each Pipe model gets its own session ("column") and line.

HISTORY_TAIL_MAX = 64
# About 8k tokens.
HISTORY_SEED_MAX_CHARS = 32_000
HISTORY_QUOTE_CHARS = 60
HISTORY_LINE_DIR = "history-line"
HISTORY_LINE_KEEP_S = 180 * 86400
MULTI_MODEL_COLUMNS_MAX = 16
HISTORY_REPLY_MAX_CHARS = 8_000
_history_line_pruned = False

_DETAILS_BLOCK = re.compile(r"<details\b[^>]*>.*?</details>", re.S | re.I)


def _history_line_id(chat_id: str, column: int = 0) -> str:
    """Line file name: the chat id, plus `-m<column>` for columns > 0."""
    return f"{chat_id}-m{column}" if column else chat_id


def _pipe_model_column(model, models, model_idx=None) -> tuple[int, int]:
    """(column, Pipe model count) of a request among `user_message.models`.

    Pipe models share `model`'s function id; `model_idx` tells a model picked twice apart.
    """
    if not (isinstance(model, str) and "." in model and isinstance(models, list)):
        return 0, 1
    prefix = model.split(".", 1)[0] + "."
    ours = [m for m in models if isinstance(m, str) and m.startswith(prefix)]
    if len(ours) < 2 or model not in ours:
        return 0, 1
    if isinstance(model_idx, int) and 0 <= model_idx < len(models) and models[model_idx] == model:
        column = len([m for m in models[:model_idx] if isinstance(m, str) and m.startswith(prefix)])
    else:
        column = ours.index(model)
    return min(column, MULTI_MODEL_COLUMNS_MAX - 1), len(ours)


def _history_column_of(state_dir: str, chat_id: str, ids) -> int | None:
    """The column whose line holds one of `ids`, or None. Columns are made in
    order, so stop at the first one without a line."""
    ids = [i for i in ids if i]
    for column in range(MULTI_MODEL_COLUMNS_MAX):
        path = _history_line_path(state_dir, _history_line_id(chat_id, column))
        if column and not os.path.exists(path):
            break
        tail = _history_line_read(state_dir, _history_line_id(chat_id, column))["tail"]
        if any(i in tail for i in ids):
            return column
    return None


async def _reply_column(state_dir: str, chat_id: str | None, model, metadata: dict) -> int:
    """The column (session) for this reply: the model's place when several are
    picked, else the column holding the message it follows, else 0."""
    um = metadata.get("user_message") if isinstance(metadata.get("user_message"), dict) else {}
    models = um.get("models")
    model_idx = None
    if chat_id and isinstance(models, list) and models.count(model) > 1 and metadata.get("message_id"):
        try:
            model_idx = await _owui_history_rows(chat_id).model_idx(metadata["message_id"])
        except Exception as ex:
            pipe_log(f"[history] chat {chat_id[:8]}: no column index for the reply: {ex!r}")
    column, count = _pipe_model_column(model, models, model_idx)
    if count > 1:
        return column
    if chat_id:
        found = _history_column_of(state_dir, chat_id, [metadata.get("user_message_id"), um.get("parentId")])
        if found is not None:
            return found
    return 0


def _history_line_path(state_dir: str, chat_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", chat_id)[:120]
    return os.path.join(state_dir, HISTORY_LINE_DIR, f"{safe}.json")


def _history_line_read(state_dir: str, chat_id: str) -> dict:
    data = _read_json_file(_history_line_path(state_dir, chat_id))
    if not isinstance(data, dict) or not isinstance(data.get("tail"), list):
        return {"tail": []}
    return data


def _history_line_write(state_dir: str, chat_id: str, data: dict) -> None:
    """Replace the line under a lock: two OWUI workers may answer one chat."""
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
        _history_line_prune(os.path.dirname(path), time.time() - HISTORY_LINE_KEEP_S)


def _history_line_prune(folder: str, cutoff: float) -> None:
    """Drop lines older than `cutoff`; OWUI never reports chat deletion.
    Age comes from the .json, since lock files are never written."""
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        full = os.path.join(folder, name)
        try:
            if os.path.getmtime(full) < cutoff:
                os.remove(full)
                with contextlib.suppress(FileNotFoundError):
                    os.remove(full + ".lock")
        except OSError:
            pass


class _OwuiHistoryRows:
    """Reads the line needs, by row, never loading the whole chat (ELI-115)."""

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

    async def model_idx(self, message_id: str) -> int | None:
        """The reply's `modelIdx` from the chat row."""
        from sqlalchemy import select
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.chats import Chat

        async with get_async_db_context() as db:
            row = (await db.execute(
                select(Chat.chat["history"]["messages"][message_id]["modelIdx"].as_integer())
                .where(Chat.id == self.chat_id))).first()
        return row[0] if row and row[0] is not None else None

    async def message(self, message_id: str) -> tuple:
        """(role, text, model id) of a message; role None when it has no row."""
        row = await self._row(message_id, "role", "content", "model_id")
        if row is None:
            return None, None, None
        return row[0], _coerce_text(row[1]), row[2]

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
    """Compare a request with the line: {kind, replaced, shown, tail}.

    `kind` is None, "regenerate", "edit", "deleted" or "branch". `user_parent`
    is used if the user message's row is not written yet.
    """
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


async def _shown_in_full(shown: list, rows) -> str:
    """The shown messages in full, within the per-reply and total caps."""
    entries, used = [], 0
    for mid in shown:
        role, text, _ = await rows.message(mid)
        text = _DETAILS_BLOCK.sub("", text or "").strip()
        if role not in ("user", "assistant") or not text:
            continue
        if len(text) > HISTORY_REPLY_MAX_CHARS:
            text = text[:HISTORY_REPLY_MAX_CHARS - 1].rstrip() + "…"
        entry = f"{'User' if role == 'user' else 'Assistant'}: {text}"
        if used + len(entry) + 2 > HISTORY_SEED_MAX_CHARS:
            break
        entries.append(entry)
        used += len(entry) + 2
    return "\n\n".join(entries)


async def _shown_from_other_model(shown: list, rows, model_prefix: str | None) -> bool:
    """Whether a shown reply came from a non-Pipe model."""
    for mid in shown:
        role, _, model_id = await rows.message(mid)
        if role == "assistant" and model_id and model_prefix and not model_id.startswith(model_prefix):
            return True
    return False


async def _history_note(change: dict, rows, in_full: bool = False) -> str:
    """Short note for a history change. `in_full`: a branch note carries the
    shown messages, since the agent never saw them."""
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
    if in_full:
        whole = await _shown_in_full(shown, rows)
        if whole:
            return (f"[Open WebUI: the chat shows answers of several models side by side and the user "
                    f"went on from another one. {_messages(len(replaced))} from {first_old} on are no "
                    f"longer shown; it now shows {_messages(len(shown))} from {first_new} on, here in "
                    f"full:]\n\n{whole}\n\n[End of those messages. The user's new message follows.]")
    return (f"[Open WebUI: the user switched to another branch of the chat. {_messages(len(replaced))} "
            f"from {first_old} on are no longer shown; it now shows {_messages(len(shown))} from "
            f"{first_new} on, then the message below.]")


def _voice_note(metadata) -> str:
    """Note for an OWUI voice call (`features.voice`), else ""."""
    features = metadata.get("features") if isinstance(metadata, dict) else None
    if not (isinstance(features, dict) and features.get("voice") is True):
        return ""
    return ("[Open WebUI voice call: your reply will be read aloud. Answer in the user's language, "
            "in one to three short spoken sentences unless they ask for more. Plain speech only: "
            "no markdown, lists, tables, code, links or emoji. Say numbers and times the way you "
            "would say them out loud.]\n\n")


# The chat's system prompt. Open WebUI writes the prompts people set into the request's
# system message, and also its own instructions for features the agent doesn't get
# (voice mode, code interpreter, skills, knowledge, tool servers). So the Pipe reads
# the three people set from where they are kept, not from that message, and joins
# them in Open WebUI's order: the model's (Admin > Models), the chat's folder's, then
# the user's (the chat's Controls, else Settings > General).
SYSTEM_ROLE = "system"          # a chat message's role
OWUI_ADMIN_ROLE = "admin"       # an Open WebUI user's role
FOLDER_READ = "read"            # has_folder_access permission

async def _model_system_prompt(metadata: dict) -> str:
    """The prompt set on the model Open WebUI ran (a workspace model's own), read
    from its row as functions.py does: the model in the metadata has it removed
    (utils/models.py `public_model_params`)."""
    model = metadata.get("model") if isinstance(metadata.get("model"), dict) else {}
    if not model.get("id"):
        return ""
    try:
        from open_webui.models.models import Models
        row = await Models.get_model_by_id(model["id"])
        params = row.params.model_dump() if row and row.params else {}
        return params.get("system") or ""
    except Exception as ex:
        pipe_log(f"[system prompt] model {model['id']} not read: {ex!r}")
        return ""


def _user_system_prompt(metadata: dict) -> str:
    """The system messages the browser sent, before Open WebUI added its own:
    the chat's Controls prompt, else the user's (main.py `chat_context`)."""
    context = metadata.get("chat_context") if isinstance(metadata.get("chat_context"), dict) else {}
    texts = [_coerce_text(m.get("content")).strip() for m in context.get("messages") or []
             if isinstance(m, dict) and m.get("role") == SYSTEM_ROLE]
    return "\n".join(t for t in texts if t)


async def _folder_system_prompt(metadata: dict, chat_id: str | None, user_id: str | None) -> str:
    """The prompt of the chat's folder, with Open WebUI's access rule
    (middleware.py: a non-admin needs read access to the folder)."""
    try:
        from open_webui.models.chats import Chats
        from open_webui.models.folders import Folders
        from open_webui.models.users import Users
        from open_webui.utils.access_control.folders import has_folder_access
        folder_id = (await Chats.get_chat_folder_id(chat_id, user_id) if chat_id and user_id else None) \
            or metadata.get("folder_id")
        if not folder_id:
            return ""
        folder = await Folders.get_folder_by_id(folder_id)
        user = await Users.get_user_by_id(user_id) if user_id else None
        if not folder or not user:
            return ""
        if user.role != OWUI_ADMIN_ROLE and not await has_folder_access(user.id, folder, FOLDER_READ,
                                                                         db=None):
            return ""
        return (folder.data or {}).get("system_prompt") or ""
    except Exception as ex:
        pipe_log(f"[system prompt] folder of chat {str(chat_id)[:8]} not read: {ex!r}")
        return ""


async def _resolved_prompt(text: str, metadata: dict, user_id: str | None) -> str:
    """`text` with Open WebUI's prompt variables filled in, as it does for any model."""
    try:
        from open_webui.models.users import Users
        from open_webui.utils.payload import resolve_system_prompt
        user = await Users.get_user_by_id(user_id) if user_id else None
        return await resolve_system_prompt(text, metadata, user)
    except Exception as ex:
        pipe_log(f"[system prompt] variables not filled in: {ex!r}")
        return text


async def _system_prompt_note(metadata: dict, chat_id: str | None, user_id: str | None) -> str:
    """The chat's system prompt as a note for the agent, else "". OpenClaw has no
    per-request system prompt, so it goes before every message; the agent's own
    instructions still come first and win."""
    parts = []
    for text in (await _model_system_prompt(metadata),
                 await _folder_system_prompt(metadata, chat_id, user_id),
                 _user_system_prompt(metadata)):
        text = (await _resolved_prompt(text, metadata, user_id)).strip() if text else ""
        if text:
            parts.append(text)
    if not parts:
        return ""
    return ("[Open WebUI system prompt for this chat (set on the model, the chat's folder or by "
            "the user in Open WebUI; it comes with every message):]\n"
            + "\n".join(parts) + "\n[End of the system prompt. The user's message follows.]\n\n")


def _history_seed(messages: list, side_by_side: bool = False) -> str:
    """Earlier messages for a new session, newest kept first within the cap.
    `side_by_side`: a new model column rather than a copied chat."""
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
    why = ("this chat shows answers of several models side by side, and you are one of them. "
           "You have not seen its earlier messages" if side_by_side
           else "this chat is a copy of another chat. You have not seen its earlier messages")
    return (f"[Open WebUI: {why}, so here they are{cut}:]\n\n" + "\n\n".join(reversed(kept))
            + f"\n\n[End of the {'earlier' if side_by_side else 'copied'} messages. "
            "The user's new message follows.]")


async def _gateway_has_session(conn, session_key: str) -> bool:
    try:
        lst = await conn.send_request("sessions.list", {"search": session_key[-36:], "limit": 10},
                                      timeout=15)
    except Exception as ex:
        pipe_log(f"[history] session lookup for {session_key[-12:]} failed: {ex!r}")
        return False
    return any(r.get("key") == session_key for r in lst.get("sessions") or [])


async def _history_prefix(conn, state_dir: str, chat_id: str | None, session_key: str,
                          metadata: dict, messages: list, column: int = 0, model=None) -> str:
    """Prefix that syncs the agent with the chat ("" normally); updates the line."""
    user_id = metadata.get("user_message_id")
    reply_id = metadata.get("message_id")
    if not (chat_id and user_id and reply_id):
        return ""   # not a user's turn in a saved chat (e.g. a scheduled turn)
    line_id = _history_line_id(chat_id, column)
    line = _history_line_read(state_dir, line_id)
    if line.get("reply") == reply_id:
        return line.get("prefix") or ""   # Open WebUI's second call for this reply
    rows = _owui_history_rows(chat_id)
    um = metadata.get("user_message") if isinstance(metadata.get("user_message"), dict) else {}
    user_parent = um.get("parentId")
    prefix = ""
    try:
        change = await _history_change(line["tail"], user_id, reply_id, rows, user_parent)
        if change["kind"]:
            in_full = False
            if change["kind"] == "branch":
                prefixed = isinstance(model, str) and "." in model
                in_full = (len(um.get("models") or []) > 1 or await _shown_from_other_model(
                    change["shown"], rows, model.split(".", 1)[0] + "." if prefixed else None))
            prefix = await _history_note(change, rows, in_full) + "\n\n"
            pipe_log(f"[history] chat {chat_id[:8]}: {change['kind']}, {len(change['replaced'])} "
                     f"replaced, {len(change['shown'])} shown")
        elif not line["tail"] and len(messages) > 1 and column \
                and not await _gateway_has_session(conn, session_key):
            seed = _history_seed(messages[:-1], side_by_side=True)
            if seed:
                prefix = seed + "\n\n"
                pipe_log(f"[history] chat {chat_id[:8]}: new model column {column}, "
                         f"seeding {len(seed)} chars")
        elif not line["tail"] and len(messages) > 1 and await rows.origin() \
                and not await _gateway_has_session(conn, session_key):
            seed = _history_seed(messages[:-1])
            if seed:
                prefix = seed + "\n\n"
                pipe_log(f"[history] chat {chat_id[:8]}: a copy, seeding {len(seed)} chars")
    except Exception as ex:
        pipe_log(f"[history] chat {chat_id[:8]}: could not compare with the line: {ex!r}")
        return ""
    _history_line_write(state_dir, line_id, {"tail": change["tail"], "reply": reply_id,
                                             "prefix": prefix})
    return prefix


def _owui_history_rows(chat_id: str):
    return _OwuiHistoryRows(chat_id)
