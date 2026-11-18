"""ELI-512 live re-check of #416 (chat 792559df).

1. A model's `<details><summary>Sources</summary>` on one line showed as plain text. Open WebUI
   v0.13.3 renders `<details>` only as `<details ...>` ending its line and `<summary>...</summary>`
   ending the next (src/lib/utils/marked/extension.ts:34-35); anything else is an HTML token, shown
   as text in a chat (Markdown/HTMLToken.svelte:139). The Pipe puts such a block in that shape.
2. A stopped group read "חקר ToolSearch, 3 web_search": Open WebUI's cancel save marks every card
   still `in_progress` `incomplete` (backend/open_webui/utils/middleware.py:6578-6584), and a group
   shows no labels for those (ConsecutiveDetailsGroup.svelte:186-196). The Pipe closes its cards
   after that save as a finished reply's are: completed, a stopped call with its note."""
import unittest

import test_pipe_unit  # noqa: F401  (installs the module stubs openclaw_pipe needs)
from test_eli512_p2_unit import _WriterCase

import openclaw_pipe as ocp

ONE_LINE = "Japan has 6,852 islands.\n\n<details><summary>Sources</summary>\n\n- mofa.go.jp\n\n</details>"
SHAPED = "Japan has 6,852 islands.\n\n<details>\n<summary>Sources</summary>\n\n- mofa.go.jp\n\n</details>"


def _shaped(*chunks):
    r = ocp._TurnRenderer("en")
    shown = []
    for c in chunks:
        shown += [e for e in r.feed({"stream": "assistant", "data": {"delta": c}}) if isinstance(e, str)]
    shown.append(r.flush())
    return r, "".join(shown)


class DetailsShapeTests(unittest.TestCase):
    def test_a_one_line_details_gets_open_webuis_lines(self):
        r, shown = _shaped(ONE_LINE)
        self.assertEqual(shown, SHAPED)
        self.assertEqual(r.text, SHAPED)

    def test_in_any_chunks(self):
        for size in (1, 3, 7, 12):
            chunks = [ONE_LINE[i:i + size] for i in range(0, len(ONE_LINE), size)]
            self.assertEqual(_shaped(*chunks)[1], SHAPED, size)

    def test_a_whole_block_on_one_line(self):
        self.assertEqual(_shaped("<details><summary>S</summary>body</details>\n")[1],
                         "<details>\n<summary>S</summary>\nbody</details>\n")

    def test_a_summary_with_text_after_it(self):
        self.assertEqual(_shaped("<details>\n<summary>S</summary> - a link\n</details>")[1],
                         "<details>\n<summary>S</summary>\n- a link\n</details>")

    def test_open_webuis_own_shape_and_other_text_stay(self):
        for text in (SHAPED, "Use `<details><summary>x</summary>` in HTML.", "<detailsx> no",
                     "a <details><summary>S</summary> mid-line", "<div>\n<summary>S</summary> x"):
            self.assertEqual(_shaped(text)[1], text)

    def test_a_code_fence_is_left_alone(self):
        text = "```html\n<details><summary>S</summary>\n</details>\n```\nafter"
        self.assertEqual(_shaped(text)[1], text)

    def test_text_before_a_details_line_is_not_held(self):
        r = ocp._TurnRenderer("en")
        out = r.feed({"stream": "assistant", "data": {"delta": "Hello there.\n<det"}})
        self.assertEqual("".join(e for e in out if isinstance(e, str)), "Hello there.\n")


class StoppedCardsTests(_WriterCase):
    def _card(self, tcid, status, label):
        return {"type": "function_call", "id": tcid, "name": "web_search", "arguments": "{}",
                "status": status, "display_labels": {"running": "מחפש ברשת", "completed": label}}

    def _saved(self, content):
        self.owui.msgs()["m2"] = {"role": "assistant", "content": content, "done": True, "output": [
            self._card("t1", "incomplete", "חיפש ברשת"),
            {"type": "function_call_output", "id": "r1", "call_id": "t1", "status": "completed",
             "output": [{"type": "output_text", "text": "ok"}]},
            self._card("t2", "incomplete", "חיפש ברשת")]}

    async def _stop(self):
        ref = {"chat_id": self.CHAT, "message_id": "m2", "user_id": self.USER, "lang": "he"}
        await ocp._close_unsent_reply(self.conn, ref, ocp.ui_text("stopped_reason", "he"), only_if_empty=True)
        return self.owui.msgs()["m2"]["output"]

    def _check(self, output):
        cards = [i for i in output if i.get("type") == "function_call"]
        self.assertEqual([c["status"] for c in cards], ["completed", "completed"])
        results = {i["call_id"]: i for i in output if i.get("type") == "function_call_output"}
        self.assertEqual(set(results), {"t1", "t2"})
        self.assertEqual(results["t2"]["output"][0]["text"], ocp.ui_text("stopped", "he"))

    async def test_a_reply_stopped_with_text_closes_its_cards(self):
        self._saved("חלק מהתשובה")
        output = await self._stop()
        self._check(output)
        self.assertEqual(self.owui.msgs()["m2"]["content"], "חלק מהתשובה")
        pushed = self.owui.events("chat:messages")[-1]["data"]["messages"]["m2"]
        self._check(pushed["output"])

    async def test_a_reply_stopped_before_text_closes_its_cards(self):
        self._saved("")
        self._check(await self._stop())
        self.assertEqual(self.owui.msgs()["m2"]["content"], "**נעצר.**")

    def test_stop_hands_every_reply_to_the_close(self):
        """Not only one stopped before any text: its cards need closing too."""
        from unittest import mock
        turn = ocp._PipeTurn.__new__(ocp._PipeTurn)
        turn.stopped_shown, turn.status_closed = False, False
        turn.renderer = ocp._TurnRenderer("he")
        turn.visible_message_text = "half an answer"
        turn.owui_origin_chat_id, turn.owui_message_id, turn.channel_id = self.CHAT, "m9", None
        turn.__user__, turn.__metadata__, turn.lang, turn.conn = {"id": self.USER}, {}, "he", object()
        with mock.patch.object(ocp, "spawn_background", lambda coro: coro.close()), \
                mock.patch.object(ocp, "_close_unsent_reply", mock.AsyncMock()) as close:
            turn.on_parked_stop()
        close.assert_called_once()
        self.assertEqual(close.call_args.args[1]["lang"], "he")


class StoppedWithTextTests(_WriterCase):
    """CodeRabbit on #417: Open WebUI's cancel save writes no `content`, and the stop's own
    snapshot can fail, so a reply that showed text can read back with none."""

    def _saved(self, content, output):
        self.owui.msgs()["m3"] = {"role": "assistant", "content": content, "done": True, "output": output,
                                  "statusHistory": [{"description": "Stopped", "done": True}]}

    async def _stop(self, shown, output=None):
        ref = {"chat_id": self.CHAT, "message_id": "m3", "user_id": self.USER, "lang": "he", "shown": shown,
               "output": output or []}
        await ocp._close_unsent_reply(self.conn, ref, ocp.ui_text("stopped_reason", "he"), only_if_empty=True)
        return self.owui.msgs()["m3"]

    async def test_text_it_showed_is_kept_not_replaced_by_the_note(self):
        text = {"type": "message", "id": "msg_1", "content": [{"type": "output_text", "text": "חצי תשובה"}]}
        self._saved("", [text])
        saved = await self._stop("חצי תשובה")
        self.assertEqual(saved["content"], "חצי תשובה")
        self.assertFalse([i for i in saved["output"] if str(i.get("id", "")).startswith("msg_note_")])
        self.assertEqual(saved["statusHistory"][-1]["description"], "Stopped")

    async def test_a_card_the_save_missed_is_restored(self):
        card = {"type": "function_call", "id": "t9", "name": "web_search", "arguments": "{}", "status": "completed"}
        self._saved("חצי תשובה", [])
        saved = await self._stop("חצי תשובה", [card])
        self.assertIn("t9", [i.get("id") for i in saved["output"]])

    async def test_the_generic_no_result_note_says_stopped(self):
        card = {"type": "function_call", "id": "t9", "name": "web_search", "arguments": "{}", "status": "completed"}
        note = ocp._tool_call_result_event("t9", ocp.TOOL_NO_RESULT_NOTE)["item"]
        real = ocp._tool_call_result_event("t8", "real result")["item"]
        self._saved("", [])
        saved = await self._stop("", [card, note, real])
        texts = {i["call_id"]: i["output"][0]["text"] for i in saved["output"]
                 if i.get("type") == "function_call_output"}
        self.assertEqual(texts, {"t9": ocp.ui_text("stopped", "he"), "t8": "real result"})


class ShaperFenceTests(unittest.TestCase):
    def test_a_fence_closes_only_on_a_closing_line(self):
        text = "```\n```not-a-close\n<details><summary>S</summary>\n```\n"
        self.assertEqual(_shaped(text)[1], text)

    def test_a_fence_left_open_ends_with_its_message(self):
        """A tool card ends the message; Open WebUI starts the next one outside any fence."""
        r = ocp._TurnRenderer("en")
        shown = []
        for p in ({"stream": "assistant", "data": {"delta": "```\ncode\n"}},
                  {"stream": "tool", "data": {"phase": "start", "name": "exec", "toolCallId": "t1", "args": {}}},
                  {"stream": "assistant", "data": {"delta": "<details><summary>S</summary>\nbody\n</details>"}}):
            shown += [e for e in r.feed(p) if isinstance(e, str)]
        shown.append(r.flush())
        self.assertIn("<details>\n<summary>S</summary>\nbody", "".join(shown))


if __name__ == "__main__":
    unittest.main()
