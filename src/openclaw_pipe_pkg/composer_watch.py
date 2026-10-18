# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built artifacts directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------
#
# Pipe only (the Action artifact does not include this fragment). Uses the
# snippet helpers from composer.py.


# ---------------------------------------------------------------------------
# (c) Composer watcher: push the snippet when a tab opens an OpenClaw chat.
#
# Open WebUI 0.12 tabs emit `events:chat` join/leave, and the server keeps the
# joined ids in SESSION_POOL[sid]["chat_ids"] (socket/main.py). There is no
# hook on the join itself, so this polls the pool. For each (sid, chat) pair,
# after a short settle delay, it reads the chat's head (`_composer_chat_head`:
# its owner and its branch's newest messages, never the whole chat), checks
# that the socket's own user owns it and that it is on one of this Pipe's
# models, and sends the snippet to that one socket (`to=sid`, never the user
# room) targeting the chat's latest user message.
#
# It is sent with `sio.call`, not `emit`: Chat.svelte acks an `execute` event
# only after actually running it, and drops it (no ack) while the tab has not
# loaded that chat's history yet. No ack means a retry a few seconds later, up
# to _COMPOSER_WATCH_ATTEMPTS times. Leaving and re-joining a chat re-arms the
# pair. The watcher idles while this Pipe function is disabled and stops when
# it is deleted. Every failure is logged once and swallowed: on an Open WebUI
# without `chat_ids` the loop simply finds nothing.
# ---------------------------------------------------------------------------

# Until the snippet lands, a toolbar Status click takes Open WebUI's slow path
# (it uploads the chat's messages and saves the whole chat), so a newly opened
# chat gets it within a second. A poll reads SESSION_POOL, in memory.
_COMPOSER_WATCH_INTERVAL_S = 0.5
_COMPOSER_WATCH_SETTLE_S = 0.5
_COMPOSER_WATCH_ACK_TIMEOUT_S = 4.0
_COMPOSER_WATCH_RETRY_S = 3.0
_COMPOSER_WATCH_ATTEMPTS = 3
_COMPOSER_WATCH_LIVENESS_S = 30.0

# Stashed on `open_webui.socket.main` (OWUI's own stable module, see
# `_STALE_CONN_ATTR` in gateway.py for why) so a redeploy can cancel the
# previous deploy's watcher task instead of leaving two running.
_COMPOSER_WATCH_ATTR = "_openclaw_composer_watcher_v1"

_composer_watcher_task = None


async def _composer_chat_head(user_id, current_id, message) -> dict:
    """What a push needs of a chat: {"user_id", "message_id", "model"}.
    `message(id)` reads one message as (role, parent_id, model_id), or None.
    `message_id` is the snippet's targetMessageId(): the latest user message
    on the current branch, else the current message. `model` is the newest
    assistant message's on the branch, the model the chat replies with."""
    target, model, message_id = None, None, current_id
    for _ in range(500):
        if not message_id:
            break
        row = await message(message_id)
        if row is None:
            break
        role, parent_id, model_id = row
        if role == "assistant" and model is None:
            model = model_id
        if role == "user" and target is None:
            target = message_id
        if target is not None and model is not None:
            break
        message_id = parent_id
    return {"user_id": user_id, "message_id": target or current_id, "model": model}


class _ComposerWatcher:
    def __init__(self, valves_getter, prefixes_getter, function_id=None):
        self.valves_getter = valves_getter
        self.prefixes_getter = prefixes_getter
        self.function_id = function_id
        # (sid, chat_id) -> {"next": monotonic time, "attempts": int, "done": bool}
        self._pairs: dict = {}

    def enabled(self) -> bool:
        try:
            return bool(getattr(self.valves_getter(), "COMPOSER_STATUS_BUTTON", True))
        except Exception:
            return False

    @staticmethod
    def _session_pool_batches():
        import open_webui.socket.main as _owui_socket_main
        getter = getattr(_owui_socket_main, "get_session_pool_batches", None)
        if getter is not None:
            return getter()
        pool = getattr(_owui_socket_main, "SESSION_POOL", None) or {}
        return [list(pool.items())]

    @staticmethod
    async def _chat_head(chat_id):
        """`_composer_chat_head` of a chat, from Open WebUI's DB: the chat
        row's owner and current message id, then one `chat_message` row per
        step up the branch. Open WebUI builds a whole chat in Python, about
        14x its JSON, for every read of the chat itself (ELI-115)."""
        from sqlalchemy import select
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.chats import Chat
        from open_webui.models.chat_messages import ChatMessage

        async with get_async_db_context() as db:
            row = (await db.execute(select(Chat.user_id, Chat.current_message_id)
                                    .where(Chat.id == chat_id))).first()
            if row is None:
                return None

            async def message(message_id):
                return (await db.execute(
                    select(ChatMessage.role, ChatMessage.parent_id, ChatMessage.model_id)
                    .where(ChatMessage.id == f"{chat_id}-{message_id}"))).first()
            return await _composer_chat_head(row[0], row[1], message)

    @staticmethod
    async def _call_sid(sid, payload) -> bool:
        """True when the tab acked, i.e. actually ran the snippet."""
        from open_webui.socket.main import sio
        try:
            await sio.call("events", payload, to=sid, timeout=_COMPOSER_WATCH_ACK_TIMEOUT_S)
            return True
        except Exception:
            return False

    async def function_state(self) -> str:
        """"active", "inactive", "gone" or "unknown" for this Pipe function."""
        if not self.function_id:
            return "unknown"
        try:
            from open_webui.models.functions import Functions
            fn = Functions.get_function_by_id(self.function_id)
            if asyncio.iscoroutine(fn):
                fn = await fn
        except Exception:
            return "unknown"
        if fn is None:
            return "gone"
        return "active" if getattr(fn, "is_active", True) else "inactive"

    async def tick(self, now=None):
        if not self.enabled():
            self._pairs.clear()
            return 0
        now = time.monotonic() if now is None else now
        live = set()
        due = []
        for batch in self._session_pool_batches():
            for sid, session in batch:
                if not isinstance(session, dict):
                    continue
                user_id = session.get("id")
                for chat_id in session.get("chat_ids") or []:
                    key = (sid, chat_id)
                    live.add(key)
                    st = self._pairs.get(key)
                    if st is None:
                        self._pairs[key] = {"next": now + _COMPOSER_WATCH_SETTLE_S,
                                            "attempts": 0, "done": False}
                        continue
                    if st["done"] or now < st["next"]:
                        continue
                    st["attempts"] += 1
                    due.append((key, user_id))
        for key in [k for k in self._pairs if k not in live]:
            del self._pairs[key]
        if not due:
            return 0

        results = await asyncio.gather(
            *(self._push(sid, chat_id, user_id) for (sid, chat_id), user_id in due),
            return_exceptions=True,
        )
        pushed = 0
        for ((sid, chat_id), _user_id), result in zip(due, results):
            st = self._pairs.get((sid, chat_id))
            if isinstance(result, BaseException):
                pipe_log(f"[composer] push to {str(sid)[:8]} failed: {result}")
                result = "retry"
            if result == "acked":
                pushed += 1
            if st is None:
                continue
            if result == "held":
                st["attempts"] -= 1
                continue
            if result == "retry" and st["attempts"] < _COMPOSER_WATCH_ATTEMPTS:
                st["next"] = now + _COMPOSER_WATCH_RETRY_S
            else:
                st["done"] = True
        return pushed

    async def _push(self, sid, chat_id, user_id) -> str:
        """"acked" (the tab ran it), "skip" (not ours to push), "retry", or
        "held": the tab shows an input dialog, which an `execute` would take
        the answer of (ELI-195), so the push waits for it to close."""
        if not user_id:
            return "skip"
        head = await self._chat_head(chat_id)
        if head is None or head["user_id"] != user_id:
            return "skip"
        prefixes = self.prefixes_getter()
        model = head["model"]
        if not (isinstance(model, str) and any(model.startswith(p) for p in prefixes)):
            return "skip"
        message_id = head["message_id"]
        if not message_id:
            return "skip"
        # The Stop / Send now intent hook and the auto-send hook ride along: a
        # tab that opens the chat after a turn writer started must still
        # answer its cancel query (ELI-100 case 8) and send what it queues.
        code = (_page_hooks_js(chat_id, _auto_send_on(self.valves_getter()))
                + _render_composer_button_js(session_id=sid, model_prefixes=prefixes, chat_id=chat_id,
                                             chat_model=model))
        if _dialog_open_on(sid):
            return "held"
        acked = await self._call_sid(sid, {
            "chat_id": chat_id,
            "message_id": message_id,
            "data": {"type": "execute", "data": {"code": code}},
        })
        return "acked" if acked else "retry"

    async def run(self):
        last_error = None
        next_liveness = 0.0
        state = "unknown"
        while True:
            try:
                now = time.monotonic()
                if now >= next_liveness:
                    next_liveness = now + _COMPOSER_WATCH_LIVENESS_S
                    state = await self.function_state()
                    if state == "gone":
                        pipe_log("[composer] Pipe function deleted; watcher stops")
                        return
                if state != "inactive":
                    await self.tick()
                last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                # Logged once per distinct failure, not every interval.
                if str(ex) != last_error:
                    last_error = str(ex)
                    pipe_log(f"[composer] watcher tick failed: {ex}")
            await asyncio.sleep(_COMPOSER_WATCH_INTERVAL_S)


_composer_known_prefixes = [_COMPOSER_DEFAULT_MODEL_PREFIX]


def _composer_note_model(model_id=None) -> list:
    """Remember the Pipe's own function id and the prefix of a model it
    actually served, so the button also recognises chats when the Pipe is
    installed under a non-default id. Returns the known prefixes. Pipe only:
    in the Action artifact the module name is the Action's id."""
    for p in _composer_model_prefixes(model_id, own_id=_composer_own_function_id()):
        if p not in _composer_known_prefixes:
            _composer_known_prefixes.append(p)
    return list(_composer_known_prefixes)


def _ensure_composer_watcher(valves_getter):
    """Start the watcher once per deploy, inside Open WebUI only. Cancels a
    previous deploy's watcher found on the stable socket module. Returns the
    running task, or None when not inside OWUI, disabled, or on failure."""
    global _composer_watcher_task
    try:
        if not bool(getattr(valves_getter(), "COMPOSER_STATUS_BUTTON", True)):
            return None
        if _composer_watcher_task is not None and not _composer_watcher_task.done():
            return _composer_watcher_task
        try:
            import open_webui.socket.main as _owui_socket_main
        except Exception:
            return None
        _composer_note_model()
        stale = getattr(_owui_socket_main, _COMPOSER_WATCH_ATTR, None)
        if stale is not None and stale is not _composer_watcher_task:
            try:
                stale.cancel()
            except Exception:
                pass
        watcher = _ComposerWatcher(valves_getter, lambda: list(_composer_known_prefixes),
                                   function_id=_composer_own_function_id())
        _composer_watcher_task = asyncio.get_running_loop().create_task(watcher.run())
        setattr(_owui_socket_main, _COMPOSER_WATCH_ATTR, _composer_watcher_task)
        return _composer_watcher_task
    except Exception as ex:
        pipe_log(f"[composer] could not start the watcher: {ex}")
        return None
