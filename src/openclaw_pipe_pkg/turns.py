# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Turn identity (ELI-100, docs/design/agent-turns.md 2.2)
# ---------------------------------------------------------------------------
# A run's message is decided from its run id alone, so any process agrees.
#   msg-<chat>-<M>-<suffix>   pipe-started run: message M
#   owui-action-<...>         Status Action runs (/compact): none
#   anything else             agent turn: uuid5(TURN_NS, "<key>:<run>")

TURN_NS = uuid.UUID("6f1c3e2a-9d4b-4c6e-8a1f-0e0a1b100100")
ACTION_RUN_PREFIX = "owui-action-"
_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
# `parse_pipe_run_id`'s kind of a run that writes a reply.
PIPE_RUN_REPLY = "reply"
_PIPE_RUN_RE = re.compile(rf"^msg-(?P<chat>{_UUID_RE})-(?P<message>{_UUID_RE})-(?P<rest>.+)$")


def pipe_run_id(chat_id: str, message_id: str, kind: str | None = None, at: float | None = None) -> str:
    """Run id for a pipe run writing into `message_id`; `_run_id_sent_at` reads the trailing time.
    `at` is the send time: a message kept for later sends again under its first id (ELI-504)."""
    return f"msg-{chat_id}-{message_id}-{kind + '-' if kind else ''}{time.time() if at is None else at}"


def parse_pipe_run_id(run_id: str) -> dict | None:
    """{chat, message, kind} of a pipe-started run id, else None."""
    m = _PIPE_RUN_RE.match(run_id or "")
    if not m:
        return None
    head = m.group("rest").split("-", 1)[0]
    kind = head if head in ("input", "resume") else PIPE_RUN_REPLY
    return {"chat": m.group("chat"), "message": m.group("message"), "kind": kind}


# Every run the Gateway starts to announce something (a sub-agent's result,
# a requester settle) has an id with this prefix; none carries a user message.
ANNOUNCE_RUN_PREFIX = "announce:"


def parse_announce_run_id(run_id: str) -> dict | None:
    """{child_session, child_run} of a sub-agent announce run, else None.
    Id shape: `announce:v1:<childSessionKey>:<childRunId>[:wake|:retry-<n>]` (announce-idempotency-D7LnUTJR.mjs:4-10)."""
    if not (run_id or "").startswith("announce:v1:"):
        return None
    rest = re.sub(r":(?:wake|retry-\d+)$", "", run_id[len("announce:v1:"):])
    child_session, _, child_run = rest.rpartition(":")
    if not child_session.startswith("agent:") or not child_run:
        return None
    return {"child_session": child_session, "child_run": child_run}


def turn_message_id(session_key: str, run_id: str, chat_id: str | None = None) -> str | None:
    """The message `run_id` renders into, or None (Status Action runs). A pipe
    run id is only trusted inside the chat it was minted for."""
    if not run_id or run_id.startswith(ACTION_RUN_PREFIX):
        return None
    parsed = parse_pipe_run_id(run_id)
    if parsed and (chat_id is None or parsed["chat"] == chat_id):
        return parsed["message"]
    return str(uuid.uuid5(TURN_NS, f"{session_key}:{run_id}"))


# ---------------------------------------------------------------------------
# Claim: one message per run, anchored at the tail (DESIGN 2.3)
# ---------------------------------------------------------------------------
# The claim lives in Open WebUI's DB under the chat's row lock, so every
# process agrees: whoever inserts M first owns it.

_TURN_WRITERS_ATTR = "_openclaw_turn_writers"


def _live_turn_writers() -> set:
    """Writer ids alive in this process. Kept on `open_webui.socket.main` so
    every load of this Function (a redeploy keeps old modules alive) shares it."""
    mod = sys.modules.get("open_webui.socket.main")
    if mod is None:
        return set()
    writers = getattr(mod, _TURN_WRITERS_ATTR, None)
    if writers is None:
        writers = set()
        setattr(mod, _TURN_WRITERS_ATTR, writers)
    return writers


def _register_turn_writer(writer_id: str) -> None:
    _live_turn_writers().add(writer_id)


def _retire_turn_writer(writer_id: str) -> None:
    _live_turn_writers().discard(writer_id)


# Writer id: `<host>/<boot>/<pid>.<start>:<uuid>`; boot id and start time
# guard against a reused pid. Another host's or boot's writer is judged gone:
# a takeover across hosts would need a shared lease.
_WRITER_ID_RE = re.compile(r"^(?P<host>[^/]*)/(?P<boot>[^/]*)/(?P<pid>\d+)\.(?P<start>\d+):")


def _proc_start(pid: int) -> str | None:
    """Start time of process `pid` (/proc/<pid>/stat field 22), None if gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()[:8]
    except OSError:
        return ""


def turn_writer_id() -> str:
    import socket
    pid = os.getpid()
    return f"{socket.gethostname()}/{_boot_id()}/{pid}.{_proc_start(pid) or 0}:{uuid.uuid4()}"


def _turn_writer_alive(writer: str | None) -> bool:
    if not writer:
        return False
    if writer in _live_turn_writers():
        return True
    m = _WRITER_ID_RE.match(writer)
    if not m or int(m.group("pid")) == os.getpid():
        return False
    import socket
    if (m.group("host"), m.group("boot")) != (socket.gethostname(), _boot_id()):
        return False
    return _proc_start(int(m.group("pid"))) == m.group("start")


def _writer_may_take_over(existing: dict) -> bool:
    """A recorded writer's message can be taken only when that writer is gone
    and the message is unfinished. A stub with no writer is handed over only
    with `takeover=True` (DESIGN 2.6)."""
    writer = (existing.get("meta") or {}).get("openclaw_writer")
    return bool(writer) and existing.get("done") is False and not _turn_writer_alive(writer)


async def _uncancellable(coro):
    """Await `coro`, a write to Open WebUI's DB, to its end even if this task is
    cancelled meanwhile, then let the cancel go on. A cancel inside the DB call
    can wedge it and hold SQLite's write lock for good (ELI-205)."""
    task = spawn_background(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            await asyncio.wait({task})
        raise


async def _owui_upsert_message(chat_id: str, message_id: str, fields: dict):
    """Open WebUI's message upsert, never cut short by a cancel (ELI-205)."""
    from open_webui.models.chats import Chats
    # The held messages are scheduled in the protected task, once the done row
    # committed: a cancel of the caller after the commit cannot skip them.
    on_commit = (lambda: _answer_held_messages(chat_id, message_id)) if fields.get("done") is True else None
    return await _uncancellable(_committed_then(
        Chats.upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, fields), on_commit))


_HOLD_ORIG_ATTR = "_openclaw_queue_user_message"
# On Open WebUI's subagents module: {Function id: its AUTO_SEND_WHILE_RUNNING getter}.
_HOLD_SETTINGS_ATTR = "_openclaw_auto_send_by_function"
# The run settings of each reply's own request (`_remember_reply_run`), newest last.
_REPLY_RUNS: "collections.OrderedDict[tuple, dict]" = collections.OrderedDict()
_REPLY_RUNS_MAX = 256


def _ensure_no_owui_hold(auto_send=lambda: True) -> None:
    """Keep Open WebUI from holding a message sent while an OpenClaw reply
    runs (ELI-509). From 0.13.4 main.py holds it under the running reply
    (subagents.queue_user_message) until that reply's request ends with it
    done; the Pipe steers it into the run itself, in order, as before 0.13.4.
    With `auto_send()` false (AUTO_SEND_WHILE_RUNNING off) it is held, as
    Open WebUI's queue held it up to 0.13.3: sent when the reply ends or on
    Send now (`_answer_held_on_send_now`).

    One wrapper serves every OpenClaw Function: each load registers its own
    Function id's setting, and a reply is decided by the Function its model
    id names (`<function>.<model>`), so two installs never use each other's
    valve. Other models' replies are held as Open WebUI holds them. No-op
    before 0.13.4."""
    mod = sys.modules.get("open_webui.utils.subagents")
    if mod is None:
        try:
            import open_webui.utils.subagents as mod
        except Exception:
            return
    if getattr(mod, "queue_user_message", None) is None:
        return  # Open WebUI before 0.13.4
    settings = getattr(mod, _HOLD_SETTINGS_ATTR, None)
    if settings is None:
        settings = {}
        setattr(mod, _HOLD_SETTINGS_ATTR, settings)
    settings[_composer_own_function_id() or _COMPOSER_DEFAULT_MODEL_PREFIX.rstrip(".")] = auto_send
    if getattr(mod.queue_user_message, "_openclaw_module", None) == __name__:
        return
    orig = getattr(mod, _HOLD_ORIG_ATTR, None) or mod.queue_user_message

    async def queue_user_message(request, chat_id, user, user_message, parent_id, *args, **kwargs):
        from open_webui.models.chats import Chats
        reply = await Chats.get_message_by_id_and_message_id(chat_id, parent_id) or {}
        owner = getattr(mod, _HOLD_SETTINGS_ATTR, {}).get(str(reply.get("model") or "").split(".", 1)[0])
        if owner is not None and owner():
            return False
        return await orig(request, chat_id, user, user_message, parent_id, *args, **kwargs)

    queue_user_message._openclaw_module = __name__
    setattr(mod, _HOLD_ORIG_ATTR, orig)
    mod.queue_user_message = queue_user_message


def _answer_held_messages(chat_id: str, reply_id: str) -> None:
    """Answer the messages Open WebUI holds under reply `reply_id`, now done
    (ELI-509). From 0.13.4 a message sent while a reply runs waits under it
    (meta.status "pending", main.py queue_user_message) and is answered when
    the request writing the reply ends with it done. A reply the Pipe saves
    after its request ended (handed over, steered) is done later, so one held
    before `_ensure_no_owui_hold` ran would wait for good. Before 0.13.4
    nothing is held."""
    spawn_background(_answer_held_messages_now(chat_id, reply_id))


def _remember_reply_run(chat_id: str | None, reply_id: str | None, metadata: dict | None) -> None:
    """Keep the settings of the request writing reply `reply_id` (its model,
    tools, chat context: Controls' system prompt, variables), which Open WebUI
    gives the messages it held under that reply when it sends them
    (middleware.py: get_pending_run(metadata)). A reply the Pipe ends later
    sends them with the same (`_answer_held_messages_now`). 0.13.4+ only."""
    if not chat_id or not reply_id or not metadata:
        return
    try:
        from open_webui.utils.subagents import get_pending_run
    except ImportError:
        return
    _REPLY_RUNS[(chat_id, reply_id)] = get_pending_run(metadata)
    _REPLY_RUNS.move_to_end((chat_id, reply_id))
    while len(_REPLY_RUNS) > _REPLY_RUNS_MAX:
        _REPLY_RUNS.popitem(last=False)


def _answer_held_on_send_now(chat_id: str, reply_id: str) -> None:
    """Send now on a reply Open WebUI holds messages under (AUTO_SEND_WHILE_RUNNING
    off, 0.13.4+): its cancel parked the run for the follow-up, and the held
    messages are that follow-up. Open WebUI's own Send now only stops the
    reply and answers them once it is done, which a parked run is not."""
    spawn_background(_answer_held_messages_now(chat_id, reply_id, idle=True))


async def _answer_held_messages_now(chat_id: str, reply_id: str, idle: bool = False) -> None:
    app = getattr(sys.modules.get("open_webui.main"), "app", None)
    if app is None:
        return
    try:
        from open_webui.utils.subagents import get_pending_run, process_pending_internal_messages
    except ImportError:
        return  # Open WebUI before 0.13.4
    try:
        from open_webui.models.chat_messages import ChatMessages
        from open_webui.models.chats import Chats
        from open_webui.tasks import list_task_ids_by_item_id, tasks
        from starlette.requests import Request

        if not await ChatMessages.has_queued_message(chat_id, reply_id):
            return
        # A request still running in the chat answers them itself when it ends
        # (middleware.py background_tasks_handler): after it, only what it left.
        for task_id in await list_task_ids_by_item_id(getattr(getattr(app, "state", None), "redis", None), chat_id):
            task = tasks.get(task_id)
            if task is not None and not task.done() and task is not asyncio.current_task():
                await asyncio.wait({task})
        chat = await Chats.get_chat_by_id(chat_id)
        reply = await Chats.get_message_by_id_and_message_id(chat_id, reply_id) or {}
        if chat is None or not await ChatMessages.has_queued_message(chat_id, reply_id):
            return
        request = Request({"type": "http", "app": app, "method": "POST", "path": "/",
                           "headers": [], "query_string": b""})
        # `idle`: once nothing runs in the chat, whether or not the reply is done.
        # The reply's own request's settings, as Open WebUI sends them; the model alone
        # when that request was in another process or before a restart.
        run = _REPLY_RUNS.pop((chat_id, reply_id), None) or get_pending_run({}, reply.get("model"))
        await process_pending_internal_messages(
            request, chat_id, chat.user_id, run, reply_id=None if idle else reply_id)
        pipe_log(f"  messages held under reply {reply_id[:8]}... sent on, now it is done")
    except Exception as ex:
        pipe_log(f"  answering the messages held under reply {reply_id[:8]}... failed: {ex!r}")


async def _committed_then(write, on_commit):
    """`write`'s result, after `on_commit()` when it is truthy (the write
    committed). Run inside `_uncancellable`'s task, so a cancel of the caller
    between the commit and its resume cannot skip it."""
    result = await write
    if result and on_commit is not None:
        on_commit()
    return result


async def _owui_upsert_message_with_statuses(chat_id: str, message_id: str, fields: dict,
                                             statuses: list, on_commit=None) -> bool:
    """`fields` upserted and `statuses` appended to the message's statusHistory
    in one write under the chat's row lock: a reply's final status lines ride
    in its end snapshot instead of a save each. Never cut short by a cancel;
    `on_commit` runs once it committed, even when the caller was cancelled."""
    if fields.get("done") is True:
        then = on_commit

        def on_commit():
            if then is not None:
                then()
            _answer_held_messages(chat_id, message_id)
    return await _uncancellable(_committed_then(
        _owui_upsert_message_with_statuses_now(chat_id, message_id, fields, statuses), on_commit))


async def _owui_upsert_message_with_statuses_now(chat_id: str, message_id: str, fields: dict,
                                                 statuses: list) -> bool:
    from open_webui.models.chats import Chats
    from open_webui.models.chat_messages import ChatMessages
    from sqlalchemy.orm.attributes import flag_modified

    async with Chats._chat_transaction(chat_id) as (db, chat):
        if chat is None:
            return False
        data = chat.chat or {}
        history = data.get("history") or {}
        saved = (history.get("messages") or {}).get(message_id) or {}
        # As Open WebUI's upsert and its status save, in one transaction.
        message = Chats._clean_null_bytes(
            {**fields, "statusHistory": [*(saved.get("statusHistory") or []), *statuses]})
        message = Chats.upsert_message_to_history(history, message_id, message)
        await ChatMessages.upsert_message(message_id, chat_id, message.get("user_id") or chat.user_id,
                                          message, db=db)
        chat.chat = {**data, "history": history}
        flag_modified(chat, "chat")
        chat.updated_at = int(time.time())
        return True


async def _claim_turn_message(chat_id: str, user_id: str, message_id: str, run_id: str,
                              writer_id: str, takeover: bool = False) -> dict:
    """Claim `message_id` for `run_id` under the chat's row lock, inserting it
    under the deepest leaf. Never cut short by a cancel (ELI-205).

    Returns {claimed, how: new|takeover|taken, message_id, parent, messages,
    was_done: a taken-over message was already finished}."""
    return await _uncancellable(_claim_turn_message_now(
        chat_id, user_id, message_id, run_id, writer_id, takeover))


async def _claim_turn_message_now(chat_id: str, user_id: str, message_id: str, run_id: str,
                                  writer_id: str, takeover: bool) -> dict:
    from open_webui.models.chats import Chats
    from open_webui.models.chat_messages import ChatMessages
    from sqlalchemy.orm.attributes import flag_modified

    async with Chats._chat_transaction(chat_id) as (db, chat):
        if chat is None:
            # Deleted in Open WebUI while its session lives on (ELI-218).
            return {"claimed": False, "how": "gone", "message_id": message_id,
                    "parent": None, "messages": {}}
        data = chat.chat or {}
        history = data.get("history") or {"messages": {}, "currentId": None}
        msgs = history.setdefault("messages", {})
        existing = msgs.get(message_id)
        if existing is not None:
            holder = (existing.get("meta") or {}).get("openclaw_writer")
            held = bool(holder) and holder != writer_id and _turn_writer_alive(holder)
            if held or not (takeover or _writer_may_take_over(existing)):
                return {"claimed": False, "how": "taken", "message_id": message_id,
                        "parent": existing.get("parentId"), "messages": {}}
            meta = {**(existing.get("meta") or {}), "openclaw_run": run_id,
                    "openclaw_writer": writer_id}
            msgs[message_id] = {**existing, "meta": meta}
            changed = {message_id: msgs[message_id]}
            how, parent = "takeover", existing.get("parentId")
            was_done = existing.get("done") is True
        else:
            parent = _deepest_leaf_id(msgs, history.get("currentId"))
            leaf = msgs.get(parent) or {}
            model = leaf.get("model") or next(iter(data.get("models") or []), None)
            msgs[message_id] = {
                "id": message_id, "parentId": parent, "childrenIds": [], "role": "assistant",
                "content": "", "output": [], "done": False, "model": model,
                "modelName": leaf.get("modelName") or model, "timestamp": int(time.time()),
                "meta": {"openclaw_run": run_id, "openclaw_writer": writer_id},
            }
            changed = {message_id: msgs[message_id]}
            if parent:
                msgs[parent] = {**leaf, "childrenIds": [*(leaf.get("childrenIds") or []), message_id]}
                changed[parent] = msgs[parent]
            history["currentId"] = message_id
            chat.current_message_id = message_id
            how, was_done = "new", False
        chat.chat = {**data, "history": history}
        flag_modified(chat, "chat")
        chat.updated_at = int(time.time())
        for mid, m in changed.items():
            await ChatMessages.upsert_message(mid, chat_id, user_id, m, db=db)
        return {"claimed": True, "how": how, "message_id": message_id, "parent": parent,
                "messages": changed, "was_done": was_done}


# ---------------------------------------------------------------------------
# Owner rule: who continues an interrupted run (DESIGN 2.7)
# ---------------------------------------------------------------------------
# The Gateway recovers only rows still `running`; the pipe resumes only rows
# already terminal with an outside kill, so a run never gets two continuations.

MAX_TURN_RESUMES = 3
# The row status a SIGKILL of the Gateway leaves on 2026.11.10, until the startup resume.
ROW_KILLED = "killed"
# The exit reaches the row only as text (pinned in
# test_fixture_openclaw_exit_errors.json): SIGTERM 143, SIGKILL / OOM 137.
_EXTERNAL_KILL_RE = re.compile(r"process exited with (?:code (?:143|137)\b|signal SIG[A-Z]+)")


def turn_owner(row: dict, stop_notice_before: bool = False) -> str:
    """`gateway`, `pipe` or `none` for an interrupted run's settled row.
    `stop_notice_before`: a stop-mode shutdown came first (never recovered)."""
    status = (row or {}).get("status")
    if status == "running":
        return "gateway"
    if status == "interrupted":
        # 2026.10: marked for restart recovery, not started yet.
        return "gateway"
    if status == "failed" and _EXTERNAL_KILL_RE.search(str(row.get("lastRunError") or "")):
        return "pipe"
    if status == "killed" and stop_notice_before:
        return "pipe"
    return "none"


def resumes_a_restart_run(version) -> bool:
    """True for a Gateway that continues the run a restart or crash cut by a startup resume input: its own
    run, with no user turn of its own in `chat.history` (the input is hidden), started within seconds of
    the boot (2026.11.10-beta.1+: "Root session left unfinished in a pending crash-recovery interval
    receives the same single resume input", docs/gateway/restart-recovery.md). Unknown versions count as older."""
    m = re.match(r"\s*(\d{4})\.(\d{1,2})\.(\d{1,3})", str(version or ""))
    return bool(m) and tuple(int(g) for g in m.groups()) >= RESTART_RESUME_FROM


RESTART_RESUME_FROM = (2026, 11, 10)


def resumes_a_crash(version) -> bool:
    """True for a Gateway whose startup recovery continues a run its crash cut as a run with a user turn naming
    it (rule E): 2026.11.3 and older. From 2026.11.5 only a graceful restart writes a resume marker, so a
    crash leaves an idle session with no status and nothing coming (docs/gateway/restart-recovery.md); from
    2026.11.10 a crash is resumed again, but by a hidden input (`resumes_a_restart_run`, rule F) and only for a
    run the boot ledger records, so the message waits a grace for it. Unknown versions count as older."""
    return not hides_cron_run_output(version)


def next_continuation(row: dict, stop_notice_before: bool, resumes_so_far: int,
                      after_restart: bool = False, restart_resume: bool = False) -> str:
    """`gateway`, `wait`, `resume` or `finalize` for M's writer when its run settles.
    A resume into a busy session fails after a restart recovery (M3-crash
    04351f67), so it is never sent then. MAX_TURN_RESUMES stops an OOM loop.
    `after_restart`: the idle no-status row is not a settle yet. `restart_resume` (2026.11.10+, rule F): nor is
    the `killed` row a crash leaves, until the Gateway's startup resume or the grace has said otherwise."""
    if after_restart and not (row or {}).get("hasActiveRun") and (row or {}).get("status") is None:
        return "gateway"
    owner = turn_owner(row, stop_notice_before)
    if owner == "gateway":
        return "gateway"
    if (after_restart and restart_resume and not (row or {}).get("hasActiveRun")
            and (row or {}).get("status") == ROW_KILLED):
        return "gateway"
    if (row or {}).get("hasActiveRun"):
        return "wait"
    if owner == "pipe" and resumes_so_far < MAX_TURN_RESUMES:
        return "resume"
    return "finalize"


# ---------------------------------------------------------------------------
# Rule E and the active run, from the transcript (DESIGN 2.1, case 5)
# ---------------------------------------------------------------------------

RECOVERY_SOURCE_TOOL = "main_session_restart_recovery"


def _history_user_turns(messages: list) -> list:
    """[(run_id, provenance sourceTool)] of the admitted user turns, in order."""
    out = []
    for m in messages or []:
        run = _history_user_run_id(m)
        if run:
            out.append((run, (m.get("provenance") or {}).get("sourceTool")))
    return out


def recovery_source_run(messages: list, run_id: str) -> str | None:
    """Rule E: the run a restart-recovery run recovers, the newest non-recovery
    turn before it (main-session-restart-recovery-CPJjd8da.mjs:441). Else None."""
    turns = _history_user_turns(messages)
    for i, (run, tool) in enumerate(turns):
        if run == run_id:
            if tool != RECOVERY_SOURCE_TOOL:
                return None
            return next((r for r, t in reversed(turns[:i]) if t != RECOVERY_SOURCE_TOOL), None)
    return None


def resumed_source_run(messages: list, run_id: str, open_runs) -> str | None:
    """Rule F (2026.11.10+): the run a startup resume continues. The resume input is hidden from `chat.history`
    and the run is its own, so `run_id` has no user turn there; it continues the run whose turn is the newest
    admitted one, if a message of that run was kept open for the Gateway's recovery (`open_runs`).
    Anything newer from the user, or a run with a turn of its own, is not a resume. Else None."""
    turns = _history_user_turns(messages)
    if not turns or any(run == run_id for run, _tool in turns):
        return None
    newest = turns[-1][0]
    return newest if newest in open_runs else None


def run_message_id(session_key: str, chat_id: str, run_id: str, messages: list | None = None,
                   open_runs=()) -> str | None:
    """The message `run_id` writes into: its source run's for a recovery (rule E) or a startup resume of a
    run whose message is still open (rule F, `open_runs`)."""
    source = (recovery_source_run(messages, run_id) or resumed_source_run(messages, run_id, open_runs)
              if messages else None)
    return turn_message_id(session_key, source or run_id, chat_id)


# ---------------------------------------------------------------------------
# Cron wakes: one run id for every wake into a session (ELI-103)
# ---------------------------------------------------------------------------
# The cron executor runs a wake with runId = the session's sessionId
# (run-executor.runtime-ro-4Z0fM.mjs:499), so the wake is found by its own
# `[cron:<jobId> <name>]` user turn: the newest committed at or before the
# event's `ts`. Its closing `cli-assistant:<sessionId>` message is deduplicated
# by key, so only the first wake gets one: never end a wake from it.

# 2026.10.2+ marks the wake turn by cron provenance instead and runs it under
# its own run id: an ordinary agent turn.

# 2026.11.5 hides the whole run: sourceTool "cron_execution"
# (session-event-handoff-Ct6SCwLg.mjs:376), no assistant stream
# (embedded-agent-subscribe-EcG91LL0.mjs:816), prompt and reply left out of
# chat.history (chat-display-projection.history-m8O6y-mh.mjs:242, 251). Its
# reply comes as an `automation-result` message after the run
# (conversation-result-CfQov6Jy.mjs:6-24).

_CRON_PROMPT_RE = re.compile(r"^\[cron:(?P<job>[^\s\]]+)[\s\]]")
_CRON_SOURCE_TOOLS = ("cron", "cron_execution")
_CRON_PROVENANCE_KIND = "internal_system"
_AUTOMATION_KIND_CRON = "cron"


def hides_cron_run_output(version) -> bool:
    """True for a 2026.11.5+ Gateway, which shows a cron run into a session
    only by its `automation-result` message. Unknown versions count as older."""
    m = re.match(r"\s*(\d{4})\.(\d{1,2})\.(\d{1,3})", str(version or ""))
    return bool(m) and tuple(int(g) for g in m.groups()) >= (2026, 11, 5)


def cron_execution_id(job: str, start_ms) -> str:
    """The Gateway's id of one cron job run (run-id-Be9gQ-r9.mjs createCronExecutionId)."""
    return f"cron:{job}:{int(start_ms)}"


_CRON_EXECUTION_ID_RE = re.compile(r"^cron:(?P<job>.+):(?P<start>\d+)$")


def parse_cron_execution_id(execution_id) -> tuple[str, int] | None:
    """(job, start ms) of a `cron:<job>:<start>` id, else None."""
    m = _CRON_EXECUTION_ID_RE.match(str(execution_id or ""))
    return (m.group("job"), int(m.group("start"))) if m else None


def cron_result_execution(message) -> str | None:
    """The cron run id an `automation-result` message reports, else None."""
    auto = message.get("openclawAutomation") if isinstance(message, dict) else None
    if (isinstance(auto, dict) and auto.get("kind") == _AUTOMATION_KIND_CRON
            and message.get("role") == _ASSISTANT_ROLE and parse_cron_execution_id(auto.get("runId"))):
        return auto["runId"]
    return None


def cron_result_message(messages: list, execution_id: str) -> dict | None:
    """The `automation-result` message that cron run committed, else None."""
    for m in reversed(messages or []):
        if cron_result_execution(m) == execution_id:
            return m
    return None


def _cron_turn_job(m: dict) -> str | None:
    """The cron job a wake turn names (provenance, else prompt header), else None."""
    prov = m.get("provenance") if isinstance(m.get("provenance"), dict) else None
    if prov:
        if prov.get("kind") != _CRON_PROVENANCE_KIND or prov.get("sourceTool") not in _CRON_SOURCE_TOOLS:
            return None
        hit = _CRON_PROMPT_RE.match(str(prov.get("sourcePromptPrefix") or ""))
        if prov.get("jobId") or hit:
            return prov.get("jobId") or hit.group("job")
    hit = _CRON_PROMPT_RE.match(((_history_item(m) or {}).get("text") or "").lstrip())
    return hit.group("job") if hit else None


def wake_turns(messages: list) -> list:
    """[{key, at, job, shared_run}] of the cron wake turns, in order.
    `shared_run`: the wake runs under the session's id (9.3, ELI-103)."""
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        key = _history_user_run_id({**m, "role": "user"} if _is_forwarded_turn(m) else m)
        if not key or parse_pipe_run_id(key):
            continue
        job = _cron_turn_job(m)
        if not job:
            continue
        meta = m.get("__openclaw") if isinstance(m.get("__openclaw"), dict) else {}
        at = meta.get("recordTimestampMs") or m.get("timestamp")
        out.append({"key": key, "at": at if isinstance(at, (int, float)) else None,
                    "job": job, "shared_run": not m.get("provenance")})
    return out


def _shared_wake_turns(messages: list) -> list:
    """Wake turns running under the session's id: the only ones a sessionId event can belong to."""
    return [w for w in wake_turns(messages) if w["shared_run"]]


def wake_turn_at(messages: list, at_ms: float, bound=frozenset()) -> dict | None:
    """The wake whose event was emitted at `at_ms`, not already `bound`. None
    while its turn is not committed yet (embedded-agent-DNQn_PMM.mjs:5891)."""
    for turn in reversed(_shared_wake_turns(messages)):
        if turn["at"] is not None and turn["at"] <= at_ms:
            return None if turn["key"] in bound else turn
    return None


def wake_turn_of_run(messages: list, job: str, start_ms: float, bound=frozenset()) -> dict | None:
    """The wake turn job `job`'s run started at `start_ms` wrote, or None while
    not committed yet."""
    for turn in reversed(_shared_wake_turns(messages)):
        if turn["job"] == job and turn["at"] is not None and turn["at"] >= start_ms:
            return None if turn["key"] in bound else turn
    return None


def wake_span(messages: list, start_ms: float, end_ms: float) -> list:
    """A wake's assistant messages: those between its turn's commit and its
    terminal, minus user turns queued meanwhile."""
    out = []
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        ts = m.get("timestamp")
        if isinstance(ts, (int, float)) and start_ms <= ts <= end_ms:
            out.append(m)
    return out
