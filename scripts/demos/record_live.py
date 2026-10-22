#!/usr/bin/env python3
"""Record the `live-browser` demo by hand, on a real setup: it is not part of the CI demos.

CI cannot make this one. The agent really opens Google Images, the reply carries the real live
view of its browser (LIVE_VIEW_URL, behind a login of ours), and the photo it picks is somebody
else's. So the GIF, MP4 and WebM in docs/img/demos/live-browser.* are recorded here, once, from a
real chat in a signed-in Open WebUI, and docs/img/media.json marks them "manual" so the staleness
guard never asks CI to rebuild them (docs/development.md says how to re-record).

    OWUI_URL=http://localhost:8080 OWUI_TOKEN=<an Open WebUI token> \\
      python3 scripts/demos/record_live.py record --work /tmp/live-browser \\
        [--folder QA] [--viewer-header 'CF-Access-Client-Id: ...'] [--model openclaw_gateway.default]
    python3 scripts/demos/record_live.py encode --work /tmp/live-browser --out docs/img/demos \\
        [--cut 6-42=4,62-118=2.5]

`record` drives one new chat in a desktop Chromium (same window, cursor and light theme as the
other demos) and keeps every frame under --work. `encode` turns those frames into the three files,
so the dead waits can be tuned without another real run. --cut 'from-to=shown' plays that
stretch of the recording (seconds since its first frame) in `shown` seconds, a speed-up the
page's own clock gives away; a frame held still for over 0.5 s anywhere else is cut to 0.5 s.
Nothing else is retimed.

Needs Playwright's Chromium and imageio-ffmpeg (record.py's own requirements).
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import os
import re
import shutil
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import record  # noqa: E402  (the CI recorder: cursor, screencast, ffmpeg)

capture = record.capture
PROMPT = "Open Google and search for a picture of a cute kitten for me. Find the cutest one and send it here."
MAX_SECONDS = 30.0

# The live view is in the reply: an iframe tall enough to be a browser tab, not the empty waiting note.
LIVE_VIEW_UP = """() => { const a = [...document.querySelectorAll('.chat-assistant')].pop();
  const f = a && a.querySelector('iframe'); return !!f && f.getBoundingClientRect().height > 250; }"""
# The reply is over: "Thinking" gone and the picked photo loaded inline (not inside the live view).
REPLY_WITH_PICTURE = """() => { const a = [...document.querySelectorAll('.chat-assistant')].pop();
  const m = a && a.closest('[id^=message-]'); if (!a || /Thinking/.test(m ? m.innerText : '')) return false;
  return [...a.querySelectorAll('img')].some((i) => i.complete && i.naturalWidth > 100); }"""


# Google shows the signed-in account's avatar at the top right of its page. The agent's browser is a
# signed-in profile, so a white disc is drawn over that spot, in the Open WebUI page above the live
# view (the frame itself is another origin). Placed from the live view's own width: the avatar sits
# at 93 % across and, below the viewer's 25 px caption, 4.7 % of the width down.
AVATAR_MASK = r"""
(() => {
  const go = () => {
    const m = document.createElement('div'); m.id = '__avatar-mask';
    m.style.cssText = 'position:fixed;z-index:2147483000;border-radius:50%;background:#fff;pointer-events:none;display:none';
    document.body.appendChild(m);
    const tick = () => {
      const a = [...document.querySelectorAll('.chat-assistant')].pop();
      const f = a && a.querySelector('iframe'), r = f && f.getBoundingClientRect();
      if (r && r.height > 250) {
        const d = r.width * 0.052;
        Object.assign(m.style, {display: 'block', width: d + 'px', height: d + 'px',
          left: (r.left + r.width * 0.928 - d / 2) + 'px', top: (r.top + 25 + r.width * 0.0466 - d / 2) + 'px'});
      } else m.style.display = 'none';
      requestAnimationFrame(tick);
    };
    tick();
  };
  if (document.body) go(); else addEventListener('DOMContentLoaded', go);
})();
"""
# The end of the reply, as a reader would leave it: the agent's reason above the photo, both on screen.
SCROLL_TO_REASON = """() => { const a = [...document.querySelectorAll('.chat-assistant')].pop();
  const img = [...a.querySelectorAll('img')].filter((i) => i.naturalWidth > 100).pop();
  const p = img && img.closest('p'); const el = (p && p.previousElementSibling) || img;
  const box = document.getElementById('messages-container');
  box.scrollTo({top: box.scrollTop + el.getBoundingClientRect().top - 96}); }"""


def headers(pairs: list[str]) -> dict:
    out = {}
    for pair in pairs:
        name, _, value = pair.partition(":")
        out[name.strip()] = value.strip()
    return out


async def put_in_folder(token: str, chat_id: str, name: str) -> None:
    """The recording chat goes in a folder (the QA one) so it never sits among real chats."""
    folders = await asyncio.to_thread(capture.api, "GET", "/api/v1/folders/", None, token)
    found = [f for f in folders if f.get("name") == name and not f.get("parent_id")]
    if not found:
        raise SystemExit(f"no top-level folder {name!r} in Open WebUI: the chat would stay among the real ones")
    await asyncio.to_thread(capture.api, "POST", f"/api/v1/chats/{chat_id}/folder", {"folder_id": found[0]["id"]}, token)


async def record_run(a) -> None:
    token = os.environ["OWUI_TOKEN"]
    viewer = headers(a.viewer_header or [])
    os.makedirs(a.work, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--force-device-scale-factor=2"])
        page = await record.new_page(browser, token)
        await page.context.add_init_script(AVATAR_MASK)
        if viewer:
            # The viewer's own login, sent with every request to its origin (the frame is sandboxed: no cookies of ours).
            origin = re.sub(r"^(https?://[^/]+).*$", r"\1", a.viewer_origin)

            async def add(route):
                await route.continue_(headers={**route.request.headers, **viewer})
            await page.context.route(f"{origin}/**", add)
        await record.open_new_chat(page)
        rec = record.Recorder(await page.context.new_cdp_session(page))
        demo = record.Demo(page, rec, token, {})
        await demo.move(demo.x, demo.y, steps=1)
        await page.wait_for_timeout(800)
        await rec.start()
        rec.record(True)
        await page.wait_for_timeout(500)
        marks = {}
        try:
            await demo.type({"text": a.prompt, "key_ms": a.key_ms})
            await demo.send({})
            if a.folder:
                await put_in_folder(token, demo.chat_id, a.folder)
            marks["up"] = len(rec.frames)
            await demo.waiting({}, demo._wait({"js": LIVE_VIEW_UP, "timeout_ms": 240_000}))
            marks["done"] = len(rec.frames)
            await demo.waiting({}, demo._wait({"js": REPLY_WITH_PICTURE, "timeout_ms": 400_000}))
            marks["reply"] = len(rec.frames)
            await demo.park({})
            await page.wait_for_timeout(600)
            await page.evaluate(SCROLL_TO_REASON)
            await page.wait_for_timeout(2600)
        finally:
            await rec.stop()
            await page.screenshot(path=os.path.join(a.work, "last.png"))
        # what the frame can show: the chat, not the collapsed sidebar's list of chat titles
        text = await page.evaluate("() => (document.getElementById('messages-container') || document.body).innerText")
        for i, (_, jpeg, _) in enumerate(rec.frames):
            with open(os.path.join(a.work, f"{i:05d}.jpg"), "wb") as fh:
                fh.write(jpeg)
        meta = {"ts": [f[0] for f in rec.frames], "segment": [f[2] for f in rec.frames], "waits": rec.waits, "marks": marks,
                "chat": demo.chat_id, "text": text}
        with open(os.path.join(a.work, "frames.json"), "w") as fh:
            json.dump(meta, fh)
        await page.context.close()
        await browser.close()
    print(f"{len(rec.frames)} frames, chat {demo.chat_id}, kept in {a.work}")


def retimed(ts: list, cuts: list[tuple[float, float, float]], fps: int = 20) -> list[tuple[int, float]]:
    """(frame index, seconds shown) for the output: the recording as it happened, except that each
    cut (from s, to s, shown s: seconds since the first frame) plays its stretch in the time given,
    one frame in every 1/fps of that time, so a clock on the page visibly jumps."""
    out, i, n, t0 = [], 0, len(ts), ts[0]
    for start, end, shown in sorted(cuts) + [(float("inf"), float("inf"), 0)]:
        while i < n and ts[i] - t0 < start:     # as it happened
            nxt = ts[i + 1] - ts[i] if i + 1 < n else 0.1
            out.append((i, max(min(nxt, 0.5), 0.01)))
            i += 1
        if start == float("inf"):
            break
        for k in range(max(int(shown * fps), 1)):
            t = t0 + start + (end - start) * k / max(int(shown * fps), 1)
            j = i
            while j + 1 < n and ts[j + 1] <= t:
                j += 1
            out.append((j, 1 / fps))
        while i < n and ts[i] - t0 < end:
            i += 1
    return out


def purple(path: str) -> bool:
    """The signed-in Google account's avatar is a purple disc: does this frame show one? (Needs Pillow.)"""
    try:
        from PIL import Image
    except ImportError:
        return False
    px = Image.open(path).convert("RGB").resize((480, 320)).getdata()
    return sum(1 for r, g, b in px if abs(r - 120) < 30 and abs(g - 65) < 30 and abs(b - 195) < 30) > 12


def encode_run(a) -> None:
    with open(os.path.join(a.work, "frames.json")) as fh:
        meta = json.load(fh)
    hits = capture.deny_hits(meta["text"], ())
    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", meta["text"])
    if hits or emails:
        raise SystemExit(f"the page showed {sorted(set(hits + emails))[:10]}: do not publish this recording")
    cuts = [(float(x), float(y), float(z)) for x, y, z in
            (re.split(r"[-=]", c) for c in a.cut.split(","))] if a.cut else []
    ts = meta["ts"][:bisect.bisect_right([t - meta["ts"][0] for t in meta["ts"]], a.end)] if a.end else meta["ts"]
    plan = retimed(ts, cuts)
    if a.hold:
        plan[-1] = (plan[-1][0], plan[-1][1] + a.hold)
    avatars = [i for i, _ in plan[::6] if purple(os.path.join(a.work, f"{i:05d}.jpg"))]
    if avatars:
        raise SystemExit(f"a Google account's avatar shows in frames {avatars[:5]}: mask it or take another run")
    work = os.path.join(a.work, "out")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    lines, seconds = [], 0.0
    for k, (i, dur) in enumerate(plan):
        shutil.copy(os.path.join(a.work, f"{i:05d}.jpg"), os.path.join(work, f"{k:05d}.jpg"))
        lines += [f"file '{os.path.join(work, f'{k:05d}.jpg')}'", f"duration {dur:.4f}"]
        seconds += dur
    lines.append(f"file '{os.path.join(work, f'{len(plan) - 1:05d}.jpg')}'")
    concat = os.path.join(work, "concat.txt")
    with open(concat, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"{seconds:.1f} s shown, {len(plan)} frames")
    if seconds > a.max_seconds:
        raise SystemExit(f"{seconds:.1f} s, over {a.max_seconds:.0f} s: shorten a cut")
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f"{a.name}.gif")
    size = record.encode(concat, path)
    if size > record.MAX_BYTES:
        size = record.encode(concat, path, fps=12, colors=96)
    if size > record.MAX_BYTES:
        raise SystemExit(f"{size} bytes, over {record.MAX_BYTES}")
    videos = record.encode_videos(concat, os.path.join(a.out, a.name))
    print(json.dumps({"gif_kb": round(size / 1024), "mp4_kb": round(videos["mp4"] / 1024),
                      "webm_kb": round(videos["webm"] / 1024), "seconds": round(seconds, 1)}))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--work", required=True)
    r.add_argument("--prompt", default=PROMPT)
    r.add_argument("--key-ms", type=int, default=30)
    r.add_argument("--folder", help="put the chat in this Open WebUI folder")
    r.add_argument("--viewer-header", action="append", help="'Name: value' sent to the live view's origin")
    r.add_argument("--viewer-origin", help="the live view page's origin, with --viewer-header")
    e = sub.add_parser("encode")
    e.add_argument("--work", required=True)
    e.add_argument("--out", default="docs/img/demos")
    e.add_argument("--name", default="live-browser")
    e.add_argument("--cut", help="'from-to=shown' seconds, comma-separated: play recording seconds from..to in `shown` s")
    e.add_argument("--end", type=float, help="drop the recording after this many seconds")
    e.add_argument("--hold", type=float, default=0, help="seconds to hold the last frame")
    e.add_argument("--max-seconds", type=float, default=MAX_SECONDS)
    a = ap.parse_args(argv)
    if a.cmd == "record":
        asyncio.run(record_run(a))
    else:
        encode_run(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
