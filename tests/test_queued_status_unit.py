"""ELI-511: a "Queued"/"Waiting" status line gives way once the reply's run starts.

OpenClaw 2026.11.5 reports every send `status: "queued"` from admission until
it runs: the row is `{hasActiveRun: active, status: state.status ?? "running"}`
(dist/session-row-prepared-read-*.mjs `projectGatewaySessionActiveRun`), and
`resolveVisibleActiveSessionRunState` (dist/session-active-runs-*.mjs) gives a
reply operation `"queued"` until `markReplyOperationExecutionStarted`. The
`sessions.changed` push carrying it lands before the run's first event, so the
Waiting line it raised stayed up for the whole run.
"""
import asyncio

from test_pipe_unit import _TurnCase

WAITING = "Waiting -- your message is queued behind an active response..."
QUEUED = "Queued: waiting for the current run to finish…"
THINKING = "Thinking..."


class QueuedStatusReplacedTests(_TurnCase):
    SK = None

    def _statuses(self):
        return [e["data"]["description"] for e in self.emitted if e.get("type") == "status"]

    def _admission_push(self):
        """The 2026.11.5 row of a run admitted but not yet executing."""
        self.conn._on_sessions_changed({"sessionKey": self.SK, "reason": "send",
                                        "hasActiveRun": True, "status": "queued"})

    def _lifecycle_start(self, run_id):
        return {"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": run_id, "stream": "lifecycle",
            "data": {"phase": "start"}}}

    async def _finish(self, gen, first, run_id):
        q = self._queue(run_id)
        q.put_nowait(self._final(run_id))
        out = await asyncio.wait_for(first, 2)
        async for chunk in gen:
            out += chunk if isinstance(chunk, str) else ""
        return out

    async def _spin(self):
        for _ in range(50):
            await asyncio.sleep(0)

    async def test_the_admission_queued_row_gives_way_when_the_run_starts(self):
        """The row says `queued` before the run's first event: Waiting shows, and the run's
        first event replaces it with the running line, before any text."""
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        self._admission_push()
        await self._spin()
        self.assertEqual(self._statuses()[-1], WAITING)

        q = self._queue("run-1")
        q.put_nowait(self._lifecycle_start("run-1"))
        await self._spin()
        self.assertEqual(self._statuses()[-1], THINKING, self._statuses())

        q.put_nowait(self._delta("run-1", "hi there"))
        out = await self._finish(gen, first, "run-1")
        self.assertIn("hi there", out)
        statuses = self._statuses()
        self.assertEqual(statuses.count(WAITING), 1, statuses)
        self.assertLess(statuses.index(WAITING), len(statuses) - statuses[::-1].index(THINKING) - 1,
                        statuses)

    async def test_a_queued_row_after_the_first_event_shows_nothing(self):
        """Once the run's events flow, a late `queued` row raises no Waiting line."""
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._lifecycle_start("run-1"))
        await self._spin()
        self._admission_push()
        await self._spin()
        await self._finish(gen, first, "run-1")
        self.assertNotIn(WAITING, self._statuses())

    async def test_a_message_really_queued_shows_queued_until_its_run_starts(self):
        """A message sent while the chat's run works waits in the Pipe with the Queued line;
        that line stays through the send, and the queued run's first event replaces it."""
        self.active = True
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._spin()
        self.assertEqual(self.sent, [])
        self.assertEqual(self._statuses()[-1], QUEUED)

        self.active = False
        self.conn._on_sessions_changed({"sessionKey": self.SK, "hasActiveRun": False,
                                        "reason": "chat.run.settled"})
        await self._until_registered("run-1")
        self._admission_push()
        await self._spin()
        self.assertIn(self._statuses()[-1], (QUEUED, WAITING), "not started yet: still waiting")

        q = self._queue("run-1")
        q.put_nowait(self._lifecycle_start("run-1"))
        await self._spin()
        self.assertEqual(self._statuses()[-1], THINKING, self._statuses())
        q.put_nowait(self._delta("run-1", "hi there"))
        self.assertIn("hi there", await self._finish(gen, first, "run-1"))

    async def test_a_reply_never_queued_shows_no_extra_line(self):
        """No queued row and no wait: the run's first event emits nothing new."""
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._lifecycle_start("run-1"))
        q.put_nowait(self._delta("run-1", "hi there"))
        await self._finish(gen, first, "run-1")
        statuses = self._statuses()
        self.assertNotIn(WAITING, statuses)
        self.assertNotIn(QUEUED, statuses)
        self.assertEqual(statuses.count(THINKING), 1, statuses)
