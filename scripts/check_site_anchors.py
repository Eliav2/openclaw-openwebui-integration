#!/usr/bin/env python3
"""Every link inside the built docs site that names a heading lands on one.

VitePress fails its build on a link to a missing page, not on a link to a missing heading, and
tests/test_docs_unit.py checks headings with GitHub's anchors. This reads the built HTML
(docs/.vitepress/dist by default) and fails when a link's #fragment is not an id on its page,
so the site and github.com cannot disagree about an anchor without CI saying so.

    python3 scripts/check_site_anchors.py [dist]
"""
import posixpath
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

BASE = "/openclaw-openwebui-integration/"


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.hrefs = set(), []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])


def read(path: Path) -> Page:
    p = Page()
    p.feed(path.read_text(encoding="utf-8"))
    return p


def target(dist: Path, page: Path, href: str) -> "Path | None":
    """The built HTML file a site-internal href points at, or None for an outside link."""
    url = urlsplit(href)
    if url.scheme or url.netloc:
        return None
    if not url.path:
        return page
    if url.path.startswith("/"):
        if not url.path.startswith(BASE):
            return None
        rel = url.path[len(BASE):]
    else:
        here = page.relative_to(dist).parent.as_posix()
        rel = posixpath.normpath(posixpath.join(here, unquote(url.path)))
    rel = rel.rstrip("/")
    for cand in (f"{rel}.html", f"{rel}/index.html", rel or "index.html"):
        if (dist / cand).is_file() and cand.endswith(".html"):
            return dist / cand
    return None


def broken(dist: Path) -> "tuple[list, int]":
    """(the links to a missing heading, how many heading links were checked)."""
    pages = {p: read(p) for p in sorted(dist.rglob("*.html"))}
    out, checked = [], 0
    for page, parsed in pages.items():
        for href in parsed.hrefs:
            frag = unquote(urlsplit(href).fragment)
            if not frag:
                continue
            dest = target(dist, page, href)
            if dest is None or dest not in pages:
                continue    # outside the site, or a file the build copies (VitePress checks pages)
            checked += 1
            if frag not in pages[dest].ids:
                out.append(f"{page.relative_to(dist)}: {href} (no id {frag!r} in {dest.relative_to(dist)})")
    return out, checked


def main(argv) -> int:
    dist = Path(argv[1] if len(argv) > 1 else "docs/.vitepress/dist")
    if not dist.is_dir():
        print(f"no built site at {dist}: run `bun run docs:build` first")
        return 2
    bad, checked = broken(dist)
    for line in bad:
        print(line)
    if not checked:
        print("no heading links found: is this the built site?")
        return 1
    print(f"{len(bad)} of {checked} heading link(s) land nowhere" if bad
          else f"all {checked} heading links resolve")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
