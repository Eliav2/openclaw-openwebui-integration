#!/usr/bin/env python3
"""Extract the Gateway's claude-cli exit-error source line from an installed
OpenClaw dist, for the pipe's owner rule (ELI-100 DESIGN 2.7).

The session row exposes a cut-off child only as `lastRunError` text, built
by this one line (dist 2026.9.3: cli-transport-CXSP7FlN.mjs:96). The fixture
pins it, and test_pipe_unit.ExitTextFixtureTest re-extracts it from the
installed dist when one is present, so a reworded error fails the suite.

usage: extract_openclaw_exit_errors.py [DIST_DIR] [--write]
(DIST_DIR defaults to $OPENCLAW_DIST, else the global npm install's)
"""
import glob
import json
import os
import re
import sys

from openclaw_dist import default_dist

NEEDLE = "Claude Code process exited with"
FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures",
                       "test_fixture_openclaw_exit_errors.json")


# The statement that builds the error: `if (code !== 0) exitError = new Error(...)`. A dist from 2026.11.10 on is
# minified onto a few lines (`if(e!==0)f=/* @__PURE__ */ new Error(...)`, the whole transport on line 3), so the line
# holding the needle is no longer the statement: it is cut out of the line. The template literals hold no `;`.
_STATEMENT = re.compile(r"if\s*\(\s*[\w$]+\s*!==\s*0\s*\)\s*[\w$]+\s*=\s*(?:/\*.*?\*/\s*)?new Error\([^;]*;")


def _statement(line: str) -> str:
    """The exit-error statement of a dist line, or the whole line when it has no such statement."""
    for m in _STATEMENT.finditer(line):
        if NEEDLE in m.group(0):
            return m.group(0)
    return line.strip()


def extract(dist_dir: str) -> dict | None:
    for path in sorted(glob.glob(os.path.join(dist_dir, "cli-transport-*.mjs"))):
        for n, line in enumerate(open(path, encoding="utf-8"), 1):
            if NEEDLE in line:
                pkg = os.path.join(os.path.dirname(dist_dir.rstrip("/")), "package.json")
                version = json.load(open(pkg)).get("version") if os.path.exists(pkg) else None
                return {"openclaw_version": version, "file": os.path.basename(path), "line": n,
                        "source": _statement(line)}
    return None


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    got = extract(args[0] if args else default_dist())
    if got is None:
        sys.exit(f"no line containing {NEEDLE!r} in cli-transport-*.mjs")
    if "--write" in sys.argv:
        fixture = json.load(open(FIXTURE)) if os.path.exists(FIXTURE) else {}
        fixture.update(got)
        json.dump(fixture, open(FIXTURE, "w"), indent=1)
        open(FIXTURE, "a").write("\n")
    print(json.dumps(got, indent=1))
