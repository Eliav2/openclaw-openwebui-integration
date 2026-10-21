"""ELI-94: a run that ended while Open WebUI was stopped leaves its reply partial.

A graceful Open WebUI stop cancels the request that streams a reply and saves
the message `done: true` with what it had so far. The run keeps going in the
Gateway and closes while Open WebUI is down, so no event reaches the next
process and the startup reconcile (ELI-90) only looks at `done: false`
messages. DESIGN-agent-turns.md case 4: the session's `updatedAt` is newer
than the chat's `updated_at`, so `chat.history` is read and a run that closed
after the chat was last written gets its message rebuilt from the transcript.

Run: python3 -m pytest -q tests/test_eli94_unit.py
"""
import asyncio
import copy
import sys
import types
import unittest
from unittest import mock

import test_pipe_unit  # noqa: F401  (installs the module stubs openclaw_pipe needs)
from test_pipe_unit import _turn_history
from _test_helpers import owui_modules

import openclaw_pipe
from openclaw_pipe import _owui_session_key, _GatewayConnection, turn_message_id

USER = "11111111-1111-1111-1111-111111111111"
CHAT = "22222222-2222-2222-2222-222222222222"
M = "33333333-3333-3333-3333-333333333333"
RUN = f"msg-{CHAT}-{M}-1791880000.0"
SESSION_ID = "44444444-4444-4444-4444-444444444444"

# Open WebUI saved the cancelled reply at T0 (whole seconds, chat.updated_at).
T0 = 1791880010
# The run closed 30 s later, while Open WebUI was down.
CLOSED_MS = (T0 + 30) * 1000 + 250


def _closed_history(closed_ms=CLOSED_MS, run=RUN):
    hist = _turn_history(run=run)
    for m in hist["messages"]:
        if m.get("idempotencyKey") == f"cli-assistant:{run}":
            m["timestamp"] = closed_ms
    return hist


def _cards(output):
    return sum(1 for i in output or [] if i.get("type") == "function_call")


class _EndedWhileDownCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.key = _owui_session_key("main", USER, CHAT)
        # What Open WebUI saved when its stop cancelled the reply: the
        # preamble streamed, the tool call had not, done:true.
        self.chat = {"history": {"currentId": M, "messages": {
            "u1": {"id": "u1", "role": "user", "content": "check the logs", "childrenIds": [M],
                   "parentId": None},
            M: {"id": M, "role": "assistant", "parentId": "u1", "childrenIds": [], "done": True,
                "model": "openclaw_gateway.m", "content": "Reading them.",
                "output": [{"type": "message", "role": "assistant", "id": "o1", "status": "completed",
                            "content": [{"type": "output_text", "text": "Reading them."}]}]},
        }}}
        self.updated_at = T0
        self.history = _closed_history()
        self.row = {"key": self.key, "hasActiveRun": False, "status": "done",
                    "updatedAt": CLOSED_MS + 40, "sessionId": SESSION_ID, "lastRunId": RUN}
        self.live_task = False
        self.history_reads = 0
        self.writes = []
        test = self

        class FakeChat:
            def __init__(self):
                self.chat, self.id, self.user_id = test.chat, CHAT, USER
                self.updated_at = test.updated_at

        class FakeChats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                return FakeChat() if chat_id == CHAT else None

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                test.writes.append((message_id, copy.deepcopy(message)))
                msgs = test.chat["history"]["messages"]
                msgs[message_id] = {**msgs.get(message_id, {}), **message}
                return FakeChat()

        chats_mod = types.ModuleType("open_webui.models.chats")
        chats_mod.Chats = FakeChats
        self.sio = mock.AsyncMock()
        modules = {**owui_modules(sio=self.sio),
                   "open_webui.models": types.ModuleType("open_webui.models"),
                   "open_webui.models.chats": chats_mod}
        self.conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))

        async def send_request(method, params, timeout=10):
            if method == "chat.history":
                test.history_reads += 1
                return copy.deepcopy(test.history)
            if method == "sessions.list":
                return {"sessions": [dict(test.row)]}
            raise AssertionError(method)
        self.conn.send_request = send_request

        async def live(chat_id):
            return test.live_task

        async def no_stubs(prefixes):
            return []  # nothing is done:false: the ELI-90 pass has nothing to do

        async def get_conn(valves_getter):
            return test.conn
        patches = [mock.patch.dict(sys.modules, modules),
                   mock.patch("openclaw_pipe._owui_chat_has_live_task", live),
                   mock.patch("openclaw_pipe._orphan_stub_ids", no_stubs),
                   mock.patch("openclaw_pipe._get_gateway_connection", get_conn),
                   mock.patch("openclaw_pipe._composer_model_prefixes", lambda **kw: ["openclaw_gateway."]),
                   mock.patch("openclaw_pipe._composer_own_function_id", lambda: "openclaw_gateway")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def startup(self):
        """What a fresh Open WebUI process runs on its first model listing."""
        await openclaw_pipe._reconcile_orphan_stubs(lambda: types.SimpleNamespace(AGENT_ID="main"))
        for _ in range(50):
            await asyncio.sleep(0)

    def msg(self):
        return self.chat["history"]["messages"][M]


class EndedWhileDownTests(_EndedWhileDownCase):

    async def test_a_run_that_ended_while_down_completes_its_partial_reply(self):
        await self.startup()
        m = self.msg()
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Reading them.\n\nFound it.")
        self.assertEqual(_cards(m.get("output")), 1, "the tool card comes from the transcript")
        self.assertEqual(set(self.chat["history"]["messages"]), {"u1", M}, "no second message")

    async def test_a_run_that_closed_before_the_chat_was_last_written_is_left_alone(self):
        """The reply was written after its run closed: it already holds the end."""
        self.history = _closed_history(closed_ms=(T0 - 1) * 1000 + 900)
        await self.startup()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.msg()["content"], "Reading them.")

    async def test_the_same_second_counts_as_written_after(self):
        """Open WebUI stamps whole seconds: a run that closed inside the
        second the chat was written is not proof the chat missed it."""
        self.history = _closed_history(closed_ms=T0 * 1000 + 999)
        await self.startup()
        self.assertEqual(self.writes, [])

    async def test_a_session_not_newer_than_the_chat_is_not_read(self):
        self.row["updatedAt"] = T0 * 1000 + 500
        await self.startup()
        self.assertEqual(self.history_reads, 0)
        self.assertEqual(self.writes, [])

    async def test_a_session_with_an_active_run_is_left_to_the_handover(self):
        self.row["hasActiveRun"] = True
        await self.startup()
        self.assertEqual(self.history_reads, 0)
        self.assertEqual(self.writes, [])

    async def test_a_chat_with_a_live_request_is_not_touched(self):
        self.live_task = True
        await self.startup()
        self.assertEqual(self.writes, [])

    async def test_a_complete_reply_is_never_rewritten(self):
        """Closed after the chat's write and found by identity, but the
        message already holds everything the transcript has."""
        full = _closed_history()
        self.msg()["content"] = "Reading them.\n\nFound it."
        self.msg()["output"] = openclaw_pipe._history_renderer(
            openclaw_pipe._stub_turn(full, M, "")["messages"]).finished_output()
        await self.startup()
        self.assertEqual(self.writes, [])

    async def test_a_wake_closer_is_never_used(self):
        """A cron wake's closing message is keyed by the sessionId and only
        the first wake gets one (ELI-103): never rebuild from it."""
        wake = SESSION_ID
        self.history = _closed_history(run=wake)
        wake_m = turn_message_id(self.key, wake, CHAT)
        self.chat["history"]["messages"][wake_m] = {
            "id": wake_m, "role": "assistant", "parentId": M, "childrenIds": [], "done": True,
            "content": "Reading them.", "output": []}
        await self.startup()
        self.assertEqual(self.writes, [])

    async def test_a_message_still_generating_is_left_to_the_orphan_pass(self):
        self.msg()["done"] = False
        await self.startup()
        self.assertNotIn(M, [mid for mid, _ in self.writes], "the ELI-90 pass owns done:false")

    async def test_a_recovery_that_closed_while_down_lands_in_the_interrupted_reply(self):
        """Rule E: the Gateway restarted mid-run (RUN has no closer) and its
        recovery closed while Open WebUI was down. Both spans go into M."""
        hist = _turn_history(run=RUN, closed=False)
        rec = "rec-1"
        hist["messages"] += [
            {"role": "user", "content": "[System] continue", "idempotencyKey": f"{rec}:user",
             "provenance": {"kind": "internal_system", "sourceTool": "main_session_restart_recovery"}},
            {"role": "assistant", "__openclaw": {"importedFrom": "claude-cli"},
             "content": [{"type": "text", "text": "Found it."}]},
            {"role": "assistant", "idempotencyKey": f"cli-assistant:{rec}", "timestamp": CLOSED_MS,
             "content": [{"type": "text", "text": "Found it."}]}]
        self.history = hist
        await self.startup()
        m = self.msg()
        self.assertEqual(_cards(m.get("output")), 1, "the interrupted run's card")
        self.assertIn("Found it.", m["content"], "the recovery's text")
        self.assertEqual(set(self.chat["history"]["messages"]), {"u1", M})

    def embedded(self, *, newer_turn=False):
        """The run as the embedded runtime stores it (e2e 2026-10-13, run
        38405904032): no closing key, `timestamp` is when the message began
        (before the chat's write), the run id only in `__openclaw.runId`.
        The settled session row is the end signal."""
        msgs = [{"role": "user", "content": "check the logs", "idempotencyKey": f"{RUN}:user",
                 "timestamp": T0 * 1000 - 900},
                {"role": "assistant", "timestamp": T0 * 1000 - 700,
                 "__openclaw": {"runId": RUN, "recordTimestampMs": CLOSED_MS},
                 "content": [{"type": "text", "text": "Reading them. Found it."}]}]
        if newer_turn:
            msgs.append({"role": "user", "content": "and now?", "timestamp": CLOSED_MS + 20,
                         "idempotencyKey": f"msg-{CHAT}-later-1791880099.0:user"})
            msgs.append({"role": "assistant", "timestamp": CLOSED_MS + 30,
                         "__openclaw": {"runId": f"msg-{CHAT}-later-1791880099.0"},
                         "content": [{"type": "text", "text": "Looking again"}]})
        self.history = {"messages": msgs}

    async def test_an_embedded_run_settled_while_down_completes_its_reply(self):
        self.embedded()
        await self.startup()
        self.assertEqual(self.msg()["content"], "Reading them. Found it.")
        self.assertIs(self.msg()["done"], True)
        self.assertEqual(set(self.chat["history"]["messages"]), {"u1", M})

    async def test_a_run_that_settled_in_the_second_of_the_write_is_left_alone(self):
        self.embedded()
        self.row["updatedAt"] = T0 * 1000 + 900
        await self.startup()
        self.assertEqual(self.writes, [])

    async def test_a_turn_admitted_after_the_settled_run_bounds_its_span(self):
        """A new run began between the row and the transcript read: the
        settled run's reply stops at that run's user turn."""
        self.embedded(newer_turn=True)
        await self.startup()
        self.assertEqual(self.msg()["content"], "Reading them. Found it.")
        self.assertEqual([mid for mid, _ in self.writes], [M], "the new run's reply is not touched")

    async def test_a_missing_chat_is_skipped(self):
        self.row["key"] = _owui_session_key("main", USER, "55555555-5555-5555-5555-555555555555")
        await self.startup()
        self.assertEqual(self.history_reads, 0)


class AskReplyEndedWhileDownTests(_EndedWhileDownCase):
    """#21 round, ask-rebuild (2026-10-14 07:06-07:08Z, message
    a8351886): the question was answered Blue from the card, then Open WebUI
    restarted during the answer run's tools. The startup reconcile rewrote the
    reply from the transcript as 6 Bash cards and "You chose Blue ...": the
    question and its answered Ask User card, which the user had seen, were
    gone from both `content` and the items. Real transcript, ids mapped:
    test_fixture_eli94_ask_rebuild.json. Each run's text is only in its
    closing message there (the imports carry tool blocks only)."""

    REAL = {"d0f0c6ff-9968-40f6-bbd9-b3b4418c048e": CHAT,
            "a8351886-2ab5-4acf-8752-1ac1612d64c3": M,
            "00000000-0000-4000-8000-0000000000a1": USER}
    END = "You chose Blue ASK-END-5f270568"

    def setUp(self):
        super().setUp()
        import json
        import os
        raw = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                "test_fixture_eli94_ask_rebuild.json")).read()
        for real, test_id in self.REAL.items():
            raw = raw.replace(real, test_id)
        self.history = json.loads(raw)
        closed = max(m.get("timestamp") or 0 for m in self.history["messages"])
        self.updated_at = closed // 1000 - 30          # the cut, during the answer run
        self.row["updatedAt"] = closed + 40
        self.row["lastRunId"] = next(
            m["idempotencyKey"][len("cli-assistant:"):] for m in reversed(self.history["messages"])
            if str(m.get("idempotencyKey", "")).startswith("cli-assistant:"))
        card = openclaw_pipe._ask_user_detail_block(
            "\n".join([openclaw_pipe._USER_INPUT_TRIGGER_PREFIXES[-1], "Colour 390661",
                       "Which colour do you prefer?", "1. Red", "2. Blue", "3. Green"]), "Blue")
        held = []
        for n in range(4):
            held += [{"type": "function_call", "id": f"h{n}", "call_id": f"h{n}", "name": "Bash",
                      "arguments": "{}", "status": "completed"},
                     {"type": "function_call_output", "call_id": f"h{n}", "output": [],
                      "status": "completed"}]
        held.insert(4, {"type": "message", "role": "assistant", "id": "o1", "status": "completed",
                        "content": [{"type": "output_text", "text": card}]})
        self.msg().update({"content": card, "output": held})

    def _texts(self, output):
        return "".join("".join(p.get("text", "") for p in o.get("content") or [])
                       for o in output if o.get("type") == "message")

    async def test_the_answered_question_survives_the_rebuild(self):
        await self.startup()
        m = self.msg()
        self.assertEqual(_cards(m["output"]), 6)
        for where, text in (("content", m["content"]), ("items", self._texts(m["output"]))):
            with self.subTest(where=where):
                self.assertIn("Ask User", text, "the answered card is gone")
                self.assertIn('result="Blue"', text)
                self.assertNotIn("needs input", text.lower(), "the raw prompt is shown")
                self.assertTrue(text.rstrip().endswith(self.END))
        self.assertEqual(m["content"].strip(), self._texts(m["output"]).strip(),
                         "content and the items disagree")
        self.assertLess(m["content"].index("Ask User"), m["content"].index(self.END))

    def test_each_runs_closing_text_is_rendered_in_place(self):
        r = openclaw_pipe._history_renderer(
            [x for x in self.history["messages"] if x.get("role") == "assistant"])
        self.assertIn("Which colour do you prefer?", r.text, "the question run's text is lost")
        self.assertTrue(r.text.endswith(self.END))
        kinds = [o["type"] for o in r.finished_output() if o["type"] != "function_call_output"]
        self.assertEqual(kinds, ["function_call"] * 2 + ["message"] + ["function_call"] * 4 + ["message"])


class _FakeUvicornServer:
    """uvicorn.Server's shutdown surface (0.51): `handle_exit` is installed as
    the SIGTERM/SIGINT handler while it serves and sets `should_exit`."""

    def __init__(self):
        self.should_exit = False

    def handle_exit(self, sig, frame):
        self.should_exit = True


class ShutdownCancelTests(unittest.IsolatedAsyncioTestCase):
    """ELI-94 live, 2026-10-14 05:37 (round 21, eli94 r2): `ha apps stop`
    cancelled both open replies at 05:37:15; the page could not answer the
    cancel intent, and the 10 s fallback aborted both runs at 05:37:25 while
    Open WebUI was still draining its cancelled tasks. The run then "ended
    while down" with only its first step. A shutdown cancel is not a Stop: the
    run keeps going and the next process's startup reconcile finishes it."""

    def setUp(self):
        openclaw_pipe._UVICORN_SERVER = None
        self.addCleanup(setattr, openclaw_pipe, "_UVICORN_SERVER", None)

    def _conn(self):
        conn = _GatewayConnection(lambda: None)
        calls = []

        async def fake_abort(session_key, run_id):
            calls.append(("abort", session_key, run_id))

        async def fake_stop(session_key):
            calls.append(("stop", session_key))

        conn.abort = fake_abort
        conn.send_stop = fake_stop
        return conn, calls

    def _serve(self):
        """Install a server's handler the way uvicorn's capture_signals does;
        returns the server and a callable that restores the old handler."""
        import signal
        server = _FakeUvicornServer()
        old = signal.signal(signal.SIGTERM, server.handle_exit)
        restore = lambda: signal.signal(signal.SIGTERM, old)  # noqa: E731
        self.addCleanup(restore)
        return server, restore

    async def test_a_cancel_during_shutdown_leaves_the_run_running(self):
        server, restore = self._serve()
        openclaw_pipe.remember_owui_server()   # the pipe loaded while serving
        conn, calls = self._conn()
        q = conn.register_consumer("s", "run-1")
        server.handle_exit(15, None)           # SIGTERM: ha apps stop
        restore()                              # uvicorn restores it before cancelling tasks
        conn.park_cancelled_turn("s", "run-1", q, [], 0.05, send_stop=True)
        await asyncio.sleep(0.15)
        self.assertEqual(calls, [], "a shutdown is not a Stop: no chat.abort, no /stop")
        self.assertEqual(conn._consumers, {}, "the dying process releases its queue")
        self.assertIsNone(await conn.claim_cancelled_turn("s"))

    async def test_a_cancel_while_serving_still_stops_after_grace(self):
        self._serve()
        openclaw_pipe.remember_owui_server()
        conn, calls = self._conn()
        q = conn.register_consumer("s", "run-1")
        conn.park_cancelled_turn("s", "run-1", q, [], 0.05, send_stop=True)
        await asyncio.sleep(0.15)
        self.assertEqual(calls, [("abort", "s", "run-1"), ("stop", "s")])

    def test_the_server_is_kept_after_its_handler_is_restored(self):
        server, restore = self._serve()
        openclaw_pipe.remember_owui_server()
        restore()
        openclaw_pipe.remember_owui_server()   # a later call must not drop it
        server.should_exit = True
        self.assertTrue(openclaw_pipe.owui_shutting_down())

    def test_no_uvicorn_handler_means_not_shutting_down(self):
        import signal
        old = signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        self.assertFalse(openclaw_pipe.owui_shutting_down())


if __name__ == "__main__":
    unittest.main()
