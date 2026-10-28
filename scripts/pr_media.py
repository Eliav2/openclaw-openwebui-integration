#!/usr/bin/env python3
"""Fail a pull request that changes what users see but shows no image.

AGENTS.md: a PR with a visual change shows a Before and After pair in its
description, from the Screenshots workflow. This is the check for it: when the
PR's diff touches a file that draws the UI (VISUAL below) or an image under
docs/img/, its description needs an `<img>` tag. Tags inside an HTML comment
or a code span do not count, so the PR template's own hints never pass it.

The label `no-visual-change` skips the check, for a change to one of those
files that nothing on screen shows (a refactor, a log line).

    python3 scripts/pr_media.py --event "$GITHUB_EVENT_PATH" --files changed.txt

`--event` is the pull_request event's JSON (its body and labels), `--files`
holds the PR's changed paths, one per line. Exits 1 with the reason when the
check fails. Standard library only.
"""

import argparse
import json
import re
import sys

SKIP_LABEL = "no-visual-change"

# Sources of what users see in the browser: the Status dialog and the
# sub-agent drawer (action.py), the composer's buttons and notes (composer.py,
# composer_watch.py), and the turn as the chat renders it (writer.py). The
# built artifacts follow src/ (build.py --check-all), so src/ is enough.
VISUAL = (
    "src/openclaw_pipe_pkg/action.py",
    "src/openclaw_pipe_pkg/composer.py",
    "src/openclaw_pipe_pkg/composer_watch.py",
    "src/openclaw_pipe_pkg/writer.py",
)
VISUAL_DIRS = ("docs/img/",)

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.S | re.M)
_CODE_SPAN = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.S)
_IMG = re.compile(r"<img\b", re.I)


def visual_files(paths) -> list:
    """The changed paths that draw the UI or are a docs image."""
    return [p for p in paths if p in VISUAL or p.startswith(VISUAL_DIRS)]


def has_image(body: str | None) -> bool:
    """Whether the description shows an image: an `<img` tag outside HTML
    comments, fenced code blocks and code spans."""
    text = _COMMENT.sub("", body or "")
    text = _FENCE.sub("", text)
    text = _CODE_SPAN.sub("", text)
    return bool(_IMG.search(text))


def check(paths, body: str | None, labels) -> str | None:
    """None when the PR passes, else why it does not."""
    if SKIP_LABEL in labels:
        return None
    touched = visual_files(paths)
    if not touched or has_image(body):
        return None
    return ("This PR changes what users see (" + ", ".join(touched[:5])
            + (", ..." if len(touched) > 5 else "") + ") but its description has no image.\n"
            "Add a Before and After pair from the Screenshots workflow as <img src=\"...\" width=\"...\"> "
            "tags (AGENTS.md, \"Docs and screenshots ship with the feature\"), or, when nothing "
            f"on screen changes, add the label `{SKIP_LABEL}`.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--event", required=True, help="the pull_request event's JSON file")
    ap.add_argument("--files", required=True, help="the PR's changed paths, one per line")
    a = ap.parse_args(argv)
    with open(a.event, encoding="utf-8") as fh:
        pr = json.load(fh)["pull_request"]
    with open(a.files, encoding="utf-8") as fh:
        paths = [line.strip() for line in fh if line.strip()]
    labels = [lb["name"] for lb in pr.get("labels") or []]
    why = check(paths, pr.get("body"), labels)
    if why:
        print(f"::error title=No Before and After images::{why.splitlines()[0]}")
        print(why)
        return 1
    print("ok: " + ("label " + SKIP_LABEL if SKIP_LABEL in labels
                    else "has an image" if visual_files(paths) else "no visual files changed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
