#!/usr/bin/env python3
"""List what the old README said that the new README + docs/ no longer say.

Takes every concrete token of the old README (inline code, ALL_CAPS valve and
env names, versions, port numbers, file paths) and looks each one up in the
current README.md and docs/. Prints the misses with the old line they came
from. Each miss is either moved somewhere the token search can't see (reworded),
or removed on purpose; the docs PR lists the removals with the reason.

    python3 scripts/docs_nothing_lost.py [old-ref]     # default: origin/main
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATTERNS = (
    re.compile(r"`([^`\n]{2,80})`"),                      # inline code
    re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b"),   # VALVE_NAMES, ENV_VARS
    re.compile(r"\b(\d+\.\d+(?:\.\d+)?)\b"),              # versions
    re.compile(r"\b(\d{4,5})\b"),                         # ports
    re.compile(r"\b([\w.-]+/[\w./-]+\.(?:py|md|json|sh|js|ts|yml|txt))\b"),  # paths
)


def old_readme(ref: str) -> str:
    return subprocess.run(["git", "show", f"{ref}:README.md"], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout


def new_text() -> str:
    files = [ROOT / "README.md"] + sorted((ROOT / "docs").rglob("*.md"))
    return "\n".join(f.read_text() for f in files)


def main() -> int:
    ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    haystack = new_text()
    misses: dict = {}
    for n, line in enumerate(old_readme(ref).splitlines(), 1):
        for pat in TOKEN_PATTERNS:
            for tok in pat.findall(line):
                if tok not in haystack and tok not in misses:
                    misses[tok] = (n, line.strip())
    for tok, (n, line) in misses.items():
        print(f"{tok!r:40}  old line {n}: {line[:110]}")
    print(f"\n{len(misses)} tokens of the old README are not in README.md + docs/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
