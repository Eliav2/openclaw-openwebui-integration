"""Issue #245: an Open WebUI container cannot reach a Gateway at localhost, and
the refused-connect error said only "is the Gateway running?". Inside a
container it now names the cause and the fix.

Run: uv run --with-requirements requirements-dev.txt pytest -q tests/test_container_hint_unit.py
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


def refused_message(gateway_url, in_container):
    """The GatewayError text of a connect the host refuses."""
    async def refuse(*a, **k):
        raise ConnectionRefusedError(111, "Connection refused")

    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "identity.json"), "w") as fh:
                json.dump({"id": "dev-fixed", "publicKey": "pub-fixed", "privateKey": "pem-fixed"}, fh)
            valves = types.SimpleNamespace(GATEWAY_URL=gateway_url, GATEWAY_TOKEN="tok",
                                           STATE_DIR=tmp, DEVICE_IDENTITY="", AGENT_ID="main",
                                           OWNER_USERS="")
            ws_errors = types.SimpleNamespace(WebSocketException=type("WebSocketException", (Exception,), {}))
            with mock.patch.object(ocp.websockets, "connect", refuse, create=True), \
                    mock.patch.object(ocp.websockets, "exceptions", ws_errors, create=True), \
                    mock.patch.object(ocp, "_in_container", lambda: in_container), \
                    mock.patch.object(ocp, "pipe_log", lambda *a, **k: None):
                conn = ocp._GatewayConnection(lambda: valves)
                with unittest.TestCase().assertRaises(ocp.GatewayError) as ctx:
                    await conn._connect_and_start()
                return ctx.exception
    return asyncio.run(run())


class ContainerLoopbackHintTests(unittest.TestCase):

    def test_container_with_a_loopback_gateway_gets_the_hint(self):
        for url in ("localhost:18789", "127.0.0.1:18789", "127.0.1.1:18789"):
            with self.subTest(url=url):
                ex = refused_message(url, in_container=True)
                text = str(ex)
                self.assertIn("is the container itself", text)
                self.assertIn("host.docker.internal:18789", text)
                self.assertIn("gateway.bind", text)
                self.assertEqual(ex.error["code"], ocp.GATEWAY_REFUSED_CODE)

    def test_no_hint_outside_a_container(self):
        self.assertNotIn("container", str(refused_message("localhost:18789", in_container=False)))

    def test_no_hint_for_a_gateway_that_is_not_loopback(self):
        self.assertNotIn("container", str(refused_message("192.168.1.10:18789", in_container=True)))

    def test_in_container_follows_the_marker_files(self):
        exists = {"/.dockerenv": False, "/run/.containerenv": False}
        with mock.patch.object(ocp.os.path, "exists", lambda p: exists.get(p, False)):
            self.assertFalse(ocp._in_container())
            exists["/.dockerenv"] = True
            self.assertTrue(ocp._in_container())
            exists.update({"/.dockerenv": False, "/run/.containerenv": True})
            self.assertTrue(ocp._in_container())


if __name__ == "__main__":
    unittest.main()
