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
