#!/usr/bin/env python3
"""Unit tests for the dev-only cross-agent deploy coordination helpers
(ELI-24), defined in src/openclaw_pipe_pkg/state.py inside a DEV-ONLY block.

These functions are stripped out of the shipped openclaw_pipe.py by
build.py (see test_build.py's leak guards), so they can't be imported from
the built artifact the way test_pipe_unit.py imports normal pipe code.
Instead this execs the *unstripped* _prelude + state fragments into a
throwaway namespace, matching what build.py would produce without the
_strip_dev_only step.

Run: python3 -m unittest test_devcoord_unit -v
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = ROOT / "src" / "openclaw_pipe_pkg"


def _install_stub_modules():
    """_prelude.py imports third-party deps OWUI provides at runtime; stub
    them out the same way test_pipe_unit.py does, so this exec doesn't need
    the real packages installed."""
    if "websockets" not in sys.modules:
        sys.modules["websockets"] = types.SimpleNamespace(
            WebSocketClientProtocol=object,
            exceptions=types.SimpleNamespace(ConnectionClosed=Exception),
        )

    if "cryptography" not in sys.modules:
        crypto = types.ModuleType("cryptography")
        hazmat = types.ModuleType("cryptography.hazmat")
        primitives = types.ModuleType("cryptography.hazmat.primitives")
        asymmetric = types.ModuleType("cryptography.hazmat.primitives.asymmetric")
        ed25519 = types.ModuleType("cryptography.hazmat.primitives.asymmetric.ed25519")
        serialization = types.ModuleType("cryptography.hazmat.primitives.serialization")
        backends = types.ModuleType("cryptography.hazmat.backends")

        class _DummyPrivateKey:
            @staticmethod
            def generate():
                raise RuntimeError("cryptography stub cannot generate keys")

        ed25519.Ed25519PrivateKey = _DummyPrivateKey
        serialization.Encoding = types.SimpleNamespace(Raw="Raw", PEM="PEM")
        serialization.PublicFormat = types.SimpleNamespace(Raw="Raw")
        serialization.PrivateFormat = types.SimpleNamespace(PKCS8="PKCS8")
        serialization.NoEncryption = lambda: None
        serialization.load_pem_private_key = lambda *args, **kwargs: None
        backends.default_backend = lambda: None

        sys.modules["cryptography"] = crypto
        sys.modules["cryptography.hazmat"] = hazmat
        sys.modules["cryptography.hazmat.primitives"] = primitives
        sys.modules["cryptography.hazmat.primitives.asymmetric"] = asymmetric
        sys.modules["cryptography.hazmat.primitives.asymmetric.ed25519"] = ed25519
        sys.modules["cryptography.hazmat.primitives.serialization"] = serialization
        sys.modules["cryptography.hazmat.backends"] = backends

    if "pydantic" not in sys.modules:
        pydantic = types.ModuleType("pydantic")

        class _BaseModel:
            def __init__(self, **kwargs):
                for name, value in self.__class__.__dict__.items():
                    if name.startswith("_") or callable(value):
                        continue
                    setattr(self, name, kwargs.get(name, value))

        def _field(*, default=None, **kwargs):
            return default

        pydantic.BaseModel = _BaseModel
        pydantic.Field = _field
        sys.modules["pydantic"] = pydantic


def _load_devcoord_namespace():
    _install_stub_modules()
    ns = {}
    for mod in ("_prelude", "state", "media"):
        code = (PKG / f"{mod}.py").read_text()
        exec(compile(code, str(PKG / f"{mod}.py"), "exec"), ns)
    return types.SimpleNamespace(**ns)


devcoord = _load_devcoord_namespace()


class DevCoordSourceContainsMarkersTests(unittest.TestCase):
    def test_markers_present_in_source(self):
        """Sanity check: if someone renames the markers, this test file
        (which relies on them being absent from the exec'd namespace check
        below) should fail loudly rather than silently testing nothing."""
        text = (PKG / "state.py").read_text()
        self.assertIn("# DEV-ONLY-START", text)
        self.assertIn("# DEV-ONLY-END", text)
        self.assertIn("_devcoord_turn_begin", text)


class DevCoordTurnMarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="devcoord-test-")
        self._old_env = os.environ.get("OPENCLAW_BRIDGE_STATE_DIR")
        os.environ["OPENCLAW_BRIDGE_STATE_DIR"] = self.tmpdir

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("OPENCLAW_BRIDGE_STATE_DIR", None)
        else:
            os.environ["OPENCLAW_BRIDGE_STATE_DIR"] = self._old_env
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _inflight_dir(self):
        return Path(self.tmpdir) / "deploy-coord" / "inflight"

    def test_turn_begin_creates_marker(self):
        marker = devcoord._devcoord_turn_begin()
        self.assertTrue(marker)
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(len(list(self._inflight_dir().iterdir())), 1)

    def test_turn_end_removes_marker(self):
        marker = devcoord._devcoord_turn_begin()
        devcoord._devcoord_turn_end(marker)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(len(list(self._inflight_dir().iterdir())), 0)

    def test_turn_end_is_idempotent(self):
        marker = devcoord._devcoord_turn_begin()
        devcoord._devcoord_turn_end(marker)
        devcoord._devcoord_turn_end(marker)  # must not raise

    def test_turn_end_none_is_noop(self):
        devcoord._devcoord_turn_end(None)  # must not raise

    def test_concurrent_turns_get_distinct_markers(self):
        m1 = devcoord._devcoord_turn_begin()
        m2 = devcoord._devcoord_turn_begin()
        self.assertNotEqual(m1, m2)
        self.assertEqual(len(list(self._inflight_dir().iterdir())), 2)
        devcoord._devcoord_turn_end(m1)
        self.assertEqual(len(list(self._inflight_dir().iterdir())), 1)
        devcoord._devcoord_turn_end(m2)
        self.assertEqual(len(list(self._inflight_dir().iterdir())), 0)


class DevCoordWaitIfDeployPendingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="devcoord-test-")
        self._old_env = os.environ.get("OPENCLAW_BRIDGE_STATE_DIR")
        os.environ["OPENCLAW_BRIDGE_STATE_DIR"] = self.tmpdir

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("OPENCLAW_BRIDGE_STATE_DIR", None)
        else:
            os.environ["OPENCLAW_BRIDGE_STATE_DIR"] = self._old_env
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_no_pending_flag_returns_immediately(self):
        start = devcoord.time.time()
        await devcoord._devcoord_wait_if_deploy_pending(max_wait_s=5.0, poll_interval_s=0.1)
        self.assertLess(devcoord.time.time() - start, 1.0)

    async def test_pending_flag_cleared_mid_wait_returns_early(self):
        pending_path = devcoord._devcoord_pending_path()
        devcoord._write_json_file(pending_path, {"requested_by": "test"})

        async def clear_after_delay():
            await asyncio.sleep(0.2)
            os.remove(pending_path)

        start = devcoord.time.time()
        await asyncio.gather(
            devcoord._devcoord_wait_if_deploy_pending(max_wait_s=5.0, poll_interval_s=0.05),
            clear_after_delay(),
        )
        elapsed = devcoord.time.time() - start
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 1.0)

    async def test_pending_flag_never_clears_hits_timeout(self):
        pending_path = devcoord._devcoord_pending_path()
        devcoord._write_json_file(pending_path, {"requested_by": "test"})
        start = devcoord.time.time()
        await devcoord._devcoord_wait_if_deploy_pending(max_wait_s=0.3, poll_interval_s=0.05)
        elapsed = devcoord.time.time() - start
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, 1.0)


class StaleFileServerReapTests(unittest.TestCase):
    """Not devcoord-specific, but discovered while testing it live: a
    hot-swap deploy leaves the *previous* file server still bound to the
    port (module-level _file_server_started resets, but the OS socket from
    the old module instance doesn't), so devcoord's routes -- and any other
    media.py fix -- silently never take effect until a container restart.
    Fixed the same way gateway.py already handles the analogous WS-connection
    zombie (P33/P36): stash the server on OWUI's own stable module so the
    next deploy can reap it first."""

    PORT = 28792

    def setUp(self):
        self.stub_owui_socket_main = types.ModuleType("open_webui.socket.main")
        sys.modules["open_webui"] = types.ModuleType("open_webui")
        sys.modules["open_webui.socket"] = types.ModuleType("open_webui.socket")
        sys.modules["open_webui.socket.main"] = self.stub_owui_socket_main
        # devcoord is a SimpleNamespace snapshot of the exec'd globals, not a
        # live view -- functions still close over the original dict via
        # __globals__, so mutating devcoord._file_server_started directly
        # wouldn't be seen by _start_file_server's `global` statement.
        devcoord._start_file_server.__globals__["_file_server_started"] = False

    def tearDown(self):
        for name in ("open_webui.socket.main", "open_webui.socket", "open_webui"):
            sys.modules.pop(name, None)
        devcoord._start_file_server.__globals__["_file_server_started"] = False

    def _wait_up(self, port, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.3)
                return True
            except Exception:
                time.sleep(0.05)
        return False

    def test_second_deploy_reaps_first_and_rebinds_same_port(self):
        devcoord._start_file_server(port=self.PORT)
        self.assertTrue(self._wait_up(self.PORT), "first server never came up")
        first_server = getattr(self.stub_owui_socket_main, devcoord._STALE_FILE_SERVER_ATTR)
        self.assertIsNotNone(first_server)

        # Simulate the next deploy: a "new module" (fresh _file_server_started)
        # trying to bind the same port while the old one is still listening.
        devcoord._start_file_server.__globals__["_file_server_started"] = False
        devcoord._start_file_server(port=self.PORT)
        self.assertTrue(self._wait_up(self.PORT), "second server never came up")

        second_server = getattr(self.stub_owui_socket_main, devcoord._STALE_FILE_SERVER_ATTR)
        self.assertIsNot(first_server, second_server, "reap did not replace the stashed server")
        # The reaped server's socket must actually be closed, not just its
        # serve_forever loop stopped, or the port would still be held.
        self.assertEqual(first_server.socket.fileno(), -1)

    def test_reap_is_noop_outside_owui(self):
        for name in ("open_webui.socket.main", "open_webui.socket", "open_webui"):
            sys.modules.pop(name, None)
        devcoord._reap_stale_file_server()  # must not raise


class DevCoordHttpRouteTests(unittest.TestCase):
    """End-to-end: actually start the file server and hit the devcoord
    routes over HTTP, the same way install.py --dev-bundle will."""

    PORT = 28791  # distinct from the real 18791 to avoid clobbering it in dev

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="devcoord-http-test-")
        cls._old_env = os.environ.get("OPENCLAW_BRIDGE_STATE_DIR")
        os.environ["OPENCLAW_BRIDGE_STATE_DIR"] = cls.tmpdir
        devcoord._start_file_server(port=cls.PORT)
        deadline = time.time() + 2.0
        last_err = None
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.PORT}/health", timeout=0.5)
                return
            except Exception as ex:
                last_err = ex
                time.sleep(0.05)
        raise RuntimeError(f"file server did not come up: {last_err}")

    @classmethod
    def tearDownClass(cls):
        if cls._old_env is None:
            os.environ.pop("OPENCLAW_BRIDGE_STATE_DIR", None)
        else:
            os.environ["OPENCLAW_BRIDGE_STATE_DIR"] = cls._old_env
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _url(self, path):
        return f"http://127.0.0.1:{self.PORT}{path}"

    def _request(self, method, path, expect_status=200):
        req = urllib.request.Request(self._url(path), method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=2)
            return resp.status, resp.read()
        except urllib.error.HTTPError as ex:
            return ex.code, ex.read()

    def setUp(self):
        # devcoord state is process-global via STATE_DIR; reset between tests.
        try:
            os.remove(devcoord._devcoord_pending_path())
        except FileNotFoundError:
            pass
        for f in Path(devcoord._devcoord_dir()).iterdir():
            f.unlink()

    def test_status_route_reports_zero_when_idle(self):
        status, body = self._request("GET", "/__devcoord__/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"inflight": 0})

    def test_status_route_reflects_inflight_turns(self):
        m1 = devcoord._devcoord_turn_begin()
        m2 = devcoord._devcoord_turn_begin()
        status, body = self._request("GET", "/__devcoord__/status")
        self.assertEqual(json.loads(body), {"inflight": 2})
        devcoord._devcoord_turn_end(m1)
        devcoord._devcoord_turn_end(m2)

    def test_post_deploy_pending_sets_flag_readable_by_pipe(self):
        status, _ = self._request("POST", "/__devcoord__/deploy-pending")
        self.assertEqual(status, 200)
        self.assertTrue(devcoord._read_json_file(devcoord._devcoord_pending_path()))

    def test_delete_deploy_pending_clears_flag(self):
        self._request("POST", "/__devcoord__/deploy-pending")
        status, _ = self._request("DELETE", "/__devcoord__/deploy-pending")
        self.assertEqual(status, 200)
        self.assertIsNone(devcoord._read_json_file(devcoord._devcoord_pending_path()))

    def test_delete_deploy_pending_when_absent_is_ok(self):
        status, _ = self._request("DELETE", "/__devcoord__/deploy-pending")
        self.assertEqual(status, 200)

    def test_unknown_delete_path_is_404(self):
        status, _ = self._request("DELETE", "/some/other/path")
        self.assertEqual(status, 404)

    def test_regular_media_get_still_works(self):
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")

    def test_regular_upload_post_still_works(self):
        req = urllib.request.Request(
            self._url("/some-file.txt"), method="POST", data=b"hello",
        )
        resp = urllib.request.urlopen(req, timeout=2)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.read(), b"ok:some-file.txt")


if __name__ == "__main__":
    unittest.main()
