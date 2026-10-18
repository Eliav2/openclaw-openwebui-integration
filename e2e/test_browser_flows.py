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

import pytest

# Before the browser imports: the unit job collects e2e/ without Playwright.
if os.environ.get("E2E_STACK") != "1":
    pytest.skip("end-to-end stack not running (set E2E_STACK=1; see e2e/start_stack.sh)",
                allow_module_level=True)

from playwright.sync_api import sync_playwright  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "screenshots"))
import capture  # noqa: E402

TURN_MS = 120_000


def _openclaw_release() -> tuple:
    """(year, month) of the Gateway the stack installed, from `openclaw --version`."""
    out = subprocess.run(["openclaw", "--version"], capture_output=True, text=True, timeout=60)
    m = re.search(r"(\d{4})\.(\d{1,2})\.\d+", out.stdout)
    assert m, f"no version in {out.stdout!r} {out.stderr[-300:]!r}"
    return int(m.group(1)), int(m.group(2))


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
    capture.wait_js(page, _last_assistant_js("t.includes('E2E_ANSWER_END')"),
                    "the whole reply to the answer", TURN_MS)
    return page.evaluate(f"() => {capture.LAST_ASSISTANT}.innerText")


def test_a_single_select_question_is_answered_with_a_click(page):
    """The card shows the options as rows; a click answers at once and the
    agent gets the option."""
    def click_green(pg):
        pg.locator("section button", has=pg.locator("span", has_text="Green")).first.click()
    reply = _answer_question(page, "E2E_ASK_SINGLE", "Which color?", click_green)
    assert "E2E_ANSWERED" in reply and "Green" in reply.split("E2E_ANSWERED", 1)[1], reply[-200:]


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


def _both_answered(page, run: str, answer: str) -> None:
    """The running reply went on to its end in its own message, nothing cut,
    and the new message got its own answer, with none of the run in it; the
    same after a reload. Open WebUI draws the running reply finished as soon
    as the new message goes in, while it is still being written, so it is
    judged once the new message's answer, which waits for the run, is done."""
    _wait(page, _reply_js(-1, "all.length === 2 && all[0].done && r.done"), "both replies done")
    first, second = _replies(page)
    assert first["text"].count(run) == 1, f"the running reply was cut or doubled: {first['text'][-120:]!r}"
    assert answer in second["text"] and "slow-" not in second["text"], second["text"]
    _reload(page)
    _wait(page, _reply_js(-1, "all.length === 2 && all[0].done && r.done"), "both replies after a reload", 60_000)
    again = _replies(page)
    assert run in again[0]["text"] and answer in again[1]["text"], [r["text"][-80:] for r in again]


def test_a_message_sent_while_a_reply_runs_is_in_the_chat_at_once(page):
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
    _both_answered(page, run, "BROWSER_BUSY_ANSWER")


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
        _both_answered(page, run, "BROWSER_SENDNOW_ANSWER")
    finally:
        capture.pipe_valves(token, **old)


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


def test_a_reload_mid_reply_shows_the_answer_so_far_and_keeps_going(page):
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
    _both_answered(page, full, "BROWSER_RELOAD_ANSWER")


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

    assert "e2e-cards-slept" in _open_card(page, 0, "View Result from exec")
    assert "tool call failed" in _open_card(page, 0, "View Result from read")

    _reload(page)
    _wait(page, _reply_js(0, "r.done && r.text.includes('E2E_CARDS_END')"), "the reply after a reload", 60_000)
    rows = page.evaluate("() => [...document.querySelectorAll('.chat-assistant')][0]"
                         ".querySelectorAll('[role=button]').length")
    text = _replies(page)[0]["text"]
    assert rows == 2 and "read ❌" in text and "View Result from exec" in text, text[-200:]


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
    dialog comes back with the numbers."""
    if _openclaw_release() >= (2026, 10):
        pytest.skip("ELI-177: on OpenClaw 2026.10 the session gets no usage, so the dialog has no "
                    "context numbers or Compact button; remove when fixed")
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
