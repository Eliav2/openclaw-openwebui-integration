#!/usr/bin/env python3
"""Photograph a README the way a phone shows it on github.com.

Opens the rendered README (the blob page) and the repository's front page in
a 390 px phone viewport, light and dark, and writes one PNG per screen
(844 px tall), top to bottom. Also prints where the hero image and the Quick
start heading land, counted in screens, which is the README's scan test: both
should be on screen one or two.

    uv run --with playwright==1.64.0 python scripts/readme_mobile.py \\
        --repo OWNER/NAME --ref <sha or branch> --out DIR

Runs on a GitHub runner (.github/workflows/readme-mobile.yml), never on a
developer's machine as part of a test.
"""

import argparse
import json
import os

from playwright.sync_api import sync_playwright

SCREEN_W, SCREEN_H = 390, 844
README_SCREENS, HOME_SCREENS = 12, 3
# Safari on an iPhone, so github.com serves its phone layout.
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")

LANDMARKS = """() => {
  const md = document.querySelector('article.markdown-body');
  if (!md) return null;
  const y = (el) => el ? Math.round(el.getBoundingClientRect().top + scrollY) : null;
  const heading = [...md.querySelectorAll('h2')].find((h) => /quick start/i.test(h.textContent));
  return {readmeTop: y(md), hero: y(md.querySelector('picture img, p img')),
          quickStart: y(heading), pageHeight: document.documentElement.scrollHeight};
}"""

IMAGES_LOADED = """() => {
  const md = document.querySelector('article.markdown-body');
  return !!md && [...md.querySelectorAll('img')].every((i) => i.complete);
}"""


def screens(page, out: str, prefix: str, limit: int) -> list:
    height = page.evaluate("() => document.documentElement.scrollHeight")
    written = []
    for i in range(min(limit, -(-height // SCREEN_H))):
        path = os.path.join(out, f"{prefix}-{i + 1:02d}.png")
        page.screenshot(path=path, full_page=True,
                        clip={"x": 0, "y": i * SCREEN_H, "width": SCREEN_W,
                              "height": min(SCREEN_H, height - i * SCREEN_H)})
        written.append(path)
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    readme_url = f"https://github.com/{a.repo}/blob/{a.ref}/README.md"
    home_url = f"https://github.com/{a.repo}/tree/{a.ref}"
    report = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for theme in ("light", "dark"):
                ctx = browser.new_context(viewport={"width": SCREEN_W, "height": SCREEN_H},
                                          device_scale_factor=2, is_mobile=True, has_touch=True,
                                          user_agent=UA, color_scheme=theme, locale="en-US")
                page = ctx.new_page()
                for name, url, limit in (("readme", readme_url, README_SCREENS),
                                         ("home", home_url, HOME_SCREENS)):
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_selector("article.markdown-body", timeout=60_000)
                    # Lazy images load as they scroll into view: walk down once.
                    page.evaluate("""async () => {
                      for (let y = 0; y < document.documentElement.scrollHeight; y += 600) {
                        scrollTo(0, y); await new Promise((r) => setTimeout(r, 150));
                      }
                      scrollTo(0, 0);
                    }""")
                    page.wait_for_function(IMAGES_LOADED, timeout=60_000)
                    marks = page.evaluate(LANDMARKS)
                    if marks:
                        for k in ("hero", "quickStart"):
                            if marks.get(k) is not None:
                                marks[k + "Screen"] = marks[k] // SCREEN_H + 1
                    files = screens(page, a.out, f"{name}-{theme}", limit)
                    report[f"{name}-{theme}"] = {"url": url, "landmarks": marks, "screens": len(files)}
                    print(f"{name}-{theme}: {json.dumps(report[f'{name}-{theme}'])}", flush=True)
                ctx.close()
        finally:
            browser.close()
    with open(os.path.join(a.out, "report.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
