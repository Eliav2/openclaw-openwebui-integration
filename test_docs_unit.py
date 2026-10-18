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


MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
IMG_TAG = re.compile(r'<img\b(?:[^>"]|"[^"]*")*>')
PHONE_WIDTH = 320      # a phone shot, as the README shows it
WIDE_MAX = 800


def png_size(path: Path) -> tuple:
    head = path.read_bytes()[:24]
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def image_width_limit(size: tuple) -> tuple:
    """(kind, largest display width). Shots are 2x captures, so none shows above half its
    pixel width: a strip (3x wider than tall) at half, a phone shot at PHONE_WIDTH, a wide
    (desktop) shot at half, capped at WIDE_MAX."""
    w, h = size
    if w >= 3 * h:
        return "strip", w // 2
    if w > 1000:
        return "wide", min(w // 2, WIDE_MAX)
    return "phone", PHONE_WIDTH


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

    def test_every_image_has_a_width_within_its_kind(self):
        """github.com shows an image without a width at the full content column, so a phone
        shot fills the page on desktop. Every local image is an <img> with a width."""
        bad = []
        for md in markdown_files():
            text = re.sub(r"`[^`\n]*`", "", FENCE.sub("", md.read_text()))
            for _alt, src in MD_IMAGE.findall(text):
                if not re.match(r"^[a-z]+:", src):
                    bad.append(f"{rel(md)}: {src} is ![...](...), use <img src alt width>")
            for tag in IMG_TAG.findall(text):
                src = re.search(r'\bsrc="([^"]+)"', tag).group(1)
                if re.match(r"^[a-z]+:", src):
                    continue
                kind, limit = image_width_limit(png_size(md.parent / src))
                width = re.search(r'\bwidth="(\d+)"', tag)
                if not width:
                    bad.append(f"{rel(md)}: {src} has no width ({kind}: at most {limit})")
                elif int(width.group(1)) > limit:
                    bad.append(f"{rel(md)}: {src} width {width.group(1)} > {limit} ({kind})")
        self.assertEqual(bad, [])

    def test_image_width_limits(self):
        self.assertEqual(image_width_limit((780, 1688)), ("phone", 320))
        self.assertEqual(image_width_limit((780, 430)), ("phone", 320))
        self.assertEqual(image_width_limit((604, 168)), ("strip", 302))
        self.assertEqual(image_width_limit((1600, 1000)), ("wide", 800))
        self.assertEqual(image_width_limit((1200, 700)), ("wide", 600))


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
