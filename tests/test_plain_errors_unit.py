"""One renderer turns a Gateway error into what the user sees (S13)."""
import unittest

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)
from _test_stubs import install_stubs  # noqa: E402

install_stubs()

from openclaw_pipe import (  # noqa: E402
    ERROR_DETAILS, ERROR_RUN, ERROR_UNFINISHED, LANG_EN, LANG_HE, _ERROR_TEXT,
    _fenced, _message_language, _plain_error,
)


class MessageLanguageTests(unittest.TestCase):
    def test_the_script_of_the_message_picks_the_language(self):
        for text, lang in (
            ("מתי יוצאת הרכבת הבאה לקיוטו", LANG_HE),
            ("when is the next train to Kyoto", LANG_EN),
            ("מה זה Shinkansen", LANG_HE),             # one foreign word in a Hebrew sentence
            ("is קיוטו worth two days", LANG_EN),      # one Hebrew word in an English one
            ("תבדוק https://example.com/some/long/english/path", LANG_HE),  # links do not count
            ("", LANG_EN),
            ("12345 !!!", LANG_EN),
            (None, LANG_EN),
        ):
            with self.subTest(text=text):
                self.assertEqual(_message_language(text), lang)

    def test_every_language_has_every_sentence(self):
        self.assertEqual(set(_ERROR_TEXT[LANG_HE]), set(_ERROR_TEXT[LANG_EN]))


class PlainErrorTests(unittest.TestCase):
    def test_the_raw_text_is_kept_in_a_collapsed_block_after_the_sentence(self):
        out = _plain_error(ERROR_RUN, "  Error: boom  ", "hello")
        sentence, rest = out.split("\n\n<details>\n", 1)
        self.assertEqual(sentence, _ERROR_TEXT[LANG_EN][ERROR_RUN])
        self.assertEqual(rest, f"<summary>{_ERROR_TEXT[LANG_EN][ERROR_DETAILS]}</summary>\n\n"
                               "```\nError: boom\n```\n\n</details>")

    def test_without_raw_text_there_is_only_the_sentence(self):
        for raw in (None, "", "   "):
            self.assertEqual(_plain_error(ERROR_UNFINISHED, raw, "hi"),
                             _ERROR_TEXT[LANG_EN][ERROR_UNFINISHED])

    def test_backticks_in_the_raw_text_cannot_close_the_block(self):
        raw = "bad ``` fence and ```` more"
        self.assertEqual(_fenced(raw), f"`````\n{raw}\n`````")
        self.assertIn(_fenced(raw), _plain_error(ERROR_RUN, raw, "hi"))


if __name__ == "__main__":
    unittest.main()
