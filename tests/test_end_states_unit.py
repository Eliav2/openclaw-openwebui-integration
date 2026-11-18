"""ELI-512: a reply's end as OpenClaw 2026.11.5 reports it.

Rows captured on the live 2026.11.5 Gateway (W5, projects/qa-explorer/run/eli512):
- Stop from another surface (`sessions.abort`, T5b): `running` -> `{status: null,
  hasActiveRun: false, lastRunId: null}` -> about 1.4 s later `killed`, `abortedLastRun: true`.
- Stop in Open WebUI (T3b): `running` -> `killed`, `abortedLastRun: true`.
The run status enum is queued, running, done, failed, interrupted, killed, timeout
(dist/sessions-row-*.mjs SessionRunStatusSchema): no `cancelled`.
"""
import asyncio
from unittest import mock

import openclaw_pipe
from test_pipe_unit import _TurnCase

NO_TEXT = "_(This turn produced no reply text.)_"


class TextlessEndTests(_TurnCase):
    SK = None

    def _aborted_final(self, run_id):
        return {"type": "event", "event": "chat", "payload": {
            "sessionKey": self.SK, "runId": run_id, "state": "aborted"}}

    def _lifecycle(self, run_id, phase):
        return {"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": run_id, "stream": "lifecycle", "data": {"phase": phase}}}

    async def _textless(self, ending, row_status):
        """A run that ends with no text by `ending` (events), its row then at `row_status`."""
        self.history = {"messages": []}
        self.describe_extra = {"status": row_status}
        gen = self._reply("hello", "m1")
        out = ""
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._lifecycle("run-1", "start"))
        for event in ending:
            q.put_nowait(event)
        chunk = await asyncio.wait_for(first, 5)
        out += chunk if isinstance(chunk, str) else ""
        async for chunk in gen:
            out += chunk if isinstance(chunk, str) else ""
        return out

    async def test_a_stop_from_another_surface_says_stopped(self):
        """T5b: the run's own end says aborted while the row reads `status: null` (the moment
        before `killed`): the reply says Stopped, not that it produced no text."""
        out = await self._textless([self._aborted_final("run-1")], None)
        self.assertIn(openclaw_pipe.STOPPED_NOTE, out)
        self.assertNotIn(NO_TEXT, out)

    async def test_a_killed_row_says_stopped(self):
        out = await self._textless([self._final("run-1")], "killed")
        self.assertIn(openclaw_pipe.STOPPED_NOTE, out)
        self.assertNotIn(NO_TEXT, out)

    async def test_a_timed_out_run_says_it_timed_out(self):
        out = await self._textless([self._final("run-1")], "timeout")
        self.assertIn("**The response timed out.**", out)
        self.assertNotIn(NO_TEXT, out)

    async def test_a_failed_run_still_says_it_failed(self):
        out = await self._textless([self._final("run-1")], "failed")
        self.assertIn("**The response failed.**", out)

    async def test_a_done_run_with_no_text_still_says_so(self):
        out = await self._textless([self._final("run-1")], "done")
        self.assertIn(NO_TEXT, out)


class StoppedStatusLineTests(_TurnCase):
    """P2-1: a reply stopped in Open WebUI ends on its "Stopped" line. Open WebUI shows the last
    status line; an empty closing line after it left the bubble saying nothing."""
    SK = None

    async def test_a_stopped_reply_ends_on_its_stopped_line(self):
        self.pipe.valves.INTERRUPT_GRACE_SECONDS = 30
        r1 = self._reply("count", "m1", intent="stop")
        first = asyncio.ensure_future(r1.__anext__())
        await self._until_registered("run-1")
        self.active = True
        self._queue("run-1").put_nowait(self._delta("run-1", "1\n"))

        async def mark(chat_id, message_id, request=None):
            return None
        with mock.patch.object(openclaw_pipe, "_mark_stopped", mark):
            await first
            await r1.aclose()
            await asyncio.sleep(0.05)
        statuses = [e["data"] for e in self.emitted if e.get("type") == "status"]
        self.assertEqual(statuses[-1], {"description": "Stopped", "done": True}, statuses)
