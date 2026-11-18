"""ELI-512: a sub-agent's answer after `sessions_yield` on a claude-cli chat.

Claude Code records a `<synthetic>` "No response requested." for the turn the
yield ended. OpenClaw 2026.11.5 drops it from the live stream
(`isClaudeSyntheticNoResponse`) but imports it into `chat.history`, where it
sorts inside the announce run that answers. Read as that run's text, it hid
the run's real closer, and the reply saved "No response requested.".
Fixture: the 2026.11.5 `chat.history` of a live chat (ids and prompt replaced)."""
import copy
import json
import os
import unittest

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
FX = json.load(open(os.path.join(FIXTURES, "test_fixture_eli512_yield_announce_history.json")))
CHAT = "00000000-0000-4000-8000-0000000000cc"
ANNOUNCE = next((m.get("__openclaw") or {})["runId"] for m in FX["messages"]
                if str((m.get("__openclaw") or {}).get("runId") or "").startswith("announce:"))
ANSWER = 'The sub-agent "qa-w5 capital" finished with status `ok` and said: **Tokyo**'
SYNTHETIC = "No response requested."


def _history():
    return {"messages": ocp._transcript_roles(copy.deepcopy(FX["messages"]))}


class SyntheticNoResponseTests(unittest.TestCase):
    def test_the_fixture_holds_the_synthetic_line_before_the_announce_closer(self):
        models = [m.get("model") for m in FX["messages"] if m.get("role") == "assistant"]
        self.assertEqual(models[-2:], ["<synthetic>", "claude-opus-5-5"])

    def test_the_announce_run_rebuilt_from_the_transcript_says_the_answer(self):
        r = ocp._history_renderer(ocp._history_span_messages(_history(), ANNOUNCE))
        self.assertTrue(r.text.startswith(ANSWER), r.text)
        self.assertNotIn(SYNTHETIC, r.text)
        texts = [i["content"][0]["text"] for i in r.output if i["type"] == "message"]
        self.assertEqual(len(texts), 1, texts)
        self.assertNotIn(SYNTHETIC, texts[0])

    def test_the_announce_run_holds_none_of_the_yielded_runs_calls(self):
        """2026.11.5 sends the announce run no lifecycle start, so no start time cuts its span:
        the placeholder does. Its calls were shown by the reply the yield ended."""
        r = ocp._history_renderer(ocp._history_span_messages(_history(), ANNOUNCE))
        self.assertEqual([i for i in r.output if i["type"] == "function_call"], [])

    def test_the_run_text_read_by_its_key_is_the_answer(self):
        found, text = ocp._history_text_for_run(_history(), ANNOUNCE)
        self.assertTrue(found)
        self.assertTrue(text.startswith(ANSWER), text)

    def test_a_synthetic_line_alone_is_no_text(self):
        synthetic = next(m for m in FX["messages"] if m.get("model") == "<synthetic>")
        self.assertEqual(ocp._history_item(synthetic)["text"], "")
        self.assertEqual(ocp._history_renderer([copy.deepcopy(synthetic)]).text, "")

    def test_recovery_of_the_yielded_turn_never_reads_the_synthetic_line(self):
        # Read after the placeholder was imported, before the announce closer.
        history = _history()
        history["messages"] = history["messages"][:-1]
        prompt = FX["messages"][0]["content"]
        self.assertIsNone(ocp._history_recovery_text(history, prompt))

    def test_the_same_text_from_the_model_is_still_shown(self):
        said = {"role": "assistant", "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": SYNTHETIC}]}
        self.assertEqual(ocp._history_renderer([said]).text, SYNTHETIC)
        self.assertEqual(ocp._history_item(said)["text"], SYNTHETIC)

    def test_a_synthetic_message_with_other_text_is_shown(self):
        other = {"role": "assistant", "model": "<synthetic>",
                 "content": [{"type": "text", "text": "API Error: overloaded"}]}
        self.assertEqual(ocp._history_renderer([other]).text, "API Error: overloaded")


if __name__ == "__main__":
    unittest.main()
