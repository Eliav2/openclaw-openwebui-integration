#!/usr/bin/env python3
"""Unit tests for install.py's own safety logic, with click and rich stubbed."""
import http.server
import shutil
import tempfile
import sys
import threading
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    """The README's no-clone install (`uv run https://raw.githubusercontent.com/.../install.py
    install --wizard`) fetches the artifact, before the wizard asks for credentials."""

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

    def test_returns_the_fetched_artifact(self):
        body = b'"""frontmatter"""\n\n\nclass Pipe:\n    pass\n'
        inst.ARTIFACT_URL = self._serve(body)
        self.assertEqual(inst._fetch_pipe_file(), body.decode())

    def test_rejects_a_200_that_is_not_the_artifact(self):
        # A raw URL typo can return an HTML page with status 200.
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
        # For a fork, a pinned tag or a local mirror.
        import importlib.util
        import os
        from unittest import mock

        def fresh_url(**env):
            spec = importlib.util.spec_from_file_location("install_env_probe", ROOT / "install.py")
            mod = importlib.util.module_from_spec(spec)
            # dataclasses look their module up in sys.modules.
            with mock.patch.dict(os.environ, env), mock.patch.dict(sys.modules, {spec.name: mod}):
                if not env:
                    os.environ.pop("OPENCLAW_PIPE_ARTIFACT_URL", None)
                spec.loader.exec_module(mod)
            return mod.ARTIFACT_URL

        self.assertEqual(fresh_url(OPENCLAW_PIPE_ARTIFACT_URL="http://mirror.example/openclaw_pipe.py"),
                         "http://mirror.example/openclaw_pipe.py")
        self.assertEqual(fresh_url(), "https://raw.githubusercontent.com/Eliav2/openclaw-openwebui-integration"
                                      "/main/openclaw_pipe.py")


class MirrorPipeValvesTests(unittest.TestCase):
    """Open WebUI stores exactly the posted valves, so copying the five shared ones
    must keep the Action's own CONTROL_UI_URL (ELI-141)."""

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
    """ELI-146: the Action and Filter install through install.py's core, a FunctionSpec each."""

    PIPE = {"id": "openclaw_gateway", "type": "pipe", "content": "pipe", "is_active": True, "is_global": True}
    PIPE_VALVES = {"GATEWAY_URL": "localhost:18789", "GATEWAY_TOKEN": "t", "DEVICE_IDENTITY": "",
                   "STATE_DIR": "/data/openclaw", "AGENT_ID": "main", "PIPE_ONLY": "x"}

    def setUp(self):
        import dataclasses
        from unittest import mock
        import install_action
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for patch in (mock.patch.object(inst, "BACKUP_DIR", tmp / "backups"),
                      mock.patch.object(inst, "rebuild_artifact", lambda spec: None)):
            patch.start()
            self.addCleanup(patch.stop)
        (tmp / "action.py").write_text("action v2")
        self.action = dataclasses.replace(install_action.SPEC, artifact=tmp / "action.py")

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

    def test_a_server_that_kept_the_old_code_fails_the_install_and_the_status(self):
        old = {"id": "openclaw_status_action", "type": "action", "content": "action v1",
               "is_active": True, "is_global": True}
        owui = _FakeOwui([self.PIPE, old], {"openclaw_gateway": self.PIPE_VALVES}, takes_updates=False)
        self.assertFalse(inst.install_function(owui, self.action))
        self.assertFalse(inst.function_status(owui, self.action))


class UpdateValvesTests(unittest.TestCase):
    """A re-run keeps the switches an operator turned off: ENABLE_FILE_SERVER
    off must survive a deploy, or the deploy's smoke test starts the
    unauthenticated file server again."""

    CFG = dict(owui_url="http://o", owui_email="e", owui_password="p", gateway_url="localhost:18789",
               gateway_token="t", agent_id="main", state_dir="/data/openclaw-bridge",
               owui_api_base_url="http://127.0.0.1:8080", owui_api_key="", file_server_base_url="")

    def _run(self, stored):
        owui = _FakeOwui([{"id": inst.FUNCTION_ID, "type": "pipe", "content": "pipe"}],
                         {inst.FUNCTION_ID: stored} if stored is not None else {})
        # The identity step generates keys and writes the local identity file; this test is about the switches.
        from unittest import mock
        with mock.patch.object(inst, "choose_identity", return_value={"id": "test-device"}):
            inst.update_valves(owui, inst.Config(**self.CFG))
        return owui.valves[inst.FUNCTION_ID]

    def test_a_first_install_turns_the_switches_on(self):
        v = self._run(None)
        self.assertEqual([v[k] for k in ("ENABLE_FILE_SERVER", "USE_OWUI_FILES", "SEND_STOP_ON_CANCEL")],
                         [True, True, True])

    def test_a_re_run_keeps_a_switch_the_operator_turned_off(self):
        stored = {"ENABLE_FILE_SERVER": False, "USE_OWUI_FILES": False, "SEND_STOP_ON_CANCEL": False,
                  "GATEWAY_URL": "other:1", "STATE_DIR": "/old"}
        v = self._run(stored)
        self.assertEqual([v[k] for k in ("ENABLE_FILE_SERVER", "USE_OWUI_FILES", "SEND_STOP_ON_CANCEL")],
                         [False, False, False])
        # The connection valves are still the run's own.
        self.assertEqual((v["GATEWAY_URL"], v["STATE_DIR"]), ("localhost:18789", "/data/openclaw-bridge"))

    def test_a_switch_missing_from_an_old_install_is_added_on(self):
        v = self._run({"ENABLE_FILE_SERVER": False})
        self.assertEqual((v["ENABLE_FILE_SERVER"], v["USE_OWUI_FILES"], v["SEND_STOP_ON_CANCEL"]),
                         (False, True, True))


class _OwuiChatCompletions:
    """Open WebUI's /api/chat/completions as measured on 0.13.0: a `metadata` field in the
    request is dropped, a top-level `chat_id` must name a chat row (404 otherwise), and a
    top-level `session_id` reaches the Pipe as metadata.session_id, which the Pipe uses
    as its session when there is no chat id. A request with neither gets a random one."""

    def __init__(self):
        self.bodies = []
        self.sessions = []

    def request(self, method, path, body=None, timeout=30):
        if path == "/api/v1/models":
            return 200, {"data": [{"id": f"{inst.FUNCTION_ID}.default"}, {"id": f"{inst.FUNCTION_ID}.other"}]}
        self.bodies.append(body)
        if body.get("chat_id"):
            return 404, {"detail": "not found"}
        self.sessions.append(body.get("session_id") or f"owui-random-{len(self.sessions)}")
        return 200, {"choices": [{"message": {"content": "OK_CONFIG_TEST other"}}]}


class SmokeSessionTests(unittest.TestCase):
    """The smokes name their Gateway session in the field Open WebUI passes on, so a deploy
    reuses one session per smoke instead of starting a new Opus session each time."""

    def run_smokes(self, runs=2):
        from datetime import datetime, timezone
        from unittest import mock
        owui = _OwuiChatCompletions()
        with mock.patch.object(inst, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
            for _ in range(runs):
                self.assertTrue(inst.smoke_test(owui))
                self.assertTrue(inst.route_smoke_test(owui))
        return owui

    def test_repeated_deploys_reuse_one_session_per_smoke(self):
        owui = self.run_smokes()
        smoke, route = owui.sessions[0::2], owui.sessions[1::2]
        self.assertEqual(len(set(smoke)), 1, smoke)
        self.assertEqual(len(set(route)), 1, route)

    def test_a_new_utc_day_starts_a_new_session(self):
        from datetime import datetime, timezone
        from unittest import mock
        day = lambda d, h: datetime(2026, 11, d, h, 30, tzinfo=timezone.utc)  # noqa: E731
        with mock.patch.object(inst, "datetime", wraps=datetime) as clock:
            clock.now.return_value = day(5, 23)
            late = _OwuiChatCompletions()
            inst.smoke_test(late)
            clock.now.return_value = day(6, 0)
            early = _OwuiChatCompletions()
            inst.smoke_test(early)
        self.assertEqual(late.sessions, ["install-smoke-2026-11-05"])
        self.assertEqual(early.sessions, ["install-smoke-2026-11-06"])

    def test_the_session_id_uses_the_utc_date_not_the_local_one(self):
        from datetime import datetime, timedelta, timezone
        tz = timezone(timedelta(hours=14))
        self.assertEqual(inst.smoke_session_id("p", datetime(2026, 11, 6, 1, 0, tzinfo=tz).astimezone(timezone.utc)), "p-2026-11-05")

    def test_the_two_smokes_do_not_share_a_session(self):
        owui = self.run_smokes(runs=1)
        self.assertEqual(len(set(owui.sessions)), 2, owui.sessions)

    def test_no_request_carries_the_field_open_webui_drops(self):
        owui = self.run_smokes(runs=1)
        for body in owui.bodies:
            self.assertNotIn("metadata", body)
            self.assertNotIn("chat_id", body)


class IdentityFileTests(unittest.TestCase):
    """PIPE_DEVICE_IDENTITY_FILE moves the installer's copy of the device identity; unset, it stays next to install.py."""

    def test_unset_it_is_next_to_install_py(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {}, clear=False):
            os_env = __import__("os").environ
            os_env.pop(inst.IDENTITY_FILE_ENV, None)
            self.assertEqual(inst.local_identity_file(), inst.ROOT / ".pipe_device_identity.json")

    def test_set_the_identity_is_written_and_read_there_and_not_next_to_install_py(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "stack-a" / "identity.json"
            target.parent.mkdir()
            with mock.patch.dict("os.environ", {inst.IDENTITY_FILE_ENV: str(target)}), \
                    mock.patch.object(inst, "generate_device_identity", return_value={"id": "dev-a", "privateKey": "k", "publicKey": "p"}):
                made = inst.choose_identity({})
                self.assertEqual(made["id"], "dev-a")
                self.assertTrue(target.exists())
                self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")
                again = inst.choose_identity({})                      # a reinstall of this stack finds it
                self.assertEqual(again["id"], "dev-a")
            self.assertFalse(inst.DEFAULT_IDENTITY_FILE.exists() and inst.DEFAULT_IDENTITY_FILE.stat().st_mtime > target.stat().st_mtime)

    def test_a_missing_directory_for_the_file_is_created(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "not" / "yet" / "identity.json"
            with mock.patch.dict("os.environ", {inst.IDENTITY_FILE_ENV: str(target)}), \
                    mock.patch.object(inst, "generate_device_identity", return_value={"id": "dev-a", "privateKey": "k", "publicKey": "p"}):
                inst.choose_identity({})
            self.assertTrue(target.exists())
            self.assertEqual(oct(target.parent.stat().st_mode & 0o777), "0o700")

    def test_two_stacks_get_two_identities(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            ids = []
            for name in ("a", "b"):
                with mock.patch.dict("os.environ", {inst.IDENTITY_FILE_ENV: str(Path(d) / f"{name}.json")}), \
                        mock.patch.object(inst, "generate_device_identity", return_value={"id": f"dev-{name}", "privateKey": "k", "publicKey": "p"}):
                    ids.append(inst.choose_identity({})["id"])
            self.assertEqual(ids, ["dev-a", "dev-b"])


if __name__ == "__main__":
    unittest.main()
