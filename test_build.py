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


class _Scope:
    """One lexical scope for the name-resolution check below.

    `parent` is already the correct *lookup* parent: a function's or
    comprehension's scope points past any enclosing class body, mirroring the
    real Python rule that a class's own namespace is invisible to nested
    functions (only to the class body's own statements).
    """

    __slots__ = ("bindings", "parent")

    def __init__(self, parent):
        self.parent = parent
        self.bindings = set()

    def resolves(self, name):
        scope = self
        while scope is not None:
            if name in scope.bindings:
                return True
            scope = scope.parent
        return False


def _nearest_non_class_scope(stack, class_scopes):
    for scope in reversed(stack):
        if scope not in class_scopes:
            return scope
    return stack[0]


class _ScopedNameCollector:
    """Walks a module's AST building one scope per function/class/
    comprehension, and records every `Name` load against the scope it was
    lexically found in. Resolution (`_Scope.resolves`) happens afterwards,
    once every scope's bindings are complete -- a local binding in one
    function must never appear to define a name used, unresolved, in another.
    """

    def __init__(self, module_scope):
        self.class_scopes = set()
        self.stack = [module_scope]
        self.global_names = set()  # names `global`-declared in the innermost function
        self.pending_uses = []  # (name, lineno, scope)

    @property
    def scope(self):
        return self.stack[-1]

    def bind(self, name):
        if name in self.global_names:
            self.stack[0].bindings.add(name)
        else:
            self.scope.bindings.add(name)

    def bind_target(self, target):
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name):
                self.bind(sub.id)

    def use(self, name, lineno):
        self.pending_uses.append((name, lineno, self.scope))

    def visit_body(self, stmts):
        for stmt in stmts:
            self.visit(stmt)

    def visit(self, node):
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is not None:
            method(node)
        else:
            self.generic_visit(node)

    def generic_visit(self, node):
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    # -- name-introducing leaves --------------------------------------------

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.use(node.id, node.lineno)
        else:
            self.bind(node.id)

    def visit_Import(self, node):
        for alias in node.names:
            self.bind(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node):
        for alias in node.names:
            self.bind(alias.asname or alias.name)

    def visit_Global(self, node):
        # A declaration, not a binding: the accompanying assignment (handled
        # via visit_Name -> bind, which checks global_names) is what actually
        # lands the name in module scope.
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node):
        # Also just a declaration. Python requires the name to already exist
        # in an enclosing function scope, which the normal scope chain finds
        # without treating this statement as a binding site itself.
        pass

    def visit_ExceptHandler(self, node):
        if node.name:
            self.bind(node.name)
        self.generic_visit(node)

    # -- scope-introducing nodes ---------------------------------------------

    def _visit_arg_annotations(self, args):
        for a in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                  + ([args.vararg] if args.vararg else [])
                  + ([args.kwarg] if args.kwarg else [])):
            if a.annotation:
                self.visit(a.annotation)

    def _visit_defaults(self, args):
        for default in list(args.defaults) + [d for d in args.kw_defaults if d is not None]:
            self.visit(default)

    def _enter_function(self, args, visit_contents):
        parent = _nearest_non_class_scope(self.stack, self.class_scopes)
        scope = _Scope(parent)
        self.stack.append(scope)
        saved_globals = self.global_names
        self.global_names = set()
        for a in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                  + ([args.vararg] if args.vararg else [])
                  + ([args.kwarg] if args.kwarg else [])):
            self.bind(a.arg)
        visit_contents()
        self.global_names = saved_globals
        self.stack.pop()

    def _visit_function(self, node):
        self.bind(node.name)
        for dec in node.decorator_list:
            self.visit(dec)
        self._visit_defaults(node.args)
        self._visit_arg_annotations(node.args)
        if node.returns:
            self.visit(node.returns)
        self._enter_function(node.args, lambda: self.visit_body(node.body))

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node):
        self._visit_defaults(node.args)
        self._enter_function(node.args, lambda: self.visit(node.body))

    def visit_ClassDef(self, node):
        self.bind(node.name)
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw)
        scope = _Scope(self.scope)
        self.class_scopes.add(scope)
        self.stack.append(scope)
        self.visit_body(node.body)
        self.stack.pop()

    def _enter_comprehension(self, node, visit_contents):
        # The outermost iterable is evaluated in the enclosing scope; the
        # comprehension itself (targets, later clauses, element) is its own
        # scope and, like a function, skips over an enclosing class body.
        self.visit(node.generators[0].iter)
        parent = _nearest_non_class_scope(self.stack, self.class_scopes)
        scope = _Scope(parent)
        self.stack.append(scope)
        for i, gen in enumerate(node.generators):
            self.bind_target(gen.target)
            if i > 0:
                self.visit(gen.iter)
            for cond in gen.ifs:
                self.visit(cond)
        visit_contents()
        self.stack.pop()

    def visit_ListComp(self, node):
        self._enter_comprehension(node, lambda: self.visit(node.elt))

    visit_SetComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node):
        self._enter_comprehension(
            node, lambda: (self.visit(node.key), self.visit(node.value)))


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
        module_scope = _Scope(parent=None)
        module_scope.bindings |= set(dir(builtins)) | {
            "__name__", "__file__", "__doc__", "__builtins__", "__spec__",
        }
        collector = _ScopedNameCollector(module_scope)
        collector.visit_body(tree.body)

        missing = {}
        for name, lineno, scope in collector.pending_uses:
            if name not in missing and not scope.resolves(name):
                missing[name] = lineno
        return missing

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

    def test_a_same_named_local_does_not_mask_an_unresolved_global(self):
        # Regression for a real gap: aggregating every scope's bindings into
        # one file-wide bag let a local `token = ...` in one function make
        # `token` look resolved everywhere, hiding a genuinely undefined
        # global `token` read by a different function.
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(
                "def has_local():\n"
                "    token = 'local value'\n"
                "    return token\n"
                "\n"
                "def reads_undefined_global():\n"
                "    return token\n"
            )
            broken = Path(fh.name)
        try:
            self.assertIn("token", self._undefined_globals(broken))
        finally:
            broken.unlink()
