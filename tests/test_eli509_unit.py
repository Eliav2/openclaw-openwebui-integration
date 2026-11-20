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
        await asyncio.gather(*list(ocp._BACKGROUND_TASKS))
        self.assertEqual(owui.calls, [("answer", CHAT, "user-1", "openclaw_gateway.default", REPLY, True)])

    async def test_a_reply_saved_still_running_answers_nothing(self):
        owui = _Owui(self)
        await ocp._owui_upsert_message(CHAT, REPLY, {"content": "x", "done": False})
        await asyncio.gather(*list(ocp._BACKGROUND_TASKS))
        self.assertEqual(owui.calls, [])

    async def test_nothing_held_answers_nothing(self):
        owui = _Owui(self, held=False)
        await ocp._owui_upsert_message(CHAT, REPLY, {"done": True})
        await asyncio.gather(*list(ocp._BACKGROUND_TASKS))
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
        await asyncio.gather(*list(ocp._BACKGROUND_TASKS))
        self.assertEqual(owui.calls, [("answer", CHAT, "user-1", "openclaw_gateway.default", REPLY, True)])

    async def test_before_0_13_4_nothing_is_answered(self):
        owui = _Owui(self, version="0.13.3")
        await ocp._owui_upsert_message(CHAT, REPLY, {"done": True})
        await asyncio.gather(*list(ocp._BACKGROUND_TASKS))
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
