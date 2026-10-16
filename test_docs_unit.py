#!/usr/bin/env python3
"""The README and docs/ stay true to the repo: every relative link and anchor
resolves, every image exists and fits its budget, the versions they state are
the ones CI pins, and the wording rules hold (no em dash, no internal terms)."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMG = ROOT / "docs" / "img"
IMAGE_BUDGET = 250 * 1024          # each image
IMAGES_TOTAL_BUDGET = 4 * 1024 * 1024
# Contributor and design pages may name issues and internals; user pages may not.
INTERNAL_OK = {"docs/architecture.md", "docs/development.md", "docs/metadata-contract.md",
               "docs/rehydration-persistence.md"}
JARGON = re.compile(r"\bELI-\d+|\bturn writer\b|\bproactive\b|\bpipe run id\b|\bworker\b|\bmanager\b", re.I)

LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)|(?:src|srcset)=\"([^\"]+)\"")
FENCE = re.compile(r"^(```|~~~).*?^\1", re.M | re.S)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)


def markdown_files() -> list:
    return [ROOT / "README.md"] + sorted((ROOT / "docs").rglob("*.md"))


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"<[^>]+>", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def anchors(path: Path) -> set:
    seen: dict = {}
    out = set()
    for h in HEADING.findall(FENCE.sub("", path.read_text())):
        s = slug(h)
        n = seen.get(s, 0)
        seen[s] = n + 1
        out.add(s if n == 0 else f"{s}-{n}")
    return out


def links(path: Path) -> list:
    text = FENCE.sub("", path.read_text())
    text = re.sub(r"`[^`\n]*`", "", text)
    return [a or b for a, b in LINK.findall(text)]


class DocsLinkTests(unittest.TestCase):

    def test_every_relative_link_and_anchor_resolves(self):
        broken = []
        for md in markdown_files():
            for target in links(md):
                if re.match(r"^[a-z]+:", target):
                    continue
                path, _, frag = target.partition("#")
                dest = (md.parent / path).resolve() if path else md
                if not dest.exists():
                    broken.append(f"{rel(md)}: {target} (no such file)")
                elif frag and dest.suffix == ".md" and frag not in anchors(dest):
                    broken.append(f"{rel(md)}: {target} (no such heading)")
        self.assertEqual(broken, [])

    def test_every_image_exists_fits_its_budget_and_is_used(self):
        used = set()
        for md in markdown_files():
            for target in links(md):
                path = target.partition("#")[0]
                if path.endswith((".png", ".jpg", ".webp", ".gif")):
                    used.add((md.parent / path).resolve())
        images = sorted(p for p in IMG.iterdir() if p.is_file())
        self.assertEqual([p.name for p in images if p.stat().st_size > IMAGE_BUDGET], [])
        self.assertLessEqual(sum(p.stat().st_size for p in images), IMAGES_TOTAL_BUDGET)
        self.assertEqual([p.name for p in images if p.resolve() not in used], [],
                         "images in docs/img that no page uses")


class DocsWordingTests(unittest.TestCase):

    def test_no_em_dash(self):
        hits = [f"{rel(md)}:{n}" for md in markdown_files()
                for n, line in enumerate(md.read_text().splitlines(), 1) if "—" in line]
        self.assertEqual(hits, [])

    def test_no_internal_terms_on_user_pages(self):
        hits = []
        for md in markdown_files():
            name = rel(md)
            if name in INTERNAL_OK or name.startswith("docs/design/"):
                continue
            for n, line in enumerate(md.read_text().splitlines(), 1):
                m = JARGON.search(line)
                if m:
                    hits.append(f"{name}:{n}: {m.group(0)}")
        self.assertEqual(hits, [])


class VersionsTests(unittest.TestCase):
    """The versions the README and docs/versions.md call tested are the
    ones CI pins in e2e/versions.env."""

    def setUp(self):
        env = dict(line.split("=", 1) for line in (ROOT / "e2e" / "versions.env").read_text().splitlines()
                   if "=" in line and not line.lstrip().startswith("#"))
        self.owui = env["OWUI_REF"].strip().lstrip("v")
        self.openclaw = env["OPENCLAW_VERSION"].strip()

    def _row(self, path: Path, label: str) -> str:
        rows = [r for r in path.read_text().splitlines() if r.startswith(f"| {label} |")]
        self.assertTrue(rows, f"{rel(path)} has no '| {label} |' row")
        return rows[0]

    def test_tested_versions_match_ci(self):
        for path in (ROOT / "README.md", ROOT / "docs" / "versions.md"):
            with self.subTest(rel(path)):
                self.assertIn(self.owui, self._row(path, "Open WebUI"))
                self.assertIn(self.openclaw, self._row(path, "OpenClaw Gateway"))

    def test_functions_require_the_minimum_open_webui(self):
        """Open WebUI refuses to install a function whose
        `required_open_webui_version` is above its own version, so the field
        must be the documented minimum: lower lets a release the Pipe breaks
        on install it (spinning tool cards, missing agent replies)."""
        minimums = {self._row(path, "Open WebUI").split("|")[2].strip()
                    for path in (ROOT / "README.md", ROOT / "docs" / "versions.md")}
        self.assertEqual(len(minimums), 1, f"README and versions.md disagree: {minimums}")
        tested = tuple(int(x) for x in self.owui.split("."))
        self.assertLessEqual(tuple(int(x) for x in next(iter(minimums)).split(".")), tested,
                             "the minimum is above the version CI tests")
        for name in ("openclaw_pipe.py", "openclaw_status_action.py", "openclaw_thinking_filter.py"):
            with self.subTest(name):
                field = re.search(r"^required_open_webui_version:\s*(\S+)$",
                                  (ROOT / name).read_text(), re.M)
                self.assertIsNotNone(field, f"{name} declares no required_open_webui_version")
                self.assertEqual({field.group(1)}, minimums)


if __name__ == "__main__":
    unittest.main()
