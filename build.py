#!/usr/bin/env python3
"""Bundle src/openclaw_pipe_pkg/ fragments into the single-file openclaw_pipe.py
that Open WebUI deploys.

Why a hand-written concat and not stickytape/pinliner: OWUI stores a function as
one flat .py file and (a) reads the frontmatter docstring from the FIRST bytes and
(b) introspects a top-level `Pipe` class. Loader-based bundlers prepend boilerplate
(breaking (a)) and bury classes inside a runtime loader (breaking (b)). We only
bundle our OWN first-party fragments -- third-party deps (websockets, cryptography,
pydantic) are provided by the OWUI runtime and stay as normal imports -- so the
"bundle" is a deterministic ordered concatenation.

Usage:
  python3 build.py --all      # build ALL distributed artifacts. Use this after
                              # touching any fragment -- a fragment can feed both.
  python3 build.py --check-all  # drift guard for ALL artifacts. This is what CI
                              # runs; exits 1 if any is stale.

  python3 build.py            # build -> openclaw_pipe.py only
  python3 build.py --check    # drift guard for openclaw_pipe.py only
  python3 build.py --filter        # build -> openclaw_thinking_filter.py only, the
                              # per-chat thinking control (a Filter, not a Pipe)
  python3 build.py --check-filter  # drift guard for openclaw_thinking_filter.py only
  python3 build.py --action        # build -> openclaw_status_action.py only, the
                              # companion Action Function (see ARTIFACTS below)
  python3 build.py --check-action  # drift guard for openclaw_status_action.py only

Each single-artifact invocation builds exactly one file. Prefer --all/--check-all
so an edit to a shared fragment cannot leave the other artifact silently stale.
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent
PKG = ROOT / "src" / "openclaw_pipe_pkg"


class Artifact(NamedTuple):
    """One buildable output: a frontmatter file + an ordered subset of
    fragments from PKG, concatenated into a single flat .py file. Multiple
    artifacts can (and do) share the same fragment files -- see ACTION's
    reuse of _prelude/identity/state/gateway, which is the whole point of
    keeping those as separate fragments rather than inlining them into
    pipe.py.

    NamedTuple, not @dataclass: install.py's _rebuild_pipe_file and
    rebuild_artifact load this module dynamically via
    importlib.util.module_from_spec + exec_module, which does NOT register
    the module in sys.modules the way a normal `import` does. @dataclass's
    class-processing internally does sys.modules.get(cls.__module__) to
    resolve forward-referenced annotations (this file uses `from __future__
    import annotations`), which raises AttributeError on None under that
    loading pattern -- confirmed live, this broke install.py's existing
    _rebuild_pipe_file the moment this class was added as a dataclass, even
    though install.py itself was never touched. NamedTuple has no such
    dependency and sidesteps the whole issue."""
    frontmatter: Path
    out: Path
    module_order: tuple[str, ...]


# Fixed dependency order. Module-load statements (constants, asyncio.Lock(),
# logging.basicConfig) have no cross-module load-time references, so this order
# only needs to satisfy human readability, not import semantics.
PIPE = Artifact(
    frontmatter=ROOT / "src" / "frontmatter.txt",
    out=ROOT / "openclaw_pipe.py",
    module_order=("_prelude", "identity", "state", "turns", "thinking", "media", "emit",
                  "composer", "composer_watch", "askuser", "gateway", "writer", "models",
                  "pipe"),
)

# The status Action reuses the Pipe's connection/identity/formatting
# fragments verbatim (same files, not copies) and adds only its own
# "action" fragment. "emit" is included so the modal's numbers come from
# the exact same _build_usage_status_lines/_fmt_tokens logic the status
# line itself uses -- one source of truth, no risk of the two drifting
# apart. It writes no turns, so it leaves out "writer" (ELI-149).
ACTION = Artifact(
    frontmatter=ROOT / "src" / "frontmatter-action.txt",
    out=ROOT / "openclaw_status_action.py",
    module_order=("_prelude", "identity", "state", "turns", "thinking", "gateway",
                  "emit", "composer", "action"),
)
# "composer" is in both: the Action and the Pipe each deliver the composer
# Status button snippet (see composer.py), so it has one source. Its watcher
# ("composer_watch") runs in the Pipe only.
# "thinking" is here because ACTION takes "gateway", and gateway's ladder cache
# helper reads LEVEL_RANKS / LADDER_CACHE_NAME from it. Python resolves those at
# call time, not import time, so omitting the fragment would not fail the build
# or the import: it would fail the first time the code ran. Any artifact that
# takes a fragment must take what that fragment names.

# The Thinking filter shares exactly ONE fragment with the Pipe: "thinking",
# which holds the level ranks and the clamping rule. The filter offers the
# levels and the pipe enforces them, so those two have to be one implementation.
# It deliberately does NOT take "_prelude", which pulls in websockets and
# cryptography: a filter runs on every message, so an import it does not need is
# an outage it does not need either. "thinking" is therefore stdlib-only and
# class-free, and "thinking_filter" carries the pydantic surface the Pipe must
# not expose (a stray top-level Filter class in the Pipe artifact is one more
# thing for Open WebUI's introspection to trip over).
FILTER = Artifact(
    frontmatter=ROOT / "src" / "frontmatter-filter.txt",
    out=ROOT / "openclaw_thinking_filter.py",
    module_order=("thinking", "thinking_filter"),
)

BANNER = (
    "# ==========================================================================\n"
    "# AUTO-GENERATED by build.py from src/openclaw_pipe_pkg/ -- DO NOT EDIT HERE.\n"
    "# Edit the fragments under src/openclaw_pipe_pkg/ and re-run: python3 build.py\n"
    "# ==========================================================================\n"
)


def _strip_fragment_banner(text: str) -> str:
    """Remove the leading 'BUILD FRAGMENT' banner block a fragment carries."""
    out_lines = []
    lines = text.splitlines(keepends=True)
    i = 0
    # skip a contiguous leading comment block that is the fragment banner
    while i < len(lines) and lines[i].startswith("# "):
        i += 1
    # skip one blank separator after the banner, if present
    if i < len(lines) and lines[i].strip() == "":
        i += 1
    out_lines = lines[i:]
    return "".join(out_lines)


def build(artifact: Artifact = PIPE) -> str:
    """Assemble one artifact's frontmatter + fragments into a flat .py file."""
    frontmatter = artifact.frontmatter.read_text()
    if not frontmatter.endswith("\n"):
        frontmatter += "\n"
    parts = [frontmatter, "\n", BANNER]
    for mod in artifact.module_order:
        body = _strip_fragment_banner((PKG / f"{mod}.py").read_text())
        parts.append("\n\n")
        parts.append(f"# --- {mod} " + "-" * (70 - len(mod)) + "\n")
        parts.append(body.rstrip("\n") + "\n")
    result = "".join(parts)
    if not result.endswith("\n"):
        result += "\n"
    return result


def _check(out_path: Path, built: str, rebuild_flag: str) -> bool:
    """Compare a freshly built artifact against the committed one.

    Returns True when in sync. `rebuild_flag` is the flag that REBUILDS this
    artifact, not the flag that checks it -- telling someone to re-run the
    check they just failed is useless advice.
    """
    current = out_path.read_text() if out_path.exists() else ""
    if built != current:
        cmd = f"python3 build.py {rebuild_flag}".rstrip()
        print(f"DRIFT: {out_path.name} is out of date vs src/. Rebuild with: {cmd}",
              file=sys.stderr)
        return False
    print(f"{out_path.name} is in sync with src/ (no drift)")
    return True


CONFIG_DOC = ROOT / "docs" / "configuration.md"
# (heading, fragment, file a user installs): the three functions' Valves, in
# the order a user meets them.
_VALVE_SOURCES = (
    ("Pipe", "pipe", "openclaw_pipe.py"),
    ("Status Action", "action", "openclaw_status_action.py"),
    ("Thinking filter", "thinking_filter", "openclaw_thinking_filter.py"),
)


def _valves(fragment: str) -> list:
    """[(name, type, default, description)] of the `Valves` class in a
    fragment, read from its source: the docs cannot list a valve the code
    doesn't have, or miss one it has."""
    import ast
    tree = ast.parse((PKG / f"{fragment}.py").read_text())
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Valves")
    out = []
    for node in cls.body:
        if not (isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Call)):
            continue
        kw = {k.arg: k.value for k in node.value.keywords}
        default = ast.literal_eval(kw["default"]) if "default" in kw else None
        out.append((node.target.id, ast.unparse(node.annotation), default,
                    ast.literal_eval(kw["description"]) if "description" in kw else ""))
    return out


def _user_valves(fragment: str) -> list:
    """[(name, type, default, description)] of a `create_model("UserValves",
    name=(type, Field(...)))` call in a fragment. Names in the default and the
    description resolve against the package's module-level string constants."""
    import ast
    consts = {}
    for path in PKG.glob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                consts[node.targets[0].id] = node.value.value

    class _Inline(ast.NodeTransformer):
        def visit_Name(self, node):
            return ast.copy_location(ast.Constant(consts[node.id]), node) if node.id in consts else node

        def visit_BinOp(self, node):  # "a" + NAME + "b": fold, literal_eval won't
            node = self.generic_visit(node)
            if (isinstance(node.op, ast.Add) and isinstance(node.left, ast.Constant)
                    and isinstance(node.right, ast.Constant)
                    and isinstance(node.left.value, str) and isinstance(node.right.value, str)):
                return ast.copy_location(ast.Constant(node.left.value + node.right.value), node)
            return node

    def value(expr):
        return ast.literal_eval(ast.fix_missing_locations(_Inline().visit(expr)))

    out = []
    for call in ast.walk(ast.parse((PKG / f"{fragment}.py").read_text())):
        if not (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "create_model"
                and call.args and isinstance(call.args[0], ast.Constant)
                and call.args[0].value == "UserValves"):
            continue
        for k in call.keywords:
            if not isinstance(k.value, ast.Tuple):
                continue
            typ, field = k.value.elts
            kw = {x.arg: x.value for x in field.keywords}
            out.append((k.arg, ast.unparse(typ), value(kw["default"]) if "default" in kw else None,
                        value(kw["description"]) if "description" in kw else ""))
    return out


def _valve_lines(valves: list, per_user: bool = False) -> list:
    lines = []
    for name, typ, default, desc in valves:
        shown = "empty" if default == "" else f"`{default}`"
        where = (" Per user: each user sets it in **Chat Controls > Valves**, from only the "
                 "levels your models support ([Thinking control](./install/thinking-filter.md))."
                 if per_user else "")
        lines += ["", f"### {name}", "", f"Type: `{typ}`. Default: {shown}.{where}", "",
                  " ".join(desc.split())]
    return lines


def render_configuration() -> str:
    """docs/configuration.md, generated from the three Valves classes."""
    lines = [
        "Every setting (valve) of the three functions, with its type, default and what it does. "
        "Generated from the code by `python3 build.py --all`; do not edit by hand.",
        "",
        "Applies to: Pipe, Action, Filter",
        "",
        "Set them in Open WebUI: **Admin Panel > Functions >** the function **> Valves** (the gear). "
        "The installer rewrites the connection valves on every run "
        "([which ones](./install/installer.md#valves-the-installer-sets)) and keeps the rest. "
        "Changes apply on the next message, with no restart.",
        "",
        "![The Pipe's valves in Open WebUI's admin panel, from Owner Users down]"
        "(./img/valves-light.png)",
    ]
    for heading, fragment, filename in _VALVE_SOURCES:
        lines += ["", f"## {heading}", "", f"File: `{filename}`."]
        if fragment == "action":
            lines += ["", "The Action reuses the Pipe's Gateway connection when the Pipe has one. "
                      "These valves are its own connection's settings, for when it has to open one: "
                      "copy them from the Pipe's valves."]
        lines += _valve_lines(_valves(fragment))
        for line in _valve_lines(_user_valves(fragment), per_user=True):
            lines.append(line)
    return "\n".join(lines) + "\n"


def main():
    do_all = "--all" in sys.argv or "--check-all" in sys.argv
    action = "--action" in sys.argv or "--check-action" in sys.argv
    filt = "--filter" in sys.argv or "--check-filter" in sys.argv
    checking = any(a in sys.argv
                   for a in ("--check", "--check-action", "--check-filter", "--check-all"))

    if action and filt:
        print("Select only one of --action or --filter", file=sys.stderr)
        sys.exit(2)

    if do_all:
        if action or filt:
            print("--all/--check-all already covers every distributed artifact; "
                  "do not combine it with --action or --filter", file=sys.stderr)
            sys.exit(2)
        targets = [(PIPE, PIPE.out, ""), (ACTION, ACTION.out, "--action"),
                   (FILTER, FILTER.out, "--filter")]
    elif action:
        targets = [(ACTION, ACTION.out, "--action")]
    elif filt:
        targets = [(FILTER, FILTER.out, "--filter")]
    else:
        targets = [(PIPE, PIPE.out, "")]

    ok = True
    for artifact, out_path, rebuild_flag in targets:
        built = build(artifact=artifact)
        if checking:
            ok = _check(out_path, built, rebuild_flag) and ok
        else:
            out_path.write_text(built)
            print(f"built {out_path}  ({len(built.splitlines())} lines)")

    if do_all:
        # The docs page generated from the same sources.
        doc = render_configuration()
        if checking:
            ok = _check(CONFIG_DOC, doc, "--all") and ok
        else:
            CONFIG_DOC.write_text(doc)
            print(f"built {CONFIG_DOC}  ({len(doc.splitlines())} lines)")

    if checking and not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
