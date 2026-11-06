"""Every Pipe log line of a turn names its chat and run (ELI-368).

Central logs (Loki) index chat_id/runId from the line text; before this only 2% of
[openclaw-pipe] lines carried either, so `logtrace <chat id>` missed most of a turn.
"""
import asyncio
import contextlib
import io
import unittest

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

CHAT = "0a4c8821-8252-4141-a3e5-e6beafa16082"
RUN = "msg-26d09b7a-6a03-4fa8-b069-5ceb4bca23c6"


def logged(fn):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn()
    return out.getvalue().strip()


class LogIds(unittest.TestCase):
    def test_a_line_outside_any_turn_has_no_ids(self):
        self.assertEqual(logged(lambda: ocp.pipe_log("WS connected")), "[openclaw-pipe] WS connected")

    def test_a_line_in_a_turn_ends_with_its_chat_and_run(self):
        async def turn():
            ocp.log_context(chat_id=CHAT)
            first = logged(lambda: ocp.pipe_log("Messages: 3"))
            ocp.log_context(run_id=RUN)
            return first, logged(lambda: ocp.pipe_log("  Tool start: Read"))

        first, second = asyncio.run(turn())
        self.assertEqual(first, f"[openclaw-pipe] Messages: 3 chat_id={CHAT}")
        self.assertEqual(second, f"[openclaw-pipe]   Tool start: Read chat_id={CHAT} runId={RUN}")

    def test_ids_already_in_the_line_are_not_repeated(self):
        async def turn():
            ocp.log_context(chat_id=CHAT, run_id=RUN)
            return logged(lambda: ocp.pipe_log(f"Captured runId: {RUN}"))

        self.assertEqual(asyncio.run(turn()), f"[openclaw-pipe] Captured runId: {RUN} chat_id={CHAT}")

    def test_two_turns_at_once_keep_their_own_ids_and_background_tasks_inherit_them(self):
        async def turn(chat):
            ocp.log_context(chat_id=chat)
            await asyncio.sleep(0)
            here = logged(lambda: ocp.pipe_log("here"))

            async def background():
                return logged(lambda: ocp.pipe_log("bg"))

            return here, await ocp.spawn_background(background())

        async def both():
            return await asyncio.gather(turn("chat-a"), turn("chat-b"))

        (a, a_bg), (b, b_bg) = asyncio.run(both())
        self.assertTrue(a.endswith("chat_id=chat-a") and a_bg.endswith("chat_id=chat-a"))
        self.assertTrue(b.endswith("chat_id=chat-b") and b_bg.endswith("chat_id=chat-b"))


if __name__ == "__main__":
    unittest.main()
