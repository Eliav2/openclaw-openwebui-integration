"""ELI-509: a message sent while an OpenClaw reply runs, on Open WebUI 0.13.4.

0.13.4 holds a message sent while a reply runs under that reply (main.py ->
utils/subagents.py queue_user_message, meta.status "pending") and answers it
when the request writing the reply ends with it done. The Pipe steers such a
message into the run itself, and saves a handed-over or steered reply done
after its request ended, so a held message was never answered. Its browser
shows the held message in the queue above the composer, where the Pipe's
auto-send hook clicked its Send now: a Stop (sendQueuedMessageNow), which
cancelled the run. Open WebUI is stubbed: only the calls the Pipe makes."""
import asyncio
import functools
import sys
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

CHAT = "00000000-0000-4000-8000-0000000000c1"
REPLY = "00000000-0000-4000-8000-0000000000a1"


async def _drain():
    """This test's background tasks to their end (other tests' loops left theirs)."""
    loop = asyncio.get_running_loop()
    await asyncio.gather(*[t for t in list(ocp._BACKGROUND_TASKS) if t.get_loop() is loop])


def _mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    return m


class _Owui:
    """The Open WebUI modules the Pipe imports, as of `version`."""

    def __init__(self, test, version="0.13.4", reply_model="openclaw_gateway.default", held=True):
        self.calls, self.held, self.chat_tasks = [], held, {}
        owui = self

        async def queue_user_message(request, chat_id, user, user_message, parent_id):
            owui.calls.append(("hold", chat_id, parent_id))
            return True

        async def process_pending_internal_messages(source_request, parent_chat_id, user_id, run,
                                                    reply_id=None):
            owui.calls.append(("answer", parent_chat_id, user_id, run.get("model_id"), reply_id,
                               all(t.done() for t in owui.chat_tasks.values())))
            owui.held = False

        def get_pending_run(metadata, model_id=None):
            return {"model_id": metadata.get("model_id") or model_id}

        class Chats:
            @staticmethod
            async def get_message_by_id_and_message_id(chat_id, message_id):
                return {"id": message_id, "role": "assistant", "model": reply_model, "done": False}

            @staticmethod
            async def get_chat_by_id(chat_id):
                return types.SimpleNamespace(id=chat_id, user_id="user-1")

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, fields):
                return {"id": message_id, **fields}

        class ChatMessages:
            @staticmethod
            async def has_queued_message(chat_id, message_id):
                return owui.held

        async def list_task_ids_by_item_id(redis, item_id):
            return list(owui.chat_tasks)

        class Request:
            def __init__(self, scope):
                self.scope, self.app = scope, scope["app"]

        subagents = {"process_pending_internal_messages": process_pending_internal_messages}
        if version >= "0.13.4":
            subagents.update(queue_user_message=queue_user_message, get_pending_run=get_pending_run)
        self.subagents = _mod("open_webui.utils.subagents", **subagents)
        p = mock.patch.dict(sys.modules, {
            "open_webui": _mod("open_webui"), "open_webui.main": _mod("open_webui.main", app=object()),
            "open_webui.utils": _mod("open_webui.utils"),
            "open_webui.utils.subagents": self.subagents,
            "open_webui.utils.chat_id": _mod("open_webui.utils.chat_id",
                                             is_saved_chat_id=lambda c: bool(c) and not c.startswith("local:")),
            "open_webui.models": _mod("open_webui.models"),
            "open_webui.models.chats": _mod("open_webui.models.chats", Chats=Chats),
            "open_webui.models.chat_messages": _mod("open_webui.models.chat_messages", ChatMessages=ChatMessages),
            "open_webui.tasks": _mod("open_webui.tasks", list_task_ids_by_item_id=list_task_ids_by_item_id,
                                     tasks=self.chat_tasks),
            "open_webui.env": _mod("open_webui.env", REDIS_KEY_PREFIX="open-webui"),
            "starlette": _mod("starlette"), "starlette.requests": _mod("starlette.requests", Request=Request),
        })
        p.start()
        test.addCleanup(p.stop)


class NoHoldTests(unittest.IsolatedAsyncioTestCase):
    """0.13.4 holds no message for an OpenClaw reply: the Pipe steers it."""

    async def test_a_message_sent_while_an_openclaw_reply_runs_is_not_held(self):
        owui = _Owui(self)
        ocp._ensure_no_owui_hold()
        self.assertIs(await owui.subagents.queue_user_message(None, CHAT, None, {"id": "u"}, REPLY), False)
        self.assertEqual(owui.calls, [])

    async def test_another_models_reply_still_holds_it(self):
        owui = _Owui(self, reply_model="other.model")
        ocp._ensure_no_owui_hold()
        self.assertIs(await owui.subagents.queue_user_message(None, CHAT, None, {"id": "u"}, REPLY), True)
        self.assertEqual(owui.calls, [("hold", CHAT, REPLY)])

    async def test_it_wraps_open_webuis_function_once_and_a_redeploy_rewraps_the_original(self):
        owui = _Owui(self, reply_model="other.model")
        ocp._ensure_no_owui_hold()
        first = owui.subagents.queue_user_message
        ocp._ensure_no_owui_hold()
        self.assertIs(owui.subagents.queue_user_message, first)
        # A new deploy is a new module: its wrapper replaces the old one, over Open WebUI's own.
        first._openclaw_module = "function_openclaw_gateway_old"
        ocp._ensure_no_owui_hold()
        self.assertIsNot(owui.subagents.queue_user_message, first)
        await owui.subagents.queue_user_message(None, CHAT, None, {"id": "u"}, REPLY)
        self.assertEqual(owui.calls, [("hold", CHAT, REPLY)])

    def test_before_0_13_4_nothing_is_wrapped(self):
        owui = _Owui(self, version="0.13.3")
        ocp._ensure_no_owui_hold()
        self.assertFalse(hasattr(owui.subagents, "queue_user_message"))


class AnswerHeldTests(unittest.IsolatedAsyncioTestCase):
    """A message held before the wrap is answered once its reply is done."""

    async def test_a_held_message_is_answered_once_the_reply_is_saved_done(self):
        owui = _Owui(self)
        await ocp._owui_upsert_message(CHAT, REPLY, {"content": "x", "done": True})
        await _drain()
        self.assertEqual(owui.calls, [("answer", CHAT, "user-1", "openclaw_gateway.default", REPLY, True)])

    async def test_a_reply_saved_still_running_answers_nothing(self):
        owui = _Owui(self)
        await ocp._owui_upsert_message(CHAT, REPLY, {"content": "x", "done": False})
        await _drain()
        self.assertEqual(owui.calls, [])

    async def test_nothing_held_answers_nothing(self):
        owui = _Owui(self, held=False)
        await ocp._owui_upsert_message(CHAT, REPLY, {"done": True})
        await _drain()
        self.assertEqual(owui.calls, [])

    async def test_a_request_still_running_in_the_chat_is_let_end_first(self):
        """Open WebUI answers held messages itself when that request ends."""
        owui = _Owui(self)
        gate = asyncio.Event()
        owui.chat_tasks["t1"] = asyncio.ensure_future(gate.wait())
        await ocp._owui_upsert_message(CHAT, REPLY, {"done": True})
        await asyncio.sleep(0)
        self.assertEqual(owui.calls, [])
        gate.set()
        await _drain()
        self.assertEqual(owui.calls, [("answer", CHAT, "user-1", "openclaw_gateway.default", REPLY, True)])

    async def test_before_0_13_4_nothing_is_answered(self):
        owui = _Owui(self, version="0.13.3")
        await ocp._owui_upsert_message(CHAT, REPLY, {"done": True})
        await _drain()
        self.assertEqual(owui.calls, [])


class AutoSendTests(unittest.TestCase):
    """The auto-send hook clicks Send now only where the queue is the browser's."""

    @staticmethod
    def _flag(code: str, chat: str) -> str:
        return code.split(f'window.__ocAutoSend["{chat}"] = ', 1)[1].split(";", 1)[0]

    def test_on_0_13_4_a_saved_chat_sends_mid_run_itself(self):
        _Owui(self)
        self.assertIs(ocp._owui_sends_mid_run(CHAT), True)
        self.assertEqual(self._flag(ocp._page_hooks_js(CHAT, True), CHAT), "false")

    def test_on_0_13_4_a_temporary_chat_still_queues_in_the_browser(self):
        _Owui(self)
        self.assertIs(ocp._owui_sends_mid_run("local:abc"), False)
        self.assertEqual(self._flag(ocp._page_hooks_js("local:abc", True), "local:abc"), "true")

    def test_before_0_13_4_the_hook_clicks_send_now(self):
        _Owui(self, version="0.13.3")
        self.assertIs(ocp._owui_sends_mid_run(CHAT), False)
        self.assertEqual(self._flag(ocp._page_hooks_js(CHAT, True), CHAT), "true")


if __name__ == "__main__":
    unittest.main()


SK = "agent:main:openwebui-00000000-0000-4000-8000-0000000000e1-" + CHAT
OTHER_SK = "agent:main:openwebui-00000000-0000-4000-8000-0000000000e2-" + CHAT


def _stop_task_module(test, *, stop_task="default"):
    """`open_webui.tasks` of a single-process Open WebUI: `stop_task` cancels the local task,
    as tasks.py does without Redis. `stop_task=None`: a release without it."""
    local, stopped = {}, []

    async def default_stop_task(redis, task_id: str):
        stopped.append(task_id)
        task = local.pop(task_id, None)
        if task is None:
            return {"status": False}
        task.cancel()
        await asyncio.wait({task})
        return {"status": True}

    async def list_task_ids_by_item_id(redis, item_id):
        return list(local) + list(test.remote_ids)
    attrs = {"tasks": local, "list_task_ids_by_item_id": list_task_ids_by_item_id}
    fn = default_stop_task if stop_task == "default" else stop_task
    if fn is not None:
        attrs["stop_task"] = fn
    p = mock.patch.dict(sys.modules, {
        "open_webui": _mod("open_webui"), "open_webui.main": _mod("open_webui.main", app=None),
        "open_webui.tasks": _mod("open_webui.tasks", **attrs)})
    p.start()
    test.addCleanup(p.stop)
    test.addCleanup(ocp._STOP_TASK.clear)
    return local, stopped


class HandoverTests(unittest.IsolatedAsyncioTestCase):
    """A message sent while a reply runs ends that reply's request as 0.13.3's Send now did,
    so the run is parked and claimed: kept in its own message, never "Stopped" (ELI-509)."""

    async def asyncSetUp(self):
        self.remote_ids = []
        self.conn = ocp._GatewayConnection(lambda: None)
        self.local, self.stopped = _stop_task_module(self)
        self.page_asked, self.stops, self.logged = [], [], []
        p = mock.patch.object(ocp, "pipe_log", lambda *a, **k: self.logged.append(a[0]))
        p.start()
        self.addCleanup(p.stop)

    async def asyncTearDown(self):
        for entry in list(self.conn._pending_cancel_aborts.values()):
            if entry["task"] is not None:
                entry["task"].cancel()

    def _request(self, task_id, sk=SK, run_id="run-1", ignores_cancel=False):
        """A reply's live request: parks its run when cancelled, as `_PipeTurn.park` does."""
        conn, test = self.conn, self
        release = asyncio.Event()

        async def page(_event):
            test.page_asked.append(task_id)
            return "send_now"

        async def request():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if ignores_cancel:
                    await release.wait()
                conn.park_cancelled_turn(
                    sk, run_id, None, [], 10.0, send_stop=False,
                    settle=functools.partial(ocp._settle_cancel_intent, conn, sk, run_id, CHAT, page,
                                             task_id=task_id),
                    on_stop=lambda: test.stops.append(run_id))
                raise
            finally:
                conn.unregister_reply_call(sk, id(task_id))
        task = asyncio.ensure_future(request())
        self.local[task_id] = task
        conn.register_reply_call(sk, id(task_id), task_id)
        self.addCleanup(release.set)
        self.addCleanup(task.cancel)
        return task, release

    async def _claim(self, sk=SK):
        claimed = await self.conn.claim_cancelled_turn(sk)
        for entry in self.conn._pending_cancel_aborts.values():
            if entry["task"] is not None:
                entry["task"].cancel()
        return claimed

    async def test_a_live_reply_request_is_ended_as_a_send_now_and_its_run_claimed(self):
        task, _ = self._request("t-run")
        await asyncio.sleep(0)
        self.assertIs(await ocp._hand_over_running_reply(self.conn, SK, "t-new"), True)
        self.assertTrue(task.cancelled())
        claimed = await self._claim()
        self.assertEqual(claimed["run_id"], "run-1")
        self.assertEqual(self.page_asked, [], "the page decided whether the running reply survives")
        self.assertEqual(self.stops, [], "the running reply was stopped")
        self.assertEqual(self.conn._reply_calls, {}, "an ended request left its entry")

    async def test_a_send_now_that_parked_it_first_is_left_as_it_is(self):
        """0.13.3: Send now cancelled the reply before the message came."""
        task, _ = self._request("t-run")
        await asyncio.sleep(0)
        await ocp._owui_stop_task()(None, "t-run")   # Open WebUI's own Send now cancel
        self.assertIs(await ocp._hand_over_running_reply(self.conn, SK, "t-new"), False)
        self.assertEqual(self.stopped, ["t-run"], "stopped twice")
        claimed = await self._claim()
        self.assertEqual(claimed["run_id"], "run-1")
        self.assertEqual(self.page_asked, ["t-run"], "a Send now's cancel is still the page's to name")
        self.assertEqual(self.stops, [])

    async def test_a_send_now_cancel_after_the_handover_changes_nothing(self):
        """0.13.3 with the message first: Open WebUI's own cancel lands on an ended request."""
        self._request("t-run")
        await asyncio.sleep(0)
        await ocp._hand_over_running_reply(self.conn, SK, "t-new")
        self.assertEqual((await ocp._owui_stop_task()(None, "t-run"))["status"], False)
        claimed = await self._claim()
        self.assertEqual(claimed["run_id"], "run-1")
        self.conn.settle_parked_turn(SK, "run-1", "send_now", hold_s=120)   # the page's late answer
        self.assertIsNone(await self.conn.claim_cancelled_turn(SK), "parked twice")
        self.assertEqual(self.stops, [])

    async def test_a_request_that_never_ends_falls_back_within_the_bound(self):
        task, release = self._request("t-run", ignores_cancel=True)
        await asyncio.sleep(0)
        with mock.patch.object(ocp, "HANDOVER_PARK_TIMEOUT_S", 0.05):
            self.assertIs(await ocp._hand_over_running_reply(self.conn, SK, "t-new"), False)
        self.assertTrue(any("did not end within" in line and SK in line for line in self.logged), self.logged)
        release.set()
        await asyncio.wait({task})

    async def test_another_users_session_is_left_running(self):
        """0.13.3 stops only the sender's own tasks in a saved chat (main.py stop endpoint,
        filter_task_ids_by_user_id): another user's message never ended this reply."""
        task, _ = self._request("t-run", sk=OTHER_SK)
        await asyncio.sleep(0)
        self.assertIs(await ocp._hand_over_running_reply(self.conn, SK, "t-new"), False)
        self.assertFalse(task.done())
        self.assertEqual(self.stopped, [])

    async def test_its_own_request_is_never_stopped(self):
        task, _ = self._request("t-new")
        await asyncio.sleep(0)
        self.assertIs(await ocp._hand_over_running_reply(self.conn, SK, "t-new"), False)
        self.assertFalse(task.done())

    async def test_a_real_stop_after_the_handover_is_still_a_stop(self):
        """Only the requests the handover ended are a Send now; the next cancel asks the page."""
        self._request("t-run")
        await asyncio.sleep(0)
        await ocp._hand_over_running_reply(self.conn, SK, "t-new")
        await self._claim()
        self.assertIs(self.conn.take_handover_cancel(SK, "t-new"), False)
        self.assertIs(self.conn.take_handover_cancel(SK, "t-run"), False, "a handover cancel counted twice")

    async def test_a_request_in_another_worker_is_logged_not_touched(self):
        self.remote_ids.append("t-remote")
        with mock.patch.object(self.conn, "live_runs", lambda sk: {"run-1"}), \
                mock.patch.object(self.conn, "parse_owui_session_key", lambda sk: ("u", CHAT)):
            self.assertIs(await ocp._hand_over_running_reply(self.conn, SK, "t-new"), False)
        self.assertTrue(any("another Open WebUI worker" in line for line in self.logged), self.logged)
        self.assertEqual(self.stopped, [])


class StopTaskAdapterTests(unittest.TestCase):
    """`open_webui.tasks.stop_task` is Open WebUI's: used only with the shape it has had."""

    def test_the_signature_of_0_13_2_to_0_13_4_is_used(self):
        # backend/open_webui/tasks.py:311 at v0.13.2, v0.13.3 and v0.13.4 alike.
        async def stop_task(redis, task_id: str):
            return {}
        for version in ("0.13.2", "0.13.3", "0.13.4"):
            with self.subTest(version=version):
                _stop_task_module(self, stop_task=stop_task)
                self.assertIs(ocp._owui_stop_task(), stop_task)
                ocp._STOP_TASK.clear()

    def test_a_missing_or_changed_one_is_not_used_and_says_so(self):
        async def changed(redis, task_id, reason):
            return {}

        def not_async(redis, task_id):
            return {}
        for fn in (None, changed, not_async):
            with self.subTest(fn=fn), mock.patch.object(ocp, "pipe_log") as log:
                _stop_task_module(self, stop_task=fn)
                self.assertIsNone(ocp._owui_stop_task())
                self.assertIn("stop_task", log.call_args[0][0])
                ocp._STOP_TASK.clear()


class HandoverFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_stop_task_the_message_goes_on_and_the_reply_keeps_running(self):
        self.remote_ids = []
        conn = ocp._GatewayConnection(lambda: None)
        local, _ = _stop_task_module(self, stop_task=None)
        task = asyncio.ensure_future(asyncio.Event().wait())
        self.addCleanup(task.cancel)
        local["t-run"] = task
        conn.register_reply_call(SK, 1, "t-run")
        with mock.patch.object(ocp, "pipe_log") as log:
            self.assertIs(await ocp._hand_over_running_reply(conn, SK, "t-new"), False)
        self.assertFalse(task.done())
        self.assertTrue(any("handover skipped" in c[0][0] for c in log.call_args_list))


class ReplyCallRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_reply_that_raises_leaves_no_entry(self):
        conn = ocp._GatewayConnection(lambda: None)
        turn = ocp._PipeTurn.__new__(ocp._PipeTurn)
        turn.conn, turn.session_key, turn.ended = conn, SK, False
        conn.register_reply_call(SK, id(turn), "t-run")
        turn.reply_call_registered = True

        async def boom():
            raise RuntimeError("boom")
            yield  # makes this an async generator

        async def nothing():
            return
        turn.prepare = boom
        turn.save_final_statuses = nothing
        with mock.patch.object(ocp, "_secret_asker_leave", lambda t: None):
            with self.assertRaises(RuntimeError):
                async for _ in turn.run():
                    pass
        self.assertEqual(conn._reply_calls, {})

    def test_the_map_holds_task_ids_not_tasks(self):
        conn = ocp._GatewayConnection(lambda: None)
        conn.register_reply_call(SK, 1, "t-run")
        self.assertEqual(conn._reply_calls, {SK: {1: ("t-run", None)}})


class HandoverReachTests(unittest.TestCase):
    def _note(self, workers):
        with mock.patch.dict(sys.modules, {"open_webui": _mod("open_webui"),
                                           "open_webui.env": _mod("open_webui.env", UVICORN_WORKERS=workers)}), \
                mock.patch.object(ocp, "_HANDOVER_REACH_NOTED", []), mock.patch.object(ocp, "pipe_log") as log:
            ocp._note_handover_reach()
            ocp._note_handover_reach()
        return [c[0][0] for c in log.call_args_list]

    def test_several_workers_are_named_once(self):
        lines = self._note(4)
        self.assertEqual(len(lines), 1)
        self.assertIn("4 workers", lines[0])

    def test_one_worker_says_nothing(self):
        self.assertEqual(self._note(1), [])


class QuestionLeftTests(unittest.IsolatedAsyncioTestCase):
    """A message sent while a question card waits: the card is left first and the question ends
    its reply as text, then the request ends, in the order of 0.13.3's Send now (ELI-200)."""

    async def test_the_card_is_left_before_the_request_is_stopped(self):
        self.remote_ids = []
        conn = ocp._GatewayConnection(lambda: None)
        local, stopped = _stop_task_module(self)
        order = []

        class Call:
            async def leave_question(self, timeout_s):
                order.append(("left", list(stopped)))
        task = asyncio.ensure_future(asyncio.Event().wait())
        local["t-run"] = task
        conn.register_reply_call(SK, 1, "t-run", call=Call())
        with mock.patch.object(ocp, "pipe_log"):
            await ocp._hand_over_running_reply(conn, SK, "t-new")
        self.assertEqual(order, [("left", [])], "the request was stopped before its card was left")
        self.assertEqual(stopped, ["t-run"])

    def _turn(self, ask):
        turn = ocp._PipeTurn.__new__(ocp._PipeTurn)
        turn.pending_prompt_text = "Which color?"
        turn.maybe_answer_user_input = ask
        return turn

    async def test_a_left_card_reads_as_unanswered(self):
        gate = asyncio.Event()

        async def ask(_text):
            await gate.wait()
        turn = self._turn(ask)
        with mock.patch.object(ocp, "pipe_log"):
            asking = asyncio.ensure_future(turn.ask_held_prompt())
            await asyncio.sleep(0)
            leaving = asyncio.ensure_future(turn.leave_question(1.0))
            result = await asking
        self.assertIs(result.handled, False)
        turn.question_left.set()   # what finish_held_prompt does once the question is shown
        await leaving

    async def test_a_cancel_of_the_reply_itself_still_cancels(self):
        async def ask(_text):
            await asyncio.Event().wait()
        turn = self._turn(ask)
        asking = asyncio.ensure_future(turn.ask_held_prompt())
        await asyncio.sleep(0)
        asking.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asking


class AutoSendOffTests(unittest.IsolatedAsyncioTestCase):
    """AUTO_SEND_WHILE_RUNNING off on 0.13.4: the message waits in Open WebUI's queue, as up to
    0.13.3, until the reply ends or the user's Send now, which sends it at once."""

    async def test_with_auto_send_off_the_message_is_held(self):
        owui = _Owui(self)
        ocp._ensure_no_owui_hold(lambda: False)
        self.assertIs(await owui.subagents.queue_user_message(None, CHAT, None, {"id": "u"}, REPLY), True)
        self.assertEqual(owui.calls, [("hold", CHAT, REPLY)])

    async def test_send_now_sends_the_held_messages_at_once(self):
        owui = _Owui(self)
        conn = types.SimpleNamespace(take_handover_cancel=lambda sk, t: False,
                                     settle_parked_turn=mock.Mock())

        async def page(_event):
            return "send_now"
        with mock.patch.object(ocp, "pipe_log"):
            await ocp._settle_cancel_intent(conn, "sk", "run-1", CHAT, page, message_id=REPLY)
            await _drain()
        # reply_id None: answered once the chat is idle, though the parked reply is not done.
        self.assertEqual(owui.calls, [("answer", CHAT, "user-1", "openclaw_gateway.default", None, True)])
        conn.settle_parked_turn.assert_called_once()

    async def test_a_stop_sends_nothing(self):
        owui = _Owui(self)
        conn = types.SimpleNamespace(take_handover_cancel=lambda sk, t: False,
                                     settle_parked_turn=mock.Mock())

        async def page(_event):
            return "stop"
        with mock.patch.object(ocp, "pipe_log"), mock.patch.object(ocp, "_mark_stopped", mock.AsyncMock()):
            await ocp._settle_cancel_intent(conn, "sk", "run-1", CHAT, page, message_id=REPLY)
            await _drain()
        self.assertEqual(owui.calls, [])


class SentFromAPageTests(unittest.TestCase):
    """Only a message a chat page sends gets the handover: an API client's request waits behind
    the run as before (E2E oldest: test_messages_waiting_behind_a_run_go_to_the_agent_as_one_turn)."""

    def setUp(self):
        pool = {"sid-page": {"id": "user-1"}}
        p = mock.patch.dict(sys.modules, {
            "open_webui": _mod("open_webui"), "open_webui.socket": _mod("open_webui.socket"),
            "open_webui.socket.main": _mod("open_webui.socket.main",
                                           get_user_id_from_session_pool=lambda sid: (pool.get(sid) or {}).get("id"))})
        p.start()
        self.addCleanup(p.stop)

    def test_the_users_live_page_socket_is_a_page(self):
        self.assertIs(ocp._sent_from_a_page({"session_id": "sid-page"}, "user-1"), True)

    def test_a_made_up_session_id_is_an_api_client(self):
        self.assertIs(ocp._sent_from_a_page({"session_id": "e2e-1234"}, "user-1"), False)

    def test_another_users_socket_is_not_this_users_page(self):
        self.assertIs(ocp._sent_from_a_page({"session_id": "sid-page"}, "user-2"), False)

    def test_no_session_id_is_an_api_client(self):
        self.assertIs(ocp._sent_from_a_page({}, "user-1"), False)
        self.assertIs(ocp._sent_from_a_page(None, "user-1"), False)
