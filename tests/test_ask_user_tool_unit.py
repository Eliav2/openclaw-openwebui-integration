"""ELI-216: a question from the agent's ask_user tool (a Gateway question,
`question.requested`) is asked in Open WebUI's question card, on a tab that
has loaded the chat, and the answer goes to `question.resolve`. With no such
tab it stays pending until one opens the chat.

Run: uv run --with-requirements requirements-dev.txt pytest -q tests/test_ask_user_tool_unit.py
"""
import asyncio
import json
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402
from _test_helpers import FakeSio, fake_owui  # noqa: E402

KEY = "agent:main:openwebui-user-1-chat-1"


def record(qid="ask_1", status="pending", questions=None):
    """What OpenClaw 2026.10.5 broadcasts for an ask_user call (the tool's
    normalized questions: header cut to 12, isOther always true)."""
    return {"id": qid, "status": status, "sessionKey": KEY, "runId": "run-1",
            "expiresAtMs": int((ocp.time.time() + 900) * 1000),
            "questions": questions or [{
                "questionId": "db", "header": "Database", "question": "Which database?",
                "options": [{"label": "Postgres", "description": "needs a server"},
                            {"label": "SQLite"}],
                "isOther": True}]}


class FakeConn:
    def __init__(self):
        self.gateway_version = "2026.10.5"
        self.sent = []
        self.dispatched = []
        self._turn_writers = {}

    def parse_owui_session_key(self, key):
        return ("user-1", "chat-1") if key == KEY else None

    async def send_request(self, method, params, timeout=10):
        self.sent.append((method, json.loads(json.dumps(params))))
        return {"status": "cancelled" if "cancel" in params else "answered"}

    async def dispatch_run_event(self, msg):
        """The Pipe's own note into the reply (ELI-229)."""
        self.dispatched.append(msg)


class FakeTurn:
    """The reply streaming the run, on socket `sid`."""

    def __init__(self, sid="sid-own"):
        self.session_key = KEY
        self.owui_message_id = "msg-1"
        self.__metadata__ = {"session_id": sid}
        self.__event_emitter__ = None

        async def event_call(payload):
            # What Open WebUI answers for a call on a socket that is gone.
            return {"error": "Client session disconnected."}
        self.__event_call__ = event_call


class Tabs:
    """Open WebUI's sockets: SESSION_POOL, and a card on each socket that
    answers when the test says so."""

    def __init__(self, test, pool=None):
        self.pool = pool if pool is not None else {}
        self.pending = {}
        self.sio = FakeSio(self._reply)
        fake_owui(test, sio=self.sio, SESSION_POOL=self.pool)

    def _reply(self, event, data, to, timeout):
        future = asyncio.get_running_loop().create_future()
        self.pending[to] = future
        return future

    def open(self, sid, chat="chat-1"):
        self.pool[sid] = {"id": "user-1", "chat_ids": [chat]}

    async def card_on(self, sid):
        """The card the Pipe showed on `sid` (waits for it)."""
        for _ in range(500):
            if sid in self.pending and not self.pending[sid].done():
                return [c for c in self.sio.calls if c[2] == sid][-1][1]
            await asyncio.sleep(0.01)
        raise AssertionError(f"no card on {sid}; calls to {[c[2] for c in self.sio.calls]}")

    def answer(self, sid, value):
        self.pending[sid].set_result(value)

    def removed_from(self, sid) -> bool:
        return any(e[2] == sid and e[1]["data"]["type"] == "request:interaction:done"
                   for e in self.sio.emits)


def answered(**answers):
    return {"status": "answered", "answers": answers}


def requested(conn, rec):
    """What the Pipe's connection calls on `question.requested`."""
    return ocp._QUESTION_HOOKS[0](conn, rec)


def asked() -> dict:
    """The Pipe's question id -> asking task map (none before ELI-216)."""
    return getattr(ocp, "_AGENT_QUESTIONS", {})


class AskUserToolTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        patch = mock.patch.object(ocp, "pipe_log", lambda *a, **k: None)
        patch.start()
        self.addCleanup(patch.stop)
        for registry in (asked(), ocp._SECRET_REQUESTS, ocp._SECRET_ASKERS,
                         getattr(ocp, "_READY_CHAT_TABS", {})):
            registry.clear()
            self.addCleanup(registry.clear)
        tracked = getattr(ocp, "_CHAT_TABS_TRACKED", [False])
        tracked[0] = True     # the composer watcher runs, as it does by default
        self.addCleanup(tracked.__setitem__, 0, False)

    def ask(self, conn, rec=None, turn=None):
        if turn is not None:
            ocp._secret_asker_join(turn)
        return requested(conn, rec or record())

    async def finished(self, rec_id="ask_1"):
        task = asked().get(rec_id)
        if task is not None:
            await asyncio.wait_for(task, 2)   # bounds the test only

    async def test_the_card_shows_on_the_reply_tab_and_its_answer_resolves_the_question(self):
        conn, tabs = FakeConn(), Tabs(self)
        tabs.open("sid-own")
        self.assertTrue(self.ask(conn, turn=FakeTurn()))
        sent = await tabs.card_on("sid-own")
        self.assertEqual((sent["chat_id"], sent["message_id"]), ("chat-1", "msg-1"))
        card = sent["data"]
        self.assertEqual(card["type"], "request:user_input")
        self.assertIsNone(card["data"]["timeout_ms"], "a card countdown would read as Cancel")
        q = card["data"]["questions"][0]
        self.assertEqual((q["id"], q["header"], q["question"]), ("db", "Database", "Which database?"))
        self.assertEqual(q["options"], [{"label": "Postgres", "description": "needs a server"},
                                        {"label": "SQLite", "description": ""}])
        tabs.answer("sid-own", answered(db={"type": "option", "label": "SQLite"}))
        await self.finished()
        self.assertEqual(conn.sent, [("question.resolve", {
            "id": "ask_1", "answers": {"answers": {"db": ["SQLite"]}}, "resolvedBy": "openwebui"})])
        self.assertTrue(tabs.removed_from("sid-own"))

    async def test_asked_with_no_tab_open_it_waits_and_shows_when_a_tab_opens_the_chat(self):
        """Eliav asks, closes the tab (or the phone sleeps), comes back later:
        the question is still there to answer."""
        conn, tabs = FakeConn(), Tabs(self)
        self.ask(conn, turn=FakeTurn("sid-gone"))
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual((tabs.sio.calls, conn.sent), ([], []), "nothing to show it on, nothing to send")
        tabs.open("sid-later")
        ocp._chat_tab_ready("sid-later", "chat-1")      # the tab has loaded the chat
        await tabs.card_on("sid-later")
        tabs.answer("sid-later", answered(db={"type": "option", "label": "Postgres"}))
        await self.finished()
        self.assertEqual(conn.sent[0][1]["answers"], {"answers": {"db": ["Postgres"]}})

    async def test_a_tab_that_has_not_loaded_the_chat_gets_no_card(self):
        """Open WebUI cancels a card at once on a tab still loading the chat:
        that must not read as the user's Cancel."""
        conn, tabs = FakeConn(), Tabs(self)
        tabs.open("sid-loading")
        self.ask(conn, turn=FakeTurn("sid-gone"))
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(tabs.sio.calls, [])
        ocp._chat_tab_ready("sid-loading", "chat-1")
        await tabs.card_on("sid-loading")
        asked()["ask_1"].cancel()

    async def test_a_newer_tab_takes_the_card(self):
        conn, tabs = FakeConn(), Tabs(self)
        tabs.open("sid-desk")
        ocp._chat_tab_ready("sid-desk", "chat-1")
        self.ask(conn, turn=FakeTurn("sid-gone"))
        await tabs.card_on("sid-desk")
        tabs.open("sid-phone")
        ocp._chat_tab_ready("sid-phone", "chat-1")
        await tabs.card_on("sid-phone")
        self.assertTrue(tabs.removed_from("sid-desk"))
        tabs.answer("sid-phone", answered(db={"type": "other", "text": "DuckDB"}))
        await self.finished()
        self.assertEqual(conn.sent[0][1]["answers"], {"answers": {"db": ["DuckDB"]}})

    async def test_cancel_on_the_card_cancels_the_question(self):
        conn, tabs = FakeConn(), Tabs(self)
        tabs.open("sid-own")
        self.ask(conn, turn=FakeTurn())
        await tabs.card_on("sid-own")
        tabs.answer("sid-own", {"status": "cancelled", "answers": {}})
        await self.finished()
        self.assertEqual(conn.sent, [("question.resolve", {"id": "ask_1", "cancel": True,
                                                           "resolvedBy": "openwebui"})])

    async def test_a_cancel_from_a_tab_that_left_the_chat_is_not_the_users(self):
        """The tab switched to another chat: Open WebUI cancels the card
        there. The question stays, for the next tab that shows the chat."""
        conn, tabs = FakeConn(), Tabs(self)
        tabs.open("sid-own")
        self.ask(conn, turn=FakeTurn())
        await tabs.card_on("sid-own")
        tabs.pool["sid-own"]["chat_ids"] = ["chat-other"]
        tabs.answer("sid-own", {"status": "cancelled", "answers": {}})
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(conn.sent, [])
        tabs.pool["sid-own"]["chat_ids"] = ["chat-1"]
        ocp._chat_tab_ready("sid-own", "chat-1")
        await tabs.card_on("sid-own")
        tabs.answer("sid-own", answered(db={"type": "option", "label": "SQLite"}))
        await self.finished()
        self.assertEqual(conn.sent[0][1]["answers"], {"answers": {"db": ["SQLite"]}})

    async def test_several_questions_and_a_multi_select_one(self):
        conn, tabs = FakeConn(), Tabs(self)
        tabs.open("sid-own")
        rec = record(questions=[
            {"questionId": "name", "header": "Name", "question": "Project name?",
             "options": [{"label": "alpha"}, {"label": "beta"}], "isOther": True},
            {"questionId": "toppings", "header": "Toppings", "question": "Which toppings?",
             "options": [{"label": "Cheese"}, {"label": "Mushrooms"}, {"label": "Olives"}],
             "multiSelect": True, "isOther": True}])
        self.ask(conn, rec, turn=FakeTurn())
        sent = await tabs.card_on("sid-own")
        multi = sent["data"]["data"]["questions"][1]
        self.assertEqual(multi["options"], [])
        self.assertIn("1. Cheese", multi["question"])
        tabs.answer("sid-own", answered(name={"type": "other", "text": "gamma"},
                                        toppings={"type": "other", "text": "1, 3"}))
        await self.finished()
        self.assertEqual(conn.sent[0][1]["answers"],
                         {"answers": {"name": ["gamma"], "toppings": ["Cheese", "Olives"]}})

    async def test_a_run_no_reply_streams_asks_in_the_turn_writers_reply(self):
        """A run the agent started: the card belongs to the reply the turn
        writer streams it into."""
        conn, tabs = FakeConn(), Tabs(self)
        conn._turn_writers[("chat-1", "msg-w")] = mock.Mock(
            chat_id="chat-1", session_key=KEY, message_id="msg-w", closed=False)
        tabs.open("sid-1")
        ocp._chat_tab_ready("sid-1", "chat-1")
        self.ask(conn)
        sent = await tabs.card_on("sid-1")
        self.assertEqual(sent["message_id"], "msg-w")
        tabs.answer("sid-1", answered(db={"type": "option", "label": "Postgres"}))
        await self.finished()
        self.assertEqual(conn.sent[0][1]["answers"], {"answers": {"db": ["Postgres"]}})

    async def test_answered_elsewhere_closes_the_card_and_sends_nothing(self):
        conn, tabs = FakeConn(), Tabs(self)
        tabs.open("sid-own")
        self.ask(conn, turn=FakeTurn())
        await tabs.card_on("sid-own")
        task = asked()["ask_1"]
        ocp._QUESTION_HOOKS[1]({"id": "ask_1", "status": "answered"})
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(conn.sent, [])
        self.assertTrue(tabs.removed_from("sid-own"))

    async def test_a_secret_or_a_chat_not_in_open_webui_is_not_asked_as_a_card(self):
        conn, tabs = FakeConn(), Tabs(self)
        tabs.open("sid-own")
        secret = record(questions=[{"questionId": "secret_value", "header": "Key",
                                    "question": "Provide it.", "options": [], "isSecret": True,
                                    "secretStore": {"name": "DUMMY", "kind": "secret"}}])
        with mock.patch.object(ocp, "_sender_is_owner", mock.AsyncMock(return_value=False)):
            self.ask(conn, secret, turn=FakeTurn())
            for task in list(ocp._SECRET_REQUESTS.values()):
                await asyncio.wait_for(task, 2)
        other = record(qid="ask_2")
        other["sessionKey"] = "agent:main:main"
        self.assertFalse(self.ask(conn, other))
        self.assertFalse(self.ask(conn, record(qid="ask_3", status="answered")))
        self.assertEqual((tabs.sio.calls, conn.sent), ([], []))
        self.assertEqual(asked(), {})

    def test_the_pipe_hooks_ask_both_kinds(self):
        self.assertEqual(ocp._QUESTION_HOOKS, (getattr(ocp, "question_requested", None),
                                               getattr(ocp, "question_resolved", None)))


if __name__ == "__main__":
    unittest.main()
