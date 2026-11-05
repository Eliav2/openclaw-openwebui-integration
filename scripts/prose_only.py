#!/usr/bin/env python3
"""Prove a change is prose only: every Python file it touches has the same
AST once docstrings are dropped (comments never reach the AST), and every
other file it touches is Markdown. A PR labelled `prose-only` must pass this
to skip the live gate (.github/workflows/prose-only.yml).

    python3 scripts/prose_only.py origin/main   # exit 1 on any code change
"""
from __future__ import annotations

import ast
import subprocess
import sys

PROSE_SUFFIXES = (".md",)


def _drop_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            # A body that held only its docstring needs `pass` once it goes.
            node.body = body or [ast.Pass()]
    return tree


def code_of(source: str) -> str:
    """The code of a Python source: its AST without docstrings or positions."""
    return ast.dump(_drop_docstrings(ast.parse(source)))


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def _show(ref: str, path: str) -> str | None:
    out = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else None


def code_changes(base: str, head: str = "HEAD") -> list[str]:
    """What in `base...head` is more than prose, one line per file."""
    merge_base = _git("merge-base", base, head).strip()
    problems = []
    for line in _git("diff", "--name-status", "--no-renames", merge_base, head).splitlines():
        status, path = line.split("\t", 1)
        if path.endswith(PROSE_SUFFIXES):
            continue
        if not path.endswith(".py"):
            problems.append(f"{path}: not a Python or Markdown file")
            continue
        old, new = _show(merge_base, path), _show(head, path)
        if old is None or new is None:
            problems.append(f"{path}: {'added' if old is None else 'deleted'}")
        elif code_of(old) != code_of(new):
            problems.append(f"{path}: its code changed, not only comments and docstrings")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.exit(__doc__)
    problems = code_changes(argv[0])
    for p in problems:
        print(p)
    print("prose only: no code changed" if not problems else f"not prose only: {len(problems)} file(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
