# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built artifacts directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------
#
# Pipe only (ELI-96). OpenClaw's `secrets` tool, `action=request`, is a
# Gateway question: `question.request` with one question that has
# `isSecret: true` and a `secretStore` binding {name, kind: "secret",
# allowedHosts, reason}. The Gateway broadcasts `question.requested`; whoever
# answers sends `question.resolve {id, answers: {answers: {<qid>: [value]}}}`
# and the Gateway itself writes the value into the secret store, registers it
# for log redaction, and resolves the agent's question with ["stored"]
# (dist 2026.9.3, server-methods/question.ts). Cancel is
# `question.resolve {id, cancel: true}`, which the agent sees as
# "cancelled" (status no_answer); expiry is "No credential arrived".
#
# The card is Open WebUI's own input dialog with `type: "password"` (its
# dialog renders `<input type="password">` for that). It is sent with
# `sio.call` to the tab that has the chat open; the typed value comes back in
# the call's ack and goes from there straight into `question.resolve`. It is
# never yielded, emitted, persisted or logged: the chat, the DB, the
# transcript and the model only ever see "stored" / "cancelled".
#
# A tab that is not on the chat drops the call; the composer watcher sees
# tabs join and leave chats (`SESSION_POOL[sid]["chat_ids"]`) and
# `_secret_card_sync` shows the card again when one comes back.
# ---------------------------------------------------------------------------

# question id -> {"record", "question", "user_id", "chat_id", "conn", "tasks": {sid: task}}
_SECRET_QUESTIONS: dict = {}

SECRET_CARD_TITLE = "OpenClaw needs a secret"


def _secret_question_of(record) -> dict | None:
    """The store-bound secret question of a `question.requested` record."""
    if not isinstance(record, dict):
        return None
    questions = record.get("questions") or []
    if len(questions) != 1 or not isinstance(questions[0], dict):
        return None
    question = questions[0]
    store = question.get("secretStore")
    if not question.get("isSecret") or not isinstance(store, dict) or not store.get("name"):
        return None
    return question


def _secret_card_payload(question: dict) -> dict:
    """The dialog shown to the user. Built from the request's metadata only:
    it never carries a value field."""
    store = question["secretStore"]
    lines = [f"**{store['name']}**"]
    if store.get("reason"):
        lines.append(str(store["reason"]))
    hosts = store.get("allowedHosts") or []
    lines.append("Allowed hosts: " + (", ".join(f"`{h}`" for h in hosts) if hosts
                                       else "none (usable only as a config SecretRef)"))
    lines.append("The value goes straight to OpenClaw's secret store. "
                 "It is not saved in this chat.")
    return {"type": "input", "data": {
        "title": SECRET_CARD_TITLE,
        "message": "\n\n".join(lines),
        "placeholder": f"Paste {store['name']}",
        "type": "password",
    }}


def _secret_tabs_on_chat(user_id: str, chat_id: str) -> list:
    """Socket ids of this user's tabs that have `chat_id` open."""
    try:
        batches = _ComposerWatcher._session_pool_batches()
    except Exception:
        return []
    return [sid for batch in batches for sid, session in batch
            if isinstance(session, dict) and session.get("id") == user_id
            and chat_id in (session.get("chat_ids") or [])]


def _secret_card_track(conn, record) -> bool:
    """Start tracking a pending secret question asked in an OWUI chat and
    show it on every tab that has the chat open. False for anything else."""
    question = _secret_question_of(record)
    if question is None or record.get("status", "pending") != "pending":
        return False
    parsed = conn.parse_owui_session_key(record.get("sessionKey") or "")
    if not parsed or record.get("id") in _SECRET_QUESTIONS:
        return False
    user_id, chat_id = parsed
    _SECRET_QUESTIONS[record["id"]] = {"record": record, "question": question,
                                       "user_id": user_id, "chat_id": chat_id,
                                       "conn": conn, "tasks": {}}
    pipe_log(f"  secret card: {question['secretStore']['name']} requested in chat "
             f"{chat_id[:8]}... (question {record['id'][:8]}...)")
    _secret_card_sync([(sid, chat_id, user_id) for sid in _secret_tabs_on_chat(user_id, chat_id)])
    return True


def _secret_card_resolved(payload) -> None:
    """`question.resolved`: answered here or elsewhere, cancelled or expired."""
    entry = _SECRET_QUESTIONS.pop((payload or {}).get("id"), None)
    if entry is None:
        return
    for task in entry["tasks"].values():
        task.cancel()
    pipe_log(f"  secret card: question {payload['id'][:8]}... {payload.get('status')}")


def _secret_card_sync(live_pairs) -> None:
    """Show each pending secret on the tabs that have its chat open, and
    drop the show of a tab that left it (its dialog can no longer answer)."""
    live = {(sid, chat_id, user_id) for sid, chat_id, user_id in live_pairs}
    for qid, entry in list(_SECRET_QUESTIONS.items()):
        for sid, task in list(entry["tasks"].items()):
            if (sid, entry["chat_id"], entry["user_id"]) not in live:
                task.cancel()
                entry["tasks"].pop(sid, None)
        for sid, chat_id, user_id in live:
            if (chat_id == entry["chat_id"] and user_id == entry["user_id"]
                    and sid not in entry["tasks"]):
                entry["tasks"][sid] = spawn_background(_secret_card_show(qid, sid))


async def _secret_card_show(qid: str, sid: str) -> None:
    entry = _SECRET_QUESTIONS.get(qid)
    if entry is None:
        return
    record, question = entry["record"], entry["question"]
    name = question["secretStore"]["name"]
    # The question's own expiry bounds the wait; the Gateway resolves it then.
    remaining = max(1.0, (record.get("expiresAtMs") or 0) / 1000 - time.time())
    try:
        from open_webui.socket.main import sio
        chat = await _ComposerWatcher._load_chat(entry["chat_id"])
        history = (getattr(chat, "chat", None) or {}).get("history") or {}
        response = await sio.call("events", {
            "chat_id": entry["chat_id"], "message_id": history.get("currentId"),
            "data": _secret_card_payload(question),
        }, to=sid, timeout=remaining)
    except asyncio.CancelledError:
        raise
    except Exception as ex:
        pipe_log(f"  secret card: {name} not answered on tab {str(sid)[:8]}: {type(ex).__name__}")
        entry["tasks"].pop(sid, None)
        return
    if _SECRET_QUESTIONS.get(qid) is not entry:
        return  # resolved meanwhile; whatever came back is dropped unread
    value = response if isinstance(response, str) and response.strip() else None
    params = ({"id": qid, "answers": {"answers": {question["questionId"]: [value.strip()]}},
               "resolvedBy": "openwebui"} if value is not None
              else {"id": qid, "cancel": True, "resolvedBy": "openwebui"})
    value = response = None
    try:
        result = await entry["conn"].send_request("question.resolve", params, timeout=30)
        status = (result or {}).get("status", "?")
    except Exception as ex:
        status = f"failed ({type(ex).__name__})"
    params = None
    _SECRET_QUESTIONS.pop(qid, None)
    for other_sid, task in entry["tasks"].items():
        if other_sid != sid:
            task.cancel()
    shown = "stored" if status == "answered" else status
    pipe_log(f"  secret card: {name} {shown} from tab {str(sid)[:8]}")
    try:
        await sio.emit("events", {
            "chat_id": entry["chat_id"], "message_id": history.get("currentId"),
            "data": {"type": "notification", "data": {
                "type": "success" if shown == "stored" else "info",
                "content": f"{name}: {shown}"}},
        }, to=sid)
    except Exception:
        pass
