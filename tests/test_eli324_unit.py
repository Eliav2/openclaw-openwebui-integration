"""ELI-324: a reply the agent sends with its `message` tool is shown once.

An agent that answers an inter-session send (or any agent turn) with
`message(action=send, final=true)` leaves three assistant messages in the
Gateway's transcript: the send's delivery mirror (`<runId>:message-tool:...`),
whatever text the model wrote after it (here "Delivered."), and, because the
send was the run's final reply, one more record with the sent text again. The
rebuilt reply was "<sent>\\n\\nDelivered.<sent>". The fixture is that run as
2026.10.5 returned it from `chat.history` (a QA chat, haiku 5.5).

Run: python3 -m pytest -q tests/test_eli324_unit.py
"""
import copy
import json
import os
import unittest

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
FX = json.load(open(os.path.join(FIXTURES, "test_fixture_eli324_message_tool_final.json")))
RUN = "1d72c285-4234-42d7-b757-2078e861cf22"
SENT = "QA-SENT-TEXT-GAMMA this is the sent text"


def _says(message, text):
    content = message.get("content")
    return isinstance(content, list) and any(b.get("text") == text for b in content if isinstance(b, dict))


def _render(messages):
    history = {"messages": ocp._transcript_roles(copy.deepcopy(messages))}
    return ocp._history_renderer(ocp._history_run_messages(history, RUN))


class MessageToolRepeatTests(unittest.TestCase):
    def test_the_sent_text_is_shown_once_before_the_text_after_it(self):
        r = _render(FX["messages"])
        self.assertEqual(r.text, f"{SENT}\n\nDelivered.")
        texts = [i["content"][0]["text"] for i in r.output if i["type"] == "message"]
        self.assertEqual(texts, [f"{SENT}\n\nDelivered."])

    def test_the_send_stays_a_card_ahead_of_the_text(self):
        r = _render(FX["messages"])
        self.assertEqual([i["type"] for i in r.output], ["function_call", "function_call_output", "message"])
        self.assertEqual(r.output[0]["name"], "message")

    def test_a_run_that_says_nothing_after_the_send_shows_the_sent_text_once(self):
        """The model's own text is dropped (it ended silent): the record of the
        send is the only message after the mirror."""
        silent = [m for m in FX["messages"] if not _says(m, "Delivered.")]
        self.assertEqual(len(silent), len(FX["messages"]) - 1)
        self.assertEqual(_render(silent).text, SENT)

    def test_the_same_text_twice_without_a_send_is_kept(self):
        """Only a text the `message` tool delivered is a repeat: an agent that
        really says the same thing twice says it twice."""
        msgs = copy.deepcopy(FX["messages"])
        mirror = next(m for m in msgs if m.get("model") == "delivery-mirror")
        mirror["idempotencyKey"] = mirror["__openclaw"]["idempotencyKey"] = f"{RUN}:not-a-send:1"
        self.assertEqual(_render(msgs).text.count(SENT), 2)

    def test_two_sends_of_the_same_text_are_both_shown(self):
        mirror = next(m for m in FX["messages"] if ":message-tool:" in (m.get("idempotencyKey") or ""))
        again = copy.deepcopy(mirror)
        again["idempotencyKey"] = again["__openclaw"]["idempotencyKey"] = f"{RUN}:message-tool:other:call"
        msgs = copy.deepcopy(FX["messages"])
        msgs.insert(msgs.index(next(m for m in msgs if m.get("model") == "delivery-mirror")) + 1, again)
        self.assertEqual(_render(msgs).text.count(SENT), 2)


if __name__ == "__main__":
    unittest.main()
