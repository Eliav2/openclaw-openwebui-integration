#!/usr/bin/env python3
"""Extract the Gateway's claude-cli exit-error source line from an installed
OpenClaw dist, for the pipe's owner rule (ELI-100 DESIGN 2.7).

The session row exposes a cut-off child only as `lastRunError` text, built
by this one line (dist 2026.9.3: cli-transport-CXSP7FlN.mjs:96). The fixture
pins it, and test_pipe_unit.ExitTextFixtureTest re-extracts it from the
installed dist when one is present, so a reworded error fails the suite.

usage: extract_openclaw_exit_errors.py [DIST_DIR] [--write]
"""
import glob
import json
import os
import sys

DEFAULT_DIST = "/config/.node_global/lib/node_modules/openclaw/dist"
NEEDLE = "Claude Code process exited with"
FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "test_fixture_openclaw_exit_errors.json")


def extract(dist_dir: str) -> dict | None:
    for path in sorted(glob.glob(os.path.join(dist_dir, "cli-transport-*.mjs"))):
        for n, line in enumerate(open(path, encoding="utf-8"), 1):
            if NEEDLE in line:
                pkg = os.path.join(os.path.dirname(dist_dir.rstrip("/")), "package.json")
                version = json.load(open(pkg)).get("version") if os.path.exists(pkg) else None
                return {"openclaw_version": version, "file": os.path.basename(path), "line": n,
                        "source": line.strip()}
    return None


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    got = extract(args[0] if args else DEFAULT_DIST)
    if got is None:
        sys.exit(f"no line containing {NEEDLE!r} in cli-transport-*.mjs")
    if "--write" in sys.argv:
        fixture = json.load(open(FIXTURE)) if os.path.exists(FIXTURE) else {}
        fixture.update(got)
        json.dump(fixture, open(FIXTURE, "w"), indent=1)
        open(FIXTURE, "a").write("\n")
    print(json.dumps(got, indent=1))
