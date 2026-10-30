"""User flows driven through a real browser, asserting on what the user sees.

One test per flow, on the same stack as test_e2e.py (e2e/start_stack.sh: Open
WebUI + the integration + the Gateway + the scripted model in fake_llm.py).
Waits are on DOM state, never on time. A PR that adds a user flow adds its
test here (AGENTS.md). Runs on GitHub runners only, in the E2E job.

The browser helpers are the screenshot capture's (scripts/screenshots/
capture.py), so the docs images and these tests drive Open WebUI one way.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

import pytest

# Before the browser imports: the unit job collects e2e/ without Playwright.
if os.environ.get("E2E_STACK") != "1":
    pytest.skip("end-to-end stack not running (set E2E_STACK=1; see e2e/start_stack.sh)",
                allow_module_level=True)

from playwright.sync_api import sync_playwright  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "screenshots"))
import capture  # noqa: E402
from owui_version import has_fix_0_12_4  # noqa: E402

TURN_MS = 120_000


def _openclaw_version() -> tuple:
    """(year, month, patch) of the Gateway the stack installed, from `openclaw --version`."""
    out = subprocess.run(["openclaw", "--version"], capture_output=True, text=True, timeout=60)
    m = re.search(r"(\d{4})\.(\d{1,2})\.(\d+)", out.stdout)
    assert m, f"no version in {out.stdout!r} {out.stderr[-300:]!r}"
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _openclaw_release() -> tuple:
    """(year, month) of the Gateway the stack installed."""
    return _openclaw_version()[:2]


@pytest.fixture(scope="module")
def token():
    return capture.signin()


@pytest.fixture(scope="module")
def page(token):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        pg = capture.signed_in_page(browser, token, capture.DESKTOP)
        # Before any page script, on every load (a reload too): every text
        # each reply shows, from every DOM change, so a state the user saw
        # cannot fall between two polls.
        pg.context.add_init_script(RECORD_TEXTS)
        pg.reload()
        pg.wait_for_selector("#chat-input", timeout=60_000)
        yield pg
        browser.close()


def _last_assistant_js(body: str) -> str:
    """A predicate on the last assistant message's visible text `t`."""
    return (f"() => {{ const a = {capture.LAST_ASSISTANT}; if (!a) return false; "
            f"const t = a.innerText; return {body}; }}")


# Every distinct text each assistant message shows, by message id, in order;
# each message's tool cards ({rows, running, text}) the same way; and every
# text of the Status dialog and the sub-agent drawer.
RECORD_TEXTS = """(() => {
  window.__seenBy = {};
  window.__cardsBy = {};
  window.__dialog = [];
  window.__drawer = [];
  const add = (list, v) => { if (v && list[list.length - 1] !== v) list.push(v); };
  const push = () => {
    for (const a of document.querySelectorAll('.chat-assistant')) {
      const m = a.closest('[id^="message-"]');
      const t = a.innerText.trim();
      if (!m || !t) continue;
      add(window.__seenBy[m.id] = window.__seenBy[m.id] || [], t);
      const rows = [...a.querySelectorAll('[role=button]')].map(b => b.innerText.trim());
      if (rows.length) add(window.__cardsBy[m.id] = window.__cardsBy[m.id] || [], JSON.stringify(
        {rows, running: a.querySelectorAll('[role=button] .shimmer').length, text: t}));
    }
    const d = document.getElementById('openclaw-status-modal-root');
    add(window.__dialog, d && d.innerText.trim());
    const r = document.getElementById('openclaw-drawer-root');
    const chip = document.getElementById('openclaw-drawer-chip');
    const stream = document.getElementById('openclaw-drawer-stream');
    add(window.__drawer, r && JSON.stringify({chip: chip ? chip.textContent : null,
                                              stream: stream ? stream.innerText.trim() : ''}));
  };
  new MutationObserver(push).observe(document, {subtree: true, childList: true, characterData: true});
})();"""

# The assistant messages on screen, oldest first: {id, text, done}. A reply is
# done for the user when Open WebUI draws its Copy button.
REPLIES = """() => [...document.querySelectorAll('.chat-assistant')].map(a => {
  const m = a.closest('[id^="message-"]');
  return {id: m ? m.id : null, text: a.innerText.trim(),
          done: !!(m && m.querySelector('button[aria-label="Copy"]'))};
})"""
STOP_GONE = """() => !document.querySelector('button[aria-label="Stop"]')"""


def _seen(page, message_id: str | None = None) -> list:
    """The texts one reply showed (default: the last one on screen)."""
    return page.evaluate("""(id) => {
      const all = window.__seenBy || {};
      if (!id) { const a = [...document.querySelectorAll('.chat-assistant')].pop();
                 const m = a && a.closest('[id^="message-"]'); id = m && m.id; }
      return (id && all[id]) || [];
    }""", message_id)


def _replies(page) -> list:
    return page.evaluate(REPLIES)


def _reply_js(index: int, body: str) -> str:
    """A predicate on reply `index` (-1: the last): `r` is {id, text, done},
    `all` every reply."""
    return (f"() => {{ const all = ({REPLIES})(); const r = all.at({index}); "
            f"if (!r) return false; return {body}; }}")


def _tails(texts: list) -> list:
    """For a failure message: the end of each text."""
    return [t[-60:] for t in texts]


def test_send_streams_the_reply_and_keeps_it(page):
    """Send/stream: the reply shows word by word while the run goes, ends as
    the full reply with the composer ready again, and is still there after a
    reload. The scripted model sends one word a second."""
    words = " ".join(f"slow-{i}" for i in range(1, 11))
    full = f"BROWSER_STREAM {words}"
    capture.new_chat(page)
    chat_id = capture.send(page, "E2E_SLOW BROWSER_STREAM")

    try:
        capture.wait_js(page, _last_assistant_js(f"t.includes({json.dumps(full)})"),
                        "the full reply", TURN_MS)
        # The turn is over for the user: Open WebUI shows Stop while the last
        # message is not done (MessageInput.svelte `isActive`), and hides it after.
        capture.wait_js(page, STOP_GONE, "the Stop button gone (the reply is done)", TURN_MS)
    except TimeoutError as ex:
        raise AssertionError(f"{ex}; texts the reply showed: {_tails(_seen(page))}") from None
    seen = _seen(page)
    partial = [t for t in seen if "BROWSER_STREAM" in t and "slow-2" in t and "slow-10" not in t]
    assert partial, f"the reply never showed part way through; texts it showed: {_tails(seen)}"

    page.reload()
    page.wait_for_selector("#chat-input", timeout=60_000)
    assert chat_id in page.url
    capture.wait_js(page, _last_assistant_js(f"t.includes({json.dumps(full)})"),
                    "the reply after a reload", 60_000)


def _answer_question(page, marker: str, question: str, answer) -> str:
    """Ask the scripted question, wait for the card above the composer,
    answer it with `answer(page)`, and return the reply text the agent's
    answer run wrote."""
    capture.new_chat(page)
    capture.send(page, marker)
    capture.wait_js(page, f"() => document.body.innerText.includes({json.dumps(question)}) "
                          "&& !!document.querySelector('input[placeholder=\"Type your answer\"]')",
                    "the question card", TURN_MS)
    answer(page)
    capture.wait_js(page, "() => !document.querySelector('input[placeholder=\"Type your answer\"]')",
                    "the card gone after the answer", 30_000)
    capture.wait_js(page, _last_reply_done("E2E_ANSWER_END"), "the whole reply to the answer", TURN_MS)
    return page.evaluate(f"() => {capture.LAST_ASSISTANT}.innerText")


def test_a_single_select_question_is_answered_with_a_click(page):
    """The card shows the options as rows; a click answers at once and the
    agent gets the option."""
    def click_green(pg):
        pg.locator("section button", has=pg.locator("span", has_text="Green")).first.click()
    reply = _answer_question(page, "E2E_ASK_SINGLE", "Which color?", click_green)
    assert "E2E_ANSWERED" in reply and "Green" in reply.split("E2E_ANSWERED", 1)[1], reply[-200:]


def test_a_question_from_the_ask_user_tool_is_answered_in_the_card(page):
    """ELI-216: OpenClaw's ask_user tool asks through the Gateway, not the
    reply text. Its question shows as the card, and the clicked option is
    the tool's answer to the agent."""
    capture.new_chat(page)
    capture.send(page, "E2E_ASK_TOOL")
    capture.wait_js(page, "() => document.body.innerText.includes('Which database?') "
                          "&& !!document.querySelector('input[placeholder=\"Type your answer\"]')",
                    "the ask_user card", TURN_MS)
    page.locator("section button", has=page.locator("span", has_text="SQLite")).first.click()
    capture.wait_js(page, _last_reply_done("E2E_ASK_TOOL_RESULT answered Database: SQLite"),
                    "the reply with the tool's answer", TURN_MS)


def test_a_reload_while_a_question_waits_shows_one_card_and_the_answer_still_arrives(page):
    """Open WebUI 0.12.4 ("questions survive reconnects"): the page is
    reloaded while the ask_user question is open. The card comes back once
    (no duplicate), and the click still reaches the run."""
    capture.new_chat(page)
    chat_id = capture.send(page, "E2E_ASK_TOOL")
    card = "() => document.body.innerText.includes('Which database?') " \
           "&& !!document.querySelector('input[placeholder=\"Type your answer\"]')"
    capture.wait_js(page, card, "the ask_user card", TURN_MS)
    _reload(page)
    assert chat_id in page.url
    capture.wait_js(page, card, "the ask_user card after the reload", TURN_MS)
    page.wait_for_timeout(3_000)
    cards = page.evaluate("() => document.querySelectorAll('input[placeholder=\"Type your answer\"]').length")
    assert cards == 1, f"{cards} question cards after the reload"
    page.locator("section button", has=page.locator("span", has_text="SQLite")).first.click()
    capture.wait_js(page, _last_reply_done("E2E_ASK_TOOL_RESULT answered Database: SQLite"),
                    "the reply with the tool's answer", TURN_MS)


def _pending_ask_user(chat_id: str):
    """The chat's pending Gateway question, from `question.list`."""
    out = subprocess.run(["openclaw", "gateway", "call", "question.list", "--json", "--params", "{}"],
                         capture_output=True, text=True, timeout=90)
    assert out.returncode == 0, out.stderr[-1500:]
    listed = json.loads(out.stdout[out.stdout.find("{"):])
    return next((q for q in listed.get("questions") or []
                 if chat_id in (q.get("sessionKey") or "") and q.get("status") == "pending"), None)


def test_an_ask_user_question_asked_with_no_tab_open_shows_when_the_chat_is_opened(page):
    """ELI-216: the question comes after the tab is closed (or the phone
    sleeps). It stays pending, the card shows when the chat is opened again,
    and the answer reaches the agent."""
    capture.new_chat(page)
    chat_id = capture.send(page, "E2E_ASK_TOOL_LATER")
    capture.wait_js(page, "() => document.body.innerText.includes('E2E_ASK_SOON')",
                    "the reply before the question", TURN_MS)
    page.goto("about:blank")         # the tab is closed: no socket shows the chat
    deadline = time.time() + 60
    while not _pending_ask_user(chat_id):
        assert time.time() < deadline, \
            "the ask_user question was never pending with no tab open (ended at once, or never asked)"
        time.sleep(1)
    # Still pending a while later: nothing gave up on it with no tab open.
    page.wait_for_timeout(5_000)
    assert _pending_ask_user(chat_id), "the question was ended while no tab was open"
    page.goto(f"{capture.BASE}/c/{chat_id}")
    capture.wait_js(page, "() => document.body.innerText.includes('Which database?') "
                          "&& !!document.querySelector('input[placeholder=\"Type your answer\"]')",
                    "the ask_user card on the reopened chat", TURN_MS)
    page.locator("section button", has=page.locator("span", has_text="SQLite")).first.click()
    capture.wait_js(page, _last_reply_done("E2E_ASK_TOOL_RESULT answered Database: SQLite"),
                    "the reply with the tool's answer", TURN_MS)


def test_a_question_left_for_a_new_message_ends_its_reply(page):
    """ELI-200: with the question card open, a new message is sent instead
    of an answer. The card goes, the question stays in its reply as text,
    and that reply is finished: no "Thinking..." left on it, also after a
    reload; the new message gets its own answer. Skipped on the OpenClaw 2026.10 lane while
    Open WebUI lacks the ELI-205 fix (0.12.4): see below."""
    if _openclaw_release() >= (2026, 10) and not has_fix_0_12_4():
        pytest.skip("OpenClaw 2026.10 lane on Open WebUI before 0.12.4: this flow's Stop lands as the "
                    "reply ends, so Open WebUI's own end-of-reply save is cancelled mid-transaction and "
                    "keeps the SQLite lock, and every later test in the job fails (ELI-205, "
                    "https://linear.app/eliavb/issue/ELI-205).")
    capture.new_chat(page)
    capture.send(page, "E2E_ASK_SINGLE")
    capture.wait_js(page, "() => document.body.innerText.includes('Which color?') "
                          "&& !!document.querySelector('input[placeholder=\"Type your answer\"]')",
                    "the question card", TURN_MS)
    _type_and_send(page, "E2E_ECHO: BROWSER_INSTEAD_OF_ANSWER")
    _wait(page, _reply_js(-1, "all.length === 2 && all[0].done && r.done"
                              " && r.text.includes('BROWSER_INSTEAD_OF_ANSWER')"),
          "the new message's answer")
    for when in ("", " after a reload"):
        shown = page.evaluate(f"() => {_message_text_js(0)}")
        assert "Which color?" in shown, f"the question is gone from its reply{when}: {shown[-200:]!r}"
        assert "Thinking..." not in shown, f"the question's reply still says Thinking...{when}: {shown[-200:]!r}"
        if not when:
            _reload(page)
            _wait(page, _reply_js(-1, "all.length === 2 && r.done"), "both replies after a reload", 60_000)


def _secret_dialog_open(page) -> None:
    # Open WebUI masks the field with a `password` class (CSS text-security), not type=password.
    capture.wait_js(page, "() => document.body.innerText.includes('Paste the test API token') "
                          "&& !!document.querySelector('input.password')",
                    "the masked dialog", TURN_MS)


def test_cancel_on_the_secret_dialog_tells_the_agent_and_the_card_says_cancelled(page):
    """A question asking for a secret opens Open WebUI's masked dialog; its
    Cancel reaches the agent as a cancel, and the reply's Ask User card says
    cancelled (ELI-189: the agent got "no" and the card said a secret was
    entered)."""
    capture.new_chat(page)
    capture.send(page, "E2E_ASK_SECRET")
    _secret_dialog_open(page)
    page.get_by_role("button", name="Cancel", exact=True).click()
    capture.wait_js(page, "() => !document.querySelector('input.password')",
                    "the dialog gone after Cancel", 30_000)
    _wait(page, _last_reply_done("E2E_ANSWER_END"), "the reply to the cancel")
    reply = page.evaluate(f"() => {capture.LAST_ASSISTANT}.innerText")
    told = reply.split("E2E_ANSWERED", 1)[1].split("E2E_ANSWER_END", 1)[0].strip()
    assert "cancel" in told.lower() and told.lower() != "no", f"the agent was told {told!r}"
    card = _open_card(page, -1, "Ask User")
    assert "Cancelled" in card and "secret entered" not in card, card[-300:]


def test_status_opened_in_another_tab_leaves_the_secret_dialog_its_answer(page):
    """Open WebUI's input dialog and its `execute` event share one callback in
    the tab: an `execute` arriving while the dialog is open took the dialog's
    answer, so Confirm reached nobody (ELI-195). Status opened from a second
    tab of the chat sends one to every tab of the user; the tab with the
    dialog gets it once the dialog is answered."""
    capture.new_chat(page)
    capture.send(page, "E2E_ASK_SECRET")
    _secret_dialog_open(page)
    other = page.context.new_page()
    try:
        other.goto(page.url)
        other.wait_for_selector("#chat-input", timeout=60_000)
        _open_status(other)
        page.locator("input.password").fill("e2e-secret-value")
        page.get_by_role("button", name="Confirm", exact=True).click()
        # Done and Stop gone: Open WebUI's request is over. Leaving while it
        # still saves the reply can lock its database for good (ELI-195 CI).
        _wait(page, _last_reply_done("E2E_ANSWER_END"), "the reply to the secret")
        reply = page.evaluate(f"() => {capture.LAST_ASSISTANT}.innerText")
        # The scripted model echoes what reached it.
        assert "E2E_ANSWERED e2e-secret-value" in reply, reply[-200:]
        # The held one: Status opens in this tab too, after the answer.
        page.wait_for_selector("#openclaw-status-modal-root", timeout=30_000)
        page.keyboard.press("Escape")
    finally:
        other.close()
def test_a_choice_that_mentions_a_password_keeps_its_options(page):
    """A question with options that mentions a password is a choice, not a
    secret: the card with its options, never the masked dialog (ELI-196)."""
    def click_no(pg):
        assert not pg.query_selector("input.password"), "the masked dialog opened"
        pg.locator("section button", has=pg.locator("span", has_text="No")).first.click()
    reply = _answer_question(page, "E2E_ASK_MENTIONS_SECRET", "use the password?", click_no)
    # Yes / No options answer as "yes" / "no", as a yes/no question does.
    assert "E2E_ANSWERED no E2E_ANSWER_END" in reply, reply[-200:]


def test_a_secret_ask_with_numbered_steps_keeps_the_masked_dialog(page):
    """Asking for an API key with the steps as a numbered list is still a
    secret: the masked dialog, never a card whose Other field would put the
    key in the chat (ELI-196)."""
    capture.new_chat(page)
    capture.send(page, "E2E_ASK_SECRET_STEPS")
    _wait(page, "() => document.body.innerText.includes('Paste your API key') "
                "&& (!!document.querySelector('input.password') "
                "|| !!document.querySelector('input[placeholder=\"Type your answer\"]'))",
          "the question", TURN_MS)
    assert page.query_selector("input.password"), "a card instead of the masked dialog"
    page.get_by_role("button", name="Cancel", exact=True).click()
    _wait(page, _last_reply_done("E2E_ANSWER_END"), "the reply after the dialog")


def test_a_hebrew_yes_no_question_gets_yes_and_no(page):
    """A Hebrew "(כן/לא)" question is a yes/no question: the card offers Yes
    and No, with no typed answer (ELI-193: it got a typed answer only)."""
    capture.new_chat(page)
    capture.send(page, "E2E_ASK_YES_NO_HE")
    yes = page.locator("section button", has=page.locator("span", has_text="Yes")).first
    yes.wait_for(timeout=TURN_MS)
    assert page.evaluate("() => document.body.innerText.includes('למחוק את הקובץ')")
    yes.click()
    _wait(page, _last_assistant_js("t.includes('E2E_ANSWER_END')"), "the reply to the answer")
    reply = page.evaluate(f"() => {capture.LAST_ASSISTANT}.innerText")
    assert "E2E_ANSWERED yes E2E_ANSWER_END" in reply, reply[-200:]


def test_a_multi_select_question_takes_typed_numbers(page):
    """The card lists the options by number; "1, 3" typed and submitted
    reaches the agent as the two options."""
    seen = {}

    def type_numbers(pg):
        seen["card"] = pg.evaluate("() => document.body.innerText")
        box = pg.locator('input[placeholder="Type your answer"]')
        box.click()
        box.fill("1, 3")
        pg.get_by_role("button", name="Submit answers").click()
    reply = _answer_question(page, "E2E_ASK_MULTI", "Which toppings?", type_numbers)
    for line in ("1. Cheese", "2. Olives", "3. Mushrooms"):
        assert line in seen["card"], f"the card lists {line!r}"
    assert "multiselect" not in seen["card"].lower()
    got = reply.split("E2E_ANSWERED", 1)[1]
    assert "Cheese, Mushrooms" in got, reply[-200:]


def _slow(token: str, n: int) -> str:
    """What the scripted model replies to `E2E_SLOW:<n> <token>`, one word a second."""
    return token + " " + " ".join(f"slow-{i}" for i in range(1, n + 1))


def _type_and_send(page, text: str) -> None:
    """Type in the composer of the open chat and press its send button."""
    page.locator("#chat-input").click()
    page.keyboard.type(text, delay=5)
    page.locator("#send-message-button").click()


def _user_message_js(text: str) -> str:
    return (f"[...document.querySelectorAll('.user-message')]"
            f".some(m => m.innerText.includes({json.dumps(text)}))")


def _wait(page, js: str, what: str, timeout_ms: int = TURN_MS) -> None:
    """wait_js that says what the user saw when it times out."""
    try:
        capture.wait_js(page, js, what, timeout_ms)
    except TimeoutError as ex:
        shown = [(r["text"][-80:], r["done"]) for r in _replies(page)]
        raise AssertionError(f"{ex}; replies on screen: {shown}") from None


def _reload(page) -> None:
    page.reload()
    page.wait_for_selector("#chat-input", timeout=60_000)


def test_continue_on_a_reply_adds_nothing_and_says_why(page):
    """Continue Response (ELI-184): OpenClaw cannot continue a finished reply.
    The reply stays as it was, also after a reload: nothing is glued onto it,
    the agent is never sent its own reply as a message, and a toast says why."""
    full = _slow("BROWSER_CONTINUE", 3)
    capture.new_chat(page)
    capture.send(page, "E2E_SLOW:3 BROWSER_CONTINUE")
    _wait(page, _reply_js(-1, f"r.done && r.text.includes({json.dumps(full)})"), "the reply, done")
    before = _replies(page)[-1]["text"]
    asked = len(_fake_llm_requests())

    page.locator(".chat-assistant").last.hover()
    page.locator('button[aria-label="Continue Response"]').last.click()
    _wait(page, """() => [...document.querySelectorAll('[data-sonner-toast]')]
                  .some(t => t.innerText.includes("Continue isn't supported"))""",
          "a toast saying Continue isn't supported", 30_000)
    _wait(page, _reply_js(-1, "r.done"), "the reply done again after Continue")
    page.wait_for_timeout(3_000)
    assert _replies(page)[-1]["text"] == before, _tails(_seen(page))
    sent = [r for r in _fake_llm_requests()[asked:] if "BROWSER_CONTINUE slow-1" in json.dumps(r["request"])]
    assert sent == [], "the agent was sent its own reply"

    _reload(page)
    _wait(page, _reply_js(-1, "r.done"), "the reply after a reload", 60_000)
    assert _replies(page)[-1]["text"] == before


def test_stop_ends_the_reply_where_it_was(page):
    """Stop: the reply keeps the words shown so far and never grows after it
    (the run is stopped, its rest is not written into the message later), the
    composer works, the next message gets its own answer with nothing of the
    stopped run in it, and a reload shows the same."""
    full = _slow("BROWSER_STOP", 10)
    capture.new_chat(page)
    capture.send(page, "E2E_SLOW BROWSER_STOP")
    _wait(page, _reply_js(0, "r.text.includes('BROWSER_STOP slow-1 slow-2')"), "the reply part way through")
    page.locator('button[aria-label="Stop"]').click()
    _wait(page, f"() => ({STOP_GONE})() && ({_reply_js(0, 'r.done')})()", "the stopped reply, done")
    stopped = _replies(page)[0]
    shown_at_stop = len(_seen(page, stopped["id"]))
    assert "BROWSER_STOP slow-1" in stopped["text"] and full not in stopped["text"], stopped["text"]

    _type_and_send(page, "E2E_ECHO: BROWSER_AFTER_STOP")
    _wait(page, _reply_js(-1, "all.length === 2 && r.done && r.text.includes('BROWSER_AFTER_STOP')"),
          "the next message's answer")
    first, second = _replies(page)
    seen = _seen(page, stopped["id"])
    assert len(seen) == shown_at_stop and first["text"] == stopped["text"], (
        f"the stopped reply changed after Stop: {_tails(seen[shown_at_stop - 1:])}")
    assert "slow-" not in second["text"] and "BROWSER_STOP" not in second["text"], second["text"]

    _reload(page)
    _wait(page, _reply_js(-1, "all.length === 2 && r.done"), "both replies after a reload", 60_000)
    assert [r["text"] for r in _replies(page)] == [stopped["text"], second["text"]]


def _saved_replies(page, token: str) -> list:
    """The chat's assistant messages as saved, in no set order."""
    saved = capture.api("GET", f"/api/v1/chats/{_chat_id(page)}", token=token)["chat"]["history"]["messages"]
    return [m for m in saved.values() if m.get("role") == "assistant"]


def _saved_whole(page, token: str, run: str) -> None:
    """Wait for the running reply (holding `run`) to be saved whole and done,
    before a reload.

    The new message goes once the run is over at the Gateway, while the
    Pipe's writer still reads the run's transcript to save the running
    reply, so its answer can be done, on the page, before that save. Until
    then a load gets the saved reply (cut, as saved at the takeover) with the
    writer's mid-turn copy laid over it, but Open WebUI's chat GET reads the
    two one after the other: one that reads the reply before the save and
    the copies after it (the writer drops its copy once saved) gets it cut,
    and the page, with no task left on the chat, shows it cut and finished
    (ELI-274's window, for the reply's text). A mid-turn copy is never done."""
    deadline = time.time() + TURN_MS / 1000
    while True:
        replies = _saved_replies(page, token)
        if any(m.get("done") and run in (m.get("content") or "") for m in replies):
            return
        assert time.time() < deadline, (
            f"the running reply was never saved whole: {[((m.get('content') or '')[-80:], m.get('done')) for m in replies]}")
        time.sleep(0.5)


def _both_answered(page, token: str, run: str, answer: str) -> None:
    """The running reply went on to its end in its own message, nothing cut,
    and the new message got its own answer, with none of the run in it; the
    same after a reload. Open WebUI draws the running reply finished as soon
    as the new message goes in, while it is still being written, so it is
    judged once the new message's answer, which waits for the run, is done.
    The reload waits for the reply's save (`_saved_whole`)."""
    _wait(page, _reply_js(-1, "all.length === 2 && all[0].done && r.done"), "both replies done")
    first, second = _replies(page)
    assert first["text"].count(run) == 1, f"the running reply was cut or doubled: {first['text'][-120:]!r}"
    assert answer in second["text"] and "slow-" not in second["text"], second["text"]
    _saved_whole(page, token, run)
    _reload(page)
    _wait(page, _reply_js(-1, "all.length === 2 && all[0].done && r.done"), "both replies after a reload", 60_000)
    again = _replies(page)
    assert run in again[0]["text"] and answer in again[1]["text"], [r["text"][-80:] for r in again]


def _ends_like_the_answer(page, token: str, run: str) -> None:
    """The running reply (holding `run`), finished by the Pipe's writer after
    a takeover, ends as the answer to the new message (an inline reply) does
    (ELI-210): with no "Stopped" from the cancel, and with the usage line
    whenever the answer has it. On 2026.10 the running reply is its session's
    first run, whose row has no totals yet: the line comes from the run's own
    usage (ELI-177). In the saved status lines, and in the status line shown
    on the reply by a page loaded after that save.

    Open WebUI saves the running reply done as soon as the new message goes
    in, so "both replies done" can hold before the writer's last write (the
    status lines). A page loaded in between never shows them: their event
    lands while loadChat still assigns what it fetched, ELI-230's window
    (ELI-274). So the saved lines are waited for, then the page reloaded."""
    usage = lambda ls: any(str(line).startswith("\U0001f9e0") for line in ls)  # noqa: E731
    deadline = time.time() + TURN_MS / 1000
    while True:
        replies = _saved_replies(page, token)
        held = [m for m in replies if run in (m.get("content") or "")]
        assert len(held) == 1 and len(replies) == 2, [m.get("content", "")[-80:] for m in replies]
        answer = next(m for m in replies if m is not held[0])
        lines, ref = ([h.get("description") for h in m.get("statusHistory") or [] if isinstance(h, dict)]
                      for m in (held[0], answer))
        if ("Stopped" not in lines and usage(lines) == usage(ref)) or time.time() > deadline:
            break
        time.sleep(1)
    assert "Stopped" not in lines, f"a Stopped line: {lines}"
    assert usage(lines) == usage(ref), f"the running reply: {lines}; the answer: {ref}"
    _reload(page)
    _wait(page, _reply_js(-1, "all.length === 2 && all[0].done && r.done"), "both replies after a reload", 60_000)
    shown = page.evaluate(f"() => {_message_text_js(0)}")
    assert "Stopped" not in shown, shown[-200:]
    assert ("\U0001f9e0" in shown) == usage(ref), shown[-200:]


def test_a_message_sent_while_a_reply_runs_is_in_the_chat_at_once(page, token):
    """Sent while busy (AUTO_SEND_WHILE_RUNNING, on by default): the message
    shows in the chat right away, not in Open WebUI's queue above the
    composer, while the reply is still being written; that reply is not
    stopped and finishes, then the message gets its own answer."""
    run = _slow("BROWSER_BUSY", 20)
    capture.new_chat(page)
    capture.send(page, "E2E_SLOW:20 BROWSER_BUSY")
    _wait(page, _reply_js(0, "r.text.includes('BROWSER_BUSY slow-1 slow-2')"), "the reply part way through")
    _type_and_send(page, "E2E_ECHO: BROWSER_BUSY_ANSWER")
    _wait(page, f"() => {_user_message_js('BROWSER_BUSY_ANSWER')}"
                f" && !document.querySelector('button path[d=\"{capture.SEND_NOW_ICON}\"]')"
                f" && ({_reply_js(0, '!r.text.includes(' + json.dumps(run) + ')')})()",
          "the new message in the chat while the first reply is still being written")
    _both_answered(page, token, run, "BROWSER_BUSY_ANSWER")


def test_a_message_queued_behind_a_run_waits_out_a_socket_drop(page, token):
    """ELI-199: a message waits behind a run the Pipe keeps going in its own
    reply, and the Pipe's Gateway socket drops (1006, the Gateway itself
    stays up). The message must wait for the reconnect and then for the run,
    and get its own answer: not be sent into the dropped socket and lost
    ("Error sending message: gateway connection not ready")."""
    import socket_drop
    run = _slow("BROWSER_DROP", 20)
    capture.new_chat(page)
    capture.send(page, "E2E_SLOW:20 BROWSER_DROP")
    _wait(page, _reply_js(0, "r.text.includes('BROWSER_DROP slow-1 slow-2')"), "the reply part way through")
    _type_and_send(page, "E2E_ECHO: BROWSER_DROP_ANSWER")
    _wait(page, "() => document.body.innerText.includes('Queued: waiting')", "the new message queued")
    dropped = [d for d in socket_drop.drop() if "fd" in d]
    assert dropped, "no Gateway connection of Open WebUI was dropped"
    _wait(page, _reply_js(-1, "all.length === 2 && r.done"), "the queued message's reply done")
    assert "Error sending message" not in _replies(page)[1]["text"], _replies(page)[1]["text"]
    # A word in flight during the drop can be missing from the running reply
    # until its end, which the Pipe writes from the run's transcript.
    _wait(page, _reply_js(0, f"r.text.includes({json.dumps(run)})"), "the running reply whole", 30_000)
    _both_answered(page, token, run, "BROWSER_DROP_ANSWER")


def test_send_now_on_a_queued_message_keeps_the_running_reply(page, token):
    """Send now (AUTO_SEND_WHILE_RUNNING off): Open WebUI holds the message
    in its queue above the composer; Send now puts it in the chat without
    stopping the running reply, which finishes, then the message is answered."""
    run = _slow("BROWSER_SENDNOW", 20)
    old = capture.pipe_valves(token, AUTO_SEND_WHILE_RUNNING=False)
    try:
        capture.new_chat(page)
        capture.send(page, "E2E_SLOW:20 BROWSER_SENDNOW")
        _wait(page, _reply_js(0, "r.text.includes('BROWSER_SENDNOW slow-1 slow-2')"), "the reply part way through")
        _type_and_send(page, "E2E_ECHO: BROWSER_SENDNOW_ANSWER")
        send_now = page.locator('button[aria-label="Send now"]').first
        send_now.wait_for(timeout=30_000)
        assert not page.evaluate(f"() => {_user_message_js('BROWSER_SENDNOW_ANSWER')}"), \
            "the message went into the chat with auto-send off"
        send_now.click()
        _wait(page, f"() => {_user_message_js('BROWSER_SENDNOW_ANSWER')}", "the message in the chat after Send now")
        _both_answered(page, token, run, "BROWSER_SENDNOW_ANSWER")
        _ends_like_the_answer(page, token, run)
    finally:
        capture.pipe_valves(token, **old)


def test_a_message_sent_from_a_second_tab_keeps_the_running_reply(page, token):
    """A plain send from another tab (or the phone) on the same chat while
    tab A streams (ELI-210): the running reply goes on to its end, ends with
    the usage line like any reply, and the message gets its own answer."""
    run = _slow("BROWSER_TAB2", 20)
    capture.new_chat(page)
    capture.send(page, "E2E_SLOW:20 BROWSER_TAB2")
    _wait(page, _reply_js(0, "r.text.includes('BROWSER_TAB2 slow-1 slow-2')"), "the reply part way through")
    other = page.context.new_page()
    try:
        other.goto(page.url)
        other.wait_for_selector("#chat-input", timeout=60_000)
        _type_and_send(other, "E2E_ECHO: BROWSER_TAB2_ANSWER")
        _wait(other, _reply_js(-1, "all.length === 2 && all[0].done && r.done"
                                   " && r.text.includes('BROWSER_TAB2_ANSWER')"), "the answer in the second tab")
    finally:
        other.close()
    _saved_whole(page, token, run)
    _reload(page)
    _both_answered(page, token, run, "BROWSER_TAB2_ANSWER")
    _ends_like_the_answer(page, token, run)


def _taken_over(page, token: str) -> None:
    """Wait for the Pipe's writer to hold the running reply: its handover
    write, the reply saved as it stood then and not done."""
    deadline = time.time() + TURN_MS / 1000
    while not any((m.get("meta") or {}).get("openclaw_writer") and m.get("done") is False
                  for m in _saved_replies(page, token)):
        assert time.time() < deadline, "the Pipe's writer never took the running reply over"
        time.sleep(0.2)


def test_a_tab_that_loads_the_chat_across_the_running_replys_save_shows_it_whole(page, token):
    """A tab that loads the chat while the Pipe's writer saves the running
    reply (ELI-340): its chat GET read the reply before the save and found no
    mid-turn copy after it. Here every load of the reloaded tab is answered
    with the chat as it was before the save, and only once the reply is saved
    whole. The tab shows the whole reply, done, without another reload."""
    run = _slow("BROWSER_ACROSS", 12)
    capture.new_chat(page)
    capture.send(page, "E2E_SLOW:12 BROWSER_ACROSS")
    _wait(page, _reply_js(0, "r.text.includes('BROWSER_ACROSS slow-1 slow-2')"), "the reply part way through")
    other = page.context.new_page()
    try:
        other.goto(page.url)
        other.wait_for_selector("#chat-input", timeout=60_000)
        _type_and_send(other, "E2E_ECHO: BROWSER_ACROSS_ANSWER")
        _taken_over(page, token)
        held, holding = [], [True]
        chat_get = f"**/api/v1/chats/{_chat_id(page)}"

        def hold(route):
            if holding[0]:
                held.append((route, route.fetch()))
            else:
                route.continue_()
        page.route(chat_get, hold)
        page.reload()
        deadline = time.time() + 30
        while not held:
            assert time.time() < deadline, "the reloaded tab never asked for the chat"
            page.wait_for_timeout(50)
        _saved_whole(page, token, run)
        holding[0] = False
        for route, response in held:
            route.fulfill(response=response)
        page.wait_for_selector("#chat-input", timeout=60_000)
        _wait(page, _reply_js(0, f"r.done && r.text.includes({json.dumps(run)})"),
              "the running reply whole and done in the tab that loaded across its save", 30_000)
    finally:
        other.close()


def _message_text_js(index: int) -> str:
    """Reply `index`'s whole message as shown: its status line too."""
    return (f"(() => {{ const a = [...document.querySelectorAll('.chat-assistant')].at({index}); "
            f"const m = a && a.closest('[id^=\"message-\"]'); return m ? m.innerText : ''; }})()")


def _says_where_its_message_went(page, index: int, note: str) -> None:
    """Reply `index`, cancelled before it sent its message, is done and
    says `note`; no "Queued" is left on it, also after a reload."""
    queued = "Queued: waiting"
    for when in ("", " after a reload"):
        _wait(page, f"() => {_message_text_js(index)}.includes({json.dumps(note)})",
              f"the interrupted reply saying {note!r}{when}", 60_000)
        shown = page.evaluate(f"() => {_message_text_js(index)}")
        assert queued not in shown, f"the interrupted reply still says Queued{when}: {shown[-200:]!r}"
        assert _replies(page)[index]["done"], f"the interrupted reply is not done{when}"
        if not when:
            _reload(page)


def test_a_reply_interrupted_while_queued_says_its_message_went_with_the_next(page):
    """Send twice while a reply runs (ELI-188): the 2nd reply, waiting
    behind the run, is interrupted by the 3rd message, which takes its
    message along. The 2nd reply must not say "Queued" forever with no text:
    it says its message went with the next one, which answers it."""
    run = _slow("BROWSER_TWICE", 15)
    capture.new_chat(page)
    capture.send(page, "E2E_SLOW:15 BROWSER_TWICE")
    _wait(page, _reply_js(0, "r.text.includes('BROWSER_TWICE slow-1 slow-2')"), "the reply part way through")
    _type_and_send(page, "E2E_ECHO: BROWSER_TWICE_A")
    _wait(page, "() => document.body.innerText.includes('Queued: waiting')", "the 2nd reply queued")
    _type_and_send(page, "E2E_ECHO: BROWSER_TWICE_B")
    _wait(page, _reply_js(-1, "all.length === 3 && all[0].done && r.done"
                              " && r.text.includes('BROWSER_TWICE_A')"),
          "the 3rd reply answering the 2nd message")
    assert _replies(page)[0]["text"].count(run) == 1
    _says_where_its_message_went(page, 1, "sent together with your next one")


def test_a_queued_reply_stopped_says_its_message_was_not_sent(page):
    """Send now, then Stop while the new message waits (ELI-188 variant):
    the Pipe drops the message, as the user stopped it, and its reply says
    so instead of "Queued" forever."""
    capture.new_chat(page)
    capture.send(page, "E2E_SLOW:15 BROWSER_SENDSTOP")
    _wait(page, _reply_js(0, "r.text.includes('BROWSER_SENDSTOP slow-1 slow-2')"), "the reply part way through")
    _type_and_send(page, "E2E_ECHO: BROWSER_SENDSTOP_ANSWER")
    _wait(page, "() => document.body.innerText.includes('Queued: waiting')", "the 2nd reply queued")
    page.locator('button[aria-label="Stop"]').click()
    _wait(page, f"() => ({STOP_GONE})() && ({_reply_js(-1, 'all.length === 2 && r.done')})()",
          "the stopped replies, done")
    _says_where_its_message_went(page, 1, "this message was not sent")
    assert "BROWSER_SENDSTOP_ANSWER" not in _replies(page)[1]["text"]


def test_stop_after_send_now_stops_the_run(page, token):
    """Stop after Send now (ELI-198): the running reply's run goes on in its
    message (the turn writer holds it) and the new message waits behind it.
    Stop cancels both, and both ask the page which button it was; the first
    to ask took the answer, so the run went on and filled the stopped reply
    later. The scripted run waits for a file this test writes only after
    checking that the run is over."""
    hold_token = f"sn{os.getpid()}_{len(_fake_llm_requests())}"
    # e2e/fake_llm.py hold_file
    hold = os.path.join(os.path.dirname(os.environ["E2E_FAKE_LLM_LOG"]), f"hold-{hold_token}")
    capture.new_chat(page)
    try:
        capture.send(page, f"E2E_HOLD {hold_token}")
        _wait(page, _reply_js(0, "r.text.includes('exec') && !r.done"), "the reply running its command")
        _type_and_send(page, "E2E_ECHO: BROWSER_STOP_AFTER_SEND_NOW")
        # The new message's reply waits behind the run, at the Pipe.
        _wait(page, _reply_js(1, "r.text.includes('Queued') && !r.done"), "the new message's reply queued behind the run")
        stop = page.locator('button[aria-label="Stop"]')
        stop.wait_for(timeout=30_000)
        stop.click()
        me = capture.api("GET", "/api/v1/auths/", token=token)
        key = f"agent:main:openwebui-{me['id']}-{_chat_id(page)}"
        deadline, active = time.monotonic() + 60, True
        while active and time.monotonic() < deadline:
            rows = capture.gateway_call("sessions.list", {"search": _chat_id(page), "limit": 5}).get("sessions") or []
            active = any(r.get("key") == key and r.get("hasActiveRun") for r in rows)
    finally:
        open(hold, "w").close()
    assert not active, "the run went on after Stop: the session still had a running run"
    history = capture.gateway_call("chat.history", {"sessionKey": key, "limit": 50})
    assert "E2E_HOLD_END" not in json.dumps(history.get("messages")), "the stopped run still answered"
    os.remove(hold)


def _from(texts: list, word: str) -> list:
    """The reply's words in each text, from `word` on (Open WebUI's status
    line, "Thinking...", can sit above them); texts with none are left out."""
    return [t[t.index(word):] for t in texts if word in t]


def _reload_mid(page, reply: int, word: str, full: str) -> tuple:
    """Reload while reply `reply` is being written: (its words just before the
    reload, every text of its words after it, up to its end)."""
    rid = _replies(page)[reply]["id"]
    before = _from(_seen(page, rid), word)[-1]
    _reload(page)
    _wait(page, _reply_js(reply, f"r.text.includes({json.dumps(full)})"), "the reply's end after the reload")
    after = _from(_seen(page, rid), word)
    assert after and all(full.startswith(t) for t in after), (
        f"after the reload the reply showed words out of order or with a gap: {_tails(after)}")
    assert any(t != full for t in after), f"the reply did not show part way through after the reload: {_tails(after)}"
    return before, after


def test_a_reload_mid_reply_shows_the_answer_so_far_and_keeps_going(page, token):
    """Reload mid-reply: after the reload the reply shows the answer so far,
    from its first word, and keeps growing to its end, in one message. Twice:
    a plain reply (Open WebUI keeps its mid-turn copy itself), then a reply the
    Pipe writes itself, a run kept going by a message sent while it ran: there
    nothing the reply already showed is gone after the reload."""
    full = _slow("BROWSER_RELOAD", 30)
    capture.new_chat(page)
    chat_id = capture.send(page, "E2E_SLOW:30 BROWSER_RELOAD")
    _wait(page, _reply_js(0, "r.text.includes('BROWSER_RELOAD slow-1 slow-2 slow-3')"), "the reply part way through")
    rid = _replies(page)[0]["id"]
    _reload(page)
    assert chat_id in page.url
    _wait(page, _reply_js(0, "r.text.includes('slow-6')"), "the reply growing after the reload")
    after = _from(_seen(page, rid), "BROWSER_RELOAD")
    assert all(full.startswith(t) for t in after), (
        f"after the reload the reply showed words out of order or with a gap: {_tails(after)}")

    # A message sent now keeps the run going in its own reply, which the
    # Pipe then writes itself (the run is handed over to it).
    _type_and_send(page, "E2E_ECHO: BROWSER_RELOAD_ANSWER")
    _wait(page, f"() => {_user_message_js('BROWSER_RELOAD_ANSWER')}", "the new message in the chat")
    shown = max(int(n) for n in re.findall(r"slow-(\d+)", _from(_seen(page, rid), "BROWSER_RELOAD")[-1]))
    _wait(page, _reply_js(0, f"r.text.includes('slow-{shown + 2} ')"), "the kept reply two words further")
    before, after = _reload_mid(page, 0, "BROWSER_RELOAD", full)
    assert len(after[0]) >= len(before), (
        f"the reload lost words the reply had shown: before {before[-60:]!r}, after {after[0][-60:]!r}")
    _both_answered(page, token, full, "BROWSER_RELOAD_ANSWER")


def _turn_over(needle: str) -> str:
    """The chat's only reply is done, shows `needle`, and Stop is gone."""
    done = _reply_js(0, f"r.done && r.text.includes({json.dumps(needle)})")
    return f"() => ({STOP_GONE})() && ({done})()"


def _cards(page, message_id: str) -> list:
    """Every state of one reply's tool cards: {rows, running, text}."""
    return [json.loads(c) for c in page.evaluate("(id) => (window.__cardsBy || {})[id] || []", message_id)]


def _open_card(page, reply: int, row: str) -> str:
    """Open the reply's card whose row reads `row`; its text once open."""
    card = page.locator(".chat-assistant").nth(reply).locator("[role=button]", has_text=row).first
    card.click()
    _wait(page, f"""() => [...document.querySelectorAll('.chat-assistant')].at({reply})
                     .innerText.toLowerCase().includes('output')""", f"the {row!r} card open", 10_000)
    return page.evaluate(f"() => [...document.querySelectorAll('.chat-assistant')].at({reply}).innerText")


_CARD_ICONS_JS = """() => [...[...document.querySelectorAll('.chat-assistant')][0]
    .querySelectorAll('[role=button]')].map(b => {
        const icon = b.querySelector('.text-red-500, .text-emerald-500');
        return icon ? (icon.matches('.text-red-500') ? 'red' : 'green') : null;
    })"""


def test_tool_cards_show_running_failed_and_done(page):
    """Tool cards: a failed call shows ❌ as soon as it fails; a long call
    shows running, below the text the reply wrote before it, then done; each
    opens to its input and output; the same after a reload. The scripted
    model reads a file that is not there, then runs `sleep 6`."""
    capture.new_chat(page)
    capture.send(page, "E2E_CARDS")
    _wait(page, _turn_over("E2E_CARDS_END"), "the reply's end")
    reply = _replies(page)[0]
    states = _cards(page, reply["id"])
    shown = [(s["rows"], s["running"]) for s in states]
    running = [s for s in states if s["running"] and any(r.startswith("Preparing exec") for r in s["rows"])]
    assert running, f"the exec card never showed running: {shown}"
    for s in states:
        t = s["text"]
        if "E2E_CARDS_MID" in t and "exec" in t:
            assert t.index("E2E_CARDS_MID") < t.index("exec"), (
                f"a card showed above the text written before it: {t[-160:]!r}")
    failed_live = [s for s in states if "E2E_CARDS_END" not in s["text"] and any("read ❌" in r for r in s["rows"])]
    assert failed_live, f"the failed read never showed ❌ while the reply ran: {shown}"
    final = states[-1]
    assert final["rows"] == ["View Result from read ❌", "View Result from exec"] and not final["running"], shown[-1]
    # The failed card has the red X, the other the green check (ELI-278).
    assert page.evaluate(_CARD_ICONS_JS) == ["red", "green"], page.evaluate(_CARD_ICONS_JS)

    assert "e2e-cards-slept" in _open_card(page, 0, "View Result from exec")
    assert "tool call failed" in _open_card(page, 0, "View Result from read")

    _reload(page)
    _wait(page, _reply_js(0, "r.done && r.text.includes('E2E_CARDS_END')"), "the reply after a reload", 60_000)
    rows = page.evaluate("() => [...document.querySelectorAll('.chat-assistant')][0]"
                         ".querySelectorAll('[role=button]').length")
    text = _replies(page)[0]["text"]
    assert rows == 2 and "read ❌" in text and "View Result from exec" in text, text[-200:]
    assert page.evaluate(_CARD_ICONS_JS) == ["red", "green"], page.evaluate(_CARD_ICONS_JS)


def _open_status(page) -> None:
    page.locator('button[aria-label="OpenClaw status"]').click()
    page.wait_for_selector("#openclaw-status-modal-root", timeout=30_000)


def _fake_llm_requests() -> list:
    """The scripted model's request log (the stack's E2E_FAKE_LLM_LOG)."""
    with open(os.environ["E2E_FAKE_LLM_LOG"], encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_the_status_dialog_compacts_the_chat(page):
    """Status dialog + Compact: the composer's Status button opens the dialog
    with this chat's context numbers; Compact shows "Compacting…", the
    session is compacted (its summary request reaches the model), and the
    dialog comes back with the numbers. On OpenClaw 2026.10.10 a new
    session's row has no totals after its first run: the numbers come from
    the run's own usage (ELI-223)."""
    nonce = f"BROWSER_COMPACT_{os.getpid()}_{len(_fake_llm_requests())}"
    capture.new_chat(page)
    capture.send(page, f"E2E_ECHO: {nonce}")
    _wait(page, _turn_over(nonce), "the reply")
    _open_status(page)
    context = "#openclaw-status-section-context"
    numbers = f"""() => {{ const s = document.querySelector('{context}');
      return !!s && /\\d+ \\/ 200k tokens/.test(s.innerText) && [...s.querySelectorAll('button')]
        .some(b => b.textContent === 'Compact'); }}"""
    _wait(page, numbers, "the context numbers and the Compact button", 60_000)
    page.locator(f"{context} button", has_text="Compact").click()
    _wait(page, f"""() => (window.__dialog || []).some(t => t.includes('Compacting'))
                     && ({numbers})()""", "Compacting…, then the numbers again")
    summaries = [r for r in _fake_llm_requests()
                 if any("summarization" in json.dumps(m.get("content")) for m in r["request"].get("messages") or [])
                 and nonce in json.dumps(r["request"])]
    assert summaries, "no summary request for this chat reached the model: the session was not compacted"
    page.keyboard.press("Escape")


def test_a_compaction_the_gateway_refuses_shows_its_error(page):
    """Status dialog + Compact, refused (ELI-209): the model refuses the
    summary, so the Gateway's compaction fails and its reply says so. The
    dialog showed "Compacting…" and then the same numbers, as if it had
    worked. It says the compaction failed, with the Gateway's reason, and
    keeps the numbers."""
    nonce = f"BROWSER_COMPACT_FAIL_{os.getpid()}_{len(_fake_llm_requests())}"
    capture.new_chat(page)
    capture.send(page, f"E2E_ECHO: E2E_COMPACT_FAIL {nonce}")
    _wait(page, _turn_over(nonce), "the reply")
    _open_status(page)
    context = "#openclaw-status-section-context"
    numbers = f"""() => {{ const s = document.querySelector('{context}');
      return !!s && /\\d+ \\/ 200k tokens/.test(s.innerText); }}"""
    _wait(page, f"() => ({numbers})() && !!document.querySelector('{context} button')",
          "the context numbers and the Compact button", 60_000)
    page.locator(f"{context} button", has_text="Compact").click()
    _wait(page, f"""() => (window.__dialog || []).some(t => t.includes('Compacting'))
                     && !document.body.innerText.includes('Compacting…') && ({numbers})()""",
          "Compacting…, then the dialog back")
    refused = [r for r in _fake_llm_requests()
               if r["reply"].get("http_error") and nonce in json.dumps(r["request"])]
    assert refused, "precondition: the model never refused this chat's summary request"
    shown = page.locator(context).inner_text()
    assert "Compaction failed" in shown, f"the dialog shows no failure after a refused compaction: {shown!r}"
    page.keyboard.press("Escape")


def test_a_compaction_mid_reply_shows_in_the_reply(page):
    """The run reports a context compaction in the middle of the reply
    (ELI-283; the stack's e2e_compact tool makes it, as Claude Code's
    compaction does on claude-cli): the reply shows a collapsed "Context
    compacted" block where it happened, between the text before and after
    it, and keeps it after a reload."""
    nonce = f"BROWSER_COMPACT_{os.getpid()}_{len(_fake_llm_requests())}"
    capture.new_chat(page)
    capture.send(page, f"E2E_COMPACT_MARK {nonce}")
    _wait(page, _turn_over(f"E2E_COMPACT_AFTER {nonce}"), "the reply")
    block = f"""() => {{
      const replies = document.querySelectorAll('.chat-assistant');
      const last = replies[replies.length - 1];
      if (!last) return false;
      // Open WebUI draws <details> as its own collapsible, not a native
      // <summary>: closed, it shows the title and hides the body.
      const t = last.innerText, at = t.indexOf('Context compacted');
      return at >= 0 && !t.includes('Older messages in this conversation were summarized')
        && t.indexOf('E2E_COMPACT_BEFORE {nonce}') >= 0
        && t.indexOf('E2E_COMPACT_BEFORE {nonce}') < at && at < t.indexOf('E2E_COMPACT_AFTER {nonce}');
    }}"""
    _wait(page, block, "a collapsed 'Context compacted' block between the reply's text")
    page.reload()
    _wait(page, block, "the block again after a reload")


TAKEOVER_LINK = "https://takeover.e2e.invalid/control/E2eTakeoverSession0001"   # e2e/fake_llm.py


@pytest.fixture
def control_link_pattern(token):
    """CONTROL_LINK_PATTERN set to match e2e/fake_llm.py's takeover link, then put back."""
    fid = capture.MODEL.split(".", 1)[0]
    before = capture.api("GET", f"/api/v1/functions/id/{fid}/valves", token=token) or {}
    capture.api("POST", f"/api/v1/functions/id/{fid}/valves/update",
                {**before, "CONTROL_LINK_PATTERN": r"https://takeover\.e2e\.invalid/control/\w+"}, token)
    yield
    capture.api("POST", f"/api/v1/functions/id/{fid}/valves/update", before, token)


def test_a_takeover_link_shows_in_the_reply_while_its_run_waits(page, control_link_pattern):
    """ELI-318: the agent hands its browser to the user with a link printed
    by a command and waits. The link shows in the reply as "Take control" while
    the run waits, in the page itself (not in a sandboxed embed, where the
    takeover page and an expired sign-in's login page refuse to load), opens
    in a new tab, and stays after the run ends and a reload."""
    token = f"t{os.getpid()}_{len(_fake_llm_requests())}"
    hold = os.path.join(os.path.dirname(os.environ["E2E_FAKE_LLM_LOG"]), f"hold-{token}")   # fake_llm hold_file
    link = f"a[href={json.dumps(TAKEOVER_LINK)}]"
    capture.new_chat(page)
    try:
        capture.send(page, f"E2E_TAKEOVER {token}")
        _wait(page, f"() => !!document.querySelector('{link}')", "the Take control link while the run waits")
        assert not _replies(page)[0]["done"], "the reply ended before the link showed"
        anchor = page.locator(link)
        assert anchor.count() == 1, anchor.count()
        assert "Take control" in anchor.inner_text(), anchor.inner_text()
        assert anchor.get_attribute("target") == "_blank", anchor.evaluate("a => a.outerHTML")
    finally:
        open(hold, "w").close()
    _wait(page, _turn_over("E2E_TAKEOVER_END"), "the reply's end")
    _reload(page)
    _wait(page, f"() => document.querySelectorAll('{link}').length === 1", "the link after a reload")
    os.remove(hold)


def test_the_subagent_drawer_follows_a_running_child_to_its_end(page):
    """Sub-agent drawer: the Status dialog lists the running sub-agent;
    its row opens the drawer, which shows it running and then, with nothing
    clicked, done with the child's whole reply."""
    child = "E2E_CHILDTASK " + " ".join(f"slow-{i}" for i in range(1, 21))
    capture.new_chat(page)
    capture.send(page, "E2E_SPAWN one helper")
    _wait(page, _turn_over("E2E_SPAWNED"), "the parent's reply")
    _open_status(page)
    section = "#openclaw-status-section-subagents"
    _wait(page, f"""() => {{ const s = document.querySelector('{section}');
                     return !!s && s.innerText.includes('E2E_CHILDTASK') && s.innerText.includes('Running'); }}""",
          "the running sub-agent in the Status dialog", 60_000)
    page.locator(f"{section} button", has_text="E2E_CHILDTASK").first.click()
    page.wait_for_selector("#openclaw-drawer-root", timeout=30_000)
    _wait(page, f"""() => {{ const c = document.getElementById('openclaw-drawer-chip');
                     const s = document.getElementById('openclaw-drawer-stream');
                     return !!c && c.textContent === 'Done' && s.innerText.includes({json.dumps(child)}); }}""",
          "the drawer showing the child done, with its whole reply")
    drawer = [json.loads(d) for d in page.evaluate("() => window.__drawer || []")]
    assert drawer[0]["chip"] == "Running", f"the drawer did not open on the running child: {drawer[:3]}"
    assert "E2E_CHILDTASK" in page.locator("#openclaw-drawer-root").inner_text()


def test_subagents_whose_sessions_were_deleted_stay_listed_with_their_errors(page, token):
    """ELI-276, ELI-277: sub-agents spawned with cleanup "delete" lose their
    sessions seconds after they end, and the Status dialog listed none of
    them. A failed one's error read the Gateway's canned copy for its class
    ("needs a short break" for a 402 billing error). Both stay listed after
    the delete, the failed one with what its run reported, and its drawer
    opens and says why its run is not there."""
    capture.new_chat(page)
    capture.send(page, "E2E_SPAWN_CLEANUP")
    _wait(page, _turn_over("E2E_CLEANUP_SPAWNED"), "the parent's reply")
    me = capture.api("GET", "/api/v1/auths/", token=token)
    key = f"agent:main:openwebui-{me['id']}-{_chat_id(page)}"

    def ran(task):
        return any(task in json.dumps(r["request"]) and "[Subagent Task]" in json.dumps(r["request"])
                   for r in _fake_llm_requests())
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not (ran("E2E_CLEAN_OK") and ran("E2E_CLEAN_FAIL") and not (
            capture.gateway_call("sessions.list", {"spawnedBy": key, "limit": 10}).get("sessions"))):
        time.sleep(1)
    assert ran("E2E_CLEAN_OK") and ran("E2E_CLEAN_FAIL"), "precondition: a child never reached the model"
    assert not capture.gateway_call("sessions.list", {"spawnedBy": key, "limit": 10}).get("sessions"), \
        "precondition: the Gateway kept a child's session"

    _open_status(page)
    # Nothing runs, so the section opens folded, its count in its title.
    _wait(page, f"""() => {{ const f = document.querySelector('{capture.STATUS_ROOT} [data-openclaw-fold=subagents]');
                     return !!f && /subagents \\(2\\)/i.test(f.innerText); }}""",
          "both deleted sub-agents counted in the Status dialog", 60_000)
    capture.open_fold(page, "subagents")
    section = "#openclaw-status-section-subagents"
    _wait(page, f"""() => {{ const s = document.querySelector('{section}');
                     return !!s && s.innerText.includes('clean ok') && s.innerText.includes('clean fail'); }}""",
          "both deleted sub-agents listed", 60_000)
    shown = page.locator(section).inner_text()
    # The run's own error (the Gateway's text for the provider's 402), then
    # the canned summary it picked for the row.
    assert "returned a billing error" in shown and "| 402 (Gateway: " in shown, shown
    page.locator(f"{section} button", has_text="clean fail").first.click()
    page.wait_for_selector("#openclaw-drawer-root", timeout=30_000)
    _wait(page, """() => { const c = document.getElementById('openclaw-drawer-chip');
                     const r = document.getElementById('openclaw-drawer-root');
                     return !!c && c.textContent === 'Failed' && r.innerText.includes('removed when it ended'); }""",
          "the deleted child's drawer, failed, saying why its run is gone", 30_000)
    assert "returned a billing error" in page.locator("#openclaw-drawer-root").inner_text()


BG_AGENT_TITLE = "E2E background agent"   # e2e/fake_llm.py BG_AGENT_TITLE


def test_the_status_dialog_lists_a_background_agent_while_its_reply_runs(page):
    """Status dialog mid-reply (ELI-186): a reply that started an Agent in
    the background and is still running. Background and In-process agents
    list that agent while the reply runs, not only once it has ended. The
    scripted model calls the stack's Agent tool (e2e/agent-tool), then runs a
    command that waits for a file this test writes."""
    token = f"t{os.getpid()}_{len(_fake_llm_requests())}"
    # e2e/fake_llm.py bg_agent_gate
    gate = os.path.join(os.path.dirname(os.environ["E2E_FAKE_LLM_LOG"]), f"bg-agent-gate-{token}")
    capture.new_chat(page)
    try:
        capture.send(page, f"E2E_BG_AGENT {token}")
        _wait(page, _reply_js(0, "r.text.includes('E2E_BG_AGENT_MID') && !r.done"), "the reply running past its Agent call")
        _open_status(page)
        # Background lists it running; In-process agents counts it (its fold
        # opens by itself only for an agent the reply runs in the foreground).
        for section, shown in (("background", BG_AGENT_TITLE), ("agents", "In-process agents (1)")):
            _wait(page, f"""() => {{ const s = document.querySelector('#openclaw-status-section-{section}');
                             return !!s && s.textContent.includes({json.dumps(shown)}); }}""",
                  f"the background agent in the Status dialog's {section} section", 60_000)
        # The reply cannot end before the gate exists: this was mid-reply.
        assert not _replies(page)[0]["done"], "the reply ended before the dialog listed its agent"
        background = page.locator("#openclaw-status-section-background").inner_text()
        assert "Running" in background, background
        page.keyboard.press("Escape")
    finally:
        open(gate, "w").close()
    _wait(page, _turn_over("E2E_BG_AGENT_END"), "the reply's end")
    os.remove(gate)


BG_EXEC_TITLE, BG_EXEC_COMMAND = "E2E background sleep", "sleep 25; echo e2e-bg-done"   # e2e/fake_llm.py


def test_a_background_command_reads_by_its_title_and_a_tap_shows_the_command(page):
    """ELI-232: a command left running in the background is listed by the
    title its exec call gave it, its first real command under it, and the
    whole command on hover and on a tap, not the raw command as its title."""
    capture.new_chat(page)
    capture.send(page, "E2E_BG_EXEC")
    _wait(page, _turn_over("E2E_BG_STARTED"), "the reply that leaves the command running")
    _open_status(page)
    section = "#openclaw-status-section-background"
    _wait(page, f"""() => {{ const s = document.querySelector('{section}');
                     return !!s && s.innerText.includes({json.dumps(BG_EXEC_TITLE)}); }}""",
          "the command in the Background section, by its title", 60_000)
    row = page.locator(f"{section} [data-openclaw-bg-row]", has_text=BG_EXEC_TITLE).first
    assert "Command · sleep 25" in row.inner_text(), row.inner_text()
    assert row.get_attribute("title") == BG_EXEC_COMMAND
    assert row.locator("[data-openclaw-command]").count() == 0
    row.click()
    assert row.locator("[data-openclaw-command]").inner_text() == BG_EXEC_COMMAND
    row.click()
    assert row.locator("[data-openclaw-command]").count() == 0
    page.keyboard.press("Escape")


def test_a_background_command_that_ends_shows_done_in_the_open_dialog(page):
    """ELI-256: with the Status dialog left open, a background command that
    ended kept its row "Running", its timer counting up, until the dialog was
    closed and opened again. The Gateway pushes nothing when it ends, so the
    open dialog has to read the section again while a row runs."""
    capture.new_chat(page)
    capture.send(page, "E2E_BG_EXEC")
    _wait(page, _turn_over("E2E_BG_STARTED"), "the reply that leaves the command running")
    _open_status(page)
    row = "#openclaw-status-section-background [data-openclaw-bg-row]"
    has = f"""(want) => {{ const r = [...document.querySelectorAll('{row}')]
                  .find(e => e.innerText.includes({json.dumps(BG_EXEC_TITLE)}));
                  return !!r && r.innerText.includes(want); }}"""
    page.wait_for_function(has, arg="Running", timeout=60_000)
    # sleep 25: the same dialog, never closed, shows it done.
    page.wait_for_function(has, arg="Done", timeout=90_000)
    assert page.locator("#openclaw-status-modal-root").count() == 1
    page.keyboard.press("Escape")


# The drawer's close control (× on a wide screen, ‹ Back on a phone) is on
# screen and is what a tap at its centre reaches; and the task shown whole
# scrolls inside its own box, which ends on screen.
DRAWER_CLOSABLE = """() => {
  const root = document.getElementById('openclaw-drawer-root');
  const close = root && [...root.querySelectorAll('button')].find(b => /^(×|‹ Back)$/.test(b.textContent.trim()));
  const body = root && root.querySelector('#openclaw-drawer-task [data-openclaw-block-text]');
  if (!close || !body) return 'no close control or task text';
  const r = close.getBoundingClientRect();
  const W = innerWidth, H = innerHeight;
  if (r.top < 0 || r.left < 0 || r.bottom > H || r.right > W) return 'close off screen: ' + JSON.stringify(r);
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  if (!hit || !close.contains(hit)) return 'close covered by ' + (hit ? hit.outerHTML.slice(0, 80) : null);
  const b = body.getBoundingClientRect();
  if (b.bottom > H) return 'task text runs off screen: bottom ' + b.bottom + ' > ' + H;
  if (body.scrollHeight <= body.clientHeight) return 'task text does not scroll';
  return 'ok';
}"""


@pytest.mark.parametrize("screen", ["desktop", "phone"])
def test_a_long_task_shown_whole_keeps_the_drawer_closable(page, token, screen):
    """Sub-agent drawer, "Show all" on a long task (ELI-201): the task grew
    with no cap, past the bottom of the screen, and the user could not close the
    drawer. Shown whole, it scrolls in its own box and the close control
    stays in reach."""
    view = page if screen == "desktop" else capture.signed_in_page(page.context.browser, token, capture.SMALL_PHONE)
    try:
        capture.new_chat(view)
        capture.send(view, "E2E_SPAWN_LONG one helper")
        _wait(view, _turn_over("E2E_SPAWNED"), "the parent's reply")
        _open_status(view)
        section = "#openclaw-status-section-subagents"
        _wait(view, f"""() => {{ const s = document.querySelector('{section}');
                         return !!s && s.innerText.includes('E2E_CHILDTASK'); }}""",
              "the sub-agent in the Status dialog", 60_000)
        view.locator(f"{section} button", has_text="E2E_CHILDTASK").first.click()
        view.wait_for_selector('#openclaw-drawer-root [data-openclaw-more="task"]', timeout=30_000)
        view.locator('#openclaw-drawer-root [data-openclaw-more="task"]').click()
        _wait(view, """() => (document.querySelector('[data-openclaw-more="task"]') || {}).textContent === 'Show less'""",
              "the task shown whole", 10_000)
        verdict = view.evaluate(DRAWER_CLOSABLE)
        assert verdict == "ok", verdict
        view.locator("#openclaw-drawer-root button", has_text=re.compile(r"^(×|‹ Back)$")).click()
        _wait(view, "() => !document.getElementById('openclaw-drawer-root')", "the drawer closed", 10_000)
    finally:
        if view is not page:
            view.close()


# The task text's first line is what its box shows: the box is scrolled to the
# top and the first characters lie inside it, on screen.
TASK_SHOWS_FIRST_LINE = """() => {
  const body = document.querySelector('#openclaw-drawer-task [data-openclaw-block-text]');
  if (!body) return 'no task text';
  if (body.scrollTop !== 0) return 'task box scrolled to ' + body.scrollTop;
  const node = body.firstChild;
  const range = document.createRange();
  range.setStart(node, 0);
  range.setEnd(node, Math.min(10, node.length));
  const r = range.getBoundingClientRect(), b = body.getBoundingClientRect();
  if (r.top < b.top - 1 || r.bottom > b.bottom + 1) return 'first line outside the box: ' + r.top + '..' + r.bottom + ' vs ' + b.top + '..' + b.bottom;
  if (b.top < 0 || b.bottom > innerHeight) return 'task box off screen: ' + b.top + '..' + b.bottom;
  return 'ok';
}"""


def test_show_less_puts_a_long_task_back_at_its_first_lines(page, token):
    """Sub-agent drawer (ELI-236): "Show all" on a long task, the task's box
    scrolled down, then "Show less". The two-line preview must show the
    task's first lines again, not two lines from wherever the box was
    scrolled to."""
    capture.new_chat(page)
    capture.send(page, "E2E_SPAWN_LONG one helper")
    _wait(page, _turn_over("E2E_SPAWNED"), "the parent's reply")
    _open_status(page)
    section = "#openclaw-status-section-subagents"
    _wait(page, f"""() => {{ const s = document.querySelector('{section}');
                     return !!s && s.innerText.includes('E2E_CHILDTASK'); }}""",
          "the sub-agent in the Status dialog", 60_000)
    page.locator(f"{section} button", has_text="E2E_CHILDTASK").first.click()
    more = page.locator('#openclaw-drawer-root [data-openclaw-more="task"]')
    more.wait_for(timeout=30_000)
    more.click()
    _wait(page, """() => (document.querySelector('[data-openclaw-more="task"]') || {}).textContent === 'Show less'""",
          "the task shown whole", 10_000)
    scrolled = page.evaluate("""() => {
      const body = document.querySelector('#openclaw-drawer-task [data-openclaw-block-text]');
      body.scrollTop = body.scrollHeight;
      return body.scrollTop; }""")
    assert scrolled > 0, "the task box did not scroll, so this proves nothing"
    more.click()
    _wait(page, """() => (document.querySelector('[data-openclaw-more="task"]') || {}).textContent === 'Show all'""",
          "the task clamped again", 10_000)
    verdict = page.evaluate(TASK_SHOWS_FIRST_LINE)
    assert verdict == "ok", verdict


def _prompts(token: str) -> dict:
    """Open WebUI's Workspace Prompts, by command."""
    return {p["command"]: p for p in capture.api("GET", "/api/v1/prompts/", token=token) or []}


def _wait_prompts(token: str, ok, what: str, timeout_s: float = 120) -> dict:
    return capture.poll(lambda: _prompts(token), ok, what, timeout_s)


def _slash_menu_js(command: str) -> str:
    return (f"() => [...document.querySelectorAll('#suggestions-container button span:first-child')]"
            f".some(s => s.innerText.trim() === {json.dumps(command)})")


def test_slash_commands_show_in_the_slash_menu_and_go_when_turned_off(page, token):
    """SYNC_SLASH_COMMANDS: the Gateway's commands show in the composer's /
    menu as prompts, excluded ones left out and a prompt someone made with
    the same command left as it was. Picking one puts the command in the
    composer and sending it runs it. Turned off, the synced prompts go and
    the hand-made one stays."""
    # The hand-made prompt takes a command the Gateway has, so the sync has one to skip.
    gateway = capture.gateway_call("commands.list", {"agentId": "main", "scope": "text"})["commands"]
    assert "whoami" in {c.get("name") for c in gateway}, "precondition: the Gateway has no /whoami"
    mine = capture.api("POST", "/api/v1/prompts/create",
                       {"command": "whoami", "name": "Mine", "content": "my own text"}, token)
    old = capture.pipe_valves(token, SYNC_SLASH_COMMANDS=True)
    try:
        _reload(page)  # a page load lists the models, which starts a sync
        prompts = _wait_prompts(token, lambda p: "help" in p, "the synced /help prompt")
        # Open WebUI runs a lone /status or /compact itself: excluded by default.
        for left_out in ("stop", "new", "status", "compact"):
            assert left_out not in prompts, sorted(prompts)
        assert prompts["whoami"]["content"] == "my own text", prompts["whoami"]
        # Skipped, not taken over: not the sync's (it marks its own), and the same row.
        assert prompts["whoami"]["id"] == mine["id"], prompts["whoami"]
        assert not (prompts["whoami"].get("meta") or {}).get("openclaw"), prompts["whoami"]
        assert prompts["help"]["meta"]["openclaw"]["command"] == "help", prompts["help"]
        assert prompts["help"]["content"].strip() == "/help", prompts["help"]
        capture.new_chat(page)
        page.locator("#chat-input").click()
        page.keyboard.type("/hel", delay=20)
        _wait(page, _slash_menu_js("help"), "/help in the / menu", 30_000)
        page.locator("#suggestions-container button", has=page.locator("span", has_text=re.compile(r"^help$"))).first.click()
        _wait(page, "() => document.querySelector('#chat-input').innerText.trim() === '/help'",
              "the command in the composer", 30_000)
        page.locator("#send-message-button").click()
        _wait(page, _last_assistant_js("t.includes('/commands for full list')"), "the /help reply")
    finally:
        capture.pipe_valves(token, **old)
        try:
            _reload(page)
            prompts = _wait_prompts(token, lambda p: "help" not in p, "the synced prompts removed")
            assert prompts.get("whoami", {}).get("content") == "my own text", sorted(prompts)
        finally:
            capture.api("DELETE", f"/api/v1/prompts/id/{mine['id']}/delete", token=token)


def _chat_id(page) -> str:
    m = re.search(r"/c/([0-9a-f-]{36})", page.url)
    assert m, f"no chat id in {page.url}"
    return m.group(1)


def test_a_reply_the_agent_starts_streams_into_the_open_tab_and_asks_there(page, token):
    """A reply the agent starts (a cron wake into the chat's session) shows
    in the tab open on the chat with no reload: a new message that streams
    word by word, then the question it ends with as a card in that tab.
    Answering the card reaches the agent."""
    capture.new_chat(page)
    capture.send(page, "E2E_ECHO: BROWSER_WAKE_CHAT")
    _wait(page, _turn_over("BROWSER_WAKE_CHAT"), "the first reply")
    me = capture.api("GET", "/api/v1/auths/", token=token)
    key = f"agent:main:openwebui-{me['id']}-{_chat_id(page)}"
    capture.schedule_wake(key, "E2E_WAKE_ASK", 2)
    _wait(page, _reply_js(1, "r.text.includes('E2E_WAKE_ASK_START') && r.text.includes('Which color?')"),
          "the agent's own reply in the open tab, with no reload")
    texts = _seen(page, _replies(page)[1]["id"])
    assert any("slow-1" in t and "slow-6" not in t for t in texts), \
        f"the reply did not stream in the tab: {_tails(texts)}"
    capture.wait_js(page, "() => !!document.querySelector('input[placeholder=\"Type your answer\"]')",
                    "the question card in the open tab", TURN_MS)
    page.locator("section button", has=page.locator("span", has_text="Green")).first.click()
    capture.wait_js(page, "() => !document.querySelector('input[placeholder=\"Type your answer\"]')",
                    "the card gone after the answer", 30_000)
    _wait(page, _reply_js(-1, "r.text.includes('E2E_ANSWER_END')"), "the reply to the answer")
    reply = _replies(page)[-1]["text"]
    assert "Green" in reply.split("E2E_ANSWERED", 1)[1], reply[-200:]
    # One question, one answer: the reply keeps a single answered card, and a
    # reload neither asks again nor shows the question twice.
    saved = capture.api("GET", f"/api/v1/chats/{_chat_id(page)}", token=token)["chat"]["history"]["messages"]
    asks = [m for m in saved.values() if 'name="Ask User"' in (m.get("content") or "")]
    assert len(asks) == 1 and asks[0]["content"].count('name="Ask User"') == 1, [a["content"] for a in asks]
    page.reload()
    page.wait_for_selector("#chat-input", timeout=60_000)
    _wait(page, _reply_js(-1, "r.done && r.text.includes('E2E_ANSWER_END')"), "the answered reply after a reload")
    assert len(_replies(page)) == 2, [r["text"][-80:] for r in _replies(page)]
    assert not page.query_selector('input[placeholder="Type your answer"]'), "asked again after a reload"
    assert _replies(page)[-1]["text"].count("Which color?") <= 1, _replies(page)[-1]["text"]


def test_stop_on_a_reply_the_agent_starts_stops_its_run(page, token):
    """Stop on a reply the agent started (a cron wake, ELI-192) in the open
    tab: the message ends, and so does the run. Before, the run went on to
    its end, its text shown nowhere. On 2026.9 the wake's cron task is
    cancelled; from 2026.10.5, which has no tasks.cancel, the run is aborted
    by its id. The scripted wake runs a command that waits for a file this
    test writes only once the run is over."""
    hold_token = f"stop{os.getpid()}_{len(_fake_llm_requests())}"
    # e2e/fake_llm.py hold_file
    hold = os.path.join(os.path.dirname(os.environ["E2E_FAKE_LLM_LOG"]), f"hold-{hold_token}")
    capture.new_chat(page)
    capture.send(page, "E2E_ECHO: BROWSER_WAKE_STOP")
    _wait(page, _turn_over("BROWSER_WAKE_STOP"), "the first reply")
    me = capture.api("GET", "/api/v1/auths/", token=token)
    key = f"agent:main:openwebui-{me['id']}-{_chat_id(page)}"
    job = capture.schedule_wake(key, f"E2E_HOLD {hold_token}", 2)
    try:
        _wait(page, _reply_js(1, "r.text.includes('E2E_HOLD_START') && !r.done"),
              "the agent's reply running in the open tab")
        page.locator('button[aria-label="Stop"]').click()
        _wait(page, _reply_js(1, "r.done"), "the reply ended by Stop", 30_000)
        deadline, runs = time.monotonic() + 60, []
        while not runs and time.monotonic() < deadline:
            runs = [e for e in capture.gateway_call("cron.runs", {"id": job}).get("entries") or []
                    if e.get("action") == "finished"]
    finally:
        open(hold, "w").close()
    assert runs, "the wake's run went on after Stop: its job never finished while the command held it"
    assert runs[0].get("status") != "ok", f"the wake's run ran to its end: {runs[0]}"
    history = capture.gateway_call("chat.history", {"sessionKey": key, "limit": 50})
    assert "E2E_HOLD_END" not in json.dumps(history.get("messages")), "the stopped run still answered"
    os.remove(hold)


def test_a_tab_that_loads_the_chat_as_the_agent_starts_a_reply_shows_it(page, token):
    """ELI-230: Open WebUI's loadChat fetches the chat, awaits its tags, then
    assigns the history it fetched first. A reply the agent starts in that
    window is introduced, merged, then overwritten: the tab never showed it
    until a reload. The second tab's tags are held until the reply's message
    is in the DB, which puts its introduction in that window every time. The
    tab shows the reply while it runs; the tab open all along shows it once,
    never blanked, also through the end's second send of it."""
    word = f"LOADRACE{os.getpid()}"
    capture.new_chat(page)
    capture.send(page, "E2E_ECHO: BROWSER_LOAD_RACE")
    _wait(page, _turn_over("BROWSER_LOAD_RACE"), "the first reply")
    chat_id = _chat_id(page)
    me = capture.api("GET", "/api/v1/auths/", token=token)
    key = f"agent:main:openwebui-{me['id']}-{chat_id}"

    def messages():
        return capture.api("GET", f"/api/v1/chats/{chat_id}", token=token)["chat"]["history"]["messages"]
    before = set(messages())
    late = page.context.new_page()
    held = []
    late.route(f"**/api/v1/chats/{chat_id}/tags", lambda route: held.append(route))
    try:
        with late.expect_request(lambda r: r.url.endswith(f"/api/v1/chats/{chat_id}/tags"), timeout=60_000):
            late.goto(page.url)
        capture.gateway_call("chat.send", {"sessionKey": key, "message": f"E2E_SLOW:15 {word}",
                                           "idempotencyKey": f"race-{word}", "deliver": False})
        deadline = time.monotonic() + 60
        while not any(m.get("role") == "assistant" for i, m in messages().items() if i not in before):
            assert time.monotonic() < deadline, "the agent's reply never got its message"
            late.wait_for_timeout(200)  # polls the DB; the route callback runs meanwhile
        assert held, "the late tab's tags request was not held"
        for route in held:
            route.continue_()
        _wait(late, _reply_js(1, f"r.text.includes({json.dumps(word)}) && !r.done"),
              "the agent's reply in the tab that loaded the chat as it began, while it runs")
        for tab in (late, page):
            _wait(tab, _reply_js(1, f"r.done && r.text.includes({json.dumps(_slow(word, 15))})"), "the finished reply")
            shown = [r["text"] for r in _replies(tab) if word in r["text"]]
            assert len(shown) == 1 and shown[0].count(word) == 1, shown
        texts = _seen(page, _replies(page)[1]["id"])
        first = next(n for n, t in enumerate(texts) if word in t)
        assert all(word in t for t in texts[first:]), f"the open tab's reply blanked: {_tails(texts)}"
    finally:
        late.close()


def test_an_attached_image_reaches_the_agent(page):
    """An image attached in the composer shows in the sent message, and the
    agent gets it as an image (the scripted model says how many images it
    received and their types)."""
    path = os.path.join(os.environ.get("E2E_LOG_DIR", "/tmp"), f"browser-attach-{os.getpid()}.png")
    with open(path, "wb") as fh:
        fh.write(capture.forecast_chart_png())
    capture.new_chat(page)
    page.locator('input[type="file"]:not(#camera-input)').set_input_files(path)
    capture.wait_js(page, "() => [...document.querySelectorAll('form img')]"
                          ".some(i => i.src.includes('/api/v1/files/'))",
                    "the image in the composer", 30_000)
    capture.send(page, "E2E_IMAGE_IN what is in this picture")
    _wait(page, _turn_over("E2E_IMAGE_SEEN"), "the reply about the image")
    assert page.evaluate("() => [...document.querySelectorAll('.user-message img')]"
                         ".some(i => i.src.includes('/api/v1/files/'))"), "the sent message shows no image"
    reply = _replies(page)[0]["text"]
    assert "E2E_IMAGE_SEEN 1 image/png" in reply, f"the agent did not get the image: {reply[-200:]}"
    assert "couldn't be relayed" not in reply, reply[-200:]


# Each element of `sel`'s box is inside the screen and inside every ancestor
# that clips it: what is cut off there is not on screen for the user.
FITS = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return 'missing';
  const r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return 'not shown';
  const bad = [];
  if (r.left < 0 || r.top < 0 || r.right > innerWidth + 0.5 || r.bottom > innerHeight + 0.5)
    bad.push('screen ' + JSON.stringify([r.left, r.top, r.right, r.bottom]));
  for (let a = el.parentElement; a; a = a.parentElement) {
    const s = getComputedStyle(a);
    if (s.overflowX === 'visible' && s.overflowY === 'visible') continue;
    const p = a.getBoundingClientRect();
    if (r.left < p.left - 0.5 || r.right > p.right + 0.5 || r.top < p.top - 0.5 || r.bottom > p.bottom + 0.5)
      bad.push('clipped by ' + a.tagName + '.' + (a.className || '').toString().slice(0, 40));
  }
  if (el.scrollWidth > el.clientWidth + 1) bad.push('its own content overflows');
  return bad.length ? bad.join('; ') : 'ok';
}"""


def test_at_phone_width_the_status_button_fits_and_opens_the_dialog(page, token):
    """On a small phone (375 x 667) the composer's Status button is fully on
    screen, not cut off by the chip row, and a tap opens the Status dialog
    inside the screen: its close button and its footer are in view."""
    phone = capture.signed_in_page(page.context.browser, token, capture.SMALL_PHONE)
    try:
        capture.new_chat(phone)
        capture.send(phone, "E2E_ECHO: BROWSER_PHONE")
        _wait(phone, _turn_over("BROWSER_PHONE"), "the reply")
        button = 'button[aria-label="OpenClaw status"]'
        phone.wait_for_selector(button, timeout=30_000)
        assert phone.evaluate(FITS, button) == "ok", f"the Status button: {phone.evaluate(FITS, button)}"
        phone.locator(button).tap()
        phone.wait_for_selector("#openclaw-status-modal-root", timeout=30_000)
        capture.wait_js(phone, "() => /via pipe/.test(document.getElementById('openclaw-status-modal-root').innerText)",
                        "the dialog filled", 30_000)
        for what, sel in (("the dialog", "#openclaw-status-modal-root > div"),
                          ("its close button", "#openclaw-status-modal-root button"),
                          ("its footer", "#openclaw-status-modal-root > div > div:last-child")):
            assert phone.evaluate(FITS, sel) == "ok", f"{what}: {phone.evaluate(FITS, sel)}"
    finally:
        phone.context.close()


E2E_TITLE = "🧪 E2E chat title"   # e2e/fake_llm.py TITLE


def _two_exchanges(page, first: str, second: str) -> str:
    """A new chat with two finished exchanges; returns its id."""
    capture.new_chat(page)
    chat_id = capture.send(page, f"E2E_ECHO: {first}")
    _wait(page, _turn_over(first), "the first reply")
    _type_and_send(page, f"E2E_ECHO: {second}")
    _wait(page, f"() => ({STOP_GONE})() && ({_reply_js(-1, f'all.length === 2 && r.done && r.text.includes({json.dumps(second)})')})()",
          "the second reply")
    return chat_id


def _titles_js(chat_id: str, title: str) -> str:
    """The open tab shows `title`: the sidebar's entry of the chat and the browser tab."""
    return (f"() => document.title.includes({json.dumps(title)}) && [...document.querySelectorAll("
            f"'a[href=\"/c/{chat_id}\"]')].some(a => a.innerText.includes({json.dumps(title)}))")


def _sidebar_js(chat_id: str, title: str) -> str:
    """The sidebar's entry of the chat shows `title` (Open WebUI's own rename
    leaves the browser tab's title as it was)."""
    return (f"() => [...document.querySelectorAll('a[href=\"/c/{chat_id}\"]')]"
            f".some(a => a.innerText.includes({json.dumps(title)}))")


def _shown_titles(page, chat_id: str) -> str:
    return page.evaluate(f"""() => JSON.stringify({{tab: document.title, sidebar: [...document.querySelectorAll(
        'a[href="/c/{chat_id}"]')].map(a => a.innerText.trim())}})""")


@pytest.fixture
def title_events(page):
    """The `chat:title` events the open tab's socket receives, as
    (seconds since the test began, chat id, title), for a failure message: a
    title that never shows is either an event that never came or one the tab
    did not act on, and only this tells the two apart."""
    seen, began = [], time.time()

    def on_frame(payload):
        text = payload if isinstance(payload, str) else payload.decode("utf-8", "replace")
        if '"chat:title"' not in text:
            return
        chat = re.search(r'"chat_id":"([^"]*)"', text)
        title = re.search(r'"type":"chat:title","data":"((?:[^"\\]|\\.)*)"', text)
        seen.append((round(time.time() - began, 2), chat and chat.group(1), title and title.group(1)))

    def on_socket(ws):
        ws.on("framereceived", on_frame)

    page.on("websocket", on_socket)
    yield seen
    page.remove_listener("websocket", on_socket)


def _wait_for_title(page, token, chat_id: str, title: str, what: str, timeout_ms: int, events: list) -> None:
    """Wait until the open tab shows `title`; on a timeout, fail with what the
    tab shows, what is saved, and the `chat:title` events the socket received."""
    try:
        capture.wait_js(page, _titles_js(chat_id, title), what, timeout_ms)
    except TimeoutError as ex:
        raise AssertionError(f"{ex}; the tab shows {_shown_titles(page, chat_id)}, "
                             f"the chat's saved title is {capture.chat_title(token, chat_id)!r}, "
                             f"chat:title events received: {events}") from None


def _wait_for_title_event(page, token, chat_id: str, title: str, what: str, timeout_ms: int, events: list) -> tuple:
    """Wait for the `chat:title` event that names `title` for the chat, the
    signal Open WebUI sends when it titles a chat, then for the tab to act on
    it. Returns the event, so a test can show the path it relies on ran: a
    title that was already there when the wait began would pass a plain look
    at the tab without Open WebUI having sent one."""
    def seen():
        return next((e for e in events if e[1] == chat_id and e[2] and title in e[2]), None)

    deadline = time.time() + timeout_ms / 1000
    while not seen() and time.time() < deadline:
        page.wait_for_timeout(100)
    if not seen():
        raise AssertionError(f"no chat:title event naming {title!r} for chat {chat_id} within {timeout_ms} ms ({what}); "
                             f"the tab shows {_shown_titles(page, chat_id)}, "
                             f"the chat's saved title is {capture.chat_title(token, chat_id)!r}, "
                             f"chat:title events received: {events}")
    event = seen()
    try:
        capture.wait_js(page, _titles_js(chat_id, title), f"the tab to show {what}", 10_000)
    except TimeoutError as ex:
        raise AssertionError(f"{ex}; the chat:title event came at {event[0]} s but the tab shows "
                             f"{_shown_titles(page, chat_id)}") from None
    return event


def _auto_title_decision(chat_id: str, since: str) -> str:
    """The Pipe's auto-title decision for the chat, from its log: read as the
    lines come (Open WebUI's stdout), until the decision line."""
    pattern = re.compile(rf"Auto-title: (?:chat {re.escape(chat_id)}: |set to )")
    proc = subprocess.Popen(["timeout", str(TURN_MS // 1000), "docker", "logs", "-f", "--since", since, "owui"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for line in proc.stdout:
            if pattern.search(line):
                return line.strip()
        raise AssertionError(f"no auto-title decision for chat {chat_id} in the Pipe's log")
    finally:
        proc.kill()
        proc.wait()


def test_the_generated_title_shows_in_the_open_tab(page, token, title_events):
    """After the second exchange the Pipe titles the chat, and the open tab
    shows the new title at once, in the sidebar and the browser tab, with no
    reload (ELI-182: it showed only after a reload)."""
    chat_id = _two_exchanges(page, "BROWSER_TITLE_ONE", "BROWSER_TITLE_TWO")
    _wait_for_title(page, token, chat_id, E2E_TITLE, "the generated title in the open tab", TURN_MS, title_events)


SIDEBAR_OPEN_JS = """() => { const s = document.getElementById('sidebar');
  return !!s && s.clientWidth > 0 && s.scrollWidth <= s.clientWidth && s.scrollLeft === 0; }"""


def _open_sidebar(page) -> None:
    """Open the sidebar and wait until it has stopped widening.

    It opens with a 250 ms width transition over content of its full width,
    clipped until the end. A click on a chat's menu button in that time
    focuses the clipped button, which scrolls the sidebar sideways; the
    sidebar then keeps widening and scrolls back, so the release lands on the
    chat's title and the menu never opens (ELI-228). The end of the opening is
    the sidebar showing all of its content, unscrolled."""
    page.locator('button[aria-label="Open Sidebar"]').first.click()
    capture.wait_js(page, SIDEBAR_OPEN_JS, "the sidebar to finish opening", 10_000)


def _rename_in_sidebar(page, chat_id: str, title: str) -> None:
    """Rename the chat by a double click on its sidebar entry, as a user does.

    The first click of a double click on the entry of a chat started from the
    new-chat page navigates to it: the chat page loads it and ends by focusing
    the message input. Open WebUI moves the focus back to the rename box
    after a tick, and an Enter in between went to the message input, so the
    rename stayed unsaved (ELI-220). So the chat is opened first, the load's
    last step (the message input taking the focus) is awaited, and the
    double click, a click on the open chat, starts no load."""
    link = page.locator(f'a[href="/c/{chat_id}"]')
    with page.expect_response(lambda r: r.request.method == "GET"
                              and r.url.endswith(f"/api/v1/chats/{chat_id}")):
        link.click()
    capture.wait_js(page, "() => !!document.activeElement?.closest('#chat-input')",
                    "the opened chat to focus its message input", 30_000)
    link.dblclick()
    box_id = f"chat-title-input-{chat_id}"
    capture.wait_js(page, f"() => document.activeElement?.id === {json.dumps(box_id)}",
                    "the rename box to take the focus", 10_000)
    box = page.locator(f"#{box_id}")
    box.fill(title)
    with page.expect_request(lambda r: r.method == "POST"
                             and r.url.endswith(f"/api/v1/chats/{chat_id}")) as saved:
        box.press("Enter")
    assert title in (saved.value.post_data or ""), saved.value.post_data
    capture.wait_js(page, _sidebar_js(chat_id, title), "the chosen title in the sidebar", 30_000)


def test_a_title_the_user_chose_stays(page, token, title_events):
    """A chat renamed in the sidebar keeps its name after the next message:
    the Pipe titles only a chat Open WebUI named by itself (ELI-182: the
    rename was replaced by the generated title)."""
    first, chosen = "BROWSER_RENAME_ONE", "BROWSER my own title"
    capture.new_chat(page)
    chat_id = capture.send(page, f"E2E_ECHO: {first}")
    _wait(page, _turn_over(first), "the first reply")
    # Open WebUI's own first title (its first message), before renaming over it:
    # the test is about a rename that outlives a later title, so the title it
    # renames over must have been sent by Open WebUI, not assumed.
    early = _wait_for_title_event(page, token, chat_id, first, "Open WebUI's first title", 60_000, title_events)
    assert capture.chat_title(token, chat_id) == early[2], (
        f"the chat:title event {early} does not match the saved title {capture.chat_title(token, chat_id)!r}")
    # The sidebar is where a user renames a chat; open it if it is closed.
    was_closed = page.locator('button[aria-label="Open Sidebar"]').first.is_visible()
    if was_closed:
        _open_sidebar(page)
    try:
        _rename_in_sidebar(page, chat_id, chosen)
    finally:
        if was_closed:
            page.locator('button[aria-label="Close Sidebar"]').first.click()
    assert capture.chat_title(token, chat_id) == chosen

    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _type_and_send(page, "E2E_ECHO: BROWSER_RENAME_TWO")
    _wait(page, f"() => ({STOP_GONE})() && ({_reply_js(-1, 'all.length === 2 && r.done')})()", "the second reply")
    decision = _auto_title_decision(chat_id, since)
    saved = capture.chat_title(token, chat_id)
    assert saved == chosen, f"the chosen title was replaced by {saved!r} (Pipe: {decision})"
    assert page.evaluate(_sidebar_js(chat_id, chosen)), f"the tab shows {_shown_titles(page, chat_id)}"


# Edit, Regenerate, a branch switch, Fork and Clone (ELI-183): the agent's
# session is one line of messages, so each of these reaches the agent as a
# short note before the message (or, for a copy, the copied messages once).
# What reached the agent is read from the scripted model's request log; the
# earlier messages are plain text, so a quote of them in the note carries no
# marker the scripted model would act on.

def _turn_text(messages: list) -> str:
    """The user's side of a model request's newest turn: the user messages
    after the last assistant message, without the Gateway's internal context
    blocks."""
    tail = []
    for m in reversed(messages):
        if m.get("role") == "assistant":
            break
        if m.get("role") == "user":
            content = m.get("content")
            parts = [content] if isinstance(content, str) else [
                p.get("text", "") for p in content or [] if isinstance(p, dict)]
            tail[:0] = [t for t in parts if not t.startswith("<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>")]
    return "\n".join(tail)


def _to_model(needle: str) -> list:
    """The newest turn's user text of each model request whose newest turn
    contains `needle`, oldest first."""
    found = []
    for r in _fake_llm_requests():
        text = _turn_text(r["request"].get("messages") or [])
        if needle in text:
            found.append(text)
    return found


def _last_reply_done(needle: str) -> str:
    done = _reply_js(-1, f"r.done && r.text.includes({json.dumps(needle)})")
    return f"() => ({STOP_GONE})() && ({done})()"


def _say(page, text: str, needle: str = "E2E fake model reply.") -> None:
    """Send in the open chat (a new chat on the first message) and wait for the reply."""
    if "/c/" in page.url:
        _type_and_send(page, text)
    else:
        capture.send(page, text)
    _wait(page, _last_reply_done(needle), f"the reply to {text!r}")


def _edit_user_message(page, index: int, text: str) -> None:
    """Edit user message `index` and press Send: Open WebUI sends the edit as a
    new sibling of that message."""
    msg = page.locator(".user-message").nth(index)
    msg.hover()
    msg.locator('button[aria-label="Edit"]').click()
    box = page.locator('textarea[id^="message-edit-"]')
    box.fill(text)
    page.locator("#confirm-edit-message-button").click()


def test_an_edited_message_reaches_the_agent_as_an_edit(page):
    """Edit: the agent gets the edited message with a note naming the message
    it replaces, so it answers the chat as shown."""
    n = os.getpid()
    capture.new_chat(page)
    _say(page, f"first plain message {n}")
    _say(page, f"second plain message {n}")
    _edit_user_message(page, 1, f"E2E_ECHO: EDITED_{n}")
    _wait(page, _last_reply_done(f"EDITED_{n}"), "the reply to the edit")
    sent = _to_model(f"EDITED_{n}")
    assert sent and f'the user edited their message "second plain message {n}"' in sent[-1], sent[-1:]
    assert "[Open WebUI:" not in _to_model(f"second plain message {n}")[0], "a normal send carried a note"


def test_a_regenerated_reply_reaches_the_agent_as_a_regenerate(page):
    """Regenerate: the agent gets the same message again with a note that its
    earlier answer is no longer in the chat."""
    n = os.getpid()
    capture.new_chat(page)
    _say(page, f"E2E_ECHO: REGEN_{n}", f"REGEN_{n}")
    page.locator(".chat-assistant").last.hover()
    page.locator('button[aria-label="Regenerate"]').last.click()
    page.get_by_text("Try Again", exact=True).click()
    _wait(page, f"() => ({STOP_GONE})() && /\\b2\\s*\\/\\s*2\\b/.test(document.body.innerText)",
          "the second answer (2/2)")
    _wait(page, _last_reply_done(f"REGEN_{n}"), "the regenerated reply")
    sent = _to_model(f"REGEN_{n}")
    assert len(sent) >= 2, sent
    assert "[Open WebUI:" not in sent[0], sent[0]
    assert "the user asked for a new answer to the message below" in sent[-1], sent[-1]


def test_switching_branches_reaches_the_agent_as_a_branch_switch(page):
    """Branch switch: after an edit, the arrows go back to the original
    message; the next message reaches the agent with a note naming what is no
    longer shown and what is."""
    n = os.getpid()
    capture.new_chat(page)
    _say(page, f"first plain message {n}")
    _say(page, f"original branch {n}")
    _edit_user_message(page, 1, f"edited branch {n}")
    _wait(page, f"() => ({STOP_GONE})() && /\\b2\\s*\\/\\s*2\\b/.test(document.body.innerText)",
          "the edit as branch 2/2")
    _wait(page, _last_reply_done("E2E fake model reply."), "the reply to the edit")
    msg = page.locator(".user-message").nth(1)
    msg.hover()
    page.locator('button[aria-label="Previous message"]').first.click()
    _wait(page, _user_message_js(f"original branch {n}"), "the original branch shown again", 30_000)
    _type_and_send(page, f"E2E_ECHO: AFTER_SWITCH_{n}")
    _wait(page, _last_reply_done(f"AFTER_SWITCH_{n}"), "the reply on the original branch")
    sent = _to_model(f"AFTER_SWITCH_{n}")
    assert sent and "the user switched to another branch" in sent[-1], sent[-1:]
    assert f'2 messages from "edited branch {n}" on are no longer shown' in sent[-1], sent[-1]
    assert f'2 messages from "original branch {n}" on' in sent[-1], sent[-1]


def _first_send_of_copy(page, n: int, word: str) -> str:
    _type_and_send(page, f"E2E_ECHO: {word}_{n}")
    _wait(page, _last_reply_done(f"{word}_{n}"), f"the reply in the {word.lower()}")
    sent = _to_model(f"{word}_{n}")
    assert len(sent) == 1, sent
    return sent[0]


def test_a_forked_chat_starts_with_the_copied_messages(page):
    """Fork (a reply's Fork chat button): the new chat's first message
    reaches the agent with the copied messages once; its second does not."""
    n = os.getpid()
    capture.new_chat(page)
    source = capture.send(page, f"fork source question {n}")
    _wait(page, _last_reply_done("E2E fake model reply."), "the source chat's reply")
    page.locator(".chat-assistant").last.hover()
    page.locator('button[aria-label="Fork chat"]').last.click()
    _wait(page, f"() => /^\\/c\\//.test(location.pathname) && !location.pathname.includes({json.dumps(source)})",
          "the forked chat open", 30_000)
    page.wait_for_selector("#chat-input", timeout=60_000)
    first = _first_send_of_copy(page, n, "FORKED")
    assert "this chat is a copy of another chat" in first, first[:300]
    assert f"User: fork source question {n}" in first and "Assistant: E2E fake model reply." in first, first[:600]
    _say(page, f"E2E_ECHO: FORK_AGAIN_{n}", f"FORK_AGAIN_{n}")
    assert "copy of another chat" not in _to_model(f"FORK_AGAIN_{n}")[-1]


def test_a_cloned_chat_starts_with_the_copied_messages(page):
    """Clone (the chat menu's Clone): the same as a fork, for the whole chat."""
    n = os.getpid()
    capture.new_chat(page)
    source = capture.send(page, f"clone source question {n}")
    _wait(page, _last_reply_done("E2E fake model reply."), "the source chat's reply")
    # Clone is in the sidebar's menu for the chat.
    _open_sidebar(page)
    menus = page.locator('button[aria-label="Chat Menu"]')
    mine = page.evaluate("""(href) => [...document.querySelectorAll('button[aria-label="Chat Menu"]')]
      .findIndex(b => { for (let el = b.parentElement, i = 0; el && i < 6; el = el.parentElement, i++)
                          if (el.querySelector(`a[href="${href}"]`)) return true; return false; })""",
                         f"/c/{source}")
    assert mine >= 0, "no sidebar menu for the chat"
    menus.nth(mine).click()
    page.locator("[role=menu]").get_by_text("Clone", exact=True).click()
    _wait(page, f"() => /^\\/c\\//.test(location.pathname) && !location.pathname.includes({json.dumps(source)})",
          "the clone open", 30_000)
    page.wait_for_selector("#chat-input", timeout=60_000)
    first = _first_send_of_copy(page, n, "CLONED")
    assert "this chat is a copy of another chat" in first, first[:300]
    assert f"User: clone source question {n}" in first, first[:600]


ELI195_UPSTREAM = "https://github.com/open-webui/open-webui/issues/32742"


class Eli195Present(AssertionError):
    """The dialog's answer never reached the function. Only this fails the
    drop gate's expected way."""


class Eli195ProbeIneffective(AssertionError):
    """`raw` could not show either way: the probe never got the `execute`
    under the open dialog, or never replied. A real failure, never the
    expected one, never a pass."""


# A function that asks for input and, a second later, sends an `execute` to
# the user's tabs while the dialog is open: open-webui#32742's probe. With
# HOLD it wraps both the way the Pipe does before a turn (pipe.py, ELI-195),
# from the Pipe file the test copied into the container.
ELI195_PROBE = '''"""
title: ELI-195 probe
"""
import asyncio
import importlib.util

HOLD = %s


class Pipe:
    async def pipe(self, body: dict, __user__=None, __metadata__=None,
                   __event_emitter__=None, __event_call__=None):
        # A new chat's tab learns the chat id only when the request returns,
        # and drops a dialog for a chat it does not show yet: ask in the
        # chat's second turn, never its first.
        if sum(m.get("role") == "user" for m in body.get("messages", [])) < 2:
            return "ready"
        emit, call = __event_emitter__, __event_call__
        if HOLD:
            spec = importlib.util.spec_from_file_location("openclaw_pipe_probe", "/tmp/openclaw_pipe_probe.py")
            pipe = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(pipe)
            meta, user_id = __metadata__ or {}, (__user__ or {}).get("id")
            emit = pipe._dialog_safe_emitter(emit, user_id, meta.get("chat_id"), meta.get("message_id"))
            call = pipe._dialog_safe_event_call(call, meta.get("session_id"), user_id)

        async def poke():
            await asyncio.sleep(1)
            await emit({"type": "execute", "data": {"code": "window.__eli195Poked = true"}})
            await __event_emitter__({"type": "status", "data": {"description": "execute sent", "done": False}})

        poked = asyncio.create_task(poke())
        try:
            value = await asyncio.wait_for(call({"type": "input", "data": {
                "title": "Probe", "message": "Type anything", "placeholder": "here"}}), 30)
        except asyncio.TimeoutError:
            value = "NO ANSWER"
        await poked
        return f"got {value!r}"
'''


@pytest.mark.parametrize("mode", [
    "held",
    # Fixed upstream in 0.12.4 (open-webui#32744): the raw mode is expected to fail only on older
    # releases, and to pass from 0.12.4 on.
    pytest.param("raw", marks=pytest.mark.xfail(not has_fix_0_12_4(), strict=True, raises=Eli195Present, reason=(
        "ELI-195 drop gate: on this Open WebUI an `execute` reaching a tab while its input dialog "
        f"is open takes the dialog's answer ({ELI195_UPSTREAM}). An XPASS means the pinned release "
        "fixed it: drop the Pipe's execute hold (emit.py dialog holds) and this gate."))),
])
def test_an_execute_while_the_input_dialog_is_open_leaves_the_dialog_its_answer(page, token, mode):
    """ELI-195: a function's `execute` that reaches a tab while that tab
    shows an input dialog. The probe produces it every time: it asks, then
    sends an `execute` a second later; the page must run that `execute` while
    the dialog is open (or, held, right after it) and the answer must reach
    the function. `held` (the Pipe's wrappers) must always pass; `raw` shows
    whether Open WebUI still needs them."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "openclaw_pipe.py"), "rb") as f:
        subprocess.run(["docker", "exec", "-i", "owui", "sh", "-c", "cat > /tmp/openclaw_pipe_probe.py"], stdin=f,
                       check=True, capture_output=True, timeout=60)
    fid = f"eli195_probe_{mode}"
    capture.api("DELETE", f"/api/v1/functions/id/{fid}/delete", token=token) if any(
        f.get("id") == fid for f in capture.api("GET", "/api/v1/functions/", token=token)) else None
    capture.api("POST", "/api/v1/functions/create", {"id": fid, "name": fid, "meta": {"description": fid},
                                                     "content": ELI195_PROBE % (mode == "held")}, token=token)
    capture.api("POST", f"/api/v1/functions/id/{fid}/toggle", token=token)
    try:
        _eli195_probe(page, mode, fid)
    except Eli195Present:
        raise
    except Exception as ex:
        if mode == "raw":
            # Raw has three outcomes only: the bug (Eli195Present), its
            # absence (a pass), or this.
            raise Eli195ProbeIneffective(f"{type(ex).__name__}: {ex}") from None
        raise
    finally:
        capture.api("DELETE", f"/api/v1/functions/id/{fid}/delete", token=token)


def _eli195_probe(page, mode: str, fid: str) -> None:
    page.goto(f"{capture.BASE}/?models={fid}")
    page.wait_for_selector("#chat-input", timeout=60_000)
    capture.dismiss_popups(page)
    page.evaluate("() => { window.__eli195Poked = false; }")
    capture.send(page, "hi")
    _wait(page, _reply_js(-1, "r.done && r.text.includes('ready')"), "the probe's first reply", 60_000)
    capture.send(page, "ask")
    page.get_by_text("Type anything").wait_for(timeout=60_000)
    # The precondition: the `execute` went out while the dialog is open;
    # raw, the page also ran it under the dialog; held, it did not.
    page.get_by_text("execute sent").wait_for(timeout=30_000)
    assert page.get_by_text("Type anything").is_visible(), "the dialog closed before the execute"
    if mode == "raw":
        _wait(page, "() => window.__eli195Poked === true", "the execute while the dialog is open", 30_000)
    else:
        assert page.evaluate("() => window.__eli195Poked") is False, "the held execute ran under the dialog"
    page.locator("input[placeholder=here], textarea[placeholder=here]").first.fill("hello")
    page.get_by_role("button", name="Confirm", exact=True).click()
    # The probe always replies `got <answer>`, `got 'NO ANSWER'` after 30 s.
    _wait(page, _last_assistant_js("t.includes('got ')"), "the probe's reply", 60_000)
    reply = page.evaluate(f"() => {capture.LAST_ASSISTANT}.innerText")
    print(f"ELI-195 probe ({mode}): execute ran under the dialog: {mode == 'raw'}; reply: {reply[-80:]!r}")
    if "got 'hello'" not in reply:
        raise Eli195Present(reply[-200:])
    if mode == "held":
        # Held, the `execute` runs once the dialog is answered.
        _wait(page, "() => window.__eli195Poked === true", "the held execute after the answer", 30_000)


def test_one_message_to_two_models_is_answered_by_each_without_a_note(page, token):
    """Several models side by side (ELI-329): the message goes to each model in
    its own session. Neither gets a Regenerate note, and Try Again on one reply
    reaches the agent as a Regenerate for that model only."""
    n = os.getpid()
    models = capture.api("GET", "/api/models", token=token)["data"]
    echo = next(m["id"] for m in models
                if m["id"].startswith("openclaw_gateway.") and m["id"].endswith("/e2e-echo"))
    page.goto(f"{capture.BASE}/?models={capture.MODEL},{echo}")
    page.wait_for_selector("#chat-input", timeout=60_000)
    capture.dismiss_popups(page)
    capture.send(page, f"E2E_ECHO: MULTI_{n}")
    both = (f"() => ({STOP_GONE})() && (() => {{ const all = ({REPLIES})(); return all.length === 2 && "
            f"all.every(r => r.done && r.text.includes('MULTI_{n}')); }})()")
    _wait(page, both, "both models' replies")
    sent = _to_model(f"MULTI_{n}")
    assert len(sent) == 2, sent
    assert all("[Open WebUI:" not in t for t in sent), sent
    # Try Again on the second reply.
    page.locator(".chat-assistant").last.hover()
    page.locator('button[aria-label="Regenerate"]').last.click()
    page.get_by_text("Try Again", exact=True).click()
    _wait(page, f"() => ({STOP_GONE})() && /\\b2\\s*\\/\\s*2\\b/.test(document.body.innerText)",
          "the second answer (2/2)")
    _wait(page, _last_reply_done(f"MULTI_{n}"), "the regenerated reply")
    sent = _to_model(f"MULTI_{n}")
    assert len(sent) == 3 and "the user asked for a new answer" in sent[-1], sent
    assert "[Open WebUI:" not in sent[0] + sent[1], sent
