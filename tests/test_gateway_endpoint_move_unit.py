"""A change to the GATEWAY_URL valve (host, port, or ws to wss) takes effect on the
next message. `ensure_connected` keeps a live socket, so a valve change used to
be ignored until something else dropped the connection.

Run: uv run --with-requirements requirements-dev.txt pytest -q tests/test_gateway_endpoint_move_unit.py
"""
import asyncio
import json
import os
import tempfile
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402
import openclaw_status_action as action  # noqa: E402


class _Endpoints(unittest.IsolatedAsyncioTestCase):
    """Real `_GatewayConnection`s whose socket is a stand-in: connecting
    records the endpoint the valve names, as the real connect does."""

    def setUp(self):
        self.valves = types.SimpleNamespace(GATEWAY_URL="localhost:18789")
        self.dialled = []
        self.disconnected = []

        async def connect(conn):
            host, port, tls = getattr(conn, "_pinned_endpoint", None) or ocp._parse_gateway_url(conn._valves().GATEWAY_URL)
            self.dialled.append((host, port, tls))
            conn._ws = object()
            conn._endpoint = (host, port, tls)

        async def idle_loop(conn):
            await asyncio.Event().wait()

        async def disconnect(conn):
            self.disconnected.append(conn)
            conn._stopped = True
            conn._ws = None

        for owner in (ocp, action):
            for name, value in (("_gateway_connection", None), ("_restricted_connection", None),
                                ("_action_fallback_connection", None)):
                if hasattr(owner, name):
                    patch = mock.patch.object(owner, name, value)
                    patch.start()
                    self.addCleanup(patch.stop)
        for cls in (ocp._GatewayConnection, action._GatewayConnection):
            for name, value in (("_connect_and_start", connect), ("_event_loop", idle_loop),
                                ("disconnect", disconnect)):
                patch = mock.patch.object(cls, name, value)
                patch.start()
                self.addCleanup(patch.stop)
        for owner in (ocp, action):
            for name, value in (("pipe_log", lambda *a, **k: None),
                                ("_reap_stale_gateway_connection", mock.AsyncMock()),
                                ("_remember_gateway_connection", lambda conn: None)):
                patch = mock.patch.object(owner, name, value)
                patch.start()
                self.addCleanup(patch.stop)
        for owner in (ocp, action):
            patch = mock.patch.object(owner, "_retired_connections", [])
            patch.start()
            self.addCleanup(patch.stop)
        self.addAsyncCleanup(self._stop_loops)
        self.conns = []

    async def _stop_loops(self):
        for conn in self.conns:
            if conn._event_loop_task:
                conn._event_loop_task.cancel()

    async def get(self):
        conn = await ocp._get_gateway_connection(lambda: self.valves)
        if conn not in self.conns:
            self.conns.append(conn)
        return conn


class MainConnectionTests(_Endpoints):

    async def test_an_unchanged_valve_keeps_the_connection(self):
        first = await self.get()
        self.assertIs(await self.get(), first)
        self.assertEqual(self.dialled, [("localhost", 18789, False)])

    async def test_a_new_host_connects_there_and_retires_the_old_connection(self):
        first = await self.get()
        self.valves.GATEWAY_URL = "gateway.lan:18789"
        second = await self.get()
        self.assertIsNot(second, first)
        self.assertEqual(self.dialled, [("localhost", 18789, False), ("gateway.lan", 18789, False)])
        self.assertTrue(first._retired)
        self.assertFalse(second._retired)
        self.assertIs(ocp._gateway_connection, second)
        self.assertIs(await self.get(), second)

    async def test_ws_to_wss_on_the_same_host_and_port_is_a_new_endpoint(self):
        first = await self.get()
        self.valves.GATEWAY_URL = "wss://localhost:18789"
        second = await self.get()
        self.assertIsNot(second, first)
        self.assertEqual(self.dialled[-1], ("localhost", 18789, True))

    async def test_a_new_port_connects_there(self):
        first = await self.get()
        self.valves.GATEWAY_URL = "localhost:19000"
        self.assertIsNot(await self.get(), first)
        self.assertEqual(self.dialled[-1], ("localhost", 19000, False))

    async def test_a_spelling_of_the_same_endpoint_is_not_a_change(self):
        first = await self.get()
        self.valves.GATEWAY_URL = "ws://localhost:18789/"
        self.assertIs(await self.get(), first)
        self.assertEqual(len(self.dialled), 1)

    async def test_a_reply_in_flight_finishes_on_the_old_connection(self):
        first = await self.get()
        first._consumers["run-1"] = object()   # a reply still streaming here
        self.valves.GATEWAY_URL = "gateway.lan:18789"
        second = await self.get()
        self.assertIsNot(second, first)
        # not closed while the reply is served, and its events still count as served
        self.assertEqual(self.disconnected, [])
        self.assertIsNotNone(first._ws)
        self.assertTrue(first._serving())
        # the last reply leaves: the old connection closes itself, the new one stays
        closed = asyncio.Event()

        async def disconnect():
            closed.set()
        first.disconnect = disconnect
        del first._consumers["run-1"]
        first._close_if_retired_and_idle()
        await asyncio.wait_for(closed.wait(), 1)
        self.assertNotIn(second, self.disconnected)

    async def test_an_idle_old_connection_closes_at_once(self):
        first = await self.get()
        self.valves.GATEWAY_URL = "gateway.lan:18789"
        await self.get()
        self.assertEqual(self.disconnected, [first])

    async def test_a_valve_that_does_not_parse_fails_naming_it_and_keeps_the_connection(self):
        first = await self.get()
        self.valves.GATEWAY_URL = "ftp://gateway.lan"
        with self.assertRaises(ocp.GatewayError) as ctx:
            await self.get()
        self.assertIn("GATEWAY_URL", str(ctx.exception))
        self.assertFalse(first._retired)
        self.assertIs(ocp._gateway_connection, first)
        # fixed: the next message gets a working connection again
        self.valves.GATEWAY_URL = "localhost:18789"
        self.assertIs(await self.get(), first)

    async def test_two_messages_at_once_make_one_new_connection(self):
        await self.get()
        self.valves.GATEWAY_URL = "gateway.lan:18789"
        a, b = await asyncio.gather(self.get(), self.get())
        self.assertIs(a, b)
        self.assertEqual(self.dialled.count(("gateway.lan", 18789, False)), 1)


class RetiredConnectionTests(unittest.TestCase):

    def test_a_replaced_connection_that_drops_reconnects_to_its_own_endpoint(self):
        seen = []

        async def refuse(url, *a, **k):
            seen.append(url)
            raise ConnectionRefusedError(111, "Connection refused")

        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                with open(os.path.join(tmp, "identity.json"), "w") as fh:
                    json.dump({"id": "dev-fixed", "publicKey": "pub-fixed", "privateKey": "pem-fixed"}, fh)
                valves = types.SimpleNamespace(GATEWAY_URL="new.example:18789", GATEWAY_TOKEN="tok",
                                               STATE_DIR=tmp, DEVICE_IDENTITY="", AGENT_ID="main",
                                               OWNER_USERS="")
                ws_errors = types.SimpleNamespace(WebSocketException=type("WebSocketException", (Exception,), {}))
                with mock.patch.object(ocp.websockets, "connect", refuse, create=True), \
                        mock.patch.object(ocp.websockets, "exceptions", ws_errors, create=True), \
                        mock.patch.object(ocp, "pipe_log", lambda *a, **k: None):
                    conn = ocp._GatewayConnection(lambda: valves)
                    conn._pinned_endpoint = ("old.example", 18789, False)
                    with self.assertRaises(ocp.GatewayError):
                        await conn._connect_and_start()
        asyncio.run(run())
        self.assertEqual(seen, ["ws://old.example:18789"])


class RestrictedConnectionTests(_Endpoints):

    async def test_the_non_owner_connection_follows_the_valve_too(self):
        first = await ocp._get_restricted_connection(lambda: self.valves)
        self.conns.append(first)
        self.assertIs(await ocp._get_restricted_connection(lambda: self.valves), first)
        self.valves.GATEWAY_URL = "wss://gateway.example.ts.net"
        second = await ocp._get_restricted_connection(lambda: self.valves)
        self.conns.append(second)
        self.assertIsNot(second, first)
        self.assertTrue(first._retired)
        self.assertEqual(self.dialled[-1], ("gateway.example.ts.net", 443, True))
        self.assertFalse(second.writes_turns)


class StatusActionConnectionTests(_Endpoints):

    async def test_the_action_connection_follows_the_valve(self):
        first, source = await action._get_action_connection(lambda: self.valves)
        self.conns.append(first)
        self.assertEqual(source, "action-own")
        again, _ = await action._get_action_connection(lambda: self.valves)
        self.assertIs(again, first)
        self.valves.GATEWAY_URL = "gateway.lan:18789"
        second, _ = await action._get_action_connection(lambda: self.valves)
        self.conns.append(second)
        self.assertIsNot(second, first)
        self.assertEqual(self.dialled[-1], ("gateway.lan", 18789, False))
        self.assertTrue(first._retired)


if __name__ == "__main__":
    unittest.main()
