"""ELI-513: the startup ended-while-down pass rebuilt only what closed while Open WebUI was down.

2026-11-18 09:32 (first Open WebUI start on 2026.11.5): chats 87b1f8a0 and ceb0550b got
cron results from 2026-10-25 22:47 and 2026-11-18 07:14 as new messages stamped 09:32,
with a push. 2026.11.5 shows the `cron-current-completion` records 2026.10.10 wrote for
isolated cron runs as `automation-result` messages; the 2026.10.10 Pipe never wrote them
(no run in the chat's session). The pass took "committed after the chat's last write" for
"committed while Open WebUI was down", which every idle chat's old result is.
"""
import time
import types
from unittest import mock

import openclaw_pipe
from test_pipe_unit import _RoutingCase, turn_message_id

DAY_S = 86_400


class EndedWhileDownCronResultTests(_RoutingCase):
    JOB = "811ae3e9-ddbb-4e47-b95e-b8a5c5771ad2"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.conn.gateway_version = "2026.11.5"
        now = int(time.time())
        # The chat's last Open WebUI write, weeks ago.
        self.chat_written = now - 30 * DAY_S
        self.owui.chat.updated_at = self.chat_written
        self.owui.chat.user_id = self.USER
        self.owui._chats.get_chat_by_id = staticmethod(self._get_chat)
        self.session_updated_ms = (now - 60) * 1000
        test = self
        plain = self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "sessions.list":
                return {"sessions": [{"key": test.key, "hasActiveRun": False, "sessionId": "s1",
                                      "updatedAt": test.session_updated_ms}], "totalCount": 1}
            return await plain(method, params, timeout)
        self.conn.send_request = send_request
        self.pushed = mock.AsyncMock()

    async def _get_chat(self, chat_id):
        return self.owui.chat

    def result(self, text, committed_s):
        start = (committed_s - 400) * 1000
        cron_run = f"cron:{self.JOB}:{start}"
        self.cron_run = cron_run
        return {"role": "assistant", "content": [{"type": "text", "text": text}],
                "api": "openclaw-transcript", "provider": "openclaw", "model": "automation-result",
                "stopReason": "stop", "timestamp": committed_s * 1000,
                "idempotencyKey": f"cron-current-completion:{cron_run}",
                "openclawAutomation": {"kind": "cron", "jobId": self.JOB, "runId": cron_run}}

    async def start_owui(self, last_reply_s):
        """Open WebUI starts: the startup pass, its last finished reply at `last_reply_s`."""
        async def get_conn(_valves):
            return self.conn
        valves = lambda: types.SimpleNamespace(AGENT_ID="main", PUBLISH_CHAT_FINISHED=True)  # noqa: E731
        self.conn._valves = valves
        with mock.patch.object(openclaw_pipe, "_get_gateway_connection", get_conn), \
                mock.patch.object(openclaw_pipe, "_orphan_stub_ids", mock.AsyncMock(return_value=[])), \
                mock.patch.object(openclaw_pipe, "_drop_chat_finished_claims_of_deleted_replies",
                                  mock.AsyncMock()), \
                mock.patch.object(openclaw_pipe, "_owui_last_reply_at",
                                  mock.AsyncMock(return_value=last_reply_s), create=True), \
                mock.patch.object(openclaw_pipe, "_owui_chat_has_live_task", mock.AsyncMock(return_value=False)), \
                mock.patch.object(openclaw_pipe, "_publish_chat_finished", self.pushed), \
                mock.patch.object(openclaw_pipe, "_relay_emit_event", mock.AsyncMock()):
            await openclaw_pipe._reconcile_orphan_stubs(valves)
        await self.spin()

    def written(self):
        return self.new_messages().get(turn_message_id(self.key, self.cron_run, self.CHAT))

    async def test_a_result_committed_while_open_webui_was_up_is_not_written_at_restart(self):
        """The 87b1f8a0 case: committed 25 days ago, Open WebUI wrote replies for weeks after
        it, then restarted. Nothing to rebuild: no new message and no push."""
        now = int(time.time())
        self.history = {"messages": [self.result("The sync is running on the Pi.", now - 25 * DAY_S)]}
        await self.start_owui(last_reply_s=now - 120)
        self.assertIsNone(self.written(), self.new_messages())
        self.pushed.assert_not_awaited()

    async def test_a_result_committed_while_open_webui_was_down_is_written_at_its_own_time(self):
        """Committed after Open WebUI's last reply: it ended while down. Written, dated
        when the run committed it."""
        now = int(time.time())
        committed = now - 90
        self.history = {"messages": [self.result("Both runs are green.", committed)]}
        await self.start_owui(last_reply_s=now - 120)
        m = self.written()
        self.assertIsNotNone(m, self.new_messages())
        self.assertEqual((m["content"].strip(), m["done"], m["timestamp"]),
                         ("Both runs are green.", True, committed))

    async def test_without_a_known_last_reply_the_chats_own_write_decides(self):
        """Open WebUI's last reply unreadable: the chat's last write is the bound, as before."""
        now = int(time.time())
        committed = now - 90
        self.history = {"messages": [self.result("Both runs are green.", committed)]}
        await self.start_owui(last_reply_s=None)
        self.assertIsNotNone(self.written(), self.new_messages())
