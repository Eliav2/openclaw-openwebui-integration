#!/usr/bin/env python3
"""Extract the Gateway's Claude thinking profiles (level ids per model) from
an installed OpenClaw dist, for the Pipe's claude-cli `off` clamp
(CLAUDE_CLI_THINKING_OFF_IGNORED).

The profiles are `const CLAUDE_<MODEL>_THINKING_PROFILE = {levels: [{id}...]}`
in anthropic-*.mjs (2026.10.10: anthropic-B3HirzdP.mjs); some alias another
(`= OTHER;`) or spread it (`{...OTHER, defaultLevel}`). The fixture pins them,
and test_pipe_unit.ClaudeThinkingProfileFixtureTest re-extracts them from the
installed dist when one is present.

usage: extract_openclaw_claude_thinking_profiles.py [DIST_DIR] [--write]
(DIST_DIR defaults to $OPENCLAW_DIST, else the global npm install's)
"""
import glob
import json
import os
import re
import sys

from openclaw_dist import default_dist

# The models the Pipe's clamp is about, by the profile constant naming each.
PROFILES = {"claude-sonnet-5-5": "CLAUDE_SONNET_55_THINKING_PROFILE",
            "claude-haiku-5-5": "CLAUDE_HAIKU_55_THINKING_PROFILE"}
FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures",
                       "test_fixture_openclaw_claude_thinking_profiles.json")
WRITE_FLAG = "--write"
_DEF_RE = re.compile(r"const (CLAUDE_\w+_THINKING_PROFILE) = (.*?);\n(?=const |function |/\*\*|//|export )", re.S)


def _levels(defs: dict, name: str, seen=()) -> list | None:
    body = defs.get(name)
    if body is None or name in seen:
        return None
    alias = re.fullmatch(r"\s*(CLAUDE_\w+_THINKING_PROFILE)\s*", body)
    if alias:
        return _levels(defs, alias.group(1), (*seen, name))
    own = re.search(r"levels:\s*\[(.*?)\]", body, re.S)
    if own:
        return re.findall(r'id:\s*"([^"]+)"', own.group(1))
    spread = re.search(r"\.\.\.(CLAUDE_\w+_THINKING_PROFILE)", body)
    return _levels(defs, spread.group(1), (*seen, name)) if spread else None


def extract(dist_dir: str) -> dict | None:
    for path in sorted(glob.glob(os.path.join(dist_dir, "anthropic-*.mjs"))):
        defs = dict(_DEF_RE.findall(open(path, encoding="utf-8").read()))
        if not all(name in defs for name in PROFILES.values()):
            continue
        pkg = os.path.join(os.path.dirname(dist_dir.rstrip("/")), "package.json")
        version = json.load(open(pkg)).get("version") if os.path.exists(pkg) else None
        return {"openclaw_version": version, "file": os.path.basename(path),
                "profiles": {model: _levels(defs, name) for model, name in PROFILES.items()}}
    return None


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    got = extract(args[0] if args else default_dist())
    if got is None:
        sys.exit(f"no anthropic-*.mjs defines {sorted(PROFILES.values())}")
    print(json.dumps(got, indent=1))
    if WRITE_FLAG in sys.argv:
        with open(FIXTURE, "w") as f:
            f.write(json.dumps(got, indent=1) + "\n")
