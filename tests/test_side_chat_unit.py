"""A side chat (Open WebUI 0.13 /side, /btw) or a temporary chat: its own session,
seeded once with the messages it starts from, and that session is deleted once Open
WebUI has forgotten the chat."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

from test_multi_model_unit import CHAT, HAIKU, USER, MultiModelBase, _key  # noqa: E402

# A side chat's id: its socket's id, then its own uuid (SideChat.svelte createTemporaryChatId).
SIDE = "temporary:jicrHYe0t7QTfdKOAAAB:a25e0788-e7f6-49a0-8373-b26cb2c5f307"
PARENT = [{"role": "system", "content": "This is a separate side conversation."},
          {"role": "user", "content": "PARENT_CTX the secret word is PAPAYA"},
          {"role": "assistant", "content": "noted"}]


class SideChatTurnTests(MultiModelBase):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.started = []

        async def started(*args):
            self.started.append(args)
        patcher = mock.patch.object(ocp, "_temporary_sessions_tend", started)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_the_first_side_message_brings_the_chat_it_was_opened_from(self):
        send = await self.call(HAIKU, [HAIKU], "s1", "sa1", "SIDE_Q what is the word?", chat=SIDE,
                               earlier=PARENT)
        self.assertTrue(send["message"].startswith("[Open WebUI: this is a temporary chat"))
        self.assertIn("User: PARENT_CTX the secret word is PAPAYA", send["message"])
        self.assertIn("Assistant: noted", send["message"])
        self.assertTrue(send["message"].endswith("The user's new message follows.]\n\nSIDE_Q what is the word?"))

    async def test_open_webui_s_side_chat_instruction_comes_as_the_system_prompt(self):
        """The side chat's system message (Open WebUI's "separate side conversation"
        line) reaches the agent once, in the system prompt note before the seed."""
        send = await self.call(HAIKU, [HAIKU], "s1", "sa1", "SIDE_Q", chat=SIDE, earlier=PARENT,
                               extra_meta={"chat_context": {"messages": PARENT}})
        side_line = PARENT[0]["content"]
        self.assertTrue(send["message"].startswith("[Open WebUI system prompt for this chat"))
        self.assertEqual(send["message"].count(side_line), 1)
        self.assertLess(send["message"].index(side_line),
                        send["message"].index("[Open WebUI: this is a temporary chat"))

    async def test_it_runs_in_its_own_session_never_the_parent_s(self):
        send = await self.call(HAIKU, [HAIKU], "s1", "sa1", "SIDE_Q", chat=SIDE, earlier=PARENT)
        self.assertEqual(send["session"], ocp._owui_session_key("main", USER, SIDE))
        self.assertNotEqual(send["session"], _key())

    async def test_the_next_side_message_brings_nothing_more(self):
        await self.call(HAIKU, [HAIKU], "s1", "sa1", "SIDE_Q one", chat=SIDE, earlier=PARENT)
        later = PARENT + [{"role": "user", "content": "SIDE_Q one"}, {"role": "assistant", "content": "ok from sa1"}]
        send = await self.call(HAIKU, [HAIKU], "s2", "sa2", "SIDE_Q two", parent="sa1", chat=SIDE,
                               earlier=later)
        self.assertEqual(send["message"], "SIDE_Q two")

    async def test_a_temporary_chat_s_first_message_alone_is_sent_as_it_is(self):
        send = await self.call(HAIKU, [HAIKU], "s1", "sa1", "hi", chat=SIDE)
        self.assertEqual(send["message"], "hi")

    async def test_a_saved_chat_is_not_seeded(self):
        self.message("u1", "user", None, "hi")
        send = await self.call(HAIKU, [HAIKU], "u1", "a1", "hi", earlier=PARENT[1:])
        self.assertEqual(send["message"], "hi")

    async def test_every_turn_tends_the_user_s_temporary_sessions(self):
        """A saved chat's turn too: a side chat's session goes even if the user
        never opens another side chat."""
        await self.call(HAIKU, [HAIKU], "s1", "sa1", "SIDE_Q", chat=SIDE, earlier=PARENT)
        self.message("u1", "user", None, "hi")
        await self.call(HAIKU, [HAIKU], "u1", "a1", "hi")
        for _ in range(100):
            if len(self.started) == 2:
                break
            await asyncio.sleep(0.005)
        self.assertEqual([(a[3], a[4], a[5]) for a in self.started],
                         [(ocp._owui_session_key("main", USER, SIDE), SIDE, USER), (_key(), CHAT, USER)])

    async def test_a_failed_session_lookup_does_not_seed(self):
        """The session may exist: seeding it again would repeat the chat. Unknown
        is not missing."""
        answer = self.conn.send_request

        async def lookup_fails(method, params, timeout=10):
            if method == "sessions.list":
                raise RuntimeError("sessions.list timed out")
            return await answer(method, params, timeout)
        self.conn.send_request = lookup_fails
        send = await self.call(HAIKU, [HAIKU], "s1", "sa1", "SIDE_Q", chat=SIDE, earlier=PARENT)
        self.assertEqual(send["message"], "SIDE_Q")


class TemporaryChatIdTests(unittest.TestCase):

    def test_open_webui_s_prefixes(self):
        for chat_id in (SIDE, "temporary:abc", "local:abc"):
            self.assertTrue(ocp._is_temporary_chat(chat_id), chat_id)
        for chat_id in (CHAT, None, "", "channel:abc", "a-temporary:b"):
            self.assertFalse(ocp._is_temporary_chat(chat_id), chat_id)

    def test_the_session_key_is_in_the_gateway_s_case(self):
        """The Gateway stores keys in lower case and names them so in its events;
        a side chat's id carries its socket's id, in mixed case."""
        key = ocp._owui_session_key("main", USER, SIDE)
        self.assertEqual(key, key.lower())
        self.assertTrue(key.endswith(SIDE.lower()))

    def test_a_key_is_parsed_back_whatever_the_agent_id_s_case(self):
        """Keys are built lower case; an AGENT_ID with upper case still names its chat."""
        uid, cid = "2d206eab-acac-4945-bd2f-7e8b6365de8c", "66ee29bc-808d-4cbc-b0d0-e6b847b3f778"
        conn = ocp._GatewayConnection(lambda: mock.Mock(AGENT_ID="Main"))
        self.assertEqual(conn.parse_owui_session_key(ocp._owui_session_key("Main", uid, cid)), (uid, cid))

    def test_a_saved_chat_s_key_is_unchanged(self):
        self.assertEqual(ocp._owui_session_key("main", USER, CHAT), f"agent:main:openwebui-{USER}-{CHAT}")

    def test_a_listed_session_is_found(self):
        key = ocp._owui_session_key("main", USER, SIDE)
        self.assertTrue(ocp._lists_session({"sessions": [{"key": key}]}, key))
        self.assertFalse(ocp._lists_session({"sessions": [{"key": key + "-m1"}]}, key))
        self.assertFalse(ocp._lists_session({}, key))


class FakeGateway:
    """sessions.list / patch / delete on a set of keys, stored lower case."""

    def __init__(self, keys, fail=()):
        self.keys = {k.lower() for k in keys}
        self.calls = []
        self.fail = set(fail)

    async def send_request(self, method, params, timeout=10):
        self.calls.append((method, params))
        if method in self.fail:
            raise RuntimeError(f"{method} failed")
        if method == "sessions.list":
            return {"sessions": [{"key": k} for k in self.keys if params["search"].lower() in k]}
        if method == "sessions.delete":
            assert params["archivedOnly"] is True
            self.keys.discard(params["key"].lower())
        return {}


def _side(n):
    return f"temporary:SockEt{n}:00000000-0000-4000-8000-00000000000{n}"


class TemporarySessionCleanupTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.state = tempfile.mkdtemp()
        self.alive = set()

        async def alive(request, chat_id):
            return chat_id in self.alive
        async def run_request(conn, method, params, **kw):
            return await conn.send_request(method, params)
        # The router picks the owner's or the restricted connection; one fake here.
        for patcher in (mock.patch.object(ocp, "_temporary_chat_alive", alive),
                        mock.patch.object(ocp, "_run_request", run_request)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def key(self, n, user=USER):
        return ocp._owui_session_key("main", user, _side(n))

    async def start(self, gw, n, user=USER):
        await ocp._temporary_sessions_tend(gw, None, self.state, self.key(n, user), _side(n), user)

    def records(self):
        with open(os.path.join(self.state, ocp.TEMPORARY_SESSIONS_FILE)) as fh:
            return json.load(fh)

    async def test_a_session_open_webui_forgot_is_archived_then_deleted_when_the_next_one_starts(self):
        gw = FakeGateway([self.key(1), self.key(2)])
        await self.start(gw, 1)
        await self.start(gw, 2)
        self.assertEqual(gw.keys, {self.key(2).lower()})
        methods = [m for m, p in gw.calls if p.get("key") == self.key(1)]
        self.assertEqual(methods, ["sessions.patch", "sessions.delete"])
        self.assertEqual(list(self.records()), [self.key(2)])

    async def test_a_saved_chat_s_turn_deletes_them_and_records_nothing(self):
        gw = FakeGateway([self.key(1)])
        await self.start(gw, 1)
        await ocp._temporary_sessions_tend(gw, None, self.state, _key(), CHAT, USER)
        self.assertEqual(gw.keys, set())
        self.assertEqual(self.records(), {})

    async def test_a_saved_chat_s_turn_with_no_record_writes_nothing(self):
        gw = FakeGateway([])
        await ocp._temporary_sessions_tend(gw, None, self.state, _key(), CHAT, USER)
        self.assertEqual(gw.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.state, ocp.TEMPORARY_SESSIONS_FILE)))

    async def test_a_chat_open_webui_still_keeps_is_left(self):
        self.alive.add(_side(1))
        gw = FakeGateway([self.key(1), self.key(2)])
        await self.start(gw, 1)
        await self.start(gw, 2)
        self.assertEqual(gw.keys, {self.key(1).lower(), self.key(2).lower()})
        self.assertEqual(sorted(self.records()), sorted([self.key(1), self.key(2)]))

    async def test_another_user_s_sessions_are_left(self):
        gw = FakeGateway([self.key(1, "other"), self.key(2)])
        await self.start(gw, 1, "other")
        await self.start(gw, 2)
        self.assertIn(self.key(1, "other").lower(), gw.keys)

    async def test_the_chat_that_just_started_is_never_deleted(self):
        gw = FakeGateway([self.key(1)])
        await self.start(gw, 1)
        self.assertEqual(gw.keys, {self.key(1).lower()})

    async def test_a_session_already_gone_is_dropped_without_a_delete(self):
        gw = FakeGateway([self.key(2)])
        await self.start(gw, 1)
        await self.start(gw, 2)
        self.assertNotIn("sessions.delete", [m for m, _ in gw.calls])
        self.assertEqual(list(self.records()), [self.key(2)])

    async def test_a_failed_delete_keeps_the_record_for_the_next_start(self):
        gw = FakeGateway([self.key(1), self.key(2), self.key(3)], fail={"sessions.delete"})
        await self.start(gw, 1)
        await self.start(gw, 2)
        self.assertIn(self.key(1), self.records())
        gw.fail.clear()
        await self.start(gw, 3)
        self.assertEqual(gw.keys, {self.key(3).lower()})

    async def test_a_failed_lookup_keeps_the_record(self):
        gw = FakeGateway([self.key(1), self.key(2)], fail={"sessions.list"})
        await self.start(gw, 1)
        await self.start(gw, 2)
        self.assertIn(self.key(1), self.records())


class TemporaryChatAliveTests(unittest.IsolatedAsyncioTestCase):

    async def test_when_open_webui_can_t_tell_the_chat_is_kept(self):
        # The stubs have no open_webui.utils.chat.
        self.assertTrue(await ocp._temporary_chat_alive(None, _side(1)))

    async def alive(self, workers, redis):
        """`_temporary_chat_alive` on a stand-in Open WebUI that has forgotten the chat."""
        import sys
        import types
        mods = {n: types.ModuleType(n) for n in ("open_webui", "open_webui.env", "open_webui.utils",
                                                  "open_webui.utils.chat")}
        mods["open_webui.env"].UVICORN_WORKERS = workers

        async def get_temporary_chat(redis, chat_id):
            return None
        mods["open_webui.utils.chat"].get_temporary_chat = get_temporary_chat
        request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(redis=redis)))
        with mock.patch.dict(sys.modules, mods):
            return await ocp._temporary_chat_alive(request, _side(1))

    async def test_a_forgotten_chat_is_gone_with_one_worker_or_with_redis(self):
        self.assertFalse(await self.alive(1, None))
        self.assertFalse(await self.alive(4, object()))

    async def test_several_workers_without_redis_can_t_tell_so_the_chat_is_kept(self):
        """Each worker keeps its own temporary chats: another's may be alive."""
        self.assertTrue(await self.alive(4, None))


if __name__ == "__main__":
    unittest.main()
