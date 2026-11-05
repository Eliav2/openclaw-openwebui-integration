#!/usr/bin/env python3
"""Unit tests for the Pipe's media file server (src/openclaw_pipe_pkg/media.py)
across a hot-swap redeploy. Execs the _prelude, state and media fragments into
a throwaway namespace, with the third-party modules Open WebUI provides
stubbed.

Run: python3 -m pytest -q tests/test_file_server_unit.py
"""
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
    """A hot-swap deploy leaves the *previous* file server still bound to the
    port (module-level _file_server_started resets, but the OS socket from
    the old module instance doesn't), so a media.py fix silently never takes
    effect until a container restart.
    Fixed the same way gateway.py already handles the analogous WS-connection
    zombie (P33/P36): stash the server on OWUI's own stable module so the
    next deploy can reap it first."""

    def setUp(self):
        self.stub_owui_socket_main = fake_owui(self)
        # fs is a SimpleNamespace snapshot of the exec'd globals, not a
        # live view -- functions still close over the original dict via
        # __globals__, so mutating fs._file_server_started directly
        # wouldn't be seen by _start_file_server's `global` statement.
        fs._start_file_server.__globals__["_file_server_started"] = False

    def tearDown(self):
        # Shut the server down and release the port. Without this the daemon
        # thread outlives the test and keeps PORT bound for the rest of the
        # process, so the NEXT test to bind it fails -- and because
        # _start_file_server swallows bind errors, that failure surfaces as the
        # misleading "server never came up" rather than "port in use". This made
        # the suite order-dependent: it passed on 3.10-3.12 and failed on 3.13
        # purely because test ordering/timing differs.
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
        """True once the server answers /health. Its socket is bound and
        listening before _start_file_server returns, so one request waits in
        the listen queue until the serving thread takes it. A retry loop with
        short timeouts gave up on that request when a busy runner stalled the
        process, and the server then answered a client already gone (ELI-263).
        The timeout only bounds a server that never serves. Its status, not
        its body: the health file is shared by every process on the host and
        rewritten by each server start."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=30) as resp:
                return resp.status == 200
        except OSError:
            return False

    def test_second_deploy_reaps_first_and_rebinds_same_port(self):
        # Port 0: a fixed port is shared by every suite on the host, and a
        # second suite running at the same time answers this one's requests.
        fs._start_file_server(port=0)
        first_server = getattr(self.stub_owui_socket_main, fs._STALE_FILE_SERVER_ATTR)
        self.assertIsNotNone(first_server, "first server never bound")
        port = first_server.server_address[1]
        self.assertTrue(self._wait_up(port), "first server never came up")

        # Simulate the next deploy: a "new module" (fresh _file_server_started)
        # trying to bind the same port while the old one is still listening.
        fs._start_file_server.__globals__["_file_server_started"] = False
        fs._start_file_server(port=port)
        self.assertTrue(self._wait_up(port), "second server never came up")

        second_server = getattr(self.stub_owui_socket_main, fs._STALE_FILE_SERVER_ATTR)
        self.assertIsNot(first_server, second_server, "reap did not replace the stashed server")
        # The reaped server's socket must actually be closed, not just its
        # serve_forever loop stopped, or the port would still be held.
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
        fs._reap_stale_file_server()  # must not raise


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
