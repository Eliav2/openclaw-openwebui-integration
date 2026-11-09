#!/usr/bin/env python3
"""Focused unit tests for pipe stream recovery behavior."""

import unittest
import ast
import html
import os
import tempfile
import textwrap
import contextlib
import base64
import io
import sqlite3
import copy
import inspect
import json
import re
import shutil
import subprocess
import sys
import time
import types
import asyncio
import uuid
import urllib.error
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_helpers import FakeSio, RecordingEmitter, fake_owui, owui_modules, run_on_virtual_clock  # noqa: E402
from _paged_sessions import PagedSessions, crowd  # noqa: E402
from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import websockets  # noqa: E402  (after the stubs)

import openclaw_pipe  # noqa: E402
import openclaw_status_action  # noqa: E402

RUN_ERROR_SENTENCE = openclaw_pipe._ERROR_TEXT[openclaw_pipe.LANG_EN][openclaw_pipe.ERROR_RUN]
HEBREW_ASK = "מתי יוצאת הרכבת הבאה לקיוטו"
from openclaw_pipe import (
    ACTION_RUN_PREFIX, TURN_NS, _run_id_sent_at, parse_pipe_run_id, pipe_run_id, turn_message_id,
    _claim_turn_message, _register_turn_writer, _retire_turn_writer,
    MAX_TURN_RESUMES, next_continuation, turn_owner,
    recovery_source_run, run_message_id, _TurnWriter, _INTENT_HOOK_JS,
    _turn_route_now, _turn_after_hello, _turn_resync, _history_span_messages,
    wake_turns, wake_turn_at, wake_span, _wake_resync, _adopt_turn_message,
    _GatewayConnection,
    GatewayError,
    _parse_gateway_url,
    _explain_connect_rejection,
    _explain_session_patch_failure,
    _marker_line_index,
    _discover_models_with_source,
    _parse_text_prompt,
    _truncate_at_repeated_marker,
    _dedupe_options,
    Pipe,
    _write_json_file,
    _state_dir,
    UNVERIFIED_MODEL_SUFFIX,
    MODELS_SOURCE_FALLBACK,
    MODELS_SOURCE_CACHE,
    _TurnRenderer,
    _render_tool_result_block,
    _tool_call_started_event,
    _tool_call_result_event,
    _tool_call_error_relabel_event,
    _tool_error_banner,
    _relay_content_is_showable,
    _relay_output_events,
    _history_renderer,
    _history_run_messages,
    _stub_turn,
    _history_user_run_id,
    _reconcile_stub,
    ORPHAN_STUB_NOTE,
    TOOL_ERROR_MARK,
    TOOL_NO_RESULT_NOTE,
    _FALLBACK_MODELS,
    _advance_input_prompt_buffer,
    _advance_media_buffer,
    _ask_user_detail_block,
    _ask_user_fallback_text,
    _ask_user_input_modal,
    _build_usage_status_lines,
    _extract_numbered_options,
    _coerce_text,
    _content_has_image,
    _could_be_user_input_prefix,
    _deepest_leaf_id,
    _emit_message_snapshot,
    _emit_status,
    _extract_image_attachments,
    _fmt_tokens,
    _friendly_name,
    _is_user_input_prompt,
    _item_assistant_text,
    _item_delta_text,
    _live_session_id_for_user,
    _native_ask_user_payload,
    _format_native_answers,
    _model_patch_matches,
    _normalize_event_call_response,
    _normalize_model_entry,
    _owui_chat_send_params,
    _owui_session_key,
    _history_recovery_text,
    _recover_turn_text,
    _recovery_text_from_items,
    _provider_from_key,
    _reap_stale_gateway_connection,
    _relative_time,
    _remember_gateway_connection,
    _resolve_media,
    _resolve_media_text,
    _resolve_media_via_owui,
    _session_active_from_signals,
    _strip_input_marker_line,
    _shared_gateway_state,
    _SHARED_STATE_ATTR,
    _SUBAGENT_TASK_ID_MARKER_RE,
    _suppress_already_shown,
    _owui_chat_send_params,
    _session_thinking_ladder,
    _session_model_key,
    _record_thinking_ladder,
    _read_model_thinking_ladder,
    _thinking_rejection_retry_body,
    parse_thinking_rejection,
    clamp_to_ladder,
    LEVEL_RANKS,
    LADDER_CACHE_NAME,
    Pipe,
)


class RecoveryItemsTests(unittest.TestCase):
    def test_recovers_assistant_after_matching_user_message(self):
        items = [
            {"role": "user", "text": "old"},
            {"role": "assistant", "text": "old answer"},
            {"role": "user", "text": "current question"},
            {"role": "assistant", "text": "current answer"},
        ]
        self.assertEqual(_recovery_text_from_items(items, "current question"), "current answer")

    def test_does_not_recover_without_matching_user_message(self):
        items = [{"role": "assistant", "text": "answer"}]
        self.assertIsNone(_recovery_text_from_items(items, "current question"))


class InterruptedTurnTailRecoveryTests(unittest.TestCase):
    """ELI-74: a turn whose live stream was lost is read back whole from `chat.history`, never from
    the clipped `sessions.preview`."""

    KEY = "agent:main:openwebui-u-c"
    QUESTION = "what changed?"
    # Longer than both server-side preview caps.
    LONG = "".join(f"Sentence {i} of the final answer. " for i in range(200)).strip()

    def _history(self):
        # The live claude-cli layout: the pipe's user message, the imported copy behind the
        # metadata envelope, the turn's assistant messages, then the closer with the whole text.
        envelope = (
            "Conversation info (untrusted metadata):\n```json\n{}\n```\n\n"
            + self.QUESTION
        )
        return {"messages": [
            {"role": "user", "content": "older question"},
            {"role": "assistant", "content": [{"type": "text", "text": "older answer"}]},
            {"role": "user", "content": self.QUESTION,
             "idempotencyKey": "msg-c-1:user"},
            {"role": "user", "content": envelope},
            {"role": "assistant", "content": [{"type": "text", "text": "I'll check."}]},
            {"role": "assistant", "content": [
                {"type": "toolcall", "name": "exec"}, {"type": "tool_result"}]},
            {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": self.LONG}]},
        ]}

    def _conn(self, history=None, history_error=None):
        conn = types.SimpleNamespace()
        if history_error is not None:
            conn.session_history = mock.AsyncMock(side_effect=history_error)
        else:
            conn.session_history = mock.AsyncMock(return_value=history)
        return conn

    def test_long_tail_survives_intact(self):
        self.assertGreater(len(self.LONG), 2000)
        conn = self._conn(self._history())
        recovered = asyncio.run(_recover_turn_text(conn, self.KEY, self.QUESTION))
        self.assertEqual(recovered, self.LONG)
        self.assertFalse(recovered.endswith("..."))

    def test_history_is_requested_with_a_budget_above_the_old_clip(self):
        conn = self._conn(self._history())
        asyncio.run(_recover_turn_text(conn, self.KEY, self.QUESTION))
        conn.session_history.assert_awaited_once_with(self.KEY)
        self.assertGreaterEqual(
            inspect.signature(_GatewayConnection.session_history)
            .parameters["max_chars"].default,
            len(self.LONG),
        )

    def test_returns_the_turns_closing_text_not_its_opening_preamble(self):
        self.assertEqual(_history_recovery_text(self._history(), self.QUESTION), self.LONG)

    def test_stops_at_the_next_turn(self):
        history = self._history()
        history["messages"] += [
            {"role": "user", "content": "a later question"},
            {"role": "assistant", "content": [{"type": "text", "text": "later answer"}]},
        ]
        self.assertEqual(_history_recovery_text(history, self.QUESTION), self.LONG)

    def test_a_history_failure_recovers_nothing(self):
        conn = self._conn(history_error=GatewayError("chat.history timed out"))
        self.assertIsNone(asyncio.run(_recover_turn_text(conn, self.KEY, self.QUESTION)))

    def test_no_match_anywhere_is_none(self):
        conn = self._conn({"messages": []})
        self.assertIsNone(asyncio.run(_recover_turn_text(conn, self.KEY, "never asked")))


async def _play_frames(conn, frames):
    """Run `conn`'s loop over `frames`, then stop it as a shutdown (not a tick timeout)."""
    frames = list(frames)

    class ScriptedWS:
        async def recv(self_):
            if frames:
                return json.dumps(frames.pop(0))
            conn._stopped = True
            raise asyncio.CancelledError

    conn._ws = ScriptedWS()
    await conn._event_loop()


class SessionToolEventTests(unittest.IsolatedAsyncioTestCase):
    """ELI-92: a run this connection did not start sends `session.tool` events; they fold into the
    `agent` path, first copy of each (call, phase) kept."""

    SK = _owui_session_key("main", "11111111-1111-1111-1111-111111111111",
                           "22222222-2222-2222-2222-222222222222")

    async def _run_loop(self, conn, frames):
        await _play_frames(conn, frames)
        for _ in range(5):
            await asyncio.sleep(0)

    def _tool(self, event, phase, run_id="run-1"):
        payload = {"runId": run_id, "sessionKey": self.SK, "stream": "tool",
                   "data": {"phase": phase, "name": "Bash", "toolCallId": "t1",
                            "args": {"command": "ls"}}}
        if event == "session.tool":
            payload["displayName"] = "session row fields ride along"
        return {"type": "event", "event": event, "payload": payload}

    async def test_session_tool_reaches_the_runs_consumer_as_a_tool_event(self):
        conn = _GatewayConnection(lambda: None)
        q = conn.register_consumer(self.SK, "run-1")
        await self._run_loop(conn, [self._tool("session.tool", "start")])
        msg = q.get_nowait()
        self.assertEqual(msg["event"], "agent")
        self.assertEqual(msg["payload"]["data"]["toolCallId"], "t1")
        self.assertNotIn("displayName", msg["payload"])

    async def test_initiator_and_subscriber_copies_are_dispatched_once(self):
        conn = _GatewayConnection(lambda: None)
        q = conn.register_consumer(self.SK, "run-1")
        await self._run_loop(conn, [
            self._tool("agent", "start"), self._tool("session.tool", "start"),
            self._tool("session.tool", "result"), self._tool("agent", "result"),
        ])
        phases = []
        while not q.empty():
            phases.append(q.get_nowait()["payload"]["data"]["phase"])
        self.assertEqual(phases, ["start", "result"])

    async def test_a_childs_row_change_wakes_its_parents_child_waiter(self):
        """ELI-260: an open Status dialog learns of a new child from its row's `sessions.changed`,
        which names the parent as `spawnedBy` (2026.10) or `parentSessionKey`."""
        child = "agent:main:subagent:aaaaaaaa-0000-0000-0000-000000000001"
        conn = _GatewayConnection(lambda: None)
        for field in ("spawnedBy", "parentSessionKey"):
            ev = conn.session_child_event(self.SK)
            conn._on_sessions_changed({"sessionKey": "agent:main:other", "hasActiveRun": True})
            self.assertFalse(ev.is_set(), "another session's change")
            conn._on_sessions_changed({"sessionKey": child, field: self.SK, "hasActiveRun": True})
            self.assertTrue(ev.is_set(), field)
        ev = conn.session_child_event(self.SK)
        conn._mark_event_gap()
        self.assertTrue(ev.is_set(), "a gap may have lost the child's push")

    async def test_any_push_for_a_session_wakes_its_waiters_not_only_one_with_run_state(self):
        """Any push for a session wakes its waiters. 2026.10.10 announces a cron wake's idle with
        no `hasActiveRun`, so waking only on that field left a queued message queued."""
        conn = _GatewayConnection(lambda: None)
        seen = []
        conn.row_to_consumers = lambda key, payload, *a, **k: seen.append(payload)
        ev = conn.session_change_event(self.SK)
        conn._on_sessions_changed({"sessionKey": "agent:main:other", "reason": "cron-binding", "status": "done"})
        conn._on_sessions_changed({"reason": "cron-bindings-loaded"})
        self.assertFalse(ev.is_set(), "another session's change, or one that names no session")
        conn._on_sessions_changed({"sessionKey": self.SK, "reason": "cron-binding", "status": "done",
                                   "session": {"status": "done", "hasActiveRun": False, "activeRunIds": []}})
        self.assertTrue(ev.is_set())
        self.assertEqual(seen, [], "no run state in it: nothing for the replies to apply")
        conn._on_sessions_changed({"sessionKey": self.SK, "hasActiveRun": False, "status": "done"})
        self.assertEqual(len(seen), 1)

    async def test_a_reply_counts_until_its_consumer_goes_and_its_end_wakes_the_waiters(self):
        """ELI-260: the Status dialog counts a chat as running while this connection streams a
        reply for it; the reply's end makes the dialog read the chat again."""
        conn = _GatewayConnection(lambda: None)
        self.assertFalse(conn.has_reply(self.SK))
        q = conn.register_consumer(self.SK, "run-1")
        ev = conn.session_change_event(self.SK)
        self.assertTrue(conn.has_reply(self.SK))
        self.assertFalse(conn.has_reply("agent:main:other"))
        conn.unregister_consumer(self.SK, "run-1", q)
        self.assertFalse(conn.has_reply(self.SK))
        self.assertTrue(ev.is_set())

    async def test_an_accepted_spawn_is_recorded_in_the_child_index(self):
        """The Status dialog lists children from this index (state.py), not every session."""
        child = "agent:main:subagent:aaaaaaaa-0000-0000-0000-000000000001"
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"OPENCLAW_BRIDGE_STATE_DIR": td}):
            conn = _GatewayConnection(lambda: None)
            frame = {"type": "event", "event": "session.tool", "payload": {
                "runId": "run-1", "sessionKey": self.SK, "stream": "tool",
                "data": {"phase": "result", "name": "sessions_spawn", "toolCallId": "t7",
                         "result": json.dumps({"status": "accepted", "childSessionKey": child, "runId": "r"})}}}
            await self._run_loop(conn, [frame])
            for _ in range(40):
                if openclaw_pipe._subagent_children(self.SK):
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(openclaw_pipe._subagent_children(self.SK), {child: None})

    async def test_a_run_starting_in_an_owui_session_subscribes_its_transcript_once(self):
        conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        conn.send_request = mock.AsyncMock(return_value={})
        started = {"type": "event", "event": "sessions.changed",
                   "payload": {"sessionKey": self.SK, "hasActiveRun": True,
                               "reason": "chat.run.started"}}
        other = {"type": "event", "event": "sessions.changed",
                 "payload": {"sessionKey": "agent:main:subagent:x", "hasActiveRun": True}}
        await self._run_loop(conn, [started, started, other])
        calls = [c.args for c in conn.send_request.await_args_list]
        self.assertEqual(calls, [("sessions.messages.subscribe", {"key": self.SK})])

    async def test_a_new_socket_renews_every_subscription(self):
        conn = _GatewayConnection(lambda: None)
        conn.send_request = mock.AsyncMock(return_value={})
        conn._message_subscriptions = {self.SK}
        conn._subscriptions_stale = True
        await self._run_loop(conn, [])
        calls = [c.args for c in conn.send_request.await_args_list]
        self.assertEqual(calls, [("sessions.subscribe", {"activeOnly": True}),
                                 ("sessions.messages.subscribe", {"key": self.SK})])
        self.assertFalse(conn._subscriptions_stale)


def _cli_history(run_id="run-9"):
    """A claude-cli run in `chat.history`: imported text / toolcall / tool_result messages, then
    the `cli-assistant:<runId>` closer with all its text."""
    imported = {"importedFrom": "claude-cli"}
    return {"messages": [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": [{"type": "text", "text": "old answer"}],
         "idempotencyKey": "cli-assistant:run-1"},
        {"role": "user", "content": "check the logs"},
        {"role": "assistant", "__openclaw": imported,
         "content": [{"type": "text", "text": "Reading them."}]},
        {"role": "assistant", "__openclaw": imported, "content": [
            {"type": "toolcall", "id": "toolu_1", "name": "Bash",
             "arguments": {"command": "tail log"}}]},
        {"role": "assistant", "__openclaw": imported, "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "name": "Bash",
             "content": [{"type": "text", "text": "ERROR x"}]}]},
        {"role": "assistant", "__openclaw": imported,
         "content": [{"type": "text", "text": "Found it."}]},
        {"role": "assistant", "idempotencyKey": f"cli-assistant:{run_id}",
         "content": [{"type": "text", "text": "Reading them.\n\nFound it."}]},
    ]}


class HistoryRunRebuildTests(unittest.TestCase):
    """A run whose events were lost is rebuilt from `chat.history` into the same `text` + `output`
    the live renderer makes."""

    def test_run_slice_is_from_its_user_message_to_its_closing_message(self):
        msgs = _history_run_messages(_cli_history(), "run-9")
        self.assertEqual(len(msgs), 5)
        self.assertEqual(msgs[-1]["idempotencyKey"], "cli-assistant:run-9")
        self.assertIsNone(_history_run_messages(_cli_history(), "run-unknown"))

    def test_rebuild_interleaves_text_and_cards_without_repeating_the_closing_text(self):
        r = _history_renderer(_history_run_messages(_cli_history(), "run-9"))
        self.assertEqual(r.text, "Reading them.\n\nFound it.")
        self.assertEqual([i["type"] for i in r.output],
                         ["message", "function_call", "function_call_output", "message"])
        self.assertEqual(r.output[1]["arguments"], json.dumps({"command": "tail log"}))
        self.assertEqual(r.output[2]["output"][0]["text"], "ERROR x")

    def test_tool_free_reply_comes_from_the_closing_message(self):
        r = _history_renderer(_history_run_messages(_cli_history(), "run-1"))
        self.assertEqual(r.text, "old answer")
        self.assertEqual([i["type"] for i in r.output], ["message"])

    @staticmethod
    def _two_text_blocks(closer=True):
        """A preamble, five Bash calls, the answer. The Gateway's closer joins the two text blocks
        with a blank line, as the live stream did."""
        imported = {"importedFrom": "claude-cli"}
        msgs = [{"role": "assistant", "__openclaw": imported,
                 "content": [{"type": "text", "text": "Running the five sleep steps one at a time."}]}]
        for n in range(1, 3):
            msgs.append({"role": "assistant", "__openclaw": imported, "content": [
                {"type": "toolcall", "id": f"toolu_{n}", "name": "Bash", "arguments": {"command": "x"}},
                {"type": "tool_result", "tool_use_id": f"toolu_{n}", "name": "Bash",
                 "content": [{"type": "text", "text": f"step-{n}"}]}]})
        msgs.append({"role": "assistant", "__openclaw": imported,
                     "content": [{"type": "text", "text": "USER-DONE-9e127db9"}]})
        if closer:
            msgs.append({"role": "assistant", "idempotencyKey": "cli-assistant:msg-89bb045d-1", "content": [
                {"type": "text", "text": "Running the five sleep steps one at a time.\n\nUSER-DONE-9e127db9"}]})
        return msgs

    def test_text_after_a_tool_is_joined_the_way_the_gateway_streamed_it(self):
        r = _history_renderer(self._two_text_blocks())
        self.assertEqual(r.text, "Running the five sleep steps one at a time.\n\nUSER-DONE-9e127db9")
        texts = [i["content"][0]["text"] for i in r.output if i["type"] == "message"]
        self.assertEqual(texts, ["Running the five sleep steps one at a time.", "\n\nUSER-DONE-9e127db9"])

    def test_without_a_closing_message_text_blocks_get_the_gateways_blank_line(self):
        """An aborted run has no closer: the join does not depend on one."""
        r = _history_renderer(self._two_text_blocks(closer=False))
        self.assertEqual(r.text, "Running the five sleep steps one at a time.\n\nUSER-DONE-9e127db9")

    def test_a_native_runs_transcript_rebuilds_its_cards(self):
        """A run on the Gateway's own runtime (GPT, Gemini, DeepSeek) has OpenClaw's shape:
        `toolCall` blocks, and each result is its own `toolResult` message."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "test_fixture_native_history.json")
        with open(path) as f:
            messages = json.load(f)["messages"]
        messages = messages + [{"role": "assistant", "stopReason": "stop",
                                "content": [{"type": "text", "text": "The workshop is empty."}]}]
        r = _history_renderer(messages)
        cards = [i for i in r.output if i["type"] == "function_call"]
        self.assertEqual([c["name"] for c in cards],
                         ["ls", f"ls {TOOL_ERROR_MARK}", "exec", f"ls {TOOL_ERROR_MARK}", "exec"])
        outputs = {i["call_id"]: i["output"][0]["text"] for i in r.output if i["type"] == "function_call_output"}
        self.assertEqual(outputs["call_00_bLXMsmgz1pJc5IL1J0Rx1971"], "(empty directory)")
        self.assertTrue(outputs["call_00_QFthybdFqQt4QEdyJoAu2999"].startswith(f"{TOOL_ERROR_MARK} tool call failed"))
        self.assertEqual(len(r.finished_output()), len(r.output))  # every call answered
        self.assertEqual(r.output[-1]["type"], "message")
        self.assertTrue(r.text.endswith("The workshop is empty."))

    @staticmethod
    def _native_chat_send():
        """A `chat.send` on the Gateway's own runtime: no closing `cli-assistant:` message; each
        message carries its run id in `__openclaw.runId`, the last one with `stopReason` "stop"."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "test_fixture_native_chat_send.json")
        with open(path) as f:
            messages = json.load(f)["messages"]
        return messages, messages[0]["idempotencyKey"].removesuffix(":user")

    def test_a_native_run_is_found_by_its_run_id_once_it_stopped(self):
        messages, run = self._native_chat_send()
        later = [{"role": "user", "content": "next", "idempotencyKey": "msg-c-x-9.0:user"}]
        for label, hist in (("newest turn", messages), ("a later turn follows", messages + later)):
            with self.subTest(label):
                span = _history_span_messages({"messages": hist}, run)
                self.assertEqual([m["role"] for m in span], ["assistant", "toolResult", "assistant"])
                r = _history_renderer(span)
                self.assertEqual([i["type"] for i in r.output],
                                 ["function_call", "function_call_output", "message"])
                self.assertEqual(r.text, "DONE")

    def test_a_native_run_still_calling_tools_is_not_closed(self):
        messages, run = self._native_chat_send()
        self.assertIsNone(_history_run_messages({"messages": messages[:3]}, run))
        self.assertIsNone(_history_span_messages({"messages": messages[:3]}, run))

    def test_a_run_a_second_announce_was_steered_into_keeps_both_replies(self):
        """ELI-271: a second sub-agent announce steered into the first's run makes one run write
        two closing-looking replies. The rebuild keeps both."""
        spawn, announce, settle = "msg-c-1-1.0", "announce:v1:agent:main:subagent:a:r1", "announce:settle"
        reply = lambda run, text: {"role": "assistant", "stopReason": "stop",  # noqa: E731
                                   "__openclaw": {"runId": run}, "content": [{"type": "text", "text": text}]}
        messages = [{"role": "user", "content": "spawn twins", "idempotencyKey": f"{spawn}:user"},
                    reply(spawn, "SPAWNED"), reply(announce, "A heard."), reply(announce, "B heard.")]
        for label, hist in (("newest run", messages), ("the settle turn follows", messages + [reply(settle, "ok")])):
            with self.subTest(label):
                span = _history_span_messages({"messages": hist}, announce)
                self.assertEqual([m["content"][0]["text"] for m in span], ["A heard.", "B heard."])
                r = _history_renderer(span)
                self.assertEqual(r.text, "A heard.\n\nB heard.")

    @staticmethod
    def _tool(n):
        return {"role": "assistant", "stopReason": "tool_use", "__openclaw": {"importedFrom": "claude-cli"},
                "content": [{"type": "toolcall", "id": f"t{n}", "name": "exec", "arguments": {"command": f"step {n}"}},
                            {"type": "tool_result", "tool_use_id": f"t{n}", "name": "exec",
                             "content": [{"type": "text", "text": f"out {n}"}]}]}

    @staticmethod
    def _cron_prompt(key, job):
        # As 2026.10.10 stores it: a cron provenance with no jobId or prompt prefix.
        return {"role": "user", "content": f"[cron:{job} selftest] This is a test",
                "provenance": {"kind": "internal_system", "sourceTool": "cron"},
                "__openclaw": {"idempotencyKey": f"{key}:user", "importedFrom": "claude-cli"}}

    @staticmethod
    def _closing(run, text):
        return {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": text}],
                "__openclaw": {"runId": run, "idempotencyKey": f"cli-assistant:{run}", "importedFrom": "claude-cli"}}

    def _cards(self, span):
        return sum(1 for i in _history_renderer(span).output if i["type"] == "function_call")

    def test_a_cron_prompt_recorded_mid_run_stays_out_of_the_run_it_landed_in(self):
        """A cron wake's prompt (2026.10.10) sorts among the running run's messages; a wake's span
        must not stop at the next wake's prompt."""
        w1, w2 = "6f367d1b-4012-4af0-a399-a19fd9957567", "9a0a91fd-fd1b-4f04-9b83-7feb4e2f1acb"
        ready = "msg-c-1-1.0"
        messages = [{"role": "user", "content": "Reply with exactly: READY", "idempotencyKey": f"{ready}:user"},
                    self._closing(ready, "READY"),
                    self._cron_prompt("46f6a533-3ebe-4822-90eb-6df00aaf33c9", "2faa013f"), self._tool(1),
                    self._cron_prompt("ef53af68-1b7a-461e-ba17-5d69bcd82101", "4751274e"), self._tool(2),
                    self._closing(w1, "W1-DONE"), self._tool(3), self._tool(4), self._closing(w2, "W2-DONE")]
        for run, cards, text in ((ready, 0, "READY"), (w1, 2, "W1-DONE"), (w2, 2, "W2-DONE")):
            with self.subTest(run=run):
                span = _history_span_messages({"messages": messages}, run)
                self.assertNotIn("user", [m["role"] for m in span])
                self.assertEqual(self._cards(span), cards)
                self.assertEqual(_history_renderer(span).text, text)

    def test_a_user_run_a_cron_wake_fired_into_keeps_every_tool_card(self):
        """A cron wake firing mid-run sorts its prompt between the user run's tool calls; the
        rebuild keeps every card."""
        run, wake = "msg-c-1-1.0", "9a0a91fd-fd1b-4f04-9b83-7feb4e2f1acb"
        messages = [{"role": "user", "content": "do two steps", "idempotencyKey": f"{run}:user"},
                    {"role": "user", "content": "Conversation info (untrusted metadata): {}\n\ndo two steps"},
                    self._tool(1), self._cron_prompt("46f6a533-3ebe-4822-90eb-6df00aaf33c9", "2faa013f"),
                    self._tool(2), self._closing(run, "R-DONE"), self._tool(3), self._closing(wake, "W-DONE")]
        for rid, cards, text in ((run, 2, "R-DONE"), (wake, 1, "W-DONE")):
            with self.subTest(run=rid):
                span = _history_span_messages({"messages": messages}, rid)
                self.assertNotIn("user", [m["role"] for m in span])
                self.assertEqual(self._cards(span), cards)
                self.assertEqual(_history_renderer(span).text, text)


def _turn_history(stub_id="stub-1", *, run="msg-c-stub-1-1.0", closed=True,
                  text="check the logs", later=False):
    """A claude-cli `chat.send` as stored: `<runId>:user`, the imported copy, assistant messages,
    then `cli-assistant:<runId>`."""
    imported = {"importedFrom": "claude-cli"}
    msgs = [
        {"role": "user", "content": "earlier", "idempotencyKey": "msg-c-old-0.5:user"},
        {"role": "assistant", "content": [{"type": "text", "text": "old"}],
         "idempotencyKey": "cli-assistant:msg-c-old-0.5"},
        {"role": "user", "content": text, "idempotencyKey": f"{run}:user"},
        {"role": "user", "content": "Conversation info (untrusted metadata): {}\n\n" + text},
        {"role": "assistant", "__openclaw": imported,
         "content": [{"type": "text", "text": "Reading them."}]},
        {"role": "assistant", "__openclaw": imported, "content": [
            {"type": "toolcall", "id": "toolu_1", "name": "Bash",
             "arguments": {"command": "tail log"}},
            {"type": "tool_result", "tool_use_id": "toolu_1", "name": "Bash",
             "content": [{"type": "text", "text": "ERROR x"}]}]},
    ]
    if closed:
        msgs += [
            {"role": "assistant", "__openclaw": imported,
             "content": [{"type": "text", "text": "Found it."}]},
            {"role": "assistant", "idempotencyKey": f"cli-assistant:{run}",
             "content": [{"type": "text", "text": "Reading them.\n\nFound it."}]},
        ]
    if later:
        msgs += [{"role": "user", "content": "next", "idempotencyKey": "msg-c-x-9.0:user"},
                 {"role": "assistant", "content": [{"type": "text", "text": "later answer"}],
                  "idempotencyKey": "cli-assistant:msg-c-x-9.0"}]
    return {"messages": msgs}


class StubTurnTests(unittest.TestCase):
    """ELI-90: an orphaned reply stub is matched to its run in the transcript."""

    def test_found_by_the_reply_id_inside_the_run_id(self):
        turn = _stub_turn(_turn_history(), "stub-1", "some other text")
        self.assertEqual(turn["run_id"], "msg-c-stub-1-1.0")
        self.assertTrue(turn["closed"])
        self.assertEqual(turn["messages"][-1]["idempotencyKey"], "cli-assistant:msg-c-stub-1-1.0")
        r = _history_renderer(turn["messages"])
        self.assertEqual(r.text, "Reading them.\n\nFound it.")
        self.assertEqual(sum(1 for i in r.output if i["type"] == "function_call"), 1)

    def test_older_run_ids_fall_back_to_the_user_text(self):
        hist = _turn_history(run="msg-c-1.0")
        self.assertEqual(_stub_turn(hist, "stub-1", "check the logs")["run_id"], "msg-c-1.0")
        self.assertIsNone(_stub_turn(hist, "stub-1", "never sent"))

    def test_open_turn_is_not_closed_until_the_transcript_says_so(self):
        self.assertFalse(_stub_turn(_turn_history(closed=False), "stub-1", "")["closed"])
        # A later turn means this one is over even without a closing message.
        turn = _stub_turn(_turn_history(closed=False, later=True), "stub-1", "")
        self.assertTrue(turn["closed"])
        self.assertNotIn("later answer", json.dumps(turn["messages"]))

    def test_a_repeated_text_matches_the_first_run_sent_after_the_stub(self):
        hist = _turn_history(run="msg-c-100.5", text="works?")
        hist["messages"] += [
            {"role": "user", "content": "works?", "idempotencyKey": "msg-c-200.5:user"},
            {"role": "assistant", "content": [{"type": "text", "text": "second"}],
             "idempotencyKey": "cli-assistant:msg-c-200.5"}]
        self.assertEqual(_stub_turn(hist, "s", "works?", 100.0)["run_id"], "msg-c-100.5")
        self.assertEqual(_stub_turn(hist, "s", "works?", 199.0)["run_id"], "msg-c-200.5")
        self.assertIsNone(_stub_turn(hist, "s", "works?", 300.0))
        self.assertEqual(_stub_turn(hist, "s", "works?", 100.0)["by"], "text")

    def test_the_imported_copy_of_the_user_message_starts_no_turn(self):
        self.assertIsNone(_history_user_run_id(_turn_history()["messages"][3]))
        self.assertEqual(_history_user_run_id(_turn_history()["messages"][2]), "msg-c-stub-1-1.0")


class OrphanStubReconcileTests(unittest.IsolatedAsyncioTestCase):
    """ELI-90: replies an Open WebUI restart orphaned are finished from the Gateway transcript,
    waiting for a running run's pushed settle, never on a clock."""

    USER = "11111111-1111-1111-1111-111111111111"
    CHAT = "22222222-2222-2222-2222-222222222222"
    STUB = "stub-1"
    RUN = "msg-c-stub-1-1.0"

    def setUp(self):
        self.sk = _owui_session_key("main", self.USER, self.CHAT)
        self.chat = {"history": {"currentId": self.STUB, "messages": {
            "u1": {"role": "user", "content": "check the logs", "childrenIds": [self.STUB]},
            self.STUB: {"role": "assistant", "content": "", "done": False,
                        "parentId": "u1", "childrenIds": [], "model": "openclaw_gateway.m",
                        # The task of the request that died with Open WebUI.
                        "meta": {"task_id": "task-dead"}},
        }}}
        self.history = _turn_history(closed=False)
        self.on_history = None
        self.active = True
        self.live_task = False
        test = self

        class FakeChat:
            def __init__(self):
                self.chat, self.id, self.user_id = test.chat, test.CHAT, test.USER

        class FakeChats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                return FakeChat()

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                msgs = test.chat["history"]["messages"]
                msgs[message_id] = {**msgs.get(message_id, {}), **message}
                test.chat["history"]["currentId"] = message_id
                return FakeChat()

        chats_mod = types.ModuleType("open_webui.models.chats")
        chats_mod.Chats = FakeChats
        self.sio = mock.AsyncMock()
        self.modules = {**owui_modules(sio=self.sio),
                        "open_webui.models": types.ModuleType("open_webui.models"),
                        "open_webui.models.chats": chats_mod}
        self.conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))

        async def send_request(method, params, timeout=10):
            if method == "chat.history":
                if test.on_history:
                    test.on_history()
                return copy.deepcopy(test.history)
            if method == "sessions.list":
                return {"sessions": [{"key": test.sk, "hasActiveRun": test.active}]}
            raise AssertionError(method)
        self.conn.send_request = send_request

        async def live(chat_id):
            return test.live_task
        patches = [mock.patch.dict(sys.modules, self.modules),
                   mock.patch("openclaw_pipe._owui_chat_has_live_task", live)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    async def _spin():
        for _ in range(50):
            await asyncio.sleep(0)

    def _stub(self):
        return self.chat["history"]["messages"][self.STUB]

    def _emits(self, kind):
        return [c.args[1]["data"] for c in self.sio.emit.await_args_list
                if c.args[0] == "events" and c.args[1]["data"]["type"] == kind]

    async def test_a_running_run_is_finished_on_the_pushed_settle(self):
        task = asyncio.create_task(_reconcile_stub(self.conn, "main", self.CHAT, self.STUB))
        await self._spin()
        self.assertFalse(task.done(), "must wait for the run, not finish a partial")
        self.assertIs(self._stub()["done"], False)
        # Unrelated change for the same session: re-reads, still running.
        self.conn._on_sessions_changed({"sessionKey": self.sk, "hasActiveRun": True})
        await self._spin()
        self.assertFalse(task.done())
        self.history, self.active = _turn_history(), False
        self.conn._on_sessions_changed({"sessionKey": self.sk, "hasActiveRun": False})
        await self._spin()
        self.assertTrue(task.done())
        self.assertEqual(self._stub()["content"], "Reading them.\n\nFound it.")
        self.assertIs(self._stub()["done"], True)

    async def test_a_finished_stub_publishes_chat_finished_with_the_valve(self):
        """A recovered reply is saved whole and PUBLISH_CHAT_FINISHED publishes it (#273)."""
        published = []

        async def publish(chat_id, message_id, user_id, text=None):
            published.append((chat_id, message_id, user_id))
        self.history, self.active = _turn_history(), False
        for on in (False, True):
            self.chat["history"]["messages"][self.STUB].update(done=False, content="")
            self.conn._claimed_runs.clear()
            self.conn._valves = lambda: types.SimpleNamespace(AGENT_ID="main", PUBLISH_CHAT_FINISHED=on)
            with mock.patch.object(openclaw_pipe, "_publish_chat_finished", publish):
                await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB)
                await self._spin()
            self.assertIs(self._stub()["done"], True)
            self.assertEqual(published, [(self.CHAT, self.STUB, self.USER)] if on else [])

    async def test_a_reply_answered_in_a_model_column_is_finished_from_that_session(self):
        """ELI-329: the second model of a multi-model chat has its own session."""
        import tempfile
        state = tempfile.mkdtemp()
        self.conn._valves = lambda: types.SimpleNamespace(AGENT_ID="main", STATE_DIR=state)
        sk1 = openclaw_pipe._owui_session_key("main", self.USER, self.CHAT, 1)
        asked = []
        real = self.conn.send_request

        async def send_request(method, params, timeout=10):
            asked.append(params.get("sessionKey") or params.get("key"))
            if method == "sessions.list":
                return {"sessions": [{"key": sk1, "hasActiveRun": False}]}
            return await real(method, params, timeout)
        self.conn.send_request = send_request
        for column, tail in ((0, ["u1", "other-reply"]), (1, ["u1", self.STUB])):
            openclaw_pipe._history_line_write(
                state, openclaw_pipe._history_line_id(self.CHAT, column), {"tail": tail})
        self.history, self.active = _turn_history(), False
        await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB)
        self.assertIn(sk1, asked)
        self.assertNotIn(self.sk, asked)
        self.assertIs(self._stub()["done"], True)

    async def test_a_gone_writers_message_is_adopted_even_with_another_task_in_the_chat(self):
        self._stub()["meta"] = {"openclaw_writer": "1:gone", "openclaw_run": "agent-run-1"}
        self.live_task = True
        with mock.patch("openclaw_pipe._adopt_turn_message", mock.AsyncMock(return_value="adopted")) as adopt:
            got = await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB)
        self.assertEqual(got, "adopted")
        self.assertEqual(adopt.await_args.kwargs["meta"]["openclaw_run"], "agent-run-1")

    async def test_a_live_writers_message_is_left_to_it(self):
        _register_turn_writer("1:alive")
        self.addCleanup(_retire_turn_writer, "1:alive")
        self._stub()["meta"] = {"openclaw_writer": "1:alive", "openclaw_run": "agent-run-1"}
        with mock.patch("openclaw_pipe._adopt_turn_message", mock.AsyncMock()) as adopt:
            self.assertEqual(await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB), "skip")
        adopt.assert_not_awaited()

    async def test_a_message_that_never_reached_the_gateway_gets_an_honest_note(self):
        self.history, self.active = {"messages": []}, False
        await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB)
        self.assertIs(self._stub()["done"], True)
        self.assertEqual(self._stub()["content"], ORPHAN_STUB_NOTE)

    async def test_content_a_stub_already_shows_is_not_replaced_on_a_text_match(self):
        self.history, self.active = _turn_history(run="msg-c-1.0"), False
        self._stub()["content"] = "what streamed before"
        await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB)
        self.assertIs(self._stub()["done"], True)
        self.assertEqual(self._stub()["content"], "what streamed before")
        self.assertNotIn("output", self._stub())

    async def test_content_is_replaced_when_the_run_matches_by_identity(self):
        self.history, self.active = _turn_history(), False
        self._stub()["content"] = "Reading th"
        await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB)
        self.assertEqual(self._stub()["content"], "Reading them.\n\nFound it.")

    async def test_a_stub_with_content_and_no_run_is_closed_as_is(self):
        self.history, self.active = {"messages": []}, False
        self._stub()["content"] = "partial"
        await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB)
        self.assertEqual(self._stub()["content"], "partial")
        self.assertIs(self._stub()["done"], True)

    async def test_without_wait_a_running_run_is_reported_not_awaited(self):
        self.assertEqual(
            await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB, wait=False), "wait")
        self.assertIs(self._stub()["done"], False)

    async def test_a_stub_a_live_turn_writer_took_over_is_left_to_it(self):
        """ELI-100: a turn writer that took the stub over during the read finishes it."""
        self.history, self.active = _turn_history(), False
        self._stub()["meta"] = {"openclaw_writer": "writer-live", "openclaw_run": self.RUN}
        _register_turn_writer("writer-live")
        self.assertEqual(await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB), "skip")
        self.assertIs(self._stub()["done"], False)
        self.assertEqual(self._stub()["content"], "")

    async def test_a_stub_no_request_has_claimed_is_left_alone(self):
        """ELI-156: after an Open WebUI restart the sweep can run before a stub's request has a
        task. A stub with no task id written since this process started is not an orphan."""
        self.history, self.active = {"messages": []}, False
        del self._stub()["meta"]
        self._stub()["timestamp"] = 1_000_100
        with mock.patch("openclaw_pipe._owui_process_started_at", return_value=1_000_000.5):
            self.assertEqual(await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB), "skip")
        self.assertIs(self._stub()["done"], False)
        self.assertEqual(self._stub()["content"], "")

    async def test_a_stub_older_than_this_process_with_no_task_is_an_orphan(self):
        """ELI-156: an older stub with no task id has no request left to fill it."""
        self.history, self.active = {"messages": []}, False
        del self._stub()["meta"]
        self._stub()["timestamp"] = 999_990
        with mock.patch("openclaw_pipe._owui_process_started_at", return_value=1_000_000.5):
            self.assertEqual(await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB), "done")
        self.assertIs(self._stub()["done"], True)
        self.assertEqual(self._stub()["content"], ORPHAN_STUB_NOTE)

    async def test_a_stub_from_the_second_this_process_started_is_not_counted_as_older(self):
        self.history, self.active = {"messages": []}, False
        del self._stub()["meta"]
        self._stub()["timestamp"] = 1_000_000
        with mock.patch("openclaw_pipe._owui_process_started_at", return_value=1_000_000.5):
            self.assertEqual(await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB), "skip")

    def test_the_process_start_is_read_from_proc(self):
        started = openclaw_pipe._owui_process_started_at()
        if not os.path.exists("/proc/self/stat"):
            self.assertIsNone(started)
            return
        self.assertIsNotNone(started)
        self.assertLessEqual(started, time.time() + 1)
        self.assertGreater(started, time.time() - 7 * 86400)

    async def test_a_request_that_claims_the_stub_meanwhile_keeps_it(self):
        """ELI-156: a stub Open WebUI claims during the read belongs to that request."""
        self.history, self.active = {"messages": []}, False

        def claim():
            self._stub()["meta"] = {"task_id": "task-new"}
        self.on_history = claim
        self.assertEqual(await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB), "skip")
        self.assertIs(self._stub()["done"], False)
        self.assertEqual(self._stub()["content"], "")

    async def test_a_reply_still_streaming_in_this_process_is_left_alone(self):
        self.history, self.active, self.live_task = _turn_history(), False, True
        await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB)
        self.assertIs(self._stub()["done"], False)
        self.assertEqual(self.sio.emit.await_count, 0)

class RelayOutputEventTests(unittest.TestCase):
    """`_relay_output_events`: events taking `message.output` to the renderer's items."""

    @staticmethod
    def _msg(mid, text):
        return {"type": "message", "id": mid, "status": "in_progress", "role": "assistant",
                "content": [{"type": "output_text", "text": text}]}

    def test_new_message_is_added_empty_then_filled_by_a_delta(self):
        evs = _relay_output_events([], [self._msg("m1", "Hi")])
        self.assertEqual([e["type"] for e in evs],
                         ["response.output_item.added", "response.output_text.delta"])
        self.assertEqual(evs[0]["output_index"], 0)
        self.assertEqual(evs[0]["item"]["content"][0]["text"], "")
        self.assertEqual((evs[1]["item_id"], evs[1]["delta"]), ("m1", "Hi"))

    def test_grown_text_is_only_the_new_part(self):
        evs = _relay_output_events([self._msg("m1", "Hi")], [self._msg("m1", "Hi there")])
        self.assertEqual([(e["type"], e.get("delta")) for e in evs],
                         [("response.output_text.delta", " there")])

    def test_changed_tool_item_is_replaced_and_new_items_are_indexed(self):
        call = {"type": "function_call", "id": "t1", "name": "Bash", "status": "in_progress"}
        done = {**call, "status": "completed"}
        out = {"type": "function_call_output", "id": "fco_t1", "call_id": "t1"}
        evs = _relay_output_events([self._msg("m1", "a"), call],
                                   [self._msg("m1", "a"), done, out])
        self.assertEqual([(e["type"], e["output_index"]) for e in evs],
                         [("response.output_item.done", 1), ("response.output_item.added", 2)])

    def test_nothing_changed_sends_nothing(self):
        same = [self._msg("m1", "a")]
        self.assertEqual(_relay_output_events(same, copy.deepcopy(same)), [])


class TurnRendererOutputTests(unittest.TestCase):
    """The renderer keeps a run as OWUI 0.12 stores a reply: text-only `text` and Responses
    `output` items (ELI-92)."""

    def test_text_and_tools_interleave_as_items(self):
        r = _TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "Looking."}})
        r.feed({"stream": "tool", "data": {"phase": "start", "name": "Read",
                                           "toolCallId": "t1", "args": {"p": 1}}})
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "Read",
                                           "toolCallId": "t1", "result": "x"}})
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "Read",
                                           "toolCallId": "t1", "result": "x"}})
        r.feed({"stream": "assistant", "data": {"delta": " Found it."}})
        self.assertEqual([i["type"] for i in r.output],
                         ["message", "function_call", "function_call_output", "message"])
        self.assertEqual(r.text, "Looking. Found it.")
        self.assertEqual(r.output[1]["status"], "completed")
        self.assertEqual(r.output[1]["arguments"], json.dumps({"p": 1}))

    # ELI-219: a claude-cli run starts a background Agent, answers NO_REPLY, and goes on once the
    # task reports. The sentinel and the closer's repeat must not reach the reply.
    ANSWER = "The background sub-agent finished and answered: SUB-51"

    def feed_eli219(self, r):
        r.feed({"stream": "tool", "data": {"phase": "start", "name": "Agent", "toolCallId": "t1",
                                           "args": {"run_in_background": True}}})
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "Agent", "toolCallId": "t1",
                                           "result": "Async agent launched successfully."}})
        r.feed({"stream": "assistant", "data": {"delta": "NO_REPLY"}})
        r.feed({"stream": "assistant", "data": {"delta": "\n\n" + self.ANSWER}})
        r.feed({"stream": "item", "data": {"kind": "message", "text": self.ANSWER}})
        r.feed({"stream": "item", "data": {"kind": "message", "text": "NO_REPLY\n" + self.ANSWER}})
        r.flush()

    def test_a_run_that_answered_no_reply_then_its_task_reported_shows_its_answer_once(self):
        r = _TurnRenderer()
        self.feed_eli219(r)
        self.assertEqual(r.text, self.ANSWER)
        texts = [i["content"][0]["text"] for i in r.output if i["type"] == "message"]
        self.assertEqual(texts, [self.ANSWER])

    # ELI-219: short answer, no item events; the closer joins by one newline.
    def test_a_closer_joining_the_messages_by_one_newline_is_not_shown_again(self):
        r = _TurnRenderer()
        r.feed({"stream": "tool", "data": {"phase": "start", "name": "Agent", "toolCallId": "t1",
                                           "args": {"run_in_background": True}}})
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "Agent", "toolCallId": "t1",
                                           "result": "Async agent launched successfully."}})
        for delta in ("NO_REPLY", "\n\nSUB", "-51"):
            r.feed({"stream": "assistant", "data": {"itemId": "cli-assistant:r1", "text": "", "delta": delta}})
        r.feed({"stream": "assistant", "data": {"assistantMessageIndex": 1, "completedText": "SUB-51"}})
        r.feed({"stream": "assistant", "data": {"itemId": "cli-assistant:r1", "text": "NO_REPLY\nSUB-51"}})
        r.flush()
        self.assertEqual(r.text, "SUB-51")
        texts = [i["content"][0]["text"] for i in r.output if i["type"] == "message"]
        self.assertEqual(texts, ["SUB-51"])

    def test_a_sentinel_split_across_deltas_stays_out(self):
        r = _TurnRenderer()
        for d in ("NO", "_RE", "PLY"):
            r.feed({"stream": "assistant", "data": {"delta": d}})
        r.flush()
        self.assertEqual((r.text, r.output), ("", []))

    def test_a_short_reply_that_starts_like_a_sentinel_still_shows(self):
        r = _TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "NO"}})
        self.assertEqual(r.text, "", "held while it may be a sentinel")
        self.assertEqual(r.flush(), "NO")
        self.assertEqual(r.text, "NO")
        r = _TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "NO"}})
        r.feed({"stream": "assistant", "data": {"delta": " way, that is wrong."}})
        self.assertEqual(r.text, "NO way, that is wrong.")

    def test_a_sentinel_before_a_card_is_dropped_and_a_held_word_is_kept(self):
        r = _TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "NO_REPLY"}})
        r.feed({"stream": "tool", "data": {"phase": "start", "name": "Read", "toolCallId": "t1", "args": {}}})
        self.assertEqual([i["type"] for i in r.output], ["function_call"])
        r = _TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "NO"}})
        added = r.feed({"stream": "tool", "data": {"phase": "start", "name": "Read", "toolCallId": "t1", "args": {}}})
        self.assertEqual(added[0], "NO", "the held word goes out ahead of the card")
        self.assertEqual([i["type"] for i in r.output], ["message", "function_call"])

    def test_failed_call_is_marked_and_explained(self):
        r = _TurnRenderer()
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "Bash",
                                           "toolCallId": "t2", "result": "boom",
                                           "isError": True}})
        self.assertEqual(r.output[0]["name"], f"Bash {TOOL_ERROR_MARK}")
        self.assertIn("tool call failed", r.output[1]["output"][0]["text"])

    def test_drop_shown_cuts_the_text_and_the_cards_another_message_holds(self):
        r = _TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "One."}})
        r.feed({"stream": "tool", "data": {"phase": "start", "name": "Read", "toolCallId": "t1", "args": {}}})
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "Read", "toolCallId": "t1", "result": "x"}})
        r.feed({"stream": "assistant", "data": {"delta": "Two. Three."}})
        r.feed({"stream": "tool", "data": {"phase": "start", "name": "Bash", "toolCallId": "t2", "args": {}}})
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "Bash", "toolCallId": "t2", "result": "y"}})
        self.assertFalse(r.drop_shown("One!"), "a start that does not line up changes nothing")
        self.assertEqual(len(r.output), 6)
        self.assertTrue(r.drop_shown("One.Two. "))
        self.assertEqual(r.text, "Three.")
        self.assertEqual([(i["type"], i.get("id") or i.get("call_id")) for i in r.output[1:]],
                         [("function_call", "t2"), ("function_call_output", "fco_t2")])
        self.assertEqual(r.output[0]["content"][0]["text"], "Three.")

    def test_finished_output_completes_open_items_without_touching_the_live_copy(self):
        r = _TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "Hi"}})
        r.feed({"stream": "tool", "data": {"phase": "start", "name": "Bash",
                                           "toolCallId": "t3", "args": {}}})
        done = r.finished_output()
        self.assertEqual({i["status"] for i in done}, {"completed"})
        self.assertEqual(done[-1]["call_id"], "t3", "the call the run never answered is closed")
        self.assertEqual(done[-1]["output"][0]["text"], TOOL_NO_RESULT_NOTE)
        self.assertEqual(r.output[1]["status"], "in_progress")
        self.assertEqual(len(r.output), 2)


class DeferredToolCallCardTests(unittest.TestCase):
    """ELI-164: OpenClaw 2026.10 calls deferred tools through a wrapper (`tool_call` on 10.1 to
    10.4, `dispatch_action` from 10.10) and reports the target as a nested call. One action is one
    card, named after the real tool."""

    INNER = "Model: fake/e2e-default"

    def envelope(self, target, text):
        return {"content": [{"type": "text", "text": json.dumps(
            {"tool": {"id": f"openclaw:core:{target}", "name": target, "source": "openclaw"},
             "result": {"content": [{"type": "text", "text": text}]}}, indent=2)}]}

    def live(self, wrapper="dispatch_action", target="session_status", args=None,
             inner_result=None, parent_field=True):
        inner_result = self.INNER if inner_result is None else inner_result
        nested = f"tool_call:call_1:{target}:1"
        link = {"parentToolCallId": "call_1"} if parent_field else {}
        r, events = _TurnRenderer(), []
        for data in (
            {"phase": "start", "name": wrapper, "toolCallId": "call_1",
             "args": {"id": f"openclaw:core:{target}", "args": args or {}}},
            {"phase": "start", "name": target, "toolCallId": nested, "args": args or {}, **link},
            {"phase": "result", "name": target, "toolCallId": nested,
             "result": {"content": [{"type": "text", "text": inner_result}]}, **link},
            {"phase": "result", "name": wrapper, "toolCallId": "call_1",
             "result": self.envelope(target, inner_result)},
        ):
            events += r.feed({"stream": "tool", "data": data})
        return r, events

    def cards(self, r):
        return [(i["type"], i.get("call_id") or i.get("id"), i.get("name")) for i in r.output
                if i["type"] in ("function_call", "function_call_output")]

    def test_one_card_named_after_the_target(self):
        for wrapper in ("dispatch_action", "tool_call"):
            with self.subTest(wrapper):
                r, events = self.live(wrapper)
                self.assertEqual(self.cards(r), [("function_call", "call_1", "session_status"),
                                                 ("function_call_output", "call_1", None)])
                started = [e["item"]["name"] for e in events
                           if isinstance(e, dict) and e["item"]["type"] == "function_call"]
                self.assertEqual(started, ["session_status"], "named so from its start")
                call = r.output[0]
                self.assertEqual(json.loads(call["arguments"]), {})
                self.assertEqual(r.output[1]["output"][0]["text"], self.INNER,
                                 "the card shows the target's result, not the wrapper's envelope")

    def test_a_nested_call_known_only_by_its_id_is_still_folded(self):
        r, _ = self.live(parent_field=False)
        self.assertEqual([c[1] for c in self.cards(r)], ["call_1", "call_1"])

    def test_a_spawn_through_the_wrapper_is_a_spawn_card(self):
        receipt = json.dumps({"status": "accepted", "childSessionKey": "agent:main:subagent:x", "runId": "r"})
        r, _ = self.live(target="sessions_spawn", args={"task": "Look it up.", "label": "Lookup"},
                         inner_result=receipt)
        self.assertEqual(r.output[0]["name"], "🤖 Lookup")
        self.assertEqual(r.output[1]["output"][0]["text"], "Started: Lookup")

    def test_the_rebuild_from_history_is_the_same_card(self):
        history = [
            {"role": "assistant", "content": [
                {"type": "toolCall", "id": "call_1", "name": "dispatch_action",
                 "arguments": {"id": "session_status", "args": {}}}]},
            # 10.10 chat.history: the nested call as its own entry (role "custom").
            {"role": "custom", "content": [
                {"type": "toolCall", "id": "tool_call:call_1:session_status:1", "name": "session_status",
                 "arguments": {}, "parentToolCallId": "call_1"},
                {"type": "toolResult", "toolCallId": "tool_call:call_1:session_status:1",
                 "parentToolCallId": "call_1", "content": [{"type": "text", "text": self.INNER}]}]},
            {"role": "toolResult", "toolCallId": "call_1", "toolName": "dispatch_action",
             "content": self.envelope("session_status", self.INNER)["content"]},
        ]
        rebuilt = _history_renderer(history)
        live, _ = self.live()
        self.assertEqual(self.cards(rebuilt), self.cards(live))
        self.assertEqual(rebuilt.output[1]["output"], live.output[1]["output"])

    def test_a_wrapper_result_that_is_not_an_envelope_shows_as_it_came(self):
        r = _TurnRenderer()
        r.feed({"stream": "tool", "data": {"phase": "start", "name": "dispatch_action", "toolCallId": "c",
                                           "args": {"id": "web_fetch", "args": {"url": "x"}}}})
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "dispatch_action", "toolCallId": "c",
                                           "result": "<<<EXTERNAL_UNTRUSTED_CONTENT>>> page"}})
        self.assertEqual(r.output[0]["name"], "web_fetch")
        self.assertEqual(json.loads(r.output[0]["arguments"]), {"url": "x"})
        self.assertEqual(r.output[1]["output"][0]["text"], "<<<EXTERNAL_UNTRUSTED_CONTENT>>> page")


class OpenClawMcpToolNameTests(unittest.TestCase):
    """ELI-164: claude-cli calls OpenClaw's tools over MCP. Live events name them bare (`exec`),
    the transcript keeps `mcp__openclaw__exec`; both give the same card title."""

    def test_a_rebuilt_card_is_named_as_it_streamed(self):
        args = {"command": "sleep 30; echo done", "yieldMs": 10000}
        live = _TurnRenderer()
        live.feed({"stream": "tool", "data": {"phase": "start", "name": "exec", "toolCallId": "toolu_1", "args": args}})
        live.feed({"stream": "tool", "data": {"phase": "result", "name": "exec", "toolCallId": "toolu_1",
                                              "result": "Command still running (session calm-reef)."}})
        rebuilt = _history_renderer([{"role": "assistant", "content": [
            {"type": "toolcall", "id": "toolu_1", "name": "mcp__openclaw__exec", "arguments": args},
            {"type": "tool_result", "tool_use_id": "toolu_1", "name": "mcp__openclaw__exec",
             "content": "Command still running (session calm-reef)."}]}])
        names = lambda r: [i.get("name") for i in r.output if i["type"] == "function_call"]
        self.assertEqual(names(live), ["exec"])
        self.assertEqual(names(rebuilt), names(live))
        self.assertEqual(rebuilt.output, live.output)

    def test_other_mcp_servers_keep_their_names(self):
        r = _TurnRenderer()
        r.feed({"stream": "tool", "data": {"phase": "start", "name": "mcp__github__get_issue",
                                           "toolCallId": "t", "args": {}}})
        self.assertEqual(r.output[0]["name"], "mcp__github__get_issue")


class SpawnCardTests(unittest.TestCase):
    """ELI-155: a sessions_spawn card is named after the sub-agent, shows the head of its task and
    one line of outcome, streamed or rebuilt."""

    LONG_TASK = ("Repo: the forecast-bench repository, re-clone it into your scratch folder.\n"
                 "Workflow .github/workflows/bench.yml, code in experiments/bench/.\n"
                 "Goal: a head-to-head, net of noise, on the same hourly data.\n"
                 "Split 2021-06/2024-07 in-sample, the rest out-of-sample.\n"
                 + "Report one table per split with MAE and RMSE. " * 20)
    RECEIPT = {"status": "accepted", "childSessionKey": "agent:main:subagent:1d33ab12-9b17-4fed-a36f-7f2031dbcb32",
               "runId": "5e93c66a-a9a8-41f4-8884-2073fc656900", "mode": "run"}

    def live(self, name, args, result, is_error=False):
        r = _TurnRenderer()
        events = r.feed({"stream": "tool", "data": {"phase": "start", "name": name,
                                                    "toolCallId": "t1", "args": args}})
        events += r.feed({"stream": "tool", "data": {"phase": "result", "name": name, "toolCallId": "t1",
                                                     "result": result, "isError": is_error}})
        return r, events

    def card(self, r):
        call = next(i for i in r.output if i["type"] == "function_call")
        out = next(i for i in r.output if i["type"] == "function_call_output")
        return call["name"], json.loads(call["arguments"]), out["output"][0]["text"]

    def test_a_labelled_spawn_is_named_after_its_label_with_the_head_of_its_task(self):
        for name in ("sessions_spawn", "mcp__openclaw__sessions_spawn"):
            r, events = self.live(name, {"task": self.LONG_TASK, "label": "Forecaster benchmark"},
                                  json.dumps(self.RECEIPT))
            title, args, output = self.card(r)
            self.assertEqual(title, "🤖 Forecaster benchmark", name)
            self.assertEqual(list(args), ["task"])
            self.assertTrue(args["task"].startswith("Repo: the forecast-bench"))
            self.assertLessEqual(len(args["task"]), openclaw_pipe.SPAWN_TASK_CHARS)
            self.assertNotIn("Report one table", args["task"])
            self.assertEqual(output, "Started: Forecaster benchmark")
            self.assertNotIn("1d33ab12", json.dumps(r.output), "no session key or run id in the card")
            started = [e["item"]["name"] for e in events
                       if isinstance(e, dict) and e["item"]["type"] == "function_call"]
            self.assertEqual(started, ["🤖 Forecaster benchmark"], "the live card is named so from its start")
            self.assertIn('name="🤖 Forecaster benchmark"', r.visible_text, "the markdown card too")

    def test_an_unlabelled_spawn_is_named_after_its_tasks_first_sentence(self):
        r, _ = self.live("sessions_spawn", {"task": "Price out Fly.io for the forecast app. One small machine."},
                         json.dumps(self.RECEIPT))
        self.assertEqual(self.card(r)[0], "🤖 Price out Fly.io for the forecast app.")
        r, _ = self.live("sessions_spawn", {"task": self.LONG_TASK}, json.dumps(self.RECEIPT))
        title = self.card(r)[0]
        self.assertTrue(title.startswith("🤖 Repo: the forecast-bench repository") and title.endswith("…"), title)
        self.assertLessEqual(len(title), len("🤖 ") + openclaw_pipe.SUBAGENT_TITLE_MAX)

    def test_a_short_task_is_shown_whole(self):
        r, _ = self.live("sessions_spawn", {"task": "Line one.\nLine two."}, json.dumps(self.RECEIPT))
        self.assertEqual(self.card(r)[1], {"task": "Line one.\nLine two."})

    def test_a_refused_spawn_is_marked_failed_with_its_reason(self):
        receipt = {"status": "forbidden", "errorCode": "agent_forbidden",
                   "error": "agentId research is not allowed"}
        r, events = self.live("sessions_spawn", {"task": "Look it up.", "label": "Lookup"}, json.dumps(receipt))
        title, _, output = self.card(r)
        self.assertEqual(title, f"🤖 Lookup {TOOL_ERROR_MARK}")
        self.assertIn("Not started (forbidden): agentId research is not allowed", output)
        self.assertNotIn("agent_forbidden", output)
        relabels = [e["item"]["name"] for e in events
                    if isinstance(e, dict) and e["item"]["type"] == "function_call"]
        self.assertEqual(relabels[-1], f"🤖 Lookup {TOOL_ERROR_MARK}", "the mark shows live")

    def test_a_tool_error_keeps_its_text(self):
        r, _ = self.live("sessions_spawn", {"task": "Go.", "label": "Go"}, "spawn timed out", is_error=True)
        title, _, output = self.card(r)
        self.assertEqual(title, f"🤖 Go {TOOL_ERROR_MARK}")
        self.assertIn("spawn timed out", output)

    def test_a_receipt_that_is_not_json_is_shown_as_it_came(self):
        r, _ = self.live("sessions_spawn", {"task": "Go.", "label": "Go"}, "started")
        self.assertEqual(self.card(r)[2], "started")

    def test_other_tools_are_shown_as_called(self):
        r, _ = self.live("exec", {"command": "ls", "label": "x"}, json.dumps({"status": "accepted"}))
        self.assertEqual(self.card(r), ("exec", {"command": "ls", "label": "x"}, json.dumps({"status": "accepted"})))

    def test_the_rebuilt_card_is_the_streamed_card(self):
        """claude-cli's `tool_result` has no tool name; it still answers a spawn."""
        args = {"task": self.LONG_TASK, "label": "Forecaster benchmark"}
        streamed, _ = self.live("mcp__openclaw__sessions_spawn", args, json.dumps(self.RECEIPT))
        rebuilt = _history_renderer([{"role": "assistant", "content": [
            {"type": "toolcall", "id": "t1", "name": "mcp__openclaw__sessions_spawn", "arguments": args},
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": json.dumps(self.RECEIPT)}]}]}])
        self.assertEqual(self.card(rebuilt), self.card(streamed))
        native = _history_renderer([
            {"role": "assistant", "content": [{"type": "toolCall", "id": "t1", "name": "sessions_spawn",
                                               "arguments": args}]},
            {"role": "toolResult", "toolCallId": "t1", "toolName": "sessions_spawn",
             "content": [{"type": "text", "text": json.dumps(self.RECEIPT)}]}])
        self.assertEqual(self.card(native), self.card(streamed))


class ThinkingNoteAnnouncementTests(unittest.TestCase):
    """The clamp note shows once per change; the level is a standing setting."""

    NOTE = "This model does not support thinking level 'high', using 'off' instead."

    def test_the_same_note_is_shown_once_per_session(self):
        conn = _GatewayConnection(lambda: None)
        self.assertTrue(conn.should_announce_thinking_note("s", self.NOTE))
        for _ in range(3):
            self.assertFalse(conn.should_announce_thinking_note("s", self.NOTE))

    def test_a_changed_note_speaks_up_again(self):
        # A different level, model, or newly learned ladder changes the sentence, so each is news.
        conn = _GatewayConnection(lambda: None)
        conn.should_announce_thinking_note("s", self.NOTE)
        self.assertTrue(conn.should_announce_thinking_note(
            "s", "This model does not support thinking level 'max', using 'low' instead."))

    def test_sessions_do_not_silence_each_other(self):
        conn = _GatewayConnection(lambda: None)
        conn.should_announce_thinking_note("chat-a", self.NOTE)
        self.assertTrue(conn.should_announce_thinking_note("chat-b", self.NOTE))

    def test_an_empty_note_is_never_announced(self):
        self.assertFalse(
            _GatewayConnection(lambda: None).should_announce_thinking_note("s", ""))

    def test_the_pipe_gates_the_yield_on_it(self):
        # The note is still always logged.
        source = inspect.getsource(openclaw_pipe._PipeTurn)
        self.assertIn("should_announce_thinking_note(self.session_key, thinking_note)", source)
        self.assertIn('pipe_log(f"thinking: {thinking_note}")', source)


class OwuiTaskNoopTests(unittest.TestCase):
    """image_prompt_generation and function_calling are no-ops; moa_response_generation is not."""

    def _noop_task_values(self):
        source = inspect.getsource(openclaw_pipe._PipeTurn)
        m = re.search(
            r"if self\.__task__ and self\.__task__ in \(\s*((?:\"[a-z_]+\",?\s*)+)\)",
            source,
        )
        self.assertIsNotNone(m, "could not find the __task__ no-op tuple")
        return set(re.findall(r'"([a-z_]+)"', m.group(1)))

    def test_original_six_still_noop(self):
        values = self._noop_task_values()
        for task in (
            "title_generation",
            "tags_generation",
            "follow_up_generation",
            "emoji_generation",
            "autocomplete_generation",
            "query_generation",
        ):
            self.assertIn(task, values)

    def test_image_prompt_and_function_calling_now_noop(self):
        values = self._noop_task_values()
        self.assertIn("image_prompt_generation", values)
        self.assertIn("function_calling", values)

    def test_moa_response_generation_still_gets_a_full_agent_turn(self):
        values = self._noop_task_values()
        self.assertNotIn("moa_response_generation", values)


class ContinueResponseTests(unittest.IsolatedAsyncioTestCase):
    """ELI-184: Continue Response sends the agent's own reply last. OpenClaw cannot continue it, so
    the Pipe sends nothing, adds nothing, and says so in a toast."""

    async def _prepare(self, messages):
        events, connected = [], []

        async def emitter(event):
            events.append(event)

        async def get_conn(valves_getter):
            connected.append(True)
            raise openclaw_pipe.GatewayError("stop at the connection")

        pipe = openclaw_pipe.Pipe()
        turn = openclaw_pipe._PipeTurn(
            pipe, {"model": "openclaw_gateway.main", "messages": messages}, emitter, None, None,
            {"chat_id": "chat-1", "user_id": "u1"}, None, None, None, "", [], None)
        with mock.patch("openclaw_pipe._get_gateway_connection", get_conn), \
                mock.patch("openclaw_pipe._start_file_server", lambda: None), \
                mock.patch("openclaw_pipe._ensure_orphan_reconcile", lambda *a: None):
            yielded = [item async for item in turn.prepare()]
        return turn, yielded, events, connected

    async def test_a_continue_sends_nothing_and_adds_nothing_to_the_reply(self):
        turn, yielded, events, connected = await self._prepare([
            {"role": "user", "content": "count 1 to 5"},
            {"role": "assistant", "content": "1 2 3 4 5"},
        ])
        self.assertTrue(turn.ended)
        self.assertEqual(yielded, [])
        self.assertEqual(connected, [])
        notes = [e["data"] for e in events if e.get("type") == "notification"]
        self.assertEqual(len(notes), 1)
        self.assertIn("Continue", notes[0]["content"])
        self.assertEqual([e for e in events if e.get("type") != "notification"], [])

    async def test_a_user_message_last_still_goes_to_the_gateway(self):
        turn, yielded, events, connected = await self._prepare([
            {"role": "assistant", "content": "1 2 3 4 5"},
            {"role": "user", "content": "and to 10?"},
        ])
        self.assertEqual(connected, [True])
        self.assertEqual([e for e in events if e.get("type") == "notification"], [])


class EventConsumerMatchingTests(unittest.TestCase):
    def test_matches_exact_session_and_run(self):
        conn = _GatewayConnection(lambda: None)
        conn.register_consumer("session-a", "run-1")
        conn.register_consumer("session-a", "run-2")

        consumers = conn.consumers_for_event({
            "sessionKey": "session-a",
            "runId": "run-2",
        })

        self.assertEqual(len(consumers), 1)
        self.assertEqual(consumers[0].run_id, "run-2")

    def test_matches_session_only_event_when_unambiguous(self):
        conn = _GatewayConnection(lambda: None)
        conn.register_consumer("session-a", "run-1")

        consumers = conn.consumers_for_event({"sessionKey": "session-a"})

        self.assertEqual(len(consumers), 1)
        self.assertEqual(consumers[0].run_id, "run-1")

    def test_drops_session_only_event_when_ambiguous(self):
        conn = _GatewayConnection(lambda: None)
        conn.register_consumer("session-a", "run-1")
        conn.register_consumer("session-a", "run-2")

        consumers = conn.consumers_for_event({"sessionKey": "session-a"})

        self.assertEqual(consumers, [])

    def test_reports_sole_active_run_for_session(self):
        conn = _GatewayConnection(lambda: None)
        conn.register_consumer("session-a", "run-1")

        self.assertEqual(conn.active_run_id_for_session("session-a"), "run-1")

    def test_does_not_report_ambiguous_active_run(self):
        conn = _GatewayConnection(lambda: None)
        conn.register_consumer("session-a", "run-1")
        conn.register_consumer("session-a", "run-2")

        self.assertIsNone(conn.active_run_id_for_session("session-a"))

    def test_was_delivered_live_false_before_any_mark(self):
        conn = _GatewayConnection(lambda: None)
        self.assertFalse(conn.was_delivered_live("session-a", "run-1"))

    def test_was_delivered_live_true_after_mark(self):
        conn = _GatewayConnection(lambda: None)
        conn.mark_delivered_live("session-a", "run-1")
        self.assertTrue(conn.was_delivered_live("session-a", "run-1"))

    def test_was_delivered_live_is_scoped_to_exact_session_and_run(self):
        """A live mark exempts only the exact (session, run) pair from proactive delivery."""
        conn = _GatewayConnection(lambda: None)
        conn.mark_delivered_live("session-a", "run-1")
        self.assertFalse(conn.was_delivered_live("session-a", "run-2"))
        self.assertFalse(conn.was_delivered_live("session-b", "run-1"))

class StripInputMarkerLineTests(unittest.TestCase):
    """A needs-input prompt never leaks its "OpenClaw needs input:" line as chat text."""

    def test_strips_marker_line_keeps_question(self):
        text = "OpenClaw needs input:\n\nPick a color\n1. Red\n2. Blue"
        out = _strip_input_marker_line(text)
        self.assertNotIn("needs input:", out)
        self.assertIn("Pick a color", out)
        self.assertIn("1. Red", out)

    def test_strips_codex_marker(self):
        self.assertEqual(
            _strip_input_marker_line("Codex needs input:\nProceed?"), "Proceed?"
        )

    def test_strips_marker_with_leading_whitespace(self):
        out = _strip_input_marker_line("  \nOpenClaw needs input:\nQ?")
        self.assertNotIn("needs input:", out)
        self.assertIn("Q?", out)

    def test_leaves_normal_text_untouched(self):
        text = "Here is a normal answer with no marker."
        self.assertEqual(_strip_input_marker_line(text), text)

    def test_marker_only_becomes_empty(self):
        self.assertEqual(_strip_input_marker_line("OpenClaw needs input:"), "")

    def test_a_question_on_the_marker_line_is_kept(self):
        """ELI-151: the question written on the marker line is the question."""
        self.assertEqual(_strip_input_marker_line("OpenClaw needs input: Which branch?\n1. main\n2. dev"),
                         "Which branch?\n1. main\n2. dev")
        self.assertEqual(_strip_input_marker_line("Codex needs input:  Proceed?"), "Proceed?")

    def test_empty_input(self):
        self.assertEqual(_strip_input_marker_line(""), "")


class ParkedCancelTests(unittest.IsolatedAsyncioTestCase):
    """OWUI 0.12's Send now cancels, then sends the new message about 0.5 s later; the parked turn
    waits a grace window for it. A plain Stop aborts after the window."""

    def _conn(self):
        conn = _GatewayConnection(lambda: None)
        calls = []

        async def fake_abort(session_key, run_id):
            calls.append(("abort", session_key, run_id))

        async def fake_stop(session_key):
            calls.append(("stop", session_key))

        conn.abort = fake_abort
        conn.send_stop = fake_stop
        return conn, calls

    async def test_unclaimed_cancel_stops_after_grace(self):
        # A plain Stop: the run is stopped and its queue released.
        conn, calls = self._conn()
        q = conn.register_consumer("s", "run-1")
        conn.park_cancelled_turn("s", "run-1", q, [], 0.05, send_stop=True)
        await asyncio.sleep(0)
        self.assertEqual(calls, [], "abort must wait for the grace window")
        self.assertTrue(conn._consumers, "the queue keeps buffering meanwhile")
        await asyncio.sleep(0.15)
        self.assertEqual(calls, [("abort", "s", "run-1"), ("stop", "s")])
        self.assertEqual(conn._consumers, {})
        self.assertIsNone(await conn.claim_cancelled_turn("s"))

    async def test_claim_inside_grace_takes_run_queue_and_messages(self):
        conn, calls = self._conn()
        q = conn.register_consumer("s", "run-1")
        conn.park_cancelled_turn("s", "run-1", q, ["test"], 0.05, send_stop=True)
        claimed = await conn.claim_cancelled_turn("s")
        self.assertEqual(claimed["run_id"], "run-1")
        self.assertIs(claimed["queue"], q)
        self.assertEqual(claimed["carried"], ["test"])
        await asyncio.sleep(0.15)
        self.assertEqual(calls, [], "a claimed run must never be aborted")
        self.assertIn("s:run-1", conn._consumers, "a claimed queue stays registered")

    async def test_zero_grace_stops_immediately(self):
        conn, calls = self._conn()
        q = conn.register_consumer("s", "run-1")
        conn.park_cancelled_turn("s", "run-1", q, [], 0, send_stop=False)
        await asyncio.sleep(0)
        self.assertEqual(calls, [("abort", "s", "run-1")])
        self.assertEqual(conn._consumers, {})
        self.assertIsNone(await conn.claim_cancelled_turn("s"))

    async def test_claim_is_per_session(self):
        conn, calls = self._conn()
        conn.park_cancelled_turn("s", "run-1", None, [], 0.05, send_stop=False)
        self.assertIsNone(await conn.claim_cancelled_turn("other"))
        await asyncio.sleep(0.15)
        self.assertEqual(calls, [("abort", "s", "run-1")])

    async def test_second_park_stops_the_older_run_but_keeps_its_messages(self):
        conn, calls = self._conn()
        conn.park_cancelled_turn("s", "run-1", None, ["a"], 5, send_stop=False)
        conn.park_cancelled_turn("s", "run-2", None, ["b"], 5, send_stop=False)
        await asyncio.sleep(0)
        self.assertEqual(calls, [("abort", "s", "run-1")])
        claimed = await conn.claim_cancelled_turn("s")
        self.assertEqual(claimed["run_id"], "run-2")
        self.assertEqual(claimed["carried"], ["a", "b"])

    async def test_parking_a_message_with_no_run(self):
        # Cancelled while queued: nothing to abort, but the message reaches the next reply.
        conn, calls = self._conn()
        conn.park_cancelled_turn("s", None, None, ["hello"], 0.05, send_stop=True)
        self.assertEqual((await conn.claim_cancelled_turn("s"))["carried"], ["hello"])
        await asyncio.sleep(0.1)
        self.assertEqual(calls, [])

    async def test_parked_run_is_not_backfilled_or_redelivered(self):
        # The stopped message keeps what it showed: no parity finalize, no proactive bubble.
        conn, _ = self._conn()
        conn.register_run_target("s", "run-1", "chat", "msg-1")
        conn.park_cancelled_turn("s", "run-1", None, [], 5, send_stop=False)
        self.assertNotIn("s:run-1", conn._run_targets)
        self.assertTrue(conn.was_delivered_live("s", "run-1"))
        await conn.claim_cancelled_turn("s")

    async def test_seeded_renderer_keeps_the_run_baseline(self):
        conn, _ = self._conn()
        q = conn.register_consumer("s", "run-1")
        q.put_nowait({"payload": {"stream": "assistant", "data": {"delta": "2\n"}}})
        conn.register_run_target("s", "run-1", "chat", "msg-2")
        conn.seed_run_renderer("s", "run-1", q,
                               run_text={"received": "1\n", "stream": "1\n", "item": ""})
        r = conn._run_renderers["s:run-1"]
        r.feed({"stream": "assistant", "data": {"text": "1\n2\n"}})
        self.assertEqual(r.visible_text, "2\n")

    async def test_seed_renderer_from_buffered_queue(self):
        conn, _ = self._conn()
        q = conn.register_consumer("s", "run-1")
        q.put_nowait({"payload": {"stream": "assistant", "data": {"delta": "185\n"}}})
        conn.register_run_target("s", "run-1", "chat", "msg-2")
        conn.seed_run_renderer("s", "run-1", q)
        self.assertEqual(conn._run_renderers["s:run-1"].visible_text, "185\n")
        self.assertEqual(q.qsize(), 1, "seeding must not consume the queue")

    async def test_stop_intent_stops_at_once(self):
        conn, calls = self._conn()
        q = conn.register_consumer("s", "run-1")
        conn.park_cancelled_turn("s", "run-1", q, ["x"], 30, send_stop=False)
        conn.settle_parked_turn("s", "run-1", "stop", hold_s=120)
        await asyncio.sleep(0)
        self.assertEqual(calls, [("abort", "s", "run-1")])
        self.assertIsNone(await conn.claim_cancelled_turn("s"))

    async def test_send_now_intent_outlasts_the_fallback_window(self):
        # With the page's answer, a slow follow-up races no timer.
        conn, calls = self._conn()
        conn.park_cancelled_turn("s", "run-1", None, [], 0.05, send_stop=False)
        conn.settle_parked_turn("s", "run-1", "send_now", hold_s=5)
        await asyncio.sleep(0.2)
        self.assertEqual(calls, [])
        self.assertEqual((await conn.claim_cancelled_turn("s"))["run_id"], "run-1")

    async def test_unknown_intent_keeps_the_fallback(self):
        conn, calls = self._conn()
        conn.park_cancelled_turn("s", "run-1", None, [], 0.05, send_stop=False)
        conn.settle_parked_turn("s", "run-1", None, hold_s=5)
        await asyncio.sleep(0.15)
        self.assertEqual(calls, [("abort", "s", "run-1")])

    @staticmethod
    async def _spin():
        for _ in range(50):
            await asyncio.sleep(0)

    def _park_asking(self, conn, run_id="run-1"):
        """Park `run_id` with a settle whose page answer the test gives."""
        answer = asyncio.get_running_loop().create_future()

        async def settle():
            conn.settle_parked_turn("s", run_id, await answer, hold_s=120)
        conn.park_cancelled_turn("s", run_id, None, [], 30, send_stop=False, settle=settle)
        return answer

    async def test_claim_waits_for_a_stop_answer_in_flight(self):
        # After a Stop the next queued message starts fresh.
        conn, calls = self._conn()
        answer = self._park_asking(conn)
        claim = asyncio.ensure_future(conn.claim_cancelled_turn("s"))
        await self._spin()
        self.assertFalse(claim.done(), "the claim waits for the page's answer, however long")
        answer.set_result("stop")
        self.assertIsNone(await claim)
        self.assertEqual(calls, [("abort", "s", "run-1")])

    async def test_claim_takes_the_run_on_a_send_now_answer(self):
        conn, calls = self._conn()
        answer = self._park_asking(conn)
        claim = asyncio.ensure_future(conn.claim_cancelled_turn("s"))
        await self._spin()
        self.assertFalse(claim.done())
        answer.set_result("send_now")
        self.assertEqual((await claim)["run_id"], "run-1")
        self.assertEqual(calls, [])

    async def test_a_park_nobody_can_settle_is_resolved_at_once(self):
        """No page to ask (API use): the fallback window decides at once."""
        conn, _ = self._conn()
        conn.park_cancelled_turn("s", "run-1", None, [], 30, send_stop=False)
        self.assertTrue(conn._pending_cancel_aborts["s"]["resolved"].is_set())

    async def test_a_settle_that_fails_still_resolves(self):
        conn, _ = self._conn()

        async def settle():
            raise RuntimeError("page gone")
        conn.park_cancelled_turn("s", "run-1", None, [], 30, send_stop=False, settle=settle)
        claim = asyncio.ensure_future(conn.claim_cancelled_turn("s"))
        self.assertEqual((await claim)["run_id"], "run-1")

    def _park_with_reply(self, conn, carried=()):
        """Park run-1 for a reply showing m1, with one post-cancel event buffered."""
        q = conn.register_consumer("s", "run-1")
        q.put_nowait({"type": "event", "payload": {"stream": "tool", "data": {"phase": "result"}}})
        reply = {"message_id": "m1", "renderer": object()}
        conn.park_cancelled_turn("s", "run-1", q, list(carried), 30, send_stop=False, reply=reply)
        return reply

    async def test_send_now_keeps_the_run_in_its_own_message(self):
        """ELI-158: on Send now the turn writer keeps the run in its own message. The follow-up
        hands it over, not the page's answer, which Open WebUI's cancel event could overwrite
        (#83)."""
        conn, calls = self._conn()
        reply = self._park_with_reply(conn)
        with mock.patch.object(openclaw_pipe._TurnHooks, "hand_over", return_value=True) as hand:
            conn.settle_parked_turn("s", "run-1", "send_now", hold_s=5)
            hand.assert_not_called()
            claimed = await conn.claim_cancelled_turn("s")
        hand.assert_called_once()
        args, kwargs = hand.call_args
        self.assertEqual(args[1:], ("s", "run-1"))
        self.assertEqual(kwargs["reply"]["message_id"], "m1")
        self.assertIs(kwargs["reply"]["renderer"], reply["renderer"])
        self.assertEqual(len(kwargs["reply"]["events"]), 1, "the buffered event goes to the writer")
        self.assertNotIn("s:run-1", conn._consumers)
        self.assertIsNone(claimed["run_id"])
        await asyncio.sleep(0)
        self.assertEqual(calls, [], "the run is never aborted")

    async def test_messages_the_cancelled_reply_had_not_sent_still_reach_the_next(self):
        conn, _ = self._conn()
        self._park_with_reply(conn, carried=["first"])
        with mock.patch.object(openclaw_pipe._TurnHooks, "hand_over", return_value=True):
            conn.settle_parked_turn("s", "run-1", "send_now", hold_s=5)
            claimed = await conn.claim_cancelled_turn("s")
        self.assertIsNone(claimed["run_id"])
        self.assertEqual(claimed["carried"], ["first"])

    async def test_a_message_in_the_fallback_window_keeps_the_run_in_its_own_message(self):
        """No page answer: a message inside the window keeps the run, as Send now does."""
        conn, calls = self._conn()
        self._park_with_reply(conn)
        with mock.patch.object(openclaw_pipe._TurnHooks, "hand_over", return_value=True) as hand:
            claimed = await conn.claim_cancelled_turn("s")
        hand.assert_called_once()
        self.assertIsNone(claimed["run_id"])
        self.assertEqual(calls, [])

    def test_intent_query_names_its_chat(self):
        from openclaw_pipe import _intent_query_js
        self.assertIn('"chat-1"', _intent_query_js("chat-1"))

    def test_pipe_claims_before_model_patch(self):
        # The claim comes before sessions.patch (10 s+); the cancel handler parks.
        src = inspect.getsource(openclaw_pipe._PipeTurn)
        claim = src.index("await self.conn.claim_cancelled_turn(self.session_key)")
        patch = src.index("RUN_PATCH")
        self.assertLess(claim, patch)
        self.assertIn("conn.park_cancelled_turn(", src)
        self.assertNotIn("await self.conn.abort(", src)

    def test_grace_valve_defaults_on(self):
        self.assertGreater(Pipe.Valves().INTERRUPT_GRACE_SECONDS, 0)


class MarkStoppedTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_mark_is_written_at_once_and_again_after_the_request(self):
        """A Stop marks the reply at once and again after Open WebUI's request ends, so no `done:
        true` save lands after the mark (#282)."""
        writes = []

        async def set_meta(chat_id, message_id, meta):
            writes.append((chat_id, message_id, meta))
            return True
        request = asyncio.get_running_loop().create_future()
        with mock.patch.object(openclaw_pipe, "_set_turn_meta", set_meta):
            await asyncio.wait_for(openclaw_pipe._mark_stopped("c1", "m1", request), 1)
            self.assertEqual(len(writes), 1, "written at once, the request still running")
            request.set_result(None)
            for _ in range(20):
                await asyncio.sleep(0)
        self.assertEqual(writes, [("c1", "m1", {openclaw_pipe.STOPPED_META: True})] * 2)


class _TurnCase(unittest.IsolatedAsyncioTestCase):
    """A Pipe turn against a fake Gateway connection: the shared setup and stream helpers."""

    def _run_message(self, message, session_key=None):
        """The Gateway's `session.message` push of a stored message (ELI-381). A
        reply's consumer registers before its run starts, so a push held here
        lands once one does, as the run's own pushes come after its start."""
        payload = {"sessionKey": session_key or self.SK, "message": message}
        runs = self.ocp._transcript_run_ids(message)
        if any(self.conn._reply_follows(payload["sessionKey"], r) for r in runs):
            self.conn._on_session_message(payload)
            return
        if not hasattr(self, "_held_pushes"):
            self._held_pushes = []
            register = self.conn.register_consumer

            def register_then_push(session_key, run_id):
                q = register(session_key, run_id)
                for held in [h for h in self._held_pushes if h["sessionKey"] == session_key]:
                    if run_id in self.ocp._transcript_run_ids(held["message"]):
                        self._held_pushes.remove(held)
                        self.conn._on_session_message(held)
                return q
            self.conn.register_consumer = register_then_push
        self._held_pushes.append(payload)

    async def asyncSetUp(self):
        import openclaw_pipe as ocp
        self.ocp = ocp
        self.conn = _GatewayConnection(lambda: None)
        self.conn._ws = object()
        self.sent = []
        self.sent_images = []
        self.rpc = []
        self.aborts = []
        self.active = False
        self.run_ended_at = None
        self.describe_extra = {}
        self.list_active = None     # None: the list says what describe says
        self.crowded = False        # the session sits past the Gateway's first 100 rows
        self.runtime = None
        self.keys = []
        self.history = {}
        n = {"run": 0}

        async def send_request(method, params, timeout=10):
            self.rpc.append(method)
            if method == "chat.send":
                if getattr(self, "send_fails", None):
                    raise self.send_fails
                n["run"] += 1
                self.sent.append(params.get("message"))
                self.sent_images.append([(a["fileName"], a["content"]) for a in params.get("attachments") or []])
                self.keys.append(params.get("idempotencyKey"))
                return {"runId": f"run-{n['run']}"}
            if method == "chat.history":
                return copy.deepcopy(self.history)
            if method == "chat.abort":
                self.aborts.append(params)
                return {"aborted": True}
            if method == "sessions.describe":
                row = {"status": "running" if self.active else "done",
                       "agentRuntime": {"id": self.runtime}}
                if self.active and self.run_ended_at:
                    row["endedAt"] = self.run_ended_at
                row.update(self.describe_extra)
                return {"session": row}
            if method == "sessions.list":
                busy = self.active if self.list_active is None else self.list_active
                rows = crowd(self.SK, busy=busy) if self.crowded else [{"key": self.SK, "hasActiveRun": busy}]
                return PagedSessions(rows).list(params)
            return {}

        async def no_abort(session_key, run_id):
            self.rpc.append("abort")

        async def no_stop(session_key):
            self.rpc.append("stop")

        self.conn.send_request = send_request
        self.conn.abort = no_abort
        self.conn.send_stop = no_stop
        self.conn.model_patch_cached = lambda *a, **k: True

        async def get_conn(_valves):
            return self.conn

        async def noop(*a, **k):
            return None

        self.patches = [
            mock.patch.object(ocp, "_get_gateway_connection", get_conn),
            mock.patch.object(ocp, "_build_usage_status_lines", mock.AsyncMock(return_value=[])),
        ]
        for p in self.patches:
            p.start()
        self.pipe = Pipe()
        self.pipe.valves.INTERRUPT_GRACE_SECONDS = 0.3
        self.pipe.valves.AUTO_TITLE = False
        type(self).SK = self.ocp._owui_session_key(
            self.pipe.valves.AGENT_ID, "u1", "chat-1")

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()

    def _reply(self, text, msg_id, intent=None, image=None, chat="chat-1", thinking=None):
        """`image`: base64 data of a PNG attached to the message."""
        self.emitted = getattr(self, "emitted", [])

        async def emitter(event):
            self.emitted.append(event)

        async def event_call(event):
            # The page's answer to `_intent_query_js`.
            return intent

        content = text if image is None else [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}}]
        body = {"model": "openclaw_gateway.default",
                "messages": [{"role": "user", "content": content}]}
        if thinking:
            body["reasoning_effort"] = thinking
        meta = {"chat_id": chat, "user_id": "u1", "message_id": msg_id}
        return self.pipe._pipe_impl(body, emitter, __metadata__=meta,
                                    __event_call__=event_call if intent else None)

    def _queue(self, run_id):
        return self.conn._consumers[f"{self.SK}:{run_id}"].queues[0]

    def _delta(self, run_id, text):
        return {"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": run_id,
            "stream": "assistant", "data": {"delta": text}}}

    def _final(self, run_id):
        return {"type": "event", "event": "chat", "payload": {
            "sessionKey": self.SK, "runId": run_id, "state": "final"}}

    async def _until_registered(self, run_id):
        for _ in range(200):
            if f"{self.SK}:{run_id}" in self.conn._consumers:
                return
            await asyncio.sleep(0.005)
        self.fail(f"{run_id} never registered")


class NoDroppedMessagesTests(_TurnCase):
    """Drive real reply generators through OWUI's ways of ending a response. A generator close must
    not skip the cancel handler or lose lines in the gap before the next reply registers."""

    SK = None

    def _hold_parked_timers(self):
        """Hold the parked run's fallback timer (`INTERRUPT_GRACE_SECONDS`) so the test drives the
        claim (ELI-154). Returns the windows armed."""
        armed = []
        patch = mock.patch.object(self.conn, "_arm_parked_timer",
                                  lambda session_key, entry, wait_s: armed.append(wait_s))
        patch.start()
        self.addCleanup(patch.stop)
        return armed

    async def _read(self, gen, want):
        out = ""
        while want not in out:
            # Bounds a hang only (ELI-154).
            out += await asyncio.wait_for(gen.__anext__(), 30)
        return out

    @staticmethod
    async def _spin():
        """Let every ready task run until it blocks. No clock involved."""
        for _ in range(50):
            await asyncio.sleep(0)

    def _settled(self, active):
        self.active = active
        self.conn._on_sessions_changed({
            "sessionKey": self.SK, "hasActiveRun": active,
            "reason": "chat.run.started" if active else "chat.run.settled"})

    async def test_message_waits_for_the_pushed_settle_not_a_timer(self):
        """ELI-88: a message sent during a long run waits for the Gateway's pushed settle, however
        long, re-reads once per push (no polling), then sends once."""
        self.active = True
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._spin()
        self.assertEqual(self.sent, [])
        self.assertTrue(any("Queued: waiting" in str(e) for e in self.emitted))
        reads = self.rpc.count("sessions.describe")

        await self._spin()
        self.assertEqual(self.rpc.count("sessions.describe"), reads, "nothing pushed, nothing read")

        self._settled(True)  # the run moves on, still busy
        await self._spin()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.rpc.count("sessions.describe"), reads + 1)

        self._settled(False)
        await self._until_registered("run-1")
        self.assertEqual(self.sent, ["hello"])
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "hi there"))
        q.put_nowait(self._final("run-1"))
        out = await asyncio.wait_for(first, 2)
        async for chunk in gen:
            out += chunk if isinstance(chunk, str) else ""
        self.assertIn("hi there", out)

    def _gateway_push(self, **fields):
        """A `sessions.changed` for the chat's session, with exactly these fields."""
        self.conn._on_sessions_changed({"sessionKey": self.SK, **fields})

    async def test_a_message_queued_behind_a_wake_leaves_on_the_push_that_announces_the_idle(self):
        """2026.10.10: a wake's end push still says `hasActiveRun: true` and the idle push has
        none. Any push wakes the waiter; the row decides."""
        self.run_ended_at = 1793702659908
        self.active = True
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._spin()
        self.assertEqual(self.sent, [])
        self.assertTrue(any("Queued: waiting" in str(e) for e in self.emitted))

        self._gateway_push(phase="end", runId="wake-1", hasActiveRun=True, status="running")
        await self._spin()
        self.assertEqual(self.sent, [], "the end push still counts the run")

        self.active = False                       # cleared on the Gateway
        self._gateway_push(reason="cron-binding", status="done",
                            session={"status": "done", "hasActiveRun": False, "activeRunIds": []})
        await self._spin()
        self.assertEqual(self.sent, ["hello"], "left on the push that carries no hasActiveRun")
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "hi there"))
        q.put_nowait(self._final("run-1"))
        out = await asyncio.wait_for(first, 5)
        async for chunk in gen:
            out += chunk if isinstance(chunk, str) else ""
        self.assertIn("hi there", out)

    async def test_a_row_that_looks_ended_is_not_released_while_the_gateway_counts_a_run(self):
        """ELI-88: a run admitted but not started looks ended (old `endedAt`, no run id). Nothing
        is sent until the Gateway's own flag says idle."""
        self.run_ended_at = 1793702659908
        self.active = True
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._spin()
        reads = self.rpc.count("sessions.describe")
        for push in ({"reason": "participants", "hasActiveRun": True, "status": "running"},
                     {"reason": "send", "hasActiveRun": True, "status": "running"},
                     {"reason": "cron-binding", "status": "running"},
                     {"phase": "start", "runId": "r2", "hasActiveRun": True, "status": "running"}):
            self._gateway_push(**push)
            await self._spin()
            self.assertEqual(self.sent, [], push)
        self.assertEqual(self.rpc.count("sessions.describe"), reads + 4, "one read per push, nothing sent")
        self.active = False
        self._gateway_push(reason="agent.input.settled", hasActiveRun=False, status="done")
        await self._until_registered("run-1")
        self.assertEqual(self.sent, ["hello"])
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    async def _queued_while_describe_says(self, **row):
        """A message sent while `sessions.describe` gives `row` and `sessions.list` says idle;
        returns whether it went at once."""
        self.active = False
        self.describe_extra = row
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._spin()
        sent = list(self.sent)
        self.describe_extra = {}
        self._gateway_push(reason="agent.input.settled", hasActiveRun=False, status="done")
        await self._until_registered("run-1")
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        return bool(sent)

    async def test_the_describe_row_counts_a_run_by_has_active_run(self):
        """`sessions.describe` rows carry `hasActiveRun` like `sessions.list` rows; read when the
        status says nothing."""
        self.assertFalse(await self._queued_while_describe_says(status="done", hasActiveRun=True))

    async def test_the_describe_row_counts_a_run_by_its_active_run_ids(self):
        """`activeRunIds` is the exact set of runs; a non-empty one is a run."""
        self.assertFalse(await self._queued_while_describe_says(status="done", activeRunIds=["r1"]))

    async def test_the_describe_row_has_no_singular_active_run_id(self):
        """No Gateway version has `activeRunId` on a row, so it is not read."""
        self.assertTrue(await self._queued_while_describe_says(
            status="done", hasActiveRun=False, activeRunIds=[], activeRunId="r1"))

    async def test_a_message_waits_behind_a_busy_session_past_the_first_hundred_rows(self):
        """`sessions.list` with no filter returns only the newest 100 rows; a long run's row can be
        deeper. Here it is 120 rows deep and the message still waits."""
        self.crowded = True
        self.active = False
        self.list_active = True
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._spin()
        self.assertEqual(self.sent, [], "sent into a run the first page does not show")
        self.list_active = False
        self._gateway_push(reason="agent.input.settled", hasActiveRun=False, status="done")
        await self._until_registered("run-1")
        self.assertEqual(self.sent, ["hello"])
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    async def test_a_push_for_another_session_does_not_read(self):
        """A chat's queue does not read the Gateway for another chat's push."""
        self.active = True
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._spin()
        reads = self.rpc.count("sessions.describe")
        self.conn._on_sessions_changed({"sessionKey": "agent:main:other", "reason": "cron-binding", "status": "done"})
        self.conn._on_sessions_changed({"reason": "cron-bindings-loaded"})
        await self._spin()
        self.assertEqual(self.rpc.count("sessions.describe"), reads)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    async def test_queued_message_survives_a_redeploy_retiring_its_connection(self):
        """ELI-172: a retired connection that closed at the very settle a queued message waited for
        still wakes the waiter."""
        closed = []

        async def disconnect():
            closed.append(True)
        self.conn.disconnect = disconnect
        self.active = True
        self.conn.register_consumer(self.SK, "run-0")   # the long run being served
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._spin()
        self.assertEqual(self.sent, [])
        self.assertTrue(any("Queued: waiting" in str(e) for e in self.emitted))

        await self.conn.retire()                         # the redeploy
        self.conn.unregister_consumer(self.SK, "run-0")  # the run ends
        await self._spin()
        if not closed:
            self._settled(False)                         # its settle push
        await self._until_registered("run-1")
        self.assertEqual(self.sent, ["hello"])
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "hi there"))
        q.put_nowait(self._final("run-1"))
        out = await asyncio.wait_for(first, 2)
        async for chunk in gen:
            out += chunk if isinstance(chunk, str) else ""
        self.assertIn("hi there", out)
        await self._spin()
        self.assertTrue(closed, "the retired connection closes once the queued message is served")

    async def test_a_reply_on_a_retired_connection_reads_its_usage_before_the_close(self):
        """ELI-102 (#157): a reply on a retired connection gets its usage before the socket
        closes, or its end waits out the timeout and done:true never lands. Since ELI-381
        the usage is the run's pushed message: no request at all."""
        closed = []
        closed_event = asyncio.Event()
        hung = []
        send_request = self.conn.send_request

        async def disconnect():
            closed.append(True)
            closed_event.set()

        async def closing_send_request(method, params, timeout=10):
            await asyncio.sleep(0.02)          # the answer's way back
            if closed:
                hung.append(method)
                await asyncio.Event().wait()   # the socket closed meanwhile: no answer, ever
            return await send_request(method, params, timeout=timeout)
        self.conn.disconnect = disconnect
        self.conn.send_request = closing_send_request
        # The run's stored message, as the Gateway pushes it (ELI-381).
        self._run_message({
            "role": "assistant", "content": "hi", "usage": {"input": 10, "output": 5},
            "__openclaw": {"runId": "run-1"}})

        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        await self.conn.retire()                         # the redeploy
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "hi"))
        q.put_nowait({"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": "run-1", "stream": "lifecycle",
            "data": {"phase": "end"}}})
        out = await asyncio.wait_for(first, 2)

        async def rest():
            nonlocal out
            async for chunk in gen:
                out += chunk if isinstance(chunk, str) else ""
        await asyncio.wait_for(rest(), 2)
        self.assertIn("hi", out)
        # The close is a background task the dropped consumer starts.
        await asyncio.wait_for(closed_event.wait(), 2)
        self.assertEqual(hung, [], "a request sent after the close")
        self.assertNotIn("chat.history", self.rpc, "the usage comes from the run's pushed message")
        usage = self.ocp._build_usage_status_lines.await_args.kwargs.get("run_usage")
        self.assertEqual(usage, {"input": 10, "output": 5})

    async def test_a_reply_that_saw_its_run_end_leaves_no_parity_target(self):
        """The trailing `chat` final after `lifecycle end` triggers no parity finalize."""
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "hi"))
        q.put_nowait({"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": "run-1", "stream": "lifecycle",
            "data": {"phase": "end"}}})
        await asyncio.wait_for(first, 2)
        async for _ in gen:
            pass
        self.assertNotIn(f"{self.SK}:run-1", self.conn._run_targets)
        self.assertTrue(self.conn.was_delivered_live(self.SK, "run-1"))

    def _sent_history(self, key):
        """The transcript once run `key` (a reply's chat.send) has ended."""
        hist = _cli_history(key)
        hist["messages"][2]["idempotencyKey"] = f"{key}:user"
        return hist

    @staticmethod
    async def _collect(first, gen):
        out, items = "", []
        for chunk in [await asyncio.wait_for(first, 2)] + [c async for c in gen]:
            if isinstance(chunk, str):
                out += chunk
            else:
                items.append(chunk)
        return out, items

    async def test_one_owui_message_invoked_twice_is_sent_once(self):
        """ELI-82: Open WebUI can call the pipe twice for one reply. The second call follows the
        first run and ends with its reply instead of sending the text again."""
        gen1 = self._reply("check the logs", "m1")
        first1 = asyncio.ensure_future(gen1.__anext__())
        await self._until_registered("run-1")
        self.active = True
        # The Gateway writes the user turn when it admits the send.
        self.history = {"messages": self._sent_history(self.keys[0])["messages"][:3]}

        gen2 = self._reply("check the logs", "m1")
        first2 = asyncio.ensure_future(gen2.__anext__())
        await self._spin()
        self.assertFalse(first2.done(), "the second call waits for the run")

        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "Reading them.\n\nFound it."))
        q.put_nowait(self._final("run-1"))
        out1, _ = await self._collect(first1, gen1)
        self.assertIn("Found it.", out1)
        self.history = self._sent_history(self.keys[0])
        self._settled(False)

        out2, items2 = await self._collect(first2, gen2)
        self.assertEqual(self.sent, ["check the logs"], "the same message ran twice")
        self.assertEqual(out2, "Reading them.\n\nFound it.")
        self.assertEqual([i["item"]["type"] for i in items2 if i.get("type") == "response.output_item.added"],
                         ["function_call", "function_call_output"])

    async def test_a_second_call_after_the_run_ended_is_not_sent_again(self):
        """A second call after the run ended rebuilds the reply without waiting."""
        gen1 = self._reply("check the logs", "m1")
        first1 = asyncio.ensure_future(gen1.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "Reading them.\n\nFound it."))
        q.put_nowait(self._final("run-1"))
        await self._collect(first1, gen1)
        self.history = self._sent_history(self.keys[0])

        gen2 = self._reply("check the logs", "m1")
        out2, _ = await self._collect(asyncio.ensure_future(gen2.__anext__()), gen2)
        self.assertEqual(self.sent, ["check the logs"])
        self.assertIn("Found it.", out2)

    async def test_a_second_call_follows_an_ended_run_until_the_push_that_announces_the_idle(self):
        """The duplicate-reply wait wakes like the queue (2026.10.10): it reads again on the idle
        push that has no `hasActiveRun`, and still does not resend."""
        gen1 = self._reply("check the logs", "m1")
        first1 = asyncio.ensure_future(gen1.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "Reading them.\n\nFound it."))
        q.put_nowait(self._final("run-1"))
        await self._collect(first1, gen1)
        # The user turn is written, the run is not closed in the transcript yet.
        self.history = {"messages": self._sent_history(self.keys[0])["messages"][:3]}
        self.run_ended_at = 1793702659908
        self.active = True

        gen2 = self._reply("check the logs", "m1")
        first2 = asyncio.ensure_future(gen2.__anext__())
        await self._spin()
        self.assertFalse(first2.done(), "the second call waits for the run")
        self._gateway_push(phase="end", runId="run-1", hasActiveRun=True, status="running")
        await self._spin()
        self.assertFalse(first2.done(), "the end push still counts the run")

        self.active = False                       # cleared on the Gateway
        self.history = self._sent_history(self.keys[0])
        self._gateway_push(reason="cron-binding", status="done",
                            session={"status": "done", "hasActiveRun": False, "activeRunIds": []})
        await self._spin()
        self.assertTrue(first2.done(), "read again on the push that carries no hasActiveRun")
        out2, _ = await asyncio.wait_for(self._collect(first2, gen2), 5)
        self.assertEqual(self.sent, ["check the logs"], "the same message ran twice")
        self.assertEqual(out2, "Reading them.\n\nFound it.")

    async def test_a_second_call_renders_the_reply_written_between_the_read_and_the_idle_check(self):
        """The turn is read again once the session reads idle, so a closer written between the read
        and the idle check is not lost (#270)."""
        gen1 = self._reply("check the logs", "m1")
        first1 = asyncio.ensure_future(gen1.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "Reading them.\n\nFound it."))
        q.put_nowait(self._final("run-1"))
        await self._collect(first1, gen1)
        # The user turn is written, the run is not closed in the transcript yet.
        self.history = {"messages": self._sent_history(self.keys[0])["messages"][:3]}
        self.active = True

        read, send_request = {"history": 0}, self.conn.send_request

        async def write_after_the_read(method, params, timeout=10):
            if method == "sessions.describe" and read["history"] == 1:
                # The run ends: its closing message lands after the read above.
                self.history = self._sent_history(self.keys[0])
                self.active = False
            if method == "chat.history":
                read["history"] += 1
            return await send_request(method, params, timeout)
        self.conn.send_request = write_after_the_read

        gen2 = self._reply("check the logs", "m1")
        out2, _ = await asyncio.wait_for(
            self._collect(asyncio.ensure_future(gen2.__anext__()), gen2), 5)
        self.assertEqual(self.sent, ["check the logs"], "the same message ran twice")
        self.assertEqual(out2, "Reading them.\n\nFound it.")

    @staticmethod
    async def _drain(gen):
        return [c async for c in gen]

    async def test_a_second_call_keeps_its_snapshot_when_the_read_after_the_idle_check_gives_nothing(self):
        """The read after the idle check only improves the snapshot: if it fails or finds no turn,
        the earlier one is rendered and nothing is resent."""
        for n, outcome in enumerate(("fails", "no turn")):
            mid = f"m{n}"
            gen1 = self._reply("check the logs", mid)
            first1 = asyncio.ensure_future(gen1.__anext__())
            await self._until_registered(f"run-{n + 1}")
            q = self._queue(f"run-{n + 1}")
            q.put_nowait(self._delta(f"run-{n + 1}", "Reading them.\n\nFound it."))
            q.put_nowait(self._final(f"run-{n + 1}"))
            await self._collect(first1, gen1)
            # Closed in the transcript, still active on the Gateway.
            self.history = self._sent_history(self.keys[n])
            self.active = True
            read, send_request = {"history": 0}, self.conn.send_request

            async def second_read_gives_nothing(method, params, timeout=10, read=read, outcome=outcome):
                if method == "sessions.describe":
                    self.active = False
                if method == "chat.history":
                    read["history"] += 1
                    if read["history"] == 2:
                        if outcome == "fails":
                            raise self.ocp.GatewayError("chat.history timed out")
                        return {"messages": []}
                return await send_request(method, params, timeout)
            # Not closed, so the wait reaches the idle check and the second read.
            self.history = {"messages": self.history["messages"][:3]}
            self.conn.send_request = second_read_gives_nothing
            try:
                gen2 = self._reply("check the logs", mid)
                with self.subTest(outcome):
                    await asyncio.wait_for(self._drain(gen2), 5)
            finally:
                self.conn.send_request = send_request
            self.assertEqual(len(self.sent), n + 1, f"{outcome}: the message was sent again")

    async def test_a_second_call_with_new_text_is_sent(self):
        """A second call with different input is not a repeat."""
        gen1 = self._reply("check the logs", "m1")
        first1 = asyncio.ensure_future(gen1.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "done"))
        q.put_nowait(self._final("run-1"))
        await self._collect(first1, gen1)

        gen2 = self._reply("and now the other log", "m1")
        first2 = asyncio.ensure_future(gen2.__anext__())
        await self._until_registered("run-2")
        q = self._queue("run-2")
        q.put_nowait(self._delta("run-2", "other"))
        q.put_nowait(self._final("run-2"))
        await self._collect(first2, gen2)
        self.assertEqual(self.sent, ["check the logs", "and now the other log"])

    _NOT_ALLOWED = ('Model "anthropic/are" is not allowed. Use /models to list providers, '
                    'or /models <provider> to list models.')

    def _chat_error(self, run_id, message):
        return {"type": "event", "event": "chat", "payload": {
            "sessionKey": self.SK, "runId": run_id, "state": "error",
            "errorMessage": message}}

    async def _reply_text(self, events, text="these /model are fine"):
        gen = self._reply(text, "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        for ev in events:
            self._queue("run-1").put_nowait(ev)
        out = await asyncio.wait_for(first, 2)
        async for chunk in gen:
            out += chunk if isinstance(chunk, str) else ""
        return out

    async def test_a_run_refused_before_it_starts_shows_its_error(self):
        """ELI-139: a run the Gateway refused before it started (a "/model" directive in the text)
        shows the error, not "(This turn produced no reply text.)"."""
        out = await self._reply_text([self._chat_error("run-1", self._NOT_ALLOWED)])
        self.assertIn(RUN_ERROR_SENTENCE, out)
        self.assertIn(f"```\n{self._NOT_ALLOWED}\n```", out)
        self.assertNotIn("no reply text", out)

    def _spawn_card_events(self, run_id):
        def tool(phase, **data):
            return {"type": "event", "event": "agent", "payload": {
                "sessionKey": self.SK, "runId": run_id, "stream": "tool",
                "data": {"phase": phase, "name": "sessions_spawn", "toolCallId": "t1", **data}}}
        return [tool("start", args={"task": "Answer what 17 times 3 is."}),
                tool("result", result="Started: QA 17x3")]

    async def test_a_run_that_answers_with_silence_after_its_card_keeps_the_card_alone(self):
        """ELI-227: a reply that spawns a sub-agent and answers NO_REPLY keeps the card alone, with
        no "no reply text" note."""
        gen = self._reply("start one", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        for ev in [*self._spawn_card_events("run-1"), self._delta("run-1", "NO_REPLY"),
                   self._final("run-1")]:
            self._queue("run-1").put_nowait(ev)
        chunks = [await asyncio.wait_for(first, 2)]
        chunks += [c async for c in gen]
        text = "".join(c for c in chunks if isinstance(c, str))
        self.assertNotIn("no reply text", text)
        self.assertNotIn("NO_REPLY", text)
        cards = [c for c in chunks if isinstance(c, dict) and c.get("type") == "response.output_item.added"
                 and (c.get("item") or {}).get("type") == "function_call"]
        self.assertEqual(len(cards), 1, "the card stays")

    async def test_a_run_with_no_text_and_no_card_still_says_so(self):
        out = await self._reply_text([self._final("run-1")])
        self.assertIn("no reply text", out)

    async def test_a_lifecycle_error_is_not_shown_twice(self):
        out = await self._reply_text([
            {"type": "event", "event": "agent", "payload": {
                "sessionKey": self.SK, "runId": "run-1", "stream": "lifecycle",
                "data": {"phase": "error", "error": "provider overloaded"}}},
            self._chat_error("run-1", "provider overloaded"),
        ])
        self.assertEqual(out.count(RUN_ERROR_SENTENCE), 1)
        self.assertIn("```\nprovider overloaded\n```", out)

    async def test_a_run_error_is_a_plain_sentence_in_the_language_of_the_message(self):
        """S13: Ortal saw `**Error:** Error: prepared reply dispatch runtime owner was not
        published for main` for a question in Hebrew."""
        raw = "Error: prepared reply dispatch runtime owner was not published for main"
        out = await self._reply_text([self._chat_error("run-1", raw)], text=HEBREW_ASK)
        he = openclaw_pipe._ERROR_TEXT[openclaw_pipe.LANG_HE]
        self.assertIn(he[openclaw_pipe.ERROR_RUN], out)
        self.assertNotIn("**Error:**", out)
        head, details = out.split("<details>")
        self.assertNotIn("dispatch runtime", head)
        self.assertIn(f"<summary>{he[openclaw_pipe.ERROR_DETAILS]}</summary>", details)
        self.assertIn(f"```\n{raw}\n```", details)

    async def test_a_send_failure_is_a_plain_sentence_with_the_raw_error_collapsed(self):
        self.send_fails = RuntimeError("attachment image-2.png: image exceeds size limit")
        out = "".join([c async for c in self._reply(HEBREW_ASK, "m1") if isinstance(c, str)])
        he = openclaw_pipe._ERROR_TEXT[openclaw_pipe.LANG_HE]
        self.assertTrue(out.startswith(he[openclaw_pipe.ERROR_SEND]), out)
        self.assertNotIn("**Error sending message:**", out)
        self.assertIn("exceeds size limit", out.split("<details>")[1])

    async def test_a_socket_gap_makes_the_waiter_read_again(self):
        """The settle can be among the events lost during a gap, so a gap wakes every waiter."""
        self.active = True
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._spin()
        self.assertEqual(self.sent, [])
        self.active = False  # settled while the socket was down: never pushed
        self.conn._mark_event_gap()
        await self._until_registered("run-1")
        self.assertEqual(self.sent, ["hello"])
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "hi"))
        q.put_nowait(self._final("run-1"))
        await asyncio.wait_for(first, 2)
        async for _ in gen:
            pass

    async def test_claude_cli_message_queued_behind_a_new_run_is_taken_back_and_resent(self):
        """A run starting between the idle read and the send queues the message (a lone empty
        final), which claude-cli drops. The pipe aborts that runId and resends after the settle."""
        self.runtime = "claude-cli"
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        self.active = True  # the resume won the race
        self._queue("run-1").put_nowait(self._final("run-1"))
        await self._spin()
        self.assertEqual(self.aborts, [{"sessionKey": self.SK, "runId": "run-1"}])
        self.assertEqual(self.sent, ["hello"], "not resent while the run is going")

        self._settled(False)
        await self._until_registered("run-2")
        self.assertEqual(self.sent, ["hello", "hello"])
        q = self._queue("run-2")
        q.put_nowait(self._delta("run-2", "answer"))
        q.put_nowait(self._final("run-2"))
        out = await asyncio.wait_for(first, 2)
        async for chunk in gen:
            out += chunk if isinstance(chunk, str) else ""
        self.assertIn("answer", out)
        self.assertEqual(len(self.aborts), 1)

    async def test_api_runtime_steered_message_is_not_taken_back(self):
        """An API runtime injects a mid-run message into the live run, so a lone empty final there
        is not a lost message."""
        self.runtime = "openai"
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        self._queue("run-1").put_nowait(self._final("run-1"))
        with contextlib.suppress(StopAsyncIteration):
            await asyncio.wait_for(first, 2)
            async for _ in gen:
                pass
        self.assertEqual(self.aborts, [])
        self.assertEqual(self.sent, ["hello"])

    def _whole(self, run_id, text):
        # claude-cli closes a turn with a catch-all carrying the WHOLE reply.
        return {"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": run_id,
            "stream": "assistant", "data": {"text": text}}}

    async def _drain(self, gen):
        out = ""
        while True:
            try:
                chunk = await asyncio.wait_for(gen.__anext__(), 2)
            except StopAsyncIteration:
                return out
            if isinstance(chunk, str):
                out += chunk

    @staticmethod
    def _nums(a, b):
        return "".join(f"{i}\n" for i in range(a, b + 1))

    @staticmethod
    def _count(text):
        return [int(t) for t in text.split() if t.isdigit()]

    async def test_socket_drop_mid_reply_posts_no_second_copy(self):
        """After a socket drop the closing catch-all (the whole reply) adds only what follows the
        received text. With no message to hand over, lost lines stay lost (ELI-147)."""
        with mock.patch.object(self.ocp, "_gateway_connection", self.conn):
            r1 = self._reply("count to 100", "m1")
            first = asyncio.ensure_future(r1.__anext__())
            await self._until_registered("run-1")
            self.active = True
            q = self._queue("run-1")
            q.put_nowait(self._delta("run-1", self._nums(1, 50)))
            out = await first
            out += await self._read(r1, "50\n") if "50\n" not in out else ""
            self.conn._mark_event_gap()          # 51..60 are sent while the socket is down
            q.put_nowait(self._delta("run-1", self._nums(61, 100)))
            q.put_nowait(self._whole("run-1", self._nums(1, 100)))
            q.put_nowait(self._final("run-1"))
            self.active = False
            out += await self._drain(r1)
            nums = self._count(out)
            self.assertEqual(len(nums), len(set(nums)), "no line may be shown twice")
            self.assertEqual(sorted(nums), list(range(1, 51)) + list(range(61, 101)))

    async def test_socket_drop_before_a_send_now_shows_no_line_twice(self):
        """A drop right after reply 1 registered, then two Send nows: no line shows twice. With no
        message to hand over, lost lines stay lost (see GapHandoverReplayTests)."""
        armed = self._hold_parked_timers()
        r1 = self._reply("count to 30", "m1", intent="send_now")
        first = asyncio.ensure_future(r1.__anext__())
        await self._until_registered("run-1")
        self.active = True
        q = self._queue("run-1")
        self.conn._mark_event_gap()
        q.put_nowait(self._delta("run-1", self._nums(4, 10)))
        out1 = await first
        out1 += await self._read(r1, "10\n") if "10\n" not in out1 else ""
        await r1.aclose()

        r2 = self._reply("MSG2", "m2", intent="send_now")
        q.put_nowait(self._delta("run-1", self._nums(11, 20)))
        out2 = await self._read(r2, "20\n")
        await r2.aclose()

        r3 = self._reply("MSG3", "m3")
        q.put_nowait(self._delta("run-1", self._nums(21, 30)))
        q.put_nowait(self._whole("run-1", self._nums(1, 30)))
        q.put_nowait(self._final("run-1"))
        out3 = await self._read(r3, "30\n")
        self.active = False
        rest = asyncio.ensure_future(self._read(r3, "ANSWER"))
        await self._until_registered("run-2")
        self._queue("run-2").put_nowait(self._delta("run-2", "ANSWER"))
        self._queue("run-2").put_nowait(self._final("run-2"))
        out3 += await rest
        await r3.aclose()
        await asyncio.sleep(0)

        nums = self._count(out1 + out2 + out3)
        self.assertEqual(len(nums), len(set(nums)), "no line may be shown twice")
        self.assertEqual(sorted(nums), list(range(4, 31)))
        self.assertNotIn("abort", self.rpc)
        self.assertGreaterEqual(armed.count(0.3), 2, "each Send now parked the run for the next reply")

    async def test_send_now_twice_loses_nothing(self):
        armed = self._hold_parked_timers()
        r1 = self._reply("count to 1500", "m1")
        first = asyncio.ensure_future(r1.__anext__())
        await self._until_registered("run-1")
        self.active = True
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "1\n"))
        self.assertIn("1", await first)
        # OWUI closes r1 while it is suspended at a yield (Send now "test").
        await r1.aclose()
        # The gap before the next reply registers.
        q.put_nowait(self._delta("run-1", "185\n"))

        r2 = self._reply("test", "m2")
        out2 = await self._read(r2, "185")
        q.put_nowait(self._delta("run-1", "720\n"))
        out2 += await self._read(r2, "720")
        await r2.aclose()  # Send now "another test", again as a close

        r3 = self._reply("another test", "m3")
        q.put_nowait(self._delta("run-1", "1500\n"))
        q.put_nowait(self._final("run-1"))
        out3 = await self._read(r3, "1500")
        self.active = False
        rest = asyncio.ensure_future(self._read(r3, "ANSWER"))
        await self._until_registered("run-2")
        self._queue("run-2").put_nowait(self._delta("run-2", "ANSWER"))
        self._queue("run-2").put_nowait(self._final("run-2"))
        out3 += await rest
        await r3.aclose()

        self.assertIn("185", out2, "events in the cancel gap must not vanish")
        self.assertIn("720", out2)
        self.assertNotIn("720", out3, "the continuation must not repeat itself")
        self.assertEqual(self.sent, ["count to 1500", "test\n\nanother test"],
                         "the interrupted reply's own message must still be sent")
        self.assertNotIn("abort", self.rpc, "an interrupt must not stop the run")
        self.assertEqual(armed[:2], [0.3, 0.3], "each Send now parked the run for the next reply")

    async def test_send_now_answer_survives_a_slow_follow_up(self):
        # A follow-up 3.4 s after the cancel must not find the run already aborted.
        self.pipe.valves.INTERRUPT_GRACE_SECONDS = 0.05
        r1 = self._reply("count", "m1", intent="send_now")
        first = asyncio.ensure_future(r1.__anext__())
        await self._until_registered("run-1")
        self.active = True
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "1\n"))
        await first
        await r1.aclose()
        await asyncio.sleep(0.4)  # far past the fallback window
        q.put_nowait(self._delta("run-1", "2\n"))
        r2 = self._reply("next", "m2")
        out = await self._read(r2, "2")
        await r2.aclose()
        self.assertIn("2", out)
        self.assertNotIn("abort", self.rpc)
        self.assertTrue(any(e.get("type") == "execute" for e in self.emitted),
                        "the page hook is installed at turn start")

    async def test_the_turn_tells_the_page_this_chats_auto_send_setting(self):
        self.pipe.valves.AUTO_SEND_WHILE_RUNNING = False
        r = self._reply("count", "m1")
        first = asyncio.ensure_future(r.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "1\n"))
        q.put_nowait(self._final("run-1"))
        await first
        async for _chunk in r:
            pass
        codes = [e["data"]["code"] for e in self.emitted if e.get("type") == "execute"]
        self.assertTrue(any('window.__ocAutoSend["chat-1"] = false' in c for c in codes), codes)

    async def test_stop_answer_stops_without_waiting(self):
        self.pipe.valves.INTERRUPT_GRACE_SECONDS = 30
        r1 = self._reply("count", "m1", intent="stop")
        first = asyncio.ensure_future(r1.__anext__())
        await self._until_registered("run-1")
        self.active = True
        self._queue("run-1").put_nowait(self._delta("run-1", "1\n"))
        marked = []

        async def mark(chat_id, message_id, request=None):
            marked.append((chat_id, message_id))
        with mock.patch.object(openclaw_pipe, "_mark_stopped", mark):
            await first
            await r1.aclose()
            await asyncio.sleep(0.05)
        self.assertIn("abort", self.rpc)
        self.assertEqual(self.conn._consumers, {})
        # Nothing adds to it after a restart.
        self.assertEqual(marked, [("chat-1", "m1")])

    async def _interrupted_during_settle(self):
        """A reply cut while its run streams, whose closing is cancelled again while the page's
        answer is in flight. Returns the future the page answers through."""
        self.pipe.valves.INTERRUPT_GRACE_SECONDS = 30
        answer = asyncio.get_running_loop().create_future()

        async def event_call(event):
            code = (event.get("data") or {}).get("code", "")
            if "const i = all[c]" in code:   # the intent query
                return await answer
            return True                  # the hook install at turn start
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "count"}]}
        r1 = self.pipe._pipe_impl(body, lambda e: asyncio.sleep(0),
                                  __metadata__={"chat_id": "chat-1", "user_id": "u1", "message_id": "m1"},
                                  __event_call__=event_call)
        first = asyncio.ensure_future(r1.__anext__())
        await self._spin()
        self.assertIn(f"{self.SK}:run-1", self.conn._consumers)
        self.active = True
        self._queue("run-1").put_nowait(self._delta("run-1", "1\n"))
        await first
        closing = asyncio.ensure_future(r1.aclose())
        await self._spin()
        closing.cancel()   # the cancelled reply is interrupted again, mid-settle
        await asyncio.gather(closing, return_exceptions=True)
        return answer

    async def test_a_stop_answer_holds_when_the_cancelled_reply_is_interrupted(self):
        """ELI-95: a second interruption on the close path keeps the Stop a Stop."""
        answer = await self._interrupted_during_settle()
        r2 = self._reply("next", "m2")
        nxt = asyncio.ensure_future(r2.__anext__())
        await self._spin()
        self.assertIn(self.SK, self.conn._pending_cancel_aborts,
                      "the next message waits for the page's answer")
        answer.set_result("stop")
        await self._spin()
        self.assertIn("abort", self.rpc, "the stopped run is aborted")
        self.assertNotIn(self.SK, self.conn._pending_cancel_aborts)
        self._settled(False)   # the Gateway settles the aborted run
        await self._spin()
        self.assertIn(f"{self.SK}:run-2", self.conn._consumers)
        self.assertEqual(self.sent[-1], "next", "the next message starts its own run")
        nxt.cancel()
        await asyncio.gather(nxt, return_exceptions=True)
        await r2.aclose()

    async def test_a_send_now_answer_is_adopted_when_the_cancelled_reply_is_interrupted(self):
        answer = await self._interrupted_during_settle()
        r2 = self._reply("next", "m2")
        nxt = asyncio.ensure_future(r2.__anext__())
        await self._spin()
        self.assertIn(self.SK, self.conn._pending_cancel_aborts,
                      "the next message waits for the page's answer")
        self.assertFalse(nxt.done())
        answer.set_result("send_now")
        self._queue("run-1").put_nowait(self._delta("run-1", "2\n"))
        out = await asyncio.wait_for(nxt, 2)   # bounds the test only
        while "2" not in out:
            out += await asyncio.wait_for(r2.__anext__(), 2)
        self.assertIn("2", out)
        self.assertNotIn("abort", self.rpc)
        await r2.aclose()

    async def test_adopted_run_closing_text_is_not_appended_again(self):
        # The closer carries the whole text; diff it against the run's baseline.
        r1 = self._reply("count", "m1")
        first = asyncio.ensure_future(r1.__anext__())
        await self._until_registered("run-1")
        self.active = True
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "1\n"))
        await first
        await r1.aclose()

        r2 = self._reply("next", "m2")
        q.put_nowait(self._delta("run-1", "2\n"))
        q.put_nowait({"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": "run-1", "stream": "assistant",
            "data": {"text": "1\n2\n"}}})
        q.put_nowait(self._final("run-1"))
        out = await self._read(r2, "2")
        self.active = False
        rest = asyncio.ensure_future(self._read(r2, "ANSWER"))
        await self._until_registered("run-2")
        self._queue("run-2").put_nowait(self._delta("run-2", "ANSWER"))
        self._queue("run-2").put_nowait(self._final("run-2"))
        out += await rest
        await r2.aclose()
        self.assertEqual(out.count("1\n"), 0, f"run text appended again: {out!r}")
        self.assertEqual(out.count("2\n"), 1, out)

    async def test_plain_stop_still_stops(self):
        r1 = self._reply("count", "m1")
        first = asyncio.ensure_future(r1.__anext__())
        await self._until_registered("run-1")
        self.active = True
        self._queue("run-1").put_nowait(self._delta("run-1", "1\n"))
        await first
        await r1.aclose()
        await asyncio.sleep(0.5)
        self.assertIn("abort", self.rpc)
        self.assertEqual(self.conn._consumers, {}, "the parked queue is released")

    async def test_a_queued_message_waits_out_a_socket_reconnect(self):
        """ELI-199: during a reconnect the run state is unknown, so a queued message waits; it is
        sent once the run settles."""
        real_send = self.conn.send_request

        async def send_request(method, params, timeout=10):
            if self.conn._ws is None:   # as `send_request` does mid-reconnect
                raise self.ocp.GatewayError("gateway connection not ready (reconnecting)")
            return await real_send(method, params, timeout)
        self.conn.send_request = send_request
        loop_task = asyncio.ensure_future(asyncio.Event().wait())  # the event loop, reconnecting
        self.addCleanup(loop_task.cancel)
        self.conn._event_loop_task = loop_task

        self.active = True
        gen = self._reply("queued", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._spin()
        self.assertTrue(any("Queued: waiting" in str(e) for e in self.emitted))

        self.conn._ws = None                # 1006: the socket is gone
        self.conn._wake_session_waiters()   # the drop wakes every waiter (`_mark_event_gap`)
        await self._spin()
        self.assertEqual(self.sent, [], "sent while the run's state was unknown")
        self.assertFalse(first.done(), f"the reply ended: {first.result() if first.done() else ''!r}")
        # It asks the loop to reconnect now and waits.
        self.assertTrue(self.conn._reconnect_now.is_set())
        self.assertEqual(self.conn._turns_awaiting_gateway, 1)

        refused = self.ocp.GatewayError("Connection refused", error={"code": self.ocp.GATEWAY_REFUSED_CODE})
        self.conn._answer_reconnect_waiters(refused)   # the Gateway is still down
        await self._spin()
        self.assertFalse(first.done(), "a refused reconnect ended the reply")

        self.conn._ws = object()            # reconnected, same Gateway; the run still goes
        self.conn._answer_reconnect_waiters(None)   # the loop's attempt succeeded
        self.conn._wake_session_waiters()   # the renewed subscriptions wake it (`_subscribe_all`)
        await self._spin()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.conn._turns_awaiting_gateway, 0)

        self._settled(False)
        await self._until_registered("run-1")
        self.assertEqual(self.sent, ["queued"])
        self._queue("run-1").put_nowait(self._delta("run-1", "ANSWER"))
        self._queue("run-1").put_nowait(self._final("run-1"))
        out = await asyncio.wait_for(first, 5)
        async for chunk in gen:
            out += chunk if isinstance(chunk, str) else ""
        self.assertIn("ANSWER", out)
        self.assertNotIn("Error sending message", out)

    async def test_cancel_while_queued_carries_the_message(self):
        self.active = True  # a run this chat did not start, e.g. a scheduled turn
        r1 = self._reply("first", "m1")
        task = asyncio.ensure_future(r1.__anext__())
        await asyncio.sleep(0.2)  # waiting behind the active run, nothing sent
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.sent, [])

        self.active = False
        r2 = self._reply("second", "m2")
        nxt = asyncio.ensure_future(self._read(r2, "OK"))
        await self._until_registered("run-1")
        self._queue("run-1").put_nowait(self._delta("run-1", "OK"))
        self._queue("run-1").put_nowait(self._final("run-1"))
        await nxt
        await r2.aclose()
        self.assertEqual(self.sent, ["first\n\nsecond"])

    def _record_closes(self):
        """What each interrupted reply was ended with, as (message_id, note)."""
        closed = []

        async def close(conn, ref, note):
            closed.append((ref.get("message_id"), note))
        patch = mock.patch.object(self.ocp, "_close_unsent_reply", close, create=True)
        patch.start()
        self.addCleanup(patch.stop)
        return closed

    async def test_a_reply_interrupted_while_queued_ends_once_its_message_is_sent(self):
        """ELI-188: a reply cancelled while queued says its message went with a later reply, once
        that reply really sent it."""
        closed = self._record_closes()
        self.active = True
        r1 = self._reply("second", "m2")
        task = asyncio.ensure_future(r1.__anext__())
        await self._spin()
        self.assertTrue(any("Queued: waiting" in str(e) for e in self.emitted))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        r2 = self._reply("third", "m3")
        nxt = asyncio.ensure_future(self._read(r2, "OK"))
        await self._spin()
        self.assertEqual(self.sent, [])
        self.assertEqual(closed, [], "still waiting: nothing to say yet")

        self._settled(False)
        await self._until_registered("run-1")
        self.assertEqual(self.sent, ["second\n\nthird"])
        await self._spin()
        self.assertEqual(closed, [("m2", self.ocp.REPLY_SENT_WITH_NEXT_NOTE)])
        self._queue("run-1").put_nowait(self._delta("run-1", "OK"))
        self._queue("run-1").put_nowait(self._final("run-1"))
        await nxt
        await r2.aclose()
        self.assertEqual(len(closed), 1)

    async def test_a_queued_reply_stopped_says_its_message_was_not_sent(self):
        """ELI-188: Send now, then Stop: the queued reply says its message was not sent."""
        closed = self._record_closes()
        self.active = True
        r1 = self._reply("second", "m2", intent="stop")
        task = asyncio.ensure_future(r1.__anext__())
        await self._spin()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        for _ in range(200):
            if closed:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(closed, [("m2", self.ocp.REPLY_NOT_SENT_NOTE)])
        self.assertEqual(self.sent, [])

    async def test_two_interrupted_replies_both_end_when_the_third_sends(self):
        """Two Send nows during a run: both replies hand their message on and say so."""
        closed = self._record_closes()
        self._hold_parked_timers()
        self.active = True
        for text, mid in (("a", "m2"), ("b", "m3")):
            r = self._reply(text, mid)
            t = asyncio.ensure_future(r.__anext__())
            await self._spin()
            t.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t
        r3 = self._reply("c", "m4")
        nxt = asyncio.ensure_future(self._read(r3, "OK"))
        await self._spin()
        self._settled(False)
        await self._until_registered("run-1")
        await self._spin()
        self.assertEqual(self.sent, ["a\n\nb\n\nc"])
        self.assertEqual(sorted(closed), [("m2", self.ocp.REPLY_SENT_WITH_NEXT_NOTE),
                                          ("m3", self.ocp.REPLY_SENT_WITH_NEXT_NOTE)])
        self._queue("run-1").put_nowait(self._delta("run-1", "OK"))
        self._queue("run-1").put_nowait(self._final("run-1"))
        await nxt
        await r3.aclose()

    async def test_images_of_interrupted_replies_go_with_the_next_send(self):
        """A handed-on message keeps its image: the next send carries every image, in order, under
        its own file name."""
        self._record_closes()
        self._hold_parked_timers()
        self.active = True
        for text, mid, image in (("a", "m2", "QUFB"), ("b", "m3", None)):
            r = self._reply(text, mid, image=image)
            t = asyncio.ensure_future(r.__anext__())
            await self._spin()
            t.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t
        r3 = self._reply("c", "m4", image="Q0ND")
        nxt = asyncio.ensure_future(self._read(r3, "OK"))
        await self._spin()
        self._settled(False)
        await self._until_registered("run-1")
        self.assertEqual(self.sent, ["a\n\nb\n\nc"])
        self.assertEqual(self.sent_images, [[("image-1.png", "QUFB"), ("image-2.png", "Q0ND")]])
        self._queue("run-1").put_nowait(self._delta("run-1", "OK"))
        self._queue("run-1").put_nowait(self._final("run-1"))
        await nxt
        await r3.aclose()

    async def _sent_or_answer(self, gen):
        """The reply's first line, or None once chat.send went out (bounds a hang only)."""
        first = asyncio.ensure_future(gen.__anext__())
        for _ in range(3000):
            if self.sent or first.done():
                break
            await asyncio.sleep(0.01)
        return first

    async def _answer(self, gen, first):
        await self._until_registered("run-1")
        self._queue("run-1").put_nowait(self._delta("run-1", "OK"))
        self._queue("run-1").put_nowait(self._final("run-1"))
        out = await asyncio.wait_for(first, 30)
        if "OK" not in out:
            out += await self._read(gen, "OK")
        await gen.aclose()

    @staticmethod
    def _noise_png(size):
        from PIL import Image
        out = io.BytesIO()
        Image.effect_noise(size, 60).convert("RGB").save(out, "PNG")
        return base64.b64encode(out.getvalue()).decode("ascii")

    async def test_a_photo_over_the_gateway_cap_is_sent_shrunk_under_it(self):
        """A phone photo over hello-ok's maxImageBytes goes out as a JPEG under it, not whole
        (the Gateway refuses the whole one: "image exceeds size limit", and no reply)."""
        self.conn.gateway_max_image_bytes = 300_000
        r = self._reply("what does the sign say?", "m2", image=self._noise_png((1600, 1200)))
        first = await self._sent_or_answer(r)
        self.assertFalse(first.done() and "Error" in str(first.result()))
        [[(name, content)]] = self.sent_images
        self.assertEqual(name, "image-2.jpg")
        self.assertLessEqual(len(base64.b64decode(content)), 300_000)
        await self._answer(r, first)

    async def test_the_parked_turn_is_claimed_before_a_photo_is_shrunk(self):
        """The parked turn's timer does not wait for a claim: a slow fit ahead of it could outlast
        the grace window and the follow-up would find no run to adopt."""
        self.conn.gateway_max_image_bytes = 300_000
        order = []
        claim, fit = self.conn.claim_cancelled_turn, self.ocp._fit_image_attachments

        async def claiming(session_key):
            order.append("claim")
            return await claim(session_key)

        def fitting(*args):
            order.append("fit")
            return fit(*args)

        self.conn.claim_cancelled_turn = claiming
        with mock.patch.object(self.ocp, "_fit_image_attachments", fitting):
            r = self._reply("what does the sign say?", "m2", image=self._noise_png((1600, 1200)))
            first = await self._sent_or_answer(r)
        self.assertEqual(order, ["claim", "fit"])
        await self._answer(r, first)

    def _claiming(self, **claimed):
        """A cancelled turn's leftovers, claimed by the reply that follows it."""
        async def claim(session_key):
            return {"run_id": None, "queue": None, "carried": [], "attachments": [],
                    "waiting": [], **claimed}
        self.conn.claim_cancelled_turn = claim

    async def test_a_photo_carried_from_a_cancelled_reply_is_shrunk_too(self):
        """A reply cancelled while its own fit ran leaves its photo whole in the claim: the next
        reply, which has no photo of its own, must not send it over the cap."""
        self.conn.gateway_max_image_bytes = 300_000
        big = {"type": "image", "mimeType": "image/png", "fileName": "image-1.png",
               "content": self._noise_png((1600, 1200))}
        self._claiming(carried=["the photo question"], attachments=[big])
        r = self._reply("and this one?", "m2")
        first = await self._sent_or_answer(r)
        [[(name, content)]] = self.sent_images
        self.assertEqual(name, "image-1.jpg")
        self.assertLessEqual(len(base64.b64decode(content)), 300_000)
        await self._answer(r, first)

    async def test_when_the_photo_cannot_be_shrunk_the_claimed_replies_are_told_not_sent(self):
        self.conn.gateway_max_image_bytes = 300_000
        self._claiming(waiting=["earlier reply"], carried=["earlier"])
        with mock.patch.object(self.ocp, "_load_pil", lambda: None), \
                mock.patch.object(self.ocp, "_end_unsent_replies") as ended:
            out = await asyncio.wait_for(
                self._drain(self._reply("hi", "m2", image=self._noise_png((1600, 1200)))), 30)
        self.assertIn("Couldn't send the image", out)
        ended.assert_called_once_with(self.conn, ["earlier reply"], sent=False,
                                      note=self.ocp.REPLY_SEND_FAILED_NOTE)
        self.assertEqual(self.sent, [])

    async def test_photos_of_different_chats_are_shrunk_one_at_a_time(self):
        """Each fit holds a whole photo decoded: two at once would hold two."""
        self.conn.gateway_max_image_bytes = 300_000
        running, most = [0], [0]

        def fitting(attachments, max_bytes):
            running[0] += 1
            most[0] = max(most[0], running[0])
            time.sleep(0.15)
            running[0] -= 1
            return attachments

        image = self._noise_png((1600, 1200))
        with mock.patch.object(self.ocp, "_fit_image_attachments", fitting):
            gens = [self._reply("hi", f"m{n}", image=image, chat=f"chat-{n}") for n in (2, 3)]
            tasks = [asyncio.ensure_future(g.__anext__()) for g in gens]
            for _ in range(500):
                if len(self.sent) >= 2 or all(t.done() for t in tasks):
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(most[0], 1)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_a_photo_under_the_gateway_cap_is_sent_byte_for_byte(self):
        image = self._noise_png((200, 150))
        r = self._reply("hi", "m2", image=image)
        first = await self._sent_or_answer(r)
        self.assertEqual(self.sent_images, [[("image-2.png", image)]])
        await self._answer(r, first)

    async def test_without_pillow_a_photo_over_the_cap_gets_one_plain_sentence(self):
        self.conn.gateway_max_image_bytes = 300_000
        with mock.patch.object(self.ocp, "_load_pil", lambda: None):
            out = await asyncio.wait_for(
                self._drain(self._reply("hi", "m2", image=self._noise_png((1600, 1200)))), 30)
        self.assertIn("Couldn't send the image", out)
        self.assertNotIn("Error sending message", out)
        self.assertEqual(self.sent, [])

    async def test_images_carried_through_a_kept_run_are_sent_after_it(self):
        """Send now twice while a run streams: the continuing reply sends the interrupted message
        after it, image included."""
        self._hold_parked_timers()
        r1 = self._reply("count", "m1")
        first = asyncio.ensure_future(r1.__anext__())
        await self._until_registered("run-1")
        self.active = True
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "1\n"))
        await first
        await r1.aclose()
        r2 = self._reply("test", "m2", image="QUFB")
        q.put_nowait(self._delta("run-1", "2\n"))
        await self._read(r2, "2")
        await r2.aclose()
        r3 = self._reply("another test", "m3")
        q.put_nowait(self._delta("run-1", "3\n"))
        q.put_nowait(self._final("run-1"))
        await self._read(r3, "3")
        self.active = False
        rest = asyncio.ensure_future(self._read(r3, "ANSWER"))
        await self._until_registered("run-2")
        self._queue("run-2").put_nowait(self._delta("run-2", "ANSWER"))
        self._queue("run-2").put_nowait(self._final("run-2"))
        await rest
        await r3.aclose()
        self.assertEqual(self.sent, ["count", "test\n\nanother test"])
        self.assertEqual(self.sent_images, [[], [("image-1.png", "QUFB")]])

    async def _waiting_replies(self, *specs):
        """Start a reply per (text, msg_id, image) waiting behind the run, uncancelled (another
        tab). Each is read until its first text."""
        reads = []
        for text, mid, image in specs:
            r = self._reply(text, mid, image=image)
            reads.append((r, asyncio.ensure_future(self._drain(r))))
            await self._spin()
        return reads

    @staticmethod
    async def _drain(gen):
        out = ""
        async for item in gen:
            if isinstance(item, str):
                out += item
        return out

    async def test_messages_waiting_behind_a_run_go_out_as_one(self):
        """Messages waiting behind a run go out as ONE chat.send once it settles, in order, with
        every image. The answer is in the last reply; the others say where their message went."""
        closed = self._record_closes()
        self.active = True
        reads = await self._waiting_replies(("a", "m2", "QUFB"), ("b", "m3", None), ("c", "m4", "Q0ND"))
        self.assertEqual(self.sent, [])
        self._settled(False)
        await self._until_registered("run-1")
        await self._spin()
        self.assertEqual(self.sent, ["a\n\nb\n\nc"])
        self.assertEqual(self.sent_images, [[("image-1.png", "QUFB"), ("image-2.png", "Q0ND")]])
        self.assertEqual(sorted(closed), [("m2", self.ocp.REPLY_SENT_WITH_NEXT_NOTE),
                                          ("m3", self.ocp.REPLY_SENT_WITH_NEXT_NOTE)])
        self.assertEqual(await reads[0][1], "")
        self.assertEqual(await reads[1][1], "")
        self._queue("run-1").put_nowait(self._delta("run-1", "OK"))
        self._queue("run-1").put_nowait(self._final("run-1"))
        self.assertIn("OK", await asyncio.wait_for(reads[2][1], 30))
        self.assertEqual(len(self.sent), 1)

    async def test_a_message_that_arrives_during_the_joint_send_waits_its_turn(self):
        """A message arriving during the joint send waits for that run, then goes alone."""
        self.active = True
        reads = await self._waiting_replies(("a", "m2", None), ("b", "m3", None))
        self._settled(False)
        await self._until_registered("run-1")
        self.active = True
        late = await self._waiting_replies(("c", "m4", None))
        self.assertEqual(self.sent, ["a\n\nb"])
        self._queue("run-1").put_nowait(self._delta("run-1", "OK"))
        self._queue("run-1").put_nowait(self._final("run-1"))
        await asyncio.wait_for(reads[1][1], 30)
        self._settled(False)
        await self._until_registered("run-2")
        self.assertEqual(self.sent, ["a\n\nb", "c"])
        self._queue("run-2").put_nowait(self._delta("run-2", "OK"))
        self._queue("run-2").put_nowait(self._final("run-2"))
        await asyncio.wait_for(late[0][1], 30)

    async def test_a_waiting_command_goes_alone_and_nothing_joins_across_it(self):
        """ELI-376: a command glued to other text is no longer one, and a message joined
        across it would go before it: "a", "/new", "c" go as three sends, in order."""
        self.active = True
        reads = await self._waiting_replies(("a", "m2", None), ("/new", "m3", None), ("c", "m4", None))
        for run in ("run-1", "run-2", "run-3"):
            self._settled(False)
            await self._until_registered(run)
            self._queue(run).put_nowait(self._delta(run, "OK"))
            self._queue(run).put_nowait(self._final(run))
            self.active = True
        self.assertEqual(self.sent, ["a", "/new", "c"])
        for _, task in reads:
            task.cancel()

    async def test_messages_before_a_waiting_command_still_join(self):
        self.active = True
        reads = await self._waiting_replies(("a", "m2", None), ("b", "m3", None), ("/model opus", "m4", None))
        for run in ("run-1", "run-2"):
            self._settled(False)
            await self._until_registered(run)
            self._queue(run).put_nowait(self._delta(run, "OK"))
            self._queue(run).put_nowait(self._final(run))
            self.active = True
        self.assertEqual(self.sent, ["a\n\nb", "/model opus"])
        for _, task in reads:
            task.cancel()

    async def test_waiting_messages_of_another_chat_are_not_joined(self):
        """Only one session's waiting messages are joined."""
        self.active = True
        reads = await self._waiting_replies(("a", "m2", None))
        other = self._reply("x", "o1", chat="chat-2")
        other_read = asyncio.ensure_future(self._drain(other))
        await self._spin()
        reads += await self._waiting_replies(("b", "m3", None))
        self._settled(False)
        self.conn._on_sessions_changed({
            "sessionKey": self.ocp._owui_session_key(self.pipe.valves.AGENT_ID, "u1", "chat-2"),
            "hasActiveRun": False, "reason": "chat.run.settled"})
        for _ in range(200):
            if len(self.sent) == 2:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(sorted(self.sent), ["a\n\nb", "x"])
        for task in [t for _, t in reads] + [other_read]:
            task.cancel()

    async def test_a_stop_after_a_message_was_handed_on_drops_both(self):
        """A handed-on message stopped while waiting again: both dropped, both say so."""
        closed = self._record_closes()
        self.active = True
        hand_on = self.ocp._PipeTurn.hand_on

        def hand_on_then_busy(turn):
            handed = hand_on(turn)
            self.active = True   # a run from elsewhere starts before the send
            return handed
        patch = mock.patch.object(self.ocp._PipeTurn, "hand_on", hand_on_then_busy)
        patch.start()
        self.addCleanup(patch.stop)
        first = self._reply("a", "m2", intent="stop")
        first_read = asyncio.ensure_future(self._drain(first))
        await self._spin()
        last = self._reply("b", "m3", intent="stop")
        last_task = asyncio.ensure_future(last.__anext__())
        await self._spin()
        self._settled(False)
        self.assertEqual(await asyncio.wait_for(first_read, 30), "")
        await self._spin()
        last_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await last_task
        for _ in range(200):
            if len(closed) == 2:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(sorted(closed), [("m2", self.ocp.REPLY_NOT_SENT_NOTE),
                                          ("m3", self.ocp.REPLY_NOT_SENT_NOTE)])
        self.assertEqual(self.sent, [])

    async def test_waiting_messages_with_other_thinking_levels_go_apart(self):
        """One send has one thinking level, so messages with other levels are not joined."""
        self.active = True
        reads = []
        for text, mid, level in (("a", "m2", "low"), ("b", "m3", "high")):
            r = self._reply(text, mid, thinking=level)
            reads.append(asyncio.ensure_future(self._drain(r)))
            await self._spin()
        self._settled(False)
        await self._until_registered("run-2")
        self.assertEqual(self.sent, ["a", "b"])
        for task in reads:
            task.cancel()

    async def test_a_failed_joint_send_tells_the_handed_on_replies(self):
        """A failed joint send: its reply shows the error and the handing-on reply says so."""
        closed = self._record_closes()
        self.active = True
        reads = await self._waiting_replies(("a", "m2", None), ("b", "m3", None))
        self.send_fails = RuntimeError("gateway said no")
        self._settled(False)
        out = await asyncio.wait_for(reads[1][1], 30)
        self.assertIn("Error sending message", out)
        for _ in range(200):
            if closed:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(closed, [("m2", self.ocp.REPLY_SEND_FAILED_NOTE)])

    async def test_a_stop_while_messages_wait_drops_them_all(self):
        """Stop while messages wait: each is dropped and says so."""
        closed = self._record_closes()
        self.active = True
        replies = []
        for text, mid in (("a", "m2"), ("b", "m3")):
            r = self._reply(text, mid, intent="stop")
            t = asyncio.ensure_future(r.__anext__())
            await self._spin()
            replies.append(t)
        for t in replies:
            t.cancel()
        for t in replies:
            with self.assertRaises(asyncio.CancelledError):
                await t
        for _ in range(200):
            if len(closed) == 2:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(sorted(closed), [("m2", self.ocp.REPLY_NOT_SENT_NOTE),
                                          ("m3", self.ocp.REPLY_NOT_SENT_NOTE)])
        self.assertEqual(self.sent, [])

    def _record_status_closes(self):
        """The replies whose open status line was closed after they ended."""
        closed = []

        async def close(ref):
            closed.append(ref.get("message_id"))
        patch = mock.patch.object(self.ocp, "_close_reply_status", close, create=True)
        patch.start()
        self.addCleanup(patch.stop)
        return closed

    def _owui_request(self, text, msg_id, *, question=False, between_chunks=None, after_stream=None):
        """Run a reply as Open WebUI does, in a request task registered under its task id.
        `question`: the card is cancelled. `between_chunks`: awaited after each chunk."""
        self.emitted = getattr(self, "emitted", [])

        async def emitter(event):
            self.emitted.append(event)

        async def event_call(event):
            if event.get("type") == "request:user_input":
                return {"status": "cancelled", "answers": {}}
            return None
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": text}]}
        meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": msg_id, "task_id": "task-1"}
        out = []

        # Open WebUI's response object holds the stream: a Stop in its own code leaves the
        # generator suspended, never closed.
        self.streams = getattr(self, "streams", [])
        stream = self.pipe._pipe_impl(body, emitter, __metadata__=meta,
                                      __event_call__=event_call if question else None)
        self.streams.append(stream)

        async def request():
            async for chunk in stream:
                out.append(chunk)
                if between_chunks is not None:
                    await between_chunks()
            if after_stream is not None:
                await after_stream()   # Open WebUI's own work at the stream's end
        tasks_mod = types.ModuleType("open_webui.tasks")
        tasks_mod.tasks = {}
        patch = mock.patch.dict(sys.modules, {"open_webui.tasks": tasks_mod})
        patch.start()
        self.addCleanup(patch.stop)
        tasks_mod.tasks["task-1"] = task = asyncio.ensure_future(request())
        return task, out

    async def _stopped_as_it_ends(self, between_chunks=None):
        closed = self._record_status_closes()
        rows_asked = asyncio.Event()

        async def usage_rows(*a, **k):
            rows_asked.set()
            await asyncio.Event().wait()   # the Gateway calls, under way when Stop lands
        # Stop before asyncTearDown stops the class's patch of the same name, or that mock stays.
        with mock.patch.object(self.ocp, "_build_usage_status_lines", usage_rows):
            await self._stopped_as_it_ends_with_rows_pending(closed, rows_asked, between_chunks)

    async def _stopped_as_it_ends_with_rows_pending(self, closed, rows_asked, between_chunks):
        reached = asyncio.Event()

        async def owui_work():
            if any("Which?" in c for c in out if isinstance(c, str)):
                reached.set()
                await asyncio.Event().wait()   # Open WebUI busy with the chunk when Stop lands
        task, out = self._owui_request("ask me", "m1", question=True,
                                       between_chunks=owui_work if between_chunks else None)
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b"))
        q.put_nowait(self._final("run-1"))
        await asyncio.wait_for((reached if between_chunks else rows_asked).wait(), 30)
        self.assertIn("Which?", "".join(c for c in out if isinstance(c, str)))
        task.cancel()                    # the Stop that sends the new message
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self._spin()
        statuses = [e["data"] for e in self.emitted if e.get("type") == "status"]
        self.assertFalse(statuses[-1]["done"], f"precondition: the reply's status is open: {statuses}")
        self.assertEqual(closed, ["m1"])

    async def test_a_reply_stopped_as_it_ends_closes_its_status(self):
        """ELI-200: a Stop that cancels a reply while its last status line waits for usage rows
        still closes that status, so it does not shimmer "Thinking..." forever."""
        await self._stopped_as_it_ends()

    async def test_a_stop_landing_in_open_webuis_own_code_closes_it_too(self):
        """A Stop landing in Open WebUI's own code closes the status too."""
        await self._stopped_as_it_ends(between_chunks=True)

    async def test_a_cancelled_reply_has_its_saved_status_checked(self):
        """A cancelled request's save can race the reply's last status write, so a cancelled
        request always has its saved status checked."""
        closed = self._record_status_closes()
        at_end = asyncio.Event()

        async def owui_end_work():
            at_end.set()
            await asyncio.Event().wait()
        task, out = self._owui_request("hello", "m1", after_stream=owui_end_work)
        await self._until_registered("run-1")
        self._queue("run-1").put_nowait(self._delta("run-1", "hi"))
        self._queue("run-1").put_nowait(self._final("run-1"))
        await asyncio.wait_for(at_end.wait(), 30)
        statuses = [e["data"] for e in self.emitted if e.get("type") == "status"]
        self.assertTrue(statuses[-1]["done"], "precondition: the reply closed its own status")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self._spin()
        self.assertEqual(closed, ["m1"])

    async def test_a_reply_that_ended_leaves_its_last_status_alone(self):
        """A reply that wrote its last status before its request ended keeps it."""
        closed = self._record_status_closes()
        task, out = self._owui_request("hello", "m1")
        await self._until_registered("run-1")
        self._queue("run-1").put_nowait(self._delta("run-1", "hi"))
        self._queue("run-1").put_nowait(self._final("run-1"))
        await asyncio.wait_for(task, 30)
        await self._spin()
        statuses = [e["data"] for e in self.emitted if e.get("type") == "status"]
        self.assertTrue(statuses[-1]["done"])
        self.assertEqual(closed, [])

    def _record_publishes(self):
        """The replies `_publish_chat_finished` was called for."""
        published = []

        async def publish(chat_id, message_id, user_id, text=None):
            published.append((chat_id, message_id, user_id))
        patch = mock.patch.object(self.ocp, "_publish_chat_finished", publish)
        patch.start()
        self.addCleanup(patch.stop)
        return published

    async def _ended_reply(self):
        task, out = self._owui_request("hello", "m1")
        await self._until_registered("run-1")
        self._queue("run-1").put_nowait(self._delta("run-1", "hi"))
        self._queue("run-1").put_nowait(self._final("run-1"))
        await asyncio.wait_for(task, 30)
        await self._spin()

    async def test_a_reply_that_ended_in_its_request_publishes_chat_finished(self):
        """PUBLISH_CHAT_FINISHED: a reply that ended in its request is published once, after Open
        WebUI's own save."""
        published = self._record_publishes()
        self.pipe.valves.PUBLISH_CHAT_FINISHED = True
        await self._ended_reply()
        self.assertEqual(published, [("chat-1", "m1", "u1")])

    async def test_no_chat_finished_when_the_valve_is_off(self):
        published = self._record_publishes()
        await self._ended_reply()
        self.assertEqual(published, [])

    async def test_a_cancelled_request_publishes_no_chat_finished(self):
        """A Stop cancels the request: nothing finished to tell."""
        published = self._record_publishes()
        self._record_status_closes()
        self.pipe.valves.PUBLISH_CHAT_FINISHED = True
        at_end = asyncio.Event()

        async def owui_end_work():
            at_end.set()
            await asyncio.Event().wait()
        task, out = self._owui_request("hello", "m1", after_stream=owui_end_work)
        await self._until_registered("run-1")
        self._queue("run-1").put_nowait(self._delta("run-1", "hi"))
        self._queue("run-1").put_nowait(self._final("run-1"))
        await asyncio.wait_for(at_end.wait(), 30)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self._spin()
        self.assertEqual(published, [])


class GatewayImageBlockTests(_TurnCase):
    """ELI-322: a tool-made image reaches only the stored message as an `image` block; the live
    final has only text (lifecycle end, then the final)."""

    SK = None
    _collect = staticmethod(NoDroppedMessagesTests._collect)

    BLOCK = {
        "type": "image",
        "artifactId": "artifact_managed_image_68dbed25-cf2c-438d-b82e-4f39c206d52e",
        "url": "/api/chat/media/outgoing/agent%3Amain%3Aopenwebui-u1-chat-1/"
               "68dbed25-cf2c-438d-b82e-4f39c206d52e/full",
        "alt": "codex-image-generation-2---05e44654-900b-456e-b597-185a71329378.png",
        "mimeType": "image/png", "width": 1254, "height": 1254, "sizeBytes": 2064532,
    }

    def _stored(self, run_id, *blocks, push=True):
        """The run's stored message, in `chat.history` and (`push`) as the
        Gateway pushes it (`session.message`, ELI-381)."""
        message = {"role": "assistant", "content": [{"type": "text", "text": "Here it is."}, *blocks],
                   "__openclaw": {"runId": run_id, "runTerminal": True}}
        self.history = {"messages": [{"role": "user", "content": "draw a cactus"}, message]}
        if push:
            self._run_message(message)

    def _lifecycle_end(self, run_id):
        return {"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": run_id,
            "stream": "lifecycle", "data": {"phase": "end"}}}

    def _media_dir(self):
        media_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_dir, True)
        patch = mock.patch.object(self.ocp, "MEDIA_DIR", media_dir)
        patch.start()
        self.addCleanup(patch.stop)
        return media_dir

    async def _turn(self, text):
        gen = self._reply("draw a cactus", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", text))
        q.put_nowait(self._lifecycle_end("run-1"))
        q.put_nowait(self._final("run-1"))
        return await self._collect(first, gen)

    async def test_a_stored_image_block_is_fetched_uploaded_and_shown(self):
        media_dir = self._media_dir()
        self.pipe.valves.GATEWAY_URL = "localhost:18789"
        self.pipe.valves.GATEWAY_TOKEN = "gw-token"
        self.pipe.valves.OWUI_API_KEY = "owui-key"
        self._stored("run-1", self.BLOCK)
        uploads = []

        def upload(path, base_url, token):
            with open(path, "rb") as f:
                uploads.append((os.path.basename(path), f.read(), token))
            return {"id": "f1", "meta": {"content_type": "image/png"}}

        with mock.patch.object(self.ocp, "_fetch_gateway_media", return_value=b"PNG") as fetch, \
             mock.patch.object(self.ocp, "_upload_owui_file", upload):
            out, _ = await self._turn("Here it is.")

        fetch.assert_called_once_with("localhost:18789", "gw-token", self.BLOCK["url"])
        self.assertEqual(len(uploads), 1)
        fname, raw, token = uploads[0]
        self.assertEqual((raw, token), (b"PNG", "owui-key"))
        self.assertRegex(fname, r"^codex-image-generation-2---05e44654-[0-9a-f-]+-[0-9a-f]{8}\.png$")
        self.assertTrue(os.path.isfile(os.path.join(media_dir, fname)))
        self.assertTrue(out.startswith("Here it is."))
        self.assertIn(f"![{fname}](/api/v1/files/f1/content)", out)
        self.assertIn({"type": "files", "data": {"files": [
            {"id": "f1", "meta": {"content_type": "image/png"}}]}}, self.emitted)
        self.assertEqual(self.rpc.count("chat.history"), 0,
                         "the images and the usage line come from the run's pushed message")

    def _image_made(self, run_id):
        return {"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": run_id, "stream": "codex_app_server.item",
            "data": {"phase": "completed", "itemId": "exec-1", "type": "imageGeneration"}}}

    def _attached_late(self, attach_after=None, appended=True):
        """The Gateway stores the image block after the final, once the reply
        waits for it (`attach_after` s after the first look; None: never): with
        `appended` (2026.10.10) as a `<run>:assistant-media:1` message and its
        `session.message` push, else in the run's own message with only a
        `sessions.changed` phase `message` push (2026.10.5+). Returns the
        `chat.history` reads."""
        reads = []
        send_request = self.conn.send_request
        self._stored("run-1")

        def attach():
            if appended:
                media = {"role": "assistant", "content": [self.BLOCK], "model": "gateway-injected",
                         "__openclaw": {"idempotencyKey": "run-1:assistant-media:1"}}
                self.history["messages"].append(media)
                self._run_message(media)
                return
            self._stored("run-1", self.BLOCK, push=False)
            self.conn._on_sessions_changed({"sessionKey": self.SK, "phase": "message", "ts": 1})

        looked = []
        run_transcript = self.conn.run_transcript

        def first_look(session_key, run_id):
            if not looked and attach_after is not None:
                asyncio.get_running_loop().call_later(attach_after, attach)
            looked.append(1)
            return run_transcript(session_key, run_id)
        self.conn.run_transcript = first_look

        async def counted(method, params, timeout=10):
            if method == "chat.history":
                reads.append(1)
            return await send_request(method, params, timeout=timeout)
        self.conn.send_request = counted
        return reads

    async def test_an_image_the_run_made_is_read_once_it_is_attached(self):
        self._media_dir()
        reads = self._attached_late(0.05)
        gen = self._reply("draw a cactus", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._image_made("run-1"))
        q.put_nowait(self._delta("run-1", "Here it is."))
        q.put_nowait(self._lifecycle_end("run-1"))
        q.put_nowait(self._final("run-1"))
        with mock.patch.object(self.ocp, "_fetch_gateway_media", return_value=b"PNG") as fetch, \
             mock.patch.object(self.ocp, "_upload_owui_file",
                               return_value={"id": "f1", "meta": {"content_type": "image/png"}}):
            out, _ = await self._collect(first, gen)
        fetch.assert_called_once()
        self.assertEqual(len(reads), 0, "the append's push wakes the wait and carries the image")
        self.assertIn("](/api/v1/files/f1/content)", out)

    async def test_an_image_never_attached_ends_the_reply_after_the_wait(self):
        reads = self._attached_late(None)
        gen = self._reply("draw a cactus", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._image_made("run-1"))
        q.put_nowait(self._delta("run-1", "Here it is."))
        q.put_nowait(self._lifecycle_end("run-1"))
        with mock.patch.object(self.ocp, "_RUN_IMAGES_MAX_WAIT_S", 0.1, create=True):
            out, _ = await self._collect(first, gen)
        self.assertEqual(out, "Here it is.")
        self.assertEqual(len(reads), 0, "no push, no read: the transcript is not polled")

    async def test_a_run_that_made_no_image_reads_no_transcript(self):
        """ELI-381: `chat.history` takes seconds on a big chat; the run's
        messages came pushed."""
        reads = self._attached_late(None)
        out, _ = await self._turn("Here it is.")
        self.assertEqual(out, "Here it is.")
        self.assertEqual(len(reads), 0)

    async def test_another_runs_image_is_not_shown(self):
        self._stored("run-0", self.BLOCK)
        with mock.patch.object(self.ocp, "_fetch_gateway_media") as fetch:
            out, _ = await self._turn("Here it is.")
        fetch.assert_not_called()
        self.assertEqual(out, "Here it is.")

    async def test_a_reply_that_showed_media_directives_shows_no_blocks(self):
        """A file a MEDIA: directive already showed is not shown again as a block."""
        self._media_dir()
        self._stored("run-1", self.BLOCK)
        with mock.patch.object(self.ocp, "_fetch_gateway_media") as fetch, \
             mock.patch.object(self.ocp, "_resolve_media_text",
                               mock.AsyncMock(return_value=("![a.png](/x)", True))):
            out, _ = await self._turn("Here it is.\nMEDIA:a.png\n")
        fetch.assert_not_called()
        self.assertEqual(out.count("!["), 1)

    async def test_an_image_that_cannot_be_fetched_is_named_not_dropped(self):
        self._media_dir()
        self._stored("run-1", self.BLOCK)
        with mock.patch.object(self.ocp, "_fetch_gateway_media",
                               side_effect=urllib.error.URLError("refused")):
            out, _ = await self._turn("Here it is.")
        self.assertIn(f"an image could not be loaded: {self.BLOCK['alt']}", out)

    async def test_a_block_outside_the_media_route_is_ignored(self):
        self._stored("run-1", {"type": "image", "url": "https://elsewhere.example/x.png"})
        with mock.patch.object(self.ocp, "_fetch_gateway_media") as fetch:
            out, _ = await self._turn("Here it is.")
        fetch.assert_not_called()
        self.assertEqual(out, "Here it is.")

    # ELI-328: an image named by path (DeepSeek's `MEDIA:` arrives as `mediaUrls`, claude-cli's as
    # text) is stored as an image block a second later.
    PATH = "/config/.openclaw/media/tool-image-generation/qa-media-probe-eli.png"

    def _media_event(self, run_id, text, urls):
        return {"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": run_id, "stream": "assistant",
            "data": {"text": text, "delta": text, "mediaUrls": urls}}}

    async def _events(self, *events):
        gen = self._reply("show the image", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        for event in events:
            q.put_nowait(event)
        q.put_nowait(self._lifecycle_end("run-1"))
        q.put_nowait(self._final("run-1"))
        with mock.patch.object(self.ocp, "_RUN_IMAGES_MAX_WAIT_S", 0.3, create=True), \
             mock.patch.object(self.ocp, "_fetch_gateway_media", return_value=b"PNG") as fetch, \
             mock.patch.object(self.ocp, "_upload_owui_file",
                               return_value={"id": "f1", "meta": {"content_type": "image/png"}}):
            out, _ = await self._collect(first, gen)
        return out, fetch

    async def test_a_path_in_media_urls_waits_for_its_stored_block(self):
        self._media_dir()
        reads = self._attached_late(0.05, appended=False)
        out, fetch = await self._events(self._media_event("run-1", "Here it is.", [self.PATH]))
        fetch.assert_called_once()
        self.assertEqual(len(reads), 1, "a rewrite comes with no message: read once, after it")
        self.assertTrue(out.startswith("Here it is."))
        self.assertIn("](/api/v1/files/f1/content)", out)
        self.assertNotIn("MEDIA:", out)

    async def test_a_path_stored_in_a_message_the_gateway_appends_is_shown(self):
        self._media_dir()
        reads = self._attached_late(0.05)
        out, fetch = await self._events(self._media_event("run-1", "Here it is.", [self.PATH]))
        fetch.assert_called_once()
        self.assertEqual(len(reads), 0, "the append's push carries the block")
        self.assertIn("](/api/v1/files/f1/content)", out)
        self.assertNotIn("could not be loaded", out)

    async def test_a_path_directive_in_the_text_shows_the_stored_block_not_a_link(self):
        """The file server never had a path: its link was a broken image."""
        self._media_dir()
        self._attached_late(0.05)
        out, fetch = await self._events(self._delta("run-1", f"Here it is.\nMEDIA:{self.PATH}\n"))
        fetch.assert_called_once()
        self.assertNotIn("MEDIA:", out)
        self.assertNotIn(":18791", out)
        self.assertNotIn(self.PATH, out)
        self.assertEqual(out.count("!["), 1)
        self.assertIn("](/api/v1/files/f1/content)", out)

    async def test_a_path_the_gateway_could_not_store_is_named(self):
        self._run_message({
            "role": "assistant", "__openclaw": {"runId": "run-1"}, "content": [
                {"type": "text", "text": "Here it is."},
                {"type": "attachment_error", "attachment": {
                    "code": "file-not-found", "kind": "image", "label": "qa-media-probe-eli.png"}}]})
        out, fetch = await self._events(self._media_event("run-1", "Here it is.", [self.PATH]))
        fetch.assert_not_called()
        self.assertIn("an image could not be loaded: qa-media-probe-eli.png", out)

    async def test_a_path_never_stored_is_named_after_the_wait(self):
        out, fetch = await self._events(self._media_event("run-1", "Here it is.", [self.PATH]))
        fetch.assert_not_called()
        self.assertIn("an image could not be loaded: qa-media-probe-eli.png", out)

    async def test_a_bare_name_in_media_urls_is_shown_as_its_directive(self):
        """The wrapper's bare name, on a model whose line the Gateway took out."""
        media_dir = self._media_dir()
        with open(os.path.join(media_dir, "file-1.png"), "wb") as f:
            f.write(b"PNG")
        out, fetch = await self._events(self._media_event("run-1", "Here it is.", ["file-1.png"]))
        fetch.assert_not_called()
        self.assertIn("![file-1.png](/api/v1/files/f1/content)", out)
        self.assertEqual(out.count("!["), 1)


class RunMessagesTests(_TurnCase):
    """ELI-381: a reply's usage line comes from its run's messages as the Gateway
    pushes them (`session.message`), not a `chat.history` read after every reply,
    which takes seconds on a big chat; the read is left for when a push may be lost."""

    SK = None
    _collect = staticmethod(NoDroppedMessagesTests._collect)
    USAGE = {"input": 10, "cacheRead": 2000, "output": 5}

    def _push(self, message):
        self._run_message(message)

    async def _turn(self, during=None):
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        if during:
            during()
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "hi"))
        q.put_nowait({"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": "run-1", "stream": "lifecycle", "data": {"phase": "end"}}})
        q.put_nowait(self._final("run-1"))
        out, _ = await self._collect(first, gen)
        called = self.ocp._build_usage_status_lines.await_args
        return out, called.kwargs.get("run_usage") if called else None

    async def test_the_usage_line_takes_the_runs_pushed_usage(self):
        self._push({"role": "assistant", "content": "hi", "usage": self.USAGE,
                    "__openclaw": {"runId": "run-1"}})
        self._push({"role": "assistant", "content": "other", "usage": {"input": 99},
                    "__openclaw": {"runId": "run-0"}})
        _, usage = await self._turn()
        self.assertEqual(usage, self.USAGE)
        self.assertNotIn("chat.history", self.rpc)

    async def test_a_cli_reply_is_found_by_its_key(self):
        self._push({"role": "assistant", "content": "hi", "usage": self.USAGE,
                    "idempotencyKey": "cli-assistant:run-1"})
        _, usage = await self._turn()
        self.assertEqual(usage, self.USAGE)

    async def test_after_a_socket_gap_the_usage_is_read_from_chat_history(self):
        """The Gateway has no replay: a push sent in the gap is lost."""
        self.history = {"messages": [{"role": "assistant", "content": "hi", "usage": self.USAGE,
                                      "__openclaw": {"runId": "run-1"}}]}
        _, usage = await self._turn(during=self.conn._wake_session_waiters)
        self.assertEqual(usage, self.USAGE)
        self.assertEqual(self.rpc.count("chat.history"), 1)

    async def test_a_rewritten_message_is_read_from_chat_history(self):
        """A rewrite comes as `sessions.changed` phase `message`, without the message."""
        self.history = {"messages": [{"role": "assistant", "content": "hi", "usage": self.USAGE,
                                      "__openclaw": {"runId": "run-1"}}]}
        _, usage = await self._turn(during=lambda: self.conn._on_sessions_changed(
            {"sessionKey": self.SK, "phase": "message", "ts": 1}))
        self.assertEqual(usage, self.USAGE)
        self.assertEqual(self.rpc.count("chat.history"), 1)

    async def test_the_runs_messages_go_when_its_reply_ends(self):
        self._push({"role": "assistant", "content": "hi", "usage": self.USAGE,
                    "__openclaw": {"runId": "run-1"}})
        await self._turn()
        self.assertEqual(self.conn.run_transcript(self.SK, "run-1"), {"messages": []})

    async def test_without_a_message_subscription_the_usage_is_read_from_chat_history(self):
        """A failed subscribe gets no pushes for the run."""
        self.history = {"messages": [{"role": "assistant", "content": "hi", "usage": self.USAGE,
                                      "__openclaw": {"runId": "run-1"}}]}

        async def refused(session_key):
            pass
        self.conn.ensure_message_subscription = refused
        _, usage = await self._turn()
        self.assertEqual(usage, self.USAGE)
        self.assertEqual(self.rpc.count("chat.history"), 1)

    def test_a_run_no_reply_follows_is_not_kept(self):
        """A wake's run, say: nobody reads its messages."""
        self.conn._on_session_message({"sessionKey": self.SK, "message": {
            "role": "assistant", "usage": self.USAGE, "__openclaw": {"runId": "wake-1"}}})
        self.assertEqual(self.conn._run_messages, {})

    def test_an_idle_session_drops_what_no_reply_reads(self):
        """A reply parked and never claimed, say, never forgets its run."""
        q = self.conn.register_consumer(self.SK, "run-1")
        self._push({"role": "assistant", "usage": self.USAGE, "__openclaw": {"runId": "run-1"}})
        self.conn._consumers.pop(f"{self.SK}:run-1")
        self.assertTrue(self.conn._run_messages)
        self.conn._on_sessions_changed({"sessionKey": self.SK, "hasActiveRun": False, "ts": 1})
        self.assertEqual(self.conn._run_messages, {})
        del q

    async def test_a_closed_connection_drops_every_runs_messages(self):
        """A retired connection closes once its replies end; nothing reads it after."""
        self.conn.register_consumer(self.SK, "run-1")
        self._push({"role": "assistant", "usage": self.USAGE, "__openclaw": {"runId": "run-1"}})
        self.conn._ws = None
        await self.conn.disconnect()
        self.assertEqual(self.conn._run_messages, {})

    async def test_a_reply_a_later_message_was_steered_into_keeps_the_runs_messages(self):
        """That message's reply continues the run and reads them."""
        def steered():
            self._push({"role": "assistant", "content": [self._image()], "__openclaw": {"runId": "run-1"}})
            # The steered message's reply takes a queue on the run (`_steer`).
            continuing.append(self.conn.register_consumer(self.SK, "run-1"))
            self._queue("run-1").put_nowait({"type": self.ocp._STEERED_MARKER_TYPE, "into": "run-1"})
        continuing = []
        await self._turn(during=steered)
        self.assertEqual(len(self.conn.run_transcript(self.SK, "run-1")["messages"]), 1)
        self.conn.unregister_consumer(self.SK, "run-1", queue=continuing[0])
        self.conn.forget_run_transcript(self.SK, "run-1")
        self.assertEqual(self.conn.run_transcript(self.SK, "run-1"), {"messages": []},
                         "gone once the continuing reply ends")

    def _image(self):
        return {"type": "image", "url": "/api/chat/media/outgoing/x/full", "alt": "a.png"}

    def test_a_kept_message_holds_only_what_a_reply_reads(self):
        self.conn.register_consumer(self.SK, "run-1")
        self._push({"role": "assistant", "usage": self.USAGE, "model": "m",
                    "content": [{"type": "text", "text": "long text " * 1000},
                                {"type": "toolCall", "arguments": {"x": 1}}, self._image()],
                    "__openclaw": {"runId": "run-1", "seq": 4}})
        self.assertEqual(self.conn.run_transcript(self.SK, "run-1")["messages"], [{
            "role": "assistant", "usage": self.USAGE, "__openclaw": {"runId": "run-1"},
            "content": [self._image()]}])

    def test_a_message_names_its_runs(self):
        ids = self.ocp._transcript_run_ids
        self.assertEqual(ids({"__openclaw": {"runId": "r1"}}), {"r1"})
        self.assertEqual(ids({"idempotencyKey": "cli-assistant:r2"}), {"r2"})
        self.assertEqual(ids({"__openclaw": {"idempotencyKey": "r3:assistant-media:2"}}), {"r3"})
        self.assertEqual(ids({"role": "assistant"}), set())


class CloseUnsentReplyTests(unittest.IsolatedAsyncioTestCase):
    """`_close_unsent_reply` (ELI-188): the interrupted reply's last write."""

    async def _close_status(self, saved_history, release_first=True):
        import openclaw_pipe as ocp
        events, order = [], []

        class Chats:
            @staticmethod
            async def get_message_by_id_and_message_id(chat_id, message_id):
                order.append("read")
                return {"role": "assistant", "done": True, "statusHistory": copy.deepcopy(saved_history)}

        async def get_event_emitter(meta, update_db=True):
            async def emit(event):
                order.append(event["type"])
                events.append((meta["message_id"], update_db, copy.deepcopy(event)))
            return emit
        release = asyncio.Event()

        async def request():
            await release.wait()
            order.append("request-ended")
        req = asyncio.ensure_future(request())
        tasks_mod = types.ModuleType("open_webui.tasks")
        tasks_mod.tasks = {"task-1": req}
        chats_mod = types.ModuleType("open_webui.models.chats")
        chats_mod.Chats = Chats
        modules = {**owui_modules(get_event_emitter=get_event_emitter), "open_webui.tasks": tasks_mod,
                   "open_webui.models": types.ModuleType("open_webui.models"),
                   "open_webui.models.chats": chats_mod}
        with mock.patch.dict(sys.modules, modules):
            close = asyncio.ensure_future(ocp._close_reply_status(
                {"chat_id": "chat-1", "user_id": "u1", "message_id": "m1", "request_task_id": "task-1"}))
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertEqual(order, [], "Open WebUI's own save at the request's end comes first")
            release.set()
            await asyncio.wait_for(close, 5)
        return order, events

    async def test_a_saved_open_status_is_closed_after_the_request(self):
        """`_close_reply_status` (ELI-200): a saved message still ending on an open status gets a
        done one, saved (update_db) so a reload shows it ended."""
        order, events = await self._close_status([{"description": "Thinking...", "done": False}])
        self.assertEqual(order, ["request-ended", "read", "status"])
        self.assertEqual(events, [("m1", True, {"type": "status",
                                                "data": {"description": "", "done": True}})])

    async def test_a_saved_closed_status_is_left_as_it_is(self):
        """The reply's own last status reached the saved message: nothing to add."""
        order, events = await self._close_status([{"description": "Thinking...", "done": False},
                                                  {"description": "🧠 10/200k", "done": True}])
        self.assertEqual(events, [])

    async def test_it_waits_for_the_request_then_ends_the_message_with_the_note(self):
        import openclaw_pipe as ocp
        events, upserts, order = [], [], []
        chat = {"history": {"messages": {"m2": {
            "role": "assistant", "content": "", "done": True,
            "statusHistory": [{"description": "Queued: waiting for the current run to finish…",
                               "done": False}]}}}}

        class Chats:
            @staticmethod
            async def get_message_by_id_and_message_id(chat_id, message_id):
                return copy.deepcopy(chat["history"]["messages"].get(message_id))

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                order.append("upsert")
                upserts.append((chat_id, message_id, copy.deepcopy(message)))

        async def get_event_emitter(meta, update_db=True):
            async def emit(event):
                order.append(event["type"])
                events.append((meta["message_id"], update_db, copy.deepcopy(event)))
            return emit

        release = asyncio.Event()

        async def request():
            await release.wait()
            order.append("request-ended")
        req = asyncio.ensure_future(request())
        chats_mod = types.ModuleType("open_webui.models.chats")
        chats_mod.Chats = Chats
        tasks_mod = types.ModuleType("open_webui.tasks")
        tasks_mod.tasks = {"task-2": req}
        modules = {**owui_modules(get_event_emitter=get_event_emitter),
                   "open_webui.models": types.ModuleType("open_webui.models"),
                   "open_webui.models.chats": chats_mod, "open_webui.tasks": tasks_mod}
        conn = _GatewayConnection(lambda: None)
        ref = {"chat_id": "chat-1", "user_id": "u1", "message_id": "m2",
               "request_task_id": "task-2"}
        with mock.patch.dict(sys.modules, modules):
            close = asyncio.ensure_future(ocp._close_unsent_reply(
                conn, ref, ocp.REPLY_SENT_WITH_NEXT_NOTE))
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertEqual(order, [], "Open WebUI's own end-of-request save comes first")
            release.set()
            await asyncio.wait_for(close, 5)
        self.assertEqual(order[0], "request-ended")
        note = ocp.REPLY_SENT_WITH_NEXT_NOTE
        (cid, mid, msg), = upserts
        self.assertEqual((cid, mid, msg["content"], msg["done"]), ("chat-1", "m2", note, True))
        self.assertEqual(msg["output"][-1]["content"][0]["text"], note)
        status = [e for m, db, e in events if e["type"] == "status"]
        self.assertEqual([s["data"]["done"] for s in status], [True], "the Queued line ends")
        self.assertTrue(all(db for m, db, e in events if e["type"] == "status"),
                        "the status is saved, so a reload shows it ended")
        done = [e for m, db, e in events if e["type"] == "chat:completion"]
        self.assertEqual(done[-1]["data"]["content"], note)
        self.assertTrue(done[-1]["data"]["done"])

    async def test_text_the_reply_already_showed_is_kept(self):
        import openclaw_pipe as ocp
        upserts = []

        class Chats:
            @staticmethod
            async def get_message_by_id_and_message_id(chat_id, message_id):
                return {"role": "assistant", "content": "earlier run text", "done": True,
                        "output": [{"type": "message", "id": "x", "status": "completed",
                                    "role": "assistant", "content": [
                                        {"type": "output_text", "text": "earlier run text"}]}]}

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                upserts.append(copy.deepcopy(message))

        async def get_event_emitter(meta, update_db=True):
            async def emit(event):
                pass
            return emit
        chats_mod = types.ModuleType("open_webui.models.chats")
        chats_mod.Chats = Chats
        modules = {**owui_modules(get_event_emitter=get_event_emitter),
                   "open_webui.models": types.ModuleType("open_webui.models"),
                   "open_webui.models.chats": chats_mod}
        with mock.patch.dict(sys.modules, modules):
            await ocp._close_unsent_reply(
                _GatewayConnection(lambda: None),
                {"chat_id": "c", "user_id": "u", "message_id": "m"}, ocp.REPLY_NOT_SENT_NOTE)
        (msg,) = upserts
        self.assertEqual(msg["content"], f"earlier run text\n\n{ocp.REPLY_NOT_SENT_NOTE}")
        self.assertEqual(len(msg["output"]), 2)


class SessionActiveSignalsTests(unittest.TestCase):
    """ELI-56: during sessions_yield describe.status says 'done' while sessions.list's hasActiveRun
    stays true. That gap counts as active."""

    def test_running_status_is_active_regardless_of_list(self):
        self.assertTrue(_session_active_from_signals("running", False))
        self.assertTrue(_session_active_from_signals("streaming", None))
        self.assertTrue(_session_active_from_signals("queued", False))

    def test_yield_gap_done_status_but_list_active_is_active(self):
        self.assertTrue(_session_active_from_signals("done", True))

    def test_truly_idle_is_not_active(self):
        self.assertFalse(_session_active_from_signals("done", False))
        self.assertFalse(_session_active_from_signals("idle", False))
        self.assertFalse(_session_active_from_signals("failed", False))

    def test_unknown_status_defers_to_list_signal(self):
        self.assertFalse(_session_active_from_signals(None, False))
        self.assertTrue(_session_active_from_signals(None, True))


class SubagentTaskIdMarkerRegexTests(unittest.TestCase):
    def test_extracts_task_id_from_marker(self):
        text = "*↳ Sub-agent finished: Fix bug*\n\nAll done.\n\n<!-- openclaw:taskId=task-42 -->"
        match = _SUBAGENT_TASK_ID_MARKER_RE.search(text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "task-42")

    def test_no_match_without_marker(self):
        self.assertIsNone(_SUBAGENT_TASK_ID_MARKER_RE.search("just a normal message"))


class TurnRendererTests(unittest.TestCase):
    """The shadow renderer rebuilds a run's visible content (text + tool blocks) from its event
    stream, so an interrupted turn can be finalized whole."""

    def test_accumulates_text_and_tool_block_in_order(self):
        r = _TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "Working"}})
        r.feed({"stream": "assistant", "data": {"delta": " on it. "}})
        r.feed({"stream": "tool", "data": {"phase": "start", "name": "Bash",
                                           "toolCallId": "t1", "args": {"cmd": "echo hi"}}})
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "Bash",
                                           "toolCallId": "t1", "result": "hi"}})
        r.feed({"stream": "assistant", "data": {"delta": "Done."}})
        self.assertTrue(r.visible_text.startswith("Working on it. "))
        self.assertIn('<details type="tool_calls"', r.visible_text)
        self.assertIn("Bash", r.visible_text)
        self.assertTrue(r.visible_text.rstrip().endswith("Done."))

    def test_tool_block_matches_shared_renderer(self):
        r = _TurnRenderer()
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "Read",
                                           "toolCallId": "t9", "result": "contents"}})
        expected = _render_tool_result_block("Read", "t9", "{}", "contents", "")
        self.assertIn(expected.strip(), r.visible_text)

    def test_multiline_result_survives_owui_attribute_regex(self):
        r"""A multi-line tool result stays readable by OWUI's non-dotAll attribute regex
        `/(\w+)="(.*?)"/g`: the opening tag must be single-line."""
        result = "hello from bash\nFri Jul 24 23:45:02 Asia 2026\n up 18 days"
        block = _render_tool_result_block("Bash", "t1", '{"a": 1}', result, "")

        open_tag = block.strip().split(">")[0] + ">"
        self.assertNotIn("\n", open_tag)

        # Mirror of OWUI's parser: `.` must not need to match a newline.
        attrs = dict(re.findall(r'(\w+)="(.*?)"', open_tag))
        self.assertIn("result", attrs)
        self.assertEqual(
            html.unescape(attrs["result"]).replace("\r\n", "\n"), result
        )
        self.assertEqual(html.unescape(attrs["arguments"]), '{"a": 1}')

    def test_catch_all_text_is_not_duplicated(self):
        """A final catch-all `text` (no `delta`) does not re-append what streamed."""
        r = _TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "Hello world"}})
        r.feed({"stream": "assistant", "data": {"text": "Hello world"}})
        self.assertEqual(r.visible_text, "Hello world")

    def _reply(self, r, item, *words):
        for n, word in enumerate(words):
            data = {"delta": word, "itemId": item}
            if n == 0:
                data["text"] = word
            r.feed({"stream": "assistant", "data": data})

    def test_a_second_reply_of_one_run_starts_on_a_new_paragraph(self):
        """A steered announce answered as a second reply (`itemId` `<id>:2`) starts a new
        paragraph, as the transcript rebuild does."""
        r = _TurnRenderer()
        self._reply(r, "run-1:1", "E2E_TWIN_A_HEARD", " heard-1")
        self._reply(r, "run-1:2", "E2E_TWIN_B_HEARD", " heard-1")
        self.assertEqual(r.text, "E2E_TWIN_A_HEARD heard-1\n\nE2E_TWIN_B_HEARD heard-1")
        self.assertEqual(r.visible_text, r.text)
        self.assertEqual([i["content"][0]["text"] for i in r.output], [r.text])
        reply = lambda text: {"role": "assistant", "stopReason": "stop",  # noqa: E731
                              "__openclaw": {"runId": "announce:a"}, "content": [{"type": "text", "text": text}]}
        rebuilt = _history_renderer([reply("E2E_TWIN_A_HEARD heard-1"), reply("E2E_TWIN_B_HEARD heard-1")])
        self.assertEqual(r.text, rebuilt.text)

    def test_one_reply_and_replies_without_an_item_id_stay_as_they_came(self):
        r = _TurnRenderer()
        self._reply(r, "run-1:1", "One", " reply", " in", " pieces")
        self.assertEqual(r.text, "One reply in pieces")
        r = _TurnRenderer()   # a claude-cli run: no `itemId`, its own breaks inside the delta
        r.feed({"stream": "assistant", "data": {"delta": "First."}})
        r.feed({"stream": "assistant", "data": {"delta": "\n\nSecond."}})
        self.assertEqual(r.text, "First.\n\nSecond.")

    def test_a_pipe_note_between_two_replies_does_not_hide_the_break(self):
        r = _TurnRenderer()
        self._reply(r, "run-1:1", "First.")
        r.feed({"stream": openclaw_pipe.PIPE_NOTE_STREAM,
                "data": {"id": f"{openclaw_pipe._NOTE_ITEM_PREFIX}compaction_x_1", "text": "Compacted."}})
        self._reply(r, "run-1:2", "Second.")
        self.assertEqual(r.text, "First.\n\nSecond.")

    def test_a_card_between_two_replies_adds_no_break_of_its_own(self):
        r = _TurnRenderer()
        self._reply(r, "run-1:1", "Checking.")
        r.feed({"stream": "tool", "data": {"phase": "start", "name": "Bash",
                                           "toolCallId": "t1", "args": {"cmd": "ls"}}})
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "Bash",
                                           "toolCallId": "t1", "result": "ok"}})
        self._reply(r, "run-1:2", "Done.")
        self.assertEqual(r.text, "Checking.Done.")

    def test_filters_sender_metadata(self):
        r = _TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "Sender (untrusted metadata) ..."}})
        self.assertEqual(r.visible_text, "")


class NativeToolItemTests(unittest.TestCase):
    """Tool calls as Responses output items, folded into OWUI's output list so OWUI flips the
    spinner; markdown cards are append-only and could never stop spinning."""

    def test_start_event_is_a_pending_function_call(self):
        ev = _tool_call_started_event("Bash", "call-1", '{"command": "sleep 8"}')
        self.assertEqual(ev["type"], "response.output_item.added")
        item = ev["item"]
        self.assertEqual(item["type"], "function_call")
        self.assertEqual(item["id"], "call-1")
        self.assertEqual(item["name"], "Bash")
        self.assertEqual(item["arguments"], '{"command": "sleep 8"}')
        self.assertEqual(item["status"], "in_progress")
        self.assertNotIn("output", item)

    def test_start_event_carries_no_call_id(self):
        """OWUI 0.11.1+ dedups added items on id OR call_id and replaces on a hit. The start event
        carries no call_id, so the result does not overwrite the call and remove the card."""
        start = _tool_call_started_event("Bash", "call-1", "{}")
        result = _tool_call_result_event("call-1", "x")
        self.assertNotIn("call_id", start["item"])
        # Distinct ids; the result's `call_id` equals the call's `id`.
        self.assertNotEqual(start["item"]["id"], result["item"]["id"])

    def test_result_event_matches_the_start_event_by_call_id(self):
        """The frontend pairs by `call_id ?? id`: the result's `call_id` must be the call's `id`,
        or the card spins forever."""
        start = _tool_call_started_event("Bash", "call-1", "{}")
        result = _tool_call_result_event("call-1", "total 4\ndrwxr-xr-x")
        self.assertEqual(start["item"]["id"], result["item"]["call_id"])
        self.assertEqual(result["item"]["type"], "function_call_output")
        self.assertEqual(result["item"]["status"], "completed")
        self.assertEqual(
            result["item"]["output"], [{"type": "output_text", "text": "total 4\ndrwxr-xr-x"}]
        )

    def test_item_ids_are_distinct_between_call_and_output(self):
        """Both items share one output list, so their ids must differ."""
        start = _tool_call_started_event("Bash", "call-1", "{}")
        result = _tool_call_result_event("call-1", "x")
        self.assertNotEqual(start["item"]["id"], result["item"]["id"])

    def test_payloads_are_json_serializable(self):
        """OWUI serializes yielded dicts with `json.dumps`."""
        json.dumps(_tool_call_started_event("Bash", "c", '{"a": 1}'))
        json.dumps(_tool_call_result_event("c", "out"))

    def test_result_text_is_capped(self):
        ev = _tool_call_result_event("c", "x" * 20000)
        self.assertEqual(len(ev["item"]["output"][0]["text"]), 8000)

    def test_markdown_card_path_is_unchanged(self):
        """Valve off (and the shadow renderer) still produce the finished-only markdown card."""
        block = _render_tool_result_block("Bash", "t1", "{}", "out", "")
        self.assertIn('done="true"', block)
        self.assertIn('result="out"', block)

    def test_orphan_close_reuses_the_result_event(self):
        """A call never resolved is still completed, or its card spins forever."""
        ev = _tool_call_result_event("call-1", "(no result -- the run ended first)")
        self.assertEqual(ev["item"]["type"], "function_call_output")
        self.assertEqual(ev["item"]["call_id"], "call-1")
        self.assertEqual(ev["item"]["status"], "completed")

    def test_no_valve_gates_native_items(self):
        """Native items are unconditional; no toggle."""
        valves = Pipe.Valves()
        self.assertFalse(hasattr(valves, "NATIVE_TOOL_ITEMS"))
        self.assertFalse(hasattr(valves, "SHOW_RUNNING_TOOL_CARDS"))


class ToolErrorIndicationTests(unittest.TestCase):
    """A failed tool call shows OWUI's red X (status "failed", `ToolCallDisplay.svelte` `isError`,
    0.11.1+, ELI-278), a ❌ name and why. ELI-75: see RealHandlerReplayTests."""

    def test_relabel_reannounces_the_call(self):
        """The start event again under the same id replaces the item (ELI-124)."""
        ev = _tool_call_error_relabel_event("Bash", "call-1", '{"command": "ls"}')
        started = _tool_call_started_event("Bash ❌", "call-1", '{"command": "ls"}')
        started["item"]["status"] = "failed"
        self.assertEqual(ev, started)
        self.assertEqual(ev["type"], "response.output_item.added")

    def test_relabel_marks_the_name(self):
        ev = _tool_call_error_relabel_event("Bash", "call-1", "{}")
        self.assertEqual(ev["item"]["name"], "Bash ❌")

    def test_relabel_is_json_serializable(self):
        json.dumps(_tool_call_error_relabel_event("Bash", "call-1", "{}"))

    def test_banner_precedes_the_original_output(self):
        """The banner never costs the actual error text."""
        banner = _tool_error_banner("ls: cannot access '/nope'")
        self.assertTrue(banner.startswith("❌"))
        self.assertIn("ls: cannot access '/nope'", banner)


class RealHandlerReplayTests(unittest.TestCase):
    """Replay our events through an extraction of OWUI's `handle_responses_streaming_event`
    (`test_fixture_owui_streaming_handler.py`), ELI-75."""

    def _replay(self, events, initial_output=()):
        from test_fixture_owui_streaming_handler import (
            handle_responses_streaming_event,
        )

        output = list(initial_output)
        metadata_seq = []
        for ev in events:
            output, metadata = handle_responses_streaming_event(ev, output)
            metadata_seq.append(metadata)
        return output, metadata_seq

    def test_both_items_survive_the_real_handler(self):
        """The 0.11.1 regression: both items survive and pair by `call_id ?? id`."""
        started = _tool_call_started_event("Bash", "call-1", '{"command": "ls"}')
        result = _tool_call_result_event("call-1", "total 4")

        output, _ = self._replay([started, result])

        calls = [item for item in output if item["type"] == "function_call"]
        results = [item for item in output if item["type"] == "function_call_output"]
        self.assertEqual(len(calls), 1, f"function_call was swallowed: {output}")
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["call_id"], calls[0].get("call_id") or calls[0]["id"]
        )

    def test_parallel_calls_all_survive_the_real_handler(self):
        """Interleaved parallel calls must not collide with each other either."""
        events = [
            _tool_call_started_event("Bash", "call-1", "{}"),
            _tool_call_started_event("Read", "call-2", "{}"),
            _tool_call_result_event("call-1", "a"),
            _tool_call_result_event("call-2", "b"),
        ]

        output, _ = self._replay(events)

        self.assertEqual(
            [item["type"] for item in output],
            [
                "function_call",
                "function_call",
                "function_call_output",
                "function_call_output",
            ],
            f"items were merged or dropped: {output}",
        )

    def test_relabel_lands_on_the_real_handler(self):
        started = _tool_call_started_event("Read", "call-1", '{"p": 1}')
        relabel = _tool_call_error_relabel_event("Read", "call-1", '{"p": 1}')
        result = _tool_call_result_event("call-1", "BANNER")

        output, _ = self._replay([started, relabel, result])

        self.assertEqual([i["type"] for i in output], ["function_call", "function_call_output"])
        self.assertEqual(output[0]["name"], "Read ❌")
        self.assertEqual(output[0]["arguments"], '{"p": 1}')

    def test_relabel_lands_on_a_card_that_is_not_last(self):
        """The relabel matches by id, not position."""
        preceding_text = {
            "type": "message",
            "id": "msg_1",
            "status": "in_progress",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi"}],
        }
        events = [
            _tool_call_started_event("Read", "call-1", "{}"),
            _tool_call_started_event("Bash", "call-2", "{}"),
            _tool_call_error_relabel_event("Read", "call-1", "{}"),
            _tool_call_result_event("call-1", "BANNER"),
        ]

        output, _ = self._replay(events, initial_output=[preceding_text])

        self.assertEqual([(i["type"], i.get("name")) for i in output],
                         [("message", None), ("function_call", "Read ❌"),
                          ("function_call", "Bash"), ("function_call_output", None)])

    def test_output_item_done_is_no_longer_dead_code(self):
        """`response.output_item.done` was dead on 0.11.0 and replaces the item at `output_index`
        on 0.11.1+. A watch on that branch."""
        from test_fixture_owui_streaming_handler import (
            handle_responses_streaming_event,
        )

        started = _tool_call_started_event("Read", "call-1", "{}")
        done_shape_relabel = {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "call-1",
                "name": "Read ❌",
                "arguments": "{}",
                "status": "failed",
            },
        }

        output = [started["item"]]
        output, metadata = handle_responses_streaming_event(done_shape_relabel, output)

        self.assertEqual(metadata, {})
        self.assertEqual(output[0]["name"], "Read ❌")


class GatewayReconnectStormTests(unittest.IsolatedAsyncioTestCase):
    """`_reconnect()` must not start a second `_event_loop` task; two loops racing on `recv()`
    caused a reconnect storm."""

    async def test_reconnect_never_spawns_a_task(self):
        conn = _GatewayConnection(lambda: None)
        conn._max_backoff = 0

        connect_calls = {"n": 0}

        async def fake_connect_and_start():
            connect_calls["n"] += 1
            conn._ws = mock.MagicMock()

        conn._connect_and_start = fake_connect_and_start

        with mock.patch("asyncio.create_task") as mock_create_task:
            await conn._reconnect()
            await conn._reconnect()
            await conn._reconnect()

        self.assertEqual(connect_calls["n"], 3)
        mock_create_task.assert_not_called()

    async def test_event_loop_survives_disconnect_with_a_single_task(self):
        conn = _GatewayConnection(lambda: None)
        conn._max_backoff = 0

        recv_calls = {"n": 0}

        class FakeWS:
            async def recv(self):
                recv_calls["n"] += 1
                if recv_calls["n"] == 2:
                    conn._stopped = True
                raise websockets.exceptions.ConnectionClosed("closed")

        connect_calls = {"n": 0}

        async def fake_connect_and_start():
            connect_calls["n"] += 1
            conn._ws = FakeWS()

        conn._connect_and_start = fake_connect_and_start
        conn._ws = FakeWS()

        with mock.patch("asyncio.create_task") as mock_create_task:
            await conn._event_loop()

        # One disconnect, one reconnect, then a disconnect that stops the loop.
        self.assertEqual(recv_calls["n"], 2)
        self.assertEqual(connect_calls["n"], 1)
        mock_create_task.assert_not_called()

    async def test_ensure_connected_starts_exactly_one_task(self):
        conn = _GatewayConnection(lambda: None)

        async def fake_connect_and_start():
            conn._ws = mock.MagicMock()

        conn._connect_and_start = fake_connect_and_start

        async def fake_event_loop():
            await asyncio.sleep(3600)

        conn._event_loop = fake_event_loop

        await conn.ensure_connected()
        first_task = conn._event_loop_task
        self.assertIsNotNone(first_task)

        await conn.ensure_connected()
        self.assertIs(conn._event_loop_task, first_task)

        first_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_task

    async def test_send_request_raises_clean_error_while_reconnecting(self):
        """While reconnecting `self._ws` is None; `send_request` raises a clear GatewayError, not
        an AttributeError."""
        conn = _GatewayConnection(lambda: None)
        conn._ws = None
        with self.assertRaises(GatewayError):
            await conn.send_request("sessions.describe", {"key": "k"}, timeout=1)


class TickWatchTests(unittest.IsolatedAsyncioTestCase):
    """The Gateway ticks every `policy.tickIntervalMs` (30 s) and its own client closes after twice
    that with no frame. A half-open socket never closes on its own, so the pipe does the same."""

    # The watchdog tests run on a virtual clock (VirtualClockLoop): a tick, the 2x interval window and the reconnect are ordered by the
    # loop's own timers, which move only when every task is idle, so a slow or CPU-starved runner cannot make a tick "late".
    TICK_S = 0.05                    # the interval the Gateway told us; the watchdog closes after twice that with no frame
    TICK_GAP_S = 0.03                # a tick arrives this long after the previous one: inside the window

    def conn(self):
        conn = _GatewayConnection(lambda: None)
        conn._max_backoff = 0
        conn._tick_interval_s = self.TICK_S
        return conn

    def test_a_silent_socket_is_closed_and_replaced_after_two_tick_intervals(self):
        async def scenario():
            conn = self.conn()
            loop = asyncio.get_running_loop()
            aborted, reconnects = [], []

            class HalfOpen:
                transport = types.SimpleNamespace(abort=lambda: aborted.append(True))

                async def recv(self):
                    await asyncio.sleep(3600)

            class Fresh:
                async def recv(self):
                    conn._stopped = True
                    raise websockets.exceptions.ConnectionClosed("closed")

            async def connect_and_start():
                reconnects.append(loop.time())
                conn._ws = Fresh()
            conn._connect_and_start = connect_and_start
            conn._ws = HalfOpen()
            gaps = []
            conn._mark_event_gap = lambda: gaps.append(True)
            t0 = loop.time()
            await asyncio.wait_for(conn._event_loop(), 5)
            self.assertEqual(len(reconnects), 1, "one new socket")
            self.assertAlmostEqual(reconnects[0] - t0, 2 * self.TICK_S, places=6, msg="exactly two intervals after the last frame")
            self.assertEqual(aborted, [True], "the dead socket is torn down")
            self.assertTrue(gaps, "runs learn their events may be lost")
        run_on_virtual_clock(scenario())

    def _ticking(self, conn, ticks, gap_s, after_ticks):
        """A socket that delivers `ticks` ticks `gap_s` apart and then does `after_ticks(conn)`; returns the log of (recv number, loop time)."""
        loop = asyncio.get_running_loop()
        seen = []

        class Ticking:
            async def recv(self):
                if len(seen) >= ticks:
                    return await after_ticks()
                await asyncio.sleep(gap_s)
                seen.append(loop.time())
                return json.dumps({"type": "event", "event": "tick", "payload": {"ts": len(seen)}})
        return Ticking(), seen

    def test_ticks_keep_a_quiet_socket(self):
        """Each frame restarts the window: a socket whose only traffic is ticks, for far longer than one window in total, is never replaced."""
        ticks = 40

        async def scenario():
            conn = self.conn()

            async def end():
                conn._stopped = True
                raise websockets.exceptions.ConnectionClosed("closed")

            async def connect_and_start():
                raise AssertionError("a ticking socket was replaced")
            conn._connect_and_start = connect_and_start
            conn._ws, seen = self._ticking(conn, ticks, self.TICK_GAP_S, end)
            await asyncio.wait_for(conn._event_loop(), 5)
            self.assertEqual(len(seen), ticks)
            self.assertGreater(seen[-1], 10 * 2 * self.TICK_S, "the ticks spanned many windows")
        run_on_virtual_clock(scenario())

    def test_a_socket_that_stops_ticking_is_replaced_two_intervals_after_its_last_tick(self):
        async def scenario():
            conn = self.conn()
            loop = asyncio.get_running_loop()
            replaced = []

            async def go_quiet():
                await asyncio.sleep(3600)

            class Fresh:
                async def recv(self):
                    conn._stopped = True
                    raise websockets.exceptions.ConnectionClosed("closed")

            async def connect_and_start():
                replaced.append(loop.time())
                conn._ws = Fresh()
            conn._connect_and_start = connect_and_start
            conn._ws, seen = self._ticking(conn, 5, self.TICK_GAP_S, go_quiet)
            await asyncio.wait_for(conn._event_loop(), 5)
            self.assertEqual(len(replaced), 1)
            self.assertAlmostEqual(replaced[0] - seen[-1], 2 * self.TICK_S, places=6)
        run_on_virtual_clock(scenario())

    def test_a_starved_runner_cannot_make_a_tick_late(self):
        """Real time is not the watchdog's clock: every recv here burns more REAL time than the whole window (a CPU-starved runner), and the
        socket is still kept, because on the loop's clock each tick is still inside it."""
        burn_s = 4 * 2 * self.TICK_S

        async def scenario():
            conn = self.conn()
            ws_ticks = 3

            async def end():
                conn._stopped = True
                raise websockets.exceptions.ConnectionClosed("closed")

            async def connect_and_start():
                raise AssertionError("a runner stall replaced a ticking socket")
            conn._connect_and_start = connect_and_start
            ws, seen = self._ticking(conn, ws_ticks, self.TICK_GAP_S, end)
            recv = ws.recv

            async def starved():
                time.sleep(burn_s)                     # blocks the loop for real, as a starved runner does
                return await recv()
            ws.recv = starved
            conn._ws = ws
            await asyncio.wait_for(conn._event_loop(), 5)
            self.assertEqual(len(seen), ws_ticks)
        run_on_virtual_clock(scenario())

    async def test_hello_sets_the_interval(self):
        conn = _GatewayConnection(lambda: types.SimpleNamespace(
            GATEWAY_URL="localhost:18789", GATEWAY_TOKEN="tok"))
        conn._ensure_identity = lambda valves: None
        conn._ident = {"id": "device-1"}
        self.assertEqual(conn._tick_interval_s, 30.0, "the Gateway's default before a hello")
        ws = _HandshakeWS()
        hello = json.loads(ws._frames[1])
        hello["payload"]["policy"] = {"tickIntervalMs": 12000}
        ws._frames[1] = json.dumps(hello)

        async def fake_connect(_url, **_kw):
            return ws
        with mock.patch.object(websockets, "connect", fake_connect, create=True), \
                mock.patch("openclaw_pipe._sign_challenge", lambda *a, **k: {}):
            await conn._connect_and_start()
        self.assertEqual(conn._tick_interval_s, 12.0)

    async def test_a_frame_before_the_connect_response_is_not_taken_for_it(self):
        # Any other frame taken as the response reads as a rejection with no reason.
        conn = _GatewayConnection(lambda: types.SimpleNamespace(
            GATEWAY_URL="localhost:18789", GATEWAY_TOKEN="tok"))
        conn._ensure_identity = lambda valves: None
        conn._ident = {"id": "device-1"}
        ws = _HandshakeWS()
        ws._frames.insert(1, json.dumps({"type": "event", "event": "health", "payload": {}}))

        async def fake_connect(_url, **_kw):
            return ws
        with mock.patch.object(websockets, "connect", fake_connect, create=True), \
                mock.patch("openclaw_pipe._sign_challenge", lambda *a, **k: {}):
            await conn._connect_and_start()
        self.assertEqual((conn.gateway_boot_id, ws._frames), ("boot-1", []), "the connect response was read")


class RetiredConnectionServesItsReplyTests(unittest.IsolatedAsyncioTestCase):
    """ELI-150: a redeploy mid-reply. The retired connection keeps serving its reply; the new
    connection's turn writer takes the message over only after that reply ends."""

    USER, CHAT, MSG = (str(uuid.UUID(int=1)), str(uuid.UUID(int=2)), str(uuid.UUID(int=3)))

    async def test_no_takeover_while_the_retired_connection_serves_the_reply(self):
        modules = owui_modules()
        socket_main = modules["open_webui.socket.main"]
        valves = lambda: types.SimpleNamespace(AGENT_ID="main")   # noqa: E731
        key = _owui_session_key("main", self.USER, self.CHAT)
        run = f"msg-{self.CHAT}-{self.MSG}-1792100000.5"
        old = _GatewayConnection(valves)
        old.register_consumer(key, run)                # the reply the old load streams
        socket_main._openclaw_gateway_connection_v1 = old
        new = _GatewayConnection(valves)
        spawned = []

        def spawn(coro):
            spawned.append(coro.__qualname__)
            coro.close()
        with mock.patch.dict(sys.modules, modules), \
             mock.patch("openclaw_pipe._gateway_connection", None), \
             mock.patch("openclaw_pipe._retired_connections", [], create=True), \
             mock.patch("openclaw_pipe.spawn_background", spawn):
            await _reap_stale_gateway_connection()     # the new load starts
            self.assertTrue(old._retired)

            def event(text):
                return {"sessionKey": key, "runId": run, "stream": "assistant",
                        "data": {"delta": text}}
            self.assertFalse(_turn_route_now(new, event("mid-4")))
            self.assertEqual(new._turn_pumps, {}, "no turn writer while the reply lives")

            old.unregister_consumer(key, run)          # that reply ended before its run
            self.assertTrue(_turn_route_now(new, event("mid-5")))
        self.assertIn((key, run), new._turn_pumps)
        self.assertIn("_turn_pump_loop", spawned)


class GatewayReloadReapTests(unittest.IsolatedAsyncioTestCase):
    """OWUI execs each redeploy into a new module with no teardown.
    `_reap_stale_gateway_connection` / `_remember_gateway_connection` anchor the connection on
    `open_webui.socket.main` so a deploy tears down the old one."""

    async def test_reaps_and_disconnects_a_stale_connection_from_a_prior_deploy(self):
        fake_socket_module = fake_owui(self)

        disconnect_calls = {"n": 0}

        class _FakeStaleConn:
            async def disconnect(self):
                disconnect_calls["n"] += 1

        stale = _FakeStaleConn()
        fake_socket_module._openclaw_gateway_connection_v1 = stale

        with mock.patch("openclaw_pipe._gateway_connection", None):
            await _reap_stale_gateway_connection()

        self.assertEqual(disconnect_calls["n"], 1)

    async def test_does_not_reap_when_stale_is_the_current_connection(self):
        fake_socket_module = fake_owui(self)
        conn = _GatewayConnection(lambda: None)

        disconnect_calls = {"n": 0}
        conn.disconnect = mock.AsyncMock(side_effect=lambda: disconnect_calls.__setitem__("n", disconnect_calls["n"] + 1))
        fake_socket_module._openclaw_gateway_connection_v1 = conn

        with mock.patch("openclaw_pipe._gateway_connection", conn):
            await _reap_stale_gateway_connection()

        self.assertEqual(disconnect_calls["n"], 0)

    async def test_reap_is_a_noop_when_no_anchor_present(self):
        fake_owui(self)
        with mock.patch("openclaw_pipe._gateway_connection", None):
            await _reap_stale_gateway_connection()  # must not raise

    async def test_reap_is_a_noop_outside_owui(self):
        sys.modules.pop("open_webui.socket.main", None)
        sys.modules.pop("open_webui.socket", None)
        sys.modules.pop("open_webui", None)
        with mock.patch("openclaw_pipe._gateway_connection", None):
            await _reap_stale_gateway_connection()  # must not raise

    async def test_disconnect_failure_is_swallowed_not_fatal(self):
        fake_socket_module = fake_owui(self)

        class _FakeStaleConn:
            async def disconnect(self):
                raise RuntimeError("socket already dead")

        fake_socket_module._openclaw_gateway_connection_v1 = _FakeStaleConn()

        with mock.patch("openclaw_pipe._gateway_connection", None):
            await _reap_stale_gateway_connection()  # must not raise

    async def test_old_code_stale_connection_cannot_be_revived_after_reap(self):
        # Connections without `retire()` are pinned shut on the instance.
        fake_socket_module = fake_owui(self)

        class _FakeStaleConn:
            _stopped = False

            async def disconnect(self):
                pass

            async def ensure_connected(self):
                self._stopped = False

        stale = _FakeStaleConn()
        fake_socket_module._openclaw_gateway_connection_v1 = stale
        with mock.patch("openclaw_pipe._gateway_connection", None):
            await _reap_stale_gateway_connection()

        self.assertTrue(stale._stopped)
        with self.assertRaises(GatewayError):
            await stale.ensure_connected()

    async def test_reap_retires_new_code_connection_and_it_stays_down(self):
        fake_socket_module = fake_owui(self)
        stale = _GatewayConnection(lambda: None)
        stale._connect_and_start = mock.AsyncMock()
        fake_socket_module._openclaw_gateway_connection_v1 = stale

        with mock.patch("openclaw_pipe._gateway_connection", None):
            await _reap_stale_gateway_connection()

        self.assertTrue(stale._retired)
        with self.assertRaises(GatewayError):
            await stale.ensure_connected()
        stale._connect_and_start.assert_not_called()

    async def test_retired_connection_keeps_serving_in_flight_run_then_closes(self):
        conn = _GatewayConnection(lambda: None)
        conn.register_consumer("agent:main:openwebui-u-c", "run-1")
        conn.disconnect = mock.AsyncMock()

        await conn.retire()
        conn.disconnect.assert_not_called()

        # The run's own resume path may still reconnect while it is in flight.
        async def fake_connect_and_start():
            conn._ws = object()
        conn._connect_and_start = fake_connect_and_start
        with mock.patch.object(conn, "_event_loop", mock.AsyncMock()):
            await conn.ensure_connected()
        self.assertIsNotNone(conn._ws)

        conn.unregister_consumer("agent:main:openwebui-u-c", "run-1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        conn.disconnect.assert_awaited()

        conn._ws = None
        with self.assertRaises(GatewayError):
            await conn.ensure_connected()

    async def test_reap_finds_orphans_from_older_loads_but_not_other_functions(self):
        import openclaw_pipe
        fake_owui(self)
        me = "function_openclaw_gateway_" + "a" * 32

        def make(module_name):
            cls = type("_GatewayConnection", (), {"__module__": module_name})
            obj = cls()
            obj.retire = mock.AsyncMock()
            return obj

        orphan_a = make("function_openclaw_gateway_" + "b" * 32)
        orphan_b = make("function_openclaw_gateway_" + "c" * 32)
        action_conn = make("function_openclaw_status_action_" + "d" * 32)
        same_load = make(me)

        with mock.patch.dict(openclaw_pipe.__dict__, {"__name__": me}), \
                mock.patch("openclaw_pipe._gateway_connection", None):
            await _reap_stale_gateway_connection()

        orphan_a.retire.assert_awaited_once()
        orphan_b.retire.assert_awaited_once()
        action_conn.retire.assert_not_called()
        same_load.retire.assert_not_called()

    def test_function_module_prefix(self):
        import openclaw_pipe
        self.assertEqual(
            openclaw_pipe._function_module_prefix("function_openclaw_gateway_" + "f" * 32),
            "function_openclaw_gateway",
        )
        self.assertEqual(openclaw_pipe._function_module_prefix("openclaw_pipe"), "openclaw_pipe")
        self.assertEqual(
            openclaw_pipe._function_module_prefix("function_x_notahexsuffix"),
            "function_x_notahexsuffix",
        )

    def test_remember_stores_connection_on_owui_socket_module(self):
        fake_socket_module = fake_owui(self)
        conn = _GatewayConnection(lambda: None)

        _remember_gateway_connection(conn)

        self.assertIs(fake_socket_module._openclaw_gateway_connection_v1, conn)

    def test_remember_is_a_noop_outside_owui(self):
        sys.modules.pop("open_webui.socket.main", None)
        sys.modules.pop("open_webui.socket", None)
        sys.modules.pop("open_webui", None)
        conn = _GatewayConnection(lambda: None)
        _remember_gateway_connection(conn)  # must not raise


class OwuiSessionModelTests(unittest.TestCase):
    def test_session_key_is_stable_across_model_presets(self):
        expected = "agent:main:openwebui-user-123-chat-456"

        self.assertEqual(_owui_session_key("main", "user-123", "chat-456"), expected)

    def test_chat_send_params_include_owui_origin_metadata(self):
        params = _owui_chat_send_params(
            session_key="agent:main:openwebui-user-123-chat-456",
            message="hello",
            idempotency_key="msg-1",
            owui_chat_id="chat-456",
            owui_user_id="user-123",
        )

        receipt = params["systemProvenanceReceipt"]
        self.assertIn("Conversation info (untrusted metadata):", receipt)
        self.assertIn('"chat_id": "chat-456"', receipt)
        self.assertIn('"source": "openwebui"', receipt)
        self.assertIn('"user_id": "user-123"', receipt)
        self.assertNotIn("originatingChannel", params)
        self.assertNotIn("originatingTo", params)

    def test_chat_send_params_do_not_invent_originating_to(self):
        params = _owui_chat_send_params(
            session_key="agent:main:openwebui-unknown-owui-generated",
            message="hello",
            idempotency_key="msg-1",
            owui_chat_id=None,
            owui_user_id="unknown",
        )

        self.assertNotIn("systemProvenanceReceipt", params)

    def test_model_patch_matches_explicit_override(self):
        patch_resp = {"resolved": {"modelProvider": "openai", "model": "gpt-5.5"}}

        self.assertTrue(_model_patch_matches("openai/gpt-5.5", patch_resp))
        self.assertFalse(_model_patch_matches("anthropic/claude-sonnet-5", patch_resp))

    def test_model_patch_reset_accepts_resolved_agent_default(self):
        patch_resp = {"resolved": {"modelProvider": "openai", "model": "gpt-5-mini"}}

        self.assertTrue(_model_patch_matches(None, patch_resp))

    def test_model_patch_runtime_namespace_matches_canonical_vendor(self):
        # A runtime-keyed model (claude-cli/claude-opus-5) resolving to its vendor
        # (anthropic/claude-opus-5) counts as applied.
        patch_resp = {"resolved": {"modelProvider": "anthropic", "model": "claude-opus-5"}}
        self.assertTrue(_model_patch_matches("claude-cli/claude-opus-5", patch_resp))

    def test_model_patch_runtime_namespace_rejects_different_model(self):
        patch_resp = {"resolved": {"modelProvider": "anthropic", "model": "claude-sonnet-5"}}
        self.assertFalse(_model_patch_matches("claude-cli/claude-opus-5", patch_resp))

    def test_model_patch_non_runtime_provider_still_strict(self):
        # A vendor-keyed override must match exactly.
        patch_resp = {"resolved": {"modelProvider": "anthropic", "model": "claude-opus-5"}}
        self.assertFalse(_model_patch_matches("openrouter/claude-opus-5", patch_resp))


class ItemTextExtractionTests(unittest.TestCase):
    def test_coerces_text_from_content_blocks(self):
        self.assertEqual(
            _coerce_text([
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
            ]),
            "hello world",
        )

    def test_extracts_preamble_text(self):
        self.assertEqual(
            _item_assistant_text({"kind": "preamble", "text": "visible"}),
            "visible",
        )

    def test_ignores_non_assistant_item_kinds(self):
        self.assertEqual(
            _item_assistant_text({"kind": "tool", "text": "internal"}),
            "",
        )


class MultimodalUserMessageTests(unittest.TestCase):
    """OWUI sends `content` as a list of blocks when an image is attached."""

    def test_content_has_image_false_for_plain_text(self):
        self.assertFalse(_content_has_image("just plain text"))

    def test_content_has_image_false_for_text_only_blocks(self):
        self.assertFalse(_content_has_image([
            {"type": "text", "text": "hello"},
        ]))

    def test_content_has_image_true_when_image_url_block_present(self):
        self.assertTrue(_content_has_image([
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xyz"}},
        ]))

    def test_content_has_image_true_for_image_only_message(self):
        self.assertTrue(_content_has_image([
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xyz"}},
        ]))

    def test_coerce_text_extracts_text_and_skips_image_block(self):
        """_coerce_text keeps only the text of a text+image list; the data: URL never leaks."""
        text = _coerce_text([
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xyz"}},
        ])
        self.assertEqual(text, "what is this?")
        self.assertNotIn("data:image", text)


class ExtractImageAttachmentsTests(unittest.TestCase):
    """OWUI image_url blocks become `chat.send` attachments."""

    def test_returns_empty_list_for_plain_text(self):
        self.assertEqual(_extract_image_attachments("just plain text"), [])

    def test_returns_empty_list_for_text_only_blocks(self):
        self.assertEqual(_extract_image_attachments([
            {"type": "text", "text": "hello"},
        ]), [])

    def test_decodes_single_base64_image(self):
        attachments = _extract_image_attachments([
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ])
        self.assertEqual(len(attachments), 1)
        att = attachments[0]
        self.assertEqual(att["mimeType"], "image/png")
        self.assertEqual(att["content"], "QUJD")
        self.assertEqual(att["type"], "image")
        self.assertTrue(att["fileName"].endswith(".png"))

    def test_decodes_multiple_images_in_order(self):
        attachments = _extract_image_attachments([
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBB="}},
        ])
        self.assertEqual([a["content"] for a in attachments], ["AAA=", "BBB="])
        self.assertEqual(attachments[1]["mimeType"], "image/jpeg")

    def test_skips_non_data_url_image_refs(self):
        self.assertEqual(_extract_image_attachments([
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
        ]), [])

    def test_skips_non_base64_data_urls(self):
        self.assertEqual(_extract_image_attachments([
            {"type": "image_url", "image_url": {"url": "data:image/svg+xml,<svg/>"}},
        ]), [])

    def test_defaults_mime_when_missing(self):
        attachments = _extract_image_attachments([
            {"type": "image_url", "image_url": {"url": "data:;base64,QUJD"}},
        ])
        self.assertEqual(attachments[0]["mimeType"], "image/png")


class ItemDeltaDedupTests(unittest.TestCase):
    """An `item` event echoing the full text is not yielded on top of the deltas."""

    def test_genuine_preamble_before_any_assistant_text(self):
        self.assertEqual(
            _item_delta_text("Let me check that.", "", ""),
            "Let me check that.",
        )

    def test_duplicate_full_message_after_assistant_stream_is_suppressed(self):
        self.assertEqual(
            _item_delta_text("Hello world", "", "Hello world"),
            "",
        )

    def test_item_event_extends_beyond_assistant_stream(self):
        self.assertEqual(
            _item_delta_text("Hello world, more.", "", "Hello world"),
            ", more.",
        )

    def test_sequential_item_only_events_still_dedup_against_each_other(self):
        self.assertEqual(
            _item_delta_text("Hello wor", "Hello", ""),
            " wor",
        )
        self.assertEqual(
            _item_delta_text("Hello", "Hello", ""),
            "",
        )

    def test_unrelated_item_text_passes_through(self):
        self.assertEqual(
            _item_delta_text("Something else entirely", "Hello", ""),
            "Something else entirely",
        )


class SuppressAlreadyShownTests(unittest.TestCase):
    """`_suppress_already_shown` stops a catch-all repeating the reply when `assistant_stream_text`
    drifted from `visible_message_text`."""

    def test_suppresses_text_already_at_the_tail(self):
        self.assertEqual(
            _suppress_already_shown("world", "hello world"),
            "",
        )

    def test_suppresses_exact_full_duplicate(self):
        # assistant_stream_text drifted, so _item_delta_text returned the whole message.
        full_text = "Status update: all six repro attempts came back clean.\n\nWhat next?"
        self.assertEqual(
            _suppress_already_shown(full_text, full_text),
            "",
        )

    def test_genuine_new_text_passes_through(self):
        self.assertEqual(
            _suppress_already_shown(" more", "hello world"),
            " more",
        )

    def test_empty_delta_is_noop(self):
        self.assertEqual(_suppress_already_shown("", "hello world"), "")

    def test_substring_in_the_middle_is_not_suppressed(self):
        # Only a match at the very tail counts as already shown.
        self.assertEqual(
            _suppress_already_shown("world", "world peace hello"),
            "world",
        )


class ResolveMediaTests(unittest.TestCase):
    """_resolve_media keeps text before the MEDIA: prefix in the same chunk."""

    def test_preserves_text_before_media_directive(self):
        result, handled = _resolve_media(
            "CHECKING MEDIA:p2-test.png DONE", base_url="https://example.com"
        )
        self.assertTrue(handled)
        self.assertEqual(
            result,
            "CHECKING ![p2-test.png](https://example.com/p2-test.png)\nDONE",
        )

    def test_no_directive_passes_through_unchanged(self):
        self.assertEqual(
            _resolve_media("plain text", base_url="https://example.com"),
            ("plain text", False),
        )

    def test_directive_at_start_of_chunk(self):
        result, handled = _resolve_media(
            "MEDIA:pic.png", base_url="https://example.com"
        )
        self.assertTrue(handled)
        self.assertEqual(result, "![pic.png](https://example.com/pic.png)")

    def test_resolves_multiple_directives_in_one_chunk(self):
        # Every MEDIA: directive in a reply resolves, not only the first.
        text = "one\nMEDIA:a.png\n\ntwo\nMEDIA:b.png\n\nMEDIA:c.png\nend"
        result, handled = _resolve_media(text, base_url="https://example.com")
        self.assertTrue(handled)
        self.assertEqual(
            result,
            "one\n![a.png](https://example.com/a.png)\n"
            "two\n![b.png](https://example.com/b.png)\n"
            "![c.png](https://example.com/c.png)\nend",
        )

    def test_multiple_directives_back_to_back_no_text_between(self):
        result, handled = _resolve_media(
            "MEDIA:a.png\nMEDIA:b.png", base_url="https://example.com"
        )
        self.assertTrue(handled)
        self.assertEqual(
            result,
            "![a.png](https://example.com/a.png)\n![b.png](https://example.com/b.png)",
        )

    def test_prose_use_of_media_term_is_not_mangled(self):
        # "MEDIA: fix" in prose is not a directive: "fix" has no extension.
        text = "not something my MEDIA: fix touched, see the MEDIA: multi-image work"
        result, handled = _resolve_media(text, base_url="https://example.com")
        self.assertFalse(handled)
        self.assertEqual(result, text)

    def test_prose_use_does_not_block_a_real_directive_later_on(self):
        text = "the MEDIA: fix now handles MEDIA:real-file.png correctly"
        result, handled = _resolve_media(text, base_url="https://example.com")
        self.assertTrue(handled)
        self.assertEqual(
            result,
            "the MEDIA: fix now handles ![real-file.png](https://example.com/real-file.png)\ncorrectly",
        )


class ResolveMediaViaOwuiTests(unittest.IsolatedAsyncioTestCase):
    """The native-OWUI-upload MEDIA: path, same cases as ResolveMediaTests."""

    async def test_resolves_multiple_directives_in_one_chunk(self):
        uploads = []

        def fake_upload(fpath, base_url, token):
            uploads.append(fpath)
            name = fpath.rsplit("/", 1)[-1]
            return {"id": f"id-{name}", "meta": {"content_type": "image/png"}}

        with mock.patch("openclaw_pipe._upload_owui_file", side_effect=fake_upload), \
             mock.patch("os.path.isfile", return_value=True):
            result, handled = await _resolve_media_via_owui(
                "caption\nMEDIA:a.png\n\nMEDIA:b.png\nend",
                base_url="https://owui.example.com",
                token="tok",
                __event_emitter__=None,
            )

        self.assertTrue(handled)
        self.assertEqual(len(uploads), 2)
        self.assertEqual(
            result,
            "caption\n![a.png](/api/v1/files/id-a.png/content)\n"
            "![b.png](/api/v1/files/id-b.png/content)\nend",
        )

    async def test_missing_file_bails_out_entirely_for_caller_fallback(self):
        # One unresolvable directive bails the whole attempt (handled=False), so _resolve_media
        # handles all of them uniformly.
        with mock.patch("os.path.isfile", return_value=False):
            result, handled = await _resolve_media_via_owui(
                "MEDIA:missing.png",
                base_url="https://owui.example.com",
                token="tok",
                __event_emitter__=None,
            )
        self.assertFalse(handled)
        self.assertEqual(result, "MEDIA:missing.png")

    async def test_prose_use_of_media_term_is_not_mangled(self):
        # No upload is attempted for "MEDIA: fix".
        with mock.patch("openclaw_pipe._upload_owui_file") as upload_mock, \
             mock.patch("os.path.isfile", return_value=True):
            text = "not something my MEDIA: fix touched"
            result, handled = await _resolve_media_via_owui(
                text,
                base_url="https://owui.example.com",
                token="tok",
                __event_emitter__=None,
            )
        upload_mock.assert_not_called()
        self.assertFalse(handled)
        self.assertEqual(result, text)


class MediaWithoutFileServerTests(unittest.IsolatedAsyncioTestCase):
    """With ENABLE_FILE_SERVER off nothing serves a fallback link, so a file
    that cannot be uploaded is an error in the reply, not an image that
    never loads."""

    def test_a_bare_filename_is_an_error_when_the_file_server_is_off(self):
        result, handled = _resolve_media(
            "see\nMEDIA:pic.png\nend", base_url="https://example.com", file_server=False)
        self.assertTrue(handled)
        self.assertEqual(result, "see\n_(a file could not be uploaded: pic.png)_\nend")
        self.assertNotIn("](", result)

    def test_a_url_directive_needs_no_file_server(self):
        result, handled = _resolve_media(
            "MEDIA:https://example.com/a.png", base_url="https://x", file_server=False)
        self.assertTrue(handled)
        self.assertEqual(result, "![a.png](https://example.com/a.png)")

    async def _resolve(self, text, **valves):
        v = Pipe.Valves(**valves)
        with mock.patch("openclaw_pipe._upload_owui_file", side_effect=OSError("upload refused")), \
             mock.patch("os.path.isfile", return_value=True):
            return await _resolve_media_text(text, valves=v)

    async def test_a_failed_upload_with_the_file_server_off_is_an_error(self):
        result, handled = await self._resolve(
            "here MEDIA:chart.png now", ENABLE_FILE_SERVER=False, FILE_SERVER_BASE_URL="https://x")
        self.assertTrue(handled)
        self.assertEqual(result, "here _(a file could not be uploaded: chart.png)_\nnow")
        self.assertNotIn("https://x", result)

    async def test_a_failed_upload_with_the_file_server_on_still_links_to_it(self):
        result, handled = await self._resolve(
            "MEDIA:chart.png", ENABLE_FILE_SERVER=True, FILE_SERVER_BASE_URL="https://x")
        self.assertTrue(handled)
        self.assertEqual(result, "![chart.png](https://x/chart.png)")

    async def test_owui_files_off_and_file_server_off_is_an_error_too(self):
        result, handled = await self._resolve(
            "MEDIA:chart.png", ENABLE_FILE_SERVER=False, USE_OWUI_FILES=False)
        self.assertTrue(handled)
        self.assertEqual(result, "_(a file could not be uploaded: chart.png)_")


class MediaBufferTests(unittest.TestCase):
    """A stream can split "MEDIA:filename.png" anywhere; _advance_media_buffer holds back until
    trailing whitespace shows the name is whole."""

    def test_filename_split_across_deltas_stays_intact(self):
        pending = ""
        flush, pending = _advance_media_buffer(pending, "caption\nMEDIA:repro2-")
        self.assertEqual(flush, "caption\n")
        self.assertEqual(pending, "MEDIA:repro2-")

        flush, pending = _advance_media_buffer(pending, "orphaned.png\n\nmore text")
        self.assertEqual(flush, "MEDIA:repro2-orphaned.png\n\nmore text")
        self.assertEqual(pending, "")

    def test_prefix_itself_split_across_deltas(self):
        pending = ""
        flush, pending = _advance_media_buffer(pending, "some text MED")
        self.assertEqual(flush, "some text ")
        self.assertEqual(pending, "MED")

        flush, pending = _advance_media_buffer(pending, "IA:repro3-revealed.png")
        self.assertEqual(flush, "")
        self.assertEqual(pending, "MEDIA:repro3-revealed.png")

        flush, pending = _advance_media_buffer(pending, "\nmore")
        self.assertEqual(flush, "MEDIA:repro3-revealed.png\nmore")
        self.assertEqual(pending, "")

    def test_plain_text_passes_through_unbuffered(self):
        flush, pending = _advance_media_buffer("", "just normal text, no media here")
        self.assertEqual(flush, "just normal text, no media here")
        self.assertEqual(pending, "")

    def test_directive_fully_formed_in_one_chunk_releases_immediately(self):
        flush, pending = _advance_media_buffer("", "caption\nMEDIA:file.png\n\nmore")
        self.assertEqual(flush, "caption\nMEDIA:file.png\n\nmore")
        self.assertEqual(pending, "")

    def test_partial_prefix_at_chunk_end_not_mistaken_for_unrelated_text(self):
        # A single "M" at the end of a chunk could still become "MEDIA:".
        flush, pending = _advance_media_buffer("", "word ending in M")
        self.assertEqual(flush, "word ending in ")
        self.assertEqual(pending, "M")


class StatusEmitterTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_owui_status_event(self):
        events = []
        emitter = RecordingEmitter(events)

        await _emit_status(emitter, "Thinking...", done=False)

        self.assertEqual(
            events,
            [{
                "type": "status",
                "data": {"description": "Thinking...", "done": False},
            }],
        )

    async def test_missing_emitter_is_noop(self):
        await _emit_status(None, "Thinking...", done=False)


class FormatTokensTests(unittest.TestCase):
    def test_formats_thousands_and_millions(self):
        self.assertEqual(_fmt_tokens(115212), "115k")
        self.assertEqual(_fmt_tokens(1_000_000), "1.0m")
        self.assertEqual(_fmt_tokens(42), "42")
        self.assertEqual(_fmt_tokens(None), "?")


class RelativeTimeTests(unittest.TestCase):
    # A +2 s buffer per delta keeps whole-unit rounding deterministic.
    def test_minutes_only(self):
        now_ms = time.time() * 1000
        self.assertEqual(_relative_time(now_ms + 22 * 60_000 + 2000), "22m")

    def test_hours_and_minutes(self):
        now_ms = time.time() * 1000
        self.assertEqual(_relative_time(now_ms + (4 * 3600 + 7 * 60) * 1000 + 2000), "4h07m")

    def test_days_and_hours(self):
        now_ms = time.time() * 1000
        self.assertEqual(_relative_time(now_ms + (3 * 86400 + 7 * 3600) * 1000 + 2000), "3d07h")

    def test_missing_or_past_returns_none(self):
        self.assertIsNone(_relative_time(None))
        self.assertIsNone(_relative_time(0))
        self.assertIsNone(_relative_time(time.time() * 1000 - 1000))


class UsageStatusLineTests(unittest.IsolatedAsyncioTestCase):
    def _conn(self, describe_result, usage_result=None, usage_exc=None):
        conn = mock.Mock()

        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return describe_result
            if method == "usage.status":
                if usage_exc:
                    raise usage_exc
                return usage_result
            raise AssertionError(f"unexpected method {method}")

        conn.send_request = send_request
        return conn

    async def test_context_combines_with_primary_window_thin(self):
        conn = self._conn(
            describe_result={
                "session": {
                    "contextTokens": 1_000_000,
                    "totalTokens": 115212,
                    "modelProvider": "anthropic",
                }
            },
            usage_result={
                "providers": [
                    {
                        "provider": "anthropic",
                        # No resetAt: just the thin combined line.
                        "windows": [
                            {"label": "5h", "usedPercent": 24},
                            {"label": "Week", "usedPercent": 14},
                        ],
                    }
                ]
            },
        )
        lines = await _build_usage_status_lines(conn, "agent:main:x")
        self.assertEqual(lines, ["🧠 115k/1.0m (11.5%) · ⏱ 5h 76% left"])

    async def test_windows_with_reset_time_get_their_own_detail_line(self):
        now_ms = time.time() * 1000
        conn = self._conn(
            describe_result={
                "session": {
                    "contextTokens": 1_000_000,
                    "totalTokens": 115212,
                    "modelProvider": "anthropic",
                }
            },
            usage_result={
                "providers": [
                    {
                        "provider": "anthropic",
                        "windows": [
                            {
                                "label": "5h",
                                "usedPercent": 24,
                                "resetAt": now_ms + 22 * 60_000 + 2000,
                            },
                            {
                                "label": "Week",
                                "usedPercent": 14,
                                "resetAt": now_ms + (6 * 86400 + 3 * 3600) * 1000 + 2000,
                            },
                        ],
                    }
                ]
            },
        )
        lines = await _build_usage_status_lines(conn, "agent:main:x")
        # The thin line is unchanged; each window with a reset time adds its own detail line.
        self.assertEqual(
            lines,
            [
                "⏱ 5h 76% left (resets in 22m)",
                "⏱ Week 86% left (resets in 6d03h)",
                "🧠 115k/1.0m (11.5%) · ⏱ 5h 76% left",
            ],
        )

    async def test_single_window_has_no_secondary_line(self):
        conn = self._conn(
            describe_result={
                "session": {
                    "contextTokens": 1_000_000,
                    "totalTokens": 500,
                    "modelProvider": "google-gemini-cli",
                }
            },
            usage_result={
                "providers": [
                    {"provider": "google-gemini-cli", "windows": [{"label": "Pro", "usedPercent": 5}]}
                ]
            },
        )
        lines = await _build_usage_status_lines(conn, "agent:main:x")
        self.assertEqual(lines, ["🧠 500/1.0m (0.1%) · ⏱ Pro 95% left"])

    async def test_falls_back_to_summary_when_no_windows(self):
        conn = self._conn(
            describe_result={
                "session": {
                    "contextTokens": 1_000_000,
                    "totalTokens": 1000,
                    "modelProvider": "deepseek",
                }
            },
            usage_result={
                "providers": [
                    {"provider": "deepseek", "windows": [], "summary": "Balance $4.58"}
                ]
            },
        )
        lines = await _build_usage_status_lines(conn, "agent:main:x")
        self.assertEqual(lines, ["🧠 1k/1.0m (0.1%) · ⏱ Balance $4.58"])

    async def test_describe_failure_yields_no_lines(self):
        conn = mock.Mock()

        async def send_request(method, params, timeout=5):
            raise TimeoutError("gateway unreachable")

        conn.send_request = send_request
        lines = await _build_usage_status_lines(conn, "agent:main:x")
        self.assertEqual(lines, [])

    async def test_usage_status_failure_still_returns_context(self):
        conn = self._conn(
            describe_result={
                "session": {
                    "contextTokens": 1_000_000,
                    "totalTokens": 115212,
                    "modelProvider": "anthropic",
                }
            },
            usage_exc=TimeoutError("no usage"),
        )
        lines = await _build_usage_status_lines(conn, "agent:main:x")
        self.assertEqual(lines, ["🧠 115k/1.0m (11.5%)"])

    async def test_no_matching_provider_yields_context_only(self):
        conn = self._conn(
            describe_result={
                "session": {
                    "contextTokens": 1_000_000,
                    "totalTokens": 500,
                    "modelProvider": "openai",
                }
            },
            usage_result={"providers": [{"provider": "anthropic", "windows": []}]},
        )
        lines = await _build_usage_status_lines(conn, "agent:main:x")
        self.assertEqual(lines, ["🧠 500/1.0m (0.1%)"])

    async def test_active_goal_with_budget(self):
        conn = self._conn(
            describe_result={
                "session": {
                    "contextTokens": 1_000_000,
                    "totalTokens": 500,
                    "modelProvider": "anthropic",
                    "goal": {
                        "status": "active",
                        "objective": "get CI green",
                        "tokensUsed": 12000,
                        "tokenBudget": 50000,
                    },
                }
            },
            usage_result={"providers": []},
        )
        lines = await _build_usage_status_lines(conn, "agent:main:x")
        self.assertEqual(lines, ["🎯 Pursuing goal (12k/50k)", "🧠 500/1.0m (0.1%)"])

    async def test_active_goal_without_budget_shows_objective(self):
        conn = self._conn(
            describe_result={
                "session": {
                    "contextTokens": 1_000_000,
                    "totalTokens": 500,
                    "modelProvider": "anthropic",
                    "goal": {"status": "active", "objective": "ship the fix", "tokensUsed": 0},
                }
            },
            usage_result={"providers": []},
        )
        lines = await _build_usage_status_lines(conn, "agent:main:x")
        self.assertEqual(lines, ["🎯 Pursuing goal: ship the fix", "🧠 500/1.0m (0.1%)"])

    async def test_paused_goal(self):
        conn = self._conn(
            describe_result={
                "session": {
                    "contextTokens": 1_000_000,
                    "totalTokens": 500,
                    "modelProvider": "anthropic",
                    "goal": {"status": "paused"},
                }
            },
            usage_result={"providers": []},
        )
        lines = await _build_usage_status_lines(conn, "agent:main:x")
        self.assertIn("🎯 Goal paused (/goal resume)", lines)

    async def test_no_goal_omits_goal_line(self):
        conn = self._conn(
            describe_result={
                "session": {
                    "contextTokens": 1_000_000,
                    "totalTokens": 500,
                    "modelProvider": "anthropic",
                }
            },
            usage_result={"providers": []},
        )
        lines = await _build_usage_status_lines(conn, "agent:main:x")
        self.assertTrue(all("🎯" not in line for line in lines))

    async def test_the_runs_own_usage_fills_the_line_before_the_row_has_totals(self):
        """ELI-177 (OpenClaw 2026.10): a new session's row has no totals when the first reply ends;
        the run's own usage fills the line."""
        conn = self._conn(describe_result={"session": {"contextTokens": 200_000}},
                          usage_result={"providers": []})
        self.assertEqual(await _build_usage_status_lines(conn, "agent:main:x"), [])
        usage = {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 15}
        lines = await _build_usage_status_lines(conn, "agent:main:x", run_usage=usage)
        self.assertEqual(lines, ["🧠 10/200k (0%)"])

    async def test_the_runs_own_usage_wins_over_the_previous_runs_row(self):
        conn = self._conn(describe_result={"session": {"contextTokens": 1_000_000, "totalTokens": 500}},
                          usage_result={"providers": []})
        usage = {"input": 2, "cacheRead": 114_000, "cacheWrite": 1_210, "output": 40}
        lines = await _build_usage_status_lines(conn, "agent:main:x", run_usage=usage)
        self.assertEqual(lines, ["🧠 115k/1.0m (11.5%)"])


class RunUsageTests(unittest.TestCase):
    """The context a run filled, from its own usage (ELI-177)."""

    def test_prompt_tokens_count_as_openclaw_counts_a_rows_total(self):
        f = openclaw_pipe._usage_prompt_tokens
        self.assertEqual(f({"input": 3, "cacheRead": 100, "cacheWrite": 7, "output": 9}), 110)
        self.assertEqual(f({"contextUsage": {"state": "available", "promptTokens": 42}, "input": 3}), 42)
        self.assertIsNone(f({"contextUsage": {"state": "unavailable"}, "input": 3}))
        self.assertEqual(f({"totalTokens": 15, "output": 5}), 10)
        self.assertIsNone(f({"input": 0, "output": 0, "totalTokens": 0}))
        self.assertIsNone(f(None))

    def test_a_runs_usage_is_found_by_its_id_not_by_position(self):
        f = openclaw_pipe._history_usage_for_run
        mine, later = {"input": 10, "output": 5}, {"input": 99, "output": 1}
        hist = {"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "usage": {"input": 4}, "__openclaw": {"runId": "run-a"}},
            {"role": "assistant", "usage": mine, "__openclaw": {"runId": "run-a"}},
            {"role": "assistant", "usage": {"input": 0, "output": 0}, "__openclaw": {"runId": "run-a"}},
            {"role": "assistant", "usage": later, "__openclaw": {"runId": "run-b"}},
        ]}
        self.assertEqual(f(hist, "run-a"), mine)
        self.assertEqual(f(hist, "run-b"), later)
        self.assertIsNone(f(hist, "run-c"))
        cli = {"messages": [{"role": "assistant", "usage": mine, "idempotencyKey": "cli-assistant:run-x"}]}
        self.assertEqual(f(cli, "run-x"), mine)


class MessageSnapshotEmitterTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_persisted_replace_event(self):
        events = []
        emitter = RecordingEmitter(events)

        await _emit_message_snapshot(emitter, "partial answer")

        self.assertEqual(
            events,
            [{
                "type": "replace",
                "data": {"content": "partial answer"},
            }],
        )

    async def test_missing_emitter_or_empty_content_is_noop(self):
        events = []
        emitter = RecordingEmitter(events)

        await _emit_message_snapshot(None, "partial answer")
        await _emit_message_snapshot(emitter, "")

        self.assertEqual(events, [])


class UserInputPromptTests(unittest.IsolatedAsyncioTestCase):
    def test_detects_codex_user_input_prompt(self):
        self.assertTrue(_is_user_input_prompt("Codex needs input:\n\nPackage\nPick one"))
        self.assertFalse(_is_user_input_prompt("I need input for this function"))

    def test_partial_prefix_stays_ambiguous(self):
        # Token streaming delivers the trigger in pieces; each partial prefix keeps buffering.
        for partial in ("", "Open", "OpenClaw needs", "OpenClaw needs input:", "Codex"):
            self.assertTrue(
                _could_be_user_input_prefix(partial),
                f"expected {partial!r} to still be ambiguous",
            )

    def test_diverged_text_is_not_ambiguous(self):
        for text in ("Hello there", "Once closed, ", "Codexx needs input:"):
            self.assertFalse(
                _could_be_user_input_prefix(text),
                f"expected {text!r} to have diverged",
            )

    def test_full_prefix_plus_question_stays_matched(self):
        # Once the prefix is confirmed, the question of any length keeps buffering to the end.
        self.assertTrue(
            _could_be_user_input_prefix(
                "OpenClaw needs input: what's your favorite color?"
            )
        )

    def test_advance_buffer_holds_ambiguous_prefix(self):
        flush, pending = _advance_input_prompt_buffer("", "Open")
        self.assertEqual(flush, "")
        self.assertEqual(pending, "Open")

    def test_advance_buffer_flushes_diverged_text_with_no_newline(self):
        flush, pending = _advance_input_prompt_buffer("", "Hello there")
        self.assertEqual(flush, "Hello there")
        self.assertEqual(pending, "")

    def test_advance_buffer_accumulates_across_deltas_until_resolved(self):
        pending = ""
        flush, pending = _advance_input_prompt_buffer(pending, "Open")
        self.assertEqual(flush, "")
        flush, pending = _advance_input_prompt_buffer(pending, "Claw needs input: color?")
        self.assertEqual(flush, "")
        self.assertEqual(pending, "OpenClaw needs input: color?")

    def test_advance_buffer_catches_trigger_after_midchunk_newline(self):
        # One delta can hold prose, the newline and the next paragraph's start; the text after the
        # LAST newline is buffered.
        flush, pending = _advance_input_prompt_buffer(
            "", "fixed:\n\nOpenClaw needs input: what's your favorite color?"
        )
        self.assertEqual(flush, "fixed:\n\n")
        self.assertEqual(pending, "OpenClaw needs input: what's your favorite color?")

    def test_advance_buffer_flushes_everything_when_no_fresh_line_matches(self):
        flush, pending = _advance_input_prompt_buffer(
            "", "line one\nline two\nline three"
        )
        self.assertEqual(flush, "line one\nline two\nline three")
        self.assertEqual(pending, "")

    def test_free_text_prompt_fields(self):
        f = _parse_text_prompt("Codex needs input:\n\nPackage\nName the package you want")
        self.assertEqual(f["title"], "Package")
        self.assertIn("Name the package", f["message"])
        self.assertEqual((f["confirm"], f["secret"], f["options"]), (False, False, []))

    def test_numbered_options_are_the_options(self):
        f = _parse_text_prompt(
            "OpenClaw needs input:\n\nFavorite color?\nPick one\n1. Red\n2. Green\n3. Blue"
        )
        self.assertFalse(f["confirm"])
        self.assertEqual(f["title"], "Favorite color?")
        self.assertEqual(f["options"], ["Red", "Green", "Blue"])
        self.assertFalse(f["multi"])

    def test_extract_numbered_options_parses_dot_and_paren(self):
        opts = _extract_numbered_options("intro\n1. alpha\n2) beta\n  3. gamma\nfooter")
        self.assertEqual(opts, [("1", "alpha"), ("2", "beta"), ("3", "gamma")])

    def test_extract_numbered_options_none(self):
        self.assertEqual(_extract_numbered_options("no options here"), [])

    def test_multiselect_marker_sets_multi_and_is_stripped(self):
        f = _parse_text_prompt(
            "OpenClaw needs input:\n\nToppings (multiselect)\n"
            "Pick any\n1. Cheese\n2. Olives\n3. Mushrooms"
        )
        self.assertTrue(f["multi"])
        self.assertEqual(f["options"], ["Cheese", "Olives", "Mushrooms"])
        self.assertEqual(f["title"], "Toppings")

    def test_multiselect_hebrew_marker(self):
        f = _parse_text_prompt("OpenClaw needs input:\n\nמה בא לך? בחר כמה\n1. פיצה\n2. סושי")
        self.assertTrue(f["multi"])
        self.assertEqual(f["options"], ["פיצה", "סושי"])

    def test_normalizes_event_call_response_shapes(self):
        self.assertEqual(_normalize_event_call_response("  answer  "), "answer")
        self.assertEqual(_normalize_event_call_response({"value": "1"}), "1")
        self.assertEqual(_normalize_event_call_response({"confirmed": True}), "yes")
        self.assertEqual(_normalize_event_call_response({"confirmed": False}), "no")

    def test_normalizes_bare_boolean_confirmation_response(self):
        # OWUI confirmation modals resolve to a bare bool, not a dict.
        self.assertEqual(_normalize_event_call_response(True), "yes")
        self.assertEqual(_normalize_event_call_response(False), "no")

    def test_marks_secret_prompts(self):
        f = _parse_text_prompt(
            "Codex needs input:\n\nToken\nThis channel may show your reply to other participants."
        )
        self.assertTrue(f["secret"])
        self.assertFalse(f["confirm"])

    def test_confirmation_for_explicit_yes_no(self):
        f = _parse_text_prompt("OpenClaw needs input:\n\nProceed?\nRun the deploy now? (y/n)")
        self.assertTrue(f["confirm"])

    def test_hebrew_and_arabic_yes_no_questions_are_confirmations(self):
        """ELI-193: Hebrew "(כן/לא)" and Arabic "؟" questions are yes/no confirmations too."""
        for question in ("למחוק את הקובץ? (כן/לא)", "هل أحذف الملف؟ (نعم/لا)", "هل تريد المتابعة؟"):
            f = _parse_text_prompt(f"OpenClaw needs input:\n\nאישור\n{question}")
            self.assertEqual((f["confirm"], f["options"]), (True, []), question)

    def test_arabic_multiselect_marker(self):
        f = _parse_text_prompt("OpenClaw needs input:\n\nإضافات\nاختر كل ما ينطبق\n1. جبن\n2. زيتون")
        self.assertTrue(f["multi"])
        self.assertEqual(f["options"], ["جبن", "زيتون"])

    def test_confirmation_for_confirm_verb_question(self):
        f = _parse_text_prompt("OpenClaw needs input:\n\nDelete file?\nDelete config.json?")
        self.assertTrue(f["confirm"])

    def test_choice_question_is_not_confirmation(self):
        # A short "?" title that is really a free-text choice is not yes/no.
        f = _parse_text_prompt("OpenClaw needs input:\n\nWhich model?\nWhich model should I use?")
        self.assertEqual((f["confirm"], f["options"]), (False, []))

    def test_numbered_options_never_confirmation(self):
        # An enumerated option list is a choice even if a confirm verb appears.
        f = _parse_text_prompt("OpenClaw needs input:\n\nProceed how?\nProceed?\n1. rebase\n2. merge")
        self.assertFalse(f["confirm"])
        self.assertEqual(f["options"], ["rebase", "merge"])

    def test_a_choice_or_yes_no_question_that_mentions_a_secret_keeps_its_answers(self):
        """ELI-196: a choice or yes/no question that only mentions a password, secret or token
        keeps its options, not the masked dialog."""
        f = _parse_text_prompt("OpenClaw needs input:\n\nRouter entry\nOK to add the router-debug "
                               "entry so the deploy can use the password?\n1. Yes\n2. No")
        self.assertEqual((f["secret"], f["options"]), (False, ["Yes", "No"]))
        f = _parse_text_prompt("OpenClaw needs input:\n\nTokens\nRotate the API token now? (y/n)")
        self.assertEqual((f["secret"], f["confirm"]), (False, True))
        # A question that asks for the value itself still gets the dialog.
        f = _parse_text_prompt("OpenClaw needs input:\n\nTest token\nPaste the test API token (password)")
        self.assertEqual((f["secret"], f["confirm"], f["options"]), (True, False, []))

    def test_a_secret_ask_keeps_the_masked_field_whatever_else_it_has(self):
        """Asking for the value wins over a numbered list, a confirm verb, or options under Codex's
        warning; a key typed into Other would land in the chat (#137)."""
        f = _parse_text_prompt("OpenClaw needs input:\n\nToken\nPaste your API key:\n1. Open Settings\n2. Copy the key")
        self.assertEqual((f["secret"], f["options"]), (True, []))
        f = _parse_text_prompt("OpenClaw needs input:\n\nPassword\nPlease confirm your password?")
        self.assertEqual((f["secret"], f["confirm"]), (True, False))
        f = _parse_text_prompt("Codex needs input:\n\nToken\nThis channel may show your reply to other participants.\n1. a\n2. b")
        self.assertEqual((f["secret"], f["options"]), (True, []))

    def test_secret_marker_does_not_match_innocent_key_substring(self):
        # "monkey" contains "key" but is not a secret.
        f = _parse_text_prompt("OpenClaw needs input:\n\nName the monkey\nWhat should we call the monkey?")
        self.assertFalse(f["secret"])

    async def test_ask_user_input_modal_noops_without_event_call(self):
        answer = await _ask_user_input_modal(
            None,
            "Codex needs input:\n\nPackage\nChoose",
        )

        self.assertIsNone(answer)

    async def test_an_unanswered_secret_dialog_ends_the_wait(self):
        """OWUI's input dialog has no timeout; ours ends the wait with the question as text."""
        async def event_call(event):
            await asyncio.sleep(0.05)
            return {"value": "late"}

        answer = await asyncio.wait_for(_ask_user_input_modal(
            event_call,
            "Codex needs input:\n\nPackage\nYour API key",
            timeout_s=0.01,
        ), 2)
        self.assertIsNone(answer)

    def test_ask_user_detail_block_renders_as_tool_call(self):
        block = _ask_user_detail_block(
            "OpenClaw needs input:\n\nPick one\nWhich?\n1. a\n2. b",
            "2",
        )
        self.assertIn('<details type="tool_calls"', block)
        self.assertIn('name="Ask User"', block)
        self.assertIn("Ask User", block)
        self.assertIn("Pick one", block)
        self.assertIn("Which?", block)
        self.assertIn('result="2"', block)

    def test_live_session_id_for_user_finds_match(self):
        pool = {"sid-a": {"id": "u1"}, "sid-b": {"id": "u2"}}
        self.assertEqual(_live_session_id_for_user("u2", pool), "sid-b")
        self.assertIsNone(_live_session_id_for_user("u3", pool))
        self.assertIsNone(_live_session_id_for_user("u1", {}))


class NativeAskUserCardTests(unittest.IsolatedAsyncioTestCase):
    """OWUI >= 0.11.1 renders `request:user_input` as its native question card."""

    def setUp(self):
        sys.modules.setdefault("open_webui", types.ModuleType("open_webui"))
        env = types.ModuleType("open_webui.env")
        env.VERSION = "0.12.0"
        sys.modules["open_webui.env"] = env
        self.addCleanup(sys.modules.pop, "open_webui.env", None)

    def test_numbered_options_become_card_options_with_descriptions(self):
        payload = _native_ask_user_payload(
            "OpenClaw needs input:\n\nDatabase\nWhich database should I use?\n"
            "1. Postgres - robust, needs a server\n2. SQLite\n3. SQLite"
        )
        self.assertEqual(payload["type"], "request:user_input")
        (q,) = payload["data"]["questions"]
        self.assertEqual(q["header"], "Database")
        # The numbered list moves into the options.
        self.assertEqual(q["question"], "Which database should I use?")
        self.assertEqual(
            q["options"],
            [
                {"label": "Postgres", "description": "robust, needs a server"},
                {"label": "SQLite", "description": ""},
            ],
        )
        self.assertTrue(q["allow_other"])

    def test_a_question_on_the_marker_line_is_the_cards_question(self):
        """ELI-151: a question on the marker line gets the same card as one on its own line."""
        inline = _native_ask_user_payload("OpenClaw needs input: Which branch?\n1. main\n2. dev")
        (q,) = inline["data"]["questions"]
        self.assertEqual((q["header"], q["question"]), ("Which branch?", "Which branch?"))
        self.assertEqual([o["label"] for o in q["options"]], ["main", "dev"])
        self.assertEqual(inline, _native_ask_user_payload("OpenClaw needs input:\nWhich branch?\n1. main\n2. dev"))
        # A free-text question and a yes/no question on the marker line, too.
        for marker, prompt in (("Codex needs input:", "Package\nName the package you want"),
                               ("OpenClaw needs input:", "Deploy now? (y/n)")):
            inline = _native_ask_user_payload(f"{marker} {prompt}")
            self.assertNotIn("needs input", json.dumps(inline))
            self.assertEqual(inline, _native_ask_user_payload(f"{marker}\n{prompt}"))

    def test_confirmation_becomes_yes_no_without_free_text(self):
        payload = _native_ask_user_payload(
            "OpenClaw needs input:\n\nDeploy\nDeploy now? (y/n)"
        )
        (q,) = payload["data"]["questions"]
        self.assertEqual([o["label"] for o in q["options"]], ["Yes", "No"])
        self.assertFalse(q["allow_other"])

    def test_free_text_is_a_card_with_only_the_typed_answer(self):
        payload = _native_ask_user_payload(
            "OpenClaw needs input:\n\nPackage\nName the package you want"
        )
        (q,) = payload["data"]["questions"]
        self.assertEqual(q["options"], [])
        self.assertTrue(q["allow_other"])


    def test_json_form_carries_several_questions(self):
        prompt = (
            "OpenClaw needs input:\n```json\n"
            + json.dumps({"questions": [
                {"header": "Frequency", "question": "How often?",
                 "options": [{"label": "Daily", "description": "Every night"}, "Weekly - Sundays"]},
                {"id": "where", "header": "Storage", "question": "Where to?",
                 "options": ["NAS", "Cloud"], "allow_other": False},
                {"header": "Name", "question": "Name the job"},
            ]})
            + "\n```"
        )
        qs = _native_ask_user_payload(prompt)["data"]["questions"]
        self.assertEqual([q["id"] for q in qs], ["q1", "where", "q3"])
        self.assertEqual(qs[0]["options"][1], {"label": "Weekly", "description": "Sundays"})
        self.assertFalse(qs[1]["allow_other"])
        # No options means free text, so typing must stay possible.
        self.assertTrue(qs[2]["allow_other"])
        block = _ask_user_detail_block(prompt, "x")
        self.assertIn("Frequency", block)
        self.assertNotIn("questions", block)

    def test_format_answers(self):
        one = [{"id": "answer", "header": "H", "options": []}]
        self.assertEqual(
            _format_native_answers(one, {"answer": {"type": "other", "text": " pipx "}}), "pipx"
        )
        yn = [{"id": "answer", "header": "H", "options": [{"label": "Yes"}, {"label": "No"}]}]
        self.assertEqual(
            _format_native_answers(yn, {"answer": {"type": "option", "label": "No"}}), "no"
        )
        two = [{"id": "a", "header": "Frequency", "options": []},
               {"id": "b", "header": "Storage", "options": []}]
        self.assertEqual(
            _format_native_answers(two, {
                "a": {"type": "option", "label": "Daily"},
                "b": {"type": "other", "text": "S3"},
            }),
            "Frequency: Daily\nStorage: S3",
        )

    async def test_modal_uses_native_card_and_returns_label(self):
        calls = []

        async def event_call(event):
            calls.append(event)
            return {"status": "answered", "answers": {
                "answer": {"type": "option", "option_index": 1, "label": "pipx"}}}

        answer = await _ask_user_input_modal(
            event_call, "Codex needs input:\n\nPackage\nChoose\n1. curl\n2. pipx"
        )
        self.assertEqual(answer, "pipx")
        self.assertEqual(calls[0]["type"], "request:user_input")
        self.assertEqual(calls[0]["data"]["timeout_ms"], 240_000)

    async def test_explicit_cancel_stops_without_reasking(self):
        calls = []

        async def event_call(event):
            calls.append(event)
            return {"status": "cancelled", "answers": {}}

        answer = await _ask_user_input_modal(
            event_call, "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b",
            owui_user_id="user-1", poll_interval_s=0.01,
        )
        self.assertIsNone(answer)
        self.assertEqual(len(calls), 1)

    async def test_a_timeout_or_an_unseen_cancel_ends_with_the_question_as_text(self):
        """OWUI's timeout, or a cancel as the card reaches a tab on another chat, does not count as
        seen: no re-show (ELI-142)."""
        for response in ({"error": "Event call timed out. The browser tab may be inactive or closed."},
                         {"status": "cancelled", "answers": {}}):
            calls = []

            async def event_call(event):
                calls.append(event)
                return response

            answer = await _ask_user_input_modal(
                event_call, "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b",
                owui_user_id="user-1", poll_interval_s=0.01,
            )
            self.assertIsNone(answer, response)
            self.assertEqual(len(calls), 1, response)

    # ELI-107: the wait for an answer ends with the run.

    def _socket(self, pool, call):
        fake_owui(self, sio=FakeSio(lambda event, data, to, timeout: call(data, to)), SESSION_POOL=pool)

    async def test_a_tab_on_another_chat_is_no_viewer(self):
        """OWUI 0.12 records the chats each socket views; a tab on another chat is no viewer, so
        the reply ends at once."""
        sent = []

        async def call(data, to):
            sent.append(to)
            return {"status": "answered", "answers": {"answer": {"type": "option", "label": "a"}}}
        self._socket({"sid-other": {"id": "user-1", "chat_ids": ["chat-x"]}}, call)

        async def event_call(event):
            return {"error": "Client session disconnected."}
        answer = await asyncio.wait_for(_ask_user_input_modal(
            event_call, "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b",
            owui_user_id="user-1", owui_chat_id="chat-1", owui_message_id="msg-1",
            poll_interval_s=0.01), 2)
        self.assertIsNone(answer)
        self.assertEqual(sent, [], "never another chat's tab")

    async def test_a_tab_that_does_not_come_back_ends_the_wait(self):
        """The viewing tab went away: the reply ends with the question as text."""
        pool = {"sid-tab": {"id": "user-1", "chat_ids": ["chat-1"]}}

        async def call(data, to):
            pool.clear()                                 # the tab is closed
            return {"error": "Event call timed out"}
        self._socket(pool, call)

        async def event_call(event):
            return {"error": "Client session disconnected."}
        answer = await asyncio.wait_for(_ask_user_input_modal(
            event_call, "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b",
            owui_user_id="user-1", owui_chat_id="chat-1", owui_message_id="msg-1",
            poll_interval_s=0.01), 2)
        self.assertIsNone(answer)

    async def test_disconnected_session_moves_to_the_users_new_socket(self):
        sio = FakeSio(lambda *call: {"status": "answered", "answers": {"answer": {"type": "option", "label": "a"}}})
        fake_owui(self, sio=sio, SESSION_POOL={"sid-new": {"id": "user-1"}})

        async def event_call(event):
            return {"error": "Client session disconnected."}

        answer = await _ask_user_input_modal(
            event_call, "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b",
            owui_user_id="user-1", owui_chat_id="chat-1", owui_message_id="msg-1",
            poll_interval_s=0.01,
        )
        self.assertEqual(answer, "a")
        _event, data, to, timeout = sio.calls[0]
        self.assertEqual(to, "sid-new")
        self.assertEqual(timeout, 240)
        self.assertEqual(data["chat_id"], "chat-1")
        interaction_id = data["data"]["data"]["interaction_id"]
        # The card is removed from the screen once the call settles.
        self.assertEqual(sio.emits[0][1]["data"], {
            "type": "request:interaction:done", "data": {"interaction_id": interaction_id}})

    # ELI-58: after a reload the card goes to a socket viewing this chat
    # (`SESSION_POOL[sid]["chat_ids"]`, OWUI 0.12) without waiting out the 240 s timeout.

    def fake_socket(self, pool, answer="a"):
        """The sids the card was sent to, in order (filled as it is sent)."""
        sent = []

        def reply(event, data, to, timeout):
            sent.append(to)
            return {"status": "answered", "answers": {"answer": {"type": "option", "label": answer}}}
        fake_owui(self, sio=FakeSio(reply), SESSION_POOL=pool)
        return sent

    async def test_the_card_comes_back_on_the_tab_viewing_the_chat(self):
        pool = {"sid-other": {"id": "user-1", "chat_ids": ["chat-x"]},
                "sid-tab": {"id": "user-1", "chat_ids": ["chat-1"]}}
        sent = self.fake_socket(pool)

        async def event_call(event):
            return {"error": "Client session disconnected."}

        answer = await _ask_user_input_modal(
            event_call, "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b",
            owui_user_id="user-1", owui_chat_id="chat-1", owui_message_id="msg-1",
            poll_interval_s=0.01)
        self.assertEqual(answer, "a")
        self.assertEqual(sent, ["sid-tab"], "never another chat's tab")

    async def test_with_no_tab_on_the_chat_the_reply_ends_with_the_question(self):
        """ELI-107: with no tab on the chat the question ends the reply as text instead of holding
        the stream open."""
        pool = {"sid-other": {"id": "user-1", "chat_ids": ["chat-x"]}}
        sent = self.fake_socket(pool)

        async def event_call(event):
            return {"error": "Client session disconnected."}

        answer = await asyncio.wait_for(_ask_user_input_modal(
            event_call, "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b",
            owui_user_id="user-1", owui_chat_id="chat-1", owui_message_id="msg-1",
            poll_interval_s=0.01), 2)
        self.assertIsNone(answer)
        self.assertEqual(sent, [], "never another chat's tab")

    async def test_a_reload_moves_the_card_without_waiting_out_owuis_timeout(self):
        """The old socket leaving SESSION_POOL moves the card; no wait for OWUI's 240 s timeout."""
        pool = {"sid-old": {"id": "user-1", "chat_ids": ["chat-1"]}}
        sent = self.fake_socket(pool)
        pending = asyncio.get_running_loop().create_future()

        async def event_call(event):
            return await pending                       # OWUI's 240 s wait

        async def reload_tab():
            await asyncio.sleep(0.05)
            del pool["sid-old"]
            pool["sid-new"] = {"id": "user-1", "chat_ids": ["chat-1"]}
        reloader = asyncio.ensure_future(reload_tab())
        answer = await asyncio.wait_for(_ask_user_input_modal(
            event_call, "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b",
            owui_user_id="user-1", owui_chat_id="chat-1", owui_message_id="msg-1",
            owui_session_id="sid-old", poll_interval_s=0.01), 2)
        await reloader
        self.assertEqual(answer, "a")
        self.assertEqual(sent, ["sid-new"])

    # ELI-142: a socket that stays in SESSION_POOL but never shows the card must not get re-shows
    # after each 240 s timeout.

    async def test_a_socket_that_never_answers_ends_with_the_question_as_text(self):
        pool = {"sid-tab": {"id": "user-1", "chat_ids": ["chat-1"]}}
        self.fake_socket(pool)
        calls = []

        async def event_call(event):
            calls.append(event)
            return {"error": "Event call timed out. The browser tab may be inactive or closed."}

        answer = await asyncio.wait_for(_ask_user_input_modal(
            event_call, "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b",
            owui_user_id="user-1", owui_chat_id="chat-1", owui_message_id="msg-1",
            owui_session_id="sid-tab", poll_interval_s=0.01), 2)
        self.assertIsNone(answer)
        self.assertEqual(len(calls), 1, "one card, never re-shown")

    async def test_a_moved_card_that_is_never_answered_ends_with_the_question_as_text(self):
        """A moved card that is never answered also ends with the question as text."""
        sent = []

        async def call(data, to):
            sent.append(to)
            return {"error": "Event call timed out: TimeoutError()"}
        self._socket({"sid-tab": {"id": "user-1", "chat_ids": ["chat-1"]}}, call)

        async def event_call(event):
            return {"error": "Client session disconnected."}
        answer = await asyncio.wait_for(_ask_user_input_modal(
            event_call, "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b",
            owui_user_id="user-1", owui_chat_id="chat-1", owui_message_id="msg-1",
            poll_interval_s=0.01), 2)
        self.assertIsNone(answer)
        self.assertEqual(sent, ["sid-tab"])


class AskUserMultiSelectCardTests(unittest.IsolatedAsyncioTestCase):
    """ELI-148: a multi-select question is a card listing options by number, answered by typing
    "1,3", which reaches the agent as the options. Uses only the card's entry points."""

    MULTI = ("OpenClaw needs input:\n\nToppings (multiselect)\nPick any\n"
             "1. Cheese\n2. Olives - green ones\n3. Mushrooms")

    def setUp(self):
        sys.modules.setdefault("open_webui", types.ModuleType("open_webui"))
        env = types.ModuleType("open_webui.env")
        env.VERSION = "0.12.0"
        sys.modules["open_webui.env"] = env
        self.addCleanup(sys.modules.pop, "open_webui.env", None)

    def _answer(self, typed, prompt=None):
        payload = _native_ask_user_payload(prompt or self.MULTI)
        self.assertIsNotNone(payload, "a multi-select question gets a card")
        return _format_native_answers(payload["data"]["questions"],
                                      {"answer": {"type": "other", "text": typed}})

    def test_the_card_lists_the_options_by_number_and_takes_typing(self):
        payload = _native_ask_user_payload(self.MULTI)
        self.assertEqual(payload["type"], "request:user_input")
        (q,) = payload["data"]["questions"]
        self.assertEqual(q["header"], "Toppings")
        # No option rows: a click would answer at once.
        self.assertEqual(q["options"], [])
        self.assertTrue(q["allow_other"])
        for line in ("1. Cheese", "2. Olives - green ones", "3. Mushrooms"):
            self.assertIn(line, q["question"])
        self.assertIn("Pick any", q["question"])
        self.assertNotIn("multiselect", json.dumps(payload).lower())

    def test_numbers_map_to_the_options(self):
        for typed in ("1,3", "1, 3", " 1 3 ", "1;3", "3, 1, 3"):
            want = "Mushrooms, Cheese" if typed.startswith("3") else "Cheese, Mushrooms"
            self.assertEqual(self._answer(typed), want, typed)

    def test_option_text_maps_to_the_options(self):
        self.assertEqual(self._answer("cheese, Mushrooms"), "Cheese, Mushrooms")
        # The short label before " - " names the option too.
        self.assertEqual(self._answer("Olives"), "Olives - green ones")

    def test_numbers_and_text_mixed(self):
        self.assertEqual(self._answer("2, Mushrooms"), "Olives - green ones, Mushrooms")

    def test_what_does_not_map_goes_as_typed(self):
        for typed in ("1, 4", "0", "none of these", "1, pineapple", "2 and 3"):
            self.assertEqual(self._answer(typed), typed, typed)

    def test_an_empty_answer_sends_nothing(self):
        self.assertEqual(self._answer("  "), "")
        # Only separators name no option: as typed.
        self.assertEqual(self._answer(" , "), ",")

    def test_the_hebrew_marker_is_a_multi_select_card(self):
        self.assertEqual(
            self._answer("1 2", "OpenClaw needs input:\n\nמה בא לך? בחר כמה\n1. פיצה\n2. סושי"),
            "פיצה, סושי")

    async def test_the_inline_ask_shows_the_card_and_returns_the_options(self):
        calls = []

        async def event_call(event):
            calls.append(event)
            return {"status": "answered", "answers": {"answer": {"type": "other", "text": "1,3"}}}

        answer = await _ask_user_input_modal(event_call, self.MULTI)
        self.assertEqual(answer, "Cheese, Mushrooms")
        self.assertEqual([c["type"] for c in calls], ["request:user_input"])

    # Unchanged by ELI-148.

    def test_single_select_keeps_its_option_rows(self):
        payload = _native_ask_user_payload("OpenClaw needs input:\n\nColor?\nPick\n1. Red\n2. Green")
        (q,) = payload["data"]["questions"]
        self.assertEqual([o["label"] for o in q["options"]], ["Red", "Green"])
        self.assertEqual(q["question"], "Pick")
        answers = {"answer": {"type": "option", "option_index": 1, "label": "Green"}}
        self.assertEqual(_format_native_answers(payload["data"]["questions"], answers), "Green")
        # A typed "2" on a single-select card goes as typed, as before.
        typed = {"answer": {"type": "other", "text": "2"}}
        self.assertEqual(_format_native_answers(payload["data"]["questions"], typed), "2")

    async def test_a_secret_keeps_the_masked_input(self):
        prompt = "OpenClaw needs input:\n\nToken\nPaste your API key"
        self.assertIsNone(_native_ask_user_payload(prompt), "the card has no masked field")
        calls = []

        async def event_call(event):
            calls.append(event)
            return {"value": " s3cret "}

        answer = await _ask_user_input_modal(event_call, prompt)
        self.assertEqual(answer, "s3cret")
        (event,) = calls
        self.assertEqual(event["type"], "input")
        self.assertEqual(event["data"]["type"], "password")
        self.assertEqual(event["data"]["title"], "Token")


class AskUserSecretDialogTests(unittest.IsolatedAsyncioTestCase):
    """ELI-148: a secret keeps OWUI's masked dialog, asked once, moved once to the viewing tab,
    never re-shown after a timeout. Its answer never shows in the chat."""

    PROMPT = "OpenClaw needs input:\n\nToken\nPaste your API key"
    VALUE = "s3cret-VALUE-9f2"

    def _sio(self, pool, reply):
        sio = FakeSio(lambda event, data, to, timeout: reply(data, to))
        fake_owui(self, sio=sio, SESSION_POOL=pool)
        return sio

    async def _ask(self, event_call, **kw):
        return await asyncio.wait_for(_ask_user_input_modal(
            event_call, self.PROMPT, owui_user_id="user-1", owui_chat_id="chat-1",
            owui_message_id="msg-1", poll_interval_s=0.01, **kw), 2)   # bounds the test only

    async def test_a_reload_moves_the_dialog_to_the_reloaded_tab(self):
        pool = {"sid-old": {"id": "user-1", "chat_ids": ["chat-1"]}}
        sio = self._sio(pool, lambda data, to: {"value": self.VALUE})
        pending = asyncio.get_running_loop().create_future()

        async def event_call(event):
            return await pending                       # the dialog left with the old page

        async def reload_tab():
            await asyncio.sleep(0.05)
            del pool["sid-old"]
            pool["sid-new"] = {"id": "user-1", "chat_ids": ["chat-1"]}
        reloader = asyncio.ensure_future(reload_tab())
        answer = await self._ask(event_call, owui_session_id="sid-old")
        await reloader
        self.assertEqual(answer, self.VALUE)
        ((_event, data, to, timeout),) = sio.calls
        self.assertEqual(to, "sid-new")
        self.assertEqual((data["data"]["type"], data["data"]["data"]["type"]), ("input", "password"))
        self.assertEqual(timeout, 240)
        self.assertEqual(sio.emits, [], "no card to close")

    async def test_with_no_tab_on_the_chat_the_question_ends_the_reply(self):
        pool = {"sid-old": {"id": "user-1", "chat_ids": ["chat-1"]},
                "sid-other": {"id": "user-1", "chat_ids": ["chat-x"]}}
        sio = self._sio(pool, lambda data, to: {"value": self.VALUE})

        async def event_call(event):
            del pool["sid-old"]                        # the tab is closed
            return await asyncio.get_running_loop().create_future()
        self.assertIsNone(await self._ask(event_call, owui_session_id="sid-old"))
        self.assertEqual(sio.calls, [], "never another chat's tab")

    async def test_a_moved_dialog_that_is_never_answered_ends_the_wait(self):
        pool = {"sid-tab": {"id": "user-1", "chat_ids": ["chat-1"]}}
        sio = self._sio(pool, lambda data, to: {"error": "Event call timed out"})

        async def event_call(event):
            return {"error": "Client session disconnected."}
        self.assertIsNone(await self._ask(event_call))
        self.assertEqual(len(sio.calls), 1, "shown once, never re-shown")

    def test_the_ask_user_block_never_shows_the_value(self):
        block = _ask_user_detail_block(self.PROMPT, self.VALUE)
        self.assertNotIn(self.VALUE, block)
        self.assertIn("secret entered", block)
        self.assertIn("Token", block)
        # Any other answer shows as given.
        self.assertIn('result="Blue"', _ask_user_detail_block(
            "OpenClaw needs input:\n\nColor?\nPick\n1. Red\n2. Blue", "Blue"))


class SecretAnswerNeverInTheChatTests(_TurnCase):
    """ELI-148: a secret typed into the masked dialog goes only to the agent: not the reply text or
    items, emitted events, turn-writer rewrites, or the pipe log."""

    VALUE = "s3cret-VALUE-9f2"

    async def test_the_value_reaches_only_the_agent(self):
        written, asked = [], []

        async def emitter(event):
            written.append(event)

        async def event_call(event):
            asked.append(event)
            return {"value": self.VALUE} if event.get("type") == "input" else None
        rewrites = []
        note = self.conn.note_turn_rewrite
        self.conn.note_turn_rewrite = lambda *a: (rewrites.append(a), note(*a))[1]
        logged = []
        log = mock.patch.object(self.ocp, "pipe_log", lambda *a, **k: logged.append(a))
        log.start()
        self.addCleanup(log.stop)

        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "deploy"}]}
        meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": "m1"}
        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta, __event_call__=event_call)
        chunks = []

        async def drain():
            async for c in gen:
                chunks.append(c)
        task = asyncio.ensure_future(drain())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "I need a key.\n\nOpenClaw needs input:\n\nToken\nPaste your API key\n"))
        q.put_nowait(self._final("run-1"))
        for _ in range(400):
            if len(self.sent) == 2:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(self.sent, ["deploy", self.VALUE], "the agent gets the value")
        answer_run = next(r for r in ("run-2", self.keys[-1])
                          if f"{self.SK}:{r}" in self.conn._consumers)
        q2 = self._queue(answer_run)
        q2.put_nowait(self._delta(answer_run, "Stored."))
        q2.put_nowait(self._final(answer_run))
        await asyncio.wait_for(task, 5)   # bounds the test only

        self.assertEqual([e["type"] for e in asked if e.get("type") == "input"], ["input"])
        shown = "".join(c for c in chunks if isinstance(c, str))
        self.assertIn("secret entered", shown)
        self.assertIn("Stored.", shown)
        for where, what in (("reply", chunks), ("events", written), ("rewrites", rewrites),
                            ("log", logged)):
            self.assertNotIn(self.VALUE, json.dumps(what, default=repr), where)


class SecretNotReceivedTests(_TurnCase):
    """ELI-148: an unanswered secret dialog never asks for the secret in the chat. The reply says
    it was not received; the agent is told once."""

    PROMPT = "OpenClaw needs input:\n\nToken\nPaste your API key here so I can deploy"
    NOTICE = openclaw_pipe.SECRET_NOT_RECEIVED_FOR_AGENT

    def assert_never_invites_a_chat_answer(self, text):
        self.assertNotIn("Paste your API key", text, "the agent's own question asks for the value")
        for invite in ("next message", "answer in", "reply with", "type it here", "needs input"):
            self.assertNotIn(invite, text.lower())
        self.assertIn("not received", text)
        self.assertIn("never in the chat", text)

    def test_the_fallback_text_never_invites_a_chat_answer(self):
        self.assert_never_invites_a_chat_answer(_ask_user_fallback_text(self.PROMPT))
        self.assertIn("**Token**", _ask_user_fallback_text(self.PROMPT))
        # Same when rebuilt from the transcript.
        block = _ask_user_detail_block(self.PROMPT, f"[Sat 10:00] {self.NOTICE}")
        self.assert_never_invites_a_chat_answer(block)
        self.assertNotIn("secret entered", block)
        # Any other question keeps its plain-text fallback.
        self.assertEqual(_ask_user_fallback_text("OpenClaw needs input:\n\nColor?\nWhich color?"),
                         "Color?\nWhich color?")

    async def test_the_agent_is_told_once_and_the_chat_never_asks(self):
        asked = []

        async def emitter(event):
            pass

        async def event_call(event):
            asked.append(event.get("type"))
            return None                                     # nobody answers the dialog
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "deploy"}]}
        meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": "m1"}
        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta, __event_call__=event_call)
        chunks = []

        async def drain():
            async for c in gen:
                chunks.append(c)
        task = asyncio.ensure_future(drain())

        async def until_sent(n):
            for _ in range(400):
                if len(self.sent) >= n:
                    return
                await asyncio.sleep(0.005)
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "I need a key.\n\n" + self.PROMPT + "\n"))
        q.put_nowait(self._final("run-1"))
        await until_sent(2)
        self.assertEqual(self.sent, ["deploy", self.NOTICE], "the agent is told nothing came")
        # The agent asks again in the same reply: the text alone this time.
        answer_run = next(r for r in ("run-2", self.keys[-1]) if f"{self.SK}:{r}" in self.conn._consumers)
        q2 = self._queue(answer_run)
        q2.put_nowait(self._delta(answer_run, "Still need it.\n\n" + self.PROMPT + "\n"))
        q2.put_nowait(self._final(answer_run))
        await asyncio.wait_for(task, 5)   # bounds the test only
        self.assertEqual(self.sent, ["deploy", self.NOTICE], "told once per reply")
        self.assertEqual(asked.count("input"), 2, "each ask shows the dialog")
        shown = "".join(c for c in chunks if isinstance(c, str))
        self.assertEqual(shown.count("not received"), 2)
        self.assertIn("Still need it.", shown)
        self.assert_never_invites_a_chat_answer(shown.replace("I need a key.", "").replace("Still need it.", ""))


class SecretDialogCancelledTests(_TurnCase):
    """ELI-189: Cancel on the masked dialog resolves with False (Open WebUI 0.12.3
    EventConfirmDialog `cancel`). The agent learns the user cancelled and the card says so."""

    PROMPT = "OpenClaw needs input:\n\nToken\nPaste your API key (password)"

    async def test_cancel_reaches_the_agent_as_a_cancel_and_the_card_says_cancelled(self):
        async def emitter(event):
            pass

        async def event_call(event):
            return False if event.get("type") == "input" else None    # the user clicks Cancel
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "deploy"}]}
        meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": "m1"}
        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta, __event_call__=event_call)
        chunks = []

        async def drain():
            async for c in gen:
                chunks.append(c)
        task = asyncio.ensure_future(drain())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "I need a key.\n\n" + self.PROMPT + "\n"))
        q.put_nowait(self._final("run-1"))
        for _ in range(400):
            if len(self.sent) == 2:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(len(self.sent), 2, f"the agent hears back: {self.sent}")
        told = self.sent[1]
        self.assertNotEqual(told.strip().lower(), "no", "a cancel is not the answer 'no'")
        self.assertIn("cancel", told.lower())
        answer_run = next(r for r in ("run-2", self.keys[-1]) if f"{self.SK}:{r}" in self.conn._consumers)
        q2 = self._queue(answer_run)
        q2.put_nowait(self._delta(answer_run, "Understood, no key."))
        q2.put_nowait(self._final(answer_run))
        await asyncio.wait_for(task, 5)   # bounds the test only
        shown = "".join(c for c in chunks if isinstance(c, str))
        self.assertNotIn("secret entered", shown)
        self.assertIn('result="Cancelled', shown)
        self.assertIn("Understood, no key.", shown)

    async def test_a_cancelled_card_rebuilt_from_the_transcript_says_cancelled(self):
        """The card rebuilt from the transcript (time-prefixed) still says cancelled."""
        async def event_call(event):
            return False
        told = await _ask_user_input_modal(event_call, self.PROMPT, owui_user_id=None, owui_chat_id="chat-1",
                                           owui_message_id="m1", poll_interval_s=0.01)
        block = _ask_user_detail_block(self.PROMPT, f"[Sat 10:00] {told}")
        self.assertIn('result="Cancelled', block)
        self.assertNotIn("secret entered", block)


class DynamicModelSelectorTests(unittest.TestCase):
    """ELI-11: dynamic model discovery."""

    def test_friendly_name_prefers_versioned_catalog_name_over_alias(self):
        # The catalog name carries the version, so two versions are not both "Opus".
        self.assertEqual(
            _friendly_name({"key": "anthropic/claude-opus-4-8",
                            "name": "Claude Opus 4.8", "tags": ["alias:opus"]}),
            "Claude Opus 4.8",
        )
        self.assertEqual(
            _friendly_name({"key": "claude-cli/claude-opus-5",
                            "name": "Claude Opus 5", "tags": ["alias:opus-5"]}),
            "Claude Opus 5",
        )

    def test_friendly_name_falls_back_to_alias_when_unnamed(self):
        self.assertEqual(_friendly_name({"key": "a/b", "tags": ["alias:opus"]}), "Opus")
        self.assertEqual(_friendly_name({"key": "a/b", "name": "", "tags": ["alias:sonnet-5"]}), "Sonnet-5")

    def test_friendly_name_no_alias_uses_name_field(self):
        self.assertEqual(_friendly_name({"key": "a/b", "name": "gpt-5.5", "tags": ["configured"]}), "gpt-5.5")

    def test_friendly_name_key_fallback(self):
        self.assertEqual(_friendly_name({"key": "openai/o3-mini", "tags": []}), "o3-mini")

    def test_provider_from_key(self):
        self.assertEqual(_provider_from_key("anthropic/claude-opus-4-8"), "anthropic")
        self.assertEqual(_provider_from_key("deepseek/deepseek-v4-flash"), "deepseek")
        self.assertEqual(_provider_from_key("o3-mini"), "")

    def test_pipe_selected_preset_default(self):
        pipe = Pipe()
        self.assertEqual(pipe._selected_preset({"model": "openclaw_gateway.default"}), "default")

    def test_pipe_selected_preset_key(self):
        pipe = Pipe()
        self.assertEqual(
            pipe._selected_preset({"model": "openclaw_gateway.deepseek/deepseek-v4-flash"}),
            "deepseek/deepseek-v4-flash",
        )

    def test_pipe_selected_preset_key_with_dot_in_version(self):
        # Only the function-id prefix's dot is stripped.
        pipe = Pipe()
        self.assertEqual(
            pipe._selected_preset({"model": "openclaw_gateway.google/gemini-3.1-pro-preview"}),
            "google/gemini-3.1-pro-preview",
        )

    def test_pipe_model_override_default_empty(self):
        pipe = Pipe()
        self.assertIsNone(pipe._model_override_for_preset("default"))

    def test_pipe_model_override_default_set(self):
        pipe = Pipe()
        pipe.valves.DEFAULT_MODEL = "openai/gpt-5.5"
        self.assertEqual(pipe._model_override_for_preset("default"), "openai/gpt-5.5")

    def test_pipe_model_override_raw_key_passthrough(self):
        pipe = Pipe()
        self.assertEqual(
            pipe._model_override_for_preset("openai/gpt-5.5"),
            "openai/gpt-5.5",
        )

    def test_normalize_model_entry_matches_real_gateway_shape(self):
        # Gateway `models.list` entries use id/name/provider/alias.
        raw = {
            "id": "claude-opus-4-8",
            "name": "Claude Opus 4.8",
            "provider": "anthropic",
            "alias": "opus",
            "available": True,
        }
        normalized = _normalize_model_entry(raw)
        self.assertEqual(normalized["key"], "anthropic/claude-opus-4-8")
        self.assertEqual(normalized["name"], "Claude Opus 4.8")
        self.assertIn("configured", normalized["tags"])
        self.assertIn("alias:opus", normalized["tags"])

    def test_normalize_model_entry_no_alias(self):
        raw = {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "provider": "anthropic", "available": True}
        normalized = _normalize_model_entry(raw)
        self.assertEqual(normalized["key"], "anthropic/claude-opus-4-6")
        self.assertNotIn("alias:", str(normalized["tags"]))

    def test_normalize_model_entry_unavailable_not_configured(self):
        raw = {"id": "x", "name": "X", "provider": "p", "available": False}
        normalized = _normalize_model_entry(raw)
        self.assertNotIn("configured", normalized["tags"])


class DiscoverModelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_discover_models_returns_fallback_when_everything_cold(self):
        """When no gateway connection and no cache, returns hardcoded fallback."""
        import openclaw_pipe as ocp
        orig_conn = ocp._gateway_connection
        try:
            ocp._gateway_connection = None
            class FakeValves:
                STATE_DIR = "/tmp/openclaw-test-discover"
            models, _source = await _discover_models_with_source(FakeValves())
            self.assertEqual(len(models), 5)
            keys = {m["key"] for m in models}
            self.assertIn("deepseek/deepseek-v4-flash", keys)
            self.assertIn("anthropic/claude-opus-4-8", keys)
        finally:
            ocp._gateway_connection = orig_conn

    async def test_discover_models_normalizes_live_gateway_response(self):
        """Live id/name/provider/alias entries are normalized before return and cache."""
        import openclaw_pipe as ocp

        class FakeConn:
            _ws = object()

            class _event_loop_task:
                @staticmethod
                def done():
                    return False

            async def send_request(self, method, params, timeout=5):
                return {
                    "models": [
                        {
                            "id": "claude-opus-4-8",
                            "name": "Claude Opus 4.8",
                            "provider": "anthropic",
                            "alias": "opus",
                            "available": True,
                        }
                    ]
                }

        class FakeValves:
            STATE_DIR = "/tmp/openclaw-test-discover-live"

        orig_conn = ocp._gateway_connection
        try:
            ocp._gateway_connection = FakeConn()
            models, _source = await _discover_models_with_source(FakeValves())
        finally:
            ocp._gateway_connection = orig_conn

        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["key"], "anthropic/claude-opus-4-8")
        self.assertIn("alias:opus", models[0]["tags"])

    async def test_discover_models_drops_gateway_unavailable_models(self):
        """Models listed as available:false (no credentials) are dropped; they could only 401."""
        import openclaw_pipe as ocp

        class FakeConn:
            _ws = object()

            class _event_loop_task:
                @staticmethod
                def done():
                    return False

            async def send_request(self, method, params, timeout=5):
                return {
                    "models": [
                        {
                            "id": "claude-haiku-5-5",
                            "name": "Claude Haiku 5.5",
                            "provider": "anthropic",
                            "available": False,
                            "unavailableReason": "missing-auth",
                        },
                        {
                            "id": "claude-haiku-5-5",
                            "name": "Claude Haiku 5.5",
                            "provider": "claude-cli",
                            "alias": "haiku",
                            "available": True,
                        },
                        # Older Gateways omit the field: keep those.
                        {"id": "gpt-5.5", "name": "GPT-5.5", "provider": "openai"},
                    ]
                }

        class FakeValves:
            STATE_DIR = "/tmp/openclaw-test-discover-unavailable"

        orig_conn = ocp._gateway_connection
        try:
            ocp._gateway_connection = FakeConn()
            models, _source = await _discover_models_with_source(FakeValves())
        finally:
            ocp._gateway_connection = orig_conn

        self.assertEqual(
            [m["key"] for m in models],
            ["claude-cli/claude-haiku-5-5", "openai/gpt-5.5"],
        )


class GetModelOptionsTests(unittest.TestCase):
    def test_returns_fallback_when_no_cache(self):
        options = Pipe.get_model_options()
        self.assertEqual(len(options), 5)
        values = {opt["value"] for opt in options}
        self.assertIn("deepseek/deepseek-v4-flash", values)
        for opt in options:
            self.assertIn("(", opt["label"])
            self.assertIn(")", opt["label"])


class SharedProactiveStateAcrossConnectionsTests(unittest.TestCase):
    """A leftover "zombie" connection from an old deploy must not re-write turns as proactive
    messages: inside OWUI liveness is shared via `open_webui.socket.main`."""

    def setUp(self):
        self._fake_owui = fake_owui(self)

    def _conn(self):
        return _GatewayConnection(lambda: None)

    def test_shared_state_helper_returns_same_object(self):
        a = _shared_gateway_state()
        b = _shared_gateway_state()
        self.assertIsNotNone(a)
        self.assertIs(a, b)
        self.assertIs(getattr(self._fake_owui, _SHARED_STATE_ATTR), a)

    def test_shared_state_backfills_new_keys_into_existing_dict(self):
        """The shared dict outlives redeploys, so new keys are backfilled with setdefault; a
        missing `chat_write_locks` would KeyError after a deploy."""
        old_state = {"delivered_live": {"keep": 1.0}}
        setattr(self._fake_owui, _SHARED_STATE_ATTR, old_state)
        state = _shared_gateway_state()
        self.assertIs(state, old_state)  # same object, mutated in place
        self.assertEqual(state["delivered_live"], {"keep": 1.0})  # not clobbered
        self.assertIn("chat_write_locks", state)
        # A connection built against it must not raise.
        conn = self._conn()
        self.assertIs(conn._chat_write_locks, state["chat_write_locks"])

    def test_zombie_sees_live_delivery_and_wont_redeliver(self):
        live, zombie = self._conn(), self._conn()
        live.mark_delivered_live("session-x", "run-1")
        # The zombie sees the run as already shown.
        self.assertTrue(zombie.was_delivered_live("session-x", "run-1"))

    def test_isolated_when_not_inside_owui(self):
        # Without OWUI's module each connection keeps private state.
        for k in ("open_webui.socket.main", "open_webui.socket", "open_webui"):
            sys.modules.pop(k, None)
        self.assertIsNone(_shared_gateway_state())
        a, b = self._conn(), self._conn()
        self.assertIsNot(a._delivered_live, b._delivered_live)


class ModelPatchCacheTests(unittest.TestCase):
    """ELI-59: per-session model-patch cache and lock against same-model bursts."""

    def test_cache_miss_initially(self):
        conn = _GatewayConnection(lambda: None)
        self.assertFalse(conn.model_patch_cached("s1", "m1"))

    def test_cache_hit_after_record(self):
        conn = _GatewayConnection(lambda: None)
        conn.record_model_patched("s1", "m1")
        self.assertTrue(conn.model_patch_cached("s1", "m1"))

    def test_cache_none_model_is_a_distinct_value(self):
        conn = _GatewayConnection(lambda: None)
        conn.record_model_patched("s1", None)
        self.assertTrue(conn.model_patch_cached("s1", None))
        self.assertFalse(conn.model_patch_cached("s1", "m1"))

    def test_cache_miss_for_different_model_or_session(self):
        conn = _GatewayConnection(lambda: None)
        conn.record_model_patched("s1", "m1")
        self.assertFalse(conn.model_patch_cached("s1", "m2"))
        self.assertFalse(conn.model_patch_cached("s2", "m1"))

    def test_cache_expires_after_ttl(self):
        conn = _GatewayConnection(lambda: None)
        conn.record_model_patched("s1", "m1")
        model, ts = conn._model_patch_cache["s1"]
        conn._model_patch_cache["s1"] = (model, ts - conn.MODEL_PATCH_CACHE_TTL_S - 1)
        self.assertFalse(conn.model_patch_cached("s1", "m1"))

    def test_invalidate_drops_entry(self):
        conn = _GatewayConnection(lambda: None)
        conn.record_model_patched("s1", "m1")
        conn.invalidate_model_patch("s1")
        self.assertFalse(conn.model_patch_cached("s1", "m1"))
        conn.invalidate_model_patch("s1")  # idempotent on missing key

    def test_lock_is_per_session_and_stable(self):
        conn = _GatewayConnection(lambda: None)
        a1 = conn.model_patch_lock("s1")
        a2 = conn.model_patch_lock("s1")
        b = conn.model_patch_lock("s2")
        self.assertIs(a1, a2)
        self.assertIsNot(a1, b)


class GatewayUrlParsingTests(unittest.TestCase):
    """Users paste URLs into GATEWAY_URL; `http://gw:18789` and `http://gw` must parse, not
    misparse or raise ValueError."""

    def test_plain_host_port(self):
        self.assertEqual(_parse_gateway_url("localhost:18789"), ("localhost", 18789, False))

    def test_host_only_uses_default_port(self):
        self.assertEqual(_parse_gateway_url("gw.local"), ("gw.local", 18789, False))

    def test_whitespace_is_tolerated(self):
        self.assertEqual(_parse_gateway_url("  gw.local:18789 "), ("gw.local", 18789, False))

    def test_every_scheme_is_accepted(self):
        for raw, tls in (("http://gw.local:18789", False), ("https://gw.local:18789", True),
                         ("ws://gw.local:18789", False), ("wss://gw.local:18789", True),
                         ("WSS://gw.local:18789", True)):
            with self.subTest(raw=raw):
                self.assertEqual(_parse_gateway_url(raw), ("gw.local", 18789, tls))

    def test_tls_without_a_port_means_443(self):
        for raw in ("wss://gw.local", "https://gw.local", "wss://gw.local/"):
            with self.subTest(raw=raw):
                self.assertEqual(_parse_gateway_url(raw), ("gw.local", 443, True))
        self.assertEqual(_parse_gateway_url("wss://[::1]"), ("[::1]", 443, True))

    def test_tls_keeps_an_explicit_port(self):
        self.assertEqual(_parse_gateway_url("wss://gw.local:8443"), ("gw.local", 8443, True))
        self.assertEqual(_parse_gateway_url("wss://[::1]:8443"), ("[::1]", 8443, True))

    def test_plain_without_a_port_is_still_18789(self):
        for raw in ("gw.local", "ws://gw.local", "http://gw.local"):
            with self.subTest(raw=raw):
                self.assertEqual(_parse_gateway_url(raw), ("gw.local", 18789, False))

    def test_scheme_without_port_falls_back_to_default(self):
        self.assertEqual(_parse_gateway_url("http://gw.local"), ("gw.local", 18789, False))

    def test_trailing_path_and_query_are_dropped(self):
        self.assertEqual(_parse_gateway_url("https://gw.local:8443/"), ("gw.local", 8443, True))
        self.assertEqual(_parse_gateway_url("gw.local:8443/api?x=1"), ("gw.local", 8443, False))

    def test_never_yields_a_scheme_as_the_hostname(self):
        host, _, _ = _parse_gateway_url("http://gw.local:18789")
        self.assertNotIn(host, ("http", "https", "ws", "wss"))

    def test_empty_raises_gateway_error_naming_the_valve(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                with self.assertRaises(GatewayError) as cm:
                    _parse_gateway_url(raw)
                self.assertIn("GATEWAY_URL", str(cm.exception))

    def test_bad_port_raises_gateway_error_not_valueerror(self):
        with self.assertRaises(GatewayError) as cm:
            _parse_gateway_url("gw.local:not-a-port")
        self.assertIn("GATEWAY_URL", str(cm.exception))
        self.assertIn("localhost:18789", str(cm.exception))

    def test_ipv6_must_be_bracketed_and_is_kept_bracketed(self):
        """websockets needs the brackets in the URL, so they are preserved."""
        self.assertEqual(_parse_gateway_url("[::1]:18789"), ("[::1]", 18789, False))
        self.assertEqual(_parse_gateway_url("[::1]"), ("[::1]", 18789, False))
        self.assertEqual(_parse_gateway_url("[fe80::1]"), ("[fe80::1]", 18789, False))
        self.assertEqual(_parse_gateway_url("http://[::1]:18789"), ("[::1]", 18789, False))

    def test_bare_ipv6_is_rejected_rather_than_silently_misparsed(self):
        """rpartition(":") would read "::1" as host ":" port 1; brackets beat guessing."""
        with self.assertRaises(GatewayError) as cm:
            _parse_gateway_url("::1")
        self.assertIn("brackets", str(cm.exception))

    def test_unclosed_bracket_is_reported(self):
        with self.assertRaises(GatewayError) as cm:
            _parse_gateway_url("[::1")
        self.assertIn("]", str(cm.exception))

    def test_port_must_be_in_range(self):
        for raw in ("host:0", "host:99999", "host:65536"):
            with self.subTest(raw=raw):
                with self.assertRaises(GatewayError) as cm:
                    _parse_gateway_url(raw)
                self.assertIn("65535", str(cm.exception))
        self.assertEqual(_parse_gateway_url("host:65535"), ("host", 65535, False))

    def test_unsupported_scheme_is_rejected_clearly(self):
        with self.assertRaises(GatewayError) as cm:
            _parse_gateway_url("ftp://gw.local:18789")
        self.assertIn("ftp://", str(cm.exception))


class ConnectRejectionMessageTests(unittest.TestCase):
    """'pairing required' explains the device approval command."""

    def test_pairing_message_names_the_command_and_device(self):
        msg = _explain_connect_rejection("pairing required", device_id="abc123")
        self.assertIn("openclaw devices approve", msg)
        self.assertIn("openclaw devices list", msg)
        self.assertIn("abc123", msg)

    def test_pairing_message_without_device_id_still_helps(self):
        msg = _explain_connect_rejection("device not approved", device_id=None)
        self.assertIn("openclaw devices approve", msg)

    def test_auth_failure_points_at_the_token_valve(self):
        for raw in ("unauthorized", "invalid token", "forbidden"):
            with self.subTest(raw=raw):
                msg = _explain_connect_rejection(raw)
                self.assertIn("GATEWAY_TOKEN", msg)

    def test_unrecognized_errors_pass_through_verbatim(self):
        self.assertEqual(_explain_connect_rejection("some novel failure"),
                         "some novel failure")


class FileServerValveOffOnLoadTests(unittest.TestCase):
    """A deploy that turns ENABLE_FILE_SERVER off must close the port an
    earlier load left open: the first model list after the load does it."""

    def _pipes(self, enabled):
        with tempfile.TemporaryDirectory() as tmp:
            p = Pipe()
            p.valves.STATE_DIR = tmp
            p.valves.ENABLE_FILE_SERVER = enabled
            with mock.patch("openclaw_pipe._stop_file_server") as stop:
                asyncio.run(p.pipes())
            return stop

    def test_the_model_list_closes_the_file_server_when_the_valve_is_off(self):
        self._pipes(False).assert_called_once_with()

    def test_the_model_list_leaves_it_alone_when_the_valve_is_on(self):
        self._pipes(True).assert_not_called()


class UnverifiedModelListTests(unittest.TestCase):
    """On a fresh install pipes() has no connection and no cache, so fallback models are marked
    unverified instead of looking real."""

    def _pipe(self, tmp):
        p = Pipe()
        p.valves.STATE_DIR = tmp
        p.valves.MAX_MODELS = 30
        return p

    def test_fresh_install_labels_every_fabricated_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = asyncio.run(self._pipe(tmp).pipes())
            self.assertGreater(len(entries), 1)
            default, models = entries[0], entries[1:]
            self.assertEqual(default["id"], "default")
            # Default works with no Gateway: it clears the override.
            self.assertNotIn(UNVERIFIED_MODEL_SUFFIX, default["name"])
            for e in models:
                self.assertIn(UNVERIFIED_MODEL_SUFFIX, e["name"], e)

    def test_labelling_never_touches_the_routing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            for e in asyncio.run(self._pipe(tmp).pipes()):
                self.assertNotIn(UNVERIFIED_MODEL_SUFFIX, e["id"])
                self.assertNotIn("--", e["id"])

    def test_cached_models_are_not_labelled_as_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_json_file(
                os.path.join(_state_dir(tmp), "models-cache.json"),
                {"models": [{"key": "vendor/real-model", "name": "Real", "tags": []}]},
            )
            entries = asyncio.run(self._pipe(tmp).pipes())
            self.assertEqual(len(entries), 2)
            self.assertNotIn(UNVERIFIED_MODEL_SUFFIX, entries[1]["name"])
            self.assertEqual(entries[1]["id"], "vendor/real-model")

    def test_source_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = self._pipe(tmp).valves
            _, source = asyncio.run(_discover_models_with_source(v))
            self.assertEqual(source, MODELS_SOURCE_FALLBACK)
            _write_json_file(
                os.path.join(_state_dir(tmp), "models-cache.json"),
                {"models": [{"key": "vendor/m", "name": "M", "tags": []}]},
            )
            _, source = asyncio.run(_discover_models_with_source(v))
            self.assertEqual(source, MODELS_SOURCE_CACHE)


class SessionPatchFailureAttributionTests(unittest.TestCase):
    """A mistyped AGENT_ID fails the turn's first agent-scoped RPC; it is blamed on AGENT_ID, not
    the model valves."""

    def test_agent_shaped_error_names_agent_id_not_the_model(self):
        msg = _explain_session_patch_failure(
            Exception("unknown agent 'typo'"), model_override=None, agent_id="typo")
        self.assertIn("AGENT_ID", msg)
        self.assertIn("typo", msg)
        self.assertNotIn("Model selection error", msg)

    def test_session_shaped_error_also_points_at_the_agent(self):
        for detail in ("no such session", "unknown session xyz", "agent not found"):
            with self.subTest(detail=detail):
                msg = _explain_session_patch_failure(
                    Exception(detail), model_override="a/b", agent_id="main")
                self.assertIn("AGENT_ID", msg)

    def test_genuine_model_failure_still_reads_as_a_model_failure(self):
        msg = _explain_session_patch_failure(
            Exception("model 'x/y' is not configured"),
            model_override="x/y", agent_id="main")
        self.assertIn("Model selection error", msg)
        self.assertIn("x/y", msg)

    def test_model_errors_are_never_blamed_on_the_agent(self):
        """"model 'x/y' not found" is a model error, never an AGENT_ID hint."""
        for detail in (
            "model 'x/y' not found",
            "model not found: openai/gpt-9",
            "no such model 'foo/bar'",
            "unknown model key",
            "model x/y is not configured for this agent",
            "model x/y unavailable for this session",
        ):
            with self.subTest(detail=detail):
                msg = _explain_session_patch_failure(
                    Exception(detail), model_override="x/y", agent_id="main")
                self.assertIn("Model selection error", msg)
                self.assertNotIn("AGENT_ID", msg)

    def test_error_naming_the_model_key_is_a_model_error(self):
        msg = _explain_session_patch_failure(
            Exception("openrouter/z-ai/glm-5.2 is not available"),
            model_override="openrouter/z-ai/glm-5.2", agent_id="main")
        self.assertIn("Model selection error", msg)

    def test_ambiguous_error_names_both_valves_rather_than_guessing(self):
        msg = _explain_session_patch_failure(
            Exception("provider not found"), model_override="x/y", agent_id="main")
        self.assertIn("AGENT_ID", msg)
        self.assertIn("x/y", msg)
        self.assertNotIn("Model selection error", msg)

    def test_model_failure_mentions_agent_default_when_no_override(self):
        msg = _explain_session_patch_failure(
            Exception("boom"), model_override=None, agent_id="main")
        self.assertIn("agent default", msg)


class ReviewFollowupRegressionTests(unittest.TestCase):
    """Defects found reviewing the fixes above."""

    def test_bracketed_hostname_is_rejected_not_crashed(self):
        """A bracketed hostname makes urlsplit() raise a bare ValueError that escaped every
        handler; it is rejected with a GatewayError."""
        for raw in ("[gateway.local]:18789", "[localhost]:18789", "[192.168.1.10]:18789"):
            with self.subTest(raw=raw):
                with self.assertRaises(GatewayError) as cm:
                    _parse_gateway_url(raw)
                self.assertIn("brackets", str(cm.exception))
        self.assertEqual(_parse_gateway_url("[fe80::1]:18789"), ("[fe80::1]", 18789, False))

    def test_multi_colon_non_ipv6_is_not_called_ipv6(self):
        with self.assertRaises(GatewayError) as cm:
            _parse_gateway_url("myhost:80:90")
        self.assertNotIn("is an IPv6 address", str(cm.exception))

    def test_non_ascii_digit_port_raises_gateway_error_not_valueerror(self):
        """str.isdigit() accepts superscripts int() rejects."""
        for raw in ("host:²", "host:¹8789"):
            with self.subTest(raw=raw):
                with self.assertRaises(GatewayError):
                    _parse_gateway_url(raw)

    def test_transient_failures_are_not_blamed_on_a_valve(self):
        """str(TimeoutError()) is "", which gave a dangling colon and wrong valve advice."""
        for err in (asyncio.TimeoutError(), ConnectionResetError(104, "reset")):
            with self.subTest(err=type(err).__name__):
                msg = _explain_session_patch_failure(
                    err, model_override="x/y", agent_id="main")
                self.assertNotIn("AGENT_ID", msg)
                self.assertNotIn("Model selection error", msg)
                self.assertIn("again", msg.lower())

    def test_the_start_timeout_is_plain_in_the_users_language_with_the_advice_collapsed(self):
        """S13: the 11-03 timeout text, English, for a question in Hebrew."""
        msg = _explain_session_patch_failure(
            asyncio.TimeoutError(), model_override=None, agent_id="main", user_text=HEBREW_ASK)
        he = openclaw_pipe._ERROR_TEXT[openclaw_pipe.LANG_HE]
        self.assertTrue(msg.startswith(he[openclaw_pipe.ERROR_TIMEOUT]), msg)
        self.assertIn("check that the Gateway is healthy", msg.split("<details>")[1])
        english = _explain_session_patch_failure(
            asyncio.TimeoutError(), model_override=None, agent_id="main", user_text="when is the next train")
        self.assertTrue(english.startswith(
            openclaw_pipe._ERROR_TEXT[openclaw_pipe.LANG_EN][openclaw_pipe.ERROR_TIMEOUT]))

    def test_dropdown_reads_the_cache_the_pipe_actually_writes(self):
        """get_model_options is a classmethod and read `_state_dir()` with no argument, missing a
        custom STATE_DIR cache."""
        with tempfile.TemporaryDirectory() as tmp:
            custom = os.path.join(tmp, "custom")
            p = Pipe()
            p.valves.STATE_DIR = custom
            _write_json_file(
                os.path.join(_state_dir(custom), "models-cache.json"),
                {"models": [{"key": "vendor/real", "name": "Real", "tags": []}]},
            )
            asyncio.run(p.pipes())
            labels = [o["label"] for o in Pipe.get_model_options()]
            self.assertTrue(any("Real" in l for l in labels), labels)
            for l in labels:
                self.assertNotIn(UNVERIFIED_MODEL_SUFFIX, l)


class RepeatedAskUserMarkerTests(unittest.TestCase):
    """A needs-input block emitted twice, glued to the last option, must not offer extra buttons
    like "Park itOpenClaw needs input: Which PoC do we run?"."""

    REAL = ("OpenClaw needs input:\n"
            "Which PoC do we run?\n"
            "1. Dev-tool micro-product\n"
            "2. Prediction markets, small bankroll\n"
            "3. Park itOpenClaw needs input: Which PoC do we run?\n"
            "1. Dev-tool micro-product\n"
            "2. Prediction markets, small bankroll\n"
            "3. Park it")

    def test_repeated_block_yields_three_clean_options(self):
        data = _parse_text_prompt(self.REAL)
        self.assertEqual(data["options"],
                         ["Dev-tool micro-product",
                          "Prediction markets, small bankroll",
                          "Park it"])

    def test_no_option_label_leaks_the_internal_marker(self):
        data = _parse_text_prompt(self.REAL)
        for label in data["options"]:
            self.assertNotIn("needs input:", label.lower())

    def test_truncation_keeps_only_the_first_block(self):
        out = _truncate_at_repeated_marker(self.REAL)
        self.assertEqual(out.lower().count("needs input:"), 1)
        self.assertTrue(out.rstrip().endswith("Park it"))

    def test_truncation_is_a_noop_on_a_normal_prompt(self):
        normal = "OpenClaw needs input:\nPick one\n1. Alpha\n2. Beta"
        self.assertEqual(_truncate_at_repeated_marker(normal), normal)

    def test_normal_prompt_is_unaffected(self):
        data = _parse_text_prompt(
            "OpenClaw needs input:\nPick one\n1. Alpha\n2. Beta\n3. Gamma")
        self.assertEqual(data["title"], "Pick one")
        self.assertEqual(data["options"], ["Alpha", "Beta", "Gamma"])

    def test_dedupe_preserves_first_seen_order(self):
        self.assertEqual(_dedupe_options(["b", "a", "B", "a", "c"]), ["b", "a", "c"])

    def test_dedupe_drops_blanks(self):
        self.assertEqual(_dedupe_options(["a", "  ", "", "a"]), ["a"])


def _catch_all_delta(raw_text: str, received: str, visible: str) -> str:
    """The text the renderer adds for a cumulative catch-all (no `delta`) after `received` arrived
    and `visible` showed (ELI-147)."""
    r = _TurnRenderer()
    r.seed({"received": received})
    r.visible_text = visible
    added = r.feed({"stream": "assistant", "data": {"text": raw_text}})
    return "".join(a for a in added if isinstance(a, str))


class MediaDirectiveDoubledFilenameTests(unittest.IsolatedAsyncioTestCase):
    """ELI-76: a bare `MEDIA:<file>.png` reply was saved with the name doubled (stream half fixed
    in ELI-80). A filename ends at the next `MEDIA:` and a glued copy is dropped."""

    BASE = "https://pipe.example.invalid:18791"
    F = "baseline-test2-0102-1785351819.png"
    MALFORMED = (f"![{F}MEDIA:{F}]"
                 f"(https://pipe.example.invalid:18791/{F}MEDIA:{F})")
    CORRECT = f"![{F}](https://pipe.example.invalid:18791/{F})"

    def _stream(self, catch_all_baseline):
        """Replay through the assistant-stream helpers; returns what the flush hands
        `_resolve_media`. `catch_all_baseline`: "received" (ELI-80) or "shown" (ELI-76)."""
        reply = "MEDIA:" + self.F
        prompt_pending = media_pending = received = visible = ""
        for delta in (reply[i:i + 9] for i in range(0, len(reply), 9)):
            received += delta
            flush, prompt_pending = _advance_input_prompt_buffer(prompt_pending, delta)
            if not flush:
                continue
            shown, media_pending = _advance_media_buffer(media_pending, flush)
            visible += shown
        self.assertEqual(visible, "")  # the whole directive was held back
        if catch_all_baseline == "received":
            delta = _catch_all_delta(reply, received, visible)
        else:
            delta = _item_delta_text(reply, "", visible)
        flush, prompt_pending = _advance_input_prompt_buffer(prompt_pending, delta)
        if flush:
            _shown, media_pending = _advance_media_buffer(media_pending, flush)
        return prompt_pending + media_pending

    def test_issue_sequence_resolves_to_one_correct_link(self):
        pending = self._stream("received")
        self.assertEqual(pending, "MEDIA:" + self.F)
        self.assertEqual(_resolve_media(pending, base_url=self.BASE), (self.CORRECT, True))

    def test_shown_only_catch_all_is_what_doubled_the_directive(self):
        """Pins the stream half of the mechanism."""
        self.assertEqual(self._stream("shown"), f"MEDIA:{self.F}MEDIA:{self.F}")

    def test_glued_duplicate_no_longer_yields_the_issue_link(self):
        """The exact malformed link from the issue, fed its exact input."""
        result, handled = _resolve_media(f"MEDIA:{self.F}MEDIA:{self.F}", base_url=self.BASE)
        self.assertTrue(handled)
        self.assertNotEqual(result, self.MALFORMED)
        self.assertEqual(result, self.CORRECT)

    def test_glued_distinct_directives_become_two_images(self):
        result, handled = _resolve_media("MEDIA:a.pngMEDIA:b.png", base_url="https://x")
        self.assertTrue(handled)
        self.assertEqual(result, "![a.png](https://x/a.png)\n![b.png](https://x/b.png)")

    def test_longer_name_sharing_the_prefix_is_not_swallowed(self):
        result, _ = _resolve_media("MEDIA:a.pngMEDIA:a.png.bak", base_url="https://x")
        self.assertEqual(result, "![a.png](https://x/a.png)\n![a.png.bak](https://x/a.png.bak)")

    async def test_owui_upload_path_uploads_once_with_the_real_name(self):
        uploads = []

        def fake_upload(fpath, base_url, token):
            uploads.append(fpath.rsplit("/", 1)[-1])
            return {"id": "f1", "meta": {"content_type": "image/png"}}

        with mock.patch("openclaw_pipe._upload_owui_file", side_effect=fake_upload), \
             mock.patch("os.path.isfile", return_value=True):
            result, handled = await _resolve_media_via_owui(
                f"MEDIA:{self.F}MEDIA:{self.F}",
                base_url="https://owui.example.com", token="tok", __event_emitter__=None,
            )
        self.assertTrue(handled)
        self.assertEqual(uploads, [self.F])
        self.assertEqual(result, f"![{self.F}](/api/v1/files/f1/content)")


class CatchAllDuplicatesAskUserPromptTests(unittest.TestCase):
    """ELI-80: the closing catch-all is diffed against everything received, not shown, so a
    withheld prompt is not appended twice."""

    BLOCK = ("OpenClaw needs input:\n"
             "Pick a theme\n"
             "1. Dark\n"
             "2. Light\n"
             "3. Auto")

    def _stream_withheld(self):
        """Feed the block through the ask-user buffer one delta at a time. Returns (pending,
        received, visible) as pipe() holds them."""
        pending, received, visible = "", "", ""
        for delta in (self.BLOCK[i:i + 7] for i in range(0, len(self.BLOCK), 7)):
            received += delta
            flush, pending = _advance_input_prompt_buffer(pending, delta)
            visible += flush
        return pending, received, visible

    def test_whole_block_is_withheld_and_nothing_is_shown(self):
        pending, received, visible = self._stream_withheld()
        self.assertEqual(pending, self.BLOCK)
        self.assertEqual(received, self.BLOCK)
        self.assertEqual(visible, "")

    def test_catch_all_adds_nothing_when_block_was_withheld(self):
        """The regression itself. Before the fix this returned the whole block."""
        _pending, received, visible = self._stream_withheld()
        self.assertEqual(_catch_all_delta(self.BLOCK, received, visible), "")

    def test_prompt_is_not_doubled(self):
        """End state the user sees: the assembled prompt, once."""
        pending, received, visible = self._stream_withheld()
        pending += _catch_all_delta(self.BLOCK, received, visible)
        self.assertEqual(pending, self.BLOCK)
        self.assertEqual(pending.lower().count("needs input:"), 1)
        self.assertNotIn("AutoOpenClaw", pending)

    def test_dialog_offers_three_options_not_six(self):
        pending, received, visible = self._stream_withheld()
        pending += _catch_all_delta(self.BLOCK, received, visible)
        data = _parse_text_prompt(pending)
        self.assertEqual(data["title"], "Pick a theme")
        self.assertEqual(data["options"], ["Dark", "Light", "Auto"])

    def test_diffing_against_shown_text_alone_is_the_bug(self):
        """Shown text is empty while a block is withheld, so a shown-only diff is wrong."""
        _pending, _received, visible = self._stream_withheld()
        shown_only = _catch_all_delta(self.BLOCK, visible, visible)
        self.assertEqual(shown_only, self.BLOCK)

    def test_genuinely_new_trailing_text_still_arrives(self):
        """The fix must not swallow real content the catch-all adds."""
        pending, received, visible = self._stream_withheld()
        full = self.BLOCK + "\nAnything else?"
        self.assertEqual(_catch_all_delta(full, received, visible), "\nAnything else?")

    def test_catch_all_after_normal_shown_text_is_unchanged(self):
        """No needs-input involved: unchanged."""
        received = visible = "Hello there."
        self.assertEqual(_catch_all_delta("Hello there.", received, visible), "")
        self.assertEqual(
            _catch_all_delta("Hello there. More.", received, visible), " More.")

    def test_empty_catch_all_is_noop(self):
        self.assertEqual(_catch_all_delta("", "abc", "abc"), "")


class MarkerAfterPreambleTests(unittest.TestCase):
    """The buffer only looked after the last newline, so a whole reply in one chunk with the marker
    mid-string leaked it with no dialog."""

    PREAMBLE = ("Deployed and verified. The live function now contains stuff.\n\n"
                "Now the real proof. Same shape as the one that failed.\n\n")
    BLOCK = "OpenClaw needs input:\nPick a theme\n1. Dark\n2. Light\n3. Auto"

    def _feed(self, deltas):
        pending, shown = "", ""
        for d in deltas:
            flush, pending = _advance_input_prompt_buffer(pending, d)
            shown += flush
        return pending, shown

    def test_whole_reply_in_one_chunk_still_triggers(self):
        """The whole reply in one chunk still opens the dialog."""
        pending, shown = self._feed([self.PREAMBLE + self.BLOCK])
        self.assertTrue(_is_user_input_prompt(pending))
        self.assertNotIn("needs input:", shown)
        self.assertIn("Deployed and verified", shown)

    def test_token_streamed_reply_still_triggers(self):
        msg = self.PREAMBLE + self.BLOCK
        pending, shown = self._feed([msg[i:i + 7] for i in range(0, len(msg), 7)])
        self.assertTrue(_is_user_input_prompt(pending))
        self.assertNotIn("needs input:", shown)

    def test_marker_only_reply_still_triggers(self):
        pending, shown = self._feed([self.BLOCK])
        self.assertTrue(_is_user_input_prompt(pending))
        self.assertEqual(shown, "")

    def test_ordinary_reply_is_not_held(self):
        pending, shown = self._feed(["A normal reply.\nWith two lines.\n"])
        self.assertFalse(_is_user_input_prompt(pending))
        self.assertIn("A normal reply.", shown)

    def test_preamble_is_still_shown(self):
        """The explanation before the question must not be swallowed."""
        _pending, shown = self._feed([self.PREAMBLE + self.BLOCK])
        self.assertIn("Now the real proof.", shown)

    def test_marker_line_index_finds_first_line_only(self):
        self.assertEqual(_marker_line_index("OpenClaw needs input:\nx"), 0)
        self.assertEqual(_marker_line_index("abc\nOpenClaw needs input:\nx"), 4)
        self.assertIsNone(_marker_line_index("abc\ndef"))

    def test_marker_must_open_a_line_not_appear_mid_sentence(self):
        """Prose mentioning the marker must not pop a dialog."""
        self.assertIsNone(
            _marker_line_index("I emit OpenClaw needs input: when I want a dialog."))

    def test_dialog_built_from_a_preamble_reply_is_clean(self):
        pending, _shown = self._feed([self.PREAMBLE + self.BLOCK])
        data = _parse_text_prompt(pending)
        self.assertEqual(data["title"], "Pick a theme")
        self.assertEqual(data["options"], ["Dark", "Light", "Auto"])


class _HandshakeWS:
    """Minimal Gateway handshake partner: challenge, then the connect reply."""

    def __init__(self, ok=True, boot_id="boot-1"):
        hello = {"server": {"bootId": boot_id}}
        self._frames = [
            json.dumps({"event": "connect.challenge",
                        "payload": {"nonce": "n", "ts": 1}}),
            json.dumps({"type": "res", "id": "1", "ok": ok, "payload": hello,
                        "error": {"message": "connect failed"}}),
        ]
        self.sent = []

    async def recv(self):
        return self._frames.pop(0)

    async def send(self, data):
        self.sent.append(data)


class ConnectionEpochTests(unittest.IsolatedAsyncioTestCase):
    """`connection_epoch` tells a thinking agent from a restart that took the run (no terminal
    event). It must move on reconnect."""

    def _conn(self):
        conn = _GatewayConnection(lambda: types.SimpleNamespace(
            GATEWAY_URL="localhost:18789", GATEWAY_TOKEN="tok"))
        conn._ensure_identity = lambda valves: None
        conn._ident = {"id": "device-1"}
        return conn

    async def _connect(self, conn, ok=True, boot_id="boot-1"):
        ws = _HandshakeWS(ok=ok, boot_id=boot_id)

        async def fake_connect(_url, **_kw):
            return ws

        with mock.patch.object(websockets, "connect", fake_connect, create=True), \
                mock.patch("openclaw_pipe._sign_challenge", lambda *a, **k: {}):
            await conn._connect_and_start()

    async def test_epoch_starts_at_zero_and_bumps_once_per_connect(self):
        conn = self._conn()
        self.assertEqual(conn.connection_epoch, 0)
        await self._connect(conn)
        self.assertEqual(conn.connection_epoch, 1)
        await self._connect(conn)
        self.assertEqual(conn.connection_epoch, 2)

    async def test_failed_connect_does_not_bump_the_epoch(self):
        """A rejected handshake is not a new connection."""
        conn = self._conn()
        with self.assertRaises(GatewayError):
            await self._connect(conn, ok=False)
        self.assertEqual(conn.connection_epoch, 0)

    async def test_reconnect_path_bumps_the_epoch(self):
        """A restart reaches `_connect_and_start` via `_reconnect`; cover that path."""
        conn = self._conn()
        conn._max_backoff = 0
        conn._reconnect_attempt = 0
        ws = _HandshakeWS()

        async def fake_connect(_url, **_kw):
            return ws

        with mock.patch.object(websockets, "connect", fake_connect, create=True), \
                mock.patch("openclaw_pipe._sign_challenge", lambda *a, **k: {}), \
                mock.patch("asyncio.create_task"):
            await conn._reconnect()

        self.assertEqual(conn.connection_epoch, 1)

    async def test_socket_accepts_gateway_frames_over_one_mib(self):
        """websockets' 1 MiB frame cap closed the socket (1009) on a big history reply."""
        conn = self._conn()
        seen = {}

        async def fake_connect(_url, **kw):
            seen.update(kw)
            return _HandshakeWS()

        with mock.patch.object(websockets, "connect", fake_connect, create=True), \
                mock.patch("openclaw_pipe._sign_challenge", lambda *a, **k: {}):
            await conn._connect_and_start()
        self.assertGreaterEqual(seen.get("max_size") or 0, 16 * 1024 * 1024)

    async def test_reconnect_to_the_same_gateway_is_not_a_restart(self):
        """A drop to the same Gateway (same bootId) is not a restart."""
        conn = self._conn()
        await self._connect(conn, boot_id="boot-a")
        await self._connect(conn, boot_id="boot-a")
        self.assertEqual(conn.connection_epoch, 2)
        self.assertEqual(conn.gateway_restarts, 0)

    async def test_a_new_boot_id_is_a_restart(self):
        conn = self._conn()
        await self._connect(conn, boot_id="boot-a")
        await self._connect(conn, boot_id="boot-b")
        self.assertEqual(conn.gateway_restarts, 1)
        self.assertEqual(conn.gateway_boot_id, "boot-b")


class ThinkingWiringTests(unittest.TestCase):
    """ELI-85: the pipe half of the thinking control and the filter's ladder cache."""

    SEND_KW = dict(session_key="agent:main:openwebui-u-c", message="hi",
                   idempotency_key="k", owui_chat_id=None, owui_user_id=None)

    def test_no_thinking_field_when_nothing_was_chosen(self):
        # Absent means the agent's configured level; any value overrides it.
        for value in (None, "", False):
            params = _owui_chat_send_params(thinking=value, **self.SEND_KW)
            self.assertNotIn("thinking", params, f"thinking={value!r}")

    def test_a_chosen_level_reaches_chat_send(self):
        params = _owui_chat_send_params(thinking="high", **self.SEND_KW)
        self.assertEqual(params["thinking"], "high")

    def test_off_is_sent_because_it_is_an_instruction_not_an_absence(self):
        params = _owui_chat_send_params(thinking="off", **self.SEND_KW)
        self.assertEqual(params["thinking"], "off")

    def test_ladder_read_from_thinking_levels(self):
        # thinkingLevels carries {id, label}: the ids clamp_to_ladder matches.
        desc = {"session": {"thinkingLevels": [
            {"id": "off", "label": "off"}, {"id": "medium", "label": "medium"}]}}
        self.assertEqual(_session_thinking_ladder(desc), ["off", "medium"])

    def test_thinking_levels_wins_when_both_are_present(self):
        # ELI-85: thinkingOptions holds display labels ("Off"), not ids, so it never matched and
        # the Gateway rejected the turn. thinkingLevels wins when both are present.
        desc = {"session": {"thinkingOptions": ["Off"],
                            "thinkingLevels": [{"id": "max"}]}}
        self.assertEqual(_session_thinking_ladder(desc), ["max"])

    def test_ladder_falls_back_to_the_labelled_form_lowercased(self):
        # thinkingOptions is the last resort, lowercased so "Off" still ranks.
        desc = {"session": {"thinkingOptions": ["Off", "High"]}}
        self.assertEqual(_session_thinking_ladder(desc), ["off", "high"])

    def test_no_ladder_is_None_not_empty(self):
        # None means unknown (pass through); [] would clamp every request away.
        for desc in ({}, {"session": {}}, {"session": {"thinkingOptions": []}},
                     {"session": {"thinkingLevels": [{"label": "no id"}]}}, None):
            self.assertIsNone(_session_thinking_ladder(desc), repr(desc))

    def _cache(self, td):
        path = os.path.join(td, LADDER_CACHE_NAME)
        return path, (json.load(open(path)) if os.path.exists(path) else None)

    def test_recording_a_ladder_writes_the_cache_the_filter_reads(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"OPENCLAW_BRIDGE_STATE_DIR": td}):
                _record_thinking_ladder(["high", "off", "low"])
            _, data = self._cache(td)
            # Ordered by rank, as the dropdown shows.
            self.assertEqual(data["levels"], ["off", "low", "high"])
            self.assertIsInstance(data["updated"], int)

    def test_the_cache_is_a_union_across_models(self):
        # The dropdown must not flicker as models change mid-chat.
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"OPENCLAW_BRIDGE_STATE_DIR": td}):
                _record_thinking_ladder(["off", "low", "medium", "high"])
                _record_thinking_ladder(["off", "xhigh", "max"])
            _, data = self._cache(td)
            self.assertEqual(data["levels"],
                             ["off", "low", "medium", "high", "xhigh", "max"])

    def test_unknown_level_ids_never_enter_the_cache(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"OPENCLAW_BRIDGE_STATE_DIR": td}):
                _record_thinking_ladder(["low", "turbo", "high"])
            _, data = self._cache(td)
            self.assertEqual(data["levels"], ["low", "high"])

    def test_nothing_recognisable_leaves_the_cache_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"OPENCLAW_BRIDGE_STATE_DIR": td}):
                _record_thinking_ladder([])
                _record_thinking_ladder(["turbo"])
                _record_thinking_ladder(None)
            self.assertFalse(os.path.exists(os.path.join(td, LADDER_CACHE_NAME)))

    def test_a_per_model_ladder_is_exact_while_the_union_stays_wide(self):
        # `levels` is the union for the dropdown; `models[<key>]` is exact for the send clamp, or
        # `high` reaches an `off`-only model.
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"OPENCLAW_BRIDGE_STATE_DIR": td}):
                _record_thinking_ladder(["off", "low", "high"],
                                        model_key="anthropic/claude-sonnet-5")
                _record_thinking_ladder(["off"], model_key="claude-cli/claude-opus-5",
                                        authoritative=True)
                self.assertEqual(
                    _read_model_thinking_ladder("claude-cli/claude-opus-5"), ["off"])
                self.assertEqual(
                    _read_model_thinking_ladder("anthropic/claude-sonnet-5"),
                    ["off", "low", "high"])
                # A never-seen model is unknown, not unsupported.
                self.assertIsNone(_read_model_thinking_ladder("openai/gpt-9"))
                self.assertIsNone(_read_model_thinking_ladder(None))
            _, data = self._cache(td)
            self.assertEqual(data["levels"], ["off", "low", "high"])

    def test_describe_never_overwrites_what_the_gateway_stated(self):
        # `sessions.describe` reports the generic profile with no catalog in scope; a rejection is
        # authoritative and a later weak observation must not undo it.
        with tempfile.TemporaryDirectory() as td:
            key = "claude-cli/claude-opus-5"
            with mock.patch.dict(os.environ, {"OPENCLAW_BRIDGE_STATE_DIR": td}):
                _record_thinking_ladder(["off"], model_key=key, authoritative=True)
                _record_thinking_ladder(["off", "low", "medium", "high"],
                                        model_key=key)
                self.assertEqual(_read_model_thinking_ladder(key), ["off"])
                # A later authoritative statement does replace it.
                _record_thinking_ladder(["off", "low"], model_key=key,
                                        authoritative=True)
                self.assertEqual(_read_model_thinking_ladder(key), ["off", "low"])
            _, data = self._cache(td)
            self.assertEqual(data["models"][key]["source"], "gateway")

    def test_model_key_is_formatted_the_way_the_gateway_names_it(self):
        # The key matches the model ref parsed from the rejection verbatim.
        self.assertEqual(
            _session_model_key({"modelProvider": "claude-cli",
                                "model": "claude-opus-5"}),
            "claude-cli/claude-opus-5")
        for row in ({"model": "claude-opus-5"}, {"modelProvider": "claude-cli"},
                    {"modelProvider": "", "model": " "}, {}, None):
            self.assertIsNone(_session_model_key(row), repr(row))

    def test_a_corrupt_cache_is_rebuilt_rather_than_inherited(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, LADDER_CACHE_NAME)
            open(path, "w").write("{ not json")
            with mock.patch.dict(os.environ, {"OPENCLAW_BRIDGE_STATE_DIR": td}):
                _record_thinking_ladder(["off", "high"])
            self.assertEqual(json.load(open(path))["levels"], ["off", "high"])

    def test_the_pipe_shares_the_filter_clamping_rule(self):
        # Same fragment in both artifacts, so pipe and dropdown clamp alike.
        got, note = clamp_to_ladder("max", ["off", "minimal", "low", "medium", "high"])
        self.assertEqual(got, "high")
        self.assertIn("high", note)
        self.assertEqual(clamp_to_ladder("medium", ["off", "medium"]), ("medium", None))
        self.assertEqual(clamp_to_ladder("high", None), ("high", None))
        self.assertEqual(clamp_to_ladder(None, ["off"]), (None, None))
        self.assertEqual(LEVEL_RANKS["off"], 0)

    def test_clamp_does_not_crash_on_an_unranked_ladder_entry(self):
        # A ladder entry outside LEVEL_RANKS degrades gracefully instead of a KeyError.
        got, note = clamp_to_ladder("high", ["off", "Unranked"])
        self.assertEqual(got, "off")
        self.assertIn("high", note)

    def test_the_pipe_artifact_exposes_no_Filter_class(self):
        # The shared fragment is class-free so OWUI never sees the pipe as a filter.
        import openclaw_pipe
        self.assertFalse(hasattr(openclaw_pipe, "Filter"))
        self.assertTrue(hasattr(openclaw_pipe, "Pipe"))


class ThinkingRejectionRetryTests(unittest.TestCase):
    """ELI-85: a level the model cannot honour makes the Gateway reject the turn, with no describe
    row or a wrong ladder. The text surfaces only via `recover_from_transcript()`, which
    `_thinking_rejection_retry_body` gates."""

    REJECTION_TEXT = (
        'Thinking level "high" is not supported for claude-cli/'
        'claude-opus-5. Use one of: off.'
    )

    def test_the_parser_matches_the_real_gateway_wording(self):
        # Detection and learning are one call, so they agree on what a rejection is.
        got = parse_thinking_rejection(self.REJECTION_TEXT)
        self.assertEqual(got, {"level": "high",
                               "model": "claude-cli/claude-opus-5",
                               "levels": ["off"]})

    def test_the_parser_does_not_misfire_on_ordinary_text_about_thinking(self):
        # Narrow: assistant prose about thinking levels must not match.
        for text in (
            "Let me think about supported levels here.",
            "high is not supported in this context, unrelated to models.",
            'Thinking level "high" is not supported for some models. Use one of: off.',
            None,
            "",
        ):
            self.assertIsNone(parse_thinking_rejection(text), repr(text))

    def test_retry_body_built_when_recovered_text_is_the_rejection(self):
        body = {"reasoning_effort": "high", "messages": ["hi"]}
        retry_body = _thinking_rejection_retry_body(
            body, self.REJECTION_TEXT, "high")
        self.assertIsNotNone(retry_body)
        self.assertNotIn("reasoning_effort", retry_body)
        self.assertEqual(retry_body["messages"], ["hi"])
        # The original body stays untouched for logging.
        self.assertEqual(body["reasoning_effort"], "high")

    def test_no_retry_when_nothing_was_recovered(self):
        self.assertIsNone(
            _thinking_rejection_retry_body({"reasoning_effort": "high"},
                                            None, "high"))

    def test_no_retry_when_no_explicit_level_was_ever_sent(self):
        # No override requested, or already retried: retrying would loop.
        retry_body = _thinking_rejection_retry_body(
            {"reasoning_effort": None}, self.REJECTION_TEXT, None)
        self.assertIsNone(retry_body)

    def test_no_retry_when_recovered_text_is_unrelated(self):
        retry_body = _thinking_rejection_retry_body(
            {"reasoning_effort": "high"}, "Here is your answer.", "high")
        self.assertIsNone(retry_body)

    def test_retry_cannot_fire_twice_in_a_row(self):
        # The first pass strips reasoning_effort, so the second always has resolved_thinking=None:
        # that ends the recursion.
        first_body = {"reasoning_effort": "high"}
        retried = _thinking_rejection_retry_body(
            first_body, self.REJECTION_TEXT, "high")
        self.assertIsNotNone(retried)
        second = _thinking_rejection_retry_body(
            retried, self.REJECTION_TEXT, None)
        self.assertIsNone(second)


class TestSpawnBackground(unittest.TestCase):
    """spawn_background keeps a task referenced; the loop holds only a weak one."""

    def test_task_is_held_until_done_then_released(self):
        import asyncio
        import gc
        import openclaw_pipe

        ran = []

        async def work():
            await asyncio.sleep(0)
            gc.collect()
            await asyncio.sleep(0)
            ran.append(True)

        held = {}

        async def main():
            # The set is module-wide; other tasks in it do not matter.
            other = openclaw_pipe.spawn_background(asyncio.sleep(60))
            task = openclaw_pipe.spawn_background(work())
            gc.collect()
            held["while running"] = task in openclaw_pipe._BACKGROUND_TASKS
            for _ in range(5):
                await asyncio.sleep(0)
            held["after"] = task in openclaw_pipe._BACKGROUND_TASKS
            held["other"] = other in openclaw_pipe._BACKGROUND_TASKS
            other.cancel()
            await asyncio.sleep(0)

        asyncio.run(main())
        self.assertEqual(ran, [True])
        self.assertEqual(held, {"while running": True, "after": False, "other": True})

    def test_no_bare_create_task_left_in_sources(self):
        # A create_task whose result is discarded is exactly the bug; ruff's
        # RUF006 enforces this in CI, this keeps it true without ruff.
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "src", "openclaw_pipe_pkg")
        offenders = []
        for name in sorted(os.listdir(src)):
            if not name.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(src, name)).read())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Attribute)
                        and node.value.func.attr == "create_task"):
                    offenders.append(f"{name}:{node.lineno}")
        self.assertEqual(offenders, [], "use spawn_background() instead")


class OrphanStubLoopTests(unittest.IsolatedAsyncioTestCase):
    """Startup pass over unfinished replies (ELI-90/156): one stub at a time, running stubs get a
    waiter, failures do not stop the rest."""

    async def _run(self, stubs, statuses):
        import openclaw_pipe
        calls, order = [], []

        async def reconcile(conn, agent_id, chat_id, stub_id, *, wait=True):
            calls.append((stub_id, wait))
            if not wait:  # the pass itself; a waiter runs beside it
                order.append(f"start {stub_id}")
            await asyncio.sleep(0)
            if not wait:
                order.append(f"end {stub_id}")
            status = statuses[stub_id] if not wait else "finished"
            if isinstance(status, Exception):
                raise status
            return status

        async def stub_ids(prefixes):
            return [("chat-1", s) for s in stubs]

        async def get_conn(valves_getter):
            return object()

        ended = mock.AsyncMock(side_effect=RuntimeError("history down"))
        with mock.patch("openclaw_pipe._orphan_stub_ids", stub_ids), \
                mock.patch("openclaw_pipe._reconcile_stub", reconcile), \
                mock.patch("openclaw_pipe._reconcile_ended_while_down", ended), \
                mock.patch("openclaw_pipe._get_gateway_connection", get_conn), \
                mock.patch("openclaw_pipe._composer_model_prefixes", lambda **kw: ["openclaw_gateway."]), \
                mock.patch("openclaw_pipe._composer_own_function_id", lambda: "openclaw_gateway"):
            await openclaw_pipe._reconcile_orphan_stubs(lambda: types.SimpleNamespace(AGENT_ID="main"))
            for _ in range(10):
                await asyncio.sleep(0)
        return calls, order, ended

    async def test_each_stub_in_turn_and_a_waiter_only_for_a_running_one(self):
        calls, order, ended = await self._run(
            ["a", "b", "c"], {"a": "finished", "b": "wait", "c": RuntimeError("chat gone")})
        self.assertEqual(order, ["start a", "end a", "start b", "end b", "start c", "end c"])
        self.assertEqual(calls, [("a", False), ("b", False), ("c", False), ("b", True)])
        ended.assert_awaited_once()

    async def test_no_stubs_still_runs_the_ended_while_down_pass(self):
        calls, _order, ended = await self._run([], {})
        self.assertEqual(calls, [])
        ended.assert_awaited_once()


class SettledTailTests(unittest.IsolatedAsyncioTestCase):
    """What a reply shows after a settle with no final: the rest, or why nothing."""

    def _turn(self, *, history=None, text_yielded=False, received=""):
        import openclaw_pipe
        turn = openclaw_pipe._PipeTurn(None, {}, None, None, None, None, None, None, None, "", [], None)
        turn.conn = types.SimpleNamespace(session_history=mock.AsyncMock(return_value=history))
        turn.session_key, turn.our_run_id = "agent:main:x", "run-1"
        turn.renderer = openclaw_pipe._TurnRenderer()
        turn.renderer.seed({"received": received})
        turn.text_yielded, turn.first_event_arrived, turn.text = text_yielded, False, "hi"
        return turn

    async def test_the_rest_of_the_runs_text(self):
        turn = self._turn(history={"messages": []}, received="Hello ")
        with mock.patch("openclaw_pipe._history_text_for_run", return_value=(True, "Hello world")):
            self.assertEqual(await turn.settled_tail({"status": "done"}), "world")

    async def test_a_stopped_run_that_showed_nothing(self):
        for status in ("cancelled", "killed"):
            self.assertEqual(await self._turn().settled_tail({"status": status}), "**Stopped.**")

    async def test_a_failed_run_that_showed_nothing(self):
        self.assertEqual(await self._turn().settled_tail({"status": "failed", "lastRunError": "quota"}),
                         openclaw_pipe._plain_error(openclaw_pipe.ERROR_RUN, "quota", "hi"))
        self.assertEqual(await self._turn().settled_tail({"status": "timeout"}),
                         "**The reply didn't finish.** Please send your message again.")

    async def test_a_run_that_showed_text_owes_nothing_more(self):
        self.assertEqual(await self._turn(text_yielded=True).settled_tail({"status": "failed"}), "")


class TurnIdentityTests(unittest.TestCase):
    """ELI-100 DESIGN 2.2: the message a run renders into, from its id alone."""

    CHAT = "0abe9bde-9b86-4ef3-8a46-a3a98ed9410a"
    M = "8acdce95-1111-4222-8333-444455556666"
    KEY = f"agent:main:openwebui-00000000-0000-4000-8000-0000000000a1-{CHAT}"

    def test_the_replys_own_run_names_its_message(self):
        run = pipe_run_id(self.CHAT, self.M)
        self.assertEqual(parse_pipe_run_id(run)["kind"], "reply")
        self.assertEqual(turn_message_id(self.KEY, run, self.CHAT), self.M)

    def test_input_and_resume_runs_continue_the_same_message(self):
        for kind in ("input", "resume"):
            run = pipe_run_id(self.CHAT, self.M, kind)
            self.assertTrue(run.startswith(f"msg-{self.CHAT}-{self.M}-{kind}-"))
            self.assertEqual(parse_pipe_run_id(run)["kind"], kind)
            self.assertEqual(turn_message_id(self.KEY, run, self.CHAT), self.M)
            self.assertIsNotNone(_run_id_sent_at(run), "the send time stays last")

    def test_an_agent_turn_gets_a_stable_derived_id(self):
        a = turn_message_id(self.KEY, "m3-agent-1f257b6e", self.CHAT)
        self.assertEqual(a, turn_message_id(self.KEY, "m3-agent-1f257b6e", self.CHAT))
        self.assertEqual(a, str(uuid.uuid5(TURN_NS, f"{self.KEY}:m3-agent-1f257b6e")))
        self.assertNotEqual(a, turn_message_id(self.KEY, "m3-agent-other", self.CHAT))

    def test_a_pipe_run_id_from_another_chat_is_not_trusted(self):
        other = "11111111-2222-4333-8444-555566667777"
        run = pipe_run_id(other, self.M)
        self.assertNotEqual(turn_message_id(self.KEY, run, self.CHAT), self.M)

    def test_old_unkeyed_runs_are_agent_turns(self):
        for run in (f"msg-{self.CHAT}-1791849271.07", f"resume-{self.CHAT}-1791849383.28",
                    f"user-input-{self.CHAT}-1.0"):
            self.assertIsNone(parse_pipe_run_id(run))
            self.assertEqual(turn_message_id(self.KEY, run, self.CHAT),
                             str(uuid.uuid5(TURN_NS, f"{self.KEY}:{run}")))

    def test_the_status_actions_runs_get_no_message(self):
        self.assertIsNone(turn_message_id(self.KEY, f"{ACTION_RUN_PREFIX}compact-{self.CHAT}-1.0"))
        self.assertIsNone(turn_message_id(self.KEY, ""))
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "openclaw_pipe_pkg",
                                "action.py")).read()
        self.assertIn('idempotency_key = f"{ACTION_RUN_PREFIX}compact-', src)


class ClaimTurnMessageTests(unittest.IsolatedAsyncioTestCase):
    """ELI-100 DESIGN 2.3: the claim of a run's message under the chat's row lock. The fake
    transaction holds a real lock and yields between read and commit, so claims interleave."""

    CHAT, USER = "c1", "u1"

    def setUp(self):
        self.chat = types.SimpleNamespace(chat={"models": ["openclaw_gateway.m"], "history": {
            "currentId": "a1", "messages": {
                "q1": {"id": "q1", "role": "user", "parentId": None, "childrenIds": ["a1"]},
                "a1": {"id": "a1", "role": "assistant", "parentId": "q1", "childrenIds": [],
                       "model": "openclaw_gateway.m", "done": True},
            }}}, current_message_id="a1", updated_at=0)
        self.rows = {}
        lock = asyncio.Lock()
        test = self

        class Chats:
            @staticmethod
            @contextlib.asynccontextmanager
            async def _chat_transaction(chat_id):
                async with lock:
                    await asyncio.sleep(0)
                    yield ("db", test.chat)

        class ChatMessages:
            @staticmethod
            async def upsert_message(mid, chat_id, user_id, m, db=None):
                test.rows[mid] = copy.deepcopy(m)

        chats_mod = types.ModuleType("open_webui.models.chats")
        chats_mod.Chats = Chats
        cm_mod = types.ModuleType("open_webui.models.chat_messages")
        cm_mod.ChatMessages = ChatMessages
        attrs = types.ModuleType("sqlalchemy.orm.attributes")
        attrs.flag_modified = lambda obj, name: None
        mods = {**owui_modules(),
                "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.chats": chats_mod,
                "open_webui.models.chat_messages": cm_mod,
                "sqlalchemy": types.ModuleType("sqlalchemy"),
                "sqlalchemy.orm": types.ModuleType("sqlalchemy.orm"),
                "sqlalchemy.orm.attributes": attrs}
        self.socket_main = mods["open_webui.socket.main"]
        p = mock.patch.dict(sys.modules, mods)
        p.start()
        self.addCleanup(p.stop)

    def msgs(self):
        return self.chat.chat["history"]["messages"]

    async def claim(self, mid, run, writer="w1", takeover=False):
        return await _claim_turn_message(self.CHAT, self.USER, mid, run, writer, takeover)

    async def test_a_new_message_goes_under_the_tail_and_becomes_current(self):
        r = await self.claim("m1", "run-1")
        self.assertEqual((r["claimed"], r["how"], r["parent"]), (True, "new", "a1"))
        m = self.msgs()["m1"]
        self.assertEqual((m["role"], m["done"], m["content"], m["output"]), ("assistant", False, "", []))
        self.assertEqual(m["meta"], {"openclaw_run": "run-1", "openclaw_writer": "w1"})
        self.assertEqual(m["model"], "openclaw_gateway.m")
        self.assertEqual(self.msgs()["a1"]["childrenIds"], ["m1"])
        self.assertEqual(self.chat.chat["history"]["currentId"], "m1")
        self.assertEqual(self.chat.current_message_id, "m1")
        self.assertEqual(set(self.rows), {"m1", "a1"}, "chat_message rows mirrored")
        self.assertEqual(set(r["messages"]), {"m1", "a1"})

    async def test_the_tail_is_the_deepest_leaf_not_a_stale_current_id(self):
        self.chat.chat["history"]["currentId"] = "q1"
        r = await self.claim("m1", "run-1")
        self.assertEqual(r["parent"], "a1", "never a sibling of a1")

    async def test_three_concurrent_claims_of_one_run_make_one_message(self):
        for i in range(3):  # three live connections (an old deploy's and new ones)
            _register_turn_writer(f"w{i}")
            self.addCleanup(_retire_turn_writer, f"w{i}")
        rs = await asyncio.gather(*(self.claim("m1", "run-1", writer=f"w{i}") for i in range(3)))
        self.assertEqual(sum(r["claimed"] for r in rs), 1)
        self.assertEqual(self.msgs()["a1"]["childrenIds"], ["m1"])

    async def test_two_runs_starting_together_chain_and_never_branch(self):
        r1, r2 = await asyncio.gather(self.claim("m1", "run-1"), self.claim("m2", "run-2"))
        self.assertEqual(r1["parent"], "a1")
        self.assertEqual(r2["parent"], "m1")
        self.assertTrue(all(len(m.get("childrenIds") or []) <= 1 for m in self.msgs().values()),
                        "no variant group")

    async def test_a_message_with_a_live_writer_is_not_taken(self):
        _register_turn_writer("w1")
        self.addCleanup(_retire_turn_writer, "w1")
        await self.claim("m1", "run-1", writer="w1")
        r = await self.claim("m1", "run-1", writer="w2")
        self.assertEqual((r["claimed"], r["how"]), (False, "taken"))
        self.assertEqual(self.msgs()["m1"]["meta"]["openclaw_writer"], "w1")

    async def test_a_message_whose_writer_is_gone_is_taken_over_in_place(self):
        await self.claim("m1", "run-1", writer="dead-writer")
        r = await self.claim("m1", "run-1", writer="w2")
        self.assertEqual((r["claimed"], r["how"], r["parent"]), (True, "takeover", "a1"))
        self.assertEqual(self.msgs()["m1"]["meta"]["openclaw_writer"], "w2")
        self.assertEqual(self.msgs()["a1"]["childrenIds"], ["m1"], "not inserted twice")

    async def test_a_finished_message_is_never_taken_over(self):
        await self.claim("m1", "run-1", writer="dead-writer")
        self.msgs()["m1"]["done"] = True
        self.assertFalse((await self.claim("m1", "run-1", writer="w2"))["claimed"])

    async def test_open_webuis_own_stub_is_handed_over_only_by_its_request(self):
        self.msgs()["a1"].update(done=False, content="")
        self.assertFalse((await self.claim("a1", "msg-run"))["claimed"],
                         "no recorded writer: not an orphan by itself")
        r = await self.claim("a1", "msg-run", takeover=True)
        self.assertEqual((r["claimed"], r["how"]), (True, "takeover"))
        self.assertEqual(self.msgs()["a1"]["meta"]["openclaw_writer"], "w1")

    # Writer liveness spans Open WebUI workers: the other worker is a real second process minting
    # its writer id as the pipe does.

    def other_worker(self, stay_alive):
        code = ("import sys, types, conftest, openclaw_pipe\n"
                "c = openclaw_pipe._GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID='main'))\n"
                "print(c.writer_id, flush=True)\n"
                + ("sys.stdin.read()\n" if stay_alive else ""))
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=os.path.dirname(os.path.abspath(__file__)),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        writer = proc.stdout.readline().strip()
        if stay_alive:
            self.addCleanup(proc.wait, 30)
            self.addCleanup(proc.stdin.close)
        else:
            proc.wait(30)
        self.assertTrue(writer)
        return writer

    async def test_a_live_writer_in_another_worker_is_not_taken(self):
        writer = self.other_worker(stay_alive=True)
        await self.claim("m1", "run-1", writer=writer)
        r = await self.claim("m1", "run-1", writer="w2")
        self.assertEqual((r["claimed"], r["how"]), (False, "taken"))
        self.assertEqual(self.msgs()["m1"]["meta"]["openclaw_writer"], writer)

    async def test_a_writer_whose_worker_exited_is_taken_over(self):
        writer = self.other_worker(stay_alive=False)
        await self.claim("m1", "run-1", writer=writer)
        r = await self.claim("m1", "run-1", writer="w2")
        self.assertEqual((r["claimed"], r["how"]), (True, "takeover"))

    async def test_a_retired_writer_of_this_process_is_taken_over(self):
        conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        await self.claim("m1", "run-1", writer=conn.writer_id)   # never registered: retired
        r = await self.claim("m1", "run-1", writer="w2")
        self.assertEqual((r["claimed"], r["how"]), (True, "takeover"))


def _row(status, err=None, aborted=False, run="R"):
    """A settled session row as the Gateway pushes it (DESIGN 2.7, F5)."""
    return {"status": status, "lastRunError": err, "abortedLastRun": aborted, "lastRunId": run}


class TurnOwnerTests(unittest.TestCase):
    """ELI-100 DESIGN 2.7: who continues an interrupted run, per shutdown kind."""

    def test_the_four_shutdown_kinds(self):
        exit143 = "Claude Code process exited with code 143"
        cases = [
            ("container stop: child exits 143 first", _row("failed", exit143), False, "pipe"),
            ("restart mode: cancelled stopReason restart", _row("running", aborted=True), False, "gateway"),
            ("crash: no terminal, row still running", _row("running"), False, "gateway"),
            ("gateway stop mode: rpc abort after the notice", _row("killed", aborted=True), True, "pipe"),
        ]
        for name, row, notice, want in cases:
            with self.subTest(name):
                self.assertEqual(turn_owner(row, notice), want)

    def test_runs_nobody_continues(self):
        cases = [
            ("a user Stop", _row("killed", aborted=True)),
            ("a real error", _row("failed", "429 rate limited")),
            ("a CLI that exits 1", _row("failed", "Claude Code process exited with code 1")),
            ("a finished run", _row("done")),
            ("no row", {}),
        ]
        for name, row in cases:
            with self.subTest(name):
                self.assertEqual(turn_owner(row, False), "none")

    def test_the_gateway_owns_a_running_row_even_after_a_notice(self):
        self.assertEqual(turn_owner(_row("running", aborted=True), True), "gateway")


@contextlib.contextmanager
def _scripts_on_path():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    try:
        yield
    finally:
        sys.path.pop(0)


def _installed_dist(test) -> str:
    """The OpenClaw dist to check fixtures against. No OpenClaw installed: skip; with OPENCLAW_DIST
    set a missing dist fails."""
    with _scripts_on_path():
        from openclaw_dist import default_dist
    dist = default_dist()
    if os.environ.get("OPENCLAW_DIST"):
        test.assertTrue(os.path.isdir(dist), f"OPENCLAW_DIST={dist!r} is not a directory")
    elif not dist or not os.path.isdir(dist):
        test.skipTest(f"no OpenClaw dist installed (looked at {dist!r})")
    return dist


class ExitTextFixtureTest(unittest.TestCase):
    """The owner rule matches the Gateway's exit-error TEXT (no structured field, DESIGN 2.7). The
    fixture is the dist's exact source line; this fails if an installed dist changed it."""

    FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                           "test_fixture_openclaw_exit_errors.json")

    def setUp(self):
        self.fx = json.load(open(self.FIXTURE))
        self.templates = re.findall(r"`([^`]*\$\{(?:signal|code)\}[^`]*)`", self.fx["source"])

    def render(self, **values):
        for t in self.templates:
            key = "signal" if "${signal}" in t else "code"
            if key in values:
                return t.replace("${" + key + "}", str(values[key]))
        raise AssertionError(f"no template for {values}")

    def test_the_fixture_has_both_templates(self):
        self.assertEqual(len(self.templates), 2, self.fx["source"])

    def test_each_rendered_exit_classifies_as_the_rule_says(self):
        for values, want in (({"code": 143}, "pipe"), ({"code": 137}, "pipe"),
                             ({"signal": "SIGKILL"}, "pipe"), ({"signal": "SIGTERM"}, "pipe"),
                             ({"code": 1}, "none"), ({"code": 1430}, "none")):
            with self.subTest(values):
                self.assertEqual(turn_owner(_row("failed", self.render(**values)), False), want)

    def test_the_text_seen_live_classifies_as_pipe(self):
        for name, text in self.fx["observed_row_texts"].items():
            with self.subTest(name):
                self.assertEqual(turn_owner(_row("failed", text), False), "pipe")

    def test_the_installed_dist_still_has_the_fixture_line(self):
        dist = _installed_dist(self)
        with _scripts_on_path():
            from extract_openclaw_exit_errors import extract
        got = extract(dist)
        self.assertIsNotNone(got, "the exit-error line is gone from the dist: re-check the owner rule")
        self.assertEqual(got["source"], self.fx["source"],
                         f"OpenClaw {got['openclaw_version']} changed the exit-error text "
                         f"(fixture: {self.fx['openclaw_version']}); re-check turn_owner, then "
                         "re-extract with scripts/extract_openclaw_exit_errors.py --write")


class CronPromptFixtureTest(unittest.TestCase):
    """ELI-103: a wake is found by its user turn's `[cron:<jobId> <name>]` prefix. The fixture
    holds the dist lines per OpenClaw line (2026.10 own line, ELI-164)."""

    FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                           "test_fixture_openclaw_cron_prompt.json")
    JOB = "1542eaee-50d8-4583-8d9b-d28bb12e0145"

    def setUp(self):
        self.fx = json.load(open(self.FIXTURE))
        self.forms = self.fx["forms"]

    def render(self, form, name, message, time_line="Current time: x"):
        tpl = lambda part: re.search(r"`([^`]*)`", form[part]["source"]).group(1)
        prefix = (tpl("prefix") if "prefix" in form else "").replace(
            "${input.job.id}", self.JOB).replace("${input.job.name}", name)
        base = (tpl("base").replace("${sourcePromptPrefix}", prefix)
                .replace("${input.job.id}", self.JOB).replace("${input.job.name}", name)
                .replace("${message}", message)).strip()
        self.assertNotIn("${", base, base)
        body = tpl("command_body")
        self.assertNotIn("${", body.replace("${base}", "").replace("${timeLine}", ""), body)
        return body.replace("${base}", base).replace("\\n", "\n").replace("${timeLine}", time_line)

    def turn(self, text):
        return {"role": "user", "content": text, "idempotencyKey": "turn-1:user", "timestamp": 5}

    def test_both_openclaw_lines_are_pinned(self):
        self.assertEqual(sorted(f["openclaw_version"].split(".")[1] for f in self.forms), ["10", "9"])

    def test_a_rendered_wake_prompt_is_a_wake_turn_of_its_job(self):
        for form in self.forms:
            for name in ("eli103-wake", "nightly digest (v2)"):
                with self.subTest(form["openclaw_version"], name=name):
                    got = wake_turns([self.turn(self.render(form, name, "Check the queue."))])
                    self.assertEqual([t["job"] for t in got], [self.JOB])

    def test_the_text_seen_live_is_a_wake_turn(self):
        for label, text in self.fx["observed_turn_texts"].items():
            with self.subTest(label):
                self.assertEqual([t["job"] for t in wake_turns([self.turn(text)])], [self.JOB])

    def test_the_installed_dist_still_has_the_fixture_lines(self):
        dist = _installed_dist(self)
        with _scripts_on_path():
            from extract_openclaw_cron_prompt import extract
        got = extract(dist)
        self.assertIsNotNone(got, "the cron prompt lines are gone from the dist: re-check wake_turns")
        parts = lambda f: {k: f[k]["source"] for k in ("prefix", "base", "command_body") if k in f}
        self.assertIn(parts(got), [parts(f) for f in self.forms],
                      f"OpenClaw {got['openclaw_version']} changed the cron prompt; re-check "
                      "_CRON_PROMPT_RE, then re-extract with scripts/extract_openclaw_cron_prompt.py --write")


class OomResumeLoopTest(unittest.TestCase):
    """A resume that is OOM-killed again must not loop: the budget is counted on M."""

    def test_four_consecutive_oom_kills_stop_after_the_budget(self):
        oom = _row("failed", "Claude Code process exited with code 137")
        resumes, decisions = 0, []
        for _ in range(MAX_TURN_RESUMES + 1):
            d = next_continuation(oom, False, resumes)
            decisions.append(d)
            if d == "resume":
                resumes += 1
        self.assertEqual(MAX_TURN_RESUMES, 3)
        self.assertEqual(decisions, ["resume", "resume", "resume", "finalize"])

    def test_no_resume_while_a_recovery_holds_the_session(self):
        """A row whose session has an active run never gets a resume; the decision is taken again
        on that run's settle. A resume sent during a recovery queued and failed."""
        oom = "Claude Code process exited with code 143"
        self.assertEqual(next_continuation({"status": "running", "hasActiveRun": True}, False, 0),
                         "gateway")
        for status in ("failed", "killed"):
            with self.subTest(status):
                row = {"status": status, "lastRunError": oom, "hasActiveRun": True}
                self.assertEqual(next_continuation(row, True, 0), "wait")
        settled = {"status": "failed", "lastRunError": oom, "hasActiveRun": False}
        self.assertEqual(next_continuation(settled, False, 0), "resume")

    def test_the_gateway_and_nobody_cases_never_spend_the_budget(self):
        self.assertEqual(next_continuation(_row("running"), False, 0), "gateway")
        self.assertEqual(next_continuation(_row("killed", aborted=True), False, 0), "finalize")
        self.assertEqual(next_continuation(_row("done"), False, 0), "finalize")


class HelloSubscribeSnapshotTests(unittest.IsolatedAsyncioTestCase):
    """ELI-100 DESIGN 2.1: restart recovery starts before the pipe subscribes. Every hello
    subscribes with list params, so its reply is the snapshot of runs already active."""

    USER = "00000000-0000-4000-8000-0000000000a1"
    CHAT = "0c5d682c-afaa-44f9-99a9-ada05599c997"

    def setUp(self):
        self.conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        self.calls = []
        self.key = _owui_session_key("main", self.USER, self.CHAT)
        self.key2 = _owui_session_key("main", self.USER, "24c342e4-50fa-442e-9b93-c215ed135f3a")
        self.key3 = _owui_session_key("main", self.USER, "11111111-2222-4333-8444-555566667777")
        test = self

        async def send_request(method, params, timeout=10):
            test.calls.append((method, params))
            if method == "sessions.subscribe":
                return {"subscribed": True, "list": {"sessions": [
                    {"key": test.key, "hasActiveRun": True,
                     "activeRunIds": ["f2445aa8-aeda-47d8-af7b-cf853aae34e6"]},
                    {"key": "agent:main:main", "hasActiveRun": True, "activeRunIds": ["hb-1"]},
                    # A claude-cli run: active, no ids.
                    {"key": test.key2, "hasActiveRun": True, "status": "running"},
                    {"key": test.key3, "hasActiveRun": False, "status": "done"},
                ]}}
            raise AssertionError(method)
        self.conn.send_request = send_request

    async def test_one_subscribe_request_carries_the_list(self):
        await self.conn._subscribe_all()
        subs = [p for m, p in self.calls if m == "sessions.subscribe"]
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0], {"activeOnly": True},
                         "list params make the Gateway list right after registering")
        self.assertNotIn("sessions.list", [m for m, _ in self.calls],
                         "a separate list would leave a gap after the subscribe")

    async def test_active_owui_runs_from_the_snapshot_are_kept(self):
        await self.conn._subscribe_all()
        self.assertEqual(self.conn.active_run_snapshot,
                         {self.key: ["f2445aa8-aeda-47d8-af7b-cf853aae34e6"], self.key2: []},
                         "active runs are known without their start push (ids when the Gateway "
                         "gives them); idle and non-OWUI sessions ignored")

    async def test_a_failed_subscribe_keeps_the_previous_snapshot(self):
        self.conn.active_run_snapshot = {"k": ["r"]}

        async def failing(method, params, timeout=10):
            raise GatewayError("UNAVAILABLE", "closing")
        self.conn.send_request = failing
        await self.conn._subscribe_all()
        self.assertEqual(self.conn.active_run_snapshot, {"k": ["r"]})

    async def test_a_failed_subscribe_still_runs_the_turn_resync(self):
        """ELI-369: a resync skipped while the socket came back is owed by this
        hello; it reads rows with sessions.list, so a failed subscribe must not drop it."""
        hellos = []

        class Turns:
            @staticmethod
            async def after_hello(conn):
                hellos.append(conn)

        async def failing(method, params, timeout=10):
            raise GatewayError("UNAVAILABLE", "closing")
        self.conn.send_request = failing
        with mock.patch.object(openclaw_pipe, "_TURNS", Turns):
            await self.conn._subscribe_all()
            for _ in range(3):
                await asyncio.sleep(0)
        self.assertEqual(hellos, [self.conn])


class RecoveryLinkTests(unittest.TestCase):
    """ELI-100 rule E and the active run behind a snapshot without ids."""

    CHAT = "0c5d682c-afaa-44f9-99a9-ada05599c997"
    M = "7371727a-1373-4108-9650-13b909f067b7"
    KEY = f"agent:main:openwebui-00000000-0000-4000-8000-0000000000a1-{CHAT}"
    SRC = f"msg-{CHAT}-{M}-1791851610.194554"
    REC = "f2445aa8-aeda-47d8-af7b-cf853aae34e6"

    @staticmethod
    def user(run, tool=None):
        m = {"role": "user", "idempotencyKey": f"{run}:user", "content": "x"}
        if tool:
            m["provenance"] = {"kind": "internal_system", "sourceTool": tool}
        return m

    @staticmethod
    def close(run):
        return {"role": "assistant", "content": "done",
                "__openclaw": {"idempotencyKey": f"cli-assistant:{run}"}}

    def history(self, closed=True):
        msgs = [self.user(self.SRC), self.user(self.REC, "main_session_restart_recovery"),
                {"role": "assistant", "content": [{"type": "toolcall"}]}]
        return msgs + ([self.close(self.REC)] if closed else [])

    def test_the_recovery_maps_to_the_interrupted_runs_message(self):
        self.assertEqual(recovery_source_run(self.history(), self.REC), self.SRC)
        self.assertEqual(run_message_id(self.KEY, self.CHAT, self.REC, self.history()), self.M)

    def test_a_recovery_after_a_pipe_resume_maps_to_the_same_message(self):
        resume = f"msg-{self.CHAT}-{self.M}-resume-1791851723.6"
        msgs = [self.user(self.SRC), self.user(resume), self.user(self.REC, "main_session_restart_recovery")]
        self.assertEqual(recovery_source_run(msgs, self.REC), resume)
        self.assertEqual(run_message_id(self.KEY, self.CHAT, self.REC, msgs), self.M)

    def test_a_recovery_of_an_agent_turn_maps_to_that_turns_message(self):
        msgs = [self.user("m3-agent-91c29446"),
                self.user("8f27e4c8-ed9a-4acf-9df1-992ee2f4e801", "main_session_restart_recovery")]
        self.assertEqual(run_message_id(self.KEY, self.CHAT, "8f27e4c8-ed9a-4acf-9df1-992ee2f4e801", msgs),
                         turn_message_id(self.KEY, "m3-agent-91c29446", self.CHAT))

    def test_an_ordinary_run_is_not_a_recovery(self):
        self.assertIsNone(recovery_source_run(self.history(), self.SRC))
        self.assertEqual(run_message_id(self.KEY, self.CHAT, self.SRC, self.history()), self.M)
        other = "04351f67-dbc3-4533-b2aa-2a0d88d79da0"
        self.assertEqual(run_message_id(self.KEY, self.CHAT, other, self.history()),
                         turn_message_id(self.KEY, other, self.CHAT))

    def test_the_transcript_cannot_name_the_active_run(self):
        """A queued follow-up has no transcript turn, so the transcript cannot name the active run;
        the writer opens on the run's first event."""
        r_active = [self.user(self.SRC), {"role": "assistant", "content": [{"type": "toolcall"}]}]
        r_failed_then_f_active = copy.deepcopy(r_active)  # F leaves no trace while it runs
        self.assertEqual(r_active, r_failed_then_f_active)
        self.assertFalse(hasattr(openclaw_pipe, "active_run_from_history"),
                         "no transcript inference of the active run")

    def test_a_queued_follow_up_never_takes_the_recoverys_message(self):
        """A follow-up with no admitted turn maps to its own id, never into M."""
        f = "04351f67-dbc3-4533-b2aa-2a0d88d79da0"
        self.assertIsNone(recovery_source_run(self.history(), f))
        self.assertNotEqual(run_message_id(self.KEY, self.CHAT, f, self.history()), self.M)


def _owui_output_text(output) -> str:
    """Open WebUI 0.12's `get_output_text` (utils/misc.py): message texts, one per line."""
    texts = []
    for item in output if isinstance(output, list) else []:
        if isinstance(item, dict) and item.get("type") == "message":
            text = "".join(str(p.get("text")) for p in item.get("content") or []
                           if isinstance(p, dict) and p.get("text") is not None)
            if text and not text.isspace():
                texts.append(text)
    return "\n".join(texts)


def _owui_fold_chunk(output: list, chunk) -> list:
    """A reply's output as Open WebUI folds its stream: Responses events through its handler, text
    onto the last message item. A simplification of middleware.py's text path."""
    from test_fixture_owui_streaming_handler import handle_responses_streaming_event
    if isinstance(chunk, dict) and str(chunk.get("type", "")).startswith("response."):
        return handle_responses_streaming_event(chunk, output)[0]
    if not isinstance(chunk, str) or not chunk:
        return output
    output = copy.deepcopy(output)
    if not output or output[-1].get("type") != "message":
        output.append({"type": "message", "id": f"msg-{len(output)}", "role": "assistant",
                       "content": [{"type": "output_text", "text": ""}]})
    output[-1]["content"][0]["text"] += chunk
    return output


class _FakeOwuiChat:
    """A one-chat Open WebUI for the turn writer: the chat row behind a real lock, chat_message
    rows, the socket server and the task registry."""

    def __init__(self, testcase, chat_id="c1"):
        self.chat_id = chat_id
        self.chat = types.SimpleNamespace(chat={"models": ["openclaw_gateway.m"], "history": {
            "currentId": "a1", "messages": {
                "q1": {"id": "q1", "role": "user", "parentId": None, "childrenIds": ["a1"]},
                "a1": {"id": "a1", "role": "assistant", "parentId": "q1", "childrenIds": [],
                       "model": "openclaw_gateway.m", "done": True},
            }}}, current_message_id="a1", updated_at=0)
        self.rows, self.emits, self.tasks = {}, [], {}
        # open_webui.tasks.tasks: the request tasks, by the request's task_id
        self.request_tasks = {}
        lock = asyncio.Lock()
        fake = self
        # ELI-167: which row transactions (1-based) hit the lock; `locked_from` and after.
        self.transactions, self.locked, self.locked_from = 0, set(), None

        class Chats:
            @staticmethod
            @contextlib.asynccontextmanager
            async def _chat_transaction(chat_id):
                async with lock:
                    await asyncio.sleep(0)
                    fake.transactions += 1
                    n = fake.transactions
                    if n in fake.locked or (fake.locked_from is not None and n >= fake.locked_from):
                        # What BEGIN IMMEDIATE raises once Open WebUI's 5 s busy_timeout runs out.
                        raise sqlite3.OperationalError("database is locked")
                    yield ("db", fake.chat)

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                fake.upserts.append(message_id)
                if not message.get("content"):        # as Open WebUI does (models/chats.py)
                    text = _owui_output_text(message.get("output"))
                    if text:
                        message = {**message, "content": text}
                msgs = fake.chat.chat["history"]["messages"]
                msgs[message_id] = {**msgs.get(message_id, {}), **message}
                return fake.chat

            @staticmethod
            async def get_message_by_id_and_message_id(chat_id, message_id):
                return copy.deepcopy(fake.chat.chat["history"]["messages"].get(message_id))
        self.upserts = []
        self._chats = Chats

        class ChatMessages:
            @staticmethod
            async def upsert_message(mid, chat_id, user_id, m, db=None):
                fake.rows[mid] = copy.deepcopy(m)

        # Each sid's chats (SESSION_POOL[sid]["chat_ids"]) and its intent hook's answer.
        self.session_pool, self.page_intent, self.calls = {}, {}, []

        class Sio:
            async def emit(self, event, data, room=None, to=None):
                fake.emits.append((event, copy.deepcopy(data), room or to))

            async def call(self, event, data, to=None, timeout=None):
                fake.calls.append((event, copy.deepcopy(data), to))
                return fake.page_intent.get(to)

        async def get_event_emitter(meta, update_db=True):
            fake.emitter_update_db = update_db
            async def emit(event):
                fake.emits.append(("events", {"chat_id": meta["chat_id"], "message_id": meta["message_id"],
                                              "data": copy.deepcopy(event)}, f"user:{meta['user_id']}"))
            return emit

        self.streams = []

        async def save_response_stream(redis, task_id, chat_id, message_id, content, output):
            fake.streams.append((task_id, message_id, content, copy.deepcopy(output)))

        # Each mid-turn copy dropped: (task_id, events so far, saved messages).
        self.cleared = []

        async def clear_response_stream(redis, task_id):
            fake.cleared.append((task_id, len(fake.emits), copy.deepcopy(fake.msgs())))

        async def create_task(redis, coroutine, id=None, task_id=None):
            task_id = task_id or f"task-{len(fake.tasks) + 1}"
            fake.tasks[task_id] = (id, asyncio.ensure_future(coroutine))
            return task_id, fake.tasks[task_id][1]

        def mod(name, **attrs):
            m = types.ModuleType(name)
            for k, v in attrs.items():
                setattr(m, k, v)
            return m
        self.socket_main = mod("open_webui.socket.main", sio=Sio(), get_event_emitter=get_event_emitter,
                               SESSION_POOL=self.session_pool)
        mods = {"open_webui": mod("open_webui"), "open_webui.models": mod("open_webui.models"),
                "open_webui.models.chats": mod("open_webui.models.chats", Chats=Chats),
                "open_webui.models.chat_messages": mod("open_webui.models.chat_messages",
                                                       ChatMessages=ChatMessages),
                "open_webui.socket": mod("open_webui.socket"),
                "open_webui.socket.main": self.socket_main,
                "open_webui.tasks": mod("open_webui.tasks", create_task=create_task,
                                        save_response_stream=save_response_stream,
                                        clear_response_stream=clear_response_stream,
                                        tasks=self.request_tasks),
                "open_webui.main": mod("open_webui.main", app=types.SimpleNamespace(
                    state=types.SimpleNamespace(redis=None))),
                "sqlalchemy": mod("sqlalchemy"), "sqlalchemy.orm": mod("sqlalchemy.orm"),
                "sqlalchemy.orm.attributes": mod("sqlalchemy.orm.attributes",
                                                 flag_modified=lambda obj, name: None)}
        p = mock.patch.dict(sys.modules, mods)
        p.start()
        testcase.addCleanup(p.stop)

    def msgs(self):
        return self.chat.chat["history"]["messages"]

    async def end_request(self, message_id, output):
        """What Open WebUI 0.12 saves when a response stream ends or is cancelled: `done` and its
        folded output (utils/middleware.py:6599, :6667)."""
        await self._chats.upsert_message_to_chat_by_id_and_message_id(
            self.chat_id, message_id, {"done": True, "output": copy.deepcopy(output)})

    def events(self, kind):
        return [d["data"] for e, d, room in self.emits if e == "events" and d["data"].get("type") == kind]

    async def cancel_tasks(self):
        for _, t in self.tasks.values():
            t.cancel()
        await asyncio.gather(*(t for _, t in self.tasks.values()), return_exceptions=True)


class _TurnWriterCase(unittest.IsolatedAsyncioTestCase):
    USER, CHAT = "u1", "c1"
    KEY = "agent:main:openwebui-u1-c1"

    async def asyncSetUp(self):
        self.owui = _FakeOwuiChat(self, self.CHAT)
        self.conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        self.addAsyncCleanup(self.owui.cancel_tasks)

    def writer(self, run="agent-run-1", message_id="m1"):
        return _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT,
                           user_id=self.USER, run_id=run, message_id=message_id)


class TurnWriterOpenTests(_TurnWriterCase):
    """ELI-100 DESIGN 2.3: open a run's message: claim it, announce it (`chat:messages`, M1),
    register it so the page shows it generating and Stop reaches it (M4, M5)."""

    async def test_open_claims_introduces_and_registers_a_task(self):
        w = self.writer()
        self.assertTrue(await w.open())
        m = self.owui.msgs()["m1"]
        self.assertEqual((m["parentId"], m["done"]), ("a1", False))
        intro = [d for e, d, room in self.owui.emits
                 if e == "events" and d["data"]["type"] == "chat:messages"]
        self.assertEqual(len(intro), 1)
        self.assertEqual(intro[0]["data"]["data"]["currentId"], "m1")
        self.assertTrue(intro[0]["shared"])
        self.assertEqual([r for e, d, r in self.owui.emits if d["data"]["type"] == "chat:messages"],
                         [f"chat:{self.CHAT}"], "to the chat room, where every open tab is")
        self.assertEqual(set(intro[0]["data"]["data"]["messages"]), {"m1", "a1"})
        (task_id, (item, _)), = self.owui.tasks.items()
        self.assertEqual(item, self.CHAT, "listed for the chat: the page shows it as generating")
        self.assertEqual(m["meta"]["task_id"], task_id, "Open WebUI finds the task by meta.task_id")
        self.assertEqual(w.task_id, task_id)

    async def test_a_message_another_writer_holds_is_not_opened_again(self):
        _register_turn_writer("other")
        self.addCleanup(_retire_turn_writer, "other")
        await _claim_turn_message(self.CHAT, self.USER, "m1", "agent-run-1", "other")
        emits_before = len(self.owui.emits)
        w = self.writer()
        self.assertFalse(await w.open())
        self.assertEqual(len(self.owui.emits), emits_before, "nothing introduced")
        self.assertEqual(self.owui.tasks, {}, "no second task")

    async def test_the_status_actions_runs_never_open(self):
        w = self.writer(run=f"{ACTION_RUN_PREFIX}compact-c1-1.0", message_id=None)
        self.assertFalse(await w.open())
        self.assertEqual(set(self.owui.msgs()), {"q1", "a1"})


class TurnWriterChatFinishedTests(_TurnWriterCase):
    """PUBLISH_CHAT_FINISHED: the writer publishes `chat.finished` (source `openclaw-pipe`) once
    M's final save reads back whole."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.valves = types.SimpleNamespace(AGENT_ID="main", PUBLISH_CHAT_FINISHED=True)
        self.conn = _GatewayConnection(lambda: self.valves)
        self.conn.session_history = mock.AsyncMock(return_value={"messages": []})
        self.published = []

        async def publish(chat_id, message_id, user_id, text=None):
            self.published.append((chat_id, message_id, user_id, text))
        p = mock.patch.object(openclaw_pipe, "_publish_chat_finished", publish)
        p.start()
        self.addCleanup(p.stop)

    async def test_the_final_save_publishes_once(self):
        w = self.writer()
        await w.feed(_delta("Hello"))
        await w.finalize()
        await w.finalize()
        self.assertEqual(self.published, [(self.CHAT, "m1", self.USER, "Hello")])

    async def test_a_failed_final_save_publishes_nothing(self):
        w = self.writer()
        await w.feed(_delta("Hello"))
        w._persist_final = mock.AsyncMock(return_value=([], [], False))
        await w.finalize()
        self.assertEqual(self.published, [])

    async def test_a_stopped_reply_publishes_nothing(self):
        w = self.writer()
        await w.feed(_delta("Hello"))
        await w.finalize(stopped=True)
        self.assertEqual(self.published, [])

    async def test_nothing_when_the_valve_is_off(self):
        self.valves.PUBLISH_CHAT_FINISHED = False
        w = self.writer()
        await w.feed(_delta("Hello"))
        await w.finalize()
        self.assertEqual(self.published, [])


class PublishChatFinishedTests(unittest.IsolatedAsyncioTestCase):
    """`_publish_chat_finished`: the event shaped as Open WebUI's `publish_chat_finished_event`,
    through `publish_event`, for a saved chat's finished message only."""

    async def asyncSetUp(self):
        self.app = types.SimpleNamespace(state=types.SimpleNamespace())
        self.calls = []
        self.message = {"id": "m1", "model": "openclaw_gateway.main", "done": True, "content": "saved text"}
        test = self

        async def publish_event(request, event, **kw):
            test.calls.append((request, event, kw))

        self.chat = types.SimpleNamespace(chat={"history": {"messages": {"m1": self.message}}})

        class Chats:
            @staticmethod
            @contextlib.asynccontextmanager
            async def _chat_transaction(chat_id):
                yield ("db", test.chat)

            @staticmethod
            async def get_chat_title_by_id(chat_id):
                return "Trip plan"

        class Users:
            @staticmethod
            async def get_user_by_id(user_id):
                return {"id": user_id, "name": "U", "role": "user"}

        class Config:
            @staticmethod
            async def get(key):
                return {"webui.url": "https://chat.example/"}.get(key)

        class Request:
            def __init__(self, scope):
                self.scope, self.app = scope, scope["app"]

        def mod(name, **attrs):
            m = types.ModuleType(name)
            m.__dict__.update(attrs)
            return m
        p = mock.patch.dict(sys.modules, {
            "open_webui": mod("open_webui"), "open_webui.main": mod("open_webui.main", app=self.app),
            "open_webui.events": mod("open_webui.events", publish_event=publish_event,
                                     EVENTS=types.SimpleNamespace(CHAT_FINISHED="chat.finished")),
            "open_webui.models": mod("open_webui.models"),
            "open_webui.models.chats": mod("open_webui.models.chats", Chats=Chats),
            "open_webui.models.users": mod("open_webui.models.users", Users=Users),
            "open_webui.models.config": mod("open_webui.models.config", Config=Config),
            "open_webui.utils": mod("open_webui.utils"),
            "open_webui.utils.chat_id": mod("open_webui.utils.chat_id",
                                            is_saved_chat_id=lambda c: bool(c) and ":" not in c),
            "starlette": mod("starlette"), "starlette.requests": mod("starlette.requests", Request=Request),
            "sqlalchemy": mod("sqlalchemy"), "sqlalchemy.orm": mod("sqlalchemy.orm"),
            "sqlalchemy.orm.attributes": mod("sqlalchemy.orm.attributes", flag_modified=lambda o, n: None)})
        p.start()
        self.addCleanup(p.stop)

    async def test_a_reply_is_published_once(self):
        """Open WebUI can call the Pipe twice for one reply; the first publish claims it in the
        message meta under the row lock (#273)."""
        await openclaw_pipe._publish_chat_finished("c1", "m1", "u1", "the reply")
        await openclaw_pipe._publish_chat_finished("c1", "m1", "u1", "the reply")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.message["meta"][openclaw_pipe.CHAT_FINISHED_META])

    async def test_the_event_as_the_middleware_shapes_it(self):
        await openclaw_pipe._publish_chat_finished("c1", "m1", "u1", "the reply")
        (request, event, kw), = self.calls
        self.assertIs(request.app, self.app, "the sinks and event functions read request.app")
        self.assertEqual(event, "chat.finished")
        self.assertEqual(kw["source"], "openclaw-pipe")
        self.assertEqual((kw["subject_id"], kw["subject_type"], kw["message"]), ("c1", "chat", "Trip plan"))
        self.assertEqual(kw["actor"]["id"], "u1")
        self.assertEqual(kw["data"], {"user_id": "u1", "chat_id": "c1", "message_id": "m1",
                                      "model_id": "openclaw_gateway.main", "title": "Trip plan",
                                      "url": "https://chat.example/c/c1", "message": "the reply"})

    async def test_without_text_the_saved_message_is_sent(self):
        await openclaw_pipe._publish_chat_finished("c1", "m1", "u1")
        self.assertEqual(self.calls[0][2]["data"]["message"], "saved text")

    async def test_an_unfinished_message_is_not_published(self):
        self.message["done"] = False
        await openclaw_pipe._publish_chat_finished("c1", "m1", "u1")
        self.assertEqual(self.calls, [])

    async def test_a_temporary_chat_is_not_published(self):
        await openclaw_pipe._publish_chat_finished("local:c1", "m1", "u1")
        self.assertEqual(self.calls, [])

    async def test_a_failing_publish_is_logged_not_raised(self):
        async def boom(*a, **k):
            raise RuntimeError("sink down")
        sys.modules["open_webui.events"].publish_event = boom
        await openclaw_pipe._publish_chat_finished("c1", "m1", "u1", "x")


def _delta(text):
    return {"stream": "assistant", "data": {"delta": text}}


def _tool(phase, tcid="t1", name="Bash", result="ok"):
    data = {"phase": phase, "name": name, "toolCallId": tcid, "args": {"command": "ls"}}
    if phase == "result":
        data["result"] = result
    return {"stream": "tool", "data": data}


class TurnWriterLoadAcrossTheSaveTests(_TurnWriterCase):
    """ELI-340: a tab loading across M's final save can show M cut. The end saves M whole, drops
    the copy, then sends M finished to the chat's room."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.conn.session_history = mock.AsyncMock(return_value={"messages": []})

    async def test_the_copy_is_dropped_after_the_whole_save_then_m_is_sent_finished(self):
        shown = _TurnRenderer()
        shown.feed(_delta("slow-1 slow-2"))
        await self.owui.end_request("m1", copy.deepcopy(shown.output))
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id="agent-run-1", message_id="m1", takeover=True, renderer=shown)
        await w.feed(None)
        await w.feed(_delta(" slow-3"))
        self.assertEqual(self.owui.msgs()["m1"]["content"], "slow-1 slow-2", "the handover's write: M cut")
        await w.finalize()
        (cleared,) = [c for c in self.owui.cleared if c[0] == w.task_id]
        _, at, saved = cleared
        self.assertEqual((saved["m1"]["content"], saved["m1"]["done"]), ("slow-1 slow-2 slow-3", True),
                         "the copy goes only once the row holds the whole reply")
        events = [d for e, d, room in self.owui.emits if e == "events"]
        rooms = [room for e, d, room in self.owui.emits if e == "events"]
        kinds = [d["data"]["type"] for d in events]
        pushes = [i for i, d in enumerate(events) if d["data"]["type"] == "chat:messages"]
        self.assertTrue(pushes, "M is sent finished")
        push = pushes[-1]
        self.assertGreaterEqual(push, at, "after the copy is dropped")
        self.assertLess(push, kinds.index("chat:completion"))
        sent = events[push]
        self.assertTrue(sent["shared"])
        m = sent["data"]["data"]["messages"]["m1"]
        self.assertEqual((m["content"], m["done"]), ("slow-1 slow-2 slow-3", True))
        self.assertEqual(sent["message_id"], "m1")
        self.assertEqual(rooms[push], f"chat:{self.CHAT}", "to the chat room, where a loading tab already is")

    async def test_an_inserted_message_is_sent_with_its_parent_and_view(self):
        w = self.writer()
        await w.feed(_delta("Hello"))
        await w.finalize()
        self.assertTrue(any(c[0] == w.task_id for c in self.owui.cleared))
        final = self.owui.events("chat:messages")[-1]
        self.assertEqual(final["data"]["currentId"], "m1")
        self.assertEqual(final["data"]["messages"]["a1"], {"id": "a1", "childrenIds": ["m1"]})

    async def failed_final_save(self, fail):
        """If Open WebUI's final save fails, the copy is the only whole reply: it stays and M is
        not sent finished over it."""
        shown = _TurnRenderer()
        shown.feed(_delta("slow-1 slow-2"))
        await self.owui.end_request("m1", copy.deepcopy(shown.output))
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id="agent-run-1", message_id="m1", takeover=True, renderer=shown)
        await w.feed(None)
        await w.feed(_delta(" slow-3"))
        pushed = len(self.owui.events("chat:messages"))
        chats = self.owui._chats
        real = chats.upsert_message_to_chat_by_id_and_message_id

        async def upsert(chat_id, message_id, message):
            if "statusHistory" in message:       # the final save
                return await fail()
            return await real(chat_id, message_id, message)
        with mock.patch.object(chats, "upsert_message_to_chat_by_id_and_message_id", upsert):
            await w.finalize()
        self.assertEqual(self.owui.msgs()["m1"]["content"], "slow-1 slow-2", "the final save failed")
        self.assertEqual([c for c in self.owui.cleared if c[0] == w.task_id], [], "the copy stays")
        self.assertEqual(len(self.owui.events("chat:messages")), pushed, "M is not sent finished")

    async def test_a_final_save_that_raises_keeps_the_copy(self):
        async def fail():
            raise sqlite3.OperationalError("database is locked")
        await self.failed_final_save(fail)

    async def test_a_final_save_that_returns_none_keeps_the_copy(self):
        async def fail():
            return None          # Open WebUI's upsert when its write failed (ELI-90)
        await self.failed_final_save(fail)

    async def test_m_is_never_shown_finished_without_its_final_status_lines(self):
        """ELI-371: M is never shown finished without its final status lines (the context meter), and
        each shows once. Sent after the completion, a tab showed M done without the meter for a moment
        (the E2E 'the chat changed on a reload' flake). A tab appends each status event to M's history
        and takes the push's history over its own (Open WebUI 0.13.0 Chat.svelte:1582-1587, 1389-1390),
        so a line both pushed and sent as an event shows twice."""
        meter = "🧠 10/200k (0%)"
        shown = _TurnRenderer()
        shown.feed(_delta("slow-1"))
        await self.owui.end_request("m1", copy.deepcopy(shown.output))
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id="agent-run-1", message_id="m1", takeover=True, renderer=shown)
        await w.feed(None)
        tab = copy.deepcopy(self.owui.msgs()["m1"])
        with mock.patch.object(openclaw_pipe, "_build_usage_status_lines", mock.AsyncMock(return_value=[meter])):
            await w.finalize()
        meters = lambda history: [h.get("description") for h in history or []].count(meter)
        self.assertEqual(meters(self.owui.msgs()["m1"].get("statusHistory")), 1, "the stored M")
        for d in (d["data"] for e, d, room in self.owui.emits if e == "events"):
            if d["type"] == "status":
                tab["statusHistory"] = [*(tab.get("statusHistory") or []), d["data"]]
            elif d["type"] == "chat:messages" and "m1" in d["data"]["messages"]:
                tab = {**tab, **d["data"]["messages"]["m1"]}
            elif d["type"] == "chat:completion":
                self.assertEqual(meters(tab.get("statusHistory")), 1, "the tab when M shows done")
        self.assertEqual(meters(tab.get("statusHistory")), 1, "the tab at the end")


class MediaDirectiveKindTests(unittest.TestCase):
    """ELI-328: which `MEDIA:` directives the file server can show."""

    def test_a_url_directive_is_its_own_image(self):
        out, handled = openclaw_pipe._resolve_media("See:\nMEDIA:https://example.com/a/cat.png\n",
                                                    base_url="http://fs:18791")
        self.assertTrue(handled)
        self.assertIn("![cat.png](https://example.com/a/cat.png)", out)
        self.assertNotIn("fs:18791", out)

    def test_a_path_directive_is_taken_out_and_named(self):
        text, paths = openclaw_pipe._take_gateway_media("Here.\nMEDIA:/tmp/a.png\nMEDIA:b.png\nBye.")
        self.assertEqual(paths, ["/tmp/a.png"])
        self.assertEqual(text, "Here.\nMEDIA:b.png\nBye.")

    def test_a_path_in_code_is_text(self):
        text = "Use `MEDIA:/tmp/a.png` like this."
        self.assertEqual(openclaw_pipe._take_gateway_media(text), (text, []))

    def test_unstored_paths(self):
        f = openclaw_pipe._unstored_gateway_media
        self.assertEqual(f(["/a/x.png"], 1, []), [])
        self.assertEqual(f(["/a/x.png"], 0, []), ["x.png"])
        self.assertEqual(f(["/a/x.png", "/a/y.png"], 1, ["y.png"]), ["y.png"])


class TurnWriterImageTests(_TurnWriterCase):
    """ELI-328: a turn the writer shows shows its images as the inline reply does, from the stored
    Gateway image blocks."""

    PATH = "/config/.openclaw/media/tool-image-generation/x.png"
    BLOCK = {"type": "image", "url": "/api/chat/media/outgoing/s/1/full", "alt": "x.png",
             "mimeType": "image/png"}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.reads = []
        # The read after which the block is stored, or None (stored by the second read).
        self.attach_after = None
        self.attached = False

        def attach():
            self.attached = True
            self.conn._on_sessions_changed({"sessionKey": self.KEY, "phase": "message", "ts": 1})

        async def history(session_key, limit=None, **kw):
            self.reads.append(1)
            if self.attach_after is None:
                blocks = [self.BLOCK] if len(self.reads) > 1 else []
            else:
                blocks = [self.BLOCK] if self.attached else []
                if len(self.reads) == self.attach_after:
                    asyncio.get_running_loop().call_soon(attach)
            return {"messages": [{"role": "assistant", "__openclaw": {"runId": "agent-run-1"},
                                  "content": [{"type": "text", "text": "Done."}, *blocks]}]}
        self.conn.session_history = history
        self.shown = mock.AsyncMock(return_value="\n\n![x.png](/api/v1/files/f1/content)\n")
        for name, value in (("_gateway_images_text", self.shown),):
            patch = mock.patch.object(openclaw_pipe, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def _final_message(self):
        return self.owui.msgs()["m1"]

    async def test_a_path_in_media_urls_shows_the_stored_block(self):
        w = self.writer()
        await w.feed({"stream": "assistant", "data": {"delta": "Done.", "mediaUrls": [self.PATH]}})
        await w.finalize()
        self.assertEqual(len(self.reads), 2, "read again until the block is stored")
        (blocks,), kw = self.shown.call_args
        self.assertEqual((blocks, kw["missing"]), ([self.BLOCK], []))
        m = self._final_message()
        self.assertTrue(m["done"])
        self.assertEqual(m["content"], "Done.\n\n![x.png](/api/v1/files/f1/content)")

    async def test_a_block_stored_late_is_read_on_the_gateways_push(self):
        """A late block is read on the `sessions.changed` phase `message` push."""
        self.attach_after = 2
        w = self.writer()
        await w.feed({"stream": "assistant", "data": {"delta": "Done.", "mediaUrls": [self.PATH]}})
        with mock.patch.object(openclaw_pipe, "_RUN_IMAGES_MAX_WAIT_S", 5.0, create=True):
            await asyncio.wait_for(w.finalize(), 2)
        self.assertEqual(len(self.reads), 3)
        (blocks,), kw = self.shown.call_args
        self.assertEqual((blocks, kw["missing"]), ([self.BLOCK], []))

    async def test_a_block_never_stored_is_not_polled_for(self):
        self.attach_after = 10 ** 6
        w = self.writer()
        await w.feed({"stream": "assistant", "data": {"delta": "Done.", "mediaUrls": [self.PATH]}})
        with mock.patch.object(openclaw_pipe, "_RUN_IMAGES_MAX_WAIT_S", 0.2, create=True):
            await w.finalize()
        self.assertEqual(len(self.reads), 2, "no push, no read")
        self.assertTrue(self._final_message()["done"])

    async def test_the_wait_ends_at_its_bound_counting_every_read(self):
        """The 30 s bound counts from the start and each read gets only what is left, so a hanging
        read ends the wait with what was read (#274)."""
        conn = self._bare_conn()
        reads = []

        async def read(fresh):
            reads.append(fresh)
            if fresh:
                await asyncio.sleep(3600)   # a read the Gateway never answers
            await asyncio.sleep(0.15)       # the first read takes part of the bound
            return "cached"
        loop = asyncio.get_running_loop()
        start = loop.time()
        got = await asyncio.wait_for(openclaw_pipe._GatewayConnection.read_until_stored(
            conn, "k", read, lambda _got: False, 0.3), 5)
        self.assertEqual(got, ("cached", False))
        self.assertEqual(reads, [False, True])
        self.assertLess(loop.time() - start, 0.45, "the first read's time counts against the bound")

    @staticmethod
    def _bare_conn(subscribed=None):
        async def subscribe(key):
            if subscribed is not None:
                subscribed.append(key)
        return types.SimpleNamespace(session_message_event=lambda _key: asyncio.Event(),
                                     ensure_message_subscription=subscribe)

    async def test_a_first_read_that_hangs_ends_at_the_bound_as_the_bound(self):
        """A first read held past the bound ends the wait like any read (#274)."""
        async def read(fresh):
            await asyncio.sleep(3600)
        got = await asyncio.wait_for(openclaw_pipe._GatewayConnection.read_until_stored(
            self._bare_conn(), "k", read, lambda _got: False, 0.1, empty=([], [])), 5)
        self.assertEqual(got, (([], []), False))

    async def test_a_reads_own_timeout_is_a_failed_read_not_the_bound(self):
        """`session_history`'s own 5 s timeout is a failed read, not the bound (#274)."""
        async def read(fresh):
            if fresh:
                raise asyncio.TimeoutError("chat.history")
            return "cached"
        with self.assertRaises(asyncio.TimeoutError):
            await openclaw_pipe._GatewayConnection.read_until_stored(
                self._bare_conn(), "k", read, lambda _got: False, 5)

    async def test_a_dropped_subscription_is_taken_again_before_the_wait(self):
        """A dropped `sessions.messages.subscribe` is taken again before the wait, or no push would
        wake it (#274)."""
        subscribed = []

        async def read(fresh):
            return "none yet"
        await openclaw_pipe._GatewayConnection.read_until_stored(
            self._bare_conn(subscribed), "k", read, lambda _got: False, 0.1)
        self.assertEqual(subscribed[:1], ["k"])

    async def test_a_path_directive_is_taken_out_of_the_text(self):
        w = self.writer()
        await w.feed(_delta(f"Done.\nMEDIA:{self.PATH}\n"))
        await w.finalize()
        self.assertNotIn("MEDIA:", self._final_message()["content"])
        self.assertIn("![x.png]", self._final_message()["content"])

    async def test_a_reply_that_is_only_an_image_is_still_written(self):
        w = self.writer()
        await w.feed({"stream": "assistant", "data": {"delta": "", "mediaUrls": [self.PATH]}})
        self.assertFalse(w.opened, "nothing showable streamed")
        await w.finalize()
        self.assertIn("![x.png]", self._final_message()["content"])

    async def test_a_run_that_named_no_file_reads_the_transcript_once(self):
        w = self.writer()
        await w.feed(_delta("Done."))
        await w.finalize()
        self.assertEqual(len(self.reads), 1)
        self.shown.assert_not_called()


class TurnWriterLockedDbTests(_TurnWriterCase):
    """ELI-167: a sqlite write lock held past busy_timeout at open: the open waits and never loses
    the run; a lasting lock fails visibly."""

    RUN = "c0ffee00-0000-4000-8000-000000000001"  # a wake's run id is its sessionId

    def wake_writer(self):
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id=self.RUN, message_id="m1",
                        wake={"key": "wake-1", "at": None, "job": "job-1"})
        pump = openclaw_pipe._RunPump()
        pump.wake, pump.writer = True, w
        self.conn.session_history = mock.AsyncMock(return_value={"messages": []})
        return w, pump

    async def step(self, pump, item):
        return await openclaw_pipe._wake_step(self.conn, self.KEY, self.RUN, self.USER, self.CHAT,
                                              pump, "event", item)

    async def test_a_wake_whose_open_finds_the_db_locked_opens_on_its_next_event(self):
        w, pump = self.wake_writer()
        self.owui.locked = {1}                      # the claim
        await self.step(pump, _tool("start"))       # on main: raises into the pump's catch-all
        self.assertFalse(w.opened)
        self.assertFalse(w.closed, "still this run's writer: nothing is dropped")
        self.assertNotIn("m1", self.owui.msgs())
        await self.step(pump, _tool("result"))
        self.assertTrue(w.opened, "the next event opens it")
        await w.finalize()
        m = self.owui.msgs()["m1"]
        self.assertIs(m["done"], True)
        self.assertEqual([i["type"] for i in m["output"]][:2], ["function_call", "function_call_output"],
                         "the card from the event that met the lock is there")

    async def test_a_lock_after_the_claim_resumes_the_open_where_it_stopped(self):
        """The claim committed, then the task meta met the lock: the next open must not read its
        own claim as another writer's."""
        w, pump = self.wake_writer()
        self.owui.locked = {2}                      # claim ok, _set_turn_meta locked
        await self.step(pump, _tool("start"))
        self.assertFalse(w.opened)
        await self.step(pump, _tool("result"))
        self.assertTrue(w.opened)
        self.assertFalse(w.closed)
        self.assertEqual(len(self.owui.tasks), 1, "one task registered, not two")
        self.assertEqual(len(self.owui.events("chat:messages")), 1, "introduced once")
        self.assertEqual(self.owui.msgs()["m1"]["meta"]["task_id"], w.task_id)

    async def test_a_wake_that_ends_while_the_db_is_locked_is_written_at_its_end(self):
        w, pump = self.wake_writer()
        self.owui.locked = {1}
        await self.step(pump, _tool("start"))       # its only event before the end
        self.owui.locked = {2, 3}                   # still held for two more busy waits
        await w.finalize()
        self.assertTrue(w.opened)
        self.assertIs(self.owui.msgs()["m1"]["done"], True)

    async def test_a_db_that_stays_locked_fails_visibly(self):
        w, pump = self.wake_writer()
        self.owui.locked_from = 1
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await self.step(pump, _tool("start"))
            await w.finalize()
        self.assertNotIn("m1", self.owui.msgs())
        self.assertTrue(w._done.is_set(), "the writer ends; nothing waits on it forever")
        log = out.getvalue()
        self.assertIn("NOT WRITTEN", log)
        self.assertIn("database is locked", log)
        notes = self.owui.events("notification")
        self.assertEqual(len(notes), 1, "the user's open tabs are told, without the DB")
        self.assertEqual(notes[0]["data"]["type"], "error")


class TurnWriterStreamTests(_TurnWriterCase):
    """ELI-100 DESIGN 2.3 (when, condition B) and 2.4 (streaming)."""

    async def test_text_opens_only_once_it_is_showable(self):
        w = self.writer()
        for chunk in ("NO_", "RE"):  # still a prefix of the NO_REPLY sentinel
            await w.feed(_delta(chunk))
        self.assertFalse(w.opened)
        await w.feed(_delta("ST of the answer"))
        self.assertTrue(w.opened)
        self.assertEqual(self.owui.msgs()["m1"]["done"], False)

    async def test_a_sentinel_only_run_never_opens(self):
        w = self.writer()
        await w.feed(_delta("NO_REPLY"))
        self.assertFalse(w.opened)
        self.assertEqual(set(self.owui.msgs()), {"q1", "a1"})

    async def test_a_tool_start_opens_before_any_text(self):
        """Condition B: a tool-heavy agent turn shows its cards from the first call."""
        w = self.writer()
        await w.feed(_tool("start"))
        self.assertTrue(w.opened)
        added = [e["data"] for e in self.owui.events("response:completion")]
        self.assertEqual([a["type"] for a in added], ["response.output_item.added"])
        self.assertEqual(added[0]["item"]["type"], "function_call")

    async def test_events_stream_to_the_users_room_without_db_side_effects(self):
        w = self.writer()
        await w.feed(_delta("Hello"))
        await w.feed(_delta(" world"))
        kinds = [e["data"]["type"] for e in self.owui.events("response:completion")]
        self.assertEqual(kinds, ["response.output_item.added", "response.output_text.delta",
                                 "response.output_text.delta"])
        self.assertIs(self.owui.emitter_update_db, False, "the writer owns the DB writes")

    async def test_the_db_gets_no_write_mid_turn_and_the_stream_every_event(self):
        """ELI-115: a DB write rewrites the whole chat, so mid-turn the writer only feeds the
        response stream (`overlay_response_streams`); the DB gets the claim and the end."""
        w = self.writer()
        await w.feed(_delta("Reading the logs"))  # opens: the claim's own write
        writes = len(self.owui.upserts)
        for n in range(1, 16):
            await w.feed(_tool("start", tcid=f"t{n}"))
            await w.feed(_tool("result", tcid=f"t{n}"))
        await w.feed(_delta("Found it."))
        self.assertEqual(len(self.owui.upserts), writes, "no write per item")
        self.assertEqual(self.owui.msgs()["m1"].get("output"), [])
        self.assertEqual(len(self.owui.streams), 2 * 15 + 2, "the response stream gets every event")
        self.assertEqual(self.owui.streams[-1][0], w.task_id)
        self.assertTrue(self.owui.streams[-1][2].endswith("Found it."))

    async def test_the_response_stream_is_saved_with_open_webui_0123s_signature(self):
        """ELI-166: 0.12.3 dropped `content` from `save_response_stream`; the 0.12.0 call raised
        TypeError and killed the pump."""
        saved = []

        async def save_response_stream(redis, task_id, chat_id, message_id, output):
            saved.append((task_id, message_id, copy.deepcopy(output)))
            return 1
        tasks = sys.modules["open_webui.tasks"]
        with mock.patch.object(tasks, "save_response_stream", save_response_stream):
            w = self.writer()
            await w.feed(_delta("Reading the logs"))
            await w.feed(_tool("start", tcid="t1"))
        self.assertFalse(w.closed)
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[-1][0], w.task_id)
        self.assertEqual([i["type"] for i in saved[-1][2]], ["message", "function_call"])

    async def test_a_handed_over_reply_is_written_once_at_the_handover(self):
        """The handover's write marks M as still being written (what the orphan reconcile adopts,
        ELI-90). Request already ended: one write at once, none per item."""
        shown = _TurnRenderer()
        shown.feed(_delta("Reading them."))
        await self.owui.end_request("m1", copy.deepcopy(shown.output))
        self.assertIs(self.owui.msgs()["m1"]["done"], True)
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id="agent-run-1", message_id="m1", takeover=True, renderer=shown)
        writes = len(self.owui.upserts)
        await w.feed(None)
        self.assertEqual(len(self.owui.upserts), writes + 1)
        self.assertEqual((self.owui.msgs()["m1"]["done"], self.owui.msgs()["m1"]["content"]),
                         (False, "Reading them."))
        await w.feed(_tool("start"))
        await w.feed(_tool("result"))
        self.assertEqual(len(self.owui.upserts), writes + 1, "then no write per item")


class TurnWriterEndTests(_TurnWriterCase):
    """ELI-100 DESIGN 2.5 (end on the settled row, from the transcript), 2.7 (resume / gateway /
    wait, budget on M) and Stop."""

    RUN = "agent-run-1"
    KILLED = {"status": "failed", "hasActiveRun": False,
              "lastRunError": "Claude Code process exited with code 143"}

    def test_the_resume_says_the_interruption_killed_the_tool_and_asks_to_finish(self):
        """A restart kills the tool child (`Exit code 137`). The resume says the interruption
        killed it, not the command, and asks to finish the request."""
        for shown in ("Running step 1.", ""):
            with self.subTest(shown=shown):
                d = openclaw_pipe._resume_directive(shown).lower()
                self.assertIn("gateway", d)
                self.assertIn("tool call", d)
                self.assertIn("rerun", d)
                self.assertIn("finish", d)

    async def test_the_writers_resume_sends_the_directive(self):
        w = await self.opened_writer()
        await w.end({"status": "failed", "hasActiveRun": False,
                     "lastRunError": "Claude Code process exited with code 143"})
        sends = [p for m, p in self.sent if m == "chat.send"]
        self.assertEqual(sends[0]["message"],
                         openclaw_pipe._resume_directive(w.renderer.visible_text, "check the logs"))

    async def test_the_resume_carries_the_users_request(self):
        """The resume runs in a new claude-cli session, so it carries the request."""
        w = await self.opened_writer()
        await w.end(self.KILLED)
        sends = [p for m, p in self.sent if m == "chat.send"]
        self.assertIn("check the logs", sends[0]["message"])

    async def test_a_second_resume_carries_the_first_request_not_the_resume(self):
        w = await self.opened_writer()
        await w.end(self.KILLED)
        first = [p for m, p in self.sent if m == "chat.send"][0]
        # The resume's own user turn is cut off too.
        self.history["messages"].append({"role": "user", "content": first["message"],
                                         "idempotencyKey": f"{first['idempotencyKey']}:user"})
        await w.end(self.KILLED)
        second = [p for m, p in self.sent if m == "chat.send"][1]
        self.assertIn("check the logs", second["message"])
        self.assertEqual(second["message"].count("[system]"), 1, "a resume quotes no resume")

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.history = _turn_history(run=self.RUN)
        self.sent = []
        self.refuse_send = False
        test = self

        async def send_request(method, params, timeout=10):
            test.sent.append((method, copy.deepcopy(params)))
            if method == "chat.history":
                return copy.deepcopy(test.history)
            if method == "chat.send":
                if test.refuse_send:
                    raise GatewayError("UNAVAILABLE: gateway closing")
                return {"runId": params["idempotencyKey"], "status": "started"}
            if method == "chat.abort":
                return {"aborted": True}
            if method == "sessions.describe":
                return {"session": {"modelProvider": "anthropic", "contextTokens": 1_000_000,
                                    "totalTokens": 34_000}}
            if method == "usage.status":
                return {"providers": []}
            raise AssertionError(method)
        self.conn.send_request = send_request

    async def opened_writer(self):
        w = self.writer(run=self.RUN)
        await w.feed(_delta("Reading them."))
        await w.feed(_tool("start", tcid="toolu_1"))
        self.assertTrue(w.opened)
        return w

    def m(self):
        return self.owui.msgs()["m1"]

    def pushed_history(self):
        """M's status history in the last push to open tabs."""
        return self.owui.events("chat:messages")[-1]["data"]["messages"]["m1"].get("statusHistory")

    async def task_done(self, w):
        _, task = self.owui.tasks[w.task_id]
        await asyncio.wait_for(asyncio.shield(task), 1)
        return task

    async def test_an_announce_after_a_silent_parent_does_not_take_the_parents_card(self):
        """ELI-227 (OpenClaw 2026.10.5): after a NO_REPLY parent the announce run's span must not
        reach back to the parent's sessions_spawn card."""
        ann = "announce:v1:agent:main:subagent:x:y"
        started = 1792407300000
        answer = "The background sub-agent finished and answered SUB-51."
        self.history = {"messages": [
            {"role": "user", "content": "spawn", "idempotencyKey": "msg-c1-p-1.0:user",
             "timestamp": 1792407258841},
            {"role": "user", "content": "Conversation info (untrusted metadata): {}",
             "timestamp": 1792407260372, "__openclaw": {"importedFrom": "claude-cli"}},
            {"role": "assistant", "stopReason": "tool_use", "timestamp": 1792407264343,
             "__openclaw": {"importedFrom": "claude-cli"}, "content": [
                 {"type": "toolcall", "id": "toolu_s", "name": "mcp__openclaw__sessions_spawn",
                  "arguments": {"task": "Answer what 17 times 3 is."}},
                 {"type": "tool_result", "tool_use_id": "toolu_s", "name": "mcp__openclaw__sessions_spawn",
                  "content": [{"type": "text", "text": "Started: QA 17x3"}]}]},
            {"role": "assistant", "stopReason": "stop", "timestamp": 1792407306581,
             "idempotencyKey": f"cli-assistant:{ann}", "content": [{"type": "text", "text": answer}]}]}
        w = self.writer(run=ann)
        await w.feed({"runId": ann, "stream": "lifecycle", "data": {"phase": "start", "startedAt": started}})
        await w.feed(_delta(answer))
        self.assertTrue(w.opened)
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": ann})
        self.assertEqual(self.m()["content"], answer)
        self.assertEqual([i["type"] for i in self.m()["output"]], ["message"])

    async def test_a_settled_run_is_finalized_from_its_transcript(self):
        w = await self.opened_writer()
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
        self.assertIs(self.m()["done"], True)
        self.assertEqual(self.m()["content"], "Reading them.\n\nFound it.")
        self.assertEqual([i["type"] for i in self.m()["output"]],
                         ["message", "function_call", "function_call_output", "message"])
        self.assertEqual(self.owui.events("chat:active")[-1]["data"], {"active": False})
        self.assertTrue((await self.task_done(w)).done(), "the Open WebUI task ends with the run")
        self.assertTrue(w.closed)

    async def test_a_reply_cancelled_while_its_message_waited_to_be_steered_says_where_it_went(self):
        """2026.10.10 steers messages from other tabs into a running run. A reply cancelled while
        its message waited ends saying it went with the next one, not empty."""
        self.conn.gateway_version = "2026.10.10"
        self.owui.msgs()["m1"] = {"id": "m1", "role": "assistant", "parentId": "q1",
                                  "content": "", "done": True,
                                  "statusHistory": copy.deepcopy(self.LEFT_BY_THE_CANCEL)}
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id="steered-run", message_id="m1", takeover=True, renderer=_TurnRenderer())
        self.conn.session_history = mock.AsyncMock(return_value={"messages": []})
        await w.feed({"runId": "steered-run", "sessionKey": self.KEY, "state": "final"})
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": "active-run"})
        self.assertIs(self.m()["done"], True)
        self.assertEqual(self.m()["content"], openclaw_pipe.REPLY_SENT_WITH_NEXT_NOTE)

    # A Pipe reply run, as `pipe_run_id` names it.
    STEERED = "msg-c0ffee00-0000-4000-8000-000000000001-00000000-0000-4000-8000-000000000002-1.0"

    def _steered_transcript(self, *after, committed=True):
        """R1's message, its answer, the message steered in as STEERED, then `after`."""
        return {"messages": [
            {"role": "user", "content": "count", "__openclaw": {"idempotencyKey": "R1:user"}},
            {"role": "assistant", "content": [{"type": "text", "text": "slow-1 slow-2"}],
             "stopReason": "stop", "__openclaw": {"runId": "R1"}},
            *([{"role": "user", "content": "aside",
                "__openclaw": {"idempotencyKey": f"{self.STEERED}:user"}}] if committed else []),
            *after]}

    async def test_a_steered_reply_waits_for_its_answer_in_the_transcript(self):
        """The idle push can come before the Gateway stores the answer to a steered message; the
        reply waits for that transcript append."""
        self.conn.gateway_version = "2026.10.10"
        self.owui.msgs()["m1"] = {"id": "m1", "role": "assistant", "parentId": "q1",
                                  "content": "", "done": True,
                                  "statusHistory": copy.deepcopy(self.LEFT_BY_THE_CANCEL)}
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id=self.STEERED, message_id="m1", takeover=True, renderer=_TurnRenderer())
        hist = {"now": self._steered_transcript()}
        self.conn.session_history = mock.AsyncMock(side_effect=lambda *a, **k: copy.deepcopy(hist["now"]))
        ending = asyncio.ensure_future(w.end({"status": "done", "hasActiveRun": False, "lastRunId": "R1"}))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(ending.done(), "it waits: the answer is not stored yet")
        hist["now"] = self._steered_transcript(
            {"role": "assistant", "content": [{"type": "text", "text": "the aside"}],
             "stopReason": "stop", "__openclaw": {"runId": "R1"}})
        self.conn._on_session_message({"sessionKey": self.KEY})
        await asyncio.wait_for(ending, 5)
        self.assertIs(self.m()["done"], True)
        self.assertEqual(self.m()["content"], "the aside")

    async def test_a_steered_reply_waits_while_its_run_has_stored_nothing_yet(self):
        """Committed message, run not yet stored anything: the reply's lone final said steered, so
        it waits for that run's answer (#278)."""
        self.conn.gateway_version = "2026.10.10"
        self.owui.msgs()["m1"] = {"id": "m1", "role": "assistant", "parentId": "q1",
                                  "content": "", "done": True,
                                  "statusHistory": copy.deepcopy(self.LEFT_BY_THE_CANCEL)}
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id=self.STEERED, message_id="m1", takeover=True, renderer=_TurnRenderer())
        w.steered_message = True
        start = [{"role": "user", "content": "count", "__openclaw": {"idempotencyKey": "R1:user"}},
                 {"role": "user", "content": "aside", "__openclaw": {"idempotencyKey": f"{self.STEERED}:user"}}]
        hist = {"now": {"messages": start}}
        self.conn.session_history = mock.AsyncMock(side_effect=lambda *a, **k: copy.deepcopy(hist["now"]))
        ending = asyncio.ensure_future(w.end({"status": "done", "hasActiveRun": False, "lastRunId": "R1"}))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(ending.done(), "it waits: the run stored nothing yet")
        hist["now"] = {"messages": start + [
            {"role": "assistant", "content": [{"type": "text", "text": "the aside"}],
             "stopReason": "stop", "__openclaw": {"runId": "R1"}}]}
        self.conn._on_session_message({"sessionKey": self.KEY})
        await asyncio.wait_for(ending, 5)
        self.assertEqual(self.m()["content"], "the aside")

    async def test_a_steered_reply_stops_waiting_when_its_run_is_aborted(self):
        """An aborted or failed run stores no answer; its event ends the wait."""
        self.conn.gateway_version = "2026.10.10"
        self.owui.msgs()["m1"] = {"id": "m1", "role": "assistant", "parentId": "q1",
                                  "content": "", "done": True,
                                  "statusHistory": copy.deepcopy(self.LEFT_BY_THE_CANCEL)}
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id=self.STEERED, message_id="m1", takeover=True, renderer=_TurnRenderer())
        self.conn.session_history = mock.AsyncMock(side_effect=lambda *a, **k: self._steered_transcript())
        ending = asyncio.ensure_future(w.end({"status": "done", "hasActiveRun": False, "lastRunId": "R1"}))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(ending.done(), "it waits: the answer is not stored yet")
        await self.conn._dispatch_run_event({"type": "event", "event": "chat", "payload": {
            "sessionKey": self.KEY, "runId": "R1", "state": "aborted"}})
        await asyncio.wait_for(ending, 5)
        self.assertIs(self.m()["done"], True)

    async def test_a_steered_reply_stops_waiting_once_a_gateway_restart_lost_its_run(self):
        """A Gateway restart that lost the run ends the wait, read after the reconnect gap once the
        session has no run (#278)."""
        self.conn.gateway_version = "2026.10.10"
        self.owui.msgs()["m1"] = {"id": "m1", "role": "assistant", "parentId": "q1",
                                  "content": "", "done": True,
                                  "statusHistory": copy.deepcopy(self.LEFT_BY_THE_CANCEL)}
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id=self.STEERED, message_id="m1", takeover=True, renderer=_TurnRenderer())
        self.conn.session_history = mock.AsyncMock(side_effect=lambda *a, **k: self._steered_transcript())
        rows = {"active": True}

        async def send_request(method, params, timeout=10):
            assert method == "sessions.list", method
            return {"sessions": [{"key": self.KEY, "hasActiveRun": rows["active"]}]}
        self.conn.send_request = send_request
        ending = asyncio.ensure_future(w.end({"status": "done", "hasActiveRun": False, "lastRunId": "R1"}))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(ending.done(), "it waits: the answer is not stored yet")
        self.conn.gateway_restarts += 1
        rows["active"] = False
        self.conn._wake_session_waiters()           # the reconnect's gap
        await asyncio.wait_for(ending, 5)
        self.assertIs(self.m()["done"], True)

    async def test_a_steered_reply_whose_final_came_after_the_settle_says_where_it_went(self):
        """A lone final reaching the writer after the idle row: the transcript says its message
        went with the next."""
        self.conn.gateway_version = "2026.10.10"
        self.owui.msgs()["m1"] = {"id": "m1", "role": "assistant", "parentId": "q1",
                                  "content": "", "done": True,
                                  "statusHistory": copy.deepcopy(self.LEFT_BY_THE_CANCEL)}
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id=self.STEERED, message_id="m1", takeover=True, renderer=_TurnRenderer())
        self.conn.session_history = mock.AsyncMock(return_value=self._steered_transcript(
            {"role": "user", "content": "second", "__openclaw": {"idempotencyKey": "R3:user"}},
            {"role": "assistant", "content": [{"type": "text", "text": "both"}],
             "stopReason": "stop", "__openclaw": {"runId": "R1"}}))
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": "R1"})
        self.assertIs(self.m()["done"], True)
        self.assertEqual(self.m()["content"], openclaw_pipe.REPLY_SENT_WITH_NEXT_NOTE)

    async def test_a_gateway_that_never_steers_never_says_sent_with_next(self):
        """A Gateway before 2026.10.10 never steers, so the transcript shape alone does not set the
        steered note (#278)."""
        self.conn.gateway_version = "2026.10.5"
        self.owui.msgs()["m1"] = {"id": "m1", "role": "assistant", "parentId": "q1",
                                  "content": "", "done": True,
                                  "statusHistory": copy.deepcopy(self.LEFT_BY_THE_CANCEL)}
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id=self.STEERED, message_id="m1", takeover=True, renderer=_TurnRenderer())
        self.conn.session_history = mock.AsyncMock(return_value=self._steered_transcript(
            {"role": "user", "content": "second", "__openclaw": {"idempotencyKey": "R3:user"}},
            {"role": "assistant", "content": [{"type": "text", "text": "both"}],
             "stopReason": "stop", "__openclaw": {"runId": "R1"}}))
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": "R1"})
        self.assertIs(self.m()["done"], True)
        self.assertNotEqual(self.m()["content"], openclaw_pipe.REPLY_SENT_WITH_NEXT_NOTE)

    async def test_a_steered_reply_waits_for_its_message_to_be_committed(self):
        """The idle row can come before the steered message is committed: the reply waits for the
        commit, then the answer."""
        self.conn.gateway_version = "2026.10.10"
        self.owui.msgs()["m1"] = {"id": "m1", "role": "assistant", "parentId": "q1",
                                  "content": "", "done": True,
                                  "statusHistory": copy.deepcopy(self.LEFT_BY_THE_CANCEL)}
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id=self.STEERED, message_id="m1", takeover=True, renderer=_TurnRenderer())
        hist = {"now": self._steered_transcript(committed=False)}
        self.conn.session_history = mock.AsyncMock(side_effect=lambda *a, **k: copy.deepcopy(hist["now"]))
        ending = asyncio.ensure_future(w.end({"status": "done", "hasActiveRun": False, "lastRunId": "R1"}))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(ending.done(), "it waits: its message is not committed yet")
        hist["now"] = self._steered_transcript()
        self.conn._on_session_message({"sessionKey": self.KEY})
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(ending.done(), "committed, its answer not stored yet")
        hist["now"] = self._steered_transcript(
            {"role": "assistant", "content": [{"type": "text", "text": "the aside"}],
             "stopReason": "stop", "__openclaw": {"runId": "R1"}})
        self.conn._on_session_message({"sessionKey": self.KEY})
        await asyncio.wait_for(ending, 5)
        self.assertEqual(self.m()["content"], "the aside")

    async def test_a_steered_reply_whose_lone_final_came_before_the_commit_waits(self):
        """A lone final before the commit is the steer's sign, not an abort: the reply waits for
        the commit and the answer."""
        self.conn.gateway_version = "2026.10.10"
        self.owui.msgs()["m1"] = {"id": "m1", "role": "assistant", "parentId": "q1",
                                  "content": "", "done": True,
                                  "statusHistory": copy.deepcopy(self.LEFT_BY_THE_CANCEL)}
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id=self.STEERED, message_id="m1", takeover=True, renderer=_TurnRenderer())
        pump = openclaw_pipe._RunPump()
        pump.terminal.set()
        self.conn._turn_pumps[(self.KEY, self.STEERED)] = pump
        hist = {"now": self._steered_transcript(committed=False)}
        self.conn.session_history = mock.AsyncMock(side_effect=lambda *a, **k: copy.deepcopy(hist["now"]))
        await w.feed({"runId": self.STEERED, "sessionKey": self.KEY, "state": "final"})
        ending = asyncio.ensure_future(w.end({"status": "done", "hasActiveRun": False, "lastRunId": "R1"}))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(ending.done(), "it waits: its message is not committed yet")
        reads = self.conn.session_history.await_count
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(self.conn.session_history.await_count, reads,
                         "no read without a transcript append: the set terminal wakes nothing")
        hist["now"] = self._steered_transcript(
            {"role": "assistant", "content": [{"type": "text", "text": "the aside"}],
             "stopReason": "stop", "__openclaw": {"runId": "R1"}})
        self.conn._on_session_message({"sessionKey": self.KEY})
        await asyncio.wait_for(ending, 5)
        self.assertEqual(self.m()["content"], "the aside")

    async def test_a_reply_whose_steered_message_was_aborted_does_not_wait(self):
        """Its run aborted and its message never reached the transcript: no wait."""
        self.conn.gateway_version = "2026.10.10"
        w = _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                        run_id=self.STEERED, message_id="m1", takeover=True, renderer=_TurnRenderer())
        pump = openclaw_pipe._RunPump()
        pump.terminal.set()
        self.conn._turn_pumps[(self.KEY, self.STEERED)] = pump
        self.conn.session_history = mock.AsyncMock(return_value=self._steered_transcript(committed=False))
        await asyncio.wait_for(w.end({"status": "done", "hasActiveRun": False, "lastRunId": "R1"}), 5)
        self.assertTrue(w.closed)

    def test_a_reply_cancelled_before_its_run_started_is_not_steered_into(self):
        """Its handed run never ran, so a later steered message takes the running run."""
        empty = {"renderer": _TurnRenderer()}
        shown = _TurnRenderer()
        shown.feed(_delta("Reading them."))
        self.assertFalse(openclaw_pipe._handover_started(empty))
        self.assertTrue(openclaw_pipe._handover_started({"renderer": shown}))

    USAGE = {"description": "🧠 34k/1.0m (3.4%)", "done": True}
    LEFT_BY_THE_CANCEL = [{"description": "Thinking...", "done": False},
                          {"description": "Stopped", "done": True},
                          {"description": "", "done": True}]

    def handed_writer(self, request=None):
        """A Send now mid-reply: the request parked its run, saved "Stopped" and an empty line into
        M's status history, and handed M to this writer."""
        shown = _TurnRenderer()
        shown.feed(_delta("Reading them."))
        self.owui.msgs()["m1"] = {"id": "m1", "role": "assistant", "parentId": "q1",
                                  "content": "Reading them.", "done": True,
                                  "statusHistory": copy.deepcopy(self.LEFT_BY_THE_CANCEL)}
        return _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                           run_id=self.RUN, message_id="m1", takeover=True, renderer=shown,
                           request=request)

    async def test_a_run_the_writer_finishes_ends_with_the_usage_line(self):
        """ELI-210: a writer-finished reply gets the usage line, saved and sent to open tabs (in the
        push of M finished, ELI-371)."""
        w = await self.opened_writer()
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
        self.assertEqual(self.m()["statusHistory"], [self.USAGE])
        self.assertEqual(self.pushed_history(), [self.USAGE])

    async def test_a_first_run_the_writer_finishes_gets_its_usage_from_the_run(self):
        """ELI-210 / ELI-177: with no row totals yet the line uses the run's usage."""
        describe = self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "sessions.describe":
                return {"session": {"modelProvider": "anthropic", "contextTokens": 200_000}}
            return await describe(method, params, timeout=timeout)
        self.conn.send_request = send_request
        closer = next(m for m in self.history["messages"]
                      if m.get("idempotencyKey") == f"cli-assistant:{self.RUN}")
        closer["usage"] = {"input": 10, "cacheRead": 0, "cacheWrite": 0, "output": 5}
        w = await self.opened_writer()
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
        usage = {"description": "🧠 10/200k (0%)", "done": True}
        self.assertEqual(self.m()["statusHistory"], [usage])
        self.assertEqual(self.pushed_history(), [usage])

    async def test_a_takeover_that_finished_drops_the_cancels_stopped_line(self):
        """ELI-210: a finished takeover drops the cancel's "Stopped"."""
        w = self.handed_writer()
        await w.feed(None)
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
        self.assertEqual(self.m()["statusHistory"],
                         [{"description": "Thinking...", "done": False}, self.USAGE])

    async def test_a_takeover_waits_for_its_request_before_it_writes_the_status(self):
        """The cancelled request appends status lines until it ends; the takeover waits."""
        request = asyncio.get_running_loop().create_future()
        w = self.handed_writer(request=request)
        await w.feed(None)
        ending = asyncio.ensure_future(
            w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN}))
        for _ in range(50):
            await asyncio.sleep(0)
        self.assertFalse(ending.done(), "the final write waits for the request")
        request.set_result(None)
        await asyncio.wait_for(ending, 1)
        self.assertEqual(self.m()["statusHistory"][-1], self.USAGE)

    async def test_a_request_still_running_after_the_wait_is_logged(self):
        """After FINAL_STATUS_REQUEST_WAIT_S the write goes ahead; a later one is logged."""
        request = asyncio.get_running_loop().create_future()
        w = self.handed_writer(request=request)
        await w.feed(None)
        logged = []
        with mock.patch.object(openclaw_pipe, "FINAL_STATUS_REQUEST_WAIT_S", 0.01), \
                mock.patch.object(openclaw_pipe, "pipe_log", lambda *a, **k: logged.append(a[0])):
            await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
        late = [line for line in logged if "request still running after" in line]
        self.assertEqual(len(late), 1, logged)
        self.assertIn(w.chat_id[:8], late[0])
        self.assertIn(w.message_id[:8], late[0])
        self.assertIn("its own status write may land after this one", late[0])
        self.assertEqual(self.m()["statusHistory"][-1], self.USAGE)
        request.set_result(None)

    async def test_a_stop_ends_with_stopped_and_no_usage_line(self):
        """A real Stop reads as the normal reply's Stop: "Stopped", no usage."""
        for handed in (False, True):
            with self.subTest(handed=handed):
                self.owui.msgs().pop("m1", None)
                self.sent.clear()
                if handed:
                    w = self.handed_writer()
                    await w.feed(None)
                    await w.open(takeover=True)
                else:
                    w = await self.opened_writer()
                await self.cancel_from_the_page(w, "stop")
                history = self.m().get("statusHistory") or []
                self.assertEqual([h for h in history if h.get("description")][-1],
                                 {"description": "Stopped", "done": True})
                self.assertNotIn("🧠", json.dumps(history, ensure_ascii=False))
                # Nothing adds to it after a restart.
                self.assertIs((self.m().get("meta") or {}).get(openclaw_pipe.STOPPED_META), True)

    async def test_a_handed_over_reply_resumes_with_the_users_request(self):
        """A handed-over reply cut by a restart quotes the user's request, not the task object."""
        request = asyncio.get_running_loop().create_future()
        request.set_result(None)
        w = self.handed_writer(request=request)
        await w.feed(None)
        await w.end(self.KILLED)
        sends = [p for m, p in self.sent if m == "chat.send"]
        self.assertEqual(len(sends), 1)
        self.assertIn("check the logs", sends[0]["message"])
        self.assertNotIn("Future", sends[0]["message"])

    def _found_it_says(self, text):
        for m in self.history["messages"]:
            for b in m.get("content") if isinstance(m.get("content"), list) else []:
                if b.get("text") == "Found it.":
                    b["text"] = text

    async def test_a_question_the_writer_shows_has_no_marker_line(self):
        """The writer has no card (#20): a question shows as clean text without its marker line."""
        marker = openclaw_pipe._USER_INPUT_TRIGGER_PREFIXES[-1]
        self._found_it_says(f"Found it.\n\n{marker}\nColour a1\nWhich colour do you prefer?\n1. Red")
        w = await self.opened_writer()
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
        content = self.m()["content"]
        self.assertNotIn(marker, content)
        self.assertIn("Found it.\n\nColour a1\nWhich colour do you prefer?\n1. Red", content)
        self.assertNotIn(marker, json.dumps(self.m()["output"]))

    async def test_a_quoted_marker_in_a_fence_stays_in_the_writers_text(self):
        marker = openclaw_pipe._USER_INPUT_TRIGGER_PREFIXES[-1]
        quote = f"Found it:\n```\n{marker}\nColour a1\n```\nDone."
        self._found_it_says(quote)
        w = await self.opened_writer()
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
        self.assertIn(quote, self.m()["content"])

    async def test_without_a_transcript_what_streamed_is_kept(self):
        self.history = {"messages": []}
        w = await self.opened_writer()
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
        self.assertIs(self.m()["done"], True)
        self.assertEqual(self.m()["content"].strip()[:13], "Reading them.")

    async def test_a_real_error_is_shown_and_not_resumed(self):
        w = await self.opened_writer()
        await w.end({"status": "failed", "lastRunError": "429 rate limited", "hasActiveRun": False})
        self.assertIs(self.m()["done"], True)
        self.assertIn(RUN_ERROR_SENTENCE, self.m()["content"])
        self.assertIn("```\n429 rate limited\n```", self.m()["content"])
        self.assertNotIn("**Error:**", self.m()["content"])
        self.assertNotIn("chat.send", [m for m, _ in self.sent])

    async def test_the_writers_error_follows_the_language_of_the_request_it_answers(self):
        for m in self.history["messages"]:
            if m.get("idempotencyKey") == f"{self.RUN}:user":
                m["content"] = [{"type": "text", "text": HEBREW_ASK}]
        w = await self.opened_writer()
        await w.end({"status": "failed", "lastRunError": "429 rate limited", "hasActiveRun": False})
        he = openclaw_pipe._ERROR_TEXT[openclaw_pipe.LANG_HE]
        self.assertIn(he[openclaw_pipe.ERROR_RUN], self.m()["content"])
        self.assertIn("429 rate limited", self.m()["content"].split("<details>")[1])

    EMPTY = "CLI backend returned an empty response."

    def _user_turn_from(self, provenance):
        """Make the run's `<run>:user` turn come from `provenance` (None: a person)."""
        for m in self.history["messages"]:
            if m.get("idempotencyKey") == f"{self.RUN}:user":
                if provenance:
                    m["provenance"] = provenance
                else:
                    m.pop("provenance", None)

    async def test_a_silent_inter_session_turn_shows_no_error(self):
        """ELI-336: a sessions_send turn ending NO_REPLY on purpose shows no error."""
        self._user_turn_from({"kind": "inter_session", "sourceTool": "sessions_send",
                              "sourceSessionKey": "agent:main:openwebui-u-other"})
        w = await self.opened_writer()
        await w.end({"status": "failed", "lastRunError": self.EMPTY, "hasActiveRun": False})
        self.assertIs(self.m()["done"], True)
        self.assertNotIn("empty response", self.m()["content"])
        self.assertNotIn("Error", self.m()["content"])

    async def test_an_empty_response_to_a_person_is_still_shown(self):
        self._user_turn_from(None)
        w = await self.opened_writer()
        await w.end({"status": "failed", "lastRunError": self.EMPTY, "hasActiveRun": False})
        self.assertIn(self.EMPTY, self.m()["content"].split("<details>")[1])

    async def test_another_error_on_an_inter_session_turn_is_still_shown(self):
        self._user_turn_from({"kind": "inter_session", "sourceTool": "sessions_send"})
        w = await self.opened_writer()
        await w.end({"status": "failed", "lastRunError": "429 rate limited", "hasActiveRun": False})
        self.assertIn("429 rate limited", self.m()["content"].split("<details>")[1])

    async def test_a_running_row_keeps_the_message_open_for_the_recovery(self):
        w = await self.opened_writer()
        await w.end({"status": "running", "hasActiveRun": True})
        self.assertIs(self.m()["done"], False)
        self.assertFalse(w.closed)
        self.assertNotIn("chat.send", [m for m, _ in self.sent])
        w.adopt("f2445aa8-aeda-47d8-af7b-cf853aae34e6")
        self.assertEqual(w.run_id, "f2445aa8-aeda-47d8-af7b-cf853aae34e6")

    async def test_an_outside_kill_is_resumed_into_the_same_message(self):
        w = await self.opened_writer()
        await w.end({"status": "failed", "hasActiveRun": False,
                     "lastRunError": "Claude Code process exited with code 143"})
        sends = [p for m, p in self.sent if m == "chat.send"]
        self.assertEqual(len(sends), 1)
        self.assertTrue(sends[0]["idempotencyKey"].startswith("msg-c1-m1-resume-"),
                        "keyed to the message it continues")
        self.assertIn("cut off", sends[0]["message"])
        self.assertEqual(w.run_id, sends[0]["idempotencyKey"])
        self.assertIs(self.m()["done"], False)
        self.assertEqual(w.resumes, 1)

    async def test_a_row_of_another_run_resumes_only_that_run(self):
        """ELI-159: one settled row can reach two writers of a session. Only the run it names is
        resumed; the other message ends cut off."""
        mine = await self.opened_writer()
        other = self.writer(run="ad4cc4fc-84e8-4746-9430-837aaf987f8e", message_id="m2")
        row = {"status": "failed", "hasActiveRun": False, "lastRunId": other.run_id,
               "lastRunError": "Claude Code process exited with signal SIGKILL"}
        await mine.end(row)
        await other.end(row)
        sends = [p["idempotencyKey"] for m, p in self.sent if m == "chat.send"]
        self.assertEqual(len(sends), 1)
        self.assertTrue(sends[0].startswith("msg-c1-m2-resume-"))
        self.assertTrue(mine.closed)
        self.assertIs(self.m()["done"], True)
        self.assertIn(openclaw_pipe.TURN_CUT_OFF_NOTE, self.m()["content"])

    async def test_a_row_naming_the_writers_resume_run_resumes_again(self):
        w = await self.opened_writer()
        await w.end(self.KILLED)
        await w.end({**self.KILLED, "lastRunId": w.run_id})
        self.assertEqual(len([m for m, _ in self.sent if m == "chat.send"]), 2)
        self.assertEqual(w.resumes, 2)

    async def test_a_resume_keeps_the_handed_over_thinking_level(self):
        """The resume keeps the handed-over thinking level (ELI-85)."""
        w = await self.opened_writer()
        w.thinking = "high"
        await w.end({"status": "failed", "hasActiveRun": False,
                     "lastRunError": "Claude Code process exited with code 143"})
        (send,) = [p for m, p in self.sent if m == "chat.send"]
        self.assertEqual(send.get("thinking"), "high")

    async def test_a_refused_resume_is_sent_on_the_next_hello(self):
        w = await self.opened_writer()
        self.refuse_send = True
        await w.end({"status": "failed", "hasActiveRun": False,
                     "lastRunError": "Claude Code process exited with code 143"})
        self.assertTrue(w.pending_resume)
        self.refuse_send = False
        await w.on_hello()
        self.assertEqual(len([m for m, _ in self.sent if m == "chat.send"]), 2)
        self.assertFalse(w.pending_resume)

    async def test_an_oom_loop_stops_after_the_budget_with_a_note(self):
        w = await self.opened_writer()
        oom = {"status": "failed", "hasActiveRun": False,
               "lastRunError": "Claude Code process exited with code 137"}
        for _ in range(MAX_TURN_RESUMES + 1):
            await w.end(oom)
        self.assertEqual(len([m for m, _ in self.sent if m == "chat.send"]), MAX_TURN_RESUMES)
        self.assertIs(self.m()["done"], True)
        self.assertIn("cut off", self.m()["content"])

    async def test_a_busy_session_waits(self):
        w = await self.opened_writer()
        await w.end({"status": "failed", "hasActiveRun": True,
                     "lastRunError": "Claude Code process exited with code 143"})
        self.assertNotIn("chat.send", [m for m, _ in self.sent])
        self.assertIs(self.m()["done"], False)

    async def cancel_from_the_page(self, w, answer):
        """The page cancels the writer's task; its intent hook answers `answer`."""
        self.owui.session_pool.update({"sid-1": {"id": self.USER, "chat_ids": [self.CHAT]},
                                       "sid-other": {"id": self.USER, "chat_ids": ["c2"]}})
        self.owui.page_intent["sid-1"] = answer
        _, task = self.owui.tasks[w.task_id]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(200):
            await asyncio.sleep(0)

    async def test_the_writer_installs_the_page_intent_hook_when_it_opens_m(self):
        await self.opened_writer()
        codes = [e["data"]["code"] for e in self.owui.events("execute")]
        self.assertTrue(any(_INTENT_HOOK_JS in c and 'window.__ocAutoSend["c1"] = true' in c
                            for c in codes), codes)

    async def test_a_stopped_reply_is_marked_before_its_done_save(self):
        """The Stop mark is there when M is saved done, so a restart cannot add to it (#282)."""
        w = await self.opened_writer()
        at_done = []
        persist = w._persist

        async def watched(**kw):
            if kw.get("done"):
                at_done.append((self.m().get("meta") or {}).get(openclaw_pipe.STOPPED_META))
            return await persist(**kw)
        w._persist = watched
        await self.cancel_from_the_page(w, "stop")
        self.assertEqual(at_done, [True])

    async def test_stop_aborts_the_run_and_finalizes_what_it_has(self):
        w = await self.opened_writer()
        await self.cancel_from_the_page(w, "stop")
        (_e, call, sid), = self.owui.calls
        self.assertEqual((sid, call["message_id"]), ("sid-1", "m1"),
                         "only a tab showing this chat is asked, about M")
        self.assertIn(("chat.abort", {"sessionKey": self.KEY, "runId": self.RUN}), self.sent)
        self.assertIs(self.m()["done"], True)
        self.assertTrue(w.closed)

    def _aborted_transcript(self, text="Reading them."):
        """An aborted claude-cli run as stored: the streamed text injected by chat.abort, the CLI's
        aborted closer repeating it, no tool calls."""
        self.history = {"messages": [
            {"role": "user", "content": "check the logs", "idempotencyKey": f"{self.RUN}:user"},
            {"role": "assistant", "provider": "openclaw", "model": "gateway-injected",
             "stopReason": "stop", "idempotencyKey": f"{self.RUN}:assistant",
             "openclawAbort": {"aborted": True, "origin": "rpc", "runId": self.RUN},
             "content": [{"type": "text", "text": text}]},
            {"role": "assistant", "provider": "claude-cli", "stopReason": "aborted",
             "idempotencyKey": f"cli-assistant:{self.RUN}",
             "content": [{"type": "text", "text": text}]},
        ]}

    async def test_an_aborted_runs_transcript_never_replaces_its_cards(self):
        """Finalizing from an aborted run's transcript keeps the streamed cards with the text once,
        ended by Stop or by the settled row."""
        for how in ("stop", "settled"):
            with self.subTest(how=how):
                await self.asyncSetUp()
                self._aborted_transcript()
                w = await self.opened_writer()
                await w.feed(_tool("result", tcid="toolu_1"))
                if how == "stop":
                    await self.cancel_from_the_page(w, "stop")
                else:
                    await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
                self.assertIs(self.m()["done"], True)
                calls = [o.get("call_id") or o.get("id") for o in self.m()["output"]
                         if o["type"] == "function_call"]
                self.assertEqual(calls, ["toolu_1"], "the streamed card was dropped")
                self.assertEqual(self.m()["content"].count("Reading them."), 1)

    KILLED_RUN, NEXT_RUN = "killed-run", "next-run"

    def _interrupted_transcript(self, next_items):
        """The killed run left only its user turn; the continuing run is complete."""
        self.history = {"messages": [
            {"role": "user", "content": "check the logs", "idempotencyKey": f"{self.KILLED_RUN}:user"},
            {"role": "user", "content": "[system] continue", "idempotencyKey": f"{self.NEXT_RUN}:user"},
            *({"role": "assistant", "__openclaw": {"importedFrom": "claude-cli"}, "content": [b]}
              for b in next_items),
            {"role": "assistant", "idempotencyKey": f"cli-assistant:{self.NEXT_RUN}",
             "content": [{"type": "text", "text": "\n\n".join(
                 b["text"] for b in next_items if b["type"] == "text")}]},
        ]}

    @staticmethod
    def _call(tcid, out="ok"):
        return [{"type": "toolcall", "id": tcid, "name": "Bash", "arguments": {"command": "ls"}},
                {"type": "tool_result", "tool_use_id": tcid, "name": "Bash",
                 "content": [{"type": "text", "text": out}]}]

    def _shape(self):
        return [(o["type"], o.get("call_id") or o.get("id")) if o["type"] == "function_call"
                else ("message", o["content"][0]["text"])
                for o in self.m()["output"] if o["type"] in ("function_call", "message")]

    async def test_a_resume_after_a_killed_run_keeps_what_the_killed_run_showed(self):
        """A resume keeps what the killed run showed, then its own text after the tool-call
        separator, not glued on (#21)."""
        calls = [b for t in ("r1", "r2") for b in self._call(t)]
        self._interrupted_transcript([calls[0]] + [calls[1]] + [calls[2]] + [calls[3]]
                                     + [{"type": "text", "text": "USER-DONE"}])
        self.history["messages"][2:6] = [
            {"role": "assistant", "__openclaw": {"importedFrom": "claude-cli"}, "content": self._call(t)}
            for t in ("r1", "r2")]
        w = self.writer(run=self.KILLED_RUN)
        await w.feed(_delta("I'll run the steps."))
        await w.feed(_tool("start", tcid="toolu_k"))
        await w.feed(_tool("result", tcid="toolu_k", result="Exit code 137"))
        w.adopt(self.NEXT_RUN)
        for t in ("r1", "r2"):
            await w.feed(_tool("start", tcid=t))
            await w.feed(_tool("result", tcid=t))
        await w.feed(_delta("USER-DONE"))
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.NEXT_RUN})
        self.assertEqual(self.m()["content"], "I'll run the steps.\n\nUSER-DONE")
        self.assertEqual(self._shape(), [("message", "I'll run the steps."), ("function_call", "toolu_k"),
                                         ("function_call", "r1"), ("function_call", "r2"),
                                         ("message", "\n\nUSER-DONE")])

    async def test_a_killed_run_that_showed_only_text_keeps_it(self):
        self._interrupted_transcript([*self._call("r1"), {"type": "text", "text": "USER-DONE"}])
        self.history["messages"][2:4] = [
            {"role": "assistant", "__openclaw": {"importedFrom": "claude-cli"}, "content": self._call("r1")}]
        w = self.writer(run=self.KILLED_RUN)
        await w.feed(_delta("I'll run the steps."))
        w.adopt(self.NEXT_RUN)
        await w.feed(_tool("start", tcid="r1"))
        await w.feed(_delta("USER-DONE"))
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.NEXT_RUN})
        self.assertEqual(self.m()["content"], "I'll run the steps.\n\nUSER-DONE")

    async def test_a_recovery_the_writer_joined_late_is_completed_from_its_transcript(self):
        """A recovery that started before the pipe was back is completed from its transcript,
        keeping the killed run's card too (#21)."""
        self._interrupted_transcript([{"type": "text", "text": "Checking the history."},
                                      *self._call("r1"), {"type": "text", "text": "Ran again."}])
        self.history["messages"][2:6] = [
            {"role": "assistant", "__openclaw": {"importedFrom": "claude-cli"}, "content": [b]}
            for b in ({"type": "text", "text": "Checking the history."},)] + [
            {"role": "assistant", "__openclaw": {"importedFrom": "claude-cli"}, "content": self._call("r1")},
            {"role": "assistant", "__openclaw": {"importedFrom": "claude-cli"},
             "content": [{"type": "text", "text": "Ran again."}]}]
        w = self.writer(run=self.KILLED_RUN)
        await w.feed(_tool("start", tcid="toolu_k"))
        w.adopt(self.NEXT_RUN)
        await w.feed(_delta("\n\nRan again."))   # joined after r1: missed its start
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.NEXT_RUN})
        self.assertEqual(self._shape(), [("function_call", "toolu_k"),
                                         ("message", "Checking the history."), ("function_call", "r1"),
                                         ("message", "\n\nRan again.")])
        self.assertEqual(self.m()["content"], "Checking the history.\n\nRan again.")

    async def test_send_now_keeps_writing_the_agent_turn(self):
        """DESIGN case 8: Send now cancels the task but the run keeps its message to the end; the
        queued message runs after (case 7)."""
        w = await self.opened_writer()
        await self.cancel_from_the_page(w, "send_now")
        self.assertNotIn("chat.abort", [m for m, _ in self.sent])
        self.assertFalse(w.closed)
        await w.feed(_delta("\n\nFound it."))
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
        self.assertIs(self.m()["done"], True)
        self.assertEqual(self.m()["content"], "Reading them.\n\nFound it.")

    async def test_an_unknown_intent_keeps_writing(self):
        w = await self.opened_writer()
        await self.cancel_from_the_page(w, None)
        self.assertNotIn("chat.abort", [m for m, _ in self.sent])
        self.assertFalse(w.closed)

    async def test_a_silent_run_with_tools_keeps_its_cards_and_no_sentinel(self):
        self.history = {"messages": []}
        w = self.writer(run=self.RUN)
        await w.feed(_tool("start", tcid="toolu_1"))
        await w.feed(_tool("result", tcid="toolu_1"))
        await w.feed(_delta("NO_REPLY"))
        await w.end({"status": "done", "hasActiveRun": False})
        self.assertIs(self.m()["done"], True)
        self.assertNotIn("NO_REPLY", self.m()["content"])
        self.assertIn("function_call", [i["type"] for i in self.m()["output"]])


class ResumeRequestReviewTests(_TurnWriterCase):
    """The request a resume quotes survives a re-created writer, a long request, its own rule
    lines, a metadata block and attachments (#20)."""

    RUN, KILLED = TurnWriterEndTests.RUN, TurnWriterEndTests.KILLED
    opened_writer = TurnWriterEndTests.opened_writer

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sent, test = [], self

        async def send_request(method, params, timeout=10):
            test.sent.append((method, copy.deepcopy(params)))
            if method == "chat.history":
                return copy.deepcopy(test.history)
            if method == "chat.send":
                return {"runId": params["idempotencyKey"], "status": "started"}
            raise AssertionError(method)
        self.conn.send_request = send_request
    # Pipe run ids name real UUIDs, as parse_pipe_run_id requires.
    CHAT = "24c342e4-50fa-442e-9b93-c215ed135f3a"
    KEY = f"agent:main:openwebui-u1-{CHAT}"
    MID = "8acdce95-1111-4222-8333-444455556666"
    ORIGINAL_RUN = f"msg-{CHAT}-{MID}-1.0"                  # M's own run
    RESUME_RUN = f"msg-{CHAT}-{MID}-resume-2.0"             # the first resume of M
    REQUEST = "Run steps 1 to 5 of the migration and tell me which one failed."
    BLOCK = 'Conversation info (untrusted metadata):\n```json\n{"chat_id": "c1"}\n```\n\n'

    @staticmethod
    def _user(run, content):
        return {"role": "user", "content": content, "idempotencyKey": f"{run}:user"}

    def sends(self):
        return [p for m, p in self.sent if m == "chat.send"]

    async def adopted_writer(self, run):
        """A writer for a run it did not start (`_adopt_turn_message` after a redeploy,
        `_turn_writer_for_locked` after a restart). Its `runs` holds only that run."""
        w = self.writer(run=run, message_id=self.MID)
        await w.feed(_delta("Step 3 done."))
        return w

    async def test_F1_a_resume_of_a_resume_after_the_writer_is_recreated_keeps_the_request(self):
        """A resume of a resume after a re-created writer still quotes the request."""
        first = openclaw_pipe._resume_directive("Step 1 done.", self.REQUEST)
        self.history = {"messages": [self._user(self.ORIGINAL_RUN, self.REQUEST),
                                     self._user(self.RESUME_RUN, first)]}
        w = await self.adopted_writer(self.RESUME_RUN)
        await w.end(self.KILLED)
        self.assertIn(self.REQUEST, self.sends()[0]["message"])

    async def test_F1_an_agent_turns_request_survives_a_recreated_writer_too(self):
        """An agent turn's message id comes from its run id; its resume names M."""
        agent_run = "gate-agent-1234"
        mid = openclaw_pipe.turn_message_id(self.KEY, agent_run, self.CHAT)
        resume = f"msg-{self.CHAT}-{mid}-resume-2.0"
        first = openclaw_pipe._resume_directive("Step 1 done.", self.REQUEST)
        self.history = {"messages": [self._user("other-run", "an earlier turn"),
                                     self._user(agent_run, self.REQUEST),
                                     self._user(resume, first)]}
        w = self.writer(run=resume, message_id=mid)
        await w.feed(_delta("Step 3 done."))
        await w.end(self.KILLED)
        sent = self.sends()[0]["message"]
        self.assertIn(self.REQUEST, sent)
        self.assertNotIn("an earlier turn", sent)

    async def test_N1_an_image_turn_as_stored_says_it_had_an_attachment(self):
        """The 9.3 Gateway stores an image turn as string content with the <attached_files> listing
        and images in `__openclaw.media`."""
        text = ('<attached_files>\n<file type="image" url="c653b2d2" content_type="image/png" '
                'name="image.png"/>\n</attached_files>\n\nWhat is wrong in this screenshot?')
        turn = self._user(self.RUN, text)
        turn["__openclaw"] = {"media": [{"path": "/x/image.png", "contentType": "image/png"}],
                              "mediaImageLayout": {"slots": [{"kind": "image", "factIndex": 0}]}}
        self.history = {"messages": [turn]}
        w = await self.opened_writer()
        await w.end(self.KILLED)
        self.assertIn("1 attachment ", self.sends()[0]["message"])

    async def test_N2_an_answer_turn_is_not_quoted_as_the_request(self):
        """The first turn naming M may be an answer (an `input` run), not a request."""
        answer_run = f"msg-{self.CHAT}-{self.MID}-input-1.5"
        first = openclaw_pipe._resume_directive("Step 1 done.", None)
        self.history = {"messages": [self._user(answer_run, "Postgres"),
                                     self._user(self.RESUME_RUN, first)]}
        w = await self.adopted_writer(self.RESUME_RUN)
        await w.end(self.KILLED)
        self.assertNotIn("Postgres", self.sends()[0]["message"])

    async def test_N2_the_reply_run_is_the_request_even_after_an_answer(self):
        answer_run = f"msg-{self.CHAT}-{self.MID}-input-1.5"
        self.history = {"messages": [self._user(self.ORIGINAL_RUN, self.REQUEST),
                                     self._user(answer_run, "Postgres"),
                                     self._user(self.RESUME_RUN, "x")]}
        w = await self.adopted_writer(answer_run)
        await w.end(self.KILLED)
        sent = self.sends()[0]["message"]
        self.assertIn(self.REQUEST, sent)
        self.assertNotIn("Postgres", sent)

    async def test_F5_a_directive_behind_the_metadata_block_is_not_taken_for_the_request(self):
        """A resume behind the metadata block (#22) is still a resume, not the request."""
        first = openclaw_pipe._resume_directive("Step 1 done.", self.REQUEST)
        self.history = {"messages": [self._user(self.RESUME_RUN, self.BLOCK + first)]}
        w = await self.adopted_writer(self.RESUME_RUN)
        await w.end(self.KILLED)
        self.assertEqual(self.sends()[0]["message"].count("[system] Your previous"), 1,
                         "a resume quotes no resume")

    async def test_F5_the_quoted_request_has_no_metadata_block(self):
        self.history = {"messages": [self._user(self.RUN, self.BLOCK + self.REQUEST)]}
        w = await self.opened_writer()
        await w.end(self.KILLED)
        sent = self.sends()[0]["message"]
        self.assertIn(self.REQUEST, sent)
        self.assertNotIn("untrusted metadata", sent)

    async def test_F3_a_request_with_an_image_says_so_in_the_resume(self):
        """The resume's new session gets no image, so it says one was there."""
        content = [{"type": "text", "text": "What is wrong in this screenshot?"},
                   {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="}]
        self.history = {"messages": [self._user(self.RUN, content)]}
        w = await self.opened_writer()
        await w.end(self.KILLED)
        sent = self.sends()[0]
        self.assertIn("What is wrong in this screenshot?", sent["message"])
        self.assertTrue(sent.get("attachments") or "image" in sent["message"].lower(),
                        "the resume neither re-attaches the image nor says there was one")
        self.assertIn("1 attachment", sent["message"])

    def test_F2_a_long_request_keeps_its_question(self):
        """A long request keeps head and tail, with a line saying what was left out."""
        request = "LOG LINE\n" * 800 + "Which step failed, and why?"
        d = openclaw_pipe._resume_directive("Looking at the log.", request)
        self.assertIn("Which step failed, and why?", d)
        self.assertIn("LOG LINE\nLOG LINE", d)
        self.assertIn("omitted", d)
        self.assertLess(len(d), 9000)

    def test_F2b_a_request_with_a_rule_line_stays_one_quote(self):
        """The delimiter is chosen so the closing line never occurs in the request."""
        for request in ("front matter:\n---\ntitle: x\n---\nRender this page.",
                        "a\n------\nb", "plain request"):
            with self.subTest(request=request):
                d = openclaw_pipe._resume_directive("", request)
                delim, rest = d.split("was:\n\n", 1)[1].split("\n", 1)
                self.assertNotIn(delim, request.split("\n"))
                self.assertEqual(rest.split("\n" + delim + "\n", 1)[0], request)


class _RoutingCase(unittest.IsolatedAsyncioTestCase):
    """A connection wired to a fake Open WebUI chat, fed as the receive loop does."""

    USER = "00000000-0000-4000-8000-0000000000a1"
    CHAT = "24c342e4-50fa-442e-9b93-c215ed135f3a"
    RUN = "agent-run-1"
    REC = "8f27e4c8-ed9a-4acf-9df1-992ee2f4e801"

    async def asyncSetUp(self):
        self.owui = _FakeOwuiChat(self, self.CHAT)
        self.addAsyncCleanup(self.owui.cancel_tasks)
        self.conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        self.key = _owui_session_key("main", self.USER, self.CHAT)
        self.history = _turn_history(run=self.RUN)
        self.sent = []
        test = self

        async def send_request(method, params, timeout=10):
            test.sent.append((method, copy.deepcopy(params)))
            if method == "chat.history":
                return copy.deepcopy(test.history)
            if method in ("sessions.messages.subscribe", "chat.abort"):
                return {}
            raise AssertionError(method)
        self.conn.send_request = send_request

    async def spin(self, n=300):
        for _ in range(n):
            await asyncio.sleep(0)

    def ev(self, run, payload):
        return {"sessionKey": self.key, "runId": run, **payload}

    async def run_turn(self, run, *, text=("Reading them.", "\n\nFound it."), settle=None):
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "start", "runId": run,
                                        "hasActiveRun": True, "status": "running"})
        handled = [_turn_route_now(self.conn, self.ev(run, _delta(text[0])))]
        handled.append(_turn_route_now(self.conn, self.ev(run, _tool("start", tcid="toolu_1"))))
        handled.append(_turn_route_now(self.conn, self.ev(run, _tool("result", tcid="toolu_1"))))
        handled.append(_turn_route_now(self.conn, self.ev(run, _delta(text[1]))))
        handled.append(_turn_route_now(self.conn, self.ev(run, {"state": "final"})))
        self.conn._on_sessions_changed(settle or {"sessionKey": self.key, "hasActiveRun": False,
                                                  "status": "done", "lastRunId": run,
                                                  "reason": "chat.run.settled"})
        await self.spin()
        return handled

    def new_messages(self):
        return {k: v for k, v in self.owui.msgs().items() if k not in ("q1", "a1")}

    # ── Handover (DESIGN 2.6): a user turn whose request ends before its run ──

    STUB = "8acdce95-1111-4222-8333-444455556666"

    def add_stub(self, mid, **fields):
        msgs = self.owui.chat.chat["history"]["messages"]
        msgs["a1"]["childrenIds"] = [mid]
        msgs[mid] = {"id": mid, "role": "assistant", "parentId": "a1", "childrenIds": [],
                     "content": "", "output": [], "done": False, **fields}
        self.owui.chat.chat["history"]["currentId"] = mid

    async def dispatch(self, run, payload):
        """One event as the receive loop handles it: the shadow renderer, then routing."""
        shadow = self.conn._run_renderers.get(f"{self.key}:{run}")
        if shadow is not None:
            shadow.feed(payload)
        return _turn_route_now(self.conn, self.ev(run, payload))

    def crashed_then_recovered_history(self):
        """RUN streamed, the Gateway died, and boot recovered it as REC, which finished."""
        imported = {"importedFrom": "claude-cli"}
        return {"messages": [
            {"role": "user", "content": "x", "idempotencyKey": f"{self.RUN}:user"},
            {"role": "assistant", "__openclaw": imported,
             "content": [{"type": "text", "text": "Reading them."}]},
            {"role": "user", "content": "[System] continue", "idempotencyKey": f"{self.REC}:user",
             "provenance": {"kind": "internal_system", "sourceTool": "main_session_restart_recovery"}},
            {"role": "assistant", "__openclaw": imported, "content": [
                {"type": "toolcall", "id": "toolu_9", "name": "Bash", "arguments": {"command": "tail"}},
                {"type": "tool_result", "tool_use_id": "toolu_9", "name": "Bash",
                 "content": [{"type": "text", "text": "ok"}]}]},
            {"role": "assistant", "__openclaw": imported,
             "content": [{"type": "text", "text": "Found it."}]},
            {"role": "assistant", "idempotencyKey": f"cli-assistant:{self.REC}",
             "content": [{"type": "text", "text": "Found it."}]}]}


class AgentTurnRoutingTests(_RoutingCase):
    """ELI-100 DESIGN 2.1-2.5: a run on an OWUI key with no live request is written by a turn
    writer as one ordinary message."""

    async def test_a_run_with_no_settle_push_ends_on_its_own_final(self):
        """ELI-215 (2026.10.5): a Gateway-started run gets no `agent.input.settled` push, so its
        own final ends the message."""
        self.history = {"messages": [
            {"role": "user", "content": "System: exec completed :: e2e-bg-done"},
            {"role": "assistant", "idempotencyKey": f"cli-assistant:{self.RUN}",
             "__openclaw": {"runId": self.RUN}, "content": [{"type": "text", "text": "E2E_BG_NOTICED"}]}]}
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "start", "runId": self.RUN,
                                        "hasActiveRun": True, "status": "running"})
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("E2E_BG_NOTICED")))
        _turn_route_now(self.conn, self.ev(self.RUN, {"stream": "lifecycle", "data": {"phase": "end"}}))
        _turn_route_now(self.conn, self.ev(self.RUN, {"state": "final"}))
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "end", "runId": self.RUN,
                                        "hasActiveRun": True, "status": "running", "lastRunId": self.RUN})
        await self.spin()
        (_mid, m), = self.new_messages().items()
        self.assertEqual((m["content"], m["done"]), ("E2E_BG_NOTICED", True))
        # A later settle (9.3, or a chat.send run) finds it done.
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.RUN})
        await self.spin()
        (_mid, again), = self.new_messages().items()
        self.assertEqual((again["content"], again["done"]), ("E2E_BG_NOTICED", True))

    async def test_a_run_that_ends_in_error_still_waits_for_its_row(self):
        """Only a clean final ends the message on its own; an error's row may name a resume or
        recovery (DESIGN 2.7)."""
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "start", "runId": self.RUN,
                                        "hasActiveRun": True, "status": "running"})
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("partial")))
        _turn_route_now(self.conn, self.ev(self.RUN, {"state": "error", "errorMessage": "boom"}))
        await self.spin()
        (_mid, m), = self.new_messages().items()
        self.assertIs(m["done"], False)
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False, "status": "failed",
                                        "lastRunId": self.RUN, "lastRunError": "boom"})
        await self.spin()
        (_mid, m), = self.new_messages().items()
        self.assertIs(m["done"], True)
        self.assertIn("boom", m["content"])

    async def test_an_agent_turn_renders_as_one_ordinary_message(self):
        handled = await self.run_turn(self.RUN)
        self.assertTrue(all(handled))
        (mid, m), = self.new_messages().items()
        self.assertEqual(mid, turn_message_id(self.key, self.RUN, self.CHAT))
        self.assertEqual((m["parentId"], m["done"]), ("a1", True))
        self.assertEqual(m["content"], "Reading them.\n\nFound it.")
        self.assertEqual(sum(1 for i in m["output"] if i["type"] == "function_call"), 1)
        self.assertIn(("sessions.messages.subscribe", {"key": self.key}), self.sent,
                      "tool events of a run this socket did not start come as session.tool")

    async def test_a_recovery_continues_the_interrupted_message(self):
        # The run streamed, then the Gateway crashed: the row is running.
        self.history = {"messages": [
            {"role": "user", "content": "x", "idempotencyKey": f"{self.RUN}:user"},
            {"role": "user", "content": "[System] continue", "idempotencyKey": f"{self.REC}:user",
             "provenance": {"kind": "internal_system", "sourceTool": "main_session_restart_recovery"}}]}
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": True,
                                        "status": "running"})
        await self.spin()
        await self.run_turn(self.REC, text=("Again.", "\n\nFound it."),
                            settle={"sessionKey": self.key, "hasActiveRun": False, "status": "done",
                                    "lastRunId": self.REC, "reason": "chat.run.settled"})
        (mid, m), = self.new_messages().items()
        self.assertEqual(mid, turn_message_id(self.key, self.RUN, self.CHAT),
                         "rule E: the recovery writes into the interrupted run's message")
        self.assertIs(m["done"], True)
        self.assertIn("Again.", m["content"])

    async def test_a_recovery_that_ended_while_away_is_folded_in(self):
        """Case 4: a recovery that ran and ended while the socket was down. Its settle (or the row
        after hello) ends RUN's writer and the finalize takes the recovery by rule E."""
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        await self.spin()
        self.history = self.crashed_then_recovered_history()
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.REC})
        await self.spin()
        (mid, m), = self.new_messages().items()
        self.assertEqual(mid, turn_message_id(self.key, self.RUN, self.CHAT))
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Reading them.\n\nFound it.")
        self.assertEqual(sum(1 for i in m["output"] if i["type"] == "function_call"), 1,
                         "the recovery's tool card comes from the transcript")

    async def test_after_hello_an_idle_session_ends_its_open_writer(self):
        """The first hello's snapshot lists the session as idle, so its row is read and ends the
        writer; no wait for a push that already happened."""
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        await self.spin()
        self.history = self.crashed_then_recovered_history()
        row = {"key": self.key, "hasActiveRun": False, "status": "done", "lastRunId": self.REC}
        inner = self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "sessions.list":
                self.sent.append((method, params))
                return {"sessions": [row]}
            return await inner(method, params, timeout)
        self.conn.send_request = send_request
        self.conn.active_run_snapshot = {}
        await _turn_after_hello(self.conn)
        await self.spin()
        (_mid, m), = self.new_messages().items()
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Reading them.\n\nFound it.")

    async def test_after_hello_an_active_session_waits_for_its_settle(self):
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        await self.spin()
        self.conn.active_run_snapshot = {self.key: []}
        await _turn_after_hello(self.conn)
        await self.spin()
        self.assertNotIn("sessions.list", [m for m, _ in self.sent])
        (_mid, m), = self.new_messages().items()
        self.assertIs(m["done"], False, "the recovery still runs: M stays open for it")

    async def test_handover_keeps_the_requests_start_with_no_transcript(self):
        """With no transcript only the shadow renderer has what the request showed."""
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB, content="Reading them.")
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB)
        self.conn._run_renderers[f"{self.key}:{run}"].feed(_delta("Reading them."))
        self.assertTrue(await self.dispatch(run, _tool("start", tcid="toolu_1")))
        await self.spin()
        await self.dispatch(run, _tool("result", tcid="toolu_1"))
        await self.dispatch(run, _delta("\n\nFound it."))
        await self.dispatch(run, {"state": "final"})
        self.history = {"messages": []}
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": run})
        await self.spin()
        self.assertEqual(self.new_messages().keys(), {self.STUB}, "no second message")
        m = self.owui.msgs()[self.STUB]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Reading them.\n\nFound it.")

    async def test_the_handover_write_lands_after_the_requests_own_save(self):
        """ELI-115: M's handover write waits for the request's task to end, so its `done: true`
        save cannot land after it; the DB says `done: false` mid-turn."""
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB, content="Reading them.")
        request = asyncio.get_running_loop().create_future()
        self.owui.request_tasks["req-1"] = request
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB)
        self.conn._run_targets[f"{self.key}:{run}"]["request_task_id"] = "req-1"
        shown = self.conn._run_renderers[f"{self.key}:{run}"]
        shown.feed(_delta("Reading them."))
        self.assertTrue(await self.dispatch(run, _tool("start", tcid="toolu_1")))
        await self.spin()
        await self.dispatch(run, _tool("result", tcid="toolu_1"))
        await self.spin()
        # The request's save, then its task ends.
        await self.owui.end_request(self.STUB, copy.deepcopy(shown.output))
        self.assertIs(self.owui.msgs()[self.STUB]["done"], True)
        writes = len(self.owui.upserts)
        request.set_result(None)
        await self.spin()
        self.assertEqual(len(self.owui.upserts), writes + 1, "the handover's write, once")
        m = self.owui.msgs()[self.STUB]
        self.assertIs(m["done"], False)
        self.assertEqual(sum(1 for i in m["output"] if i["type"] == "function_call_output"), 1)
        await self.dispatch(run, _tool("start", tcid="toolu_2"))
        await self.dispatch(run, _tool("result", tcid="toolu_2"))
        await self.spin()
        self.assertEqual(len(self.owui.upserts), writes + 1, "then none mid-turn")

    async def test_a_reply_handed_over_while_its_message_waits_to_be_steered_stays_open(self):
        """A reply handed over while its message waits to be steered stays open, not done and
        empty, until its answer ends it."""
        self.conn.gateway_version = "2026.10.10"
        run = pipe_run_id(self.CHAT, self.STUB)
        pending = {"description": openclaw_pipe.STEER_PENDING_STATUS, "done": False}
        self.add_stub(self.STUB, statusHistory=[pending])
        request = asyncio.get_running_loop().create_future()
        self.owui.request_tasks["req-1"] = request
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB,
                                      request_task_id="req-1", steer_pending=True)
        self.assertTrue(openclaw_pipe._hand_over_run(self.conn, self.key, run))
        await self.spin()
        patches = lambda: [d["data"]["messages"][self.STUB]  # noqa: E731
                           for d in self.owui.events("chat:messages") if self.STUB in d["data"]["messages"]]
        self.assertEqual(patches(), [], "not before the request's end, whose own done would win in the tab")
        await self.owui.end_request(self.STUB, [])
        request.set_result(None)
        await self.spin()
        m = self.owui.msgs()[self.STUB]
        self.assertIs(m["done"], False, "the DB says the reply goes on")
        shown = patches()
        self.assertTrue(shown and shown[-1]["done"] is False, "open tabs show it going on")
        self.assertEqual(shown[-1].get("statusHistory"), [pending], "with the line it waits under")
        # The run takes the message in at its end.
        self.history = {"messages": [
            {"role": "user", "content": "count", "__openclaw": {"idempotencyKey": "R1:user"}},
            {"role": "assistant", "content": [{"type": "text", "text": "slow-1 slow-2"}],
             "stopReason": "stop", "__openclaw": {"runId": "R1"}},
            {"role": "user", "content": "aside", "__openclaw": {"idempotencyKey": f"{run}:user"}},
            {"role": "assistant", "content": [{"type": "text", "text": "the aside"}],
             "stopReason": "stop", "__openclaw": {"runId": "R1"}}]}
        await self.dispatch(run, {"state": "final"})
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": "R1"})
        await self.spin()
        m = self.owui.msgs()[self.STUB]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "the aside")

    async def test_handover_an_inline_reply_that_ends_early_continues_in_its_own_message(self):
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB, content="Reading them.")
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB)
        self.conn._run_renderers[f"{self.key}:{run}"].feed(_delta("Reading them."))
        # The request is gone: the run's next events.
        self.assertTrue(await self.dispatch(run, _tool("start", tcid="toolu_1")))
        await self.dispatch(run, _tool("result", tcid="toolu_1"))
        await self.dispatch(run, _delta("\n\nFound it."))
        await self.dispatch(run, {"state": "final"})
        self.history = _turn_history(run=run)
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": run})
        await self.spin()
        self.assertEqual(self.new_messages().keys(), {self.STUB}, "no second message")
        m = self.owui.msgs()[self.STUB]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Reading them.\n\nFound it.")
        self.assertEqual(sum(1 for i in m["output"] if i["type"] == "function_call"), 1)
        self.assertEqual(m["meta"]["openclaw_writer"], self.conn.writer_id)
        self.assertNotIn(f"{self.key}:{run}", self.conn._run_targets, "the shadow is retired")

    async def test_a_handed_over_adopted_run_does_not_repeat_what_the_first_reply_showed(self):
        """ELI-147: a handed-over adopting reply rebuilds from where reply 1 left off."""
        def lines(a, b):
            return "".join(f"{i}\n" for i in range(a, b + 1))
        run = pipe_run_id(self.CHAT, "m-first")       # named by reply 1's message
        self.add_stub(self.STUB, content=lines(11, 20))
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB)
        self.conn.seed_run_renderer(self.key, run, asyncio.Queue(),
                                    run_text={"received": lines(1, 10), "stream": lines(1, 10),
                                              "item": ""})
        self.conn._run_renderers[f"{self.key}:{run}"].feed(_delta(lines(11, 20)))
        self.assertTrue(await self.dispatch(run, _delta(lines(21, 25))))
        await self.dispatch(run, {"state": "final"})
        self.history = {"messages": [
            {"role": "user", "content": "count to 30", "idempotencyKey": f"{run}:user"},
            {"role": "assistant", "idempotencyKey": f"cli-assistant:{run}",
             "content": [{"type": "text", "text": lines(1, 30)}]}]}
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": run})
        await self.spin()
        m = self.owui.msgs()[self.STUB]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"].strip(), lines(11, 30).strip(),
                         "lines 1-10 are reply 1's; 26-30 are what the transcript adds")

    async def test_handover_after_an_owui_restart_takes_the_stub_its_run_id_names(self):
        """After an Open WebUI restart (no request, chat.history failing) the run id names its
        stub; no second message is introduced."""
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB)
        inner = self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "chat.history":
                raise asyncio.TimeoutError()
            return await inner(method, params, timeout)
        self.conn.send_request = send_request
        self.assertTrue(await self.dispatch(run, _delta("Found it.")))
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": run})
        await self.spin()
        self.assertEqual(self.new_messages().keys(), {self.STUB}, "no second message next to the stub")
        m = self.owui.msgs()[self.STUB]
        self.assertEqual((m["content"], m["done"]), ("Found it.", True))

    async def test_handover_never_takes_a_message_another_live_writer_holds(self):
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB, content="theirs", meta={"openclaw_writer": "other-writer"})
        _register_turn_writer("other-writer")
        self.addCleanup(_retire_turn_writer, "other-writer")
        await self.dispatch(run, _delta("mine"))
        await self.spin()
        m = self.owui.msgs()[self.STUB]
        self.assertEqual((m["content"], m["meta"]["openclaw_writer"]), ("theirs", "other-writer"))
        self.assertEqual(self.new_messages().keys(), {self.STUB})

    async def test_a_late_event_of_a_run_shown_live_reopens_nothing(self):
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB, content="Done.", done=True)
        self.conn.mark_delivered_live(self.key, run)
        self.assertTrue(await self.dispatch(run, _tool("result", tcid="toolu_1")))
        await self.spin()
        self.assertEqual(self.owui.msgs()[self.STUB]["done"], True)
        self.assertEqual(self.conn._turn_pumps, {})

    async def test_a_heartbeat_run_never_gets_a_message(self):
        """Condition B: a run whose agent events carry isHeartbeat:true renders nothing, including
        its unflagged later events (session.tool)."""
        hb = {**_delta("HEARTBEAT check"), "isHeartbeat": True}
        self.assertTrue(_turn_route_now(self.conn, self.ev(self.RUN, hb)))
        self.assertTrue(_turn_route_now(self.conn, self.ev(self.RUN, _tool("start", tcid="t"))))
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.RUN})
        await self.spin()
        self.assertEqual(self.new_messages(), {})
        self.assertEqual(self.conn._turn_pumps, {})

    async def test_a_run_a_live_request_consumes_is_not_routed(self):
        q = self.conn.register_consumer(self.key, "msg-run")
        self.addCleanup(self.conn.unregister_consumer, self.key, "msg-run", queue=q)
        self.assertFalse(_turn_route_now(self.conn, self.ev("msg-run", _delta("hi"))))

    async def test_an_answer_run_waits_for_the_reply_streaming_its_message(self):
        """An answer run's early event, before its reply registers, is held and reaches the reply's
        queue when it registers."""
        q_run = pipe_run_id(self.CHAT, self.STUB)
        live = self.conn.register_consumer(self.key, q_run)
        self.addCleanup(self.conn.unregister_consumer, self.key, q_run, queue=live)
        a_run = pipe_run_id(self.CHAT, self.STUB, "input")
        self.assertTrue(_turn_route_now(self.conn, self.ev(a_run, _delta("Reading a.py."))))
        self.assertEqual(self.conn._turn_pumps, {})
        q = self.conn.register_consumer(self.key, a_run)
        self.addCleanup(self.conn.unregister_consumer, self.key, a_run, queue=q)
        # As a frame: the inline loop reads `msg["payload"]`.
        frame = q.get_nowait()
        self.assertEqual((frame["type"], frame["event"]), ("event", "agent"))
        self.assertEqual(frame["payload"]["data"]["delta"], "Reading a.py.")

    async def test_a_held_run_is_a_turn_again_when_the_reply_ends_without_it(self):
        self.add_stub(self.STUB, content="Which file?")
        q_run = pipe_run_id(self.CHAT, self.STUB)
        live = self.conn.register_consumer(self.key, q_run)
        a_run = pipe_run_id(self.CHAT, self.STUB, "input")
        _turn_route_now(self.conn, self.ev(a_run, _delta("Reading a.py.")))
        self.conn.unregister_consumer(self.key, q_run, queue=live)
        await self.spin()
        self.assertIn((self.key, a_run), self.conn._turn_pumps)
        self.assertEqual(self.conn._held_for_live, {})

    async def test_held_events_drain_before_the_runs_next_event(self):
        """Held events drain before the run's next event."""
        self.add_stub(self.STUB, content="")
        q_run = pipe_run_id(self.CHAT, self.STUB)
        self.conn.register_consumer(self.key, q_run)
        r_run = pipe_run_id(self.CHAT, self.STUB, "resume")
        for text in ("One.", " Two."):
            _turn_route_now(self.conn, self.ev(r_run, _delta(text)))
        self.conn.unregister_consumer(self.key, q_run)
        _turn_route_now(self.conn, self.ev(r_run, _delta(" Three.")))
        pump = self.conn._turn_pumps[(self.key, r_run)]
        queued = []
        while not pump.queue.empty():
            queued.append(pump.queue.get_nowait()[1]["data"]["delta"])
        self.assertEqual(queued, ["One.", " Two.", " Three."])
        self.assertEqual(self.conn._held_for_live, {})

    async def test_held_events_reach_the_reply_that_registers_right_after_the_release(self):
        """The ask-user switch unregisters the question run and registers the answer run in one
        step; the release between must not lose held events."""
        q_run = pipe_run_id(self.CHAT, self.STUB)
        self.conn.register_consumer(self.key, q_run)
        a_run = pipe_run_id(self.CHAT, self.STUB, "input")
        for text in ("Reading", " a.py."):
            _turn_route_now(self.conn, self.ev(a_run, _delta(text)))
        self.conn.unregister_consumer(self.key, q_run)
        q = self.conn.register_consumer(self.key, a_run)
        self.addCleanup(self.conn.unregister_consumer, self.key, a_run, queue=q)
        await self.spin()
        self.assertEqual([q.get_nowait()["payload"]["data"]["delta"] for _ in range(q.qsize())],
                         ["Reading", " a.py."])
        self.assertEqual(self.conn._turn_pumps, {})

    async def test_held_events_for_a_registered_consumer_are_counted_and_still_delivered(self):
        """Unreachable through register_consumer; forced so the guard counts and delivers."""
        a_run = pipe_run_id(self.CHAT, self.STUB, "input")
        q = self.conn.register_consumer(self.key, a_run)
        self.addCleanup(self.conn.unregister_consumer, self.key, a_run, queue=q)
        self.conn._held_for_live[(self.key, a_run)] = [self.ev(a_run, _delta("late"))]
        self.conn.drain_held(self.key, a_run)
        self.assertEqual(self.conn.unreachable_held_drains, 1)
        self.assertEqual(q.get_nowait()["payload"]["data"]["delta"], "late")
        self.assertEqual(self.conn._turn_pumps, {})

    async def test_another_messages_run_is_not_held(self):
        q = self.conn.register_consumer(self.key, pipe_run_id(self.CHAT, "other-message"))
        self.addCleanup(self.conn.unregister_consumer, self.key,
                        pipe_run_id(self.CHAT, "other-message"), queue=q)
        self.assertFalse(self.conn.live_reply_owns(self.key, pipe_run_id(self.CHAT, self.STUB, "input")))
        self.assertFalse(self.conn.live_reply_owns(self.key, pipe_run_id(self.CHAT, "other-message")))

    async def test_the_action_connection_never_writes_turns(self):
        conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"), writes_turns=False)
        self.assertFalse(_turn_route_now(conn, self.ev(self.RUN, _delta("hi"))))

    async def test_a_retired_connection_writes_no_turns(self):
        self.conn._retired = True
        self.assertFalse(_turn_route_now(self.conn, self.ev(self.RUN, _delta("hi"))))
        self.assertEqual(self.conn._turn_pumps, {})

    def test_an_interrupted_runs_span_ends_at_the_next_admitted_turn(self):
        hist = self.crashed_then_recovered_history()
        span = _history_span_messages(hist, self.RUN)
        self.assertEqual([m.get("content") for m in span if m["role"] == "assistant"],
                         [[{"type": "text", "text": "Reading them."}]])
        self.assertEqual(len(_history_span_messages(hist, self.REC)), 3)
        self.assertIsNone(_history_span_messages(
            {"messages": hist["messages"][:2]}, self.RUN), "still the newest turn: not over")

    async def test_the_status_actions_runs_are_swallowed(self):
        self.assertTrue(_turn_route_now(
            self.conn, self.ev(f"{ACTION_RUN_PREFIX}compact-x-1.0", _delta("Compacted."))))
        await self.spin()
        self.assertEqual(self.new_messages(), {})


class RegressionPlanTests(_RoutingCase):
    """DESIGN section 6: each fails on main 787a471 (no writer there) and passes here."""

    def tool_history(self, run, n, text="Done."):
        """A closed run whose transcript holds `n` tool calls."""
        imported = {"importedFrom": "claude-cli"}
        msgs = [{"role": "user", "content": "go", "idempotencyKey": f"{run}:user"}]
        for i in range(n):
            msgs.append({"role": "assistant", "__openclaw": imported, "content": [
                {"type": "toolcall", "id": f"toolu_{i}", "name": "Bash", "arguments": {"command": f"c{i}"}},
                {"type": "tool_result", "tool_use_id": f"toolu_{i}", "name": "Bash",
                 "content": [{"type": "text", "text": "ok"}]}]})
        msgs += [{"role": "assistant", "__openclaw": imported, "content": [{"type": "text", "text": text}]},
                 {"role": "assistant", "idempotencyKey": f"cli-assistant:{run}",
                  "content": [{"type": "text", "text": text}]}]
        return {"messages": msgs}

    def cards(self, m):
        return sum(1 for i in m["output"] if i["type"] == "function_call")

    def settled(self, run, **row):
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "lastRunId": run, **row})

    async def scripted_loop(self, conn, frames):
        await _play_frames(conn, frames)
        await self.spin()

    async def test_two_connections_one_message(self):
        """A deploy's old and new connections get one run; the row lock lets one claim M."""
        other = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        other.send_request = self.conn.send_request
        for conn in (self.conn, other):
            _register_turn_writer(conn.writer_id)
            self.addCleanup(_retire_turn_writer, conn.writer_id)
        for payload in (_delta("Reading them."), _tool("start", tcid="toolu_1"),
                        _tool("result", tcid="toolu_1"), _delta("\n\nFound it."), {"state": "final"}):
            for conn in (self.conn, other):
                self.assertTrue(_turn_route_now(conn, self.ev(self.RUN, payload)))
        await self.spin()
        # uuid5 gives one message even if both wrote it, so count the writers' own effects.
        n_tasks = len(self.owui.tasks)
        # An unfinished M's introductions: the end sends it once more, finished (ELI-230).
        n_intro = sum(1 for d in self.owui.events("chat:messages")
                      if not d["data"]["messages"][d["data"]["currentId"]].get("done"))
        for conn in (self.conn, other):
            conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                       "status": "done", "lastRunId": self.RUN})
        await self.spin()
        (_mid, m), = self.new_messages().items()
        self.assertEqual((m["content"], m["done"], self.cards(m)), ("Reading them.\n\nFound it.", True, 1))
        self.assertEqual(n_tasks, 1, "one Open WebUI task: only one connection writes M")
        self.assertEqual(n_intro, 1, "M introduced once")
        self.assertEqual(len(self.owui.events("chat:completion")), 1, "M finalized once")

    async def test_tool_cards_from_the_transcript(self):
        """Events after the first tool call were lost; M ends with the transcript's cards."""
        _turn_route_now(self.conn, self.ev(self.RUN, _tool("start", tcid="toolu_0")))
        self.history = self.tool_history(self.RUN, 3)
        self.settled(self.RUN, status="done")
        await self.spin()
        (_mid, m), = self.new_messages().items()
        self.assertEqual((self.cards(m), m["content"], m["done"]), (3, "Done.", True))

    async def test_user_turn_with_no_tab(self):
        """A user turn via the API whose request ended before its run: one message M, 5 cards."""
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB)
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB)
        await self.dispatch(run, _tool("start", tcid="toolu_0"))
        self.history = self.tool_history(run, 5)
        self.settled(run, status="done")
        await self.spin()
        self.assertEqual(self.new_messages().keys(), {self.STUB})
        m = self.owui.msgs()[self.STUB]
        self.assertEqual((self.cards(m), m["done"]), (5, True))

    async def owner_case(self, settled_row, *, stop_notice=False, terminal="error"):
        """The run streams, ends with the active-run push, then the settled row (DESIGN 2.7)."""
        self.sent_send = []
        inner = self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "chat.send":
                self.sent_send.append(params)
                return {"runId": params["idempotencyKey"], "status": "started"}
            return await inner(method, params, timeout)
        self.conn.send_request = send_request
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        self.conn._stop_notice_seen = stop_notice
        _turn_route_now(self.conn, self.ev(self.RUN, {"state": terminal}))
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "end", "runId": self.RUN,
                                        "status": "running", "hasActiveRun": True})
        self.history = {"messages": []}
        self.settled(self.RUN, **settled_row)
        await self.spin()
        (_mid, m), = self.new_messages().items()
        return m

    async def test_owner_rule_container_stop_resumes_into_m(self):
        m = await self.owner_case({"status": "failed",
                                   "lastRunError": "Claude Code process exited with code 143"})
        (send,) = self.sent_send
        self.assertTrue(send["idempotencyKey"].startswith(
            f"msg-{self.CHAT}-{turn_message_id(self.key, self.RUN, self.CHAT)}-resume-"))
        self.assertIs(m["done"], False)

    async def test_owner_rule_restart_mode_leaves_m_to_the_recovery(self):
        m = await self.owner_case({"status": "running", "abortedLastRun": True})
        self.assertEqual(self.sent_send, [])
        self.assertIs(m["done"], False)

    async def test_owner_rule_gateway_stop_resumes(self):
        m = await self.owner_case({"status": "killed"}, stop_notice=True, terminal="aborted")
        self.assertEqual(len(self.sent_send), 1)
        self.assertIs(m["done"], False)

    async def test_owner_rule_user_stop_and_real_errors_end_m(self):
        for row in ({"status": "killed"},
                    {"status": "failed", "lastRunError": "rate limited"},
                    {"status": "failed", "lastRunError": "Claude Code process exited with code 1"}):
            with self.subTest(row=row):
                await self.asyncTearDown() if hasattr(self, "asyncTearDown") else None
                await self.asyncSetUp()
                m = await self.owner_case(row, terminal="aborted" if row["status"] == "killed" else "error")
                self.assertEqual(self.sent_send, [])
                self.assertIs(m["done"], True)

    async def test_owner_rule_crash_sends_no_resume(self):
        """No terminal; after hello the session is active (recovery running): nothing is sent and M
        stays open. The fake accepts chat.send, so a resume would show."""
        row = {"key": self.key, "status": "running", "hasActiveRun": True}
        inner = self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "sessions.list":
                self.sent.append((method, params))
                return {"sessions": [row]}
            if method == "chat.send":
                self.sent.append((method, params))
                return {"runId": params["idempotencyKey"], "status": "started"}
            return await inner(method, params, timeout)
        self.conn.send_request = send_request
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        self.conn.active_run_snapshot = {self.key: []}
        await _turn_after_hello(self.conn)
        await self.spin()
        self.assertNotIn("sessions.list", [m for m, _ in self.sent], "the active snapshot is honoured")
        self.assertNotIn("chat.send", [m for m, _ in self.sent])
        (_mid, m), = self.new_messages().items()
        self.assertIs(m["done"], False)

    async def test_crash_double_continuation(self):
        """A crash on a user turn: no pipe resume, and M gets the recovery's card and text."""
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        self.conn.active_run_snapshot = {self.key: []}
        await _turn_after_hello(self.conn)
        self.history = self.crashed_then_recovered_history()
        for payload in (_tool("start", tcid="toolu_9"), _tool("result", tcid="toolu_9"),
                        _delta("\n\nFound it."), {"state": "final"}):
            _turn_route_now(self.conn, self.ev(self.REC, payload))
        self.settled(self.REC, status="done")
        await self.spin()
        self.assertNotIn("chat.send", [m for m, _ in self.sent])
        (mid, m), = self.new_messages().items()
        self.assertEqual(mid, turn_message_id(self.key, self.RUN, self.CHAT))
        self.assertEqual((m["content"], self.cards(m), m["done"]), ("Reading them.\n\nFound it.", 1, True))

    async def test_a_frame_seq_gap_resyncs_open_writers(self):
        """Frames dropped for a slow client show only in the seq: a dropped settle reads the row; a
        dropped start opens on the next event."""
        row = {"key": self.key, "hasActiveRun": False, "status": "done", "lastRunId": self.RUN}
        inner = self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "sessions.list":
                self.sent.append((method, params))
                return {"sessions": [row]}
            return await inner(method, params, timeout)
        self.conn.send_request = send_request
        self.history = _turn_history(run=self.RUN)
        ev = lambda seq, payload: {"type": "event", "event": "agent", "seq": seq,
                                   "payload": self.ev(self.RUN, payload)}
        await self.scripted_loop(self.conn, [
            ev(7, _delta("Reading them.")),          # seq 5-6 (the start push) dropped
            ev(8, _tool("start", tcid="toolu_1")),
            ev(9, {"state": "final"}),
            {"type": "event", "event": "tick", "seq": 11, "payload": {}},  # 10 (settle) dropped
        ])
        self.assertIn("sessions.list", [m for m, _ in self.sent])
        (_mid, m), = self.new_messages().items()
        self.assertEqual((m["content"], m["done"]), ("Reading them.\n\nFound it.", True))

    async def test_a_heartbeat_runs_session_tool_events_open_nothing(self):
        """A heartbeat run's unflagged session.tool copies must not open M."""
        hb = {"type": "event", "event": "agent",
              "payload": {**self.ev(self.RUN, _delta("checking")), "isHeartbeat": True}}
        tool = {"type": "event", "event": "session.tool",
                "payload": self.ev(self.RUN, _tool("start", tcid="toolu_1"))}
        await self.scripted_loop(self.conn, [hb, tool])
        self.assertEqual(self.new_messages(), {})

    # A heartbeat run is known only by isHeartbeat on its agent events. Its chat events and
    # session.tool copies never carry it and can arrive first.

    def hb_frame(self, payload):
        return {"type": "event", "event": "agent",
                "payload": {**self.ev(self.RUN, payload), "isHeartbeat": True}}

    def frame(self, event, payload):
        return {"type": "event", "event": event, "payload": self.ev(self.RUN, payload)}

    async def test_a_heartbeat_whose_session_tool_comes_first_opens_nothing(self):
        await self.scripted_loop(self.conn, [
            self.frame("session.tool", _tool("start", tcid="toolu_1")),
            self.hb_frame({"stream": "lifecycle", "data": {"phase": "start"}}),
            self.frame("session.tool", _tool("result", tcid="toolu_1")),
            self.hb_frame({"stream": "lifecycle", "data": {"phase": "end"}}),
            self.frame("chat", {"state": "final"}),
        ])
        self.settled(self.RUN, status="done")
        await self.spin()
        self.assertEqual(self.new_messages(), {})
        self.assertEqual(self.conn._turn_pumps, {})

    async def test_a_heartbeat_whose_chat_delta_comes_first_opens_nothing(self):
        await self.scripted_loop(self.conn, [
            self.frame("chat", {"state": "delta", "message": {
                "role": "assistant", "content": [{"type": "text", "text": "Checking the inbox."}]}}),
            self.hb_frame(_delta("Checking the inbox.")),
            self.hb_frame({"stream": "lifecycle", "data": {"phase": "end"}}),
            self.frame("chat", {"state": "final"}),
        ])
        self.settled(self.RUN, status="done")
        await self.spin()
        self.assertEqual(self.new_messages(), {})

    async def test_an_agent_turn_whose_session_tool_comes_first_keeps_its_order(self):
        """An unflagged first frame is held, not dropped, and goes out first once the run is known
        not to be a heartbeat."""
        self.history = self.tool_history(self.RUN, 1, text="Found it.")
        await self.scripted_loop(self.conn, [
            self.frame("session.tool", _tool("start", tcid="toolu_0")),
            self.frame("agent", _delta("Found it.")),
            self.frame("session.tool", _tool("result", tcid="toolu_0")),
            self.frame("chat", {"state": "final"}),
        ])
        self.settled(self.RUN, status="done")
        await self.spin()
        (_mid, m), = self.new_messages().items()
        self.assertEqual((self.cards(m), m["content"], m["done"]), (1, "Found it.", True))
        self.assertEqual([i["type"] for i in m["output"]][0], "function_call", "the held card is first")

    async def test_an_agent_turn_seen_only_as_tool_copies_still_ends_with_its_message(self):
        """Only session.tool copies arrived (after a reconnect); the settled row releases them."""
        self.history = self.tool_history(self.RUN, 2)
        await self.scripted_loop(self.conn, [
            self.frame("session.tool", _tool("start", tcid="toolu_0")),
            self.frame("session.tool", _tool("result", tcid="toolu_0")),
        ])
        self.settled(self.RUN, status="done")
        await self.spin()
        (_mid, m), = self.new_messages().items()
        self.assertEqual((self.cards(m), m["content"], m["done"]), (2, "Done.", True))


class ShutdownHandoverTests(_RoutingCase):
    """ELI-173: on `docker stop` the next process's startup reconcile owns every reply (ELI-90,
    ELI-94); a dying process writes none."""

    SAVED = "E2E_DOWN slow-1"

    def stopped_reply(self):
        """The reply Open WebUI's shutdown cancelled and saved."""
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB, content=self.SAVED, done=True,
                      statusHistory=[{"description": "Stopped", "done": True}])
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB)
        self.conn._run_renderers[f"{self.key}:{run}"].feed(_delta(self.SAVED))
        return run

    async def test_a_word_after_the_shutdown_cancel_leaves_the_saved_reply(self):
        run = self.stopped_reply()
        with mock.patch.object(openclaw_pipe, "_UVICORN_SERVER", types.SimpleNamespace(should_exit=True)):
            await self.dispatch(run, _delta(" slow-2"))
            await self.spin()
        m = self.owui.msgs()[self.STUB]
        self.assertEqual((m["content"], m["done"]), (self.SAVED, True))
        self.assertEqual(self.conn._turn_pumps, {}, "no turn writer in a process that is shutting down")

    async def test_while_serving_the_same_word_is_handed_to_a_writer(self):
        """Control: the handover is real (DESIGN 2.6)."""
        run = self.stopped_reply()
        await self.dispatch(run, _delta(" slow-2"))
        await self.spin()
        self.assertIn((self.key, run), self.conn._turn_pumps)
        self.assertIs(self.owui.msgs()[self.STUB]["done"], False)


class HandoverRewriteTests(_RoutingCase):
    """A handed-over reply ends as the inline reply showed it: `MEDIA:` directives as images,
    answered prompts as cards."""

    PROMPT = "OpenClaw needs input:\n\nColor?\nPick one\n1. Red\n2. Blue"
    CARD = ('\n<details type="tool_calls" done="true" id="ask-user-abc" name="Ask User" '
            'arguments="{}" result="Blue">\n<summary>Ask User</summary>\n</details>\n')

    def closed_run(self, run, text, user="go"):
        imported = {"importedFrom": "claude-cli"}
        return [{"role": "user", "content": user, "idempotencyKey": f"{run}:user"},
                {"role": "assistant", "__openclaw": imported, "content": [{"type": "text", "text": text}]},
                {"role": "assistant", "idempotencyKey": f"cli-assistant:{run}",
                 "content": [{"type": "text", "text": text}]}]

    async def settle(self, run):
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": run})
        await self.spin()

    async def test_a_handover_keeps_an_image_the_reply_resolved(self):
        run = pipe_run_id(self.CHAT, self.STUB)
        raw, shown = "Here: MEDIA:shot.png\n", "Here: ![shot.png](/api/v1/files/f1/content)\n"
        self.add_stub(self.STUB, content=shown)
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB)
        self.conn._run_renderers[f"{self.key}:{run}"].feed(_delta(raw))
        self.conn.note_turn_rewrite(self.CHAT, self.STUB, raw, shown)
        await self.dispatch(run, _delta("Done."))
        await self.spin()
        live = self.owui.streams[-1][2]
        self.assertIn("![shot.png](/api/v1/files/f1/content)", live, "the live view too")
        await self.dispatch(run, {"state": "final"})
        self.history = {"messages": self.closed_run(run, raw + "Done.")}
        await self.settle(run)
        m = self.owui.msgs()[self.STUB]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], shown + "Done.")
        self.assertNotIn("MEDIA:", json.dumps(m["output"]))

    async def test_an_image_that_arrives_after_the_handover_is_resolved_once(self):
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB, content="Look.")
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB)
        self.conn._run_renderers[f"{self.key}:{run}"].feed(_delta("Look. "))
        calls = []

        async def resolve(text, **kw):
            calls.append(text)
            return text.replace("MEDIA:late.png", "![late.png](/api/v1/files/f2/content)"), True
        with mock.patch.object(openclaw_pipe, "_resolve_media_text", resolve):
            await self.dispatch(run, _delta("MEDIA:la"))
            await self.spin()
            self.assertNotIn("MEDIA:la", self.owui.streams[-1][2], "a partial directive is held back")
            await self.dispatch(run, _delta("te.png\nNice."))
            await self.dispatch(run, {"state": "final"})
            self.history = {"messages": self.closed_run(run, "Look. MEDIA:late.png\nNice.")}
            await self.settle(run)
        m = self.owui.msgs()[self.STUB]
        self.assertEqual(m["content"], "Look. ![late.png](/api/v1/files/f2/content)\nNice.")
        self.assertEqual(calls, ["MEDIA:late.png"], "uploaded once, not on every write")

    async def test_an_answered_question_card_survives_a_handover_of_the_answer_run(self):
        """A handover of the answer run keeps the question run's text and card."""
        run1 = pipe_run_id(self.CHAT, self.STUB)
        run2 = pipe_run_id(self.CHAT, self.STUB, "input")
        self.add_stub(self.STUB, content="Let me ask.\n" + self.CARD)
        self.conn.register_run_target(self.key, run1, self.CHAT, self.STUB)
        shadow = self.conn._run_renderers[f"{self.key}:{run1}"]
        shadow.feed(_delta("Let me ask.\n"))
        shadow.feed(_delta(self.PROMPT))
        self.conn.note_turn_rewrite(self.CHAT, self.STUB, self.PROMPT, self.CARD)
        self.conn.continue_run_target(self.key, run1, run2)
        await self.dispatch(run2, _delta("Blue it is."))
        await self.spin()
        self.assertEqual(self.owui.streams[-1][2], "Let me ask.\n" + self.CARD + "Blue it is.",
                         "live, the answer run continues the reply the page shows")
        await self.dispatch(run2, {"state": "final"})
        self.history = {"messages": self.closed_run(run1, "Let me ask.\n" + self.PROMPT)
                        + self.closed_run(run2, "Blue it is.", user="2")}
        await self.settle(run2)
        self.assertEqual(self.new_messages().keys(), {self.STUB}, "no second message")
        m = self.owui.msgs()[self.STUB]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Let me ask.\n" + self.CARD + "Blue it is.")

    async def test_the_question_runs_late_events_open_nothing_after_the_switch(self):
        """The question run's trailing final must not hand M over while the reply streams."""
        run1 = pipe_run_id(self.CHAT, self.STUB)
        run2 = pipe_run_id(self.CHAT, self.STUB, "input")
        self.add_stub(self.STUB)
        self.conn.register_run_target(self.key, run1, self.CHAT, self.STUB)
        self.conn._run_renderers[f"{self.key}:{run1}"].feed(_delta("Let me ask."))
        self.conn.continue_run_target(self.key, run1, run2)
        self.assertTrue(await self.dispatch(run1, {"state": "final"}))
        await self.spin()
        self.assertNotIn((self.key, run1), self.conn._turn_pumps)
        self.assertNotIn("meta", self.owui.msgs()[self.STUB], "nobody claimed M")
        self.assertIn(f"{self.key}:{run2}", self.conn._run_targets)

    async def test_after_an_owui_restart_the_answer_run_keeps_the_question_run(self):
        """After an Open WebUI restart the transcript names every pipe run of M, in order."""
        run1 = pipe_run_id(self.CHAT, self.STUB)
        run2 = pipe_run_id(self.CHAT, self.STUB, "input")
        self.add_stub(self.STUB, content="Let me ask.")
        self.history = {"messages": self.closed_run(run1, "Let me ask.")
                        + self.closed_run(run2, " Blue it is.", user="2")}
        await self.dispatch(run2, _delta(" Blue it is."))
        await self.dispatch(run2, {"state": "final"})
        await self.settle(run2)
        m = self.owui.msgs()[self.STUB]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Let me ask. Blue it is.")

    # After an Open WebUI restart the card is rebuilt from the transcript: the prompt ends the
    # question run's text, the answer is the answer run's user turn (#20).

    def card_answer(self, content):
        hit = re.search(r'<details type="tool_calls" done="true" id="ask-user-[0-9a-f]+" '
                        r'name="Ask User" arguments="([^"]*)" result="([^"]*)"', content)
        return (html.unescape(hit.group(1)), html.unescape(hit.group(2))) if hit else None

    async def test_after_an_owui_restart_an_answered_question_is_still_a_card(self):
        run1 = pipe_run_id(self.CHAT, self.STUB)
        run2 = pipe_run_id(self.CHAT, self.STUB, "input")
        self.add_stub(self.STUB, content="Let me ask.\n")
        self.history = {"messages": self.closed_run(run1, "Let me ask.\n" + self.PROMPT)
                        + self.closed_run(run2, "Blue it is.", user="2. Blue")}
        await self.dispatch(run2, _delta("Blue it is."))
        await self.dispatch(run2, {"state": "final"})
        await self.settle(run2)
        m = self.owui.msgs()[self.STUB]
        self.assertIs(m["done"], True)
        self.assertNotIn("needs input", m["content"])
        self.assertEqual(self.card_answer(m["content"]),
                         ('{"question": "Color?\\nPick one\\n1. Red\\n2. Blue"}', "2. Blue"))
        self.assertTrue(m["content"].startswith("Let me ask.\n"))
        self.assertTrue(m["content"].endswith("Blue it is."))
        self.assertNotIn("needs input", json.dumps(m["output"]))

    async def test_a_quoted_prompt_in_a_code_fence_is_no_card_after_a_restart(self):
        """A prompt quoted in a code fence asked nothing (ELI-107)."""
        run1 = pipe_run_id(self.CHAT, self.STUB)
        run2 = pipe_run_id(self.CHAT, self.STUB, "input")
        quoted = "Report:\n```\n" + self.PROMPT + "\n```\n"
        self.add_stub(self.STUB, content=quoted)
        self.history = {"messages": self.closed_run(run1, quoted)
                        + self.closed_run(run2, "Noted.", user="go on")}
        await self.dispatch(run2, _delta("Noted."))
        await self.dispatch(run2, {"state": "final"})
        await self.settle(run2)
        m = self.owui.msgs()[self.STUB]
        self.assertNotIn("Ask User", m["content"])
        self.assertEqual(m["content"], quoted + "Noted.")

    async def test_a_card_the_reply_recorded_is_kept_as_it_was(self):
        """In the same process the inline reply's own card (its id) stays."""
        run1 = pipe_run_id(self.CHAT, self.STUB)
        run2 = pipe_run_id(self.CHAT, self.STUB, "input")
        self.add_stub(self.STUB, content="Let me ask.\n" + self.CARD)
        self.conn.note_turn_rewrite(self.CHAT, self.STUB, self.PROMPT, self.CARD)
        self.history = {"messages": self.closed_run(run1, "Let me ask.\n" + self.PROMPT)
                        + self.closed_run(run2, "Blue it is.", user="2. Blue")}
        await self.dispatch(run2, _delta("Blue it is."))
        await self.dispatch(run2, {"state": "final"})
        await self.settle(run2)
        self.assertEqual(self.owui.msgs()[self.STUB]["content"], "Let me ask.\n" + self.CARD + "Blue it is.")

    async def test_a_question_nobody_answered_shows_without_its_marker_line(self):
        """A writer shows an agent turn's prompt without its directive line (ELI-58)."""
        self.history = {"messages": self.closed_run(self.RUN, "Before I go on:\n" + self.PROMPT)}
        await self.dispatch(self.RUN, _delta("Before I go on:\n" + self.PROMPT))
        await self.dispatch(self.RUN, {"state": "final"})
        await self.settle(self.RUN)
        (_mid, m), = self.new_messages().items()
        self.assertEqual(m["content"], "Before I go on:\nColor?\nPick one\n1. Red\n2. Blue")

    async def test_a_routed_handover_keeps_the_turns_thinking_level(self):
        """`_turn_route_now`'s handover carries the thinking level like `_hand_over_run`."""
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB)
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB, thinking="high")
        self.conn._run_renderers[f"{self.key}:{run}"].feed(_delta("Reading."))
        await self.dispatch(run, _delta(" More."))
        await self.spin()
        self.assertEqual(self.conn._turn_writers[(self.CHAT, self.STUB)].thinking, "high")


class WriterQuestionCardTests(_RoutingCase):
    """ELI-107 (#21): a writer run ending with a question asks it once on the viewing tab's card;
    no tab, cancel or timeout leaves it as text."""

    PROMPT = HandoverRewriteTests.PROMPT
    closed_run = HandoverRewriteTests.closed_run
    settle = HandoverRewriteTests.settle
    ANSWERED = {"status": "answered", "answers": {"answer": {"type": "option", "label": "Blue"}}}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        env = types.ModuleType("open_webui.env")
        env.VERSION = "0.12.0"
        p = mock.patch.dict(sys.modules, {"open_webui.env": env})
        p.start()
        self.addCleanup(p.stop)
        self.history = {"messages": self.closed_run(self.RUN, "Before I go on:\n" + self.PROMPT)}
        inner, test = self.conn.send_request, self

        async def send_request(method, params, timeout=10):
            if method == "chat.send":
                test.sent.append((method, copy.deepcopy(params)))
                return {"runId": params["idempotencyKey"], "status": "started"}
            return await inner(method, params, timeout)
        self.conn.send_request = send_request

    def card_calls(self):
        return [(to, d["data"]) for e, d, to in self.owui.calls
                if e == "events" and d["data"].get("type") == "request:user_input"]

    def sends(self):
        return [p for m, p in self.sent if m == "chat.send"]

    async def ask_run(self, text=None):
        text = text or "Before I go on:\n" + self.PROMPT
        await self.dispatch(self.RUN, _delta(text))
        await self.dispatch(self.RUN, {"state": "final"})
        await self.settle(self.RUN)

    async def test_the_answer_continues_the_reply_under_its_card(self):
        self.owui.session_pool["sid-tab"] = {"id": self.USER, "chat_ids": [self.CHAT]}
        self.owui.page_intent["sid-tab"] = self.ANSWERED
        await self.ask_run()
        (to, _card), = self.card_calls()
        self.assertEqual(to, "sid-tab")
        (send,) = self.sends()
        self.assertEqual(send["message"], "Blue")
        (mid, m), = self.new_messages().items()
        self.assertEqual(parse_pipe_run_id(send["idempotencyKey"]),
                         {"chat": self.CHAT, "message": mid, "kind": "input"})
        self.assertIs(m["done"], False, "the answer run continues this reply")
        run2 = send["idempotencyKey"]
        self.history = {"messages": self.closed_run(self.RUN, "Before I go on:\n" + self.PROMPT)
                        + self.closed_run(run2, "Blue it is.", user="Blue")}
        await self.dispatch(run2, _delta("Blue it is."))
        await self.dispatch(run2, {"state": "final"})
        await self.settle(run2)
        m = self.new_messages()[mid]
        self.assertIs(m["done"], True)
        self.assertTrue(m["content"].startswith("Before I go on:\n"))
        self.assertIn('name="Ask User"', m["content"])
        self.assertIn('result="Blue"', m["content"])
        self.assertTrue(m["content"].endswith("Blue it is."))
        self.assertNotIn("needs input", m["content"])
        self.assertEqual(len(self.card_calls()), 1, "the answer run asked nothing")

    async def test_a_question_the_inline_reply_answered_is_not_asked_again(self):
        """A question the inline reply asked is not asked again by the writer."""
        self.owui.session_pool["sid-tab"] = {"id": self.USER, "chat_ids": [self.CHAT]}
        self.owui.page_intent["sid-tab"] = self.ANSWERED
        card = HandoverRewriteTests.CARD
        run1 = pipe_run_id(self.CHAT, self.STUB)
        run2 = pipe_run_id(self.CHAT, self.STUB, "input")
        self.add_stub(self.STUB, content="Let me ask.\n" + card)
        self.conn.note_turn_rewrite(self.CHAT, self.STUB, self.PROMPT, card)
        self.conn.register_run_target(self.key, run2, self.CHAT, self.STUB)
        self.conn._run_renderers[f"{self.key}:{run2}"].feed(_delta("Let me ask.\n" + self.PROMPT))
        self.history = {"messages": self.closed_run(run1, "Let me ask.\n" + self.PROMPT)
                        + self.closed_run(run2, "Blue it is.", user="Blue")}
        await self.dispatch(run2, _delta("Blue it is."))
        await self.dispatch(run2, {"state": "final"})
        await self.settle(run2)
        self.assertEqual(self.card_calls(), [])
        self.assertEqual(self.owui.msgs()[self.STUB]["content"], "Let me ask.\n" + card + "Blue it is.")

    async def test_with_no_tab_on_the_chat_the_question_ends_the_reply_as_text(self):
        self.owui.session_pool["sid-other"] = {"id": self.USER, "chat_ids": ["another-chat"]}
        await self.ask_run()
        self.assertEqual(self.card_calls(), [])
        self.assertEqual(self.sends(), [])
        (_mid, m), = self.new_messages().items()
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Before I go on:\nColor?\nPick one\n1. Red\n2. Blue")

    async def test_a_cancelled_card_leaves_the_question_as_text(self):
        self.owui.session_pool["sid-tab"] = {"id": self.USER, "chat_ids": [self.CHAT]}
        self.owui.page_intent["sid-tab"] = {"status": "cancelled"}
        await self.ask_run()
        self.assertEqual(len(self.card_calls()), 1)
        self.assertEqual(self.sends(), [])
        (_mid, m), = self.new_messages().items()
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Before I go on:\nColor?\nPick one\n1. Red\n2. Blue")

    async def test_an_unanswered_json_form_ends_the_reply_as_readable_text(self):
        """ELI-142: the writer's fallback reads as the inline reply's does."""
        self.owui.session_pool["sid-tab"] = {"id": self.USER, "chat_ids": [self.CHAT]}
        self.owui.page_intent["sid-tab"] = {"status": "cancelled"}
        text = "Before I go on:\n" + openclaw_pipe._USER_INPUT_TRIGGER_PREFIXES[0] + "\n" + json.dumps(
            NeedsInputMarkerTests.HEBREW_FORM, ensure_ascii=False)
        self.history = {"messages": self.closed_run(self.RUN, text)}
        await self.ask_run(text)
        self.assertEqual(len(self.card_calls()), 1)
        (_mid, m), = self.new_messages().items()
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Before I go on:\n" + openclaw_pipe._ask_user_fallback_text(text[16:]))
        self.assertIn("1. **טווח**: לאיזה טווח השקעה?\n   - קצר - עד שנה", m["content"])

    async def test_a_fence_left_open_before_a_tool_call_does_not_hide_the_question(self):
        """Fences are tracked per message item."""
        self.owui.session_pool["sid-tab"] = {"id": self.USER, "chat_ids": [self.CHAT]}
        self.owui.page_intent["sid-tab"] = {"status": "cancelled"}
        await self.dispatch(self.RUN, _delta("Checking:\n```python\nx = 1\n"))
        await self.dispatch(self.RUN, _tool("start", tcid="toolu_1"))
        await self.dispatch(self.RUN, _tool("result", tcid="toolu_1"))
        await self.ask_run("Before I go on:\n" + self.PROMPT)
        self.assertEqual(len(self.card_calls()), 1)

    async def test_a_quoted_question_in_a_code_fence_asks_nothing(self):
        self.owui.session_pool["sid-tab"] = {"id": self.USER, "chat_ids": [self.CHAT]}
        self.owui.page_intent["sid-tab"] = self.ANSWERED
        quoted = "Report:\n```\n" + self.PROMPT + "\n```\nAll done."
        self.history = {"messages": self.closed_run(self.RUN, quoted)}
        await self.ask_run(quoted)
        self.assertEqual(self.card_calls(), [])
        (_mid, m), = self.new_messages().items()
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], quoted)


class InlineRewriteWiringTests(unittest.TestCase):
    """The inline reply records each rewrite the writer must repeat and moves its target to an
    answer run. Checked on the source (e2e drives the loop)."""

    def test_the_inline_reply_records_its_rewrites_and_answer_runs(self):
        src = inspect.getsource(openclaw_pipe._PipeTurn)
        # One answer switch (ELI-147).
        self.assertEqual(src.count("switch_to_answer_run(answer_res)"), 1, "the answer switch")
        self.assertEqual(src.count("answer_res.queue or self.conn.register_consumer("), 1)
        # The answer run is listened for before its chat.send.
        ask = src.split("async def maybe_answer_user_input", 1)[1]
        send = ask.index("RUN_SEND")
        self.assertLess(ask.index("conn.register_consumer(self.session_key, idempotency_key)"), send)
        self.assertLess(ask.index("conn.continue_run_target(self.session_key, self.our_run_id, idempotency_key"), send)
        self.assertGreaterEqual(src.count("conn.note_turn_rewrite("), 3,
                                "1 MEDIA site (settle_held_media, also for streamed text), "
                                "1 answered card, 1 fallback prompt")
        self.assertIn("await self.settle_held_media(text)", src.split("async def show_text", 1)[1])
        self.assertIn("thinking=self.resolved_thinking", src.split("conn.register_run_target(")[1][:200])


class SupersededLoadTests(unittest.IsolatedAsyncioTestCase):
    """ELI-102: a turn an older Function load admitted is served by the current load, not lost on
    the retired connection."""

    OLD = "function_openclaw_gateway_" + "a" * 32

    def setUp(self):
        self.lookups = []
        self.current = None
        test = self

        async def get_function_module_by_id(request, function_id):
            test.lookups.append(function_id)
            if isinstance(test.current, Exception):
                raise test.current
            return test.current
        mods = {"open_webui": types.ModuleType("open_webui"),
                "open_webui.functions": types.SimpleNamespace(
                    get_function_module_by_id=get_function_module_by_id)}
        for p in (mock.patch.dict(sys.modules, mods),
                  mock.patch.object(openclaw_pipe, "__name__", self.OLD)):
            p.start()
            self.addCleanup(p.stop)
        # The old load's connection, retired by the new load's first request.
        retired = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        retired._retired = True
        p = mock.patch.object(openclaw_pipe, "_gateway_connection", retired)
        p.start()
        self.addCleanup(p.stop)

    def new_load(self):
        class Current:
            __module__ = "function_openclaw_gateway_" + "b" * 32

            def __init__(self):
                self.calls = []

            async def pipe(self, body, __event_emitter__, **kw):
                self.calls.append((body, kw))
                yield "served by the current load"
        return Current()

    async def turn(self, pipe):
        body = {"model": "openclaw_gateway.m", "messages": [{"role": "user", "content": "hi"}]}
        meta = {"chat_id": "c1", "message_id": "m1", "session_id": "s1"}

        async def emit(_):
            pass
        return [x async for x in pipe.pipe(body, emit, __user__={"id": "u1"}, __metadata__=meta,
                                           __request__=object())]

    async def test_a_turn_of_a_superseded_load_is_served_by_the_current_load(self):
        self.current = self.new_load()
        out = await self.turn(openclaw_pipe.Pipe())
        self.assertEqual(out, ["served by the current load"])
        self.assertEqual(self.lookups, ["openclaw_gateway"])
        (body, kw), = self.current.calls
        self.assertEqual(kw["__metadata__"]["message_id"], "m1", "the same turn, unchanged")

    async def test_the_current_load_serves_its_own_turn(self):
        pipe = openclaw_pipe.Pipe()
        self.current = pipe
        out = await self.turn(pipe)
        self.assertTrue(out and "retired" in out[-1], out)  # its own path, here the retired conn

    async def test_a_failed_lookup_serves_the_turn_here(self):
        self.current = RuntimeError("db locked")
        out = await self.turn(openclaw_pipe.Pipe())
        self.assertTrue(out and "retired" in out[-1], out)


class ControlMarkerBoundTests(unittest.IsolatedAsyncioTestCase):
    """A full queue: a row, gap or restart marker must not raise QueueFull in the receive loop."""

    async def test_markers_go_past_the_bound_of_a_full_queue(self):
        conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        q = conn.register_consumer("agent:main:k", "run-1")
        for i in range(500):
            q.put_nowait({"n": i})
        conn.row_to_consumers("agent:main:k", {"hasActiveRun": True})
        conn._mark_event_gap()
        conn._mark_gateway_restart()
        self.assertEqual(q.qsize(), 503)
        self.assertTrue(q.full(), "events still wait for room")
        self.assertEqual([q.get_nowait()["n"] for _ in range(500)], list(range(500)))
        self.assertEqual([q.get_nowait()["type"] for _ in range(3)],
                         [openclaw_pipe._ROW_MARKER_TYPE, openclaw_pipe._GAP_MARKER_TYPE,
                          openclaw_pipe._RESTART_MARKER_TYPE])


class OutsideLoopMarkerTests(unittest.IsolatedAsyncioTestCase):
    """A row from a resync task must not pass an event the loop is waiting to put."""

    async def test_a_resync_row_waits_behind_an_event_the_loop_is_putting(self):
        conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        q = conn.register_consumer("agent:main:k", "run-1")
        for i in range(500):
            q.put_nowait({"n": i})
        blocked = asyncio.ensure_future(q.put({"n": "Z"}))  # the loop, waiting for room
        await asyncio.sleep(0)
        conn.row_to_consumers("agent:main:k", {"hasActiveRun": False}, resync=True, outside_loop=True)
        await asyncio.sleep(0)
        got = []
        while len(got) < 502:
            got.append(await asyncio.wait_for(q.get(), 1))
        await blocked
        self.assertEqual(got[500], {"n": "Z"})
        self.assertEqual(got[501]["type"], openclaw_pipe._ROW_MARKER_TYPE)

    async def test_the_resync_hands_its_rows_over_from_outside_the_loop(self):
        src = inspect.getsource(openclaw_pipe._turn_resync)
        self.assertIn("outside_loop=True", src)


class InlineEndSignalTests(unittest.IsolatedAsyncioTestCase):
    """An inline reply ends on its run's signals, never silence: the settle row
    (`chat.run.settled`, lastRunId = the run) or `status: queued`."""

    CHAT = "22222222-2222-2222-2222-222222222222"
    MSG = "33333333-3333-3333-3333-333333333333"

    async def asyncSetUp(self):
        self.pipe = openclaw_pipe.Pipe()
        self.conn = _GatewayConnection(lambda: self.pipe.valves)
        self.frames, self.calls, self.emitted, self.out = asyncio.Queue(), [], [], []
        self.history = {"messages": []}
        self.rows = []
        test = self

        class WS:
            async def recv(self_):
                return json.dumps(await test.frames.get())
        self.conn._ws = WS()

        async def send_request(method, params, timeout=10):
            test.calls.append((method, params))
            if method == "sessions.patch":
                a, b = params["model"].split("/", 1)
                return {"resolved": {"modelProvider": a, "model": b}}
            if method == "chat.send":
                test.consumers_at_send = list(test.conn._consumers)
                return {"runId": params["idempotencyKey"], "status": "started"}
            if method == "chat.history":
                return copy.deepcopy(test.history)
            if method == "sessions.list":
                return {"sessions": copy.deepcopy(test.rows)}
            return {}
        self.conn.send_request = send_request

        async def ensure():
            pass
        self.conn.ensure_connected = ensure
        self.loop_task = asyncio.ensure_future(self.conn._event_loop())

        async def get_conn(_v):
            return self.conn
        p = mock.patch.object(openclaw_pipe, "_get_gateway_connection", get_conn)
        p.start()
        self.addCleanup(p.stop)

        async def emit(e):
            test.emitted.append(e)
        body = {"model": "openclaw_gateway.anthropic/claude-haiku-5-5", "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}
        meta = {"chat_id": self.CHAT, "message_id": self.MSG, "session_id": "s"}

        async def consume():
            async for x in self.pipe.pipe(body, emit, __user__={"id": "u1"}, __metadata__=meta):
                test.out.append(x)
        self.reply = asyncio.ensure_future(consume())
        for _ in range(300):
            await asyncio.sleep(0.01)
            if self.conn._consumers:
                break
        (key,) = self.conn._consumers
        self.sk, self.run = key.split(":msg-", 1)[0], "msg-" + key.split(":msg-", 1)[1]
        self.rows = [{"key": self.sk, "hasActiveRun": True, "status": "running"}]

    async def asyncTearDown(self):
        for t in (self.reply, self.loop_task):
            t.cancel()
        await asyncio.gather(self.reply, self.loop_task, return_exceptions=True)

    async def push(self, event, payload):
        await self.frames.put({"type": "event", "event": event, "payload": payload})
        await asyncio.sleep(0.05)

    async def delta(self, text, run=None):
        await self.push("agent", {"runId": run or self.run, "sessionKey": self.sk,
                                  "stream": "assistant", "data": {"delta": text}})

    async def row(self, **row):
        await self.push("sessions.changed", {"sessionKey": self.sk, **row})

    async def settled(self, run=None):
        self.rows = [{"key": self.sk, "hasActiveRun": False, "status": "done",
                      "lastRunId": run or self.run}]
        await self.row(hasActiveRun=False, status="done", lastRunId=run or self.run,
                       reason="chat.run.settled")

    async def ended(self, within=2.0):
        await asyncio.wait_for(asyncio.shield(self.reply), within)
        return "".join(x for x in self.out if isinstance(x, str))

    def closed_run(self, text):
        return [{"role": "user", "content": "hi", "idempotencyKey": f"{self.run}:user"},
                {"role": "assistant", "content": [{"type": "text", "text": text}],
                 "idempotencyKey": f"cli-assistant:{self.run}"}]

    async def test_a_reply_whose_final_was_lost_ends_on_its_settled_row(self):
        await self.delta("Hello there.")
        self.history = {"messages": self.closed_run("Hello there.")}
        await self.settled()
        self.assertIn("Hello there.", await self.ended())

    async def test_a_run_with_no_event_ends_on_its_row_with_its_transcript_text(self):
        self.history = {"messages": self.closed_run("Recovered.")}
        await self.settled()
        self.assertIn("Recovered.", await self.ended())

    async def test_another_runs_settled_row_does_not_end_the_reply(self):
        await self.delta("Part one.")
        await self.settled(run="some-other-run")
        self.assertFalse(self.reply.done())
        await self.delta(" Part two.")
        await self.push("chat", {"runId": self.run, "sessionKey": self.sk, "state": "final"})
        text = await self.ended()
        self.assertIn("Part one. Part two.", text)

    async def test_a_queued_send_shows_queued_from_its_row(self):
        await self.row(hasActiveRun=True, status="queued", reason="run-capacity")
        shown = [e["data"].get("description", "") for e in self.emitted if e.get("type") == "status"]
        self.assertTrue(any("queued" in d for d in shown), shown)
        self.assertFalse(self.reply.done())

    async def test_a_reconnect_resync_ends_a_reply_whose_run_ended_while_away(self):
        """The run ended while down; the idle row after the reconnect ends the reply."""
        await self.delta("Before the drop.")
        self.history = {"messages": self.closed_run("Before the drop. After it.")}
        self.rows = [{"key": self.sk, "hasActiveRun": False, "status": "done",
                      "lastRunId": "a-later-run"}]
        await _turn_resync(self.conn)
        text = await self.ended()
        self.assertIn("After it.", text)

    async def test_a_resync_while_the_socket_comes_back_waits_for_its_hello(self):
        """ELI-369: a handover at the drop asked for the row while the socket was
        down, which can only fail. It reads nothing; the reconnect's hello reads it."""
        await self.delta("Before the drop.")
        self.history = {"messages": self.closed_run("Before the drop. After it.")}
        self.rows = [{"key": self.sk, "hasActiveRun": False, "status": "done",
                      "lastRunId": "a-later-run"}]
        self.conn._event_loop_task = self.loop_task
        ws, self.conn._ws = self.conn._ws, None   # the read loop is reconnecting
        self.assertTrue(self.conn.reconnecting)
        reads = len([c for c in self.calls if c[0] == "sessions.list"])
        await _turn_resync(self.conn)
        self.assertEqual(len([c for c in self.calls if c[0] == "sessions.list"]), reads)
        self.conn._ws = ws                         # back: its hello resyncs
        await _turn_after_hello(self.conn)
        self.assertIn("After it.", await self.ended())

    async def test_after_a_gap_a_later_runs_settle_ends_the_reply(self):
        """Final and settle lost in a gap: the first idle row after it ends the reply."""
        await self.delta("Before the drop.")
        await _turn_resync(self.conn)  # busy, and no closer yet
        await asyncio.sleep(0.1)
        self.assertFalse(self.reply.done())
        self.history = {"messages": self.closed_run("Before the drop. After it.")}
        await self.settled(run="a-later-run")
        self.assertIn("After it.", await self.ended())

    async def test_after_a_gap_the_runs_closer_ends_the_reply_while_another_run_is_busy(self):
        await self.delta("Before the drop.")
        self.history = {"messages": self.closed_run("Before the drop. After it.")}
        await _turn_resync(self.conn)  # busy with a later run
        self.assertIn("After it.", await self.ended())

    async def test_a_resync_without_the_runs_closer_waits(self):
        await self.delta("Still working.")
        await _turn_resync(self.conn)
        await asyncio.sleep(0.1)
        self.assertFalse(self.reply.done())

    async def test_a_row_read_before_the_admission_does_not_end_the_reply(self):
        """A row read before chat.send returned does not end the reply."""
        self.history = {"messages": [
            {"role": "user", "content": "hi", "idempotencyKey": "msg-previous:user"},
            {"role": "assistant", "content": [{"type": "text", "text": "Yesterday's answer."}],
             "idempotencyKey": "cli-assistant:msg-previous"}]}
        idle = {"key": self.sk, "hasActiveRun": False, "status": "done", "lastRunId": "msg-previous"}
        self.conn.row_to_consumers(self.sk, idle, resync=True, read_at=0.0)
        await asyncio.sleep(0.1)
        self.assertFalse(self.reply.done())
        await self.delta("Today's answer.")
        await self.push("chat", {"runId": self.run, "sessionKey": self.sk, "state": "final"})
        text = await self.ended()
        self.assertNotIn("Yesterday", text)
        self.assertIn("Today's answer.", text)

    async def test_a_run_seen_nowhere_never_takes_another_runs_text(self):
        """Idle after admission, nothing of this run seen: the last reply is not its."""
        self.history = {"messages": [
            {"role": "user", "content": "hi", "idempotencyKey": "msg-previous:user"},
            {"role": "assistant", "content": [{"type": "text", "text": "Yesterday's answer."}],
             "idempotencyKey": "cli-assistant:msg-previous"}]}
        self.rows = [{"key": self.sk, "hasActiveRun": False, "status": "done",
                      "lastRunId": "msg-previous"}]
        await _turn_resync(self.conn)
        self.assertNotIn("Yesterday", await self.ended())

    async def test_the_reply_listens_before_it_sends(self):
        """A run's first events can arrive before chat.send returns; the reply listens first."""
        self.assertEqual(self.consumers_at_send, [f"{self.sk}:{self.run}"])

    def test_no_silence_timer_is_left_in_the_inline_loop(self):
        src = inspect.getsource(openclaw_pipe._PipeTurn)
        for timer in ("idle_probe_s", "no_text_deadman_s", "describe_check_interval",
                      "recv_timeout"):
            self.assertNotIn(timer, src)


class TitleRunSignalTests(unittest.IsolatedAsyncioTestCase):
    """The auto-title run is read from its events or settle row, not polled."""

    async def asyncSetUp(self):
        self.pipe = openclaw_pipe.Pipe()
        self.conn = _GatewayConnection(lambda: self.pipe.valves)
        self.calls, self.history = [], {"messages": []}
        test = self

        async def send_request(method, params, timeout=10):
            test.calls.append((method, params))
            if method == "chat.send":
                test.run = params["idempotencyKey"]
                test.sk = params["sessionKey"]
                return {"runId": test.run, "status": "started"}
            if method == "chat.history":
                return copy.deepcopy(test.history)
            raise AssertionError(f"unexpected {method}")
        self.conn.send_request = send_request
        self.task = asyncio.ensure_future(self.pipe._generate_title_text(self.conn, "hi", "hello"))
        for _ in range(100):
            await asyncio.sleep(0)
            if any(m == "chat.send" for m, _ in self.calls):
                break

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    def put(self, msg):
        (consumer,) = [c for k, c in self.conn._consumers.items() if k.startswith(self.sk)]
        for q in consumer.queues:
            q.put_nowait(msg)

    def ev(self, event, **payload):
        return {"type": "event", "event": event,
                "payload": {"sessionKey": self.sk, "runId": self.run, **payload}}

    async def test_the_title_comes_from_the_runs_own_events(self):
        self.put(self.ev("agent", stream="assistant", data={"delta": "\U0001F527 Fixing "}))
        self.put(self.ev("agent", stream="assistant", data={"delta": "the pipe"}))
        self.put(self.ev("chat", state="final"))
        self.assertEqual(await asyncio.wait_for(self.task, 1), "\U0001F527 Fixing the pipe")
        self.assertNotIn("sessions.preview", [m for m, _ in self.calls])
        self.assertFalse(self.conn._consumers, "the consumer is released")

    async def test_a_run_settled_without_its_final_is_read_from_the_transcript(self):
        self.history = {"messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "Title Here"}],
             "idempotencyKey": f"cli-assistant:{self.run}"}]}
        self.put({"type": openclaw_pipe._ROW_MARKER_TYPE, "resync": False,
                  "row": {"hasActiveRun": False, "status": "done", "lastRunId": self.run}})
        self.assertEqual(await asyncio.wait_for(self.task, 1), "Title Here")

    async def test_a_failed_title_run_gives_no_title(self):
        self.put(self.ev("agent", stream="lifecycle", data={"phase": "error", "error": "boom"}))
        self.assertIsNone(await asyncio.wait_for(self.task, 1))
        self.assertFalse(self.conn._consumers)

    async def test_the_consumer_exists_before_the_send(self):
        """A fast title run must not finish before anyone listens (R12 shape)."""
        consumers_at_send = []
        orig = self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "chat.send":
                consumers_at_send.append(list(self.conn._consumers))
            return await orig(method, params, timeout)
        self.conn.send_request = send_request
        task = asyncio.ensure_future(self.pipe._generate_title_text(self.conn, "a", "b"))
        for _ in range(50):
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(consumers_at_send and any("title-gen" in k for k in consumers_at_send[0]),
                        consumers_at_send)


class AnnounceRunIdTests(unittest.TestCase):
    CHILD = "agent:main:subagent:8b40b654-9b17-4fed-a36f-7f2031dbcb32"

    def test_shapes(self):
        want = {"child_session": self.CHILD, "child_run": "r-1"}
        for rid in (f"announce:v1:{self.CHILD}:r-1", f"announce:v1:{self.CHILD}:r-1:wake",
                    f"announce:v1:{self.CHILD}:r-1:retry-2"):
            with self.subTest(rid):
                self.assertEqual(openclaw_pipe.parse_announce_run_id(rid), want)
        for rid in ("announce:requester-settle:main:k:r-1", "msg-c-m-1.0", "", None):
            with self.subTest(rid):
                self.assertIsNone(openclaw_pipe.parse_announce_run_id(rid))


class SilentAnnounceTests(_RoutingCase):
    """A sub-agent completion arrives as announce run `announce:v1:<childSessionKey>:<childRunId>`
    with no user turn. When the parent answers NO_REPLY, the child's result still shows."""

    CHILD = "agent:main:subagent:8b40b654-9b17-4fed-a36f-7f2031dbcb32"
    CHILD_RUN = "df81c6dc-3035-49fc-89d4-55ecf4e339ce"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.announce = f"announce:v1:{self.CHILD}:{self.CHILD_RUN}"
        self.child_history = {"messages": [
            {"role": "user", "content": "Reply with exactly SUB-DONE-R10.",
             "idempotencyKey": f"{self.CHILD_RUN}:user"},
            {"role": "assistant", "content": [{"type": "text", "text": "SUB-DONE-R10"}],
             "idempotencyKey": f"cli-assistant:{self.CHILD_RUN}"}]}
        test, parent = self, self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "chat.history" and params.get("sessionKey") == test.CHILD:
                test.sent.append((method, copy.deepcopy(params)))
                if test.child_history is None:
                    raise GatewayError("session not found")
                return copy.deepcopy(test.child_history)
            return await parent(method, params, timeout)
        self.conn.send_request = send_request

    SPAWN_ARGS = {"task": "Reply with exactly SUB-DONE-R10.", "label": "R10 probe sub-agent"}

    def parent_history(self, reply, spawn_args=None):
        imported = {"importedFrom": "claude-cli"}
        return {"messages": [
            {"role": "user", "content": "spawn it", "idempotencyKey": "msg-x-1.0:user"},
            {"role": "assistant", "__openclaw": imported, "content": [
                {"type": "toolcall", "id": "toolu_s", "name": "mcp__openclaw__sessions_spawn",
                 "arguments": spawn_args or self.SPAWN_ARGS},
                {"type": "tool_result", "tool_use_id": "toolu_s", "content": [{"type": "text", "text":
                    json.dumps({"status": "accepted", "childSessionKey": self.CHILD,
                                "runId": self.CHILD_RUN})}]}]},
            {"role": "assistant", "content": [{"type": "text", "text": "SPAWNED"}],
             "idempotencyKey": "cli-assistant:msg-x-1.0"},
            {"role": "assistant", "content": [{"type": "text", "text": reply}],
             "idempotencyKey": f"cli-assistant:{self.announce}"}]}

    async def announce_run(self, reply, spawn_args=None):
        self.history = self.parent_history(reply, spawn_args)
        _turn_route_now(self.conn, self.ev(self.announce, _delta(reply)))
        _turn_route_now(self.conn, self.ev(self.announce, {"state": "final"}))
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.announce})
        await self.spin()

    async def test_a_silent_announce_shows_the_childs_result_as_a_card(self):
        await self.announce_run("NO_REPLY")
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], True)
        self.assertNotIn("NO_REPLY", m.get("content") or "")
        calls = [i for i in m["output"] if i["type"] == "function_call"]
        outputs = [i for i in m["output"] if i["type"] == "function_call_output"]
        self.assertEqual(len(calls), 1)
        self.assertIn("R10 probe sub-agent", calls[0]["arguments"])
        self.assertIn("SUB-DONE-R10", json.dumps(outputs))

    async def test_the_card_of_an_unlabelled_child_is_named_after_its_tasks_first_sentence(self):
        """ELI-155: with no spawn label the card's label was the whole task."""
        task = ("Benchmark the forecasters against the baseline on the hourly data. "
                + "Report a table per split. " * 40)
        await self.announce_run("NO_REPLY", {"task": task})
        (m,) = self.new_messages().values()
        (call,) = [i for i in m["output"] if i["type"] == "function_call"]
        label = json.loads(call["arguments"])["label"]
        self.assertTrue(label.startswith("Benchmark the forecasters"), label)
        self.assertLessEqual(len(label), 60)
        self.assertNotIn("Report a table", call["arguments"])

    async def test_an_announce_the_parent_answered_shows_only_the_answer(self):
        await self.announce_run("The sub-agent returned SUB-DONE-R10.")
        (m,) = self.new_messages().values()
        self.assertEqual(m["content"], "The sub-agent returned SUB-DONE-R10.")
        self.assertFalse([i for i in m["output"] if i["type"] == "function_call"])

    async def test_a_silent_announce_whose_child_is_gone_stays_silent(self):
        self.child_history = None
        await self.announce_run("NO_REPLY")
        self.assertEqual(self.new_messages(), {})

    # `cleanup: "delete"` deletes the child once announced, so its result is caught first: its chat
    # final or a transcript read (#20).

    def child_final(self, text="SUB-DONE-R10"):
        return {"sessionKey": self.CHILD, "runId": self.CHILD_RUN, "spawnedBy": self.key,
                "state": "final", "message": {"role": "assistant",
                                              "content": [{"type": "text", "text": text}]}}

    def card_result(self):
        (m,) = self.new_messages().values()
        calls = [i for i in m["output"] if i["type"] == "function_call"]
        self.assertEqual(len(calls), 1)
        self.assertIn("R10 probe sub-agent", calls[0]["arguments"])
        return json.dumps([i for i in m["output"] if i["type"] == "function_call_output"])

    async def test_a_deleted_childs_result_seen_in_its_final_still_shows(self):
        _turn_route_now(self.conn, self.child_final())
        self.child_history = None            # cleanup: "delete"
        await self.announce_run("NO_REPLY")
        self.assertIn("SUB-DONE-R10", self.card_result())

    async def test_a_child_deleted_while_the_announce_runs_still_shows(self):
        """No child final arrived: the transcript is read as the announce starts."""
        self.history = self.parent_history("NO_REPLY")
        _turn_route_now(self.conn, self.ev(self.announce, {"stream": "lifecycle",
                                                                   "data": {"phase": "start"}}))
        await self.spin()
        self.child_history = None            # deleted once the announcement is delivered
        _turn_route_now(self.conn, self.ev(self.announce, _delta("NO_REPLY")))
        _turn_route_now(self.conn, self.ev(self.announce, {"state": "final"}))
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.announce})
        await self.spin()
        self.assertIn("SUB-DONE-R10", self.card_result())

    async def test_another_sessions_child_final_is_not_kept(self):
        other = {**self.child_final(), "spawnedBy": "agent:main:main"}
        _turn_route_now(self.conn, other)
        self.child_history = None
        await self.announce_run("NO_REPLY")
        self.assertEqual(self.new_messages(), {})

    async def test_a_silent_requester_settle_wake_stays_silent(self):
        """`announce:requester-settle:...` silence is the parent's, not a missing result."""
        self.announce = f"announce:requester-settle:main:{self.key}:{self.CHILD_RUN}"
        await self.announce_run("NO_REPLY")
        self.assertEqual(self.new_messages(), {})

    # ELI-217 (2026.10.5): NO_REPLY to a completion fails as an empty response; the Gateway retries
    # and pushes only `phase: error` with hasActiveRun true, though sessions.list reads failed.

    ERROR_TEXT = "⚠️ Agent couldn't generate a response. Please try again."

    def failed_row(self, active=False):
        return {"key": self.key, "hasActiveRun": active,
                "status": "running" if active else "failed", "lastRunId": self.announce,
                "lastRunError": None if active else self.ERROR_TEXT}

    def list_rows(self, *rows):
        """sessions.list returns these rows in turn, then the last again."""
        rows, parent = list(rows), self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "sessions.list":
                self.sent.append((method, copy.deepcopy(params)))
                return {"sessions": [rows.pop(0) if len(rows) > 1 else rows[0]]}
            return await parent(method, params, timeout)
        self.conn.send_request = send_request

    async def failed_announce_run(self):
        """The 10.5 run: no text, lifecycle error, chat error, and only the `phase: error` push."""
        h = self.parent_history("x")
        self.history = {"messages": h["messages"][:-1]}  # a failed run has no closing message
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "start",
                                        "runId": self.announce, "hasActiveRun": True,
                                        "status": "running"})
        _turn_route_now(self.conn, self.ev(self.announce, {"stream": "lifecycle",
                                                           "data": {"phase": "start"}}))
        _turn_route_now(self.conn, self.ev(self.announce, {"stream": "lifecycle", "data": {
            "phase": "error", "error": self.ERROR_TEXT}}))
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "error",
                                        "runId": self.announce, "hasActiveRun": True,
                                        "status": "running", "lastRunId": self.announce})
        _turn_route_now(self.conn, self.ev(self.announce, {"state": "error",
                                                           "errorMessage": self.ERROR_TEXT}))
        await self.spin()

    def cards(self):
        return [(mid, m) for mid, m in self.new_messages().items()
                if any(i["type"] == "function_call" for i in m.get("output") or [])]

    async def test_a_silent_announce_that_fails_on_2026_10_shows_its_card(self):
        self.list_rows(self.failed_row(active=True), self.failed_row())
        await self.failed_announce_run()
        ((_mid, m),) = self.cards()
        self.assertIs(m["done"], True)
        self.assertIn("R10 probe sub-agent", json.dumps(m["output"]))
        self.assertIn("SUB-DONE-R10", json.dumps(m["output"]))
        self.assertEqual(self.conn._turn_pumps, {}, "the run's pump ended")

    async def test_the_card_of_a_native_runs_spawn_is_named_by_its_label(self):
        """ELI-217: a native parent's spawn card (`toolCall` + `toolResult`) has its label."""
        h = self.parent_history("NO_REPLY")
        result = json.dumps({"status": "accepted", "childSessionKey": self.CHILD,
                             "runId": self.CHILD_RUN})
        h["messages"][1] = {"role": "assistant", "content": [
            {"type": "toolCall", "id": "call_s", "name": "sessions_spawn",
             "arguments": self.SPAWN_ARGS}]}
        h["messages"].insert(2, {"role": "toolResult", "toolCallId": "call_s",
                                 "toolName": "sessions_spawn",
                                 "content": [{"type": "text", "text": result}]})
        self.history = h
        _turn_route_now(self.conn, self.ev(self.announce, _delta("NO_REPLY")))
        _turn_route_now(self.conn, self.ev(self.announce, {"state": "final"}))
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.announce})
        await self.spin()
        (m,) = self.new_messages().values()
        (call,) = [i for i in m["output"] if i["type"] == "function_call"]
        self.assertIn("R10 probe sub-agent", call["arguments"])

    async def test_the_card_of_a_spawn_made_through_a_deferred_tool_wrapper_is_named(self):
        """On 2026.10 a deferred `sessions_spawn` via `tool_call` still names its card."""
        h = self.parent_history("NO_REPLY")
        result = json.dumps({"status": "accepted", "childSessionKey": self.CHILD,
                             "runId": self.CHILD_RUN})
        h["messages"][1] = {"role": "assistant", "content": [
            {"type": "toolCall", "id": "call_w", "name": "tool_call",
             "arguments": {"id": "sessions_spawn", "args": self.SPAWN_ARGS}}]}
        h["messages"].insert(2, {"role": "toolResult", "toolCallId": "call_w", "toolName": "tool_call",
                                 "content": [{"type": "text", "text": json.dumps(
                                     {"tool": {"id": "sessions_spawn"}, "result": result})}]})
        self.history = h
        _turn_route_now(self.conn, self.ev(self.announce, _delta("NO_REPLY")))
        _turn_route_now(self.conn, self.ev(self.announce, {"state": "final"}))
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.announce})
        await self.spin()
        (m,) = self.new_messages().values()
        (call,) = [i for i in m["output"] if i["type"] == "function_call"]
        self.assertIn("R10 probe sub-agent", call["arguments"])

    async def test_a_failed_announce_waits_while_its_row_is_still_active(self):
        """No guess: a row still active at the error waits for the next push."""
        self.list_rows(self.failed_row(active=True))
        await self.failed_announce_run()
        self.assertEqual(self.cards(), [])
        self.assertTrue(self.conn._turn_pumps, "the pump waits for the settle")
        self.conn._on_sessions_changed({"sessionKey": self.key, **self.failed_row()})
        await self.spin()
        self.assertEqual(len(self.cards()), 1)
        self.assertEqual(self.conn._turn_pumps, {})

    async def test_a_later_retry_reply_lands_once_beside_the_one_card(self):
        """Retries reuse the run id: silent ones add no card, an aloud one lands once."""
        self.list_rows(self.failed_row(active=True), self.failed_row())
        await self.failed_announce_run()
        self.list_rows(self.failed_row(active=True), self.failed_row())
        await self.failed_announce_run()
        self.assertEqual(len(self.cards()), 1)
        self.list_rows({"key": self.key, "hasActiveRun": False, "status": "done",
                        "lastRunId": self.announce})
        await self.announce_run("The sub-agent returned SUB-DONE-R10.")
        self.assertEqual(len(self.cards()), 1)
        replies = [m for m in self.new_messages().values()
                   if m.get("content") == "The sub-agent returned SUB-DONE-R10."]
        self.assertEqual(len(replies), 1)
        self.assertEqual(len(self.new_messages()), 2)

    async def unsettled_announce_run(self, reply):
        """An aloud attempt still active after `phase: end` (ELI-215) ends on its final."""
        self.history = self.parent_history(reply)
        _turn_route_now(self.conn, self.ev(self.announce, _delta(reply)))
        _turn_route_now(self.conn, self.ev(self.announce, {"state": "final"}))
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "end",
                                        "runId": self.announce, "hasActiveRun": True,
                                        "status": "running", "lastRunId": self.announce})
        await self.spin()

    async def test_a_retry_answered_aloud_with_no_settle_push_lands_once(self):
        """ELI-215 with ELI-217: the aloud retry ends on its own final, once."""
        self.list_rows(self.failed_row(active=True), self.failed_row())
        await self.failed_announce_run()
        self.list_rows(self.failed_row(active=True), self.failed_row())
        await self.failed_announce_run()
        await self.unsettled_announce_run("The sub-agent returned SUB-DONE-R10.")
        self.assertEqual(len(self.cards()), 1)
        replies = [m for m in self.new_messages().values()
                   if m.get("content") == "The sub-agent returned SUB-DONE-R10."]
        self.assertEqual([m["done"] for m in replies], [True])
        self.assertEqual(len(self.new_messages()), 2)
        self.assertEqual(self.conn._turn_pumps, {})

    async def test_a_retry_after_a_clean_final_does_not_reopen_its_message(self):
        """A retry after a clean final keeps the one done message. 10.5 never retries a visible
        reply (`hasAnnounceSendEvidence`); this guards a Gateway change."""
        await self.unsettled_announce_run("First answer.")
        await self.unsettled_announce_run("Second answer.")
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.announce})
        await self.spin()
        self.assertEqual([(m["content"], m["done"]) for m in self.new_messages().values()],
                         [("First answer.", True)])
        self.assertEqual(self.conn._turn_pumps, {})


class ErroredAgentRunTests(_RoutingCase):
    """ELI-217: an errored agent-started run gets no settle push. Its error event reads the row
    once; idle ends the message, active waits."""

    ERROR_TEXT = "⚠️ Agent couldn't generate a response. Please try again."

    def rows(self, *rows):
        rows, parent = list(rows), self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "sessions.list":
                self.sent.append((method, copy.deepcopy(params)))
                return {"sessions": [rows.pop(0) if len(rows) > 1 else rows[0]]}
            return await parent(method, params, timeout)
        self.conn.send_request = send_request

    def row(self, active):
        return {"key": self.key, "hasActiveRun": active, "status": "running" if active else "failed",
                "lastRunId": self.RUN, "lastRunError": None if active else self.ERROR_TEXT}

    async def errored_run(self):
        self.history = {"messages": [
            {"role": "user", "content": "x", "idempotencyKey": f"{self.RUN}:user"}]}
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "start", "runId": self.RUN,
                                        "hasActiveRun": True, "status": "running"})
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("Working on it.")))
        _turn_route_now(self.conn, self.ev(self.RUN, {"stream": "lifecycle", "data": {
            "phase": "error", "error": self.ERROR_TEXT}}))
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "error", "runId": self.RUN,
                                        "hasActiveRun": True, "status": "running",
                                        "lastRunId": self.RUN})
        _turn_route_now(self.conn, self.ev(self.RUN, {"state": "error",
                                                      "errorMessage": self.ERROR_TEXT}))
        await self.spin()

    async def test_an_errored_agent_run_with_no_settle_push_ends_its_message(self):
        self.rows(self.row(True), self.row(False))
        await self.errored_run()
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], True)
        self.assertIn("Working on it.", m["content"])
        self.assertIn(self.ERROR_TEXT, m["content"])
        self.assertEqual(self.conn._turn_pumps, {})

    async def test_an_errored_agent_run_whose_row_is_active_waits_for_its_settle(self):
        self.rows(self.row(True))
        await self.errored_run()
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], False)
        self.conn._on_sessions_changed({"sessionKey": self.key, **self.row(False)})
        await self.spin()
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], True)
        self.assertEqual(self.conn._turn_pumps, {})


class DeletedChatAgentRunTests(_RoutingCase):
    """ELI-218: a run on a deleted chat's session starts no failing pumps."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.owui.chat = None          # Chats._chat_transaction yields None for a missing row
        self.logged = []
        log = mock.patch.object(openclaw_pipe, "pipe_log", lambda *a, **k: self.logged.append(a))
        log.start()
        self.addCleanup(log.stop)

    def failures(self):
        return [a for a in self.logged if "failed" in str(a[0])]

    async def test_a_run_on_a_deleted_chats_session_writes_nothing_and_fails_nothing(self):
        await self.run_turn(self.RUN)
        self.assertEqual(self.failures(), [])
        self.assertIsNone(self.owui.chat)
        self.assertEqual(self.conn._turn_pumps, {})

    async def test_it_is_logged_once_for_the_run(self):
        await self.run_turn(self.RUN)
        gone = [a for a in self.logged if "is gone" in str(a[0])]
        self.assertEqual(len(gone), 1, self.logged)


class IntentHookStopIconTests(unittest.TestCase):
    """The page hook also matches the Stop icon, not only the English aria-label. Fixture:
    scripts/extract_owui_stop_button.py."""

    FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                           "test_fixture_owui_stop_button.json")

    def test_the_hook_matches_the_composer_stop_icon(self):
        marker = re.search(r"STOP_ICON = '([^']+)'", openclaw_pipe._INTENT_HOOK_JS).group(1)
        templates = [t["template"] for t in json.load(open(self.FIXTURE))["templates"]]
        composer = [t for t in templates if "<path" in t and t.startswith("<button class=\"bg-white")]
        self.assertEqual(len(composer), 2, "the composer's Stop button, both input variants")
        for t in composer:
            self.assertIn(f'd="{marker}', t)

    def test_a_stop_click_in_another_language_is_recorded_as_stop(self):
        """Under node with a stub page: a Hebrew-labelled Stop click records "stop"."""
        import shutil
        import subprocess
        node = shutil.which("node")
        if node is None:
            self.skipTest("no node")
        script = textwrap.dedent("""
            const listeners = {};
            const mkPath = (d) => ({ getAttribute: (k) => (k === 'd' ? d : null) });
            const button = (label, d) => ({
              id: '', getAttribute: (k) => (k === 'aria-label' ? label : null),
              querySelectorAll: () => (d ? [mkPath(d)] : []),
              closest() { return this; },
            });
            const D = process.argv[2];
            const stop = button('\u05e2\u05e6\u05d5\u05e8', D);
            global.window = {};
            global.location = { pathname: '/c/chat-1' };
            global.document = {
              addEventListener: (k, f) => { listeners[k] = f; },
              querySelectorAll: () => [stop],
            };
            const hook = new Function(process.argv[1]);
            hook();
            listeners.click({ target: stop });
            console.log(JSON.stringify({ intent: window.__ocIntents['chat-1'].kind }));
        """)
        tpl = next(t["template"] for t in json.load(open(self.FIXTURE))["templates"]
                   if t["template"].startswith("<button class=\"bg-white"))
        d = re.search(r' d="([^"]+)"', tpl).group(1)
        out = subprocess.run([node, "-e", script, openclaw_pipe._INTENT_HOOK_JS, d],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["intent"], "stop")

    def test_no_english_only_running_check_is_left(self):
        self.assertNotIn('querySelector(\'button[aria-label="Stop"]', openclaw_pipe._INTENT_HOOK_JS)


class IntentOfOneStopTests(unittest.TestCase):
    """ELI-198: one Stop cancels every task of the chat; each must hear "stop" from the page, so
    the query does not clear the record."""

    def run_page(self, steps: list) -> list:
        """Plays `steps`: ["stop"] clicks Stop, ["submit", running] submits, ["query"] runs the
        intent query. Returns the query answers."""
        import shutil
        import subprocess
        node = shutil.which("node")
        if node is None:
            self.skipTest("no node")
        script = textwrap.dedent("""
            const [hook, query, steps] = JSON.parse(process.argv[1]);
            const listeners = {};
            let running = true;
            const stop = { id: 'stop-response-button', getAttribute: () => null,
                           querySelectorAll: () => [], closest() { return this; } };
            const input = { closest: (sel) => (sel === '#chat-input' ? input : null) };
            const form = { contains: (el) => el === input, querySelector: () => input,
                           closest: () => null };
            global.window = {};
            global.location = { pathname: '/c/chat-1' };
            global.document = {
              addEventListener: (k, f) => { listeners[k] = f; },
              querySelectorAll: (sel) => (sel === 'button' && running ? [stop] : []),
              querySelector: (sel) => (sel === '#chat-input' ? input : null),
            };
            new Function(hook)();
            const answers = [];
            for (const s of steps) {
              if (s[0] === 'stop') listeners.click({ target: stop });
              else if (s[0] === 'submit') { running = s[1]; if (listeners.submit) listeners.submit({ target: form }); }
              else answers.push(new Function(query)());
            }
            console.log(JSON.stringify(answers));
        """)
        args = [openclaw_pipe._INTENT_HOOK_JS, openclaw_pipe._intent_query_js("chat-1"), steps]
        out = subprocess.run([node, "-e", script, json.dumps(args)], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_one_stop_answers_every_task_it_cancelled(self):
        self.assertEqual(self.run_page([["stop"], ["query"], ["query"]]), ["stop", "stop"])

    def test_a_new_message_ends_the_stop(self):
        """A cancel after the next message is not that Stop's."""
        self.assertEqual(self.run_page([["stop"], ["query"], ["submit", False], ["query"]]), ["stop", "none"])

    def test_a_message_queued_while_a_reply_runs_keeps_it(self):
        self.assertEqual(self.run_page([["stop"], ["submit", True], ["query"]]), ["stop"])


class AutoSendHookTests(unittest.TestCase):
    """AUTO_SEND_WHILE_RUNNING: a message Open WebUI queues during a reply is sent at once via its
    Send now, marked so the run is kept. Under node with a stub page."""

    SEND_NOW_ICON = "M4.5 10.5 12 3m0 0 7.5 7.5M12 3v18"

    def run_page(self, chat_flags: dict, steps: list, path="/c/chat-1") -> dict:
        """Install the hooks once per `chat_flags` entry, then play `steps`: ["queue"], ["user",
        id], ["mutate"]. Returns the clicks per queued item and the intents."""
        import shutil
        import subprocess
        node = shutil.which("node")
        if node is None:
            self.skipTest("no node")
        script = textwrap.dedent("""
            const [codes, steps, path, icon] = JSON.parse(process.argv[1]);
            let observer = null;
            const queue = [], users = [], clicks = [];
            global.MutationObserver = class { constructor(f) { observer = f; } observe() {} };
            global.window = {};
            global.location = { pathname: path };
            global.document = {
              body: {},
              addEventListener() {},
              querySelectorAll(sel) {
                if (sel.startsWith('.user-message')) return users.map((id) => ({ id: 'message-' + id }));
                return [];
              },
              querySelector(sel) {
                const item = queue[0];
                if (!item || !sel.includes(':not([disabled])') || !sel.includes('path[d="' + icon + '"]')) return null;
                return { closest: () => ({ click() { clicks.push(item.n); } }) };
              },
            };
            for (const c of codes) new Function(c)();
            let n = 0;
            for (const s of steps) {
              if (s[0] === 'queue') queue.push({ n: ++n });
              else if (s[0] === 'user') { users.push(s[1]); queue.shift(); }
              observer();
            }
            console.log(JSON.stringify({ clicks, intents: window.__ocIntents || {} }));
        """)
        codes = [openclaw_pipe._page_hooks_js(chat, on) for chat, on in chat_flags.items()]
        out = subprocess.run([node, "-e", script, json.dumps([codes, steps, path, self.SEND_NOW_ICON])],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_a_queued_message_is_sent_at_once_as_send_now(self):
        got = self.run_page({"chat-1": True}, [["user", "u1"], ["queue"]])
        self.assertEqual(got["clicks"], [1])
        self.assertEqual(got["intents"]["chat-1"]["kind"], "send_now")

    def test_the_next_queued_message_waits_until_the_last_one_is_in_the_chat(self):
        """Open WebUI ignores Send now while submitting; the next click waits."""
        got = self.run_page({"chat-1": True}, [
            ["user", "u1"], ["queue"], ["queue"], ["mutate"], ["user", "u2"]])
        self.assertEqual(got["clicks"], [1, 2])

    def test_a_second_queued_message_is_not_clicked_while_the_first_is_in_flight(self):
        got = self.run_page({"chat-1": True}, [["user", "u1"], ["queue"], ["queue"], ["mutate"]])
        self.assertEqual(got["clicks"], [1])

    def test_off_leaves_the_message_in_open_webuis_queue(self):
        got = self.run_page({"chat-1": False}, [["user", "u1"], ["queue"]])
        self.assertEqual(got["clicks"], [])
        self.assertEqual(got["intents"], {})

    def test_a_chat_the_pipe_never_flagged_is_left_alone(self):
        """A chat on another model gets no hooks, so no flag."""
        got = self.run_page({"chat-1": True}, [["user", "u1"], ["queue"]], path="/c/chat-2")
        self.assertEqual(got["clicks"], [])

    def test_the_valve_defaults_on(self):
        self.assertIs(openclaw_pipe.Pipe.Valves().AUTO_SEND_WHILE_RUNNING, True)
        self.assertIs(openclaw_pipe._auto_send_on(types.SimpleNamespace()), True)
        self.assertIs(openclaw_pipe._auto_send_on(
            types.SimpleNamespace(AUTO_SEND_WHILE_RUNNING=False)), False)


class RunSpanTests(unittest.TestCase):
    """A run's messages in chat.history never include another run's."""

    def test_an_announce_run_without_a_user_turn_does_not_take_the_parents_card(self):
        """An announce run (`announce:v1:...`) has no `<id>:user` turn; its span must not run back
        into the parent's sessions_spawn card and closer."""
        imported = {"importedFrom": "claude-cli"}
        parent, ann = "msg-c-m-1.0", "announce:v1:agent:main:subagent:x:y"
        hist = {"messages": [
            {"role": "user", "content": "spawn", "idempotencyKey": f"{parent}:user"},
            {"role": "user", "content": "Conversation info (untrusted metadata): {}"},
            {"role": "assistant", "__openclaw": imported, "content": [
                {"type": "toolcall", "id": "toolu_s", "name": "sessions_spawn", "arguments": {}},
                {"type": "tool_result", "tool_use_id": "toolu_s", "content": [{"type": "text", "text": "ok"}]}]},
            {"role": "assistant", "idempotencyKey": f"cli-assistant:{parent}",
             "content": [{"type": "text", "text": "SPAWNED"}]},
            {"role": "assistant", "idempotencyKey": f"cli-assistant:{ann}",
             "content": [{"type": "text", "text": "The check finished."}]}]}
        span = _history_span_messages(hist, ann)
        self.assertEqual([m.get("idempotencyKey") for m in span], [f"cli-assistant:{ann}"])
        r = _history_renderer(span)
        self.assertEqual((r.text.strip(), r.output and sum(
            1 for i in r.finished_output() if i["type"] == "function_call")), ("The check finished.", 0))
        self.assertEqual(len(_history_span_messages(hist, parent)), 2, "the parent keeps its own")


class GatewayTenRecoveryRowTests(unittest.TestCase):
    """ELI-164: on 2026.10.10 a crash-cut session reads idle for about 8 s, then `interrupted`,
    then `running`. 2026.9.3 reads `running`."""

    UNJUDGED = {"status": None, "hasActiveRun": False, "abortedLastRun": False}
    MARKED = {"status": "interrupted", "hasActiveRun": False, "abortedLastRun": True,
              "lastRunError": "Run interrupted by Gateway restart or lost ownership"}

    def test_a_row_marked_for_recovery_keeps_the_message_open(self):
        self.assertEqual(next_continuation(self.MARKED, False, 0), "gateway")

    def test_a_row_not_judged_yet_after_a_restart_keeps_the_message_open(self):
        self.assertEqual(next_continuation(self.UNJUDGED, False, 0, after_restart=True), "gateway")

    def test_without_a_restart_an_idle_row_with_no_status_still_finalizes(self):
        self.assertEqual(next_continuation(self.UNJUDGED, False, 0), "finalize")


class GatewayTenRecoveryRoutingTests(_RoutingCase):
    """ELI-164: on 2026.10.10 the recovery run's `:user` turn commits about 0.7 s after its start,
    so chat.history on its first event cannot name the run it recovers."""

    def recovery_turn(self):
        return {"role": "user", "content": "[System] continue", "idempotencyKey": f"{self.REC}:user",
                "provenance": {"kind": "internal_system", "sourceTool": "main_session_restart_recovery"}}

    async def test_a_crashed_reply_and_its_late_named_recovery_are_one_message(self):
        self.history = {"messages": [{"role": "user", "content": "x", "idempotencyKey": f"{self.RUN}:user"}]}
        self.conn.gateway_boot_id = "boot-a"
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        await self.spin()
        self.conn.gateway_version = "2026.10.10-beta.1"
        self.conn._note_gateway_boot("boot-b")          # the crash, seen at the reconnect
        # After hello, before the recovery pass: idle, no status.
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False})
        await self.spin()
        mid = turn_message_id(self.key, self.RUN, self.CHAT)
        self.assertIsNot(self.owui.msgs().get(mid, {}).get("done"), True,
                         "the Gateway has not judged the run yet: M stays open")
        # The recovery starts; its turn is not in the transcript yet.
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "start", "runId": self.REC,
                                        "hasActiveRun": True, "status": "running"})
        _turn_route_now(self.conn, self.ev(self.REC, {"stream": "lifecycle", "data": {"phase": "start"}}))
        _turn_route_now(self.conn, self.ev(self.REC, _delta("Again.")))
        await self.spin()
        self.history["messages"].append(self.recovery_turn())
        self.conn._on_session_message({"sessionKey": self.key, "messageId": "m-2", "messageSeq": 2})
        await self.spin()
        _turn_route_now(self.conn, self.ev(self.REC, _delta(" Found it.")))
        _turn_route_now(self.conn, self.ev(self.REC, {"state": "final"}))
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False, "status": "done",
                                        "lastRunId": self.REC, "reason": "chat.run.settled"})
        await self.spin()
        (got, m), = self.new_messages().items()
        self.assertEqual(got, mid, "rule E: the recovery writes into the interrupted run's message")
        self.assertIs(m["done"], True)
        self.assertIn("Reading them.", m["content"])
        self.assertIn("Found it.", m["content"])

    def crash(self, version):
        """RUN streamed "Reading them.", then the Gateway restarted as `version`."""
        self.history = {"messages": [{"role": "user", "content": "x", "idempotencyKey": f"{self.RUN}:user"}]}
        self.conn.gateway_boot_id = "boot-a"
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("Reading them.")))

    async def restarted(self, version, row):
        await self.spin()
        self.conn.gateway_version = version
        self.conn._note_gateway_boot("boot-b")
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False, **row})
        await self.spin()
        return self.owui.msgs().get(turn_message_id(self.key, self.RUN, self.CHAT), {})

    async def test_on_9_3_an_idle_row_with_no_status_after_a_restart_finalizes(self):
        """9.3 keeps `running` across a restart and recovers within ms, so a no-status row there is
        never recovered: M must not wait."""
        self.crash("2026.9.3")
        m = await self.restarted("2026.9.3", {})
        self.assertIs(m.get("done"), True, "9.3: nothing would ever close M")

    async def test_a_run_that_ended_before_the_crash_is_not_held(self):
        """Its final arrived: no Gateway recovers a finished run."""
        self.crash("2026.10.10-beta.1")
        _turn_route_now(self.conn, self.ev(self.RUN, {"state": "final"}))
        m = await self.restarted("2026.10.10-beta.1", {})
        self.assertIs(m.get("done"), True, "a finished run must not wait for a recovery")

    async def test_a_recovery_the_gateway_gives_up_on_closes_the_message(self):
        """The give-up marks the row failed and appends a notice but pushes no row; the notice's
        `session.message` re-reads it."""
        self.crash("2026.10.10-beta.1")
        m = await self.restarted("2026.10.10-beta.1", {"status": "interrupted", "abortedLastRun": True})
        self.assertIsNot(m.get("done"), True, "marked for recovery: M stays open")
        tomb = {"key": self.key, "hasActiveRun": False, "status": "failed", "abortedLastRun": False,
                "lastRunError": "Restart recovery gave up"}
        test, base = self, self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "sessions.list":
                test.sent.append((method, params))
                return {"sessions": [tomb]}
            return await base(method, params, timeout)
        self.conn.send_request = send_request
        self.conn._on_session_message({"sessionKey": self.key, "messageId": "m-3", "messageSeq": 3})
        await self.spin()
        m = self.owui.msgs()[turn_message_id(self.key, self.RUN, self.CHAT)]
        self.assertIs(m.get("done"), True, "the tombstone's notice closes M")
        self.assertIn("Restart recovery gave up", m.get("content", ""))

    async def test_a_run_whose_turn_never_comes_is_written_at_its_end(self):
        """A run with no admitted turn while M waits resolves at its end, to its own message."""
        self.history = {"messages": [{"role": "user", "content": "x", "idempotencyKey": f"{self.RUN}:user"}]}
        self.conn.gateway_boot_id = "boot-a"
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        await self.spin()
        self.conn.gateway_version = "2026.10.10-beta.1"
        self.conn._note_gateway_boot("boot-b")
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False})
        await self.spin()
        other = "04351f67-dbc3-4533-b2aa-2a0d88d79da0"
        _turn_route_now(self.conn, self.ev(other, _delta("Follow-up.")))
        _turn_route_now(self.conn, self.ev(other, {"state": "final"}))
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False, "status": "done",
                                        "lastRunId": other, "reason": "chat.run.settled"})
        await self.spin()
        own = turn_message_id(self.key, other, self.CHAT)
        self.assertIs(self.owui.msgs().get(own, {}).get("done"), True)
        self.assertNotIn("Follow-up.", self.owui.msgs()[turn_message_id(self.key, self.RUN, self.CHAT)]
                         .get("content", ""))

class SessionListCapTests(unittest.IsolatedAsyncioTestCase):
    """`sessions.list` returns 100 rows by default; a full read pages."""

    KEY = "agent:main:openwebui-11111111-1111-1111-1111-111111111111-22222222-2222-2222-2222-222222222222"

    def conn(self, rows):
        gw = PagedSessions(rows)
        conn = types.SimpleNamespace(gw=gw)

        async def send_request(method, params, timeout=10):
            assert method == "sessions.list", method
            return gw.list(params)
        conn.send_request = send_request
        return conn

    async def test_a_busy_session_past_the_default_page_reads_busy(self):
        conn = self.conn(crowd(self.KEY, busy=True))
        self.assertIs(await openclaw_pipe._session_list_active(conn, self.KEY, missing=False), True)
        self.assertTrue(all("search" in c for c in conn.gw.calls), conn.gw.calls)

    async def test_an_idle_session_past_the_default_page_reads_idle(self):
        conn = self.conn(crowd(self.KEY, busy=False))
        self.assertIs(await openclaw_pipe._session_list_active(conn, self.KEY, missing=False), False)

    async def test_a_session_the_gateway_does_not_have_is_missing_by_the_callers_rule(self):
        """Not listed at all: the caller decides what that means."""
        conn = self.conn(crowd(self.KEY, busy=True)[:50])
        self.assertIs(await openclaw_pipe._session_list_active(conn, self.KEY, missing=False), False)
        self.assertIsNone(await openclaw_pipe._session_list_active(conn, self.KEY, missing=None))

    async def test_an_answer_that_may_hide_the_session_cannot_say(self):
        """More rows matched than returned, none is the session: no answer."""
        async def send_request(method, params, timeout=10):
            return {"sessions": [{"key": f"other-{i}", "hasActiveRun": False} for i in range(10)], "totalCount": 25}
        conn = types.SimpleNamespace(send_request=send_request)
        self.assertIsNone(await openclaw_pipe._session_list_active(conn, self.KEY, missing=False))

    async def test_a_full_read_pages_through_every_session(self):
        rows = crowd(self.KEY, busy=True, before=400, after=700)
        conn = self.conn(rows)
        got = await openclaw_pipe._list_all_sessions(conn)
        self.assertEqual([r["key"] for r in got["sessions"]], [r["key"] for r in rows])
        self.assertGreater(len(conn.gw.calls), 1)

    async def test_the_ended_while_down_pass_reaches_a_session_past_the_first_page(self):
        """The ended-while-down startup pass reaches past the newest 100 sessions."""
        user, chat = "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444"
        key = f"agent:main:openwebui-{user}-{chat}"
        rows = crowd(key, busy=False)
        rows[120] = {**rows[120], "updatedAt": 10_000_000, "sessionId": "s1", "lastRunId": "r1"}
        conn = self.conn(rows)
        chats = types.ModuleType("open_webui.models.chats")

        async def get_chat_by_id(chat_id):
            return types.SimpleNamespace(user_id=user, updated_at=0)
        chats.Chats = types.SimpleNamespace(get_chat_by_id=get_chat_by_id)
        reconcile = mock.AsyncMock(return_value=1)
        with mock.patch.dict(sys.modules, {"open_webui": types.ModuleType("open_webui"),
                                           "open_webui.models": types.ModuleType("open_webui.models"),
                                           "open_webui.models.chats": chats}), \
                mock.patch.object(openclaw_pipe, "_reconcile_chat_ended_runs", reconcile):
            await openclaw_pipe._reconcile_ended_while_down(conn, "main")
        self.assertEqual([c.args[1] for c in reconcile.await_args_list], [key])


if __name__ == "__main__":
    unittest.main()


class EventGapTests(unittest.IsolatedAsyncioTestCase):
    """Events lost during a reconnect; seq numbers are per connection, so no replay."""

    def test_whole_text_with_a_hole_yields_only_what_follows(self):
        whole = "".join(f"{i}\n" for i in range(1, 400))
        received = "".join(f"{i}\n" for i in range(184, 400))
        self.assertEqual(_item_delta_text(whole, "", received), "")
        self.assertEqual(_item_delta_text(whole + "END", "", received), "END")

    def test_short_baseline_keeps_the_old_rule(self):
        # "OK." in a later, different text must not swallow it.
        self.assertEqual(_item_delta_text("Fine. Then more text. OK.", "", "OK."),
                         "Fine. Then more text. OK.")

    async def test_disconnect_marks_a_gap_after_what_was_queued(self):
        conn = _GatewayConnection(lambda: None)
        q = conn.register_consumer("s", "r")
        q.put_nowait({"type": "event", "n": 1})

        class DroppedWS:
            async def recv(self_):
                raise websockets.exceptions.ConnectionClosed(None, None)

        async def stop_instead_of_reconnecting():
            conn._stopped = True

        conn._ws = DroppedWS()
        conn._reconnect = stop_instead_of_reconnecting
        await conn._event_loop()
        if not conn._stopped:
            await conn._event_loop()
        self.assertEqual(q.get_nowait(), {"type": "event", "n": 1})
        self.assertEqual(q.get_nowait()["type"], "_pipe_gap")

class NeedsInputMarkerTests(_TurnCase):
    """ELI-107: a needs-input block QUOTED in a fenced code block is not a live prompt; holding it
    stopped the stream and left the reply open for an hour."""

    MARKER = openclaw_pipe._USER_INPUT_TRIGGER_PREFIXES[-1]
    REPORT = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                         "test_fixture_eli107_report.json")))["text"]

    async def _stream(self, text, event_call, chunk=40):
        """One run streams `text` in `chunk`-sized deltas; returns the shown chunks."""
        async def emitter(event):
            pass
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "report"}]}
        meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": "m1"}
        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta, __event_call__=event_call)

        async def drain():
            out = ""
            async for c in gen:
                out += c if isinstance(c, str) else ""
            return out
        task = asyncio.ensure_future(drain())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        for i in range(0, len(text), chunk):
            q.put_nowait(self._delta("run-1", text[i:i + chunk]))
        q.put_nowait(self._final("run-1"))
        return await asyncio.wait_for(task, 5)   # bounds the test only

    async def test_a_quoted_marker_in_a_code_fence_is_text_not_a_prompt(self):
        self.assertIn("```\n  " + self.MARKER, self.REPORT, "the fixture quotes it in a fence")
        calls = []

        async def event_call(payload):
            calls.append(payload)
            return {"error": "Client session disconnected."}
        out = await self._stream(self.REPORT, event_call)
        self.assertEqual(calls, [], "no card for a quote")
        self.assertEqual(out.strip(), self.REPORT.strip(), "the whole report is shown as written")

    def test_a_prompt_after_a_closed_fence_still_counts(self):
        m = self.MARKER
        for fence in ("```", "~~~"):
            with self.subTest(fence=fence):
                quoted = f"See:\n{fence}\n{m}\nold\n{fence}\n"
                text = quoted + f"{m}\nTitle\nQ?\n"
                self.assertEqual(self.ocp._marker_line_index(text), len(quoted))
                self.assertIsNone(self.ocp._marker_line_index(quoted))
                # Streamed: the quote is shown, the real prompt is held.
                shown, held = self.ocp._advance_input_prompt_buffer("", quoted, False)
                self.assertEqual((shown, held), (quoted, ""))
                shown, held = self.ocp._advance_input_prompt_buffer(
                    "", f"{m}\nTitle", self.ocp._fence_open_after(quoted))
                self.assertEqual((shown, held), ("", f"{m}\nTitle"))
                # Inside an open fence a marker line is never held.
                shown, held = self.ocp._advance_input_prompt_buffer("", f"{m}\n", True)
                self.assertEqual(held, "")

    # Fences per CommonMark 4.5: a backtick fence's info string has no backtick, only the same
    # character at least as long closes it, and a fence never spans message items.
    Q = MARKER + " Which database should I migrate first?"

    def test_F4_inline_triple_backticks_do_not_open_a_fence(self):
        text = "```json``` is the format I used.\n" + self.Q
        self.assertIsNotNone(self.ocp._marker_line_index(text))

    def test_F4b_a_tilde_line_inside_a_backtick_block_does_not_close_it(self):
        text = "```\n~~~\n```\n" + self.Q
        self.assertIsNotNone(self.ocp._marker_line_index(text))
        self.assertIsNone(self.ocp._marker_line_index("```\n~~~\n" + self.Q))

    def test_F4_a_shorter_fence_does_not_close_a_longer_one(self):
        self.assertIsNone(self.ocp._marker_line_index("````\n```\n" + self.Q))
        self.assertIsNotNone(self.ocp._marker_line_index("````\n`````\n" + self.Q))
        self.assertIsNone(self.ocp._marker_line_index("```\n``` not a close\n" + self.Q))

    def test_F4c_the_live_buffer_holds_a_question_after_such_a_line(self):
        shown = "```json``` is the format I used.\n"
        flush, held = self.ocp._advance_input_prompt_buffer(
            "", self.Q, self.ocp._fence_open_after(shown))
        self.assertEqual((flush, held), ("", self.Q), "the question was not held for a card")

    # ELI-142: an unanswered card ends the reply with its question as text, not a raw JSON body.
    HEBREW_FORM = {"questions": [
        {"header": "טווח", "question": "לאיזה טווח השקעה?",
         "options": ["קצר - עד שנה", "ארוך - 5 שנים ומעלה"]},
        {"header": "סיכון", "question": "כמה סיכון אתה מוכן לקחת?",
         "options": [{"label": "נמוך", "description": "אג\"ח ומדדים"}, {"label": "גבוה"}]},
        {"header": "סכום", "question": "כמה להשקיע בחודש?"},
    ]}

    async def test_an_unanswered_json_form_ends_the_reply_as_readable_text(self):
        async def event_call(payload):
            return {"error": "Event call timed out. The browser tab may be inactive or closed."}
        prompt = (f"{self.MARKER}\n```json\n"
                  + json.dumps(self.HEBREW_FORM, ensure_ascii=False, indent=2) + "\n```\n")
        out = await self._stream("Two things first.\n" + prompt, event_call)
        self.assertEqual(out, (
            "Two things first.\n"
            "1. **טווח**: לאיזה טווח השקעה?\n"
            "   - קצר - עד שנה\n"
            "   - ארוך - 5 שנים ומעלה\n"
            "2. **סיכון**: כמה סיכון אתה מוכן לקחת?\n"
            "   - נמוך - אג\"ח ומדדים\n"
            "   - גבוה\n"
            "3. **סכום**: כמה להשקיע בחודש?\n"
            "\n"
            "_Answer in your next message._"))

    def test_a_single_json_question_is_not_numbered(self):
        prompt = self.MARKER + "\n" + json.dumps({"questions": [
            {"question": "Where should the checks run?",
             "options": [{"label": "GitHub Actions", "description": "Free here"}, "GitLab CI"]}]})
        self.assertEqual(self.ocp._ask_user_fallback_text(prompt), (
            "Where should the checks run?\n"
            "- GitHub Actions - Free here\n"
            "- GitLab CI\n"
            "\n"
            "_Answer in your next message._"))
        # The text form keeps its text, marker line stripped.
        self.assertEqual(self.ocp._ask_user_fallback_text(self.MARKER + "\nColor?\n1. Red\n2. Blue"),
                         "Color?\n1. Red\n2. Blue")

    async def test_F4_a_fence_left_open_before_a_tool_call_does_not_hide_the_next_question(self):
        """A fence the previous message item left open does not continue past a tool call."""
        calls = []

        async def event_call(payload):
            calls.append(payload)
            return {"error": "Client session disconnected."}

        async def emitter(event):
            pass
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "go"}]}
        meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": "m1"}
        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta, __event_call__=event_call)

        async def drain():
            out = ""
            async for c in gen:
                out += c if isinstance(c, str) else ""
            return out
        task = asyncio.ensure_future(drain())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "Checking:\n```python\nx = 1\n"))
        for phase in ("start", "result"):
            q.put_nowait({"type": "event", "event": "agent", "payload": {
                "sessionKey": self.SK, "runId": "run-1", "stream": "tool",
                "data": {"phase": phase, "name": "Bash", "toolCallId": "t1", "args": {}}}})
        q.put_nowait(self._delta("run-1", f"{self.MARKER}\nDB\nWhich one?\n1. a\n2. b\n"))
        q.put_nowait(self._final("run-1"))
        out = await asyncio.wait_for(task, 5)
        self.assertEqual(len(calls), 1, "the question after the tool call was asked")
        self.assertNotIn(self.MARKER, out)

    async def test_a_card_nobody_can_see_ends_the_reply_with_the_question(self):
        """A card on a session with no live socket (OWUI answers `Client session disconnected.`)
        ends the reply at once with the question as text."""
        prompt = f"Let me ask.\n{self.MARKER}\nColour a1\nWhich colour do you prefer?\n1. Red\n2. Blue\n"
        calls = []
        fake_owui(self, SESSION_POOL={})         # the user has no tab open at all

        async def event_call(payload):
            calls.append(payload)
            return {"error": "Client session disconnected."}
        out = await self._stream(prompt, event_call)
        self.assertEqual(len(calls), 1)
        self.assertIn("Which colour do you prefer?", out)
        self.assertNotIn(self.MARKER, out)


class ToolStatusEventTests(_TurnCase):
    """ELI-115: every `status` event rewrites the whole chat, so a reply's statuses are
    "Thinking..." and its final done."""


    def _tool(self, phase, tcid):
        data = {"phase": phase, "name": "Bash", "toolCallId": tcid, "args": {"command": "ls"}}
        if phase == "result":
            data["result"] = "ok"
        return {"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": "run-1", "stream": "tool", "data": data}}

    async def test_a_reply_with_tool_calls_sends_no_status_per_tool(self):
        gen = self._reply("check the logs", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        for event in (self._delta("run-1", "Reading them."),
                      self._tool("start", "toolu_1"), self._tool("result", "toolu_1"),
                      self._tool("start", "toolu_2"), self._tool("result", "toolu_2"),
                      self._delta("run-1", "Both done."), self._final("run-1")):
            q.put_nowait(event)

        async def drain():
            got = [await first]
            async for chunk in gen:
                got.append(chunk)
            return got
        got = await asyncio.wait_for(drain(), 5)

        statuses = [(e["data"]["description"], e["data"]["done"])
                    for e in self.emitted if e.get("type") == "status"]
        self.assertEqual(statuses, [("Thinking...", False), ("", True)])
        # Each card runs, then its output flips it to done (`call_id or id`).
        cards = {}
        for c in got:
            item = c.get("item") if isinstance(c, dict) else None
            if item:
                cards.setdefault(item.get("call_id") or item.get("id"), []).append(
                    (item["type"], item.get("status")))
        for tcid in ("toolu_1", "toolu_2"):
            self.assertEqual(cards[tcid], [("function_call", "in_progress"),
                                           ("function_call_output", "completed")])


class InlineOwnerRuleTests(_TurnCase):
    """ELI-100 DESIGN 2.7: an interrupted inline turn hands M to the turn writer, whose settled row
    applies the owner rule. An error or Stop ends the reply."""

    # A handover needs a session key the connection can parse.
    USER, CHAT = str(uuid.UUID(int=1)), str(uuid.UUID(int=2))

    async def asyncSetUp(self):
        await NoDroppedMessagesTests.asyncSetUp(self)
        agent = self.pipe.valves.AGENT_ID
        self.conn._valves = lambda: types.SimpleNamespace(AGENT_ID=agent)
        type(self).SK = self.ocp._owui_session_key(agent, self.USER, self.CHAT)
        self.handed = []

        async def pump_loop(conn, sess, run, parsed, pump):
            self.handed.append((sess, run, pump))
        p = mock.patch.object(self.ocp, "_turn_pump_loop", pump_loop)
        p.start()
        self.patches.append(p)

    def _reply(self, text, msg_id):
        async def emitter(event):
            pass
        body = {"model": "openclaw_gateway.default",
                "messages": [{"role": "user", "content": text}]}
        meta = {"chat_id": self.CHAT, "user_id": self.USER, "message_id": msg_id}
        return self.pipe._pipe_impl(body, emitter, __metadata__=meta)

    def _event(self, event, **payload):
        return {"type": "event", "event": event,
                "payload": {"sessionKey": self.SK, "runId": "run-1", **payload}}

    def _lifecycle_error(self, err):
        # The Gateway stamps state='error' on the same payload.
        return self._event("agent", stream="lifecycle", state="error",
                           data={"phase": "error", "error": err})

    def _aborted(self):
        return self._event("chat", state="aborted")

    async def _turn(self, *steps):
        """A user turn whose run streams one delta, then `steps`: an event for its queue, or a
        callable on the connection."""
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "Reading them."))
        for step in steps:
            if callable(step):
                step()
            else:
                q.put_nowait(step)
        async def drain():
            out = await first
            async for chunk in gen:
                out += chunk if isinstance(chunk, str) else ""
            return out
        # Bounds the test only.
        out = await asyncio.wait_for(drain(), 5)
        for _ in range(20):
            await asyncio.sleep(0)
        return out

    def _restart(self):
        self.conn.connection_epoch = 2
        self.conn.gateway_boot_id = "boot-A"
        self.conn._note_gateway_boot("boot-B")

    def assert_handed_over(self, out, *, stop_notice_before=False):
        (sess, run, pump), = self.handed
        self.assertEqual((sess, run), (self.SK, "run-1"))
        self.assertEqual(pump.handover["message_id"], "m1")
        self.assertIs(pump.stop_notice_before, stop_notice_before)
        self.assertEqual(self.sent, ["hello"], "the inline turn sends no resume of its own")
        self.assertNotIn("**Error:**", out)
        self.assertNotIn(f"{self.SK}:run-1", self.conn._consumers)
        self.assertNotIn(f"{self.SK}:run-1", self.conn._run_targets)
        self.assertFalse(self.conn.was_delivered_live(self.SK, "run-1"),
                         "the writer still owns the run's later events")

    def assert_not_handed_over(self):
        self.assertEqual(self.handed, [])
        self.assertEqual(self.sent, ["hello"])

    def _tool_start(self, tcid="toolu_1"):
        return self._event("agent", stream="tool", data={
            "phase": "start", "name": "Bash", "toolCallId": tcid, "args": {"command": "sleep 25"}})

    async def _turn_items(self, *steps):
        """`_turn`, but returning the Responses items the request yielded."""
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "Reading them."))
        for step in steps:
            step() if callable(step) else q.put_nowait(step)

        async def drain():
            got = [await first]
            async for chunk in gen:
                got.append(chunk)
            return got
        got = await asyncio.wait_for(drain(), 5)
        for _ in range(20):
            await asyncio.sleep(0)
        return [c["item"] for c in got if isinstance(c, dict) and c.get("type") == "response.output_item.added"]

    def assert_no_call_left_for_open_webui(self, items):
        """Open WebUI's post-stream sweep (middleware.py) runs any `function_call` with no output
        and calls the model again."""
        handled = {i.get("call_id") for i in items if i.get("type") == "function_call_output"}
        calls = [i.get("call_id") or i.get("id") for i in items if i.get("type") == "function_call"]
        self.assertTrue(calls, "the test must stream a tool card")
        self.assertEqual([c for c in calls if c not in handled], [])

    async def test_a_crash_mid_tool_leaves_no_card_for_open_webui_to_run(self):
        """A crash mid-tool must leave no open card for Open WebUI to run."""
        items = await self._turn_items(self._tool_start(), self._restart)
        self.assertEqual(len(self.handed), 1)
        self.assert_no_call_left_for_open_webui(items)

    async def test_a_settled_row_read_before_the_kill_reaches_the_reply_still_decides(self):
        """ELI-159: a settled push that came before the handover had no pump to take it; the
        handover reads the row itself."""
        kill = "Claude Code process exited with signal SIGKILL"
        row = {"key": self.SK, "hasActiveRun": False, "status": "failed",
               "lastRunId": "run-1", "lastRunError": kill}
        fake = self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "sessions.list":
                return {"sessions": [dict(row)]}
            return await fake(method, params, timeout)
        self.conn.send_request = send_request
        await self._turn(self._lifecycle_error(kill),
                         lambda: self.conn._on_sessions_changed({"sessionKey": self.SK, **row}))
        (_, _, pump), = self.handed
        self.assertEqual(pump.queue.get_nowait(), ("settled", row))

    async def test_an_exit_143_mid_tool_leaves_no_card_for_open_webui_to_run(self):
        items = await self._turn_items(
            self._tool_start(), self._lifecycle_error("Claude Code process exited with code 143"))
        self.assertEqual(len(self.handed), 1)
        self.assert_no_call_left_for_open_webui(items)

    async def test_crash_hands_over_instead_of_resuming(self):
        """A new boot id and no terminal: no inline resume (the Gateway recovers)."""
        out = await self._turn(self._restart)
        self.assert_handed_over(out)

    async def test_container_stop_exit_143_hands_over(self):
        """`ha apps restart`: the CLI child exits 143 before the Gateway closes. The writer reads
        the `failed` row and resumes."""
        out = await self._turn(self._lifecycle_error("Claude Code process exited with code 143"))
        self.assert_handed_over(out)

    async def test_gateway_stop_abort_hands_over_with_the_notice(self):
        out = await self._turn(lambda: self.conn._on_shutdown({"reason": "gateway stopping"}),
                               self._aborted())
        self.assert_handed_over(out, stop_notice_before=True)

    async def test_restart_mode_abort_hands_over_to_the_recovery(self):
        """In restart mode the aborted row stays `running`; M stays open for the recovery."""
        out = await self._turn(lambda: self.conn._on_shutdown(
            {"reason": "gateway restarting", "restartExpectedMs": 1500}), self._aborted())
        self.assert_handed_over(out)

    async def test_an_abort_without_a_shutdown_is_a_stop(self):
        out = await self._turn(self._aborted())
        self.assert_not_handed_over()
        self.assertNotIn("**Error:**", out)

    async def test_a_real_error_is_shown_inline(self):
        out = await self._turn(self._lifecycle_error("429 rate limited"))
        self.assert_not_handed_over()
        self.assertIn(RUN_ERROR_SENTENCE, out)
        self.assertIn("```\n429 rate limited\n```", out)

    async def test_a_cli_exit_1_is_not_an_outside_kill(self):
        """`process exited with code 1` is the CLI failing, not an outside kill."""
        out = await self._turn(self._lifecycle_error("Claude Code process exited with code 1"))
        self.assert_not_handed_over()
        self.assertIn(RUN_ERROR_SENTENCE, out)
        self.assertIn("```\nClaude Code process exited with code 1\n```", out)

    async def test_the_handover_carries_the_thinking_level(self):
        """The writer's resume runs at the user's thinking level (ELI-85)."""
        await self._turn(self._restart)
        (_s, _r, pump), = self.handed
        self.assertIn("thinking", pump.handover)

    async def test_a_handover_after_a_restart_reads_the_row(self):
        """The restart marker can reach the inline loop after the post-hello resync; the handover
        reads the row itself."""
        await self._turn(self._restart)
        self.assertIn("sessions.list", self.rpc)


class BackgroundAgentLineTests(unittest.IsolatedAsyncioTestCase):
    """ELI-163: a background Agent call holds the reply open until it reports; its status row says
    why until the reply ends."""

    asyncTearDown = InlineOwnerRuleTests.asyncTearDown
    _queue = InlineOwnerRuleTests._queue
    _delta = InlineOwnerRuleTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = InlineOwnerRuleTests._until_registered
    _event = InlineOwnerRuleTests._event
    _turn = InlineOwnerRuleTests._turn
    USER, CHAT = InlineOwnerRuleTests.USER, InlineOwnerRuleTests.CHAT

    async def asyncSetUp(self):
        self.statuses = []
        await InlineOwnerRuleTests.asyncSetUp(self)

    def _reply(self, text, msg_id):
        async def emitter(event):
            if event.get("type") == "status":
                self.statuses.append(event["data"]["description"])
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": text}]}
        meta = {"chat_id": self.CHAT, "user_id": self.USER, "message_id": msg_id}
        return self.pipe._pipe_impl(body, emitter, __metadata__=meta)

    def _tool(self, phase, name, tcid, result=None):
        data = {"phase": phase, "name": name, "toolCallId": tcid, "args": {}}
        if result is not None:
            data["result"] = result
        return self._event("agent", stream="tool", data=data)

    async def test_the_reply_says_how_many_background_agents_it_waits_on(self):
        launched = "Async agent launched successfully. agentId: a1 (internal metadata)"
        await self._turn(
            self._tool("start", "Agent", "t1"), self._tool("result", "Agent", "t1", launched),
            self._tool("start", "Bash", "t2"), self._tool("result", "Bash", "t2", "ok"),
            self._tool("start", "Agent", "t3"), self._tool("result", "Agent", "t3", launched),
            self._tool("start", "Agent", "t4"), self._tool("result", "Agent", "t4", "Report: done"),
            self._final("run-1"))
        waiting = [s for s in self.statuses if s.startswith("Waiting on")]
        self.assertEqual(waiting, ["Waiting on 1 background agent", "Waiting on 2 background agents"])
        self.assertNotIn("Bash done", self.statuses)

    async def test_the_status_action_lists_the_agent_while_the_reply_runs(self):
        """ELI-186: Open WebUI saves tool cards only at the reply's end, so mid-reply the Status
        Action lists them from the streaming Pipe in the same process, then from what was saved."""
        import openclaw_status_action as action
        launched = "Async agent launched successfully. agentId: a1 (internal metadata)"
        # The chat mid-reply: done:false, no output yet.
        held = {"history": {"currentId": "m1", "messages": {"m1": {
            "id": "m1", "parentId": None, "role": "assistant", "done": False, "content": "", "timestamp": 5}}}}
        chats = types.ModuleType("open_webui.models.chats")
        chats.Chats = types.SimpleNamespace(
            get_chat_by_id=lambda chat_id: types.SimpleNamespace(user_id=self.USER, chat=held))
        # The Pipe and the Action meet on Open WebUI's module.
        stub = mock.patch.dict(sys.modules, {"open_webui": types.ModuleType("open_webui"),
                                             "open_webui.socket": types.ModuleType("open_webui.socket"),
                                             "open_webui.socket.main": types.ModuleType("open_webui.socket.main"),
                                             "open_webui.models": types.ModuleType("open_webui.models"),
                                             "open_webui.models.chats": chats})
        stub.start()
        self.addCleanup(stub.stop)

        async def listed():
            chat = await action._load_owui_recent(self.CHAT, self.USER)
            return [(r["title"], r["state"]) for r in action._agent_call_rows(chat)]

        gen = self._reply("hello", "m1")

        async def drain():
            async for _chunk in gen:
                pass
        reply = asyncio.ensure_future(drain())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        start = self._tool("start", "Agent", "t1")
        start["payload"]["data"]["args"] = {"description": "Vendor research", "run_in_background": True}
        for event in (self._delta("run-1", "Starting it."), start, self._tool("result", "Agent", "t1", launched)):
            q.put_nowait(event)

        async def until_waiting():
            while "Waiting on 1 background agent" not in self.statuses:
                await asyncio.sleep(0)
        # Bounds the test only.
        await asyncio.wait_for(until_waiting(), 5)
        self.assertFalse(reply.done())
        self.assertEqual(await listed(), [("Vendor research", "launched")])
        q.put_nowait(self._final("run-1"))
        await asyncio.wait_for(reply, 5)
        self.assertEqual(await listed(), [], "an ended reply is listed from what Open WebUI saved")

    def test_what_counts_as_a_background_agent(self):
        launch = self.ocp._is_background_agent_launch
        launched = "Async agent launched successfully."
        # Any case: the Gateway lowercases some tool names.
        self.assertTrue(launch("Agent", launched) and launch("agent", launched))
        self.assertFalse(launch("Agent", launched, True))
        self.assertFalse(launch("Agent", "Report: done"))
        self.assertFalse(launch("sessions_spawn", launched))

    async def test_an_agent_that_ran_in_the_foreground_is_not_waited_on(self):
        await self._turn(self._tool("start", "Agent", "t1"), self._tool("result", "Agent", "t1", "Report: done"),
                         self._final("run-1"))
        self.assertFalse([s for s in self.statuses if s.startswith("Waiting on")])
        self.assertNotIn("Agent done", self.statuses, "no status line per tool call (ELI-115)")

class GapHandoverReplayTests(_RoutingCase):
    """ELI-147 slice 4: lost events hand the reply to the turn writer, whose transcript rebuild
    restores them. Real request, receive loop and writer."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        test = self
        self.active = False                          # busy from its chat.send to its settle
        self.run = None
        self.chat_sends = []

        def row():
            return {"key": test.key, "hasActiveRun": test.active,
                    "status": "running" if test.active else "done", "lastRunId": test.run}

        async def send_request(method, params, timeout=10):
            test.sent.append((method, copy.deepcopy(params)))
            if method == "chat.send":
                test.chat_sends.append(params)
                test.active = True
                return {"runId": params["idempotencyKey"], "status": "started"}
            if method == "chat.history":
                return copy.deepcopy(test.history)
            if method == "sessions.list":
                # The row's response comes over the socket behind frames already sent, so an idle
                # row never overtakes the run's final.
                read, target = row(), test.socket.got + test.socket.frames.qsize()
                async with test.socket.moved:
                    await test.socket.moved.wait_for(lambda: test.socket.taken >= target)
                return {"sessions": [read]}
            if method == "sessions.subscribe":
                return {"list": {"sessions": [row()] if test.active else []}}
            if method == "sessions.describe":
                return {"session": {"status": "running" if test.active else "done"}}
            return {}
        self.conn.send_request = send_request
        self.conn.model_patch_cached = lambda *a, **k: True

        class Socket:
            def __init__(self_):
                self_.frames = asyncio.Queue()
                self_.got = self_.taken = 0             # received / handled frames
                self_.moved = asyncio.Condition()

            async def recv(self_):
                # Asked for the next frame: the one before is handled.
                self_.taken = self_.got
                async with self_.moved:
                    self_.moved.notify_all()
                frame = await self_.frames.get()
                self_.got += 1
                if frame == "drop":
                    raise websockets.exceptions.ConnectionClosed(None, None)
                return json.dumps(frame)
        self.socket = Socket()
        self.conn._ws = self.socket
        self.seq = 0

        async def reconnect():
            # A new socket: frame seq restarts and `_subscribe_all` renews subscriptions.
            test.conn._ws, test.conn._frame_seq, test.seq = test.socket, None, 0
            test.conn._subscriptions_stale = True
        self.conn._reconnect = reconnect
        loop = asyncio.ensure_future(self.conn._event_loop())

        async def stop_loop():
            self.conn._stopped = True
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
        self.addAsyncCleanup(stop_loop)

        import openclaw_pipe as ocp

        async def get_conn(_valves):
            return self.conn
        for patch in (mock.patch.object(ocp, "_get_gateway_connection", get_conn),
                      mock.patch.object(ocp, "_build_usage_status_lines",
                                        mock.AsyncMock(return_value=[]))):
            patch.start()
            self.addCleanup(patch.stop)
        self.pipe = Pipe()
        self.pipe.valves.AGENT_ID = "main"
        self.pipe.valves.AUTO_TITLE = False
        self.pipe.valves.INTERRUPT_GRACE_SECONDS = 0.3

    async def start_reply(self, text="count", message_id=None, intent=None, sends=True, request=None):
        """A user turn's request, running until its run is sent (or a parked run is consumed)."""
        message_id = message_id or self.STUB
        self.add_stub(message_id)
        emitted = []
        emitter = RecordingEmitter(emitted)

        async def event_call(event):
            return intent
        body = {"model": "openclaw_gateway.main", "messages": [{"role": "user", "content": text}]}
        meta = {"chat_id": self.CHAT, "user_id": self.USER, "message_id": message_id}
        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta, __request__=request,
                                   __event_call__=event_call if intent else None)
        reply = {"gen": gen, "text": "", "items": [], "emitted": emitted, "output": [],
                 "chunks": []}

        async def drain():
            try:
                async for chunk in gen:
                    reply["chunks"].append(chunk)
                    reply["output"] = _owui_fold_chunk(reply["output"], chunk)
                    if isinstance(chunk, str):
                        reply["text"] += chunk
                    elif isinstance(chunk, dict) and chunk.get("type") == "response.output_item.added":
                        reply["items"].append(chunk["item"])
            finally:
                await self.owui.end_request(message_id, reply["output"])
        sent_before = len(self.chat_sends)
        reply["task"] = asyncio.ensure_future(drain())
        for _ in range(400 if sends else 0):
            if len(self.chat_sends) > sent_before:
                break
            await asyncio.sleep(0.005)
        if self.run is None:
            self.run = self.chat_sends[-1]["idempotencyKey"]
        for _ in range(400):
            if self.conn.consumers_for_event({"sessionKey": self.key, "runId": self.run}):
                break
            await asyncio.sleep(0.005)
        return reply

    def frame(self, payload, event="agent"):
        self.seq += 1
        body = payload if event == "sessions.changed" else self.ev(self.run, payload)
        self.socket.frames.put_nowait({"type": "event", "event": event, "seq": self.seq, "payload": body})

    def lost(self, n=1):
        """Frames sent while the client was slow or down: seqs pass, nothing arrives."""
        self.seq += n

    def settle(self):
        self.active = False
        self.frame({"sessionKey": self.key, "hasActiveRun": False, "status": "done",
                    "lastRunId": self.run, "reason": "chat.run.settled"}, event="sessions.changed")

    async def ended(self, reply):
        await asyncio.wait_for(reply["task"], 5)   # bounds the test only
        await self.spin(600)

    def stored(self, message_id=None):
        m = self.owui.msgs()[message_id or self.STUB]
        return (m.get("done"), m.get("content"),
                sum(1 for i in m.get("output") or [] if i.get("type") == "function_call"))

    async def test_the_message_holds_the_tool_cards_before_open_webuis_end_save(self):
        """ELI-267: until Open WebUI's end save the message holds the Pipe's save: the live output
        copy (cards and text), or the text alone."""
        live = {}

        async def streams(redis, chat_id):
            return [{"chat_id": chat_id, "message_id": self.STUB, "output": live["reply"]["output"]}]
        sys.modules["open_webui.tasks"].get_response_streams_by_chat_id = streams
        at_end_save, end_request = [], self.owui.end_request

        async def end_save(message_id, output):
            at_end_save.append(copy.deepcopy(self.owui.msgs()[message_id]))
            await end_request(message_id, output)
        self.owui.end_request = end_save
        state = types.SimpleNamespace(redis=None)
        for request in (types.SimpleNamespace(app=types.SimpleNamespace(state=state)), None):
            with self.subTest(live_copy=request is not None):
                self.run, self.history = None, None
                mid = self.STUB if request is not None else str(uuid.uuid4())
                live["reply"] = reply = await self.start_reply(message_id=mid, request=request)
                self.history = _turn_history(run=self.run)
                for payload in (_delta("Reading them."), _tool("start", tcid="toolu_1"),
                                _tool("result", tcid="toolu_1"), _delta("\n\nFound it.")):
                    self.frame(payload)
                self.frame({"state": "final"}, event="chat")
                self.settle()
                await self.ended(reply)
                saved = at_end_save.pop()
                kinds = [i.get("type") for i in saved.get("output") or []]
                replaced = [e["data"]["content"] for e in reply["emitted"] if e.get("type") == "replace"]
                self.assertFalse(saved.get("done"))
                if request is not None:
                    self.assertEqual(saved.get("content"), "Reading them.\n\nFound it.")
                    self.assertEqual(saved["output"], reply["output"])
                    self.assertEqual((kinds.count("function_call"), kinds.count("function_call_output")), (1, 1))
                    self.assertEqual(replaced, [])
                else:
                    # The `replace` event is Open WebUI's to save; this emitter only records it.
                    self.assertEqual(kinds, [])
                    self.assertEqual(replaced, ["Reading them.\n\nFound it."])

    async def test_case_1_frames_lost_mid_run(self):
        """A slow client: the frames of a whole tool call were dropped (only the seq shows it)."""
        reply = await self.start_reply()
        self.history = _turn_history(run=self.run)
        self.frame(_delta("Reading them."))
        self.lost(2)                                   # the call's start and result
        self.frame(_delta("\n\nFound it."))
        self.frame({"state": "final"}, event="chat")
        self.settle()
        await self.ended(reply)
        self.assertEqual(self.stored(), (True, "Reading them.\n\nFound it.", 1))

    async def test_case_2_the_socket_drops_mid_tool_and_the_run_ends_while_down(self):
        """Everything after the call was sent while down; the row after is idle."""
        reply = await self.start_reply()
        self.history = _turn_history(run=self.run)
        self.frame(_delta("Reading them."))
        self.frame(_tool("start", tcid="toolu_1"))
        self.active = False
        self.socket.frames.put_nowait("drop")
        await self.ended(reply)
        self.assertEqual(self.stored(), (True, "Reading them.\n\nFound it.", 1))

    async def test_case_3_only_the_final_and_the_settle_are_lost(self):
        reply = await self.start_reply()
        self.history = _turn_history(run=self.run)
        for payload in (_delta("Reading them."), _tool("start", tcid="toolu_1"),
                        _tool("result", tcid="toolu_1"), _delta("\n\nFound it.")):
            self.frame(payload)
        self.active = False
        self.socket.frames.put_nowait("drop")
        await self.ended(reply)
        self.assertEqual(self.stored(), (True, "Reading them.\n\nFound it.", 1))

    @staticmethod
    def _nums(a, b):
        return "".join(f"{i}\n" for i in range(a, b + 1))

    async def test_a_drop_right_after_the_send(self):
        """A drop right after registering: the writer gives the message the whole run once; the
        request leaves it alone."""
        reply = await self.start_reply("count to 30")
        self.history = {"messages": [
            {"role": "user", "content": "count to 30", "idempotencyKey": f"{self.run}:user"},
            {"role": "assistant", "idempotencyKey": f"cli-assistant:{self.run}",
             "content": [{"type": "text", "text": self._nums(1, 30)}]}]}
        self.socket.frames.put_nowait("drop")          # 1-3 are sent while it is down
        self.frame(_delta(self._nums(4, 30)))
        self.frame({"state": "final"}, event="chat")
        self.settle()
        await self.ended(reply)
        self.assertEqual(self.stored(), (True, self._nums(1, 30).strip(), 0))
        self.assertEqual(reply["text"], "")
        self.assertEqual([e for e in reply["emitted"] if e.get("type") == "replace"], [])
        self.assertEqual(len(self.chat_sends), 1, "no retry or resend")

    # Real uuids: only a uuid makes the run the Pipe's own (`parse_pipe_run_id`).
    M1 = "11111111-1111-4111-8111-111111111111"
    M2 = "22222222-2222-4222-8222-222222222222"

    async def _send_now_mid_tool(self, cancel_event=False):
        """Reply 1 streams 1..10 and starts a tool call; Send now; reply 2 brings MSG2. Returns
        reply 2 and run 1's id. `cancel_event`: `chat:tasks:cancel` reaches the tab first
        (`self.cancel_at`, `self.cancelled_output`)."""
        first = await self.start_reply("count to 20", self.M1, intent="send_now")
        run1 = self.run
        self.frame(_delta(self._nums(1, 10)))
        self.frame(_tool("start", tcid="toolu_1"))
        for _ in range(400):
            if first["items"]:
                break
            await asyncio.sleep(0.005)
        first["task"].cancel()                         # Send now
        await asyncio.gather(first["task"], return_exceptions=True)
        self.first = first
        if cancel_event:
            # The page answered Send now: the run is held for MSG2 or already taken over.
            for _ in range(400):
                entry = self.conn._pending_cancel_aborts.get(self.key)
                if entry is not None and entry["resolved"].is_set():
                    break
                if entry is None and self._writer_events(self.M1):
                    break
                await asyncio.sleep(0.005)
            self.cancel_at = len(self.owui.emits)
            self.cancelled_output = _owui_cancelled_output(_owui_stream(first["chunks"])[1])
        second = await self.start_reply("MSG2", self.M2, intent="send_now", sends=False)
        for _ in range(400):
            if (self.M1 in [k[1] for k in self.conn._turn_writers]
                    and self.owui.msgs()[self.M1].get("done") is False):
                break
            await asyncio.sleep(0.005)
        return second, run1

    @staticmethod
    def _history(run, *parts, text=None):
        """A claude-cli turn as stored: user message, imported assistant content, closer."""
        imported = {"importedFrom": "claude-cli"}
        closing = text if text is not None else "".join(
            p.get("text", "") for p in parts if p.get("type") == "text")
        return {"messages": [
            {"role": "user", "content": "count to 20", "idempotencyKey": f"{run}:user"},
            {"role": "assistant", "__openclaw": imported, "content": list(parts)},
            {"role": "assistant", "idempotencyKey": f"cli-assistant:{run}",
             "content": [{"type": "text", "text": closing}]}]}

    @staticmethod
    def _call_ids(out):
        return ([i.get("call_id") or i.get("id") for i in out if i.get("type") == "function_call"],
                [i.get("call_id") or i.get("id") for i in out if i.get("type") == "function_call_output"])

    def _calls(self, message_id):
        return self._call_ids(self.owui.msgs()[message_id].get("output") or [])

    def _shown_calls(self, message_id):
        """What a tab opening the chat now sees mid-turn: the latest response stream over the DB
        copy (`overlay_response_streams`)."""
        outs = [o for _t, m, _c, o in self.owui.streams if m == message_id]
        return self._call_ids(outs[-1] if outs else [])

    async def _answer_msg2(self, second):
        for _ in range(400):
            if len(self.chat_sends) == 2:
                break
            await asyncio.sleep(0.005)
        self.assertEqual([p["message"] for p in self.chat_sends], ["count to 20", "MSG2"])
        self.run, self.active = self.chat_sends[1]["idempotencyKey"], True
        for _ in range(400):
            if self.conn.consumers_for_event({"sessionKey": self.key, "runId": self.run}):
                break
            await asyncio.sleep(0.005)
        self.frame(_delta("ANSWER"))
        self.frame({"state": "final"}, event="chat")
        self.settle()
        await self.ended(second)

    async def test_send_now_keeps_a_running_tool_card_running_in_its_own_message(self):
        """ELI-158: Send now while a tool runs. The run stays in its own message, its card running
        until the tool ends; the new message gets only its own answer."""
        second, run1 = await self._send_now_mid_tool()
        self.assertEqual(self._calls(self.M1), (["toolu_1"], []), "the card is still running")
        self.assertEqual(len(self.chat_sends), 1, "MSG2 waits for the run")
        self.history = self._history(
            run1, {"type": "text", "text": self._nums(1, 10)},
            {"type": "toolcall", "id": "toolu_1", "name": "Bash", "arguments": {"command": "ls"}},
            {"type": "tool_result", "tool_use_id": "toolu_1", "name": "Bash",
             "content": [{"type": "text", "text": "ok"}]},
            {"type": "text", "text": self._nums(11, 20)})
        self.frame(_tool("result", tcid="toolu_1"))
        for _ in range(400):
            if self._shown_calls(self.M1)[1]:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(self._shown_calls(self.M1), (["toolu_1"], ["toolu_1"]),
                         "done when the tool ends")
        self.frame(_delta(self._nums(11, 20)))
        self.frame({"state": "final"}, event="chat")
        self.active = False
        self.settle()
        await self._answer_msg2(second)
        m1, m2 = self.owui.msgs()[self.M1], self.owui.msgs()[self.M2]
        self.assertIs(m1.get("done"), True)
        self.assertIn(self._nums(11, 20).strip(), m1.get("content"))
        self.assertEqual(m2.get("content"), "ANSWER")
        self.assertEqual(self._calls(self.M2), ([], []), "the run's card is not repeated")

    async def test_the_tab_shows_the_kept_run_once(self):
        """ELI-158: the kept run shows once. Message items carry Open WebUI's ids, so the writer's
        first `output_item.added` must not splice in a second copy."""
        if shutil.which("bun") is None:
            if os.environ.get("CI"):
                self.fail("bun is missing: the live-order tests must run in CI")
            self.skipTest("bun runs the vendored Open WebUI frontend reducer")
        await self._send_now_mid_tool()
        self.frame(_tool("result", tcid="toolu_1"))
        self.frame(_delta(self._nums(11, 13)))
        for _ in range(400):
            if any(m == self.M1 and "13" in c for _t, m, c, _o in self.owui.streams):
                break
            await asyncio.sleep(0.005)
        writer = [d["data"]["data"] for e, d, _room in self.owui.emits
                  if e == "events" and d["message_id"] == self.M1
                  and d["data"].get("type") == "response:completion"]
        live = _owui_live_output(_owui_stream(self.first["chunks"])[2] + writer)
        order = _item_order(live)
        self.assertEqual([t for t, _id, _x in order],
                         ["message", "function_call", "function_call_output", "message"], order)
        self.assertEqual(self._count(" ".join(x for _t, _id, x in order if x)),
                         list(range(1, 14)), order)

    def _writer_events(self, message_id, since=0):
        return [d["data"]["data"] for e, d, _room in self.owui.emits[since:]
                if e == "events" and d["message_id"] == message_id
                and d["data"].get("type") == "response:completion"]

    async def test_the_kept_runs_card_stays_running_after_open_webuis_cancel(self):
        """Open WebUI's `chat:tasks:cancel` stamps the running call `incomplete` (drawn done) and
        can land after the writer's events. The tab folds it first, so the card stays running
        (#83)."""
        if shutil.which("bun") is None:
            if os.environ.get("CI"):
                self.fail("bun is missing: the live-order tests must run in CI")
            self.skipTest("bun runs the vendored Open WebUI frontend reducer")
        await self._send_now_mid_tool(cancel_event=True)
        stamped = [i for i in self.cancelled_output if i.get("type") == "function_call"]
        self.assertEqual([i.get("status") for i in stamped], ["incomplete"],
                         "Open WebUI's cancel shows the running card done")
        for _ in range(400):
            if self._writer_events(self.M1, since=self.cancel_at):
                break
            await asyncio.sleep(0.005)
        live = _owui_live_output(
            [{"type": "chat:tasks:cancel", "data": {"output": self.cancelled_output}}]
            + self._writer_events(self.M1, since=self.cancel_at))
        calls = [i for i in live if i.get("type") == "function_call"]
        self.assertEqual([(i.get("id"), i.get("status")) for i in calls],
                         [("toolu_1", "in_progress")], "the card is running")
        self.assertEqual([i for i in live if i.get("type") == "function_call_output"], [])
        self.assertEqual(self._count(" ".join(x for _t, _id, x in _item_order(live) if x)),
                         list(range(1, 11)), live)

    async def test_a_gap_in_the_kept_run_is_repaired_in_its_own_message(self):
        """A kept run that loses frames is rebuilt in its own message (ELI-158)."""
        second, run1 = await self._send_now_mid_tool()
        self.history = self._history(
            run1, {"type": "text", "text": self._nums(1, 10)},
            {"type": "toolcall", "id": "toolu_1", "name": "Bash", "arguments": {"command": "ls"}},
            {"type": "tool_result", "tool_use_id": "toolu_1", "name": "Bash",
             "content": [{"type": "text", "text": "ok"}]},
            {"type": "text", "text": self._nums(11, 20)})
        self.frame(_tool("result", tcid="toolu_1"))
        self.frame(_delta(self._nums(11, 13)))
        self.lost(1)                                   # 14-15
        self.frame(_delta(self._nums(16, 20)))
        self.frame({"state": "final"}, event="chat")
        self.active = False
        self.settle()
        await self._answer_msg2(second)
        m1 = self.owui.msgs()[self.M1].get("content")
        self.assertEqual(self._count(m1), list(range(1, 21)), m1)
        self.assertEqual(self.owui.msgs()[self.M2].get("content"), "ANSWER")

    async def test_a_kept_run_killed_from_outside_ends_in_its_own_message(self):
        """A kept run killed from outside (exit 143) ends with the cut-off note, not resumed, since
        a resume would race MSG2 (ELI-159); MSG2 goes after it alone."""
        second, run1 = await self._send_now_mid_tool()
        self.history = self._history(run1, {"type": "text", "text": self._nums(1, 10)})
        kill = "Claude Code process exited with code 143"
        self.frame({"stream": "lifecycle", "data": {"phase": "error", "error": kill}})
        self.active = False
        self.frame({"sessionKey": self.key, "hasActiveRun": False, "status": "failed",
                    "lastRunId": run1, "lastRunError": kill, "reason": "chat.run.settled"},
                   event="sessions.changed")
        await self._answer_msg2(second)
        self.assertIs(self.owui.msgs()[self.M1].get("done"), True)
        self.assertIn("cut off", self.owui.msgs()[self.M1].get("content"))
        self.assertEqual(self.owui.msgs()[self.M2].get("content"), "ANSWER")

    @staticmethod
    def _count(text):
        return [int(t) for t in text.split() if t.isdigit()]


class ToolCardParityTests(unittest.IsolatedAsyncioTestCase):
    """ELI-147 slice 2: inline reply and turn writer show the same cards for one recorded run: a
    success, a failure, a result with no start, and an unanswered call."""

    asyncSetUp = InlineOwnerRuleTests.asyncSetUp
    asyncTearDown = InlineOwnerRuleTests.asyncTearDown
    _queue = InlineOwnerRuleTests._queue
    _delta = InlineOwnerRuleTests._delta
    _until_registered = InlineOwnerRuleTests._until_registered
    _reply = InlineOwnerRuleTests._reply
    _event = InlineOwnerRuleTests._event
    _turn_items = InlineOwnerRuleTests._turn_items
    USER, CHAT = InlineOwnerRuleTests.USER, InlineOwnerRuleTests.CHAT

    def _tool(self, phase, tcid, name, **data):
        return self._event("agent", stream="tool", data={
            "phase": phase, "name": name, "toolCallId": tcid, **data})

    def _run(self):
        return [
            self._tool("start", "t1", "Read", args={"path": "a"}),
            self._tool("result", "t1", "Read", result="contents"),
            self._tool("start", "t2", "Bash", args={"command": "false"}),
            self._tool("result", "t2", "Bash", result="exit 1", isError=True),
            self._tool("result", "t3", "Grep", args={"q": "x"}, result="hit"),
            self._tool("start", "t4", "Bash", args={"command": "sleep 99"}),
            NoDroppedMessagesTests._final(self, "run-1"),
        ]

    @staticmethod
    def _fold(items):
        """The output list Open WebUI keeps from `output_item.added`: same id replaces, else append
        (`handle_responses_streaming_event`)."""
        out = []
        for item in items:
            at = next((i for i, o in enumerate(out) if o.get("id") == item.get("id")), None)
            if at is None:
                out.append(item)
            else:
                out[at] = item
        return out

    @staticmethod
    def _cards(items):
        """What a card shows: the call's name and arguments, its output."""
        cards = []
        for i in items:
            if i["type"] == "function_call":
                cards.append(("call", i["id"], i["name"], i["arguments"]))
            elif i["type"] == "function_call_output":
                cards.append(("output", i["call_id"], i["output"][0]["text"]))
        return cards

    async def test_the_inline_reply_and_the_writer_show_the_same_cards(self):
        run = self._run()
        inline = self._cards(self._fold(await self._turn_items(*run)))
        renderer = _TurnRenderer()
        for frame in run:
            renderer.feed(frame["payload"])
        writer = self._cards(renderer.finished_output())
        self.assertEqual(inline, writer)
        self.assertIn(("call", "t2", f"Bash {TOOL_ERROR_MARK}", json.dumps({"command": "false"})),
                      writer)
        self.assertIn(("call", "t3", "Grep", json.dumps({"q": "x"})), writer,
                      "a result whose start was missed still gets its card")
        self.assertIn(("output", "t4", TOOL_NO_RESULT_NOTE), writer,
                      "a call the run never answered is closed, not left spinning")


class TextParityTests(unittest.IsolatedAsyncioTestCase):
    """ELI-147 slice 3: inline reply and turn writer show a run's text the same: dedup, metadata
    filter, `MEDIA:` directives and needs-input prompts. No tab answers on either side."""

    asyncSetUp_inline = InlineOwnerRuleTests.asyncSetUp
    asyncTearDown = InlineOwnerRuleTests.asyncTearDown
    _queue = InlineOwnerRuleTests._queue
    _until_registered = InlineOwnerRuleTests._until_registered
    _reply = InlineOwnerRuleTests._reply
    _event = InlineOwnerRuleTests._event
    USER, CHAT = InlineOwnerRuleTests.USER, InlineOwnerRuleTests.CHAT

    PROMPT = "OpenClaw needs input: Which branch?\n1. main\n2. dev"
    # The documented form, marker on its own line (ELI-151).
    PROMPT_OWN_LINE = "OpenClaw needs input:\nWhich branch?\n1. main\n2. dev"
    IMG = "![chart.png](http://localhost:18791/chart.png)"

    async def asyncSetUp(self):
        await self.asyncSetUp_inline()
        self.owui = _FakeOwuiChat(self, self.CHAT)

    @staticmethod
    def A(text):
        return ("assistant", {"delta": text})

    @staticmethod
    def W(text):
        return ("assistant", {"text": text})

    @staticmethod
    def I(kind, text):
        return ("item", {"kind": kind, "text": text})

    @staticmethod
    def T(phase, tcid, **data):
        return ("tool", {"phase": phase, "name": "Bash", "toolCallId": tcid, **data})

    def cases(self):
        A, W, I, T, P, P2 = self.A, self.W, self.I, self.T, self.PROMPT, self.PROMPT_OWN_LINE
        tool = [T("start", "t1", args={}), T("result", "t1", result="ok")]
        # ELI-151: a question on the marker line is kept; only the marker goes.
        q = "Which branch?\n1. main\n2. dev"
        return {
            "catch-all equal": ([A("Hello "), A("world."), W("Hello world.")], "Hello world."),
            "catch-all with more": ([A("Hello "), W("Hello world.")], "Hello world."),
            "item echo": ([A("Hello world."), I("message", "Hello world.")], "Hello world."),
            "preamble, tool, text": ([I("preamble", "Let me check."), *tool, A("All good.")],
                                     "Let me check.All good."),
            "text, tool, text, catch-all": ([A("Checking."), *tool, A("Done."), W("Checking.Done.")],
                                            "Checking.Done."),
            "metadata block": ([A("Hi. "), A("Sender (untrusted metadata): x"), A("Bye.")], "Hi. Bye."),
            "item grows": ([I("preamble", "Let"), I("preamble", "Let me check."), A(" More.")],
                           "Let me check. More."),
            "two items": ([I("message", "First."), *tool, I("message", "Second.")], "First.Second."),
            "prompt in deltas": ([A("Before.\n\n"), A(P)], "Before.\n\n" + q),
            "prompt in an item": ([A("Before.\n\n"), I("message", "Before.\n\n" + P)], "Before.\n\n" + q),
            "prompt, then catch-all": ([A("Before.\n\n"), A(P[:25]), A(P[25:]), W("Before.\n\n" + P)],
                                       "Before.\n\n" + q),
            "prompt in a fence": ([A("Example:\n```\n" + P + "\n```\nThat's it.")],
                                  "Example:\n```\n" + P + "\n```\nThat's it."),
            "prompt, tool, text": ([A("Q:\n\n" + P + "\n\n"), *tool, A("After the tool.")],
                                   "Q:\n\n" + q + "\n\nAfter the tool."),
            "own-line prompt in deltas": ([A("Before.\n\n"), A(P2)], "Before.\n\n" + q),
            "own-line prompt in an item": ([A("Before.\n\n"), I("message", "Before.\n\n" + P2)],
                                           "Before.\n\n" + q),
            "own-line prompt, tool, text": ([A("Q:\n\n" + P2 + "\n\n"), *tool, A("After the tool.")],
                                            "Q:\n\n" + q + "\n\nAfter the tool."),
            "prompt mid-line, one delta": ([A("Checking. " + P)], "Checking. " + P),
            "media": ([A("Here: MEDIA:chart.png "), A("done.")], f"Here: {self.IMG}\ndone."),
            "media at the end": ([A("Here: MEDIA:chart.png")], f"Here: {self.IMG}"),
            "media in code": ([A("Use `MEDIA:chart.png` to show it.")], "Use `MEDIA:chart.png` to show it."),
            # Differed on main; the expected text is the side that was right.
            "media before a tool card (inline)": ([A("See MEDIA:chart.png"), *tool, A("Next.")],
                                                  f"See {self.IMG}Next."),
            "prompt mid-line, two deltas (writer)": ([A("Checking. "), A(P)], "Checking. " + P),
            "media in an item (writer)": ([I("message", "Look: MEDIA:chart.png done.")],
                                          f"Look: {self.IMG}\ndone."),
            "media ending an item (writer)": ([I("message", "Look: MEDIA:chart.png")], f"Look: {self.IMG}"),
            "prompt after text in an item (writer)": ([I("message", "Some context.\n\n" + P)],
                                                      "Some context.\n\n" + q),
            "prompt mid-line after an item (writer)": ([I("preamble", "Checking."), A(" " + P)],
                                                       "Checking. " + P),
        }

    async def inline(self, steps):
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        for stream, data in steps:
            q.put_nowait(self._event("agent", stream=stream, data=data))
        q.put_nowait(NoDroppedMessagesTests._final(self, "run-1"))
        got = [await asyncio.wait_for(first, 5)]
        async for chunk in gen:
            got.append(chunk)
        return "".join(c for c in got if isinstance(c, str))

    async def writer(self, steps, message_id):
        w = _TurnWriter(self.conn, session_key=self.SK, chat_id=self.CHAT, user_id=self.USER,
                        run_id="run-w", message_id=message_id)
        for stream, data in steps:
            w.renderer.feed({"stream": stream, "data": data})
        w.opened = True
        self.conn._valves = lambda: self.pipe.valves
        # What `end` does when no tab answers, then `finalize`'s view.
        with mock.patch.object(_TurnWriter, "_ask_card", new=mock.AsyncMock(return_value=False)):
            await w._ask_question()
        return (await w._view(final=True))[1]

    async def test_the_inline_reply_and_the_writer_show_the_same_text(self):
        for n, (name, (steps, expected)) in enumerate(self.cases().items()):
            with self.subTest(name):
                if n:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                inline = await self.inline(steps)
                writer = await self.writer(steps, f"w{n}")
                self.assertEqual((inline, writer), (expected, expected))


class ApiCallerOwnerRuleTests(_TurnCase):
    """An API request names no message to hand over, so on interruption the reply applies the owner
    rule itself in its own stream: resume, follow the recovery run, or end."""

    _event = InlineOwnerRuleTests._event
    _lifecycle_error = InlineOwnerRuleTests._lifecycle_error
    _restart = InlineOwnerRuleTests._restart
    USER, CHAT = InlineOwnerRuleTests.USER, InlineOwnerRuleTests.CHAT
    REC = "8f27e4c8-ed9a-4acf-9df1-992ee2f4e801"

    async def asyncSetUp(self):
        await InlineOwnerRuleTests.asyncSetUp(self)
        self.history = {"messages": []}
        inner = self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "chat.history":
                self.rpc.append(method)
                return copy.deepcopy(self.history)
            return await inner(method, params, timeout)
        self.conn.send_request = send_request

    def _api_reply(self):
        async def emitter(event):
            pass
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "hello"}]}
        return self.pipe._pipe_impl(body, emitter, __metadata__={"chat_id": self.CHAT, "user_id": self.USER})

    async def _start(self):
        """The API reply streams `Reading them.` of run-1; returns the task draining it."""
        gen = self._api_reply()

        async def drain():
            out = ""
            async for chunk in gen:
                out += chunk if isinstance(chunk, str) else ""
            return out
        task = asyncio.ensure_future(drain())
        await self._until_registered("run-1")
        self.active = True                   # sessions.list: the run is going
        self._queue("run-1").put_nowait(self._delta("run-1", "Reading them."))
        await self.spin()
        return task

    async def spin(self, n=50):
        for _ in range(n):
            await asyncio.sleep(0)

    def row(self, **row):
        self.conn._on_sessions_changed({"sessionKey": self.SK, **row})

    async def test_an_outside_kill_is_resumed_in_the_same_stream(self):
        task = await self._start()
        self._queue("run-1").put_nowait(self._lifecycle_error("Claude Code process exited with code 143"))
        await self.spin()
        self.row(hasActiveRun=False, status="failed", lastRunId="run-1",
                 lastRunError="Claude Code process exited with code 143")
        await self._until_registered("run-2")
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Reading them.", self.sent[1], "the resume says what the user already sees")
        self.assertIn("hello", self.sent[1], "the resume carries the request (a new CLI session)")
        self._queue("run-2").put_nowait(self._delta("run-2", " Found it."))
        self._queue("run-2").put_nowait(self._final("run-2"))
        out = await asyncio.wait_for(task, 5)
        self.assertIn("Reading them. Found it.", out)
        self.assertNotIn("**Error:**", out)
        self.assertEqual(self.handed, [], "no turn writer: there is no message")

    async def test_a_crash_follows_the_gateways_recovery_run(self):
        task = await self._start()
        self._restart()
        await self.spin()
        # The recovery's first events arrive before the reply knows them: held.
        rec = lambda payload: {"sessionKey": self.SK, "runId": self.REC, **payload}
        _turn_route_now(self.conn, rec({"stream": "lifecycle", "data": {"phase": "start"}}))
        _turn_route_now(self.conn, rec({"stream": "assistant", "data": {"delta": " Found it."}}))
        self.history = {"messages": [
            {"role": "user", "content": "hello", "idempotencyKey": "run-1:user"},
            {"role": "user", "content": "[System] continue", "idempotencyKey": f"{self.REC}:user",
             "provenance": {"kind": "internal_system", "sourceTool": "main_session_restart_recovery"}}]}
        self.row(hasActiveRun=True, status="running", phase="start", runId=self.REC)
        await self._until_registered(self.REC)
        self._queue(self.REC).put_nowait({"type": "event", "event": "chat", "payload": rec({"state": "final"})})
        out = await asyncio.wait_for(task, 5)
        self.assertIn("Reading them. Found it.", out)
        self.assertNotIn("**Error:**", out)
        self.assertEqual(self.sent, ["hello"], "the Gateway continues it: no resume of our own")
        self.assertEqual(self.handed, [])

    async def test_a_recovery_the_gateway_gives_up_on_ends_the_reply(self):
        """A recovery the Gateway gives up on is tombstoned `failed`
        (main-session-restart-recovery-CPJjd8da.mjs:1150), which ends the reply."""
        task = await self._start()
        self._restart()
        await self.spin()
        self.row(hasActiveRun=False, status="failed", lastRunId="run-1",
                 lastRunError="restart recovery gave up")
        out = await asyncio.wait_for(task, 5)
        self.assertIn("Reading them.", out)
        self.assertIn(RUN_ERROR_SENTENCE, out)
        self.assertIn("```\nrestart recovery gave up\n```", out)
        self.assertEqual(self.sent, ["hello"])


class SessionIdRunTests(_RoutingCase):
    """ELI-103: cron runs every wake as runId = sessionId (run-executor.runtime-ro-4Z0fM.mjs:499).
    A wake is its user turn `<turnId>:user` (cli-runner-9MSP9PU4.mjs:1370);
    `cli-assistant:<sessionId>` is the first wake's only (session-accessor-CX17Bx59.mjs:3343)."""

    SID = "ad9e461b-724a-4797-9b3f-befa1090e36b"
    M = "7c5fe99c-cb51-4f24-90ca-1fd2bc78ae28"
    JOB = "f8f7cc90-5a10-4a5f-a12d-aa30402f9371"
    W1 = "98a3ba9e-f500-4875-85f1-c181040bca1a"
    W2 = "1d549bd7-80a0-448a-ab9e-3dcbcad0015b"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.R = pipe_run_id(self.CHAT, self.M)
        self.add_stub(self.M, content="Deploying in the background.", done=True)
        self.conn.mark_delivered_live(self.key, self.R)
        self.history = {"messages": []}
        self.now = 1_791_860_559_000
        self.jobs = []
        self.cron_runs = []
        # This session's sessionId is already known.
        self.conn.note_session_id(self.key, self.SID)
        plain = self.conn.send_request
        test = self

        async def send_request(method, params, timeout=10):
            if method == "cron.list":
                test.sent.append((method, copy.deepcopy(params)))
                return {"jobs": copy.deepcopy(test.jobs)}
            if method == "cron.runs":
                return {"entries": [e for e in test.cron_runs if params.get("id") == e["jobId"]]}
            if method == "sessions.list":
                test.sent.append((method, copy.deepcopy(params)))
                return {"sessions": [{"key": test.key, "sessionId": test.SID, "hasActiveRun": False,
                                      "status": "done"}]}
            return await plain(method, params, timeout)
        self.conn.send_request = send_request

    def tick(self):
        self.now += 1000
        return self.now

    def start_job(self):
        """The cron executor marks its job running before the run's first event."""
        self.jobs = [{"id": self.JOB, "sessionTarget": f"session:{self.key}",
                      "state": {"runningAtMs": self.tick()}}]

    def wake_turn(self, key, text="Wake: deploy finished.", started=False):
        """Cron marks the job running (unless `started`), then commits the wake's turn."""
        at = self.tick()
        if not started:
            self.jobs = [{"id": self.JOB, "sessionTarget": f"session:{self.key}",
                          "state": {"runningAtMs": at - 400}}]
        self.history["messages"].append({
            "role": "user", "content": f"[cron:{self.JOB} eli100-wake] {text}",
            "idempotencyKey": f"{key}:user", "timestamp": at - 150,
            "__openclaw": {"idempotencyKey": f"{key}:user", "recordTimestampMs": at}})

    def queued_turn(self, text="status?"):
        """A chat.send queued behind the wake: committed at admission."""
        run = pipe_run_id(self.CHAT, "efe80f6a-ffb4-48eb-a044-f9b99d2e4fe6")
        at = self.tick()
        self.history["messages"].append({
            "role": "user", "content": text, "idempotencyKey": f"{run}:user", "timestamp": at,
            "__openclaw": {"idempotencyKey": f"{run}:user", "recordTimestampMs": at}})

    def imported(self, text):
        """claude-cli's own transcript, imported at the run's end with its own times."""
        self.history["messages"].append({"role": "assistant", "timestamp": self.tick(),
                                         "__openclaw": {"importedFrom": "claude-cli"},
                                         "content": [{"type": "text", "text": text}]})

    def closing(self, text):
        key = f"cli-assistant:{self.SID}"
        if not any((m.get("__openclaw") or {}).get("idempotencyKey") == key
                   for m in self.history["messages"]):  # deduplicated by key
            self.history["messages"].append({"role": "assistant", "content": text, "timestamp": self.tick(),
                                             "__openclaw": {"idempotencyKey": key}})

    def sid_ev(self, payload):
        return {"sessionKey": self.key, "runId": self.SID, "ts": self.tick(), **payload}

    async def wake(self, key, *texts):
        self.wake_turn(key)
        for text in texts:
            _turn_route_now(self.conn, self.sid_ev(_delta(text)))
        self.imported("".join(texts))
        self.closing("".join(texts))
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        self.job_finished()
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.SID})
        await self.spin()

    def job_finished(self):
        """A one-shot job is deleted after its run; cron.runs keeps the run."""
        for j in self.jobs:
            self.cron_runs.append({"jobId": j["id"], "action": "finished",
                                   "runAtMs": j["state"]["runningAtMs"], "ts": self.tick()})
        self.jobs = []

    def new_messages(self):
        return {k: v for k, v in super().new_messages().items() if k != self.M}

    async def test_a_wake_ended_after_a_reconnect_keeps_only_its_own_reply(self):
        """The wake ends at its job run's finish (cron.runs ts), not the reconnect time, so the
        queued run's reply stays out of it."""
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev(_delta("Working.")))
        await self.spin()
        self.queued_turn()
        self.imported("Working. Done.")
        self.job_finished()
        self.imported("Status: all green.")
        self.history["messages"].append({"role": "assistant", "timestamp": self.tick(),
                                         "content": "Status: all green.",
                                         "__openclaw": {"idempotencyKey": f"cli-assistant:{self.R}"}})
        await _wake_resync(self.conn)
        await self.spin()
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Working. Done.")

    async def test_with_no_finish_time_the_wake_keeps_what_it_streamed(self):
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev(_delta("Working.")))
        await self.spin()
        self.jobs = []  # gone, and cron.runs has no entry for it
        self.imported("Working. Done.")
        self.imported("Someone else's reply.")
        await _wake_resync(self.conn)
        await self.spin()
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Working.")

    async def test_stray_events_do_not_land_in_the_next_wake(self):
        """A stray (runId = sessionId, no running wake) is not flushed into the next wake."""
        await self.wake(self.W1, "First wake.")
        _turn_route_now(self.conn, self.sid_ev(_tool("start", tcid="toolu_stray")))
        await self.spin()
        self.assertNotIn((self.key, self.SID), self.conn._turn_pumps, "the stray pump is closed")
        await self.wake(self.W2, "Second wake.")
        second = self.new_messages()[turn_message_id(self.key, self.W2, self.CHAT)]
        self.assertEqual([i for i in second.get("output") or [] if i.get("type") == "function_call"], [])
        self.assertEqual(second["content"], "Second wake.")

    async def test_held_events_older_than_the_wakes_job_run_are_dropped(self):
        early = self.sid_ev(_tool("start", tcid="toolu_early"))  # emitted before the job started
        self.start_job()
        _turn_route_now(self.conn, early)
        await self.spin()
        self.wake_turn(self.W1, started=True)
        _turn_route_now(self.conn, self.sid_ev(_delta("Own text.")))
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        (m,) = self.new_messages().values()
        self.assertEqual(m["content"], "Own text.")
        self.assertEqual([i for i in m.get("output") or [] if i.get("type") == "function_call"], [])

    async def test_a_wake_turn_beyond_the_first_50_messages_is_found(self):
        """A fresh pump's read widens past 50 messages until before the job's start."""
        plain = self.conn.send_request
        limits = []

        async def send_request(method, params, timeout=10):
            if method == "chat.history":
                limits.append(params.get("limit"))
                msgs = self.history["messages"]
                return {"messages": copy.deepcopy(msgs[-params.get("limit", 1000):])}
            return await plain(method, params, timeout)
        self.conn.send_request = send_request
        self.wake_turn(self.W1)
        for i in range(120):
            self.imported(f"step {i}")
        _turn_route_now(self.conn, self.sid_ev(_delta("Still going.")))
        await self.spin()
        self.assertEqual(list(self.new_messages()), [turn_message_id(self.key, self.W1, self.CHAT)])
        self.assertEqual(limits[:2], [50, 200])

    async def test_the_read_stops_once_it_reaches_before_the_jobs_start(self):
        """The widening stops once it reaches before the job's start."""
        plain = self.conn.send_request
        limits = []

        async def send_request(method, params, timeout=10):
            if method == "chat.history":
                limits.append(params.get("limit"))
                return {"messages": copy.deepcopy(self.history["messages"][-params.get("limit", 1000):])}
            return await plain(method, params, timeout)
        self.conn.send_request = send_request
        for i in range(300):
            self.imported(f"old {i}")
        self.start_job()
        _turn_route_now(self.conn, self.sid_ev(_delta("Early.")))
        await self.spin()
        self.assertEqual(limits, [50])
        self.assertEqual(self.new_messages(), {})

    async def test_a_sessionid_run_with_no_running_wake_opens_nothing(self):
        """Node agent commands and compactions also use runId = sessionId: strays."""
        await self.wake(self.W1, "First wake.")
        first = copy.deepcopy(self.new_messages())
        _turn_route_now(self.conn, self.sid_ev(_tool("start", tcid="toolu_9")))
        _turn_route_now(self.conn, self.sid_ev(_delta("compaction note")))
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        self.assertEqual(self.new_messages(), first, "a stray reopened or added a message")
        self.assertFalse(self.conn._turn_pumps)

    async def test_a_fresh_connection_does_not_bind_a_stray_to_an_old_wake(self):
        await self.wake(self.W1, "First wake.")
        first = copy.deepcopy(self.new_messages())
        self.conn._wake_turns_bound.clear()  # a new deploy's connection knows no wake
        _turn_route_now(self.conn, self.sid_ev(_delta("late stray")))
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        self.assertEqual(self.new_messages(), first)

    async def test_a_stray_opens_no_message_for_an_old_wake_shown_elsewhere(self):
        """An old wake shown as its own bubble left no message with its turn's id; only its cron
        state says a later sessionId event is not its."""
        self.wake_turn(self.W1)
        self.job_finished()
        _turn_route_now(self.conn, self.sid_ev(_delta("compaction note")))
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        self.assertEqual(self.new_messages(), {}, "a stray opened a message for a finished wake")

    async def test_a_sessions_send_turn_queued_during_the_wake_is_not_the_wakes(self):
        """sessions_send turns carry provenance and their own run id."""
        self.wake_turn(self.W1)
        at = self.tick()
        self.history["messages"].append({
            "role": "user", "content": "[cron:other x] forwarded", "idempotencyKey": "cb27292c:user",
            "provenance": {"kind": "inter_session", "sourceTool": "sessions_send"},
            "__openclaw": {"idempotencyKey": "cb27292c:user", "recordTimestampMs": at}})
        _turn_route_now(self.conn, self.sid_ev(_delta("Wake text.")))
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        self.assertEqual(list(self.new_messages()), [turn_message_id(self.key, self.W1, self.CHAT)])

    async def test_a_heartbeat_flagged_wake_id_opens_nothing_and_frees_the_id(self):
        """Skipped by resolved identity, not the shared run id."""
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev({**_delta("HEARTBEAT_OK"), "isHeartbeat": True}))
        _turn_route_now(self.conn, self.sid_ev({"state": "final", "isHeartbeat": True}))
        self.job_finished()
        await self.spin()
        self.assertEqual(self.new_messages(), {})
        self.assertNotIn((self.key, self.SID), self.conn._turn_skip_runs)
        await self.wake(self.W2, "Second wake.")
        self.assertEqual([m["content"] for m in self.new_messages().values()], ["Second wake."])

    async def test_a_resync_does_not_end_a_running_wake(self):
        """A seq gap or reconnect reading the idle row does not end a running wake whose turn is in
        the transcript."""
        from openclaw_pipe import _turn_resync
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev(_delta("Reruns all passed.")))
        await self.spin()
        await _turn_resync(self.conn)
        await self.spin()
        _turn_route_now(self.conn, self.sid_ev(_delta(" Tests written.")))
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        self.assertEqual([m["content"] for m in self.new_messages().values()],
                         ["Reruns all passed. Tests written."])

    async def test_another_runs_settle_row_does_not_end_the_wake(self):
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev(_delta("Reruns all passed.")))
        await self.spin()
        # The queued message's row: idle, naming another run.
        self.queued_turn()
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.R})
        await self.spin()
        _turn_route_now(self.conn, self.sid_ev(_delta(" Tests written.")))
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        (mid, m), = self.new_messages().items()
        self.assertEqual(mid, turn_message_id(self.key, self.W1, self.CHAT))
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Reruns all passed. Tests written.",
                         "the wake's tail after a foreign settle row was lost")

    async def test_two_cron_wakes_get_two_messages(self):
        await self.wake(self.W1, "First wake.")
        await self.wake(self.W2, "Second wake.")
        got = {mid: m["content"] for mid, m in self.new_messages().items()}
        self.assertEqual(got, {turn_message_id(self.key, self.W1, self.CHAT): "First wake.",
                               turn_message_id(self.key, self.W2, self.CHAT): "Second wake."},
                         "runId == sessionId is not unique per run: the second wake reused the first message")
        self.assertTrue(all(m["done"] is True for m in self.new_messages().values()))

    async def test_the_second_wake_is_not_rebuilt_from_the_first_wakes_closing(self):
        """`cli-assistant:<sessionId>` is the first wake's; the second is not built from it."""
        await self.wake(self.W1, "First wake.")
        await self.wake(self.W2, "Second wake.")
        second = self.new_messages()[turn_message_id(self.key, self.W2, self.CHAT)]
        self.assertNotIn("First", second["content"])

    async def test_lost_deltas_are_repaired_from_the_wakes_own_window(self):
        await self.wake(self.W1, "First wake.")
        self.wake_turn(self.W2)
        _turn_route_now(self.conn, self.sid_ev(_delta("Second wake.")))
        # " All done." never reached the socket; the import has it.
        self.imported("Second wake. All done.")
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        second = self.new_messages()[turn_message_id(self.key, self.W2, self.CHAT)]
        self.assertEqual(second["content"], "Second wake. All done.")

    async def test_a_transcript_without_the_wakes_tool_calls_never_replaces_its_cards(self):
        """A wake's transcript without its tool calls (session not imported) must not replace the
        streamed cards, whatever the item count says."""
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev(_tool("start", tcid="toolu_a")))
        # A gap left one streamed item; the transcript also renders one (text, no call), which an
        # item count would call "at least as much".
        self.imported("WQ-DONE")
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], True)
        calls = [o.get("call_id") or o.get("id") for o in m["output"] if o["type"] == "function_call"]
        self.assertEqual(calls, ["toolu_a"], "the streamed card was replaced by a transcript without it")

    async def test_a_transcript_with_the_wakes_calls_still_repairs_what_the_gap_lost(self):
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev(_tool("start", tcid="toolu_a")))
        self.history["messages"].append({
            "role": "assistant", "timestamp": self.tick(), "__openclaw": {"importedFrom": "claude-cli"},
            "content": [{"type": "toolcall", "id": "toolu_a", "name": "Bash", "arguments": {"command": "ls"}},
                        {"type": "tool_result", "tool_use_id": "toolu_a", "name": "Bash",
                         "content": [{"type": "text", "text": "WQ-step-1"}]}]})
        self.imported("WQ-DONE")
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        (m,) = self.new_messages().values()
        self.assertEqual([o["type"] for o in m["output"]], ["function_call", "function_call_output", "message"])
        self.assertEqual(m["content"], "WQ-DONE")

    async def test_a_wake_that_failed_before_its_turn_opens_nothing_and_its_retry_gets_its_own(self):
        """A CronSessionLifecycleClaimError (isolated-agent-C_VaOV3U.mjs:885-889) leaves no turn;
        the retry (service-DYcTh1Yv.mjs:670) gets its own message."""
        job_b, w_b = "3277885d-d0e5-4059-87c1-0432adafdba5", "c4b6f7a2-0d1e-4f5a-9b8c-7d6e5f4a3b2c"
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev(_delta("First ")))
        b_start = self.tick()  # b's failed attempt: running for 143 ms
        self.jobs.append({"id": job_b, "sessionTarget": f"session:{self.key}",
                          "state": {"runningAtMs": b_start}})
        _turn_route_now(self.conn, self.sid_ev(_delta("wake.")))
        self.jobs = [j for j in self.jobs if j["id"] != job_b]
        self.cron_runs.append({"jobId": job_b, "action": "finished", "status": "error", "runAtMs": b_start,
                               "ts": self.tick(), "sessionId": None,
                               "error": f'Session "{self.key}" changed while starting work. Retry.'})
        self.imported("First wake.")
        self.closing("First wake.")
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        self.job_finished()
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.SID})
        await self.spin()
        self.assertEqual(list(self.new_messages()), [turn_message_id(self.key, self.W1, self.CHAT)],
                         "the failed attempt opened a message")
        # The retry: its own turn, its own message.
        at = self.tick()
        self.jobs = [{"id": job_b, "sessionTarget": f"session:{self.key}", "state": {"runningAtMs": at - 400}}]
        self.history["messages"].append({
            "role": "user", "content": f"[cron:{job_b} eli103-gate-b] Second.", "timestamp": at - 150,
            "idempotencyKey": f"{w_b}:user", "__openclaw": {"idempotencyKey": f"{w_b}:user",
                                                           "recordTimestampMs": at}})
        _turn_route_now(self.conn, self.sid_ev(_delta("Second wake.")))
        self.imported("Second wake.")
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        self.job_finished()
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.SID})
        await self.spin()
        got = {mid: (m["content"], m["done"]) for mid, m in self.new_messages().items()}
        self.assertEqual(got, {turn_message_id(self.key, self.W1, self.CHAT): ("First wake.", True),
                               turn_message_id(self.key, w_b, self.CHAT): ("Second wake.", True)})

    async def test_a_turn_queued_behind_the_wake_is_not_the_wakes(self):
        self.wake_turn(self.W1)
        self.queued_turn()  # committed between the wake's turn and its first event
        _turn_route_now(self.conn, self.sid_ev(_delta("Reruns all passed.")))
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        self.assertEqual(list(self.new_messages()), [turn_message_id(self.key, self.W1, self.CHAT)])

    async def test_a_wake_whose_turn_is_not_committed_yet_waits_for_it(self):
        """The embedded runtime commits the user turn without awaiting it
        (embedded-agent-DNQn_PMM.mjs:5891), so the first event can come first."""
        await self.wake(self.W1, "First wake.")
        self.start_job()
        _turn_route_now(self.conn, self.sid_ev(_delta("Second ")))
        await self.spin()
        self.assertEqual(len(self.new_messages()), 1, "bound to the previous wake's turn")
        self.wake_turn(self.W2, started=True)
        _turn_route_now(self.conn, self.sid_ev(_delta("wake.")))
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        second = self.new_messages()[turn_message_id(self.key, self.W2, self.CHAT)]
        self.assertEqual(second["content"], "Second wake.")

    async def test_the_session_id_is_read_from_the_row_when_no_event_carried_it(self):
        self.conn._session_ids.clear()
        await self.wake(self.W1, "First wake.")
        self.assertIn("sessions.list", [m for m, _ in self.sent])
        self.assertEqual(list(self.new_messages()), [turn_message_id(self.key, self.W1, self.CHAT)])

    async def test_after_a_reconnect_the_cron_job_says_whether_the_wake_ended(self):
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev(_delta("Working.")))
        await self.spin()
        await _wake_resync(self.conn)  # the job is still running (wake_turn)
        await self.spin()
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], False, "a running wake was ended")
        self.jobs = []  # a one-shot job is deleted once it ran
        await _wake_resync(self.conn)
        await self.spin()
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], True)
        self.assertFalse(self.conn._turn_pumps)

    # ── ELI-180: a wake that ends with a question ──

    PROMPT = "OpenClaw needs input:\n\nColor?\nPick one\n1. Red\n2. Blue"

    def tab_on_chat(self, call=None):
        """A tab views the chat; `call` answers its card (default "Blue")."""
        env = types.ModuleType("open_webui.env")
        env.VERSION = "0.12.0"
        p = mock.patch.dict(sys.modules, {"open_webui.env": env})
        p.start()
        self.addCleanup(p.stop)
        self.owui.session_pool["sid-tab"] = {"id": self.USER, "chat_ids": [self.CHAT]}
        self.owui.page_intent["sid-tab"] = WriterQuestionCardTests.ANSWERED
        if call is not None:
            p = mock.patch.object(openclaw_pipe, "_call_on_chat_tab", side_effect=call)
            p.start()
            self.addCleanup(p.stop)
        inner, test = self.conn.send_request, self

        async def send_request(method, params, timeout=10):
            if method == "chat.send":
                test.sent.append((method, copy.deepcopy(params)))
                return {"runId": params["idempotencyKey"], "status": "started"}
            return await inner(method, params, timeout)
        self.conn.send_request = send_request

    card_calls = WriterQuestionCardTests.card_calls
    sends = WriterQuestionCardTests.sends

    async def answer_run(self, run, text):
        self.history["messages"] += HandoverRewriteTests.closed_run(self, run, text, user="Blue")
        await self.dispatch(run, _delta(text))
        await self.dispatch(run, {"state": "final"})
        await HandoverRewriteTests.settle(self, run)

    def cron_task_gateway(self, abortable=False, run_abortable=False):
        """The Gateway's answers for stopping a wake: on 2026.9 tasks.cancel (ELI-192);
        `run_abortable`: 2026.10.5's sessions.abort by run id."""
        inner, test = self.conn.send_request, self
        self.cron_tasks = [{"taskId": "task-wake", "kind": "automation_run", "runtime": "cron",
                            "status": "running", "childSessionKey": self.key, "sourceId": self.JOB,
                            "runId": f"cron:{self.JOB}:1:x"},
                           {"taskId": "task-other", "kind": "automation_run", "runtime": "cron",
                            "status": "running", "childSessionKey": "agent:main:other", "sourceId": "job-other"}]

        async def send_request(method, params, timeout=10):
            if method == "chat.abort":
                test.sent.append((method, copy.deepcopy(params)))
                return {"ok": True, "aborted": abortable, "runIds": [params.get("runId")] if abortable else []}
            if method == "sessions.abort":
                test.sent.append((method, copy.deepcopy(params)))
                return ({"ok": True, "abortedRunId": params.get("runId"), "status": "aborted"} if run_abortable
                        else {"ok": True, "abortedRunId": None, "status": "no-active-run"})
            if method == "tasks.list" and run_abortable:
                raise RuntimeError("unknown method: tasks.list")
            if method == "tasks.list":
                test.sent.append((method, copy.deepcopy(params)))
                return {"tasks": copy.deepcopy(test.cron_tasks)}
            if method == "tasks.cancel":
                test.sent.append((method, copy.deepcopy(params)))
                return {"found": True, "cancelled": True}
            return await inner(method, params, timeout)
        self.conn.send_request = send_request

    async def stop_on_the_tab(self, mid):
        w = self.conn._turn_writers[(self.CHAT, mid)]
        self.owui.page_intent["sid-tab"] = "stop"
        _, task = self.owui.tasks[w.task_id]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self.spin()
        return w

    async def test_stop_on_a_wake_in_the_open_tab_stops_its_run(self):
        """ELI-192: on 2026.9 Stop on a wake cancels its cron task, since chat.abort finds no run
        under the sessionId."""
        self.tab_on_chat()
        self.cron_task_gateway()
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev(_delta("Looping: 1 of 8.")))
        await self.spin()
        mid = turn_message_id(self.key, self.W1, self.CHAT)
        w = await self.stop_on_the_tab(mid)
        self.assertIn(("tasks.cancel", {"taskId": "task-wake"}), self.sent)
        self.assertNotIn(("tasks.cancel", {"taskId": "task-other"}), self.sent)
        self.assertTrue(w.closed)
        self.assertIs(self.new_messages()[mid]["done"], True)

    async def test_stop_on_a_wake_on_2026_10_aborts_its_run_by_id(self):
        """2026.10.5 has no tasks.cancel: sessions.abort with the run id, without clearQueued, so
        queued turns stay."""
        self.tab_on_chat()
        self.cron_task_gateway(run_abortable=True)
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev(_delta("Looping: 1 of 8.")))
        await self.spin()
        mid = turn_message_id(self.key, self.W1, self.CHAT)
        w = await self.stop_on_the_tab(mid)
        self.assertIn(("sessions.abort", {"key": self.key, "runId": w.run_id}), self.sent)
        self.assertNotIn("tasks.list", [m for m, _ in self.sent])
        # chat.abort on a 2026.10.5 wake only stops its running command, so it is never sent first.
        self.assertNotIn("chat.abort", [m for m, _ in self.sent])
        self.assertTrue(w.closed)
        self.assertIs(self.new_messages()[mid]["done"], True)

    async def test_stop_on_a_run_chat_abort_stops_cancels_no_task(self):
        """A run chat.abort aborts is not cancelled again as a task."""
        self.tab_on_chat()
        self.cron_task_gateway(abortable=True)
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev(_delta("Looping: 1 of 8.")))
        await self.spin()
        await self.stop_on_the_tab(turn_message_id(self.key, self.W1, self.CHAT))
        self.assertIn("chat.abort", [m for m, _ in self.sent])
        self.assertNotIn("tasks.cancel", [m for m, _ in self.sent])

    async def test_a_wake_that_ends_with_a_question_asks_it_on_the_tab(self):
        """On 2026.9 a wake ends on its own terminal event; it asks its closing question like any
        run's end, and the answer continues the wake's message."""
        self.tab_on_chat()
        await self.wake(self.W1, "Nightly check done.\n\n", self.PROMPT)
        (to, _card), = self.card_calls()
        self.assertEqual(to, "sid-tab")
        (send,) = self.sends()
        self.assertEqual(send["message"], "Blue")
        mid = turn_message_id(self.key, self.W1, self.CHAT)
        self.assertEqual(parse_pipe_run_id(send["idempotencyKey"]),
                         {"chat": self.CHAT, "message": mid, "kind": "input"})
        self.assertIs(self.new_messages()[mid]["done"], False, "the answer run continues the wake's reply")
        await self.answer_run(send["idempotencyKey"], "Blue it is.")
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], True)
        self.assertTrue(m["content"].startswith("Nightly check done."), m["content"])
        self.assertIn('name="Ask User"', m["content"])
        self.assertIn('result="Blue"', m["content"])
        self.assertTrue(m["content"].endswith("Blue it is."), m["content"])
        self.assertNotIn("needs input", m["content"])
        await _wake_resync(self.conn)    # a reconnect after it: nothing asks again
        await self.spin()
        self.assertEqual(len(self.card_calls()), 1, "one card")
        self.assertEqual(len(self.sends()), 1, "one answer")

    async def test_with_no_tab_on_the_chat_the_wakes_question_stays_text(self):
        self.tab_on_chat()
        self.owui.session_pool["sid-tab"]["chat_ids"] = ["another-chat"]
        await self.wake(self.W1, "Nightly check done.\n\n", self.PROMPT)
        self.assertEqual((self.card_calls(), self.sends()), ([], []))
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Nightly check done.\n\nColor?\nPick one\n1. Red\n2. Blue")

    async def test_the_next_wake_streams_while_a_wakes_question_card_is_open(self):
        """All wakes share one run id and pump; the next wake is not held behind a card."""
        card = asyncio.get_running_loop().create_future()

        async def call(*_a, **_k):
            return await card
        self.tab_on_chat(call)
        await self.wake(self.W1, "First wake.\n\n", self.PROMPT)
        await self.wake(self.W2, "Second wake.")
        second = self.new_messages()[turn_message_id(self.key, self.W2, self.CHAT)]
        self.assertIs(second["done"], True)
        self.assertEqual(second["content"], "Second wake.")
        first = turn_message_id(self.key, self.W1, self.CHAT)
        self.assertIs(self.new_messages()[first]["done"], False, "still asking")
        card.set_result(WriterQuestionCardTests.ANSWERED)
        await self.spin()
        (send,) = self.sends()
        self.assertEqual(parse_pipe_run_id(send["idempotencyKey"])["message"], first)

    # ── ELI-181: a wake whose job is gone before its first event is judged ──

    def heard(self, action, job, start, end=None):
        """The Gateway's `cron` event for a job run into this session (ELI-181)."""
        getattr(self.conn, "note_cron_event", lambda _p: None)({"action": action, "jobId": job, "runAtMs": start,
                                   **({"durationMs": end - start} if end is not None else {}),
                                   "job": {"id": job, "sessionTarget": f"session:{self.key}"}})

    def short_wake(self, key, text, job=None):
        """A wake over before the Pipe judges its first event, which predates its turn's commit.
        Returns the run's events, not routed."""
        job = job or self.JOB
        start = self.tick()
        self.heard("started", job, start)
        events = [self.sid_ev(_delta(text))]
        at = self.tick()
        self.history["messages"].append({
            "role": "user", "content": f"[cron:{job} eli181-wake] {key}", "idempotencyKey": f"{key}:user",
            "timestamp": at - 150, "__openclaw": {"idempotencyKey": f"{key}:user", "recordTimestampMs": at}})
        events.append(self.sid_ev({"state": "final"}))
        self.imported(text)
        end = self.tick()
        self.cron_runs.append({"jobId": job, "action": "finished", "runAtMs": start, "ts": end})
        self.heard("finished", job, start, end)
        self.jobs = []      # a one-shot job is deleted once it ran
        return events

    async def test_a_wake_whose_job_ended_before_its_first_event_is_judged_gets_its_message(self):
        """ELI-181: a wake whose job left cron.list before its first event was judged gets its
        message, not dropped as stray (#115)."""
        for cron_runs_written in (True, False):
            with self.subTest(cron_runs_written=cron_runs_written):
                key = self.W1 if cron_runs_written else self.W2
                events = self.short_wake(key, f"Short wake {key[:4]}.")
                if not cron_runs_written:
                    self.cron_runs.clear()
                for ev in events:
                    _turn_route_now(self.conn, ev)
                await self.spin()
                m = self.new_messages().get(turn_message_id(self.key, key, self.CHAT))
                self.assertIsNotNone(m, "the wake got no message")
                self.assertIs(m["done"], True)
                self.assertEqual(m["content"], f"Short wake {key[:4]}.")

    async def test_the_next_wakes_events_queued_behind_a_wakes_end_are_shown(self):
        """What is queued behind a wake's end is the next wake's, not dropped."""
        first = self.short_wake(self.W1, "First wake.", job="job-1")
        second = self.short_wake(self.W2, "Second wake.", job="job-2")
        for ev in first + second:      # all queued before the pump judges any
            _turn_route_now(self.conn, ev)
        await self.spin()
        got = {k: m["content"] for k, m in self.new_messages().items()}
        self.assertEqual(got, {turn_message_id(self.key, self.W1, self.CHAT): "First wake.",
                               turn_message_id(self.key, self.W2, self.CHAT): "Second wake."})

    async def stray_judged_while_the_next_wake_runs(self, after_final, *, cron_events=True):
        """ELI-181: a stray judged on its own pump while wake two runs into it. `cron_events`: wake
        two's cron events arrived. Returns wake two's message or None."""
        await self.wake(self.W1, "First wake.")
        inner, gate, blocked = self.conn.send_request, asyncio.Event(), asyncio.Event()

        async def slow(method, params, timeout=10):
            if method == "cron.list" and not gate.is_set():
                blocked.set()
                await gate.wait()   # the stray's judgment is slow (memory pressure in CI)
            return await inner(method, params, timeout)
        self.conn.send_request = slow
        _turn_route_now(self.conn, self.sid_ev(_delta("trailing")))   # wake one's trailing event
        await asyncio.wait_for(blocked.wait(), 5)
        self.wake_turn(self.W2)
        start = self.jobs[0]["state"]["runningAtMs"]
        if cron_events:
            self.heard("started", self.JOB, start)
        _turn_route_now(self.conn, self.sid_ev(_delta("Second wake.")))
        self.imported("Second wake.")
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        if cron_events:
            self.heard("finished", self.JOB, start, self.tick())
        after_final()
        gate.set()
        await self.spin()
        return self.new_messages().get(turn_message_id(self.key, self.W2, self.CHAT))

    def job_gone_run_log_late(self):
        """The one-shot job is deleted; its cron.runs entry is not readable yet."""
        self.job_finished()
        self.cron_runs = self.cron_runs[:-1]

    async def test_a_wake_queued_behind_a_stray_judgment_gets_its_message(self):
        for name, after_final, cron_events in (
                ("job still running", lambda: None, True),
                ("job finished", self.job_finished, True),
                ("job finished, its run log late", self.job_gone_run_log_late, True),
                ("job finished, no cron event heard", self.job_finished, False)):
            with self.subTest(name):
                await self.asyncSetUp()
                m = await self.stray_judged_while_the_next_wake_runs(after_final, cron_events=cron_events)
                self.assertIsNotNone(m, "wake two got no message")
                self.assertEqual(m["content"], "Second wake.")


class WakeTurnTests(unittest.TestCase):
    def msg(self, key, text, at, **extra):
        return {"role": "user", "content": text, "idempotencyKey": f"{key}:user",
                "__openclaw": {"recordTimestampMs": at}, **extra}

    def test_only_cron_prompts_without_provenance_are_wakes(self):
        msgs = [self.msg("w1", "[cron:job-1 nightly] go", 10),
                self.msg("s1", "[cron:job-1 x] via sessions_send", 11,
                         provenance={"kind": "inter_session", "sourceTool": "sessions_send"}),
                self.msg(pipe_run_id("c" * 8 + "-1111-2222-3333-444455556666",
                                     "d" * 8 + "-1111-2222-3333-444455556666"), "[cron:job-1 typed]", 12),
                self.msg("u1", "hello", 13)]
        self.assertEqual(wake_turns(msgs), [{"key": "w1", "at": 10, "job": "job-1", "shared_run": True}])

    def test_the_newest_turn_committed_by_the_event_not_yet_bound(self):
        msgs = [self.msg("w1", "[cron:a n] x", 10), self.msg("w2", "[cron:b n] y", 20)]
        self.assertEqual(wake_turn_at(msgs, 25)["key"], "w2")
        self.assertEqual(wake_turn_at(msgs, 15)["key"], "w1")
        self.assertIsNone(wake_turn_at(msgs, 25, {"w2"}), "w2 taken: this wake's turn is not in yet")
        self.assertIsNone(wake_turn_at(msgs, 5))

    def test_the_span_is_the_wakes_window_without_user_turns(self):
        msgs = [{"role": "assistant", "timestamp": 5}, self.msg("q", "queued", 15),
                {"role": "assistant", "timestamp": 15}, {"role": "assistant", "timestamp": 30}]
        self.assertEqual(wake_span(msgs, 10, 20), [{"role": "assistant", "timestamp": 15}])


class RetiredWriterTests(_RoutingCase):
    """ELI-103: a retired writer ends its Open WebUI task (or the chat shows generating and orphan
    repair skips it); the successor adopts the message and finishes it once the run is over."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.active = False
        plain = self.conn.send_request
        test = self

        async def send_request(method, params, timeout=10):
            if method == "sessions.list":
                test.sent.append((method, copy.deepcopy(params)))
                return {"sessions": [{"key": test.key, "hasActiveRun": test.active,
                                      "status": "running" if test.active else "done",
                                      "lastRunId": test.RUN}]}
            if method == "cron.list":
                return {"jobs": copy.deepcopy(test.jobs)}
            if method == "cron.runs":
                return {"entries": [e for e in test.cron_runs if e["jobId"] == params.get("id")]}
            return await plain(method, params, timeout)
        self.jobs, self.cron_runs = [], []
        self.send_request = send_request
        self.conn.send_request = send_request

    def successor(self):
        conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        conn.send_request = self.send_request
        return conn

    async def start_turn_then_retire(self):
        """A's writer streamed half a turn, then a deploy retired A."""
        self.active = True
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        await self.spin()
        (mid, m), = self.new_messages().items()
        (task_id, (_chat, task)), = self.owui.tasks.items()
        self.assertFalse(task.done())
        await self.conn.retire()
        await self.spin()
        return mid, task

    async def adopt(self, conn, mid):
        msg = self.owui.msgs()[mid]
        got = await _adopt_turn_message(conn, chat_id=self.CHAT, user_id=self.USER,
                                        session_key=self.key, message_id=mid, meta=msg["meta"])
        await self.spin()
        return got

    async def test_retire_ends_the_writers_task_and_leaves_its_message_open(self):
        mid, task = await self.start_turn_then_retire()
        self.assertTrue(task.done(), "a retired writer's Open WebUI task stayed registered")
        self.assertIs(self.owui.msgs()[mid]["done"], False)
        self.assertFalse(self.conn._turn_pumps, "the retired connection's pump kept waiting")

    async def test_a_successor_finishes_a_run_that_ended_meanwhile(self):
        mid, _ = await self.start_turn_then_retire()
        self.active = False  # the run ended while no connection listened
        b = self.successor()
        self.assertEqual(await self.adopt(b, mid), "adopted")
        m = self.owui.msgs()[mid]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Reading them.\n\nFound it.")

    async def test_a_successor_continues_a_run_still_going(self):
        mid, _ = await self.start_turn_then_retire()
        b = self.successor()
        await self.adopt(b, mid)
        self.assertIs(self.owui.msgs()[mid]["done"], False, "a running run was finished")
        _turn_route_now(b, self.ev(self.RUN, _delta("\n\nFound it.")))
        _turn_route_now(b, self.ev(self.RUN, {"state": "final"}))
        self.active = False
        b._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False, "status": "done",
                                "lastRunId": self.RUN, "reason": "chat.run.settled"})
        await self.spin()
        m = self.owui.msgs()[mid]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Reading them.\n\nFound it.")
        self.assertEqual(list(self.new_messages()), [mid], "the successor opened a second message")

    async def test_a_wake_is_adopted_with_its_own_turn(self):
        sid, w = "ad9e461b-724a-4797-9b3f-befa1090e36b", "1d549bd7-80a0-448a-ab9e-3dcbcad0015b"
        self.conn.note_session_id(self.key, sid)
        self.history = {"messages": [
            {"role": "user", "content": "[cron:job-9 wake] go", "idempotencyKey": f"{w}:user",
             "__openclaw": {"recordTimestampMs": 1000}}]}
        self.jobs = [{"id": "job-9", "state": {"runningAtMs": 900}}]
        _turn_route_now(self.conn, {"sessionKey": self.key, "runId": sid, "ts": 2000,
                                            **_delta("Smoke ")})
        await self.spin()
        (mid, m), = self.new_messages().items()
        self.assertEqual(m["meta"]["openclaw_wake"], {"key": w, "at": 1000, "job": "job-9"})
        await self.conn.retire()
        await self.spin()
        self.history["messages"].append({"role": "assistant", "timestamp": 3000,
                                         "__openclaw": {"importedFrom": "claude-cli"},
                                         "content": [{"type": "text", "text": "Smoke PASS."}]})
        self.jobs = []  # the one-shot job ran and was deleted; cron.runs keeps its run
        self.cron_runs = [{"jobId": "job-9", "action": "finished", "runAtMs": 900, "ts": 3500}]
        self.history["messages"].append({"role": "assistant", "timestamp": 4000,
                                         "__openclaw": {"importedFrom": "claude-cli"},
                                         "content": [{"type": "text", "text": "A queued reply."}]})
        b = self.successor()
        await self.adopt(b, mid)  # cron.list: the job is gone, the wake is over
        m = self.owui.msgs()[mid]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Smoke PASS.")

    async def test_a_message_another_pump_already_writes_is_left_to_it(self):
        mid, _ = await self.start_turn_then_retire()
        b = self.successor()
        _turn_route_now(b, self.ev(self.RUN, _delta("\n\nFound it.")))
        self.assertEqual(await self.adopt(b, mid), "skip")


class GatewayOwnerNeverRecoversTests(_RoutingCase):
    """A settled `running` row keeps M open for the recovery; the tombstone to failed
    (main-session-restart-recovery-CPJjd8da.mjs:1150) closes it."""

    async def keep_open(self):
        _turn_route_now(self.conn, self.ev(self.RUN, _delta("Working on it.")))
        await self.spin()
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "running", "lastRunId": self.RUN})
        await self.spin()
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], False, "precondition: M kept open for the Gateway's recovery")

    async def test_a_tombstoned_recovery_closes_the_message(self):
        await self.keep_open()
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "failed", "lastRunId": self.RUN,
                                        "lastRunError": "recovery skipped"})
        await self.spin()
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], True, "M stays done:false forever: no pump is left to end it")
        self.assertIn("recovery skipped", m["content"])
        self.assertFalse(self.conn._turn_pumps)

    async def test_the_row_still_running_keeps_waiting(self):
        await self.keep_open()
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "running", "lastRunId": self.RUN})
        await self.spin()
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], False)
        self.assertIn((self.key, self.RUN), self.conn._turn_pumps)

    async def test_a_recovery_that_takes_the_message_over_releases_the_pump(self):
        self.history = {"messages": [
            {"role": "user", "content": "x", "idempotencyKey": f"{self.RUN}:user"},
            {"role": "user", "content": "[System] continue", "idempotencyKey": f"{self.REC}:user",
             "provenance": {"kind": "internal_system", "sourceTool": "main_session_restart_recovery"}}]}
        await self.keep_open()
        await self.run_turn(self.REC, text=("Again.", "\n\nFound it."),
                            settle={"sessionKey": self.key, "hasActiveRun": False, "status": "done",
                                    "lastRunId": self.REC, "reason": "chat.run.settled"})
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], True)
        self.assertFalse(self.conn._turn_pumps, "the interrupted run's pump leaked")


class MediaDirectiveCodeAwareTests(_TurnCase):
    """ELI-111: media directives are code-aware (CommonMark), like the ask marker since ELI-107:
    text inside code spans and fences."""

    FX = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                     "test_fixture_eli111_report.json")))
    BASE = "https://x"
    IN_CODE = (
        "Use `MEDIA:x.png ` like this",
        "Use `` MEDIA:x.png `` like this",
        "Use ``a `MEDIA:x.png ` b`` like this",
        "Use ```MEDIA:x.png ``` like this",
        "Spans a line: `` see\nMEDIA:x.png `` there",
        "```\nMEDIA:x.png\n```\nafter",
        "~~~ text\nMEDIA:x.png\n~~~\nafter",
        "````md\n```\nMEDIA:x.png\n```\n````\nafter",
        "Unclosed fence:\n```\nMEDIA:x.png\n",
    )

    def test_the_report_is_shown_as_written(self):
        raw, stored = self.FX["raw"], self.FX["stored"]
        self.assertIn("![x.png](" + self.FX["base_url"] + "/x.png)\n` like this``", stored,
                      "the fixture holds the live rewrite")
        self.assertEqual(_resolve_media(raw, base_url=self.FX["base_url"]), (raw, False))
        self.assertEqual(_advance_media_buffer("", raw), (raw, ""))

    def test_a_directive_in_code_is_text(self):
        for text in self.IN_CODE:
            with self.subTest(text=text):
                self.assertEqual(_resolve_media(text, base_url=self.BASE), (text, False))
                self.assertEqual(openclaw_pipe._media_directives(text, True), [])

    async def test_a_directive_in_code_is_never_uploaded(self):
        for text in self.IN_CODE:
            with self.subTest(text=text), \
                 mock.patch("openclaw_pipe._upload_owui_file") as upload, \
                 mock.patch("os.path.isfile", return_value=True):
                got = await _resolve_media_via_owui(text, base_url="https://o", token="t",
                                                    __event_emitter__=None)
                self.assertEqual(got, (text, False))
                upload.assert_not_called()

    def test_a_directive_outside_code_still_resolves(self):
        img = "![x.png](https://x/x.png)"
        cases = {
            "Done. MEDIA:x.png now": f"Done. {img}\nnow",
            "`code` then MEDIA:x.png and `more`": f"`code` then {img}\nand `more`",
            "```\nold\n```\nMEDIA:x.png\n": f"```\nold\n```\n{img}",
            "A stray ` backtick, MEDIA:x.png end": f"A stray ` backtick, {img}\nend",
            "Escaped \\`MEDIA:x.png \\` ticks": f"Escaped \\`{img}\n\\` ticks",
            "Two paragraphs `a\n\nMEDIA:x.png b`": f"Two paragraphs `a\n\n{img}\nb`",
            "    MEDIA:x.png indented": f"    {img}\nindented",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_resolve_media(text, base_url=self.BASE), (want, True))
                self.assertEqual(openclaw_pipe._media_directives(text, True), ["MEDIA:x.png"])

    def test_code_and_prose_directives_in_one_text(self):
        text = "Quote ``Use `MEDIA:a.png ` here``, real: MEDIA:b.png\nend"
        self.assertEqual(_resolve_media(text, base_url=self.BASE),
                         ("Quote ``Use `MEDIA:a.png ` here``, real: ![b.png](https://x/b.png)\nend",
                          True))
        self.assertEqual(openclaw_pipe._media_directives(text, True), ["MEDIA:b.png"])

    def test_the_text_already_shown_sets_the_code_state(self):
        """A streamed chunk starts where its message item's shown text left off: inside a fence or
        an open code span."""
        for ctx, chunk in (("```\n", "MEDIA:x.png\nmore"), ("~~~\nold\n", "MEDIA:x.png\nmore"),
                           ("Use `` ", "MEDIA:x.png `` more")):
            with self.subTest(ctx=ctx):
                self.assertEqual(_resolve_media(chunk, base_url=self.BASE, ctx=ctx), (chunk, False))
        for ctx in ("```\nold\n```\n", "`done` ", "A stray ` "):
            with self.subTest(ctx=ctx):
                self.assertTrue(_resolve_media("MEDIA:x.png\nmore", base_url=self.BASE, ctx=ctx)[1])

    def test_a_directive_after_an_open_code_span_is_held_until_decided(self):
        # Closed by a later run of the same length: code, released as text.
        shown, held = _advance_media_buffer("", "MEDIA:x.png and", ctx="Use `` ")
        self.assertEqual((shown, held), ("", "MEDIA:x.png and"))
        shown, held = _advance_media_buffer(held, " more`` done", ctx="Use `` ")
        self.assertEqual((shown, held), ("MEDIA:x.png and more`` done", ""))
        self.assertFalse(_resolve_media(shown, base_url=self.BASE, ctx="Use `` ")[1])
        # The paragraph ends first: the run was a literal backtick.
        shown, held = _advance_media_buffer("", "MEDIA:x.png\n", ctx="a ` stray ")
        self.assertEqual((shown, held), ("", "MEDIA:x.png\n"))
        shown, held = _advance_media_buffer(held, "\nnext", ctx="a ` stray ")
        self.assertEqual((shown, held), ("MEDIA:x.png\n\nnext", ""))
        self.assertTrue(_resolve_media(shown, base_url=self.BASE, ctx="a ` stray ")[1])
        # Inside a fence nothing is held.
        self.assertEqual(_advance_media_buffer("", "MEDIA:x.pn", ctx="```\n"),
                         ("MEDIA:x.pn", ""))

    def test_the_writer_waits_for_an_open_span(self):
        self.assertEqual(openclaw_pipe._media_directives("a `MEDIA:x.png b", False), [])
        self.assertEqual(openclaw_pipe._media_directives("a `MEDIA:x.png b", True),
                         ["MEDIA:x.png"])

    _stream = NeedsInputMarkerTests._stream

    async def test_the_report_streams_as_written(self):
        self.pipe.valves.USE_OWUI_FILES = False
        self.pipe.valves.FILE_SERVER_BASE_URL = self.BASE
        for chunk in (40, 7):
            with self.subTest(chunk=chunk):
                out = await self._stream(self.FX["raw"], None, chunk=chunk)
                self.assertNotIn("![", out)
                self.assertEqual(out.strip(), self.FX["raw"].strip())
                await self.asyncTearDown()
                await self.asyncSetUp()
                self.pipe.valves.USE_OWUI_FILES = False
                self.pipe.valves.FILE_SERVER_BASE_URL = self.BASE

    async def test_a_streamed_reply_resolves_only_its_prose_directive(self):
        self.pipe.valves.USE_OWUI_FILES = False
        self.pipe.valves.FILE_SERVER_BASE_URL = self.BASE
        text = ("Quoted: ``Use `MEDIA:a.png ` here`` and\n```\nMEDIA:b.png\n```\n"
                "Real: MEDIA:c.png and more.\n")
        out = await self._stream(text, None, chunk=5)
        self.assertIn("``Use `MEDIA:a.png ` here``", out)
        self.assertIn("```\nMEDIA:b.png\n```", out)
        self.assertIn("![c.png](https://x/c.png)", out)
        self.assertEqual(out.count("!["), 1)


    async def test_a_fence_opened_by_a_resolved_chunk_quotes_what_follows(self):
        """A chunk resolving a directive can open a fence for the next chunk."""
        self.pipe.valves.USE_OWUI_FILES = False
        self.pipe.valves.FILE_SERVER_BASE_URL = self.BASE
        out = await self._stream("Real: MEDIA:a.png\n```\nMEDIA:b.png\n```\nend\n", None, chunk=5)
        self.assertIn("![a.png](https://x/a.png)", out)
        self.assertIn("MEDIA:b.png", out)
        self.assertEqual(out.count("!["), 1)

    async def test_a_held_directive_shows_before_the_next_tool_card(self):
        """A directive held for a possible code span shows before the next tool card."""
        self.pipe.valves.USE_OWUI_FILES = False
        self.pipe.valves.FILE_SERVER_BASE_URL = self.BASE

        async def emitter(event):
            pass
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "go"}]}
        meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": "m1"}
        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta)
        seen = []

        async def drain():
            async for c in gen:
                seen.append(c)
        task = asyncio.ensure_future(drain())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "A stray ` tick, MEDIA:x.png and"))
        q.put_nowait({"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": "run-1", "stream": "tool",
            "data": {"phase": "start", "toolCallId": "t1", "name": "Bash", "args": {}}}})
        q.put_nowait(self._delta("run-1", "after"))
        q.put_nowait(self._final("run-1"))
        await asyncio.wait_for(task, 5)   # bounds the test only
        card = next(i for i, c in enumerate(seen) if not isinstance(c, str))
        before = "".join(c for c in seen[:card] if isinstance(c, str))
        self.assertIn("A stray ` tick, ![x.png](https://x/x.png)\nand", before)


def _owui_stream(chunks) -> tuple[str, list, list]:
    """(content, output, live) for a pipe's yields as Open WebUI 0.12's loop (`middleware.py`)
    handles them: the joined text, the saved output (dicts via `handle_responses_streaming_event`),
    and what the tab gets (`emit_response_completion_event`)."""
    from test_fixture_owui_streaming_handler import handle_responses_streaming_event
    output, parts, live = [], [], []
    for c in chunks:
        if isinstance(c, str):
            if not c:
                continue
            parts.append(c)
            if not output or output[-1].get("type") != "message":
                output.append({"type": "message", "id": f"msg_{len(output)}", "role": "assistant",
                               "content": [{"type": "output_text", "text": ""}]})
            output[-1]["content"][-1]["text"] += c
            live.append({"type": "response.output_text.delta", "item_id": output[-1]["id"],
                         "output_index": len(output) - 1,
                         "content_index": len(output[-1]["content"]) - 1, "delta": c})
        elif isinstance(c, dict) and c.get("type", "").startswith("response."):
            output, _ = handle_responses_streaming_event(c, output)
            live.append(c)
    if output and output[-1].get("type") == "message":
        output[-1]["content"][-1]["text"] = output[-1]["content"][-1]["text"].strip()
    return "".join(parts), output, live


def _owui_saved(chunks) -> tuple[str, list]:
    """(content, output) Open WebUI 0.12 saves for a pipe's yields."""
    content, output, _live = _owui_stream(chunks)
    return content, output


def _owui_cancelled_output(output: list) -> list:
    """What Open WebUI 0.12 sends and saves on cancel (`save_cancelled_state`,
    utils/middleware.py:6652): running items stamped `incomplete`, drawn done (`isDone`)."""
    output = copy.deepcopy(output)
    answered = {i.get("call_id") for i in output
                if i.get("type") == "function_call_output" and i.get("call_id")}
    for i in output:
        running = (i.get("type") == "function_call" and i.get("status") == "completed"
                   and (i.get("call_id") or i.get("id")) not in answered)
        if i.get("status") == "in_progress" or running:
            i["status"] = "incomplete"
    return output


def _owui_live_output(events: list) -> list:
    """The output an Open WebUI 0.12 tab renders while streaming: `events` folded by the vendored
    frontend reducer (`test_fixture_owui_structured_output.ts`), run with bun."""
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "owui_live_fold.ts")
    done = subprocess.run(["bun", script], input=json.dumps(events), capture_output=True,
                          text=True, check=True, timeout=60)
    return json.loads(done.stdout)


def _item_order(output: list) -> list:
    """Each output item as (type, id or call_id, message text)."""
    return [(i.get("type"), i.get("call_id") or i.get("id"),
             "".join(str(p.get("text", "")) for p in i.get("content") or []).strip()
             if i.get("type") == "message" else None) for i in output]


class LiveToolCardOrderTests(_TurnCase):
    """ELI-122 (seen in ELI-110): the tab's reducer splices an item before the last one, the
    backend appends. Live must match saved."""


    def _tool(self, phase, tcid, **data):
        return {"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": "run-1", "stream": "tool",
            "data": {"phase": phase, "toolCallId": tcid, "name": "Bash", "args": {"command": "ls"},
                     **data}}}

    async def _run(self, events):
        """One run fed `events` (str = text delta); (saved, live) outputs."""
        if shutil.which("bun") is None:
            if os.environ.get("CI"):
                self.fail("bun is missing: the live-order tests must run in CI")
            self.skipTest("bun runs the vendored Open WebUI frontend reducer")

        async def emitter(event):
            pass
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "go"}]}
        meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": "m1"}
        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta)

        async def drain():
            return [c async for c in gen]
        task = asyncio.ensure_future(drain())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        for ev in events:
            q.put_nowait(self._delta("run-1", ev) if isinstance(ev, str) else ev)
        q.put_nowait(self._final("run-1"))
        _content, saved, live = _owui_stream(await asyncio.wait_for(task, 5))   # bounds the test only
        return saved, _owui_live_output(live)

    async def test_text_before_a_tool_call_shows_above_its_card(self):
        saved, live = await self._run([
            "Let me look first.", self._tool("start", "t1"),
            self._tool("result", "t1", result="a.txt"), "Found it."])
        self.assertEqual([t for t, _id, _x in _item_order(saved)],
                         ["message", "function_call", "function_call_output", "message"])
        self.assertEqual(_item_order(live), _item_order(saved))

    async def test_parallel_and_back_to_back_calls_keep_their_order_live(self):
        saved, live = await self._run([
            "Two at once.", self._tool("start", "t1"), self._tool("start", "t2"),
            self._tool("result", "t2", result="b"), self._tool("result", "t1", result="a"),
            "Then one more.", self._tool("start", "t3"), self._tool("result", "t3", result="c"),
            "Done."])
        self.assertEqual([t for t, _id, _x in _item_order(saved)],
                         ["message", "function_call", "function_call", "function_call_output",
                          "function_call_output", "message", "function_call",
                          "function_call_output", "message"])
        self.assertEqual(_item_order(live), _item_order(saved))

    async def test_a_reply_that_starts_with_a_tool_call(self):
        saved, live = await self._run([
            self._tool("start", "t1"), self._tool("result", "t1", result="x"), "After."])
        self.assertEqual(_item_order(live), _item_order(saved))


class ModelPatchFailureTests(_TurnCase):
    """ELI-131: a failed model patch is reported, not retried after a sleep; since ELI-59's lock
    and cache every such failure failed twice."""


    async def test_a_failed_patch_is_one_rpc_and_an_explained_reply(self):
        self.conn.model_patch_cached = lambda *a, **k: False
        sent = self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "sessions.patch":
                self.rpc.append(method)
                raise GatewayError("model not allowed: claude-cli/claude-opus-5")
            return await sent(method, params, timeout)
        self.conn.send_request = send_request
        self.pipe.valves.DEFAULT_MODEL = "claude-cli/claude-opus-5"
        gen = NoDroppedMessagesTests._reply(self, "hi", "m1")

        async def drain():
            return [c async for c in gen]
        chunks = await asyncio.wait_for(drain(), 5)   # bounds the test only
        self.assertEqual(self.rpc.count("sessions.patch"), 1)
        self.assertNotIn("chat.send", self.rpc)
        self.assertIn("Model selection error", "".join(c for c in chunks if isinstance(c, str)))


class LongToolOnlyRunTests(_TurnCase):
    """ELI-132: a run sending thousands of tool events before its first text is live, not stuck; no
    "Timeout" after 5000 events."""

    _tool = LiveToolCardOrderTests._tool

    async def test_thousands_of_tool_events_without_text_finish_the_reply(self):
        calls = 2600    # 5200 tool events before any text
        async def emitter(event):
            pass
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "go"}]}
        meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": "m1"}
        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta)

        async def drain():
            return [c async for c in gen]
        task = asyncio.ensure_future(drain())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        # As the receive loop: each event waits for room in the bounded queue.
        for i in range(calls):
            await q.put(self._tool("start", f"t{i}"))
            await q.put(self._tool("result", f"t{i}", result="ok"))
        await q.put(self._delta("run-1", "All 2600 checks passed."))
        await q.put(self._final("run-1"))
        content, saved, _live = _owui_stream(await asyncio.wait_for(task, 60))   # bounds the test only
        self.assertNotIn("Timeout", content)
        self.assertEqual(content.strip(), "All 2600 checks passed.")
        self.assertEqual(len(_card_names(saved)), calls)
        self.assertEqual(sum(1 for i in saved if i.get("type") == "function_call_output"), calls)


def _card_names(output: list) -> dict:
    """Each tool card's name (the collapsed row), by call id."""
    return {i.get("call_id") or i.get("id"): i.get("name")
            for i in output if i.get("type") == "function_call"}


class LiveToolErrorMarkTests(_TurnCase):
    """ELI-124: a failed call's ❌ shows while streaming. `response.name.done` reaches only the
    backend's last item; the tab's reducer ignores it."""

    _tool = LiveToolCardOrderTests._tool
    _run = LiveToolCardOrderTests._run

    def _failed(self, tcid):
        return self._tool("result", tcid, result="ls: cannot access 'x'", isError=True)

    async def test_a_failed_call_is_marked_live(self):
        saved, live = await self._run([
            "Trying.", self._tool("start", "t1"), self._failed("t1"), "It failed."])
        self.assertEqual(_card_names(saved), {"t1": f"Bash {TOOL_ERROR_MARK}"})
        self.assertEqual(_card_names(live), _card_names(saved))
        self.assertEqual(_item_order(live), _item_order(saved))

    async def test_parallel_failed_calls_are_each_marked(self):
        """Each parallel failed call is marked, not only the last item."""
        saved, live = await self._run([
            self._tool("start", "t1"), self._tool("start", "t2"),
            self._failed("t1"), self._tool("result", "t2", result="ok"),
            "Then.", self._tool("start", "t3"), "Text before its result.", self._failed("t3")])
        want = {"t1": f"Bash {TOOL_ERROR_MARK}", "t2": "Bash", "t3": f"Bash {TOOL_ERROR_MARK}"}
        self.assertEqual(_card_names(saved), want)
        self.assertEqual(_card_names(live), want)
        self.assertEqual(_item_order(live), _item_order(saved))

    async def test_the_mark_keeps_the_card_whole(self):
        saved, live = await self._run([self._tool("start", "t1"), self._failed("t1")])
        for output in (saved, live):
            call = next(i for i in output if i.get("type") == "function_call")
            self.assertEqual(call["arguments"], json.dumps({"command": "ls"}))
            out = next(i for i in output if i.get("type") == "function_call_output")
            self.assertTrue(out["output"][0]["text"].startswith(f"{TOOL_ERROR_MARK} tool call failed"))

    async def test_a_failed_call_is_failed_not_completed(self):
        """ELI-278: a failed call's item is "failed", which Open WebUI 0.11.1+ draws as the red X
        (`isError`, `_is_tool_result_error`)."""
        saved, live = await self._run([
            self._tool("start", "t1"), self._failed("t1"),
            self._tool("start", "t2"), self._tool("result", "t2", result="ok"),
            self._failed("t3")])
        for label, output in (("saved", saved), ("live", live)):
            with self.subTest(label):
                failed = {i.get("call_id") or i.get("id"): i.get("status") == "failed"
                          for i in output if i.get("type") == "function_call"}
                self.assertEqual(failed, {"t1": True, "t2": False, "t3": True})


class MediaDirectiveLineBreakTests(_TurnCase):
    """ELI-113: the line break after a directive survives a chunk ending right after it. Where a
    stream is cut must not change what Open WebUI saves."""

    BASE = "https://x"
    IMG = "![c.png](https://x/c.png)"


    async def _saved(self, chunks):
        """Stream `chunks` as one run's deltas; (content, message texts) saved."""
        self.pipe.valves.USE_OWUI_FILES = False
        self.pipe.valves.FILE_SERVER_BASE_URL = self.BASE

        async def emitter(event):
            pass
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "go"}]}
        meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": "m1"}
        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta)

        async def drain():
            return [c async for c in gen]
        task = asyncio.ensure_future(drain())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        for c in chunks:
            q.put_nowait(self._delta("run-1", c))
        q.put_nowait(self._final("run-1"))
        content, output = _owui_saved(await asyncio.wait_for(task, 5))   # bounds the test only
        texts = [p["text"] for i in output if i.get("type") == "message" for p in i["content"]]
        return content, texts

    async def _fresh(self):
        await self.asyncTearDown()
        await self.asyncSetUp()

    def test_a_stream_holds_the_break_until_the_next_line_starts(self):
        self.assertEqual(_advance_media_buffer("", "Real: MEDIA:c.png\n"), ("Real: ", "MEDIA:c.png\n"))
        self.assertEqual(_advance_media_buffer("MEDIA:c.png\n", "Next"), ("MEDIA:c.png\nNext", ""))
        self.assertEqual(_resolve_media("MEDIA:c.png\n", base_url=self.BASE), (self.IMG, True))

    async def test_the_reported_split_keeps_the_line_break(self):
        content, texts = await self._saved(["Real: MEDIA:c.p", "ng\n", "Next line"])
        self.assertEqual(content, f"Real: {self.IMG}\nNext line")
        self.assertEqual(texts, [f"Real: {self.IMG}\nNext line"])

    async def test_a_line_break_ending_a_chunk_after_a_directive_is_kept(self):
        content, texts = await self._saved(["Real: MEDIA:c.png\nmore\n", "next"])
        self.assertEqual(content, f"Real: {self.IMG}\nmore\nnext")
        self.assertEqual(texts, [f"Real: {self.IMG}\nmore\nnext"])

    async def test_every_split_saves_what_one_chunk_saves(self):
        text = "Real: MEDIA:c.png\nNext line\nMEDIA:d.png  and more\nend\n"
        whole = await self._saved([text])
        self.assertEqual(whole[0], f"Real: {self.IMG}\nNext line\n![d.png](https://x/d.png)\nand more\nend\n")
        for cut in range(1, len(text)):
            with self.subTest(cut=cut):
                await self._fresh()
                self.assertEqual(await self._saved([text[:cut], text[cut:]]), whole)
        with self.subTest(chunk=1):
            await self._fresh()
            self.assertEqual(await self._saved(list(text)), whole)


def _card_output_text(output: list, call_id: str) -> str:
    """A tool card's Output: Open WebUI 0.12's `getToolResultText` (`structuredOutput.ts`), the
    text parts of `function_call_output`, images left out."""
    item = next(i for i in output if i.get("type") == "function_call_output"
                and i.get("call_id") == call_id)
    return "".join(str(p.get("text", "")) for p in item.get("output") or []
                   if p.get("type") != "input_image" and p.get("text") is not None)


class ToolCardOutputTextTests(_TurnCase):
    """ELI-121: a tool result is a string, content blocks, or the envelope (`content` + `details`,
    `sanitizeToolResult`). The card shows only the text."""

    # The exec tool's result (`ExecToolDetails`, bash-tools.exec-types.ts).
    ENVELOPE = {"content": [{"type": "text", "text": "README.md\nsrc"}],
                "details": {"status": "completed", "exitCode": 0, "durationMs": 14,
                            "aggregated": "README.md\nsrc", "cwd": "/home/runner/work"}}
    CASES = (
        (ENVELOPE, "README.md\nsrc"),
        ({"content": [{"type": "text", "text": "line 1"}, {"type": "text", "text": "line 2"}],
          "details": {"truncated": False}}, "line 1\nline 2"),
        ({"content": [{"type": "text", "text": "Shot taken."},
                      {"type": "image", "mimeType": "image/png", "bytes": 2048, "omitted": True}],
          "details": {"path": "/tmp/s.png"}}, "Shot taken.\n[image/png image]"),
        ([{"type": "text", "text": "from a CLI tool"}], "from a CLI tool"),
        ("plain text", "plain text"),
        ({"ok": True, "count": 2}, json.dumps({"ok": True, "count": 2})),
    )


    def _tool(self, phase, **data):
        return {"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": "run-1", "stream": "tool",
            "data": {"phase": phase, "toolCallId": "t1", "name": "exec",
                     "args": {"command": "ls"}, **data}}}

    async def _inline_card(self, result, is_error=False) -> str:
        """What the card shows after a pipe run with one tool call."""
        async def emitter(event):
            pass
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "go"}]}
        meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": "m1"}
        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta)

        async def drain():
            return [c async for c in gen]
        task = asyncio.ensure_future(drain())
        await self._until_registered("run-1")
        q = self._queue("run-1")
        q.put_nowait(self._tool("start"))
        q.put_nowait(self._tool("result", result=result, isError=is_error))
        q.put_nowait(self._final("run-1"))
        _content, output = _owui_saved(await asyncio.wait_for(task, 5))   # bounds the test only
        return _card_output_text(output, "t1")

    @staticmethod
    def _renderer_card(result, is_error=False) -> str:
        r = _TurnRenderer()
        r.feed({"stream": "tool", "data": {"phase": "start", "name": "exec", "toolCallId": "t1",
                                           "args": {"command": "ls"}}})
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "exec", "toolCallId": "t1",
                                           "result": result, "isError": is_error}})
        return _card_output_text(r.output, "t1")

    async def test_the_inline_card_shows_the_output(self):
        for result, shown in self.CASES:
            with self.subTest(result=result):
                await self.asyncTearDown()
                await self.asyncSetUp()
                self.assertEqual(await self._inline_card(result), shown)

    def test_a_handed_over_card_shows_the_output(self):
        for result, shown in self.CASES:
            with self.subTest(result=result):
                self.assertEqual(self._renderer_card(result), shown)

    async def test_details_never_reach_the_card(self):
        inline = await self._inline_card(self.ENVELOPE)
        for card in (inline, self._renderer_card(self.ENVELOPE)):
            self.assertNotIn("cwd", card)
            self.assertNotIn("details", card)
            self.assertNotIn('"content"', card)

    async def test_a_failed_call_explains_itself_with_the_output(self):
        err = {"content": [{"type": "text", "text": "ls: cannot access 'x'"}],
               "details": {"status": "failed", "exitCode": 2, "cwd": "/w"}}
        for card in (await self._inline_card(err, is_error=True),
                     self._renderer_card(err, is_error=True)):
            self.assertEqual(card, f"{TOOL_ERROR_MARK} tool call failed\n\nls: cannot access 'x'")


class _InlineReply:
    """A pipe reply with a scripted Gateway (`NoDroppedMessagesTests`)."""

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered

    def fail(self, msg):
        raise AssertionError(msg)

    async def saved(self, chunks, base_url) -> tuple[str, list]:
        """(content, output) saved for one run streaming `chunks`, directives resolved via the file
        server at `base_url`."""
        await self.asyncSetUp()
        try:
            self.pipe.valves.USE_OWUI_FILES = False
            self.pipe.valves.FILE_SERVER_BASE_URL = base_url

            async def emitter(event):
                pass
            body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "go"}]}
            meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": "m1"}
            gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta)

            async def drain():
                return [c async for c in gen]
            task = asyncio.ensure_future(drain())
            await self._until_registered("run-1")
            q = self._queue("run-1")
            for c in chunks:
                q.put_nowait(self._delta("run-1", c))
            q.put_nowait(self._final("run-1"))
            return _owui_saved(await asyncio.wait_for(task, 5))   # bounds the test only
        finally:
            await self.asyncTearDown()


def _message_texts(output: list) -> list:
    return ["".join(str(p.get("text", "")) for p in i.get("content") or [])
            for i in output if i.get("type") == "message"]


class WriterMediaLineTests(_RoutingCase):
    """ELI-117: inline and handed-over replies save the same content for the same text; text after
    a `MEDIA:` directive goes on its own line in both."""

    closed_run = HandoverRewriteTests.closed_run
    settle = HandoverRewriteTests.settle
    BASE = "https://x"
    TEXTS = (
        "Here: MEDIA:x.png more",
        "Shot MEDIA:x.png\nnext line",
        "a MEDIA:x.png   \n\n  b",
        "Two: MEDIA:x.png MEDIA:y.png end",
        "Done: MEDIA:x.png\n",
        "Quoted `MEDIA:x.png ` stays, real: MEDIA:y.png then",
    )

    async def _handed_over(self, text) -> tuple[str, list]:
        """(content, output) the turn writer saves for a run never streamed inline, directives
        resolved via the same server."""
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB, content="")
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB)

        async def resolve(directive, **kw):
            fname = directive.split(":", 1)[1].strip()
            return f"![{fname}]({self.BASE}/{fname})", True
        with mock.patch.object(openclaw_pipe, "_resolve_media_text", resolve):
            await self.dispatch(run, _delta(text))
            await self.dispatch(run, {"state": "final"})
            self.history = {"messages": self.closed_run(run, text)}
            await self.settle(run)
        m = self.owui.msgs()[self.STUB]
        self.assertIs(m["done"], True)
        return m["content"], m["output"]

    async def test_inline_and_handed_over_replies_save_the_same(self):
        for text in self.TEXTS:
            with self.subTest(text=text):
                await self.asyncSetUp()
                inline_content, inline_output = await _InlineReply().saved([text], self.BASE)
                content, output = await self._handed_over(text)
                self.assertEqual(content, inline_content)
                self.assertEqual(_message_texts(output), _message_texts(inline_output))

    async def test_the_text_after_a_directive_is_on_its_own_line(self):
        content, _output = await self._handed_over("Here: MEDIA:x.png more")
        self.assertEqual(content, f"Here: ![x.png]({self.BASE}/x.png)\nmore")


class HandoverMediaCodeAwareTests(_RoutingCase):
    """ELI-111, writer side: directives resolve as inline, and an image for a prose directive is
    not pasted over the same directive quoted in code."""

    closed_run = HandoverRewriteTests.closed_run
    settle = HandoverRewriteTests.settle

    async def test_a_quoted_directive_stays_text_after_a_handover(self):
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB, content="")
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB)
        text = "Real: MEDIA:x.png\nQuoted: `` MEDIA:x.png `` and ```MEDIA:y.png ``` done."
        calls = []

        async def resolve(t, **kw):
            calls.append(t)
            return t.replace("MEDIA:", "IMG:"), True
        with mock.patch.object(openclaw_pipe, "_resolve_media_text", resolve):
            await self.dispatch(run, _delta(text))
            await self.dispatch(run, {"state": "final"})
            self.history = {"messages": self.closed_run(run, text)}
            await self.settle(run)
        m = self.owui.msgs()[self.STUB]
        self.assertEqual(calls, ["MEDIA:x.png"])
        self.assertEqual(m["content"],
                         "Real: IMG:x.png\nQuoted: `` MEDIA:x.png `` and ```MEDIA:y.png ``` done.")


class ForwardedTurnHistoryTests(_RoutingCase):
    """GH #24: since 2026.10.2 `chat.history` shows a forwarded user turn as an "assistant" message
    (`isForwardedUserMessage`, chat-display-projection.helpers-pbUdgTgb.mjs). The `<id>:user` turn
    still starts the run."""

    FX = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                     "test_fixture_gh24_wake_history.json")))

    def with_cli_closers(self, messages):
        """A claude-cli history: each run closes with `cli-assistant:<runId>` repeating its text
        (cli-runner-C9KAAFBI.mjs:589, 2026.10.3). The fixture's fake model writes none."""
        out = []
        for n, m in enumerate(copy.deepcopy(messages)):
            out.append(m)
            run = (m.get("__openclaw") or {}).get("runId")
            nxt = (messages[n + 1].get("__openclaw") or {}).get("runId") if n + 1 < len(messages) else None
            if m["role"] == "assistant" and run and nxt != run:
                out.append({"role": "assistant", "idempotencyKey": f"cli-assistant:{run}",
                            "__openclaw": {"idempotencyKey": f"cli-assistant:{run}"},
                            "content": copy.deepcopy(m["content"])})
        return out

    def wake_run(self, version, token):
        msgs = self.FX[version]["messages"]
        return next(m["__openclaw"]["runId"] for m in msgs if m["role"] == "assistant"
                    and (openclaw_pipe._history_item(m) or {}).get("text", "").strip() == token)

    async def test_a_forwarded_cron_turn_reads_as_the_user_turn_it_is(self):
        self.history = {"messages": self.FX["2026.10.3"]["messages"]}
        msgs = (await self.conn.session_history(self.key))["messages"]
        prompts = [m for m in msgs if "Reply with exactly: WAKE_" in (openclaw_pipe._history_item(m) or {})["text"]]
        self.assertEqual([m["role"] for m in prompts], ["user", "user"])
        self.assertEqual(len(openclaw_pipe._history_user_turns(msgs)), 3, "the chat turn and both wakes")
        replies = [(openclaw_pipe._history_item(m) or {})["text"] for m in msgs if m["role"] == "assistant"]
        self.assertEqual(replies, ["E2E_WAKE_CHAT", "WAKE_ONE", "WAKE_TWO"])

    async def test_a_claude_cli_wake_ends_with_its_reply_only(self):
        """The cron prompt and its preamble are not the wake message's text."""
        run = self.wake_run("2026.10.3", "WAKE_ONE")
        self.history = {"messages": self.with_cli_closers(self.FX["2026.10.3"]["messages"])}
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "start", "runId": run,
                                        "hasActiveRun": True, "status": "running"})
        for payload in (_delta("WAKE"), _delta("_ONE"), {"state": "final"}):
            self.assertTrue(_turn_route_now(self.conn, self.ev(run, payload)))
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": run,
                                        "reason": "chat.run.settled"})
        await self.spin()
        m = self.new_messages()[turn_message_id(self.key, run, self.CHAT)]
        self.assertIs(m["done"], True)
        self.assertNotIn("Reply with exactly", json.dumps(m))
        self.assertEqual(m["content"], "WAKE_ONE")

    async def test_a_forwarded_sessions_send_turn_is_a_user_turn_too(self):
        turn = {"role": "assistant", "idempotencyKey": "t1:user", "senderLabel": "Forwarded from main",
                "provenance": {"kind": "inter_session", "sourceTool": "sessions_send"},
                "content": [{"type": "text", "text": "please check X"}]}
        self.history = {"messages": [turn]}
        (m,) = (await self.conn.session_history(self.key))["messages"]
        self.assertEqual(m["role"], "user")
        self.assertEqual(openclaw_pipe._history_user_turns([m]), [("t1", "sessions_send")])

    async def test_any_other_message_is_kept(self):
        for version in ("2026.9.3", "2026.10.3"):
            with self.subTest(version=version):
                msgs = self.FX[version]["messages"]
                reply_like = [m for m in msgs if m["role"] == "assistant" and not str(
                    m.get("idempotencyKey") or "").endswith(":user")]
                self.history = {"messages": msgs}
                got = (await self.conn.session_history(self.key))["messages"]
                self.assertEqual([m for m in got if m["role"] == "assistant"], reply_like)
        self.history = {"messages": self.FX["2026.9.3"]["messages"]}
        self.assertEqual((await self.conn.session_history(self.key))["messages"],
                         self.FX["2026.9.3"]["messages"], "9.3 history is unchanged")


class CancelledWriteFinishesTests(unittest.IsolatedAsyncioTestCase):
    """ELI-205: a cancel inside Open WebUI's DB write can wedge SQLite's write lock for good. A
    Stop never cancels the Pipe's writes midway: the write finishes, then the cancel goes on."""

    async def asyncSetUp(self):
        self.events = []
        self.in_db = asyncio.Event()       # the write is inside the DB call
        self.release = asyncio.Event()     # the DB call returns
        test = self
        chat = types.SimpleNamespace(
            title="New Chat", current_message_id=None,
            chat={"history": {"messages": {"m1": {"id": "m1", "role": "assistant", "meta": {}}},
                              "currentId": "m1"}})
        self.chat = chat

        async def db_call(what):
            test.events.append(f"{what} start")
            test.in_db.set()
            await test.release.wait()
            test.events.append(f"{what} done")

        class Chats:
            @staticmethod
            @contextlib.asynccontextmanager
            async def _chat_transaction(chat_id):
                await db_call("begin")
                yield ("db", chat)
                test.events.append("commit")

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                await db_call("upsert")
                return chat

        class ChatMessages:
            @staticmethod
            async def upsert_message(*a, **kw):
                return None

        def mod(name, **attrs):
            m = types.ModuleType(name)
            m.__dict__.update(attrs)
            return m
        patch = mock.patch.dict(sys.modules, {
            "open_webui.models": mod("open_webui.models"),
            "open_webui.models.chats": mod("open_webui.models.chats", Chats=Chats),
            "open_webui.models.chat_messages": mod("open_webui.models.chat_messages", ChatMessages=ChatMessages),
            "sqlalchemy": mod("sqlalchemy"), "sqlalchemy.orm": mod("sqlalchemy.orm"),
            "sqlalchemy.orm.attributes": mod("sqlalchemy.orm.attributes", flag_modified=lambda o, n: None)})
        patch.start()
        self.addCleanup(patch.stop)

    async def _cancel_midway(self, coro, last_event):
        task = asyncio.ensure_future(coro)
        await asyncio.wait_for(self.in_db.wait(), 2)
        task.cancel()                     # Stop, while the DB call runs
        await asyncio.sleep(0)
        self.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(self.events[-1], last_event, f"the write was cut short: {self.events}")

    async def test_set_turn_meta(self):
        await self._cancel_midway(openclaw_pipe._set_turn_meta("c1", "m1", {"k": 1}), "commit")
        self.assertEqual(self.chat.chat["history"]["messages"]["m1"]["meta"], {"k": 1})

    async def test_set_title_unless_chosen(self):
        await self._cancel_midway(openclaw_pipe._set_title_unless_chosen("c1", "A title"), "commit")
        self.assertEqual(self.chat.title, "A title")

    async def test_claim_turn_message(self):
        await self._cancel_midway(_claim_turn_message("c1", "u1", "m2", "run-1", "w1"), "commit")
        self.assertIn("m2", self.chat.chat["history"]["messages"])

    async def test_message_upsert(self):
        await self._cancel_midway(openclaw_pipe._owui_upsert_message("c1", "m1", {"done": True}), "upsert done")

    def test_every_message_upsert_goes_through_the_guarded_helper(self):
        src = inspect.getsource(openclaw_pipe)
        calls = re.findall(r"Chats\.upsert_message_to_chat_by_id_and_message_id\(", src)
        self.assertEqual(len(calls), 1, "a message upsert outside _owui_upsert_message")


class AutoTitleTests(unittest.IsolatedAsyncioTestCase):
    """ELI-182: the Pipe's title replaces only Open WebUI's own ('New Chat' or the first message,
    main.py run_initial_title_generation) and shows at once."""

    CHAT, USER, MID = "c1", "u1", "a2"
    GENERATED = "🧪 Generated title"

    def setUp(self):
        self.chat = types.SimpleNamespace(title="hello there", chat={"title": "hello there", "history": {
            "currentId": "a1", "messages": {
                "q1": {"id": "q1", "role": "user", "parentId": None, "childrenIds": ["a1"],
                       "content": "hello there"},
                "a1": {"id": "a1", "role": "assistant", "parentId": "q1", "childrenIds": [],
                       "content": "Hi!"},
            }}})
        test = self

        class Chats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                return copy.deepcopy(test.chat) if chat_id == test.CHAT else None

            @staticmethod
            @contextlib.asynccontextmanager
            async def _chat_transaction(chat_id):
                yield ("db", test.chat if chat_id == test.CHAT else None)

        chats_mod = types.ModuleType("open_webui.models.chats")
        chats_mod.Chats = Chats
        self.emits = []

        class Sio:
            @staticmethod
            async def emit(event, data, room=None):
                test.emits.append((event, data, room))

        mods = {**owui_modules(sio=Sio), "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.chats": chats_mod}
        p = mock.patch.dict(sys.modules, mods)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(openclaw_pipe, "_sender_is_owner", mock.AsyncMock(return_value=True))
        p.start()
        self.addCleanup(p.stop)
        self.pipe = Pipe()
        self.pipe._generate_title_text = mock.AsyncMock(return_value=self.GENERATED)

    def rename(self, title):
        self.chat.title, self.chat.chat = title, {**self.chat.chat, "title": title}

    async def second_message(self):
        body = {"messages": [{"role": "user", "content": "hello there"},
                             {"role": "assistant", "content": "Hi!"},
                             {"role": "user", "content": "and now?"}]}
        await self.pipe._auto_title(body, None, "and now?", self.CHAT,
                                    session_key="agent:main:x", user_id=self.USER, message_id=self.MID)

    def title_events(self):
        return [(d["chat_id"], d["data"], room) for ev, d, room in self.emits
                if ev == "events" and d["data"].get("type") == "chat:title"]

    async def test_a_title_the_user_chose_is_kept(self):
        self.rename("[QA] My renamed chat")
        await self.second_message()
        self.assertEqual((self.chat.title, self.chat.chat["title"]),
                         ("[QA] My renamed chat", "[QA] My renamed chat"))
        self.assertEqual(self.title_events(), [])

    async def test_a_rename_while_the_title_is_generated_is_kept(self):
        async def generate(*_a, **_k):
            self.rename("Mine")
            return self.GENERATED
        self.pipe._generate_title_text = generate
        await self.second_message()
        self.assertEqual(self.chat.title, "Mine")
        self.assertEqual(self.title_events(), [])

    async def test_the_first_message_title_is_replaced_and_shown_in_the_open_tab(self):
        await self.second_message()
        self.assertEqual((self.chat.title, self.chat.chat["title"]), (self.GENERATED, self.GENERATED))
        self.assertEqual(self.title_events(), [(self.CHAT, {"type": "chat:title", "data": self.GENERATED},
                                                f"user:{self.USER}")])

    async def test_an_image_in_the_first_exchange_never_reaches_the_title_prompt(self):
        """S12: a first message with an image is a list of blocks; slicing the list kept the base64
        image, and the title run failed with a prompt of millions of tokens."""
        image = "data:image/png;base64," + "A" * 100_000
        body = {"messages": [
            {"role": "user", "content": [{"type": "text", "text": "what is in this photo?"},
                                         {"type": "image_url", "image_url": {"url": image}}]},
            {"role": "assistant", "content": [{"type": "text", "text": "A temple in Kyoto."}]},
            {"role": "user", "content": "and now?"}]}
        await self.pipe._auto_title(body, None, "and now?", self.CHAT,
                                    session_key="agent:main:x", user_id=self.USER, message_id=self.MID)
        self.assertEqual(self.pipe._generate_title_text.call_args.args[1:3],
                         ("what is in this photo?", "A temple in Kyoto."))

    async def test_new_chat_is_replaced(self):
        self.rename("New Chat")
        await self.second_message()
        self.assertEqual(self.chat.title, self.GENERATED)

    async def test_the_first_message_title_is_known_as_open_webui_derives_it(self):
        """Open WebUI's fallback title: text parts without `<details>` and images."""
        self.chat.chat["history"]["messages"]["q1"]["content"] = [
            {"type": "text", "text": "<details type=\"x\">ctx</details> look ![a](b.png) "},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
            {"type": "text", "text": "here"}]
        self.rename("look  \nhere")
        await self.second_message()
        self.assertEqual(self.chat.title, self.GENERATED)


class TranscriptRewritePushTests(unittest.IsolatedAsyncioTestCase):
    """2026.10.5+: a stored-message rewrite is pushed as `sessions.changed` phase `message` with no
    `session.message`; it wakes only that session's transcript waiters."""

    async def test_phase_message_wakes_that_sessions_transcript_waiter(self):
        conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        mine = conn.session_message_event("agent:main:a")
        other = conn.session_message_event("agent:main:b")
        conn._on_sessions_changed({"sessionKey": "agent:main:a", "phase": "start", "ts": 1})
        self.assertFalse(mine.is_set(), "a run start is not a transcript change")
        conn._on_sessions_changed({"sessionKey": "agent:main:a", "phase": "message", "ts": 2})
        self.assertTrue(mine.is_set())
        self.assertFalse(other.is_set())


class RunMediaMessageTests(unittest.TestCase):
    """2026.10.10: a file named by path is stored in a message the Gateway appends for the run (no
    `runId`, key `<run>:assistant-media[:n]`, model `gateway-injected`)."""

    RUN = "msg-fc87f8b4-cd5b-4857-bc80-b97ca2b707ef-fcb1d90e-1793748028.778025"
    BLOCK = {"type": "image", "url": "/api/chat/media/outgoing/agent%3Amain%3Ax/633ad0e2/full",
             "alt": "e2e-gw-media.png", "mimeType": "image/png"}
    ERROR = {"type": "attachment_error", "attachment": {"label": "gone.png", "code": "file-not-found"}}

    def _history(self, key, *content):
        return {"messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "Here it is."}],
             "__openclaw": {"runId": self.RUN, "seq": 2}},
            {"role": "assistant", "content": list(content), "provider": "openclaw",
             "model": "gateway-injected", "idempotencyKey": key,
             "__openclaw": {"idempotencyKey": key, "seq": 3}}]}

    def test_the_runs_appended_media_message_counts(self):
        for key in (f"{self.RUN}:assistant-media:1", f"{self.RUN}:assistant-media"):
            with self.subTest(key=key):
                h = self._history(key, self.BLOCK, self.ERROR)
                self.assertEqual(openclaw_pipe._history_image_blocks_for_run(h, self.RUN), [self.BLOCK])
                self.assertEqual(openclaw_pipe._history_media_errors_for_run(h, self.RUN), ["gone.png"])

    def test_another_runs_media_message_does_not(self):
        for key in ("msg-other:assistant-media:1", f"{self.RUN}0:assistant-media:1",
                    f"{self.RUN}:assistant-mediax", self.RUN):
            with self.subTest(key=key):
                h = self._history(key, self.BLOCK, self.ERROR)
                self.assertEqual(openclaw_pipe._history_image_blocks_for_run(h, self.RUN), [])
                self.assertEqual(openclaw_pipe._history_media_errors_for_run(h, self.RUN), [])


class SteeredTranscriptSpanTests(unittest.TestCase):
    """A run a message was steered into: its span ends at the steered message, whose answer is its
    own span (probe_steer_events.py)."""

    @staticmethod
    def _user(key, text):
        return {"role": "user", "content": text, "__openclaw": {"idempotencyKey": f"{key}:user"}}

    @staticmethod
    def _said(text, run=None, stop="stop", key=None):
        meta = {k: v for k, v in (("runId", run), ("idempotencyKey", key)) if v}
        return {"role": "assistant", "content": [{"type": "text", "text": text}],
                "stopReason": stop, "__openclaw": meta}

    def _texts(self, span):
        return [_history_renderer(span).text.strip()] if span else []

    def test_built_in_runtime(self):
        hist = {"messages": [self._user("R1", "count"), self._said("PROBE_A slow-1 slow-2", run="R1"),
                             self._user("R2", "banana"), self._said("banana", run="R1")]}
        self.assertEqual(self._texts(_history_span_messages(hist, "R1")), ["PROBE_A slow-1 slow-2"])
        self.assertEqual(self._texts(_history_span_messages(hist, "R2")), ["banana"])

    def test_claude_cli(self):
        hist = {"messages": [
            self._user("R1", "4 rounds"), self._said("ROUND 1", stop="toolUse"),
            {"role": "toolResult", "content": "done"},
            self._user("R2", "Stop and say banana"), self._said("Stopped. banana", stop="end_turn"),
            self._said("ROUND 1\n\nStopped. banana", run="R1", key="cli-assistant:R1")]}
        self.assertEqual(self._texts(_history_span_messages(hist, "R1")), ["ROUND 1"])
        self.assertEqual(self._texts(_history_span_messages(hist, "R2")), ["Stopped. banana"])

    def test_claude_cli_with_imported_user_copies(self):
        """claude-cli adds an unkeyed copy of each user message after the keyed one."""
        copy = lambda text: {"role": "user", "content": f"Conversation info ...\n\n{text}"}  # noqa: E731
        hist = {"messages": [
            self._user("R1", "hold"), copy("hold"), self._said("E2E_CLI_HOLD_START", stop="end_turn"),
            self._user("R2", "aside"), copy("aside"), self._said("E2E_CLI_STEERED: aside", stop="end_turn"),
            self._said("E2E_CLI_HOLD_START\n\nE2E_CLI_STEERED: aside", run="R1", key="cli-assistant:R1")]}
        self.assertEqual(self._texts(_history_span_messages(hist, "R1")), ["E2E_CLI_HOLD_START"])
        self.assertEqual(self._texts(_history_span_messages(hist, "R2")), ["E2E_CLI_STEERED: aside"])

    def test_claude_cli_closing_copy_in_place_of_the_answer(self):
        """The closing copy holds only the last answer; the import merges it."""
        copy = lambda text: {"role": "user", "content": f"Conversation info ...\n\n{text}"}  # noqa: E731
        hist = {"messages": [
            self._user("R1", "hold"), copy("hold"), self._said("E2E_CLI_HOLD_START", stop="end_turn"),
            self._user("R2", "aside"), copy("aside"),
            self._said("E2E_CLI_STEERED: aside", run="R1", key="cli-assistant:R1")]}
        self.assertEqual(self._texts(_history_span_messages(hist, "R1")), ["E2E_CLI_HOLD_START"])
        self.assertEqual(self._texts(_history_span_messages(hist, "R2")), ["E2E_CLI_STEERED: aside"])

    def test_a_cron_prompt_recorded_mid_run_is_not_steered_into_it(self):
        """A wake's prompt recorded mid-run (2026.10.10 claude-cli) is not a steer: the run keeps
        all its messages, closer included."""
        def tool(i):
            return {"role": "assistant", "stopReason": "tool_use", "content": [
                {"type": "toolcall", "id": f"t{i}", "name": "exec", "arguments": {"command": f"step {i}"}},
                {"type": "tool_result", "tool_use_id": f"t{i}", "name": "exec",
                 "content": [{"type": "text", "text": f"out {i}"}]}]}
        hist = {"messages": [
            self._user("R1", "do two steps"), tool(1),
            {"role": "user", "content": "[cron:job1 wake] do W",
             "__openclaw": {"idempotencyKey": "46f6a533-3ebe-4822-90eb-6df00aaf33c9:user"}},
            tool(2), self._said("R-DONE", run="R1", key="cli-assistant:R1"),
            tool(3), self._said("W-DONE", run="W1", key="cli-assistant:W1")]}
        span = _history_span_messages(hist, "R1")
        self.assertIn("R-DONE", _history_renderer(span).text)
        # The walk passes the wake's prompt (#281); the run's first call is its own (#278).
        self.assertIn(tool(1), span)

    PIPE_RUN = "msg-c0ffee00-0000-4000-8000-000000000001-00000000-0000-4000-8000-000000000002-1.0"

    def test_a_message_the_pipe_sent_that_starts_like_a_cron_prompt_is_steered(self):
        """A Pipe-keyed send starting with `[cron:<id> ...]` is never a wake prompt (`wake_turns`
        skips them too) (#278)."""
        self.assertIsNotNone(openclaw_pipe.parse_pipe_run_id(self.PIPE_RUN))
        hist = {"messages": [self._user("R1", "count"), self._said("slow-1", run="R1"),
                             self._user(self.PIPE_RUN, "[cron:job1 x] what does this header mean?")]}
        self.assertEqual(openclaw_pipe._history_steered_host(hist, self.PIPE_RUN), "R1")
        self.assertTrue(openclaw_pipe._history_steered_answer_pending(hist, self.PIPE_RUN))

    def test_a_cron_prompt_stored_mid_run_does_not_end_a_steered_answer(self):
        """A wake's prompt stored mid-run does not end a steered message's span (#278)."""
        cron = {"role": "user", "content": "[cron:job1 wake] do W",
                "__openclaw": {"idempotencyKey": "46f6a533-3ebe-4822-90eb-6df00aaf33c9:user"}}
        hist = {"messages": [self._user("R1", "count"), self._said("slow-1", run="R1"),
                             self._user("R2", "banana"), cron]}
        self.assertTrue(openclaw_pipe._history_steered_answer_pending(hist, "R2"))
        hist["messages"].append(self._said("banana", run="R1"))
        self.assertEqual(self._texts(_history_span_messages(hist, "R2")), ["banana"])

    def test_a_steered_replys_sent_turn_is_its_answer(self):
        """A second Open WebUI call for a steered reply uses its steered span
        (`_history_steered_span`), not the host's closing copy (#278)."""
        stub = "00000000-0000-4000-8000-000000000002"
        r2 = f"msg-c0ffee00-0000-4000-8000-000000000001-{stub}-1.0"
        copy = lambda text: {"role": "user", "content": f"Conversation info ...\n\n{text}"}  # noqa: E731
        hist = {"messages": [
            self._user("R1", "hold"), copy("hold"), self._said("HOLD_START", stop="end_turn"),
            self._user(r2, "aside"), copy("aside"), self._said("STEERED: aside", stop="end_turn"),
            self._said("HOLD_START\n\nSTEERED: aside", run="R1", key="cli-assistant:R1")]}
        turn = openclaw_pipe._stub_turn(hist, stub, "", steers=True)
        self.assertTrue(turn["closed"])
        self.assertEqual(self._texts(turn["messages"]), ["STEERED: aside"])

    def test_two_messages_steered_at_one_boundary(self):
        hist = {"messages": [self._user("R1", "count"), self._said("slow-1", run="R1"),
                             self._user("A", "first"), self._user("B", "second"),
                             self._said("both", run="R1")]}
        self.assertEqual(_history_span_messages(hist, "A"), [], "its answer is the next one's")
        self.assertEqual(self._texts(_history_span_messages(hist, "B")), ["both"])
        self.assertEqual(self._texts(_history_span_messages(hist, "R1")), ["slow-1"])

    def test_a_steered_message_splits_the_run_where_claude_code_took_it_in(self):
        """A steered message sorts before text written before it was taken in; its commit time
        (`recordTimestampMs`) marks the split."""
        def tool(i, at):
            return {"role": "assistant", "stopReason": "tool_use", "timestamp": at, "content": [
                {"type": "toolcall", "id": f"t{i}", "name": "exec", "arguments": {"command": "sleep 8"}},
                {"type": "tool_result", "tool_use_id": f"t{i}", "name": "exec",
                 "content": [{"type": "text", "text": f"pause {i}"}]}]}

        def user(key, text, at, committed, into=None):
            meta = {"idempotencyKey": f"{key}:user", "recordTimestampMs": committed}
            return {"role": "user", "content": text, "timestamp": at,
                    "__openclaw": meta | ({"steerTargetRunId": into} if into else {})}

        def said(text, at, **kw):
            return {**self._said(text, **kw), "timestamp": at}

        def imported(text, at):
            return {"role": "user", "content": f"Conversation info ...\n\n{text}", "timestamp": at}
        raw = {"messages": [
            user("R1", "count", 1000, 1000), imported("count", 1001), said("1-20", 1002, stop="toolUse"),
            tool(1, 1003),
            user("R2", "say tomato", 1010, 1030, into="R1"), said("21-40", 1011, stop="toolUse"),
            tool(2, 1012),
            user("R3", "say blue", 1020, 1042, into="R1"), said("tomato", 1040, stop="end_turn"),
            imported("say blue", 1041), said("blue", 1050, stop="end_turn"),
            said("1-20 21-40 tomato blue", 1051, run="R1", key="cli-assistant:R1")]}
        conn = openclaw_pipe._GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))

        async def send_request(method, params, timeout=10):
            return copy.deepcopy(raw)
        conn.send_request = send_request
        hist = asyncio.run(conn.session_history("s"))
        first = _history_renderer(_history_span_messages(hist, "R1")).text
        self.assertIn("21-40", first, "what the run wrote before it took the message in")
        self.assertNotIn("tomato", first)
        self.assertEqual(self._texts(_history_span_messages(hist, "R2")), ["tomato"])
        self.assertEqual(self._texts(_history_span_messages(hist, "R3")), ["blue"])

    def test_a_message_queued_behind_the_run_is_not_its_steered_answers_end(self):
        """A queued (not steered) message stored when sent is not the end of the earlier steered
        answer. Claude Code's copy is written just before the commit and stays after it."""
        def user(key, text, at, committed, into=None):
            meta = {"idempotencyKey": f"{key}:user", "recordTimestampMs": committed}
            return {"role": "user", "content": text, "timestamp": at,
                    "__openclaw": meta | ({"steerTargetRunId": into} if into else {})}

        def said(text, at, **kw):
            return {**self._said(text, **kw), "timestamp": at}

        def imported(text, at):
            return {"role": "user", "content": f"Conversation info ...\n\n{text}", "timestamp": at}
        raw = {"messages": [
            user("R1", "count", 1000, 1000), imported("count", 1001), said("PART-1", 1002, stop="toolUse"),
            user("R2", "say tomato", 1010, 1030, into="R1"), said("PART-2", 1011, stop="toolUse"),
            user("R3", "say blue", 1012, 1040),
            imported("say tomato", 1029), said("tomato", 1031, run="R1", key="cli-assistant:R1"),
            imported("say blue", 1041), said("blue", 1042, run="R3", key="cli-assistant:R3")]}
        conn = openclaw_pipe._GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))

        async def send_request(method, params, timeout=10):
            return copy.deepcopy(raw)
        conn.send_request = send_request
        hist = asyncio.run(conn.session_history("s"))
        first = _history_renderer(_history_span_messages(hist, "R1")).text
        self.assertTrue("PART-1" in first and "PART-2" in first and "tomato" not in first, first)
        self.assertEqual(self._texts(_history_span_messages(hist, "R2")), ["tomato"])
        self.assertEqual(self._texts(_history_span_messages(hist, "R3")), ["blue"])

    def _commit_ordered(self, messages):
        conn = openclaw_pipe._GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))

        async def send_request(method, params, timeout=10):
            return {"messages": copy.deepcopy(messages)}
        conn.send_request = send_request
        return asyncio.run(conn.session_history("s"))["messages"]

    @staticmethod
    def _sent(key, text, at, committed):
        return {"role": "user", "content": text, "timestamp": at,
                "__openclaw": {"idempotencyKey": f"{key}:user", "recordTimestampMs": committed,
                               "steerTargetRunId": "R1"}}

    def test_messages_committed_together_keep_the_order_they_were_sent_in(self):
        """Two messages committed at the same time keep their send order (#290)."""
        got = self._commit_ordered([
            self._sent("R1", "count", 1000, 1000), {**self._said("slow-1"), "timestamp": 1001},
            self._sent("A", "first", 1002, 1010), self._sent("B", "second", 1003, 1010),
            {**self._said("both"), "timestamp": 1011}])
        self.assertEqual([m.get("content") if isinstance(m.get("content"), str) else "said" for m in got],
                         ["count", "said", "first", "second", "said"])

    def test_a_message_stops_at_its_own_copy_not_an_earlier_one_with_its_text(self):
        """A message stops at its own copy, not an earlier one with the same text (#290)."""
        imported = lambda text, at: {"role": "user", "content": f"Conversation info ...\n\n{text}",  # noqa: E731
                                     "timestamp": at}
        got = self._commit_ordered([
            self._sent("R1", "ok?", 1000, 1000), imported("ok?", 1001),
            self._sent("A", "yes", 1002, 1004), self._sent("B", "yes", 1003, 1020),
            imported("yes", 1004), {**self._said("A done"), "timestamp": 1005},
            {**self._said("before B"), "timestamp": 1010},
            imported("yes", 1019), {**self._said("B done"), "timestamp": 1021}])
        keys = [(m.get("__openclaw") or {}).get("idempotencyKey") or (m.get("content") if isinstance(
            m.get("content"), str) else m["content"][0]["text"]) for m in got]
        self.assertLess(keys.index("before B"), keys.index("B:user"))
        self.assertEqual(keys[keys.index("B:user") + 1], "Conversation info ...\n\nyes")

    def test_a_run_still_going_has_no_steered_answer_yet(self):
        hist = {"messages": [self._user("R1", "count"), self._said("slow-1", run="R1", stop="toolUse"),
                             self._user("R2", "banana")]}
        self.assertIsNone(_history_span_messages(hist, "R2"))

    def test_a_run_after_the_last_one_ended_is_its_own(self):
        """Queued, not steered: it starts after the earlier run closed."""
        hist = {"messages": [self._user("R1", "count"), self._said("slow-1", run="R1"),
                             self._user("R2", "next"), self._said("own answer", run="R2")]}
        self.assertEqual(self._texts(_history_span_messages(hist, "R1")), ["slow-1"])
        self.assertEqual(self._texts(_history_span_messages(hist, "R2")), ["own answer"])


class SettleSteeredRepliesTests(unittest.TestCase):
    """`_settle_misplaced`: rewrite replies around a steer only for a moved split."""

    def test_the_answer_began_in_the_earlier_reply(self):
        self.assertTrue(openclaw_pipe._settle_misplaced(["holdSTEERED:a", "a"], ["hold", "STEERED:a"]))
        self.assertTrue(openclaw_pipe._settle_misplaced(["holdSTEERED:a", "STEERED:a"], ["hold", "STEERED:a"]))

    def test_successive_steers_each_moved_their_split(self):
        """A run's replies are settled together, every split at once, so successive steers each
        move their split (#278)."""
        self.assertTrue(openclaw_pipe._settle_misplaced(
            ["holdSTEERED:", "aSTEERED:", "b"], ["hold", "STEERED:a", "STEERED:b"]))
        self.assertTrue(openclaw_pipe._settle_misplaced(
            ["hold", "STEERED:aSTEERED:", "b"], ["hold", "STEERED:a", "STEERED:b"]))
        self.assertFalse(openclaw_pipe._settle_misplaced(
            ["holdSTEERED:", "aXSTEERED:", "b"], ["hold", "STEERED:a", "STEERED:b"]))

    def test_a_reply_that_also_shows_the_next_ones_answer_is_misplaced(self):
        """A late steer (CI 42538220842): the first reply ran on into tomato's answer, which
        tomato's own reply shows too. The replies still hold only the transcript's text, in
        order, with no gap, so it is a moved split."""
        self.assertTrue(openclaw_pipe._settle_misplaced(
            ["holdSTEERED:a", "STEERED:a", "b"], ["hold", "STEERED:a", "b"]))
        self.assertTrue(openclaw_pipe._settle_misplaced(
            ["holdSTEERED:ab", "STEERED:a", "holdSTEERED:ab"], ["hold", "STEERED:a", "b"]),
            "the forced CI order (w8 sandbox): the first and last replies each show the whole run")
        self.assertFalse(openclaw_pipe._settle_misplaced(
            ["holdSTEERED:a", "STEERED:aX", "b"], ["hold", "STEERED:a", "b"]))
        self.assertFalse(openclaw_pipe._settle_misplaced(
            ["hold", "b", "STEERED:a"], ["hold", "STEERED:a", "b"]), "out of order")

    def test_replies_already_in_place_or_holding_more_stay(self):
        self.assertFalse(openclaw_pipe._settle_misplaced(["hold", "STEERED:a"], ["hold", "STEERED:a"]))
        self.assertFalse(openclaw_pipe._settle_misplaced(["holdnote", "STEERED:a"], ["hold", "STEERED:a"]))
        self.assertFalse(openclaw_pipe._settle_misplaced(["hold", "STEERED:a(note)"], ["hold", "STEERED:a"]))


class SettleSteeredRepliesKeepNotesTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_settled_reply_keeps_the_pipes_notes(self):
        """A settled reply keeps the Pipe's own notes (`_keep_pipe_notes`) (#278)."""
        note = {"type": "message", "id": f"{openclaw_pipe._NOTE_ITEM_PREFIX}approval_1", "status": "completed",
                "role": "assistant", "content": [{"type": "output_text", "text": "Approved."}]}
        saved = {"m1": {"content": "holdSTEERED:a", "output": [note]}, "m2": {"content": "a", "output": []}}

        class Chats:
            @staticmethod
            async def get_message_by_id_and_message_id(chat_id, message_id):
                return saved[message_id]

        async def emitter(*_a, **_k):
            return mock.AsyncMock()
        modules = owui_modules(get_event_emitter=emitter)
        modules["open_webui.models"] = types.ModuleType("open_webui.models")
        modules["open_webui.models.chats"] = types.ModuleType("open_webui.models.chats")
        modules["open_webui.models.chats"].Chats = Chats
        parts = []
        for text in ("hold", "STEERED:a"):
            part = openclaw_pipe._TurnRenderer()
            part._add_text(text)
            parts.append(part)
        conn = openclaw_pipe._GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        conn.session_history = mock.AsyncMock(return_value={"messages": []})
        upserts = {}

        async def upsert(chat_id, message_id, fields):
            upserts[message_id] = fields
            return True
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(openclaw_pipe, "_history_span_messages", lambda h, r: []), \
                mock.patch.object(openclaw_pipe, "_history_renderer", mock.Mock(side_effect=parts)), \
                mock.patch.object(openclaw_pipe, "_owui_upsert_message", upsert):
            await openclaw_pipe._settle_steered_replies(conn, "k", [
                {"chat_id": "c", "message_id": "m1", "span_run": "R1"},
                {"chat_id": "c", "message_id": "m2", "span_run": "R2"}])
        self.assertEqual(upserts["m1"]["content"], "hold")
        self.assertIn(note["id"], [i.get("id") for i in upserts["m1"]["output"]])


class SteerMarkerTests(unittest.TestCase):
    def test_the_steer_markers_go_in_past_a_full_queue(self):
        """Steer markers are control markers (`_RunQueue.put_control`) (#278)."""
        conn = openclaw_pipe._GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        host = openclaw_pipe._Consumer("s", "r1", [openclaw_pipe._RunQueue(maxsize=1)], started=True)
        steered = openclaw_pipe._Consumer("s", "r2", [openclaw_pipe._RunQueue(maxsize=1)])
        host.queues[0].put_nowait("event")
        steered.queues[0].put_nowait("event")
        conn._consumers = {"s:r1": host, "s:r2": steered}
        self.assertTrue(conn._steer(steered))
        self.assertEqual([q.qsize() for q in host.queues], [2], "the steer marker behind the event")
        self.assertEqual(steered.steered_into, "r1")


class SteerChainTests(unittest.TestCase):
    def test_a_second_steer_carries_the_replies_before_it(self):
        """A second steer carries all the replies before it (#278)."""
        conn = openclaw_pipe._GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        first = {"chat_id": "c", "message_id": "m1", "request_task_id": None, "span_run": "r1"}
        conn.register_run_target("s", "r1", "c", "m2", span_run="r2", before=[first])
        host = openclaw_pipe._Consumer("s", "r1", [openclaw_pipe._RunQueue()], started=True)
        steered = openclaw_pipe._Consumer("s", "r3", [openclaw_pipe._RunQueue()])
        conn._consumers = {"s:r1": host, "s:r3": steered}
        self.assertTrue(conn._steer(steered))
        earlier = steered.queues[0].get_nowait()["_steered"]["earlier"]
        self.assertEqual(earlier, {"chat_id": "c", "message_id": "m2", "request_task_id": None,
                                   "span_run": "r2", "before": [first]})


class MidRunSteeringTests(_TurnCase):
    """2026.10.10 steers a mid-run message into the active run at its next tool boundary. Order
    (probe_steer_events.py): the run's deltas; the steered run's lone final; the answer on the
    active run, whose final repeats all its text."""

    SK = None
    CHAT = "c0ffee00-0000-4000-8000-000000000001"

    @classmethod
    def _run_id(cls, n):
        """What `pipe_run_id` gives the reply run of message n of the chat."""
        return f"msg-{cls.CHAT}-00000000-0000-4000-8000-{n:012d}-1.0"

    async def asyncSetUp(self):
        await NoDroppedMessagesTests.asyncSetUp(self)
        type(self).SK = self.ocp._owui_session_key(self.pipe.valves.AGENT_ID, "u1", self.CHAT)
        self.R1, self.R2, self.R3 = (self._run_id(n) for n in (1, 2, 3))
        send = self.conn.send_request

        async def send_request(method, params, timeout=10):
            got = await send(method, params, timeout)
            if method == "chat.send":
                return {"runId": self._run_id(int(got["runId"].rsplit("-", 1)[1]))}
            return got
        self.conn.send_request = send_request

    def _reply(self, text, msg_id, **kw):
        return NoDroppedMessagesTests._reply(self, text, msg_id, chat=self.CHAT, **kw)
    _spin = staticmethod(NoDroppedMessagesTests._spin)
    _collect = staticmethod(NoDroppedMessagesTests._collect)

    def _lifecycle(self, run_id, phase):
        return {"type": "event", "event": "agent", "payload": {
            "sessionKey": self.SK, "runId": run_id, "stream": "lifecycle", "data": {"phase": phase}}}

    def _final(self, run_id, text=None):
        payload = {"sessionKey": self.SK, "runId": run_id, "state": "final"}
        if text is not None:
            payload["message"] = {"role": "assistant", "content": [{"type": "text", "text": text}]}
        return {"type": "event", "event": "chat", "payload": payload}

    def _commit(self, run_id, text="aside", into=None):
        """The `session.message` committing a message sent as `run_id`; `into`: the run it was
        steered into (`steerTargetRunId`)."""
        meta = {"idempotencyKey": f"{run_id}:user"} | ({"steerTargetRunId": into} if into else {})
        return {"type": "event", "event": "session.message", "payload": {
            "sessionKey": self.SK, "message": {"role": "user", "content": text, "__openclaw": meta}}}

    async def _dispatch(self, *msgs):
        for msg in msgs:
            if msg.get("event") == "session.message":
                self.conn._on_session_message(msg["payload"])
            else:
                await self.conn._dispatch_run_event(msg)
        await self._spin()

    async def _running_first_reply(self):
        gen = self._reply("Do 4 rounds of sleep 12", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered(self.R1)
        self.active = True
        await self._dispatch(self._lifecycle(self.R1, "start"), self._delta(self.R1, "ROUND 1"))
        return gen, first

    async def test_a_mid_run_message_is_steered_and_answered_in_its_own_reply(self):
        self.conn.gateway_version = "2026.10.10"
        gen_a, first_a = await self._running_first_reply()
        gen_b = self._reply("Stop and say banana", "m2")
        first_b = asyncio.ensure_future(gen_b.__anext__())
        await self._until_registered(self.R2)
        self.assertEqual(self.sent[-1], "Stop and say banana", "sent while the run works")
        self.assertFalse(any("Queued: waiting" in str(e) for e in self.emitted))
        self.assertTrue(any(self.ocp.STEER_PENDING_STATUS in str(e) for e in self.emitted),
                        "the reply says its message went to the running agent")

        await self._dispatch(
            self._final(self.R2),                       # steered: no message
            self._delta(self.R1, "\n\nStopped. banana"),
            self._lifecycle(self.R1, "end"))
        self.active = False
        await self._dispatch(self._final(self.R1, "ROUND 1\n\nStopped. banana"))
        out_a, _ = await self._collect(first_a, gen_a)
        out_b, _ = await self._collect(first_b, gen_b)
        self.assertEqual(out_a.strip(), "ROUND 1", "the first reply ends where the steer landed")
        self.assertEqual(out_b.strip(), "Stopped. banana", "the answer is under the steered message")
        self.assertEqual(self.aborts, [], "nothing taken back")
        self.assertEqual(self.sent.count("Stop and say banana"), 1, "sent once")

    async def test_a_steered_reply_whose_answer_streamed_before_its_commit_shows_its_own_answer(self):
        """claude-cli streamed blue's answer before its commit, so the first reply got it and the
        steered reply's baselines hold it: the rest of the run is repeats, and blue yields nothing
        (CI 42538220842, variant A). It ends with its own answer from the transcript, once the
        transcript closed the run: never blank, never the whole run, even with no settle."""
        transcript = self._blue_transcript()
        whole = "ROUND 1\n\nblue answer"
        said = SteeredTranscriptSpanTests._said
        async with self._blue_streamed_before_its_commit(transcript, whole) as (first_b, gen_b):
            self.history = {"messages": [*transcript, said(whole, run=self.R1, key=f"cli-assistant:{self.R1}")]}
            self.conn._session_message_waiters.pop(self.SK).set()
            out_b, _ = await self._collect(first_b, gen_b)
        self.assertEqual(out_b.strip(), "blue answer")

    async def test_a_steered_reply_whose_run_ends_clean_with_its_answer_never_closed_ends_when_the_session_settles(self):
        """The run blue went into ends cleanly, but the transcript never closes blue's answer and no
        later turn starts. The session settling ends the wait: blue shows what follows its message
        (claude-cli's own record), never the whole run."""
        transcript = self._blue_transcript()
        async with self._blue_streamed_before_its_commit(transcript, "ROUND 1\n\nblue answer") as (first_b, gen_b):
            self.list_active = False
            self.conn._on_sessions_changed({"sessionKey": self.SK, "hasActiveRun": False, "status": "done"})
            out_b, _ = await self._collect(first_b, gen_b)
        self.assertEqual(out_b.strip(), "blue answer")

    async def test_a_steered_reply_whose_message_never_reached_the_transcript_ends_when_the_session_settles(self):
        """Blue's message is not in the transcript at all. The session settling ends the wait: the
        reply ends with the no-text note, never blank and never the whole run."""
        transcript = self._blue_transcript()[:3]
        async with self._blue_streamed_before_its_commit(transcript, "ROUND 1\n\nblue answer") as (first_b, gen_b):
            self.list_active = False
            self.conn._on_sessions_changed({"sessionKey": self.SK, "hasActiveRun": False, "status": "done"})
            out_b, _ = await self._collect(first_b, gen_b)
        self.assertIn("This turn produced no reply text", out_b)
        self.assertNotIn("ROUND 1", out_b)

    def _blue_transcript(self) -> list:
        """claude-cli's transcript of the held run with blue steered in, before the host's closer."""
        user, said = SteeredTranscriptSpanTests._user, SteeredTranscriptSpanTests._said
        return [user(self.R1, "Do 4 rounds of sleep 12"), said("ROUND 1", stop="toolUse"),
                {"role": "toolResult", "content": "done"},
                user(self.R2, "say blue"), said("blue answer", stop="end_turn")]

    @contextlib.asynccontextmanager
    async def _blue_streamed_before_its_commit(self, transcript: list, whole: str):
        """Blue's answer streams before its commit, the run ends, and blue's reply waits on the
        transcript, with the session still running and no settle. Yields blue's reply to finish."""
        self.conn.gateway_version = "2026.10.10"
        gen_a, first_a = await self._running_first_reply()
        gen_b = self._reply("say blue", "m2")
        first_b = asyncio.ensure_future(gen_b.__anext__())
        await self._until_registered(self.R2)
        with mock.patch.object(openclaw_pipe, "_settle_steered_replies", mock.AsyncMock()):
            await self._dispatch(
                self._delta(self.R1, "\n\nblue answer"),
                self._commit(self.R2, "say blue", into=self.R1),
                self._final(self.R2),
                self._lifecycle(self.R1, "end"))
            self.active, self.list_active = False, True
            self.history = {"messages": transcript}
            await self._dispatch(self._final(self.R1, whole))
            self.assertFalse(first_b.done(), "it waits for its answer in the transcript")
            yield first_b, gen_b
            await self._collect(first_a, gen_a)

    async def test_the_answer_goes_to_the_steered_reply_from_its_message_commit(self):
        """The built-in runtime's order: commit, the answer's first event, then the steered run's
        lone final. The commit is the boundary."""
        self.conn.gateway_version = "2026.10.10"
        gen_a, first_a = await self._running_first_reply()
        gen_b = self._reply("Stop and say banana", "m2")
        first_b = asyncio.ensure_future(gen_b.__anext__())
        await self._until_registered(self.R2)
        await self._dispatch(
            self._commit(self.R2, "Stop and say banana", into=self.R1),
            self._delta(self.R1, "\n\nStopped."),
            self._final(self.R2),
            self._delta(self.R1, " banana"),
            self._lifecycle(self.R1, "end"))
        self.active = False
        await self._dispatch(self._final(self.R1, "ROUND 1\n\nStopped. banana"))
        out_a, _ = await self._collect(first_a, gen_a)
        out_b, _ = await self._collect(first_b, gen_b)
        self.assertEqual(out_a.strip(), "ROUND 1")
        self.assertEqual(out_b.strip(), "Stopped. banana")
        self.assertNotIn(f"{self.SK}:{self.R2}", self.conn._consumers, "its lone final retired it")

    async def test_a_steered_reply_reads_as_working_until_its_answer(self):
        """A steered reply reads as working until its answer's first text."""
        self.conn.gateway_version = "2026.10.10"
        gen_a, first_a = await self._running_first_reply()
        statuses = []

        async def emitter(event):
            if event.get("type") == "status":
                statuses.append(event["data"])
        gen_b = self.pipe._pipe_impl(
            {"model": "openclaw_gateway.default",
             "messages": [{"role": "user", "content": "add tomatoes"}]},
            emitter, __metadata__={"chat_id": self.CHAT, "user_id": "u1", "message_id": "m2"})
        first_b = asyncio.ensure_future(gen_b.__anext__())
        await self._until_registered(self.R2)
        await self._dispatch(self._commit(self.R2, "add tomatoes", into=self.R1))
        self.assertEqual(statuses[-1].get("description"), "Thinking...", statuses)
        self.assertIs(statuses[-1].get("done"), False)
        await self._dispatch(self._delta(self.R1, "\n\nAdded."), self._final(self.R2),
                             self._lifecycle(self.R1, "end"))
        self.active = False
        await self._dispatch(self._final(self.R1, "ROUND 1\n\nAdded."))
        await self._collect(first_a, gen_a)
        out_b, _ = await self._collect(first_b, gen_b)
        self.assertEqual(out_b.strip(), "Added.")
        self.assertIs(statuses[-1].get("done"), True, "and ends like one")

    async def test_a_steered_run_whose_lone_final_never_comes_is_not_kept(self):
        """A steered message's run is not a reply of its own (`has_reply`, `_serving`) and goes
        with the run it was steered into; its lone final can be lost (#278)."""
        self.conn.gateway_version = "2026.10.10"
        gen_a, first_a = await self._running_first_reply()
        gen_b = self._reply("Stop and say banana", "m2")
        first_b = asyncio.ensure_future(gen_b.__anext__())
        await self._until_registered(self.R2)
        await self._dispatch(
            self._commit(self.R2, "Stop and say banana", into=self.R1),
            self._delta(self.R1, "\n\nStopped. banana"),
            self._lifecycle(self.R1, "end"))
        self.active = False
        await self._dispatch(self._final(self.R1, "ROUND 1\n\nStopped. banana"))
        out_b, _ = await self._collect(first_b, gen_b)
        await self._collect(first_a, gen_a)
        self.assertEqual(out_b.strip(), "Stopped. banana")
        self.assertNotIn(f"{self.SK}:{self.R2}", self.conn._consumers)
        self.assertFalse(self.conn.has_reply(self.SK))
        self.assertFalse(self.conn._serving())

    async def test_a_message_committed_after_the_run_ended_is_not_steered(self):
        """Committed after the active run's terminal: queued, not steered."""
        self.conn.gateway_version = "2026.10.10"
        gen_a, first_a = await self._running_first_reply()
        gen_b = self._reply("next", "m2")
        first_b = asyncio.ensure_future(gen_b.__anext__())
        await self._until_registered(self.R2)
        await self._dispatch(self._lifecycle(self.R1, "end"), self._final(self.R1, "ROUND 1"))
        self.active = False
        await self._dispatch(self._commit(self.R2, "next"), self._lifecycle(self.R2, "start"),
                             self._delta(self.R2, "own answer"), self._lifecycle(self.R2, "end"),
                             self._final(self.R2, "own answer"))
        out_a, _ = await self._collect(first_a, gen_a)
        out_b, _ = await self._collect(first_b, gen_b)
        self.assertEqual(out_a.strip(), "ROUND 1")
        self.assertEqual(out_b.strip(), "own answer")

    async def test_two_messages_steered_at_one_boundary_answer_in_the_last_reply(self):
        """Two messages steered at one boundary: the earlier reply points to the next."""
        self.conn.gateway_version = "2026.10.10"
        gen_a, first_a = await self._running_first_reply()
        replies = []
        for i, text in enumerate(("first aside", "second aside"), start=2):
            gen = self._reply(text, f"m{i}")
            replies.append((gen, asyncio.ensure_future(gen.__anext__())))
            await self._until_registered(self._run_id(i))
        await self._dispatch(self._final(self.R2), self._final(self.R3),
                             self._delta(self.R1, "\n\nBoth asides answered."),
                             self._lifecycle(self.R1, "end"))
        self.active = False
        await self._dispatch(self._final(self.R1, "ROUND 1\n\nBoth asides answered."))
        out_a, _ = await self._collect(first_a, gen_a)
        (gen_b, first_b), (gen_c, first_c) = replies
        out_b, _ = await self._collect(first_b, gen_b)
        out_c, _ = await self._collect(first_c, gen_c)
        self.assertEqual(out_a.strip(), "ROUND 1")
        self.assertEqual(out_b.strip(), self.ocp.REPLY_SENT_WITH_NEXT_NOTE)
        self.assertEqual(out_c.strip(), "Both asides answered.")

    async def test_a_steer_takes_the_run_over_from_its_turn_writer(self):
        """A steer ends a turn writer's message with what it streamed (ELI-158); the run's later
        events go to the steered reply."""
        self.conn.gateway_version = "2026.10.10"
        conn, sk = self.conn, self.SK
        pump = openclaw_pipe._RunPump()
        writer = mock.Mock(message_id="w1", renderer=types.SimpleNamespace(received="ROUND 1",
                                                                            last_item=""))
        writer.steered_away = lambda: {"received": "ROUND 1", "stream": "ROUND 1", "item": ""}
        writer.finalize = mock.AsyncMock()
        writer.feed = mock.AsyncMock()
        pump.writer, pump.started = writer, True
        conn._turn_pumps[(sk, self.R1)] = pump
        q2 = conn.register_consumer(sk, self.R2)
        conn._on_session_message(self._commit(self.R2, into=self.R1)["payload"])
        self.assertIn(f"{sk}:{self.R1}", conn._consumers, "the active run is consumed by the steered reply now")
        self.assertIs(conn._consumers[f"{sk}:{self.R1}"].queues[0], q2)
        await conn._dispatch_run_event(self._delta(self.R1, "\n\nStopped. banana"))
        await conn._dispatch_run_event(self._final(self.R2))
        steered = q2.get_nowait()["_steered"]
        self.assertEqual(steered["run"], self.R1)
        self.assertEqual(q2.get_nowait()["payload"]["runId"], self.R1, "the next delta comes here")
        self.assertTrue(q2.empty(), "the steered run's own lone final is not shown")
        self.assertNotIn((sk, self.R2), conn._turn_pumps, "nor written as a run of its own")
        kind, fut = pump.queue.get_nowait()
        self.assertEqual(kind, "steered")
        pump.queue.put_nowait((kind, fut))
        await openclaw_pipe._turn_pump_loop(conn, sk, self.R1, ("u1", self.CHAT), pump)
        self.assertEqual((await steered["run_text"])["stream"], "ROUND 1")
        writer.finalize.assert_awaited_once_with(steered=True)
        writer.feed.assert_not_awaited()

    async def test_a_steered_message_whose_reply_a_turn_writer_has_still_ends_the_run_there(self):
        """A steered message held by a turn writer: from its commit the first reply gets no more
        events and ends from the transcript (#290); the writer shows the answer."""
        self.conn.gateway_version = "2026.10.10"
        conn = self.conn
        # A session key the turn writer routes.
        conn._valves = lambda: types.SimpleNamespace(AGENT_ID="main")
        self.SK = sk = f"agent:main:openwebui-00000000-0000-4000-8000-0000000000aa-{self.CHAT}"
        host = openclaw_pipe._RunPump()
        writer = mock.Mock(chat_id=self.CHAT, message_id="w1", renderer=openclaw_pipe._TurnRenderer())
        writer.renderer.feed(self._delta(self.R1, "21-40")["payload"])
        writer.finalize = mock.AsyncMock()
        writer.feed = mock.AsyncMock()
        host.writer, host.started = writer, True
        # Its pump has not written this one yet.
        host.queue.put_nowait(("event", self._delta(self.R1, " 41-50")["payload"]))
        conn._turn_pumps[(sk, self.R1)] = host
        tomato = openclaw_pipe._RunPump()
        tomato.handover = {"message_id": "w2", "steer_pending": True,
                           "renderer": types.SimpleNamespace(received="", output=None)}
        conn._turn_pumps[(sk, self.R2)] = tomato
        q3 = conn.register_consumer(sk, self.R3)

        conn._on_session_message(self._commit(self.R2, "say tomato", into=self.R1)["payload"])
        host.queue.get_nowait()
        self.assertTrue(host.queue.empty(), "the first reply ends from the transcript, not here")
        with mock.patch.object(openclaw_pipe, "_turn_pump_loop", mock.AsyncMock()) as loop:
            await conn._dispatch_run_event(self._delta(self.R1, "tomato"))
        loop.assert_not_called()
        self.assertTrue(host.queue.empty(), "the tomato answer is not the first reply's")
        # Tomato's writer shows the answer live from the commit on.
        kind, steered = tomato.queue.get_nowait()
        self.assertEqual(kind, "steered_in")
        self.assertEqual(steered["run_text"]["received"], "21-40 41-50",
                         "after what the first reply showed, the events it has yet to write included")
        self.assertEqual(tomato.queue.get_nowait(), ("event", self._delta(self.R1, "tomato")["payload"]))
        self.assertEqual([k for k in conn._turn_pumps if k[1] == self.R1], [(sk, self.R1)],
                         "and no writer of its own")

        # What tomato's pump wrote: the first reply's baselines, then its answer.
        tomato.writer = mock.Mock(chat_id=self.CHAT, message_id="w2", renderer=openclaw_pipe._TurnRenderer())
        tomato.writer.renderer.seed(steered["run_text"])
        tomato.writer.renderer.feed(self._delta(self.R1, "tomato")["payload"])

        conn._on_session_message(self._commit(self.R3, "say blue", into=self.R1)["payload"])
        blue = q3.get_nowait()["_steered"]
        self.assertEqual(blue["run"], self.R1, "the next steer takes the run")
        # The handover keeps the run's target and renderer, so blue starts with baselines (#300).
        self.assertEqual(blue["run_text"]["received"], "21-40 41-50tomato", "after what tomato's reply showed")
        self.assertEqual(blue["earlier"]["message_id"], "w2")
        # Tomato's reply holds its own run's span, after the first reply's: the settle's chain.
        self.assertEqual(blue["earlier"]["span_run"], self.R2, "tomato's span, not the host run's")
        self.assertEqual(blue["earlier"]["before"], [{"chat_id": self.CHAT, "message_id": "w1",
                                                      "request_task_id": None, "span_run": self.R1}])
        await conn._dispatch_run_event(self._delta(self.R1, "blue"))
        self.assertEqual(q3.get_nowait()["payload"]["runId"], self.R1)
        self.assertTrue(host.queue.empty(), "nothing of the run after the commit reached the first reply")
        self.assertTrue(tomato.queue.empty(), "nor, after the next steer, tomato's")
        # A clean end marks both writers that still wait for the settle (#300).
        await conn._dispatch_run_event(self._lifecycle(self.R1, "end"))
        self.assertEqual((host.ended_clean, tomato.ended_clean), (self.R1, self.R1))
        conn.register_consumer(sk, self._run_id(4))
        await conn._dispatch_run_event(self._lifecycle(self._run_id(4), "model"))
        self.assertEqual(host.queue.get_nowait(), ("settled", None))
        self.assertEqual(tomato.queue.get_nowait(), ("settled", None))

    async def test_a_turn_writer_whose_message_was_steered_in_shows_the_rest_as_working(self):
        """Tomato's writer starts after the first reply's baselines and reads as working."""
        conn, sk = self.conn, self.SK
        baseline = {"received": "21-40", "stream": "21-40", "item": ""}
        tomato = openclaw_pipe._RunPump()
        second = mock.Mock(chat_id=self.CHAT, message_id="w2", run_id=self.R2)
        second.renderer = mock.Mock()
        second.feed, second.working = mock.AsyncMock(), mock.AsyncMock()
        tomato.writer = second
        delta = self._delta(self.R1, " tomato")["payload"]
        tomato.queue.put_nowait(("steered_in", {"run": self.R1, "run_text": baseline}))
        tomato.queue.put_nowait(("event", delta))
        tomato.queue.put_nowait(("retired", None))
        await openclaw_pipe._turn_pump_loop(conn, sk, self.R2, ("u1", self.CHAT), tomato)
        second.renderer.seed.assert_called_once_with(baseline)
        second.working.assert_awaited_once()
        second.feed.assert_awaited_once_with(delta)

    async def test_a_closed_turn_writer_does_not_read_as_working(self):
        """A message taken in after Stop finalized M leaves M finished (#300)."""
        emit = mock.AsyncMock()
        writer = types.SimpleNamespace(opened=True, closed=True, _emitter=mock.AsyncMock(return_value=emit),
                                       conn=self.conn, chat_id=self.CHAT, message_id="w2")
        saved = {"statusHistory": [{"description": "Sent while the agent works", "done": False}]}
        chats = types.SimpleNamespace(get_message_by_id_and_message_id=mock.AsyncMock(return_value=saved))
        models = types.ModuleType("open_webui.models.chats")
        models.Chats = chats
        with mock.patch.dict(sys.modules, {"open_webui.models.chats": models}), \
                mock.patch.object(openclaw_pipe, "_owui_upsert_message", mock.AsyncMock()) as upsert:
            await openclaw_pipe._TurnWriter.working(writer)
            emit.assert_not_awaited()
            writer.closed = False
            await openclaw_pipe._TurnWriter.working(writer)
        line = {"description": "Thinking...", "done": False}
        emit.assert_awaited_once_with({"type": "status", "data": line})
        # A loading tab reads it from the message (#300).
        upsert.assert_awaited_once_with(self.CHAT, "w2", {"statusHistory": [*saved["statusHistory"], line]})

    async def test_a_turn_writer_steered_into_a_run_does_not_wait_for_its_images_as_its_own(self):
        """A steered turn writer does not wait for the host run's images as its own (#300)."""
        writer = types.SimpleNamespace(steered_into=self.R1, conn=None)
        self.assertEqual(await openclaw_pipe._TurnWriter._stored_media(writer, {"messages": []}), ([], []))

    async def test_a_turn_writer_steered_into_a_run_ends_from_its_own_span_even_when_empty(self):
        """Two messages taken in at one boundary: A's writer streamed B's answer, but its own empty
        span decides, and A says where its message went (#300)."""
        renderer = openclaw_pipe._TurnRenderer()
        renderer.feed(self._delta(self.R1, "BROWSER_TWICE_B")["payload"])
        writer = types.SimpleNamespace(steered_into=self.R1, runs=[self.R2], run_id=self.R2,
                                       run_started={}, renderer=renderer, message_id="w2")
        with mock.patch.object(openclaw_pipe, "_history_span_messages", return_value=[]):
            rebuilt = openclaw_pipe._TurnWriter._from_transcript(writer, {"messages": []})
        self.assertIsNotNone(rebuilt)
        self.assertEqual((rebuilt.text, rebuilt.output), ("", []))
        with mock.patch.object(openclaw_pipe, "_history_span_messages", return_value=None):
            self.assertIsNone(openclaw_pipe._TurnWriter._from_transcript(writer, {"messages": []}),
                              "while the run it went into has not closed, what streamed stays")

    async def test_a_run_whose_steered_reply_a_turn_writer_has_is_let_go_at_its_end(self):
        """With no later steer, the run's end lets it go and the session reads idle."""
        self.conn.gateway_version = "2026.10.10"
        # A session key the turn writer routes.
        conn = self.conn
        conn._valves = lambda: types.SimpleNamespace(AGENT_ID="main")
        self.SK = sk = f"agent:main:openwebui-00000000-0000-4000-8000-0000000000aa-{self.CHAT}"
        self.assertIsNotNone(conn.parse_owui_session_key(sk))
        host = openclaw_pipe._RunPump()
        host.started = True
        conn._turn_pumps[(sk, self.R1)] = host
        tomato = openclaw_pipe._RunPump()
        tomato.handover = {"message_id": "w2", "steer_pending": True,
                           "renderer": types.SimpleNamespace(received="", output=None)}
        conn._turn_pumps[(sk, self.R2)] = tomato
        conn._on_session_message(self._commit(self.R2, "say tomato", into=self.R1)["payload"])
        self.assertTrue(conn.has_reply(sk), "the run still works")
        await conn._dispatch_run_event(self._delta(self.R1, "tomato"))
        await conn._dispatch_run_event(self._lifecycle(self.R1, "end"))
        self.assertNotIn(f"{sk}:{self.R1}", conn._consumers)
        self.assertFalse(conn.has_reply(sk))
        # Events reaching tomato's writer must not make its pump look like an endless run.
        self.assertFalse(conn.user_turn_run_active(sk, self.CHAT), "no run works")
        # A run drained from the follow-up queue pushes no idle or `phase: start` (2026.10.10); the
        # next run's first event ends both writers, once.
        while not tomato.queue.empty():
            tomato.queue.get_nowait()
        with mock.patch.object(openclaw_pipe, "_turn_pump_loop", mock.AsyncMock()) as loop:
            # The run's `chat` final follows its lifecycle end, as can tomato's (#290).
            await conn._dispatch_run_event(self._final(self.R1, "21-40 tomato"))
            await conn._dispatch_run_event(self._final(self.R2))
            await self._spin()
        loop.assert_not_called()
        queued = [host.queue.get_nowait() for _ in range(host.queue.qsize())] + [
            tomato.queue.get_nowait() for _ in range(tomato.queue.qsize())]
        self.assertNotIn("settled", [kind for kind, _item in queued], "no run started")
        tomato.writer = types.SimpleNamespace(newer_run=False)
        q3 = conn.register_consumer(sk, self.R3)
        await conn._dispatch_run_event(self._lifecycle(self.R3, "model"))
        self.assertEqual(host.queue.get_nowait(), ("settled", None))
        self.assertEqual(tomato.queue.get_nowait(), ("settled", None))
        self.assertEqual(q3.get_nowait()["payload"]["runId"], self.R3, "the next run's reply gets it")
        # Their end must not mark the chat inactive while that run works (#300).
        self.assertTrue(tomato.writer.newer_run)
        await conn._dispatch_run_event(self._delta(self.R3, "blue"))
        self.assertTrue(host.queue.empty() and tomato.queue.empty(), "once")

    async def test_a_run_whose_steered_reply_a_turn_writer_has_that_fails_waits_for_its_row(self):
        """The next run's first event ends only a clean run's writers."""
        self.conn.gateway_version = "2026.10.10"
        conn = self.conn
        conn._valves = lambda: types.SimpleNamespace(AGENT_ID="main")
        self.SK = sk = f"agent:main:openwebui-00000000-0000-4000-8000-0000000000aa-{self.CHAT}"
        host = openclaw_pipe._RunPump()
        host.started = True
        conn._turn_pumps[(sk, self.R1)] = host
        tomato = openclaw_pipe._RunPump()
        tomato.handover = {"message_id": "w2", "steer_pending": True,
                           "renderer": types.SimpleNamespace(received="", output=None)}
        conn._turn_pumps[(sk, self.R2)] = tomato
        conn._on_session_message(self._commit(self.R2, "say tomato", into=self.R1)["payload"])
        await conn._dispatch_run_event(self._lifecycle(self.R1, "error"))
        self.assertNotIn(f"{sk}:{self.R1}", conn._consumers, "its end lets the run go")
        while not tomato.queue.empty():
            tomato.queue.get_nowait()
        conn.register_consumer(sk, self.R3)
        await conn._dispatch_run_event(self._lifecycle(self.R3, "model"))
        self.assertTrue(host.queue.empty() and tomato.queue.empty())

    async def test_a_queued_message_committed_while_a_run_works_is_not_steered_into_it(self):
        """A queued message (no `steerTargetRunId`) committed when its own run starts is not
        steered into the earlier run."""
        self.conn.gateway_version = "2026.10.10"
        conn, sk = self.conn, self.SK
        q1 = conn.register_consumer(sk, self.R1)
        await conn._dispatch_run_event(self._delta(self.R1, "tomato answer"))
        q1.get_nowait()
        q2 = conn.register_consumer(sk, self.R2)
        conn._on_session_message(self._commit(self.R2, "say blue")["payload"])
        self.assertTrue(q2.empty(), "blue waits for its own run")
        self.assertTrue(q1.empty(), "the run's reply goes on")
        conn._on_session_message(self._commit(self.R2, "say blue", into=self.R1)["payload"])
        self.assertEqual(q2.get_nowait()["_steered"]["run"], self.R1, "a steered one takes it")

    async def test_a_message_queued_behind_such_a_run_is_not_steered_into_it_after_its_end(self):
        """After such a steer the writer's pump must still see the run end, so a later queued
        message is not steered into an ended run."""
        self.conn.gateway_version = "2026.10.10"
        conn = self.conn
        # A session key the turn writer routes.
        conn._valves = lambda: types.SimpleNamespace(AGENT_ID="main")
        self.SK = sk = f"agent:main:openwebui-00000000-0000-4000-8000-0000000000aa-{self.CHAT}"
        host = openclaw_pipe._RunPump()
        host.started = True
        conn._turn_pumps[(sk, self.R1)] = host
        tomato = openclaw_pipe._RunPump()
        tomato.handover = {"message_id": "w2", "steer_pending": True,
                           "renderer": types.SimpleNamespace(received="", output=None)}
        conn._turn_pumps[(sk, self.R2)] = tomato
        conn._on_session_message(self._commit(self.R2, "say tomato", into=self.R1)["payload"])
        await conn._dispatch_run_event(self._lifecycle(self.R1, "end"))
        q3 = conn.register_consumer(sk, self.R3)
        conn._on_session_message(self._commit(self.R3, "say blue")["payload"])
        self.assertTrue(q3.empty(), "blue waits for its own run")
        self.assertTrue(host.terminal.is_set(), "its writer's run has ended")

    async def test_a_handed_over_message_the_gateway_did_not_steer_takes_no_run(self):
        """Only the run the Gateway names (`steerTargetRunId`) is taken; a queued handed-over
        message takes none (#290)."""
        self.conn.gateway_version = "2026.10.10"
        conn, sk = self.conn, self.SK
        host = openclaw_pipe._RunPump()
        host.started = True
        conn._turn_pumps[(sk, self.R1)] = host
        waiting = openclaw_pipe._RunPump()
        waiting.handover = {"message_id": "w2", "steer_pending": True,
                            "renderer": types.SimpleNamespace(received="", output=None)}
        conn._turn_pumps[(sk, self.R2)] = waiting
        for into in (None, self.R3):
            conn._on_session_message(self._commit(self.R2, "say tomato", into=into)["payload"])
            self.assertNotIn(f"{sk}:{self.R1}", conn._consumers, f"steered into {into}: the active run is not taken")
        conn._on_session_message(self._commit(self.R2, "say tomato", into=self.R1)["payload"])
        self.assertIn(f"{sk}:{self.R1}", conn._consumers)

    async def test_a_message_whose_request_send_now_cancelled_while_it_waits_to_be_steered_still_takes_the_run(self):
        """A message whose request Send now cancelled while it waited to be steered still takes the
        run at its commit; parking must not drop its run target."""
        self.conn.gateway_version = "2026.10.10"
        conn = self.conn
        # A session key the turn writer routes.
        conn._valves = lambda: types.SimpleNamespace(AGENT_ID="main")
        self.SK = sk = f"agent:main:openwebui-00000000-0000-4000-8000-0000000000aa-{self.CHAT}"
        host = openclaw_pipe._RunPump()
        host.started = True
        conn._turn_pumps[(sk, self.R1)] = host
        conn.register_run_target(sk, self.R2, self.CHAT, "w2", steer_pending=True)
        conn.park_cancelled_turn(sk, self.R2, None, [], 5, send_stop=False,
                                 reply={"message_id": "w2", "renderer": openclaw_pipe._TurnRenderer()})
        with mock.patch.object(openclaw_pipe, "_turn_pump_loop", mock.AsyncMock()):
            self.assertIsNotNone(await conn.claim_cancelled_turn(sk))
            self.assertIn((sk, self.R2), conn._turn_pumps, "its reply went to a turn writer")
            conn._on_session_message(self._commit(self.R2, "add tomatoes", into=self.R1)["payload"])
            self.assertIn(f"{sk}:{self.R1}", conn._consumers, "its commit takes the run")
            await conn._dispatch_run_event(self._delta(self.R1, "the list"))
        self.assertTrue(host.queue.empty(), "the answer after the commit is not the first reply's")

    async def test_a_retired_connection_closes_when_such_a_run_ends(self):
        """A retired connection closes when such a run is let go (#290)."""
        self.conn.gateway_version = "2026.10.10"
        conn, sk = self.conn, self.SK
        host = openclaw_pipe._RunPump()
        host.started = True
        conn._turn_pumps[(sk, self.R1)] = host
        tomato = openclaw_pipe._RunPump()
        tomato.handover = {"message_id": "w2", "steer_pending": True,
                           "renderer": types.SimpleNamespace(received="", output=None)}
        conn._turn_pumps[(sk, self.R2)] = tomato
        conn._on_session_message(self._commit(self.R2, "say tomato", into=self.R1)["payload"])
        conn._retired = True
        with mock.patch.object(conn, "disconnect", mock.AsyncMock()) as disconnect:
            await conn._dispatch_run_event(self._lifecycle(self.R1, "end"))
            await self._spin()
        disconnect.assert_awaited()

    async def test_a_steer_into_a_run_whose_writer_is_not_resolved_yet_knows_the_earlier_reply(self):
        """A steer before the pump has its writer still gets the earlier reply's baselines once the
        pump has it (#278)."""
        self.conn.gateway_version = "2026.10.10"
        conn, sk = self.conn, self.SK
        pump = openclaw_pipe._RunPump()
        pump.started = True
        conn._turn_pumps[(sk, self.R1)] = pump
        q2 = conn.register_consumer(sk, self.R2)
        conn._on_session_message(self._commit(self.R2, into=self.R1)["payload"])
        steered = q2.get_nowait()["_steered"]
        writer = mock.Mock(chat_id=self.CHAT, message_id="w1")
        writer.steered_away = lambda: {"received": "ROUND 1", "stream": "ROUND 1", "item": ""}
        writer.finalize = mock.AsyncMock()
        pump.writer = writer
        await openclaw_pipe._turn_pump_loop(conn, sk, self.R1, ("u1", self.CHAT), pump)
        self.assertEqual((await steered["run_text"])["earlier"],
                         {"chat_id": self.CHAT, "message_id": "w1", "request_task_id": None,
                          "span_run": self.R1})

    async def test_a_message_at_another_thinking_level_waits_for_its_own_run(self):
        """Another model or thinking level is not steered; it waits for its run (#278)."""
        self.conn.gateway_version = "2026.10.10"
        gen_a, first_a = await self._running_first_reply()
        gen_b = self._reply("Think harder about it", "m2", thinking="high")
        first_b = asyncio.ensure_future(gen_b.__anext__())
        await self._spin()
        self.assertNotIn("Think harder about it", self.sent, "not sent into the running run")
        self.assertTrue(any("Queued: waiting" in str(e) for e in self.emitted))
        first_b.cancel()
        await self._dispatch(self._lifecycle(self.R1, "end"))
        self.active = False
        await self._dispatch(self._final(self.R1, "ROUND 1"))
        await self._collect(first_a, gen_a)
        await gen_b.aclose()

    async def test_a_gateway_without_claude_steering_still_queues(self):
        self.conn.gateway_version = "2026.10.5"
        gen_a, first_a = await self._running_first_reply()
        gen_b = self._reply("Stop and say banana", "m2")
        first_b = asyncio.ensure_future(gen_b.__anext__())
        await self._spin()
        self.assertNotIn("Stop and say banana", self.sent)
        self.assertTrue(any("Queued: waiting" in str(e) for e in self.emitted))
        first_b.cancel()
        first_a.cancel()
        await self._spin()

    async def test_a_message_with_an_image_still_queues(self):
        self.conn.gateway_version = "2026.10.10"
        gen_a, first_a = await self._running_first_reply()
        gen_b = self._reply("what is this", "m2", image="iVBORw0KGgo=")
        first_b = asyncio.ensure_future(gen_b.__anext__())
        await self._spin()
        self.assertNotIn("what is this", self.sent)
        self.assertTrue(any("Queued: waiting" in str(e) for e in self.emitted))
        first_b.cancel()
        first_a.cancel()
        await self._spin()

    def test_a_steered_message_waiting_in_a_turn_writer_is_not_a_run(self):
        """A message waiting to be steered in a writer is not a run."""
        conn = self.conn
        for run, started in ((self.R1, True), (self.R2, False)):
            pump = openclaw_pipe._RunPump()
            pump.started = started
            conn._turn_pumps[(self.SK, run)] = pump
        self.assertTrue(conn.user_turn_run_active(self.SK, self.CHAT))
        conn._turn_pumps[(self.SK, self.R2)].started = True
        self.assertFalse(conn.user_turn_run_active(self.SK, self.CHAT), "two runs: none to steer into")

    async def test_a_message_sent_during_a_wake_waits_for_it(self):
        """A cron wake's run is not steered into (the Gateway aborts the send,
        `reply-owner-ended`); the message waits and goes as its own run."""
        self.conn.gateway_version = "2026.10.10"
        self.runtime = "claude-cli"
        self.active = True
        pump = openclaw_pipe._RunPump()
        pump.started = pump.wake = True
        self.conn._turn_pumps[(self.SK, "6f1c2a7e-5b9d-4c3a-9e2f-0a1b2c3d4e5f")] = pump
        gen = self._reply("Stop and say banana", "m2")
        first = asyncio.ensure_future(gen.__anext__())
        await self._spin()
        self.assertNotIn("Stop and say banana", self.sent, "not sent into the wake")
        self.assertTrue(any("Queued: waiting" in str(e) for e in self.emitted))
        first.cancel()
        await self._spin()

    async def test_a_message_sent_during_a_run_this_worker_does_not_see_waits(self):
        """Another worker's run, or one outside the Pipe, is not one to steer into."""
        self.conn.gateway_version = "2026.10.10"
        self.active = True
        gen = self._reply("Stop and say banana", "m2")
        first = asyncio.ensure_future(gen.__anext__())
        await self._spin()
        self.assertNotIn("Stop and say banana", self.sent)
        first.cancel()
        await self._spin()

    def _ref(self, run_id):
        """A reply's settle ref, from its Pipe run id."""
        message = openclaw_pipe.parse_pipe_run_id(run_id)["message"]
        return {"chat_id": self.CHAT, "message_id": message, "request_task_id": None, "span_run": run_id}

    async def test_a_steer_committed_after_its_run_ended_is_logged_and_settled(self):
        """CI 42538220842: 'say blue' was steered in too and its commit came first, then the run
        ended; tomato's steer target came after. 2026.10.10 publishes it with no order to the
        rest (`confirmSteerTargetRunIdForPersistence` runs once the runtime took the message
        in). With no running run to steer into, the steer is logged and the reply joins the run's
        settle from the transcript, never dropped."""
        self.conn.gateway_version = "2026.10.10"
        conn, sk = self.conn, self.SK
        tomato = openclaw_pipe._RunPump()
        tomato.handover = {"message_id": "w2", "steer_pending": True,
                           "renderer": types.SimpleNamespace(received="", output=None)}
        conn._turn_pumps[(sk, self.R2)] = tomato
        logged, settled = [], []

        async def settle(_conn, key, refs):
            settled.append((key, refs))
        with mock.patch.object(openclaw_pipe, "pipe_log", lambda *a, **k: logged.append(a[0])), \
                mock.patch.object(openclaw_pipe._TurnHooks, "settle_steered_replies",
                                  staticmethod(settle), create=True):
            conn._on_session_message(self._commit(self.R2, "say tomato", into=self.R1)["payload"])
            await self._spin()
        self.assertTrue(any("not the session's running run" in line for line in logged), logged)
        self.assertEqual(settled, [(sk, [self._ref(self.R1), self._ref(self.R2)])])

    async def test_a_steer_confirmed_after_its_replys_lone_final_is_logged_and_settled(self):
        """The steered reply's lone final came first: with no running run its steer did not
        apply there, and the reply ended. The target that comes after is still a late steer,
        logged and settled, never dropped (#379 review)."""
        self.conn.gateway_version = "2026.10.10"
        conn, sk = self.conn, self.SK
        conn.register_consumer(sk, self.R2)
        await conn._dispatch_run_event(self._final(self.R2))
        logged, settled = [], []

        async def settle(_conn, key, refs):
            settled.append((key, refs))
        with mock.patch.object(openclaw_pipe, "pipe_log", lambda *a, **k: logged.append(a[0])), \
                mock.patch.object(openclaw_pipe._TurnHooks, "settle_steered_replies",
                                  staticmethod(settle), create=True):
            conn._on_session_message(self._commit(self.R2, "say tomato", into=self.R1)["payload"])
            await self._spin()
        self.assertTrue(any("not the session's running run" in line for line in logged), logged)
        self.assertEqual(settled, [(sk, [self._ref(self.R1), self._ref(self.R2)])])

    WHOLE = "E2E_LATE_PART_1\n\nE2E_LATE_PART_2\n\nE2E_CLI_STEERED: tomato\n\nE2E_CLI_REPLY"

    def _late_steer_transcript(self, *, closed=True, tool_call=None, after=()):
        """CI 42538220842's run: the first reply's two parts, tomato steered in, then blue."""
        user, said = SteeredTranscriptSpanTests._user, SteeredTranscriptSpanTests._said
        result = {"role": "toolResult", "content": "done"}
        first = said("E2E_LATE_PART_1", stop="toolUse")
        if tool_call:
            first["content"].append({"type": "toolcall", "id": tool_call, "name": "exec",
                                     "arguments": {"command": "echo step"}})
        messages = [user(self.R1, "E2E_CLI_LATE_STEER x"), first, result,
                    said("E2E_LATE_PART_2", stop="toolUse"), result,
                    user(self.R2, "say tomato"), said("E2E_CLI_STEERED: tomato", stop="end_turn"),
                    user(self.R3, "say blue"), said("E2E_CLI_REPLY", stop="end_turn")]
        if closed:
            messages.append(said(self.WHOLE, run=self.R1, key=f"cli-assistant:{self.R1}"))
        return {"messages": [*messages, *after]}

    async def _settle(self, refs, saved, history, *, hold=None, tasks=None):
        """Run `_settle_steered_replies` on `refs`. `saved`: message id -> the saved message, or a
        callable giving it. `hold`: an Event the settle must wait for before it reads the replies
        (nothing is written until it is set). `tasks`: Open WebUI's task ids by chat and its
        tasks. Returns (upserts, log lines)."""
        conn, sk = self.conn, self.SK
        conn.session_history = mock.AsyncMock(side_effect=lambda *a, **k: history() if callable(history) else history)

        class Chats:
            @staticmethod
            async def get_message_by_id_and_message_id(chat_id, message_id):
                got = saved[message_id]
                return got() if callable(got) else got

        async def emitter(*_a, **_k):
            return mock.AsyncMock()
        modules = owui_modules(get_event_emitter=emitter)
        modules["open_webui.models"] = types.ModuleType("open_webui.models")
        modules["open_webui.models.chats"] = types.ModuleType("open_webui.models.chats")
        modules["open_webui.models.chats"].Chats = Chats
        by_chat, running = tasks or ({}, {})
        owui_tasks = types.ModuleType("open_webui.tasks")
        owui_tasks.tasks = running

        async def list_task_ids_by_item_id(_redis, chat_id):
            return list(by_chat.get(chat_id, []))
        owui_tasks.list_task_ids_by_item_id = list_task_ids_by_item_id
        modules["open_webui.tasks"] = owui_tasks
        upserts, logged = {}, []

        async def upsert(chat_id, message_id, fields):
            upserts[message_id] = fields
            return True
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(openclaw_pipe, "_owui_upsert_message", upsert), \
                mock.patch.object(openclaw_pipe, "pipe_log", lambda *a, **k: logged.append(a[0])):
            settle = asyncio.ensure_future(openclaw_pipe._settle_steered_replies(conn, sk, refs))
            await self._spin()
            if hold is not None:
                self.assertEqual(upserts, {}, "nothing written before it may read")
                hold.set()
            await settle
        return upserts, logged

    def _refs(self):
        return [self._ref(r) for r in (self.R1, self.R2, self.R3)]

    @staticmethod
    def _msg(content, output=()):
        return {"content": content, "output": list(output)}

    async def test_a_late_steers_settle_waits_for_the_transcript_and_takes_the_whole_chain(self):
        """The late steer's settle (`_on_user_message_committed`) starts from [first, tomato]:
        it reads the replies once tomato's writer, still open, is done, which is when the
        transcript closed the run; blue, steered in after tomato, joins from the transcript. The
        first reply is cut at tomato's commit."""
        first, tomato, blue = self._refs()
        writer = openclaw_pipe._TurnWriter.__new__(openclaw_pipe._TurnWriter)
        writer.closed, writer._done = False, asyncio.Event()
        self.conn._turn_writers[(self.CHAT, tomato["message_id"])] = writer
        # Until the session settled, which ends tomato's writer, the run's closing message is not in.
        history = lambda: self._late_steer_transcript(closed=writer._done.is_set())  # noqa: E731
        saved = {first["message_id"]: self._msg("E2E_LATE_PART_1\n\nE2E_LATE_PART_2\n\nE2E_CLI_STEERED: tomato"),
                 tomato["message_id"]: self._msg("E2E_CLI_STEERED: tomato"),
                 blue["message_id"]: self._msg("E2E_CLI_REPLY")}
        upserts, _ = await self._settle([first, tomato], saved, history, hold=writer._done)
        self.assertEqual(list(upserts), [first["message_id"]], upserts)
        self.assertEqual(openclaw_pipe._settle_text(upserts[first["message_id"]]["content"]),
                         "E2E_LATE_PART_1E2E_LATE_PART_2")

    async def test_a_settle_reads_a_reply_its_writer_is_still_finalizing(self):
        """A writer marked closed is still saving while it finalizes: the settle waits for it
        too (#379 review)."""
        first, tomato, blue = self._refs()
        writer = openclaw_pipe._TurnWriter.__new__(openclaw_pipe._TurnWriter)
        writer.closed, writer.finalizing, writer._done = True, True, asyncio.Event()
        self.conn._turn_writers[(self.CHAT, tomato["message_id"])] = writer
        saved = {first["message_id"]: self._msg(self.WHOLE),
                 tomato["message_id"]: lambda: self._msg("E2E_CLI_STEERED: tomato" if writer._done.is_set() else ""),
                 blue["message_id"]: self._msg("E2E_CLI_REPLY")}
        upserts, _ = await self._settle([first, tomato], saved, self._late_steer_transcript(), hold=writer._done)
        self.assertEqual(list(upserts), [first["message_id"]], upserts)

    async def test_a_settle_waits_for_the_chats_open_webui_requests(self):
        """A reply still in its Open WebUI request (no task id in its ref: found from the
        transcript or a late steer) saves at its end; the settle waits for the chat's requests
        so that save cannot overwrite the correction (#379 review)."""
        first, tomato, blue = self._refs()
        done = asyncio.Event()
        request = asyncio.ensure_future(done.wait())
        saved = {first["message_id"]: self._msg(self.WHOLE),
                 tomato["message_id"]: self._msg("E2E_CLI_STEERED: tomato"),
                 blue["message_id"]: self._msg("E2E_CLI_REPLY")}
        upserts, _ = await self._settle([first, tomato], saved, self._late_steer_transcript(), hold=done,
                                        tasks=({self.CHAT: ["t1"]}, {"t1": request}))
        self.assertEqual(list(upserts), [first["message_id"]], upserts)

    async def test_a_later_turn_is_not_pulled_into_the_steer_chain(self):
        """A turn committed after the run closed, its answer not stored yet, is no steer into
        the run: it does not stop the settle (#379 review)."""
        first, tomato, blue = self._refs()
        later = SteeredTranscriptSpanTests._user(self._run_id(4), "next question")
        saved = {first["message_id"]: self._msg(self.WHOLE),
                 tomato["message_id"]: self._msg("E2E_CLI_STEERED: tomato"),
                 blue["message_id"]: self._msg("E2E_CLI_REPLY")}
        upserts, logged = await self._settle([first, tomato], saved, self._late_steer_transcript(after=[later]))
        self.assertEqual(list(upserts), [first["message_id"]], (upserts, logged))

    async def test_a_settle_keeps_the_tool_cards_the_transcript_does_not_show(self):
        """A rewrite from the transcript never drops a tool card the reply shows: a transcript
        missing it leaves the replies as they are (#379 review). One it shows may move."""
        first, tomato, blue = self._refs()
        card = {"type": "function_call", "id": "toolu_1", "name": "exec", "arguments": "{}"}
        saved = {first["message_id"]: self._msg(self.WHOLE, [card]),
                 tomato["message_id"]: self._msg("E2E_CLI_STEERED: tomato"),
                 blue["message_id"]: self._msg("E2E_CLI_REPLY")}
        upserts, logged = await self._settle([first, tomato], saved, self._late_steer_transcript())
        self.assertEqual(upserts, {})
        self.assertTrue(any("tool card" in line for line in logged), logged)
        upserts, _ = await self._settle([first, tomato], saved, self._late_steer_transcript(tool_call="toolu_1"))
        self.assertEqual(list(upserts), [first["message_id"]])

    async def test_a_reply_showing_the_pipes_note_still_settles(self):
        """A steered reply that ended on its own lone final shows the Pipe's note that it went
        with the next message; the note is the Pipe's, not the run's text, so the settle gives the
        reply its answer (#379 review)."""
        first, tomato, blue = self._refs()
        saved = {first["message_id"]: self._msg(self.WHOLE),
                 tomato["message_id"]: self._msg(openclaw_pipe.REPLY_SENT_WITH_NEXT_NOTE),
                 blue["message_id"]: self._msg("E2E_CLI_REPLY")}
        upserts, logged = await self._settle([first, tomato], saved, self._late_steer_transcript())
        self.assertEqual(sorted(upserts), sorted([first["message_id"], tomato["message_id"]]), (upserts, logged))
        self.assertEqual(openclaw_pipe._settle_text(upserts[tomato["message_id"]]["content"]),
                         "E2E_CLI_STEERED:tomato")


class UnpublishedInputResendTests(_TurnCase):
    """ELI-359: OpenClaw 2026.10.5 commits a `chat.send` and then refuses to
    publish it when another write lands on the agent database in between
    (`Pending input authority changed before publication`, UNAVAILABLE, not
    retryable). The input never runs: the same key is then refused for good
    ("Pending input ownership ended; submit a new turn"), a new key runs once.
    So the Pipe sends it once more under a new key, on that error only."""

    SK = None

    def _refuse_sends(self, *errors):
        """The next `chat.send`s raise these, in order; later ones go through."""
        plain = self.conn.send_request
        left = list(errors)

        async def send_request(method, params, timeout=10):
            if method == "chat.send" and left:
                self.rpc.append(method)
                self.keys.append(params.get("idempotencyKey"))
                raise left.pop(0)
            return await plain(method, params, timeout=timeout)
        self.conn.send_request = send_request

    @staticmethod
    def _error(message, retryable=False):
        return GatewayError(f"Error: {message}", error={
            "code": "UNAVAILABLE", "message": f"Error: {message}", "retryable": retryable})

    async def _turn(self):
        gen = self._reply("hello", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        return gen, first

    async def test_an_unpublished_input_is_sent_once_more_under_a_new_key(self):
        self._refuse_sends(self._error(self.ocp.UNPUBLISHED_INPUT_ERROR))
        gen, first = await self._turn()
        await self._until_registered("run-1")
        self.assertEqual(self.sent, ["hello"])
        self.assertEqual(self.rpc.count("chat.send"), 2)
        refused, resent = self.keys
        self.assertNotEqual(refused, resent)
        self.assertEqual(parse_pipe_run_id(resent), parse_pipe_run_id(refused),
                         "the new key still ties the run to this chat and reply")
        q = self._queue("run-1")
        q.put_nowait(self._delta("run-1", "hi there"))
        q.put_nowait(self._final("run-1"))
        out = await asyncio.wait_for(first, 2)
        async for chunk in gen:
            out += chunk if isinstance(chunk, str) else ""
        self.assertIn("hi there", out)
        self.assertNotIn("Error sending message", out)
        self.assertEqual(self.conn._consumers.get(f"{self.SK}:{refused}"), None,
                         "the refused key's consumer is gone")

    async def test_the_resend_takes_the_same_route_and_wait_budget(self):
        """Both sends go through the owner router (`_run_request`), and the
        resend's wait budget is what the first one left, not a fresh one."""
        self._refuse_sends(self._error(self.ocp.UNPUBLISHED_INPUT_ERROR))
        routed = []
        plain = self.ocp._run_request

        async def run_request(conn, method, params, **kw):
            routed.append((method, params.get("idempotencyKey"), kw.get("started")))
            return await plain(conn, method, params, **kw)
        with mock.patch.object(self.ocp, "_run_request", run_request):
            gen, first = await self._turn()
            await self._until_registered("run-1")
        sends = [r for r in routed if r[0] == "chat.send"]
        self.assertEqual([key for _m, key, _s in sends], self.keys)
        (_, _, started), (_, _, resent_started) = sends
        self.assertIsNotNone(started)
        self.assertEqual(resent_started, started)
        q = self._queue("run-1")
        q.put_nowait(self._final("run-1"))
        await asyncio.wait_for(first, 2)
        async for _chunk in gen:
            pass

    async def test_the_same_refusal_twice_is_shown_not_sent_a_third_time(self):
        self._refuse_sends(self._error(self.ocp.UNPUBLISHED_INPUT_ERROR),
                           self._error(self.ocp.UNPUBLISHED_INPUT_ERROR))
        gen, first = await self._turn()
        out = await asyncio.wait_for(first, 5)
        async for chunk in gen:
            out += chunk if isinstance(chunk, str) else ""
        self.assertIn("Error sending message", out)
        self.assertEqual(self.rpc.count("chat.send"), 2)
        self.assertEqual(self.sent, [])

    async def test_another_refusal_is_not_sent_again(self):
        self._refuse_sends(self._error("Pending input ownership ended; submit a new turn to continue"))
        gen, first = await self._turn()
        out = await asyncio.wait_for(first, 5)
        async for chunk in gen:
            out += chunk if isinstance(chunk, str) else ""
        self.assertIn("Error sending message", out)
        self.assertEqual(self.rpc.count("chat.send"), 1)
