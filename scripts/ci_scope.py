#!/usr/bin/env python3
"""Whether a pull request can change what runs: the E2E workflow's gate (ci.yml, job `scope`).

    python3 scripts/ci_scope.py <event> <base> [head]    # prints runtime=true|false for $GITHUB_OUTPUT

Everything except a pull request is runtime. A pull request is not when every file it
touches is harmless:
- under a path only the Unit job or a reader uses (docs, Markdown, tests/ outside the
  vendored fixtures, the issue templates), or
- a Python file whose code is the same once docstrings are dropped (comments and
  docstrings only), the proof .github/workflows/prose-only.yml already asks for.
Anything else, the workflows and this script included, is runtime.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prose_only  # noqa: E402

PULL_REQUEST = "pull_request"
INERT_DIRS = ("docs/", "tests/", ".github/ISSUE_TEMPLATE/")
RUNTIME_DIRS = ("tests/fixtures/",)  # the E2E job checks the CSS list and the installed-dist fixtures against the stack
INERT_FILES = ("LICENSE", "llms.txt")
INERT_SUFFIXES = (".md",)
ARGS_BASE = 2  # <event> <base>
ARGS_BASE_HEAD = 3  # <event> <base> <head>


def inert_path(path: str) -> bool:
    if path.startswith(RUNTIME_DIRS):
        return False
    return path.startswith(INERT_DIRS) or path in INERT_FILES or path.endswith(INERT_SUFFIXES)


def runtime_files(base: str, head: str = "HEAD") -> list[str]:
    """The files of `base...head` that can change what runs."""
    merge_base = prose_only._git("merge-base", base, head).strip()
    changed = []
    for line in prose_only._git("diff", "--name-status", "--no-renames", merge_base, head).splitlines():
        path = line.split("\t", 1)[1]
        if inert_path(path):
            continue
        old, new = prose_only._show(merge_base, path), prose_only._show(head, path)
        if path.endswith(".py") and old is not None and new is not None \
                and prose_only.code_of(old) == prose_only.code_of(new):
            continue
        changed.append(path)
    return changed


def main(argv: list[str]) -> int:
    if len(argv) not in (ARGS_BASE, ARGS_BASE_HEAD):
        sys.exit(__doc__)
    event, base = argv[0], argv[1]
    head = argv[ARGS_BASE] if len(argv) == ARGS_BASE_HEAD else "HEAD"
    files = runtime_files(base, head) if event == PULL_REQUEST else ["(not a pull request)"]
    print(f"runtime={'true' if files else 'false'}")
    for f in files[:20]:
        print(f"::notice::runtime path: {f}", file=sys.stderr)
    if not files:
        print("::notice::nothing that can change what runs: E2E skipped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
