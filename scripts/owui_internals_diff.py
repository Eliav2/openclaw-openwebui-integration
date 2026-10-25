#!/usr/bin/env python3
"""Compare every Open WebUI internal the Pipe imports between two Open WebUI tags (upgrade checklist step 0).

    python3 scripts/owui_internals_diff.py --repo /path/to/open-webui-clone v0.12.3 v0.12.4 [--bodies]

The Pipe reaches into Open WebUI through lazy imports (`from open_webui.models.chats import Chats`) and calls
methods on what it imports (`Chats.save_response_stream(...)`). A signature change there fails only at run time,
inside a try, so the unit tests (which fake those modules with the old signature) stay green. This script:

1. reads `src/openclaw_pipe_pkg/*.py` and collects each `open_webui` import and each attribute used on it,
2. parses the two tags' source with `ast` (read with `git show`, nothing is executed),
3. prints, per used symbol, whether it is SAME, CHANGED (old and new signature), or MISSING in the new tag,
4. with --bodies, also prints a unified diff of the source of every used function and method whose body changed.

Exit code 1 when anything used is CHANGED or MISSING.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "src" / "openclaw_pipe_pkg"
# Table classes: the Pipe also reads their columns (Chat.chat, ChatMessage.output ...), so list the columns too.
TABLES = {"Chat", "ChatMessage"}


def used_symbols(pkg: Path) -> tuple[dict[str, set[str]], dict[str, set[tuple[str, str]]]]:
    """({module: {name}}, {module: {(name, attr)}}) over every pipe source file."""
    names: dict[str, set[str]] = {}
    attrs: dict[str, set[tuple[str, str]]] = {}
    for f in sorted(pkg.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        local: dict[str, tuple[str, str | None]] = {}      # local name -> (module, symbol or None for a module alias)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("open_webui"):
                for a in node.names:
                    names.setdefault(node.module, set()).add(a.name)
                    local[a.asname or a.name] = (node.module, a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("open_webui"):
                        names.setdefault(a.name, set())
                        local[a.asname or a.name.split(".")[0]] = (a.name, None)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in local:
                mod, sym = local[node.value.id]
                if sym is None:                             # alias of a module: `_owui_socket_main.get_event_emitter`
                    names.setdefault(mod, set()).add(node.attr)
                else:
                    attrs.setdefault(mod, set()).add((sym, node.attr))
    return names, attrs


def read(repo: str, ref: str, rel: str) -> str | None:
    p = subprocess.run(["git", "-C", repo, "show", f"{ref}:{rel}"], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def module_source(repo: str, ref: str, module: str) -> tuple[str | None, str]:
    base = "backend/" + module.replace(".", "/")
    for rel in (base + ".py", base + "/__init__.py"):
        src = read(repo, ref, rel)
        if src is not None:
            return src, rel
    return None, base


def sig(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        decos = "".join(f"@{ast.unparse(d)} " for d in node.decorator_list)
        return f"{decos}{kind} {node.name}({ast.unparse(node.args)}){ret}"
    if isinstance(node, ast.ClassDef):
        return f"class {node.name}({', '.join(ast.unparse(b) for b in node.bases)})"
    return ast.unparse(node)


def index(src: str) -> dict[str, tuple[str, str]]:
    """{'name' | 'Class' | 'Class.member': (signature, source segment)} for the module's top level and class bodies."""
    tree = ast.parse(src)
    lines = src.splitlines()
    out: dict[str, tuple[str, str]] = {}

    def seg(n: ast.AST) -> str:
        return "\n".join(lines[n.lineno - 1:n.end_lineno])

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = (sig(node), seg(node))
        elif isinstance(node, ast.ClassDef):
            out[node.name] = (sig(node), "")
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{m.name}"] = (sig(m), seg(m))
                elif isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name):
                    out[f"{node.name}.{m.target.id}"] = (ast.unparse(m), seg(m))
                elif isinstance(m, ast.Assign):
                    for t in m.targets:
                        if isinstance(t, ast.Name):
                            out[f"{node.name}.{t.id}"] = (ast.unparse(m), seg(m))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            tgt = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in tgt:
                if isinstance(t, ast.Name):
                    out[t.id] = (ast.unparse(node)[:200], seg(node))
    return out


def instance_classes(src: str) -> dict[str, str]:
    """{'Chats': 'ChatTable'} for module-level `Chats = ChatTable()`: the Pipe calls methods on the instance."""
    tree = ast.parse(src)
    classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    out = {}
    for n in tree.body:
        v = n.value if isinstance(n, (ast.Assign, ast.AnnAssign)) else None
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in classes:
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                if isinstance(t, ast.Name):
                    out[t.id] = v.func.id
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="a git clone of open-webui/open-webui with both tags fetched")
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--bodies", action="store_true", help="also diff the source of used functions whose body changed")
    ap.add_argument("--pkg", default=str(PKG))
    a = ap.parse_args()

    names, attrs = used_symbols(Path(a.pkg))
    bad = 0
    body_diffs: list[str] = []
    for module in sorted(set(names) | set(attrs)):
        old_src, rel = module_source(a.repo, a.old, module)
        new_src, _ = module_source(a.repo, a.new, module)
        print(f"\n== {module}  ({rel})")
        if old_src is None:
            print(f"  not in {a.old}: nothing to compare")
            continue
        if new_src is None:
            print(f"  MISSING in {a.new}")
            bad += 1
            continue
        old_i, new_i = index(old_src), index(new_src)
        inst = {**instance_classes(old_src), **instance_classes(new_src)}
        wanted: list[str] = sorted(names.get(module, set()))
        alias: dict[str, str] = {}                           # reported key -> index key
        for sym, attr in sorted(attrs.get(module, set())):
            wanted.append(f"{sym}.{attr}")
            if sym in inst:
                alias[f"{sym}.{attr}"] = f"{inst[sym]}.{attr}"
        for sym in sorted(names.get(module, set())):
            if sym in TABLES:
                wanted += sorted(k for k in old_i if k.startswith(sym + ".") and k not in wanted)
        for shown in dict.fromkeys(wanted):
            key = alias.get(shown, shown)
            if key not in old_i and key not in new_i:
                print(f"  ?       {shown}: in neither tag (a re-export, or defined dynamically): check by hand")
                continue
            if key not in new_i:
                print(f"  MISSING {shown}\n          was: {old_i[key][0]}")
                bad += 1
                continue
            if key not in old_i:
                print(f"  new     {shown}: {new_i[key][0]}")
                continue
            if old_i[key][0] != new_i[key][0]:
                print(f"  CHANGED {shown}\n          old: {old_i[key][0]}\n          new: {new_i[key][0]}")
                bad += 1
            elif old_i[key][1] != new_i[key][1]:
                print(f"  body    {shown} ({key}): same signature, source changed")
                body_diffs.append(f"--- {module}:{shown} ({key})\n" + "\n".join(difflib.unified_diff(
                    old_i[key][1].splitlines(), new_i[key][1].splitlines(), a.old, a.new, lineterm="", n=2)))
            else:
                print(f"  same    {shown}")
    if a.bodies and body_diffs:
        print("\n\n#### body diffs of used symbols\n")
        print("\n\n".join(body_diffs))
    print(f"\n{bad} used symbol(s) CHANGED or MISSING between {a.old} and {a.new}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
