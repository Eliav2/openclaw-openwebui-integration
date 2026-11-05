# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# Plugin approvals: a `before_tool_call` hook answering `requireApproval`.
# The Gateway offers `plugin.approval.request` only to a client with
# operator.admin/approvals scope and the "plugin-approvals" cap
# (`canDeliverApprovals`); without one, a webchat turn's tool call fails with
# "no approval route". The owner answers in the ask_user question card.
# Only sessions this copy owns (by AGENT_ID) are handled. Guest chats and
# channel threads are denied at once, so the turn does not stall until the
# timeout (up to 10 min). Sub-agent sessions are left to the Control UI.

PLUGIN_APPROVAL_METHODS = ("plugin.approval.resolve",)
PLUGIN_APPROVAL_EVENTS = ("plugin.approval.requested", "plugin.approval.resolved")
# Deny first: OWUI tags the first option "Recommended".
PLUGIN_APPROVAL_CHOICES = (("deny", "Deny"), ("allow-once", "Allow once"), ("allow-always", "Always allow"))
_PLUGIN_APPROVAL_QID = "approval"
GUEST_APPROVAL_NOTE = "_The agent asked to use `{tool}`. That needs the owner's approval; it was denied here._"
_PLUGIN_APPROVAL_DETAIL_CHARS = 600
# approval id -> the task asking it
_PLUGIN_APPROVALS: dict = {}


def _plugin_approvals_offered(conn) -> bool:
    """Whether hello-ok lists the approval events and resolve method."""
    methods = getattr(conn, "gateway_methods", None) or ()
    events = getattr(conn, "gateway_events", None) or ()
    return all(m in methods for m in PLUGIN_APPROVAL_METHODS) and all(e in events for e in PLUGIN_APPROVAL_EVENTS)


def _plugin_approval_decisions(request: dict) -> list[str]:
    """Allowed decisions, deny always first (the Gateway adds deny too)."""
    known = [d for d, _ in PLUGIN_APPROVAL_CHOICES]
    raw = request.get("allowedDecisions") if isinstance(request, dict) else None
    allowed = [d for d in known if isinstance(raw, list) and d in raw] or known
    return ["deny"] + [d for d in allowed if d != "deny"]


def _plugin_approval_payload(record) -> dict | None:
    """OWUI question card for a `plugin.approval.requested` record, or None."""
    request = record.get("request") if isinstance(record, dict) else None
    if not isinstance(request, dict):
        return None
    title = str(request.get("title") or "").strip() or "Approval needed"
    text = str(request.get("description") or "").strip() or "Allow this action?"
    facts = []
    if request.get("toolName"):
        facts.append(f"Tool: {request['toolName']}")
    if request.get("pluginId"):
        facts.append(f"Plugin: {request['pluginId']}")
    if request.get("severity") == "critical":
        facts.append("Severity: critical")
    if facts:
        text = f"{text} ({', '.join(facts)})"
    detail = str(request.get("detail") or "").strip()
    if detail:
        text = f"{text}\n\n{detail[:_PLUGIN_APPROVAL_DETAIL_CHARS]}"
    labels = dict(PLUGIN_APPROVAL_CHOICES)
    options = [{"label": labels[d], "description": ""} for d in _plugin_approval_decisions(request)]
    question = {"id": _PLUGIN_APPROVAL_QID, "header": title, "question": text,
                "options": options, "allow_other": False}
    return {"type": "request:user_input",
            "data": {"questions": [question], "allow_other": False, "timeout_ms": None}}


def _plugin_approval_decision(response, allowed: list[str]) -> str:
    """The decision the card's answer names; anything else denies."""
    if isinstance(response, dict) and response.get("status") == "answered":
        answer = (response.get("answers") or {}).get(_PLUGIN_APPROVAL_QID)
        label = str((answer or {}).get("label") or "").strip() if isinstance(answer, dict) else ""
        for decision, shown in PLUGIN_APPROVAL_CHOICES:
            if shown == label and decision in allowed:
                return decision
    return "deny"


def _plugin_approval_session(conn, key: str) -> tuple | None:
    """("chat", user id, chat id), ("channel",), or None if not this copy's."""
    parsed = conn.parse_owui_session_key(key or "")   # matches AGENT_ID
    if parsed:
        return ("chat", *parsed)
    agent_id = getattr(conn._valves(), "AGENT_ID", None)
    m = _CHANNEL_KEY_RE.match(key or "")
    if agent_id and m and m.group(1) == agent_id:
        return ("channel",)
    return None


def plugin_approval_requested(conn, record) -> bool:
    """Ask or deny an approval in a session this copy owns; False otherwise."""
    if not isinstance(record, dict) or not record.get("id") or record["id"] in _PLUGIN_APPROVALS:
        return False
    if _plugin_approval_payload(record) is None or not _plugin_approvals_offered(conn):
        return False
    session = _plugin_approval_session(conn, (record.get("request") or {}).get("sessionKey") or "")
    if not session:
        return False
    _PLUGIN_APPROVALS[record["id"]] = spawn_background(_answer_plugin_approval(conn, record, *session))
    return True


def plugin_approval_resolved(payload) -> None:
    """Answered elsewhere, expired or cancelled: end the card's wait."""
    task = _PLUGIN_APPROVALS.pop((payload or {}).get("id"), None)
    if task is not None:
        pipe_log(f"  plugin approval: resolved elsewhere ({(payload or {}).get('decision')} by "
                 f"{(payload or {}).get('resolvedBy')})")
        task.cancel()


async def _resolve_plugin_approval(conn, approval_id: str, decision: str) -> str:
    try:
        await conn.send_request("plugin.approval.resolve", {"id": approval_id, "decision": decision},
                                timeout=30)
        return decision
    except Exception as ex:
        return f"not resolved ({type(ex).__name__}: {str(ex)[:120]})"


async def _deny_plugin_approval(conn, record: dict, tool: str, why: str, note: str | None) -> None:
    """Deny at once; with `note`, say so in the asking run's reply."""
    approval_id, request = record["id"], record.get("request") or {}
    # Unregister first: `plugin.approval.resolved` would cancel this task.
    if _PLUGIN_APPROVALS.get(approval_id) is asyncio.current_task():
        del _PLUGIN_APPROVALS[approval_id]
    shown = await _resolve_plugin_approval(conn, approval_id, "deny")
    pipe_log(f"  plugin approval: {tool} in {why}: {shown}")
    if note is None:
        return
    payload = {"sessionKey": request.get("sessionKey") or "", "stream": PIPE_NOTE_STREAM,
               "data": {"id": f"{_NOTE_ITEM_PREFIX}approval_{approval_id}", "text": note.format(tool=tool)}}
    if request.get("runId"):
        payload["runId"] = request["runId"]
    await conn.dispatch_run_event({"type": "event", "event": "agent", "payload": payload})


async def _answer_plugin_approval(conn, record: dict, kind: str, user_id: str = "", chat_id: str = "") -> None:
    approval_id, request = record["id"], record.get("request") or {}
    key = request.get("sessionKey") or ""
    tool = str(request.get("toolName") or request.get("title") or "a tool")
    if kind == "channel":
        await _deny_plugin_approval(conn, record, tool, "a channel thread (no card there)", None)
        return
    if not await _sender_is_owner(conn, key):
        await _deny_plugin_approval(conn, record, tool, "a chat of a user not in OWNER_USERS",
                                    GUEST_APPROVAL_NOTE)
        return
    deadline = (record.get("expiresAtMs") or 0) / 1000 or time.time() + 120
    payload = _plugin_approval_payload(record)
    turn = _SECRET_ASKERS.get(key)
    message_id = turn.owui_message_id if turn is not None else _open_writer_message_id(conn, chat_id, key)
    own_sid = (turn.__metadata__ or {}).get("session_id") if turn is not None else None
    if not message_id:
        # A card needs its reply message. Left to the Control UI.
        _PLUGIN_APPROVALS.pop(approval_id, None)
        pipe_log(f"  plugin approval: no reply in chat {chat_id[:8]}... streams the run; left to the Control UI")
        return
    pipe_log(f"  plugin approval: {tool} in chat {chat_id[:8]}... (approval {approval_id[:16]}...)")
    response = await _ask_agent_card(user_id, chat_id, message_id, own_sid, payload, deadline)
    if _PLUGIN_APPROVALS.get(approval_id) is not asyncio.current_task():
        return
    del _PLUGIN_APPROVALS[approval_id]
    if response is None:
        # The Gateway's timeout ends it and tells the agent.
        pipe_log("  plugin approval: not answered before it expired")
        return
    decision = _plugin_approval_decision(response, _plugin_approval_decisions(request))
    pipe_log(f"  plugin approval: {await _resolve_plugin_approval(conn, approval_id, decision)}")
