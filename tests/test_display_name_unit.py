"""The Gateway prunes a silently paired device when another with the same client id, mode
and displayName pairs. Both Pipe identities use a displayName of their own, ending in the start of
the device id (two installs on one Gateway would otherwise prune each other); the main one is sent by
the Pipe and the Status Action alike, which share it."""
import asyncio
import json
import os
import tempfile
import types
import unittest
from unittest import mock

import test_pipe_unit  # noqa: F401  (installs the module stubs openclaw_pipe needs)
from _test_helpers import owui_modules
from test_owner_users_unit import FIXED_IDENT, _FakeWS, _Key

import openclaw_pipe


def connect_frame(make_conn, identity_file):
    """The `connect` request a connection sends, with the Gateway scripted to accept it."""
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, identity_file), "w") as fh:
                json.dump(FIXED_IDENT, fh)
            valves = types.SimpleNamespace(GATEWAY_URL="localhost:18789", GATEWAY_TOKEN="tok", STATE_DIR=tmp,
                                           DEVICE_IDENTITY="", AGENT_ID="main", OWNER_USERS="")
            ref = [None]
            ws = _FakeWS(ref)

            async def connect(*a, **k):
                return ws
            with mock.patch.object(openclaw_pipe.websockets, "connect", connect, create=True), \
                    mock.patch.object(openclaw_pipe.serialization, "load_pem_private_key", lambda *a, **k: _Key()):
                conn = make_conn(lambda: valves)
                ref[0] = conn
                await conn._connect_and_start()
            return json.loads(ws.sent[0])
    return asyncio.run(run())


def sent_connect_names(conn_factory, scenario, identity_file="identity.json"):
    """The displayName of every `connect` frame that `scenario(conn)` makes `conn_factory`'s
    connection send, one scripted Gateway socket per connect."""
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, identity_file), "w") as fh:
                json.dump(FIXED_IDENT, fh)
            valves = types.SimpleNamespace(GATEWAY_URL="localhost:18789", GATEWAY_TOKEN="tok", STATE_DIR=tmp,
                                           DEVICE_IDENTITY="", AGENT_ID="main", OWNER_USERS="")
            ref = [None]
            sockets = []

            async def connect(*a, **k):
                sockets.append(_FakeWS(ref))
                return sockets[-1]
            with mock.patch.object(openclaw_pipe.websockets, "connect", connect, create=True), \
                    mock.patch.object(openclaw_pipe.serialization, "load_pem_private_key", lambda *a, **k: _Key()):
                conn = conn_factory(lambda: valves)
                ref[0] = conn
                await scenario(conn)
            return [json.loads(ws.sent[0])["params"]["client"].get("displayName") for ws in sockets]
    return asyncio.run(run())


class ReconnectKeepsTheNameTests(unittest.TestCase):
    """The Gateway stores the displayName of the LAST connect on a device, so one nameless connect wipes
    it (2026-11-06: device e769bc48 went nameless at the 12:02 Gateway restart)."""

    @staticmethod
    async def _drop_and_reconnect(conn):
        await conn._connect_and_start()
        conn._ws = None            # the Gateway restarted: the socket is gone
        conn._reconnect_now.set()  # no backoff wait
        await conn._reconnect()

    def test_a_reconnect_of_the_main_identity_names_it_again(self):
        names = sent_connect_names(openclaw_pipe._GatewayConnection, self._drop_and_reconnect)
        self.assertEqual(names, ["owui-pipe-dev-fixe"] * 2)

    def test_a_reconnect_of_the_restricted_identity_names_it_again(self):
        names = sent_connect_names(openclaw_pipe._new_restricted_connection, self._drop_and_reconnect,
                                   "identity-restricted.json")
        self.assertEqual(names, ["owui-pipe-restricted-dev-fixe"] * 2)

    @staticmethod
    async def _reap_serving_then_drop(conn):
        """A new load retires `conn` while it serves a run; then its socket drops."""
        conn.register_consumer("agent:main:openwebui-u-c", "run-1")
        modules = owui_modules(_openclaw_gateway_connection_v1=conn)
        with mock.patch.dict("sys.modules", modules), mock.patch("openclaw_pipe._gateway_connection", None):
            await openclaw_pipe._reap_stale_gateway_connection()
        assert conn._retired and conn._serving(), "the retired connection still serves its run"
        conn._ws = None
        conn._reconnect_now.set()
        await conn._reconnect()

    def test_a_retired_connection_of_an_older_build_never_connects_again(self):
        """A deploy retires the previous load's connection but lets it finish its runs. A build from
        before the names reconnects nameless when its socket drops, which wipes the stored name (the
        2026-11-06 incident), so the reaper pins it shut. `sends_display_name = False` is such a build."""
        async def scenario(conn):
            conn.sends_display_name = False
            await self._reap_serving_then_drop(conn)
            with self.assertRaises(openclaw_pipe.GatewayError):
                await conn.ensure_connected()
        self.assertEqual(sent_connect_names(openclaw_pipe._GatewayConnection, scenario), [])

    def test_a_retired_connection_of_this_build_reconnects_by_name_to_finish_its_run(self):
        names = sent_connect_names(openclaw_pipe._GatewayConnection, self._reap_serving_then_drop)
        self.assertEqual(names, ["owui-pipe-dev-fixe"])


class DisplayNameTests(unittest.TestCase):
    def test_the_restricted_identity_connects_under_its_own_display_name(self):
        frame = connect_frame(openclaw_pipe._new_restricted_connection, "identity-restricted.json")
        self.assertEqual(frame["params"]["client"]["displayName"], "owui-pipe-restricted-dev-fixe")

    def test_the_main_identity_connects_under_its_own_display_name(self):
        frame = connect_frame(openclaw_pipe._GatewayConnection, "identity.json")
        self.assertEqual(frame["params"]["client"]["displayName"], "owui-pipe-dev-fixe")

    def test_the_main_and_the_restricted_identity_never_share_a_name(self):
        main = connect_frame(openclaw_pipe._GatewayConnection, "identity.json")["params"]["client"]["displayName"]
        restricted = connect_frame(openclaw_pipe._new_restricted_connection, "identity-restricted.json")["params"]["client"]["displayName"]
        self.assertTrue(main and restricted)
        self.assertNotEqual(main, restricted)

    def test_two_installs_on_one_gateway_get_different_names(self):
        main = [openclaw_pipe._display_name(openclaw_pipe.MAIN_DISPLAY_NAME, {"id": i}) for i in ("e769bc48d911eb96", "3eaa000b5b1c0f21")]
        restricted = [openclaw_pipe._display_name(openclaw_pipe.RESTRICTED_DISPLAY_NAME, {"id": i}) for i in ("e769bc48d911eb96", "3eaa000b5b1c0f21")]
        self.assertEqual(len(set(main + restricted)), 4)
        self.assertEqual(main[0], "owui-pipe-e769bc48")

    def test_without_a_device_id_the_name_is_the_base_alone(self):
        self.assertEqual(openclaw_pipe._display_name("owui-pipe", None), "owui-pipe")
        self.assertEqual(openclaw_pipe._display_name("owui-pipe", {}), "owui-pipe")

    def test_the_status_action_sends_the_pipes_name_for_the_shared_main_identity(self):
        import openclaw_status_action
        frame = connect_frame(openclaw_status_action._GatewayConnection, "identity.json")
        self.assertEqual(frame["params"]["client"]["displayName"], "owui-pipe-dev-fixe")

    def test_the_client_id_and_mode_stay_what_the_signature_covers(self):
        client = connect_frame(openclaw_pipe._new_restricted_connection, "identity-restricted.json")["params"]["client"]
        self.assertEqual((client["id"], client["mode"]), ("webchat", "cli"))


if __name__ == "__main__":
    unittest.main()
