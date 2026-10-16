#!/usr/bin/env python3
"""Unit tests for install.py's own safety logic (not the deployed pipe).

install.py imports click/rich, which aren't installed in this environment's
system python (it's normally run via `uv run install.py`, which resolves
them into an ephemeral env). These tests stub both modules just enough to
import install.py directly, the same way test_file_server_unit.py stubs
websockets/cryptography/pydantic to import pipe.py fragments in isolation.

Run: python3 -m unittest test_install_unit -v
"""
import http.server
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
        console_mod.Console = lambda *a, **k: types.SimpleNamespace(
            print=lambda *a, **k: None, print_json=lambda *a, **k: None, rule=lambda *a, **k: None)
        panel_mod.Panel = lambda *a, **k: None
        prompt_mod.Prompt = types.SimpleNamespace(ask=lambda *a, **k: "")
        sys.modules["rich"] = rich
        sys.modules["rich.console"] = console_mod
        sys.modules["rich.panel"] = panel_mod
        sys.modules["rich.prompt"] = prompt_mod


_install_stub_modules()
sys.path.insert(0, str(ROOT))
import install as inst  # noqa: E402


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
        got = inst._fetch_pipe_file()
        self.assertIsInstance(got, str)
        self.assertIn("class Pipe", got)

    def test_rejects_a_200_that_is_not_the_artifact(self):
        # A raw URL typo on GitHub can return an HTML page with status 200.
        inst.ARTIFACT_URL = self._serve(b"<html>not found</html>")
        with self.assertRaises(SystemExit) as cm:
            inst._fetch_pipe_file()
        self.assertIn("did not return the pipe artifact", str(cm.exception))

    def test_unreachable_url_explains_the_clone_fallback(self):
        inst.ARTIFACT_URL = "http://127.0.0.1:1/openclaw_pipe.py"
        with self.assertRaises(SystemExit) as cm:
            inst._fetch_pipe_file()
        msg = str(cm.exception)
        self.assertIn("Could not download", msg)
        self.assertIn("git clone", msg)

    def test_url_is_overridable_by_env(self):
        # Lets a fork, a pinned tag, or a local mirror drive the no-clone path.
        self.assertIn("openclaw_pipe.py", inst.ARTIFACT_URL)


class MirrorPipeValvesTests(unittest.TestCase):
    """install_action.py copies the five shared valves from the Pipe. Open WebUI
    stores exactly the posted valve set (`Valves(**form).model_dump(exclude_unset
    =True)`), so a post of only the shared five would reset the Action's own
    CONTROL_UI_URL on every re-install (ELI-141)."""

    def test_the_actions_own_valves_survive_a_mirror(self):
        import install_action

        pipe = {"GATEWAY_URL": "localhost:18789", "GATEWAY_TOKEN": "new-token", "DEVICE_IDENTITY": "",
                "STATE_DIR": "/data/openclaw", "AGENT_ID": "main", "PIPE_ONLY": "x"}
        action = {"GATEWAY_URL": "localhost:18789", "GATEWAY_TOKEN": "old-token", "DEVICE_IDENTITY": "",
                  "STATE_DIR": "/data/openclaw", "AGENT_ID": "main",
                  "CONTROL_UI_URL": "https://claw.example.com"}
        posted = []

        class _Client:
            def request(self, method, path, body=None):
                if method == "GET":
                    return 200, dict(pipe if install_action.PIPE_FUNCTION_ID in path else action)
                posted.append(body)
                return 200, body

        install_action.mirror_pipe_valves(_Client())
        self.assertEqual(posted, [{**action, "GATEWAY_TOKEN": "new-token"}])


class _FakeOwui:
    """Open WebUI's functions API, in memory: what the installer core calls."""

    def __init__(self, functions=None, valves=None, takes_updates=True):
        self.functions = {f["id"]: dict(f) for f in (functions or [])}
        self.valves = {k: dict(v) for k, v in (valves or {}).items()}
        self.takes_updates = takes_updates
        self.posts = []

    def request(self, method, path, body=None, timeout=30):
        if method == "POST":
            self.posts.append(path)
        if path == "/api/v1/functions/":
            return 200, [dict(f) for f in self.functions.values()]
        if path == "/api/v1/functions/create":
            self.functions[body["id"]] = {**body, "is_active": False, "is_global": False}
            return 200, dict(body)
        if path.startswith("/api/v1/functions/id/"):
            fid, _, rest = path[len("/api/v1/functions/id/"):].partition("/")
            fn = self.functions.get(fid)
            if fn is None:
                return 404, {"detail": "not found"}
            if rest == "update":
                if self.takes_updates:
                    fn["content"] = body["content"]
                return 200, dict(fn)
            if rest == "valves":
                return 200, dict(self.valves.get(fid, {}))
            if rest == "valves/update":
                self.valves[fid] = dict(body)
                return 200, dict(body)
            if rest in ("toggle", "toggle/global"):
                key = "is_active" if rest == "toggle" else "is_global"
                fn[key] = not fn[key]
                return 200, dict(fn)
            return 200, dict(fn)
        if path.startswith("/api/chat/actions/"):
            return 200, {"status": "error", "error": "no session"}
        return 404, {}


class InstallerCoreTests(unittest.TestCase):
    """ELI-146: the Action and the Filter install through install.py's core,
    a FunctionSpec each, as the Pipe's own deploy does."""

    PIPE = {"id": "openclaw_gateway", "type": "pipe", "content": "pipe", "is_active": True, "is_global": True}
    PIPE_VALVES = {"GATEWAY_URL": "localhost:18789", "GATEWAY_TOKEN": "t", "DEVICE_IDENTITY": "",
                   "STATE_DIR": "/data/openclaw", "AGENT_ID": "main", "PIPE_ONLY": "x"}

    def setUp(self):
        import dataclasses
        from unittest import mock
        import install_action
        import install_filter
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for patch in (mock.patch.object(inst, "BACKUP_DIR", tmp / "backups"),
                      mock.patch.object(inst, "rebuild_artifact", lambda spec: None)):
            patch.start()
            self.addCleanup(patch.stop)
        (tmp / "action.py").write_text("action v2")
        (tmp / "filter.py").write_text("filter v2")
        self.action = dataclasses.replace(install_action.SPEC, artifact=tmp / "action.py")
        self.filter = dataclasses.replace(install_filter.SPEC, artifact=tmp / "filter.py")

    def test_a_new_action_is_created_active_global_with_the_pipes_valves(self):
        owui = _FakeOwui([self.PIPE], {"openclaw_gateway": self.PIPE_VALVES})
        self.assertTrue(inst.install_function(owui, self.action))
        fn = owui.functions["openclaw_status_action"]
        self.assertEqual((fn["type"], fn["content"], fn["is_active"], fn["is_global"]),
                         ("action", "action v2", True, True))
        self.assertEqual(owui.valves["openclaw_status_action"],
                         {k: v for k, v in self.PIPE_VALVES.items() if k != "PIPE_ONLY"})
        self.assertTrue(inst.function_status(owui, self.action))

    def test_an_update_is_in_place_and_keeps_the_actions_own_valves(self):
        old = {"id": "openclaw_status_action", "type": "action", "content": "action v1",
               "is_active": True, "is_global": True}
        own = {**{k: v for k, v in self.PIPE_VALVES.items() if k != "PIPE_ONLY"},
               "GATEWAY_TOKEN": "old", "CONTROL_UI_URL": "https://claw.example.com"}
        owui = _FakeOwui([self.PIPE, old], {"openclaw_gateway": self.PIPE_VALVES, "openclaw_status_action": own})
        self.assertTrue(inst.install_function(owui, self.action))
        self.assertNotIn("/api/v1/functions/create", owui.posts)
        self.assertEqual(owui.functions["openclaw_status_action"]["content"], "action v2")
        self.assertEqual(owui.valves["openclaw_status_action"]["CONTROL_UI_URL"], "https://claw.example.com")
        self.assertEqual(owui.valves["openclaw_status_action"]["GATEWAY_TOKEN"], "t")
        self.assertFalse([p for p in owui.posts if p.endswith("/toggle") or p.endswith("/toggle/global")])

    def test_the_filter_is_activated_and_never_made_global(self):
        owui = _FakeOwui([self.PIPE])
        self.assertTrue(inst.install_function(owui, self.filter))
        fn = owui.functions["openclaw_thinking"]
        self.assertEqual((fn["type"], fn["is_active"], fn["is_global"]), ("filter", True, False))
        self.assertTrue(inst.function_status(owui, self.filter), "per-model is the right state")

    def test_a_server_that_kept_the_old_code_fails_the_install_and_the_status(self):
        old = {"id": "openclaw_thinking", "type": "filter", "content": "filter v1",
               "is_active": True, "is_global": False}
        owui = _FakeOwui([self.PIPE, old], takes_updates=False)
        self.assertFalse(inst.install_function(owui, self.filter))
        self.assertFalse(inst.function_status(owui, self.filter))


if __name__ == "__main__":
    unittest.main()
