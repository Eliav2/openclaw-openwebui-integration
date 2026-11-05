"""scripts/prose_only.py: a change passes only when it changes no code."""
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("prose_only", ROOT / "scripts" / "prose_only.py")
prose_only = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prose_only)

BASE = '''"""Module doc."""
import os


def f(x):
    """Doc of f."""
    # why: an ordering constraint
    return os.path.join(x, "a")


class C:
    """Only a docstring."""
'''


class CodeOfTests(unittest.TestCase):
    def test_comments_and_docstrings_do_not_count(self):
        prose = BASE.replace('"""Doc of f."""', '"""Shorter."""').replace("    # why: an ordering constraint\n", "")
        prose = prose.replace('"""Module doc."""\n', "")
        self.assertEqual(prose_only.code_of(BASE), prose_only.code_of(prose))

    def test_a_class_left_with_no_docstring_needs_pass(self):
        self.assertEqual(prose_only.code_of(BASE),
                         prose_only.code_of(BASE.replace('    """Only a docstring."""', "    pass")))

    def test_any_code_counts(self):
        for changed in (BASE.replace('"a"', '"b"'), BASE.replace("return os", "return  os.sep or os"),
                        BASE.replace("def f(x)", "def f(x, y=1)")):
            with self.subTest(changed=changed):
                self.assertNotEqual(prose_only.code_of(BASE), prose_only.code_of(changed))


class CodeChangesTests(unittest.TestCase):
    def _repo(self):
        d = tempfile.mkdtemp()
        run = lambda *a: subprocess.run(["git", *a], cwd=d, check=True, capture_output=True)  # noqa: E731
        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@example.invalid")
        run("config", "user.name", "t")
        Path(d, "m.py").write_text(BASE)
        Path(d, "README.md").write_text("x\n")
        run("add", "-A")
        run("commit", "-qm", "base")
        run("checkout", "-qb", "head")
        return d, run

    def _changes(self, d):
        cwd = os.getcwd()
        os.chdir(d)
        try:
            return prose_only.code_changes("main")
        finally:
            os.chdir(cwd)

    def test_prose_and_markdown_pass(self):
        d, run = self._repo()
        Path(d, "m.py").write_text(BASE.replace('"""Doc of f."""', '"""New doc."""'))
        Path(d, "README.md").write_text("y\n")
        run("commit", "-qam", "prose")
        self.assertEqual(self._changes(d), [])

    def test_code_other_files_and_new_files_fail(self):
        d, run = self._repo()
        Path(d, "m.py").write_text(BASE.replace('"a"', '"b"'))
        Path(d, "n.py").write_text("x = 1\n")
        Path(d, "c.json").write_text("{}\n")
        run("add", "-A")
        run("commit", "-qm", "code")
        self.assertEqual(sorted(p.split(":")[0] for p in self._changes(d)), ["c.json", "m.py", "n.py"])


if __name__ == "__main__":
    unittest.main()
