"""ELI-96: a secret the agent requests (`secrets` action=request, a Gateway
store-bound question) is asked in the chat's masked input dialog, and the
typed value goes only to `question.resolve`. ELI-197: where Open WebUI does
not ask (an older Gateway, a guest's chat), the reply says where to answer it.

Run: uv run --with-requirements requirements-dev.txt pytest -q tests/test_secret_request_unit.py
"""
import asyncio
import json
import os
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALUE = "s3cret-VALUE-eli96-7c1"
CHAT = "0f3c2a54-9b1e-4c7d-8a2f-5e6d7c8b9a01"
KEY = f"agent:main:openwebui-user-1-{CHAT}"
CONTROL_UI = "https://claw.example.com"
SESSION_PAGE = f"{CONTROL_UI}/chat/main/{CHAT.replace('-', '')}"


def record(qid="q-1", status="pending", **store):
    return {"id": qid, "status": status, "sessionKey": KEY, "runId": "run-1", "expiresAtMs": 0,
            "questions": [{"questionId": "secret_value", "header": "API key",
                           "question": "Provide the secret for DUMMY.", "options": [],
                           "isSecret": True,
                           "secretStore": {"name": "DUMMY", "kind": "secret",
                                           "allowedHosts": ["api.example.com"],
                                           "reason": "to call the example API", **store}}]}


class FakeConn:
    def __init__(self, version="2026.10.10-beta.1"):
        self.gateway_version = version
        self.sent = []
        self.dispatched = []

    def parse_owui_session_key(self, key):
        return ("user-1", CHAT) if key == KEY else None

    async def dispatch_run_event(self, msg):
        self.dispatched.append(json.loads(json.dumps(msg)))

    def notes(self):
        """The text of each note the reply was given."""
        return [m["payload"]["data"]["text"] for m in self.dispatched]

    async def send_request(self, method, params, timeout=10):
        self.sent.append((method, json.loads(json.dumps(params))))
        return {"status": "cancelled" if "cancel" in params else "answered"}


class FakeTurn:
    """The reply streaming the run: what the dialog is asked through."""

    def __init__(self, answer):
        self.session_key = KEY
        self.owui_message_id = "msg-1"
        self.__metadata__ = {}
        self.asked, self.emitted = [], []
        self._answer = answer

        async def event_call(payload):
            self.asked.append(payload)
            return await self._answer() if callable(self._answer) else self._answer

        async def emitter(event):
            self.emitted.append(event)
        self.__event_call__ = event_call
        self.__event_emitter__ = emitter


class SecretRequestTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.logged = []
        for patch in (mock.patch.object(ocp, "pipe_log", lambda *a, **k: self.logged.append(a)),
                      mock.patch.object(ocp, "_sender_is_owner", mock.AsyncMock(return_value=True)),
                      mock.patch.object(ocp, "_control_ui_url_setting",
                                        mock.AsyncMock(return_value=CONTROL_UI), create=True)):
            patch.start()
            self.addCleanup(patch.stop)
        ocp._SECRET_REQUESTS.clear()
        ocp._SECRET_ASKERS.clear()
        self.addCleanup(ocp._SECRET_REQUESTS.clear)
        self.addCleanup(ocp._SECRET_ASKERS.clear)

    async def _ask(self, conn, turn, rec=None):
        if turn is not None:
            ocp._secret_asker_join(turn)
        rec = rec or record()
        rec["expiresAtMs"] = int((ocp.time.time() + 30) * 1000)
        seen = ocp.secret_request_seen(conn, rec)
        task = ocp._SECRET_REQUESTS.get(rec["id"])
        if task is not None:
            await asyncio.wait_for(task, 2)   # bounds the test only
        return seen

    def assertValueNowhere(self, *places):
        for what in places:
            self.assertNotIn(VALUE, json.dumps(what, default=repr))

    def test_the_dialog_is_built_from_the_request_metadata_only(self):
        payload = ocp._secret_request_payload(record()["questions"][0])
        self.assertEqual(payload["type"], "input")
        data = payload["data"]
        self.assertEqual(data["type"], "password")
        self.assertEqual(set(data), {"title", "message", "placeholder", "type"}, "no value field")
        self.assertIn("DUMMY", data["title"])
        self.assertIn("to call the example API", data["message"])
        self.assertIn("api.example.com", data["message"])

    async def test_a_typed_value_goes_only_to_question_resolve(self):
        conn, turn = FakeConn(), FakeTurn(VALUE)
        self.assertTrue(await self._ask(conn, turn))
        self.assertEqual(conn.sent, [("question.resolve", {
            "id": "q-1", "answers": {"answers": {"secret_value": [VALUE]}}, "resolvedBy": "openwebui"})])
        self.assertEqual([p["data"]["type"] for p in turn.asked], ["password"])
        self.assertEqual(turn.emitted[-1]["data"]["content"], "DUMMY: stored")
        self.assertValueNowhere(turn.asked, turn.emitted, self.logged)

    async def test_a_cancelled_dialog_cancels_the_request(self):
        """Open WebUI's dialog answers False on cancel: the agent hears "cancelled"."""
        conn, turn = FakeConn(), FakeTurn(False)
        await self._ask(conn, turn)
        self.assertEqual(conn.sent, [("question.resolve", {"id": "q-1", "cancel": True,
                                                           "resolvedBy": "openwebui"})])

    async def test_an_older_gateway_leaves_it_to_the_control_ui(self):
        """On 2026.9 a stored secret cannot be used by a run (ELI-97): Open
        WebUI asks nothing and sends nothing."""
        conn, turn = FakeConn("2026.9.3"), FakeTurn(VALUE)
        await self._ask(conn, turn)
        self.assertEqual((turn.asked, conn.sent), ([], []))

    async def test_an_older_gateway_says_where_to_answer_it(self):
        """ELI-197: the reply says the request waits in the Control UI and
        links this chat's session there (the Status Action's CONTROL_UI_URL),
        as a note item of the run the request came from."""
        conn = FakeConn("2026.9.3")
        self.assertTrue(await self._ask(conn, None))
        [msg] = conn.dispatched
        self.assertEqual(msg["event"], "agent")
        payload = msg["payload"]
        self.assertEqual((payload["sessionKey"], payload["runId"], payload["stream"]),
                         (KEY, "run-1", ocp.PIPE_NOTE_STREAM))
        self.assertEqual(payload["data"]["id"], "msg_note_secret_q-1")
        note = payload["data"]["text"]
        self.assertIn("`DUMMY`", note)
        self.assertIn(f"[OpenClaw Control UI]({SESSION_PAGE})", note)
        self.assertNotIn("\u2014", note)

    async def test_without_a_control_ui_url_it_names_the_place_only(self):
        conn = FakeConn("2026.9.3")
        with mock.patch.object(ocp, "_control_ui_url_setting", mock.AsyncMock(return_value="")):
            await self._ask(conn, None)
        [note] = conn.notes()
        self.assertIn("OpenClaw Control UI", note)
        self.assertNotIn("](", note)

    async def test_answered_before_the_note_shows_no_note(self):
        conn = FakeConn("2026.9.3")
        rec = record()
        rec["expiresAtMs"] = int((ocp.time.time() + 30) * 1000)
        ocp.secret_request_seen(conn, rec)
        task = ocp._SECRET_REQUESTS["q-1"]
        ocp.secret_request_resolved({"id": "q-1"})
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(conn.dispatched, [])

    async def test_a_chat_of_a_user_not_in_owner_users_is_not_asked(self):
        """The store is the owner's: a guest's chat must not write into it.
        Its reply says only the owner can enter it, with no link (ELI-197)."""
        for version in ("2026.10.10-beta.1", "2026.9.3"):
            with self.subTest(version=version):
                ocp._SECRET_REQUESTS.clear()
                conn, turn = FakeConn(version), FakeTurn(VALUE)
                with mock.patch.object(ocp, "_sender_is_owner", mock.AsyncMock(return_value=False)):
                    await self._ask(conn, turn)
                self.assertEqual((turn.asked, conn.sent), ([], []))
                [note] = conn.notes()
                self.assertIn("only the owner", note)
                self.assertNotIn(CONTROL_UI, note)

    async def test_answered_elsewhere_ends_the_wait_and_sends_nothing(self):
        conn = FakeConn()
        never = asyncio.get_running_loop().create_future()
        turn = FakeTurn(lambda: never)
        ocp._secret_asker_join(turn)
        rec = record()
        rec["expiresAtMs"] = int((ocp.time.time() + 30) * 1000)
        ocp.secret_request_seen(conn, rec)
        task = ocp._SECRET_REQUESTS["q-1"]
        for _ in range(100):
            if turn.asked:
                break
            await asyncio.sleep(0.01)
        ocp.secret_request_resolved({"id": "q-1", "status": "answered"})
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(conn.sent, [])

    async def test_no_tab_on_the_chat_leaves_it_to_expire(self):
        """No reply streams the run and no tab views the chat: nothing is sent,
        and the Gateway's expiry tells the agent no credential arrived."""
        conn = FakeConn()
        with mock.patch.object(ocp, "_call_on_chat_tab", mock.AsyncMock(return_value=None)) as tab:
            await self._ask(conn, None)
        tab.assert_awaited_once()
        self.assertEqual(conn.sent, [])

    async def test_a_question_that_is_not_a_secret_request_is_left_alone(self):
        conn, turn = FakeConn(), FakeTurn(VALUE)
        plain = record()
        del plain["questions"][0]["secretStore"]
        self.assertFalse(await self._ask(conn, turn, plain))
        self.assertFalse(await self._ask(conn, turn, record(qid="q-2", status="answered")))
        self.assertEqual((turn.asked, conn.sent), ([], []))

    async def test_an_asked_request_gets_no_note(self):
        conn, turn = FakeConn(), FakeTurn(VALUE)
        await self._ask(conn, turn)
        self.assertEqual(conn.dispatched, [])

    def test_only_the_pipe_asks(self):
        """The Status Action shares the connection code but asks nothing."""
        self.assertEqual(ocp._QUESTION_HOOKS, (ocp.secret_request_seen, ocp.secret_request_resolved))
        with open(os.path.join(HERE, "openclaw_status_action.py"), encoding="utf-8") as fh:
            action = fh.read()
        self.assertIn("_QUESTION_HOOKS = None", action)
        self.assertNotIn("def secret_request_seen", action)


class NoteItemTests(unittest.TestCase):
    """A note item (ELI-197) through `_TurnRenderer`, the one renderer the
    live reply and a turn writer share."""

    def note(self, text="note one", nid="msg_note_secret_q-1"):
        return {"sessionKey": KEY, "runId": "run-1", "stream": ocp.PIPE_NOTE_STREAM,
                "data": {"id": nid, "text": text}}

    def test_a_note_is_a_message_item_at_the_end(self):
        r = ocp._TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "before "}})
        [event] = r.feed(self.note())
        self.assertEqual(event["type"], "response.output_item.added")
        self.assertEqual(event["output_index"], ocp._OUTPUT_APPEND_INDEX)
        self.assertEqual(event["item"]["id"], "msg_note_secret_q-1")
        self.assertEqual(event["item"]["content"][0]["text"], "note one")
        self.assertEqual([i["id"] for i in r.output][-1], "msg_note_secret_q-1")
        self.assertEqual(r.text, "before ", "a note is not run text")

    def test_text_after_a_note_is_a_new_message(self):
        r = ocp._TurnRenderer()
        r.feed(self.note())
        r.feed({"stream": "assistant", "data": {"delta": "after"}})
        self.assertEqual([i["content"][0]["text"] for i in r.output], ["note one", "after"])

    def test_the_same_note_again_replaces_it(self):
        r = ocp._TurnRenderer()
        r.feed(self.note())
        r.feed(self.note("note two"))
        self.assertEqual([i["content"][0]["text"] for i in r.output], ["note two"])

    def test_a_note_without_an_id_or_text_adds_nothing(self):
        r = ocp._TurnRenderer()
        self.assertEqual(r.feed({"stream": ocp.PIPE_NOTE_STREAM, "data": {"text": "x"}}), [])
        self.assertEqual(r.output, [])


if __name__ == "__main__":
    unittest.main()
