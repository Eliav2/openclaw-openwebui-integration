#!/usr/bin/env python3
"""Extract the Gateway's cron-wake prompt source lines from an installed
OpenClaw dist, for the pipe's wake identity (ELI-103).

Up to OpenClaw 2026.9 a cron wake into a session runs with runId = sessionId,
so the pipe finds the wake by its admitted user turn, whose text starts
`[cron:<jobId> <name>]` (`_CRON_PROMPT_RE`). The turn carries no structured
cron field. Two lines build that text (dist 2026.9.3:
isolated-agent-C_VaOV3U.mjs:1066 and the `commandBody` line that puts `base`
first). From 2026.10 the prefix is its own line (`sourcePromptPrefix`, also
stored as the turn's provenance) and each wake has its own run id, so the pipe
no longer reads the text there; the prefix is pinned all the same, because
the end-to-end test reads it.

The fixture keeps one entry per form ("forms"), and
test_pipe_unit.CronPromptFixtureTest re-extracts from the installed dist when
one is present: the dist must hold one of them exactly, so a reworded prompt
fails the suite.

usage: extract_openclaw_cron_prompt.py [DIST_DIR] [--write]
(DIST_DIR defaults to $OPENCLAW_DIST, else the global npm install's;
--write adds or replaces the form for the extracted OpenClaw version)
"""
import glob
import json
import os
import sys

from openclaw_dist import default_dist

FORMS = [
    # 2026.9.x: the prefix inline in `base`.
    {"base": "const base = `[cron:", "command_body": "commandBody = `${base}"},
    # 2026.10.x: the prefix on its own line.
    {"prefix": "const sourcePromptPrefix = `[cron:", "base": "const base = `${sourcePromptPrefix}",
     "command_body": "commandBody = `${base}"},
]
FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "test_fixture_openclaw_cron_prompt.json")


def extract(dist_dir: str) -> dict | None:
    pkg = os.path.join(os.path.dirname(dist_dir.rstrip("/")), "package.json")
    version = json.load(open(pkg)).get("version") if os.path.exists(pkg) else None
    for path in sorted(glob.glob(os.path.join(dist_dir, "isolated-agent-*.mjs"))):
        lines = list(enumerate(open(path, encoding="utf-8"), 1))
        for needles in FORMS:
            found = {}
            for n, line in lines:
                for name, needle in needles.items():
                    if needle in line and name not in found:
                        found[name] = {"line": n, "source": line.strip()}
            if len(found) == len(needles):
                return {"openclaw_version": version, "file": os.path.basename(path), **found}
    return None


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    got = extract(args[0] if args else default_dist())
    if got is None:
        sys.exit(f"no isolated-agent-*.mjs holds any of {FORMS}")
    if "--write" in sys.argv:
        fixture = json.load(open(FIXTURE)) if os.path.exists(FIXTURE) else {}
        forms = [f for f in fixture.get("forms", []) if f.get("openclaw_version") != got["openclaw_version"]]
        fixture["forms"] = sorted(forms + [got], key=lambda f: f.get("openclaw_version") or "")
        json.dump(fixture, open(FIXTURE, "w"), indent=1)
        open(FIXTURE, "a").write("\n")
    print(json.dumps(got, indent=1))
