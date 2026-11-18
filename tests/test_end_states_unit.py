"""ELI-512: a reply's end as OpenClaw 2026.11.5 reports it.

Rows captured on the live 2026.11.5 Gateway (W5, projects/qa-explorer/run/eli512):
- Stop from another surface (`sessions.abort`, T5b): `running` -> `{status: null,
  hasActiveRun: false, lastRunId: null}` -> about 1.4 s later `killed`, `abortedLastRun: true`.
- Stop in Open WebUI (T3b): `running` -> `killed`, `abortedLastRun: true`.
The run status enum is queued, running, done, failed, interrupted, killed, timeout
(dist/sessions-row-*.mjs SessionRunStatusSchema): no `cancelled`.
"""
import asyncio
import unittest
from unittest import mock

import openclaw_pipe
import test_pipe_unit
from test_pipe_unit import _TurnCase

NO_TEXT = "_(This turn produced no reply text.)_"


class TextlessEndTests(_TurnCase):
    SK = None

    def _aborted_final(self, run_id, **extra):
        return {"type": "event", "event": "chat", "payload": {
            "sessionKey": self.SK, "runId": run_id, "state": "aborted", **extra}}

    def _lifecycle(self, run_id, phase):
        return {"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": run_id, "stream": "lifecycle", "data": {"phase": phase}}}

    async def _textless(self, ending, row_status, last_run="run-1"):
        """A run that ends with no text by `ending` (events), its row then at `row_status`
        with `last_run` its last run."""
        self.history = {"messages": []}
        self.describe_extra = {"status": row_status, "lastRunId": last_run}
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
        self.assertIn(openclaw_pipe.ui_text("stopped_reason"), out)
        self.assertNotIn(NO_TEXT, out)

    async def test_a_killed_row_says_stopped(self):
        out = await self._textless([self._final("run-1")], "killed")
        self.assertIn(openclaw_pipe.ui_text("stopped_reason"), out)
        self.assertNotIn(NO_TEXT, out)

    async def test_a_timed_out_run_says_it_timed_out(self):
        out = await self._textless([self._final("run-1")], "timeout")
        self.assertIn("**The response timed out.**", out)
        self.assertNotIn(NO_TEXT, out)

    async def test_a_gateway_deadline_says_timed_out_not_stopped(self):
        """2026.11.5 ends a run past its deadline as `aborted` with `stopReason: "timeout"`
        (gateway/chat-abort.ts)."""
        out = await self._textless([self._aborted_final("run-1", stopReason="timeout")], None)
        self.assertIn("**The response timed out.**", out)
        self.assertNotIn(openclaw_pipe.ui_text("stopped_reason"), out)

    async def test_another_runs_row_does_not_say_how_this_one_ended(self):
        """Run A ends with no text; the row read after it is a later run B's timeout."""
        out = await self._textless([self._final("run-1")], "timeout", last_run="run-2")
        self.assertNotIn("timed out", out)
        self.assertIn(NO_TEXT, out)

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

    async def test_a_stop_whose_snapshot_fails_still_says_stopped(self):
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
            with mock.patch.object(openclaw_pipe._PipeTurn, "maybe_emit_snapshot",
                                   mock.AsyncMock(side_effect=RuntimeError("socket gone"))):
                await r1.aclose()
            await asyncio.sleep(0.05)
        statuses = [e["data"] for e in self.emitted if e.get("type") == "status"]
        self.assertEqual(statuses[-1], {"description": "Stopped", "done": True}, statuses)


class RunEndStateTests(unittest.IsolatedAsyncioTestCase):
    """What a reply knows of its run's end, read on a bare turn."""

    def _turn(self, **attrs):
        turn = object.__new__(openclaw_pipe._PipeTurn)
        conn = mock.Mock()
        conn.session_history = mock.AsyncMock(return_value={"messages": []})
        for name, value in {"conn": conn, "session_key": "agent:main:openwebui-u1-c1",
                            "our_run_id": "run-1", "renderer": openclaw_pipe._TurnRenderer(),
                            "text_yielded": False, "lang": "en", "text": "hello",
                            "first_event_arrived": True, "aborted_run_id": None,
                            "abort_stop_reason": None, **attrs}.items():
            setattr(turn, name, value)
        return turn

    async def test_a_stop_of_the_run_it_handed_over_from_is_not_this_runs(self):
        """The owner rule switches the reply to a replacement run (`our_run_id`): the first
        run's abort no longer ends the reply as Stopped."""
        turn = self._turn(aborted_run_id="run-1")
        self.assertEqual(turn.run_end_note(), openclaw_pipe.ui_text("stopped_reason"))
        turn.our_run_id = "run-2"
        self.assertFalse(turn.run_aborted)
        self.assertIsNone(turn.run_end_note())

    async def test_a_settled_timeout_row_of_this_run_says_it_timed_out(self):
        """The final never reached the reply; its row says `timeout` with no error."""
        tail = await self._turn().settled_tail({"status": "timeout", "lastRunId": "run-1",
                                                "hasActiveRun": False})
        self.assertIn("**The response timed out.**", tail)

    async def test_a_stopped_run_is_not_answered_by_a_later_run_to_the_same_words(self):
        """Recovery by user text could return run B's answer to the same prompt."""
        turn = self._turn(aborted_run_id="run-1")
        with mock.patch.object(openclaw_pipe, "_recover_turn_text",
                               mock.AsyncMock(return_value="run B's answer")):
            self.assertIsNone(await turn.recover_from_transcript())


class FollowUpIsNoStopTests(_TurnCase):
    """P1-6 (ELI-512): Open WebUI cancels reply 1 the same way for Stop and for a message sent
    while it works. Only a stopped run says "Stopped"; a follow-up that claims the run leaves
    reply 1 as it was, and its run goes on in reply 2."""
    SK = None
    _hold_parked_timers = test_pipe_unit.NoDroppedMessagesTests._hold_parked_timers
    _read = test_pipe_unit.NoDroppedMessagesTests._read
    _spin = staticmethod(test_pipe_unit.NoDroppedMessagesTests._spin)

    def _stopped_lines(self):
        return [e["data"] for e in self.emitted if e.get("type") == "status"
                and e["data"].get("description") == openclaw_pipe.ui_text("stopped")]

    async def test_a_message_sent_while_the_run_works_does_not_stop_reply_one(self):
        armed = self._hold_parked_timers()
        r1 = self._reply("count to 3", "m1", intent="send_now")
        first = asyncio.ensure_future(r1.__anext__())
        await self._until_registered("run-1")
        self.active = True
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "1\n"))
        await first
        await r1.aclose()           # Open WebUI cancels reply 1 for the follow-up
        await self._spin()

        r2 = self._reply("and count in Hebrew", "m2")
        q.put_nowait(self._delta("run-1", "2\n"))
        out2 = await self._read(r2, "2\n")
        q.put_nowait(self._final("run-1"))
        self.active = False
        await r2.aclose()
        await self._spin()

        self.assertIn("2", out2, "reply 2 continues the run")
        self.assertNotIn("abort", self.rpc)
        self.assertEqual(armed[:1], [0.3])
        self.assertEqual(self._stopped_lines(), [])

    async def test_a_cancel_no_follow_up_claims_ends_stopped(self):
        """No page answer and no next message within the window: the run is stopped, and the
        reply says so."""
        self.pipe.valves.INTERRUPT_GRACE_SECONDS = 0.05
        r1 = self._reply("count", "m1")
        first = asyncio.ensure_future(r1.__anext__())
        await self._until_registered("run-1")
        self.active = True
        self._queue("run-1").put_nowait(self._delta("run-1", "1\n"))
        await first
        await r1.aclose()
        for _ in range(200):        # bounded: the window is 0.05 s
            if self._stopped_lines():
                break
            await asyncio.sleep(0.01)  # allow-sleep: the fallback window's own timer
        self.assertEqual(self._stopped_lines(), [{"description": openclaw_pipe.ui_text("stopped"),
                                                  "done": True}])
        statuses = [e["data"] for e in self.emitted if e.get("type") == "status"]
        self.assertEqual(statuses[-1]["description"], openclaw_pipe.ui_text("stopped"), statuses)
