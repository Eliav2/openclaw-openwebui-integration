#!/usr/bin/env python3
"""Unit tests for install.py's own safety logic (not the deployed pipe).

install.py imports click/rich, which aren't installed in this environment's
system python (it's normally run via `uv run install.py`, which resolves
them into an ephemeral env). These tests stub both modules just enough to
import install.py directly, the same way test_devcoord_unit.py stubs
websockets/cryptography/pydantic to import pipe.py fragments in isolation.

Run: python3 -m unittest test_install_unit -v
"""
import http.server
import json
import shutil
import tempfile
import sys
import threading
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _install_stub_modules():
    if "click" not in sys.modules:
        click = types.ModuleType("click")
        click.command = lambda *a, **k: (lambda f: f)
        click.option = lambda *a, **k: (lambda f: f)
        click.argument = lambda *a, **k: (lambda f: f)

        class _FakeGroup:
            def command(self, *a, **k):
                return lambda f: f

            def group(self, *a, **k):
                return lambda f: _FakeGroup()

            def __call__(self, *a, **k):
                pass

        click.group = lambda *a, **k: (lambda f: _FakeGroup())
        click.echo = print
        click.Choice = lambda *a, **k: None
        click.pass_context = lambda f: f
        click.Context = object
        sys.modules["click"] = click

    if "rich" not in sys.modules:
        rich = types.ModuleType("rich")
        console_mod = types.ModuleType("rich.console")
        panel_mod = types.ModuleType("rich.panel")
        prompt_mod = types.ModuleType("rich.prompt")
        console_mod.Console = lambda *a, **k: types.SimpleNamespace(print=lambda *a, **k: None)
        panel_mod.Panel = lambda *a, **k: None
        prompt_mod.Prompt = types.SimpleNamespace(ask=lambda *a, **k: "")
        sys.modules["rich"] = rich
        sys.modules["rich.console"] = console_mod
        sys.modules["rich.panel"] = panel_mod
        sys.modules["rich.prompt"] = prompt_mod


_install_stub_modules()
sys.path.insert(0, str(ROOT))
import install as inst  # noqa: E402


class _FakeCfg:
    dev_bundle = False
    confirm_downgrade_from_dev_bundle = False
    owui_url = "http://127.0.0.1:1"


class DowngradeGuardTests(unittest.TestCase):
    """A live pipe only answers /__devcoord__/status if it's running the dev
    bundle, so that response is the signal used to refuse a plain (non
    --dev-bundle) deploy that would silently delete the ELI-24 coordination
    mechanism from the live instance."""

    @classmethod
    def setUpClass(cls):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/__devcoord__/status":
                    body = json.dumps({"inflight": 0}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *a):
                pass

        cls.port = 28798
        cls.server = http.server.HTTPServer(("127.0.0.1", cls.port), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_allows_plain_deploy_when_live_pipe_is_not_dev_bundle(self):
        inst.DEVCOORD_PORT = 1  # nothing listening -- simulates a plain live pipe
        inst._assert_not_silently_downgrading_from_dev_bundle(_FakeCfg())  # must not raise

    def test_dev_bundle_flag_skips_the_check_entirely(self):
        class Cfg(_FakeCfg):
            dev_bundle = True
            owui_url = f"http://127.0.0.1:{self.port}"

        inst.DEVCOORD_PORT = self.port
        inst._assert_not_silently_downgrading_from_dev_bundle(Cfg())  # must not raise

    def test_refuses_plain_deploy_over_a_live_dev_bundle_pipe(self):
        class Cfg(_FakeCfg):
            owui_url = f"http://127.0.0.1:{self.port}"

        inst.DEVCOORD_PORT = self.port
        with self.assertRaises(SystemExit) as ctx:
            inst._assert_not_silently_downgrading_from_dev_bundle(Cfg())
        self.assertIn("Refusing to deploy", str(ctx.exception))
        self.assertIn("--confirm-downgrade-from-dev-bundle", str(ctx.exception))

    def test_confirm_flag_allows_the_downgrade(self):
        class Cfg(_FakeCfg):
            owui_url = f"http://127.0.0.1:{self.port}"
            confirm_downgrade_from_dev_bundle = True

        inst.DEVCOORD_PORT = self.port
        inst._assert_not_silently_downgrading_from_dev_bundle(Cfg())  # must not raise


class FetchPipeArtifactTests(unittest.TestCase):
    """The README advertises a no-clone install:

        uv run https://raw.githubusercontent.com/.../install.py install --wizard

    Under that form `__file__` is a lone temp copy, so openclaw_pipe.py is not
    beside it and there is no src/ tree to build from. Before the fetch step the
    deploy died on "Pipe file not found" -- and only AFTER the wizard had already
    collected the OWUI password and the gateway token.
    """

    def setUp(self):
        self._orig_url = inst.ARTIFACT_URL
        self.tmp = tempfile.mkdtemp(prefix="fetch-test-")

    def tearDown(self):
        inst.ARTIFACT_URL = self._orig_url
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _serve(self, body: bytes, status: int = 200):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return f"http://127.0.0.1:{srv.server_address[1]}/openclaw_pipe.py"

    def test_fetches_and_writes_the_artifact(self):
        body = b'"""frontmatter"""\n\n\nclass Pipe:\n    pass\n'
        inst.ARTIFACT_URL = self._serve(body)
        got = inst._fetch_pipe_file(Path(self.tmp))
        self.assertTrue(got.exists())
        self.assertIn("class Pipe", got.read_text())

    def test_rejects_a_200_that_is_not_the_artifact(self):
        # A raw URL typo on GitHub can return an HTML page with status 200.
        inst.ARTIFACT_URL = self._serve(b"<html>not found</html>")
        with self.assertRaises(SystemExit) as cm:
            inst._fetch_pipe_file(Path(self.tmp))
        self.assertIn("did not return the pipe artifact", str(cm.exception))

    def test_unreachable_url_explains_the_clone_fallback(self):
        inst.ARTIFACT_URL = "http://127.0.0.1:1/openclaw_pipe.py"
        with self.assertRaises(SystemExit) as cm:
            inst._fetch_pipe_file(Path(self.tmp))
        msg = str(cm.exception)
        self.assertIn("Could not download", msg)
        self.assertIn("git clone", msg)

    def test_url_is_overridable_by_env(self):
        # Lets a fork, a pinned tag, or a local mirror drive the no-clone path.
        self.assertIn("openclaw_pipe.py", inst.ARTIFACT_URL)


if __name__ == "__main__":
    unittest.main()
