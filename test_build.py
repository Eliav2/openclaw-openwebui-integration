#!/usr/bin/env python3
"""Guards for the bundle build: no drift, and the two hard OWUI constraints.

Run: python3 -m unittest test_build -v
"""
import ast
import subprocess
import sys
import unittest
from pathlib import Path

import build as build_mod

ROOT = Path(__file__).resolve().parent
BUILT = ROOT / "openclaw_pipe.py"
DEV_BUILT = ROOT / "openclaw_pipe.dev.py"
PKG = ROOT / "src" / "openclaw_pipe_pkg"


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

    def test_no_dev_only_markers_in_source_fragments_leak(self):
        """DEV-ONLY markers must never survive into the shipped artifact."""
        built = BUILT.read_text()
        self.assertNotIn(build_mod.DEV_ONLY_START, built)
        self.assertNotIn(build_mod.DEV_ONLY_END, built)
        self.assertNotIn("DEV-ONLY", built)

    def test_dev_only_block_stripped(self):
        sample = (
            "before\n"
            "# DEV-ONLY-START\n"
            "secret = 1\n"
            "# DEV-ONLY-END\n"
            "after\n"
        )
        self.assertEqual(
            build_mod._strip_dev_only(sample), "before\nafter\n"
        )

    def test_dev_only_unterminated_raises(self):
        with self.assertRaises(ValueError):
            build_mod._strip_dev_only("# DEV-ONLY-START\nsecret = 1\n")

    def test_dev_only_stray_end_raises(self):
        with self.assertRaises(ValueError):
            build_mod._strip_dev_only("# DEV-ONLY-END\n")

    def test_dev_only_nested_raises(self):
        with self.assertRaises(ValueError):
            build_mod._strip_dev_only(
                "# DEV-ONLY-START\n# DEV-ONLY-START\n# DEV-ONLY-END\n# DEV-ONLY-END\n"
            )


class DevBundleTests(unittest.TestCase):
    """openclaw_pipe.dev.py is gitignored (internal-only, ELI-24) -- these
    build it in-memory rather than diffing a committed copy."""

    def test_dev_bundle_keeps_devcoord_code(self):
        built = build_mod.build(strip_dev_only=False)
        self.assertIn("_devcoord_turn_begin", built)
        self.assertIn("_devcoord_wait_if_deploy_pending", built)

    def test_dev_bundle_compiles(self):
        built = build_mod.build(strip_dev_only=False)
        compile(built, "openclaw_pipe.dev.py", "exec")

    def test_dev_bundle_still_satisfies_owui_constraints(self):
        built = build_mod.build(strip_dev_only=False)
        tree = ast.parse(built)
        first = tree.body[0]
        self.assertIsInstance(first, ast.Expr)
        self.assertIn("OpenClaw Gateway Pipe", first.value.value)
        names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        self.assertIn("Pipe", names)

    def test_cli_dev_flag_writes_dev_bundle(self):
        r = subprocess.run(
            [sys.executable, str(ROOT / "build.py"), "--dev"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
        self.assertTrue(DEV_BUILT.exists())
        self.assertIn("_devcoord_turn_begin", DEV_BUILT.read_text())

    def test_cli_check_dev_passes_after_build(self):
        subprocess.run([sys.executable, str(ROOT / "build.py"), "--dev"],
                        capture_output=True, text=True, cwd=str(ROOT), check=True)
        r = subprocess.run(
            [sys.executable, str(ROOT / "build.py"), "--check-dev"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(r.returncode, 0, f"drift detected:\n{r.stdout}\n{r.stderr}")

    def test_regular_build_unaffected_by_dev_flag_presence(self):
        """--check (no -dev) must still validate against the stripped bundle."""
        r = subprocess.run(
            [sys.executable, str(ROOT / "build.py"), "--check"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")


if __name__ == "__main__":
    unittest.main()
