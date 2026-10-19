#!/usr/bin/env python3
"""ELI-221: an installer run from a sandbox shell must not deploy to the host's
Open WebUI by accident.

A sandbox (e2e-local's run.sh) sets E2E_ROOT and runs inside its own network
namespace, where localhost:8080 is the sandbox's Open WebUI. The same command
typed in the host shell with E2E_ROOT still exported reaches the live Open
WebUI on the host instead. The installers refuse that unless --live is given.
A plain shell (no E2E_ROOT) is unchanged.

The CLI runs as a real subprocess against a recording HTTP server standing in
for the target Open WebUI, so "refused" means no request reached it.

Run: uv run --with-requirements requirements-dev.txt pytest -q tests/test_install_sandbox_guard_unit.py
"""
import http.server
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
INSTALLERS = ("install.py", "install_action.py", "install_filter.py")


class _Recorder(http.server.BaseHTTPRequestHandler):
    def _record(self):
        self.server.paths.append(self.path)
        self.send_response(500)
        self.end_headers()

    do_GET = do_POST = _record

    def log_message(self, *args):
        pass


class InstallerSandboxGuardTests(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        self.server.paths = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def run_installer(self, script, *args, e2e_root=None, command="repair"):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("OWUI_", "GATEWAY_", "E2E_"))}
        env.update(OWUI_URL=self.url, OWUI_EMAIL="admin@example.com", OWUI_PASSWORD="pw",
                   GATEWAY_TOKEN="t", OPENCLAW_BRIDGE_STATE_DIR="/tmp/none")
        if e2e_root:
            env["E2E_ROOT"] = e2e_root
        return subprocess.run([sys.executable, str(ROOT / script), command, *args],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)

    def test_a_repair_from_a_sandbox_shell_in_the_host_netns_is_refused_and_reaches_nothing(self):
        for script in INSTALLERS:
            with self.subTest(script=script):
                self.server.paths.clear()
                done = self.run_installer(script, e2e_root="/tmp/e2e-w9")
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("E2E_ROOT", done.stderr + done.stdout)
                self.assertIn("--live", done.stderr + done.stdout)
                self.assertEqual(self.server.paths, [])

    def test_a_repair_from_a_plain_shell_still_reaches_its_target(self):
        for script in INSTALLERS:
            with self.subTest(script=script):
                self.server.paths.clear()
                self.run_installer(script)
                self.assertTrue(self.server.paths, "the plain-shell repair never reached Open WebUI")

    def test_status_changes_nothing_so_a_sandbox_shell_may_read_the_host(self):
        for script in INSTALLERS:
            with self.subTest(script=script):
                self.server.paths.clear()
                self.run_installer(script, e2e_root="/tmp/e2e-w9", command="status")
                self.assertTrue(self.server.paths, "status never reached Open WebUI")

    def test_live_lets_a_sandbox_shell_deploy_to_the_host(self):
        for script in INSTALLERS:
            with self.subTest(script=script):
                self.server.paths.clear()
                self.run_installer(script, "--live", e2e_root="/tmp/e2e-w9")
                self.assertTrue(self.server.paths, "--live did not reach Open WebUI")


class SandboxNetnsTests(unittest.TestCase):
    """Inside the sandbox's own network namespace localhost is the sandbox, so
    the wrapper's deploys (run.sh: E2E_ROOT set) go through untouched."""

    def setUp(self):
        sys.path.insert(0, str(ROOT))
        self.addCleanup(sys.path.remove, str(ROOT))
        import install
        self.install = install

    def links(self, own, init):
        table = {"/proc/self/ns/net": own, "/proc/1/ns/net": init}

        def readlink(path):
            if isinstance(table[path], Exception):
                raise table[path]
            return table[path]
        return mock.patch.object(self.install.os, "readlink", readlink)

    def test_inside_the_sandbox_netns_nothing_is_refused(self):
        with mock.patch.dict(os.environ, {"E2E_ROOT": "/tmp/e2e-w9"}), \
                self.links("net:[4026532999]", "net:[4026531840]"):
            self.assertIsNone(self.install.sandbox_refusal(live=False))

    def test_in_the_host_netns_it_is_refused(self):
        with mock.patch.dict(os.environ, {"E2E_ROOT": "/tmp/e2e-w9"}), \
                self.links("net:[4026531840]", "net:[4026531840]"):
            self.assertIn("E2E_ROOT", self.install.sandbox_refusal(live=False))
            self.assertIsNone(self.install.sandbox_refusal(live=True))

    def test_an_unreadable_netns_counts_as_the_host(self):
        with mock.patch.dict(os.environ, {"E2E_ROOT": "/tmp/e2e-w9"}), \
                self.links("net:[4026532999]", PermissionError(13, "denied")):
            self.assertIsNotNone(self.install.sandbox_refusal(live=False))

    def test_without_e2e_root_nothing_is_refused(self):
        env = {k: v for k, v in os.environ.items() if k != "E2E_ROOT"}
        with mock.patch.dict(os.environ, env, clear=True), \
                self.links("net:[4026531840]", "net:[4026531840]"):
            self.assertIsNone(self.install.sandbox_refusal(live=False))


if __name__ == "__main__":
    unittest.main()
