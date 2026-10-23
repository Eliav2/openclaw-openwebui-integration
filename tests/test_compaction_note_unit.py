"""ELI-283: the Gateway's context compaction shows in the reply as a note of
its own: "compacting" while it runs, then a collapsed "Context compacted"
block in its place.

Run: uv run --with-requirements requirements-dev.txt pytest -q tests/test_compaction_note_unit.py
"""
import unittest

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

KEY = "agent:main:openwebui-user-1-chat-1"


def compaction(phase, run="run-1", **data):
    """A compaction event as the Gateway sends it for a claude-cli run."""
    return {"sessionKey": KEY, "runId": run, "stream": ocp.COMPACTION_STREAM,
            "data": {"kind": "compaction", "phase": phase, "backend": "claude-cli", **data}}


def texts(r):
    return [i["content"][0]["text"] for i in r.output]


class CompactionNoteTests(unittest.TestCase):

    def test_a_start_shows_compacting_and_its_end_replaces_it(self):
        r = ocp._TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "before"}})
        [started] = [e for e in r.feed(compaction("start", tokensBefore=182_000)) if e]
        self.assertEqual(started["type"], "response.output_item.added")
        self.assertEqual(texts(r), ["before", ocp.COMPACTING_NOTE])
        [ended] = [e for e in r.feed(compaction("end", completed=True)) if e]
        self.assertEqual(ended["item"]["id"], started["item"]["id"], "the same note, replaced")
        note = texts(r)[-1]
        self.assertTrue(note.startswith("<details>\n<summary>🧹 Context compacted · was ~182k tokens</summary>"))
        self.assertIn(ocp.COMPACTED_BODY, note)
        self.assertTrue(note.endswith("</details>"))
        self.assertEqual(r.text, "before", "a note is not run text")

    def test_text_after_the_note_is_a_new_message(self):
        r = ocp._TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "before"}})
        r.feed(compaction("start"))
        r.feed(compaction("end", completed=True))
        r.feed({"stream": "assistant", "data": {"delta": "after"}})
        self.assertEqual([texts(r)[0], texts(r)[2]], ["before", "after"])
        self.assertTrue(r.output[1]["id"].startswith(ocp._NOTE_ITEM_PREFIX))

    def test_no_size_known_shows_none(self):
        r = ocp._TurnRenderer()
        r.feed(compaction("start"))
        r.feed(compaction("end", completed=True))
        self.assertIn("<summary>🧹 Context compacted</summary>", texts(r)[0])

    def test_a_compaction_that_did_not_complete_says_so(self):
        r = ocp._TurnRenderer()
        r.feed(compaction("start"))
        r.feed(compaction("end", completed=False))
        self.assertEqual(texts(r), [ocp.COMPACTION_FAILED_NOTE])

    def test_a_skipped_or_stopped_compaction_says_which(self):
        for outcome, note in (("skipped", ocp.COMPACTION_SKIPPED_NOTE), ("aborted", ocp.COMPACTION_STOPPED_NOTE),
                              ("failed", ocp.COMPACTION_FAILED_NOTE)):
            r = ocp._TurnRenderer()
            r.feed(compaction("start"))
            r.feed(compaction("end", completed=False, outcome=outcome))
            self.assertEqual(texts(r), [note], outcome)

    def test_each_compaction_of_a_run_gets_its_own_note(self):
        r = ocp._TurnRenderer()
        for _ in range(2):
            r.feed(compaction("start"))
            r.feed(compaction("end", completed=True))
        self.assertEqual(len({i["id"] for i in r.output}), 2)

    def test_every_renderer_fed_the_run_names_the_note_alike(self):
        """The live reply and a turn writer each render the run: a rebuild
        must find the note by the same id, not add it twice."""
        a, b = ocp._TurnRenderer(), ocp._TurnRenderer()
        for r in (a, b):
            r.feed(compaction("start"))
        self.assertEqual(a.output[0]["id"], b.output[0]["id"])
        self.assertNotEqual(a.output[0]["id"], self._note_id_of_run("run-2"))

    def _note_id_of_run(self, run):
        r = ocp._TurnRenderer()
        r.feed(compaction("start", run=run))
        return r.output[0]["id"]

    def test_an_end_without_its_start_still_shows_how_it_ended(self):
        r = ocp._TurnRenderer()
        r.feed(compaction("end", completed=True))
        self.assertEqual(len(r.output), 1)
        self.assertIn("Context compacted", texts(r)[0])

    def test_held_text_shows_before_the_note(self):
        """A message that opens like a silent sentinel is held (`_add_text`);
        the note flushes it first, as a tool card does."""
        r = ocp._TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "NO"}})
        added = r.feed(compaction("start"))
        self.assertEqual(added[0], "NO")
        self.assertEqual(texts(r), ["NO", ocp.COMPACTING_NOTE])

    def test_a_rebuild_keeps_the_note(self):
        streamed = ocp._TurnRenderer()
        streamed.feed(compaction("start"))
        streamed.feed(compaction("end", completed=True))
        rebuilt = ocp._TurnRenderer()
        rebuilt.output = [{"type": "message", "id": "msg_t", "content": []}]
        ocp._keep_pipe_notes(streamed.output, rebuilt)
        self.assertEqual(rebuilt.output[0]["id"], streamed.output[0]["id"])


class FakeConn:
    def __init__(self, row=None, fail=False):
        self.row, self.fail, self.sent = row, fail, []

    async def send_request(self, method, params, timeout=10):
        self.sent.append((method, params))
        if self.fail:
            raise TimeoutError()
        return {"session": self.row}


class TokensBeforeTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_start_gets_the_rows_context_fill(self):
        conn = FakeConn({"totalTokens": 150_000})
        out = await ocp.with_compaction_tokens(conn, KEY, compaction("start"))
        self.assertEqual(out["data"]["tokensBefore"], 150_000)
        self.assertEqual(conn.sent, [("sessions.describe", {"key": KEY})])

    async def test_only_a_compaction_start_asks(self):
        conn = FakeConn({"totalTokens": 1})
        for payload in (compaction("end", completed=True), {"stream": "assistant", "data": {}},
                        compaction("start", tokensBefore=5)):
            self.assertIs(await ocp.with_compaction_tokens(conn, KEY, payload), payload)
        self.assertEqual(conn.sent, [])

    async def test_a_failed_lookup_leaves_the_event_as_it_was(self):
        payload = compaction("start")
        self.assertIs(await ocp.with_compaction_tokens(FakeConn(fail=True), KEY, payload), payload)
        self.assertIs(await ocp.with_compaction_tokens(FakeConn({}), KEY, payload), payload)


if __name__ == "__main__":
    unittest.main()
