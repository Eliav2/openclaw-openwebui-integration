"""scripts/ci_scope.py: which pull requests run the E2E workflow, and that ci.yml gates it on that."""
import importlib.util
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ci_scope", ROOT / "scripts" / "ci_scope.py")
ci_scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_scope)

CODE = '"""Doc."""\n\n\ndef f():\n    """Doc of f."""\n    # why\n    return 1\n'


class InertPathTests(unittest.TestCase):
    def test_docs_markdown_tests_and_templates_are_inert(self):
        for path in ("docs/versions.md", "docs/img/a.png", "README.md", "src/notes.md", "tests/test_pipe_unit.py",
                     "LICENSE", "llms.txt", ".github/ISSUE_TEMPLATE/bug_report.yml"):
            with self.subTest(path):
                self.assertTrue(ci_scope.inert_path(path))

    def test_runtime_paths_are_not(self):
        for path in ("src/openclaw_pipe_pkg/gateway.py", "openclaw_pipe.py", "openclaw_status_action.py",
                     "install.py", "install_action.py", "e2e/versions.env", "e2e/test_e2e.py",
                     "gateway-plugins/x/index.js", "build.py", "requirements-dev.txt", "scripts/owui_css.py",
                     ".github/workflows/ci.yml", ".github/workflows/e2e.yml", ".github/actions/stack/action.yml",
                     "tests/fixtures/test_fixture_owui_css_classes.txt"):
            with self.subTest(path):
                self.assertFalse(ci_scope.inert_path(path))


class RuntimeFilesTests(unittest.TestCase):
    def _repo(self):
        d = tempfile.mkdtemp()
        run = lambda *a: subprocess.run(["git", *a], cwd=d, check=True, capture_output=True)  # noqa: E731
        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@example.invalid")
        run("config", "user.name", "t")
        for name, text in (("src/m.py", CODE), ("docs/a.md", "x\n"), ("tests/t.py", CODE), ("e2e/versions.env", "A=1\n")):
            Path(d, name).parent.mkdir(parents=True, exist_ok=True)
            Path(d, name).write_text(text)
        run("add", "-A")
        run("commit", "-qm", "base")
        run("checkout", "-qb", "head")
        return d, run

    def _files(self, d):
        cwd = os.getcwd()
        os.chdir(d)
        try:
            return ci_scope.runtime_files("main")
        finally:
            os.chdir(cwd)

    def test_docs_tests_and_prose_edits_of_code_skip(self):
        d, run = self._repo()
        Path(d, "docs/a.md").write_text("y\n")
        Path(d, "docs/new.md").write_text("new\n")
        Path(d, "tests/t.py").write_text(CODE.replace("return 1", "return 2"))   # tests/ is the Unit job's
        Path(d, "src/m.py").write_text(CODE.replace("# why", "# because").replace("Doc of f.", "Other."))
        run("add", "-A")
        run("commit", "-qm", "inert")
        self.assertEqual(self._files(d), [])

    def test_a_code_change_in_src_runs(self):
        d, run = self._repo()
        Path(d, "src/m.py").write_text(CODE.replace("return 1", "return 2"))
        run("commit", "-qam", "code")
        self.assertEqual(self._files(d), ["src/m.py"])

    def test_a_new_file_a_deleted_file_and_a_versions_bump_run(self):
        d, run = self._repo()
        Path(d, "src/n.py").write_text(CODE)
        Path(d, "e2e/versions.env").write_text("A=2\n")
        run("rm", "-q", "src/m.py")
        run("add", "-A")
        run("commit", "-qm", "mixed")
        self.assertEqual(sorted(self._files(d)), ["e2e/versions.env", "src/m.py", "src/n.py"])

    def test_one_runtime_file_among_docs_is_enough(self):
        d, run = self._repo()
        Path(d, "docs/a.md").write_text("y\n")
        Path(d, "e2e/versions.env").write_text("A=2\n")
        run("commit", "-qam", "both")
        self.assertEqual(self._files(d), ["e2e/versions.env"])


class WorkflowWiringTests(unittest.TestCase):
    """Every job that calls the E2E workflow waits for `scope` and runs only when it says runtime."""

    def setUp(self):
        self.text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.jobs = re.findall(r"^  (\S+):\n((?:    .*\n|\n)+)", self.text, re.M)

    def test_every_e2e_job_is_gated_on_scope(self):
        e2e = [(name, body) for name, body in self.jobs if "uses: ./.github/workflows/e2e.yml" in body]
        self.assertTrue(e2e)
        for name, body in e2e:
            with self.subTest(name):
                self.assertRegex(body, r"needs: \[[^\]]*\bscope\b")
                self.assertIn("if: needs.scope.outputs.runtime == 'true'", body)

    def test_the_scope_job_runs_this_script(self):
        scope = dict(self.jobs)["scope"]
        self.assertIn("scripts/ci_scope.py", scope)
        self.assertIn("fetch-depth: 0", scope)

    def test_only_the_oldest_lane_is_non_blocking(self):
        lanes = {name: re.search(r"^\s*blocking: (.+)$", body, re.M) for name, body in self.jobs
                 if "uses: ./.github/workflows/e2e.yml" in body}
        self.assertEqual({n for n, m in lanes.items() if m and "'false'" in m.group(1)}, {"e2e-oldest"})


if __name__ == "__main__":
    unittest.main()
