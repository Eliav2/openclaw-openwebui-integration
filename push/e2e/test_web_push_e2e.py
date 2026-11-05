"""Web Push end to end: real Google Chrome, the real push service (FCM), the
Cloudflare Worker example under `wrangler dev`, a throwaway Open WebUI.

    push/e2e/stack.sh up <run_dir> <openclaw_pipe.py build>
    WEB_PUSH_E2E=1 xvfb-run -a uv run --with playwright==1.64.0 --with pytest pytest -q push/e2e/test_web_push_e2e.py
    push/e2e/stack.sh down <run_dir>

Push needs Google Chrome (Chromium builds have no push service), a persistent
profile and background networking, so this runs headed under Xvfb. Every wait
is on state: a service worker decision (`oc-push` message), a notification,
a saved subscription. None waits on time.
"""
import json
import os
import subprocess
import time
import tempfile
import urllib.error
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
                            "message_id": a_msg, "title": "Chat A", "url": f"/c/{a_chat}", "fn": "web_push"}
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
    # ... or Open WebUI's own navigation to another chat (SvelteKit's pushState).
    page.evaluate("(c) => history.pushState({}, '', '/c/' + c)", chats["Y"])
    assert read(page, chats["Y"]) == [tag["Z"]]
    # In the background, a chat on the page is not read; coming back to the front on it is.
    other()
    page.evaluate("(c) => history.replaceState({}, '', '/c/' + c)", chats["Z"])
    assert page.evaluate("() => window.ocPushRead") == chats["Y"]
    page.bring_to_front()
    assert read(page, chats["Z"]) == []
