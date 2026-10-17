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


@pytest.fixture(scope="module")
def page():
    token = capture.signin()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        pg = capture.signed_in_page(browser, token, capture.DESKTOP)
        yield pg
        browser.close()


def _last_assistant_js(body: str) -> str:
    """A predicate on the last assistant message's visible text `t`."""
    return (f"() => {{ const a = {capture.LAST_ASSISTANT}; if (!a) return false; "
            f"const t = a.innerText; return {body}; }}")


# Every distinct text the last assistant message shows, from every DOM change:
# a state the user saw cannot fall between two polls.
RECORD_TEXTS = f"""() => {{
  window.__seen = [];
  const push = () => {{
    const a = {capture.LAST_ASSISTANT};
    const t = a ? a.innerText.trim() : '';
    const s = window.__seen;
    if (t && s[s.length - 1] !== t) s.push(t);
  }};
  new MutationObserver(push).observe(document.body, {{subtree: true, childList: true, characterData: true}});
}}"""


def _seen(page) -> list:
    return [t[-60:] for t in page.evaluate("() => window.__seen || []")]


def test_send_streams_the_reply_and_keeps_it(page):
    """Send/stream: the reply shows word by word while the run goes, ends as
    the full reply with the composer ready again, and is still there after a
    reload. The scripted model sends one word a second."""
    words = " ".join(f"slow-{i}" for i in range(1, 11))
    full = f"BROWSER_STREAM {words}"
    capture.new_chat(page)
    page.evaluate(RECORD_TEXTS)
    chat_id = capture.send(page, "E2E_SLOW BROWSER_STREAM")

    try:
        capture.wait_js(page, _last_assistant_js(f"t.includes({json.dumps(full)})"),
                        "the full reply", TURN_MS)
        # The turn is over for the user: Open WebUI shows Stop while the last
        # message is not done (MessageInput.svelte `isActive`), and hides it after.
        capture.wait_js(page, "() => !document.querySelector('button[aria-label=\"Stop\"]')",
                        "the Stop button gone (the reply is done)", TURN_MS)
    except TimeoutError as ex:
        raise AssertionError(f"{ex}; texts the reply showed: {_seen(page)}") from None
    partial = [t for t in _seen(page) if "BROWSER_STREAM slow-2" in t and "slow-10" not in t]
    assert partial, f"the reply never showed part way through; texts it showed: {_seen(page)}"

    page.reload()
    page.wait_for_selector("#chat-input", timeout=60_000)
    assert chat_id in page.url
    capture.wait_js(page, _last_assistant_js(f"t.includes({json.dumps(full)})"),
                    "the reply after a reload", 60_000)
