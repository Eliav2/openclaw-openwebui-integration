"""ELI-512 polish after the live check of #415.

1. A reply stopped before any text said "Stopped" twice: its saved "**Stopped.**" text and the
   grey "Stopped" status line. The text says it now, so the status line goes.
2. Open WebUI heads a group of cards with their labels when any card has one, and with "Explored"
   and the tools' names when none does. An English group with one titled card read
   "ToolSearch, exec: QA sleep test". A group now keeps its labels only when every card says what
   it did in words (Hebrew), else none has one ("Explored ToolSearch, exec")."""
import json
import unittest

import test_pipe_unit  # noqa: F401  (installs the module stubs openclaw_pipe needs)
from test_eli512_p2_unit import _WriterCase

import openclaw_pipe as ocp

TITLED = {"command": "sleep 5", "title": "QA sleep test"}


def _start(name, tcid, args=None):
    return {"stream": "tool", "data": {"phase": "start", "name": name, "toolCallId": tcid, "args": args or {}}}


def _result(name, tcid, args=None):
    return {"stream": "tool", "data": {"phase": "result", "name": name, "toolCallId": tcid,
                                       "args": args or {}, "result": "ok"}}


def _text(t):
    return {"stream": "assistant", "data": {"delta": t}}


def _render(lang, payloads):
    r = ocp._TurnRenderer(lang)
    events = []
    for p in payloads:
        events += [e for e in r.feed(p) if e]
    r.flush()
    return r, events


def _cards(r):
    return {i["id"]: i.get("display_labels") for i in r.output if i.get("type") == "function_call"}


def _live_cards(events):
    """What a tab's reducer ends with: the last announce of each card wins."""
    out = {}
    for e in events:
        item = e.get("item") if isinstance(e, dict) else None
        if e.get("type") == "response.output_item.added" if isinstance(e, dict) else False:
            if (item or {}).get("type") == "function_call":
                out[item["id"]] = item.get("display_labels")
    return out


class GroupLabelTests(unittest.TestCase):
    def test_an_english_group_with_a_titled_card_reads_explored(self):
        r, events = _render("en", [_start("ToolSearch", "t1"), _result("ToolSearch", "t1"),
                                   _start("exec", "t2", TITLED), _result("exec", "t2", TITLED)])
        self.assertEqual(_cards(r), {"t1": None, "t2": None})
        self.assertEqual(_live_cards(events), {"t1": None, "t2": None})

    def test_a_titled_card_first_loses_its_label_when_the_group_forms(self):
        r, events = _render("en", [_start("exec", "t1", TITLED), _result("exec", "t1", TITLED),
                                   _start("ToolSearch", "t2"), _result("ToolSearch", "t2")])
        self.assertEqual(_cards(r), {"t1": None, "t2": None})
        # The open tab drops it too: the card is announced again without it.
        self.assertEqual(_live_cards(events), {"t1": None, "t2": None})

    def test_a_titled_card_on_its_own_keeps_its_label(self):
        r, _ = _render("en", [_text("Checking."), _start("exec", "t1", TITLED), _result("exec", "t1", TITLED),
                              _text("Now the tools."), _start("ToolSearch", "t2"), _result("ToolSearch", "t2")])
        self.assertEqual(_cards(r)["t1"]["completed"], "exec: QA sleep test")

    def test_a_hebrew_group_keeps_its_words(self):
        r, events = _render("he", [_start("ToolSearch", "t1"), _result("ToolSearch", "t1"),
                                   _start("exec", "t2", TITLED), _result("exec", "t2", TITLED)])
        self.assertEqual({k: v["completed"] for k, v in _cards(r).items()}, {"t1": "מצא כלי", "t2": "הריץ פקודה"})
        self.assertEqual({k: v["completed"] for k, v in _live_cards(events).items()},
                         {"t1": "מצא כלי", "t2": "הריץ פקודה"})

    def test_a_spawn_card_does_not_turn_a_hebrew_group_back_to_names(self):
        spawn = {"label": "vendors", "task": "compare the vendors"}
        r, _ = _render("he", [_start("exec", "t1", TITLED), _result("exec", "t1", TITLED),
                              _start("sessions_spawn", "t2", spawn)])
        self.assertEqual(_cards(r)["t1"]["completed"], "הריץ פקודה")

    def test_a_rebuilt_reply_reads_the_same(self):
        """The writer's rebuild from the transcript gives the same cards as the live reply."""
        msgs = [{"role": "assistant", "content": [
            {"type": "toolcall", "id": "t1", "name": "ToolSearch", "arguments": {}},
            {"type": "tool_result", "tool_use_id": "t1", "name": "ToolSearch", "content": [{"type": "text", "text": "ok"}]},
            {"type": "toolcall", "id": "t2", "name": "exec", "arguments": TITLED},
            {"type": "tool_result", "tool_use_id": "t2", "name": "exec", "content": [{"type": "text", "text": "ok"}]}]}]
        self.assertEqual(_cards(ocp._history_renderer(msgs, "en")), {"t1": None, "t2": None})


class StoppedOnceTests(_WriterCase):
    async def test_the_writers_stopped_reply_has_no_stopped_status_line(self):
        ocp.ui_lang_remember(self.USER, {"variables": {"{{USER_LANGUAGE}}": "he-IL"}})
        self.owui.msgs()["m1"] = {"id": "m1", "role": "assistant", "parentId": "q1", "content": "", "done": False,
                                  "statusHistory": [{"description": "חושב…", "done": False},
                                                    {"description": "נעצר", "done": True}]}
        w = ocp._TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                            run_id="agent-run-1", message_id="m1", takeover=True)
        await w.finalize(stopped=True)
        self.assertEqual(self.m()["content"], "**נעצר.**")
        shown = [h["description"] for h in self.m()["statusHistory"]]
        self.assertEqual(shown[-1], "", shown)
        self.assertNotIn("נעצר", shown)

    async def test_a_stopped_reply_with_text_keeps_its_stopped_line(self):
        w = self.writer()
        await w.feed(_text("Reading them."))
        await w.finalize(stopped=True)
        self.assertEqual(self.m()["content"], "Reading them.")
        self.assertEqual(self.m()["statusHistory"][-1]["description"], "Stopped")

    async def test_the_live_reply_drops_its_stopped_line_with_the_note(self):
        self.owui.msgs()["m2"] = {"role": "assistant", "content": "", "done": True,
                                  "statusHistory": [{"description": "Thinking...", "done": False},
                                                    {"description": "Stopped", "done": True}]}
        ref = {"chat_id": self.CHAT, "message_id": "m2", "user_id": self.USER}
        await ocp._close_unsent_reply(self.conn, ref, ocp.ui_text("stopped_reason"), only_if_empty=True)
        saved = self.owui.msgs()["m2"]
        self.assertEqual(saved["content"], "**Stopped.**")
        self.assertEqual([h["description"] for h in saved["statusHistory"]], ["Thinking...", ""])
        # An open tab drops the line too.
        self.assertEqual(self.owui.events("status")[-1]["data"], {"description": "", "done": True})

    async def test_a_live_reply_with_content_keeps_its_status(self):
        self.owui.msgs()["m2"] = {"role": "assistant", "content": "partial", "done": True,
                                  "statusHistory": [{"description": "Stopped", "done": True}]}
        ref = {"chat_id": self.CHAT, "message_id": "m2", "user_id": self.USER}
        await ocp._close_unsent_reply(self.conn, ref, ocp.ui_text("stopped_reason"), only_if_empty=True)
        self.assertEqual(self.owui.msgs()["m2"]["statusHistory"], [{"description": "Stopped", "done": True}])
        self.assertEqual(self.owui.events("status"), [])


if __name__ == "__main__":
    unittest.main()
