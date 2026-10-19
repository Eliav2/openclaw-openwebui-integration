"""ELI-176: a turn that needs the Gateway connection while the event loop
waits out a reconnect backoff takes the loop's attempt, made at once, instead
of connecting a socket of its own that nothing reads.

Run: uv run --with-requirements requirements-dev.txt pytest -q tests/test_reconnect_unit.py
"""
import asyncio
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402


class _Reconnecting:

    def setUp(self):
        patch = mock.patch.object(ocp, "pipe_log", lambda *a, **k: None)
        patch.start()
        self.addCleanup(patch.stop)
        self.conn = ocp._GatewayConnection(lambda: types.SimpleNamespace())
        self.connects = []
        self.outcomes = []

        async def connect():
            self.connects.append(asyncio.current_task())
            outcome = self.outcomes.pop(0) if self.outcomes else None
            if outcome is not None:
                raise outcome
            self.conn._ws = object()
        self.conn._connect_and_start = connect
        # The connection dropped a while ago: the next attempt is 30 s away.
        self.conn._reconnect_attempt = 5

    async def _backing_off(self):
        self.conn._event_loop_task = asyncio.ensure_future(self.conn._reconnect())
        self.addCleanup(self.conn._event_loop_task.cancel)
        await asyncio.sleep(0)


class ReconnectHandoffTests(_Reconnecting, unittest.IsolatedAsyncioTestCase):

    async def test_a_turn_during_a_backoff_takes_the_loops_attempt_at_once(self):
        await self._backing_off()
        await asyncio.wait_for(self.conn.ensure_connected(), 2)   # bounds the test only
        self.assertEqual(self.connects, [self.conn._event_loop_task], "one connect, by the loop")
        self.assertIsNotNone(self.conn._ws)

    async def test_a_failed_attempt_fails_the_turn_and_the_loop_backs_off_again(self):
        self.outcomes = [ocp.GatewayError("gateway starting; retry shortly")]
        await self._backing_off()
        with self.assertRaisesRegex(ocp.GatewayError, "gateway starting"):
            await asyncio.wait_for(self.conn.ensure_connected(), 2)
        await asyncio.sleep(0)
        self.assertEqual(len(self.connects), 1, "no extra attempt without a waiting turn")
        self.assertFalse(self.conn._event_loop_task.done(), "still reconnecting")

    async def test_a_loop_stopped_mid_attempt_ends_the_wait(self):
        entered = asyncio.Event()

        async def hanging_connect():
            entered.set()
            await asyncio.get_running_loop().create_future()
        self.conn._connect_and_start = hanging_connect
        await self._backing_off()
        waiting = asyncio.ensure_future(self.conn.ensure_connected())
        await asyncio.wait_for(entered.wait(), 2)
        self.conn._event_loop_task.cancel()
        with self.assertRaisesRegex(ocp.GatewayError, "stopped"):
            await asyncio.wait_for(waiting, 2)


def refused():
    return ocp.GatewayError("Could not connect to the OpenClaw Gateway at localhost:18789 "
                            "(ConnectionRefusedError: [Errno 111] Connection refused).",
                            error={"code": ocp.GATEWAY_REFUSED_CODE})


class GatewayDownTests(_Reconnecting, unittest.IsolatedAsyncioTestCase):
    """A Gateway that is down or restarting refuses the connection until its
    new process listens. A turn that finds the socket reconnecting waits
    through those refusals instead of answering with one."""

    def setUp(self):
        super().setUp()
        self.logged, self.delays = [], []
        patch = mock.patch.object(ocp, "pipe_log", lambda *a, **k: self.logged.append(a[0]))
        patch.start()
        self.addCleanup(patch.stop)
        wait_for = asyncio.wait_for

        async def quick_wait_for(aw, timeout):
            # The loop's backoff wait: record it, end it at once.
            if getattr(aw, "__qualname__", "").startswith("Event.wait"):
                self.delays.append(timeout)
                timeout = 0
            return await wait_for(aw, timeout)
        patch = mock.patch.object(ocp.asyncio, "wait_for", quick_wait_for)
        patch.start()
        self.addCleanup(patch.stop)

    async def test_a_turn_waits_through_refused_reconnects_for_the_new_gateway(self):
        self.outcomes = [refused(), refused(), None]
        await self._backing_off()
        await self.conn.ensure_connected()
        self.assertEqual(len(self.connects), 3)
        self.assertIsNotNone(self.conn._ws)
        self.assertEqual(len([line for line in self.logged if "refuses connections" in line]), 1)

    async def test_the_loop_tries_again_at_its_first_step_while_a_turn_waits(self):
        self.outcomes = [refused(), refused(), None]
        await self._backing_off()
        await self.conn.ensure_connected()
        # 30 s before the turn asked; after each refusal, 1 s while it waits.
        self.assertEqual(self.delays, [30, 1, 1])

    async def test_a_gateway_that_does_not_come_back_fails_the_turn(self):
        self.outcomes = [refused()] * 1000
        await self._backing_off()
        with mock.patch.object(ocp, "GATEWAY_DOWN_WAIT_S", 0):
            with self.assertRaisesRegex(ocp.GatewayError, "Connection refused"):
                await self.conn.ensure_connected()
        self.assertEqual(self.conn._turns_awaiting_gateway, 0)

    async def test_a_refused_connect_is_marked_and_nothing_else_is(self):
        multi = OSError("Multiple exceptions: [Errno 111] Connect call failed ('::1', 18789, 0, 0), "
                        "[Errno 111] Connect call failed ('127.0.0.1', 18789)")
        mixed = OSError("Multiple exceptions: [Errno 99] Cannot assign requested address, "
                        "[Errno 111] Connect call failed ('127.0.0.1', 18789)")
        for ex, marked in ((ConnectionRefusedError(111, "Connection refused"), True), (multi, True),
                           (mixed, False), (OSError(113, "No route to host"), False)):
            with self.subTest(ex=str(ex)):
                valves = types.SimpleNamespace(GATEWAY_URL="localhost:18789", GATEWAY_TOKEN="tok",
                                               AGENT_ID="main")
                conn = ocp._GatewayConnection(lambda: valves)
                conn._ensure_identity = lambda _valves: None

                async def connect(*a, ex=ex, **k):
                    raise ex
                with mock.patch.object(ocp.websockets, "connect", connect, create=True), \
                        mock.patch.object(ocp.websockets.exceptions, "WebSocketException",
                                          type("WebSocketException", (Exception,), {}), create=True):
                    with self.assertRaises(ocp.GatewayError) as got:
                        await conn._connect_and_start()
                self.assertEqual(ocp._gateway_refused(got.exception), marked)
                self.assertIn("Is the Gateway running", str(got.exception))


if __name__ == "__main__":
    unittest.main()
