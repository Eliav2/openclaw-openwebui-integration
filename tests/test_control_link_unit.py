"""ELI-318: a tool result that carries a page where the user takes over from
the agent (CONTROL_LINK_PATTERN, e.g. a browser takeover's link) adds a
"Take control" link to the reply, as a note of its own, right after the call's
card: in the reply's text, which Open WebUI's own page renders, so it opens
as a page of its own and not inside a sandboxed embed.

Run: uv run --with-requirements requirements-dev.txt pytest -q tests/test_control_link_unit.py
"""
import types
import unittest

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

SK = "agent:main:openwebui-" + "u" * 36 + "-" + "c" * 36
URL = "https://replay.example.com/control/AbCdEfGhIjKlMnOpQrStUv"
PATTERN = r"https://replay\.example\.com/control/[A-Za-z0-9_-]{22}"
# What `process poll` returns while `takeover open` waits: the link is its first line.
POLL = f"{URL}\nwaiting for Eliav (up to 15 min); press End on the page when done\n\nProcess still running."


def _conn(pattern=PATTERN):
    return ocp._GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main", CONTROL_LINK_PATTERN=pattern))


def _result(text, tcid="t1", session=SK, name="process"):
    return {"type": "event", "event": "agent", "payload": {
        "runId": "run-1", "sessionKey": session, "stream": "tool",
        "data": {"phase": "result", "name": name, "toolCallId": tcid,
                 "result": {"content": [{"type": "text", "text": text}]}}}}


class ControlLinkTests(unittest.IsolatedAsyncioTestCase):

    async def _dispatch(self, conn, *msgs):
        q = conn.register_consumer(SK, "run-1")
        for msg in msgs:
            await conn.dispatch_run_event(msg)
        got = []
        while not q.empty():
            got.append(q.get_nowait()["payload"])
        return got

    async def test_a_result_with_the_link_adds_it_after_the_card(self):
        got = await self._dispatch(_conn(), _result(POLL))
        self.assertEqual([p["stream"] for p in got], ["tool", ocp.PIPE_NOTE_STREAM])
        note = got[1]
        self.assertEqual((note["sessionKey"], note["runId"]), (SK, "run-1"))
        self.assertTrue(note["data"]["id"].startswith("msg_note_control_"))
        self.assertIn(f"[Take control ↗]({URL})", note["data"]["text"])

    async def test_the_same_link_again_adds_nothing(self):
        """Each poll of the waiting command returns the link again."""
        got = await self._dispatch(_conn(), _result(POLL, "t1"), _result(POLL, "t2"))
        self.assertEqual([p["stream"] for p in got], ["tool", ocp.PIPE_NOTE_STREAM, "tool"])

    async def test_off_by_default_and_only_in_this_pipes_chats(self):
        for conn, session in ((_conn(""), SK), (_conn(), "agent:main:telegram:direct:1")):
            with self.subTest(session=session):
                q = conn.register_consumer(session, "run-1")
                await conn.dispatch_run_event(_result(POLL, session=session))
                streams = []
                while not q.empty():
                    streams.append(q.get_nowait()["payload"]["stream"])
                self.assertEqual(streams, ["tool"])

    async def test_only_a_web_address_is_linked(self):
        got = await self._dispatch(_conn(r"control/\S+"), _result("javascript:alert(1)//control/x"))
        self.assertEqual([p["stream"] for p in got], ["tool"])

    def test_the_reply_shows_the_link_as_its_own_message_after_the_card(self):
        conn, renderer = _conn(), ocp._TurnRenderer()
        start = _result("", "t1")["payload"]
        start["data"] = {"phase": "start", "name": "exec", "toolCallId": "t1",
                         "args": {"command": "takeover open t39 --reason 'Solve the CAPTCHA'"}}
        note = conn._control_link_note(_result(POLL)["payload"])
        for payload in (start, _result(POLL)["payload"], note):
            renderer.feed(payload)
        self.assertEqual([i["type"] for i in renderer.output], ["function_call", "function_call_output", "message"])
        self.assertIn(URL, renderer.output[-1]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
