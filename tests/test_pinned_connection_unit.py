"""A deploy retires the previous load's connection but lets it finish its runs. When that load is a
build from before the display names, the reaper also pins it shut so it never connects again (a
nameless reconnect wipes the stored name). Pinned means "never reconnect", not "stop reading": the
retired object still serves its in-flight runs from its open socket, which only its own event loop
reads. Stopping that loop left the socket open and unread (the Gateway's pings went unanswered, it
terminated the socket, and the Pipe never closed its end: one fd and ~0.8 MB of buffer in CLOSE-WAIT
per pin, ELI-369)."""
import asyncio
import time
import types
import unittest
import uuid
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_helpers import owui_modules
from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

KEY = "agent:main:openwebui-u-c"
USER, CHAT, MSG = (str(uuid.UUID(int=n)) for n in (1, 2, 3))
OWUI_KEY = ocp._owui_session_key("main", USER, CHAT)
OWUI_RUN = f"msg-{CHAT}-{MSG}-1792100000.5"


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
                      mock.patch.object(ocp, "_gateway_connection", None),
                      mock.patch.object(ocp, "_retired_connections", [])):
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

    async def test_a_pinned_connection_admits_no_new_work_but_keeps_its_socket(self):
        """A retired background producer of the old generation (channel watcher, announce paths) asks
        for the connection to send a new run: refused, while the socket stays read for what it has."""
        await self._reap()
        with self.assertRaises(ocp.GatewayError):
            await self.conn.ensure_connected()
        self.assertFalse(self.conn._stopped)
        self.assertFalse(self.loop_task.done(), "refusing new work does not stop the reading")
        self.assertEqual(self.ws.close_calls, 0)
        self.assertEqual(self.connects, [])

    async def test_a_pinned_connection_never_reconnects_when_asked_to(self):
        await self._reap()
        self.conn._ws = None                      # dropped while a turn asked for a connection
        with self.assertRaises(ocp.GatewayError):
            await self.conn.ensure_connected()
        with self.assertRaises(ocp.GatewayError):
            await self.conn._connect_and_start()
        self.assertEqual(self.connects, [])


class PinnedSocketDropsMidReplyTests(unittest.IsolatedAsyncioTestCase):
    """The pinned socket dropped while a reply still reads it. The retired connection cannot hand the
    run to a turn writer (it has none) and cannot reconnect, so no event would ever reach that reply,
    and the successor leaves a run alone while a retired connection serves it: the reply would stay
    generating for good. The reaper ends the reply and unregisters it, which is the case ELI-150 covers:
    the new connection's turn writer then writes the rest of the run into the same message."""

    async def asyncSetUp(self):
        for patch in (mock.patch.object(ocp, "pipe_log", lambda *a, **k: None),
                      mock.patch.object(ocp, "_gateway_connection", None),
                      mock.patch.object(ocp, "_retired_connections", [])):
            patch.start()
            self.addCleanup(patch.stop)
        valves = lambda: types.SimpleNamespace(AGENT_ID="main")   # noqa: E731
        self.old, self.new = ocp._GatewayConnection(valves), ocp._GatewayConnection(valves)
        self.old.sends_display_name = False
        self.ws = _ReadingWS()
        self.old._ws = self.ws
        self.loop_task = self.old._event_loop_task = asyncio.ensure_future(self.old._event_loop())
        self.addCleanup(self.loop_task.cancel)
        self.queue = self.old.register_consumer(OWUI_KEY, OWUI_RUN)    # the reply the old load streams
        with mock.patch.dict("sys.modules", owui_modules(_openclaw_gateway_connection_v1=self.old)):
            await ocp._reap_stale_gateway_connection()
        self.event = {"sessionKey": OWUI_KEY, "runId": OWUI_RUN, "stream": "assistant", "data": {"delta": "more"}}
        self.spawned = []

        def spawn(coro):
            self.spawned.append(coro.__qualname__)
            coro.close()
        patch = mock.patch.object(ocp, "spawn_background", spawn)
        patch.start()
        self.addCleanup(patch.stop)

    async def _drop(self):
        self.ws.frames.put_nowait(ocp.websockets.exceptions.ConnectionClosed(None, None))
        end = time.monotonic() + 5
        while not self.loop_task.done():
            self.assertLess(time.monotonic(), end, "the loop never ended")
            await asyncio.sleep(0)

    async def test_the_successor_leaves_the_run_alone_while_the_pinned_socket_is_up(self):
        self.assertFalse(ocp._turn_route_now(self.new, self.event))
        self.assertEqual(self.new._turn_pumps, {})

    async def test_a_drop_ends_the_reply_it_was_serving(self):
        await self._drop()
        sent = self.queue.take_all()
        ends = [m for m in sent if m.get("type") == "event"
                and (m["payload"].get("stream"), (m["payload"].get("data") or {}).get("phase")) == ("lifecycle", "end")
                and m["payload"].get("runId") == OWUI_RUN]
        self.assertEqual(len(ends), 1, "the reply is told its run ended, so it finishes instead of waiting")
        self.assertEqual(self.old._consumers, {}, "and it is no longer registered on the retired connection")

    async def test_after_the_drop_the_successors_turn_writer_takes_the_run(self):
        await self._drop()
        self.assertFalse(ocp._served_by_retired(self.event))
        self.assertTrue(ocp._turn_route_now(self.new, self.event))
        self.assertIn((OWUI_KEY, OWUI_RUN), self.new._turn_pumps)
        self.assertIn("_turn_pump_loop", self.spawned)


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
