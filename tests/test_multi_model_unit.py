"""Open WebUI's multi-model chat (ELI-329): one message sent to several models
makes Open WebUI call the Pipe once per model, at the same time, for the same
chat and the same user message. Each Pipe model's reply runs in its own
OpenClaw session, so the replies do not queue behind one another, do not
overwrite each other's model, and do not take each other for a Regenerate.

Run: python3 -m pytest -q tests/test_multi_model_unit.py
"""
import asyncio
import tempfile
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402
from openclaw_pipe import Pipe, _GatewayConnection  # noqa: E402

from test_history_unit import FakeRows  # noqa: E402

CHAT, USER = "chat-1", "u1"
HAIKU, OPUS = "openclaw_gateway.anthropic/claude-haiku-5-5", "openclaw_gateway.anthropic/claude-opus-5-5"
OTHER = "ollama.llama3"   # a model that is not served by the Pipe


def _key(column=0):
    return ocp._owui_session_key("main", USER, CHAT, column)


class MultiModelBase(unittest.IsolatedAsyncioTestCase):
    """The Pipe's real request path against a fake Gateway that records every
    `sessions.patch` and `chat.send`, with the chat's rows in `self.tree`."""

    async def asyncSetUp(self):
        self.conn = _GatewayConnection(lambda: None)
        self.conn._ws = object()
        self.sends = []      # {session, message, run}
        self.patches = []    # (session, model)
        self.tree = {}
        self.n = 0

        async def send_request(method, params, timeout=10):
            if method == "chat.send":
                self.n += 1
                self.sends.append({"session": params["sessionKey"], "message": params["message"],
                                   "run": f"run-{self.n}", "key": params["idempotencyKey"]})
                return {"runId": f"run-{self.n}"}
            if method == "sessions.patch":
                self.patches.append((params["key"], params["model"]))
                provider, _, model = params["model"].partition("/")
                return {"resolved": {"modelProvider": provider, "model": model}}
            if method == "sessions.describe":
                return {"session": {"status": "done", "agentRuntime": {"id": None}}}
            if method == "sessions.list":
                return {"sessions": [{"key": s["session"], "hasActiveRun": False} for s in self.sends]}
            return {}

        async def get_conn(_valves):
            return self.conn

        self.conn.send_request = send_request
        self.rows = rows = FakeRows(self.tree)
        for p in (mock.patch.object(ocp, "_get_gateway_connection", get_conn),
                  mock.patch.object(ocp, "_build_usage_status_lines", mock.AsyncMock(return_value=[])),
                  mock.patch.object(ocp, "_owui_history_rows", lambda chat_id: rows, create=True)):
            p.start()
            self.addCleanup(p.stop)
        self.pipe = Pipe()
        self.pipe.valves.AUTO_TITLE = False
        self.pipe.valves.STATE_DIR = tempfile.mkdtemp()

    def message(self, reply_id, role, parent, text="", model=None):
        self.tree[reply_id] = (role, parent, text, model)

    async def call(self, model, models, user, reply, text, parent=None):
        """One Open WebUI call to the Pipe, answered with 'ok from <reply>'.
        Returns once the reply finished."""
        body = {"model": model, "messages": [{"role": "user", "content": text}]}
        meta = {"chat_id": CHAT, "user_id": USER, "message_id": reply, "user_message_id": user,
                "user_message": {"id": user, "parentId": parent, "models": models}}

        async def emitter(event):
            pass

        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta)
        first = asyncio.ensure_future(gen.__anext__())
        send = None
        for _ in range(600):
            send = next((s for s in self.sends if f"msg-{CHAT}-{reply}-" in s["key"]
                         and f"{s['session']}:{s['run']}" in self.conn._consumers
                         and not s.get("answered")), None)
            if send:
                break
            await asyncio.sleep(0.005)
        self.assertIsNotNone(send, f"no chat.send for {text!r}")
        send["answered"] = True
        q = self.conn._consumers[f"{send['session']}:{send['run']}"].queues[0]
        q.put_nowait({"type": "event", "event": "agent", "payload": {
            "sessionKey": send["session"], "runId": send["run"], "stream": "assistant",
            "data": {"delta": f"ok from {reply}"}}})
        q.put_nowait({"type": "event", "event": "chat", "payload": {
            "sessionKey": send["session"], "runId": send["run"], "state": "final"}})
        await asyncio.wait_for(first, 30)
        async for _ in gen:
            pass
        return send

    def sent_to(self, column):
        return [s for s in self.sends if s["session"] == _key(column)]


class PipeAndPipeModelsTests(MultiModelBase):

    async def test_two_pipe_models_answer_in_two_sessions_each_on_its_own_model(self):
        self.message("u1", "user", None, "pick one")
        models = [HAIKU, OPUS]
        a, b = await asyncio.gather(
            self.call(HAIKU, models, "u1", "a1", "pick one"),
            self.call(OPUS, models, "u1", "b1", "pick one"))
        self.assertEqual({a["session"], b["session"]}, {_key(0), _key(1)})
        # the first model keeps the chat's own session, so old chats are unchanged
        self.assertEqual(a["session"], _key(0))
        self.assertEqual(sorted(self.patches), sorted([
            (_key(0), "anthropic/claude-haiku-5-5"), (_key(1), "anthropic/claude-opus-5-5")]))

    async def test_both_models_are_sent_before_either_reply_ends(self):
        """The replies run side by side: the second model's message reaches the
        Gateway while the first is still running, not after it ends."""
        self.message("u1", "user", None, "pick one")
        models = [HAIKU, OPUS]
        bodies = {HAIKU: "a1", OPUS: "b1"}
        gens = []
        for model, reply in bodies.items():
            meta = {"chat_id": CHAT, "user_id": USER, "message_id": reply, "user_message_id": "u1",
                    "user_message": {"id": "u1", "parentId": None, "models": models}}

            async def emitter(event):
                pass

            gen = self.pipe._pipe_impl({"model": model, "messages": [{"role": "user", "content": "pick one"}]},
                                       emitter, __metadata__=meta)
            gens.append((gen, asyncio.ensure_future(gen.__anext__())))
        for _ in range(600):
            if len(self.sends) == 2:
                break
            await asyncio.sleep(0.005)
        # Neither reply has ended, and both messages are already at the Gateway.
        self.assertEqual(len(self.sends), 2, "the second model waited for the first to end")
        self.assertEqual({s["session"] for s in self.sends}, {_key(0), _key(1)})
        for _, first in gens:
            self.assertFalse(first.done())
            first.cancel()
        await asyncio.gather(*[f for _, f in gens], return_exceptions=True)

    async def test_a_second_pipe_model_is_not_taken_for_a_regenerate(self):
        self.message("u1", "user", None, "pick one")
        models = [HAIKU, OPUS]
        await asyncio.gather(self.call(HAIKU, models, "u1", "a1", "pick one"),
                             self.call(OPUS, models, "u1", "b1", "pick one"))
        for s in self.sends:
            self.assertEqual(s["message"], "pick one", s)

    async def test_the_same_columns_in_the_same_order_every_time(self):
        self.message("u1", "user", None, "q")
        await asyncio.gather(self.call(OPUS, [OPUS, HAIKU], "u1", "a1", "q"),
                             self.call(HAIKU, [OPUS, HAIKU], "u1", "b1", "q"))
        self.assertEqual(self.patches and sorted(self.patches), sorted([
            (_key(0), "anthropic/claude-opus-5-5"), (_key(1), "anthropic/claude-haiku-5-5")]))


    async def test_the_same_model_twice_gets_a_session_for_each(self):
        self.message("u1", "user", None, "twice")
        self.rows.idx.update(a1=0, b1=1)
        models = [HAIKU, HAIKU]
        a, b = await asyncio.gather(
            self.call(HAIKU, models, "u1", "a1", "twice"),
            self.call(HAIKU, models, "u1", "b1", "twice"))
        self.assertEqual((a["session"], b["session"]), (_key(0), _key(1)))
        self.assertEqual(a["message"], "twice")
        self.assertEqual(b["message"], "twice")


class PipeAndOtherModelTests(MultiModelBase):

    async def test_a_pipe_model_next_to_another_model_keeps_the_chats_session(self):
        self.message("u1", "user", None, "pick one")
        send = await self.call(HAIKU, [HAIKU, OTHER], "u1", "a1", "pick one")
        self.assertEqual(send["session"], _key(0))
        self.assertEqual(self.patches, [(_key(0), "anthropic/claude-haiku-5-5")])
        self.assertEqual(send["message"], "pick one")

    async def test_a_pipe_model_listed_second_still_keeps_the_chats_session(self):
        self.message("u1", "user", None, "pick one")
        send = await self.call(HAIKU, [OTHER, HAIKU], "u1", "a1", "pick one")
        self.assertEqual(send["session"], _key(0))

    async def test_following_up_on_the_other_models_reply_tells_the_agent_what_it_said(self):
        self.message("u1", "user", None, "pick one")
        await self.call(HAIKU, [HAIKU, OTHER], "u1", "a1", "pick one")
        # the other model's reply is the one the user continues from
        self.message("o1", "assistant", "u1", "OTHER MODEL SAID THIS IN FULL " + "x" * 400, OTHER)
        self.message("u2", "user", "o1", "go on")
        send = await self.call(HAIKU, [HAIKU], "u2", "a2", "go on", parent="o1")
        self.assertEqual(send["session"], _key(0))
        self.assertIn("OTHER MODEL SAID THIS IN FULL " + "x" * 400, send["message"])
        self.assertTrue(send["message"].endswith("go on"))


class FollowUpAndRegenerateTests(MultiModelBase):

    async def _first_round(self):
        self.message("u1", "user", None, "pick one")
        models = [HAIKU, OPUS]
        await asyncio.gather(self.call(HAIKU, models, "u1", "a1", "pick one"),
                             self.call(OPUS, models, "u1", "b1", "pick one"))
        self.message("a1", "assistant", "u1", "HAIKU ANSWER")
        self.message("b1", "assistant", "u1", "OPUS ANSWER")

    async def test_a_single_model_follow_up_goes_where_the_picked_reply_lives(self):
        await self._first_round()
        self.message("u2", "user", "b1", "go on")
        send = await self.call(HAIKU, [HAIKU], "u2", "a2", "go on", parent="b1")
        self.assertEqual(send["session"], _key(1))
        # the picked reply is already in that session: a normal send
        self.assertEqual(send["message"], "go on")

    async def test_a_follow_up_on_the_first_models_reply_stays_in_the_chats_session(self):
        await self._first_round()
        self.message("u2", "user", "a1", "go on")
        send = await self.call(HAIKU, [HAIKU], "u2", "a2", "go on", parent="a1")
        self.assertEqual(send["session"], _key(0))
        self.assertEqual(send["message"], "go on")

    async def test_a_multi_model_follow_up_gives_each_session_the_picked_reply(self):
        await self._first_round()
        self.message("u2", "user", "b1", "go on")
        models = [HAIKU, OPUS]
        a, b = await asyncio.gather(
            self.call(HAIKU, models, "u2", "a2", "go on", parent="b1"),
            self.call(OPUS, models, "u2", "b2", "go on", parent="b1"))
        self.assertEqual({a["session"], b["session"]}, {_key(0), _key(1)})
        # session 1 already has the picked reply; session 0 is told it in full
        self.assertEqual(b["message"], "go on")
        self.assertIn("OPUS ANSWER", a["message"])
        self.assertTrue(a["message"].endswith("go on"))

    async def test_regenerating_one_reply_reaches_only_its_own_session(self):
        await self._first_round()
        self.message("b1b", "assistant", "u1", "")
        send = await self.call(OPUS, [HAIKU, OPUS], "u1", "b1b", "pick one")
        self.assertEqual(send["session"], _key(1))
        self.assertIn("new answer", send["message"])
        self.assertEqual(len(self.sent_to(0)), 1)   # the other column was not touched

    async def test_a_new_column_session_gets_the_chat_so_far(self):
        # the chat had one model; the user then adds a second model to the same message
        self.message("u1", "user", None, "first")
        await self.call(HAIKU, [HAIKU], "u1", "a1", "first")
        self.message("a1", "assistant", "u1", "FIRST ANSWER")
        self.message("u2", "user", "a1", "second")
        history = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "FIRST ANSWER"},
                   {"role": "user", "content": "second"}]
        models = [HAIKU, OPUS]
        body = {"model": OPUS, "messages": history}
        meta = {"chat_id": CHAT, "user_id": USER, "message_id": "b2", "user_message_id": "u2",
                "user_message": {"id": "u2", "parentId": "a1", "models": models}}

        async def emitter(event):
            pass

        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta)
        first = asyncio.ensure_future(gen.__anext__())
        for _ in range(600):
            if any(f"msg-{CHAT}-b2-" in s["key"] for s in self.sends):
                break
            await asyncio.sleep(0.005)
        send = next(s for s in self.sends if "msg-chat-1-b2-" in s["key"])
        self.assertEqual(send["session"], _key(1))
        self.assertIn("FIRST ANSWER", send["message"])
        self.assertTrue(send["message"].endswith("second"))
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


class ColumnHelperTests(unittest.TestCase):

    def test_the_session_key_of_the_first_column_is_the_chats_own(self):
        self.assertEqual(ocp._owui_session_key("main", "u", "c"), "agent:main:openwebui-u-c")
        self.assertEqual(ocp._owui_session_key("main", "u", "c", 0), "agent:main:openwebui-u-c")
        self.assertEqual(ocp._owui_session_key("main", "u", "c", 2), "agent:main:openwebui-u-c-m2")

    def test_a_column_session_key_still_names_its_chat(self):
        uid, cid = "2d206eab-acac-4945-bd2f-7e8b6365de8c", "66ee29bc-808d-4cbc-b0d0-e6b847b3f778"
        conn = _GatewayConnection(lambda: mock.Mock(AGENT_ID="main"))
        key = ocp._owui_session_key("main", uid, cid, 1)
        self.assertEqual(conn.parse_owui_session_key(key), (uid, cid))

    def test_columns_count_only_the_pipes_own_models(self):
        col = ocp._pipe_model_column
        self.assertEqual(col(HAIKU, [HAIKU, OPUS]), (0, 2))
        self.assertEqual(col(OPUS, [HAIKU, OPUS]), (1, 2))
        self.assertEqual(col(OPUS, [OTHER, HAIKU, OPUS]), (1, 2))
        self.assertEqual(col(HAIKU, [HAIKU, OTHER]), (0, 1))
        self.assertEqual(col(HAIKU, [HAIKU]), (0, 1))
        self.assertEqual(col(HAIKU, None), (0, 1))
        self.assertEqual(col(HAIKU, []), (0, 1))

    def test_the_same_model_twice_is_told_apart_by_its_place(self):
        col = ocp._pipe_model_column
        self.assertEqual(col(HAIKU, [HAIKU, HAIKU], 0), (0, 2))
        self.assertEqual(col(HAIKU, [HAIKU, HAIKU], 1), (1, 2))
        self.assertEqual(col(HAIKU, [OTHER, HAIKU, HAIKU], 2), (1, 2))
        self.assertEqual(col(HAIKU, [HAIKU, HAIKU]), (0, 2))   # place unknown

    def test_a_model_the_list_does_not_name_falls_back_to_the_first_column(self):
        self.assertEqual(ocp._pipe_model_column(OPUS, [HAIKU]), (0, 1))


if __name__ == "__main__":
    unittest.main()
