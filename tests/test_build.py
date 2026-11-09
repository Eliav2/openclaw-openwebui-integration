#!/usr/bin/env python3
"""Guards for the bundle build: no drift, and the hard OWUI constraints."""
import ast
import builtins
import re
import subprocess
import sys
import unittest
from pathlib import Path

import build as build_mod

ROOT = Path(__file__).resolve().parents[1]
BUILT = ROOT / "openclaw_pipe.py"
ACTION_BUILT = ROOT / "openclaw_status_action.py"
PKG = ROOT / "src" / "openclaw_pipe_pkg"

# Copy of open_webui.utils.plugin.extract_frontmatter (0.11.0); OWUI is not a test dependency.
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


# Keys OWUI reads off a function. Without `requirements` OWUI does not
# pip-install websockets/cryptography and the function fails to load.
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
        """OWUI reads title and requirements from the leading docstring, which
        must be the first statement."""
        tree = ast.parse(BUILT.read_text())
        self.assertTrue(tree.body, "empty module")
        first = tree.body[0]
        self.assertIsInstance(first, ast.Expr)
        self.assertIsInstance(first.value, ast.Constant)
        self.assertIn("OpenClaw Gateway Pipe", first.value.value)

    def test_owui_parses_frontmatter_metadata(self):
        """OWUI must extract real metadata (`key: value` lines), not {}."""
        fm = extract_frontmatter(BUILT.read_text())
        self.assertTrue(fm, "OWUI would parse an empty frontmatter dict")
        for key in REQUIRED_FRONTMATTER_KEYS:
            self.assertIn(key, fm, f"frontmatter is missing '{key}'")
        for mod in REQUIRED_PY_MODULES:
            self.assertIn(mod, fm["requirements"])

    def test_pipe_class_is_top_level(self):
        """OWUI introspects a top-level `Pipe` class."""
        tree = ast.parse(BUILT.read_text())
        names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        self.assertIn("Pipe", names)

    def test_built_file_compiles(self):
        compile(BUILT.read_text(), str(BUILT), "exec")


class StatusActionBuildTests(unittest.TestCase):
    """The companion Action Function: BuildTests' guards with a top-level `Action` class."""

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
        """Same contract as the Pipe."""
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

    def test_carries_no_turn_writer(self):
        """ELI-149: the Action ships nothing of writer.py; a leftover reference
        would be a NameError at run time."""
        writer = ast.parse((PKG / "writer.py").read_text())
        names = set()
        for node in writer.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names |= {t.id for t in targets if isinstance(t, ast.Name)}
        self.assertIn("_TurnWriter", names)
        # gateway.py's hook, which writer.py sets.
        names.discard("_TURNS")
        action = ast.parse(ACTION_BUILT.read_text())
        defined = {n.name for n in action.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        used = {n.id for n in ast.walk(action) if isinstance(n, ast.Name)}
        self.assertEqual(sorted(names & (defined | used)), [])
        hooks = [n for n in action.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "_TURNS" for t in n.targets)]
        self.assertEqual([ast.unparse(n.value) for n in hooks], ["None"])

    def test_does_not_reuse_pipe_stale_conn_write_path(self):
        """The Action must not touch the Pipe's redeploy bookkeeping:
        _remember_gateway_connection, `_gateway_connection`, _STALE_CONN_ATTR."""
        action_src = (PKG / "action.py").read_text()
        self.assertNotIn("_remember_gateway_connection(", action_src)
        self.assertNotIn("_get_gateway_connection(", action_src)

    def test_shares_gateway_fragment_verbatim_with_pipe(self):
        """The Action and the Pipe bundle the same gateway.py content."""
        gateway_src = (PKG / "gateway.py").read_text()
        # Compare after the fragment banner is stripped.
        body = build_mod._strip_fragment_banner(gateway_src)
        self.assertIn(body.rstrip("\n"), BUILT.read_text())
        self.assertIn(body.rstrip("\n"), ACTION_BUILT.read_text())


if __name__ == "__main__":
    unittest.main()


class _Scope:
    """One lexical scope. `parent` is the lookup parent, which skips an
    enclosing class body as Python does."""

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
    """Builds one scope per function, class and comprehension and records each
    `Name` load. Resolution runs after all bindings are known."""

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
        # A declaration; the assignment binds the name in module scope.
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node):
        # A declaration; the enclosing scope already binds the name.
        pass

    def visit_ExceptHandler(self, node):
        if node.name:
            self.bind(node.name)
        self.generic_visit(node)


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
        # The outermost iterable runs in the enclosing scope; the rest is the
        # comprehension's own scope.
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
    """Every artifact must define every module-level name its fragments use, or
    it raises NameError only when that line runs (e.g. LEVEL_RANKS in the Action)."""

    ARTIFACTS = [build_mod.PIPE, build_mod.ACTION]

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
        # Prove the detector on a broken file.
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write("def f():\n    return SOME_MISSING_CONSTANT\n")
            broken = Path(fh.name)
        try:
            self.assertIn("SOME_MISSING_CONSTANT", self._undefined_globals(broken))
        finally:
            broken.unlink()

    def test_a_same_named_local_does_not_mask_an_unresolved_global(self):
        # A local binding in one function must not resolve a global read in another.
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
