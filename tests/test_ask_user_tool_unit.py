"""ELI-216: a question from the agent's ask_user tool (a Gateway question,
`question.requested`) is asked in Open WebUI's question card, and the answer
goes to `question.resolve`.

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
        self._turn_writers = {}

    def parse_owui_session_key(self, key):
        return ("user-1", "chat-1") if key == KEY else None

    async def send_request(self, method, params, timeout=10):
        self.sent.append((method, json.loads(json.dumps(params))))
        return {"status": "cancelled" if "cancel" in params else "answered"}


class FakeTurn:
    """The reply streaming the run: what the card is asked through."""

    def __init__(self, answer):
        self.session_key = KEY
        self.owui_message_id = "msg-1"
        self.__metadata__ = {}
        self.asked = []
        self._answer = answer

        async def event_call(payload):
            self.asked.append(payload)
            return await self._answer() if callable(self._answer) else self._answer
        self.__event_call__ = event_call
        self.__event_emitter__ = None


def asked() -> dict:
    """The Pipe's question id -> asking task map (none before ELI-216)."""
    return getattr(ocp, "_AGENT_QUESTIONS", {})


def requested(conn, rec):
    """What the Pipe's connection calls on `question.requested`."""
    return ocp._QUESTION_HOOKS[0](conn, rec)


def card_answer(**answers):
    return {"status": "answered", "answers": answers}


class AskUserToolTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        patch = mock.patch.object(ocp, "pipe_log", lambda *a, **k: None)
        patch.start()
        self.addCleanup(patch.stop)
        for registry in (asked(), ocp._SECRET_REQUESTS, ocp._SECRET_ASKERS):
            registry.clear()
            self.addCleanup(registry.clear)

    async def _ask(self, conn, turn, rec=None):
        if turn is not None:
            ocp._secret_asker_join(turn)
        rec = rec or record()
        seen = requested(conn, rec)
        task = asked().get(rec["id"])
        if task is not None:
            await asyncio.wait_for(task, 2)   # bounds the test only
        return seen

    async def test_an_ask_user_question_shows_the_card_and_its_answer_resolves_it(self):
        conn = FakeConn()
        turn = FakeTurn(card_answer(db={"type": "option", "label": "SQLite"}))
        self.assertTrue(await self._ask(conn, turn))
        self.assertEqual(len(turn.asked), 1)
        card = turn.asked[0]
        self.assertEqual(card["type"], "request:user_input")
        q = card["data"]["questions"][0]
        self.assertEqual((q["id"], q["header"], q["question"]), ("db", "Database", "Which database?"))
        self.assertEqual(q["options"], [{"label": "Postgres", "description": "needs a server"},
                                        {"label": "SQLite", "description": ""}])
        self.assertTrue(q["allow_other"])
        self.assertEqual(conn.sent, [("question.resolve", {
            "id": "ask_1", "answers": {"answers": {"db": ["SQLite"]}}, "resolvedBy": "openwebui"})])

    async def test_a_typed_answer_and_several_questions(self):
        conn = FakeConn()
        rec = record(questions=[
            {"questionId": "db", "header": "Database", "question": "Which database?",
             "options": [{"label": "Postgres"}, {"label": "SQLite"}], "isOther": True},
            {"questionId": "name", "header": "Name", "question": "Project name?",
             "options": [{"label": "alpha"}, {"label": "beta"}], "isOther": True}])
        turn = FakeTurn(card_answer(db={"type": "option", "label": "Postgres"},
                                    name={"type": "other", "text": "gamma"}))
        await self._ask(conn, turn, rec)
        self.assertEqual(conn.sent[0][1]["answers"], {"answers": {"db": ["Postgres"], "name": ["gamma"]}})

    async def test_a_multi_select_question_lists_its_options_and_sends_each_pick(self):
        conn = FakeConn()
        rec = record(questions=[{
            "questionId": "toppings", "header": "Toppings", "question": "Which toppings?",
            "options": [{"label": "Cheese"}, {"label": "Mushrooms"}, {"label": "Olives"}],
            "multiSelect": True, "isOther": True}])
        turn = FakeTurn(card_answer(toppings={"type": "other", "text": "1, 3"}))
        await self._ask(conn, turn, rec)
        q = turn.asked[0]["data"]["questions"][0]
        self.assertEqual(q["options"], [])
        self.assertIn("1. Cheese", q["question"])
        self.assertEqual(conn.sent[0][1]["answers"], {"answers": {"toppings": ["Cheese", "Olives"]}})

    async def test_an_unanswered_card_cancels_the_question_at_once(self):
        """The card's timeout or Cancel: the agent hears it now, not after
        the tool's 15 minutes on "Thinking"."""
        conn, turn = FakeConn(), FakeTurn({"status": "cancelled"})
        await self._ask(conn, turn)
        self.assertEqual(conn.sent, [("question.resolve", {"id": "ask_1", "cancel": True,
                                                           "resolvedBy": "openwebui"})])

    async def test_a_run_no_reply_streams_asks_on_the_chat_tab_in_the_writer_reply(self):
        """A run the agent started: the card goes to the tab viewing the chat,
        on the reply the turn writer streams it into."""
        conn = FakeConn()
        conn._turn_writers[("chat-1", "msg-w")] = mock.Mock(
            chat_id="chat-1", session_key=KEY, message_id="msg-w", closed=False)
        tab = mock.AsyncMock(return_value=card_answer(db={"type": "option", "label": "Postgres"}))
        with mock.patch.object(ocp, "_call_on_chat_tab", tab):
            await self._ask(conn, None)
        self.assertEqual(tab.await_args.args[:3], ("user-1", "chat-1", "msg-w"))
        self.assertEqual(conn.sent[0][1]["answers"], {"answers": {"db": ["Postgres"]}})

    async def test_answered_elsewhere_ends_the_wait_and_sends_nothing(self):
        conn = FakeConn()
        never = asyncio.get_running_loop().create_future()
        turn = FakeTurn(lambda: never)
        ocp._secret_asker_join(turn)
        requested(conn, record())
        task = asked()["ask_1"]
        for _ in range(100):
            if turn.asked:
                break
            await asyncio.sleep(0.01)
        ocp._QUESTION_HOOKS[1]({"id": "ask_1", "status": "answered"})
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(conn.sent, [])

    async def test_a_secret_or_a_chat_not_in_open_webui_is_not_asked_as_a_card(self):
        conn, turn = FakeConn(), FakeTurn(card_answer())
        secret = record(questions=[{"questionId": "secret_value", "header": "Key",
                                    "question": "Provide it.", "options": [], "isSecret": True,
                                    "secretStore": {"name": "DUMMY", "kind": "secret"}}])
        with mock.patch.object(ocp, "_sender_is_owner", mock.AsyncMock(return_value=False)):
            await self._ask(conn, turn, secret)
        other = record(qid="ask_2")
        other["sessionKey"] = "agent:main:main"
        self.assertFalse(await self._ask(conn, turn, other))
        self.assertFalse(await self._ask(conn, turn, record(qid="ask_3", status="answered")))
        self.assertEqual(([p["type"] for p in turn.asked], conn.sent), ([], []))
        self.assertEqual(asked(), {})

    def test_the_pipe_hooks_ask_both_kinds(self):
        self.assertEqual(ocp._QUESTION_HOOKS, (getattr(ocp, "question_requested", None),
                                               getattr(ocp, "question_resolved", None)))


if __name__ == "__main__":
    unittest.main()
