"""Web Push end to end: real Google Chrome, the real push service (FCM), the
Cloudflare Worker example under `wrangler dev`, a throwaway Open WebUI.

    push/e2e/stack.sh up <run_dir> <openclaw_pipe.py build>
    xvfb-run -a uv run --with playwright==1.64.0 --with pytest pytest -q push/e2e/test_web_push_e2e.py
    push/e2e/stack.sh down <run_dir>

Push needs Google Chrome (Chromium builds have no push service), a persistent
profile and background networking, so this runs headed under Xvfb. Every wait
is on state: a service worker decision (`oc-push` message), a notification,
a saved subscription. None waits on time.
"""
import json
import os
import tempfile
import urllib.error
import urllib.request
import uuid

import pytest
from playwright.sync_api import sync_playwright

OWUI = os.environ.get("E2E_OWUI", "http://127.0.0.1:18780")
BASE = os.environ.get("E2E_BASE", "http://localhost:18787")   # the Worker
HERE = os.path.dirname(os.path.abspath(__file__))
PUSH = os.path.dirname(HERE)
CHROME = os.environ.get("E2E_CHROME", "/usr/bin/google-chrome")
WAIT_MS = 60000  # a safety net for each state wait, never expected to expire
SECRETS = ("STUB-SECRET-REPLY", "PIPE-SECRET-REPLY")


def api(path, body=None, token=None, method=None):
    req = urllib.request.Request(OWUI + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method or ("POST" if body is not None else "GET"),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or "null")


@pytest.fixture(scope="module")
def owui():
    # The stack's first user is its admin; a later run signs in as it.
    cred = {"email": "admin@e2e.example.com", "password": "e2e-pass-1"}
    try:
        token = api("/api/v1/auths/signin", cred)["token"]
    except urllib.error.HTTPError:
        token = api("/api/v1/auths/signup", {"name": "E2E", **cred})["token"]
    # The admin's "What's New" dialog would cover the page: mark this version seen.
    api("/api/v1/users/user/settings/update", {"ui": {"showChangelog": False}}, token)
    have = {f["id"] for f in api("/api/v1/functions/", token=token)}
    for fid, name, path in (("web_push", "Web Push", os.path.join(PUSH, "openclaw_web_push.py")),
                            ("stub", "Stub", os.path.join(HERE, "stub_pipe.py")),
                            ("openclaw_gateway", "OpenClaw", os.path.join(HERE, "fake_openclaw_pipe.py"))):
        form = {"id": fid, "name": name, "content": open(path).read(), "meta": {"description": name}}
        api(f"/api/v1/functions/id/{fid}/update" if fid in have else "/api/v1/functions/create", form, token)
        if not api(f"/api/v1/functions/id/{fid}", token=token)["is_active"]:
            api(f"/api/v1/functions/id/{fid}/toggle", {}, token)
    api("/api/v1/functions/id/web_push/valves/update",
        {"VAPID_PRIVATE_KEY_FILE": "/mnt/keys/vapid_private.pem", "VAPID_SUBJECT": "mailto:e2e@example.com"}, token)
    return token


def new_chat(token, model, title):
    """A saved chat with a user message and the assistant message to answer it."""
    u, a = str(uuid.uuid4()), str(uuid.uuid4())
    msgs = {u: {"id": u, "role": "user", "content": "hi", "parentId": None, "childrenIds": [a]},
            a: {"id": a, "role": "assistant", "content": "", "parentId": u, "childrenIds": [], "model": model}}
    chat = api("/api/v1/chats/new", {"chat": {"title": title, "models": [model],
                                              "history": {"messages": msgs, "currentId": a},
                                              "messages": list(msgs.values())}}, token)
    return chat["id"], a


def reply(token, model, chat_id, message_id):
    """The chat request the page sends; Open WebUI answers in a background task."""
    return api("/api/chat/completions", {"model": model, "messages": [{"role": "user", "content": "hi"}],
                                         "chat_id": chat_id, "id": message_id, "session_id": "e2e",
                                         "stream": True}, token)


def subscriptions(token, page):
    """This browser's entries in the user's subscription list."""
    device = page.evaluate("() => localStorage.getItem('ocPushDevice')")
    return [s for s in json.loads(api("/api/v1/functions/id/web_push/valves/user", token=token)["subscriptions"])
            if s.get("device") == device]


DECISIONS = """(() => { window.ocPushSeen = []; navigator.serviceWorker.addEventListener('message',
  (e) => { if (e.data && e.data.type === 'oc-push') window.ocPushSeen.push(e.data); });
  navigator.serviceWorker.startMessages(); })();"""
NOTES = "async () => (await (await navigator.serviceWorker.ready).getNotifications()).map(n => ({title: n.title, body: n.body, tag: n.tag, url: n.data && n.data.url}))"
CLEAR = "async () => (await (await navigator.serviceWorker.ready).getNotifications()).forEach(n => n.close())"


def seen_for(page, chat_id, n=1):
    page.wait_for_function("([c, n]) => (window.ocPushSeen || []).filter(d => d.payload.chat_id === c).length >= n",
                           arg=[chat_id, n], timeout=WAIT_MS)
    return page.evaluate("(c) => window.ocPushSeen.filter(d => d.payload.chat_id === c)", chat_id)


def ready(page):
    page.wait_for_function("() => window.ocPushState !== undefined", timeout=WAIT_MS)
    return page.evaluate("() => window.ocPushState")


@pytest.fixture(scope="module")
def browser(owui):
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            tempfile.mkdtemp(prefix="webpush-e2e-"), executable_path=CHROME, headless=False,
            args=["--no-sandbox"], viewport={"width": 390, "height": 844},
            ignore_default_args=["--disable-background-networking", "--disable-component-update", "--disable-sync"])
        ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
        ctx.add_init_script(DECISIONS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        yield ctx, page
        ctx.close()


def test_enable_subscribe_notify_suppress_and_resubscribe(owui, browser):
    ctx, page = browser
    token = owui
    # 1. The offer appears while the permission is not decided; Enable subscribes.
    page.goto(BASE + "/")
    assert ready(page) == "no-permission"
    page.locator("#oc-push-offer").wait_for(timeout=WAIT_MS)
    if os.environ.get("E2E_SHOTS"):
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)   # Open WebUI drawn around it
        page.screenshot(path=os.path.join(os.environ["E2E_SHOTS"], "web-push-offer.png"))
    # What the user's Allow in the prompt does: notifications and push for this origin.
    ctx.grant_permissions(["notifications"], origin=BASE)
    ctx.new_cdp_session(page).send("Browser.setPermission", {
        "permission": {"name": "push", "userVisibleOnly": True}, "setting": "granted", "origin": BASE})
    page.get_by_role("button", name="Enable notifications for replies").click()
    page.wait_for_function("() => window.ocPushState !== 'no-permission'", timeout=WAIT_MS)
    assert page.evaluate("() => window.ocPushState") == "saved"
    (sub,) = subscriptions(token, page)
    assert sub["endpoint"].startswith("https://fcm.googleapis.com/"), sub["endpoint"][:40]
    assert sub["keys"]["p256dh"] and sub["device"]

    # 2. A plain model's reply, while another page is open: Open WebUI's own chat.finished -> a notification.
    a_chat, a_msg = new_chat(token, "stub", "Chat A")
    reply(token, "stub", a_chat, a_msg)
    (d,) = seen_for(page, a_chat)
    assert d["shown"] and d["payload"]["source"] == "api"
    assert d["payload"] == {"v": 1, "kind": "chat.finished", "source": "api", "chat_id": a_chat,
                            "message_id": a_msg, "title": "Chat A", "url": f"/c/{a_chat}"}
    notes = page.evaluate(NOTES)   # the worker posts its decision once the notification is shown
    assert any(n["tag"] == f"chat-{a_chat}" and n["url"] == f"/c/{a_chat}" for n in notes), notes
    assert not any(s in json.dumps(notes) for s in SECRETS)
    page.evaluate(CLEAR)

    # 3. An OpenClaw reply in the chat on screen: Open WebUI's own event (source api) is skipped by
    #    the function, the Pipe's (source openclaw-pipe) is sent, and the worker drops it (on screen).
    b_chat, b_msg = new_chat(token, "openclaw_gateway.fake", "Chat B")
    page.goto(f"{BASE}/c/{b_chat}")
    assert ready(page) == "kept"
    page.bring_to_front()
    reply(token, "openclaw_gateway.fake", b_chat, b_msg)
    (d,) = seen_for(page, b_chat)
    assert d["payload"]["source"] == "openclaw-pipe" and d["shown"] is False, d
    assert not any(s in json.dumps(d) for s in SECRETS)
    # The same chat, not on screen: shown. A later chat's decision arrives after B's two events.
    c_chat, c_msg = new_chat(token, "openclaw_gateway.fake", "Chat C")
    reply(token, "openclaw_gateway.fake", c_chat, c_msg)
    (d,) = seen_for(page, c_chat)
    assert d["shown"] and d["payload"]["source"] == "openclaw-pipe"
    assert [x["payload"]["source"] for x in page.evaluate("(c) => window.ocPushSeen.filter(d => d.payload.chat_id === c)", b_chat)] == ["openclaw-pipe"]
    page.evaluate(CLEAR)

    # 4. Open WebUI's update path: every service worker unregistered, the page reloaded. The next
    #    load registers again with a new endpoint, which replaces this device's entry.
    page.evaluate("async () => { for (const r of await navigator.serviceWorker.getRegistrations()) await r.unregister(); }")
    page.goto(BASE + "/")
    assert ready(page) == "saved"
    (sub2,) = subscriptions(token, page)
    assert sub2["endpoint"] != sub["endpoint"] and sub2["device"] == sub["device"]
    d_chat, d_msg = new_chat(token, "stub", "Chat D")
    reply(token, "stub", d_chat, d_msg)
    (d,) = seen_for(page, d_chat)
    assert d["shown"]
