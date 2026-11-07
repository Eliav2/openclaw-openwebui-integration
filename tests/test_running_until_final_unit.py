"""ELI-377: a reply the turn writer took over reads running in a tab until it is
finalized. Its request's end (a cancel: Send now, or a message from another tab)
marks it done in the tab; the writer pushes it back to running, so a tab never
shows it done before its final write, whose status lines (the usage line) a
reload would show (ELI-371)."""
import asyncio
import copy

from test_pipe_unit import _TurnRenderer, _TurnWriter, _TurnWriterCase, _delta


class RunningUntilFinalTests(_TurnWriterCase):

    async def taken_over(self, request=None):
        shown = _TurnRenderer()
        shown.feed(_delta("E2E_HOLD_START"))
        await self.owui.end_request("m1", copy.deepcopy(shown.output))
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id="agent-run-1", message_id="m1", takeover=True, renderer=shown,
                        request=request)
        await w.feed(None)
        return w

    def pushed(self):
        """Each push of m1 to the chat's tabs: (content, done)."""
        return [(e["data"]["messages"]["m1"]["content"], e["data"]["messages"]["m1"]["done"])
                for e in self.owui.events("chat:messages") if "m1" in e["data"].get("messages", {})]

    async def test_a_taken_over_reply_is_pushed_running_after_its_request_ends(self):
        w = await self.taken_over()
        await w.show_running()
        self.assertEqual(self.pushed()[-1], ("E2E_HOLD_START", False))
        await w.feed(_delta(" more"))
        await w.finalize()
        self.assertEqual(self.pushed()[-1], ("E2E_HOLD_START more", True),
                         "then finished, with its final lines")

    async def test_the_running_push_waits_for_the_request_s_end(self):
        """The request's end is what marks it done in a tab: a push before it would be undone."""
        request = asyncio.get_running_loop().create_future()
        w = await self.taken_over(request)
        pushing = asyncio.ensure_future(w.show_running())
        await asyncio.sleep(0)
        self.assertNotIn(("E2E_HOLD_START", False), self.pushed())
        request.set_result(None)
        await pushing
        self.assertEqual(self.pushed()[-1], ("E2E_HOLD_START", False))

    async def test_a_reply_finalized_first_is_never_pushed_running_again(self):
        request = asyncio.get_running_loop().create_future()
        w = await self.taken_over(request)
        pushing = asyncio.ensure_future(w.show_running())
        await asyncio.sleep(0)
        request.set_result(None)
        await w.finalize()
        await pushing
        self.assertTrue(self.pushed()[-1][1], f"the last push must be the finished one: {self.pushed()}")
