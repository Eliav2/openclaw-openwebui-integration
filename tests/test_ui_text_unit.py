"""ELI-512: every line the Pipe itself shows comes from one table (texts.py), in the user's Open WebUI language.

Open WebUI sends its UI locale with each chat request as the `{{USER_LANGUAGE}}` variable (`he-IL`, `en-US`).
English stays word for word what it was; Hebrew is plain (no developer words) and a status line fits one
line on a 390 px phone."""
import ast
import asyncio
import pathlib
import re
import unittest
from unittest import mock

import test_pipe_unit  # noqa: F401  (installs the module stubs openclaw_pipe needs)

import openclaw_pipe as ocp

PKG = pathlib.Path(__file__).resolve().parent.parent / "src" / "openclaw_pipe_pkg"
HE = {"variables": {"{{USER_LANGUAGE}}": "he-IL"}}
EN = {"variables": {"{{USER_LANGUAGE}}": "en-US"}}

# The status lines: OWUI clamps each to one line.
STATUS_KEYS = ("thinking", "queued_behind", "queued_wait", "continuing", "steer_pending", "sending_message",
               "sending_answer", "answer_sent", "stopped", "gateway_wait", "background_agents_one",
               "background_agents")
# Words Ortal does not use: the Hebrew must say what happens, not how it works.
JARGON = re.compile(r"gateway|agent|run\b|session|token|context|גייטווי|סוכן|ריצה|טוקן|קונטקסט|סשן", re.I)
HEBREW_LETTER = re.compile(r"[֐-׿]")

# English as it was before the table: Eliav sees no change.
ENGLISH_BEFORE = {
    "thinking": "Thinking...",
    "queued_behind": "Waiting -- your message is queued behind an active response...",
    "queued_wait": "Queued: waiting for the current run to finish…",
    "continuing": "Continuing the current task; your message is next…",
    "steer_pending": "Sent while the agent works: it reads it at its next step…",
    "sending_message": "Sending your message…",
    "stopped": "Stopped",
    "stopped_reason": "**Stopped.**",
    "no_reply_text": "_(This turn produced no reply text.)_",
    "sent_with_next": "_(This message was sent together with your next one: the answer is in the next reply.)_",
    "not_sent": "_(Stopped: this message was not sent to the agent.)_",
    "gateway_wait": "Waiting for the gateway…",
    "compacting": "🧹 _Compacting context…_",
    "subagent_card": "Sub-agent",
}


class TableTests(unittest.TestCase):
    def test_every_line_has_english_and_hebrew(self):
        for key, entry in ocp.UI_TEXT.items():
            self.assertTrue(entry.get("en"), key)
            self.assertIn("he", entry, key)

    def test_english_is_unchanged(self):
        for key, text in ENGLISH_BEFORE.items():
            self.assertEqual(ocp.ui_text(key), text, key)
        self.assertEqual(ocp._background_agents_line(1), "Waiting on 1 background agent")
        self.assertEqual(ocp._background_agents_line(3), "Waiting on 3 background agents")

    def test_hebrew_is_hebrew_and_plain(self):
        for key, entry in ocp.UI_TEXT.items():
            he = entry["he"]
            if not he:  # a part with nothing to say in Hebrew (the token count)
                continue
            self.assertRegex(he, HEBREW_LETTER, key)
            self.assertIsNone(JARGON.search(he), f"{key}: {he}")

    def test_a_hebrew_status_line_fits_a_phone(self):
        for key in STATUS_KEYS:
            text = ocp.ui_text(key, "he", n=12)
            self.assertLessEqual(len(text), ocp.UI_STATUS_MAX_CHARS, f"{key}: {text}")
        self.assertEqual(ocp._background_agents_line(2, "he"), "מחכה ל-2 עוזרים שעובדים ברקע")


class LanguageTests(unittest.TestCase):
    def test_the_ui_locale_picks_the_language(self):
        self.assertEqual(ocp.ui_lang(HE), "he")
        self.assertEqual(ocp.ui_lang({"variables": {"{{USER_LANGUAGE}}": "HE"}}), "he")
        for meta in (EN, {"variables": {"{{USER_LANGUAGE}}": "fr-FR"}}, {"variables": {}}, {}, None, "x"):
            self.assertEqual(ocp.ui_lang(meta), "en", meta)

    def test_a_reply_without_a_request_uses_the_users_last_language(self):
        self.assertEqual(ocp.ui_lang_of("u-new"), "en")
        self.assertEqual(ocp.ui_lang_remember("u-he", HE), "he")
        self.assertEqual(ocp.ui_lang_of("u-he"), "he")
        ocp.ui_lang_remember("u-he", EN)
        self.assertEqual(ocp.ui_lang_of("u-he"), "en")
        self.assertEqual(ocp.ui_lang_remember(None, HE), "he")
        self.assertEqual(ocp.ui_lang_of(None), "en")

    def test_a_missing_hebrew_line_falls_back_to_english(self):
        with mock.patch.dict(ocp.UI_TEXT, {"x": {"en": "X {n}"}}):
            self.assertEqual(ocp.ui_text("x", "he", n=1), "X 1")
        self.assertEqual(ocp.ui_text("compacted_was", "he", tokens="1k"), "")


class CallSiteTests(unittest.TestCase):
    """Where the language reaches what the user sees."""

    def test_the_turn_takes_the_requests_language(self):
        turn = ocp._PipeTurn(None, {}, None, None, {"id": "u-turn"}, HE, None, None, None, "", None, None)
        self.assertEqual(turn.lang, "he")
        self.assertEqual(ocp.ui_lang_of("u-turn"), "he")

    def test_a_turn_writer_speaks_its_users_language(self):
        ocp.ui_lang_remember("u-writer", HE)
        writer = ocp._TurnWriter(mock.MagicMock(), session_key="k", chat_id="c", user_id="u-writer",
                                 run_id="r", message_id="m")
        self.assertEqual(writer.lang, "he")
        self.assertEqual(writer.renderer.lang, "he")

    def test_a_stop_in_hebrew_still_ends_as_stopped(self):
        history = [{"description": "חושב…", "done": False}, {"description": "נעצר", "done": True},
                   {"description": "", "done": True}]
        kept, new = ocp._final_status_history(history, ["🧠 1k"], stopped=True, lang="he")
        self.assertEqual(new, [{"description": "נעצר", "done": True}])
        self.assertEqual(kept[-2:], [{"description": "חושב…", "done": False}, new[0]])

    def test_a_hebrew_steer_note_is_no_answer(self):
        self.assertEqual(ocp._settle_shown(ocp.ui_text("sent_with_next", "he")), ocp._settle_shown(""))

    def test_compaction_notes_in_hebrew(self):
        r = ocp._TurnRenderer("he")
        start = r._compaction_note("run-1", {"phase": "start", "tokensBefore": 120000})
        self.assertEqual(start["text"], ocp.ui_text("compacting", "he"))
        end = r._compaction_note("run-1", {"phase": "end", "completed": True})
        self.assertIn("השיחה סוכמה", end["text"])
        self.assertNotIn("tokens", end["text"])
        r._compaction_note("run-1", {"phase": "start"})
        self.assertEqual(r._compaction_note("run-1", {"phase": "end", "outcome": "skipped"})["text"],
                         ocp.ui_text("compaction_skipped", "he"))

    def test_unsent_replies_are_closed_in_the_users_language(self):
        closed = []

        async def close(_conn, ref, note):
            closed.append((ref["message_id"], note))

        async def run():
            with mock.patch.object(ocp, "_close_unsent_reply", close):
                ocp._end_unsent_replies(mock.MagicMock(), [{"message_id": "m2"}], sent=True, lang="he")
                await asyncio.sleep(0)

        asyncio.run(run())
        self.assertEqual(closed, [("m2", ocp.ui_text("sent_with_next", "he"))])


class NoLiteralTests(unittest.TestCase):
    """A user-visible line written at its call site would skip the table: the source has none."""

    def test_status_lines_come_from_the_table(self):
        hits = []
        for path in PKG.glob("*.py"):
            if path.name in ("action.py",):  # the Status dialog: its own page, not a status line
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                if name == "_emit_status" and len(node.args) > 1:
                    arg = node.args[1]
                    if isinstance(arg, (ast.Constant, ast.JoinedStr)) and getattr(arg, "value", "x") != "":
                        hits.append(f"{path.name}:{node.lineno}")
                if name == "_tool_call_started_event" and node.args and isinstance(node.args[0], ast.Constant):
                    hits.append(f"{path.name}:{node.lineno}")
        self.assertEqual(hits, [])

    def test_no_english_note_literal_outside_the_table(self):
        notes = [ocp.ui_text(k) for k in ("no_reply_text", "stopped_reason", "sent_with_next",
                                          "steered_into_running", "gateway_lost_run")]
        for path in PKG.glob("*.py"):
            if path.name == "texts.py":
                continue
            text = path.read_text()
            for note in notes:
                self.assertNotIn(note, text, f"{path.name}: {note}")


if __name__ == "__main__":
    unittest.main()
