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


class ReconnectHandoffTests(unittest.IsolatedAsyncioTestCase):

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


if __name__ == "__main__":
    unittest.main()
