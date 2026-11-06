"""Web Push end to end: real Google Chrome, the real push service (FCM), the
Cloudflare Worker example under `wrangler dev`, a throwaway Open WebUI.

    push/e2e/stack.sh up <run_dir> <openclaw_pipe.py build>
    E2E_RUN=<run_dir> WEB_PUSH_E2E=1 xvfb-run -a uv run --with playwright==1.64.0 --with pytest pytest -q push/e2e/test_web_push_e2e.py
    push/e2e/stack.sh down <run_dir>

Push needs Google Chrome (Chromium builds have no push service), a persistent
profile and background networking, so this runs headed under Xvfb. Every wait
is on state: a service worker decision (`oc-push` message), a notification,
a saved subscription. None waits on time.
"""
import http.server
import json
import os
import subprocess
import threading
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

# Runs only against a stack this machine started (stack.sh up); the unit job skips it.
if not os.environ.get("WEB_PUSH_E2E"):
    pytest.skip("set WEB_PUSH_E2E=1 with push/e2e/stack.sh up", allow_module_level=True)
from playwright.sync_api import sync_playwright  # noqa: E402

OWUI = os.environ.get("E2E_OWUI", "http://127.0.0.1:18780")
BASE = os.environ.get("E2E_BASE", "http://localhost:18787")   # the Worker
HERE = os.path.dirname(os.path.abspath(__file__))
PUSH = os.path.dirname(HERE)
CHROME = os.environ.get("E2E_CHROME", "/usr/bin/google-chrome")
WAIT_MS = 60000  # a safety net for each state wait, never expected to expire
CHAT_LOAD_DELAY_MS = 200  # chat F arrives just after the reply is in the box (inside Open WebUI's 500 ms draft-save debounce)
MENU_DRAWN_WAIT_MS = 500  # a few frames: the script adds its item on the frame after the menu appears
DRAFT_RESTORE_WAIT_MS = 4000  # Open WebUI has loaded the chat and handled its own draft by then
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
    # Open WebUI keeps sign-up in its saved config (the env var is only the first default):
    # on, with new accounts as plain users, for the sign-up test.
    cfg = api("/api/v1/auths/admin/config", token=token)
    if not cfg["ENABLE_SIGNUP"] or cfg["DEFAULT_USER_ROLE"] != "user":
        api("/api/v1/auths/admin/config", {**cfg, "ENABLE_SIGNUP": True, "DEFAULT_USER_ROLE": "user"}, token)
    have = {f["id"] for f in api("/api/v1/functions/", token=token)}
    for fid, name, path in (("web_push", "Web Push", os.path.join(PUSH, "openclaw_web_push.py")),
                            ("stub", "Stub", os.path.join(HERE, "stub_pipe.py")),
                            ("slow", "Slow", os.path.join(HERE, "slow_pipe.py")),
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


def behind(ctx, page):
    """Another tab in front, so Open WebUI's page loses focus, as on a phone's home screen. A real
    tab through CDP, with Playwright's focus emulation off (it keeps every page focused). Call the result again to
    put it in front again."""
    cdp = ctx.new_cdp_session(page)
    tab = cdp.send("Target.createTarget", {"url": "about:blank"})["targetId"]
    def front():
        cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": False})   # bring_to_front turns it on again
        cdp.send("Target.activateTarget", {"targetId": tab})
        page.wait_for_function("() => !document.hasFocus()", timeout=WAIT_MS)
    front()
    return front


STATE_POLL_S = 0.3   # a poll of a state the page holds, never a wait for time


def settled(page, check, arg=None):
    """Waits until an async check in the page is true. Playwright's wait_for_function does not
    wait for a Promise (a Promise is truthy), so a check on caches is polled here instead."""
    end = time.time() + WAIT_MS / 1000
    while time.time() < end:
        if page.evaluate(check, arg):
            return
        time.sleep(STATE_POLL_S)
    raise AssertionError("never settled: " + check[:120])


def ready(page):
    page.wait_for_function("() => window.ocPushState !== undefined", timeout=WAIT_MS)
    return page.evaluate("() => window.ocPushState")


@pytest.fixture(scope="module")
def pw():
    with sync_playwright() as p:
        yield p


def chrome(pw):
    """Google Chrome with its own new profile, as a phone-sized window."""
    return pw.chromium.launch_persistent_context(
        tempfile.mkdtemp(prefix="webpush-e2e-"), executable_path=CHROME, headless=False,
        args=["--no-sandbox"], viewport={"width": 390, "height": 844},
        ignore_default_args=["--disable-background-networking", "--disable-component-update", "--disable-sync"])


@pytest.fixture(scope="module")
def browser(owui, pw):
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    ctx.add_init_script(DECISIONS)
    # Open WebUI's sign-in sets this cookie in a browser; the service worker's
    # actions authenticate with it (same origin, no token of their own).
    ctx.add_cookies([{"name": "token", "value": owui, "url": BASE}])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    yield ctx, page
    ctx.close()


def test_a_fresh_sign_up_gets_the_offer_without_a_reload(owui, pw):
    # Open WebUI signs up (and in) on the client: it stores the token and navigates without
    # loading a page. The page script loaded signed out must notice and offer, once.
    ctx = chrome(pw)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE + "/auth")
        assert ready(page) == "no-token"
        page.evaluate("() => { window.ocSameLoad = true }")
        page.get_by_role("button", name="Sign up", exact=True).click()
        page.locator("#name").fill("Fresh")
        page.locator("#email").fill(f"fresh-{uuid.uuid4().hex[:8]}@e2e.example.com")
        page.locator("input[type=password]").first.fill("e2e-pass-2")
        page.get_by_role("button", name="Create Account").click()
        page.locator("#oc-push-offer").wait_for(timeout=WAIT_MS)
        assert page.evaluate("() => window.ocSameLoad") is True, "the page was reloaded"
        assert page.evaluate("() => window.ocPushState") == "no-permission"
        assert page.locator("#oc-push-offer").count() == 1
    finally:
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

    # 2. A plain model's reply, while Open WebUI is in the background: Open WebUI's own chat.finished -> a notification.
    other = behind(ctx, page)
    a_chat, a_msg = new_chat(token, "stub", "Chat A")
    reply(token, "stub", a_chat, a_msg)
    (d,) = seen_for(page, a_chat)
    assert d["shown"] and d["payload"]["source"] == "api"
    assert d["payload"] == {"v": 2, "kind": "chat.finished", "source": "api", "chat_id": a_chat,
                            "message_id": a_msg, "title": "Chat A", "url": f"/c/{a_chat}", "fn": "web_push", "actions": ["reply", "mute"]}
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
    # Another chat while Open WebUI is in front (chat B open): nothing either, as in a messaging app.
    # A later chat's decision arrives after B's two events.
    c_chat, c_msg = new_chat(token, "openclaw_gateway.fake", "Chat C")
    reply(token, "openclaw_gateway.fake", c_chat, c_msg)
    (d,) = seen_for(page, c_chat)
    assert d["payload"]["source"] == "openclaw-pipe" and d["shown"] is False, d
    assert [x["payload"]["source"] for x in page.evaluate("(c) => window.ocPushSeen.filter(d => d.payload.chat_id === c)", b_chat)] == ["openclaw-pipe"]
    assert page.evaluate(NOTES) == []
    # The same, with Open WebUI in the background: shown.
    other()
    e_chat, e_msg = new_chat(token, "openclaw_gateway.fake", "Chat E")
    reply(token, "openclaw_gateway.fake", e_chat, e_msg)
    (d,) = seen_for(page, e_chat)
    assert d["shown"] and d["payload"]["source"] == "openclaw-pipe"
    page.evaluate(CLEAR)

    # 4. Open WebUI's update path: every service worker unregistered, the page reloaded. The next
    #    load registers again with a new endpoint, which replaces this device's entry.
    page.evaluate("async () => { for (const r of await navigator.serviceWorker.getRegistrations()) await r.unregister(); }")
    page.goto(BASE + "/")
    assert ready(page) == "saved"
    (sub2,) = subscriptions(token, page)
    assert sub2["endpoint"] != sub["endpoint"] and sub2["device"] == sub["device"]
    other()
    d_chat, d_msg = new_chat(token, "stub", "Chat D")
    reply(token, "stub", d_chat, d_msg)
    (d,) = seen_for(page, d_chat)
    assert d["shown"]


def history(token, chat_id):
    return api(f"/api/v1/chats/{chat_id}", token=token)["chat"]["history"]


def act(page, action, data, reply=None):
    """What a tap on the notification's action does: the service worker's own
    handler (`act`), asked through a message; returns its result."""
    return page.evaluate("""([action, data, reply]) => new Promise((ok) => {
        navigator.serviceWorker.addEventListener('message', function f(e) {
          if (e.data && e.data.type === 'oc-push-action') { navigator.serviceWorker.removeEventListener('message', f); ok(e.data.result); }
        });
        navigator.serviceWorker.controller.postMessage({type: 'oc-push-act', action, data, reply});
      })""", [action, data, reply])


def wait_done(token, chat_id, message_id):
    end = time.time() + WAIT_MS / 1000
    while time.time() < end:
        m = history(token, chat_id)["messages"].get(message_id) or {}
        if m.get("done") and m.get("content"):
            return m
        time.sleep(0.3)   # a poll of the saved state, never a wait for time
    raise AssertionError(f"{message_id} not done")


def owui_log(since):
    return subprocess.run(["docker", "logs", "--since", str(int(since) - 1), os.environ.get("E2E_NAME", "owui-webpush-e2e")],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout


def valves(token):
    return api("/api/v1/functions/id/web_push/valves/user", token=token)


def test_preview_reply_mute_and_the_bell(owui, browser):
    ctx, page = browser
    token = owui
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    other = behind(ctx, page)

    # B. Preview on: the start of the reply, as plain text, is the body.
    api("/api/v1/functions/id/web_push/valves/update",
        {**api("/api/v1/functions/id/web_push/valves", token=token), "PREVIEW_CHARS": 600}, token)
    try:
        p_chat, p_msg = new_chat(token, "stub", "Chat P")
        reply(token, "stub", p_chat, p_msg)
        (d,) = seen_for(page, p_chat)
        assert d["shown"] and d["payload"]["body"].startswith("STUB-SECRET-REPLY"), d
        assert d["payload"]["fn"] == "web_push"
        notes = page.evaluate(NOTES)
        assert any(n["tag"] == f"chat-{p_chat}" and n["body"].startswith("STUB-SECRET-REPLY") for n in notes), notes
        page.evaluate(CLEAR)
    finally:
        api("/api/v1/functions/id/web_push/valves/update",
            {**api("/api/v1/functions/id/web_push/valves", token=token), "PREVIEW_CHARS": 0}, token)

    # C. Reply from the notification: a user message in the chat under its leaf, answered.
    data = {"chat_id": p_chat, "url": f"/c/{p_chat}", "fn": "web_push"}
    leaf = history(token, p_chat)["currentId"]
    assert act(page, "reply", data, "a reply from the shade") == "sent"
    h = history(token, p_chat)
    (u,) = [m for m in h["messages"].values() if m["role"] == "user" and m["content"] == "a reply from the shade"]
    assert u["parentId"] == leaf
    (a_id,) = u["childrenIds"]
    assert wait_done(token, p_chat, a_id)["content"].startswith("STUB-SECRET-REPLY")

    # C. Reply while the chat's run is going: in the chat at once, under the running reply, nothing stopped.
    s_chat, s_msg = new_chat(token, "slow", "Chat S")
    reply(token, "slow", s_chat, s_msg)
    assert not history(token, s_chat)["messages"][s_msg].get("done")
    assert act(page, "reply", {"chat_id": s_chat, "url": f"/c/{s_chat}", "fn": "web_push"}, "while it runs") == "sent"
    h = history(token, s_chat)
    (u,) = [m for m in h["messages"].values() if m["role"] == "user" and m["content"] == "while it runs"]
    assert u["parentId"] == s_msg and not h["messages"][s_msg].get("done")
    assert wait_done(token, s_chat, s_msg)["content"] == "SLOW-REPLY done"
    assert wait_done(token, s_chat, u["childrenIds"][0])["content"] == "SLOW-REPLY done"

    # C. A reply that cannot be sent (the chat's model is gone): the chat opens with the text in the box.
    f_chat, f_msg = new_chat(token, "stub", "Chat F")
    api(f"/api/v1/chats/{f_chat}", {"chat": {"models": ["no-such-model"]}}, token)
    # The worker sends this page to the chat, so its answer never comes back here: wait for the chat page.
    page.evaluate("(m) => navigator.serviceWorker.controller.postMessage(m)",
                  {"type": "oc-push-act", "action": "reply", "reply": "keep this text",
                   "data": {"chat_id": f_chat, "url": f"/c/{f_chat}", "fn": "web_push"}})
    page.wait_for_url(f"**/c/{f_chat}*", timeout=WAIT_MS)
    page.wait_for_function("() => window.ocPushReply && window.ocPushReply.text === 'keep this text'", timeout=WAIT_MS)
    assert "/c/" + f_chat in page.url and "oc-reply" not in page.url
    assert "keep this text" in page.locator("#chat-input").inner_text()
    assert "not sent" in page.locator("#oc-push-notice").inner_text()
    assert not [m for m in history(token, f_chat)["messages"].values() if m.get("content") == "keep this text"]

    # D. Mute 8 h from the notification: kept server-side beside the subscriptions; nothing is sent.
    before = valves(token)["subscriptions"]
    assert act(page, "mute", data) == "muted"
    v = valves(token)
    assert v["subscriptions"] == before
    until = json.loads(v["mutes"])[p_chat]
    assert 8 * 3600 - 60 < until - time.time() <= 8 * 3600
    m_msg = str(uuid.uuid4())
    t_mute = time.time()
    h = history(token, p_chat)
    leaf = h["currentId"]
    u_id = str(uuid.uuid4())
    api("/api/chat/completions", {"model": "stub", "chat_id": p_chat, "id": m_msg, "parent_id": leaf, "stream": True,
                                  "user_message": {"id": u_id, "parentId": leaf, "childrenIds": [], "role": "user",
                                                   "content": "muted?", "models": ["stub"]},
                                  "message_ids": [{"model_id": "stub", "message_id": m_msg, "modelIdx": 0}],
                                  "session_id": "e2e"}, token)
    wait_done(token, p_chat, m_msg)
    end = time.time() + WAIT_MS / 1000
    while f"chat {p_chat[:8]} is muted" not in owui_log(t_mute):
        assert time.time() < end, "the function never skipped the muted chat"
        time.sleep(0.3)
    assert not [x for x in page.evaluate("() => window.ocPushSeen") if x["payload"]["chat_id"] == p_chat and x["payload"]["message_id"] == m_msg]

    # D. The bell in the chat: shows the mute, changes it, survives Open WebUI redrawing its header.
    page.bring_to_front()
    page.goto(f"{BASE}/c/{p_chat}")
    bell = page.locator("#oc-push-bell")
    bell.wait_for(timeout=WAIT_MS)
    assert bell.get_attribute("title").startswith("Muted until")
    assert bell.evaluate("(b) => !!(b.compareDocumentPosition(document.getElementById('chat-context-menu-button')) & Node.DOCUMENT_POSITION_FOLLOWING) && b.closest('nav') !== null")
    assert bell.evaluate("(b) => b.className === document.getElementById('chat-context-menu-button').className")
    bell.click()
    page.get_by_role("menuitem", name="Unmute").click()
    page.wait_for_function("() => document.getElementById('oc-push-bell').title === 'Notifications on'", timeout=WAIT_MS)
    assert p_chat not in json.loads(valves(token)["mutes"])
    bell.click()
    page.get_by_role("menuitem", name="Mute always").click()
    page.wait_for_function("() => document.getElementById('oc-push-bell').title === 'Muted'", timeout=WAIT_MS)
    assert json.loads(valves(token)["mutes"])[p_chat] == 0
    if os.environ.get("E2E_SHOTS"):
        bell.click()
        page.screenshot(path=os.path.join(os.environ["E2E_SHOTS"], "web-push-bell.png"))
        page.keyboard.press("Escape")
    # Open WebUI redraws the header for another chat: the bell follows, for that chat.
    page.goto(f"{BASE}/c/{s_chat}")
    page.wait_for_function(f"() => document.getElementById('oc-push-bell')?.dataset.chat === {json.dumps(s_chat)}", timeout=WAIT_MS)
    assert page.locator("#oc-push-bell").get_attribute("title") == "Notifications on"
    # Right to left: the menu opens inside the window, under the bell.
    page.evaluate("() => document.documentElement.setAttribute('dir', 'rtl')")
    page.locator("#oc-push-bell").click()
    box = page.locator("#oc-push-mute-menu").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"], box
    page.keyboard.press("Escape")
    assert page.locator("#oc-push-mute-menu").count() == 0
    page.evaluate("() => document.documentElement.removeAttribute('dir')")
    ocp = valves(token)
    api("/api/v1/functions/id/web_push/valves/user/update", {**ocp, "mutes": "{}"}, token)
    other()


def muted_log(since, text):
    end = time.time() + WAIT_MS / 1000
    while text not in owui_log(since):
        assert time.time() < end, f"the function never logged {text!r}"
        time.sleep(0.3)   # a poll of the container's log, never a wait for time


def test_one_notification_per_chat_and_the_folder_mute(owui, browser):
    ctx, page = browser
    token = owui
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    other = behind(ctx, page)
    page.evaluate(CLEAR)

    # A. Two replies in one chat leave one notification (the tag is the chat's).
    o_chat, o_msg = new_chat(token, "stub", "Chat O")
    reply(token, "stub", o_chat, o_msg)
    seen_for(page, o_chat)
    o2 = str(uuid.uuid4())
    leaf = history(token, o_chat)["currentId"]
    api("/api/chat/completions", {"model": "stub", "chat_id": o_chat, "id": o2, "parent_id": leaf, "stream": True,
                                  "user_message": {"id": str(uuid.uuid4()), "parentId": leaf, "childrenIds": [], "role": "user",
                                                   "content": "again", "models": ["stub"]},
                                  "message_ids": [{"model_id": "stub", "message_id": o2, "modelIdx": 0}], "session_id": "e2e"}, token)
    assert all(d["shown"] for d in seen_for(page, o_chat, 2))
    assert [n["tag"] for n in page.evaluate(NOTES) if n["tag"] == f"chat-{o_chat}"] == [f"chat-{o_chat}"]
    page.evaluate(CLEAR)

    # B. Folder mute. A chat in folder Child, which is inside folder Parent.
    parent = api("/api/v1/folders/", {"name": "Parent"}, token)["id"]
    child = api("/api/v1/folders/", {"name": "Child", "parent_id": parent}, token)["id"]
    q_chat, _ = new_chat(token, "stub", "Chat Q")
    api(f"/api/v1/chats/{q_chat}/folder", {"folder_id": child}, token)
    before = valves(token)["subscriptions"]

    def answer_and_check(muted_by):
        t = time.time()
        msg = str(uuid.uuid4())
        leaf = history(token, q_chat)["currentId"]
        api("/api/chat/completions", {"model": "stub", "chat_id": q_chat, "id": msg, "parent_id": leaf, "stream": True,
                                      "user_message": {"id": str(uuid.uuid4()), "parentId": leaf, "childrenIds": [], "role": "user",
                                                       "content": "folder?", "models": ["stub"]},
                                      "message_ids": [{"model_id": "stub", "message_id": msg, "modelIdx": 0}], "session_id": "e2e"}, token)
        if muted_by:
            wait_done(token, q_chat, msg)
            muted_log(t, f"chat {q_chat[:8]}: folder {muted_by[:8]} is muted")
            assert not [x for x in page.evaluate("() => window.ocPushSeen") if x["payload"].get("message_id") == msg]
        else:
            page.wait_for_function("(m) => window.ocPushSeen.some(d => d.payload.message_id === m && d.shown)", arg=msg, timeout=WAIT_MS)

    # The bell's menu mutes the chat's own folder; the bell says so.
    page.bring_to_front()
    page.goto(f"{BASE}/c/{q_chat}")
    bell = page.locator("#oc-push-bell")
    bell.wait_for(timeout=WAIT_MS)
    bell.click()
    page.get_by_role("menuitem", name="Mute folder always").click()
    page.wait_for_function("() => document.getElementById('oc-push-bell').title === 'Muted (folder Child)'", timeout=WAIT_MS)
    v = valves(token)
    assert json.loads(v["folder_mutes"]) == {child: 0} and v["subscriptions"] == before
    other()
    answer_and_check(child)

    # A mute of the folder above covers it; the menu offers to unmute that folder.
    api("/api/v1/functions/id/web_push/valves/user/update", {**valves(token), "folder_mutes": json.dumps({parent: 0})}, token)
    answer_and_check(parent)
    page.bring_to_front()
    page.goto(f"{BASE}/c/{q_chat}")
    page.wait_for_function("() => document.getElementById('oc-push-bell')?.title === 'Muted (folder Parent)'", timeout=WAIT_MS)
    bell.click()
    assert "(by Parent)" in page.locator("#oc-push-mute-menu").inner_text()
    if os.environ.get("E2E_SHOTS"):
        page.screenshot(path=os.path.join(os.environ["E2E_SHOTS"], "web-push-folder-mute.png"))
    page.get_by_role("menuitem", name="Unmute folder Parent").click()
    page.wait_for_function("() => document.getElementById('oc-push-bell').title === 'Notifications on'", timeout=WAIT_MS)
    assert json.loads(valves(token)["folder_mutes"]) == {}
    other()
    answer_and_check(None)


def read(page, chat_id):
    """The page script says it closed `chat_id`'s notifications; returns the tags still showing, read
    at once (Chrome on Linux closes notifications by itself after a while, so only an immediate read
    tells what the page closed)."""
    page.wait_for_function("(c) => window.ocPushRead === c", arg=chat_id, timeout=WAIT_MS)
    return sorted(n["tag"] for n in page.evaluate(NOTES))


def test_opening_a_chat_clears_its_notifications(owui, browser):
    ctx, page = browser
    token = owui
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    other = behind(ctx, page)
    page.evaluate(CLEAR)
    chats = {}
    for name in ("X", "Y", "Z"):
        chat, msg = new_chat(token, "stub", "Chat " + name)
        reply(token, "stub", chat, msg)
        (d,) = seen_for(page, chat)
        assert d["shown"]
        chats[name] = chat
    tag = {k: f"chat-{v}" for k, v in chats.items()}
    assert sorted(n["tag"] for n in page.evaluate(NOTES)) == sorted(tag.values())
    # Opened by any route, not only the notification: a page load ...
    page.bring_to_front()
    page.goto(f"{BASE}/c/{chats['X']}")
    assert read(page, chats["X"]) == sorted([tag["Y"], tag["Z"]])
    # ... or Open WebUI's own navigation to another chat (SvelteKit's pushState); closed even when
    # Cache Storage refuses (the page script marks its closes there first, oc-sw.js swipe = read).
    page.evaluate("() => { window.ocOpen = CacheStorage.prototype.open; CacheStorage.prototype.open = () => Promise.reject(new Error('no storage')); }")
    page.evaluate("(c) => history.pushState({}, '', '/c/' + c)", chats["Y"])
    try:
        assert read(page, chats["Y"]) == [tag["Z"]]
    finally:
        page.evaluate("() => { CacheStorage.prototype.open = window.ocOpen; }")
    # Hidden, a chat on the page is not read; shown again on it, it is. Chrome under Xvfb does not
    # reliably hide a page (another tab, a minimized window), so the page's visibility is set here:
    # what the page script reads (visibilityState) and the event it listens to.
    vis = """(v) => { Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => v});
                      document.dispatchEvent(new Event('visibilitychange')); }"""
    page.evaluate(vis, "hidden")
    page.evaluate("(c) => history.replaceState({}, '', '/c/' + c)", chats["Z"])
    assert page.evaluate("() => window.ocPushRead") == chats["Y"]
    page.evaluate(vis, "visible")
    assert read(page, chats["Z"]) == []
    page.evaluate("() => delete document.visibilityState")
    other()


def unread(token, chat_id):
    """The sidebar's blue dot (Open WebUI 0.13): the chat changed after it was last read."""
    (c,) = [c for c in api("/api/v1/chats/?page=1", token=token) if c["id"] == chat_id]
    return c.get("last_read_at") is None or c["updated_at"] > c["last_read_at"]


def test_mark_as_read_from_the_notification(owui, browser):
    ctx, page = browser
    token = owui
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    api("/api/v1/functions/id/web_push/valves/update",
        {**api("/api/v1/functions/id/web_push/valves", token=token), "NOTIFICATION_ACTIONS": "reply,read"}, token)
    try:
        other = behind(ctx, page)
        r_chat, r_msg = new_chat(token, "stub", "Chat R")
        reply(token, "stub", r_chat, r_msg)
        (d,) = seen_for(page, r_chat)
        assert d["shown"] and d["payload"]["actions"] == ["reply", "read"]
        wait_done(token, r_chat, r_msg)
        # Unread, as a reply that came while you were away (here it may share the chat's creation second).
        api(f"/api/v1/chats/{r_chat}/unread", {}, token)
        assert unread(token, r_chat)
        # Mark as read: the dot goes, through Open WebUI's own read event, without opening the chat.
        data = {"chat_id": r_chat, "url": f"/c/{r_chat}", "fn": "web_push"}
        assert act(page, "read", data) == "read"
        assert not unread(token, r_chat)
        assert "/c/" not in page.url
        # Signed out (no session cookie): not marked, the chat opens instead, which reads it.
        api(f"/api/v1/chats/{r_chat}/unread", {}, token)
        assert unread(token, r_chat)
        cookies = ctx.cookies(BASE)
        ctx.clear_cookies(name="token")
        try:
            page.evaluate("(m) => navigator.serviceWorker.controller.postMessage(m)",
                          {"type": "oc-push-act", "action": "read", "data": data})
            page.wait_for_url(f"**/c/{r_chat}", timeout=WAIT_MS)
        finally:
            ctx.add_cookies([c for c in cookies if c["name"] == "token"])
        other()
    finally:
        api("/api/v1/functions/id/web_push/valves/update",
            {**api("/api/v1/functions/id/web_push/valves", token=token), "NOTIFICATION_ACTIONS": "reply,mute"}, token)


def tap(page, action, data, reply=None):
    """A tap on the notification (or one of its buttons), as the service worker handles it; returns at once."""
    page.evaluate("(m) => navigator.serviceWorker.controller.postMessage(m)",
                  {"type": "oc-push-act", "action": action, "data": data, "reply": reply})


def test_a_tap_opens_the_chat_without_a_reload(owui, browser):
    """An open Open WebUI window goes to the chat in the app (no document load): timeOrigin is the page's identity."""
    ctx, page = browser
    token = owui
    a_chat, _ = new_chat(token, "stub", "Chat TA")
    b_chat, _ = new_chat(token, "stub", "Chat TB")
    page.goto(f"{BASE}/c/{b_chat}")
    assert ready(page) in ("kept", "saved")
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    origin = page.evaluate("() => performance.timeOrigin")
    # On chat B, the notification for chat A.
    tap(page, "", {"chat_id": a_chat, "url": f"/c/{a_chat}"})
    page.wait_for_function("(c) => location.pathname === '/c/' + c", arg=a_chat, timeout=WAIT_MS)
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    assert page.evaluate("() => performance.timeOrigin") == origin, "the page was loaded again"
    assert page.evaluate("() => window.ocPushOpened") == a_chat
    # The same chat's notification: nothing to do.
    tap(page, "", {"chat_id": a_chat, "url": f"/c/{a_chat}"})
    page.wait_for_function("() => window.ocPushOpenedSame === true", timeout=WAIT_MS)
    assert page.evaluate("() => performance.timeOrigin") == origin
    # Reply that cannot be sent: the chat opens in the app, the text in the message box.
    f_chat, _ = new_chat(token, "stub", "Chat TF")
    api(f"/api/v1/chats/{f_chat}", {"chat": {"models": ["no-such-model"]}}, token)
    # A guard, not a fail-first: chat F has a draft of its own in this tab (Open WebUI 0.13 keeps one
    # per chat in sessionStorage and puts it back while it loads the chat); the reply must stay.
    page.evaluate("(c) => sessionStorage.setItem('chat-input-' + c, JSON.stringify({prompt: 'an older draft'}))", f_chat)
    # Chat F arrives (and its draft goes back in) just after the reply is in the box.
    page.evaluate("""([c, ms]) => { const f = window.fetch; window.fetch = async (u, o) => {
        if (String(u).endsWith('/api/v1/chats/' + c)) await new Promise((ok) => setTimeout(ok, ms));
        return f(u, o); }; }""", [f_chat, CHAT_LOAD_DELAY_MS])
    tap(page, "reply", {"chat_id": f_chat, "url": f"/c/{f_chat}", "fn": "web_push"}, "kept, not reloaded")
    page.wait_for_function("() => window.ocPushReply && window.ocPushReply.text === 'kept, not reloaded'", timeout=WAIT_MS)
    assert "/c/" + f_chat in page.url and "oc-reply" not in page.url
    page.wait_for_timeout(DRAFT_RESTORE_WAIT_MS)
    assert "kept, not reloaded" in page.locator("#chat-input").inner_text(), "chat F's own draft replaced the reply"
    assert page.evaluate("(c) => caches.open('oc-push-drafts').then(d => d.match('/oc-push/draft/' + c)).then(r => !r)", f_chat), \
        "the cached reply was kept after it was put in the box"
    assert page.evaluate("() => performance.timeOrigin") == origin, "the page was loaded again"


def sidebar_link(page, chat_id):
    """A chat's entry in the sidebar, the sidebar opened (a phone-sized window keeps it closed)."""
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)   # Open WebUI drawn
    if not page.locator(f'a[href="/c/{chat_id}"]').is_visible():
        page.locator("#sidebar-toggle-button").first.click()
    return page.locator(f'a[href="/c/{chat_id}"]')


def chat_menu(page, chat_id):
    """Open WebUI's own chat menu (⋯) for a chat in the sidebar; returns the menu."""
    item = sidebar_link(page, chat_id)
    item.wait_for(timeout=WAIT_MS)
    # The chat's ⋯ (in the DOM, shown on hover), pressed as a pointer does.
    item.evaluate("""(a) => { let box = a, b = null;
        while (box && !(b = box.querySelector('button[aria-haspopup]'))) box = box.parentElement;
        for (const t of ['pointerdown', 'pointerup']) b.dispatchEvent(new PointerEvent(t, {bubbles: true}));
        b.click(); }""")
    menu = page.locator("[role=menu]")
    menu.wait_for(timeout=WAIT_MS)
    return menu


def test_mark_as_read_from_the_chat_menu(owui, browser):
    """The DOM contract with Open WebUI 0.13.0's chat menu: an upgrade that moves it fails here."""
    ctx, page = browser
    token = owui
    m_chat, m_msg = new_chat(token, "stub", "Chat M")
    reply(token, "stub", m_chat, m_msg)
    wait_done(token, m_chat, m_msg)
    api(f"/api/v1/chats/{m_chat}/unread", {}, token)
    assert unread(token, m_chat)
    page.bring_to_front()
    page.set_viewport_size({"width": 1280, "height": 844})   # the sidebar open beside the chat
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    menu = chat_menu(page, m_chat)
    own = menu.locator("button[role=menuitem]")
    item = menu.get_by_role("menuitem", name="Mark as read")
    item.wait_for(timeout=WAIT_MS)
    # Beside Open WebUI's own "Mark as unread", styled as its items.
    assert item.evaluate("(b) => b.previousElementSibling.querySelector('path').getAttribute('d').startsWith('M8 12L11 15L16 10')")
    assert item.evaluate("(b) => b.className === b.previousElementSibling.className")
    if os.environ.get("E2E_SHOTS"):
        page.screenshot(path=os.path.join(os.environ["E2E_SHOTS"], "web-push-menu-mark-read.png"))
    item.click()
    end = time.time() + WAIT_MS / 1000
    while unread(token, m_chat):
        assert time.time() < end, "the chat was not marked read"
        time.sleep(0.3)   # a poll of the chat list, never a wait for time
    assert page.locator("[role=menu]").count() == 0, "the menu stayed open"
    assert f"/c/{m_chat}" not in page.url, "the chat was opened"
    assert own.count() == 0

    # Fail safe: a menu without Open WebUI 0.13's structure gets nothing, and one console line.
    logs = []
    page.on("console", lambda m: logs.append(m.text))
    # A chat item whose ⋯ opens a menu Open WebUI 0.13 would not draw (twice: still one line).
    page.evaluate("""(c) => { const item = document.createElement('div'); item.id = 'fake-item';
        item.innerHTML = `<div id="sidebar-chat-item"><a href="/c/${c}">x</a></div><button aria-haspopup="true">⋯</button>`;
        document.body.appendChild(item);
        for (const id of ['not-owui', 'not-owui-2']) {
          item.querySelector('button').dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
          const m = document.createElement('div'); m.setAttribute('role', 'menu'); m.id = id;
          m.innerHTML = '<button role="menuitem">Something</button>'; document.body.appendChild(m);
        } }""", m_chat)
    page.wait_for_function("() => document.querySelectorAll('#not-owui, #not-owui-2').length === 2", timeout=WAIT_MS)
    page.wait_for_function("() => window.ocPushMenuContract === 'missing'", timeout=WAIT_MS)
    assert page.locator("#not-owui [role=menuitem], #not-owui-2 [role=menuitem]").count() == 2
    assert len([l for l in logs if "chat menu" in l]) == 1, logs
    # Open WebUI's structure and icon, but the label is not where the script looks: nothing either.
    page.evaluate("""(icon) => { window.ocPushMenuContract = 'ok'; const b = document.querySelector('#fake-item button');
        b.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
        const m = document.createElement('div'); m.setAttribute('role', 'menu'); m.id = 'bad-label';
        m.innerHTML = `<div class="app-dropdown-menu"><button role="menuitem"><svg><path d="${icon} Z"></path></svg>`
          + `<span>Mark as unread</span><div></div></button></div>`;
        document.body.appendChild(m); }""", "M8 12L11 15L16 10")
    page.wait_for_function("() => document.querySelector('#bad-label')?.dataset.ocPush === 'seen'", timeout=WAIT_MS)
    page.wait_for_timeout(MENU_DRAWN_WAIT_MS)
    assert page.locator("#bad-label [role=menuitem]").count() == 1, "an item was added to a menu with an unknown label"
    assert page.evaluate("() => window.ocPushMenuContract") == "missing"
    assert len([l for l in logs if "chat menu" in l]) == 1, logs
    page.evaluate("() => document.querySelectorAll('#not-owui, #not-owui-2, #bad-label, #fake-item').forEach(e => e.remove())")
    page.set_viewport_size({"width": 390, "height": 844})


def native_menu(page, trigger_js, arg):
    """Open one of Open WebUI's own ⋯ menus, pressed as a pointer does; returns the menu."""
    # Every menu before it is closed (ours close Open WebUI's when they act).
    page.wait_for_function("() => !document.querySelector('[role=menu]')", timeout=WAIT_MS)
    page.evaluate("""([find, arg]) => { const b = (new Function('arg', 'return ' + find))(arg);
        for (const t of ['pointerdown', 'pointerup']) b.dispatchEvent(new PointerEvent(t, {bubbles: true}));
        b.click(); }""", [trigger_js, arg])
    menu = page.locator("[role=menu]").first
    menu.wait_for(timeout=WAIT_MS)
    return menu


def shot(page, name):
    """A still for the PR (E2E_SHOTS=<dir>), taken before the check so an old build gives its own."""
    if os.environ.get("E2E_SHOTS"):
        page.wait_for_timeout(MENU_DRAWN_WAIT_MS)
        page.screenshot(path=os.path.join(os.environ["E2E_SHOTS"], name + ".png"))


def close_menus(page):
    """Escape, sent where Open WebUI's menus listen (focus is not in a menu opened by dispatched events),
    then a press outside, as a user closes one, while a menu is still open."""
    page.wait_for_function("""() => { if (!document.querySelector('[role=menu]')) return true;
        document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
        for (const t of ['pointerdown', 'pointerup']) document.body.dispatchEvent(new PointerEvent(t, {bubbles: true}));
        return false; }""", timeout=WAIT_MS, polling=MENU_DRAWN_WAIT_MS)


FOLDER_ROW_TRIGGER = "document.getElementById('folder-' + arg + '-button').querySelector('button[aria-haspopup]')"
CHAT_TRIGGER = """(() => { let box = document.querySelector('a[href="/c/' + arg + '"]'), b = null;
    while (box && !(b = box.querySelector('button[aria-haspopup]'))) box = box.parentElement; return b; })()"""
ITEMS = "(m) => [...m.querySelectorAll('.app-dropdown-menu > button[role=menuitem]')].map(b => b.textContent.trim())"


def test_mute_from_open_webuis_own_menus(owui, browser):
    """G: Mute in a chat's ⋯ and a folder's ⋯ (sidebar and folder page), with the DOM contract;
    H3: a folder's name is isolated in the menu's sentences."""
    ctx, page = browser
    token = owui
    page.bring_to_front()
    page.set_viewport_size({"width": 1280, "height": 844})
    name = "מסמכים חשובים של הבית והמשפחה " + uuid.uuid4().hex[:4]   # long, right to left
    folder = api("/api/v1/folders/", {"name": name}, token)["id"]
    g_chat, _ = new_chat(token, "stub", "Chat G")
    api(f"/api/v1/chats/{g_chat}/folder", {"folder_id": folder}, token)
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    if page.locator("#sidebar-open-button").is_visible():
        page.locator("#sidebar-open-button").click()
    row = page.locator(f"#folder-{folder}-button")
    if not row.is_visible():
        page.get_by_text("Folders", exact=True).first.click()
    row.wait_for(timeout=WAIT_MS)
    try:
        # The folder's ⋯ in the sidebar: "Mute folder" right after "Mark all as read", native items untouched.
        menu = native_menu(page, FOLDER_ROW_TRIGGER, folder)
        shot(page, "web-push-g-folder-menu")
        item = menu.get_by_role("menuitem", name="Mute folder")
        item.wait_for(timeout=WAIT_MS)
        items = menu.evaluate(ITEMS)
        assert items.index("Mute folder") == items.index("Mark all as read") + 1, items
        assert [i for i in items if i != "Mute folder"] == ["New Chat", "Create Folder", "Mark all as read", "Edit", "Export", "Delete"], items
        assert item.evaluate("(b) => b.className === b.previousElementSibling.className")
        # Open WebUI redraws the menu while it is open: ours comes back.
        menu.evaluate("(m) => m.querySelectorAll('[data-oc-push-item]').forEach(e => e.remove())")
        menu.get_by_role("menuitem", name="Mute folder").wait_for(timeout=WAIT_MS)
        menu.get_by_role("menuitem", name="Mute folder").click()
        mm = page.locator("#oc-push-mute-menu")
        mm.wait_for(timeout=WAIT_MS)
        # Ours closes Open WebUI's menu, as its own items do, and the focus is on the first choice.
        page.wait_for_function("() => !document.querySelector('[role=menu]:not(#oc-push-mute-menu)')", timeout=WAIT_MS)
        page.wait_for_timeout(MENU_DRAWN_WAIT_MS)
        assert page.evaluate("() => document.activeElement === document.querySelector('#oc-push-mute-menu [role=menuitem]')")
        # H3: the folder's name is isolated in the sentence.
        assert mm.locator("bdi").first.inner_text() == name
        shot(page, "web-push-g-folder-mute-menu")
        mm.get_by_role("menuitem", name="Mute folder always").click()
        page.wait_for_function(f"() => window.ocPushMuted?.id === {json.dumps(folder)}", timeout=WAIT_MS)
        assert json.loads(valves(token)["folder_mutes"]) == {folder: 0}
        # Muted: the item says so the next time.
        menu = native_menu(page, FOLDER_ROW_TRIGGER, folder)
        menu.get_by_role("menuitem", name="Muted").wait_for(timeout=WAIT_MS)
        close_menus(page)

        # The folder's own page: its ⋯ gets the item too.
        page.locator(f'a[href="/folders/{folder}"]').click()
        page.wait_for_function(f"() => location.pathname === '/folders/{folder}'", timeout=WAIT_MS)   # in the app, no load
        page_trigger = ("[...document.querySelectorAll('button[aria-haspopup]')].find(b => !b.closest('#sidebar') "
                        "&& !b.closest('#message-input-container') && b.getAttribute('aria-label') === 'Folder options')")
        page.wait_for_function(f"() => !!({page_trigger})", timeout=WAIT_MS)
        menu = native_menu(page, page_trigger, None)
        menu.get_by_role("menuitem", name="Muted").wait_for(timeout=WAIT_MS)
        close_menus(page)

        # The chat's ⋯ in the sidebar: "Mute notifications" after Mark as read; it opens the mute menu with the folder.
        page.goto(BASE + "/")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        if not page.locator(f'a[href="/c/{g_chat}"]').is_visible():
            row.locator("button[aria-expanded]").first.click()
        page.locator(f'a[href="/c/{g_chat}"]').wait_for(timeout=WAIT_MS)
        menu = native_menu(page, CHAT_TRIGGER, g_chat)
        shot(page, "web-push-g-chat-menu")
        item = menu.locator("[data-oc-push-item=mute]")
        item.wait_for(timeout=WAIT_MS)
        items = menu.evaluate(ITEMS)
        assert items.index("Mark as read") == items.index("Mark as unread") + 1, items
        assert items[items.index("Mark as read") + 1] == item.inner_text().strip(), items
        assert item.inner_text().strip().startswith("Muted"), "the chat is in a muted folder"
        item.click()
        mm.wait_for(timeout=WAIT_MS)
        page.wait_for_function("() => !document.querySelector('[role=menu]:not(#oc-push-mute-menu)')", timeout=WAIT_MS)
        assert "This chat" in mm.inner_text() and mm.locator("bdi").count() >= 1
        mm.get_by_role("menuitem", name="Mute for 1 hour").click()
        page.wait_for_function(f"() => window.ocPushMuted?.id === {json.dumps(g_chat)}", timeout=WAIT_MS)
        assert abs(json.loads(valves(token)["mutes"])[g_chat] - (time.time() + 3600)) < 60
        # Moved to another folder: the chat's menu offers the new folder, not the one it was in.
        second = api("/api/v1/folders/", {"name": "Second " + uuid.uuid4().hex[:4]}, token)
        api(f"/api/v1/chats/{g_chat}/folder", {"folder_id": second["id"]}, token)
        page.wait_for_timeout(MENU_DRAWN_WAIT_MS)
        if not page.locator(f'a[href="/c/{g_chat}"]').count():   # the sidebar redrew it into the new folder
            page.locator(f'#folder-{second["id"]}-button button[aria-expanded]').first.click()
            page.locator(f'a[href="/c/{g_chat}"]').wait_for(timeout=WAIT_MS)
        menu = native_menu(page, CHAT_TRIGGER, g_chat)
        menu.locator("[data-oc-push-item=mute]").click()
        mm.wait_for(timeout=WAIT_MS)
        assert second["name"] in mm.inner_text() and name not in mm.inner_text(), mm.inner_text()
        close_menus(page)

        # Fail safe: a folder ⋯ whose menu is not Open WebUI 0.13's gets nothing, one console line.
        logs = []
        page.on("console", lambda m: logs.append(m.text))
        page.evaluate("""() => { const row = document.createElement('div'); row.id = 'folder-fake-button';
            row.innerHTML = '<button aria-haspopup="true">⋯</button>'; document.body.appendChild(row);
            for (const id of ['bad-folder', 'bad-folder-2']) {
              row.querySelector('button').dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
              const m = document.createElement('div'); m.setAttribute('role', 'menu'); m.id = id;
              m.innerHTML = '<button role="menuitem">Something</button>'; document.body.appendChild(m);
            } }""")
        page.wait_for_function("() => window.ocPushFolderMenuContract === 'missing'", timeout=WAIT_MS)
        page.wait_for_timeout(MENU_DRAWN_WAIT_MS)
        assert page.locator("#bad-folder [role=menuitem], #bad-folder-2 [role=menuitem]").count() == 2
        assert len([l for l in logs if "folder menu" in l]) == 1, logs
        page.evaluate("() => document.querySelectorAll('#bad-folder, #bad-folder-2, #folder-fake-button').forEach(e => e.remove())")
    finally:
        api("/api/v1/functions/id/web_push/valves/user/update", {**valves(token), "mutes": "{}", "folder_mutes": "{}"}, token)
        page.set_viewport_size({"width": 390, "height": 844})

def test_the_mute_menu_on_top_with_a_gutter_and_the_day(owui, browser):
    """H2: an Open WebUI toast over the bell's menu does not take its taps; H4: on a phone the menu keeps
    a gutter at both edges; H5: a mute that ends on another day says which day."""
    ctx, page = browser
    token = owui
    page.bring_to_front()
    page.set_viewport_size({"width": 1280, "height": 844})
    g_chat, _ = new_chat(token, "stub", "Chat H")
    # A long folder name makes the menu's items long (W5's phone menu was flush with the left edge then).
    long_folder = api("/api/v1/folders/", {"name": "תיקייה עם שם ארוך מאוד של פרויקט הבית " + uuid.uuid4().hex[:4]}, token)["id"]
    api(f"/api/v1/chats/{g_chat}/folder", {"folder_id": long_folder}, token)
    mm = page.locator("#oc-push-mute-menu")
    broken = []   # all three are checked, so a run names each one that breaks
    try:
        # H2: a toast of Open WebUI over the menu does not take its taps (the menu is in the top layer).
        page.goto(f"{BASE}/c/{g_chat}")
        bell = page.locator("#oc-push-bell")
        bell.wait_for(timeout=WAIT_MS)
        bell.evaluate("(b) => b.click()")
        mm.wait_for(timeout=WAIT_MS)
        # Added after the menu, so only the top layer keeps the menu above it.
        page.evaluate("""() => { const t = document.createElement('div'); t.id = 'fake-toast';
            t.style.cssText = 'position:fixed;inset:0 0 auto 0;height:60vh;z-index:2147483647;background:#f008';
            document.body.appendChild(t); }""")
        shot(page, "web-push-h2-toast")
        if not mm.evaluate("""(m) => { const i = m.querySelector('[role=menuitem]'); const r = i.getBoundingClientRect();
            return document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2) === i; }"""):
            broken.append("H2: the toast covers the menu")
        close_menus(page)
        page.evaluate("() => document.getElementById('fake-toast').remove()")
        # Escape closes it, and the next press on the bell opens it again.
        bell.click()
        mm.wait_for(timeout=WAIT_MS)
        page.keyboard.press("Escape")
        mm.wait_for(state="detached", timeout=WAIT_MS)
        bell.click()
        try:
            mm.wait_for(timeout=MENU_DRAWN_WAIT_MS * 4)
            close_menus(page)
        except Exception:  # noqa: BLE001 - listed below
            broken.append("Escape: the next bell press did not open the menu")

        # H5: a mute that ends on another day says which day.
        later = int(time.time()) + 2 * 86400
        api("/api/v1/functions/id/web_push/valves/user/update",
            {**valves(token), "mutes": json.dumps({g_chat: later}), "folder_mutes": "{}"}, token)
        page.goto(f"{BASE}/c/{g_chat}")
        page.wait_for_function("() => (document.getElementById('oc-push-bell')?.title || '').startsWith('Muted until')", timeout=WAIT_MS)
        day = page.evaluate("(t) => new Date(t * 1000).toLocaleString([], {weekday: 'short'})", later)
        title = page.locator("#oc-push-bell").get_attribute("title")
        if day not in title:
            broken.append(f"H5: no day ({day}) in {title!r}")

        # H4: on a phone the menu keeps a gutter at both edges.
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{BASE}/c/{g_chat}")
        bell.wait_for(timeout=WAIT_MS)
        bell.click()
        box = mm.bounding_box()
        shot(page, "web-push-h4-h5-phone")
        if not (box["x"] >= 8 and box["x"] + box["width"] <= 390 - 8):
            broken.append(f"H4: no gutter, menu at {box}")
        close_menus(page)
        # A short window (a phone on its side): the menu stays inside it and scrolls.
        page.set_viewport_size({"width": 390, "height": 260})
        bell.click()
        box = mm.bounding_box()
        if not (box["y"] >= 8 and box["y"] + box["height"] <= 260 - 8):
            broken.append(f"short window: menu out of it, at {box}")
        close_menus(page)
        assert not broken, broken
    finally:
        api("/api/v1/functions/id/web_push/valves/user/update", {**valves(token), "mutes": "{}", "folder_mutes": "{}"}, token)
        page.set_viewport_size({"width": 390, "height": 844})


OFFLINE_CACHES = "() => caches.keys()"


def warm(page, chat):
    """The chat opened online under the service worker, so its reads and files are kept."""
    page.goto(f"{BASE}/c/{chat}")
    page.wait_for_function("() => !!navigator.serviceWorker.controller", timeout=WAIT_MS)
    page.reload()   # the first load ran before the worker controlled the page
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    settled(page, """(c) => caches.keys().then(async (ks) => {
        for (const k of ks.filter((k) => k.startsWith('oc-api-')))
            if (await (await caches.open(k)).match(location.origin + '/api/v1/chats/' + c)) return true;
        return false; })""", chat)


def test_offline_open_shows_the_chat_read_last(owui, browser):
    """ELI-352 phase 1: with no network, a reload opens the app and the chat read last, read-only."""
    ctx, page = browser
    token = owui
    page.bring_to_front()
    o_chat, o_msg = new_chat(token, "stub", "Chat Offline")
    reply(token, "stub", o_chat, o_msg)
    wait_done(token, o_chat, o_msg)
    text = [m for m in api(f"/api/v1/chats/{o_chat}", token=token)["chat"]["history"]["messages"].values()
            if m["role"] == "assistant"][0]["content"][:40]
    warm(page, o_chat)
    ctx.set_offline(True)
    try:
        page.reload(timeout=WAIT_MS)
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        page.get_by_text(text.strip().splitlines()[0][:30], exact=False).first.wait_for(timeout=WAIT_MS)
        if os.environ.get("E2E_SHOTS"):
            page.screenshot(path=os.path.join(os.environ["E2E_SHOTS"], "web-push-offline-chat.png"))
    finally:
        ctx.set_offline(False)


def test_offline_keeps_no_redirect_and_drops_an_old_builds_files(owui, browser):
    """A redirect (an expired Cloudflare Access session) is never kept as the app's page; a new page
    (a new Open WebUI build) drops the hashed files of the old one."""
    ctx, page = browser
    token = owui
    page.bring_to_front()
    r_chat, _ = new_chat(token, "stub", "Chat Redirect")
    warm(page, r_chat)
    kept = "() => caches.open('oc-shell-v1').then((c) => c.match('/')).then((r) => r ? r.text() : '')"
    page_before = page.evaluate(kept)
    assert "/_app/immutable/" in page_before
    # Access's answer to an expired session: a redirect to its login page.
    login = "<html><body>Cloudflare Access login</body></html>"
    ctx.route("**/access-login", lambda r: r.fulfill(status=200, content_type="text/html", body=login))
    to_login = lambda r: r.fulfill(status=302, headers={"location": f"{BASE}/access-login"})
    # Access answers every request of an expired session so, /api/version too.
    ctx.route(f"**/c/{r_chat}", to_login)
    ctx.route("**/api/version", to_login)
    try:
        page.goto(f"{BASE}/c/{r_chat}")
        page.wait_for_url("**/access-login", timeout=WAIT_MS)
        page.wait_for_timeout(MENU_DRAWN_WAIT_MS)
        assert "Access login" not in page.evaluate(kept)
    finally:
        ctx.unroute(f"**/c/{r_chat}"); ctx.unroute("**/api/version"); ctx.unroute("**/access-login")
    # An old build's file and the page it came with; the next page load drops it.
    page.goto(f"{BASE}/c/{r_chat}")
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    page.evaluate("""() => caches.open('oc-shell-v1').then(async (c) => {
        await c.put('/_app/immutable/old-build.js', new Response('// old'));
        await c.put('/_oc/page-files', new Response('/_app/immutable/old-build.js')); })""")
    page.reload()
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    settled(page, """() => caches.open('oc-shell-v1').then((c) => c.match('/_app/immutable/old-build.js'))
        .then((r) => !r)""")


# --- ELI-352 PR 2: the error page after a network change, a newer Open WebUI, the
# offline pill, another user on the same browser, the kill switch.

APP_FILE = "**/_app/immutable/nodes/0.*"
CUT_ATTEMPTS = 2   # the service worker asks a read cut on its way once more (oc-sw.js again())   # Open WebUI's root layout: without it the app cannot start
ERROR_PAGE = "() => !!document.querySelector('body > .error > .status')"
RELOADED_FLAG = "() => sessionStorage.getItem('ocReloadedAfterError')"
# The kept copy of that file (offline open on) would hide a network failure: the test drops it.
DROP_APP_FILE = """() => caches.open('oc-shell-v1').then(async (c) => {
    for (const k of await c.keys()) if (k.url.includes('/nodes/0.')) await c.delete(k); })"""
SHELL_VERSION = "() => caches.open('oc-shell-v1').then((c) => c.match('/_oc/version')).then((r) => r ? r.text() : null)"
NO_OFFLINE_CACHES = "() => caches.keys().then((ks) => !ks.some((k) => k.startsWith('oc-')))"


def offline_on():
    """The stack's Worker serves oc-sw.js with offline open on (E2E_OFFLINE, default 1)."""
    with urllib.request.urlopen(BASE + "/oc-sw.js", timeout=30) as r:
        return 'OFFLINE_SETTING = "1"' in r.read().decode()


needs_offline = pytest.mark.skipif("not offline_on()", reason="offline open is off on this stack (E2E_OFFLINE=0)")
needs_offline_off = pytest.mark.skipif("offline_on()", reason="offline open is on on this stack (E2E_OFFLINE=1)")


def chats_on():
    """The stack's Worker serves oc-sw.js with kept chats on (E2E_CHAT_CACHE, default 1; with offline open)."""
    with urllib.request.urlopen(BASE + "/oc-sw.js", timeout=30) as r:
        sw = r.read().decode()
    return 'OFFLINE_SETTING = "1"' in sw and 'CHAT_CACHE_SETTING = "1"' in sw


needs_chats = pytest.mark.skipif("not chats_on()", reason="kept chats are off on this stack (E2E_CHAT_CACHE=0)")


def opened(page, chat):
    """The chat opened under the service worker (both modes)."""
    page.goto(f"{BASE}/c/{chat}")
    page.wait_for_function("() => !!navigator.serviceWorker.controller", timeout=WAIT_MS)
    page.reload()
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)


def page_loads(page):
    """The page's loads from here on (each navigation request of its main frame)."""
    loads = []
    def seen(r):
        if r.is_navigation_request() and r.frame == page.main_frame:
            loads.append(r.url)
    page.on("request", seen)
    return loads, lambda: page.remove_listener("request", seen)


def test_the_error_page_after_a_network_change_reloads_once_into_the_chat(owui, browser):
    """A network change while the app loads fails one of Open WebUI's files and SvelteKit shows
    "500 Internal Error" for good. The page script reloads once, the server being reachable, and
    the chat opens; a load that gets to the app clears the guard."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Network Change")
    opened(page, chat)
    failed = []
    def fail_once(route):
        # The first load's request fails: the page's own, or (offline open on) the service
        # worker's and the one retry it makes.
        mine = [k for k in failed if k == bool(route.request.service_worker)]
        if len(mine) >= (CUT_ATTEMPTS if route.request.service_worker else 1):
            return route.continue_()
        failed.append(bool(route.request.service_worker))
        route.abort("internetdisconnected")   # what ERR_NETWORK_CHANGED does to the import
    loads, stop = page_loads(page)
    page.evaluate(DROP_APP_FILE)
    ctx.route(APP_FILE, fail_once)
    try:
        page.reload()
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        assert failed, "Open WebUI's file never failed"
        assert len(loads) == 2, loads   # the load that failed, the one reload
        page.wait_for_function(f"() => ({RELOADED_FLAG})() === null", timeout=WAIT_MS)
    finally:
        ctx.unroute(APP_FILE, fail_once)
        stop()


BOOT_READS = ("**/api/v1/users/user/settings", "**/api/v1/chats/?page=1", "**/api/v1/chats/pinned")
PINNED_PATH = "/api/v1/chats/pinned"
KEPT_APP = "oc-shell-v1"   # oc-sw.js SHELL
NO_API_CACHES = "() => caches.keys().then((ks) => Promise.all(ks.filter((k) => k.startsWith('oc-api-')).map((k) => caches.delete(k))))"


@needs_offline
def test_a_network_change_during_the_first_load_still_opens_the_chat(owui, browser):
    """A change of network fails every request on its way (ERR_NETWORK_CHANGED). On a load with
    nothing kept yet, Open WebUI's own reads failing leave it half drawn for good (no chat, no
    error page): the service worker asks a failed read once more, and the chat opens."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Changed Network")
    opened(page, chat)
    page.evaluate(NO_API_CACHES)   # nothing kept: as on the first load the worker handles
    failed = []
    def fail_once(route):
        if route.request.service_worker is None or route.request.url in failed:
            return route.fallback()
        failed.append(route.request.url)
        route.abort("connectionreset")   # a request cut on its way, as a network change does
    for r in BOOT_READS:
        ctx.route(r, fail_once)
    try:
        page.reload()
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        page.get_by_text("Chat Changed Network").first.wait_for(timeout=WAIT_MS)   # the chat list too
        assert len(failed) == len(BOOT_READS), failed
    finally:
        for r in BOOT_READS:
            ctx.unroute(r, fail_once)


@needs_offline_off
def test_start_reads_cut_by_a_network_change_still_open_the_chat(owui, pw):
    """Offline open off (the worker answers nothing): Open WebUI's start reads cut once on their
    way (a change of network) are asked once more by the page script, and the chat and the chat
    list open. Its own browser: nothing kept from other tests."""
    chat, _ = new_chat(owui, "stub", "Chat Start Cut")
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/c/{chat}")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        cut = []
        def cut_once(route):
            if route.request.method != "GET" or route.request.url in cut:
                return route.fallback()
            cut.append(route.request.url)
            route.abort("connectionreset")   # a request cut on its way, as a network change does
        for r in BOOT_READS:
            ctx.route(r, cut_once)
        try:
            page.reload()
            page.locator("#chat-input").wait_for(timeout=WAIT_MS)
            sidebar_link(page, chat).wait_for(timeout=WAIT_MS)   # the chat list's entry
            assert len(cut) == len(BOOT_READS), cut
        finally:
            for r in BOOT_READS:
                ctx.unroute(r, cut_once)
    finally:
        ctx.close()


# Counts the calls of fetch for a path made from the outside (Open WebUI's) and their ends. The
# page script and Open WebUI each wrap window.fetch: whoever reads it gets the top wrapper at that
# time, and a call counts only when it goes to the top one, not from one wrapper to the next.
OUTER_FETCH = """(path) => {
    const seen = window.__ocOuter = {calls: 0, ends: 0};
    const layers = [window.fetch];
    const view = (k) => function (input, init) {
        const p = layers[k].call(this, input, init);
        if (k !== layers.length - 1) return p;
        if (new URL(input instanceof Request ? input.url : String(input), location.href).pathname !== path) return p;
        seen.calls++;
        return p.finally(() => { seen.ends++; });
    };
    Object.defineProperty(window, 'fetch', {configurable: true, get: () => view(layers.length - 1),
                                            set: (f) => { layers.push(f); }});
}"""


@needs_offline
def test_a_start_read_the_worker_gave_up_on_is_not_asked_again_by_the_page(owui, pw):
    """Offline open on: the worker already asks a cut start read once more (oc-sw.js again()). A
    read that fails both times is final: the page script does not ask it again (two more
    attempts through the worker)."""
    chat, _ = new_chat(owui, "stub", "Chat Start Cut Twice")
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    ctx.add_init_script(f"({OUTER_FETCH})({json.dumps(PINNED_PATH)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/c/{chat}")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        page.wait_for_function("() => !!navigator.serviceWorker.controller", timeout=WAIT_MS)
        page.reload()   # a load the worker answers: the app is kept
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        settled(page, f"() => caches.has({json.dumps(KEPT_APP)})")
        page.evaluate(NO_API_CACHES)   # no kept answer to fall back on: the failure reaches the page
        attempts = []
        def cut_always(route):
            if route.request.method != "GET":
                return route.fallback()
            attempts.append(bool(route.request.service_worker))
            route.abort("connectionreset")
        ctx.route("**" + PINNED_PATH, cut_always)
        try:
            page.reload()
            page.locator("#chat-input").wait_for(timeout=WAIT_MS)   # (the pinned list failing for good, no chat list)
            # Every call Open WebUI made has its answer (it may ask the list more than once).
            page.wait_for_function("() => window.__ocOuter.ends >= 1 && window.__ocOuter.ends === window.__ocOuter.calls",
                                   timeout=WAIT_MS)
            calls = page.evaluate("() => window.__ocOuter.calls")
            assert attempts == [True] * (CUT_ATTEMPTS * calls), (calls, attempts)
        finally:
            ctx.unroute("**" + PINNED_PATH, cut_always)
    finally:
        ctx.close()


def test_the_error_page_reloads_only_once(owui, browser):
    """The reload fails too: the page stays on the error page, and neither 'online' nor a
    network change reloads it again."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Error Twice")
    opened(page, chat)
    fail = lambda route: route.abort("internetdisconnected")
    loads, stop = page_loads(page)
    page.evaluate(DROP_APP_FILE)
    ctx.route(APP_FILE, fail)
    try:
        page.reload()
        # The one reload, then its error page (the first error page ends in that reload).
        end = time.time() + WAIT_MS / 1000
        while len(loads) < 2:
            assert time.time() < end, "the page was never reloaded"
            page.wait_for_timeout(STATE_POLL_S * 1000)   # a poll; Playwright delivers the page's events meanwhile
        page.wait_for_function(f"() => ({ERROR_PAGE})() && ({RELOADED_FLAG})() === '1'", timeout=WAIT_MS)
        page.evaluate("() => { window.ocStayed = true; dispatchEvent(new Event('online')); }")
        assert page.evaluate("() => window.ocStayed === true")
        assert len(loads) == 2, loads
    finally:
        ctx.unroute(APP_FILE, fail)
        stop()
    opened(page, chat)   # a load that gets to the app clears the guard
    page.wait_for_function(f"() => ({RELOADED_FLAG})() === null", timeout=WAIT_MS)


# Longer than the navigation's 3 s network wait before this PR: with it, the kept app
# of the old version was used.
UPGRADED_PAGE_DELAY_S = 5
UPGRADED_VERSION = '{"version":"99.0.0-e2e","deployment_id":"e2e-upgrade"}'
UPGRADED_MARK = '<meta name="oc-e2e-build" content="upgraded">'


@needs_offline
def test_a_newer_open_webui_never_runs_the_kept_app(owui, browser):
    """The server is a newer Open WebUI (another /api/version) and its page is slow: the kept
    app of the old version is not used, the caches go, and the newer page opens and is kept
    with its version. Back on the old server, the same again the other way."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Upgrade")
    warm(page, chat)
    real = urllib.request.urlopen(OWUI + "/api/version", timeout=30).read().decode()
    html = urllib.request.urlopen(OWUI + "/", timeout=30).read().decode().replace("<head>", "<head>" + UPGRADED_MARK, 1)
    class Slow(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(UPGRADED_PAGE_DELAY_S)   # the upgraded server's slow page
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from_worker = []
    def upgraded_page(route):
        if route.request.service_worker is None:
            return route.fallback()
        from_worker.append(route.request.url)
        route.continue_(url=f"http://127.0.0.1:{srv.server_address[1]}/")
    def upgraded_version(route):
        if route.request.service_worker is None:
            return route.fallback()
        route.fulfill(status=200, content_type="application/json", body=UPGRADED_VERSION)
    ctx.route(f"**/c/{chat}", upgraded_page)
    ctx.route("**/api/version", upgraded_version)
    try:
        page.goto(f"{BASE}/c/{chat}", timeout=WAIT_MS)
        assert from_worker, "the service worker never asked the server for the page"
        # The kept app may open first; told the version differs, the page reloads onto the newer one.
        page.wait_for_function("() => !!document.querySelector('meta[name=oc-e2e-build]')", timeout=WAIT_MS)
        settled(page, f"() => ({SHELL_VERSION})().then((v) => v === {json.dumps(UPGRADED_VERSION)})")
    finally:
        ctx.unroute(f"**/c/{chat}", upgraded_page)
        ctx.unroute("**/api/version", upgraded_version)
        srv.shutdown()
    # The real server again: its version differs from the kept one, so its own page.
    page.goto(f"{BASE}/c/{chat}", timeout=WAIT_MS)
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    page.wait_for_function("() => !document.querySelector('meta[name=oc-e2e-build]')", timeout=WAIT_MS)
    settled(page, f"() => ({SHELL_VERSION})().then((v) => v === {json.dumps(real)})")


# Shorter than the 4.2 s lie-fi wait of the worker before this change (so that one waited for
# the answer), long enough to see the app drawn without it.
SLOW_VERSION_DELAY_S = 3


@needs_offline
def test_the_kept_app_opens_without_waiting_for_the_version(owui, browser):
    """The kept app (kept with its version) opens at once: the server's /api/version is asked
    alongside it, not before it."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Instant")
    warm(page, chat)
    real = urllib.request.urlopen(OWUI + "/api/version", timeout=30).read()
    answered = threading.Event()
    class SlowVersion(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(SLOW_VERSION_DELAY_S)   # a slow network's answer
            answered.set()
            body, kind = (real, "application/json") if self.path == "/api/version" else (page_html, "text/html")
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowVersion)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    page_html = urllib.request.urlopen(OWUI + "/", timeout=30).read()
    def slow(route):
        if route.request.service_worker is None:
            return route.fallback()
        route.continue_(url=f"http://127.0.0.1:{srv.server_address[1]}{urllib.parse.urlsplit(route.request.url).path}")
    # A slow network: the page and the version both slow (a fast page alone would be used as it comes).
    ctx.route("**/api/version", slow)
    ctx.route(f"**/c/{chat}", slow)
    try:
        page.reload(wait_until="commit")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        assert not answered.is_set(), "the app waited for the network"
    finally:
        ctx.unroute("**/api/version", slow)
        ctx.unroute(f"**/c/{chat}", slow)
        answered.wait(WAIT_MS / 1000)
        srv.shutdown()


VALVES_READ = "/api/v1/functions/id/web_push/valves/user"


def test_a_load_reads_the_users_valves_once(owui, pw):
    """Open WebUI navigates a few times while it starts; the page script reads the user's valves
    (mutes, devices) once for all of them. Notifications not asked yet (no subscription to save)."""
    chat, _ = new_chat(owui, "stub", "Chat One Read")
    body = json.dumps(api(VALVES_READ, token=owui)).encode()
    reads, answered = [], threading.Event()
    def answer(path):
        reads.append(path)
        answered.set()
        return body, "application/json"
    srv = slow_server(answer)
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/c/{chat}")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        # A slow answer (a slow network): Open WebUI's start navigations come while it is on its way.
        ctx.route(f"**{VALVES_READ}", lambda route: route.continue_(url=f"http://127.0.0.1:{srv.server_address[1]}/")
                  if route.request.method == "GET" else route.fallback())
        page.reload()
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        assert answered.wait(WAIT_MS / 1000), "the page script never read the valves"
        # Open WebUI's start navigations all came while that first read was on its way.
        assert len(reads) == 1, reads
    finally:
        ctx.close()
        srv.shutdown()


SLOW_ANSWER_S = 2   # a Slow 3G round trip


def slow_server(answer, delay=SLOW_ANSWER_S, gate=None):
    """A local server whose answers come late (or, with `gate`, only once it is set)."""
    class Slow(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if gate is not None:
                gate.wait(WAIT_MS / 1000)
            else:
                time.sleep(delay)
            body, kind = answer(self.path)
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@needs_offline
def test_the_kept_app_runs_while_the_version_check_gets_no_answer(owui, browser):
    """A network that says nothing (lie-fi): the kept app opens and draws the chat; the version
    check stays pending beside it, and nothing in the app waits for it."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Silent")
    warm(page, chat)
    silent = threading.Event()
    srv = slow_server(lambda path: (b"{}", "application/json"), gate=silent)
    def no_answer(route):
        if route.request.service_worker is None:
            return route.fallback()
        route.continue_(url=f"http://127.0.0.1:{srv.server_address[1]}/api/version")
    ctx.route("**/api/version", no_answer)
    try:
        page.reload(wait_until="commit")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        page.get_by_text("Chat Silent").first.wait_for(timeout=WAIT_MS)
        assert not silent.is_set()
    finally:
        ctx.unroute("**/api/version", no_answer)
        silent.set()
        srv.shutdown()


@needs_offline
def test_a_login_instead_of_a_version_drops_a_page_kept_without_one(owui, browser):
    """A page kept without its version (by an earlier worker) and an expired Access session: the
    login shows, and the kept page goes."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Old Kept")
    warm(page, chat)
    page.evaluate("() => caches.open('oc-shell-v1').then((c) => c.delete('/_oc/version'))")
    login = "<html><body>Cloudflare Access login</body></html>"
    to_login = lambda r: r.fulfill(status=302, headers={"location": f"{BASE}/access-login"})
    ctx.route("**/access-login", lambda r: r.fulfill(status=200, content_type="text/html", body=login))
    ctx.route(f"**/c/{chat}", to_login)
    ctx.route("**/api/version", to_login)
    try:
        page.goto(f"{BASE}/c/{chat}")
        page.wait_for_url("**/access-login", timeout=WAIT_MS)
        settled(page, "() => caches.open('oc-shell-v1').then((c) => c.match('/')).then((r) => !r)")
    finally:
        ctx.unroute(f"**/c/{chat}"); ctx.unroute("**/api/version"); ctx.unroute("**/access-login")


MODEL_AVATARS = "img[src*='/models/model/profile/image']"
AVATARS_DRAWN = f"""() => {{ const i = [...document.querySelectorAll("{MODEL_AVATARS}")];
    return i.length > 0 && i.every((x) => x.complete && x.naturalWidth > 0); }}"""
COMPOSER_LOCKED = "() => !!document.getElementById('chat-input')?.closest('form')?.inert"


@needs_offline
def test_offline_is_read_only_with_a_pill_and_keeps_the_avatars(owui, browser):
    """Opened offline, the saved chat shows a pill and a locked message box (typing does nothing,
    so nothing is written to be lost) and the model's picture (kept), not a broken image. The
    network going while text is in the box: the text stays, locked, and is sent once online."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Pill")
    warm(page, chat)
    page.wait_for_function(AVATARS_DRAWN, timeout=WAIT_MS)
    box = page.locator("#chat-input")
    pill = page.locator("#oc-offline")
    ctx.set_offline(True)
    try:
        page.reload(timeout=WAIT_MS)
        box.wait_for(timeout=WAIT_MS)
        pill.wait_for(timeout=WAIT_MS)
        assert pill.inner_text() == "Offline: saved chats, read-only"
        page.wait_for_function(COMPOSER_LOCKED, timeout=WAIT_MS)
        page.wait_for_function(AVATARS_DRAWN, timeout=WAIT_MS)
        if os.environ.get("E2E_SHOTS"):
            page.screenshot(path=os.path.join(os.environ["E2E_SHOTS"], "web-push-offline-pill.png"))
        box.click(force=True)
        page.keyboard.type("typed offline")
        page.keyboard.press("Enter")
        assert "typed offline" not in box.inner_text(), "the locked box took text offline"
    finally:
        ctx.set_offline(False)
    pill.wait_for(state="detached", timeout=WAIT_MS)
    page.wait_for_function(f"() => !({COMPOSER_LOCKED})()", timeout=WAIT_MS)
    # Text in the box when the network goes.
    text = "written before the network went"
    box.click()
    page.keyboard.type(text)
    ctx.set_offline(True)
    try:
        pill.wait_for(timeout=WAIT_MS)
        page.wait_for_function(COMPOSER_LOCKED, timeout=WAIT_MS)
        page.keyboard.press("Enter")
        assert text in box.inner_text(), "the text in the box was lost"
        assert not any(m.get("content") == text for m in history(owui, chat)["messages"].values())
    finally:
        ctx.set_offline(False)
    page.wait_for_function(f"() => !({COMPOSER_LOCKED})()", timeout=WAIT_MS)
    box.click()
    page.keyboard.press("Enter")
    end = time.time() + WAIT_MS / 1000
    while not any(m.get("content") == text for m in history(owui, chat)["messages"].values()):
        assert time.time() < end, "the kept text was never sent"
        time.sleep(STATE_POLL_S)


@needs_offline
def test_another_user_on_the_same_browser_never_sees_the_first_users_chats(owui, pw):
    """User A's kept reads go when user B signs in on the same browser; offline, B's app shows
    none of A's chats."""
    a_chat, _ = new_chat(owui, "stub", "Chat Of User A " + uuid.uuid4().hex[:6])
    a_title = api(f"/api/v1/chats/{a_chat}", token=owui)["title"]
    b_cred = {"name": "E2E B", "email": f"b-{uuid.uuid4().hex[:8]}@e2e.example.com", "password": "e2e-pass-b"}
    b_token = api("/api/v1/auths/signup", b_cred)["token"]
    api("/api/v1/users/user/settings/update", {"ui": {"showChangelog": False}}, b_token)
    ctx = chrome(pw)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE + "/auth")
        page.evaluate("(t) => localStorage.setItem('token', t)", owui)
        warm(page, a_chat)
        page.get_by_text(a_title).first.wait_for(timeout=WAIT_MS)
        a_caches = page.evaluate("() => caches.keys().then((ks) => ks.filter((k) => k.startsWith('oc-api-')))")
        assert len(a_caches) == 1, a_caches
        # B signs in on this browser (Open WebUI keeps the token in localStorage).
        page.evaluate("(t) => localStorage.setItem('token', t)", b_token)
        page.goto(BASE + "/")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        settled(page, """(a) => caches.keys().then((ks) => !ks.includes(a)
            && ks.some((k) => k.startsWith('oc-api-')))""", a_caches[0])
        # B asks for A's chat by its address, as Open WebUI would: the server's answer, never A's kept copy.
        got = page.evaluate("""([c, t]) => fetch('/api/v1/chats/' + c, {headers: {authorization: 'Bearer ' + t}})
            .then(async (r) => ({status: r.status, kept: r.headers.get('x-oc-kept'), body: await r.text()}))""",
                            [a_chat, b_token])
        assert got["status"] != 200 and not got["kept"] and a_title not in got["body"], got
        ctx.set_offline(True)
        page.reload(timeout=WAIT_MS)
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        page.locator("#oc-offline").wait_for(timeout=WAIT_MS)
        assert page.get_by_text(a_title).count() == 0
        found = page.evaluate("""(c) => caches.keys().then(async (ks) => {
            for (const k of ks) if (await (await caches.open(k)).match(location.origin + '/api/v1/chats/' + c)) return k;
            return null; })""", a_chat)
        assert found is None, found
    finally:
        ctx.close()


UPDATING_PILL = "#oc-updating"


def say(token, chat_id, message_id, text):
    """The chat's assistant message says `text` on the server (a reply that came in elsewhere)."""
    c = api(f"/api/v1/chats/{chat_id}", token=token)["chat"]
    c["history"]["messages"][message_id].update({"content": text, "done": True})
    c["messages"] = list(c["history"]["messages"].values())
    api(f"/api/v1/chats/{chat_id}", {"chat": {"history": c["history"], "messages": c["messages"]}}, token)


def slow_network(ctx, token, chat_id):
    """A slow network: every read of Open WebUI's API for the page (with the page's
    Authorization; the worker's or the page's own) waits at a gate, then gets the server's
    answer as it is. Not only the chat's: Open WebUI draws a chat once its other reads are in too (its tags, its running tasks,
    the tools), so a test that slows only the chat's read misses what a slow phone sees. The
    worker's own reads (its cookie, a notification's reply) go at once. Returns the gate and a
    function that undoes it."""
    gate = threading.Event()

    class Held(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            gate.wait(WAIT_MS / 1000)
            req = urllib.request.Request(OWUI + self.path, headers={"Authorization": "Bearer " + token})
            try:
                r = urllib.request.urlopen(req, timeout=30)
                status, kind, body = r.status, r.headers.get("Content-Type", "application/json"), r.read()
            except urllib.error.HTTPError as err:
                status, kind, body = err.code, err.headers.get("Content-Type", "application/json"), err.read()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Held)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def held(route):
        # The page's own reads too (those the worker lets through go from the page).
        if route.request.method != "GET" or "authorization" not in route.request.headers:
            return route.fallback()
        u = urllib.parse.urlsplit(route.request.url)
        route.continue_(url=f"http://127.0.0.1:{srv.server_address[1]}{u.path}" + (f"?{u.query}" if u.query else ""))
    ctx.route("**/api/**", held)

    def undo():
        ctx.unroute("**/api/**", held)
        gate.set()
        srv.shutdown()
    return gate, undo


def warm_kept(page, chat):
    """warm(), and the tools list kept too: Open WebUI waits for it before it draws a chat after a
    load, and a person who opened the chat before has it (warm() waits for the chat only).
    For the worker with kept chats: main's never keeps the tools list."""
    warm(page, chat)
    settled(page, """() => caches.keys().then(async (ks) => {
        for (const k of ks.filter((k) => k.startsWith('oc-api-')))
            if (await (await caches.open(k)).match(location.origin + '/api/v1/tools/?')) return true;
        return false; })""")


def kept_then_fresh(page, gate, old, new):
    """The chat shows `old` (the kept copy) while the network has nothing, marked and read-only;
    once the network's copy comes, `new`, unmarked and writable."""
    page.get_by_text(old).first.wait_for(timeout=WAIT_MS)
    page.locator(UPDATING_PILL).wait_for(timeout=WAIT_MS)
    page.wait_for_function(COMPOSER_LOCKED, timeout=WAIT_MS)
    assert page.get_by_text(new).count() == 0
    gate.set()
    page.get_by_text(new).first.wait_for(timeout=WAIT_MS)
    page.locator(UPDATING_PILL).wait_for(state="detached", timeout=WAIT_MS)
    page.wait_for_function(f"() => !({COMPOSER_LOCKED})()", timeout=WAIT_MS)


@needs_chats
def test_a_chat_opened_before_shows_at_once_and_the_fresh_copy_replaces_it(owui, pw):
    """Opened again with the network slow, a chat shows the copy kept of it at once, with
    "Updating…" and the message box locked; Open WebUI's own refresh then puts the network's copy
    in its place. The same from a notification's tap while another chat is open. Its own browser."""
    tag = uuid.uuid4().hex[:6]
    chat, answer = new_chat(owui, "stub", "Chat Kept " + tag)
    other, _ = new_chat(owui, "stub", "Chat Other " + tag)
    say(owui, chat, answer, "Kept reply " + tag)
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        warm_kept(page, chat)
        say(owui, chat, answer, "Fresh reply " + tag)   # came in while the chat was closed
        gate, undo = slow_network(ctx, owui, chat)
        try:
            page.reload()
            kept_then_fresh(page, gate, "Kept reply " + tag, "Fresh reply " + tag)
        finally:
            undo()
        # Another chat open; a notification for this one, a newer reply on the server.
        page.goto(f"{BASE}/c/{other}")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        say(owui, chat, answer, "Newest reply " + tag)
        gate, undo = slow_network(ctx, owui, chat)
        try:
            tap(page, "", {"chat_id": chat, "url": f"/c/{chat}"})
            page.wait_for_function("(c) => location.pathname === '/c/' + c", arg=chat, timeout=WAIT_MS)
            kept_then_fresh(page, gate, "Fresh reply " + tag, "Newest reply " + tag)
        finally:
            undo()
    finally:
        ctx.close()


@needs_chats
def test_the_kept_chat_is_replaced_even_with_no_socket(owui, pw):
    """Open WebUI's socket never comes up (it refreshes a chat only when it joins the chat's
    room): once the network's copy is in, the page reloads into it, unmarked and writable."""
    tag = uuid.uuid4().hex[:6]
    chat, answer = new_chat(owui, "stub", "Chat No Socket " + tag)
    say(owui, chat, answer, "Kept reply " + tag)
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        warm_kept(page, chat)
        say(owui, chat, answer, "Fresh reply " + tag)
        ctx.route_web_socket("**/ws/socket.io/**", lambda ws: None)   # connects, the server never speaks
        gate, undo = slow_network(ctx, owui, chat)
        try:
            page.reload()
            kept_then_fresh(page, gate, "Kept reply " + tag, "Fresh reply " + tag)
        finally:
            undo()
    finally:
        ctx.close()


@needs_chats
def test_an_unsent_reply_waiting_for_a_kept_chat_stays_if_the_page_goes_elsewhere(owui, pw):
    """A reply that could not be sent waits for the message box of its chat (locked while the
    chat is its kept copy). The page goes to another chat meanwhile: the reply is not put in
    that chat's box, and it stays kept on the device."""
    tag = uuid.uuid4().hex[:6]
    chat, _ = new_chat(owui, "stub", "Chat Unsent " + tag)
    api(f"/api/v1/chats/{chat}", {"chat": {"models": ["no-such-model"]}}, owui)
    other, _ = new_chat(owui, "stub", "Chat Elsewhere " + tag)
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    ctx.add_cookies([{"name": "token", "value": owui, "url": BASE}])
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        warm_kept(page, chat)
        page.goto(f"{BASE}/c/{other}")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        gate, undo = slow_network(ctx, owui, chat)
        try:
            text = "unsent " + tag
            tap(page, "reply", {"chat_id": chat, "url": f"/c/{chat}", "fn": "web_push"}, text)
            page.wait_for_function("(c) => location.pathname === '/c/' + c", arg=chat, timeout=WAIT_MS)
            page.locator(UPDATING_PILL).wait_for(timeout=WAIT_MS)   # the box locked: the reply waits
            tap(page, "", {"chat_id": other, "url": f"/c/{other}"})   # elsewhere, in the app
            page.wait_for_function("(c) => location.pathname === '/c/' + c", arg=other, timeout=WAIT_MS)
            page.wait_for_function("() => !!window.ocPushReply", timeout=WAIT_MS)
            assert page.evaluate("() => window.ocPushReply.put") is False
            # Not in the other chat's box (the network slow, it may not be drawn yet: then no box at all).
            assert text not in page.evaluate("() => document.querySelector('#chat-input')?.innerText ?? ''")
            assert page.evaluate("(c) => caches.open('oc-push-drafts').then((d) => d.match('/oc-push/draft/' + c)).then((r) => !!r)", chat)
        finally:
            undo()
    finally:
        ctx.close()


@needs_offline
def test_a_sign_out_drops_the_kept_chats(owui, pw):
    """Open WebUI's sign-out (POST /api/v1/auths/signout): nothing of the user's reads stays."""
    cred = {"name": "E2E Out", "email": f"out-{uuid.uuid4().hex[:8]}@e2e.example.com", "password": "e2e-pass-out"}
    token = api("/api/v1/auths/signup", cred)["token"]
    api("/api/v1/users/user/settings/update", {"ui": {"showChangelog": False}}, token)
    chat, _ = new_chat(token, "stub", "Chat Signed Out")
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(token)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        warm_kept(page, chat)
        page.evaluate("() => fetch('/api/v1/auths/signout', {method: 'POST', credentials: 'include'})")
        settled(page, "() => caches.keys().then((ks) => !ks.some((k) => k.startsWith('oc-api-')))")
    finally:
        ctx.close()


def worker_offline(value):
    """The stack Worker's OFFLINE var, the Worker restarted with it (stack.sh offline)."""
    subprocess.run(["bash", os.path.join(HERE, "stack.sh"), "offline", os.environ["E2E_RUN"], value],
                   check=True, capture_output=True, timeout=WAIT_MS / 1000)
    assert f'OFFLINE_SETTING = "{value}"' in urllib.request.urlopen(BASE + "/oc-sw.js", timeout=30).read().decode()


@needs_offline
@pytest.mark.skipif(not os.environ.get("E2E_RUN"), reason="set E2E_RUN=<run_dir> (stack.sh up) to turn the Worker's switch")
def test_the_kill_switch_drops_the_caches_and_stops_serving(owui, pw):
    """The Worker's OFFLINE turned to "0": the browser's next update of the service worker
    deletes its caches, and it answers nothing itself (offline, Chrome's own offline page).
    Its own browser: a request another test intercepted can keep the old worker busy, and a
    new one waits for it."""
    chat, _ = new_chat(owui, "stub", "Chat Kill Switch")
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        warm_kept(page, chat)
        worker_offline("0")
        page.evaluate("() => navigator.serviceWorker.getRegistration('/').then((r) => r.update())")
        # The new worker takes over once the old one is idle; the next open comes after that.
        settled(page, """() => navigator.serviceWorker.getRegistration('/').then((r) =>
            r.active && r.active.state === 'activated' && !r.waiting && !r.installing)""")
        page.reload()   # the next open
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        settled(page, NO_OFFLINE_CACHES)
        ctx.set_offline(True)
        with pytest.raises(Exception, match="ERR_INTERNET_DISCONNECTED"):
            page.reload(timeout=WAIT_MS)
    finally:
        ctx.close()
        worker_offline("1")
