#!/usr/bin/env python3
"""Check that every CSS class the Status Action's browser JS uses exists in
Open WebUI's shipped stylesheet.

The Action builds its dialog and drawer from JS strings with Tailwind class
names. Open WebUI ships a prebuilt Tailwind stylesheet that only holds the
classes Open WebUI's own source uses, so any other class (an arbitrary value
like `z-[10000]`, or a shade Open WebUI never needed) silently does nothing in
the browser. That is how the sub-agent drawer ended up behind the Status
dialog (ELI-118).

    python3 scripts/owui_css.py check [OWUI_URL]   # the Action vs a running Open WebUI
    python3 scripts/owui_css.py dump  [OWUI_URL]   # regenerate the vendored class list

`check` exits 1 when a class is missing. `dump` rewrites
test_fixture_owui_css_classes.txt, the class list of the Open WebUI release in
e2e/versions.env, which the unit suite checks the Action against offline. The
e2e suite checks both against the pinned image. OWUI_URL defaults to
http://localhost:8080. Standard library only.
"""
from __future__ import annotations

import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTION_SRC = os.path.join(ROOT, "src", "openclaw_pipe_pkg", "action.py")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "test_fixture_owui_css_classes.txt")
VERSIONS_ENV = os.path.join(ROOT, "e2e", "versions.env")

# `x.className = '...' + '...' + (cond ? ' a' : '');` up to the statement's end,
# `class="..."` inside HTML strings, and classList.add/toggle/remove('...').
_CLASSNAME_STMT = re.compile(r"\.className\s*=\s*(.*?);\s*$", re.S | re.M)
_CLASS_ATTR = re.compile(r"""class=\\?["']([^"'\\]*)\\?["']""")
_CLASSLIST = re.compile(r"classList\.(?:add|toggle|remove)\(([^)]*)\)")
_STRING = re.compile(r"'([^'\\]*)'|\"([^\"\\]*)\"")
_COMPARISON = re.compile(r"""[!=]==?\s*(?:'[^']*'|"[^"]*")|(?:'[^']*'|"[^"]*")\s*[!=]==?""")
_CLASS_TOKEN = re.compile(r"^-?[a-z@!][\w\-\[\]:/.%#()@!,*&>=]*$")
# A class selector in a stylesheet: `.` then name characters or `\`-escapes.
_CSS_CLASS = re.compile(r"\.((?:[\w-]|\\.)+)")


def _strings(expr: str) -> list:
    """The string literals of a JS expression, minus the ones it compares
    against (`t.status === 'running' ? ' animate-pulse' : ''`)."""
    return [a or b for a, b in _STRING.findall(_COMPARISON.sub("", expr))]


def used_classes(source: str) -> set:
    """Every class name the JS in `source` puts on an element."""
    found = []
    for m in _CLASSNAME_STMT.finditer(source):
        found += _strings(m.group(1))
    for m in _CLASS_ATTR.finditer(source):
        found.append(m.group(1))
    for m in _CLASSLIST.finditer(source):
        found += _strings(m.group(1))[:1]
    return {tok for s in found for tok in s.split() if _CLASS_TOKEN.match(tok)}


def css_class_names(css: str) -> set:
    """Every class a stylesheet defines a rule for, unescaped
    (`.dark\\:hover\\:bg-gray-800\\/60` -> `dark:hover:bg-gray-800/60`)."""
    names = set()
    for m in _CSS_CLASS.finditer(css):
        name = re.sub(r"\\(.)", r"\1", m.group(1))
        if re.match(r"^-?[a-z@!]", name):
            names.add(name)
    return names


def shipped_css(base_url: str) -> str:
    """Every stylesheet Open WebUI's index page links, concatenated."""
    base = base_url.rstrip("/")
    with urllib.request.urlopen(base + "/", timeout=30) as resp:
        html = resp.read().decode()
    parts = []
    for href in sorted(set(re.findall(r'href="(/_app/immutable/[^"]+\.css)"', html))):
        with urllib.request.urlopen(base + href, timeout=30) as resp:
            parts.append(resp.read().decode())
    if not parts:
        raise RuntimeError(f"no stylesheet linked from {base}/")
    return "\n".join(parts)


def action_classes() -> set:
    with open(ACTION_SRC, encoding="utf-8") as fh:
        return used_classes(fh.read())


def pinned_owui_ref() -> str:
    with open(VERSIONS_ENV, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("OWUI_REF="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(f"OWUI_REF not in {VERSIONS_ENV}")


def read_fixture() -> tuple:
    """(Open WebUI ref the list was taken from, set of class names)."""
    ref, names = "", set()
    with open(FIXTURE, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("# ref: "):
                ref = line[len("# ref: "):]
            elif line and not line.startswith("#"):
                names.add(line)
    return ref, names


def write_fixture(names: set, ref: str) -> None:
    with open(FIXTURE, "w", encoding="utf-8") as fh:
        fh.write("# Class names defined by Open WebUI's shipped stylesheet (every CSS file\n"
                 "# its index page links). Regenerate on every Open WebUI bump:\n"
                 "#   python3 scripts/owui_css.py dump http://localhost:8080\n"
                 f"# ref: {ref}\n")
        fh.writelines(n + "\n" for n in sorted(names))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("check", "dump"):
        print(__doc__)
        return 2
    base = argv[1] if len(argv) > 1 else os.environ.get("OWUI_URL", "http://localhost:8080")
    names = css_class_names(shipped_css(base))
    if argv[0] == "dump":
        write_fixture(names, pinned_owui_ref())
        print(f"wrote {len(names)} class names to {FIXTURE}")
        return 0
    used = action_classes()
    gone = sorted(used - names)
    print(f"{len(used)} classes used by the Status Action, {len(gone)} not in {base}'s CSS")
    for cls in gone:
        print(f"  {cls}")
    return 1 if gone else 0


if __name__ == "__main__":
    sys.exit(main())
