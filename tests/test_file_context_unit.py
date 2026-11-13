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


def wrapped(*chunks, typed=TYPED, name="plan.md"):
    """The message as Open WebUI sends it: its RAG template, a newline, the typed text."""
    tags = "".join(f'<source id="{i + 1}" name="{name}">{c}</source>\n' for i, c in enumerate(chunks))
    return TEMPLATE.format(tags) + "\n" + typed


CHAT_ID = "2fbfd87b-0000-4000-8000-000000000000"
CHAT_NAME = "🗾 טופס כניסה ליפן</br>"


def chat_source(text, chat_id=CHAT_ID):
    """A whole chat referenced in the message, as Open WebUI retrieves it."""
    return {"source": {"type": "chat", "id": chat_id, "name": CHAT_NAME}, "document": [text],
            "metadata": [{"file_id": chat_id, "name": CHAT_NAME}]}


def chat_text(n, size=40):
    return "".join(f"#### {'User' if i % 2 == 0 else 'Assistant'}\nmessage {i} " + "x" * size + "\n\n"
                   for i in range(n))


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

    async def turn(self, reply, *chunks, sent=True, sources=None, after_send=None, name="plan.md"):
        """One message's file context; committed as sent unless `sent` is False.
        `after_send` changes the session between the send and its commit."""
        context = ocp._file_context_split(wrapped(*chunks, name=name), TYPED)[0]
        text, record = await ocp._file_context_message(self.conn, self.state, self.key, reply,
                                                       context, sources or [source(*chunks)])
        ocp._file_context_hold(self.key, text, record)
        if after_send:
            after_send()
        if sent:
            await ocp._file_context_commit(self.conn, self.state, self.key, [text])
        return text

    def test_split_keeps_the_typed_text_apart(self):
        context, typed = ocp._file_context_split(wrapped(CHUNK_A), TYPED)
        self.assertEqual(typed, TYPED)
        self.assertIn(CHUNK_A, context)
        self.assertIsNone(ocp._file_context_split(TYPED, TYPED), "not wrapped")
        self.assertIsNone(ocp._file_context_split(wrapped(CHUNK_A), None), "no base_user_prompt")

    async def test_the_first_turn_goes_whole(self):
        self.session("s1")
        self.assertIn(CHUNK_A, await self.turn("r1", CHUNK_A, CHUNK_B))

    async def test_the_second_turn_keeps_the_tags_without_their_text(self):
        """The [n] citations stay valid: each chunk keeps its <source> tag and number."""
        self.session("s1")
        await self.turn("r1", CHUNK_A, CHUNK_B)
        text = await self.turn("r2", CHUNK_A, CHUNK_B)
        self.assertNotIn(CHUNK_A, text)
        self.assertNotIn(CHUNK_B, text)
        self.assertIn('<source id="1" name="plan.md"></source>', text)
        self.assertIn('<source id="2" name="plan.md"></source>', text)
        self.assertIn("Respond using the context.", text, "the rest of Open WebUI's template stays")
        self.assertIn(NOTE, text)

    async def test_a_new_or_changed_chunk_is_sent(self):
        self.session("s1")
        await self.turn("r1", CHUNK_A)
        text = await self.turn("r2", CHUNK_A, CHUNK_NEW)
        self.assertIn(CHUNK_NEW, text)
        self.assertNotIn(CHUNK_A, text)
        self.assertIn(NOTE, text)

    async def test_the_same_reply_builds_the_same_text(self):
        """Open WebUI may call twice for one reply (ELI-82): the second call must match the first."""
        self.session("s1")
        first = await self.turn("r1", CHUNK_A)
        self.assertEqual(await self.turn("r1", CHUNK_A), first)

    async def test_requests_without_a_message_id_are_different_replies(self):
        self.session("s1")
        await self.turn(None, CHUNK_A)
        self.assertNotIn(CHUNK_A, await self.turn(None, CHUNK_A))

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

    async def test_a_session_the_send_created_is_recorded_under_its_id(self):
        """No row before the first send: the commit reads the id that send created."""
        await self.turn("r1", CHUNK_A, after_send=lambda: self.session("s1"))
        self.assertNotIn(CHUNK_A, await self.turn("r2", CHUNK_A))

    async def test_a_reset_after_the_send_that_created_the_session_resends(self):
        await self.turn("r1", CHUNK_A, after_send=lambda: self.session("s1"))
        self.session("s2")
        self.assertIn(CHUNK_A, await self.turn("r2", CHUNK_A))

    async def test_a_send_whose_session_is_not_found_records_nothing(self):
        await self.turn("r1", CHUNK_A)
        self.session("s1")
        self.assertIn(CHUNK_A, await self.turn("r2", CHUNK_A))

    async def test_a_chunk_missing_from_the_context_is_not_recorded(self):
        """A custom RAG template without the context: the chunk never reached the agent."""
        self.session("s1")
        await self.turn("r1", CHUNK_A, sources=[source(CHUNK_A, CHUNK_NEW)])
        text = await self.turn("r2", CHUNK_A, CHUNK_NEW)
        self.assertIn(CHUNK_NEW, text)
        self.assertNotIn(CHUNK_A, text)

    async def test_a_message_never_sent_records_nothing(self):
        self.session("s1")
        await self.turn("r1", CHUNK_A, sent=False)
        self.assertIn(CHUNK_A, await self.turn("r2", CHUNK_A))

    async def test_a_message_that_was_never_sent_is_let_go_by_the_next_send(self):
        self.session("s1")
        await self.turn("r1", CHUNK_A, sent=False)
        await self.turn("r2", CHUNK_A)
        self.assertEqual(ocp._file_context_pending, {})

    async def test_a_chat_whose_name_has_a_tag_goes_once_even_quoted_twice(self):
        """ELI-465: a chat named "...</br>" broke the tag match, so the whole context
        (that chat twice, 585k characters) went again on every turn."""
        self.session("s1")
        chat = chat_text(20)
        sources = [chat_source(chat), chat_source(chat)]
        first = await self.turn("r1", chat, chat, sources=sources, name=CHAT_NAME)
        self.assertEqual(first.count(chat), 1, "the same chat quoted twice goes once")
        self.assertIn(f'<source id="2" name="{CHAT_NAME}"></source>', first)
        second = await self.turn("r2", chat, chat, sources=sources, name=CHAT_NAME)
        self.assertNotIn(chat, second)
        self.assertIn(f'<source id="1" name="{CHAT_NAME}"></source>', second)
        self.assertIn("not repeated here: " + CHAT_NAME, second)

    async def test_a_chunk_not_found_leaves_the_others_stripped(self):
        """One chunk the tag match misses no longer sends every chunk whole."""
        self.session("s1")
        await self.turn("r1", CHUNK_A, CHUNK_B)
        context = ocp._file_context_split(wrapped(CHUNK_A, "see: " + CHUNK_B), TYPED)[0]
        text, _ = await ocp._file_context_message(self.conn, self.state, self.key, "r2", context,
                                                  [source(CHUNK_A, CHUNK_B)])
        self.assertNotIn(CHUNK_A, text)
        self.assertIn("see: " + CHUNK_B, text)

    async def test_an_oversize_chunk_is_cut_to_its_head_and_a_line(self):
        """A whole quoted chat (1387 messages) no longer goes as one huge message."""
        self.session("s1")
        chat = chat_text(1387, size=200)
        self.assertGreater(len(chat), ocp.FILE_CONTEXT_CHUNK_MAX)
        text = await self.turn("r1", chat, sources=[chat_source(chat)], name=CHAT_NAME)
        self.assertLess(len(text), ocp.FILE_CONTEXT_CHUNK_MAX + 2000)
        self.assertIn(chat[:1000], text)
        self.assertIn(f"chat {CHAT_ID}: 1387 messages", text)
        self.assertIn("sessions_history", text)
        self.assertNotIn(chat[:1000], await self.turn("r2", chat, sources=[chat_source(chat)], name=CHAT_NAME))

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
