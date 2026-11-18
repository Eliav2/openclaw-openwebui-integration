"""ELI-512 polish after the live check of #415.

1. A reply stopped before any text said "Stopped" twice: its saved "**Stopped.**" text and the
   grey "Stopped" status line. The text says it now, so the status line goes.
2. Open WebUI heads a group of cards with their labels when any card has one, and with "Explored"
   and the tools' names when none does. An English group with one titled card read
   "ToolSearch, exec: QA sleep test". A group now keeps its labels only when every card says what
   it did in words (Hebrew), else none has one ("Explored ToolSearch, exec")."""
import json
import unittest
from unittest import mock

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

    def test_a_note_back_between_cards_gives_them_their_labels_back(self):
        """A rebuild has no Pipe note: its cards group and lose their titles. The note put back
        (`_keep_pipe_notes`) parts them again, and each gets its own label back."""
        msgs = [{"role": "assistant", "content": [
            {"type": "toolcall", "id": "t1", "name": "exec", "arguments": TITLED},
            {"type": "tool_result", "tool_use_id": "t1", "name": "exec", "content": [{"type": "text", "text": "ok"}]},
            {"type": "toolcall", "id": "t2", "name": "ToolSearch", "arguments": {}},
            {"type": "tool_result", "tool_use_id": "t2", "name": "ToolSearch", "content": [{"type": "text", "text": "ok"}]}]}]
        part = ocp._history_renderer(msgs, "en")
        self.assertEqual(_cards(part), {"t1": None, "t2": None})
        note = ocp._turn_note_item("Take control: https://example.invalid/x")
        streamed = [{"type": "function_call", "id": "t1"}, note, {"type": "function_call", "id": "t2"}]
        ocp._keep_pipe_notes(streamed, part)
        self.assertEqual(_cards(part)["t1"]["completed"], "exec: QA sleep test")

    def test_a_message_with_no_text_left_parts_no_cards(self):
        """A `MEDIA:` line's message is empty once its image moves to the reply's end: Open WebUI
        skips it and groups the cards around it."""
        out = [{"type": "function_call", "id": "t1", "name": "exec", "arguments": json.dumps(TITLED),
                "display_labels": {"running": "exec: QA sleep test", "completed": "exec: QA sleep test"}},
               {"type": "message", "id": "m", "content": [{"type": "output_text", "text": " "}]},
               {"type": "function_call", "id": "t2", "name": "ToolSearch", "arguments": "{}"}]
        self.assertEqual([c["id"] for c in ocp._even_card_labels(out, "en")], ["t1"])
        self.assertNotIn("display_labels", out[0])
        out[1]["content"][0]["text"] = "Now the tools."
        ocp._even_card_labels(out, "en")
        self.assertEqual(out[0]["display_labels"]["completed"], "exec: QA sleep test")


class WriterViewLabelTests(_WriterCase):
    async def test_the_writers_view_groups_cards_around_a_moved_image(self):
        w = self.writer()
        for payload in (_start("exec", "t1", TITLED), _result("exec", "t1", TITLED),
                        _text("MEDIA:/tmp/openclaw/chart.png\n"), _start("ToolSearch", "t2"),
                        _result("ToolSearch", "t2")):
            await w.feed(payload)
        w._rewrite = lambda text: text.replace("MEDIA:/tmp/openclaw/chart.png", "")
        with mock.patch.object(w, "_resolve_media", mock.AsyncMock()):
            output, _ = await w._view(final=True)
        self.assertEqual({i["id"]: i.get("display_labels") for i in output if i.get("type") == "function_call"},
                         {"t1": None, "t2": None})


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
        # An open tab drops the line too: its history is replaced, not added to (a status event
        # would leave "Stopped" in its expanded history).
        self.assertEqual(self.owui.events("status"), [])
        pushed = self.owui.events("chat:messages")[-1]["data"]["messages"]["m2"]
        self.assertEqual([h["description"] for h in pushed["statusHistory"]], ["Thinking...", ""])
        self.assertEqual(pushed["content"], "**Stopped.**")

    async def test_a_failed_status_push_still_completes_the_tab(self):
        self.owui.msgs()["m2"] = {"role": "assistant", "content": "", "done": True,
                                  "statusHistory": [{"description": "Stopped", "done": True}]}
        emits = self.owui.emits

        class Refusing(list):
            def append(self, entry):
                if entry[1].get("data", {}).get("type") in ("status", "chat:messages"):
                    raise ConnectionError("socket gone")
                super().append(entry)
        self.owui.emits = Refusing(emits)
        ref = {"chat_id": self.CHAT, "message_id": "m2", "user_id": self.USER}
        await ocp._close_unsent_reply(self.conn, ref, ocp.ui_text("stopped_reason"), only_if_empty=True)
        self.assertEqual(self.owui.msgs()["m2"]["content"], "**Stopped.**")
        self.assertTrue([d for e, d, _ in self.owui.emits if d["data"].get("type") == "chat:completion"])

    async def test_a_reply_quoting_stopped_keeps_its_stopped_line(self):
        """Only the Pipe's own note drops the line: a reply that ends quoting it does not."""
        w = self.writer()
        await w.feed(_text("The log ends with **Stopped.**"))
        await w.finalize(stopped=True)
        self.assertEqual(self.m()["statusHistory"][-1]["description"], "Stopped")

    async def test_a_live_reply_with_content_keeps_its_status(self):
        self.owui.msgs()["m2"] = {"role": "assistant", "content": "partial", "done": True,
                                  "statusHistory": [{"description": "Stopped", "done": True}]}
        ref = {"chat_id": self.CHAT, "message_id": "m2", "user_id": self.USER}
        await ocp._close_unsent_reply(self.conn, ref, ocp.ui_text("stopped_reason"), only_if_empty=True)
        self.assertEqual(self.owui.msgs()["m2"]["statusHistory"], [{"description": "Stopped", "done": True}])
        self.assertEqual(self.owui.events("status"), [])


class RequestStoppedLineTests(unittest.IsolatedAsyncioTestCase):
    """The live request saves its own closing lines. One that ends "Stopped" can land after the
    note's write and bring the line back (the English After shot): it ends empty instead."""

    def _turn(self, visible=""):
        turn = ocp._PipeTurn.__new__(ocp._PipeTurn)
        turn.renderer = ocp._TurnRenderer("en")
        turn.visible_message_text = visible
        turn.owui_origin_chat_id, turn.owui_message_id, turn.channel_id = "c1", "m9", None
        turn.__user__, turn.__metadata__, turn.lang = {"id": "u1"}, {}, "en"
        return turn

    async def _shown(self, turn):
        lines = []

        async def emit(event):
            lines.append(event["data"]["description"])
        await turn.show_stopped(emit)
        return lines

    async def test_a_reply_stopped_before_text_closes_empty(self):
        self.assertEqual(await self._shown(self._turn()), [""])

    async def test_a_stop_after_the_closing_lines_adds_no_stopped(self):
        """The request's lines already went out: the stop's own line is the empty one, so its late
        save cannot land after the note's and bring "Stopped" back."""
        turn = self._turn()
        turn.stopped_shown, turn.status_closed, turn.conn = False, True, object()
        lines, spawned = [], []

        async def emit(event):
            lines.append(event["data"]["description"])
        turn.__event_emitter__ = emit
        with mock.patch.object(ocp, "spawn_background", spawned.append), \
                mock.patch.object(ocp, "_close_unsent_reply", mock.AsyncMock()):
            turn.on_parked_stop()
            for coro in spawned:
                await coro
        self.assertEqual(lines, [""])

    async def test_a_reply_stopped_after_text_ends_on_stopped(self):
        self.assertEqual(await self._shown(self._turn("half an answer")), ["Stopped"])


if __name__ == "__main__":
    unittest.main()
