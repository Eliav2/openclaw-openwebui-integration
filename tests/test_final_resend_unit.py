"""A finished reply is sent once more a moment after its end, for a tab that was
loading the chat then. Open WebUI 0.13.5 joins a loading tab to the chat's room
only once its load is answered, so a tab whose load read the chat before the
final save missed the end and kept the reply running (ELI-538)."""
import copy
import sys
from unittest import mock

from test_pipe_unit import _TurnRenderer, _TurnWriter, _TurnWriterCase, _delta

import openclaw_pipe


class FinalResendTests(_TurnWriterCase):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.conn.session_history = mock.AsyncMock(return_value={"messages": []})
        p = mock.patch.object(openclaw_pipe, "FINAL_RESEND_DELAY_S", 0, create=True)
        p.start()
        self.addCleanup(p.stop)
        self.extra_tasks = []

        async def list_task_ids_by_item_id(redis, item_id):
            running = [t for t, (item, fut) in self.owui.tasks.items() if item == item_id and not fut.done()]
            return running + [t for t, item in self.extra_tasks if item == item_id]
        sys.modules["open_webui.tasks"].list_task_ids_by_item_id = list_task_ids_by_item_id

    async def taken_over(self):
        shown = _TurnRenderer()
        shown.feed(_delta("slow-1 slow-2"))
        await self.owui.end_request("m1", copy.deepcopy(shown.output))
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id="agent-run-1", message_id="m1", takeover=True, renderer=shown)
        await w.feed(None)
        await w.feed(_delta(" slow-3"))
        return w

    async def settle(self, w, repeated=True):
        """Wait for the end's repeat; `repeated`: the writer scheduled one."""
        resend = getattr(w, "_final_resend", None)
        if repeated:
            self.assertIsNotNone(resend, "the end is repeated")
        if resend is not None:
            await resend

    def finished_pushes(self):
        return [(i, d) for i, (e, d, room) in enumerate(self.owui.emits)
                if e == "events" and d["data"]["type"] == "chat:messages"
                and d["data"]["data"]["messages"].get("m1", {}).get("done")]

    def idles(self):
        return [i for i, (e, d, room) in enumerate(self.owui.emits)
                if e == "events" and d["data"]["type"] == "chat:active" and d["data"]["data"] == {"active": False}]

    async def test_the_end_is_sent_once_more_after_the_final_save(self):
        w = await self.taken_over()
        await w.finalize()
        self.assertEqual(len(self.finished_pushes()), 1, "the end itself, before the repeat")
        await self.settle(w)
        (save, at, saved), = [c for c in self.owui.cleared if c[0] == w.task_id]
        self.assertEqual((saved["m1"]["content"], saved["m1"]["done"]), ("slow-1 slow-2 slow-3", True))
        pushes = self.finished_pushes()
        self.assertEqual(len(pushes), 2, "one repeat, no more")
        (first, _), (again, repeat) = pushes
        self.assertGreater(again, at, "only after the final save")
        m = repeat["data"]["data"]["messages"]["m1"]
        self.assertEqual((m["content"], m["done"]), ("slow-1 slow-2 slow-3", True))
        self.assertEqual(set(repeat["data"]["data"]["messages"]), {"m1"}, "no parent link")
        self.assertIsNone(repeat["data"]["data"]["currentId"], "moves no view")
        idles = self.idles()
        self.assertEqual(len(idles), 2, "the chat idle at the end and once more")
        self.assertGreater(idles[-1], first, "the repeat's idle comes after the end")
        self.assertLess(idles[-1], again, "idle before the repeated message: a tab holding stale task ids "
                                          "keeps its own copy over a push")

    async def test_no_repeat_when_the_final_save_failed(self):
        w = await self.taken_over()
        chats = self.owui._chats
        real = chats.upsert_message_to_chat_by_id_and_message_id

        async def upsert(chat_id, message_id, message):
            if "statusHistory" in message:       # the final save
                return None
            return await real(chat_id, message_id, message)
        with mock.patch.object(chats, "upsert_message_to_chat_by_id_and_message_id", upsert):
            await w.finalize()
        self.assertIsNone(getattr(w, "_final_resend", None))
        self.assertEqual(self.finished_pushes(), [])

    async def test_no_repeat_when_the_chat_runs_a_task_by_then(self):
        """A new message's reply runs: a repeated idle would end it in the tab."""
        w = await self.taken_over()
        await w.finalize()
        self.extra_tasks.append(("task-new", self.CHAT))
        await self.settle(w, repeated=False)
        self.assertEqual(len(self.finished_pushes()), 1)
        self.assertEqual(len(self.idles()), 1)

    async def test_no_repeat_when_a_newer_run_started(self):
        w = await self.taken_over()
        await w.finalize()
        w.newer_run = True
        await self.settle(w, repeated=False)
        self.assertEqual(len(self.finished_pushes()), 1)

    async def test_no_repeat_while_another_writer_writes_in_the_chat(self):
        w = await self.taken_over()
        await w.finalize()
        other = self.writer(run="agent-run-2", message_id="m2")
        self.conn._turn_writers[(self.CHAT, "m2")] = other
        self.addCleanup(self.conn._turn_writers.pop, (self.CHAT, "m2"), None)
        await self.settle(w, repeated=False)
        self.assertEqual(len(self.finished_pushes()), 1)
