"""A plugin approval (a tool call a plugin's `before_tool_call` hook holds
with `requireApproval`) is asked in Open WebUI's question card, and the pick
goes to `plugin.approval.resolve`. The Gateway offers the request only to a
connection that declares the "plugin-approvals" cap; without one the tool call
failed at once with "Plugin approval unavailable (no approval route)".

Run: uv run --with-requirements requirements-dev.txt pytest -q tests/test_plugin_approval_unit.py
"""
import asyncio
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402
import openclaw_status_action as oca  # noqa: E402
from test_ask_user_tool_unit import KEY, FakeConn, FakeTurn, Tabs  # noqa: E402


def record(aid="plugin:1", allowed=None, key=KEY):
    """What OpenClaw 2026.10.6 broadcasts as `plugin.approval.requested`
    (approval-shared-*.mjs `buildRequestedApprovalEvent`)."""
    request = {"pluginId": "gmail", "title": "Apply Gmail triage", "description": "Archive 12 messages?",
               "severity": "warning", "toolName": "gmail_apply", "toolCallId": "call-1",
               "agentId": "main", "sessionKey": key, "runId": "run-1"}
    if allowed is not None:
        request["allowedDecisions"] = allowed
    return {"approvalKind": "plugin", "id": aid, "request": request,
            "createdAtMs": int(ocp.time.time() * 1000),
            "expiresAtMs": int((ocp.time.time() + 120) * 1000)}


class ApprovalConn(FakeConn):
    def __init__(self):
        super().__init__()
        self.gateway_version = "2026.10.6"
        self.gateway_methods = frozenset({"plugin.approval.resolve", "question.resolve"})
        self.gateway_events = frozenset({"plugin.approval.requested", "plugin.approval.resolved"})


def picked(option):
    return {"status": "answered", "answers": {"approval": {"type": "option", "label": option}}}


def approvals() -> dict:
    """The Pipe's approval id -> asking task map (none before #253)."""
    return getattr(ocp, "_PLUGIN_APPROVALS", {})


def hooks():
    hooks = getattr(ocp, "_APPROVAL_HOOKS", None)
    assert hooks, "the Pipe has no plugin approval handlers"
    return hooks


class ConnectCapsTests(unittest.TestCase):
    def caps(self, module, writes_turns=True, scopes=None):
        conn = types.SimpleNamespace(writes_turns=writes_turns,
                                     scopes=scopes if scopes is not None else module.GATEWAY_SCOPES)
        return module._GatewayConnection._connect_caps(conn)

    def test_the_pipes_connection_says_it_shows_plugin_approvals(self):
        """canDeliverApprovals (server-request-context-*.mjs): operator.admin
        and the "plugin-approvals" cap make a client an approval route."""
        self.assertIn("plugin-approvals", self.caps(ocp))
        self.assertIn("tool-events", self.caps(ocp))

    def test_the_guest_connection_does_not(self):
        self.assertEqual(self.caps(ocp, scopes=ocp.RESTRICTED_SCOPES), ["tool-events"])

    def test_a_connection_that_writes_no_turns_does_not(self):
        """The Status Action's connection asks nothing: as an approval route
        it would only make an approval wait out its timeout."""
        self.assertEqual(self.caps(ocp, writes_turns=False), ["tool-events"])
        self.assertEqual(self.caps(oca), ["tool-events"])


class PluginApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for patch in (mock.patch.object(ocp, "pipe_log", lambda *a, **k: None),
                      mock.patch.object(ocp, "_sender_is_owner", mock.AsyncMock(return_value=True))):
            patch.start()
            self.addCleanup(patch.stop)
        for registry in (approvals(), ocp._SECRET_ASKERS, getattr(ocp, "_READY_CHAT_TABS", {})):
            registry.clear()
            self.addCleanup(registry.clear)
        tracked = ocp._CHAT_TABS_TRACKED
        tracked[0] = True
        self.addCleanup(tracked.__setitem__, 0, False)

    def ask(self, conn, rec=None, turn=None):
        if turn is not None:
            ocp._secret_asker_join(turn)
        return hooks()[0](conn, rec or record())

    async def finished(self, aid="plugin:1"):
        task = approvals().get(aid)
        if task is not None:
            await asyncio.wait_for(task, 2)   # bounds the test only

    async def test_the_card_names_the_action_and_allow_once_resolves_it(self):
        conn, tabs = ApprovalConn(), Tabs(self)
        tabs.open("sid-own")
        self.assertTrue(self.ask(conn, turn=FakeTurn()))
        sent = await tabs.card_on("sid-own")
        self.assertEqual((sent["chat_id"], sent["message_id"]), ("chat-1", "msg-1"))
        card = sent["data"]
        self.assertEqual(card["type"], "request:user_input")
        self.assertIsNone(card["data"]["timeout_ms"], "a card countdown would read as Cancel")
        q = card["data"]["questions"][0]
        self.assertEqual(q["header"], "Apply Gmail triage")
        self.assertEqual(q["question"], "Archive 12 messages? (Tool: gmail_apply, Plugin: gmail)")
        self.assertEqual([o["label"] for o in q["options"]], ["Deny", "Allow once", "Always allow"],
                         "Open WebUI tags the first option Recommended: Deny, not an allow")
        self.assertFalse(q["allow_other"], "a typed answer is not a decision")
        tabs.answer("sid-own", picked("Allow once"))
        await self.finished()
        self.assertEqual(conn.sent, [("plugin.approval.resolve", {"id": "plugin:1", "decision": "allow-once"})])
        self.assertTrue(tabs.removed_from("sid-own"))

    async def test_the_card_offers_only_the_decisions_the_request_allows(self):
        conn, tabs = ApprovalConn(), Tabs(self)
        tabs.open("sid-own")
        self.ask(conn, rec=record(allowed=["allow-once"]), turn=FakeTurn())
        q = (await tabs.card_on("sid-own"))["data"]["data"]["questions"][0]
        self.assertEqual([o["label"] for o in q["options"]], ["Deny", "Allow once"])
        tabs.answer("sid-own", picked("Always allow"))   # not offered: no such decision
        await self.finished()
        self.assertEqual(conn.sent[0][1]["decision"], "deny")

    async def test_deny_and_cancel_both_deny(self):
        for answer in (picked("Deny"), {"status": "cancelled", "answers": {}}):
            conn, tabs = ApprovalConn(), Tabs(self)
            tabs.open("sid-own")
            self.ask(conn, turn=FakeTurn())
            await tabs.card_on("sid-own")
            tabs.answer("sid-own", answer)
            await self.finished()
            self.assertEqual(conn.sent, [("plugin.approval.resolve", {"id": "plugin:1", "decision": "deny"})])

    async def test_resolved_elsewhere_takes_the_card_away_and_sends_nothing(self):
        """Answered in the Control UI, expired, or cancelled by Stop: the
        Gateway broadcasts `plugin.approval.resolved`."""
        conn, tabs = ApprovalConn(), Tabs(self)
        tabs.open("sid-own")
        self.ask(conn, turn=FakeTurn())
        await tabs.card_on("sid-own")
        task = approvals()["plugin:1"]
        hooks()[1]({"id": "plugin:1", "decision": "allow-once", "resolvedBy": "control-ui"})
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(conn.sent, [])
        self.assertTrue(tabs.removed_from("sid-own"))

    async def test_a_guests_chat_gets_no_card_the_request_is_denied_and_the_reply_says_so(self):
        conn, tabs = ApprovalConn(), Tabs(self)
        tabs.open("sid-own")
        with mock.patch.object(ocp, "_sender_is_owner", mock.AsyncMock(return_value=False)):
            self.ask(conn, turn=FakeTurn())
            await self.finished()
        self.assertEqual(tabs.sio.calls, [])
        self.assertEqual(conn.sent, [("plugin.approval.resolve", {"id": "plugin:1", "decision": "deny"})])
        note = conn.dispatched[0]["payload"]
        self.assertEqual((note["sessionKey"], note["runId"]), (KEY, "run-1"))
        self.assertIn("needs the owner's approval", note["data"]["text"])

    def test_requests_not_from_a_chat_are_left_alone(self):
        """A sub-agent's or a channel thread's session has no chat to ask in."""
        conn = ApprovalConn()
        for key in ("agent:main:subagent:abc", "agent:main:openwebui:channel:c1:thread:m1", ""):
            self.assertFalse(self.ask(conn, rec=record(key=key)))
        self.assertEqual(approvals(), {})

    def test_a_gateway_without_the_approval_protocol_is_not_asked(self):
        conn = ApprovalConn()
        conn.gateway_methods = frozenset()
        self.assertFalse(self.ask(conn))

    async def test_the_same_request_is_asked_once(self):
        conn = ApprovalConn()
        self.assertTrue(self.ask(conn))
        self.assertFalse(self.ask(conn))
        approvals()["plugin:1"].cancel()


if __name__ == "__main__":
    unittest.main()
