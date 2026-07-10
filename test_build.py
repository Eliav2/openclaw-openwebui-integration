#!/usr/bin/env python3
"""Guards for the bundle build: no drift, and the two hard OWUI constraints.

Run: python3 -m unittest test_build -v
"""
import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILT = ROOT / "openclaw_pipe.py"


class BuildTests(unittest.TestCase):
    def test_no_drift(self):
        """The committed openclaw_pipe.py must equal a fresh build from src/."""
        r = subprocess.run(
            [sys.executable, str(ROOT / "build.py"), "--check"],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, f"drift detected:\n{r.stdout}\n{r.stderr}")

    def test_frontmatter_is_first(self):
        """OWUI reads the function title/requirements from the leading docstring;
        it must be the first statement (no bundler boilerplate above it)."""
        tree = ast.parse(BUILT.read_text())
        self.assertTrue(tree.body, "empty module")
        first = tree.body[0]
        self.assertIsInstance(first, ast.Expr)
        self.assertIsInstance(first.value, ast.Constant)
        self.assertIn("OpenClaw Gateway Pipe", first.value.value)

    def test_pipe_class_is_top_level(self):
        """OWUI introspects a top-level `Pipe` class; it must not be buried
        inside a loader/module wrapper."""
        tree = ast.parse(BUILT.read_text())
        names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        self.assertIn("Pipe", names)

    def test_built_file_compiles(self):
        compile(BUILT.read_text(), str(BUILT), "exec")


if __name__ == "__main__":
    unittest.main()
