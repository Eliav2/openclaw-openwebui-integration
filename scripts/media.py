#!/usr/bin/env python3
"""Keep the docs' screenshots and demos from going stale.

docs/img/media.json lists what each image or recording shows of this
integration's own UI, as groups of sources: the strings it hands the browser
(the composer's Claw button, the toolbar icon, the Status dialog's scripts)
and the pinned Open WebUI image. For every file under docs/img it records the
groups' fingerprints at recording time. A change to a group's sources makes
every file that shows it stale until it is recorded again, and the docs unit
tests fail until then (tests/test_docs_unit.py).

    python3 scripts/media.py check                 # the stale files, exit 1 if any
    python3 scripts/media.py stamp docs/img/x.png  # after recording x again
    python3 scripts/media.py stamp --same docs/img/x.png  # recorded again, came out identical
    python3 scripts/media.py stamp --manual docs/img/x.gif  # a recording made by hand, never by CI
    uv run --with pillow scripts/media.py posters  # the README tiles' posters, from their GIFs

A file marked `"manual": true` in media.json was recorded by hand, on a real setup
(docs/development.md says how), so no workflow can record it again. A change to its
sources does not fail the docs unit tests; `check` lists it as a note instead, for the
next person who records it.

To record again: run the Screenshots and Demos workflows on the branch (Actions,
"Run workflow", `only` = the stale names; secret-request needs `openclaw_version`
= next), put the artifacts' files in
docs/img/ and docs/img/demos/, run `posters` if a tile GIF changed, then `stamp`
them. `stamp` refuses a file whose bytes did not change since the last stamp
while its sources did, as it most likely was not recorded again. A screenshot
of a state the change did not touch can come out byte-identical: after
recording it again and comparing, stamp it with `--same`.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG = os.path.join(ROOT, "docs", "img")
MANIFEST = os.path.join(IMG, "media.json")
MEDIA = (".png", ".jpg", ".gif", ".mp4", ".webm")


def _strings(path: str, pattern: str) -> list:
    """The module-level string constants of a Python file whose names match."""
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    found = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for t in targets:
            if isinstance(t, ast.Name) and fnmatch.fnmatchcase(t.id, pattern) and node.value is not None:
                found.append(f"{t.id}={ast.unparse(node.value)}")
    if not found:
        raise SystemExit(f"media.json: {path}:{pattern} matches no constant")
    return found


def _env(path: str, key: str) -> list:
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        found = [ln.strip() for ln in fh if ln.startswith(f"{key}=")]
    if not found:
        raise SystemExit(f"media.json: {path}:{key} not set")
    return found


def fingerprint(sources: list) -> str:
    """One hash of a group's sources: `file.py:NAME_GLOB` (string constants) or `file.env:KEY`."""
    parts = []
    for src in sources:
        path, name = src.split(":", 1)
        parts += [src] + (_env(path, name) if path.endswith(".env") else _strings(path, name))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _sha(rel: str) -> str:
    with open(os.path.join(IMG, rel), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:16]


def load() -> dict:
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def media_files() -> list:
    return sorted(os.path.relpath(os.path.join(d, f), IMG) for d, _, fs in os.walk(IMG)
                  for f in fs if f.endswith(MEDIA))


def groups_of(manifest: dict, rel: str) -> list:
    return [g for g, spec in manifest["groups"].items() if re.search(spec["files"], rel)]


def stale(manifest: dict | None = None) -> list:
    """(file, reason) for every file under docs/img not recorded on its sources as they are now."""
    manifest = manifest or load()
    now = {g: fingerprint(spec["sources"]) for g, spec in manifest["groups"].items()}
    out = []
    for rel in media_files():
        entry = manifest["files"].get(rel)
        if entry is None:
            out.append((rel, "not in docs/img/media.json"))
            continue
        if entry.get("manual"):
            continue    # recorded by hand: manual_stale() lists it
        changed = [g for g in groups_of(manifest, rel) if entry["groups"].get(g) != now[g]]
        if changed:
            out.append((rel, "recorded before a change to " + ", ".join(changed)))
    out += [(rel, "in media.json but not in docs/img") for rel in manifest["files"] if rel not in media_files()]
    return out


def manual_stale(manifest: dict | None = None) -> list:
    """(file, reason) for the hand-recorded files whose sources changed after they were recorded."""
    manifest = manifest or load()
    now = {g: fingerprint(spec["sources"]) for g, spec in manifest["groups"].items()}
    out = []
    for rel, entry in manifest["files"].items():
        changed = [g for g in groups_of(manifest, rel) if entry["groups"].get(g) != now[g]]
        if entry.get("manual") and changed and rel in media_files():
            out.append((rel, "recorded by hand before a change to " + ", ".join(changed)))
    return out


def stamp(paths: list, same: bool = False, manual: bool = False) -> None:
    manifest = load()
    now = {g: fingerprint(spec["sources"]) for g, spec in manifest["groups"].items()}
    for p in paths:
        rel = os.path.relpath(os.path.abspath(p), IMG)
        if not os.path.isfile(os.path.join(IMG, rel)) or not rel.endswith(MEDIA):
            raise SystemExit(f"stamp: {p} is not an image or recording under docs/img")
        old, sha = manifest["files"].get(rel), _sha(rel)
        groups = {g: now[g] for g in groups_of(manifest, rel)}
        if old and old["sha"] == sha and old["groups"] != groups and not same:
            raise SystemExit(f"stamp: {rel} is the same file as before its sources changed: record it again"
                             " (or pass --same if the new recording came out identical)")
        manifest["files"][rel] = {"sha": sha, "groups": groups, **({"manual": True} if manual or (old or {}).get("manual") else {})}
    manifest["files"] = dict(sorted(manifest["files"].items()))
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")


def posters() -> None:
    """Each README tile's poster: the first frame of the GIF its <picture> plays (its cover,
    scripts/demos/record.py), so the still and the animation open on the same picture."""
    from PIL import Image
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        pairs = re.findall(r'<source srcset="\./(docs/img/[^"]+\.gif)"[^>]*><img src="\./(docs/img/[^"]+\.jpg)"', fh.read())
    if not pairs:
        raise SystemExit("posters: no <picture> tile in README.md")
    for gif, jpg in pairs:
        first = Image.open(os.path.join(ROOT, gif)).convert("RGB")
        first.save(os.path.join(ROOT, jpg), quality=82, optimize=True, progressive=True)
        print(f"wrote {jpg} (from {gif})")


def main(argv: list) -> int:
    if argv[:1] == ["check"]:
        found = stale()
        for rel, why in found:
            print(f"docs/img/{rel}: {why}")
        for rel, why in manual_stale():
            print(f"note: docs/img/{rel}: {why}")
        return 1 if found else 0
    flags = ("--same", "--manual")
    if argv[:1] == ["stamp"] and [a for a in argv[1:] if a not in flags]:
        stamp([a for a in argv[1:] if a not in flags], same="--same" in argv[1:], manual="--manual" in argv[1:])
        return 0
    if argv[:1] == ["posters"]:
        posters()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
