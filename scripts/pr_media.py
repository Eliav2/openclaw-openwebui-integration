#!/usr/bin/env python3
"""Fail a pull request that changes what users see but shows no image.

AGENTS.md: a PR with a visual change shows its Before and After in its
description, as a 2-column table with one row per change, from the Screenshots
workflow. This is the check for it, and it asks only for an image, wherever it
sits in the description (a table cell counts): when the
PR's diff touches a file that draws the UI (VISUAL below) or an image under
docs/img/, its description needs an image: an `<img src=...>` tag or a markdown
image `![alt](url)` whose source is an http(s) URL (`gh pr edit --attach`
rewrites markdown images into GitHub asset URLs; a relative path left in the
posted description was never uploaded).
Images inside an HTML comment, a code span or a fenced block do not count, so
the PR template's own hints never pass it.

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
MAX_LISTED_FILES = 5   # the files a failing message names before "..."

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
# A fence line: indentation and list markers (a fence can open inside a list
# item), then three or more backticks or tildes, then the rest (the info
# string on an opening line, CommonMark 4.5).
_FENCE_LINE = re.compile(r"[ \t]*(?:(?:[-+*]|\d{1,9}[.)])[ \t]+)*(`{3,}|~{3,})(.*)")
_CODE_SPAN = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.S)
# Only an image a PR page can show: a complete tag or markdown image whose
# source is an absolute http(s) URL, as `gh pr edit --attach` writes. A
# relative path in a posted description was never uploaded, and `<img` alone
# (no src, no closing `>`) shows nothing.
_TITLE = r"""(?:\s+(?:"[^"]*"|'[^']*'|\([^)]*\)))?"""
_IMG = re.compile(r"<img(?=\s)[^>]*?\ssrc\s*=\s*[\"']?https?://[^\s\"'>]+[^>]*>"
                  r"|!\[[^\]]*\]\(\s*<?https?://[^\s)>]+>?" + _TITLE + r"\s*\)", re.I)
_BRANCH_LINK = re.compile(r"raw\.githubusercontent\.com|github\.com/[^\s\"')]*/blob/|[?&]raw=true", re.I)


def visual_files(paths) -> list:
    """The changed paths that draw the UI or are a docs image."""
    return [p for p in paths if p in VISUAL or p.startswith(VISUAL_DIRS)]


def has_image(body: str | None) -> bool:
    """Whether the description shows an image: an `<img` tag or a markdown
    `![alt](url)` image outside HTML comments, fenced code blocks and code spans."""
    return bool(_IMG.search(_visible(body)))


def _visible(body: str | None) -> str:
    text = _COMMENT.sub("", body or "")
    text = _strip_fences(text)
    return _CODE_SPAN.sub("", text)


def _strip_fences(text: str) -> str:
    """`text` without its fenced code blocks. A block closes only on a line of
    the same character, at least as long as the opening run, with nothing but
    whitespace after it; an unclosed block runs to the end (CommonMark 4.5)."""
    out, opened = [], None
    for line in text.splitlines(keepends=True):
        m = _FENCE_LINE.fullmatch(line.rstrip("\r\n"))
        if opened is None:
            # A backtick fence's info string cannot hold a backtick.
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                opened = m.group(1)
            else:
                out.append(line)
        elif m and m.group(1)[0] == opened[0] and len(m.group(1)) >= len(opened) and not m.group(2).strip():
            opened = None
    return "".join(out)


def branch_links(body: str | None) -> list:
    """Image links that point at a branch (raw.githubusercontent.com, blob/,
    raw=true): they break when the branch is deleted, so gh --attach is the way."""
    return sorted(set(_BRANCH_LINK.findall(_visible(body))))


def check(paths, body: str | None, labels) -> str | None:
    """None when the PR passes, else why it does not."""
    if SKIP_LABEL in labels:
        return None
    touched = visual_files(paths)
    if not touched or has_image(body):
        return None
    return ("This PR changes what users see (" + ", ".join(touched[:MAX_LISTED_FILES])
            + (", ..." if len(touched) > MAX_LISTED_FILES else "") + ") but its description has no image.\n"
            "Add the Before and After from the Screenshots workflow, uploaded with gh pr edit --attach "
            "(AGENTS.md, \"Docs and screenshots ship with the feature\"), as a 2-column table, one row per change, "
            "with one short heading above each table saying what changed:\n"
            "### What changed\n"
            "| Before | After |\n"
            "|---|---|\n"
            "| <img src=\"URL\" alt=\"...\" width=\"320\"> | <img src=\"URL\" alt=\"...\" width=\"320\"> |\n"
            f"Or, when nothing on screen changes, add the label `{SKIP_LABEL}`.")


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
    if branch_links(pr.get("body")):
        print("::warning title=Image linked from a branch::an image points at a branch "
              "(raw.githubusercontent.com, blob/ or raw=true) and breaks when it is deleted; upload with gh --attach")
    if why:
        print(f"::error title=No Before and After images::{why.splitlines()[0]}")
        print(why)
        return 1
    print("ok: " + ("label " + SKIP_LABEL if SKIP_LABEL in labels
                    else "has an image" if visual_files(paths) else "no visual files changed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
