"""ELI-512 P2s: what a non-owner, a phone and another chat see of the Pipe's own lines.

1. The context and rate-limit line under a reply is for owner users only (`_sender_is_owner`, the
   OWNER_USERS check that picks the restricted connection); a non-owner keeps the goal line.
2. Tool cards carry Open WebUI's `display_labels`: plain localized words in Hebrew, the tool's name
   plus the call's title in English; the item's `name` stays the tool's own.
3. A reply that is only the Pipe's note ends with an `internal` event: Open WebUI shows no toast
   or notification for it in another chat, and no push is published.
4. A reply stopped before any text saves "Stopped." as its content."""
import asyncio
import copy
import json
import types
import unittest
from unittest import mock

import test_pipe_unit  # noqa: F401  (installs the module stubs openclaw_pipe needs)
from test_pipe_unit import _TurnWriterCase, _delta, _tool, _turn_history

import openclaw_pipe as ocp

OWNER = "11111111-1111-1111-1111-111111111111"
GUEST = "22222222-2222-2222-2222-222222222222"
CHAT = "33333333-3333-3333-3333-333333333333"
GOAL = {"objective": "ship it", "status": "active"}


class _Conn:
    def __init__(self, owners: str, goal=None):
        self._valves = lambda: types.SimpleNamespace(OWNER_USERS=owners)
        self.goal = goal

    async def send_request(self, method, params, timeout=10):
        if method == "sessions.describe":
            return {"session": {"modelProvider": "anthropic", "contextTokens": 500_000,
                                "totalTokens": 27_000, **({"goal": self.goal} if self.goal else {})}}
        if method == "usage.status":
            return {"providers": [{"provider": "anthropic", "windows": [{"label": "5h", "usedPercent": 18}]}]}
        raise AssertionError(method)


def _key(user):
    return f"agent:main:openwebui-{user}-{CHAT}"


class ContextMeterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        p = mock.patch.object(ocp, "_user_email", mock.AsyncMock(return_value=None))
        p.start()
        self.addCleanup(p.stop)

    async def test_an_owner_sees_the_context_line(self):
        lines = await ocp._build_usage_status_lines(_Conn(OWNER), _key(OWNER))
        self.assertTrue(any(line.startswith("🧠 27k/500k") for line in lines), lines)
        # The rate-limit part too: the fake's 5h window, 18% used.
        self.assertTrue(any("⏱ 5h 82% left" in line for line in lines), lines)

    async def test_a_non_owner_sees_no_context_or_rate_limit_line(self):
        lines = await ocp._build_usage_status_lines(_Conn(OWNER), _key(GUEST))
        self.assertEqual(lines, [])

    async def test_a_non_owner_keeps_the_goal_line(self):
        lines = await ocp._build_usage_status_lines(_Conn(OWNER, goal=GOAL), _key(GUEST))
        goal_line = ocp._format_goal_line(GOAL)
        self.assertIn("ship it", goal_line)
        self.assertEqual(lines, [goal_line])

    async def test_with_no_owner_users_everyone_is_the_owner(self):
        lines = await ocp._build_usage_status_lines(_Conn(""), _key(GUEST))
        self.assertTrue(any("🧠" in line for line in lines), lines)


EXEC_TITLED = json.dumps({"command": "df -h", "title": "Check disk space"})
EXEC_BARE = json.dumps({"command": "df -h"})


class ToolCardLabelTests(unittest.TestCase):
    def test_hebrew_says_what_the_tool_does(self):
        self.assertEqual(ocp._tool_card_labels("exec", EXEC_TITLED, "he"),
                         {"running": "מריץ פקודה", "completed": "הריץ פקודה"})
        self.assertEqual(ocp._tool_card_labels("Agent", "{}", "he")["completed"], "הפעיל עוזר")
        self.assertEqual(ocp._tool_card_labels("some_plugin_tool", "{}", "he")["completed"], "השתמש בכלי")

    def test_english_keeps_the_name_and_adds_the_calls_title(self):
        self.assertEqual(ocp._tool_card_labels("exec", EXEC_TITLED, "en"),
                         {"running": "exec: Check disk space", "completed": "exec: Check disk space"})
        self.assertEqual(ocp._tool_card_labels("Agent", json.dumps({"description": "Vendor research"}), "en"),
                         {"running": "Agent: Vendor research", "completed": "Agent: Vendor research"})

    def test_an_untitled_english_card_stays_as_it_was(self):
        self.assertIsNone(ocp._tool_card_labels("exec", EXEC_BARE, "en"))

    def test_a_spawn_card_keeps_its_own_title(self):
        self.assertIsNone(ocp._tool_card_labels("🤖 vendors", "{}", "he"))

    def test_a_long_title_is_cut(self):
        labels = ocp._tool_card_labels("exec", json.dumps({"title": "x " * 100}), "en")
        self.assertLessEqual(len(labels["completed"]), len("exec: ") + ocp.TOOL_LABEL_TITLE_MAX)

    def _card(self, lang, args, failed=False):
        r = ocp._TurnRenderer(lang)
        start = {"stream": "tool", "data": {"phase": "start", "name": "exec", "toolCallId": "t1", "args": args}}
        events = [e for e in r.feed(start) if e]
        result = {"stream": "tool", "data": {"phase": "result", "name": "exec", "toolCallId": "t1",
                                             "args": args, "result": "ok", "isError": failed}}
        events += [e for e in r.feed(result) if e]
        cards = [e["item"] for e in events if isinstance(e, dict) and (e.get("item") or {}).get("type") == "function_call"]
        saved = [i for i in r.output if i.get("type") == "function_call"]
        return cards, saved

    def test_the_card_carries_the_labels_and_the_tools_name(self):
        cards, saved = self._card("he", json.loads(EXEC_TITLED))
        self.assertEqual(cards[0]["name"], "exec")
        self.assertEqual(cards[0]["display_labels"]["completed"], "הריץ פקודה")
        self.assertEqual(saved[0]["display_labels"]["running"], "מריץ פקודה")
        self.assertIn("df -h", cards[0]["arguments"])

    def test_a_failed_card_keeps_its_labels(self):
        cards, _ = self._card("he", json.loads(EXEC_TITLED), failed=True)
        relabel = [c for c in cards if c.get("status") == "failed"]
        self.assertTrue(relabel)
        self.assertEqual(relabel[-1]["display_labels"]["completed"], "הריץ פקודה")
        self.assertTrue(relabel[-1]["name"].startswith("exec"))

    def test_an_english_untitled_card_has_no_labels(self):
        cards, _ = self._card("en", json.loads(EXEC_BARE))
        self.assertNotIn("display_labels", cards[0])


class NoteOnlyTests(unittest.TestCase):
    def test_a_reply_of_only_images_is_a_real_reply(self):
        media = ocp._turn_note_item("![a](/api/v1/files/1/content)", media=True)
        self.assertFalse(ocp._is_note_only([media]))
        self.assertFalse(ocp._is_note_only([media, ocp._turn_note_item("x")]))
        # Kept over a transcript rebuild like a note.
        self.assertTrue(media["id"].startswith(ocp._NOTE_ITEM_PREFIX))

    def test_note_only(self):
        note = ocp._turn_note_item("x")
        self.assertTrue(ocp._is_note_only([note]))
        self.assertFalse(ocp._is_note_only([]))
        self.assertFalse(ocp._is_note_only([note, {"type": "function_call", "id": "t1"}]))
        self.assertFalse(ocp._is_note_only([{"type": "message", "id": "msg_1"}, note]))


class _WriterCase(_TurnWriterCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.history = {"messages": []}
        self.published = []
        test = self

        async def send_request(method, params, timeout=10):
            if method == "chat.history":
                return copy.deepcopy(test.history)
            if method == "chat.abort":
                return {"aborted": True}
            if method == "sessions.describe":
                return {"session": {"modelProvider": "anthropic", "contextTokens": 500_000, "totalTokens": 27_000}}
            if method == "usage.status":
                return {"providers": []}
            raise AssertionError(method)
        self.conn.send_request = send_request

        async def publish(chat_id, message_id, user_id, text=None):
            test.published.append(message_id)
        p = mock.patch.object(ocp, "_publish_chat_finished", publish)
        p.start()
        self.addCleanup(p.stop)
        self.conn._valves = lambda: types.SimpleNamespace(AGENT_ID="main", PUBLISH_CHAT_FINISHED=True)

    def completions(self):
        return [d for e, d, _ in self.owui.emits
                if e == "events" and not d.get("shared") and d["data"].get("type") == "chat:completion"
                and d["data"]["data"].get("done")]

    def m(self):
        return self.owui.msgs()["m1"]


class NoteOnlyNotificationTests(_WriterCase):
    async def test_a_reply_that_is_only_a_note_ends_internal_and_unpublished(self):
        w = self.writer()
        await w.finalize(note=ocp.ui_text("sent_with_next"))
        self.assertEqual(self.m()["content"], ocp.ui_text("sent_with_next"))
        done = self.completions()
        self.assertEqual(len(done), 1)
        self.assertIs(done[0].get("internal"), True)
        self.assertEqual(self.published, [])

    async def test_a_reply_with_text_still_notifies(self):
        w = self.writer()
        await w.feed(_delta("Found it."))
        await w.finalize()
        done = self.completions()
        self.assertEqual(len(done), 1)
        self.assertNotIn("internal", done[0])
        self.assertEqual(self.published, ["m1"])

    async def test_an_unsent_reply_closed_with_its_note_ends_internal(self):
        self.owui.msgs()["m2"] = {"role": "assistant", "content": "", "done": True}
        await ocp._close_unsent_reply(self.conn, {"chat_id": self.CHAT, "message_id": "m2", "user_id": self.USER},
                                      ocp.ui_text("not_sent"))
        done = self.completions()
        self.assertEqual(done[-1].get("internal"), True)
        self.assertEqual(self.owui.msgs()["m2"]["content"], ocp.ui_text("not_sent"))


class NoteOnlyDeliveryTests(_WriterCase):
    def shared(self):
        return [(d, room) for e, d, room in self.owui.emits if e == "events" and d.get("shared")
                and d["data"].get("type") == "chat:completion"]

    async def test_the_chats_other_viewers_still_get_a_note_only_reply(self):
        """`internal` skips Open WebUI's shared copy for the chat's room: the Pipe sends it."""
        self.owui.msgs()["m2"] = {"role": "assistant", "content": "", "done": True}
        await ocp._close_unsent_reply(self.conn, {"chat_id": self.CHAT, "message_id": "m2", "user_id": self.USER},
                                      ocp.ui_text("not_sent"))
        shared = self.shared()
        self.assertEqual(len(shared), 1)
        data, room = shared[0]
        self.assertEqual(room, f"chat:{self.CHAT}")
        self.assertEqual(data["data"]["type"], "chat:completion")
        self.assertEqual(data["data"]["data"]["content"], ocp.ui_text("not_sent"))

    async def test_the_writers_note_only_end_reaches_the_room_too(self):
        await self.writer().finalize(note=ocp.ui_text("sent_with_next"))
        self.assertEqual(len(self.shared()), 1)

    async def test_a_note_that_was_not_saved_is_not_shown(self):
        self.owui.msgs()["m2"] = {"role": "assistant", "content": "", "done": True}
        with mock.patch.object(ocp, "_owui_upsert_message", mock.AsyncMock(return_value=None)):
            await ocp._close_unsent_reply(self.conn, {"chat_id": self.CHAT, "message_id": "m2",
                                                      "user_id": self.USER}, ocp.ui_text("not_sent"))
        self.assertEqual(self.completions(), [])
        self.assertEqual(self.shared(), [])


class HebrewRebuildTests(_WriterCase):
    async def test_a_card_rebuilt_from_the_transcript_keeps_its_hebrew_label(self):
        ocp.ui_lang_remember(self.USER, {"variables": {"{{USER_LANGUAGE}}": "he-IL"}})
        run = "agent-run-1"
        self.history = _turn_history(run=run)
        w = self.writer(run=run)
        await w.feed(_delta("Reading them."))
        await w.feed(_tool("start", tcid="toolu_1"))
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": run})
        cards = [i for i in self.m()["output"] if i.get("type") == "function_call"]
        self.assertEqual([c["id"] for c in cards], ["toolu_1"])
        self.assertEqual(cards[0]["display_labels"]["completed"], "הריץ פקודה")


class StoppedContentTests(_WriterCase):
    async def cancel_from_the_page(self, w):
        self.owui.session_pool.update({"sid-1": {"id": self.USER, "chat_ids": [self.CHAT]}})
        self.owui.page_intent["sid-1"] = "stop"
        _, task = self.owui.tasks[w.task_id]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # `_stopped` runs apart from the task: wait for M's end, not for event-loop turns.
        await asyncio.wait_for(w.wait_done(), 5)

    async def test_a_reply_stopped_before_any_text_saves_stopped(self):
        ocp.ui_lang_remember(self.USER, {"variables": {"{{USER_LANGUAGE}}": "he-IL"}})
        w = self.writer()
        await w.feed(_tool("start", tcid="toolu_1"))
        self.assertTrue(w.opened)
        await self.cancel_from_the_page(w)
        self.assertEqual(self.m()["content"], ocp.ui_text("stopped_reason", "he"))
        self.assertTrue(any(i.get("type") == "function_call" for i in self.m()["output"]))

    async def test_a_reply_stopped_after_text_keeps_just_its_text(self):
        w = self.writer()
        await w.feed(_delta("Reading them."))
        await w.feed(_tool("start", tcid="toolu_1"))
        await self.cancel_from_the_page(w)
        self.assertEqual(self.m()["content"], "Reading them.")

    async def test_the_live_reply_stopped_before_text_gets_the_note_after_its_request(self):
        self.owui.msgs()["m2"] = {"role": "assistant", "content": "", "done": True,
                                  "statusHistory": [{"description": "Stopped", "done": True}]}
        ref = {"chat_id": self.CHAT, "message_id": "m2", "user_id": self.USER}
        await ocp._close_unsent_reply(self.conn, ref, ocp.ui_text("stopped_reason"), only_if_empty=True)
        self.assertEqual(self.owui.msgs()["m2"]["content"], ocp.ui_text("stopped_reason"))
        # Its "Stopped" status line goes: test_eli512_polish_unit.StoppedOnceTests.

    async def test_the_note_waits_for_open_webuis_cancel_save(self):
        """Open WebUI saves a cancelled reply (`done`, its output) after the Pipe's stop: the note
        is written after that save, so the save cannot drop it or the cards."""
        self.owui.msgs()["m2"] = {"role": "assistant", "content": "", "done": False, "output": []}
        card = {"type": "function_call", "id": "toolu_1", "name": "exec", "status": "incomplete"}
        ref = {"chat_id": self.CHAT, "message_id": "m2", "user_id": self.USER}

        async def cancel_save():
            await asyncio.sleep(0.6)
            await self.owui._chats.upsert_message_to_chat_by_id_and_message_id(
                self.CHAT, "m2", {"done": True, "output": [card]})
        with mock.patch.object(ocp, "STOPPED_SAVE_POLL_S", 0.05):
            await asyncio.gather(cancel_save(), ocp._close_unsent_reply(
                self.conn, ref, ocp.ui_text("stopped_reason"), only_if_empty=True))
        saved = self.owui.msgs()["m2"]
        self.assertEqual(saved["content"], ocp.ui_text("stopped_reason"))
        ids = [i.get("id") for i in saved["output"]]
        self.assertEqual(ids[0], "toolu_1")
        self.assertTrue(ids[-1].startswith("msg_note_"), ids)

    async def test_cards_the_save_missed_are_kept(self):
        self.owui.msgs()["m2"] = {"role": "assistant", "content": "", "done": True, "output": []}
        card = {"type": "function_call", "id": "toolu_1", "name": "exec", "status": "completed"}
        ref = {"chat_id": self.CHAT, "message_id": "m2", "user_id": self.USER, "output": [card]}
        await ocp._close_unsent_reply(self.conn, ref, ocp.ui_text("stopped_reason"), only_if_empty=True)
        out = self.owui.msgs()["m2"]["output"]
        self.assertEqual(out[0]["id"], "toolu_1")
        self.assertTrue(out[-1]["id"].startswith("msg_note_"))

    async def test_an_untouched_reply_stopped_by_its_writer_still_says_stopped(self):
        """A writer that took M over and stops before anything streamed still ends M "Stopped."."""
        self.owui.msgs()["m1"] = {"id": "m1", "role": "assistant", "parentId": "q1", "content": "",
                                  "done": False}
        w = ocp._TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                            run_id="agent-run-1", message_id="m1", takeover=True)
        await w.finalize(stopped=True)
        self.assertEqual(self.m()["content"], ocp.ui_text("stopped_reason"))

    async def test_the_note_waits_for_the_live_request_and_its_save(self):
        """A live reply's stop: the request is still saving the cancelled reply. The note comes
        after the request ended and its save landed, never before."""
        self.owui.msgs()["m2"] = {"role": "assistant", "content": "", "done": False, "output": []}
        loop = asyncio.get_running_loop()
        request = loop.create_future()
        self.owui.request_tasks["task-live"] = request
        ref = {"chat_id": self.CHAT, "message_id": "m2", "user_id": self.USER, "request_task_id": "task-live"}
        with mock.patch.object(ocp, "STOPPED_SAVE_POLL_S", 0.02):
            close = asyncio.ensure_future(ocp._close_unsent_reply(
                self.conn, ref, ocp.ui_text("stopped_reason"), only_if_empty=True))
            await asyncio.sleep(0.1)
            self.assertEqual(self.owui.msgs()["m2"]["content"], "")
            await self.owui._chats.upsert_message_to_chat_by_id_and_message_id(
                self.CHAT, "m2", {"done": True, "output": [{"type": "function_call", "id": "t1"}]})
            request.set_result(None)
            await asyncio.wait_for(close, 5)
        saved = self.owui.msgs()["m2"]
        self.assertEqual(saved["content"], ocp.ui_text("stopped_reason"))
        self.assertEqual([i["id"] for i in saved["output"]][0], "t1")

    async def test_a_live_reply_with_content_is_left_as_it_is(self):
        self.owui.msgs()["m2"] = {"role": "assistant", "content": "partial", "done": True}
        ref = {"chat_id": self.CHAT, "message_id": "m2", "user_id": self.USER}
        await ocp._close_unsent_reply(self.conn, ref, ocp.ui_text("stopped_reason"), only_if_empty=True)
        self.assertEqual(self.owui.msgs()["m2"]["content"], "partial")
        self.assertEqual(self.completions(), [])


class ParkedStopTests(unittest.TestCase):
    def _turn(self, visible=""):
        turn = ocp._PipeTurn.__new__(ocp._PipeTurn)
        turn.stopped_shown, turn.status_closed = False, False
        turn.renderer = ocp._TurnRenderer("he")
        turn.renderer.visible_text = visible
        turn.visible_message_text = visible
        turn.owui_origin_chat_id, turn.owui_message_id, turn.channel_id = CHAT, "m9", None
        turn.__user__, turn.__metadata__ = {"id": GUEST}, {"task_id": "task-9"}
        turn.lang, turn.conn = "he", object()
        return turn

    def _stop(self, turn):
        spawned = []
        with mock.patch.object(ocp, "spawn_background", lambda coro: (spawned.append(coro), coro.close())), \
                mock.patch.object(ocp, "_close_unsent_reply", mock.AsyncMock()) as close:
            turn.on_parked_stop()
        return close

    def test_a_stop_before_any_text_writes_the_note(self):
        close = self._stop(self._turn())
        close.assert_called_once()
        args, kwargs = close.call_args
        self.assertEqual({k: v for k, v in args[1].items() if k != "output"},
                         {"chat_id": CHAT, "message_id": "m9", "user_id": GUEST, "request_task_id": "task-9",
                          "lang": "he"})
        self.assertEqual(args[2], "**נעצר.**")
        self.assertEqual(kwargs, {"only_if_empty": True})

    def test_a_held_media_line_is_no_shown_text(self):
        """The run's raw text holds a MEDIA line the reply has not shown: it still reads as empty."""
        turn = self._turn()
        turn.renderer.visible_text = "MEDIA:/tmp/openclaw/x.png"
        self._stop(turn).assert_called_once()

    def test_a_stop_after_text_hands_its_cards_to_the_close(self):
        """Its text stays, but its cards are closed (test_eli512_details_unit.StoppedCardsTests)."""
        self._stop(self._turn("half an answer")).assert_called_once()


if __name__ == "__main__":
    unittest.main()


class LiveNoteOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def _finish(self, shown=""):
        turn = ocp._PipeTurn.__new__(ocp._PipeTurn)
        turn.event_count, turn.text_yielded, turn.aborted = 0, False, False
        turn.steered_into, turn.lang = "run-2", "en"
        turn.visible_message_text = shown
        turn.turn = ocp._TurnState()
        gen = turn.finish()
        first = await gen.__anext__()
        await gen.aclose()
        return turn, first

    async def test_a_reply_that_is_only_the_steer_note_is_marked(self):
        """Its request's end then publishes no chat.finished (`_request_ended`)."""
        turn, first = await self._finish()
        self.assertEqual(first, ocp.ui_text("sent_with_next"))
        self.assertTrue(turn.turn.note_only)
