#!/usr/bin/env python3
"""Unit tests for the Pipe's media file server (src/openclaw_pipe_pkg/media.py)
across a hot-swap redeploy."""
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

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)
from _test_helpers import fake_owui
from _test_stubs import install_stubs

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "openclaw_pipe_pkg"


def _load_media_namespace():
    install_stubs()
    ns = {}
    for mod in ("_prelude", "state", "media"):
        code = (PKG / f"{mod}.py").read_text()
        exec(compile(code, str(PKG / f"{mod}.py"), "exec"), ns)
    return types.SimpleNamespace(**ns)


fs = _load_media_namespace()


class StaleFileServerReapTests(unittest.TestCase):
    """A hot-swap deploy left the old server on the port. As in gateway.py
    (P33/P36), it is stashed on OWUI's module so the next deploy reaps it."""

    def setUp(self):
        self.stub_owui_socket_main = fake_owui(self)
        # fs is a snapshot; the function's `global` reads __globals__.
        fs._start_file_server.__globals__["_file_server_started"] = False

    def tearDown(self):
        # Release the port, or the next test's bind fails and the suite depends on test order.
        self._shutdown_stashed_server()
        fs._start_file_server.__globals__["_file_server_started"] = False

    def _shutdown_stashed_server(self):
        server = getattr(self.stub_owui_socket_main,
                         fs._STALE_FILE_SERVER_ATTR, None)
        if server is None:
            return
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass

    def _wait_up(self, port):
        """True once /health answers. One long request, since short retries gave
        up on a stalled runner (ELI-263); status only, the file is shared."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=30) as resp:
                return resp.status == 200
        except OSError:
            return False

    def test_second_deploy_reaps_first_and_rebinds_same_port(self):
        # Port 0: a fixed port could reach another suite's server.
        fs._start_file_server(port=0)
        first_server = getattr(self.stub_owui_socket_main, fs._STALE_FILE_SERVER_ATTR)
        self.assertIsNotNone(first_server, "first server never bound")
        port = first_server.server_address[1]
        self.assertTrue(self._wait_up(port), "first server never came up")

        # The next deploy: a fresh module binds while the old one listens.
        fs._start_file_server.__globals__["_file_server_started"] = False
        fs._start_file_server(port=port)
        self.assertTrue(self._wait_up(port), "second server never came up")

        second_server = getattr(self.stub_owui_socket_main, fs._STALE_FILE_SERVER_ATTR)
        self.assertIsNot(first_server, second_server, "reap did not replace the stashed server")
        # The reaped socket is closed, not only its loop stopped.
        self.assertEqual(first_server.socket.fileno(), -1)

    def test_an_upload_is_written_and_a_bad_name_refused(self):
        fs._start_file_server(port=0)
        port = getattr(self.stub_owui_socket_main, fs._STALE_FILE_SERVER_ATTR).server_address[1]
        self.assertTrue(self._wait_up(port), "server never came up")
        name = f"unit-upload-{os.getpid()}.txt"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/{name}", data=b"hi", method="PUT")
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.assertEqual(resp.read(), f"ok:{name}".encode())
        written = Path("/tmp/openclaw-pipe-media", name)
        self.addCleanup(written.unlink)
        self.assertEqual(written.read_bytes(), b"hi")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/..", data=b"hi", method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(err.exception.code, 400)

    def test_an_upload_to_a_symlink_out_of_the_directory_is_refused(self):
        fs._start_file_server(port=0)
        port = getattr(self.stub_owui_socket_main, fs._STALE_FILE_SERVER_ATTR).server_address[1]
        self.assertTrue(self._wait_up(port), "server never came up")
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        target = Path(outside, "target")
        link = Path("/tmp/openclaw-pipe-media", f"unit-link-{os.getpid()}")
        link.symlink_to(target)
        self.addCleanup(link.unlink)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/{link.name}", data=b"hi", method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(err.exception.code, 400)
        self.assertFalse(target.exists(), "the upload followed the symlink out of the directory")

    def test_reap_is_noop_outside_owui(self):
        for name in ("open_webui.socket.main", "open_webui.socket", "open_webui"):
            sys.modules.pop(name, None)
        fs._reap_stale_file_server()


class UploadDestTests(unittest.TestCase):
    """An upload's file name resolves to a file directly inside the upload
    directory, or the upload is refused."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)

    def test_a_plain_name_lands_in_the_directory(self):
        self.assertEqual(fs._upload_dest(self.dir, "/shot.png?x=1"),
                         os.path.join(os.path.realpath(self.dir), "shot.png"))

    def test_names_that_leave_the_directory_are_refused(self):
        for raw in ("", "/", "..", "/../x", "a/b", "."):
            with self.subTest(raw=raw):
                self.assertIsNone(fs._upload_dest(self.dir, raw))

    def test_a_symlink_out_of_the_directory_is_refused(self):
        os.symlink(os.path.join(self.outside, "target"), os.path.join(self.dir, "link"))
        self.assertIsNone(fs._upload_dest(self.dir, "/link"))
        self.assertFalse(os.path.exists(os.path.join(self.outside, "target")))


if __name__ == "__main__":
    unittest.main()
