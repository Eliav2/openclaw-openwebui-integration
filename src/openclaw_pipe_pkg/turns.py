# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Turn identity (ELI-100, DESIGN-agent-turns.md 2.2)
# ---------------------------------------------------------------------------
# Every run on an Open WebUI session key renders as one assistant message, and
# which message is decided from the run id alone: any connection, deploy or
# restarted process gets the same answer without shared memory.
#
#   msg-<chat>-<M>-<suffix>   a pipe-started run: its message is M, the reply
#                             Open WebUI created for it (the user turn), or
#                             the reply a resume / ask-user answer continues
#   owui-action-<...>         the Status Action's own runs (/compact): none
#   anything else             an agent turn: uuid5(TURN_NS, "<key>:<run>")

TURN_NS = uuid.UUID("6f1c3e2a-9d4b-4c6e-8a1f-0e0a1b100100")
ACTION_RUN_PREFIX = "owui-action-"
_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_PIPE_RUN_RE = re.compile(rf"^msg-(?P<chat>{_UUID_RE})-(?P<message>{_UUID_RE})-(?P<rest>.+)$")


def pipe_run_id(chat_id: str, message_id: str, kind: str | None = None) -> str:
    """The run id for a pipe-started run that writes into Open WebUI message
    `message_id`: `msg-<chat>-<M>-<t>` for the reply itself, and
    `msg-<chat>-<M>-<kind>-<t>` for a run continuing it (`input`, `resume`).
    The trailing send time is what `_run_id_sent_at` reads."""
    return f"msg-{chat_id}-{message_id}-{kind + '-' if kind else ''}{time.time()}"


def parse_pipe_run_id(run_id: str) -> dict | None:
    """{chat, message, kind} of a pipe-started run id, else None. `kind` is
    `reply` for the reply's own run, or the continuation kind."""
    m = _PIPE_RUN_RE.match(run_id or "")
    if not m:
        return None
    head = m.group("rest").split("-", 1)[0]
    kind = head if head in ("input", "resume") else "reply"
    return {"chat": m.group("chat"), "message": m.group("message"), "kind": kind}


def turn_message_id(session_key: str, run_id: str, chat_id: str | None = None) -> str | None:
    """The Open WebUI message run `run_id` on `session_key` renders into, or
    None when it renders nothing (the Status Action's own runs). A pipe run
    id naming another chat than `chat_id` is treated as an agent turn: the
    message id is only trusted inside the chat it was minted for."""
    if not run_id or run_id.startswith(ACTION_RUN_PREFIX):
        return None
    parsed = parse_pipe_run_id(run_id)
    if parsed and (chat_id is None or parsed["chat"] == chat_id):
        return parsed["message"]
    return str(uuid.uuid5(TURN_NS, f"{session_key}:{run_id}"))


# ---------------------------------------------------------------------------
# Claim: one message per run, anchored at the tail (DESIGN 2.3)
# ---------------------------------------------------------------------------
# The claim lives in Open WebUI's DB under the chat's row lock, not in a
# process map, so an old deploy's connection, a new one and a restarted
# process all agree: whoever inserts M first owns it, and two different runs
# that start together chain off the tail each reads under the lock.

_TURN_WRITERS_ATTR = "_openclaw_turn_writers"


def _live_turn_writers() -> set:
    """Writer ids of the connections alive in this Open WebUI process. Kept on
    `open_webui.socket.main` so every load of this Function (a redeploy keeps
    the old module objects alive until they are reaped) shares one set."""
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


def _writer_may_take_over(existing: dict) -> bool:
    """A message another writer recorded can be taken only when that writer
    is gone (its connection retired or its process restarted) and the
    message is unfinished. A message with no recorded writer is Open WebUI's
    own stub for a user turn; only its own inline request hands it over
    (`takeover=True`, DESIGN 2.6)."""
    writer = (existing.get("meta") or {}).get("openclaw_writer")
    return bool(writer) and existing.get("done") is False and writer not in _live_turn_writers()


async def _claim_turn_message(chat_id: str, user_id: str, message_id: str, run_id: str,
                              writer_id: str, takeover: bool = False) -> dict:
    """Claim message `message_id` for run `run_id` in chat `chat_id`.

    Under the chat's row lock (`Chats._chat_transaction`): if the message
    exists, it is someone's already, unless `takeover` (a pipe run taking its
    own stub, DESIGN 2.6) or its recorded writer is gone. Another live
    writer's message is never taken. Otherwise it
    is inserted under the deepest leaf, which becomes its parent, and
    `currentId` moves to it. The `chat_message` row is mirrored as
    `insert_chat_turn` does.

    Returns {claimed, how: new|takeover|taken, message_id, parent, messages}:
    `messages` holds the changed nodes, for the `chat:messages` introduce."""
    from open_webui.models.chats import Chats
    from open_webui.models.chat_messages import ChatMessages
    from sqlalchemy.orm.attributes import flag_modified

    async with Chats._chat_transaction(chat_id) as (db, chat):
        data = chat.chat or {}
        history = data.get("history") or {"messages": {}, "currentId": None}
        msgs = history.setdefault("messages", {})
        existing = msgs.get(message_id)
        if existing is not None:
            holder = (existing.get("meta") or {}).get("openclaw_writer")
            held = bool(holder) and holder != writer_id and holder in _live_turn_writers()
            if held or not (takeover or _writer_may_take_over(existing)):
                return {"claimed": False, "how": "taken", "message_id": message_id,
                        "parent": existing.get("parentId"), "messages": {}}
            meta = {**(existing.get("meta") or {}), "openclaw_run": run_id,
                    "openclaw_writer": writer_id}
            msgs[message_id] = {**existing, "meta": meta}
            changed = {message_id: msgs[message_id]}
            how, parent = "takeover", existing.get("parentId")
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
            how = "new"
        chat.chat = {**data, "history": history}
        flag_modified(chat, "chat")
        chat.updated_at = int(time.time())
        for mid, m in changed.items():
            await ChatMessages.upsert_message(mid, chat_id, user_id, m, db=db)
        return {"claimed": True, "how": how, "message_id": message_id, "parent": parent,
                "messages": changed}


# ---------------------------------------------------------------------------
# Owner rule: who continues an interrupted run (DESIGN 2.7)
# ---------------------------------------------------------------------------
# Read from the run's PERSISTED session row: the settled push after its
# terminal (hasActiveRun false), or the first sessions.list after hello-ok.
# The Gateway recovers only rows still `running`; the pipe resumes only rows
# already terminal with an outside kill, so one run never gets two
# continuations.

MAX_TURN_RESUMES = 3
# The child's exit reaches the row only as this text (no structured field;
# dist cli-transport line pinned in test_fixture_openclaw_exit_errors.json,
# re-checked against the installed dist by ExitTextFixtureTest): SIGTERM
# (143) and SIGKILL / OOM (137) by exit code, or a raw signal.
_EXTERNAL_KILL_RE = re.compile(r"process exited with (?:code (?:143|137)\b|signal SIG[A-Z]+)")


def turn_owner(row: dict, stop_notice_before: bool = False) -> str:
    """`gateway`, `pipe` or `none` for an interrupted run's settled row.

    `stop_notice_before`: a `shutdown` event without `restartExpectedMs`
    reached this connection before the run's terminal (a stop-mode abort,
    which the Gateway never recovers)."""
    status = (row or {}).get("status")
    if status == "running":
        return "gateway"
    if status == "failed" and _EXTERNAL_KILL_RE.search(str(row.get("lastRunError") or "")):
        return "pipe"
    if status == "killed" and stop_notice_before:
        return "pipe"
    return "none"


def next_continuation(row: dict, stop_notice_before: bool, resumes_so_far: int) -> str:
    """What the writer of M does when its run settles: `gateway` (keep M open
    for the recovery run), `wait` (another run holds the session: send
    nothing, decide again on its settle), `resume` (send
    msg-<chat>-<M>-resume-<t>), or `finalize`. A resume sent into a busy
    session is queued as a follow-up, and after a restart recovery that
    follow-up fails (M3-crash 04351f67), so a session with an active run
    never gets one. The budget is counted on M, so a resume that is cut the
    same way again (an OOM loop) ends after MAX_TURN_RESUMES."""
    owner = turn_owner(row, stop_notice_before)
    if owner == "gateway":
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
    """Rule E: for a restart-recovery run (its `<id>:user` turn carries the
    `main_session_restart_recovery` provenance, dist
    main-session-restart-recovery-CPJjd8da.mjs:441, 452-456), the run it
    recovers: the newest non-recovery admitted turn before it, which is what
    the Gateway's restartRecoveryDeliverySourceRunId names
    (chat-send-handler-kZQfUabO.mjs:635-636, agent-command-C9QUtzmi.mjs:1019).
    None for any other run."""
    turns = _history_user_turns(messages)
    for i, (run, tool) in enumerate(turns):
        if run == run_id:
            if tool != RECOVERY_SOURCE_TOOL:
                return None
            return next((r for r, t in reversed(turns[:i]) if t != RECOVERY_SOURCE_TOOL), None)
    return None


def run_message_id(session_key: str, chat_id: str, run_id: str, messages: list | None = None) -> str | None:
    """The message run `run_id` writes into: its source run's for a restart
    recovery (rule E), else its own (turn_message_id)."""
    source = recovery_source_run(messages, run_id) if messages else None
    return turn_message_id(session_key, source or run_id, chat_id)
