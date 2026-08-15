#!/usr/bin/env python3
"""Guards for the bundle build: no drift, and the two hard OWUI constraints.

Run: python3 -m unittest test_build -v
"""
import ast
import builtins
import re
import subprocess
import sys
import unittest
from pathlib import Path

import build as build_mod

ROOT = Path(__file__).resolve().parent
BUILT = ROOT / "openclaw_pipe.py"
DEV_BUILT = ROOT / "openclaw_pipe.dev.py"
ACTION_BUILT = ROOT / "openclaw_status_action.py"
PKG = ROOT / "src" / "openclaw_pipe_pkg"

# Verbatim copy of open_webui.utils.plugin.extract_frontmatter (0.11.0).
# Reimplemented rather than imported because OWUI is not a test dependency --
# the artifacts have to satisfy OWUI's parser, not ours.
_FRONTMATTER_RE = re.compile(r"^\s*([a-z_]+):\s*(.*)\s*$", re.IGNORECASE)


def extract_frontmatter(content: str) -> dict:
    frontmatter = {}
    lines = content.splitlines()
    if len(lines) < 1 or lines[0].strip() != '"""':
        return {}
    for line in lines[1:]:
        if '"""' in line:
            break
        match = _FRONTMATTER_RE.match(line)
        if match:
            key, value = match.groups()
            frontmatter[key.strip()] = value.strip()
    return frontmatter


# Keys OWUI reads off a distributed function. `requirements` is the load-bearing
# one: OWUI pip-installs it on first load, so omitting it means every user on a
# stock image hits ModuleNotFoundError for websockets/cryptography instead of a
# working function. All of these silently parsed to {} before this was guarded.
REQUIRED_FRONTMATTER_KEYS = (
    "title", "author", "version", "license",
    "requirements", "required_open_webui_version", "description",
)
REQUIRED_PY_MODULES = ("websockets", "cryptography")


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

    def test_owui_parses_frontmatter_metadata(self):
        """OWUI must extract real metadata from the artifact, not an empty dict.

        Prose alone parses to {}: OWUI's regex only picks up `key: value` lines.
        Without `requirements`, OWUI never auto-installs websockets/cryptography
        and a stock deployment fails to load the function at all.
        """
        fm = extract_frontmatter(BUILT.read_text())
        self.assertTrue(fm, "OWUI would parse an empty frontmatter dict")
        for key in REQUIRED_FRONTMATTER_KEYS:
            self.assertIn(key, fm, f"frontmatter is missing '{key}'")
        for mod in REQUIRED_PY_MODULES:
            self.assertIn(mod, fm["requirements"])

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


class StatusActionBuildTests(unittest.TestCase):
    """openclaw_status_action.py -- the companion Action Function. Same
    guards as BuildTests, adapted to the Action's own OWUI constraints
    (top-level `Action` class rather than `Pipe`)."""

    def test_no_drift(self):
        r = subprocess.run(
            [sys.executable, str(ROOT / "build.py"), "--check-action"],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, f"drift detected:\n{r.stdout}\n{r.stderr}")

    def test_frontmatter_is_first(self):
        tree = ast.parse(ACTION_BUILT.read_text())
        self.assertTrue(tree.body, "empty module")
        first = tree.body[0]
        self.assertIsInstance(first, ast.Expr)
        self.assertIsInstance(first.value, ast.Constant)
        self.assertIn("OpenClaw Status Action", first.value.value)

    def test_owui_parses_frontmatter_metadata(self):
        """Same contract as the Pipe -- see BuildTests for why this matters."""
        fm = extract_frontmatter(ACTION_BUILT.read_text())
        self.assertTrue(fm, "OWUI would parse an empty frontmatter dict")
        for key in REQUIRED_FRONTMATTER_KEYS:
            self.assertIn(key, fm, f"frontmatter is missing '{key}'")
        for mod in REQUIRED_PY_MODULES:
            self.assertIn(mod, fm["requirements"])

    def test_action_class_is_top_level(self):
        tree = ast.parse(ACTION_BUILT.read_text())
        names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        self.assertIn("Action", names)

    def test_built_file_compiles(self):
        compile(ACTION_BUILT.read_text(), str(ACTION_BUILT), "exec")

    def test_no_dev_only_markers_leak(self):
        built = ACTION_BUILT.read_text()
        self.assertNotIn(build_mod.DEV_ONLY_START, built)
        self.assertNotIn(build_mod.DEV_ONLY_END, built)
        self.assertNotIn("DEV-ONLY", built)

    def test_proactive_delivery_disabled_for_fallback_connection(self):
        """The Action's own fallback connection must never run the Pipe's
        proactive-message-delivery logic (see action.py's module-level
        override and its docstring for why) -- the override must be the
        LAST assignment of this name in the built file, since gateway.py's
        own `= True` is defined earlier and Python module execution is
        top-to-bottom."""
        built = ACTION_BUILT.read_text()
        true_idx = built.rindex("PROACTIVE_DELIVERY_ENABLED = True")
        false_idx = built.rindex("PROACTIVE_DELIVERY_ENABLED = False")
        self.assertGreater(
            false_idx, true_idx,
            "override must appear after gateway.py's default to win",
        )

    def test_does_not_reuse_pipe_stale_conn_write_path(self):
        """The Action fragment must never call _remember_gateway_connection
        or assign the module-level `_gateway_connection` singleton itself
        -- both are the Pipe's own redeploy-reaping bookkeeping, and the
        Action must only ever READ _STALE_CONN_ATTR, never write it (see
        action.py's module docstring for the collision this avoids)."""
        action_src = (PKG / "action.py").read_text()
        self.assertNotIn("_remember_gateway_connection(", action_src)
        self.assertNotIn("_get_gateway_connection(", action_src)

    def test_shares_gateway_fragment_verbatim_with_pipe(self):
        """The Action and Pipe artifacts must bundle byte-identical
        gateway.py content -- this is what "reuse, don't duplicate" means
        here: one fragment file, two build targets."""
        gateway_src = (PKG / "gateway.py").read_text()
        # Both artifacts strip the fragment banner before inlining; compare
        # the post-strip body so this test is agnostic to how build.py
        # formats the surrounding per-fragment header.
        body = build_mod._strip_fragment_banner(gateway_src)
        self.assertIn(body.rstrip("\n"), BUILT.read_text())
        self.assertIn(body.rstrip("\n"), ACTION_BUILT.read_text())


if __name__ == "__main__":
    unittest.main()


class ArtifactNameResolutionTests(unittest.TestCase):
    """Every artifact must define every module-level name its fragments use.

    An artifact is a concatenation of a chosen subset of fragments, so taking a
    fragment without taking what that fragment references produces a file that
    imports fine and raises NameError only when the affected line finally runs.
    That is the worst possible time to find out, and neither the build nor a
    smoke import will catch it: this test does the resolving statically.

    Caught in review: adding a ladder-cache helper to `gateway` made the status
    Action reference LEVEL_RANKS, which lives in `thinking`, which the Action
    did not bundle.
    """

    ARTIFACTS = [build_mod.PIPE, build_mod.ACTION, build_mod.FILTER]

    def _undefined_globals(self, path):
        tree = ast.parse(path.read_text(), filename=str(path))
        defined = set(dir(builtins)) | {
            "__name__", "__file__", "__doc__", "__builtins__", "__spec__",
        }
        used = {}

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    defined.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    defined.add(a.asname or a.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    for sub in ast.walk(t):
                        if isinstance(sub, ast.Name):
                            defined.add(sub.id)
            elif isinstance(node, (ast.Name,)) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, (ast.arg,)):
                defined.add(node.arg)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                defined.update(node.names)
            elif isinstance(node, ast.comprehension):
                for sub in ast.walk(node.target):
                    if isinstance(sub, ast.Name):
                        defined.add(sub.id)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                used.setdefault(node.id, node.lineno)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        for sub in ast.walk(item.optional_vars):
                            if isinstance(sub, ast.Name):
                                defined.add(sub.id)

        return {n: ln for n, ln in used.items() if n not in defined}

    def test_every_artifact_resolves_its_own_names(self):
        for artifact in self.ARTIFACTS:
            with self.subTest(artifact=artifact.out.name):
                missing = self._undefined_globals(artifact.out)
                self.assertEqual(
                    missing, {},
                    f"{artifact.out.name} uses names no bundled fragment defines "
                    f"(module_order={artifact.module_order}): {missing}")

    def test_the_check_can_actually_fail(self):
        # A test that cannot fail is indistinguishable from one that passes for
        # the right reason, so prove the detector on a file that IS broken.
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write("def f():\n    return SOME_MISSING_CONSTANT\n")
            broken = Path(fh.name)
        try:
            self.assertIn("SOME_MISSING_CONSTANT", self._undefined_globals(broken))
        finally:
            broken.unlink()
