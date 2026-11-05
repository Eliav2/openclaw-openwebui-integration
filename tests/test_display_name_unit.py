"""The Gateway prunes a silently paired device when another with the same client id, mode
and displayName pairs. The restricted identity uses its own displayName so others cannot
evict it; the main identity (a legacy approved record) sends none."""
import asyncio
import json
import os
import tempfile
import types
import unittest
from unittest import mock

import test_pipe_unit  # noqa: F401  (installs the module stubs openclaw_pipe needs)
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


class DisplayNameTests(unittest.TestCase):
    def test_the_restricted_identity_connects_under_its_own_display_name(self):
        frame = connect_frame(openclaw_pipe._new_restricted_connection, "identity-restricted.json")
        self.assertEqual(frame["params"]["client"]["displayName"], "owui-pipe-restricted")

    def test_the_main_identity_keeps_sending_no_display_name(self):
        frame = connect_frame(openclaw_pipe._GatewayConnection, "identity.json")
        self.assertNotIn("displayName", frame["params"]["client"])

    def test_the_client_id_and_mode_stay_what_the_signature_covers(self):
        client = connect_frame(openclaw_pipe._new_restricted_connection, "identity-restricted.json")["params"]["client"]
        self.assertEqual((client["id"], client["mode"]), ("webchat", "cli"))


if __name__ == "__main__":
    unittest.main()
