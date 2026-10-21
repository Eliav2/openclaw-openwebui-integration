#!/usr/bin/env python3
"""Make docs/img/social-preview.png, the repository's social preview (the card
a link to the repo shows on GitHub, chat apps and social sites).

A 1280x640 light card: the claw logo, the project's name and one line, next to
the finished reply from the hero demo (docs/img/demos/hero.gif). Run it again
after recording the hero again, then stamp the file:

    uv run --with pillow --with playwright scripts/social_preview.py
    python3 scripts/media.py stamp docs/img/social-preview.png

Then upload the file in the repository's Settings > General > Social preview
(that needs the repository's owner). Needs Playwright's Chromium.
"""
from __future__ import annotations

import base64
import io
import os
import tempfile

from PIL import Image, ImageSequence
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERO = os.path.join(ROOT, "docs", "img", "demos", "hero.gif")
LOGO = os.path.join(ROOT, "docs", "public", "logo.svg")
OUT = os.path.join(ROOT, "docs", "img", "social-preview.png")
FRAME_FROM_END = 8   # the reply is finished and the cursor has settled
SIDEBAR = 44         # Open WebUI's icon rail, left of the chat
TAGLINE = "Chat with your OpenClaw agents from Open WebUI and watch them work: tool calls as cards, questions as dialogs, sub-agents in a live panel."

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;800&display=swap" rel="stylesheet">
<style>
  html, body {{ margin: 0; width: 1280px; height: 640px; overflow: hidden; }}
  body {{ font-family: Inter, Ubuntu, "DejaVu Sans", sans-serif; color: #1f1d1c; position: relative;
         background: radial-gradient(circle at 12% 18%, #fde7df 0, rgba(253,231,223,0) 46%), #fffaf7; }}
  .left {{ position: absolute; left: 72px; top: 0; bottom: 0; width: 470px;
          display: flex; flex-direction: column; justify-content: center; }}
  .left svg {{ width: 92px; height: 92px; margin-bottom: 26px; }}
  h1 {{ font-size: 58px; line-height: 1.04; font-weight: 800; letter-spacing: -1.6px; margin: 0 0 22px; }}
  h1 span {{ color: #e4572e; }}
  p {{ font-size: 25px; line-height: 1.38; color: #4a4542; margin: 0; }}
  .card {{ position: absolute; left: 590px; top: 50%; transform: translateY(-50%); width: 640px;
          border-radius: 18px; overflow: hidden; background: #fdfbfd;
          box-shadow: 0 24px 60px rgba(120, 50, 20, .16), 0 0 0 1px rgba(60, 30, 20, .08); }}
  .bar {{ height: 34px; background: #f3eeeb; display: flex; align-items: center; gap: 8px; padding-left: 16px; }}
  .bar i {{ width: 11px; height: 11px; border-radius: 50%; background: #ddd2cc; display: block; }}
  .card img {{ display: block; width: 640px; }}
</style></head><body>
  <div class="left">{logo}<h1><span>OpenClaw</span><br>for Open WebUI</h1><p>{tagline}</p></div>
  <div class="card"><div class="bar"><i></i><i></i><i></i></div><img src="data:image/png;base64,{frame}"></div>
</body></html>"""


def hero_frame() -> str:
    frames = list(ImageSequence.Iterator(Image.open(HERO)))
    frame = frames[-FRAME_FROM_END].convert("RGB")
    frame = frame.crop((SIDEBAR, 0, frame.width, frame.height))
    buf = io.BytesIO()
    frame.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def main() -> None:
    with open(LOGO, encoding="utf-8") as fh:
        page = PAGE.format(logo=fh.read().strip(), tagline=TAGLINE, frame=hero_frame())
    with tempfile.TemporaryDirectory() as tmp:
        html, shot = os.path.join(tmp, "card.html"), os.path.join(tmp, "card.png")
        with open(html, "w", encoding="utf-8") as fh:
            fh.write(page)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            tab = browser.new_page(viewport={"width": 1280, "height": 640})
            tab.goto("file://" + html, wait_until="networkidle")
            tab.evaluate("document.fonts.ready")
            tab.screenshot(path=shot)
            browser.close()
        # 256 flat colors keep it sharp and well under the docs' per-image budget.
        Image.open(shot).convert("RGB").quantize(256, method=Image.Quantize.MEDIANCUT,
                                                 dither=Image.Dither.NONE).save(OUT, optimize=True)
    print(f"{os.path.relpath(OUT, ROOT)}: {os.path.getsize(OUT) // 1024} KB")


if __name__ == "__main__":
    main()
