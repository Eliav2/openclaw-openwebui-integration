"""A stand-in for the live browser view, for the `live-browser` demo.

The demo shows what LIVE_VIEW_URL can do: a reply that uses a browser carries the
browser, live, inside the message. The service the real one points at is a private
one of ours, so the demo stack runs this instead. It does the same three things:

- `/embed` is the page Open WebUI frames inside the reply: a waiting note, then the
  tab as it is drawn, marked LIVE, then "ended" when the browser closes;
- the tab is a real headless Chromium (Playwright), driven through a small local site
  (Peak Atlas, `/site/`): it types a search, opens a result and scrolls the article;
- `/frame.jpg` is that Chromium's screen, a JPEG a few times a second.

The agent's scripted command (scripts/demos/demos.json, "live-browser") waits on
`/done`, so the reply's tool card runs exactly as long as the browser does.

    live = await LiveBrowser(browser).start()   # http://127.0.0.1:18991
    ...
    await live.stop()
"""
from __future__ import annotations

import asyncio
import http.server
import json
import threading
import urllib.parse

PORT = 18991                 # fixed: the scene's command names it
SITE_HOST = "peakatlas.example"   # what the tab list shows; the site itself is local
WAIT_MS = 3000               # the embed's waiting note, before the browser is up
VIEW = (520, 330)            # the browser tab, CSS pixels: small enough that the reply, the frame and the composer all fit
FRAME_EVERY = 0.12           # seconds between screen frames

PEAK = ("Mount Kilimanjaro", "5,895 m (19,341 ft)", "Tanzania")

SITE_CSS = """
body { margin: 0; font: 16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; color: #1c2430; background: #fbfaf7; }
header { display: flex; align-items: center; gap: 10px; padding: 14px 28px; border-bottom: 1px solid #e7e3d8; background: #fff; }
header b { font-size: 20px; letter-spacing: -.01em; } header b span { color: #2f7d5b; }
main { padding: 26px 28px; max-width: 760px; }
h1 { font-size: 28px; margin: 0 0 6px; letter-spacing: -.02em; }
.lead { color: #5b6573; margin: 0 0 22px; }
form { display: flex; gap: 8px; margin: 18px 0; }
input[type=text] { flex: 1; font: inherit; padding: 11px 14px; border: 1px solid #cfd6df; border-radius: 10px; background: #fff; }
button { font: inherit; padding: 11px 18px; border: 0; border-radius: 10px; background: #2f7d5b; color: #fff; }
a.result { display: block; padding: 14px 16px; margin: 0 0 10px; border: 1px solid #e7e3d8; border-radius: 12px; background: #fff;
  color: inherit; text-decoration: none; }
a.result strong { display: block; color: #1d5c42; font-size: 18px; } a.result span { color: #5b6573; font-size: 14px; }
table { border-collapse: collapse; width: 100%; margin: 14px 0 22px; background: #fff; border: 1px solid #e7e3d8; }
td, th { text-align: left; padding: 9px 14px; border-bottom: 1px solid #eeeae0; } th { width: 38%; color: #5b6573; font-weight: 500; }
p { margin: 0 0 14px; }
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><meta charset=utf-8><title>{title}</title><style>{SITE_CSS}</style>"
            f"<header><b>Peak <span>Atlas</span></b></header><main>{body}</main>")


def site(path: str, query: dict) -> str | None:
    """The three pages of the local site."""
    if path in ("/site", "/site/"):
        return _page("Peak Atlas", """<h1>Find a mountain</h1><p class=lead>Heights, ranges and first ascents.</p>
          <form action="/site/search"><input id=q name=q type=text placeholder="Search peaks" autocomplete=off><button>Search</button></form>""")
    if path == "/site/search":
        q = (query.get("q") or [""])[0]
        return _page("Search", f"""<h1>Results for &ldquo;{q}&rdquo;</h1><p class=lead>3 pages</p>
          <a class=result href="/site/peaks/kilimanjaro"><strong>{PEAK[0]}</strong><span>Highest peak in Africa &middot; {PEAK[2]}</span></a>
          <a class=result href="#"><strong>Kilimanjaro National Park</strong><span>Protected area around the mountain</span></a>
          <a class=result href="#"><strong>Routes up Kilimanjaro</strong><span>Seven ways to the summit</span></a>""")
    if path == "/site/peaks/kilimanjaro":
        return _page(PEAK[0], f"""<h1>{PEAK[0]}</h1><p class=lead>A dormant volcano and the highest free-standing mountain in the world.</p>
          <table><tr><th>Height</th><td><b>{PEAK[1]}</b></td></tr><tr><th>Country</th><td>{PEAK[2]}</td></tr>
          <tr><th>Summit</th><td>Uhuru Peak</td></tr><tr><th>First recorded ascent</th><td>1889</td></tr></table>
          <p>Kilimanjaro rises on its own from the plains, with three volcanic cones: Kibo, Mawenzi and Shira.</p>
          <p>Climbers cross five climate zones on the way up, from farmland and rainforest to moorland, alpine desert and an ice cap.</p>
          <p>Most routes take between five and nine days, which leaves time to adjust to the altitude.</p>""")
    return None


EMBED = """<!doctype html><meta charset=utf-8><title>Agent browser</title><style>
 :root { --fg: #15161c; --muted: #6a6d7c; --line: #e6e7ee; --live: #e5484d; --live-soft: #fdecec; }
 * { box-sizing: border-box; }
 body { margin: 0 2px; padding: 2px 0 4px; font: 13px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--fg); }
 #note { color: var(--muted); font-size: 12px; margin: 0 0 4px; }
 #note.wait { display: flex; align-items: center; gap: 10px; font-size: 15px; color: var(--fg); padding: 10px 12px; border: 1px dashed var(--line); border-radius: 8px; }
 #spin { flex: none; width: 16px; height: 16px; border: 2px solid var(--line); border-top-color: #5157f5; border-radius: 50%; animation: s .9s linear infinite; }
 @keyframes s { to { transform: rotate(360deg); } }
 #frame { max-width: 380px; } #screen { display: block; width: 100%; height: auto; border: 1px solid var(--line); border-radius: 8px; }
 #tabs { list-style: none; margin: 4px 0 0; padding: 0; font-size: 12px; color: var(--fg); }
 .live { display: inline-flex; align-items: center; gap: 5px; color: var(--live); background: var(--live-soft); border-radius: 999px;
   padding: 1px 8px 1px 7px; font-size: 11px; font-weight: 700; letter-spacing: .04em; }
 .live::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: p 1.4s ease-in-out infinite; }
 @keyframes p { 50% { opacity: .25; } }
 .ended { border: 1px solid var(--line); border-radius: 4px; padding: 0 5px; font-size: 11px; color: var(--muted); }
 a { color: #5157f5; }
</style>
<div id=note class=wait><span id=spin></span><span id=msg></span></div>
<div id=frame hidden><img id=screen alt="The agent's browser tab, live"></div>
<ul id=tabs></ul>
<script>
const t0 = Date.now(), WAIT = @WAIT@, host = "@HOST@", EVERY = @EVERY@;
const $ = (s) => document.querySelector(s);
const waited = (ms) => Math.floor(ms / 1000) + " s";
const opened = new Date().toISOString().slice(11, 16);
let asked = false, ended = false, n = 0;
const size = () => parent.postMessage({type: "iframe:height", height: document.documentElement.scrollHeight}, "*");
const api = async (p) => (await fetch(p, {cache: "no-store"})).json();
function frame() {
  if (ended) return;
  const im = new Image();
  im.onload = () => { $("#screen").src = im.src; $("#frame").hidden = false; size(); };
  im.src = "/frame.jpg?" + (++n);
  setTimeout(frame, EVERY);
}
async function tick() {
  let s = {phase: "idle", path: "/"};
  if (!asked && Date.now() - t0 >= WAIT) { asked = true; await api("/start"); frame(); }
  if (asked) s = await api("/state");
  if (s.phase === "idle") {
    $("#msg").textContent = "Waiting for the browser… " + waited(Date.now() - t0);
  } else {
    $("#note").className = ""; $("#note").textContent = "Agent browser";
    const badge = s.phase === "done" ? "<span class=ended>ended</span>" : "<span class=live>LIVE</span>";
    $("#tabs").innerHTML = "<li>" + badge + " " + opened + " " + host + s.path + ' <a href="#" onclick="return false">replay</a></li>';
  }
  size();
  if (s.phase === "done") { ended = true; return; }
  setTimeout(tick, 250);
}
tick();
</script>
"""


class LiveBrowser:
    def __init__(self, browser, port: int = PORT):
        self.browser, self.port = browser, port
        self.frame = b""
        self.path = "/"
        self.phase = "idle"
        self.finished = threading.Event()
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: http.server.ThreadingHTTPServer | None = None
        self._ctx = None

    # ── the browser ────────────────────────────────────────────────

    async def _screen(self, page) -> None:
        while self.phase == "live":
            try:
                self.frame = await page.screenshot(type="jpeg", quality=72, scale="css")
            except Exception:
                break
            await asyncio.sleep(FRAME_EVERY)

    async def _browse(self) -> None:
        base = f"http://127.0.0.1:{self.port}"
        self._ctx = await self.browser.new_context(viewport={"width": VIEW[0], "height": VIEW[1]},
                                                   device_scale_factor=1, color_scheme="light")
        page = await self._ctx.new_page()
        await page.goto(f"{base}/site/")
        self.phase = "live"
        shots = asyncio.create_task(self._screen(page))
        try:
            pause = asyncio.sleep
            await pause(1.2)
            await page.click("#q")
            await page.keyboard.type("Kilimanjaro", delay=120)
            self.path = "/search"
            await pause(0.7)
            await page.keyboard.press("Enter")
            await page.wait_for_load_state()
            await pause(1.6)
            await page.click("a.result >> nth=0")
            self.path = "/peaks/kilimanjaro"
            await page.wait_for_load_state()
            await pause(1.4)
            for _ in range(9):
                await page.mouse.wheel(0, 34)
                await pause(0.09)
            await pause(1.8)
        finally:
            self.phase = "done"
            await shots
            self.finished.set()

    def _start(self) -> None:
        if not self._started and self._loop:
            self._started = True
            asyncio.run_coroutine_threadsafe(self._browse(), self._loop)

    # ── the server ─────────────────────────────────────────────────

    def _handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.send_header("cache-control", "no-store")
                self.send_header("access-control-allow-origin", "*")   # the frame's origin is opaque
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(url.query)
                if url.path == "/embed":
                    page = (EMBED.replace("@WAIT@", str(WAIT_MS)).replace("@HOST@", SITE_HOST)
                            .replace("@EVERY@", str(int(FRAME_EVERY * 1000))))
                    return self._send(page.encode(), "text/html; charset=utf-8")
                if url.path == "/start":
                    outer._start()
                    return self._send(b"{}", "application/json")
                if url.path == "/state":
                    return self._send(json.dumps({"phase": outer.phase, "path": outer.path}).encode(), "application/json")
                if url.path == "/frame.jpg":
                    return self._send(outer.frame, "image/jpeg") if outer.frame else self._send(b"", "image/jpeg", 404)
                if url.path == "/done":     # the scripted command waits here until the browser is closed
                    outer.finished.wait(timeout=120)
                    return self._send(f"{PEAK[0]}: height {PEAK[1]}, {PEAK[2]}\n".encode(), "text/plain; charset=utf-8")
                html = site(url.path, q)
                if html is not None:
                    return self._send(html.encode(), "text/html; charset=utf-8")
                self._send(b"not found", "text/plain", 404)

            def log_message(self, *args):
                pass

        return Handler

    async def start(self) -> "LiveBrowser":
        self._loop = asyncio.get_running_loop()
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), self._handler())
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    async def stop(self) -> None:
        self.finished.set()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._ctx:
            await self._ctx.close()

    def embed_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/embed?chat={{chat_id}}&message={{message_id}}"


if __name__ == "__main__":   # a look at the stand-in without the stack: python3 live_browser.py
    from playwright.async_api import async_playwright

    async def main() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            live = await LiveBrowser(browser).start()
            print(f"embed: http://127.0.0.1:{live.port}/embed")
            await asyncio.sleep(float(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 40)
            await live.stop()
            await browser.close()

    asyncio.run(main())
