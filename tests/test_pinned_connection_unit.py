"""A deploy retires the previous load's connection but lets it finish its runs. When that load is a
build from before the display names, the reaper also pins it shut so it never connects again (a
nameless reconnect wipes the stored name). Pinned means "never reconnect", not "stop reading": the
retired object still serves its in-flight runs from its open socket, which only its own event loop
reads. Stopping that loop left the socket open and unread (the Gateway's pings went unanswered, it
terminated the socket, and the Pipe never closed its end: one fd and ~0.8 MB of buffer in CLOSE-WAIT
per pin, ELI-369)."""
import asyncio
import time
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_helpers import owui_modules
from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

KEY = "agent:main:openwebui-u-c"


class _ReadingWS:
    """A Gateway socket the event loop reads: `recv()` waits for a pushed frame."""

    def __init__(self):
        self.frames: asyncio.Queue = asyncio.Queue()
        self.reads = 0
        self.close_calls = 0

    async def recv(self):
        self.reads += 1
        frame = await self.frames.get()
        if isinstance(frame, BaseException):
            raise frame
        return frame

    async def close(self):
        self.close_calls += 1


class PinnedConnectionTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        for patch in (mock.patch.object(ocp, "pipe_log", lambda *a, **k: None),
                      mock.patch.object(ocp, "_gateway_connection", None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.conn = ocp._GatewayConnection(lambda: None)
        self.conn.sends_display_name = False        # a build from before the names
        self.ws = _ReadingWS()
        self.conn._ws = self.ws
        self.loop_task = self.conn._event_loop_task = asyncio.ensure_future(self.conn._event_loop())
        self.addCleanup(self.loop_task.cancel)
        self.queue = self.conn.register_consumer(KEY, "run-1")
        self.connects = []

        async def connect(*a, **k):
            self.connects.append(a)
            raise AssertionError("a pinned connection must never open a socket")
        patch = mock.patch.object(ocp.websockets, "connect", connect, create=True)
        patch.start()
        self.addCleanup(patch.stop)
        await asyncio.sleep(0)

    async def _reap(self):
        with mock.patch.dict("sys.modules", owui_modules(_openclaw_gateway_connection_v1=self.conn)):
            await ocp._reap_stale_gateway_connection()
        self.assertTrue(self.conn._retired and self.conn._serving(), "it still serves its run")

    async def _until(self, what, done):
        """Let the loop run until `done()`; the bound only turns a hang into a failure."""
        end = time.monotonic() + 5
        while not done():
            self.assertLess(time.monotonic(), end, f"never happened: {what}")
            await asyncio.sleep(0)

    async def test_a_pinned_connection_keeps_reading_its_socket_for_the_runs_it_serves(self):
        await self._reap()
        reads = self.ws.reads
        fut = asyncio.get_running_loop().create_future()
        self.conn._pending_reqs["r1"] = fut
        self.ws.frames.put_nowait('{"type": "res", "id": "r1", "ok": true, "payload": {"n": 1}}')
        await self._until("the frame is read", fut.done)
        await self._until("recv() is called again", lambda: self.ws.reads > reads)
        self.assertEqual(fut.result(), {"n": 1}, "a frame pushed after the pin is still read")
        self.assertGreater(self.ws.reads, reads, "recv() is called again")
        self.assertFalse(self.loop_task.done(), "the loop is still running")
        self.assertEqual(self.ws.close_calls, 0, "the socket stays open while it serves a run")

    async def test_when_its_runs_finish_the_pinned_socket_is_closed_cleanly(self):
        await self._reap()
        reads = self.ws.reads
        self.ws.frames.put_nowait('{"type": "event", "event": "tick"}')
        await self._until("the tick is read and recv() is called again", lambda: self.ws.reads > reads)
        self.assertFalse(self.loop_task.done(), "still reading after a frame, so still answering pings")
        self.assertEqual(self.ws.close_calls, 0)
        self.conn.unregister_consumer(KEY, "run-1", queue=self.queue)
        await self._until("ws.close() runs", lambda: self.ws.close_calls)
        await self._until("the loop ends", self.loop_task.done)
        self.assertEqual(self.ws.close_calls, 1, "ws.close() sends the close frame")
        self.assertIsNone(self.conn._ws)
        self.assertEqual(self.connects, [])

    async def test_a_pinned_connection_whose_socket_drops_ends_without_reconnecting(self):
        await self._reap()
        self.ws.frames.put_nowait(ocp.websockets.exceptions.ConnectionClosed(None, None))
        await self._until("the loop ends instead of dialling again", self.loop_task.done)
        self.assertEqual(self.connects, [], "no socket is opened, so no nameless connect")
        with self.assertRaises(ocp.GatewayError):
            await self.conn.ensure_connected()
        self.assertEqual(self.connects, [])

    async def test_a_pinned_connection_with_an_open_socket_is_still_usable_for_its_requests(self):
        await self._reap()
        await self.conn.ensure_connected()       # connected: nothing to dial, so no refusal
        self.assertEqual(self.connects, [])

    async def test_a_pinned_connection_never_reconnects_when_asked_to(self):
        await self._reap()
        self.conn._ws = None                      # dropped while a turn asked for a connection
        with self.assertRaises(ocp.GatewayError):
            await self.conn.ensure_connected()
        with self.assertRaises(ocp.GatewayError):
            await self.conn._connect_and_start()
        self.assertEqual(self.connects, [])


class PreRetireBuildTests(unittest.IsolatedAsyncioTestCase):
    """A build from before `retire()` cannot serve a run past its reaping: it is pinned and closed."""

    async def test_a_build_without_retire_is_pinned_and_disconnected(self):
        class Old:
            sends_display_name = False

            def __init__(self):
                self.disconnects = 0

            async def disconnect(self):
                self.disconnects += 1
        old = Old()
        with mock.patch.object(ocp, "pipe_log", lambda *a, **k: None):
            await ocp._retire_stale_connection(old)
        self.assertEqual(old.disconnects, 1)
        with self.assertRaises(ocp.GatewayError):
            await old.ensure_connected()


if __name__ == "__main__":
    unittest.main()
