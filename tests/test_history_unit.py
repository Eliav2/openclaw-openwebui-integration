"""What the agent remembers vs what the chat shows (ELI-183): Edit,
Regenerate, a branch switch and deleted messages each put a short note before
the message; a cloned chat's first message carries the copied messages once;
a normal send carries nothing.

Run: python3 -m pytest -q tests/test_history_unit.py
"""
import asyncio
import copy
import tempfile
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402
from openclaw_pipe import Pipe, _GatewayConnection  # noqa: E402


# Looked up when called, so on a build without the feature each test fails on
# its own instead of the whole file failing to import.
def _history_change(*a, **k):
    return ocp._history_change(*a, **k)


def _history_note(*a, **k):
    return ocp._history_note(*a, **k)


def _history_prefix(*a, **k):
    return ocp._history_prefix(*a, **k)


def _history_seed(*a, **k):
    return ocp._history_seed(*a, **k)


def _history_line_read(*a, **k):
    return ocp._history_line_read(*a, **k)


class FakeRows:
    """Open WebUI's `chat_message` rows of one chat: {id: (role, parent, text)}."""

    def __init__(self, tree: dict, origin: str | None = None):
        self.tree = tree
        self.origin_id = origin
        self.reads = 0

    async def parent(self, message_id):
        self.reads += 1
        row = self.tree.get(message_id)
        return (row is not None, row[1] if row else None)

    async def text(self, message_id):
        row = self.tree.get(message_id)
        return row[2] if row else None

    async def origin(self):
        return self.origin_id


def _chain(*ids, start=None):
    """A branch: user/assistant alternating, each the child of the one before."""
    tree, parent = {}, start
    for i, mid in enumerate(ids):
        tree[mid] = ("user" if i % 2 == 0 else "assistant", parent, f"text of {mid}")
        parent = mid
    return tree


class HistoryChangeTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_normal_send_changes_nothing(self):
        tree = _chain("u1", "a1", "u2", "a2")
        change = await _history_change(["u1", "a1"], "u2", "a2", FakeRows(tree))
        self.assertIsNone(change["kind"])
        self.assertEqual(change["tail"], ["u1", "a1", "u2", "a2"])

    async def test_regenerate_drops_the_earlier_answer(self):
        tree = _chain("u1", "a1")
        tree["a1b"] = ("assistant", "u1", "")
        change = await _history_change(["u1", "a1"], "u1", "a1b", FakeRows(tree))
        self.assertEqual((change["kind"], change["replaced"], change["shown"]), ("regenerate", ["a1"], []))
        self.assertEqual(change["tail"], ["u1", "a1b"])

    async def test_an_edit_replaces_the_message_and_what_followed(self):
        tree = _chain("u1", "a1", "u2", "a2", "u3", "a3")
        tree["u2b"] = ("user", "a1", "edited")
        change = await _history_change(["u1", "a1", "u2", "a2", "u3", "a3"], "u2b", "a2b", FakeRows(tree))
        self.assertEqual((change["kind"], change["replaced"], change["shown"]),
                         ("edit", ["u2", "a2", "u3", "a3"], []))
        self.assertEqual(change["tail"], ["u1", "a1", "u2b", "a2b"])

    async def test_editing_the_first_message_replaces_the_whole_line(self):
        tree = _chain("u1", "a1")
        tree["u1b"] = ("user", None, "edited")
        change = await _history_change(["u1", "a1"], "u1b", "a1b", FakeRows(tree))
        self.assertEqual((change["kind"], change["replaced"]), ("edit", ["u1", "a1"]))

    async def test_a_deleted_tail_is_not_an_edit(self):
        tree = _chain("u1", "a1")          # u2, a2 deleted in Open WebUI
        tree["u3"] = ("user", "a1", "next")
        change = await _history_change(["u1", "a1", "u2", "a2"], "u3", "a3", FakeRows(tree))
        self.assertEqual((change["kind"], change["replaced"]), ("deleted", ["u2", "a2"]))

    async def test_a_branch_switch_names_both_sides(self):
        tree = _chain("u1", "a1", "u2", "a2")
        tree.update(_chain("u2b", "a2b", start="a1"))
        tree["u3"] = ("user", "a2b", "on the other branch")
        change = await _history_change(["u1", "a1", "u2", "a2"], "u3", "a3", FakeRows(tree))
        self.assertEqual((change["kind"], change["replaced"], change["shown"]),
                         ("branch", ["u2", "a2"], ["u2b", "a2b"]))
        self.assertEqual(change["tail"], ["u1", "a1", "u2b", "a2b", "u3", "a3"])

    async def test_messages_the_agent_added_itself_change_nothing(self):
        # A cron wake's reply under the last answer: the chat gained a message,
        # the agent lost none.
        tree = _chain("u1", "a1")
        tree["wake"] = ("assistant", "a1", "a reply the agent started")
        tree["u2"] = ("user", "wake", "thanks")
        change = await _history_change(["u1", "a1"], "u2", "a2", FakeRows(tree))
        self.assertIsNone(change["kind"])
        self.assertEqual(change["tail"], ["u1", "a1", "wake", "u2", "a2"])

    async def test_a_user_row_not_written_yet_uses_the_metadata_parent(self):
        tree = _chain("u1", "a1")
        change = await _history_change(["u1", "a1"], "u2", "a2", FakeRows(tree), user_parent="a1")
        self.assertIsNone(change["kind"])
        self.assertEqual(change["tail"], ["u1", "a1", "u2", "a2"])

    async def test_the_line_keeps_its_newest_ids_and_the_walk_is_bounded(self):
        ids = [f"m{i}" for i in range(200)]
        tree = _chain(*ids)
        rows = FakeRows(tree)
        change = await _history_change([], "m198", "m199", rows)
        self.assertEqual(change["tail"], ids[-ocp.HISTORY_TAIL_MAX:])
        self.assertLessEqual(rows.reads, ocp.HISTORY_TAIL_MAX + 2)


class HistoryNoteTests(unittest.IsolatedAsyncioTestCase):

    async def _note(self, tail, user, reply, tree):
        rows = FakeRows(tree)
        return await _history_note(await _history_change(tail, user, reply, rows), rows)

    async def test_the_notes_say_what_changed_briefly(self):
        tree = _chain("u1", "a1", "u2", "a2")
        tree["a2b"] = ("assistant", "u2", "")
        regen = await self._note(["u1", "a1", "u2", "a2"], "u2", "a2b", tree)
        self.assertIn("new answer to the message below", regen)
        self.assertNotIn("after it", regen)

        tree["u2b"] = ("user", "a1", "edited")
        tree["u2"] = ("user", "a1", "what is the weather in a very long sentence that goes on and on and on")
        edit = await self._note(["u1", "a1", "u2", "a2"], "u2b", "a2c", tree)
        self.assertIn('edited their message "what is the weather', edit)
        self.assertIn("…", edit)
        self.assertIn("the 1 message after it", edit)

        tree.update(_chain("u2c", "a2d", start="a1"))
        tree["u3"] = ("user", "a2d", "")
        branch = await self._note(["u1", "a1", "u2", "a2"], "u3", "a3", tree)
        self.assertIn('2 messages from "what is the weather', branch)
        self.assertIn('2 messages from "text of u2c" on', branch)

        del tree["u2"], tree["a2"]
        tree["u4"] = ("user", "a1", "")
        deleted = await self._note(["u1", "a1", "u2", "a2"], "u4", "a4", tree)
        self.assertIn("the last 2 messages you remember were deleted", deleted)

        for note in (regen, edit, branch, deleted):
            self.assertLess(len(note.split()), 60, note)
            self.assertNotIn("\u2014", note)


class HistorySeedTests(unittest.TestCase):

    def test_the_copy_keeps_the_newest_messages_that_fit_and_says_what_was_cut(self):
        big = "x" * 9000
        messages = [{"role": "system", "content": "sys"}]
        for i in range(6):
            messages += [{"role": "user", "content": f"Q{i} {big}"},
                         {"role": "assistant", "content": f"A{i} <details type=\"tool_calls\">tool junk</details>{big}"}]
        seed = _history_seed(messages)
        self.assertLessEqual(len(seed), ocp.HISTORY_SEED_MAX_CHARS + 400)
        self.assertIn("the oldest 9 messages are left out to fit", seed)
        self.assertIn("A5", seed)
        self.assertNotIn("Q0", seed)
        self.assertNotIn("tool junk", seed)
        self.assertNotIn("sys", seed.split(":]", 1)[1])
        self.assertTrue(seed.rstrip().endswith("The user's new message follows.]"))

    def test_a_single_message_longer_than_the_cap_keeps_its_end(self):
        seed = _history_seed([{"role": "assistant", "content": "start " + "y" * 50000 + " END"}])
        self.assertIn("END", seed)
        self.assertNotIn("start", seed)
        self.assertLessEqual(len(seed), ocp.HISTORY_SEED_MAX_CHARS + 400)

    def test_nothing_to_copy_is_no_seed(self):
        self.assertEqual(_history_seed([{"role": "system", "content": "sys"}]), "")


class FakeConn:
    def __init__(self, sessions=()):
        self.sessions = list(sessions)
        self.calls = []

    async def send_request(self, method, params, timeout=10):
        self.calls.append(method)
        return {"sessions": [{"key": k} for k in self.sessions]}


class HistoryPrefixTests(unittest.IsolatedAsyncioTestCase):
    KEY = "agent:main:openwebui-u1-chat-1"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tree = _chain("u1", "a1")
        self.rows = FakeRows(self.tree)
        patch = mock.patch.object(ocp, "_owui_history_rows", lambda chat_id: self.rows, create=True)
        patch.start()
        self.addCleanup(patch.stop)

    async def _prefix(self, user, reply, messages=None, conn=None):
        meta = {"chat_id": "chat-1", "user_message_id": user, "message_id": reply}
        return await _history_prefix(conn or FakeConn(), self.dir, "chat-1", self.KEY, meta,
                                     messages or [{"role": "user", "content": "hi"}])

    async def test_a_second_call_for_the_same_reply_sends_the_same_text(self):
        await self._prefix("u1", "a1")
        self.tree["a1b"] = ("assistant", "u1", "")
        first = await self._prefix("u1", "a1b")
        self.assertIn("new answer", first)
        self.assertEqual(await self._prefix("u1", "a1b"), first)
        self.assertEqual(_history_line_read(self.dir, "chat-1")["tail"], ["u1", "a1b"])

    async def test_a_turn_with_no_user_message_leaves_the_line_alone(self):
        meta = {"chat_id": "chat-1", "message_id": "stub"}
        self.assertEqual(await _history_prefix(FakeConn(), self.dir, "chat-1", self.KEY, meta, []), "")
        self.assertEqual(_history_line_read(self.dir, "chat-1"), {"tail": []})

    async def test_a_clone_is_seeded_once_and_only_without_a_session(self):
        self.rows.origin_id = "source-chat"
        self.tree["u2"] = ("user", "a1", "go on")
        copied = [{"role": "user", "content": "COPIED_QUESTION"},
                  {"role": "assistant", "content": "COPIED_ANSWER"},
                  {"role": "user", "content": "go on"}]
        prefix = await self._prefix("u2", "a2", copied)
        self.assertIn("COPIED_QUESTION", prefix)
        self.assertIn("copy of another chat", prefix)
        self.tree["a2"] = ("assistant", "u2", "")
        self.tree["u3"] = ("user", "a2", "")
        self.assertEqual(await self._prefix("u3", "a3", copied + [{}, {"role": "user", "content": "x"}]), "")

    async def test_a_clone_the_gateway_already_has_is_not_seeded(self):
        self.rows.origin_id = "source-chat"
        self.tree["u2"] = ("user", "a1", "go on")
        copied = [{"role": "user", "content": "COPIED"}, {"role": "assistant", "content": "A"},
                  {"role": "user", "content": "go on"}]
        self.assertEqual(await self._prefix("u2", "a2", copied, FakeConn([self.KEY])), "")

    async def test_a_chat_that_is_no_copy_is_not_seeded(self):
        self.tree["u2"] = ("user", "a1", "go on")
        copied = [{"role": "user", "content": "OLD"}, {"role": "assistant", "content": "A"},
                  {"role": "user", "content": "go on"}]
        self.assertEqual(await self._prefix("u2", "a2", copied), "")


class HistoryIntakeTests(unittest.IsolatedAsyncioTestCase):
    """Through the Pipe's real request path: what `chat.send` carries."""

    async def asyncSetUp(self):
        self.conn = _GatewayConnection(lambda: None)
        self.conn._ws = object()
        self.sent = []
        self.n = 0

        async def send_request(method, params, timeout=10):
            if method == "chat.send":
                self.n += 1
                self.sent.append(params.get("message"))
                return {"runId": f"run-{self.n}"}
            if method == "sessions.describe":
                return {"session": {"status": "done", "agentRuntime": {"id": None}}}
            if method == "sessions.list":
                return {"sessions": [{"key": self.sk, "hasActiveRun": False}]}
            return {}

        async def get_conn(_valves):
            return self.conn

        self.conn.send_request = send_request
        self.conn.model_patch_cached = lambda *a, **k: True
        self.tree = _chain("u1", "a1", "u2", "a2")
        rows = FakeRows(self.tree)
        for p in (mock.patch.object(ocp, "_get_gateway_connection", get_conn),
                  mock.patch.object(ocp, "_build_usage_status_lines", mock.AsyncMock(return_value=[])),
                  mock.patch.object(ocp, "_owui_history_rows", lambda chat_id: rows, create=True)):
            p.start()
            self.addCleanup(p.stop)
        self.pipe = Pipe()
        self.pipe.valves.AUTO_TITLE = False
        self.pipe.valves.STATE_DIR = tempfile.mkdtemp()
        self.sk = ocp._owui_session_key(self.pipe.valves.AGENT_ID, "u1", "chat-1")

    async def _turn(self, user, reply, text):
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": text}]}
        meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": reply, "user_message_id": user}

        async def emitter(event):
            pass

        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta)
        first = asyncio.ensure_future(gen.__anext__())
        for _ in range(400):
            if f"{self.sk}:run-{self.n}" in self.conn._consumers and len(self.sent) >= self.n > 0:
                break
            await asyncio.sleep(0.005)
        q = self.conn._consumers[f"{self.sk}:run-{self.n}"].queues[0]
        q.put_nowait({"type": "event", "event": "agent", "payload": {
            "sessionKey": self.sk, "runId": f"run-{self.n}", "stream": "assistant",
            "data": {"delta": "ok"}}})
        q.put_nowait({"type": "event", "event": "chat", "payload": {
            "sessionKey": self.sk, "runId": f"run-{self.n}", "state": "final"}})
        await asyncio.wait_for(first, 30)
        async for _ in gen:
            pass
        return self.sent[-1]

    async def test_normal_sends_carry_only_the_message(self):
        self.assertEqual(await self._turn("u1", "a1", "first"), "first")
        self.assertEqual(await self._turn("u2", "a2", "second"), "second")

    async def test_an_edited_message_reaches_the_agent_with_the_note(self):
        await self._turn("u1", "a1", "first")
        await self._turn("u2", "a2", "second")
        self.tree["u2b"] = ("user", "a1", "second, edited")
        sent = await self._turn("u2b", "a2b", "second, edited")
        self.assertTrue(sent.startswith('[Open WebUI: the user edited their message "text of u2"'), sent)
        self.assertTrue(sent.endswith("\n\nsecond, edited"), sent)

    async def test_a_regenerated_reply_reaches_the_agent_with_the_note(self):
        await self._turn("u1", "a1", "first")
        self.tree["a1b"] = ("assistant", "u1", "")
        sent = await self._turn("u1", "a1b", "first")
        self.assertTrue(sent.startswith("[Open WebUI: the user asked for a new answer"), sent)


if __name__ == "__main__":
    unittest.main()
