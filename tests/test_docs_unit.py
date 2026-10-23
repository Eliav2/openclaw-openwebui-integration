#!/usr/bin/env python3
"""The README and docs/ stay true to the repo: every relative link and anchor
resolves, every image exists and fits its budget, the versions they state are
the ones CI pins, and the wording rules hold (no em dash, no internal terms)."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "docs" / "img"
IMAGE_BUDGET = 250 * 1024          # each image
IMAGES_TOTAL_BUDGET = 4 * 1024 * 1024
DEMOS = IMG / "demos"              # the demos, scripts/demos/record.py: a GIF, an MP4 and a WebM each
DEMO_BUDGET = 1024 * 1024          # each GIF, as record.py enforces
DEMOS_TOTAL_BUDGET = 8 * 1024 * 1024   # the docs' demos and the README's feature tiles (tile-*)
VIDEO_BUDGET = 1024 * 1024         # each MP4 and WebM, as record.py enforces
VIDEOS_TOTAL_BUDGET = 8 * 1024 * 1024
# Contributor and design pages may name issues and internals; user pages may not.
INTERNAL_OK = {"docs/architecture.md", "docs/development.md", "docs/metadata-contract.md",
               "docs/rehydration-persistence.md"}
JARGON = re.compile(r"\bELI-\d+|\bturn writer\b|\bproactive\b|\bpipe run id\b|\bworker\b|\bmanager\b", re.I)

LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)|(?:src|srcset)=\"([^\"]+)\"")
FENCE = re.compile(r"^(```|~~~).*?^\1", re.M | re.S)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)


MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
IMG_TAG = re.compile(r'<img\b(?:[^>"]|"[^"]*")*>')
# A README feature tile: a demo GIF in a <picture>, its still poster the <img>, in a table cell
# that sets the width, so the <img> fills the cell (width="100%").
PICTURE = re.compile(r"<picture>(.*?)</picture>", re.S)
PHONE_WIDTH = 320      # a phone shot, as the README shows it
WIDE_MAX = 800


def image_size(path: Path) -> tuple:
    """(width, height) from the PNG, GIF or JPEG header."""
    data = path.read_bytes()
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # a start-of-frame
                return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
            i += 2 + length
        raise ValueError(f"{path}: no JPEG frame header")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def image_width_limit(size: tuple, gif: bool = False) -> tuple:
    """(kind, largest display width). Shots are 2x captures, so none shows above half its
    pixel width: a strip (3x wider than tall) at half, a phone shot at PHONE_WIDTH, a wide
    (desktop) shot at half, capped at WIDE_MAX. A demo GIF is encoded at 1x, so it shows
    at most at its own width, capped at WIDE_MAX."""
    w, h = size
    if gif:
        return "demo", min(w, WIDE_MAX)
    if w >= 3 * h:
        return "strip", w // 2
    if w > 1000:
        return "wide", min(w // 2, WIDE_MAX)
    return "phone", PHONE_WIDTH


def markdown_files() -> list:
    """README and every docs page; not the site's build output and cache under docs/.vitepress."""
    return [ROOT / "README.md"] + sorted(p for p in (ROOT / "docs").rglob("*.md") if ".vitepress" not in p.parts)


SITE = "https://eliav2.github.io/openclaw-openwebui-integration/"


def site_page(url: str):
    """The docs/ page a docs-site URL serves (clean URLs: /features/ is features/index.md), or None."""
    path = url[len(SITE):].partition("#")[0].rstrip("/")
    if path.endswith((".txt", ".md")):
        return ROOT / "docs" / path if path.endswith(".md") else None
    for cand in (ROOT / "docs" / f"{path}.md", ROOT / "docs" / path / "index.md"):
        if cand.exists():
            return cand
    return ROOT / "docs" / f"{path or 'index'}.md"


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

    def test_every_link_into_the_docs_site_names_a_page_and_heading(self):
        """The README links to the site, not to docs/: each link must still name a page that
        exists and a heading on it, so a renamed page or section breaks here, not for a reader."""
        broken = []
        for md in markdown_files():
            for target in links(md):
                if not target.startswith(SITE):
                    continue
                page, frag = site_page(target), target.partition("#")[2]
                if page is None:
                    continue    # llms.txt and llms-full.txt, which the build writes
                if not page.exists():
                    broken.append(f"{rel(md)}: {target} (no {rel(page)})")
                elif frag and frag not in anchors(page):
                    broken.append(f"{rel(md)}: {target} (no such heading)")
        self.assertEqual(broken, [])

    def test_site_page_maps_clean_urls(self):
        self.assertEqual(site_page(SITE + "features/"), ROOT / "docs" / "features" / "index.md")
        self.assertEqual(site_page(SITE + "install/quickstart#before-you-start"),
                         ROOT / "docs" / "install" / "quickstart.md")
        self.assertEqual(site_page(SITE), ROOT / "docs" / "index.md")
        self.assertIsNone(site_page(SITE + "llms.txt"))

    def test_every_image_exists_fits_its_budget_and_is_used(self):
        used = set()
        for md in markdown_files():
            for target in links(md):
                path = target.partition("#")[0]
                if path.endswith((".png", ".jpg", ".webp", ".gif")):
                    used.add((md.parent / path).resolve())
        # media.json is the record of what each file shows (MediaFreshTests), not an image.
        images = sorted(p for p in IMG.iterdir() if p.is_file() and p.name != "media.json")
        demos = sorted(p for p in DEMOS.iterdir() if p.suffix == ".gif")
        videos = sorted(p for p in DEMOS.iterdir() if p.suffix in (".mp4", ".webm"))
        self.assertEqual([p.name for p in images if p.stat().st_size > IMAGE_BUDGET], [])
        self.assertLessEqual(sum(p.stat().st_size for p in images), IMAGES_TOTAL_BUDGET)
        self.assertEqual([p.name for p in DEMOS.iterdir() if p.suffix not in (".gif", ".mp4", ".webm")], [])
        self.assertEqual([p.name for p in demos if p.stat().st_size > DEMO_BUDGET], [])
        self.assertEqual([p.name for p in videos if p.stat().st_size > VIDEO_BUDGET], [])
        self.assertLessEqual(sum(p.stat().st_size for p in videos), VIDEOS_TOTAL_BUDGET)
        # The site plays a demo's video in place of its GIF: both formats, next to a GIF a page shows.
        stems = {p.stem for p in videos}
        self.assertEqual(sorted(f"{s}: {ext}" for s in stems for ext in ("gif", "mp4", "webm")
                                if not (DEMOS / f"{s}.{ext}").exists()), [])
        self.assertLessEqual(sum(p.stat().st_size for p in demos), DEMOS_TOTAL_BUDGET)
        self.assertEqual([rel(p) for p in images + demos if p.resolve() not in used], [],
                         "images in docs/img that no page uses")

    def test_every_image_has_a_width_within_its_kind(self):
        """github.com shows an image without a width at the full content column, so a phone
        shot fills the page on desktop. Every local image is an <img> with a width."""
        bad = []
        for md in markdown_files():
            text = re.sub(r"`[^`\n]*`", "", FENCE.sub("", md.read_text()))
            for tile in PICTURE.findall(text):
                gif = re.search(r'<source srcset="([^"]+\.gif)"', tile)
                poster = IMG_TAG.search(tile)
                if not gif or not poster or 'width="100%"' not in poster.group(0):
                    bad.append(f"{rel(md)}: a <picture> is a demo GIF source plus a poster <img width=\"100%\">")
                    continue
                g, p = image_size(md.parent / gif.group(1)), image_size(md.parent / re.search(r'\bsrc="([^"]+)"', poster.group(0)).group(1))
                if g[0] * p[1] != g[1] * p[0]:
                    bad.append(f"{rel(md)}: {gif.group(1)} {g} and its poster {p} differ in shape")
            text = PICTURE.sub("", text)
            for _alt, src in MD_IMAGE.findall(text):
                if not re.match(r"^[a-z]+:", src):
                    bad.append(f"{rel(md)}: {src} is ![...](...), use <img src alt width>")
            for tag in IMG_TAG.findall(text):
                src = re.search(r'\bsrc="([^"]+)"', tag).group(1)
                if re.match(r"^[a-z]+:", src):
                    continue
                kind, limit = image_width_limit(image_size(md.parent / src), src.endswith(".gif"))
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
        self.assertEqual(image_width_limit((960, 640), gif=True), ("demo", 800))
        self.assertEqual(image_width_limit((640, 400), gif=True), ("demo", 640))

    def test_image_size_reads_a_jpeg_frame_header(self):
        posters = sorted(IMG.glob("*.jpg"))
        for jpg in posters:
            with self.subTest(jpg=jpg.name):
                self.assertEqual(image_size(jpg), (960, 640))

    def test_image_size_reads_png_and_gif_headers(self):
        self.assertEqual(image_size(IMG / "question-card-light.png"), (780, 1688))
        self.assertEqual(image_size(DEMOS / "hero.gif"), (960, 640))


class HiddenExamplesTests(unittest.TestCase):
    """docs/examples/ describes our own setup, not the Pipe: each page is reachable only
    through a link in the section it illustrates, and listed nowhere (sidebar, home page,
    search, llms.txt)."""

    def setUp(self):
        self.pages = sorted((ROOT / "docs" / "examples").glob("*.md"))
        self.config = (ROOT / "docs" / ".vitepress" / "config.mts").read_text()

    def test_not_in_the_sidebar_or_the_home_page(self):
        self.assertTrue(self.pages)
        self.assertNotRegex(self.config, r'link:\s*"/examples/')
        self.assertNotIn("examples/", (ROOT / "docs" / "index.md").read_text())

    def test_not_in_search_or_llms_txt(self):
        self.assertIn('ignoreFiles: ["examples/**"]', self.config)
        self.assertIn('env.relativePath.startsWith("examples/") ? ""', self.config)

    def test_each_is_linked_from_a_feature_page(self):
        for page in self.pages:
            with self.subTest(page=rel(page)):
                linked = [rel(md) for md in markdown_files() if md.parent.name != "examples"
                          and any((md.parent / l.split("#")[0]).resolve() == page for l in links(md))]
                self.assertTrue(linked, f"nothing links {rel(page)}")


class SocialCardTests(unittest.TestCase):
    """Every docs page carries Open Graph and Twitter tags for the card a shared link shows
    (docs/.vitepress/config.mts): the repository's social preview, served from the site root."""

    def setUp(self):
        self.config = (ROOT / "docs" / ".vitepress" / "config.mts").read_text()

    def test_the_card_image_is_1280_by_640(self):
        self.assertEqual(image_size(IMG / "social-preview.png"), (1280, 640))
        self.assertIn('file: "social-preview.png", width: 1280, height: 640', self.config)

    def test_the_tags_a_card_needs(self):
        for tag in ("og:title", "og:description", "og:url", "og:site_name", "og:type", "og:image",
                    "og:image:width", "og:image:height", "og:image:alt",
                    "twitter:card", "twitter:image"):
            with self.subTest(tag=tag):
                self.assertIn(f'"{tag}"', self.config)
        self.assertIn('"summary_large_image"', self.config)
        self.assertRegex(self.config, r'SITE_URL = \(process\.env\.SITE_URL \?\? "https://')


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
    """The versions docs/versions.md calls tested are the ones CI pins in
    e2e/versions.env. The README leaves versions to that page."""

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
        for path in (ROOT / "docs" / "versions.md",):
            with self.subTest(rel(path)):
                self.assertIn(self.owui, self._row(path, "Open WebUI"))
                self.assertIn(self.openclaw, self._row(path, "OpenClaw Gateway"))

    def test_functions_require_the_minimum_open_webui(self):
        """Open WebUI refuses to install a function whose
        `required_open_webui_version` is above its own version, so the field
        must be the documented minimum: lower lets a release the Pipe breaks
        on install it (spinning tool cards, missing agent replies)."""
        minimums = {self._row(path, "Open WebUI").split("|")[2].strip()
                    for path in (ROOT / "docs" / "versions.md",)}
        self.assertEqual(len(minimums), 1, f"versions.md states no single minimum: {minimums}")
        tested = tuple(int(x) for x in self.owui.split("."))
        self.assertLessEqual(tuple(int(x) for x in next(iter(minimums)).split(".")), tested,
                             "the minimum is above the version CI tests")
        for name in ("openclaw_pipe.py", "openclaw_status_action.py", "openclaw_thinking_filter.py"):
            with self.subTest(name):
                field = re.search(r"^required_open_webui_version:\s*(\S+)$",
                                  (ROOT / name).read_text(), re.M)
                self.assertIsNotNone(field, f"{name} declares no required_open_webui_version")
                self.assertEqual({field.group(1)}, minimums)


class MediaFreshTests(unittest.TestCase):
    """Every screenshot and recording was made on the UI as it is now
    (scripts/media.py, docs/img/media.json): a change to the composer's Claw
    button, the Status icon or dialog, or the pinned Open WebUI fails here
    until the files that show it are recorded again."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("media", ROOT / "scripts" / "media.py")
        self.media = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.media)

    def test_every_file_was_recorded_on_the_current_ui(self):
        self.assertEqual([f"{f}: {why}" for f, why in self.media.stale()], [],
                         "record them again: python3 scripts/media.py (its docstring says how)")

    def test_a_change_to_a_groups_sources_makes_its_files_stale(self):
        manifest = self.media.load()
        manifest["groups"]["status"]["sources"] = ["src/openclaw_pipe_pkg/action.py:_MODAL_COMPACTING_JS"]
        stale = {f for f, _why in self.media.stale(manifest)}
        self.assertIn("status-dialog-light.png", stale)
        self.assertIn("demos/status-dialog.gif", stale)
        self.assertNotIn("demos/hero.gif", stale)

    def test_stamp_refuses_a_file_that_was_not_recorded_again(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "x-light.png").write_bytes(b"old frame")
            manifest = {"groups": {"chrome": {"sources": ["e2e/versions.env:OWUI_IMAGE"], "files": "."}},
                        "files": {"x-light.png": {"sha": "", "groups": {"chrome": "an older ui"}}}}
            (Path(tmp) / "media.json").write_text(json.dumps(manifest))
            self.media.IMG, self.media.MANIFEST = tmp, str(Path(tmp) / "media.json")
            manifest["files"]["x-light.png"]["sha"] = self.media._sha("x-light.png")
            (Path(tmp) / "media.json").write_text(json.dumps(manifest))
            with self.assertRaises(SystemExit):
                self.media.stamp([str(Path(tmp) / "x-light.png")])
            self.media.stamp([str(Path(tmp) / "x-light.png")], same=True)
            self.assertEqual(self.media.stale(), [])
            manifest["files"]["x-light.png"]["groups"]["chrome"] = "an older ui"
            (Path(tmp) / "media.json").write_text(json.dumps(manifest))
            (Path(tmp) / "x-light.png").write_bytes(b"new frame")
            self.media.stamp([str(Path(tmp) / "x-light.png")])
            self.assertEqual(self.media.stale(), [])


if __name__ == "__main__":
    unittest.main()
