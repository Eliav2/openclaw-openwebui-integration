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
# chunk the session already has collapses into one line naming its file.
#
# A chunk is "already sent" only once its message went out (`_file_context_commit`
# after chat.send), and only when an other reply sent it: Open WebUI may call
# twice for one reply (ELI-82), and the second call must build the same text.
# The record belongs to one Gateway session: a new sessionId (/new, /reset, a
# deleted session) or a new compactionCount starts it again.

FILE_CONTEXT_DIR = "file-context"
_file_context_pending: dict = {}     # (session_key, sent text) -> record to commit once sent
_file_context_pruned = False


def _file_context_split(content: str, typed) -> tuple[str, str] | None:
    """(Open WebUI's file context, the typed text) when Open WebUI wrapped the
    message: it puts its context before the typed text, joined by a newline."""
    if isinstance(typed, str) and typed and content.endswith("\n" + typed) and len(content) > len(typed) + 1:
        return content[: -len(typed) - 1], typed
    return None


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
                chunks.append((hashlib.sha256(text.encode()).hexdigest(), name, text))
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
    """The chunks the session already has: {hash: reply id}. A record written
    before the session existed ([]) belongs to the session that turn created."""
    stored = state.get("epoch")
    if stored == epoch or stored == []:
        return dict(state.get("sent") or {})
    return {}


def _file_context_note(names: list) -> str:
    return ("(Files attached to this chat, sent to you earlier in this conversation and not "
            f"repeated here: {', '.join(names)}.)")


def _file_context_strip(context: str, texts: list) -> str | None:
    """`context` without the <source> tag of each text; None if one is not found."""
    for text in texts:
        context, n = re.subn(r"<source\b[^>]*>" + re.escape(text) + r"</source>\n?", "", context, count=1)
        if not n:
            return None
    return context


async def _file_context_message(conn, state_dir: str, session_key: str, reply_id, context: str,
                                sources) -> tuple[str, dict | None]:
    """What goes before the typed text: Open WebUI's `context` with the chunks
    the session already has replaced by one line naming their files, and the
    record to commit once the message is sent."""
    chunks = _file_context_chunks(sources)
    epoch = await _session_epoch(conn, session_key) if chunks else None
    if epoch is None:
        return context, None
    sent = _file_context_sent(_read_json_file(_file_context_path(state_dir, session_key)) or {}, epoch)
    earlier = {h for h, by in sent.items() if by != reply_id}
    old = [c for c in chunks if c[0] in earlier]
    record = {"epoch": epoch, "hashes": [c[0] for c in chunks], "reply": reply_id}
    if not old:
        return context, record
    names = list(dict.fromkeys(name for _, name, _ in old))
    if len(old) == len(chunks):
        return _file_context_note(names), record
    stripped = _file_context_strip(context, [text for _, _, text in old])
    if stripped is None:
        pipe_log("[file context] a chunk was not found in Open WebUI's context, sent whole")
        return context, record
    return stripped + "\n" + _file_context_note(names), record


def _file_context_hold(session_key: str, text: str, record: dict | None) -> None:
    """Keep `record` until the message `text` is sent (or handed to a later reply)."""
    if record is not None:
        _file_context_pending[(session_key, text)] = record


def _file_context_commit(state_dir: str, session_key: str, sent_texts: list) -> None:
    """The messages went out: their chunks are now in the session."""
    global _file_context_pruned
    for text in sent_texts:
        record = _file_context_pending.pop((session_key, text), None)
        if record is None:
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
                state = _read_json_file(path) or {}
                sent = _file_context_sent(state, record["epoch"])
                for h in record["hashes"]:
                    sent.setdefault(h, record["reply"])
                # A session created by this turn keeps [] until a later turn reads its id.
                _write_json_file(path, {"epoch": record["epoch"], "sent": sent})
        except Exception as ex:
            pipe_log(f"[file context] could not record the sent chunks of {session_key[-12:]}: {ex}")
            continue
        if not _file_context_pruned:
            _file_context_pruned = True
            _history_line_prune(os.path.dirname(path), time.time() - HISTORY_LINE_KEEP_S)
