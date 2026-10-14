#!/usr/bin/env python3
"""Take the README and docs screenshots from a running screenshot stack.

Run by .github/workflows/screenshots.yml after e2e/start_stack.sh has brought
up Open WebUI, the Gateway and the scripted model (scenes.json). Every shot is
a real page in a phone-sized Chromium: real Open WebUI, the installed Pipe and
Action, a real Gateway running real tools against a small sample project.

Each shot waits for its own DOM state (never a fixed sleep), is taken in light
and dark, and fails if its visible text matches the deny-list (words that give
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
THEMES = ("light", "dark")
TURN_TIMEOUT_MS = 150_000

DENY = [re.compile(p, re.I) for p in (
    r"\be2e\b", r"\btest(s|ing)?\b", r"\bfake\b", r"\bdemo\b", r"\blorem\b", r"@",
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


def schedule_wake(session_key: str, message: str, in_s: float) -> None:
    at = (datetime.datetime.now(datetime.timezone.utc)
          + datetime.timedelta(seconds=in_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    params = {"name": "morning-build-check", "schedule": {"kind": "at", "at": at},
              "sessionTarget": f"session:{session_key}",
              "payload": {"kind": "agentTurn", "message": message},
              "delivery": {"mode": "none"}, "deleteAfterRun": True}
    out = subprocess.run(["openclaw", "gateway", "call", "cron.add", "--json",
                          "--params", json.dumps(params)],
                         capture_output=True, text=True, timeout=90)
    if out.returncode:
        raise RuntimeError(f"cron.add failed: {out.stderr[-1500:]}")


# ── Page helpers ────────────────────────────────────────────────────

def set_theme(page: Page, theme: str) -> None:
    page.emulate_media(color_scheme=theme)
    page.evaluate("""t => {
        try { localStorage.setItem('theme', t); } catch (e) {}
        document.documentElement.classList.toggle('dark', t === 'dark');
        document.documentElement.classList.toggle('light', t === 'light');
    }""", theme)


def login(page: Page, token: str) -> None:
    page.goto(BASE + "/auth")
    page.evaluate("t => localStorage.setItem('token', t)", token)
    page.goto(BASE + "/")
    page.wait_for_selector("#chat-input", timeout=60_000)
    dismiss_popups(page)


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


def wait_js(page: Page, js: str, what: str, timeout_ms: int = TURN_TIMEOUT_MS) -> None:
    try:
        page.wait_for_function(js, timeout=timeout_ms, polling=250)
    except Exception as ex:
        raise TimeoutError(f"waiting for {what}: {ex}") from None


LAST_ASSISTANT = "[...document.querySelectorAll('.chat-assistant')].pop()"
CARDS_STATE = f"""() => {{
  const a = {LAST_ASSISTANT}; if (!a) return null;
  return {{done: a.querySelectorAll('[role=button] .text-emerald-500').length,
          active: a.querySelectorAll('[role=button] .shimmer').length}};
}}"""


def visible_text(page: Page, selector: str | None = None) -> str:
    if selector:
        return page.locator(selector).first.inner_text()
    return page.evaluate("() => document.body.innerText")


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
        """Light and dark: the viewport, one element, or a JS-computed clip."""
        for theme in THEMES:
            set_theme(page, theme)
            page.wait_for_timeout(150)  # one frame for the class swap to paint
            self.check_text(page, name, text_selector or selector)
            path = os.path.join(self.out, f"{name}-{theme}.png")
            if selector:
                page.locator(selector).first.screenshot(path=path, animations="disabled")
            elif clip_js:
                page.screenshot(path=path, clip=page.evaluate(clip_js), animations="disabled")
            else:
                page.screenshot(path=path, animations="disabled")
            self.written.append(path)
            print(f"  wrote {path}", flush=True)
        set_theme(page, "light")

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


# ── Scenes ──────────────────────────────────────────────────────────

class Scenes:
    def __init__(self, token: str, shooter: Shooter):
        self.token, self.s = token, shooter
        self.hero_chat: str | None = None

    def hero(self, page: Page) -> None:
        """#1 hero-tool-cards mid-run, then #5 tool-card-expanded once done."""
        new_chat(page)
        self.hero_chat = send(page, "What changed in this repo this week?")
        wait_js(page, f"() => {{ const s = ({CARDS_STATE})(); return s && s.done >= 2 && s.active >= 1; }}",
                "two finished tool cards and one running")
        self.s.shot(page, "hero-tool-cards")
        wait_js(page, f"() => {{ const a = {LAST_ASSISTANT}; return a && a.innerText.includes('The build is clean'); }}",
                "the final answer")
        set_title(self.token, self.hero_chat, "Repo changes this week")

    def expanded(self, page: Page) -> None:
        page.goto(f"{BASE}/c/{self.hero_chat}")
        wait_js(page, f"() => {{ const s = ({CARDS_STATE})(); return s && s.done >= 3; }}", "three finished cards")
        page.locator(".chat-assistant").last.locator("[role=button]").first.click()
        wait_js(page, f"() => {{ const a = {LAST_ASSISTANT}; return a && a.querySelector('.tool-call-body'); }}",
                "the opened card's body")
        self.s.shot(page, "tool-card-expanded", selector=".chat-assistant >> nth=-1")

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
        wait_js(page, f"() => {{ const a = {LAST_ASSISTANT}; return a && a.innerText.includes('Where should the checks run?') && a.innerText.includes('Only on my machine'); }}",
                "the question card with its options")
        set_title(self.token, chat, "CI for the forecast app")
        self.s.shot(page, "question-card")

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
        page.locator('button[aria-label="OpenClaw status"]').click()
        root = "#openclaw-status-modal-root"
        wait_js(page, f"""() => {{
          const r = document.querySelector('{root}'); if (!r) return false;
          const t = r.innerText;
          return t.includes('Subagents (2)') && t.includes('Fly.io') && t.includes('Render')
            && /5h/.test(t) && /Week/.test(t);
        }}""", "the status dialog with two sub-agents and rate limits", 60_000)
        self.s.shot(page, "status-dialog", text_selector=root)
        page.locator(f"{root} button", has_text="Fly.io").first.click()
        page.wait_for_selector("#openclaw-drawer-root", timeout=30_000)
        page.locator("#openclaw-drawer-root button", has_text="Transcript").first.click()
        wait_js(page, """() => { const d = document.querySelector('#openclaw-drawer-root');
                 return d && d.innerText.includes('forecast/README.md'); }""",
                "the drawer transcript with the child's tool call", 60_000)
        self.s.shot(page, "subagent-drawer", text_selector="#openclaw-drawer-root")

    def model_picker(self, page: Page) -> None:
        new_chat(page)
        page.locator("button[id^=model-selector-][id$=-button], button[aria-label*='model' i]").first.click()
        wait_js(page, "() => document.body.innerText.includes('Nova Mini')", "the open model list", 30_000)
        self.s.shot(page, "model-picker")
        page.keyboard.press("Escape")

    def thinking(self, page: Page) -> None:
        page.goto(f"{BASE}/c/{self.hero_chat}")
        page.wait_for_selector("#chat-input")
        page.locator("button[aria-label='Integrations']").first.click()
        wait_js(page, "() => /thinking/i.test(document.body.innerText)", "the thinking toggle in the menu", 30_000)
        self.s.shot(page, "thinking-toggle")
        page.keyboard.press("Escape")

    def agent_turn(self, page: Page) -> None:
        new_chat(page)
        chat = send(page, "Can you check the overnight build every morning?")
        wait_js(page, f"() => {{ const a = {LAST_ASSISTANT}; return a && a.innerText.includes('only write when'); }}",
                "the first reply")
        set_title(self.token, chat, "Overnight build check")
        me = api("GET", "/api/v1/auths/", token=self.token)
        schedule_wake(f"agent:main:openwebui-{me['id']}-{chat}", "Morning build check for the forecast app", 5)
        wait_api(self.token, chat, "nothing needs you today")
        page.goto(f"{BASE}/c/{chat}")
        wait_js(page, f"() => {{ const a = {LAST_ASSISTANT}; return a && a.innerText.includes('nothing needs you today'); }}",
                "the cron reply on screen")
        self.s.shot(page, "agent-turn")

    def toolbar(self, page: Page) -> None:
        """#10: the Status button on the message toolbar, desktop width."""
        page.goto(f"{BASE}/c/{self.hero_chat}")
        last = page.locator(".chat-assistant").last
        last.wait_for(timeout=60_000)
        last.hover()
        page.locator('button[aria-label="OpenClaw Status"]').last.wait_for(timeout=30_000)
        # A 300 px strip of the message toolbar that ends at the Status button.
        clip = """() => {
          const b = [...document.querySelectorAll('button[aria-label="OpenClaw Status"]')].pop();
          let row = b.parentElement;
          while (row && !row.querySelector('button[aria-label="Copy"]')) row = row.parentElement;
          const r = (row || b).getBoundingClientRect(), q = b.getBoundingClientRect();
          const right = q.right + 12;
          return {x: Math.max(0, right - 300), y: Math.max(0, r.top - 8), width: 300, height: r.height + 16};
        }"""
        self.s.shot(page, "toolbar-button", clip_js=clip, text_selector=".chat-assistant >> nth=-1")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="docs/img")
    ap.add_argument("--only", help="comma-separated shot names")
    a = ap.parse_args(argv)
    only = set(a.only.split(",")) if a.only else None
    shooter = Shooter(a.out, only)
    token = signin()
    quiet_background_tasks(token)
    scenes = Scenes(token, shooter)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            ctx = browser.new_context(**PHONE, locale="en-US", timezone_id="America/New_York",
                                      color_scheme="light")
            page = ctx.new_page()
            login(page, token)
            shooter.run("hero-tool-cards", scenes.hero, page)
            if scenes.hero_chat:
                shooter.run("tool-card-expanded", scenes.expanded, page)
                shooter.run("composer-status", scenes.composer_status, page)
                shooter.run("thinking-toggle", scenes.thinking, page)
            shooter.run("question-card", scenes.question, page)
            shooter.run("status-dialog", scenes.status_and_drawer, page)
            shooter.run("model-picker", scenes.model_picker, page)
            shooter.run("agent-turn", scenes.agent_turn, page)
            if scenes.hero_chat:
                desk = browser.new_context(**DESKTOP, locale="en-US",
                                           timezone_id="America/New_York", color_scheme="light")
                dpage = desk.new_page()
                login(dpage, token)
                shooter.run("toolbar-button", scenes.toolbar, dpage)
        finally:
            browser.close()
    print(f"\n{len(shooter.written)} images written")
    for f in shooter.failures:
        print(f"FAILED {f}")
    return 1 if shooter.failures else 0


if __name__ == "__main__":
    sys.exit(main())
