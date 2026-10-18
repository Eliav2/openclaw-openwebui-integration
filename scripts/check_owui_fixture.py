#!/usr/bin/env python3
"""Fail when the vendored Open WebUI handler no longer matches upstream.

`test_fixture_owui_streaming_handler.py` is a verbatim copy of Open WebUI's
`handle_responses_streaming_event`, and the tool-card tests run against it so
they pin the real behaviour instead of our belief about it. A copy is only
worth that while it is current: in 0.11.1 upstream rewrote the handler, the
fixture stayed on 0.11.0, the suite stayed green, and every tool card
disappeared from the live UI.

This compares the two at the AST level (formatting and comments are ignored,
any change in behaviour is not) against a given Open WebUI git ref.

Usage:
  python3 scripts/check_owui_fixture.py --ref v0.12.0
  python3 scripts/check_owui_fixture.py --ref main   # what the next release ships
  python3 scripts/check_owui_fixture.py --ref v0.12.0 --print-upstream  # re-extract

Exit status: 0 match, 1 drift, 2 could not fetch or find the handler.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "test_fixture_owui_streaming_handler.py"
FUNCTION = "handle_responses_streaming_event"
# 0.12.3 moved the handler from middleware.py to response.py.
UPSTREAM = ("https://raw.githubusercontent.com/open-webui/open-webui/{ref}/"
            "backend/open_webui/utils/{module}.py")
MODULES = ("response", "middleware")


def find_function(source: str, origin: str) -> tuple[ast.AST, str]:
    tree = ast.parse(source, filename=origin)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == FUNCTION:
            return node, ast.get_source_segment(source, node) or ""
    raise LookupError(f"{FUNCTION} not found in {origin}")


def normalized(node: ast.AST) -> str:
    # Docstrings are documentation; drop them so a reworded docstring alone is
    # not reported as drift.
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(
            getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
        node = ast.FunctionDef(**{**node.__dict__, "body": body[1:] or [ast.Pass()]})
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ref", required=True, help="Open WebUI git ref (tag, branch, sha)")
    parser.add_argument("--print-upstream", action="store_true",
                        help="print the upstream function source and exit")
    args = parser.parse_args()

    up = None
    for module in MODULES:
        url = UPSTREAM.format(ref=args.ref, module=module)
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                upstream_src = resp.read().decode()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue  # an older ref without this module
            print(f"could not fetch {url}: {exc}", file=sys.stderr)
            return 2
        except urllib.error.URLError as exc:
            print(f"could not fetch {url}: {exc}", file=sys.stderr)
            return 2
        try:
            up = find_function(upstream_src, url)
            break
        except LookupError:
            continue
    if up is None:
        print(f"{FUNCTION} not found in open-webui@{args.ref} ({', '.join(MODULES)})", file=sys.stderr)
        return 2
    up_node, up_text = up
    try:
        fx_node, fx_text = find_function(FIXTURE.read_text(), str(FIXTURE))
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.print_upstream:
        print(up_text)
        return 0

    if normalized(up_node) == normalized(fx_node):
        print(f"OK: vendored {FUNCTION} matches Open WebUI {args.ref}")
        return 0

    print(f"DRIFT: vendored {FUNCTION} differs from Open WebUI {args.ref}.\n"
          f"Re-extract it (python3 scripts/check_owui_fixture.py --ref {args.ref} "
          f"--print-upstream), update the fixture docstring's version line, and re-run "
          f"the unit suite: tests pinned to the old behaviour may now be wrong.\n")
    sys.stdout.writelines(difflib.unified_diff(
        fx_text.splitlines(keepends=True), up_text.splitlines(keepends=True),
        fromfile="fixture", tofile=f"open-webui@{args.ref}"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
