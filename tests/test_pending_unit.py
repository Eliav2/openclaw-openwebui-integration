"""ELI-504: a message sent while the Gateway is down is kept in the chat and delivered when it is back.

The state lives in Open WebUI's DB: the empty reply carries `meta.delivery`, and the chat's
`meta.openclaw_pending` is the ordered list of the message ids waiting. The fake chat
transaction below holds a real lock and yields between read and commit, so claims interleave."""
import asyncio
import contextlib
import copy
import os
import socket
import sys
import time
import types
import unittest
from unittest import mock

import test_pipe_unit  # noqa: F401  (installs the module stubs openclaw_pipe needs)
from _test_helpers import owui_modules

import openclaw_pipe
from openclaw_pipe import (
    GATEWAY_REFUSED_CODE, GatewayError, Pipe, _run_id_sent_at, parse_pipe_run_id, pipe_run_id,
)

CHAT, USER = "22222222-2222-2222-2222-222222222222", "u1"
M1, M2 = "33333333-3333-3333-3333-333333333331", "33333333-3333-3333-3333-333333333332"
PAYLOAD = {"body": {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "hi"}]},
           "metadata": {"chat_id": CHAT, "message_id": M1, "user_id": USER}, "user": {}}


class RetryableTests(unittest.TestCase):
    def test_a_refused_or_retryable_or_unready_gateway_is_waited_for(self):
        for ex in (GatewayError("down", {"code": GATEWAY_REFUSED_CODE}),
                   GatewayError("starting", {"code": "UNAVAILABLE", "retryable": True}),
                   GatewayError("gateway connection not ready (reconnecting)",
                                {"code": openclaw_pipe.GATEWAY_NOT_READY_CODE}),
                   GatewayError("timed out", {"code": openclaw_pipe.GATEWAY_TIMEOUT_CODE})):
            self.assertTrue(openclaw_pipe.pending_retryable(ex), ex)

    def test_a_refusal_that_waiting_cannot_fix_is_shown(self):
        for ex in (GatewayError("The GATEWAY_TOKEN valve is empty."),
                   GatewayError("rejected", {"code": "INVALID_REQUEST"}),
                   asyncio.TimeoutError(), RuntimeError("boom")):
            self.assertFalse(openclaw_pipe.pending_retryable(ex), ex)


class RunIdTests(unittest.TestCase):
    def test_the_retry_key_is_the_original_one(self):
        a = pipe_run_id(CHAT, M1, at=1791849271.5)
        self.assertEqual(a, pipe_run_id(CHAT, M1, at=1791849271.5))
        self.assertNotEqual(a, pipe_run_id(CHAT, M2, at=1791849271.5))
        self.assertEqual(_run_id_sent_at(a), 1791849271.5)
        self.assertEqual(parse_pipe_run_id(a)["message"], M1)

    def test_without_a_time_it_is_still_the_clock(self):
        before = time.time()
        self.assertGreaterEqual(_run_id_sent_at(pipe_run_id(CHAT, M1)), before)


class LanguageTests(unittest.TestCase):
    def test_the_bubble_speaks_the_ui_language(self):
        he = openclaw_pipe.pending_lang({"variables": {"{{USER_LANGUAGE}}": "he-IL"}})
        self.assertEqual(he, "he")
        self.assertIn("ממתין לגייטווי", openclaw_pipe.pending_status_text(he))
        for meta in ({"variables": {"{{USER_LANGUAGE}}": "en-US"}}, {"variables": {}}, {}, None):
            self.assertEqual(openclaw_pipe.pending_lang(meta), "en")
        self.assertIn("gateway", openclaw_pipe.pending_status_text("en").lower())

    def test_the_note_names_the_original_send_time(self):
        at = time.mktime((2026, 11, 17, 7, 44, 0, 0, 0, -1))
        self.assertIn("07:44", openclaw_pipe.pending_sent_note(at, "he", now=at + 600))
        # An older one says the day too.
        self.assertIn("16/11", openclaw_pipe.pending_sent_note(at - 86400, "en", now=at))


class SentTimeTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def test_a_client_that_sends_the_time_is_believed_over_the_message_stamp(self):
        meta = {"sent_at": self.NOW - 600, "user_message": {"timestamp": self.NOW - 30}}
        self.assertEqual(openclaw_pipe.pending_sent_time(meta, self.NOW), self.NOW - 600)

    def test_without_it_the_open_webui_message_stamp_is_the_send_time(self):
        meta = {"user_message": {"timestamp": int(self.NOW) - 130}}
        self.assertEqual(openclaw_pipe.pending_sent_time(meta, self.NOW), int(self.NOW) - 130)

    def test_milliseconds_are_read_as_such(self):
        self.assertEqual(openclaw_pipe.pending_sent_time({"sent_at": (self.NOW - 60) * 1000}, self.NOW), self.NOW - 60)

    def test_with_neither_it_is_now_and_never_later_than_now(self):
        for meta in (None, {}, {"sent_at": "x"}, {"sent_at": True}, {"user_message": {"timestamp": None}}):
            self.assertEqual(openclaw_pipe.pending_sent_time(meta, self.NOW), self.NOW)
        self.assertEqual(openclaw_pipe.pending_sent_time({"sent_at": self.NOW + 3600}, self.NOW), self.NOW)


class _Db(unittest.IsolatedAsyncioTestCase):
    """A chat with two user turns and their (empty) replies, in a fake Open WebUI."""

    def setUp(self):
        openclaw_pipe._PENDING_SENT.clear()
        msgs = {"q1": {"id": "q1", "role": "user", "parentId": None, "childrenIds": [M1]},
                M1: {"id": M1, "role": "assistant", "parentId": "q1", "childrenIds": ["q2"], "done": True,
                     "content": "", "model": "openclaw_gateway.m", "meta": {}},
                "q2": {"id": "q2", "role": "user", "parentId": M1, "childrenIds": [M2]},
                M2: {"id": M2, "role": "assistant", "parentId": "q2", "childrenIds": [], "done": True,
                     "content": "", "model": "openclaw_gateway.m", "meta": {}}}
        self.chat = types.SimpleNamespace(chat={"history": {"currentId": M2, "messages": msgs}},
                                          meta={}, updated_at=0, user_id=USER)
        self.rows = {}
        lock = asyncio.Lock()
        test = self

        class Chats:
            @staticmethod
            @contextlib.asynccontextmanager
            async def _chat_transaction(chat_id):
                async with lock:
                    await asyncio.sleep(0)
                    yield ("db", test.chat if chat_id == CHAT else None)

            @staticmethod
            def upsert_message_to_history(history, mid, message):
                history["messages"][mid] = {**history["messages"].get(mid, {}), **message}
                return history["messages"][mid]

            @staticmethod
            def _clean_null_bytes(obj):
                return obj

            @staticmethod
            async def get_chat_by_id(chat_id):
                return test.chat if chat_id == CHAT else None

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, fields):
                messages = test.chat.chat["history"]["messages"]
                messages[message_id] = {**messages.get(message_id, {}), **fields}
                return messages[message_id]

        class ChatMessages:
            @staticmethod
            async def upsert_message(mid, chat_id, user_id, m, db=None):
                test.rows[mid] = copy.deepcopy(m)

        chats_mod = types.ModuleType("open_webui.models.chats")
        chats_mod.Chats = Chats
        cm_mod = types.ModuleType("open_webui.models.chat_messages")
        cm_mod.ChatMessages = ChatMessages
        attrs = types.ModuleType("sqlalchemy.orm.attributes")
        attrs.flag_modified = lambda obj, name: None
        mods = {**owui_modules(),
                "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.chats": chats_mod,
                "open_webui.models.chat_messages": cm_mod,
                "sqlalchemy": types.ModuleType("sqlalchemy"),
                "sqlalchemy.orm": types.ModuleType("sqlalchemy.orm"),
                "sqlalchemy.orm.attributes": attrs}
        p = mock.patch.dict(sys.modules, mods)
        p.start()
        self.addCleanup(p.stop)

    def msg(self, mid):
        return self.chat.chat["history"]["messages"][mid]

    def queue(self):
        return [e["id"] for e in self.chat.meta.get(openclaw_pipe.PENDING_META_KEY) or []]

    async def defer(self, mid, sent_at=100.0, armed=True):
        payload = {**PAYLOAD, "metadata": {**PAYLOAD["metadata"], "message_id": mid}}
        self.assertTrue(await openclaw_pipe._pending_enqueue(CHAT, mid, payload, sent_at))
        if armed:
            self.assertTrue(await openclaw_pipe._pending_arm(CHAT, mid))

    async def claim(self, proc="p1", now=200.0):
        return await openclaw_pipe._pending_claim_head(CHAT, proc, now)


class EnqueueTests(_Db):
    async def test_the_reply_is_marked_and_listed_in_the_chat_meta(self):
        await self.defer(M1, 100.0)
        m = self.msg(M1)
        self.assertEqual(m["meta"][openclaw_pipe.PENDING_DELIVERY], "pending")
        self.assertEqual(m["meta"][openclaw_pipe.PENDING_SENT_AT], 100.0)
        self.assertEqual(m["meta"][openclaw_pipe.PENDING_PAYLOAD]["body"], PAYLOAD["body"])
        self.assertIs(m["done"], False)  # armed: the reply waits
        self.assertEqual(self.queue(), [M1])
        self.assertEqual(self.rows[M1]["meta"][openclaw_pipe.PENDING_DELIVERY], "pending")

    async def test_listing_twice_keeps_one_entry_in_arrival_order(self):
        await self.defer(M1)
        await self.defer(M2, 101.0)
        await self.defer(M1)
        self.assertEqual(self.queue(), [M1, M2])

    async def test_a_chat_that_is_not_saved_keeps_the_error(self):
        self.assertFalse(await openclaw_pipe._pending_enqueue("local:x", M1, PAYLOAD, 1.0))

    async def test_a_reply_not_armed_yet_is_not_claimed(self):
        # The request's own end-of-reply save may still land over it.
        await self.defer(M1, armed=False)
        self.assertIsNone(await self.claim())
        self.assertEqual(self.queue(), [M1])


class ClaimTests(_Db):
    async def test_the_head_is_claimed_once_and_its_payload_returned(self):
        await self.defer(M1, 100.0)
        got = await self.claim()
        self.assertEqual((got["message_id"], got["sent_at"]), (M1, 100.0))
        self.assertEqual(got["payload"]["body"], PAYLOAD["body"])
        self.assertEqual(self.msg(M1)["meta"][openclaw_pipe.PENDING_DELIVERY], "sending")
        self.assertIsNone(await self.claim())  # never twice

    async def test_concurrent_claims_get_one_winner(self):
        await self.defer(M1)
        got = await asyncio.gather(*[self.claim() for _ in range(5)])
        self.assertEqual(sum(g is not None for g in got), 1)

    async def test_the_second_message_waits_for_the_first(self):
        await self.defer(M1)
        await self.defer(M2, 101.0)
        self.assertEqual((await self.claim())["message_id"], M1)
        self.assertIsNone(await self.claim())
        await openclaw_pipe._pending_delivered(CHAT, M1)
        self.assertEqual((await self.claim())["message_id"], M2)

    def proc_id(self, pid, start, host=None):
        return f"{host or socket.gethostname()}/{openclaw_pipe._boot_id()}/{pid}.{start}:x"

    async def test_a_claim_of_a_process_that_still_runs_is_not_taken(self):
        # Two Open WebUI processes on one database: the first still replays it.
        await self.defer(M1)
        alive = self.proc_id(os.getppid(), openclaw_pipe._proc_start(os.getppid()))
        await self.claim(proc=alive, now=200.0)
        self.assertIsNone(await self.claim(proc="other", now=201.0))
        self.assertIsNone(await self.claim(proc="other", now=200.0 + openclaw_pipe.PENDING_LEASE_S - 1))

    async def test_a_claim_of_a_process_that_is_gone_is_taken_at_once(self):
        # After an Open WebUI restart the process that claimed it no longer exists.
        await self.defer(M1)
        await self.claim(proc=self.proc_id(2 ** 22 - 1, 1), now=200.0)
        self.assertEqual((await self.claim(proc="new", now=201.0))["message_id"], M1)

    async def test_a_claim_of_a_pid_that_another_process_reuses_is_taken(self):
        await self.defer(M1)
        pid = os.getppid()
        await self.claim(proc=self.proc_id(pid, "1"), now=200.0)  # same pid, another start time
        self.assertEqual((await self.claim(proc="new", now=201.0))["message_id"], M1)

    async def test_a_claim_of_another_host_waits_for_its_lease(self):
        await self.defer(M1)
        await self.claim(proc=self.proc_id(1, 1, host="elsewhere"), now=200.0)
        self.assertIsNone(await self.claim(proc="new", now=201.0))
        self.assertIsNotNone(await self.claim(proc="new", now=200.0 + openclaw_pipe.PENDING_LEASE_S + 1))

    async def test_a_stuck_claim_of_this_process_is_taken_after_the_lease(self):
        await self.defer(M1)
        await self.claim(proc="p1", now=200.0)
        self.assertIsNone(await self.claim(proc="p1", now=200.0 + openclaw_pipe.PENDING_LEASE_S - 1))
        self.assertIsNotNone(await self.claim(proc="p1", now=200.0 + openclaw_pipe.PENDING_LEASE_S + 1))

    async def test_a_reply_the_user_stopped_is_dropped_and_the_next_goes(self):
        await self.defer(M1)
        await self.defer(M2, 101.0)
        self.msg(M1)["done"] = True  # the page's Stop saved it finished
        got = await self.claim()
        self.assertEqual(got["message_id"], M2)
        self.assertEqual(self.queue(), [M2])
        self.assertEqual(self.msg(M1)["meta"][openclaw_pipe.PENDING_DELIVERY], "stopped")

    async def test_a_reply_deleted_meanwhile_is_dropped(self):
        await self.defer(M1)
        del self.chat.chat["history"]["messages"][M1]
        self.assertIsNone(await self.claim())
        self.assertEqual(self.queue(), [])


class OwnerTests(_Db):
    async def test_a_process_does_not_release_or_finish_a_claim_another_holds(self):
        await self.defer(M1)
        await self.claim(proc="mine", now=200.0)
        await openclaw_pipe._pending_release(CHAT, M1, "theirs")
        self.assertEqual(self.msg(M1)["meta"][openclaw_pipe.PENDING_DELIVERY], "sending")
        await openclaw_pipe._pending_delivered(CHAT, M1, proc="theirs")
        self.assertEqual(self.msg(M1)["meta"][openclaw_pipe.PENDING_DELIVERY], "sending")
        self.assertEqual(self.queue(), [M1])
        await openclaw_pipe._pending_delivered(CHAT, M1, proc="mine")
        self.assertEqual(self.msg(M1)["meta"][openclaw_pipe.PENDING_DELIVERY], "delivered")


class QuietTests(_Db):
    async def test_a_chat_with_nothing_waiting_is_not_rewritten(self):
        self.rows.clear()
        self.assertIsNone(await self.claim())
        self.assertEqual(self.chat.updated_at, 0)
        self.assertEqual(self.rows, {})
        self.assertEqual(self.chat.meta, {})

    async def test_only_chats_with_a_list_are_pending_work(self):
        async def stubs(_prefixes):
            return [(CHAT, M1), ("c-streaming", "m")]

        async def waiting(chat_id):
            return chat_id == CHAT

        with mock.patch.object(openclaw_pipe, "_orphan_stub_ids", stubs), \
                mock.patch.object(openclaw_pipe, "_pending_waiting", waiting):
            self.assertEqual(await openclaw_pipe._pending_chats(), [CHAT])


class RecoverTests(_Db):
    async def test_an_entry_whose_request_ended_unarmed_is_armed(self):
        await self.defer(M1, armed=False)
        with mock.patch.object(openclaw_pipe, "_owui_request_task", lambda _id: None):
            self.assertEqual(await openclaw_pipe._pending_arm_orphans(CHAT), 1)
        self.assertIs(self.msg(M1)["done"], False)
        self.assertTrue(self.msg(M1)["meta"][openclaw_pipe.PENDING_ARMED])
        self.assertEqual((await self.claim())["message_id"], M1)

    async def test_an_entry_whose_request_still_runs_is_left_to_it(self):
        await self.defer(M1, armed=False)
        running = asyncio.ensure_future(asyncio.sleep(3600))
        self.addCleanup(running.cancel)
        with mock.patch.object(openclaw_pipe, "_owui_request_task", lambda _id: running):
            self.assertEqual(await openclaw_pipe._pending_arm_orphans(CHAT), 0)
        self.assertFalse(self.msg(M1)["meta"].get(openclaw_pipe.PENDING_ARMED))

    async def test_the_request_that_kept_it_is_recorded(self):
        payload = {**PAYLOAD}
        await openclaw_pipe._pending_enqueue(CHAT, M1, payload, 1.0, "task-1")
        self.assertEqual(self.msg(M1)["meta"][openclaw_pipe.PENDING_REQUEST], "task-1")


class ReleaseAndDeliveredTests(_Db):
    async def test_a_release_makes_it_claimable_again(self):
        await self.defer(M1)
        await self.claim()
        await openclaw_pipe._pending_release(CHAT, M1)
        self.assertEqual(self.msg(M1)["meta"][openclaw_pipe.PENDING_DELIVERY], "pending")
        self.assertEqual((await self.claim())["message_id"], M1)

    async def test_delivered_leaves_the_list_and_drops_the_payload(self):
        await self.defer(M1)
        await self.claim()
        ev = openclaw_pipe._pending_sent_event(M1)
        await openclaw_pipe._pending_delivered(CHAT, M1)
        self.assertTrue(ev.is_set())
        m = self.msg(M1)["meta"]
        self.assertEqual(m[openclaw_pipe.PENDING_DELIVERY], "delivered")
        self.assertNotIn(openclaw_pipe.PENDING_PAYLOAD, m)
        self.assertEqual(self.queue(), [])
        # Delivered twice (a resend after a crash) changes nothing.
        await openclaw_pipe._pending_delivered(CHAT, M1)
        self.assertEqual(self.queue(), [])

    async def test_a_released_message_is_not_delivered(self):
        await self.defer(M1)
        await self.claim()
        await openclaw_pipe._pending_release(CHAT, M1)
        self.assertFalse(openclaw_pipe._pending_sent_event(M1).is_set())


class PassTests(_Db):
    async def test_messages_are_delivered_in_order_and_one_failure_stops_the_chat(self):
        await self.defer(M1)
        await self.defer(M2, 101.0)
        seen = []

        async def deliver(claim):
            seen.append(claim["message_id"])
            if claim["message_id"] == M1:
                await openclaw_pipe._pending_delivered(CHAT, M1)
                return True
            await openclaw_pipe._pending_release(CHAT, claim["message_id"])
            return False

        again = await openclaw_pipe._pending_pass([CHAT], "p1", deliver)
        self.assertEqual(seen, [M1, M2])
        self.assertTrue(again)  # M2 still waits
        self.assertEqual(self.queue(), [M2])

    async def test_a_down_gateway_leaves_everything_untouched(self):
        await self.defer(M1)

        async def deliver(claim):
            await openclaw_pipe._pending_release(CHAT, claim["message_id"])
            return False

        self.assertTrue(await openclaw_pipe._pending_pass([CHAT], "p1", deliver))
        self.assertEqual(self.msg(M1)["meta"][openclaw_pipe.PENDING_DELIVERY], "pending")
        self.assertEqual(self.queue(), [M1])

    async def test_the_next_message_of_a_chat_waits_for_the_run_of_the_one_before(self):
        await self.defer(M1)
        await self.defer(M2, 101.0)
        seen, runs = [], []

        async def deliver(claim):
            seen.append(claim["message_id"])
            await openclaw_pipe._pending_delivered(CHAT, claim["message_id"])
            run = asyncio.ensure_future(asyncio.sleep(3600))  # its run goes on
            self.addCleanup(run.cancel)
            runs.append(run)
            openclaw_pipe._pending_runs[CHAT] = run
            return True

        with mock.patch.dict(openclaw_pipe._pending_runs, clear=True):
            self.assertTrue(await openclaw_pipe._pending_pass([CHAT], "p1", deliver))
            self.assertEqual(seen, [M1])
            self.assertTrue(await openclaw_pipe._pending_pass([CHAT], "p1", deliver))
            self.assertEqual(seen, [M1])  # its run still goes
            runs[0].cancel()
            await asyncio.sleep(0)
            self.assertTrue(await openclaw_pipe._pending_pass([CHAT], "p1", deliver))
        self.assertEqual(seen, [M1, M2])

    async def test_an_empty_queue_ends_the_pass(self):
        async def deliver(claim):
            raise AssertionError("nothing to deliver")

        self.assertFalse(await openclaw_pipe._pending_pass([CHAT], "p1", deliver))


class RunTests(_Db):
    """`_pending_run`: the stored turn goes through the pipe again, its text and tool cards into the
    waiting reply, and what the turn did (not what it showed) decides how it ends."""

    CARD = {"type": "response.output_item.added", "output_index": 1 << 30,
            "item": {"type": "function_call", "id": "call-1", "call_id": "call-1", "name": "Bash",
                     "arguments": "{}", "status": "completed"}}

    def setUp(self):
        super().setUp()
        self.events, self.published, self.waited = [], [], []

        async def emitter(chat_id, message_id, user_id):
            async def emit(event):
                self.events.append(event)
            return emit

        async def publish(chat_id, message_id, user_id, text=None):
            self.published.append((chat_id, message_id, user_id))

        async def wait_writer(chat_id, message_id):
            self.waited.append((chat_id, message_id))

        for name, repl in (("_pending_emitter", emitter), ("_publish_chat_finished", publish),
                           ("_pending_wait_writer", wait_writer)):
            p = mock.patch.object(openclaw_pipe, name, repl)
            p.start()
            self.addCleanup(p.stop)

    async def run_with(self, impl, publish=False):
        await self.defer(M1, 100.0)
        claim = await self.claim(proc=openclaw_pipe._pending_process_token())
        pipe = Pipe()
        pipe.valves.PUBLISH_CHAT_FINISHED = publish
        with mock.patch.object(Pipe, "_pipe_impl", impl):
            return await openclaw_pipe._pending_run(pipe, claim)

    async def test_the_text_lands_in_the_reply_and_it_is_finished(self):
        seen = {}

        async def fake_impl(self_, body, emitter, **kw):
            seen.update(body=body, meta=kw["__metadata__"], user=kw["__user__"], request=kw["__request__"])
            yield "note "
            yield "answer"
            await openclaw_pipe._pending_delivered(CHAT, M1)

        self.assertTrue(await self.run_with(fake_impl))
        self.assertEqual(self.msg(M1)["content"], "note answer")
        self.assertIs(self.msg(M1)["done"], True)
        self.assertEqual(seen["body"], PAYLOAD["body"])
        self.assertEqual(seen["meta"][openclaw_pipe.PENDING_REPLAY_META], 100.0)
        self.assertNotIn("task_id", seen["meta"])
        self.assertIsNone(seen["request"])
        self.assertEqual(self.events[-1]["data"]["content"], "note answer")
        self.assertTrue(self.events[-1]["data"]["done"])

    async def test_the_tool_cards_of_the_run_are_kept(self):
        async def fake_impl(self_, body, emitter, **kw):
            yield "before "
            yield self.CARD
            yield "after"
            await openclaw_pipe._pending_delivered(CHAT, M1)

        self.assertTrue(await self.run_with(fake_impl))
        output = self.msg(M1)["output"]
        self.assertEqual([i["type"] for i in output], ["message", "function_call", "message"])
        self.assertEqual(output[0]["content"][0]["text"], "before ")
        self.assertEqual(output[2]["content"][0]["text"], "after")
        self.assertEqual(self.msg(M1)["content"], "before after")
        relayed = [e for e in self.events if e["type"] == "response:completion"]
        self.assertTrue(any(e["data"]["item"].get("type") == "function_call" for e in relayed
                            if e["data"].get("type") == "response.output_item.added"))
        self.assertEqual(self.events[-1]["data"]["output"], output)

    async def test_the_finished_event_goes_out_after_a_replay_when_the_valve_is_on(self):
        async def fake_impl(self_, body, emitter, **kw):
            yield "answer"
            await openclaw_pipe._pending_delivered(CHAT, M1)

        await self.run_with(fake_impl, publish=True)
        self.assertEqual(self.published, [(CHAT, M1, USER)])

    async def test_no_finished_event_when_the_valve_is_off(self):
        async def fake_impl(self_, body, emitter, **kw):
            yield "answer"
            await openclaw_pipe._pending_delivered(CHAT, M1)

        await self.run_with(fake_impl, publish=False)
        self.assertEqual(self.published, [])

    async def test_a_reply_handed_to_the_turn_writer_is_left_open_and_waited_for(self):
        async def fake_impl(self_, body, emitter, **kw):
            yield "partial"
            await openclaw_pipe._pending_delivered(CHAT, M1)
            kw["_turn"].handed_over = True

        self.assertTrue(await self.run_with(fake_impl, publish=True))
        self.assertEqual(self.waited, [(CHAT, M1)])
        self.assertIs(self.msg(M1)["done"], False)  # the turn writer finishes it
        self.assertNotEqual(self.msg(M1).get("content"), "partial")
        self.assertFalse([e for e in self.events if e["type"] == "chat:completion"])
        self.assertEqual(self.published, [])

    async def test_a_turn_that_met_a_down_gateway_after_its_note_goes_back_to_waiting(self):
        # The note is text already, but the Gateway never had the message.
        async def fake_impl(self_, body, emitter, **kw):
            yield "_(Sent at 07:44, while the gateway was down. Delivered now.)_\n\n"
            await openclaw_pipe._pending_release(CHAT, M1)
            kw["_turn"].deferred = True

        self.assertFalse(await self.run_with(fake_impl))
        self.assertEqual(self.msg(M1)["meta"][openclaw_pipe.PENDING_DELIVERY], "pending")
        self.assertIs(self.msg(M1)["done"], False)
        self.assertEqual(self.queue(), [M1])

    async def test_a_turn_that_ended_without_sending_is_shown_as_it_is_not_retried_for_ever(self):
        async def fake_impl(self_, body, emitter, **kw):
            yield "**Error sending message:** refused"

        self.assertFalse(await self.run_with(fake_impl))
        self.assertEqual(self.msg(M1)["meta"][openclaw_pipe.PENDING_DELIVERY], "failed")
        self.assertIs(self.msg(M1)["done"], True)
        self.assertEqual(self.queue(), [])


class WorkerSlotTests(unittest.IsolatedAsyncioTestCase):
    """One worker per process, across function loads."""

    def setUp(self):
        socket_main = types.ModuleType("open_webui.socket.main")
        mods = {**owui_modules(), "open_webui.socket.main": socket_main}
        mods["open_webui.socket"].main = socket_main
        p = mock.patch.dict(sys.modules, mods)
        p.start()
        self.addCleanup(p.stop)
        self.socket_main = socket_main
        openclaw_pipe._pending_task = None
        openclaw_pipe._pending_discovered = False
        self.started = 0
        self.release = asyncio.Event()

        async def worker(pipe):
            self.started += 1
            await self.release.wait()

        p2 = mock.patch.object(openclaw_pipe, "_pending_worker", worker)
        p2.start()
        self.addCleanup(p2.stop)
        self.addCleanup(lambda: setattr(openclaw_pipe, "_pending_task", None))

    async def test_it_starts_once_however_often_it_is_asked(self):
        pipe = Pipe()
        a = openclaw_pipe._ensure_pending_delivery(pipe)
        await asyncio.sleep(0)
        for _ in range(3):
            self.assertIs(openclaw_pipe._ensure_pending_delivery(pipe), a)
            self.assertIs(openclaw_pipe._ensure_pending_delivery(pipe, force=True), a)
        await asyncio.sleep(0)
        self.assertEqual(self.started, 1)
        self.release.set()
        await a

    async def test_the_discovery_after_a_load_runs_once_and_a_deferral_restarts_it(self):
        pipe = Pipe()
        self.release.set()
        first = openclaw_pipe._ensure_pending_delivery(pipe)
        await first
        self.assertIsNone(openclaw_pipe._ensure_pending_delivery(pipe))  # found nothing: not again
        self.assertIsNotNone(openclaw_pipe._ensure_pending_delivery(pipe, force=True))

    async def test_a_new_load_replaces_the_old_loads_worker(self):
        stale = asyncio.ensure_future(asyncio.sleep(3600))
        setattr(self.socket_main, openclaw_pipe._pending_slot(openclaw_pipe._composer_own_function_id()), stale)
        task = openclaw_pipe._ensure_pending_delivery(Pipe())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertTrue(stale.cancelled())
        self.assertIsNot(task, stale)
        self.release.set()
        await task


class StubReconcileTests(unittest.TestCase):
    def test_a_waiting_reply_is_not_an_orphan(self):
        for state in ("pending", "sending"):
            self.assertTrue(openclaw_pipe._pending_owns({"meta": {openclaw_pipe.PENDING_DELIVERY: state}}))
        for stub in ({"meta": {openclaw_pipe.PENDING_DELIVERY: "delivered"}}, {"meta": {}}, {}):
            self.assertFalse(openclaw_pipe._pending_owns(stub))


class DeferTests(unittest.IsolatedAsyncioTestCase):
    """The turn: a gateway that cannot be reached keeps the message instead of erroring."""

    def setUp(self):
        self.pipe = Pipe()
        self.pipe.valves.ENABLE_FILE_SERVER = False
        self.events = []
        self.enqueued = []
        self.request_ids = []

    async def _emitter(self, e):
        self.events.append(e)

    async def _run(self, error, metadata, body=None, user=None):
        async def no_gateway(_getter):
            raise error

        async def enqueue(chat_id, message_id, payload, sent_at, request_task_id=None):
            self.enqueued.append((chat_id, message_id, payload, sent_at))
            self.request_ids.append(request_task_id)
            return True

        def arm(*_a, **_k):
            return None

        body = body or {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "hi"}]}
        self.arm = mock.Mock(side_effect=arm)

        async def arm_after_request(*a, **k):
            self.arm(*a, **k)

        with mock.patch.object(openclaw_pipe, "_get_gateway_connection", no_gateway), \
                mock.patch.object(openclaw_pipe, "_ensure_composer_watcher"), \
                mock.patch.object(openclaw_pipe, "_ensure_pending_delivery"), \
                mock.patch.object(openclaw_pipe, "_pending_enqueue", enqueue), \
                mock.patch.object(openclaw_pipe, "_pending_arm_after_request", arm_after_request):
            out = [c async for c in self.pipe.pipe(body, self._emitter, __metadata__=metadata, __user__=user)]
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        return out

    STAMP = int(time.time()) - 130  # the wait for a reconnect has gone by since the user sent it
    META = {"chat_id": CHAT, "message_id": M1, "user_id": USER, "session_id": "s1", "task_id": "t1",
            "variables": {"{{USER_LANGUAGE}}": "he-IL"}, "user_message": {"timestamp": STAMP}}

    async def test_a_refused_gateway_keeps_the_message_and_shows_no_error(self):
        out = await self._run(GatewayError("down", {"code": GATEWAY_REFUSED_CODE}), self.META,
                              user={"id": USER, "valves": {"thinking_default": "high"}})
        self.assertEqual("".join(out), "")
        self.assertEqual(len(self.enqueued), 1)
        chat_id, message_id, payload, sent_at = self.enqueued[0]
        self.assertEqual((chat_id, message_id), (CHAT, M1))
        self.assertEqual(payload["body"]["messages"][-1]["content"], "hi")
        self.assertEqual(payload["metadata"]["message_id"], M1)
        self.assertEqual(payload["user"]["valves"]["thinking_default"], "high")
        self.assertEqual(sent_at, self.STAMP)  # the message's own time, not the time it was kept
        status = [e for e in self.events if e["type"] == "status"][-1]["data"]
        self.assertEqual(status["done"], False)
        self.assertIn("ממתין לגייטווי", status["description"])
        self.arm.assert_called_once()
        self.assertEqual(self.request_ids, ["t1"])  # the request that kept it, for the recovery

    async def test_a_refusal_waiting_cannot_fix_still_shows_the_error(self):
        out = await self._run(GatewayError("The GATEWAY_TOKEN valve is empty."), self.META)
        self.assertIn("Gateway connection error", "".join(out))
        self.assertEqual(self.enqueued, [])
        self.arm.assert_not_called()

    async def test_without_a_saved_chat_the_error_is_shown(self):
        out = await self._run(GatewayError("down", {"code": GATEWAY_REFUSED_CODE}), {"session_id": "s1"})
        self.assertIn("Gateway connection error", "".join(out))
        self.assertEqual(self.enqueued, [])

    async def test_a_channel_mention_is_not_kept(self):
        out = await self._run(GatewayError("down", {"code": GATEWAY_REFUSED_CODE}),
                              {**self.META, "chat_id": "channel:abc"})
        self.assertIn("Gateway connection error", "".join(out))
        self.assertEqual(self.enqueued, [])

    async def test_the_replay_of_a_kept_message_that_meets_a_down_gateway_releases_it(self):
        released = []

        async def release(chat_id, message_id, proc=None):
            released.append((chat_id, message_id))

        with mock.patch.object(openclaw_pipe, "_pending_release", release):
            out = await self._run(
                GatewayError("down", {"code": GATEWAY_REFUSED_CODE}),
                {**self.META, openclaw_pipe.PENDING_REPLAY_META: 100.0})
        self.assertEqual(out, [])
        self.assertEqual(released, [(CHAT, M1)])
        self.assertEqual(self.enqueued, [])
        self.arm.assert_not_called()


class SourceWiringTests(unittest.TestCase):
    """Wiring the behavior tests above cannot reach without a live gateway."""

    def src(self, name):
        import os
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "openclaw_pipe_pkg")
        return open(os.path.join(root, name)).read()

    def test_the_orphan_sweep_leaves_a_waiting_reply_alone(self):
        w = self.src("writer.py")
        body = w[w.index("async def _reconcile_stub("):]
        self.assertIn("_pending_owns(stub)", body[:body.index("\nasync def ")])

    def test_the_send_uses_the_original_time_and_marks_the_reply_delivered(self):
        p = self.src("pipe.py")
        self.assertIn("at=None if fresh or self.body.get(THINKING_DROPPED_KEY)", p)
        self.assertIn("_pending_delivered(", p)
        self.assertIn("self.defer_for_gateway(", p)

    def test_the_worker_starts_with_the_model_list_like_the_orphan_sweep(self):
        p = self.src("pipe.py")
        self.assertGreaterEqual(p.count("_ensure_pending_delivery("), 2)


if __name__ == "__main__":
    unittest.main()
