"""Every Pipe log line of a reply names its chat and run, and only its own (ELI-368).

Central logs (Loki) index chat_id/runId from the line text; before this only 2% of
[openclaw-pipe] lines carried either, so `logtrace <chat id>` missed most of a turn.
A leaked id is worse than none: it files another chat's lines under this one.
"""
import asyncio
import contextlib
import io
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

CHAT = "0a4c8821-8252-4141-a3e5-e6beafa16082"
# The Pipe's own run ids embed the chat id.
RUN = f"msg-{CHAT}-26d09b7a-6a03-4fa8-b069-5ceb4bca23c6-1793945626.5"


def logged(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ocp.pipe_log(*args)
    return out.getvalue().strip()


def ids(line):
    """{'chat': ..., 'run': ...} from a line's tags."""
    tags = dict(t.split("=", 1) for t in line.split() if t.startswith(("chat_id=", "runId=")))
    return {"chat": tags.get("chat_id"), "run": tags.get("runId")}


class Format(unittest.TestCase):
    def test_a_line_outside_any_reply_has_no_ids(self):
        self.assertEqual(logged("WS connected"), "[openclaw-pipe] WS connected")

    def test_a_line_in_a_reply_ends_with_its_chat_then_its_run(self):
        with ocp.log_scope(chat_id=CHAT):
            first = logged("Messages: 3")
            ocp.log_run(RUN)
            second = logged("  Tool start: Read")
        self.assertEqual(first, f"[openclaw-pipe] Messages: 3 chat_id={CHAT}")
        self.assertEqual(second, f"[openclaw-pipe]   Tool start: Read chat_id={CHAT} runId={RUN}")

    def test_the_chat_tag_stays_when_the_run_id_contains_the_chat_id(self):
        with ocp.log_scope(chat_id=CHAT, run_id=RUN):
            line = logged(f"Captured runId: {RUN}")
        self.assertEqual(line, f"[openclaw-pipe] Captured runId: {RUN} chat_id={CHAT}")

    def test_a_scope_puts_the_callers_ids_back(self):
        with ocp.log_scope(chat_id="outer", run_id="run-outer"):
            with ocp.log_scope(chat_id="inner"):
                self.assertEqual(ids(logged("x")), {"chat": "inner", "run": None})
                ocp.log_run("run-inner")
            self.assertEqual(ids(logged("x")), {"chat": "outer", "run": "run-outer"})
        self.assertEqual(ids(logged("x")), {"chat": None, "run": None})


class Replies(unittest.IsolatedAsyncioTestCase):
    """Through Pipe.pipe(): its own load, a fake reply that logs, sets its run and spawns background work."""

    async def asyncSetUp(self):
        self.lines = []

        def pipe_log(*args):
            self.lines.append(logged(*args))

        async def impl(pipe, body, emit, **kw):
            chat = (kw.get("__metadata__") or {}).get("chat_id")
            pipe_log(f"start {chat}")
            await asyncio.sleep(0)
            ocp.log_run(f"run-of-{chat}")
            await asyncio.sleep(0)

            async def background():
                await asyncio.sleep(0)
                pipe_log(f"bg {chat}")
            await ocp.spawn_background(background())
            pipe_log(f"end {chat}")
            yield f"reply {chat}"

        for target, value in (("_current_load", mock.AsyncMock(return_value=None)),
                              ("remember_owui_server", lambda: None),
                              ("_ensure_slash_prompts", lambda valves: None)):
            p = mock.patch.object(ocp, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(ocp.Pipe, "_pipe_impl", impl)
        p.start()
        self.addCleanup(p.stop)
        self.pipe_log = pipe_log

    async def reply(self, chat):
        meta = {"chat_id": chat} if chat else {}

        async def emit(_):
            pass
        return [x async for x in ocp.Pipe().pipe({}, emit, __metadata__=meta, __request__=object())]

    def own(self, chat):
        """Every line logged for `chat` names only that chat and its run (or no run yet)."""
        mine = [l for l in self.lines if l.split("] ", 1)[1].split()[1:2] == [chat]]
        self.assertEqual(len(mine), 3, self.lines)
        for line in mine:
            self.assertEqual(ids(line)["chat"], chat, line)
            self.assertIn(ids(line)["run"], (None, f"run-of-{chat}"), line)

    async def test_two_concurrent_replies_each_log_only_their_own_ids(self):
        await asyncio.gather(self.reply("chat-a"), self.reply("chat-b"))
        self.own("chat-a")
        self.own("chat-b")

    async def test_replies_one_after_another_in_one_task_never_inherit_ids(self):
        await self.reply("chat-a")
        self.pipe_log("between")
        await self.reply(None)
        self.own("chat-a")
        between = next(l for l in self.lines if "between" in l)
        self.assertEqual(ids(between), {"chat": None, "run": None})
        for line in self.lines:
            if " None" in line:
                self.assertEqual(ids(line)["chat"], None, line)
                self.assertIn(ids(line)["run"], (None, "run-of-None"), line)


class RunScope(unittest.IsolatedAsyncioTestCase):
    """_PipeTurn.run: a run's id is its own; a retry inside the same reply starts with none."""

    def turn(self, run_id, logs):
        t = ocp._PipeTurn.__new__(ocp._PipeTurn)
        t.ended = False

        async def admit():
            logs.append(ids(logged("before send")))
            t.our_run_id = run_id
            logs.append(ids(logged("after send")))
            yield "x"

        async def nothing():
            return
            yield

        t.prepare, t.admit, t.consume, t.finish = nothing, admit, nothing, nothing
        return t

    async def test_a_retry_logs_no_run_until_its_own_and_the_first_run_comes_back(self):
        logs = []
        with ocp.log_scope(chat_id=CHAT):
            first = self.turn("run-1", logs)
            async for _ in first.run():
                retry = self.turn("run-2", logs)
                async for _ in retry.run():
                    pass
                logs.append(ids(logged("after the retry")))
        self.assertEqual(logs, [
            {"chat": CHAT, "run": None}, {"chat": CHAT, "run": "run-1"},
            {"chat": CHAT, "run": None}, {"chat": CHAT, "run": "run-2"},
            {"chat": CHAT, "run": "run-1"},
        ])


if __name__ == "__main__":
    unittest.main()
