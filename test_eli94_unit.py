"""ELI-94: a run that ended while Open WebUI was stopped leaves its reply partial.

A graceful Open WebUI stop cancels the request that streams a reply and saves
the message `done: true` with what it had so far. The run keeps going in the
Gateway and closes while Open WebUI is down, so no event reaches the next
process and the startup reconcile (ELI-90) only looks at `done: false`
messages. DESIGN-agent-turns.md case 4: the session's `updatedAt` is newer
than the chat's `updated_at`, so `chat.history` is read and a run that closed
after the chat was last written gets its message rebuilt from the transcript.

Run: python3 -m pytest -q test_eli94_unit.py
"""
import asyncio
import copy
import sys
import types
import unittest
from unittest import mock

import test_pipe_unit  # noqa: F401  (installs the module stubs openclaw_pipe needs)
from test_pipe_unit import _turn_history

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


class EndedWhileDownTests(unittest.IsolatedAsyncioTestCase):

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
        socket_main = types.ModuleType("open_webui.socket.main")
        self.sio = mock.AsyncMock()
        socket_main.sio = self.sio
        modules = {"open_webui": types.ModuleType("open_webui"),
                   "open_webui.models": types.ModuleType("open_webui.models"),
                   "open_webui.models.chats": chats_mod,
                   "open_webui.socket": types.ModuleType("open_webui.socket"),
                   "open_webui.socket.main": socket_main}
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

    async def test_a_missing_chat_is_skipped(self):
        self.row["key"] = _owui_session_key("main", USER, "55555555-5555-5555-5555-555555555555")
        await self.startup()
        self.assertEqual(self.history_reads, 0)


if __name__ == "__main__":
    unittest.main()
