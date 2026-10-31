# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Plugin approvals
# ---------------------------------------------------------------------------
# A plugin's `before_tool_call` hook can answer `requireApproval`: the tool
# call waits for a person to allow or deny it. The Gateway registers a
# `plugin.approval.request` and offers it only to a connected client that can
# show one: scope operator.admin or operator.approvals, and the
# "plugin-approvals" cap in its connect frame (2026.10.6 dist:
# server-request-context-*.mjs `canDeliverApprovals`). With no such client the
# request ends at once and the tool call fails with "Plugin approval
# unavailable (no approval route)"; a webchat turn has no channel route to
# fall back on (approval-shared-*.mjs `hasApprovalTurnSourceRoute`).
#
# The connection that writes turns holds operator.admin and declares the cap
# (`_GatewayConnection._connect_caps`), so the Gateway broadcasts it
# `plugin.approval.requested` {id, request: {title, description, severity,
# toolName, pluginId, allowedDecisions?, sessionKey, ...}, expiresAtMs}.
# A request made in an Open WebUI chat is asked in Open WebUI's question card,
# where the ask_user tool's questions are asked (askuser.py `_ask_agent_card`),
# and the pick goes back as `plugin.approval.resolve {id, decision}`, decision
# one of the request's allowedDecisions (allow-once, allow-always, deny).
# `plugin.approval.resolved` (answered in the Control UI or with /approve,
# expired, or cancelled by Stop) takes the card off the screen.
#
# Only the owner approves. A request in a guest's chat (a user not in
# OWNER_USERS) is denied at once with a note in the reply, as a guest's secret
# request is cancelled: left pending, it would wait out the approval's timeout
# with the guest's reply on "Thinking", and an approval from the Control UI
# would run the tool for a guest. A request from a session that is not a chat
# (a sub-agent, a channel thread) gets no card here; the Control UI answers it,
# or its timeout does.

PLUGIN_APPROVAL_METHODS = ("plugin.approval.resolve",)
PLUGIN_APPROVAL_EVENTS = ("plugin.approval.requested", "plugin.approval.resolved")
# The card's options, in this order, for the decisions a request allows. Deny
# first: Open WebUI tags the card's first option "Recommended", and that
# belongs on the fail-closed answer, not on letting the tool run.
PLUGIN_APPROVAL_CHOICES = (("deny", "Deny"), ("allow-once", "Allow once"), ("allow-always", "Always allow"))
_PLUGIN_APPROVAL_QID = "approval"
GUEST_APPROVAL_NOTE = "_The agent asked to use `{tool}`. That needs the owner's approval; it was denied here._"
# approval id -> the task asking it
_PLUGIN_APPROVALS: dict = {}


def _plugin_approvals_offered(conn) -> bool:
    """The Gateway offers what an approval needs: its events and the resolve
    method (hello-ok features)."""
    methods = getattr(conn, "gateway_methods", None) or ()
    events = getattr(conn, "gateway_events", None) or ()
    return all(m in methods for m in PLUGIN_APPROVAL_METHODS) and all(e in events for e in PLUGIN_APPROVAL_EVENTS)


def _plugin_approval_decisions(request: dict) -> list[str]:
    """The decisions the request allows, deny always among them (the Gateway
    adds it too: plugin-approval-canonical-decisions-*.mjs)."""
    known = [d for d, _ in PLUGIN_APPROVAL_CHOICES]
    raw = request.get("allowedDecisions") if isinstance(request, dict) else None
    allowed = [d for d in known if isinstance(raw, list) and d in raw] or known
    return ["deny"] + [d for d in allowed if d != "deny"]


def _plugin_approval_payload(record) -> dict | None:
    """Open WebUI's question card for a `plugin.approval.requested` record,
    or None when it is not one."""
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
    labels = dict(PLUGIN_APPROVAL_CHOICES)
    options = [{"label": labels[d], "description": ""} for d in _plugin_approval_decisions(request)]
    question = {"id": _PLUGIN_APPROVAL_QID, "header": title, "question": text,
                "options": options, "allow_other": False}
    return {"type": "request:user_input",
            "data": {"questions": [question], "allow_other": False, "timeout_ms": None}}


def _plugin_approval_decision(response, allowed: list[str]) -> str:
    """The decision the card's answer names; anything else (Cancel, no
    answer) denies."""
    if isinstance(response, dict) and response.get("status") == "answered":
        answer = (response.get("answers") or {}).get(_PLUGIN_APPROVAL_QID)
        label = str((answer or {}).get("label") or "").strip() if isinstance(answer, dict) else ""
        for decision, shown in PLUGIN_APPROVAL_CHOICES:
            if shown == label and decision in allowed:
                return decision
    return "deny"


def plugin_approval_requested(conn, record) -> bool:
    """`plugin.approval.requested`: ask an approval requested in an Open WebUI
    chat. False for anything else."""
    if not isinstance(record, dict) or not record.get("id") or record["id"] in _PLUGIN_APPROVALS:
        return False
    if _plugin_approval_payload(record) is None or not _plugin_approvals_offered(conn):
        return False
    parsed = conn.parse_owui_session_key((record.get("request") or {}).get("sessionKey") or "")
    if not parsed:
        return False
    _PLUGIN_APPROVALS[record["id"]] = spawn_background(_answer_plugin_approval(conn, record, *parsed))
    return True


def plugin_approval_resolved(payload) -> None:
    """`plugin.approval.resolved`: answered elsewhere, expired or cancelled;
    the card's wait ends."""
    task = _PLUGIN_APPROVALS.pop((payload or {}).get("id"), None)
    if task is not None:
        task.cancel()


async def _resolve_plugin_approval(conn, approval_id: str, decision: str) -> str:
    try:
        await conn.send_request("plugin.approval.resolve", {"id": approval_id, "decision": decision},
                                timeout=30)
        return decision
    except Exception as ex:
        return f"not resolved ({type(ex).__name__}: {str(ex)[:120]})"


async def _deny_guest_plugin_approval(conn, record: dict, tool: str) -> None:
    """Deny an approval requested in a guest's chat and say so in the reply of
    the run that asked."""
    approval_id, request = record["id"], record.get("request") or {}
    # Out of the registry before the request: `plugin.approval.resolved`
    # follows the answer and would cancel this task before it says so.
    if _PLUGIN_APPROVALS.get(approval_id) is asyncio.current_task():
        del _PLUGIN_APPROVALS[approval_id]
    shown = await _resolve_plugin_approval(conn, approval_id, "deny")
    pipe_log(f"  plugin approval: {tool} in a chat of a user not in OWNER_USERS: {shown}")
    payload = {"sessionKey": request.get("sessionKey") or "", "stream": PIPE_NOTE_STREAM,
               "data": {"id": f"{_NOTE_ITEM_PREFIX}approval_{approval_id}",
                        "text": GUEST_APPROVAL_NOTE.format(tool=tool)}}
    if request.get("runId"):
        payload["runId"] = request["runId"]
    await conn.dispatch_run_event({"type": "event", "event": "agent", "payload": payload})


async def _answer_plugin_approval(conn, record: dict, user_id: str, chat_id: str) -> None:
    approval_id, request = record["id"], record.get("request") or {}
    key = request.get("sessionKey") or ""
    tool = str(request.get("toolName") or request.get("title") or "a tool")
    if not await _sender_is_owner(conn, key):
        await _deny_guest_plugin_approval(conn, record, tool)
        return
    deadline = (record.get("expiresAtMs") or 0) / 1000 or time.time() + 120
    payload = _plugin_approval_payload(record)
    turn = _SECRET_ASKERS.get(key)
    message_id = turn.owui_message_id if turn is not None else _open_writer_message_id(conn, chat_id, key)
    own_sid = (turn.__metadata__ or {}).get("session_id") if turn is not None else None
    if not message_id:
        # A card needs the reply it belongs to (askuser.py
        # `_answer_agent_question`). Left to the Control UI.
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
