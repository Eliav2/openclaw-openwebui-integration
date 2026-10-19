#!/usr/bin/env python3
"""Take the README and docs screenshots from a running screenshot stack.

Run by .github/workflows/screenshots.yml after e2e/start_stack.sh has brought
up Open WebUI, the Gateway and the scripted model (scenes.json). Every shot is
a real page in a phone-sized Chromium: real Open WebUI, the installed Pipe and
Action, a real Gateway running real tools against a small sample project.

Each shot waits for its own DOM state, is taken in the
light theme, and fails if its visible text matches the deny-list (words that give
away a test stack, anything with "@", the host name, token-shaped strings).
A failed shot leaves a full-page screenshot and the page HTML under
<out>/debug/ and the run exits non-zero; the other shots still run.

    python3 scripts/screenshots/capture.py --out docs/img [--only hero-tool-cards,...]

Env: OWUI_URL, OWUI_EMAIL, OWUI_PASSWORD (as start_stack.sh exports them).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

from playwright.sync_api import Page, sync_playwright

BASE = os.environ.get("OWUI_URL", "http://localhost:8080").rstrip("/")
MODEL = "openclaw_gateway.default"
PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2,
         "is_mobile": True, "has_touch": True}
DESKTOP = {"viewport": {"width": 1280, "height": 800}, "device_scale_factor": 2}
# A small phone (iPhone SE): the Status dialog outgrows it and scrolls.
SMALL_PHONE = {**PHONE, "viewport": {"width": 375, "height": 667}}
TURN_TIMEOUT_MS = 150_000

DENY = [re.compile(p, re.I) for p in (
    r"\be2e(\b|_)", r"\btest(s|ing)?\b", r"\bfake\b", r"\bdemo\b", r"\blorem\b", r"@",
    r"\b[A-Za-z0-9_\-]{32,}\b",
)]


def deny_hits(text: str, extra: tuple = ()) -> list:
    """Every deny-list match in `text` (the page's visible text)."""
    hits = [m.group(0) for rx in DENY for m in rx.finditer(text)]
    hits += [w for w in extra if w and w.lower() in text.lower()]
    return hits


# ── Open WebUI REST ─────────────────────────────────────────────────

def api(method: str, path: str, body=None, token: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path}: HTTP {exc.code} {exc.read().decode()[:500]}") from None


def signin() -> str:
    got = api("POST", "/api/v1/auths/signin", {
        "email": os.environ["OWUI_EMAIL"], "password": os.environ["OWUI_PASSWORD"]})
    return got["token"]


def quiet_background_tasks(token: str) -> None:
    """Open WebUI's own title, tag and follow-up generation would send their
    prompts through the Pipe to the scripted model; turn them off, and the
    Pipe's auto-title too. Chat titles are set by hand per scene."""
    cfg = api("GET", "/api/v1/tasks/config", token=token)
    for key in ("ENABLE_TITLE_GENERATION", "ENABLE_TAGS_GENERATION",
                "ENABLE_FOLLOW_UP_GENERATION", "ENABLE_AUTOCOMPLETE_GENERATION",
                "ENABLE_SEARCH_QUERY_GENERATION", "ENABLE_RETRIEVAL_QUERY_GENERATION"):
        if key in cfg:
            cfg[key] = False
    api("POST", "/api/v1/tasks/config/update", cfg, token)
    fid = "openclaw_gateway"
    valves = api("GET", f"/api/v1/functions/id/{fid}/valves", token=token) or {}
    valves["AUTO_TITLE"] = False
    api("POST", f"/api/v1/functions/id/{fid}/valves/update", valves, token)


def attach_filter(token: str, model_id: str, filter_id: str) -> None:
    """Attach a Filter to a model the way the README tells a user to
    (Workspace > Models > the model > Filters): through the model's settings,
    which for a Pipe model creates its settings row on first save."""
    try:
        model = api("GET", f"/api/v1/models/model?id={model_id}", token=token)
    except RuntimeError:
        model = None
    if model:
        meta = dict(model.get("meta") or {})
        meta["filterIds"] = sorted(set(meta.get("filterIds") or []) | {filter_id})
        api("POST", f"/api/v1/models/model/update?id={model_id}", {**model, "meta": meta}, token)
    else:
        api("POST", "/api/v1/models/create", {
            "id": model_id, "base_model_id": None, "name": "OpenClaw · Default",
            "meta": {"filterIds": [filter_id]}, "params": {}, "is_active": True}, token)


def pipe_valves(token: str, **changes) -> dict:
    """Set some of the Pipe's valves; returns the values they replaced."""
    fid = "openclaw_gateway"
    valves = api("GET", f"/api/v1/functions/id/{fid}/valves", token=token) or {}
    old = {k: valves.get(k) for k in changes}
    valves.update(changes)
    for k in [k for k, v in valves.items() if v is None]:
        del valves[k]  # a valve that was never set goes back to its default
    api("POST", f"/api/v1/functions/id/{fid}/valves/update", valves, token)
    return old


def chat_title(token: str, chat_id: str) -> str:
    return (api("GET", f"/api/v1/chats/{chat_id}", token=token).get("title") or "")


def set_title(token: str, chat_id: str, title: str) -> None:
    api("POST", f"/api/v1/chats/{chat_id}", {"chat": {"title": title}}, token)


def chat_text(token: str, chat_id: str) -> str:
    got = api("GET", f"/api/v1/chats/{chat_id}", token=token)
    return " ".join((m.get("content") or "") for m in got["chat"]["history"]["messages"].values()
                    if m.get("role") == "assistant" and m.get("done") is True)


def wait_api(token: str, chat_id: str, needle: str, timeout_s: float = 180) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if needle in chat_text(token, chat_id):
            return
        time.sleep(2)
    raise TimeoutError(f"{needle!r} not in a finished reply of {chat_id} after {timeout_s:.0f}s")


def wait_children_answering(phrases: tuple, timeout_s: float = 120) -> None:
    """Until the scripted model has answered each sub-agent's request that
    carries its tool result (step 1 of its scene): read from the fake model's
    request log, the one place that sees the children's turns directly."""
    log = os.environ.get("E2E_FAKE_LLM_LOG", "")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        seen = set()
        try:
            with open(log, encoding="utf-8") as fh:
                for line in fh:
                    rec = json.loads(line)
                    msgs = rec["request"].get("messages") or []
                    # The child's own turn: its user message is the task. The
                    # parent's requests quote the task only inside a tool call.
                    users = json.dumps([m.get("content") for m in msgs if m.get("role") == "user"])
                    if any(m.get("role") == "tool" for m in msgs):
                        seen |= {p for p in phrases if p in users}
        except OSError:
            pass
        if seen == set(phrases):
            return
        time.sleep(1)
    raise TimeoutError(f"sub-agents not answering after {timeout_s:.0f}s: {phrases}")


def dump_children(session_key: str) -> None:
    """Print the timing fields of the parent's child rows as the Status
    Action reads them (sessions.list spawnedBy), for the run log."""
    out = subprocess.run(["openclaw", "gateway", "call", "sessions.list", "--json", "--params",
                          json.dumps({"spawnedBy": session_key, "limit": 50, "includeLastMessage": True})],
                         capture_output=True, text=True, timeout=90)
    try:
        rows = json.loads(out.stdout[out.stdout.find("{"):]).get("sessions") or []
    except ValueError:
        print(f"  sessions.list unreadable: {out.stderr[-500:]}")
        return
    now_ms = int(time.time() * 1000)
    for r in rows:
        fields = {k: r.get(k) for k in ("status", "startedAt", "endedAt", "updatedAt", "runtimeMs",
                                         "hasActiveRun", "subagentRunState")}
        print(f"  child row (now={now_ms}): {json.dumps(fields)}", flush=True)


def dump_child_history_shapes(session_key: str) -> None:
    """Print the shape of each child's chat.history (roles, block types and
    the tool-linking keys, no text), the input of the sub-agent drawer's run."""
    def call(method, params):
        out = subprocess.run(["openclaw", "gateway", "call", method, "--json", "--params", json.dumps(params)],
                             capture_output=True, text=True, timeout=90)
        try:
            return json.loads(out.stdout[out.stdout.find("{"):])
        except ValueError:
            print(f"  {method} unreadable: {out.stderr[-500:]}")
            return {}
    for row in call("sessions.list", {"spawnedBy": session_key, "limit": 50}).get("sessions") or []:
        shapes = []
        for m in call("chat.history", {"sessionKey": row["key"], "limit": 50}).get("messages") or []:
            c = m.get("content")
            blocks = "str" if isinstance(c, str) else [
                {k: (type(v).__name__ if k == "content" else v) for k, v in b.items()
                 if k in ("type", "id", "tool_use_id", "toolCallId", "content")}
                for b in c or [] if isinstance(b, dict)]
            shapes.append({"role": m.get("role"), "blocks": blocks,
                           **{k: m[k] for k in ("toolCallId", "toolName") if k in m}})
        print(f"  child history {row['key'][-12:]}: {json.dumps(shapes)}", flush=True)


_DIGITS = {  # 3x5 bitmap digits for the chart's labels
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111",
    "3": "111001111001111", "4": "101101111001001", "5": "111100111001111",
    "6": "111100111101111", "7": "111001010010010", "8": "111101111101111",
    "9": "111101111001111",
}


def forecast_chart_png(width: int = 600, height: int = 300) -> bytes:
    """A small bar chart of hourly temperatures as a PNG, standard library
    only: the image the media and image-input scenes show. Every third hour
    carries its hour (24 h) under the bar and its temperature above it, so a
    reply that quotes the chart can be checked against it."""
    import struct
    import zlib
    temps = [11, 11, 10, 10, 11, 12, 14, 16, 18, 19, 21, 22, 22, 21, 19, 17, 16, 15, 14, 13, 12, 12, 11, 11]
    rain = {15, 16, 17, 18}
    bg, bar, wet, axis, ink = (255, 255, 255), (59, 130, 246), (147, 197, 253), (209, 213, 219), (75, 85, 99)
    base = height - 40  # the axis line; hour labels sit below it
    slot = (width - 40) // len(temps)
    px = [[bg] * width for _ in range(height)]
    for x in range(20, width - 20):
        px[base][x] = axis
    for i, t in enumerate(temps):
        top = base - t * 9
        for x in range(20 + i * slot, 20 + i * slot + slot - 6):
            for y in range(top, base):
                px[y][x] = wet if i in rain else bar

    def text(label: str, cx: int, y0: int, scale: int = 3) -> None:
        w = (len(label) * 4 - 1) * scale
        x0 = cx - w // 2
        for k, ch in enumerate(label):
            bits = _DIGITS[ch]
            for r in range(5):
                for c in range(3):
                    if bits[r * 3 + c] == "1":
                        for dy in range(scale):
                            for dx in range(scale):
                                px[y0 + r * scale + dy][x0 + (k * 4 + c) * scale + dx] = ink
    for i in range(0, len(temps), 3):
        cx = 20 + i * slot + (slot - 6) // 2
        text(str(i), cx, base + 10)
        text(str(temps[i]), cx, base - temps[i] * 9 - 22)
    rows = [b"\x00" + b"".join(bytes(c) for c in row) for row in px]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def upload_media(name: str, data: bytes) -> None:
    """Put a file where a MEDIA:<name> directive finds it: the Pipe's media
    file server (README, native media delivery)."""
    req = urllib.request.Request(f"http://127.0.0.1:18791/{name}", data=data, method="PUT")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
    if not body.startswith("ok:"):
        raise RuntimeError(f"media upload failed: {body}")


def schedule_wake(session_key: str, message: str, in_s: float) -> str:
    """Add a one-shot cron wake into the session; its job's id."""
    at = (datetime.datetime.now(datetime.timezone.utc)
          + datetime.timedelta(seconds=in_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    # agentId: from OpenClaw 2026.10.10 a job needs its agent named when the
    # Gateway has more than one (main, title-gen).
    params = {"name": "nightly-build-check", "agentId": "main", "schedule": {"kind": "at", "at": at},
              "sessionTarget": f"session:{session_key}",
              "payload": {"kind": "agentTurn", "message": message},
              "delivery": {"mode": "none"}, "deleteAfterRun": True}
    job = gateway_call("cron.add", params)
    return (job.get("job") or job).get("id")


def gateway_call(method: str, params: dict) -> dict:
    out = subprocess.run(["openclaw", "gateway", "call", method, "--json", "--params", json.dumps(params)],
                         capture_output=True, text=True, timeout=90)
    if out.returncode:
        raise RuntimeError(f"{method} failed: {out.stderr[-1500:]}")
    return json.loads(out.stdout[out.stdout.find("{"):])


def gateway_release() -> tuple:
    """(year, month, patch) of the stack's OpenClaw: some shots need a newer one."""
    out = subprocess.run(["openclaw", "--version"], capture_output=True, text=True, timeout=60)
    m = re.search(r"(\d{4})\.(\d{1,2})\.(\d+)", out.stdout)
    return tuple(int(n) for n in m.groups()) if m else (0, 0, 0)


def add_chat_automation(session_key: str) -> str:
    """A daily automation that runs in this chat (ELI-163), next due
    tomorrow morning; its id, to remove it after the scene."""
    job = gateway_call("cron.add", {
        "name": "Morning forecast chart", "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
        "sessionTarget": f"session:{session_key}",
        "payload": {"kind": "agentTurn", "message": "Morning chart for the forecast app"},
        "delivery": {"mode": "none"}})
    return job.get("id") or job.get("job", {}).get("id")


# The sample plugin's claudeCliUsage.status waits this file's milliseconds
# (sample-usage/index.js): the one source a scene holds back.
USAGE_DELAY_FILE = "/tmp/openclaw-screenshot-usage-delay-ms"
STATUS_ROOT = "#openclaw-status-modal-root"
# Each Status dialog section: still loading (its skeleton) or filled.
SECTION_STATES = """() => Object.fromEntries(['context', 'limits', 'background', 'subagents', 'agents'].map(n => {
  const s = document.getElementById('openclaw-status-section-' + n);
  return [n, !s ? 'missing' : s.querySelector(':scope > [data-openclaw-loading]') ? 'loading' : 'filled'];
}))"""
# The dialog's scroll box and where its title bar and footer sit.
DIALOG_LAYOUT = """() => {
  const body = document.getElementById('openclaw-status-sections');
  const panel = body.parentElement, [head, , foot] = panel.children;
  const r = el => { const b = el.getBoundingClientRect(); return [Math.round(b.top), Math.round(b.bottom)]; };
  return {viewport: [innerWidth, innerHeight], panel: r(panel), head: r(head), foot: r(foot),
          scrollHeight: body.scrollHeight, clientHeight: body.clientHeight, scrollTop: Math.round(body.scrollTop)};
}"""


def open_fold(page: Page, name: str) -> None:
    """Tap a folded Status dialog section open."""
    page.locator(f"{STATUS_ROOT} [data-openclaw-fold={name}][data-open=false] [data-openclaw-fold-head]").click()


# ── Page helpers ────────────────────────────────────────────────────

def signed_in_page(browser, token: str, device: dict) -> Page:
    """A page in a new context that starts signed in, in the light theme.
    The token is in the context's storage before any page loads: writing it
    from a loaded /auth page raced that page's own boot, which read the
    token, lost its session request to the next navigation and removed it."""
    ctx = browser.new_context(**device, locale="en-US", timezone_id="UTC", color_scheme="light",
                              storage_state={"cookies": [], "origins": [{"origin": BASE, "localStorage": [
                                  {"name": "token", "value": token}, {"name": "theme", "value": "light"}]}]})
    page = ctx.new_page()
    page.goto(BASE + "/")
    page.wait_for_selector("#chat-input", timeout=60_000)
    dismiss_popups(page)
    return page


def dismiss_popups(page: Page) -> None:
    """The first-login changelog and similar one-time dialogs."""
    for label in ("Okay, Let's Go!", "Okay", "Got it"):
        btn = page.get_by_role("button", name=label, exact=True)
        try:
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=3000)
        except Exception:
            pass  # it closed on its own between the check and the click
    page.keyboard.press("Escape")


def new_chat(page: Page) -> None:
    page.goto(f"{BASE}/?models={MODEL}")
    page.wait_for_selector("#chat-input", timeout=60_000)
    dismiss_popups(page)


def send(page: Page, text: str) -> str:
    """Type `text` in the composer like a person, send it, return the chat id."""
    box = page.locator("#chat-input")
    box.click()
    # The composer keeps a draft across chats; start from an empty box.
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(text, delay=5)
    # On a touch device Enter is a newline, so send the way a phone user does.
    page.locator("#send-message-button").click()
    # Open WebUI moves to /c/<id> with history.replaceState: no navigation event.
    wait_js(page, "() => /^\\/c\\/[0-9a-f-]+/.test(location.pathname)", "the new chat's URL", 60_000)
    return page.evaluate("() => location.pathname").split("/c/", 1)[1].strip("/")


def park_mouse(page: Page) -> None:
    """Move the pointer to empty chat space, so no tooltip covers the shot."""
    page.mouse.move(PHONE["viewport"]["width"] // 2, 700)
    page.wait_for_timeout(300)  # a tooltip fades out


def wait_js(page: Page, js: str, what: str, timeout_ms: int = TURN_TIMEOUT_MS) -> None:
    try:
        page.wait_for_function(js, timeout=timeout_ms, polling=250)
    except Exception as ex:
        raise TimeoutError(f"waiting for {what}: {ex}") from None


LAST_ASSISTANT = "[...document.querySelectorAll('.chat-assistant')].pop()"
# The Send now arrow of a message Open WebUI queued above the composer.
SEND_NOW_ICON = "M4.5 10.5 12 3m0 0 7.5 7.5M12 3v18"
# The Status dialog's sub-agent elapsed cells (the only 10 px spans in it).
ELAPSED_CELLS = """() => [...document.querySelectorAll('#openclaw-status-section-subagents span')]
  .filter(s => s.style.fontSize === '10px').map(s => s.textContent)"""
FIRST_ASSISTANT = "document.querySelector('.chat-assistant')"
REBUILD_SAYS = "Rebuilding the cache from the forecast API."


def opened_card_clip(message_js: str) -> str:
    """Just the message's first card, opened, centred in the viewport and
    below the chat's sticky header (an element shot puts the header over
    its top)."""
    return f"""() => {{
      const btn = {message_js}.querySelector('[role=button]');
      let card = btn;
      while (card.parentElement && !/output/i.test(card.innerText)) card = card.parentElement;
      card.scrollIntoView({{block: 'center'}});
      const r = card.getBoundingClientRect();
      const nav = document.querySelector('nav');
      const top = Math.max(r.top, nav ? nav.getBoundingClientRect().bottom : 0, 0);
      return {{x: 0, y: top, width: innerWidth, height: Math.min(r.bottom + 12, innerHeight) - top}};
    }}"""


CARDS_STATE = f"""() => {{
  const a = {LAST_ASSISTANT}; if (!a) return null;
  return {{done: a.querySelectorAll('[role=button] .text-emerald-500').length,
          active: a.querySelectorAll('[role=button] .shimmer').length}};
}}"""


def last_text_has(*needles: str, lacks: str | None = None) -> str:
    """JS predicate: the last assistant message shows every needle (and not `lacks`)."""
    cond = " && ".join(f"t.includes({json.dumps(n)})" for n in needles)
    if lacks:
        cond += f" && !t.includes({json.dumps(lacks)})"
    return f"() => {{ const a = {LAST_ASSISTANT}; if (!a) return false; const t = a.innerText; return {cond}; }}"


def visible_text(page: Page, selector: str | None = None) -> str:
    """The text a reader can see, with the values of the fields on screen (an
    input's value is not part of innerText, and a token in a valve field is
    still on screen)."""
    root = page.locator(selector).first if selector else page.locator("body")
    return root.evaluate("""el => el.innerText + '\\n' +
        [...el.querySelectorAll('input, textarea')].filter(f => {
          const r = f.getBoundingClientRect();
          return r.bottom > 0 && r.top < innerHeight && r.width > 0;
        }).map(f => f.value).join('\\n')""")


# Where the sub-agent drawer's parts sit against the viewport (ELI-155).
DRAWER_LAYOUT = """() => {
  const root = document.getElementById('openclaw-drawer-root'), panel = root && root.children[1];
  const f = el => { if (!el) return null; const r = el.getBoundingClientRect();
    return {top: Math.round(r.top), height: Math.round(r.height)}; };
  const parts = panel ? [...panel.children] : [];
  const scroller = parts.find(el => /auto|scroll/.test(getComputedStyle(el).overflowY));
  return {viewport: [innerWidth, innerHeight], panel: f(panel), header: f(parts[0]),
          headerLines: parts[0] ? parts[0].innerText.split('\\n').length : null,
          scroller: f(scroller), scrollerIndex: parts.indexOf(scroller),
          scrolls: !!scroller && scroller.scrollHeight > scroller.clientHeight};
}"""


def back_to_dialog(page: Page) -> None:
    """Close the sub-agent drawer: its back button on a phone."""
    page.locator("#openclaw-drawer-root button", has_text="Back").first.click()
    page.wait_for_selector("#openclaw-drawer-root", state="detached", timeout=10_000)


class Shooter:
    def __init__(self, out: str, only: set | None):
        self.out, self.only = out, only
        self.failures: list = []
        self.written: list = []
        self.host = socket.gethostname()
        os.makedirs(os.path.join(out, "debug"), exist_ok=True)

    def wanted(self, name: str) -> bool:
        return not self.only or name in self.only

    def check_text(self, page: Page, name: str, selector: str | None) -> None:
        hits = deny_hits(visible_text(page, selector), (self.host,))
        if hits:
            raise AssertionError(f"{name}: deny-listed text visible: {sorted(set(hits))[:10]}")

    def shot(self, page: Page, name: str, *, selector: str | None = None,
             clip_js: str | None = None, text_selector: str | None = None) -> None:
        """The viewport, one element, or a JS-computed clip, as <name>-light.png."""
        # A toast from another chat's reply is never the subject of a shot.
        page.evaluate("""() => { if (document.getElementById('shot-no-toasts')) return;
          const s = document.createElement('style'); s.id = 'shot-no-toasts';
          s.textContent = '[data-sonner-toaster], section[aria-label^="Notifications"] { display: none !important; }';
          document.head.appendChild(s); }""")
        self.check_text(page, name, text_selector or selector)
        path = os.path.join(self.out, f"{name}-light.png")
        if selector:
            page.locator(selector).first.screenshot(path=path, animations="disabled")
        elif clip_js:
            page.screenshot(path=path, clip=page.evaluate(clip_js), animations="disabled")
        else:
            page.screenshot(path=path, animations="disabled")
        self.written.append(path)
        print(f"  wrote {path}", flush=True)

    def run(self, name: str, fn, *args) -> None:
        if not self.wanted(name):
            return
        print(f"[{name}]", flush=True)
        page = args[0]
        try:
            fn(*args)
        except Exception as ex:
            self.failures.append(f"{name}: {ex}")
            print(f"  FAILED: {ex}", flush=True)
            traceback.print_exc()
            base = os.path.join(self.out, "debug", name)
            try:
                page.screenshot(path=base + ".png", full_page=True)
                with open(base + ".html", "w", encoding="utf-8") as fh:
                    fh.write(page.content())
            except Exception as dex:
                print(f"  (debug capture failed: {dex})")


LIVE_VIEW_PAGE = """<!doctype html><meta charset=utf-8><style>
 body { margin: 0 2px; font: 12px system-ui, sans-serif; color: #6b6b76; }
 .shot { border: 1px solid #e3e3e8; border-radius: 8px; overflow: hidden; color: #1b1b1f; margin: 4px 0; }
 .bar { background: #f1f1f4; padding: 6px 10px; border-bottom: 1px solid #e3e3e8; }
 .page { padding: 12px; }
 h1 { font-size: 15px; margin: 0 0 10px; }
 .panels { display: flex; gap: 8px; }
 .panel { flex: 1; border: 1px solid #e3e3e8; border-radius: 6px; padding: 8px; }
 .panel b { display: block; font-size: 18px; }
 .live { color: #fff; background: #d33; border-radius: 4px; padding: 0 6px; font-weight: 700; font-size: 11px; }
</style>
<div>Agent browser</div>
<div class=shot><div class=bar>localhost:8501</div><div class=page><h1>Forecast dashboard</h1>
 <div class=panels><div class=panel>High<b>24°</b></div><div class=panel>Rain<b>10%</b></div><div class=panel>Wind<b>12 km/h</b></div></div>
</div></div>
<div><span class=live>LIVE</span> localhost:8501 &middot; <a href=#>replay</a></div>
<script>parent.postMessage({type: "iframe:height", height: document.documentElement.scrollHeight}, "*")</script>
"""


def live_view_page():
    """A stand-in for the live view service, on a free local port."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = LIVE_VIEW_PAGE.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ── Scenes ──────────────────────────────────────────────────────────

class Scenes:
    def __init__(self, token: str, shooter: Shooter):
        self.token, self.s = token, shooter
        self.hero_chat: str | None = None
        self.spawn_chat: str | None = None
        self.background_chat: str | None = None

    def hero(self, page: Page) -> None:
        """#1 hero-tool-cards mid-run, then #5 tool-card-expanded once done."""
        new_chat(page)
        self.hero_chat = send(page, "What changed in this repo this week?")
        wait_js(page, f"() => {{ const s = ({CARDS_STATE})(); return s && s.done >= 2 && s.active >= 1; }}",
                "two finished tool cards and one running")
        # Down to just below the running card: the empty chat and the composer
        # under it would take half the README's first screen on a phone.
        clip = f"""() => {{
          const cards = [...{LAST_ASSISTANT}.querySelectorAll('[role=button]')];
          const bottom = cards[cards.length - 1].getBoundingClientRect().bottom + 56;
          return {{x: 0, y: 0, width: innerWidth, height: Math.min(innerHeight, Math.ceil(bottom))}};
        }}"""
        self.s.shot(page, "hero-tool-cards", clip_js=clip)
        wait_js(page, f"() => {{ const a = {LAST_ASSISTANT}; return a && a.innerText.includes('The build is clean'); }}",
                "the final answer")
        set_title(self.token, self.hero_chat, "Repo changes this week")

    def expanded(self, page: Page) -> None:
        page.goto(f"{BASE}/c/{self.hero_chat}")
        wait_js(page, f"() => {{ const s = ({CARDS_STATE})(); return s && s.done >= 3; }}", "three finished cards")
        page.locator(".chat-assistant").last.locator("[role=button]").first.click()
        wait_js(page, f"() => {{ const a = {LAST_ASSISTANT}; return a && /output/i.test(a.innerText) && a.innerText.includes('git log'); }}",
                "the opened card's input and output")
        self.s.shot(page, "tool-card-expanded", clip_js=opened_card_clip(LAST_ASSISTANT),
                    text_selector=".chat-assistant >> nth=-1")

    def composer_status(self, page: Page) -> None:
        page.goto(f"{BASE}/c/{self.hero_chat}")
        btn = page.locator('button[aria-label="OpenClaw status"]')
        btn.wait_for(timeout=60_000)
        clip = """() => {
          const b = document.querySelector('button[aria-label="OpenClaw status"]');
          const f = b.closest('form') || b.parentElement.parentElement;
          const r = f.getBoundingClientRect();
          return {x: Math.max(0, r.x), y: Math.max(0, r.y - 8), width: Math.min(r.width, innerWidth), height: r.height + 16};
        }"""
        self.s.shot(page, "composer-status", clip_js=clip, text_selector="form")

    def question(self, page: Page) -> None:
        new_chat(page)
        chat = send(page, "Can you set up CI for the forecast app?")
        # Open WebUI 0.12 shows the card above the composer, not in the message.
        wait_js(page, "() => { const t = document.body.innerText; return t.includes('Where should the checks run?') && t.includes('Only on my machine') && /recommended/i.test(t); }",
                "the question card with its options")
        set_title(self.token, chat, "CI for the forecast app")
        self.s.shot(page, "question-card")

    def secret_request(self, page: Page) -> None:
        """The masked dialog for a secret the agent requests (OpenClaw 2026.10.10+)."""
        new_chat(page)
        chat = send(page, "Save my weather API key for the forecast app")
        # Open WebUI's SensitiveInput masks with its `password` class until
        # its eye button is pressed.
        wait_js(page, "() => !!document.querySelector('#event-confirm-input.password')",
                "the masked secret dialog")
        set_title(self.token, chat, "Weather API key")
        page.locator("#event-confirm-input").fill("wk-8c1f3a9d0b")
        self.s.shot(page, "secret-request")
        page.get_by_role("button", name="Cancel").click()

    def status_and_drawer(self, page: Page) -> None:
        """#3 status-dialog with two running sub-agents, #4 subagent-drawer."""
        new_chat(page)
        chat = send(page, "Compare hosting for the forecast app: Fly.io or Render?")
        wait_js(page, f"() => {{ const a = {LAST_ASSISTANT}; return a && a.innerText.includes('side by side'); }}",
                "the parent's reply")
        set_title(self.token, chat, "Hosting for the forecast app")
        # Open the dialog once both sub-agents are mid-answer (each has had its
        # tool result back and is streaming prose), as the scripted model saw it.
        wait_children_answering(("Price out Fly.io", "Price out Render"))
        me = api("GET", "/api/v1/auths/", token=self.token)
        dump_children(f"agent:main:openwebui-{me['id']}-{chat}")
        page.locator('button[aria-label="OpenClaw status"]').click()
        root = "#openclaw-status-modal-root"
        wait_js(page, f"""() => {{
          const r = document.querySelector('{root}'); if (!r) return false;
          const t = r.innerText;  // section headers are uppercased by CSS
          return /subagents \\(2\\)/i.test(t) && t.includes('Fly.io') && t.includes('Render')
            && /5h/.test(t) && /week/i.test(t);
        }}""", "the status dialog with two sub-agents and rate limits", 60_000)
        self.s.shot(page, "status-dialog", text_selector=root)
        opened_elapsed = page.evaluate(ELAPSED_CELLS)
        opened_at = time.monotonic()
        page.locator(f"{root} button", has_text="Fly.io").first.click()
        page.wait_for_selector("#openclaw-drawer-root", timeout=30_000)
        wait_js(page, """() => { const d = document.querySelector('#openclaw-drawer-root');
                 return d && d.innerText.includes('Fly.io') && /running/i.test(d.innerText); }""",
                "the drawer's header", 30_000)
        # As it opens, before anything else is clicked: where the drawer sits
        # relative to the Status dialog is what this shot is for.
        self.s.shot(page, "subagent-drawer-open")
        z = page.evaluate("""() => ({
          drawer: getComputedStyle(document.querySelector('#openclaw-drawer-root')).zIndex,
          dialog: getComputedStyle(document.querySelector('#openclaw-status-modal-root')).zIndex})""")
        print(f"  computed z-index: {z}", flush=True)
        if z["drawer"] == "auto" or (z["dialog"] != "auto" and int(z["drawer"]) <= int(z["dialog"])):
            raise AssertionError(f"the sub-agent drawer opens behind the status dialog (computed z-index {z})")
        # Its run as a read-only chat: the child's own words, its read call
        # with its result back, and the step it is on now.
        wait_js(page, """() => { const s = document.getElementById('openclaw-drawer-stream');
                 return s && s.querySelector('[data-openclaw-card]') && s.querySelector('[data-openclaw-note=working]')
                   && s.innerText.includes("start from the app's README"); }""",
                "the drawer's run so far", 60_000)
        dump_child_history_shapes(f"agent:main:openwebui-{me['id']}-{chat}")
        page.locator("#openclaw-drawer-stream [data-openclaw-card-button]").first.click()
        wait_js(page, """() => /output/i.test(document.getElementById('openclaw-drawer-stream').innerText)""",
                "the opened card's output", 10_000)
        print("  drawer layout: " + json.dumps(page.evaluate(DRAWER_LAYOUT)), flush=True)
        print("  drawer run: " + json.dumps(page.locator("#openclaw-drawer-stream").inner_text()[:600]), flush=True)
        self.s.shot(page, "subagent-drawer", text_selector="#openclaw-drawer-root")
        # Back to the dialog, a while after it opened: a running row's elapsed
        # time has moved on from what the dialog first showed (ELI-120).
        back_to_dialog(page)
        # Ten seconds after it opened, so the change reads at a glance.
        page.wait_for_timeout(max(0, 10_000 - (time.monotonic() - opened_at) * 1000))
        print(f"  elapsed at open {opened_elapsed}, now {page.evaluate(ELAPSED_CELLS)}", flush=True)
        self.s.shot(page, "status-dialog-later", text_selector=root)

    def subagents_long(self, page: Page) -> None:
        """Two finished sub-agents: one labelled with a 1,250-character task,
        one with no label (ELI-155)."""
        new_chat(page)
        chat = send(page, "Benchmark the forecasters against our baseline")
        wait_js(page, last_text_has("Two sub-agents are on it"), "the parent's reply")
        set_title(self.token, chat, "Forecaster benchmark")
        # The parent's replies to each result: both sub-agents have ended.
        wait_api(self.token, chat, "The benchmark is back", 240)
        wait_api(self.token, chat, "only station 4 changed", 240)
        page.locator('button[aria-label="OpenClaw status"]').click()
        root = STATUS_ROOT
        # Nothing runs, so every section opens folded (ELI-163).
        wait_js(page, f"""() => {{ const st = ({SECTION_STATES})(); const r = document.querySelector('{root}');
          return Object.values(st).every(v => v === 'filled') && /subagents \\(2\\)/i.test(r.innerText)
            && !r.querySelector('[data-openclaw-fold][data-open=true]'); }}""",
                "the status dialog, every section folded", 60_000)
        self.s.shot(page, "status-dialog-folded", text_selector=root)
        # The first tap's request is dropped, as a phone losing its connection
        # drops it: the section says so and offers Retry, which loads the rows.
        dropped = []

        def drop_once(route):
            if not dropped and "subagent-rows" in (route.request.post_data or ""):
                dropped.append(1)
                route.abort("internetdisconnected")
            else:
                route.continue_()
        page.route("**/api/chat/actions/**", drop_once)
        open_fold(page, "subagents")
        wait_js(page, """() => /could not load the sub-agents/i.test(
          (document.getElementById('openclaw-status-section-subagents') || {}).innerText || '')""",
                "the failed tap's message", 30_000)
        self.s.shot(page, "status-subagents-retry", selector="#openclaw-status-section-subagents")
        page.unroute("**/api/chat/actions/**")
        retry = page.locator("#openclaw-status-section-subagents button", has_text="Retry")
        if retry.count():
            retry.click()
        wait_js(page, f"""() => {{ const r = document.querySelector('{root}'); if (!r) return false;
          return /noise table/i.test(r.innerText) && /forecast/i.test(r.innerText); }}""",
                "the unfolded sub-agents with their tasks", 60_000)
        rows = f"{root} #openclaw-status-section-subagents [data-openclaw-row]"
        print("  dialog rows: " + json.dumps(page.evaluate(f"""() => [...document.querySelectorAll('{rows}')]
          .map(b => ({{h: Math.round(b.getBoundingClientRect().height), chars: b.innerText.length}}))""")), flush=True)
        self.s.shot(page, "subagents-long-dialog", text_selector=root)
        page.locator(rows).filter(has_text=re.compile("Forecaster benchmark|pre-registered baseline")).first.click()
        page.wait_for_selector("#openclaw-drawer-root", timeout=30_000)
        wait_js(page, "() => /PR #12/.test((document.getElementById('openclaw-drawer-root') || {}).innerText || '')",
                "the drawer filled", 60_000)
        print("  drawer layout: " + json.dumps(page.evaluate(DRAWER_LAYOUT)), flush=True)
        self.s.shot(page, "subagents-long-drawer", text_selector="#openclaw-drawer-root")
        page.locator("#openclaw-drawer-root [data-openclaw-more=task]").click()
        self.s.shot(page, "subagents-long-task", text_selector="#openclaw-drawer-root")
        back_to_dialog(page)
        page.locator(rows).filter(has_text="noise table").first.click()
        page.wait_for_selector("#openclaw-drawer-root", timeout=30_000)
        wait_js(page, "() => /station 4/.test((document.getElementById('openclaw-drawer-root') || {}).innerText || '')",
                "the unlabelled sub-agent's drawer", 60_000)
        print("  drawer layout: " + json.dumps(page.evaluate(DRAWER_LAYOUT)), flush=True)
        self.s.shot(page, "subagents-unlabelled-drawer", text_selector="#openclaw-drawer-root")
        back_to_dialog(page)
        page.keyboard.press("Escape")

    def background(self, page: Page) -> None:
        """ELI-163: work left running in this chat. #1 the dialog with one
        source held back (the sample rate limits), every other section
        already filled; #2 Background unfolded with the four imports and the
        chat's automation, the other sections opened."""
        new_chat(page)
        chat = send(page, "Import the station data for all four regions.")
        wait_js(page, last_text_has("All four imports are running"), "the reply")
        set_title(self.token, chat, "Station data import")
        me = api("GET", "/api/v1/auths/", token=self.token)
        job = add_chat_automation(f"agent:main:openwebui-{me['id']}-{chat}")
        root = STATUS_ROOT
        try:
            with open(USAGE_DELAY_FILE, "w", encoding="utf-8") as fh:
                # Under the Action's 10 s timeout for this request.
                fh.write("6000")
            try:
                opened = time.monotonic()
                page.locator('button[aria-label="OpenClaw status"]').click()
                wait_js(page, f"""() => {{ const st = ({SECTION_STATES})();
                  return st.limits === 'loading' && ['context', 'background', 'subagents', 'agents'].every(n => st[n] === 'filled')
                    && /import-stations/.test(document.querySelector('{root}').innerText); }}""",
                        "every section but Rate limits filled", 20_000)
                print(f"  {time.monotonic() - opened:.1f}s after the click: " + json.dumps(page.evaluate(SECTION_STATES)),
                      flush=True)
                self.s.shot(page, "status-dialog-loading", text_selector=root)
            finally:
                os.remove(USAGE_DELAY_FILE)
            wait_js(page, f"() => ({SECTION_STATES})().limits === 'filled' && /week/i.test(document.querySelector('{root}').innerText)",
                    "Rate limits, once its source answers", 40_000)
            print(f"  Rate limits filled {time.monotonic() - opened:.1f}s after the click", flush=True)
            open_fold(page, "subagents")
            open_fold(page, "agents")
            wait_js(page, f"""() => {{ const t = document.querySelector('{root}').innerText;
              return /background \\(5\\)/i.test(t) && /Morning forecast chart/.test(t) && /no sub-agents/i.test(t); }}""",
                    "Background with the imports and the automation, every section open", 30_000)
            print("  background: " + json.dumps(page.locator("#openclaw-status-section-background").inner_text()[:600]),
                  flush=True)
            # Opening the last section scrolled the dialog down; back to Background.
            page.evaluate("() => document.getElementById('openclaw-status-section-background').scrollIntoView({block: 'start'})")
            page.wait_for_timeout(300)
            self.s.shot(page, "status-dialog-background", text_selector=root)
            page.keyboard.press("Escape")
        finally:
            gateway_call("cron.remove", {"id": job})
        self.background_chat = chat

    def background_scrolled(self, page: Page) -> None:
        """#3 the same dialog, every section open, on a small phone: it is
        taller than the screen, so its sections scroll between its title bar
        and its footer."""
        page.goto(f"{BASE}/c/{self.background_chat}")
        page.wait_for_selector("#chat-input", timeout=60_000)
        page.locator('button[aria-label="OpenClaw status"]').click()
        root = STATUS_ROOT
        wait_js(page, f"() => Object.values(({SECTION_STATES})()).every(v => v === 'filled')", "every section filled", 60_000)
        open_fold(page, "subagents")
        open_fold(page, "agents")
        page.evaluate("() => { document.getElementById('openclaw-status-sections').scrollTop = 1e6; }")
        page.wait_for_timeout(300)
        layout = page.evaluate(DIALOG_LAYOUT)
        print("  dialog layout: " + json.dumps(layout), flush=True)
        if layout["scrollHeight"] <= layout["clientHeight"]:
            raise AssertionError(f"the dialog fits the screen, nothing scrolls: {layout}")
        if layout["head"][0] < 0 or layout["foot"][1] > layout["viewport"][1]:
            raise AssertionError(f"the title bar or the footer is off the screen: {layout}")
        self.s.shot(page, "status-dialog-scrolled", text_selector=root)
        page.keyboard.press("Escape")

    def background_agent(self, page: Page) -> None:
        """#4 a reply a background agent holds open says so in its status
        row (the sample Agent tool answers as Claude Code's does)."""
        new_chat(page)
        chat = send(page, "Look into the station vendors for me.")
        # A few words into the reply's text, the background agent still out.
        wait_js(page, f"""() => {{ const a = {LAST_ASSISTANT}; if (!a) return false; const t = a.innerText;
                  return t.includes('Waiting on 1 background agent') && t.includes('the same sensor'); }}""",
                "the waiting line", 90_000)
        set_title(self.token, chat, "Station vendors")
        print("  reply: " + json.dumps(page.evaluate(f"() => {LAST_ASSISTANT}.innerText")[:300]), flush=True)
        clip = f"""() => {{
          const a = {LAST_ASSISTANT};
          a.scrollIntoView({{block: 'end'}});
          const nav = document.querySelector('nav');
          const top = Math.max(a.getBoundingClientRect().top - 36, nav ? nav.getBoundingClientRect().bottom : 0);
          const box = document.getElementById('message-input-container');
          const bottom = Math.min(a.getBoundingClientRect().bottom + 8, box ? box.getBoundingClientRect().top : innerHeight);
          return {{x: 0, y: top, width: innerWidth, height: bottom - top}};
        }}"""
        self.s.shot(page, "background-agent-waiting", clip_js=clip, text_selector=".chat-assistant >> nth=-1")
        # The reply ends before the next scene starts.
        wait_js(page, last_text_has("when it is in"), "the reply's end", 90_000)

    def model_picker(self, page: Page) -> None:
        new_chat(page)
        page.locator("button[id^=model-selector-][id$=-button], button[aria-label*='model' i]").first.click()
        wait_js(page, "() => document.body.innerText.includes('Claude Sonnet 5.5')", "the open model list", 30_000)
        self.s.shot(page, "model-picker")
        page.keyboard.press("Escape")

    def thinking(self, page: Page) -> None:
        attach_filter(self.token, MODEL, "openclaw_thinking")
        # A new chat: over a long one the menu shares the screen with either
        # the first message under the translucent header (scrolled down) or
        # the jump-to-bottom arrow on the reply's toolbar (scrolled up).
        new_chat(page)
        page.locator("button[aria-label='Integrations']").first.click()
        wait_js(page, "() => /thinking/i.test(document.body.innerText)", "the thinking toggle in the menu", 30_000)
        self.s.shot(page, "thinking-toggle")
        page.keyboard.press("Escape")

    def agent_turn(self, page: Page) -> None:
        new_chat(page)
        chat = send(page, "Can you keep an eye on the nightly build?")
        wait_js(page, f"() => {{ const a = {LAST_ASSISTANT}; return a && a.innerText.includes('only write when'); }}",
                "the first reply")
        set_title(self.token, chat, "Overnight build check")
        me = api("GET", "/api/v1/auths/", token=self.token)
        schedule_wake(f"agent:main:openwebui-{me['id']}-{chat}", "Nightly build check for the forecast app", 5)
        # The tab stays open on the chat: the cron reply has to arrive in it
        # live, with no reload (agent turns, "open tabs update live").
        wait_js(page, f"() => {{ const a = {LAST_ASSISTANT}; return a && a.innerText.includes('nothing needs you today'); }}",
                "the cron reply arriving in the open tab")
        self.s.shot(page, "agent-turn")

    def media_turn(self, page: Page) -> None:
        """A cron reply with a MEDIA image followed by text. Replies the agent
        starts are finalized by the Pipe's turn writer, not the inline path."""
        new_chat(page)
        chat = send(page, "Can you send me the hourly forecast chart every morning?")
        wait_js(page, f"() => {{ const a = {LAST_ASSISTANT}; return a && a.innerText.includes('before you leave'); }}",
                "the first reply")
        set_title(self.token, chat, "Morning forecast chart")
        upload_media("hourly-forecast.png", forecast_chart_png())
        me = api("GET", "/api/v1/auths/", token=self.token)
        schedule_wake(f"agent:main:openwebui-{me['id']}-{chat}", "Morning chart for the forecast app", 5)
        wait_api(self.token, chat, "bike out this morning")
        page.goto(f"{BASE}/c/{chat}")
        wait_js(page, f"""() => {{ const a = {LAST_ASSISTANT};
                 return a && a.innerText.includes('bike out this morning')
                   && [...a.querySelectorAll('img')].some(i => i.complete && i.naturalWidth > 0); }}""",
                "the cron reply with its chart loaded")
        self.s.shot(page, "media-turn")
        # The saved markdown, as the message editor shows it: where the text
        # after the directive ended up is easier to see here than rendered.
        # The toolbar is a sibling of .chat-assistant, not inside it.
        page.locator('button[aria-label="Edit"]').last.click()
        wait_js(page, """() => [...document.querySelectorAll('textarea')].some(t => t.value.includes('bike out'))""",
                "the reply's markdown in the editor", 30_000)
        self.s.shot(page, "media-turn-source")
        page.keyboard.press("Escape")

    def first_reply(self, page: Page) -> None:
        """The first hello after install, done: what the Quick start's last
        step should look like."""
        new_chat(page)
        chat = send(page, "Hello! Are you there?")
        wait_js(page, f"""() => {{ const a = {LAST_ASSISTANT};
                 return a && a.innerText.includes('What should we start with')
                   && document.querySelectorAll('button[aria-label="Copy"]').length > 0; }}""",
                "the finished first reply")
        set_title(self.token, chat, "Hello")
        # Down to the reply's toolbar: the empty chat under it says nothing.
        clip = """() => {
          const copy = [...document.querySelectorAll('button[aria-label="Copy"]')].pop();
          const bottom = copy.getBoundingClientRect().bottom + 24;
          return {x: 0, y: 0, width: innerWidth, height: Math.min(innerHeight, Math.ceil(bottom))};
        }"""
        self.s.shot(page, "first-reply", clip_js=clip)

    def live_view(self, page: Page) -> None:
        """LIVE_VIEW_URL: a reply that uses a browser gets the live view of it
        as an embed. A small local page stands in for the service that shows
        the agent's browser (browser-replay serves the real one)."""
        server = live_view_page()
        old = pipe_valves(self.token, LIVE_VIEW_URL=f"http://127.0.0.1:{server.server_port}/embed"
                                                     "?chat={chat_id}&message={message_id}")
        try:
            new_chat(page)
            chat = send(page, "Check the forecast dashboard in a browser, please.")
            wait_js(page, f"""() => {{ const a = {LAST_ASSISTANT};
                     return a && a.innerText.includes('all three panels') && !a.innerText.includes('Thinking')
                       && document.querySelectorAll('button[aria-label="Copy"]').length > 0; }}""", "the finished reply")
            frame = f"{LAST_ASSISTANT}.querySelector('iframe')"
            wait_js(page, f"() => {{ const f = {frame}; return f && f.getBoundingClientRect().height > 150; }}",
                    "the live view in the reply, at its own height")
            src = page.evaluate(f"() => {frame}.src")
            if f"chat={chat}" not in src:
                raise RuntimeError(f"the embed is not this chat's: {src}")
            set_title(self.token, chat, "Dashboard check")
            page.evaluate(f"() => {frame}.scrollIntoView({{block: 'center'}})")
            self.s.shot(page, "live-view")
        finally:
            pipe_valves(self.token, **old)
            server.shutdown()

    def failed_tool_live(self, page: Page) -> None:
        """A tool that failed, shown while the reply is still streaming: a
        `read` of a file that isn't there, then a long `exec` still running."""
        new_chat(page)
        chat = send(page, "Can you redeploy the forecast app to staging?")
        wait_js(page, f"() => {{ const s = ({CARDS_STATE})(); return s && s.done >= 1 && s.active >= 1; }}",
                "one finished card and one running")
        rows = page.evaluate(f"() => [...{LAST_ASSISTANT}.querySelectorAll('[role=button]')].map(b => b.innerText)")
        print(f"  card rows: {json.dumps(rows)}", flush=True)
        set_title(self.token, chat, "Deploy to staging")
        self.s.shot(page, "failed-tool-live")

    def streaming_and_reload(self, page: Page) -> None:
        """A long reply mid-stream, then the same reply after a page reload
        while it is still being written: the answer so far is still there."""
        new_chat(page)
        chat = send(page, "Draft the release notes for the hourly view.")
        set_title(self.token, chat, "Release notes draft")
        wait_js(page, last_text_has("Hourly forecast", "Tap an hour", lacks="Faster startup"),
                "the reply part-way through")
        self.s.shot(page, "streaming")
        page.reload()
        page.wait_for_selector("#chat-input", timeout=60_000)
        wait_js(page, last_text_has("Hourly forecast", "Faster startup", lacks="app store"),
                "the partial answer after the reload, still growing")
        self.s.shot(page, "reload-mid-reply")
        wait_js(page, last_text_has("app store"), "the finished reply")

    def usage_goal(self, page: Page) -> None:
        """The status rows under a reply: context, the rate-limit window, and
        the session's goal, which the agent set with its create_goal tool."""
        new_chat(page)
        chat = send(page, "Keep at it until the hourly view is on staging.")
        wait_js(page, last_text_has("Goal set"), "the reply that sets the goal")
        send(page, "Start with the build.")
        wait_js(page, last_text_has("Next is the staging deploy script"), "the reply")
        set_title(self.token, chat, "Hourly view to staging")
        page.locator(".chat-assistant").last.get_by_text(re.compile(r"\d+(\.\d+)?[km]/\d+(\.\d+)?[km]")).first.click()
        wait_js(page, "() => document.body.innerText.includes('🎯')", "the goal row", 60_000)
        print("  status rows: " + json.dumps(page.evaluate(f"() => {LAST_ASSISTANT}.innerText")[:400]), flush=True)
        clip = f"""() => {{
          // From the model's name over the first status row (the 🧠 line) to the end of the reply.
          const a = {LAST_ASSISTANT};
          const w = document.createTreeWalker(a, NodeFilter.SHOW_TEXT);
          let row = null;
          while (w.nextNode()) {{ if (w.currentNode.textContent.includes('🧠')) {{ row = w.currentNode.parentElement; break; }} }}
          row.scrollIntoView({{block: 'center'}});
          const nav = document.querySelector('nav');
          const navBottom = nav ? nav.getBoundingClientRect().bottom : 0;
          const r = a.getBoundingClientRect();
          const top = Math.max(row.getBoundingClientRect().top - 36, navBottom);  // with the model's name
          return {{x: 0, y: top, width: innerWidth, height: Math.min(r.bottom + 8, innerHeight) - top}};
        }}"""
        self.s.shot(page, "usage-goal", clip_js=clip, text_selector=".chat-assistant >> nth=-1")

    def compact(self, page: Page) -> None:
        """The Status dialog's context section with its Compact pill, on the
        hero chat (three tool calls of history). No "after" shot: right after
        a compact the Gateway's number counts the kept transcript only, so it
        reads far lower than what the next request really carries."""
        page.goto(f"{BASE}/c/{self.hero_chat}")
        page.locator('button[aria-label="OpenClaw status"]').click()
        sec = "#openclaw-status-section-context"
        wait_js(page, f"() => {{ const s = document.querySelector('{sec}'); return s && /\\d+(\\.\\d+)?[km]?\\s*\\/\\s*\\d+(\\.\\d+)?[km]\\b/i.test(s.innerText); }}",
                "the context numbers", 60_000)
        print("  context: " + json.dumps(page.locator(sec).inner_text()), flush=True)
        self.s.shot(page, "compact-before", selector=sec)
        page.keyboard.press("Escape")

    def subagent_result(self, page: Page) -> None:
        """A sub-agent finishes while its parent is idle: the parent's reply
        to the result lands in the chat as a normal reply."""
        new_chat(page)
        chat = send(page, "Can you find out when the forecast cache came in and how it works?")
        wait_js(page, last_text_has("in the background"), "the parent's first reply")
        set_title(self.token, chat, "Forecast cache history")
        wait_js(page, last_text_has("helper is back", "starts it cold"), "the parent's reply to the result")
        clip = """() => {
          const box = document.getElementById('message-input-container');
          const bottom = box ? box.getBoundingClientRect().top - 8 : innerHeight;
          return {x: 0, y: 0, width: innerWidth, height: Math.min(bottom, innerHeight)};
        }"""
        self.s.shot(page, "subagent-result", clip_js=clip)
        self.spawn_chat = chat

    def spawn_card(self, page: Page) -> None:
        """The parent's spawn card opened: named after the sub-agent, the
        head of its task as input, one line saying it started as output."""
        page.goto(f"{BASE}/c/{self.spawn_chat}")
        wait_js(page, f"() => {{ const a = {FIRST_ASSISTANT}; return a && a.querySelector('[role=button] .text-emerald-500'); }}",
                "the finished spawn card")
        page.locator(".chat-assistant").first.locator("[role=button]").first.click()
        wait_js(page, f"() => {{ const a = {FIRST_ASSISTANT}; return a && /output/i.test(a.innerText) && a.innerText.includes('git log'); }}",
                "the opened card's input and output")
        self.s.shot(page, "spawn-card", clip_js=opened_card_clip(FIRST_ASSISTANT),
                    text_selector=".chat-assistant >> nth=0")

    def error_message(self, page: Page) -> None:
        """An actionable error: AGENT_ID names an agent the Gateway lacks."""
        old = pipe_valves(self.token, AGENT_ID="research")
        try:
            new_chat(page)
            chat = send(page, "Can you check the staging logs?")
            wait_js(page, last_text_has('"research"'), "the error reply")
            set_title(self.token, chat, "Staging logs")
            self.s.shot(page, "error-message")
        finally:
            pipe_valves(self.token, **old)

    def chat_titles(self, page: Page) -> None:
        """Two messages in one chat; the Pipe's auto-title names it after the
        second, and the phone header shows the title."""
        old = pipe_valves(self.token, AUTO_TITLE=True)
        tasks = api("GET", "/api/v1/tasks/config", token=self.token)
        api("POST", "/api/v1/tasks/config/update", {**tasks, "ENABLE_FOLLOW_UP_GENERATION": True}, self.token)
        try:
            new_chat(page)
            chat = send(page, "Help me plan the hourly view release.")
            wait_js(page, last_text_has("What's left"), "the first reply")
            send(page, "What should go in the release notes?")
            wait_js(page, last_text_has("Lead with the hourly strip"), "the second reply")
            deadline = time.time() + 120
            while "Hourly view release plan" not in chat_title(self.token, chat):
                if time.time() > deadline:
                    raise TimeoutError(f"no auto title, still {chat_title(self.token, chat)!r}")
                time.sleep(2)
        finally:
            pipe_valves(self.token, **old)
            api("POST", "/api/v1/tasks/config/update", tasks, self.token)
        page.goto(f"{BASE}/c/{chat}")
        wait_js(page, "() => document.body.innerText.includes('Hourly view release plan')", "the title on screen", 30_000)
        self.s.shot(page, "chat-titles")

    def stop(self, page: Page) -> None:
        """Stop while a long tool is still running: the run ends and the
        running card is closed, not left spinning."""
        new_chat(page)
        chat = send(page, "Run all the checks for the forecast app.")
        wait_js(page, f"() => {{ const s = ({CARDS_STATE})(); return s && s.active >= 1; }}", "the running checks card")
        set_title(self.token, chat, "All checks")
        page.wait_for_timeout(3000)  # a few seconds into the run, as a person would
        page.locator('button[aria-label="Stop"]').click()
        wait_js(page, f"""() => {{ const a = {LAST_ASSISTANT}; const s = ({CARDS_STATE})();
                 const m = a && a.closest('[id^=message-]');
                 return s && s.active === 0 && m && m.querySelector('button[aria-label="Copy"]'); }}""",
                "the stopped reply", 60_000)
        rows = page.evaluate(f"() => [...{LAST_ASSISTANT}.querySelectorAll('[role=button]')].map(b => b.innerText)")
        print(f"  card rows after stop: {json.dumps(rows)}", flush=True)
        park_mouse(page)
        self.s.shot(page, "stop")

    def _send_while_busy(self, page: Page, title: str) -> str:
        new_chat(page)
        chat = send(page, "Rebuild the forecast cache.")
        wait_js(page, f"() => {{ const s = ({CARDS_STATE})(); return s && s.active >= 1; }}", "the running rebuild card")
        set_title(self.token, chat, title)
        # A phone composer sends with the button, not Enter.
        page.locator("#chat-input").click()
        page.keyboard.type("Which branch is this on?", delay=5)
        page.locator("#send-message-button").click()
        return chat

    def _both_answered(self, page: Page) -> None:
        """The rebuild ran to its end in its own reply (nothing aborted it,
        and it did not move to the next reply), then the new message got
        its own answer."""
        wait_js(page, """() => { const a = [...document.querySelectorAll('.chat-assistant')];
                 return a.length >= 2 && a[0].innerText.includes('The cache is rebuilt')
                   && a[a.length - 1].innerText.includes("It's on main")
                   && !a[a.length - 1].innerText.includes('The cache is rebuilt'); }""",
                "the rebuild finished in its own reply, and the new message's own answer")
        wait_js(page, f"() => {{ const m = {LAST_ASSISTANT}.closest('[id^=message-]'); return m && m.querySelector('button[aria-label=\"Copy\"]'); }}",
                "the answer finished", 60_000)

    def _both_persisted(self, page: Page) -> None:
        """What the page shows is also what Open WebUI stored: the rebuild's
        reply and the new message's answer, each finished, in its own
        message (ELI-166: 0.12.3 changed the first turn's save and the
        response stream these replies are written through)."""
        chat_id = page.url.rstrip("/").rsplit("/c/", 1)[-1]
        deadline, last = time.time() + 60, None
        while time.time() < deadline:
            msgs = api("GET", f"/api/v1/chats/{chat_id}", token=self.token)["chat"]["history"]["messages"]
            replies = sorted((m for m in msgs.values() if m.get("role") == "assistant"),
                             key=lambda m: m.get("timestamp") or 0)
            last = [(m.get("done"), (m.get("content") or "")[-60:]) for m in replies]
            if (len(replies) >= 2 and all(m.get("done") is True for m in replies)
                    and "The cache is rebuilt" in (replies[0].get("content") or "")
                    and "It's on main" in (replies[-1].get("content") or "")
                    and "The cache is rebuilt" not in (replies[-1].get("content") or "")):
                return
            time.sleep(1)
        raise RuntimeError(f"stored replies of {chat_id} do not match the page: {last}")

    def sent_mid_run(self, page: Page) -> None:
        """A message sent while a tool runs (AUTO_SEND_WHILE_RUNNING, on by
        default): it is in the chat at once, with no queue to click, while
        the rebuild is still running; then both are answered."""
        self._send_while_busy(page, "Cache rebuild")
        # The rebuild's card is still running in its own reply (ELI-158: not
        # marked done, not moved to the next reply).
        wait_js(page, f"""() => [...document.querySelectorAll('.user-message')]
                   .some(m => m.innerText.includes('Which branch is this on?'))
                 && !document.querySelector('button path[d="{SEND_NOW_ICON}"]')
                 && !document.body.innerText.includes('The cache is rebuilt')
                 && {FIRST_ASSISTANT}.querySelectorAll('[role=button] .shimmer').length >= 1
                 && [...document.querySelectorAll('.chat-assistant')].pop().innerText.includes('Queued')""",
                "the new message in the chat while the rebuild's card still runs", 30_000)
        park_mouse(page)
        self.s.shot(page, "message-sent-mid-run")
        # The kept run's text shows once (ELI-158: the writer's takeover once
        # spliced in a second copy).
        said = page.evaluate(f"() => {FIRST_ASSISTANT}.innerText.split('{REBUILD_SAYS}').length - 1")
        if said != 1:
            raise RuntimeError(f"the rebuild's text shows {said} times in its reply")
        self._both_answered(page)
        self._both_persisted(page)

    def send_now(self, page: Page) -> None:
        """AUTO_SEND_WHILE_RUNNING off: Open WebUI queues the message; Send
        now on it lets the running reply finish, then the new message gets
        its own answer."""
        old = pipe_valves(self.token, AUTO_SEND_WHILE_RUNNING=False)
        try:
            self._send_while_busy(page, "Cache rebuild, send now")
            page.locator('button[aria-label="Send now"]').first.wait_for(timeout=30_000)
            page.locator('button[aria-label="Send now"]').first.click()
            self._both_answered(page)
        finally:
            pipe_valves(self.token, **old)
        self.s.shot(page, "send-now")
        self._both_persisted(page)

    def image_input(self, page: Page) -> None:
        """An image attached in the composer reaches the agent."""
        path = os.path.join(self.s.out, "debug", "hourly-forecast.png")
        with open(path, "wb") as fh:
            fh.write(forecast_chart_png())
        new_chat(page)
        page.locator('input[type="file"][multiple]').set_input_files(path)
        page.locator('#message-input-container button[aria-label="Show image preview"] img').wait_for(timeout=30_000)
        page.locator("#chat-input").click()
        page.keyboard.type("What does this chart say about the afternoon?", delay=5)
        wait_js(page, "() => { const b = document.querySelector('#send-message-button'); return b && !b.disabled; }",
                "the upload to finish", 60_000)
        page.locator("#send-message-button").click()
        wait_js(page, "() => /^\\/c\\/[0-9a-f-]+/.test(location.pathname)", "the new chat's URL", 60_000)
        chat = page.evaluate("() => location.pathname").split("/c/", 1)[1].strip("/")
        wait_js(page, last_text_has("wet commute home"), "the reply about the chart")
        set_title(self.token, chat, "Afternoon forecast")
        self.s.shot(page, "image-input")

    def thinking_level(self, page: Page) -> None:
        """The thinking level, a per-user valve of the filter, with its choices.
        A <select>'s native popup is not in a headless screenshot, so the same
        element is drawn as a list (size = its option count)."""
        page.goto(f"{BASE}/c/{self.hero_chat}")
        page.wait_for_selector("#chat-input")
        page.locator('button[aria-label="Controls"]').click()
        page.locator('button[aria-expanded]', has_text="Valves").first.click()
        page.locator('select:has(option[value="functions"])').first.select_option("functions")
        page.locator('select:has(option[value="openclaw_thinking"])').first.select_option("openclaw_thinking")
        box = page.locator(".chat-control-valves")
        box.wait_for(timeout=30_000)
        if not box.locator("select").count():
            box.get_by_role("button", name="Default").first.click()  # Default -> Custom shows the field
        sel = box.locator("select").first
        sel.wait_for(timeout=30_000)
        print("  levels: " + json.dumps(sel.evaluate("s => [...s.options].map(o => o.value)")), flush=True)
        sel.evaluate("s => { s.size = s.options.length; }")
        clip = """() => {
          const b = document.querySelector('.chat-control-valves');
          b.scrollIntoView({block: 'center'});
          const r = b.getBoundingClientRect();
          return {x: 0, y: Math.max(0, r.top - 60), width: innerWidth, height: Math.min(r.height + 120, innerHeight)};
        }"""
        self.s.shot(page, "thinking-level", clip_js=clip, text_selector=".chat-control-valves")
        page.locator('button[aria-label="Close"]').first.click()

    def admin_valves(self, page: Page) -> None:
        """The Pipe's valves in the admin panel, from Owner Users down."""
        page.goto(f"{BASE}/admin/functions")
        row = page.locator('div[role="button"]', has_text="OpenClaw Gateway").first
        row.locator('button[aria-label="Valves"]').click()
        dialog = page.locator('div[role="dialog"][aria-modal="true"]').last
        owner = dialog.get_by_text("Owner Users", exact=True).first
        owner.wait_for(timeout=30_000)
        # Owner Users at the top: the connection valves above it hold the
        # Gateway token and the device key.
        owner.evaluate("el => { el.scrollIntoView({block: 'start'}); el.closest('[role=dialog]').scrollBy(0, -16); }")
        page.wait_for_timeout(300)
        self.s.shot(page, "valves", text_selector='div[role="dialog"][aria-modal="true"] >> nth=-1')
        page.keyboard.press("Escape")

    def toolbar(self, page: Page) -> None:
        """#10: the Status button on the message toolbar, desktop width."""
        page.goto(f"{BASE}/c/{self.hero_chat}")
        last = page.locator(".chat-assistant").last
        last.wait_for(timeout=60_000)
        last.hover()
        page.locator('button[aria-label="OpenClaw Status"]').last.wait_for(timeout=30_000)
        # The toolbar fades and slides in on hover: measure once the Status
        # button is laid out at its full size and nothing in its row moves.
        wait_js(page, """() => { const b = [...document.querySelectorAll('button[aria-label="OpenClaw Status"]')].pop();
                 if (!b || b.getBoundingClientRect().width < 16) return false;
                 // An action with an icon_url shows an image; wait for it if there is one.
                 if ([...b.querySelectorAll('img')].some(i => !i.complete || !i.naturalWidth)) return false;
                 let row = b.parentElement;
                 while (row && !row.querySelector('button[aria-label="Copy"]')) row = row.parentElement;
                 return !(row || b).getAnimations({subtree: true}).some(a => a.playState === 'running'); }""",
                "the Status button laid out", 30_000)
        # A 300 px strip of the message toolbar that ends at the Status button.
        clip = """() => {
          const b = [...document.querySelectorAll('button[aria-label="OpenClaw Status"]')].pop();
          let row = b.parentElement;
          while (row && !row.querySelector('button[aria-label="Copy"]')) row = row.parentElement;
          const r = (row || b).getBoundingClientRect(), q = b.getBoundingClientRect();
          const right = Math.min(innerWidth, q.right + 4);  // the reply's timestamp follows
          return {x: Math.max(0, right - 300), y: Math.max(0, r.top - 8), width: 300, height: r.height + 16};
        }"""
        print("  status button: " + json.dumps(page.evaluate("""() => {
          const b = [...document.querySelectorAll('button[aria-label="OpenClaw Status"]')].pop();
          const r = b.getBoundingClientRect(); return {left: r.left, right: r.right, width: r.width, vw: innerWidth}; }""")), flush=True)
        self.s.shot(page, "toolbar-button", clip_js=clip, text_selector=".chat-assistant >> nth=-1")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="docs/img")
    ap.add_argument("--only", help="comma-separated scene (run) names")
    a = ap.parse_args(argv)
    only = set(a.only.split(",")) if a.only else None
    shooter = Shooter(a.out, only)
    token = signin()
    quiet_background_tasks(token)
    scenes = Scenes(token, shooter)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = signed_in_page(browser, token, PHONE)
            shooter.run("hero-tool-cards", scenes.hero, page)
            if scenes.hero_chat:
                shooter.run("tool-card-expanded", scenes.expanded, page)
                shooter.run("composer-status", scenes.composer_status, page)
                shooter.run("thinking-toggle", scenes.thinking, page)
            shooter.run("question-card", scenes.question, page)
            if gateway_release() >= (2026, 10, 10):
                shooter.run("secret-request", scenes.secret_request, page)
            shooter.run("status-dialog", scenes.status_and_drawer, page)
            shooter.run("subagents-long", scenes.subagents_long, page)
            shooter.run("status-background", scenes.background, page)
            if scenes.background_chat:
                small = signed_in_page(browser, token, SMALL_PHONE)
                shooter.run("status-dialog-scrolled", scenes.background_scrolled, small)
                small.context.close()
            shooter.run("background-agent-waiting", scenes.background_agent, page)
            shooter.run("model-picker", scenes.model_picker, page)
            shooter.run("agent-turn", scenes.agent_turn, page)
            shooter.run("media-turn", scenes.media_turn, page)
            shooter.run("failed-tool-live", scenes.failed_tool_live, page)
            shooter.run("live-view", scenes.live_view, page)
            shooter.run("first-reply", scenes.first_reply, page)
            shooter.run("streaming", scenes.streaming_and_reload, page)
            shooter.run("usage-goal", scenes.usage_goal, page)
            shooter.run("subagent-result", scenes.subagent_result, page)
            if scenes.spawn_chat:
                shooter.run("spawn-card", scenes.spawn_card, page)
            shooter.run("error-message", scenes.error_message, page)
            shooter.run("chat-titles", scenes.chat_titles, page)
            shooter.run("stop", scenes.stop, page)
            shooter.run("message-sent-mid-run", scenes.sent_mid_run, page)
            shooter.run("send-now", scenes.send_now, page)
            shooter.run("image-input", scenes.image_input, page)
            shooter.run("valves", scenes.admin_valves, page)
            if scenes.hero_chat:
                shooter.run("thinking-level", scenes.thinking_level, page)
                shooter.run("compact-before", scenes.compact, page)
            if scenes.hero_chat:
                dpage = signed_in_page(browser, token, DESKTOP)
                shooter.run("toolbar-button", scenes.toolbar, dpage)
        finally:
            browser.close()
    print(f"\n{len(shooter.written)} images written")
    for f in shooter.failures:
        print(f"FAILED {f}")
    return 1 if shooter.failures else 0


if __name__ == "__main__":
    sys.exit(main())
