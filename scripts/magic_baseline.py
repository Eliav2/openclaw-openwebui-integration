#!/usr/bin/env python3
"""Fail on a new magic value in a comparison (ruff PLR2004, see ruff-magic.toml).

Ruff has no baseline, so this keeps one: `magic-baseline.json` maps each file
to the source lines that held a hit when the rule arrived, with a count. A hit
is old when its file still has that line text; line numbers do not matter, so
an unrelated rebase never moves the baseline. Fixing a hit only shrinks the
count. Run `--update` after a burn-down to rewrite the file.

    python3 scripts/magic_baseline.py            # CI: exit 1 on a new hit
    python3 scripts/magic_baseline.py --update   # rewrite the baseline
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "scripts" / "magic-baseline.json"
# The Pipe's real source is the fragments under src/; the openclaw_*.py files
# at the root are built from them and would count every hit twice.
PATHS = ["src", "install.py", "install_action.py", "install_filter.py", "build.py", "scripts",
         "gateway-plugins"]


def current() -> tuple[dict[str, Counter], dict[tuple[str, str], list[int]]]:
    out = subprocess.run(
        ["uvx", "ruff@0.17.0", "check", "--config", "ruff-magic.toml",
         "--output-format", "json", *PATHS],
        cwd=ROOT, capture_output=True, text=True)
    if out.returncode not in (0, 1):
        sys.exit(f"ruff failed:\n{out.stderr}")
    found: dict[str, Counter] = {}
    rows: dict[tuple[str, str], list[int]] = {}
    for hit in json.loads(out.stdout or "[]"):
        path = Path(hit["filename"]).relative_to(ROOT).as_posix()
        line = (ROOT / path).read_text().splitlines()[hit["location"]["row"] - 1].strip()
        found.setdefault(path, Counter())[line] += 1
        rows.setdefault((path, line), []).append(hit["location"]["row"])
    return found, rows


def baseline_count(known: dict[str, dict[str, int]], path: str, line: str) -> int:
    return known.get(path, {}).get(line, 0)


def new_hits(found: dict[str, Counter], known: dict[str, dict[str, int]]) -> list[tuple[str, str, int]]:
    """(path, line text, how many more than the baseline holds) for every line that went over."""
    return [(path, line, n - known.get(path, {}).get(line, 0))
            for path, counts in sorted(found.items()) for line, n in counts.items()
            if n > known.get(path, {}).get(line, 0)]


def main() -> int:
    found, rows = current()
    if "--update" in sys.argv:
        BASELINE.write_text(json.dumps({p: dict(sorted(c.items())) for p, c in sorted(found.items())},
                                       indent=1, ensure_ascii=False) + "\n")
        print(f"baseline: {sum(sum(c.values()) for c in found.values())} hits in {len(found)} files")
        return 0
    known = json.loads(BASELINE.read_text())
    new = new_hits(found, known)
    for path, line, extra in new:
        # Identical lines count together: the baseline cannot say which of them is the new one.
        where = ", ".join(f"{path}:{row}" for row in rows[(path, line)])
        print(f"{where}: {extra} new magic value in a comparison (this exact line is in the baseline "
              f"{baseline_count(known, path, line)} time(s), found {len(rows[(path, line)])}): {line}")
    if new:
        print("\nName the value (a module constant, an enum member, http.HTTPStatus) instead of comparing "
              "against a literal. 0, 1, -1 and \"\" are allowed. See AGENTS.md, 'No magic numbers or strings'.")
        return 1
    held = sum(sum(c.values()) for c in known.values())
    print(f"no new magic values ({held} known, burn them down)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
