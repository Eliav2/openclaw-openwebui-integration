"""ELI-96: a secret the agent requests (`secrets` action=request, a Gateway
store-bound question) is asked in the chat's masked input dialog, and the
typed value goes only to `question.resolve`.

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
KEY = "agent:main:openwebui-user-1-chat-1"


def record(qid="q-1", status="pending", **store):
    return {"id": qid, "status": status, "sessionKey": KEY, "runId": "run-1", "expiresAtMs": 0,
            "questions": [{"questionId": "secret_value", "header": "API key",
                           "question": "Provide the secret for DUMMY.", "options": [],
                           "isSecret": True,
                           "secretStore": {"name": "DUMMY", "kind": "secret",
                                           "allowedHosts": ["api.example.com"],
                                           "reason": "to call the example API", **store}}]}


QUESTION_METHODS = frozenset({"question.request", "question.resolve", "question.list"})
QUESTION_EVENTS = frozenset({"question.requested", "question.resolved"})


class FakeConn:
    def __init__(self, version="2026.10.10-beta.1", methods=QUESTION_METHODS, events=QUESTION_EVENTS):
        self.gateway_version = version
        self.gateway_methods, self.gateway_events = methods, events
        self.sent = []
        self.dispatched = []

    def parse_owui_session_key(self, key):
        return ("user-1", "chat-1") if key == KEY else None

    async def send_request(self, method, params, timeout=10):
        self.sent.append((method, json.loads(json.dumps(params))))
        return {"status": "cancelled" if "cancel" in params else "answered"}

    async def dispatch_run_event(self, msg):
        self.dispatched.append(json.loads(json.dumps(msg)))


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
                      mock.patch.object(ocp, "_sender_is_owner", mock.AsyncMock(return_value=True))):
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

    async def test_a_gateway_without_the_question_protocol_leaves_it_to_the_control_ui(self):
        """No `question.resolve` or question events in hello-ok: the Pipe
        could not answer it, so it is not asked."""
        for methods, events in ((frozenset(), QUESTION_EVENTS), (QUESTION_METHODS, frozenset()),
                                (frozenset(), frozenset())):
            with self.subTest(methods=sorted(methods), events=sorted(events)):
                conn, turn = FakeConn("2026.10.10-beta.1", methods, events), FakeTurn(VALUE)
                self.assertFalse(await self._ask(conn, turn))
                self.assertEqual((turn.asked, conn.sent), ([], []))

    async def test_any_release_line_that_offers_the_protocol_asks_it(self):
        """The gate is what the Gateway offers, not its version number:
        2026.10.5 has the store-bound question as 2026.10.10 does."""
        for version in ("2026.10.5", "2026.9.3", "2026.10.10-beta.1"):
            with self.subTest(version=version):
                ocp._SECRET_REQUESTS.clear()
                conn, turn = FakeConn(version), FakeTurn(VALUE)
                self.assertTrue(await self._ask(conn, turn), version)
                self.assertEqual(conn.sent[0][0], "question.resolve")

    async def test_a_chat_of_a_user_not_in_owner_users_is_not_asked(self):
        """The store is the owner's: a guest's chat must not write into it,
        and the request is cancelled at once rather than left answerable in
        the Control UI for a run the guest reads (ELI-229). The reply of the
        run that asked says it needs the owner."""
        conn, turn = FakeConn(), FakeTurn(VALUE)
        with mock.patch.object(ocp, "_sender_is_owner", mock.AsyncMock(return_value=False)):
            self.assertTrue(await self._ask(conn, turn))
        self.assertEqual(turn.asked, [])
        self.assertEqual(conn.sent, [("question.resolve", {"id": "q-1", "cancel": True,
                                                           "resolvedBy": "openwebui"})])
        [msg] = conn.dispatched
        payload = msg["payload"]
        self.assertEqual((msg["event"], payload["sessionKey"], payload["runId"], payload["stream"]),
                         ("agent", KEY, "run-1", ocp.PIPE_NOTE_STREAM))
        self.assertEqual(payload["data"]["id"], "msg_note_secret_q-1")
        self.assertIn("`DUMMY`", payload["data"]["text"])
        self.assertIn("needs the owner", payload["data"]["text"])
        self.assertNotIn("q-1", ocp._SECRET_REQUESTS)

    async def test_the_cancel_still_says_so_when_the_gateway_reports_it_resolved_first(self):
        """The Gateway answers `question.resolve` and broadcasts
        `question.resolved` back to back, so both frames can reach the receive
        loop in one step: the event handler runs before the task awaiting the
        answer resumes. Its note still has to reach the reply, or a fast run
        finishes without it (the guest-secret E2E, PR 241 CI attempt 1)."""
        class RacingConn(FakeConn):
            async def send_request(self, method, params, timeout=10):
                self.sent.append((method, json.loads(json.dumps(params))))
                answer = asyncio.get_running_loop().create_future()
                answer.set_result({"status": "cancelled"})
                # The receive loop delivers the response, then handles the
                # event in the same step, before this task resumes.
                ocp.secret_request_resolved({"id": params["id"], "status": "cancelled"})
                await asyncio.sleep(0)
                return await answer
        conn, turn = RacingConn(), FakeTurn(VALUE)
        with mock.patch.object(ocp, "_sender_is_owner", mock.AsyncMock(return_value=False)):
            await self._ask(conn, turn)
        [msg] = conn.dispatched
        self.assertEqual(msg["payload"]["data"]["id"], "msg_note_secret_q-1")
        self.assertIn("needs the owner", msg["payload"]["data"]["text"])

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

    def test_only_the_pipe_asks(self):
        """The Status Action shares the connection code but asks nothing."""
        self.assertEqual(ocp._QUESTION_HOOKS, (ocp.question_requested, ocp.question_resolved))
        with open(os.path.join(HERE, "openclaw_status_action.py"), encoding="utf-8") as fh:
            action = fh.read()
        self.assertIn("_QUESTION_HOOKS = None", action)
        self.assertNotIn("def secret_request_seen", action)
        self.assertNotIn("def question_requested", action)


class RetiredConnectionTests(unittest.IsolatedAsyncioTestCase):
    """A deploy retires the previous load's connection but keeps it open while
    it streams a run. The Gateway sends every connected client its own copy of
    `question.requested`, so a retired connection that still handled it asked
    the same question a second time (ELI-341): after a guest secret request was
    cancelled, the second cancel failed with a GatewayError."""

    async def play(self, conn, frames):
        frames = list(frames)

        class ScriptedWS:
            async def recv(self_):
                if frames:
                    return json.dumps(frames.pop(0))
                conn._stopped = True
                raise asyncio.CancelledError
        conn._ws = ScriptedWS()
        await conn._event_loop()

    async def test_a_question_is_asked_by_the_live_connection_only(self):
        requested = {"type": "event", "event": "question.requested", "payload": record()}
        seen, gone = mock.Mock(), mock.Mock()
        live = ocp._GatewayConnection(lambda: None)
        retired = ocp._GatewayConnection(lambda: None)
        retired._retired = True   # what retire() sets while a run is still streaming here
        with mock.patch.object(ocp, "_QUESTION_HOOKS", (seen, gone)):
            for conn in (retired, live):   # one copy of the frame per connection
                await self.play(conn, [requested])
        self.assertEqual(seen.call_args_list, [mock.call(live, record())])
        self.assertEqual(gone.call_args_list, [])

    async def test_a_retired_connection_still_hands_over_a_resolved_question(self):
        """A question asked before a deploy is held by the previous load's
        module, which only the retired connection reaches: it must still hear
        that the question was answered elsewhere, cancelled or expired."""
        resolved = {"type": "event", "event": "question.resolved",
                    "payload": {"id": "q-1", "status": "cancelled"}}
        seen, gone = mock.Mock(), mock.Mock()
        retired = ocp._GatewayConnection(lambda: None)
        retired._retired = True
        with mock.patch.object(ocp, "_QUESTION_HOOKS", (seen, gone)):
            await self.play(retired, [resolved])
        self.assertEqual(gone.call_args_list, [mock.call({"id": "q-1", "status": "cancelled"})])
        self.assertEqual(seen.call_args_list, [])

    async def test_a_retired_connection_syncs_no_slash_prompts(self):
        frame = {"type": "event", "event": "chat.metadata.changed", "payload": {"commandsChanged": True}}
        hook = mock.Mock()
        retired = ocp._GatewayConnection(lambda: None)
        retired._retired = True
        with mock.patch.object(ocp, "_SLASH_PROMPTS_HOOK", hook):
            await self.play(retired, [frame])
        self.assertEqual(hook.call_args_list, [])


class NoteItemTests(unittest.TestCase):
    """A note item (ELI-229) through `_TurnRenderer`, the one renderer the
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

    def test_a_rebuild_from_the_transcript_keeps_the_note_in_place(self):
        """The turn writer rebuilds a reply from the transcript at its end;
        the note is in no transcript, so it is put back after the item it
        followed (ELI-229: the cancel note vanished from the saved reply)."""
        call = {"type": "function_call", "id": "call_1"}
        result = {"type": "function_call_output", "id": "fco_call_1"}
        streamed = ocp._TurnRenderer()
        streamed.output = [dict(call), dict(result)]
        streamed.feed(self.note())
        streamed.feed({"stream": "assistant", "data": {"delta": "streamed answer"}})
        rebuilt = ocp._TurnRenderer()
        rebuilt.output = [dict(call), dict(result), {"type": "message", "id": "msg_t", "content": []}]
        ocp._keep_pipe_notes(streamed.output, rebuilt)
        self.assertEqual([i["id"] for i in rebuilt.output],
                         ["call_1", "fco_call_1", "msg_note_secret_q-1", "msg_t"])
        ocp._keep_pipe_notes(streamed.output, rebuilt)
        self.assertEqual(len(rebuilt.output), 4, "kept once")

    def test_a_note_with_nothing_before_it_stays_first(self):
        streamed = ocp._TurnRenderer()
        streamed.feed(self.note())
        rebuilt = ocp._TurnRenderer()
        rebuilt.output = [{"type": "message", "id": "msg_t", "content": []}]
        ocp._keep_pipe_notes(streamed.output, rebuilt)
        self.assertEqual([i["id"] for i in rebuilt.output], ["msg_note_secret_q-1", "msg_t"])

    def test_a_note_without_an_id_or_text_adds_nothing(self):
        r = ocp._TurnRenderer()
        self.assertEqual(r.feed({"stream": ocp.PIPE_NOTE_STREAM, "data": {"text": "x"}}), [])
        self.assertEqual(r.output, [])


if __name__ == "__main__":
    unittest.main()
