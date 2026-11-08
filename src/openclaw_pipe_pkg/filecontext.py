# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# File context sent once per session (ELI-379).
#
# Open WebUI resends a chat's files with every request and wraps what it
# retrieves from them into the newest user message ("<context>...</context>",
# its RAG template, then "\n" and the text the user typed). A stateless model
# pays that once per request. The agent's session keeps every message, so the
# same file text piled up in it, once per turn (a quoted chat: 64 copies, 9.4M
# characters). The Pipe now sends each retrieved chunk once per session: a
# chunk the session already has keeps its <source> tag, so its citation number
# still holds, but goes without its text, the way Open WebUI itself cites
# content that is elsewhere (include_content=False), plus one line naming its file.
#
# A chunk is "already sent" only once its message went out (`_file_context_commit`
# after chat.send), and only when an other reply sent it: Open WebUI may call
# twice for one reply (ELI-82), and the second call must build the same text.
# The record belongs to one Gateway session: a new sessionId (/new, /reset, a
# deleted session) or a new compactionCount starts it again. claude-cli's own
# compaction changes neither, so after it the agent has only its summary of a
# file until /new.

FILE_CONTEXT_DIR = "file-context"
# (session_key, sha256 of the sent text) -> record to commit once sent, oldest first.
_file_context_pending: dict = {}
_file_context_pruned = False


def _file_context_split(content: str, typed) -> tuple[str, str] | None:
    """(Open WebUI's file context, the typed text) when Open WebUI wrapped the
    message: it puts its context before the typed text, joined by a newline."""
    if isinstance(typed, str) and typed and content.endswith("\n" + typed) and len(content) > len(typed) + 1:
        return content[: -len(typed) - 1], typed
    return None


def _file_context_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _file_context_chunks(sources) -> list[tuple[str, str, str]]:
    """(hash, file name, text) of each retrieved chunk in Open WebUI's sources."""
    chunks = []
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        about = source.get("source") or {}
        name = about.get("name") or about.get("id") or "a file"
        for text in source.get("document") or []:
            if isinstance(text, str) and text:
                chunks.append((_file_context_digest(text), name, text))
    return chunks


async def _session_epoch(conn, session_key: str) -> list | None:
    """[sessionId, compactionCount] of the session; [] when it does not exist
    yet; None when the lookup failed (the context then goes whole)."""
    try:
        lst = await conn.send_request("sessions.list", {"search": session_key[-36:], "limit": 10},
                                      timeout=15)
    except Exception as ex:
        pipe_log(f"[file context] session lookup for {session_key[-12:]} failed, sent whole: {ex!r}")
        return None
    for row in (lst or {}).get("sessions") or []:
        if row.get("key") == session_key:
            return [row.get("sessionId"), row.get("compactionCount") or 0]
    return []


def _file_context_path(state_dir: str, session_key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_key)[-160:]
    return os.path.join(state_dir, FILE_CONTEXT_DIR, f"{safe}.json")


def _file_context_sent(state: dict, epoch: list) -> dict:
    """The chunks this session already has: {hash: reply id}."""
    return dict(state.get("sent") or {}) if epoch and state.get("epoch") == epoch else {}


def _file_context_note(names: list) -> str:
    return ("(The empty <source> tags above are files attached to this chat, sent to you earlier "
            f"in this conversation and not repeated here: {', '.join(names)}. Their numbers still "
            "cite them.)")


def _file_context_strip(context: str, texts: list) -> str | None:
    """`context` with the text of each <source> tag in `texts` taken out, the
    tag kept; None if one is not found."""
    for text in texts:
        context, n = re.subn(r"(<source\b[^>]*>)" + re.escape(text) + r"</source>",
                             lambda m: m.group(1) + "</source>", context, count=1)
        if not n:
            return None
    return context


async def _file_context_message(conn, state_dir: str, session_key: str, reply_id, context: str,
                                sources) -> tuple[str, dict | None]:
    """What goes before the typed text: Open WebUI's `context` with the text of
    the chunks the session already has taken out and one line naming their
    files, and the record to commit once the message is sent."""
    chunks = [c for c in _file_context_chunks(sources) if c[2] in context]
    epoch = await _session_epoch(conn, session_key) if chunks else None
    if epoch is None:
        return context, None
    # Without a message id no other call can be this reply's twin.
    reply_id = reply_id or f"request-{uuid.uuid4().hex}"
    sent = _file_context_sent(_read_json_file(_file_context_path(state_dir, session_key)) or {}, epoch)
    old = [c for c in chunks if sent.get(c[0], reply_id) != reply_id]
    record = {"epoch": epoch, "hashes": [c[0] for c in chunks], "reply": reply_id}
    if not old:
        return context, record
    stripped = _file_context_strip(context, [text for _, _, text in old])
    if stripped is None:
        pipe_log("[file context] a chunk was not found in Open WebUI's context, sent whole")
        return context, record
    return stripped + "\n" + _file_context_note(list(dict.fromkeys(name for _, name, _ in old))), record


def _file_context_hold(session_key: str, text: str, record: dict | None) -> None:
    """Keep `record` until the message `text` is sent (or handed to a later reply)."""
    if record is not None:
        key = (session_key, _file_context_digest(text))
        _file_context_pending.pop(key, None)
        _file_context_pending[key] = record


def _file_context_take(session_key: str, sent_texts: list) -> list:
    """The records of the sent messages. Messages of this session held before
    the last sent one were either sent with it (handed on) or dropped: their
    records go too, so a failed send does not stay in memory."""
    sent = {(session_key, _file_context_digest(t)) for t in sent_texts}
    records, earlier = [], []
    for key in list(_file_context_pending):
        if key[0] != session_key:
            continue
        if key in sent:
            records.append(_file_context_pending.pop(key))
            for gone in earlier:
                _file_context_pending.pop(gone, None)
            earlier = []
        else:
            earlier.append(key)
    return records


async def _file_context_commit(conn, state_dir: str, session_key: str, sent_texts: list) -> None:
    """The messages went out: their chunks are now in the session."""
    global _file_context_pruned
    for record in _file_context_take(session_key, sent_texts):
        epoch = record["epoch"]
        if not epoch:
            # The send created the session: record it under the id it got.
            epoch = await _session_epoch(conn, session_key)
            if not epoch:
                continue
        path = _file_context_path(state_dir, session_key)
        try:
            import fcntl
        except ImportError:
            fcntl = None
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            with open(path + ".lock", "a") as lock:
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                sent = _file_context_sent(_read_json_file(path) or {}, epoch)
                for h in record["hashes"]:
                    sent.setdefault(h, record["reply"])
                _write_json_file(path, {"epoch": epoch, "sent": sent})
        except Exception as ex:
            pipe_log(f"[file context] could not record the sent chunks of {session_key[-12:]}: {ex}")
            continue
        if not _file_context_pruned:
            _file_context_pruned = True
            _history_line_prune(os.path.dirname(path), time.time() - HISTORY_LINE_KEEP_S)
