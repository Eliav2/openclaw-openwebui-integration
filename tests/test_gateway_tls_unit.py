"""A Gateway behind a TLS proxy (`tailscale serve`, a reverse proxy) is reached
with GATEWAY_URL=wss://host. The scheme used to be parsed and thrown away, so the
Pipe dialled plaintext ws:// on port 18789 whatever was typed.

Run: uv run --with-requirements requirements-dev.txt pytest -q tests/test_gateway_tls_unit.py
"""
import asyncio
import json
import os
import tempfile
import types
import unittest
import urllib.request
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402


def dialled_url(gateway_url):
    """The URL the Pipe hands to websockets.connect for this GATEWAY_URL."""
    seen = []

    async def refuse(url, *a, **k):
        seen.append(url)
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
                    mock.patch.object(ocp, "pipe_log", lambda *a, **k: None):
                conn = ocp._GatewayConnection(lambda: valves)
                with unittest.TestCase().assertRaises(ocp.GatewayError):
                    await conn._connect_and_start()
    asyncio.run(run())
    return seen[0]


class GatewayConnectSchemeTests(unittest.TestCase):

    def test_wss_dials_tls_on_443(self):
        self.assertEqual(dialled_url("wss://gw.example.ts.net"), "wss://gw.example.ts.net:443")

    def test_https_dials_tls(self):
        self.assertEqual(dialled_url("https://gw.example.ts.net:8443"), "wss://gw.example.ts.net:8443")

    def test_bare_host_port_stays_plaintext(self):
        self.assertEqual(dialled_url("192.168.1.10:18789"), "ws://192.168.1.10:18789")

    def test_ws_and_http_stay_plaintext(self):
        self.assertEqual(dialled_url("ws://gw.local:18789"), "ws://gw.local:18789")
        self.assertEqual(dialled_url("http://gw.local"), "ws://gw.local:18789")


class GatewayMediaSchemeTests(unittest.TestCase):

    def fetched_url(self, gateway_url):
        seen = []

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return b"x"

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            return Resp()

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            ocp._fetch_gateway_media(gateway_url, "tok", "/media/a.png")
        return seen[0]

    def test_tls_gateway_media_is_fetched_over_https(self):
        self.assertEqual(self.fetched_url("wss://gw.example.ts.net"),
                         "https://gw.example.ts.net:443/media/a.png")

    def test_plain_gateway_media_is_fetched_over_http(self):
        self.assertEqual(self.fetched_url("gw.local:18789"), "http://gw.local:18789/media/a.png")
