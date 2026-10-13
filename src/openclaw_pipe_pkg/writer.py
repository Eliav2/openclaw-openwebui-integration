# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The turn writer (ELI-100, DESIGN-agent-turns.md 2.3-2.5)
# ---------------------------------------------------------------------------
# One writer per run this connection renders outside a live `pipe()` request:
# agent turns (cron wake, sessions_send, sub-agent announce, restart recovery)
# and user turns whose request ended before the run. It owns exactly one
# message, the one turn identity names, and registers itself with Open
# WebUI's task registry so the page treats the chat as generating and its
# Stop button reaches it.


async def _set_turn_meta(chat_id: str, message_id: str, meta: dict) -> bool:
    """Merge `meta` into message `message_id` under the chat's row lock."""
    from open_webui.models.chats import Chats
    from sqlalchemy.orm.attributes import flag_modified

    async with Chats._chat_transaction(chat_id) as (db, chat):
        data = chat.chat or {}
        history = data.get("history") or {}
        msg = (history.get("messages") or {}).get(message_id)
        if msg is None:
            return False
        msg["meta"] = {**(msg.get("meta") or {}), **meta}
        chat.chat = {**data, "history": history}
        flag_modified(chat, "chat")
        return True


async def _introduce_turn_message(chat_id: str, user_id: str, messages: dict, current_id: str) -> None:
    """Show a newly claimed message in every open tab of the chat without a
    reload: the shared `chat:messages` event to room `chat:<id>`, which the
    page merges and moves its view to (Open WebUI emits the same for its own
    turns; measured live as M1)."""
    from open_webui.socket.main import sio

    await sio.emit("events", {
        "chat_id": chat_id, "message_id": current_id, "user_id": user_id, "shared": True,
        "data": {"type": "chat:messages", "data": {"messages": messages, "currentId": current_id}},
    }, room=f"chat:{chat_id}")


def _owui_redis():
    """Open WebUI's redis client, or None in a single-process install
    (`create_task` handles both)."""
    app = getattr(sys.modules.get("open_webui.main"), "app", None)
    return getattr(getattr(app, "state", None), "redis", None)


class _TurnWriter:
    """Writes run `run_id` into message `message_id` of chat `chat_id`."""

    def __init__(self, conn: "_GatewayConnection", *, session_key: str, chat_id: str,
                 user_id: str, run_id: str, message_id: str | None):
        self.conn = conn
        self.session_key, self.chat_id, self.user_id = session_key, chat_id, user_id
        self.run_id, self.message_id = run_id, message_id
        self.renderer = _TurnRenderer()
        self.opened = False
        self.task_id: str | None = None
        self.parent_id: str | None = None
        self._done = asyncio.Event()

    async def open(self, takeover: bool = False) -> bool:
        """Claim the message, introduce it to open tabs and register the
        writer's task. False when there is nothing to open (a run that renders
        nothing) or another live writer holds the message."""
        if self.opened:
            return True
        if not self.message_id:
            return False
        _register_turn_writer(self.conn.writer_id)
        claim = await _claim_turn_message(self.chat_id, self.user_id, self.message_id,
                                          self.run_id, self.conn.writer_id, takeover=takeover)
        if not claim["claimed"]:
            pipe_log(f"  turn writer: message {self.message_id[:8]}... is held by another writer")
            return False
        self.parent_id = claim["parent"]
        if claim["how"] == "new":
            await _introduce_turn_message(self.chat_id, self.user_id, claim["messages"],
                                          self.message_id)
        from open_webui.tasks import create_task

        self.task_id, _ = await create_task(_owui_redis(), self._hold(), id=self.chat_id)
        await _set_turn_meta(self.chat_id, self.message_id, {"task_id": self.task_id})
        self.opened = True
        pipe_log(f"  turn writer: opened {self.message_id[:8]}... ({claim['how']}) for run "
                 f"{self.run_id[:24]}... in chat {self.chat_id[:8]}...")
        return True

    async def _hold(self) -> None:
        """The Open WebUI task: alive exactly while this writer owns the run."""
        await self._done.wait()
