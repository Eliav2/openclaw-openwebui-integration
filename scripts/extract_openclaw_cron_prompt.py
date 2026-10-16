#!/usr/bin/env python3
"""Extract the Gateway's cron-wake prompt source lines from an installed
OpenClaw dist, for the pipe's wake identity (ELI-103).

A cron wake into a session runs with runId = sessionId, so the pipe finds the
wake by its admitted user turn, whose text starts `[cron:<jobId> <name>]`
(`_CRON_PROMPT_RE`). The turn carries no structured cron field. Two lines
build that text (dist 2026.9.3: isolated-agent-C_VaOV3U.mjs:1066 and the
`commandBody` line that puts `base` first). The fixture pins both, and
test_pipe_unit.CronPromptFixtureTest re-extracts them from the installed
dist when one is present, so a reworded prompt fails the suite.

usage: extract_openclaw_cron_prompt.py [DIST_DIR] [--write]
(DIST_DIR defaults to $OPENCLAW_DIST, else the global npm install's)
"""
import glob
import json
import os
import sys

from openclaw_dist import default_dist

NEEDLES = {"base": "const base = `[cron:", "command_body": "commandBody = `${base}"}
FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "test_fixture_openclaw_cron_prompt.json")


def extract(dist_dir: str) -> dict | None:
    for path in sorted(glob.glob(os.path.join(dist_dir, "isolated-agent-*.mjs"))):
        found = {}
        for n, line in enumerate(open(path, encoding="utf-8"), 1):
            for name, needle in NEEDLES.items():
                if needle in line and name not in found:
                    found[name] = {"line": n, "source": line.strip()}
        if len(found) == len(NEEDLES):
            pkg = os.path.join(os.path.dirname(dist_dir.rstrip("/")), "package.json")
            version = json.load(open(pkg)).get("version") if os.path.exists(pkg) else None
            return {"openclaw_version": version, "file": os.path.basename(path), **found}
    return None


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    got = extract(args[0] if args else default_dist())
    if got is None:
        sys.exit(f"no isolated-agent-*.mjs holds all of {list(NEEDLES.values())}")
    if "--write" in sys.argv:
        fixture = json.load(open(FIXTURE)) if os.path.exists(FIXTURE) else {}
        fixture.update(got)
        json.dump(fixture, open(FIXTURE, "w"), indent=1)
        open(FIXTURE, "a").write("\n")
    print(json.dumps(got, indent=1))
