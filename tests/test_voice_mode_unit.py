"""Voice mode (ELI-335): Open WebUI marks a message typed by speaking into a voice
call with `features.voice`. The reply will be read aloud, so the agent gets a
short note before that message: short spoken sentences in the user's language,
no markup. A normal message carries nothing.

Run: python3 -m pytest -q tests/test_voice_mode_unit.py
"""
import unittest

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

from test_multi_model_unit import HAIKU, MultiModelBase  # noqa: E402

VOICE = {"features": {"voice": True}}


def _voice_note(*a, **k):
    return ocp._voice_note(*a, **k)


class VoiceNoteTests(unittest.TestCase):

    def test_a_voice_call_gets_a_note(self):
        note = _voice_note({"features": {"voice": True}})
        self.assertTrue(note.startswith("[Open WebUI voice call:"))
        self.assertTrue(note.endswith("\n\n"))

    def test_the_note_asks_for_speech_not_markup(self):
        note = _voice_note({"features": {"voice": True}})
        for word in ("aloud", "language", "short", "markdown", "table", "code", "link", "numbers"):
            self.assertIn(word, note)

    def test_other_messages_get_nothing(self):
        for meta in (None, {}, {"features": None}, {"features": {}}, {"features": {"voice": False}},
                     {"features": {"web_search": True}}, {"features": "voice"}):
            self.assertEqual(_voice_note(meta), "", meta)


class VoiceTurnTests(MultiModelBase):

    async def test_a_voice_message_carries_the_note_before_the_words(self):
        self.message("u1", "user", None, "מה השעה")
        send = await self.call(HAIKU, [HAIKU], "u1", "a1", "מה השעה", extra_meta=VOICE)
        self.assertTrue(send["message"].startswith("[Open WebUI voice call:"))
        self.assertTrue(send["message"].endswith("\n\nמה השעה"))

    async def test_a_typed_message_is_sent_as_it_is(self):
        self.message("u1", "user", None, "what time is it")
        send = await self.call(HAIKU, [HAIKU], "u1", "a1", "what time is it")
        self.assertEqual(send["message"], "what time is it")

    async def test_every_voice_turn_carries_it_and_a_typed_turn_after_does_not(self):
        self.message("u1", "user", None, "one")
        s1 = await self.call(HAIKU, [HAIKU], "u1", "a1", "one", extra_meta=VOICE)
        self.message("a1", "assistant", "u1", "ok from a1")
        self.message("u2", "user", "a1", "two")
        s2 = await self.call(HAIKU, [HAIKU], "u2", "a2", "two", parent="a1", extra_meta=VOICE)
        self.message("a2", "assistant", "u2", "ok from a2")
        self.message("u3", "user", "a2", "three")
        s3 = await self.call(HAIKU, [HAIKU], "u3", "a3", "three", parent="a2")
        self.assertIn("voice call", s1["message"])
        self.assertIn("voice call", s2["message"])
        self.assertEqual(s3["message"], "three")

    async def test_a_voice_regenerate_keeps_the_history_note_first(self):
        self.message("u1", "user", None, "pick one")
        await self.call(HAIKU, [HAIKU], "u1", "a1", "pick one", extra_meta=VOICE)
        self.message("a1", "assistant", "u1", "")
        self.message("a1b", "assistant", "u1", "")
        send = await self.call(HAIKU, [HAIKU], "u1", "a1b", "pick one", extra_meta=VOICE)
        self.assertTrue(send["message"].startswith("[Open WebUI: the user asked for a new answer"))
        self.assertIn("[Open WebUI voice call:", send["message"])
        self.assertLess(send["message"].index("new answer"), send["message"].index("voice call"))
        self.assertTrue(send["message"].endswith("pick one"))


if __name__ == "__main__":
    unittest.main()
