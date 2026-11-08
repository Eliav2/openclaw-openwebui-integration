"""ELI-379: a chat's files reach the agent's session once. Open WebUI resends the chat's
files with every request and wraps what it retrieves from them into the newest user
message. A stateless model pays that once per request; the agent's session kept every
copy (a quoted chat: 64 copies, 9.4M characters). Each retrieved chunk now goes once per
Gateway session; later turns name its file in one line instead."""
import tempfile
import unittest

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

from test_multi_model_unit import HAIKU, MultiModelBase  # noqa: E402

TYPED = "what does the plan say about Kyoto?"
CHUNK_A = "Day 3: Kyoto, Fushimi Inari at 7:00."
CHUNK_B = "Day 4: Nara, deer park."
CHUNK_NEW = "Day 5: Osaka, Dotonbori."
NOTE = "sent to you earlier in this conversation and not repeated here: plan.md"
TEMPLATE = "### Task:\nRespond using the context.\n<context>\n{}</context>\n"


def source(*chunks, name="plan.md"):
    return {"source": {"id": "file-1", "name": name}, "document": list(chunks),
            "metadata": [{"source": "file-1"} for _ in chunks]}


def wrapped(*chunks, typed=TYPED):
    """The message as Open WebUI sends it: its RAG template, a newline, the typed text."""
    tags = "".join(f'<source id="{i + 1}" name="plan.md">{c}</source>\n' for i, c in enumerate(chunks))
    return TEMPLATE.format(tags) + "\n" + typed


class FakeConn:
    """sessions.list with one row whose sessionId and compactionCount a test sets."""

    def __init__(self, key):
        self.key, self.row, self.fail = key, None, False

    async def send_request(self, method, params, timeout=10):
        if self.fail:
            raise RuntimeError("socket closed")
        return {"sessions": [self.row] if self.row else []}


class FileContextTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.key = "agent:main:openwebui-u-c"
        self.conn = FakeConn(self.key)
        ocp._file_context_pending.clear()

    def session(self, session_id, compactions=None):
        self.conn.row = {"key": self.key, "sessionId": session_id,
                         **({"compactionCount": compactions} if compactions is not None else {})}

    async def turn(self, reply, *chunks, sent=True):
        """One message's file context; committed as sent unless `sent` is False."""
        context = ocp._file_context_split(wrapped(*chunks), TYPED)[0]
        text, record = await ocp._file_context_message(self.conn, self.state, self.key, reply,
                                                       context, [source(*chunks)])
        ocp._file_context_hold(self.key, text, record)
        if sent:
            ocp._file_context_commit(self.state, self.key, [text])
        return text

    def test_split_keeps_the_typed_text_apart(self):
        context, typed = ocp._file_context_split(wrapped(CHUNK_A), TYPED)
        self.assertEqual(typed, TYPED)
        self.assertIn(CHUNK_A, context)
        self.assertIsNone(ocp._file_context_split(TYPED, TYPED), "not wrapped")
        self.assertIsNone(ocp._file_context_split(wrapped(CHUNK_A), None), "no base_user_prompt")

    async def test_the_first_turn_goes_whole(self):
        self.assertIn(CHUNK_A, await self.turn("r1", CHUNK_A, CHUNK_B))

    async def test_the_second_turn_collapses_to_one_line(self):
        await self.turn("r1", CHUNK_A, CHUNK_B)
        self.session("s1")
        text = await self.turn("r2", CHUNK_A, CHUNK_B)
        self.assertNotIn(CHUNK_A, text)
        self.assertNotIn(CHUNK_B, text)
        self.assertIn(NOTE, text)

    async def test_a_new_or_changed_chunk_is_sent(self):
        await self.turn("r1", CHUNK_A)
        self.session("s1")
        text = await self.turn("r2", CHUNK_A, CHUNK_NEW)
        self.assertIn(CHUNK_NEW, text)
        self.assertNotIn(CHUNK_A, text)
        self.assertIn("<context>", text, "Open WebUI's template stays around the new chunk")
        self.assertIn(NOTE, text)

    async def test_the_same_reply_builds_the_same_text(self):
        """Open WebUI may call twice for one reply (ELI-82): the second call must match the first."""
        self.session("s1")
        first = await self.turn("r1", CHUNK_A)
        self.assertEqual(await self.turn("r1", CHUNK_A), first)

    async def test_a_new_session_clears_the_record(self):
        """/new, /reset or a deleted session: a new sessionId has none of the chunks."""
        self.session("s1")
        await self.turn("r1", CHUNK_A)
        self.session("s2")
        self.assertIn(CHUNK_A, await self.turn("r2", CHUNK_A))

    async def test_a_compaction_clears_the_record(self):
        self.session("s1", compactions=0)
        await self.turn("r1", CHUNK_A)
        self.session("s1", compactions=1)
        self.assertIn(CHUNK_A, await self.turn("r2", CHUNK_A))

    async def test_a_session_this_turn_created_keeps_the_record(self):
        """No row before the first send ([]): the next turn reads the id that send created."""
        await self.turn("r1", CHUNK_A)
        self.session("s1")
        self.assertNotIn(CHUNK_A, await self.turn("r2", CHUNK_A))

    async def test_a_message_never_sent_records_nothing(self):
        self.session("s1")
        await self.turn("r1", CHUNK_A, sent=False)
        self.assertIn(CHUNK_A, await self.turn("r2", CHUNK_A))

    async def test_a_failed_lookup_sends_whole(self):
        self.session("s1")
        await self.turn("r1", CHUNK_A)
        self.conn.fail = True
        self.assertIn(CHUNK_A, await self.turn("r2", CHUNK_A))


class PipeFileContextTests(MultiModelBase):
    """The Pipe's request path: what chat.send carries over three turns."""

    async def ask(self, n, parent=None):
        self.message(f"u{n}", "user", parent, TYPED)
        send = await self.call(HAIKU, [HAIKU], f"u{n}", f"a{n}", wrapped(CHUNK_A, CHUNK_B),
                               parent=parent,
                               extra_meta={"base_user_prompt": TYPED, "sources": [source(CHUNK_A, CHUNK_B)]})
        self.message(f"a{n}", "assistant", f"u{n}", f"ok from a{n}")
        return send["message"]

    async def test_the_chat_files_go_once_over_three_turns(self):
        first, second, third = await self.ask(1), await self.ask(2, "a1"), await self.ask(3, "a2")
        self.assertIn(CHUNK_A, first)
        for later in (second, third):
            self.assertNotIn(CHUNK_A, later)
            self.assertIn(NOTE, later)
            self.assertTrue(later.endswith(TYPED), later)
        self.assertEqual(len(second), len(third))

    async def test_a_command_goes_as_typed_without_the_file_context(self):
        self.message("u1", "user", None, "/status")
        send = await self.call(HAIKU, [HAIKU], "u1", "a1", wrapped(CHUNK_A, typed="/status"),
                               extra_meta={"base_user_prompt": "/status", "sources": [source(CHUNK_A)]})
        self.assertEqual(send["message"], "/status")


if __name__ == "__main__":
    unittest.main()
