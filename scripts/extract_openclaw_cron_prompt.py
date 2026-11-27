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
the end-to-end test reads it. From 2026.11.5 the builder is `buildCronCommandBody`
in run-prompt-*.mjs, with minified variable names, so its lines are pinned with
the names and `${...}` placeholders normalized (a "minified" form).

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
import re
import sys

from openclaw_dist import default_dist

FORMS = [
    # 2026.9.x: the prefix inline in `base`.
    {"base": "const base = `[cron:", "command_body": "commandBody = `${base}"},
    # 2026.10.x: the prefix on its own line.
    {"prefix": "const sourcePromptPrefix = `[cron:", "base": "const base = `${sourcePromptPrefix}",
     "command_body": "commandBody = `${base}"},
]
# 2026.11.5 on: run-prompt-*.mjs, variable names minified. Each part is a pattern; the pinned source is the
# line with its variable names and placeholders normalized (`_`, `${job.id}`, `${}`).
MINIFIED_FILES = "run-prompt-*.mjs"
# 2026.11.10 minifies the whole file onto a few lines with no spaces around `=` (`const o=`...;const s=`...`.trim();`,
# `}else d=`...`.trim();`), so the patterns allow any whitespace and are searched in the file, not line by line.
MINIFIED_FORM = {
    "prefix": r"const [\w$]+\s*=\s*`\[cron:\$\{[\w$]+\.id\} \$\{[\w$]+\.name\}\]`;",
    "base": r"const [\w$]+\s*=\s*`\$\{[\w$]+\} \$\{[\w$]+\}`\.trim\(\);",
    "command_body": r"\}\s*else\s+[\w$]+\s*=\s*`\$\{[\w$]+\}\\n\$\{[\w$]+\}`\.trim\(\);",
}


def normalize(line: str) -> str:
    """A minified dist line with its variable names taken out: `const _ = ...`, `${job.id}`, `${}`."""
    line = re.sub(r"\}\s*else\b", "} else", line)
    line = re.sub(r"\b(const|else)\s+[\w$]+\s*=\s*", r"\1 _ = ", line)
    line = re.sub(r"\$\{[\w$]+\.(id|name)\}", r"${job.\1}", line)
    return re.sub(r"\$\{[\w$]+\}", "${}", line)
FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures",
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
    for path in sorted(glob.glob(os.path.join(dist_dir, MINIFIED_FILES))):
        found = {}
        text = open(path, encoding="utf-8").read()
        for name, pattern in MINIFIED_FORM.items():
            m = re.search(pattern, text)
            if m:
                found[name] = {"line": text.count("\n", 0, m.start()) + 1, "source": normalize(m.group(0))}
        if len(found) == len(MINIFIED_FORM):
            return {"openclaw_version": version, "file": os.path.basename(path), "minified": True, **found}
    return None


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    got = extract(args[0] if args else default_dist())
    if got is None:
        sys.exit(f"no isolated-agent-*.mjs holds any of {FORMS}, and no {MINIFIED_FILES} holds {MINIFIED_FORM}")
    if "--write" in sys.argv:
        fixture = json.load(open(FIXTURE)) if os.path.exists(FIXTURE) else {}
        forms = [f for f in fixture.get("forms", []) if f.get("openclaw_version") != got["openclaw_version"]]
        fixture["forms"] = sorted(forms + [got], key=lambda f: f.get("openclaw_version") or "")
        json.dump(fixture, open(FIXTURE, "w"), indent=1)
        open(FIXTURE, "a").write("\n")
    print(json.dumps(got, indent=1))
