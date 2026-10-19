#!/usr/bin/env python3
"""Record the docs' demos from a running screenshot stack: a GIF, an MP4 and a WebM each.

Each demo in demos.json is a short scripted session in a desktop Chromium:
real Open WebUI, the installed Pipe and Action, a real Gateway and the
scripted model, so every run shows the same replies and leaves nothing in a
real account. The page is captured with the DevTools screencast at 2x while
a drawn cursor, a ripple on each click and a badge for each key make the
steps easy to follow; ffmpeg then encodes the frames, with their real
timing, into one light-theme recording per demo, three ways: a GIF for
github.com (the Markdown shows it) and a muted MP4 and WebM at the capture's
full 2x resolution, sharp text at a fraction of the GIF's size, which the docs
site plays instead of the GIF (docs/.vitepress/config.mts).

    python3 scripts/demos/record.py --out docs/img/demos [--only hero,send-now]

Run by .github/workflows/demos.yml on the screenshot stack (e2e/start_stack.sh
with scripts/screenshots/'s settings and
FAKE_LLM_SCENES=scripts/demos/demos.json:scripts/screenshots/scenes.json).
Needs Playwright's Chromium and imageio-ffmpeg (its own ffmpeg build). Env:
OWUI_URL, OWUI_EMAIL, OWUI_PASSWORD (as start_stack.sh exports them).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from playwright.async_api import Page, async_playwright

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screenshots"))
import capture  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 960, 640            # CSS pixels; frames are captured at 2x and the GIF is W wide
MAX_SECONDS = 15.0
MAX_BYTES = 1024 * 1024
MAX_VIDEO_BYTES = 1024 * 1024    # each of the MP4 and the WebM
TYPE_DELAY_MS = 45

# The drawn cursor, the click ripple and the key badge: page elements that
# follow the real mouse and keyboard events, so the screencast shows them.
OVERLAY = r"""
(() => {
  const mk = () => {
    if (document.getElementById('__cur')) return;
    const st = document.createElement('style');
    st.textContent = `
      #__cur{position:fixed;left:0;top:0;width:22px;height:22px;z-index:2147483647;pointer-events:none;
        transform:translate(-60px,-60px);filter:drop-shadow(0 2px 3px rgba(0,0,0,.35))}
      .__rip{position:fixed;width:36px;height:36px;margin:-18px 0 0 -18px;border-radius:50%;
        background:rgba(59,130,246,.35);z-index:2147483646;pointer-events:none;animation:__r .45s ease-out forwards}
      @keyframes __r{from{transform:scale(.3);opacity:1}to{transform:scale(1.6);opacity:0}}
      #__kb{position:fixed;left:50%;bottom:40px;transform:translateX(-50%) scale(.9);opacity:0;z-index:2147483647;
        pointer-events:none;display:flex;gap:8px;transition:opacity .18s,transform .18s}
      #__kb.on{opacity:1;transform:translateX(-50%) scale(1)}
      #__kb span{font:600 22px/1 -apple-system,Segoe UI,Inter,sans-serif;color:#fff;background:rgba(17,17,17,.82);
        padding:12px 16px;border-radius:12px;box-shadow:0 6px 20px rgba(0,0,0,.25);min-width:20px;text-align:center}`;
    document.head.appendChild(st);
    const c = document.createElement('div'); c.id = '__cur';
    c.innerHTML = '<svg viewBox="0 0 24 24" width="22" height="22"><path d="M3 2l7.5 19 2.6-7.9L21 10.5z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.body.appendChild(c);
    const kb = document.createElement('div'); kb.id = '__kb'; document.body.appendChild(kb);
    addEventListener('mousemove', e => { c.style.transform = `translate(${e.clientX - 3}px,${e.clientY - 2}px)`; }, true);
    addEventListener('mousedown', e => { const r = document.createElement('div'); r.className = '__rip';
      r.style.left = e.clientX + 'px'; r.style.top = e.clientY + 'px'; document.body.appendChild(r);
      setTimeout(() => r.remove(), 500); }, true);
    window.__keys = (keys, ms) => { kb.innerHTML = keys.map(k => `<span>${k}</span>`).join('');
      kb.classList.add('on'); clearTimeout(window.__kt); window.__kt = setTimeout(() => kb.classList.remove('on'), ms || 900); };
  };
  if (document.body) mk(); else addEventListener('DOMContentLoaded', mk);
})();
"""

# The last reply is done: its text has stopped changing and it no longer says
# "Thinking": Open WebUI fades text in while the reply still runs, so stable
# text alone can be mid-stream.
LAST_REPLY = """() => { const a = [...document.querySelectorAll('.chat-assistant')].pop();
  const m = a && a.closest('[id^=message-]');
  return a ? [a.innerText, m ? m.innerText : ''] : ['', '']; }"""


class Recorder:
    """The page's screencast frames, kept only while recording is on."""

    def __init__(self, cdp):
        self.cdp, self.on = cdp, False
        self.frames: list = []        # (timestamp, png bytes, starts a new segment)
        self._new_segment = True
        self._acks: set = set()       # in flight; a task nobody holds can be collected
        cdp.on("Page.screencastFrame", self._frame)

    def _frame(self, ev) -> None:
        task = asyncio.ensure_future(self._ack(ev["sessionId"]))
        self._acks.add(task)
        task.add_done_callback(self._acks.discard)
        if self.on:
            self.frames.append((ev["metadata"]["timestamp"], base64.b64decode(ev["data"]), self._new_segment))
            self._new_segment = False

    async def _ack(self, session_id) -> None:
        try:
            await self.cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass  # the page closed with a frame in flight

    async def stop(self) -> None:
        self.on = False
        await self.cdp.send("Page.stopScreencast")

    async def start(self) -> None:
        await self.cdp.send("Page.startScreencast", {"format": "png", "maxWidth": W * 2, "maxHeight": H * 2,
                                                     "everyNthFrame": 1})

    def record(self, on: bool) -> None:
        if on and not self.on:
            self._new_segment = True
        self.on = on

    def write(self, work: str) -> tuple[str, float]:
        """The frames as PNGs and an ffmpeg concat list with their real
        durations; a pause in the recording shows as a 0.1 s cut."""
        if not self.frames:
            raise RuntimeError("no frames were recorded")
        lines, total = [], 0.0
        for i, (ts, png, _) in enumerate(self.frames):
            path = os.path.join(work, f"{i:05d}.png")
            with open(path, "wb") as fh:
                fh.write(png)
            nxt = self.frames[i + 1] if i + 1 < len(self.frames) else None
            dur = 0.1 if nxt is None or nxt[2] else max(nxt[0] - ts, 0.01)
            total += dur
            lines += [f"file '{path}'", f"duration {dur:.4f}"]
        lines.append(f"file '{os.path.join(work, f'{len(self.frames) - 1:05d}.png')}'")
        concat = os.path.join(work, "concat.txt")
        with open(concat, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        return concat, total


def png_size(png: bytes) -> str:
    return f"{int.from_bytes(png[16:20], 'big')}x{int.from_bytes(png[20:24], 'big')}"


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def encode(concat: str, out: str, fps: int = 20, colors: int = 128) -> int:
    """One GIF, W wide, with a palette made from the frames' changes."""
    vf = (f"fps={fps},scale={W}:-1:flags=lanczos,split[a][b];"
          f"[a]palettegen=max_colors={colors}:stats_mode=diff[p];"
          "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", concat,
                    "-vf", vf, "-loop", "0", out], check=True)
    return os.path.getsize(out)


def encode_video(concat: str, out: str, crf: int) -> int:
    """One muted video at the capture's 2x size (sharp text); the codec follows the extension.
    Every frame is kept at its real timing, padded to an even size for yuv420p."""
    vf = "fps=30,pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p"
    codec = (["-c:v", "libx264", "-preset", "slow", "-tune", "stillimage", "-crf", str(crf),
              "-movflags", "+faststart"] if out.endswith(".mp4") else
             ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(crf + 10), "-row-mt", "1",
              "-deadline", "good", "-cpu-used", "2"])
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", concat,
                    "-vf", vf, "-an", *codec, out], check=True)
    return os.path.getsize(out)


def encode_videos(concat: str, base: str) -> dict:
    """The MP4 and the WebM next to the GIF, each under MAX_VIDEO_BYTES: the quality steps
    down until it fits."""
    sizes = {}
    for ext in ("mp4", "webm"):
        path = f"{base}.{ext}"
        for crf in (24, 28, 32):
            sizes[ext] = encode_video(concat, path, crf)
            if sizes[ext] <= MAX_VIDEO_BYTES:
                break
        else:
            raise AssertionError(f"{os.path.basename(path)}: {sizes[ext]} bytes, over {MAX_VIDEO_BYTES}")
    return sizes


class Demo:
    """One demo's steps on a page (STEPS maps each `do` to its method)."""

    def __init__(self, page: Page, rec: Recorder, token: str, spec: dict):
        self.page, self.rec, self.token, self.spec = page, rec, token, spec
        self.x, self.y = W - 80, H // 2
        self.chat_id: str | None = None

    async def move(self, x: float, y: float, steps: int = 30) -> None:
        await self.page.mouse.move(x, y, steps=steps)
        self.x, self.y = x, y

    async def point_of(self, selector: str) -> tuple[float, float]:
        loc = self.page.locator(selector).first
        await loc.wait_for(state="visible", timeout=60_000)
        box = await loc.bounding_box()
        return box["x"] + min(box["width"] / 2, 60), box["y"] + box["height"] / 2

    async def type(self, step: dict) -> None:
        """Click into the composer, move the cursor off it, and type."""
        x, y = await self.point_of("#chat-input")
        await self.move(x, y)
        await self.page.evaluate("document.querySelector('#chat-input').spellcheck = false")
        await self.page.mouse.click(x, y)
        # Off the text, to empty space at the right: never onto a control (a tooltip).
        await self.move(W - 120, y - 110, steps=12)
        await self.page.keyboard.type(step["text"], delay=TYPE_DELAY_MS)
        await self.page.wait_for_timeout(350)

    async def send(self, step: dict) -> None:
        await self.page.evaluate("__keys(['⏎ Enter'])")
        await self.page.wait_for_timeout(250)
        await self.page.keyboard.press("Enter")
        if self.chat_id is None:
            await self.page.wait_for_function("() => /^\\/c\\/[0-9a-f-]+/.test(location.pathname)",
                                              timeout=60_000, polling=100)
            self.chat_id = (await self.page.evaluate("() => location.pathname")).split("/c/", 1)[1].strip("/")

    async def click(self, step: dict) -> None:
        x, y = await self.point_of(step["selector"])
        await self.move(x, y)
        await self.page.wait_for_timeout(200)
        await self.page.mouse.click(x, y)

    async def wait(self, step: dict) -> None:
        try:
            await self.page.wait_for_function(step["js"], timeout=step.get("timeout_ms", 150_000), polling=100)
        except Exception as ex:
            raise TimeoutError(f"waiting for {step['js'][:120]}: {ex}") from None

    async def wait_reply_done(self, step: dict) -> None:
        last, stable, deadline = "", 0, time.monotonic() + step.get("timeout_ms", 150_000) / 1000
        while stable < 4:
            if time.monotonic() > deadline:
                raise TimeoutError(f"the last reply did not finish: {last[-200:]!r}")
            await self.page.wait_for_timeout(250)
            text, message = await self.page.evaluate(LAST_REPLY)
            stable = stable + 1 if text and text == last and "Thinking" not in message else 0
            last = text

    async def wait_children(self, step: dict) -> None:
        await asyncio.to_thread(capture.wait_children_answering, tuple(step["phrases"]))

    async def park(self, step: dict) -> None:
        """Rest the cursor in empty chat space, where it covers nothing."""
        await self.move(W - 60, 150, steps=20)

    async def pause(self, step: dict) -> None:
        await self.page.wait_for_timeout(step["ms"])

    async def keys(self, step: dict) -> None:
        await self.page.evaluate(f"__keys({json.dumps(step['keys'])}, {int(step.get('ms', 900))})")

    async def record(self, step: dict) -> None:
        self.rec.record(step["on"])

    async def reload(self, step: dict) -> None:
        """Reload the page (the key badge says so first), then wait for the chat."""
        await self.page.evaluate("__keys(['⟳ Reload'], 1200)")
        await self.page.wait_for_timeout(700)
        await self.page.reload()
        await self.page.wait_for_selector("#chat-input", timeout=60_000)
        await self.move(self.x, self.y, steps=1)


STEPS = {"type", "send", "click", "wait", "wait_reply_done", "wait_children", "park", "pause", "keys", "record",
         "reload"}


async def new_page(browser, token: str) -> Page:
    """A desktop page, signed in, light theme, sidebar closed so the chat
    fills the frame, with the overlay in every document."""
    ctx = await browser.new_context(
        viewport={"width": W, "height": H}, device_scale_factor=2, color_scheme="light",
        locale="en-US", timezone_id="UTC",
        storage_state={"cookies": [], "origins": [{"origin": capture.BASE, "localStorage": [
            {"name": "token", "value": token}, {"name": "theme", "value": "light"},
            {"name": "sidebar", "value": "false"}]}]})
    await ctx.add_init_script(OVERLAY)
    return await ctx.new_page()


async def open_new_chat(page: Page) -> None:
    await page.goto(f"{capture.BASE}/?models={capture.MODEL}")
    await page.wait_for_selector("#chat-input", timeout=60_000)
    for label in ("Okay, Let's Go!", "Okay", "Got it"):
        btn = page.get_by_role("button", name=label, exact=True)
        if await btn.count() and await btn.first.is_visible():
            await btn.first.click()
    await page.keyboard.press("Escape")
    # No toasts from other chats: they are never what a demo shows.
    await page.add_style_tag(content='[data-sonner-toaster], section[aria-label^="Notifications"] '
                                     '{ display: none !important; }')


async def run_demo(browser, token: str, spec: dict, out: str, host: str) -> dict:
    page = await new_page(browser, token)
    work = tempfile.mkdtemp(prefix=f"demo-{spec['name']}-")
    old = capture.pipe_valves(token, **spec["pipe_valves"]) if spec.get("pipe_valves") else None
    try:
        await open_new_chat(page)
        rec = Recorder(await page.context.new_cdp_session(page))
        demo = Demo(page, rec, token, spec)
        await demo.move(demo.x, demo.y, steps=1)
        await page.wait_for_timeout(800)
        await rec.start()
        # A demo that starts with recording off begins where it turns it on.
        first = spec["steps"][0] if spec["steps"] else {}
        rec.record(not (first.get("do") == "record" and first.get("on") is False))
        await page.wait_for_timeout(500)
        for step in spec["steps"]:
            if step["do"] not in STEPS:
                raise ValueError(f"{spec['name']}: unknown step {step['do']!r}")
            await getattr(demo, step["do"])(step)
        await rec.stop()
        hits = capture.deny_hits(await page.evaluate("() => document.body.innerText"), (host,) if host else ())
        if hits:
            raise AssertionError(f"{spec['name']}: deny-listed text visible: {sorted(set(hits))[:10]}")
        concat, seconds = rec.write(work)
        if seconds > MAX_SECONDS:
            raise AssertionError(f"{spec['name']}: {seconds:.1f} s, over {MAX_SECONDS:.0f} s")
        path = os.path.join(out, f"{spec['name']}.gif")
        size = encode(concat, path)
        if size > MAX_BYTES:
            size = encode(concat, path, fps=12, colors=96)
        if size > MAX_BYTES:
            raise AssertionError(f"{spec['name']}: {size} bytes, over {MAX_BYTES}")
        videos = encode_videos(concat, os.path.join(out, spec["name"]))
        shutil.rmtree(work)
        return {"name": spec["name"], "path": path, "seconds": round(seconds, 1), "kb": round(size / 1024),
                "mp4_kb": round(videos["mp4"] / 1024), "webm_kb": round(videos["webm"] / 1024),
                "frames": len(rec.frames), "frame_px": png_size(rec.frames[-1][1]), "chat": demo.chat_id}
    except Exception:
        print(f"  frames kept in {work}", flush=True)
        await page.screenshot(path=os.path.join(work, "failed.png"))
        raise
    finally:
        if old is not None:
            capture.pipe_valves(token, **old)
        await page.context.close()


async def main_async(out: str, only: set | None) -> int:
    with open(os.path.join(HERE, "demos.json"), encoding="utf-8") as fh:
        demos = json.load(fh)["demos"]
    os.makedirs(out, exist_ok=True)
    token = capture.signin()
    capture.quiet_background_tasks(token)
    host, failures = socket.gethostname(), []
    if host.lower() in "openclaw":
        host = ""  # a host named like the product would match every page
    async with async_playwright() as pw:
        # The screencast sends frames at CSS-pixel size whatever the context's device scale; this
        # flag makes them 2x (1920x1280), which keeps the videos' text sharp.
        browser = await pw.chromium.launch(args=["--force-device-scale-factor=2"])
        try:
            for spec in demos:
                if only and spec["name"] not in only:
                    continue
                print(f"[{spec['name']}]", flush=True)
                try:
                    print("  " + json.dumps(await run_demo(browser, token, spec, out, host)), flush=True)
                except Exception as ex:
                    failures.append(f"{spec['name']}: {ex}")
                    print(f"  FAILED: {ex}", flush=True)
        finally:
            await browser.close()
    for f in failures:
        print(f"FAILED {f}")
    return 1 if failures else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="docs/img/demos")
    ap.add_argument("--only", help="comma-separated demo names")
    a = ap.parse_args(argv)
    return asyncio.run(main_async(a.out, set(a.only.split(",")) if a.only else None))


if __name__ == "__main__":
    sys.exit(main())
