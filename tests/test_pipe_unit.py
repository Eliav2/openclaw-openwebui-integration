#!/usr/bin/env python3
"""Focused unit tests for pipe stream recovery behavior."""

import unittest
import ast
import html
import os
import tempfile
import textwrap
import contextlib
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
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_helpers import FakeSio, RecordingEmitter, fake_owui, owui_modules  # noqa: E402
from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import websockets  # noqa: E402  (after the stubs)

import openclaw_pipe  # noqa: E402
import openclaw_status_action  # noqa: E402
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
    """ELI-74: a turn whose live stream was lost is read back whole from the
    session transcript (`chat.history`), never from the clipped
    `sessions.preview`."""

    KEY = "agent:main:openwebui-u-c"
    QUESTION = "what changed?"
    # Longer than both server-side preview caps.
    LONG = "".join(f"Sentence {i} of the final answer. " for i in range(200)).strip()

    def _history(self):
        # The live claude-cli layout (captured 2026-10-11): the pipe's own user
        # message, the imported copy behind the metadata envelope, the turn's
        # assistant messages (tool-only ones have no text), then the closing
        # message carrying the whole turn's text.
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
        # The default budget the request goes out with.
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
    """Run `conn`'s event loop over `frames` (Gateway frames, in order), then
    stop it the way a shutdown does, not as a tick timeout (which marks a gap)."""
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
    """ELI-92: tool activity of a run this connection did not start.

    Re-measured 2026-10-12 against the live gateway (gw_events_probe.py: an
    observer subscribed with `sessions.messages.subscribe` while the CLI ran a
    3-call claude-cli turn). The observer got 3 start + 3 result as
    `session.tool` events, with the same payload as an `agent` tool event, and
    no `agent` tool events. The connection that started the run gets them as
    `agent` events. (The 2026-07-30 measurement that found no `session.tool`
    family predates it.) So the reader folds `session.tool` into the `agent`
    path and keeps the first copy of each (call, phase)."""

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

    async def test_an_accepted_spawn_is_recorded_in_the_child_index(self):
        """The Status dialog lists a chat's children from this index (state.py),
        not from a read of every session."""
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
    """A run as claude-cli imports it into `chat.history` (shapes measured
    2026-10-12 on session 0abe9bde): imported assistant messages with text /
    toolcall / tool_result blocks, then the closing `cli-assistant:<runId>`
    message that repeats all of the run's text."""
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
    """A run whose events were lost is rebuilt from `chat.history` into the
    same `text` + `output` the live renderer would have produced."""

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
        """Restart window 2026-10-13 11:26Z (chat 89bb045d, run msg-89bb045d-...):
        a preamble, five Bash calls, the answer. The Gateway's closing message
        joins the two text blocks with a blank line; the live stream carried it
        as the second message item "\n\nUSER-DONE-9e127db9" (read from OWUI)."""
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
        """A run on the Gateway's own runtime (GPT, Gemini, DeepSeek in the
        picker) keeps OpenClaw's shape, not claude-cli's: `toolCall` blocks,
        and each result is a `toolResult` message of its own. Recorded from a
        DeepSeek session; the rebuild came back with no cards at all."""
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
        """A `chat.send` on the Gateway's own runtime, as the Pipe sends it
        (recorded on an openwebui- session, DeepSeek V4 Flash): no closing
        `cli-assistant:` message; each message the run wrote carries its run
        id in `__openclaw.runId`, the last one with `stopReason` "stop"."""
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


def _turn_history(stub_id="stub-1", *, run="msg-c-stub-1-1.0", closed=True,
                  text="check the logs", later=False):
    """A turn as the Gateway stores a `chat.send` on claude-cli (measured
    2026-10-12, session 1b877b13): the user message keyed `<runId>:user`,
    the copy the runtime imports behind its metadata envelope, the imported
    assistant messages, then the closing `cli-assistant:<runId>`."""
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
    """ELI-90: replies an Open WebUI restart orphaned (empty, done=False
    forever) are finished from the Gateway transcript, waiting for a run
    still going on its pushed settle, never on a clock."""

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
        """ELI-100: a turn writer took the stub over while the transcript was
        being read (its run's events arrived). The writer finishes it."""
        self.history, self.active = _turn_history(), False
        self._stub()["meta"] = {"openclaw_writer": "writer-live", "openclaw_run": self.RUN}
        _register_turn_writer("writer-live")
        self.assertEqual(await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB), "skip")
        self.assertIs(self._stub()["done"], False)
        self.assertEqual(self._stub()["content"], "")

    async def test_a_stub_no_request_has_claimed_is_left_alone(self):
        """ELI-156: a client writes the stub, then POSTs the completion. After
        an Open WebUI restart that POST loads the models, which starts this
        sweep before the request has a task. No task id on a stub written
        since this process started means its request may still be on its
        way: it is not an orphan."""
        self.history, self.active = {"messages": []}, False
        del self._stub()["meta"]
        self._stub()["timestamp"] = 1_000_100
        with mock.patch("openclaw_pipe._owui_process_started_at", return_value=1_000_000.5):
            self.assertEqual(await _reconcile_stub(self.conn, "main", self.CHAT, self.STUB), "skip")
        self.assertIs(self._stub()["done"], False)
        self.assertEqual(self._stub()["content"], "")

    async def test_a_stub_older_than_this_process_with_no_task_is_an_orphan(self):
        """ELI-156 review: Open WebUI died after the stub was written and
        before its request took it, so it has no task id. Written before
        this process started, no request here can fill it."""
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
        """ELI-156: Open WebUI stamps its task id on the stub while the
        transcript is being read. The stub is that request's now."""
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
    """`_relay_output_events`: the Responses events that take the tab's
    `message.output` from what it holds to the renderer's current items."""

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
    """The renderer keeps the run as OWUI 0.12 stores a reply: text-only
    `text` and Responses `output` items (ELI-92)."""

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

    # ELI-219 (live chats e70feacd, f6c33478 on 2026.10.5): a claude-cli
    # run starts a background Agent, answers NO_REPLY, and goes on once the
    # task reports. The live stream had the sentinel, then the answer after
    # the Gateway's message break, then the answer's own item event and the
    # run's closer (its two messages joined by one newline). The reply read
    # "NO_REPLY\n\n<answer>NO_REPLY\n<answer>".
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

    # ELI-219 again (live chats 9562eda8, 6497a129 on 2026.10.5, Pipe 788ce7c):
    # the same run with a short answer and no item events. The Gateway puts
    # its message break ahead of the answer's first delta, and the run's
    # closer is the catch-all assistant text with the CLI's messages joined by
    # one newline. The reply read "SUB-51NO_REPLY\nSUB-51".
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
    """ELI-164: from OpenClaw 2026.10 most tools are deferred, so the model
    calls them through a wrapper (`tool_call` on 10.1 to 10.4,
    `dispatch_action` from 10.10) and the Gateway reports the target as a
    second, nested tool call (`parentToolCallId` = the wrapper's id). Both
    became cards, the inner one inside the outer one's start and result. One
    model action is one card, named and shown as the tool it ran, the same
    whether it streamed or was rebuilt from the transcript."""

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
    """ELI-164: claude-cli calls OpenClaw's tools over MCP. The Gateway's live
    tool events name them bare (`exec`, `stripOpenClawMcpToolPrefix`), the
    imported claude transcript keeps `mcp__openclaw__exec`, so a rebuilt card
    was titled differently from the one that streamed. From OpenClaw 2026.10
    every claude-cli shell command is such a call (`exec`, not `Bash`)."""

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
    """ELI-155: a sessions_spawn card was titled "sessions_spawn" (grouped as
    "Explored 2 sessions_spawn"), showed the whole task as input and the
    spawn receipt's JSON, UUIDs included, as output. It is now named after
    the sub-agent, shows the head of its task and one line of outcome, the
    same whether it streamed or was rebuilt from the transcript."""

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
        """claude-cli's transcript `tool_result` carries no tool name: the
        result still knows it answered a spawn."""
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
    """The clamp note is per-message information about a standing setting.

    The level is chosen in a filter dropdown and stays chosen, so a model that
    cannot honour it produces the same sentence on every single message. That
    is what the user actually saw: an italic line above every answer, with no
    way to dismiss it.
    """

    NOTE = "This model does not support thinking level 'high', using 'off' instead."

    def test_the_same_note_is_shown_once_per_session(self):
        conn = _GatewayConnection(lambda: None)
        self.assertTrue(conn.should_announce_thinking_note("s", self.NOTE))
        for _ in range(3):
            self.assertFalse(conn.should_announce_thinking_note("s", self.NOTE))

    def test_a_changed_note_speaks_up_again(self):
        # A different level, a different model, or a ladder just learned from
        # a rejection all change the sentence -- and all are news.
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
        # The note is still logged unconditionally: suppressing it in the chat
        # must not make a clamp invisible when diagnosing one from the logs.
        source = inspect.getsource(openclaw_pipe._PipeTurn)
        self.assertIn("should_announce_thinking_note(self.session_key, thinking_note)", source)
        self.assertIn('pipe_log(f"thinking: {thinking_note}")', source)


class OwuiTaskNoopTests(unittest.TestCase):
    """O10 (follow-up to O8): image_prompt_generation and function_calling
    join the background-task no-op set; moa_response_generation must not."""

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
    """ELI-184: Open WebUI's Continue Response sends the chat with the agent's own
    reply last. OpenClaw cannot continue a finished reply, so the Pipe sends
    nothing to the agent, adds nothing to the reply, and says so in a toast."""

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
        """A mark for one run_id must not blind the guard for a different
        run_id on the same session, or the same run_id on a different
        session -- only the exact (session, run) pair that was actually
        shown live is exempt from proactive delivery."""
        conn = _GatewayConnection(lambda: None)
        conn.mark_delivered_live("session-a", "run-1")
        self.assertFalse(conn.was_delivered_live("session-a", "run-2"))
        self.assertFalse(conn.was_delivered_live("session-b", "run-1"))

class StripInputMarkerLineTests(unittest.TestCase):
    """A needs-input prompt must never leak its raw "OpenClaw needs input:"
    directive line as visible chat text (fallback paths where no modal fired)."""

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
    """OWUI 0.12's Send now / interrupt cancels the running response and sends
    the new message ~0.5-0.8s later (measured 2026-10-11). The cancelled turn
    is parked for the grace window: its run keeps going with its events
    buffered, and messages it had not sent yet are carried, so the follow-up
    message takes all of it over. A plain Stop still aborts after the window."""

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
        # A plain Stop: nothing follows, so the run must still be stopped and
        # its buffering queue released.
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
        # Cancelled while still queued behind a run: nothing to abort, but the
        # message must reach the next reply.
        conn, calls = self._conn()
        conn.park_cancelled_turn("s", None, None, ["hello"], 0.05, send_stop=True)
        self.assertEqual((await conn.claim_cancelled_turn("s"))["carried"], ["hello"])
        await asyncio.sleep(0.1)
        self.assertEqual(calls, [])

    async def test_parked_run_is_not_backfilled_or_redelivered(self):
        # The stopped message keeps what it showed: no parity finalize into
        # it, and no proactive bubble for the run's tail.
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
        # OWUI sent the follow-up 3.4s after the cancel once (2026-10-11);
        # with the page's answer no timer races it.
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
        # Stop, then OWUI auto-sends the next queued message: that message
        # must start fresh, not continue the run the user stopped.
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
        """No page to ask (API use): the fallback window decides, and the
        next message claims without waiting."""
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
        """Park run-1 for a reply that shows message m1, with one event the
        run sent after the cancel still buffered."""
        q = conn.register_consumer("s", "run-1")
        q.put_nowait({"type": "event", "payload": {"stream": "tool", "data": {"phase": "result"}}})
        reply = {"message_id": "m1", "renderer": object()}
        conn.park_cancelled_turn("s", "run-1", q, list(carried), 30, send_stop=False, reply=reply)
        return reply

    async def test_send_now_keeps_the_run_in_its_own_message(self):
        """ELI-158: Send now kept the run for the NEXT reply, so the cancelled
        message showed its running tool card as done and the run went on in
        the new message. The run now stays in its own message: the turn
        writer takes that message over with what it showed and the events
        buffered since the cancel, and the next reply gets no run. The
        follow-up message hands it over, not the page's answer: Open WebUI's
        own cancel event comes after that answer can and would overwrite
        the writer's first events (#83 Screenshots)."""
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
        """No page answer (a non-English Send now click): a new message inside
        the window keeps the run, in its own message, as Send now does."""
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
        # The grace window is seconds; sessions.patch alone can take 10s+ with
        # its retry, so the claim must come before it. And the cancel handler
        # must park, never abort directly.
        src = inspect.getsource(openclaw_pipe._PipeTurn)
        claim = src.index("await self.conn.claim_cancelled_turn(self.session_key)")
        patch = src.index("RUN_PATCH")
        self.assertLess(claim, patch)
        self.assertIn("conn.park_cancelled_turn(", src)
        self.assertNotIn("await self.conn.abort(", src)

    def test_grace_valve_defaults_on(self):
        self.assertGreater(Pipe.Valves().INTERRUPT_GRACE_SECONDS, 0)


class NoDroppedMessagesTests(unittest.IsolatedAsyncioTestCase):
    """Drive real reply generators through OWUI's ways of ending a response.

    Incident 2026-10-11 (chat 0abe9bde): "count to 1500", Send now "test" at
    184, Send now "another test" at 720. Lines 185-244 vanished in the gap
    between the cancel and the next reply registering; the second interrupt
    arrived as a generator close, which skipped the cancel handler, so "test"
    was never sent and the run's tail went to the DB but not the screen.
    """

    SK = None

    async def asyncSetUp(self):
        import openclaw_pipe as ocp
        self.ocp = ocp
        self.conn = _GatewayConnection(lambda: None)
        self.conn._ws = object()
        self.sent = []
        self.rpc = []
        self.aborts = []
        self.active = False
        self.runtime = None
        self.keys = []
        self.history = {}
        n = {"run": 0}

        async def send_request(method, params, timeout=10):
            self.rpc.append(method)
            if method == "chat.send":
                n["run"] += 1
                self.sent.append(params.get("message"))
                self.keys.append(params.get("idempotencyKey"))
                return {"runId": f"run-{n['run']}"}
            if method == "chat.history":
                return copy.deepcopy(self.history)
            if method == "chat.abort":
                self.aborts.append(params)
                return {"aborted": True}
            if method == "sessions.describe":
                return {"session": {"status": "running" if self.active else "done",
                                    "agentRuntime": {"id": self.runtime}}}
            if method == "sessions.list":
                return {"sessions": [{"key": self.SK, "hasActiveRun": self.active}]}
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

    def _reply(self, text, msg_id, intent=None):
        self.emitted = getattr(self, "emitted", [])

        async def emitter(event):
            self.emitted.append(event)

        async def event_call(event):
            # The page's answer to `_intent_query_js`.
            return intent

        body = {"model": "openclaw_gateway.default",
                "messages": [{"role": "user", "content": text}]}
        meta = {"chat_id": "chat-1", "user_id": "u1", "message_id": msg_id}
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

    def _hold_parked_timers(self):
        """The fallback window of a parked run (`INTERRUPT_GRACE_SECONDS`) is
        a wall-clock timer. A test of the next reply claiming the run drives
        that claim itself, so the window must not close on its own: a single
        debug-mode loop step of this harness takes 0.3 s idle and seconds on
        a loaded host (ELI-154). The timer is recorded here and never runs;
        returns the windows armed."""
        armed = []
        patch = mock.patch.object(self.conn, "_arm_parked_timer",
                                  lambda session_key, entry, wait_s: armed.append(wait_s))
        patch.start()
        self.addCleanup(patch.stop)
        return armed

    async def _read(self, gen, want):
        out = ""
        while want not in out:
            # Bounds a hang only: one loop step of this harness can take
            # seconds on a loaded host (ELI-154).
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
        """ELI-88 (2026-10-12, chat 0abe9bde): a message sent while a
        73-minute claude-cli run was going sat in the pipe's queue until the
        queue's 30-minute ceiling released it into the still-running run; the
        Gateway queued it there and claude-cli dropped it. It must wait until
        the Gateway pushes that the run settled, however long that is, re-read
        once per pushed change (no polling), then send once."""
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

    async def test_queued_message_survives_a_redeploy_retiring_its_connection(self):
        """ELI-172 (2026-10-17, chat 78624cb0): a message typed at 12:03:56Z
        queued behind a running run. A deploy at 12:14:58Z retired the
        connection the message waited on; it kept serving the in-flight run
        and closed when that run ended (12:30:44Z), the very settle the
        message waited for. Nothing woke the waiter: no chat.send, an empty
        reply 42 minutes later. A closed socket gets no more pushes."""
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

    async def test_a_reply_that_saw_its_run_end_leaves_no_parity_target(self):
        """The inline loop ends on `lifecycle end` and drops its consumer; the
        run's trailing `chat` final arrives after that. Measured live
        2026-10-12: every normal turn then ran the parity finalize ("inline
        turn ended early") and rewrote the message OWUI was about to save."""
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
        """ELI-82 (2026-10-14 11:02Z, chat 56b98d09, message 5819edee): Open
        WebUI called the pipe twice for the same reply, same messages. The
        second call queued behind the first call's own run, then sent the
        user's text again as a new run once it settled. The agent answered
        twice ("I've already done this...") and the second answer, written
        last, replaced the real final in the same message. The second call
        must follow the first run and end with its reply."""
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
        """Most of the 31 second calls came after the first run had ended
        (up to 588 s later): the reply is rebuilt without waiting."""
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

    async def test_a_second_call_with_new_text_is_sent(self):
        """A second call for the same reply with different input is not a
        repeat (two such calls in the logs continue a reply after a tool)."""
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

    async def _reply_text(self, events):
        gen = self._reply("these /model are fine", "m1")
        first = asyncio.ensure_future(gen.__anext__())
        await self._until_registered("run-1")
        for ev in events:
            self._queue("run-1").put_nowait(ev)
        out = await asyncio.wait_for(first, 2)
        async for chunk in gen:
            out += chunk if isinstance(chunk, str) else ""
        return out

    async def test_a_run_refused_before_it_starts_shows_its_error(self):
        """ELI-139 (2026-10-15, chat 2af17834): a message with "/model are"
        in its text made the Gateway refuse the run as an inline model
        directive. Its only event was a chat `state: "error"` carrying the
        reason, and the reply said "(This turn produced no reply text.)"."""
        out = await self._reply_text([self._chat_error("run-1", self._NOT_ALLOWED)])
        self.assertIn(f"**Error:** {self._NOT_ALLOWED}", out)
        self.assertNotIn("no reply text", out)

    def _spawn_card_events(self, run_id):
        def tool(phase, **data):
            return {"type": "event", "event": "agent", "payload": {
                "sessionKey": self.SK, "runId": run_id, "stream": "tool",
                "data": {"phase": phase, "name": "sessions_spawn", "toolCallId": "t1", **data}}}
        return [tool("start", args={"task": "Answer what 17 times 3 is."}),
                tool("result", result="Started: QA 17x3")]

    async def test_a_run_that_answers_with_silence_after_its_card_keeps_the_card_alone(self):
        """ELI-227 (live chat 0a13a1d4 on 2026.10.5): the reply started a
        background sub-agent with sessions_spawn and answered NO_REPLY. The
        message read "(This turn produced no reply text.)" under the card."""
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
        self.assertEqual(out.count("**Error:**"), 1)
        self.assertIn("**Error:** provider overloaded", out)

    async def test_a_socket_gap_makes_the_waiter_read_again(self):
        """The settle can be among the events lost while the socket was down,
        so a gap wakes every waiter to re-read the state."""
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
        """The race the old 2-second settle window papered over: a run starts
        (a sub-agent's parent resuming) between the idle read and the send.
        The Gateway acks "started" but queues the message, which shows as a
        lone empty final. On claude-cli a queued turn fails when it drains
        ("no active tool authority snapshot"), so the pipe takes it back with
        chat.abort on that runId, waits for the settle, and sends it again."""
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
        """An API runtime takes a mid-run message into the live run (it has
        an injection hook), so a lone empty final there is not a lost message."""
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
        """2026-10-12, chat 98891cbb: the pipe's Gateway socket dropped for 1s
        mid-count. The closing catch-all (the WHOLE reply) no longer started
        with what had arrived, so all of it was appended again: 3816
        duplicates. Now only what follows the received text is new. This
        reply has no message to hand over (the session key names no Open
        WebUI chat), so the lost lines stay lost (ELI-147 slice 4)."""
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
        """The 98891cbb shape: the drop hit right after reply 1 registered
        (lines 1..3 lost), then two Send nows moved the run to replies 2 and
        3, where the catch-all arrived. With no message to hand over, the
        lost lines stay lost, and the catch-all repeats none of the others
        (a reply with a message hands over: GapHandoverReplayTests)."""
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
        # The run keeps emitting before the next reply registers: the gap.
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
        # The live failure after the first deploy: the follow-up came 3.4s
        # after the cancel and a 3s timer had already aborted the run.
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
        await first
        await r1.aclose()
        await asyncio.sleep(0.05)
        self.assertIn("abort", self.rpc)
        self.assertEqual(self.conn._consumers, {})

    async def _interrupted_during_settle(self):
        """A reply cut while its run streams, and the closing of that reply
        cancelled again while the page's Stop/Send-now answer is in flight.
        Returns the future the page answers through."""
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
        """ELI-95: the page's answer was awaited on the cancelled reply's own
        close path, so a second interruption there turned a Stop into
        "unknown", and the next message continued the run the user stopped."""
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
        # Live 2026-10-12: the reply continuing a run showed 2327..4000 and
        # then the whole 1..4000 again. The run's closing catch-all event
        # carries its ENTIRE text; the adopter must diff it against the
        # run's baseline, not against the empty one of a fresh reply.
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
        """ELI-199 (live 2026-10-18 15:56:42): a message queued behind a run
        the turn writer holds. The Gateway socket drops (1006); the drop
        wakes the queue's waiter, `sessions.describe` and `sessions.list`
        both fail "not ready", and this worker's registry, which never saw
        the held run, said idle: the message was sent into the dead socket
        and lost ("Error sending message: gateway connection not ready").
        While the socket is reconnecting the run state is unknown, so the
        message waits; the reconnect wakes it, and it is sent once the run
        really settled."""
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
        # It waits as a turn that needs the connection: the loop is asked to
        # try now, and backs off at its first step while the turn waits.
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
        """ELI-188 (QA C7): Send twice while a reply runs. The 2nd reply,
        cancelled while it waited, kept "Queued: waiting..." and no text
        forever; its message was answered in the 3rd reply. It must say so,
        once the 3rd reply really sent it, not before."""
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
        """ELI-188 variant: Send now, then Stop. The Pipe drops the queued
        message (the user stopped it), but its reply said "Queued" forever,
        even after a reload."""
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
        """Send now twice in a row while a run goes on: both waiting replies
        hand their message on, and both say where it went."""
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
        """Run a reply the way Open WebUI does: in a request task registered
        under the request's task id (`open_webui.tasks.tasks`), reading the
        stream. `question`: the card is cancelled. `between_chunks`: an
        awaitable factory Open WebUI's own code waits on after each chunk."""
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

        # Open WebUI's response object holds the stream: a Stop that lands in
        # its own code leaves the generator suspended, never closed.
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
        # Stopped here, before asyncTearDown stops the class's own patch of the
        # same name: stopped after it, this one would restore that mock for good.
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
        """ELI-200 (QA, live 2026-10-18 16:37): a question card is open, and a
        message is sent instead of pressing its Cancel. The card is cancelled,
        the question ends the reply as text, and the reply goes on to its end:
        its last status line waits for the usage rows (two Gateway calls).
        Open WebUI's Stop for the new message cancels the reply right there,
        so that line was never written: the reply was saved done with its
        status still "Thinking..." (done: false), shimmering forever, also
        after a reload. Its run was over, so nothing was parked either."""
        await self._stopped_as_it_ends()

    async def test_a_stop_landing_in_open_webuis_own_code_closes_it_too(self):
        """The same Stop landing while the reply waits at a yield, in Open
        WebUI's own handling of the last chunk: the reply's generator is
        never resumed or closed, so nothing in it runs again."""
        await self._stopped_as_it_ends(between_chunks=True)

    async def test_a_cancelled_reply_has_its_saved_status_checked(self):
        """The reply wrote its own last status, then Open WebUI's request was
        cancelled by a Stop: its save of the cancelled message runs while the
        status write may still be going, and the status write can be lost
        there. A cancelled request always has its saved status checked."""
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
        """Control: a reply that wrote its last status (usage lines, or an
        empty done one) before its request ended keeps it."""
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
        """`_close_reply_status` (ELI-200): after the request's end, the saved
        message still ends on an open status: a done one is added, saved
        (update_db) so a reload shows the line ended too."""
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
    """ELI-56: a concurrent message must queue behind a run that only LOOKS
    done. When a turn calls sessions_yield to await a subagent, describe.status
    flips to 'done' while the run is merely suspended; sessions.list's
    hasActiveRun stays true. The decision must treat that gap as active."""

    def test_running_status_is_active_regardless_of_list(self):
        self.assertTrue(_session_active_from_signals("running", False))
        self.assertTrue(_session_active_from_signals("streaming", None))
        self.assertTrue(_session_active_from_signals("queued", False))

    def test_yield_gap_done_status_but_list_active_is_active(self):
        # The exact ELI-56 signature: describe says 'done', list says a run
        # is live (isEmbeddedAgentRunActive true during the yield gap).
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
    """Parity (2026-07-19): the shadow renderer reconstructs a run's full
    visible content (text + tool blocks) from its gateway event stream, so an
    interrupted inline turn's OWUI message can be finalized to the complete
    content."""

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
        """A multi-line tool result must still be readable by OWUI's frontend.

        OWUI parses these attributes with a non-dotAll `/(\\w+)="(.*?)"/g`, so a
        literal newline anywhere in a value silently drops that attribute and
        the card renders INPUT-only (2026-07-25 regression report). Assert both
        that the opening tag is single-line and that an equivalent regex still
        recovers the full result.
        """
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
        """A provider's final catch-all `text` (full cumulative reply, no
        `delta`) must not re-append what was already streamed."""
        r = _TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "Hello world"}})
        r.feed({"stream": "assistant", "data": {"text": "Hello world"}})
        self.assertEqual(r.visible_text, "Hello world")

    def test_filters_sender_metadata(self):
        r = _TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "Sender (untrusted metadata) ..."}})
        self.assertEqual(r.visible_text, "")


class NativeToolItemTests(unittest.TestCase):
    """Tool calls streamed as Responses-API output items.

    Yielded markdown is append-only for the rest of the turn, so a card baked
    into the text can never stop spinning. These dicts are folded into OWUI's
    own output list instead, where the spinner→checkmark transition is OWUI's
    to make.
    """

    def test_start_event_is_a_pending_function_call(self):
        ev = _tool_call_started_event("Bash", "call-1", '{"command": "sleep 8"}')
        self.assertEqual(ev["type"], "response.output_item.added")
        item = ev["item"]
        self.assertEqual(item["type"], "function_call")
        self.assertEqual(item["id"], "call-1")
        self.assertEqual(item["name"], "Bash")
        self.assertEqual(item["arguments"], '{"command": "sleep 8"}')
        # in_progress + no matching output item is what renders as a spinner.
        self.assertEqual(item["status"], "in_progress")
        self.assertNotIn("output", item)

    def test_start_event_carries_no_call_id(self):
        """The regression that killed every tool card on OWUI 0.11.1+.

        `handle_responses_streaming_event`'s added-item branch dedups on id OR
        call_id and REPLACES on a hit, so a call_id shared with the result
        item made the result overwrite the call. The card is built from the
        `function_call` item alone, so overwriting it removed the card
        outright -- chats persisted `function_call_output` with no
        `function_call` and rendered nothing at all.
        """
        start = _tool_call_started_event("Bash", "call-1", "{}")
        result = _tool_call_result_event("call-1", "x")
        self.assertNotIn("call_id", start["item"])
        # The ids must differ so the dedup sees two items; the result's
        # `call_id` still equals the call's `id`, which is the pairing.
        self.assertNotEqual(start["item"]["id"], result["item"]["id"])

    def test_result_event_matches_the_start_event_by_call_id(self):
        """The shared call id is the entire flip mechanism -- if these diverge
        the card spins forever. The frontend reads `call_id ?? id` off the
        call item, so the result's `call_id` pairs with the call's `id`."""
        start = _tool_call_started_event("Bash", "call-1", "{}")
        result = _tool_call_result_event("call-1", "total 4\ndrwxr-xr-x")
        self.assertEqual(start["item"]["id"], result["item"]["call_id"])
        self.assertEqual(result["item"]["type"], "function_call_output")
        self.assertEqual(result["item"]["status"], "completed")
        self.assertEqual(
            result["item"]["output"], [{"type": "output_text", "text": "total 4\ndrwxr-xr-x"}]
        )

    def test_item_ids_are_distinct_between_call_and_output(self):
        """Both items land in one output list; colliding ids would make the
        backend treat them as the same item."""
        start = _tool_call_started_event("Bash", "call-1", "{}")
        result = _tool_call_result_event("call-1", "x")
        self.assertNotEqual(start["item"]["id"], result["item"]["id"])

    def test_payloads_are_json_serializable(self):
        """OWUI serializes a yielded dict with `json.dumps` -- a non-serializable
        value would break the SSE line rather than the card."""
        json.dumps(_tool_call_started_event("Bash", "c", '{"a": 1}'))
        json.dumps(_tool_call_result_event("c", "out"))

    def test_result_text_is_capped(self):
        ev = _tool_call_result_event("c", "x" * 20000)
        self.assertEqual(len(ev["item"]["output"][0]["text"]), 8000)

    def test_markdown_card_path_is_unchanged(self):
        """Valve off (and the shadow renderer, which has no live stream) must
        still produce the finished-only markdown card."""
        block = _render_tool_result_block("Bash", "t1", "{}", "out", "")
        self.assertIn('done="true"', block)
        self.assertIn('result="out"', block)

    def test_orphan_close_reuses_the_result_event(self):
        """A call announced but never resolved must still be completed, or its
        card spins forever. OWUI's own end-of-stream sweep does not reliably
        reach this path (observed 2026-07-25: a delivered call stayed
        `in_progress` and only rendered done because its result item existed)."""
        ev = _tool_call_result_event("call-1", "(no result -- the run ended first)")
        self.assertEqual(ev["item"]["type"], "function_call_output")
        self.assertEqual(ev["item"]["call_id"], "call-1")
        self.assertEqual(ev["item"]["status"], "completed")

    def test_no_valve_gates_native_items(self):
        """Native items are unconditional now -- a leftover toggle would be a
        second, untested code path."""
        valves = Pipe.Valves()
        self.assertFalse(hasattr(valves, "NATIVE_TOOL_ITEMS"))
        self.assertFalse(hasattr(valves, "SHOW_RUNNING_TOOL_CARDS"))


class ToolErrorIndicationTests(unittest.TestCase):
    """A failed tool call must LOOK failed.

    OWUI's card picks its status icon from `isDone` alone -- spinner, green
    check, or wrench, with no failure branch (`ToolCallDisplay.svelte:136`) --
    so the only outcome-carrying field we control is the name shown in the
    collapsed row, plus the Output text behind it.

    ELI-75: the previous shape here (`response.output_item.done`) looked
    correct by inspection but was dead code in OWUI's own event handler -- see
    `RealHandlerReplayTests` below, which replays these events through an
    extraction of that handler instead of asserting our belief about it.
    """

    def test_relabel_reannounces_the_call(self):
        """The start event again, under the same id: both of OWUI's reducers
        replace an added item whose id they already hold (ELI-124)."""
        ev = _tool_call_error_relabel_event("Bash", "call-1", '{"command": "ls"}')
        self.assertEqual(ev, _tool_call_started_event("Bash ❌", "call-1", '{"command": "ls"}'))
        self.assertEqual(ev["type"], "response.output_item.added")

    def test_relabel_marks_the_name(self):
        ev = _tool_call_error_relabel_event("Bash", "call-1", "{}")
        self.assertEqual(ev["item"]["name"], "Bash ❌")

    def test_relabel_is_json_serializable(self):
        json.dumps(_tool_call_error_relabel_event("Bash", "call-1", "{}"))

    def test_banner_precedes_the_original_output(self):
        """The banner explains the failure inside the card, so it must never
        cost the actual error text."""
        banner = _tool_error_banner("ls: cannot access '/nope'")
        self.assertTrue(banner.startswith("❌"))
        self.assertIn("ls: cannot access '/nope'", banner)


class RealHandlerReplayTests(unittest.TestCase):
    """Replay our emitted events through an extraction of OWUI's actual
    `handle_responses_streaming_event` (`test_fixture_owui_streaming_handler.py`)
    instead of asserting our belief about what it does.

    This is the regression guard for ELI-75: the old relabel event was
    logically well-formed by every unit test above and still a no-op against
    the real handler, because the handler treats `response.output_item.done`
    as dead code. A test that only inspects our own event dicts cannot catch
    that class of bug; only replaying through the real (extracted) handler
    can.
    """

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
        """The 0.11.1 regression, replayed end to end.

        `response.output_item.added` dedups on id OR call_id and replaces on a
        hit. When the call item carried `call_id`, the result item matched it
        and overwrote it, leaving a `function_call_output` alone in the output
        list -- and the card is built from the `function_call`, so nothing
        rendered. Both items must be present, and the result must pair to the
        call by the id the frontend reads (`call_id ?? id`).
        """
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
        """Matched by id, not position: text or a parallel call after the
        card does not move the mark onto them."""
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
        """`response.output_item.done` used to be unreachable on 0.11.0 (the
        broader `response.<field>.done` arm matched first and skipped it),
        which is why the relabel uses the generic `response.name.done` shape.
        0.11.1+ reordered the branches and it now lands, replacing the item at
        `output_index` wholesale -- default: the LAST item.

        Kept as a watch on that branch, not as an endorsement: the wholesale
        replace is strictly more dangerous than the in-place field mutation we
        use, because a wrong default index eats a text item instead of one
        field of it. If a future release also drops the positional default,
        the relabel could move here.
        """
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
    """Regression tests for the 2026-07-10 reconnect-storm incident (P36):
    `_reconnect()` used to call `_connect_and_start()`, which spawned a
    *second* `_event_loop` task on success -- but the original coroutine
    kept looping too, so both raced on `self._ws.recv()`. Each collision
    raised its own exception, which triggered another `_reconnect()`, which
    spawned yet another task: exponential task growth, a reconnect storm
    hammering the Gateway with connection attempts (observed as HTTP 503
    rejections), and elevated Gateway memory pressure."""

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

        # One disconnect -> exactly one reconnect (no duplicate tasks) ->
        # one more disconnect that stops the loop.
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

        # Calling ensure_connected again while already connected must not
        # spawn a second task.
        await conn.ensure_connected()
        self.assertIs(conn._event_loop_task, first_task)

        first_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_task

    async def test_send_request_raises_clean_error_while_reconnecting(self):
        """During the brief window where the event loop has dropped the
        socket and is reconnecting, `self._ws` is None. `send_request` must
        surface a clear GatewayError, not a bare AttributeError('NoneType'...)
        that reaches the user as a cryptic '**Error sending message:**'."""
        conn = _GatewayConnection(lambda: None)
        conn._ws = None
        with self.assertRaises(GatewayError):
            await conn.send_request("sessions.describe", {"key": "k"}, timeout=1)


class TickWatchTests(unittest.IsolatedAsyncioTestCase):
    """Review 2af1 R13: the Gateway sends a `tick` event every
    `policy.tickIntervalMs` of hello-ok (server-maintenance-Wf2jQ0wx.mjs:156,
    30 s), and its own client closes the socket after twice that with no frame
    (client-CKDmO6tp.mjs:1447-1459, "tick timeout"). A half-open socket sends
    nothing and never closes: the pipe's 90 s receive timeout only looped, so
    every run on it stayed stuck."""

    def conn(self):
        conn = _GatewayConnection(lambda: None)
        conn._max_backoff = 0
        conn._tick_interval_s = 0.05
        return conn

    async def test_a_silent_socket_is_closed_and_replaced_after_two_tick_intervals(self):
        conn = self.conn()
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
            reconnects.append(time.monotonic())
            conn._ws = Fresh()
        conn._connect_and_start = connect_and_start
        conn._ws = HalfOpen()
        gaps = []
        conn._mark_event_gap = lambda: gaps.append(True)
        t0 = time.monotonic()
        await asyncio.wait_for(conn._event_loop(), 5)
        self.assertEqual(len(reconnects), 1, "one new socket")
        self.assertGreaterEqual(reconnects[0] - t0, 0.1, "not before two intervals")
        self.assertEqual(aborted, [True], "the dead socket is torn down")
        self.assertTrue(gaps, "runs learn their events may be lost")

    async def test_ticks_keep_a_quiet_socket(self):
        conn = self.conn()
        n = {"recv": 0}

        class Ticking:
            async def recv(self):
                n["recv"] += 1
                if n["recv"] > 8:
                    conn._stopped = True
                    raise websockets.exceptions.ConnectionClosed("closed")
                await asyncio.sleep(0.03)
                return json.dumps({"type": "event", "event": "tick", "payload": {"ts": 1}})

        async def connect_and_start():
            raise AssertionError("a ticking socket was replaced")
        conn._connect_and_start = connect_and_start
        conn._ws = Ticking()
        await asyncio.wait_for(conn._event_loop(), 5)
        self.assertGreater(n["recv"], 8)

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
        # Taken as the response, any other frame read as a rejection with no
        # reason: "**Gateway connection error:** connect failed".
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
    """ELI-150: a plain redeploy mid-reply. The previous load's connection is
    retired but keeps serving the reply it streams; the new load's
    connection gets the same run's events with no consumer of its own. Its
    turn writer must not take the reply's message over while that reply
    lives, and does once the reply has ended (one writer per message)."""

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
    """Regression tests for the 2026-07-11 zombie-connection incident
    (P33/P36 sibling bug): OWUI's function loader execs every redeploy into a
    brand-new module with no teardown hook on the old one, so a
    module-level-only singleton reset every deploy and orphaned the previous
    deploy's WS/event-loop task forever (7 live connections observed after a
    string of same-evening redeploys). `_reap_stale_gateway_connection` /
    `_remember_gateway_connection` anchor the singleton on OWUI's own stable
    `open_webui.socket.main` module (never reloaded by our function) so the
    next deploy can tear down the previous one's connection before opening
    its own -- self-healing without a container restart."""

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
        # Connections built before `retire()` existed get pinned shut on the
        # instance, so an in-flight run of the old module can't reconnect it.
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
        # A model keyed by its serving runtime (claude-cli/claude-opus-5)
        # resolves to the model's canonical vendor (anthropic/claude-opus-5).
        # Same model id + runtime provider namespace → treated as applied.
        patch_resp = {"resolved": {"modelProvider": "anthropic", "model": "claude-opus-5"}}
        self.assertTrue(_model_patch_matches("claude-cli/claude-opus-5", patch_resp))

    def test_model_patch_runtime_namespace_rejects_different_model(self):
        # Runtime namespace does NOT excuse a genuinely different model id.
        patch_resp = {"resolved": {"modelProvider": "anthropic", "model": "claude-sonnet-5"}}
        self.assertFalse(_model_patch_matches("claude-cli/claude-opus-5", patch_resp))

    def test_model_patch_non_runtime_provider_still_strict(self):
        # A normal vendor-keyed override must still match provider + model
        # exactly; the relaxation is scoped to runtime namespaces only.
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
    """P39: OWUI sends `content` as a list of content blocks (not a plain
    string) whenever the user attaches an image. Before this, `pipe.py`
    sent that raw Python list straight through as the RPC's `message`
    field with no normalization at all."""

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
        """_coerce_text on a mixed text+image content list must yield only
        the text -- the image_url block has no "text"/"delta"/"content"/
        "message" key for _coerce_text to recurse into, so it resolves to
        an empty string and is dropped from the join, not raising and not
        leaking the raw data: URL into the text sent to the agent."""
        text = _coerce_text([
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xyz"}},
        ])
        self.assertEqual(text, "what is this?")
        self.assertNotIn("data:image", text)


class ExtractImageAttachmentsTests(unittest.TestCase):
    """P39 follow-up: OWUI image_url blocks are decoded into the gateway's
    `chat.send` attachments shape so images are actually relayed to the
    agent, not just silently dropped with a note."""

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
    """Regression tests for the whole-message duplication bug (2026-07-06):
    an `item` event of kind message/output can echo the complete final
    assistant text, which must not be re-yielded on top of what the
    `assistant` delta stream already produced."""

    def test_genuine_preamble_before_any_assistant_text(self):
        # No assistant-stream text yet -> a real preamble should pass through.
        self.assertEqual(
            _item_delta_text("Let me check that.", "", ""),
            "Let me check that.",
        )

    def test_duplicate_full_message_after_assistant_stream_is_suppressed(self):
        # This is the bug: item event echoes the exact text already streamed.
        self.assertEqual(
            _item_delta_text("Hello world", "", "Hello world"),
            "",
        )

    def test_item_event_extends_beyond_assistant_stream(self):
        # Item text repeats what was streamed and adds something new.
        self.assertEqual(
            _item_delta_text("Hello world, more.", "", "Hello world"),
            ", more.",
        )

    def test_sequential_item_only_events_still_dedup_against_each_other(self):
        # Original behavior preserved when there's no assistant-stream text.
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
    """Regression tests for P27 (reproduced live 2026-07-10): a provider's
    final catch-all `assistant` event can carry the full cumulative reply
    while `assistant_stream_text` (the primary dedup baseline) has drifted
    from what was actually recorded into `visible_message_text` -- e.g.
    across an idle-timeout recovery cycle. When that happens
    `_item_delta_text`'s prefix check fails and it falls through to
    returning the whole text unchanged, duplicating the entire message with
    no separator. `_suppress_already_shown` is the final safety net,
    checked against `visible_message_text` directly (the true record of
    what's already been shown) regardless of which baseline produced the
    false negative."""

    def test_suppresses_text_already_at_the_tail(self):
        self.assertEqual(
            _suppress_already_shown("world", "hello world"),
            "",
        )

    def test_suppresses_exact_full_duplicate(self):
        # The exact live-reproduced shape: assistant_stream_text drifted,
        # so _item_delta_text's `return item_text` branch handed back the
        # entire message verbatim.
        full_text = "Status update: all six repro attempts came back clean.\n\nWhat next?"
        self.assertEqual(
            _suppress_already_shown(full_text, full_text),
            "",
        )

    def test_genuine_new_text_passes_through(self):
        # Not yet shown anywhere in visible_message_text -> must not be dropped.
        self.assertEqual(
            _suppress_already_shown(" more", "hello world"),
            " more",
        )

    def test_empty_delta_is_noop(self):
        self.assertEqual(_suppress_already_shown("", "hello world"), "")

    def test_substring_in_the_middle_is_not_suppressed(self):
        # Only a match at the very tail counts as "already shown" -- a
        # coincidental substring earlier in the text is not evidence of
        # duplication and must pass through untouched.
        self.assertEqual(
            _suppress_already_shown("world", "world peace hello"),
            "world",
        )


class ResolveMediaTests(unittest.TestCase):
    """Regression tests for P2 (2026-07-06): _resolve_media used to drop
    any text preceding the MEDIA: prefix within the same chunk."""

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
        # Regression: sending several images in one reply (confirmed live
        # 2026-07-10) used to only resolve the first MEDIA: occurrence,
        # leaving the rest as literal "MEDIA:filename" text.
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
        # Regression: writing "the MEDIA: fix" as a documentation term (not
        # an actual directive) got misparsed live 2026-07-10 -- "fix" (no
        # extension) was treated as a filename and turned into a broken
        # image link, corrupting the assistant's own explanatory text.
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
    """Async tests for the native-OWUI-upload MEDIA: resolution path,
    covering the same multi-directive regression as ResolveMediaTests."""

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
        # If any single directive can't be resolved (file missing locally),
        # the whole attempt must bail with handled=False so the caller falls
        # back to _resolve_media for ALL directives uniformly, instead of
        # leaving a mix of native-uploaded and unresolved directives.
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
        # Same regression as _resolve_media's prose test, but for the
        # native-OWUI-upload path: no upload should even be attempted for
        # "MEDIA: fix" since "fix" isn't a plausible filename.
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


class MediaBufferTests(unittest.TestCase):
    """Regression tests for the 2026-07-10 finding: a real streaming
    provider can split "MEDIA:filename.png" across multiple deltas at any
    point, and resolving eagerly against a single truncated delta either
    drops the directive (if "MEDIA:" itself is split) or corrupts the
    filename (if the split lands mid-filename). _advance_media_buffer
    holds back until a trailing whitespace confirms the filename is whole."""

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
    # +2s buffer on every delta below: test execution time between computing
    # `now_ms` and `_relative_time()` reading the real clock can itself eat a
    # few ms, which is enough to floor a value sitting exactly on a minute
    # boundary down by one unit. The buffer keeps these deterministic
    # without weakening what's actually being checked (whole-unit rounding).
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
                        # No resetAt on either window here -> no detail
                        # lines, just the thin combined line.
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
        # Thin combined line stays exactly as before (no reset time on it);
        # each window that has a reset time gets its own extra detail line,
        # in addition to (not instead of) the thin one.
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
        # Real token-by-token streaming (e.g. Claude) delivers the trigger a
        # few characters at a time -- each partial prefix should still read
        # as "could be a match" so the pipe keeps buffering instead of
        # yielding it as plain text.
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
        # Once the prefix is fully confirmed, appending the rest of the
        # question (arbitrary length) must keep matching so buffering
        # continues right up to the end of the run.
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
        # The exact bug caught live 2026-07-08: a single delta can contain
        # the tail of normal prose, the paragraph-break newline, AND the
        # start of the next paragraph all at once -- the newline doesn't
        # land at the delta's edge, so a naive endswith("\n") check misses
        # it. The text after the LAST newline must still be buffered.
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
        # The raw "(multiselect)" directive must not leak into the shown title.
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

    def test_confirmation_for_confirm_verb_question(self):
        f = _parse_text_prompt("OpenClaw needs input:\n\nDelete file?\nDelete config.json?")
        self.assertTrue(f["confirm"])

    def test_choice_question_is_not_confirmation(self):
        # A short "?" title that is really a free-text choice must NOT become
        # a yes/no question (would silently strip the real answer).
        f = _parse_text_prompt("OpenClaw needs input:\n\nWhich model?\nWhich model should I use?")
        self.assertEqual((f["confirm"], f["options"]), (False, []))

    def test_numbered_options_never_confirmation(self):
        # An enumerated option list is a choice even if a confirm verb appears.
        f = _parse_text_prompt("OpenClaw needs input:\n\nProceed how?\nProceed?\n1. rebase\n2. merge")
        self.assertFalse(f["confirm"])
        self.assertEqual(f["options"], ["rebase", "merge"])

    def test_a_choice_or_yes_no_question_that_mentions_a_secret_keeps_its_answers(self):
        """ELI-196: a question with options or a yes/no answer that merely
        mentions a password, secret or token got the masked dialog, without
        its options or Yes/No, and what was typed reached the agent as a
        plain answer it echoed into the chat."""
        f = _parse_text_prompt("OpenClaw needs input:\n\nRouter entry\nOK to add the router-debug "
                               "entry so W6 can use the password?\n1. Yes\n2. No")
        self.assertEqual((f["secret"], f["options"]), (False, ["Yes", "No"]))
        f = _parse_text_prompt("OpenClaw needs input:\n\nTokens\nRotate the API token now? (y/n)")
        self.assertEqual((f["secret"], f["confirm"]), (False, True))
        # A question that asks for the value itself still gets the dialog.
        f = _parse_text_prompt("OpenClaw needs input:\n\nTest token\nPaste the test API token (password)")
        self.assertEqual((f["secret"], f["confirm"], f["options"]), (True, False, []))

    def test_a_secret_ask_keeps_the_masked_field_whatever_else_it_has(self):
        """W3 on #137: asking for the value wins over steps written as a
        numbered list, a confirm verb, or options under Codex's "may show
        your reply" warning; a key typed into the card's Other field would
        land in the chat."""
        f = _parse_text_prompt("OpenClaw needs input:\n\nToken\nPaste your API key:\n1. Open Settings\n2. Copy the key")
        self.assertEqual((f["secret"], f["options"]), (True, []))
        f = _parse_text_prompt("OpenClaw needs input:\n\nPassword\nPlease confirm your password?")
        self.assertEqual((f["secret"], f["confirm"]), (True, False))
        f = _parse_text_prompt("Codex needs input:\n\nToken\nThis channel may show your reply to other participants.\n1. a\n2. b")
        self.assertEqual((f["secret"], f["options"]), (True, []))

    def test_secret_marker_does_not_match_innocent_key_substring(self):
        # "monkey" contains "key" -- must not be flagged as a secret.
        f = _parse_text_prompt("OpenClaw needs input:\n\nName the monkey\nWhat should we call the monkey?")
        self.assertFalse(f["secret"])

    async def test_ask_user_input_modal_noops_without_event_call(self):
        answer = await _ask_user_input_modal(
            None,
            "Codex needs input:\n\nPackage\nChoose",
        )

        self.assertIsNone(answer)

    async def test_an_unanswered_secret_dialog_ends_the_wait(self):
        """OWUI puts no timeout on an input dialog; ours ends the wait, and
        the question ends the reply as text."""
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
        # The numbered list moves into the options instead of repeating in the text.
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
        """ELI-151: the card for the inline form showed the raw marker as its
        header and question. It is the card of the marker on its own line."""
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
        """OWUI's timeout, and a cancel the moment the card reaches a tab on
        another chat, do not show the card was seen: no re-show (ELI-142)."""
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

    # ELI-107 decision 2: a wait for the card's answer ends with the run, so
    # a tab that cannot show it is no reason to keep the reply open.

    def _socket(self, pool, call):
        fake_owui(self, sio=FakeSio(lambda event, data, to, timeout: call(data, to)), SESSION_POOL=pool)

    async def test_a_tab_on_another_chat_is_no_viewer(self):
        """OWUI 0.12 records the chats each socket views; the user's tab on
        another chat never shows this card, so the reply ends at once."""
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
        """The tab viewing the chat went away (a reload, a closed tab) while
        it held the card: the reply ends with the question as text instead
        of waiting for the user to come back."""
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

    # ELI-58 live repro (2026-10-13, chat c83d7eb7, pipe cb5a7cb): the card was
    # pending, the user reloaded the chat. OWUI's call on the gone socket timed
    # out only after 240 s, and the re-fire then went to the user's FIRST
    # socket in SESSION_POOL (another tab, another worker's client), so the
    # reloaded chat never showed the card again. OWUI 0.12 records the chats
    # each socket views (`SESSION_POOL[sid]["chat_ids"]`, events:chat join).

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
        """A tab on another chat is no viewer: waiting for the user to open
        this one kept the stream open past its run, up to an hour (ELI-107).
        The question ends the reply as text."""
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
        """The first call is pending on the reloaded tab's old socket; OWUI
        answers it only after its 240 s timeout. The old socket leaving
        SESSION_POOL is the signal."""
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

    # ELI-142 live (2026-10-15, chat ed98cbd2): after an Open WebUI restart the
    # tab's socket stayed in SESSION_POOL while the page, in a reconnect storm,
    # never showed the card. Each call ended in OWUI's 240 s timeout and the
    # pipe re-showed the card: four timeouts, about 20 minutes of an open
    # reply with nothing on screen and no text fallback.

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
        """The turn's socket is gone and the tab viewing the chat takes the
        card, but that socket never answers either."""
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
    """A multi-select question is a question card too (ELI-148): the card
    picks one option per question, so it lists the options by number and
    takes a typed answer, "1,3", which goes to the agent as the options.
    Uses only the card's entry points, so it runs against any build."""

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
        # No option rows: a click on one answers the card at once.
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
    """A secret keeps OWUI's masked input dialog (the card has none), asked
    the way the card is (ELI-148): once, moved once to the tab viewing the
    chat when the turn's socket goes away, never re-shown after a timeout.
    Its answer never shows in the chat."""

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


class DialogHoldsExecutesTests(unittest.IsolatedAsyncioTestCase):
    """Open WebUI 0.12's tab keeps one callback for its input dialog and the
    `execute` event, and each overwrites it on arrival: an `execute` reaching
    a tab while its dialog is open took the dialog's answer (ELI-195). While
    the Pipe waits on a dialog, no `execute` goes to that socket: an emitted
    one waits for the answer, a query is not sent."""

    class Sio:
        def __init__(self):
            self.calls, self.emits = [], []
            self.answer = asyncio.get_running_loop().create_future()

        async def call(self, event, data, to=None, timeout=None):
            self.calls.append((data, to))
            if data["data"]["type"] == "input":
                return await self.answer
            return "stop"

        async def emit(self, event, data, **kw):
            self.emits.append((data, kw))

    INPUT = {"type": "input", "data": {"title": "Token", "type": "password"}}

    async def asyncSetUp(self):
        self.sio = self.Sio()
        self.pool = {"sidA": {"id": "user-1", "chat_ids": ["chat-1"]},
                     "sidB": {"id": "user-1", "chat_ids": ["chat-1"]}}
        fake_owui(self, sio=self.sio, SESSION_POOL=self.pool)

    async def _dialog_on(self, sid):
        """The input dialog, open on `sid` until `self.sio.answer` is set."""
        others = {k: v for k, v in self.pool.items() if k != sid}
        for k in others:
            self.pool[k] = {**self.pool[k], "chat_ids": []}
        dialog = asyncio.ensure_future(openclaw_pipe._call_on_chat_tab(
            "user-1", "chat-1", "msg-1", self.INPUT, 60))
        while not self.sio.calls:
            await asyncio.sleep(0)
        self.pool.update(others)
        return dialog

    async def test_an_execute_emitted_meanwhile_reaches_that_tab_after_the_answer(self):
        dialog = await self._dialog_on("sidA")
        emitter = RecordingEmitter()
        # The Action artifact holds for the Pipe's dialog: the record is shared.
        emit = openclaw_status_action._dialog_safe_emitter(emitter, "user-1", "chat-1", "msg-1")
        await emit({"type": "execute", "data": {"code": "1"}})
        await emit({"type": "status", "data": {"description": "x"}})
        self.assertEqual([e["type"] for e in emitter.events], ["status"])
        payload = {"chat_id": "chat-1", "message_id": "msg-1",
                   "data": {"type": "execute", "data": {"code": "1"}}}
        self.assertEqual(self.sio.emits, [(payload, {"room": "user:user-1", "skip_sid": ["sidA"]})])
        self.sio.answer.set_result({"value": "v"})
        await dialog
        self.assertEqual(self.sio.emits[1:], [(payload, {"to": "sidA"})])
        await emit({"type": "execute", "data": {"code": "2"}})
        self.assertEqual(emitter.events[-1]["data"]["code"], "2", "no dialog: the emitter as is")

    async def test_a_query_is_not_sent_to_a_tab_showing_the_dialog(self):
        dialog = await self._dialog_on("sidA")
        self.assertEqual(await openclaw_pipe._page_cancel_intent("user-1", "chat-1", "msg-1"), "stop")
        queried = [to for data, to in self.sio.calls if data["data"]["type"] == "execute"]
        self.assertEqual(queried, ["sidB"])
        asked = []

        async def event_call(payload):
            asked.append(payload["type"])
            return "stop"
        call = openclaw_pipe._dialog_safe_event_call(event_call, "sidA", "user-1")
        self.assertIsNone(await call({"type": "execute", "data": {"code": "q"}}))
        self.assertEqual(asked, [])
        self.sio.answer.set_result({"value": "v"})
        await dialog
        self.assertEqual(await call({"type": "execute", "data": {"code": "q"}}), "stop")

    async def test_the_turns_own_dialog_holds_its_socket(self):
        answered = asyncio.get_running_loop().create_future()

        async def event_call(payload):
            return await answered
        call = openclaw_pipe._dialog_safe_event_call(event_call, "sidA", "user-1")
        dialog = asyncio.ensure_future(call(self.INPUT))
        await asyncio.sleep(0)
        self.assertTrue(openclaw_pipe._dialog_open_on("sidA"))
        self.assertFalse(openclaw_pipe._dialog_open_on("sidB"))
        answered.set_result("v")
        self.assertEqual(await dialog, "v")
        self.assertFalse(openclaw_pipe._dialog_open_on("sidA"))


class SecretAnswerNeverInTheChatTests(unittest.IsolatedAsyncioTestCase):
    """ELI-148: a secret typed into the masked dialog goes to the agent and
    nowhere else the pipe writes: not the reply's text or items, not the
    events it emits, not the rewrites a turn writer would apply to the
    stored message, not the pipe log."""

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered
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


class SecretNotReceivedTests(unittest.IsolatedAsyncioTestCase):
    """ELI-148: a secret whose dialog got no answer (no tab on the chat, a
    timeout; a cancel is SecretDialogCancelledTests) must not end the reply asking for it in the chat,
    which stores what is typed there. The reply says it was not received and
    how to enter it; the agent is told once that nothing came."""

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered
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
        # Rebuilt from the transcript after an Open WebUI restart, the same.
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


class SecretDialogCancelledTests(unittest.IsolatedAsyncioTestCase):
    """ELI-189: Cancel on the masked dialog resolves the event call with
    False (Open WebUI 0.12.3 EventConfirmDialog `cancel`). The agent got the
    answer "no" and the card said "•••• (secret entered)". The agent must
    learn the user cancelled, and the card must say so."""

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered
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
        """After an Open WebUI restart the card is rebuilt from what the agent
        was sent, which the transcript prefixes with a time."""
        async def event_call(event):
            return False
        told = await _ask_user_input_modal(event_call, self.PROMPT, owui_user_id=None, owui_chat_id="chat-1",
                                           owui_message_id="m1", poll_interval_s=0.01)
        block = _ask_user_detail_block(self.PROMPT, f"[Sat 10:00] {told}")
        self.assertIn('result="Cancelled', block)
        self.assertNotIn("secret entered", block)


class DynamicModelSelectorTests(unittest.TestCase):
    """Tests for ELI-11: dynamic model discovery."""

    def test_friendly_name_prefers_versioned_catalog_name_over_alias(self):
        # The alias ("opus") hides the version; the catalog name carries it.
        # Two versions must not both render as a bare "Opus" in the selector.
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
        # Model keys can contain dots (e.g. version numbers like "3.1");
        # only the function-id prefix's dot should be stripped.
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
        # Real gateway `models.list` entries use id/name/provider/alias --
        # not the key/tags shape the rest of this module expects.
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
            # Use a minimal valves-like object with STATE_DIR
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
        """Live gateway responses use id/name/provider/alias, not key/tags --
        _discover_models_with_source must normalize them before returning/caching."""
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
        """An allowlisted model with no credentials comes back from
        models.list as available:false. Listing it gave OWUI a selector entry
        that could only 401 (anthropic/claude-haiku-5-5 next to the working
        claude-cli/claude-haiku-5-5). Shapes below are the real Gateway's."""
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
        # label should contain provider
        for opt in options:
            self.assertIn("(", opt["label"])
            self.assertIn(")", opt["label"])


class SharedProactiveStateAcrossConnectionsTests(unittest.TestCase):
    """Regression tests for the 2/2 duplicate-branch / bogus '*↳ Proactive
    message*' bug (P33, root-caused 2026-07-11).

    Root cause: OWUI's function loader execs each redeploy into a new module
    with no teardown of the old one, so a connection left running by a
    previous deploy (a "zombie") keeps its own event loop and its own private
    proactive-delivery bookkeeping. The zombie still receives the Gateway's
    broadcast `final` events; with private bookkeeping it can't tell the live
    connection already showed the turn to the open tab, so it re-writes the
    turn into chat history as a spurious proactive message -- creating a second
    assistant branch (the 1/2 <-> 2/2 navigation) on every turn.

    Fix: when running inside OWUI the liveness bookkeeping is anchored on
    `open_webui.socket.main` and shared by *every* connection in the process,
    so a turn shown live by any connection is seen as delivered by all of them.
    These tests simulate that by installing a fake `open_webui.socket.main`
    module and asserting two independent connections share the same state.
    """

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
        """Redeploy migration guard (2026-07-13): the shared-state dict
        persists across function redeploys, so a key added in a newer version
        must be backfilled into the *already-existing* dict via setdefault --
        otherwise `__init__`'s `_shared['chat_write_locks']` KeyErrors on the
        first request after redeploy (this broke a live deploy). Simulate an
        old dict missing the new key and assert it gets added without dropping
        the pre-existing entries."""
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
        # Live connection dispatches the final event to its open tab.
        live.mark_delivered_live("session-x", "run-1")
        # The zombie, receiving the same broadcast, must recognise it was
        # already shown live and skip proactive delivery.
        self.assertTrue(zombie.was_delivered_live("session-x", "run-1"))

    def test_isolated_when_not_inside_owui(self):
        # Without OWUI's module present, each connection keeps private state so
        # unrelated processes/tests can't leak into each other.
        for k in ("open_webui.socket.main", "open_webui.socket", "open_webui"):
            sys.modules.pop(k, None)
        self.assertIsNone(_shared_gateway_state())
        a, b = self._conn(), self._conn()
        self.assertIsNot(a._delivered_live, b._delivered_live)


class ModelPatchCacheTests(unittest.TestCase):
    """ELI-59: per-session model-patch cache + lock (skip redundant
    sessions.patch RPCs under a same-model burst)."""

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
    """GATEWAY_URL renders as a plain text box that looks like a URL field, so
    users paste URLs into it. Before this parser, `http://gw:18789` silently
    dialled a host literally named `http` on port 80, and `http://gw` raised a
    bare ValueError that escaped pipe()'s `except GatewayError` entirely."""

    def test_plain_host_port(self):
        self.assertEqual(_parse_gateway_url("localhost:18789"), ("localhost", 18789))

    def test_host_only_uses_default_port(self):
        self.assertEqual(_parse_gateway_url("gw.local"), ("gw.local", 18789))

    def test_whitespace_is_tolerated(self):
        self.assertEqual(_parse_gateway_url("  gw.local:18789 "), ("gw.local", 18789))

    def test_schemes_are_stripped_not_rejected(self):
        for raw in ("http://gw.local:18789", "https://gw.local:18789",
                    "ws://gw.local:18789", "wss://gw.local:18789"):
            with self.subTest(raw=raw):
                self.assertEqual(_parse_gateway_url(raw), ("gw.local", 18789))

    def test_scheme_without_port_falls_back_to_default(self):
        # Previously: int("//gw.local") -> ValueError, uncaught.
        self.assertEqual(_parse_gateway_url("http://gw.local"), ("gw.local", 18789))

    def test_trailing_path_and_query_are_dropped(self):
        # Previously: int("8443/") -> ValueError, uncaught.
        self.assertEqual(_parse_gateway_url("https://gw.local:8443/"), ("gw.local", 8443))
        self.assertEqual(_parse_gateway_url("gw.local:8443/api?x=1"), ("gw.local", 8443))

    def test_never_yields_a_scheme_as_the_hostname(self):
        # The exact old bug: hostname would come back as "http".
        host, _ = _parse_gateway_url("http://gw.local:18789")
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
        self.assertEqual(_parse_gateway_url("[::1]:18789"), ("[::1]", 18789))
        self.assertEqual(_parse_gateway_url("[::1]"), ("[::1]", 18789))
        self.assertEqual(_parse_gateway_url("[fe80::1]"), ("[fe80::1]", 18789))
        self.assertEqual(_parse_gateway_url("http://[::1]:18789"), ("[::1]", 18789))

    def test_bare_ipv6_is_rejected_rather_than_silently_misparsed(self):
        """Regression guard: rpartition(":") reads "::1" as host ":" port 1 --
        silently wrong on both counts, the exact failure class this exists to
        remove. Asking for brackets beats guessing which colon is the port."""
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
        self.assertEqual(_parse_gateway_url("host:65535"), ("host", 65535))

    def test_unsupported_scheme_is_rejected_clearly(self):
        with self.assertRaises(GatewayError) as cm:
            _parse_gateway_url("ftp://gw.local:18789")
        self.assertIn("ftp://", str(cm.exception))


class ConnectRejectionMessageTests(unittest.TestCase):
    """Device approval happens on every first install; the raw Gateway reply is
    just 'pairing required', and the fixing command lived only in the README."""

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


class UnverifiedModelListTests(unittest.TestCase):
    """pipes() never opens a connection -- it only reuses one a previous chat
    established. So on a fresh install the live branch is skipped and there is
    no cache, and the selector used to fill with five hardcoded models rendered
    identically to real ones. Picking one the Gateway lacks then failed with a
    bare "Model selection error"."""

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
            # Default genuinely works with no Gateway: it clears the override.
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
    """The session key embeds AGENT_ID and this patch is the turn's first
    agent-scoped RPC, so a mistyped AGENT_ID surfaced as "Model selection
    error" -- sending the user to fix model valves that were never the problem."""

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
        """Regression guard. The first version of this matched "not found" as an
        agent hint, so "model 'x/y' not found" -- a plain model error -- told the
        user to go fix AGENT_ID. That is the same misattribution this function
        exists to prevent, merely pointed the other way."""
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
    """Defects found reviewing the branch that introduced the fixes above.
    Each of these shipped briefly; none may come back silently."""

    def test_bracketed_hostname_is_rejected_not_crashed(self):
        """Brackets mean IPv6 to every URL parser, so websockets' urlsplit()
        raises a bare ValueError for a bracketed hostname -- neither OSError nor
        WebSocketException, so it escaped both the connect handler and pipe()'s
        except GatewayError. Reachable by following our own advice: the bare-IPv6
        branch says "wrap it in brackets", and wrapping a HOSTNAME lands here."""
        for raw in ("[gateway.local]:18789", "[localhost]:18789", "[192.168.1.10]:18789"):
            with self.subTest(raw=raw):
                with self.assertRaises(GatewayError) as cm:
                    _parse_gateway_url(raw)
                self.assertIn("brackets", str(cm.exception))
        # real IPv6 still works
        self.assertEqual(_parse_gateway_url("[fe80::1]:18789"), ("[fe80::1]", 18789))

    def test_multi_colon_non_ipv6_is_not_called_ipv6(self):
        with self.assertRaises(GatewayError) as cm:
            _parse_gateway_url("myhost:80:90")
        self.assertNotIn("is an IPv6 address", str(cm.exception))

    def test_non_ascii_digit_port_raises_gateway_error_not_valueerror(self):
        """str.isdigit() is True for superscripts that int() then rejects, and
        that ValueError was raised before the caller's try, so it escaped."""
        for raw in ("host:²", "host:¹8789"):
            with self.subTest(raw=raw):
                with self.assertRaises(GatewayError):
                    _parse_gateway_url(raw)

    def test_transient_failures_are_not_blamed_on_a_valve(self):
        """str(TimeoutError()) is "", which rendered as a dangling colon followed
        by advice to edit two valves that were both correct."""
        for err in (asyncio.TimeoutError(), ConnectionResetError(104, "reset")):
            with self.subTest(err=type(err).__name__):
                msg = _explain_session_patch_failure(
                    err, model_override="x/y", agent_id="main")
                self.assertNotIn("AGENT_ID", msg)
                self.assertNotIn("Model selection error", msg)
                self.assertIn("again", msg.lower())

    def test_dropdown_reads_the_cache_the_pipe_actually_writes(self):
        """get_model_options is a classmethod, so it used _state_dir() with no
        argument while the writer used the STATE_DIR valve. For anyone with a
        custom STATE_DIR it read a path nothing writes, never found the cache,
        and so labelled every option "Gateway not reached" permanently."""
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
    """Caught in a real screenshot: an agent emitted the needs-input block twice,
    the second copy glued onto the last option with no newline. The dialog then
    offered SIX buttons for a three-option question, and one of them was labelled
    with the raw internal directive:

        "Park itOpenClaw needs input: Which PoC do we run?"

    The agent misbehaved, but the pipe owns what reaches the screen."""

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
    """The text the renderer adds for a cumulative catch-all event (no
    `delta`, the whole reply in `text`) after `received` arrived and
    `visible` showed. The inline reply's own copy of this diff went in
    ELI-147; the renderer's is the one both sides use."""
    r = _TurnRenderer()
    r.seed({"received": received})
    r.visible_text = visible
    added = r.feed({"stream": "assistant", "data": {"text": raw_text}})
    return "".join(a for a in added if isinstance(a, str))


class MediaDirectiveDoubledFilenameTests(unittest.IsolatedAsyncioTestCase):
    """ELI-76. Captured 2026-07-29 (OWUI 0.10.2 baseline, Gemini 3 Flash): a
    reply that was exactly `MEDIA:<file>.png`, with no trailing whitespace,
    persisted as a link with the filename doubled, which 404s.

    How the doubled text formed: `_advance_media_buffer` held the whole
    directive back (no whitespace ever confirmed the filename), so nothing was
    shown. The turn's catch-all event then carried the whole reply again; it
    was diffed against SHOWN text (empty), looked entirely new, and was
    appended to the buffer with no separator. The end-of-stream flush handed
    `MEDIA:F.pngMEDIA:F.png` to `_resolve_media`, whose whitespace-only split
    took all of it as one filename.

    The stream half was closed by ELI-80 (diff the catch-all against received
    text). The media half is fixed here: a filename now also ends at the next
    `MEDIA:`, and a glued copy of the same directive is dropped.
    """

    BASE = "https://homeassistant.tail25faa5.ts.net:18791"
    F = "baseline-test2-0102-1785351819.png"
    MALFORMED = (f"![{F}MEDIA:{F}]"
                 f"(https://homeassistant.tail25faa5.ts.net:18791/{F}MEDIA:{F})")
    CORRECT = f"![{F}](https://homeassistant.tail25faa5.ts.net:18791/{F})"

    def _stream(self, catch_all_baseline):
        """Replay the turn through the pipe's real assistant-stream helpers.

        Returns what the end-of-stream flush hands to `_resolve_media`.
        `catch_all_baseline` picks what the catch-all is diffed against:
        "received" (the code since ELI-80) or "shown" (the code ELI-76 hit).
        """
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
    """ELI-80. Reproduced live 2026-08-01 with a SINGLE marker emission.

    Sequence that broke it:

      1. The agent streams a needs-input block. Every delta is withheld by
         _advance_input_prompt_buffer, so nothing is yielded and
         `visible_message_text` stays empty. `assistant_stream_text` also stays
         empty, because it is only appended to AFTER the buffer flushes.
      2. The turn ends with a catch-all event: no `delta`, a `text` field
         holding the whole reply.
      3. That was diffed against `assistant_stream_text` (empty) and against
         `visible_message_text` (empty), so the entire block looked new.
      4. `candidate = pending + delta` appended it to the buffer a second time,
         with no separator, producing `3. AutoOpenClaw needs input:` and a
         dialog with six buttons for a three-option question.

    The fix diffs against everything RECEIVED, not everything shown.
    """

    BLOCK = ("OpenClaw needs input:\n"
             "Pick a theme\n"
             "1. Dark\n"
             "2. Light\n"
             "3. Auto")

    def _stream_withheld(self):
        """Feed the block through the ask-user buffer one delta at a time.

        Returns (pending, received, visible) exactly as pipe() would hold them:
        everything withheld, nothing shown.
        """
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
        """Pins WHY the fix works, so nobody 'simplifies' it back.

        Shown text is empty while a block is withheld, so a shown-only diff
        reports the entire block as new. That was the defect.
        """
        _pending, _received, visible = self._stream_withheld()
        shown_only = _catch_all_delta(self.BLOCK, visible, visible)
        self.assertEqual(shown_only, self.BLOCK)

    def test_genuinely_new_trailing_text_still_arrives(self):
        """The fix must not swallow real content the catch-all adds."""
        pending, received, visible = self._stream_withheld()
        full = self.BLOCK + "\nAnything else?"
        self.assertEqual(_catch_all_delta(full, received, visible), "\nAnything else?")

    def test_catch_all_after_normal_shown_text_is_unchanged(self):
        """No needs-input involved: prior behaviour preserved."""
        received = visible = "Hello there."
        self.assertEqual(_catch_all_delta("Hello there.", received, visible), "")
        self.assertEqual(
            _catch_all_delta("Hello there. More.", received, visible), " More.")

    def test_empty_catch_all_is_noop(self):
        self.assertEqual(_catch_all_delta("", "abc", "abc"), "")


class MarkerAfterPreambleTests(unittest.TestCase):
    """Seen live 2026-08-01: a reply that explained itself first and asked at the
    end leaked the raw marker into the chat with no dialog.

    The buffer only inspected the text after the LAST newline. That is fine
    while tokens arrive one at a time (the marker is briefly the final line),
    but wrong when a provider delivers the whole reply as ONE cumulative chunk:
    the marker then sits mid-string with its option lines after it, and was
    never recognised. Token-streamed turns were unaffected, which is exactly why
    this looked intermittent rather than broken.
    """

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
        """The live failure. Was: marker leaked, no dialog."""
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
    """`connection_epoch` is the only thing that separates "the queue is quiet
    because the agent is thinking" from "the queue is quiet because the Gateway
    restarted and took the run with it". The Gateway persists a turn only once
    it COMPLETES, so a mid-run restart destroys the run and emits no terminal
    event at all -- the consumer queue just goes silent. If this counter stops
    moving on reconnect, `pipe()` silently reverts to closing the stream on
    every restart, which is the original "you died again and did not come up"
    bug."""

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
        """A rejected handshake is not a new connection. Bumping here would
        make pipe() resume a turn whose run was never actually lost."""
        conn = self._conn()
        with self.assertRaises(GatewayError):
            await self._connect(conn, ok=False)
        self.assertEqual(conn.connection_epoch, 0)

    async def test_reconnect_path_bumps_the_epoch(self):
        """The restart case reaches `_connect_and_start` via `_reconnect`, not
        via `ensure_connected` -- that is the path the resume check depends on,
        so cover it explicitly rather than trusting the shared tail."""
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
        """2026-10-12: websockets' default 1 MiB frame cap made the client
        close the whole Gateway connection (1009) on a 3.1 MB `chat.history`
        reply, dropping every session's events (repro_ws_max_size.py)."""
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
        """2026-10-12: a 1s WebSocket drop (same Gateway, same bootId) was
        counted as a restart; the pipe sent a resume turn into two live
        sessions and closed their OWUI replies while the runs kept going."""
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
    """Pipe half of the per-chat thinking control (ELI-85).

    The filter offers levels; the pipe decides what is actually sendable and
    tells the gateway. These tests cover that second half, plus the seam
    between them: the ladder cache the filter reads is written here.
    """

    SEND_KW = dict(session_key="agent:main:openwebui-u-c", message="hi",
                   idempotency_key="k", owui_chat_id=None, owui_user_id=None)

    def test_no_thinking_field_when_nothing_was_chosen(self):
        # Absent is a real instruction: it means "use the agent's configured
        # level". Sending any value, including "off", would override it.
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
        # thinkingLevels carries the real {id, label} objects the gateway
        # resolves (resolveGatewaySessionThinkingProjectionInternal ->
        # thinkingLevels: metadata.levels) -- this is the authoritative,
        # canonical-id source clamp_to_ladder/LEVEL_RANKS match against.
        desc = {"session": {"thinkingLevels": [
            {"id": "off", "label": "off"}, {"id": "medium", "label": "medium"}]}}
        self.assertEqual(_session_thinking_ladder(desc), ["off", "medium"])

    def test_thinking_levels_wins_when_both_are_present(self):
        # Regression for ELI-85's live break: the old code checked
        # thinkingOptions FIRST and returned it as-is. thinkingOptions is
        # display LABELS (metadata.levels.map(level => level.label)), not
        # ids -- for a model whose label differs from its id (the realistic
        # case, e.g. "Off" vs "off"), that ladder can never match a
        # requested level, clamp_to_ladder falls through to "pass it to the
        # gateway", and the gateway hard-rejects the turn with its own
        # "Thinking level ... is not supported" error. thinkingLevels must
        # win whenever both are present.
        desc = {"session": {"thinkingOptions": ["Off"],
                            "thinkingLevels": [{"id": "max"}]}}
        self.assertEqual(_session_thinking_ladder(desc), ["max"])

    def test_ladder_falls_back_to_the_labelled_form_lowercased(self):
        # thinkingOptions is a last-resort fallback only (some future
        # gateway build stops emitting thinkingLevels). Lowercased on the
        # way out so a label that happens to already equal its id in casing
        # ("off") still matches LEVEL_RANKS instead of silently ranking as
        # unknown (rank 0) the way a bare capitalized "Off" would.
        desc = {"session": {"thinkingOptions": ["Off", "High"]}}
        self.assertEqual(_session_thinking_ladder(desc), ["off", "high"])

    def test_no_ladder_is_None_not_empty(self):
        # None means "unknown, pass the request through"; [] would mean "this
        # model supports nothing", which would clamp every request away.
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
            # Ordered by rank, because that is the order the dropdown shows.
            self.assertEqual(data["levels"], ["off", "low", "high"])
            self.assertIsInstance(data["updated"], int)

    def test_the_cache_is_a_union_across_models(self):
        # A dropdown built from only the last model seen would flicker between
        # ladders as the user switches models mid-chat.
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
        # Two different consumers of one file. `levels` is the union, because
        # the dropdown must not flicker as the user switches models mid-chat.
        # `models[<key>]` is what the SEND path clamps against and has to be
        # exact -- clamping against the union is precisely what let `high`
        # reach a model whose only level is `off`.
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
                # A model we have never seen is unknown, not unsupported.
                self.assertIsNone(_read_model_thinking_ladder("openai/gpt-9"))
                self.assertIsNone(_read_model_thinking_ladder(None))
            _, data = self._cache(td)
            self.assertEqual(data["levels"], ["off", "low", "high"])

    def test_describe_never_overwrites_what_the_gateway_stated(self):
        # `sessions.describe` resolves thinkingLevels with no model catalog in
        # scope, so it reports the generic 8-level profile for a model whose
        # real ladder is ["off"]. The rejection is the Gateway ruling on its
        # own send path. A later weak observation must not undo it, or the
        # misfire comes back once per turn forever.
        with tempfile.TemporaryDirectory() as td:
            key = "claude-cli/claude-opus-5"
            with mock.patch.dict(os.environ, {"OPENCLAW_BRIDGE_STATE_DIR": td}):
                _record_thinking_ladder(["off"], model_key=key, authoritative=True)
                _record_thinking_ladder(["off", "low", "medium", "high"],
                                        model_key=key)
                self.assertEqual(_read_model_thinking_ladder(key), ["off"])
                # A later authoritative statement DOES replace it: the model's
                # own ladder can legitimately change under it (a gateway
                # upgrade, a re-pinned runtime), and the Gateway is the only
                # source allowed to say so.
                _record_thinking_ladder(["off", "low"], model_key=key,
                                        authoritative=True)
                self.assertEqual(_read_model_thinking_ladder(key), ["off", "low"])
            _, data = self._cache(td)
            self.assertEqual(data["models"][key]["source"], "gateway")

    def test_model_key_is_formatted_the_way_the_gateway_names_it(self):
        # The cache key has to match the model ref parsed out of the rejection
        # verbatim, or a ladder learned from a rejection is filed under a name
        # the clamp never looks up.
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
        # Same fragment in both artifacts, so this cannot drift. If the pipe
        # clamped differently from what the dropdown implies, a user would pick
        # a level and get another with no explanation.
        got, note = clamp_to_ladder("max", ["off", "minimal", "low", "medium", "high"])
        self.assertEqual(got, "high")
        self.assertIn("high", note)
        self.assertEqual(clamp_to_ladder("medium", ["off", "medium"]), ("medium", None))
        self.assertEqual(clamp_to_ladder("high", None), ("high", None))
        self.assertEqual(clamp_to_ladder(None, ["off"]), (None, None))
        self.assertEqual(LEVEL_RANKS["off"], 0)

    def test_clamp_does_not_crash_on_an_unranked_ladder_entry(self):
        # A ladder entry outside LEVEL_RANKS (e.g. a raw display label that
        # slipped through, or a future gateway level id this build doesn't
        # know about yet) must degrade gracefully, not KeyError. The
        # at_or_below filter already tolerates this via .get(lv, 0); the
        # subsequent max() pick used to index LEVEL_RANKS[lv] directly and
        # would crash the whole turn on exactly this input.
        got, note = clamp_to_ladder("high", ["off", "Unranked"])
        self.assertEqual(got, "off")
        self.assertIn("high", note)

    def test_the_pipe_artifact_exposes_no_Filter_class(self):
        # Open WebUI introspects an uploaded file for Pipe/Filter/Action
        # classes. The shared fragment is class-free precisely so bundling it
        # into the pipe cannot make OWUI treat the pipe as a filter too.
        import openclaw_pipe
        self.assertFalse(hasattr(openclaw_pipe, "Filter"))
        self.assertTrue(hasattr(openclaw_pipe, "Pipe"))


class ThinkingRejectionRetryTests(unittest.TestCase):
    """An explicitly requested level a model can't honour reaches the Gateway,
    which hard-rejects the turn (ELI-85 live break, found via the sim-user
    harness). Two distinct ways that happens, and the retry has to cover both:

    * the session has no `sessions.describe` row yet (its first-ever message),
      so there is no ladder to clamp against; and because the turn is
      rejected, the row still never appears -- every later message repeats it.
    * the row exists and its ladder is WRONG. Describe resolves
      `thinkingLevels` with no model catalog in scope, so it hands back the
      generic 8-level base profile for a model whose real ladder is `["off"]`.
      Clamping against that passes `high` straight through.

    The Gateway never streams this rejection as a normal event (the run
    delivers a single contentless `final`), so an earlier attempt at this fix
    that peeked the run's event queue for it never saw it fire. The real
    text only surfaces via `recover_from_transcript()` at `_pipe_impl`'s final
    fallback, which is what `_thinking_rejection_retry_body` gates.
    """

    REJECTION_TEXT = (
        'Thinking level "high" is not supported for claude-cli/'
        'claude-opus-5. Use one of: off.'
    )

    def test_the_parser_matches_the_real_gateway_wording(self):
        # Detection and learning are the same call on purpose: whatever the
        # retry fires on is exactly what the ladder is learned from, so the
        # two can never disagree about what a rejection is.
        got = parse_thinking_rejection(self.REJECTION_TEXT)
        self.assertEqual(got, {"level": "high",
                               "model": "claude-cli/claude-opus-5",
                               "levels": ["off"]})

    def test_the_parser_does_not_misfire_on_ordinary_text_about_thinking(self):
        # Narrow on purpose: must never catch genuine assistant output that
        # happens to discuss thinking levels in passing.
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
        # The original body must be untouched -- the caller still needs it
        # for logging/diagnostics after this returns.
        self.assertEqual(body["reasoning_effort"], "high")

    def test_no_retry_when_nothing_was_recovered(self):
        self.assertIsNone(
            _thinking_rejection_retry_body({"reasoning_effort": "high"},
                                            None, "high"))

    def test_no_retry_when_no_explicit_level_was_ever_sent(self):
        # resolved_thinking is None whenever this chat never asked for an
        # override (or already retried once) -- recovered text matching the
        # rejection wording in that case is not this bug, so retrying is
        # never correct and would loop.
        retry_body = _thinking_rejection_retry_body(
            {"reasoning_effort": None}, self.REJECTION_TEXT, None)
        self.assertIsNone(retry_body)

    def test_no_retry_when_recovered_text_is_unrelated(self):
        retry_body = _thinking_rejection_retry_body(
            {"reasoning_effort": "high"}, "Here is your answer.", "high")
        self.assertIsNone(retry_body)

    def test_retry_cannot_fire_twice_in_a_row(self):
        # Simulates the recursive call: the first pass strips
        # reasoning_effort, so a second pass over that same body always has
        # resolved_thinking=None regardless of what recover_from_transcript()
        # returns -- the natural termination this fix relies on instead of
        # an explicit recursion-guard flag.
        first_body = {"reasoning_effort": "high"}
        retried = _thinking_rejection_retry_body(
            first_body, self.REJECTION_TEXT, "high")
        self.assertIsNotNone(retried)
        second = _thinking_rejection_retry_body(
            retried, self.REJECTION_TEXT, None)
        self.assertIsNone(second)



class TestSpawnBackground(unittest.TestCase):
    """Fire-and-forget work must stay referenced until it finishes.

    The event loop holds only a weak reference to a task; a bare
    asyncio.create_task() whose result is dropped can be collected mid-run.
    """

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
            # The set is module-wide: another task held in it at the same time
            # (here, one that outlives this test's) must not matter.
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
    """The startup pass over every unfinished OpenClaw reply (ELI-90/156):
    one stub at a time, a stub whose run still goes gets its own waiter, a
    failing stub does not stop the others, and the ended-while-down pass
    runs after it."""

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
    """What a reply still shows once its run settled with no final event
    here: the rest of the run's text, or why it shows nothing."""

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
                         "**Error:** quota")
        self.assertEqual(await self._turn().settled_tail({"status": "timeout"}),
                         "**Failed:** the run did not complete.")

    async def test_a_run_that_showed_text_owes_nothing_more(self):
        self.assertEqual(await self._turn(text_yielded=True).settled_tail({"status": "failed"}), "")



class TurnIdentityTests(unittest.TestCase):
    """ELI-100 DESIGN 2.2: the message a run renders into, from its id alone."""

    CHAT = "0abe9bde-9b86-4ef3-8a46-a3a98ed9410a"
    M = "8acdce95-1111-4222-8333-444455556666"
    KEY = f"agent:main:openwebui-2d206eab-acac-4945-bd2f-7e8b6365de8c-{CHAT}"

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
    """ELI-100 DESIGN 2.3: the claim of a run's message under the chat's row
    lock. The fake transaction holds a real per-chat lock and yields between
    reading the chat and committing, so concurrent claims really interleave."""

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

    # Review 2af1 R8: writer liveness was this process's set only, so with
    # more than one Open WebUI worker every other worker's live writer looked
    # gone and its message was taken over mid-stream. The other worker here is
    # a real second process that mints its writer id the way the pipe does.

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
    """ELI-100 DESIGN 2.7: who continues an interrupted run, from its
    persisted row. One case per shutdown kind, as measured or read in dist."""

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
    """The OpenClaw dist to check the fixtures against. With no OpenClaw
    installed the check skips; with OPENCLAW_DIST set (the E2E job) a missing
    dist fails it, so a wrong path cannot pass as a skip."""
    with _scripts_on_path():
        from openclaw_dist import default_dist
    dist = default_dist()
    if os.environ.get("OPENCLAW_DIST"):
        test.assertTrue(os.path.isdir(dist), f"OPENCLAW_DIST={dist!r} is not a directory")
    elif not dist or not os.path.isdir(dist):
        test.skipTest(f"no OpenClaw dist installed (looked at {dist!r})")
    return dist


class ExitTextFixtureTest(unittest.TestCase):
    """The owner rule matches the Gateway's exit-error TEXT: the dist has no
    structured exit field (DESIGN 2.7). The fixture is the exact source line
    extracted from the installed dist; this test renders its templates and,
    when the dist is installed here, fails if the line has changed."""

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
    """ELI-103: a wake is found by its admitted user turn's TEXT, the prefix
    `[cron:<jobId> <name>]`; the turn has no structured cron field. The
    fixture holds the exact dist lines that build it, one form per OpenClaw
    line (2026.9: the prefix inline; 2026.10: on its own line, ELI-164); this
    test renders each and, when a dist is installed here, fails unless it
    holds one of them exactly."""

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
    """A claude child is the first OOM victim (oom_score_adj 1000). A resume
    that is OOM-killed again must not loop: the budget is counted on M."""

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
        """Manager note 2 / M3-crash: main's resume, sent while recovery
        f2445aa8 ran, was queued as a follow-up and failed as 04351f67
        ("Reply operation has no active tool authority snapshot"). A row
        whose session has an active run never gets a resume, whatever R's
        own error: the decision is taken again on that run's settle."""
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
    """ELI-100 DESIGN 2.1 (M3-crash): restart recovery starts at boot, before
    the pipe subscribes, so its start push is never seen. Every hello
    subscribes WITH list params: one request in which the Gateway registers
    the subscriber and then lists (sessions-subscriptions-CeMquS--.mjs:21-36).
    Its reply is the snapshot of the runs already active."""

    USER = "2d206eab-acac-4945-bd2f-7e8b6365de8c"
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
                    # live shape for a claude-cli run: active, no ids (2026-10-13 00:5xZ)
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



class RecoveryLinkTests(unittest.TestCase):
    """ELI-100 rule E and the active run behind a snapshot without ids, on the
    shape of the M3-crash transcript (2026-10-13 00:33Z, chat 0c5d682c)."""

    CHAT = "0c5d682c-afaa-44f9-99a9-ada05599c997"
    M = "7371727a-1373-4108-9650-13b909f067b7"
    KEY = f"agent:main:openwebui-2d206eab-acac-4945-bd2f-7e8b6365de8c-{CHAT}"
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
        """Why no message is opened from the transcript (manager note 1): a
        queued follow-up runs with NO transcript turn (M3-crash: 04351f67 ran
        after recovery f2445aa8 and left nothing, checked live). So 'run R
        still active' and 'R failed unclosed, queued follow-up F active' are
        the same transcript. The writer opens a snapshot session's message on
        the run's first event, which carries its runId, and never guesses."""
        r_active = [self.user(self.SRC), {"role": "assistant", "content": [{"type": "toolcall"}]}]
        r_failed_then_f_active = copy.deepcopy(r_active)  # F leaves no trace while it runs
        self.assertEqual(r_active, r_failed_then_f_active)
        self.assertFalse(hasattr(openclaw_pipe, "active_run_from_history"),
                         "no transcript inference of the active run")

    def test_a_queued_follow_up_never_takes_the_recoverys_message(self):
        """04351f67 has no admitted turn, so rule E cannot call it a recovery:
        it maps to its own (agent-turn) id, never into M."""
        f = "04351f67-dbc3-4533-b2aa-2a0d88d79da0"
        self.assertIsNone(recovery_source_run(self.history(), f))
        self.assertNotEqual(run_message_id(self.KEY, self.CHAT, f, self.history()), self.M)



def _owui_output_text(output) -> str:
    """Open WebUI 0.12's `get_output_text` (utils/misc.py): the text of each
    message item, one per line."""
    texts = []
    for item in output if isinstance(output, list) else []:
        if isinstance(item, dict) and item.get("type") == "message":
            text = "".join(str(p.get("text")) for p in item.get("content") or []
                           if isinstance(p, dict) and p.get("text") is not None)
            if text and not text.isspace():
                texts.append(text)
    return "\n".join(texts)


def _owui_fold_chunk(output: list, chunk) -> list:
    """A reply's output as Open WebUI folds its stream: Responses events
    through its own handler, text onto the last message item (a new one
    after any other item). A simplification of middleware.py's text path,
    which also splits out reasoning and code blocks."""
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
    """A one-chat Open WebUI for the turn writer: the chat row behind a real
    lock (Chats._chat_transaction), chat_message rows, the socket server and
    the task registry, installed as the open_webui modules the writer
    imports."""

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
        # ELI-167: which row transactions (1-based) find sqlite's write lock
        # held past busy_timeout; `locked_from` locks every one from there on.
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

        # The page: each sid shows chats (OWUI's SESSION_POOL[sid]["chat_ids"])
        # and its intent hook answers a query with what was clicked.
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
        """What Open WebUI 0.12 saves when a reply's response stream ends or is
        cancelled: `done` and the output it folded from that stream, over
        whatever the message held (utils/middleware.py:6599, :6667)."""
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
    """ELI-100 DESIGN 2.3: opening a run's message: claim it at the tail,
    introduce it to open tabs with the shared `chat:messages` event (M1), and
    register the writer with Open WebUI's task registry so the page shows the
    chat as generating and its Stop button reaches the writer (M4, M5)."""

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



def _delta(text):
    return {"stream": "assistant", "data": {"delta": text}}


def _tool(phase, tcid="t1", name="Bash", result="ok"):
    data = {"phase": phase, "name": name, "toolCallId": tcid, "args": {"command": "ls"}}
    if phase == "result":
        data["result"] = result
    return {"stream": "tool", "data": data}


class TurnWriterLockedDbTests(_TurnWriterCase):
    """ELI-167: Open WebUI's sqlite write lock held past its busy_timeout
    (5 s, WAL, BEGIN IMMEDIATE) when a run opens its message. The open waits
    for the run's next event, or the run's end, and never loses the run; a DB
    that stays locked fails visibly."""

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
        """The claim committed, then setting the task meta met the lock: the
        next open must not read its own claim as another writer's message."""
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
        """ELI-115: each DB write rewrites the whole chat, about 14x its JSON
        in Open WebUI's memory, so a write per item (two per tool call) was
        most of a long chat's churn. A tab that opens the chat mid-turn reads
        the response stream (`overlay_response_streams`), not the DB: the DB
        gets the claim and the end."""
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
        """ELI-166: 0.12.3 dropped `content` from `save_response_stream`
        (the GET rebuilds it from `output`, `get_output_text`). Called the
        0.12.0 way, every writer event raised TypeError and killed the pump:
        no cron wake was written (E2E on 0.12.3, run 38982585148)."""
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
        """The request's own end-of-stream save writes `done: true` over M;
        the handover's write is what says M is still being written, the
        state the orphan reconcile adopts after a crash (ELI-90). Here the
        request has already ended: the write comes at once, then none per
        item."""
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
    """ELI-100 DESIGN 2.5 (end on the run's settled row, from the transcript),
    2.7 (resume / gateway / wait, budget on M) and Stop."""

    RUN = "agent-run-1"
    KILLED = {"status": "failed", "hasActiveRun": False,
              "lastRunError": "Claude Code process exited with code 143"}

    def test_the_resume_says_the_interruption_killed_the_tool_and_asks_to_finish(self):
        """M3 window (2026-10-13 20:47-21:20Z), 4 of 4 graceful restarts: the
        restart killed the Bash child, the transcript showed `Exit code 137`,
        and every resumed run read that as the command failing ("usually the
        kernel OOM killer"), stopped, and asked the user what to do. The
        user's request was never finished. The resume must say why the
        reply was cut off, that a tool call cut off with it was killed by
        that and not by the command, and that the request is to be finished."""
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
        """M3 window run 2 (2026-10-14 01:01Z, media case): the resume ran in
        a NEW claude-cli session (a12b87e4: one user message, the resume), so
        the model never saw the request and answered "I don't have the rest
        of your sequence"; the ask case reran the steps and never asked its
        question. The resume must carry the request it continues."""
        w = await self.opened_writer()
        await w.end(self.KILLED)
        sends = [p for m, p in self.sent if m == "chat.send"]
        self.assertIn("check the logs", sends[0]["message"])

    async def test_a_second_resume_carries_the_first_request_not_the_resume(self):
        w = await self.opened_writer()
        await w.end(self.KILLED)
        first = [p for m, p in self.sent if m == "chat.send"][0]
        # The resume run's own user turn is the directive; it is cut off too.
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

    async def task_done(self, w):
        _, task = self.owui.tasks[w.task_id]
        await asyncio.wait_for(asyncio.shield(task), 1)
        return task

    async def test_an_announce_after_a_silent_parent_does_not_take_the_parents_card(self):
        """ELI-227 (live chat 0a13a1d4 on 2026.10.5): the parent spawned a
        sub-agent and answered NO_REPLY, which leaves no closing message in
        the transcript. The announce run has no user turn either, so its span
        ran back to the parent's sessions_spawn call: its message showed that
        card a second time. The parent's messages are older than the announce
        run's start."""
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

    USAGE = {"description": "🧠 34k/1.0m (3.4%)", "done": True}
    LEFT_BY_THE_CANCEL = [{"description": "Thinking...", "done": False},
                          {"description": "Stopped", "done": True},
                          {"description": "", "done": True}]

    def handed_writer(self, request=None):
        """A Send now (or a send from another tab) mid-reply: the reply's
        request parked its run, saved "Stopped" and an empty line into M's
        status history (pipe.py `park`, then the reply's end), and handed M to
        this writer (W5 chat 1ac3323c, message 0cb40f0e)."""
        shown = _TurnRenderer()
        shown.feed(_delta("Reading them."))
        self.owui.msgs()["m1"] = {"id": "m1", "role": "assistant", "parentId": "q1",
                                  "content": "Reading them.", "done": True,
                                  "statusHistory": copy.deepcopy(self.LEFT_BY_THE_CANCEL)}
        return _TurnWriter(self.conn, session_key=self.KEY, chat_id=self.CHAT, user_id=self.USER,
                           run_id=self.RUN, message_id="m1", takeover=True, renderer=shown,
                           request=request)

    async def test_a_run_the_writer_finishes_ends_with_the_usage_line(self):
        """ELI-210 (W5, QA C20): a reply the writer finished (a wake, a
        sub-agent result, a takeover) had no usage line, which the normal
        reply always shows after its text. Saved with the message, so a
        reload keeps it, and sent to open tabs."""
        w = await self.opened_writer()
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
        self.assertEqual(self.m()["statusHistory"], [self.USAGE])
        self.assertEqual(self.owui.events("status")[-1]["data"], self.USAGE)

    async def test_a_takeover_that_finished_drops_the_cancels_stopped_line(self):
        """ELI-210: the run went on after Send now, so the "Stopped" its
        request saved is wrong: the finished takeover reads like a normal
        reply, "Thinking..." then the usage line."""
        w = self.handed_writer()
        await w.feed(None)
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
        self.assertEqual(self.m()["statusHistory"],
                         [{"description": "Thinking...", "done": False}, self.USAGE])

    async def test_a_takeover_waits_for_its_request_before_it_writes_the_status(self):
        """The cancelled request saves its "Stopped" and empty lines until it
        ends; a status write before that would be appended to, not final."""
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
        """The final write stops waiting for its request after
        FINAL_STATUS_REQUEST_WAIT_S; a write of the request after it could
        bring "Stopped" back, so that is logged, not silent."""
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

    async def test_a_handed_over_reply_resumes_with_the_users_request(self):
        """The writer kept Open WebUI's request task and the user's request
        in one attribute; a handed-over reply cut off by a Gateway restart
        quoted the task object instead of the request it continues."""
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
        """M3 crash window run 2 (2026-10-14 01:16Z, ask case): a Gateway
        recovery asked the window's question; the writer showed it with its
        raw needs-input directive line. The writer has no card (#20): the
        question shows as clean text, as the inline reply's fallback does."""
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
        self.assertIn("429 rate limited", self.m()["content"])
        self.assertNotIn("chat.send", [m for m, _ in self.sent])

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
        """ELI-159 (manager chat 56b98d09, 2026-10-16 07:36:55): one settled
        row reached two writers of the session, 3df09ca2's (its run killed
        at 07:33) and f93028bc's (sessions_send run ad4cc4fc, killed after
        2 s). Both resumed; the second chat.send became a follow-up of the
        first and failed ("no active tool authority snapshot"). Only the run
        the row names is resumed; the other message ends cut off."""
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
        """An inline turn handed over carries the level the user picked
        (ELI-85); the resume continues the same reply at that level."""
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
        """The page cancels the writer's task (Stop or Send now both do);
        its intent hook answers `answer` when asked which button it was."""
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
        """An aborted claude-cli run as the Gateway stores it (chat 0abe9bde,
        run 64028b9f, 2026-10-14 05:37:25): chat.abort writes the streamed
        text as a gateway-injected message, the CLI then writes its own
        aborted closer with the same text, and none of the run's tool calls
        is ever imported."""
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
        """The aborted run's transcript holds its text twice and no tool call.
        Finalizing from it must keep the cards that streamed, with the text
        once, whether the writer's own Stop or the settled row ends it."""
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
        """The killed run left only its user turn (claude-cli never imported
        its text or calls); the run that continues M is complete."""
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
        """Graceful window 2026-10-14 08:03Z (#21 7ee4488, case user, chat
        1a5d9555): the killed run streamed its preamble and one card (Exit code
        137), the pipe's resume ran the steps. Stored content was "...in the
        foreground.USER-DONE-04f06c36": the resume's text glued on with no
        break. The user keeps what the killed run showed, then the resume,
        with the separator the live stream puts after a tool call."""
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
        """Crash window 2026-10-14 08:22Z (#21 7ee4488, case user, chat
        fbc93489): the Gateway's recovery started at boot before the pipe was
        back, so the writer never saw its first text and card. Its transcript
        has all of it; the killed run's card is in no transcript. Keeping the
        whole stream (438428d) lost the recovery's start; replacing it all
        (865f90f) lost the killed run's card."""
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
        """DESIGN case 8: Send now cancels the task too, but the run keeps
        its message to the end; the queued message runs after (case 7)."""
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
    """Review 2af1 of #20 at bbbcc17 (projects/review-pr20/test_review_pr20.py):
    the request a resume quotes (893bac8) must survive a re-created writer,
    a long request, its own rule lines, a metadata block and attachments."""

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
    # Pipe run ids name UUIDs (2af1's test used "c1"/"m1", which
    # parse_pipe_run_id rejects, as it rejects nothing the pipe makes).
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
        """A writer made for a run it did not start: `_adopt_turn_message`
        after a redeploy, or `_turn_writer_for_locked` after an Open WebUI
        restart. Its `runs` holds only that run."""
        w = self.writer(run=run, message_id=self.MID)
        await w.feed(_delta("Step 3 done."))
        return w

    async def test_F1_a_resume_of_a_resume_after_the_writer_is_recreated_keeps_the_request(self):
        """M was resumed once, then the writer was re-created from the resume
        run (redeploy or Open WebUI restart) and the run was cut again: the
        second resume still quotes the user's request, from M's own first run."""
        first = openclaw_pipe._resume_directive("Step 1 done.", self.REQUEST)
        self.history = {"messages": [self._user(self.ORIGINAL_RUN, self.REQUEST),
                                     self._user(self.RESUME_RUN, first)]}
        w = await self.adopted_writer(self.RESUME_RUN)
        await w.end(self.KILLED)
        self.assertIn(self.REQUEST, self.sends()[0]["message"])

    async def test_F1_an_agent_turns_request_survives_a_recreated_writer_too(self):
        """M is an agent turn (a run the pipe did not start): its message id
        is derived from its run id, and its resume names M."""
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
        """How the 9.3 Gateway stores an Open WebUI image turn (live
        transcript, 2026-10-14): string content with the <attached_files>
        listing, the images in `__openclaw.media`, no non-text block."""
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
        """M's own first turn is past the history limit: the first turn left
        naming M is the user's answer to its question (an `input` run), which
        is no request."""
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
        """With #22, a turn is stored with the conversation metadata block in
        front of its text; a resume behind it is still a resume, not the request."""
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
        """The resume runs in a new claude-cli session holding only the
        directive; an image of the request is not carried, so the resume
        must at least say it was there."""
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
        """A pasted log followed by the question: head and tail are kept,
        with a line saying what was left out."""
        request = "LOG LINE\n" * 800 + "Which step failed, and why?"
        d = openclaw_pipe._resume_directive("Looking at the log.", request)
        self.assertIn("Which step failed, and why?", d)
        self.assertIn("LOG LINE\nLOG LINE", d)
        self.assertIn("omitted", d)
        self.assertLess(len(d), 9000)

    def test_F2b_a_request_with_a_rule_line_stays_one_quote(self):
        """2af1's test fixed the delimiter as `---`, which cannot both frame
        the request and be absent from it; adapted to read the delimiter the
        directive chose. The closing line never occurs in the request."""
        for request in ("front matter:\n---\ntitle: x\n---\nRender this page.",
                        "a\n------\nb", "plain request"):
            with self.subTest(request=request):
                d = openclaw_pipe._resume_directive("", request)
                delim, rest = d.split("was:\n\n", 1)[1].split("\n", 1)
                self.assertNotIn(delim, request.split("\n"))
                self.assertEqual(rest.split("\n" + delim + "\n", 1)[0], request)


class _RoutingCase(unittest.IsolatedAsyncioTestCase):
    """A connection wired to a fake Open WebUI chat, fed events and session
    pushes the way the receive loop feeds them."""

    USER = "2d206eab-acac-4945-bd2f-7e8b6365de8c"
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
        """What the receive loop does for one event: the shadow renderer of a
        tracked run is fed first, then the event is routed."""
        shadow = self.conn._run_renderers.get(f"{self.key}:{run}")
        if shadow is not None:
            shadow.feed(payload)
        return _turn_route_now(self.conn, self.ev(run, payload))

    def crashed_then_recovered_history(self):
        """RUN streamed, the Gateway died (no closing turn for RUN), and boot
        recovered it as REC, which ran to the end (M3-crash shape)."""
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
    """ELI-100 DESIGN 2.1-2.5 wired: a run on an OWUI key that no live request
    consumes is written by a turn writer, as one ordinary message."""

    async def test_a_run_with_no_settle_push_ends_on_its_own_final(self):
        """ELI-215, OpenClaw 2026.10.5 probe: a run the Gateway starts itself
        (a background command's end notice, heartbeat runner) gets no
        `agent.input.settled` push: its last row is the lifecycle `phase: end`
        one, still `hasActiveRun: true`, and the row goes idle later with no
        push at all. Its own final ends the message."""
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
        # A settle that does come later (9.3, or a chat.send run) finds it done.
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.RUN})
        await self.spin()
        (_mid, again), = self.new_messages().items()
        self.assertEqual((again["content"], again["done"]), ("E2E_BG_NOTICED", True))

    async def test_a_run_that_ends_in_error_still_waits_for_its_row(self):
        """Only a clean final ends the message on its own: an error's row
        carries the note and may name a resume or a recovery (DESIGN 2.7)."""
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
        # The interrupted run streamed, then the Gateway crashed: the row is running.
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
        """Case 4: the recovery ran and ended while this socket was down, so
        none of its events arrived. Its settle (or the row read after hello)
        ends RUN's writer, and the finalize takes the recovery from the
        transcript by rule E."""
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
        """No settle reached this socket (it was down). The first hello's
        snapshot does not list the session as active, so its row is read and
        ends the writer: no wait for a push that already happened."""
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
        """Review 2af1 Strong5: the transcript rebuilds the full text and hid a
        dropped shadow renderer. With no transcript only the renderer can
        supply what the request already showed."""
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
        """ELI-115: the request's end-of-stream save (`done: true`) can land
        after the writer's first stream. M's handover write waits for the
        request's task to end, so the DB says `done: false` mid-turn, the
        state the orphan reconcile adopts after a crash, with no later write
        needed to say it again."""
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

    async def test_handover_an_inline_reply_that_ends_early_continues_in_its_own_message(self):
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB, content="Reading them.")
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB)
        self.conn._run_renderers[f"{self.key}:{run}"].feed(_delta("Reading them."))
        # The request is gone (no consumer): the run's next events.
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
        """ELI-147: a Send now reply adopted a run whose start reply 1 already
        showed (lines 1-10), streamed lines 11-20 into its own message, and
        was handed over (a Gateway restart, a socket gap). The run's closing
        transcript holds lines 1-30; the rebuild of reply 2's message must
        start where reply 1 left off."""
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
        """2026-10-12 23:29Z, chat 0abe9bde (fails on main 787a471, where it
        was `test_relay_writes_into_the_stub_its_run_id_names_when_history_fails`):
        Open WebUI was OOM-restarted mid-turn, so no request and no target
        remain. chat.history failed with an empty TimeoutError, and main's
        relay introduced a second message 32e095ae under the empty done=False
        stub 8acdce95. The run id names its message: no transcript read is
        needed to know where it goes."""
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
        """Condition B: a run whose agent events carry isHeartbeat:true
        (server-chat-CbnqM5f6.mjs:791, 823-826) renders nothing, including
        its later events that come without the flag (session.tool)."""
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
        """Review 2af1 N6: an answer run's early event, before its reply
        registered for it, is held, not handed to a turn writer, and reaches
        the reply's queue when it registers."""
        q_run = pipe_run_id(self.CHAT, self.STUB)
        live = self.conn.register_consumer(self.key, q_run)
        self.addCleanup(self.conn.unregister_consumer, self.key, q_run, queue=live)
        a_run = pipe_run_id(self.CHAT, self.STUB, "input")
        self.assertTrue(_turn_route_now(self.conn, self.ev(a_run, _delta("Reading a.py."))))
        self.assertEqual(self.conn._turn_pumps, {})
        q = self.conn.register_consumer(self.key, a_run)
        self.addCleanup(self.conn.unregister_consumer, self.key, a_run, queue=q)
        # As a frame: the inline loop reads `msg["payload"]` (a bare payload
        # was read as an empty event and its text dropped).
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
        """Review 2af1 N7: the release routed the held events in a background
        task, and the run's next event, routed at once, overtook them."""
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
        """Review 2af1 N8: the ask-user switch unregisters the question run and
        registers the answer run in one step; the release in between must not
        route or lose what was held for the answer run."""
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
        """Unreachable through register_consumer, which takes held events
        first; forced here so the guard is proven to count and deliver."""
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
    """DESIGN section 6: each fails on main 787a471 (no writer exists there)
    and passes here."""

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
        """TwoConnectionsOneMessageTest: two live connections (a deploy's old
        and new one) get the same run; the chat row lock lets one claim M."""
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
        # The deterministic uuid5 gives one message even if both wrote it:
        # count the writers' own effects (review 2af1, Strong2).
        n_tasks = len(self.owui.tasks)
        n_intro = len(self.owui.events("chat:messages"))
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
        """ToolCardsFromTranscriptTest: events after the first tool call were
        lost; M ends with the transcript's cards."""
        _turn_route_now(self.conn, self.ev(self.RUN, _tool("start", tcid="toolu_0")))
        self.history = self.tool_history(self.RUN, 3)
        self.settled(self.RUN, status="done")
        await self.spin()
        (_mid, m), = self.new_messages().items()
        self.assertEqual((self.cards(m), m["content"], m["done"]), (3, "Done.", True))

    async def test_user_turn_with_no_tab(self):
        """UserTurnNoTabTest (M3 dry run, 2026-10-12): a user turn sent through
        the API, request gone before its run: one message M, 5 cards."""
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
        """The run streams, its terminal arrives with the active-run overlay
        push, then the settled row (dist-shaped, DESIGN 2.7)."""
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
        """No terminal ever arrives; after hello the session is active (the
        recovery runs): nothing is sent and M stays open. The fake answers
        sessions.list with the crash-time row and accepts chat.send, so a pipe
        that ignored the active snapshot would reach the owner rule and resume
        (review 2af1, Strong4: the old fake raised, and the pipe swallowed it)."""
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
        """CrashDoubleContinuationTest (M3-crash, user turn): on main the pipe
        also sent resume-..., queued behind recovery f2445aa8, and the reply
        closed on its immediate empty final. Here: no pipe resume, and M gets
        the recovery's card and text."""
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
        """SeqGapRecoveryTest: the Gateway drops a frame for a slow client and
        only the per-socket frame seq shows it (server-start-BB-IxTAg.mjs:
        3205-3207). The settle push was the dropped frame: the row is read and
        the writer ends. A run whose start was dropped still gets its message
        from its next event."""
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
        """The receive loop drops events flagged isHeartbeat before routing;
        the run's session.tool copies carry no flag and must not open M."""
        hb = {"type": "event", "event": "agent",
              "payload": {**self.ev(self.RUN, _delta("checking")), "isHeartbeat": True}}
        tool = {"type": "event", "event": "session.tool",
                "payload": self.ev(self.RUN, _tool("start", tcid="toolu_1"))}
        await self.scripted_loop(self.conn, [hb, tool])
        self.assertEqual(self.new_messages(), {})

    # Review 2af1 R7: a heartbeat run is known only by the isHeartbeat flag on
    # its agent events. Its chat events never carry it (server-chat-CbnqM5f6
    # emitChatDelta / emitChatTerminal build their own payload), and a frame
    # can come before the run's first flagged one: a chat delta (both are
    # buffered and flushed apart), or a session.tool copy, which the review
    # names (on 2026.9.3 the Gateway suppresses a heartbeat's tool events,
    # :833, :934, but an earlier or later Gateway may not).

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
        """The other side of R7: an unflagged first frame is held, not
        dropped, and goes out first once the run is known not to be a
        heartbeat."""
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
        """Events lost after a reconnect: only session.tool copies reached
        this connection. The settled row releases them; a heartbeat would
        have been flagged by its lifecycle end, which precedes the settle."""
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
    """ELI-173, CI run 39056959056: `docker stop` mid-reply. Open WebUI's
    shutdown cancelled the reply and saved it partial and done:true; the run's
    next word reached the still-open Gateway connection, which handed it to a
    turn writer. The writer took the message over and saved it done:false,
    and the process exited before its end. The next process's startup
    reconcile owns every reply of a shutdown (ELI-90, ELI-94): a dying process
    writes none."""

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
        """Control: the handover above is real (DESIGN 2.6), so the test
        reaches the code that reopened the reply."""
        run = self.stopped_reply()
        await self.dispatch(run, _delta(" slow-2"))
        await self.spin()
        self.assertIn((self.key, run), self.conn._turn_pumps)
        self.assertIs(self.owui.msgs()[self.STUB]["done"], False)


class HandoverRewriteTests(_RoutingCase):
    """Review 2af1 R4 (P28), R6, R9: a handed-over reply ends as the inline
    reply showed it. The inline reply turns a `MEDIA:` directive into an image
    and an answered ask-user prompt into a card; the writer rebuilds from the
    shadow renderer and the transcript, which hold the raw text. Main kept the
    whole turn inline, so losing either after a handover is a regression."""

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
        """R9: the answer run continues the reply; a handover of it must keep
        the question run's text and the card the reply showed for it."""
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
        """The reply moved on to the answer run; the question run's trailing
        final must not hand M over while the reply still streams."""
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
        """No target survives an Open WebUI restart: the transcript names
        every pipe run of M, in order."""
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

    # Known gap of PR #20: the card for an answered prompt was a rewrite the
    # inline reply kept in the Open WebUI process. After an Open WebUI restart
    # the writer rebuilt M from the transcript and showed the raw prompt. The
    # transcript holds both halves: the prompt ends the question run's text,
    # and the answer is the answer run's own user turn.

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
        """A report that quotes a prompt in a code fence asked nothing
        (ELI-107): the next run's text is no answer to rebuild a card from."""
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
        """An agent turn's prompt gets no modal from the writer: it shows as
        the inline reply's fallback does, the question without the internal
        directive line (the writer-side half of ELI-58)."""
        self.history = {"messages": self.closed_run(self.RUN, "Before I go on:\n" + self.PROMPT)}
        await self.dispatch(self.RUN, _delta("Before I go on:\n" + self.PROMPT))
        await self.dispatch(self.RUN, {"state": "final"})
        await self.settle(self.RUN)
        (_mid, m), = self.new_messages().items()
        self.assertEqual(m["content"], "Before I go on:\nColor?\nPick one\n1. Red\n2. Blue")

    async def test_a_routed_handover_keeps_the_turns_thinking_level(self):
        """R6: the handover that `_turn_route_now` makes (the request ended
        first) carries the level like `_hand_over_run` does, so a resume keeps it."""
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB)
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB, thinking="high")
        self.conn._run_renderers[f"{self.key}:{run}"].feed(_delta("Reading."))
        await self.dispatch(run, _delta(" More."))
        await self.spin()
        self.assertEqual(self.conn._turn_writers[(self.CHAT, self.STUB)].thinking, "high")


class WriterQuestionCardTests(_RoutingCase):
    """ELI-107 decision 1, option 1 (#21): a run the writer shows that ends
    with a question asks it as Open WebUI's question card on the tab viewing
    the chat, once, for as long as that card call lasts. The answer
    continues the reply as an `input` run and the question becomes its Ask
    User card, as in the inline reply. No tab on the chat, a cancel or a
    timeout: the question stays as text and the reply ends."""

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
        """The inline reply asked and recorded its card, then handed the
        answer run over: that run's end asks nothing."""
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
        """Review 2af1 F4 on the writer: fences are tracked per message item."""
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
    """The inline reply records each rewrite the writer must repeat, and moves
    its target to an answer run (R4, R9). Checked on the source: the inline
    loop has no unit harness (e2e drives it)."""

    def test_the_inline_reply_records_its_rewrites_and_answer_runs(self):
        src = inspect.getsource(openclaw_pipe._PipeTurn)
        # One answer switch: a prompt in an item event is held and asked
        # when the run ends, as one in the assistant stream is (ELI-147).
        self.assertEqual(src.count("switch_to_answer_run(answer_res)"), 1, "the answer switch")
        self.assertEqual(src.count("answer_res.queue or self.conn.register_consumer("), 1)
        # Review 2af1 N6: the answer run is listened for, and its target moved,
        # before its chat.send.
        ask = src.split("async def maybe_answer_user_input", 1)[1]
        send = ask.index("RUN_SEND")
        self.assertLess(ask.index("conn.register_consumer(self.session_key, idempotency_key)"), send)
        self.assertLess(ask.index("conn.continue_run_target(self.session_key, self.our_run_id, idempotency_key"), send)
        self.assertGreaterEqual(src.count("conn.note_turn_rewrite("), 4,
                                "2 MEDIA sites, 1 answered card, 1 fallback prompt")
        self.assertIn("thinking=self.resolved_thinking", src.split("conn.register_run_target(")[1][:200])



class SupersededLoadTests(unittest.IsolatedAsyncioTestCase):
    """ELI-102 (chat 56b98d09, bubble d04f9254, 2026-10-13 01:52:57Z): a turn
    an older load of the Function admitted met that load's retired connection
    and was lost as "gateway connection retired". The current load serves it."""

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
    """Review 2af1 N5: a reply read slower than its run streams keeps its
    queue full. A row, gap or restart marker for it then raised QueueFull in
    the receive loop, and the gap marking in its handler raised again: the
    loop died and every run on the connection hung."""

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
    """Review 2af1 N9: the receive loop may be waiting to put an event into a
    full queue. A row a resync task puts must not pass that event: with no
    closer, the row ends the reply and the event is lost."""

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
    """Review 2af1 timers: an inline reply ends on its run's own signals, never
    on silence. The Gateway settles every run with its own row
    (`sessions.changed`, reason `chat.run.settled`, hasActiveRun false,
    lastRunId = the run; live probe runs/eli103-probe 2026-10-13), and a
    queued send shows `status: queued` on the row. The 60 s first-event wait,
    the 180 s deadman, the 30 s idle probe and the 45 s describe poll stood in
    for those signals."""

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
        """The socket was down when the run ended: its final and its settle
        are lost, and a later run moved lastRunId on. The row read after the
        reconnect is idle, so nothing of this run is still coming."""
        await self.delta("Before the drop.")
        self.history = {"messages": self.closed_run("Before the drop. After it.")}
        self.rows = [{"key": self.sk, "hasActiveRun": False, "status": "done",
                      "lastRunId": "a-later-run"}]
        await _turn_resync(self.conn)
        text = await self.ended()
        self.assertIn("After it.", text)

    async def test_after_a_gap_a_later_runs_settle_ends_the_reply(self):
        """Review 2af1 N3: the final and the settle were lost in a gap, and a
        later run kept the session busy at the resync. The first idle row
        after it ends the reply, though it names the later run."""
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
        """Review 2af1 N4: a resync read the row before chat.send returned."""
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
        """N4: idle after the admission, nothing of this run seen, and no
        closer under its id: the session's last reply is not this one's."""
        self.history = {"messages": [
            {"role": "user", "content": "hi", "idempotencyKey": "msg-previous:user"},
            {"role": "assistant", "content": [{"type": "text", "text": "Yesterday's answer."}],
             "idempotencyKey": "cli-assistant:msg-previous"}]}
        self.rows = [{"key": self.sk, "hasActiveRun": False, "status": "done",
                      "lastRunId": "msg-previous"}]
        await _turn_resync(self.conn)
        self.assertNotIn("Yesterday", await self.ended())

    async def test_the_reply_listens_before_it_sends(self):
        """Review 2af1 R12: a run's first events can arrive before chat.send
        returns. With no consumer yet they went to the turn writer, which took
        the reply's message over while the reply still streamed."""
        self.assertEqual(self.consumers_at_send, [f"{self.sk}:{self.run}"])

    def test_no_silence_timer_is_left_in_the_inline_loop(self):
        src = inspect.getsource(openclaw_pipe._PipeTurn)
        for timer in ("idle_probe_s", "no_text_deadman_s", "describe_check_interval",
                      "recv_timeout"):
            self.assertNotIn(timer, src)


class TitleRunSignalTests(unittest.IsolatedAsyncioTestCase):
    """Review 2af1 timers: the auto-title run is read from its own events (its
    assistant text and final) or its settle row, not by polling
    `sessions.preview` every 3 s for ~21 s, which dropped the title whenever
    the title agent took longer."""

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
    """Review 2af1 R10: a sub-agent's completion reaches the parent as an
    announce run, id `announce:v1:<childSessionKey>:<childRunId>`
    (announce-idempotency-D7LnUTJR.mjs:4-10), whose closing message is
    `cli-assistant:<that id>` and which has no user turn in chat.history
    (live probe runs/r10-probe, 2026-10-13). When the parent answers it with
    NO_REPLY, nothing showed the child's result."""

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

    # Known gap of PR #20: with `cleanup: "delete"` the Gateway deletes the
    # child once the announcement is delivered (subagent-announce-BIG31XbZ.mjs
    # :413, in the flow's `finally`), so the card read at the end found no
    # transcript. The child's result is caught before that: its own chat final
    # (broadcast to this connection, which has no session-scoped cap, with
    # `spawnedBy` = the parent, server-chat-CbnqM5f6.mjs emitChatTerminal), or
    # its transcript read when the announce run starts.

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
        """No child final reached this connection (a socket gap): the
        transcript is read as the announce run starts, before the delete."""
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
        """`announce:requester-settle:...` wakes a yielded parent for a batch
        of children; its own silence is the parent's, not a missing result."""
        self.announce = f"announce:requester-settle:main:{self.key}:{self.CHILD_RUN}"
        await self.announce_run("NO_REPLY")
        self.assertEqual(self.new_messages(), {})

    # ELI-217: on 2026.10.5 a NO_REPLY to a completion is an empty response.
    # The run fails ("Agent couldn't generate a response"), the Gateway retries
    # the delivery on a backoff, and the session's last push for the run is
    # `phase: error` with hasActiveRun still true: no settle is pushed, though
    # sessions.list already reads the row failed (s105 probe, 2026-10-19).

    ERROR_TEXT = "⚠️ Agent couldn't generate a response. Please try again."

    def failed_row(self, active=False):
        return {"key": self.key, "hasActiveRun": active,
                "status": "running" if active else "failed", "lastRunId": self.announce,
                "lastRunError": None if active else self.ERROR_TEXT}

    def list_rows(self, *rows):
        """sessions.list answers with these rows in turn, the last one again."""
        rows, parent = list(rows), self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method == "sessions.list":
                self.sent.append((method, copy.deepcopy(params)))
                return {"sessions": [rows.pop(0) if len(rows) > 1 else rows[0]]}
            return await parent(method, params, timeout)
        self.conn.send_request = send_request

    async def failed_announce_run(self):
        """The 10.5 run: no text, its lifecycle error, its chat error, and only
        the `phase: error` push (hasActiveRun true)."""
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
        """A parent on the Gateway's own runtime records the spawn as a
        `toolCall` block and a `toolResult` message (ELI-217: the card was
        named after the child's session key)."""
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
        """On 2026.10 a native run calls `sessions_spawn` through `tool_call`
        ({id, args}) when the tool is deferred; the card was named "Sub-agent"."""
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
        """The Gateway retries the delivery under the same run id: a retry the
        parent answers again with silence adds no second card, and the one it
        answers aloud lands once, in a message of its own."""
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
        """An attempt answered aloud whose last push is `phase: end` with
        hasActiveRun still true (2026.10.5, ELI-215): it ends on its final."""
        self.history = self.parent_history(reply)
        _turn_route_now(self.conn, self.ev(self.announce, _delta(reply)))
        _turn_route_now(self.conn, self.ev(self.announce, {"state": "final"}))
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "end",
                                        "runId": self.announce, "hasActiveRun": True,
                                        "status": "running", "lastRunId": self.announce})
        await self.spin()

    async def test_a_retry_answered_aloud_with_no_settle_push_lands_once(self):
        """ELI-215 with ELI-217: the retry that answers aloud gets no settle
        either, so its own final ends its message, once, beside the one card."""
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
        """A second attempt under the run id of one that already ended on its
        final keeps the one message: done, with the first attempt's text, as
        the settle path does on main (the message id is the run's). 10.5 never
        retries a delivery that sent a visible reply (`hasAnnounceSendEvidence`,
        subagent-announce-delivery-retry-BwPVtckd.mjs), so this guards a
        Gateway change, not a path seen live."""
        await self.unsettled_announce_run("First answer.")
        await self.unsettled_announce_run("Second answer.")
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.announce})
        await self.spin()
        self.assertEqual([(m["content"], m["done"]) for m in self.new_messages().values()],
                         [("First answer.", True)])
        self.assertEqual(self.conn._turn_pumps, {})


class ErroredAgentRunTests(_RoutingCase):
    """ELI-217: a run the agent started that errors on 2026.10.5 gets no
    settle push (its last push is `phase: error`, hasActiveRun true), so its
    message stayed open and its pump waited forever. Its error event reads
    the row once; an idle row ends the message, an active one waits."""

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
    """ELI-218: a run on the session of a chat deleted in Open WebUI (a
    sub-agent's late result, a cron job, a background command's notice) found
    no chat row, and the turn writer read `chat.chat` off None: every event of
    the run started a pump that failed again (six on the 2026-10-19 gate)."""

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
    """Review 2af1 R15: the page hook told Stop from Send now by the English
    aria-label, so in any other UI language a Stop read as "unknown" and the
    run kept going. It now also matches the Stop button's icon, which is not
    translated. The fixture holds Open WebUI's button templates labelled
    t("Stop") (scripts/extract_owui_stop_button.py, re-extract on upgrade)."""

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
        """Runs the hook under node with a stub page: a click on the fixture's
        composer Stop button labelled in Hebrew records "stop"."""
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
    """ELI-198: Open WebUI's Stop cancels every task of the chat at once: a
    reply waiting behind a run (after Send now, or queued) and the turn
    writer holding that run. Each asks the page which button it was. The
    query read and cleared the chat's record, so the first to ask got "stop"
    and the writer got "none": its run went on. Runs the intent hook and its
    query under node with a stub page."""

    def run_page(self, steps: list) -> list:
        """Plays `steps` on a page with the hook: ["stop"] clicks the
        composer's Stop (a reply runs), ["submit", running] submits the
        composer, ["query"] runs the intent query. The queries' answers."""
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
    """AUTO_SEND_WHILE_RUNNING: in an OpenClaw chat, a message Open WebUI
    queues while a reply runs is sent at once by clicking its Send now, marked
    as Send now so the run is kept. Runs the page hooks under node with a stub
    page: Open WebUI's queued item (its Send now arrow) and its user messages."""

    SEND_NOW_ICON = "M4.5 10.5 12 3m0 0 7.5 7.5M12 3v18"

    def run_page(self, chat_flags: dict, steps: list, path="/c/chat-1") -> dict:
        """Installs the hooks once per entry of `chat_flags` (as several turns
        would), then plays `steps`: ["queue"] adds a queued item, ["user", id]
        adds a user message, ["mutate"] fires the observer. Returns the clicks
        (in order, by queued item) and the recorded intents."""
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
        """Open WebUI ignores a Send now while it is still submitting the
        previous one, so the second click waits for that user message."""
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
        """A chat on another model gets no hooks from the Pipe, so no flag."""
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
        """Live gate 2026-10-13 (chat a72bba4c): a sub-agent announce run
        (`announce:v1:...`) has no `<id>:user` turn. Its span ran back to the
        parent run's user message and took the parent's sessions_spawn card
        and closing message, so the announce message showed a card it never
        made. Same span on main (_proactive_run_output)."""
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
    """ELI-164: OpenClaw 2026.10.10 rows across a restart, read every 100 ms in
    the E2E stack (probe 2026-10-17): the session of a run the crash cut reads
    idle with no status from the moment the Gateway listens until its recovery
    pass marks it, about 8 s later; then `interrupted` with abortedLastRun;
    then `running` with the recovery run. 2026.9.3 reads `running` throughout."""

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
    """ELI-164: on 2026.10.10 the recovery run's own `:user` turn is committed
    about 0.7 s after its lifecycle start (probe 2026-10-17: start 26.11 s,
    `session.message` 26.84 s, turn readable 26.89 s), so chat.history read on
    its first event cannot name the run it recovers yet."""

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
        # Read after hello, before the recovery pass: idle, no status.
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
        """RUN streamed "Reading them.", then the Gateway died and came back
        (`version` from hello-ok.server.version)."""
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
        """9.3 rows keep `running` across a restart (persisted at lifecycle
        start, session-lifecycle-state-C7hBzJwC.mjs:77/102) and the recovery
        pass marks within ms of listening. A no-status row there is a session
        whose start never persisted, which 9.3 never recovers and never pushes
        again: M must not wait for it."""
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
        """The give-up tombstones the row to failed and appends a notice
        (9.3 main-session-restart-recovery-CPJjd8da.mjs:1135-1153, 10.10
        -DnTvOdiE.mjs:1040-1062) but pushes no row: the notice's
        `session.message` is what re-reads it."""
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
        """A run with no admitted turn (a queued follow-up, M3-crash 04351f67)
        while M waits: its end resolves it, to its own message."""
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

if __name__ == "__main__":
    unittest.main()


class EventGapTests(unittest.IsolatedAsyncioTestCase):
    """Events lost while the Gateway socket reconnects (no replay exists:
    sequence numbers are per connection)."""

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

class NeedsInputMarkerTests(unittest.IsolatedAsyncioTestCase):
    """ELI-107 (2026-10-14 02:07Z, chat 0abe9bde): a report that QUOTED a
    needs-input block in a fenced code block, after 48 tool calls, was held
    as a live prompt from the fence on: nothing more streamed, the card's
    first call failed (the turn came from an API session, no socket), and
    the request polled for the user's "reconnect" for up to an hour. The
    Gateway run had ended at 02:07Z; the stream ran until it was stopped by
    hand at 02:16Z, the stored message empty and done:false."""

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered
    MARKER = openclaw_pipe._USER_INPUT_TRIGGER_PREFIXES[-1]
    REPORT = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                         "test_fixture_eli107_report.json")))["text"]

    async def _stream(self, text, event_call, chunk=40):
        """One run streams `text` in `chunk`-sized deltas, then ends; returns
        what the reply showed (its text chunks)."""
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

    # Review 2af1 F4: fences as CommonMark opens and closes them (4.5): a
    # backtick fence's info string has no backtick, only the same character
    # at least as long closes it, and a fence never spans message items.
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

    # ELI-142: an unanswered card ends the reply with its question as text.
    # The incident's form was JSON with three Hebrew questions, which showed
    # as the raw JSON body.
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
        # The text form keeps its own text, marker line stripped, as before.
        self.assertEqual(self.ocp._ask_user_fallback_text(self.MARKER + "\nColor?\n1. Red\n2. Blue"),
                         "Color?\n1. Red\n2. Blue")

    async def test_F4_a_fence_left_open_before_a_tool_call_does_not_hide_the_next_question(self):
        """The text after a tool call is a new message item: a fence the
        previous item left open (cut off) does not continue into it."""
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
        """A real prompt, but the turn's session is not a live socket (OWUI
        answers the first call `Client session disconnected.` at once): the
        reply ends now with the question as text, it does not wait an hour
        for a reconnect."""
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


class ToolStatusEventTests(unittest.IsolatedAsyncioTestCase):
    """ELI-115: Open WebUI saves every `status` event by loading and rewriting
    the whole chat (`add_message_status_to_chat_by_id_and_message_id`), about
    14x the chat's JSON in memory. A "Running X..." / "X done" pair per tool
    call made 26,746 of those writes in two days, up to 372 in one reply. The
    tool cards already say running and done, so a reply's statuses are its
    "Thinking..." and its final done, however many tools it calls."""

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _reply = NoDroppedMessagesTests._reply
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered

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
        # The cards still say it: each call shows running, then its output
        # flips it to done (`call_id or id`, as structuredOutput.ts reads it).
        cards = {}
        for c in got:
            item = c.get("item") if isinstance(c, dict) else None
            if item:
                cards.setdefault(item.get("call_id") or item.get("id"), []).append(
                    (item["type"], item.get("status")))
        for tcid in ("toolu_1", "toolu_2"):
            self.assertEqual(cards[tcid], [("function_call", "in_progress"),
                                           ("function_call_output", "completed")])


class InlineOwnerRuleTests(unittest.IsolatedAsyncioTestCase):
    """ELI-100 DESIGN 2.7 for the inline user turn (item 1 of the order).

    M3-crash (2026-10-13 00:33Z, user turn): the Gateway recovered the run
    (f2445aa8) and main's inline loop ALSO resumed it on the epoch bump; the
    resume was queued behind the recovery as a follow-up, failed 67 ms after it
    ("no active tool authority snapshot"), and the reply closed empty. An
    interrupted inline turn therefore decides nothing itself: it ends its
    request and hands M to the turn writer, whose settled row decides by the
    owner rule. A real error and a Stop end the reply as before."""

    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _until_registered = NoDroppedMessagesTests._until_registered
    # A handover needs a session key the connection can parse back.
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
        """A user turn whose run streams one delta, then `steps`: an event for
        its queue, or a callable acting on the connection."""
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
        # Bounds the test only: a turn that waits for an idle probe hangs here.
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
        """Open WebUI's post-stream sweep (middleware.py, Responses API path)
        executes every `function_call` of the stream whose `call_id or id` has
        no `function_call_output`, then calls the model again with the result.
        For our cards that is `Error: Tool "Bash" not found.`, sent back to
        this pipe as a new turn."""
        handled = {i.get("call_id") for i in items if i.get("type") == "function_call_output"}
        calls = [i.get("call_id") or i.get("id") for i in items if i.get("type") == "function_call"]
        self.assertTrue(calls, "the test must stream a tool card")
        self.assertEqual([c for c in calls if c not in handled], [])

    async def test_a_crash_mid_tool_leaves_no_card_for_open_webui_to_run(self):
        """M3 crash window (2026-10-13 21:21Z, user and ask cases): the
        Gateway was killed while Bash ran. The request ended with the card
        open, Open WebUI ran "Bash", got `Error: Tool "Bash" not found.` and
        sent it back; that turn became a third run whose stream overwrote the
        writer's finished reply (the Gateway's recovery, 6 tool cards)."""
        items = await self._turn_items(self._tool_start(), self._restart)
        self.assertEqual(len(self.handed), 1)
        self.assert_no_call_left_for_open_webui(items)

    async def test_a_settled_row_read_before_the_kill_reaches_the_reply_still_decides(self):
        """ELI-159 (manager chat 56b98d09, 2026-10-16): claude-cli was
        OOM-killed at 07:33:14 and the reply handed 3df09ca2 over at 07:33:26.
        The settled push had come in between, when no pump was there to take
        it, so the writer waited for the session's next settle, another run's
        3.5 minutes later. The handover reads the row itself."""
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
        """A new boot id after the reconnect, no terminal: the inline turn
        must not resume (the Gateway recovers a `running` row, F1)."""
        out = await self._turn(self._restart)
        self.assert_handed_over(out)

    async def test_container_stop_exit_143_hands_over(self):
        """`ha apps restart`: the CLI child exits 143 before the Gateway
        closes (M3 window 1). The writer reads the `failed` row and resumes."""
        out = await self._turn(self._lifecycle_error("Claude Code process exited with code 143"))
        self.assert_handed_over(out)

    async def test_gateway_stop_abort_hands_over_with_the_notice(self):
        out = await self._turn(lambda: self.conn._on_shutdown({"reason": "gateway stopping"}),
                               self._aborted())
        self.assert_handed_over(out, stop_notice_before=True)

    async def test_restart_mode_abort_hands_over_to_the_recovery(self):
        """In restart mode the aborted row stays `running`: the writer keeps M
        open for the Gateway's recovery run."""
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
        self.assertIn("**Error:** 429 rate limited", out)

    async def test_a_cli_exit_1_is_not_an_outside_kill(self):
        """Main's `_is_restart_signature` matched any `process exited with
        code` and resumed a CLI that failed on its own."""
        out = await self._turn(self._lifecycle_error("Claude Code process exited with code 1"))
        self.assert_not_handed_over()
        self.assertIn("**Error:** Claude Code process exited with code 1", out)

    async def test_the_handover_carries_the_thinking_level(self):
        """The writer's resume must run at the level the user picked (ELI-85),
        as main's inline resume did."""
        await self._turn(self._restart)
        (_s, _r, pump), = self.handed
        self.assertIn("thinking", pump.handover)

    async def test_a_handover_after_a_restart_reads_the_row(self):
        """The restart marker can reach the inline loop after the post-hello
        resync already ran; the handover then reads the row itself."""
        await self._turn(self._restart)
        self.assertIn("sessions.list", self.rpc)



class BackgroundAgentLineTests(unittest.IsolatedAsyncioTestCase):
    """ELI-163: a Claude Code Agent call started in the background holds its
    reply open until its report is in, and the reply looked busy for no
    reason. Its status row says why, until the reply ends."""

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
        """ELI-186: Open WebUI saves a reply's tool cards only when it ends,
        so mid-reply its row (and the chat) hold none. The Status Action
        lists them from the Pipe streaming the reply, in the same process,
        until the reply ends; then from what Open WebUI saved."""
        import openclaw_status_action as action
        launched = "Async agent launched successfully. agentId: a1 (internal metadata)"
        # The chat as Open WebUI holds it mid-reply: the reply, done:false,
        # with no output yet.
        held = {"history": {"currentId": "m1", "messages": {"m1": {
            "id": "m1", "parentId": None, "role": "assistant", "done": False, "content": "", "timestamp": 5}}}}
        chats = types.ModuleType("open_webui.models.chats")
        chats.Chats = types.SimpleNamespace(
            get_chat_by_id=lambda chat_id: types.SimpleNamespace(user_id=self.USER, chat=held))
        # The Pipe and the Action are two modules; they meet on Open WebUI's.
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
    """ELI-147 slice 4: events lost on the Gateway socket (a dropped frame, a
    drop and reconnect) end the user's reply and hand its message to the
    turn writer, whose transcript rebuild puts back what was lost, tool cards
    included. Replayed through the real request, the real receive loop on a
    scripted socket, and the writer writing into Open WebUI. Gap repair put
    back lost text only, and a lost card stayed lost."""

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
                # The row as the Gateway reads it now, but its response comes
                # over the socket behind the frames already sent: an idle row
                # never overtakes the run's own final.
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
            # A new socket after the hello: its frame seq starts again, and
            # the loop renews the subscriptions (`_subscribe_all`).
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

    async def start_reply(self, text="count", message_id=None, intent=None, sends=True):
        """A user turn's request, running until its run is sent (or, for a
        reply that takes over a parked run, until it consumes it)."""
        message_id = message_id or self.STUB
        self.add_stub(message_id)
        emitted = []
        emitter = RecordingEmitter(emitted)

        async def event_call(event):
            return intent
        body = {"model": "openclaw_gateway.main", "messages": [{"role": "user", "content": text}]}
        meta = {"chat_id": self.CHAT, "user_id": self.USER, "message_id": message_id}
        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta,
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
        """Frames the Gateway sent while this client was slow, or the
        socket was down: their seq numbers pass, nothing arrives."""
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

    async def test_case_1_frames_lost_mid_run(self):
        """A slow client: the Gateway dropped the frames of a whole tool call
        (only the frame seq shows it). The text around it all arrived."""
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
        """The call's result, the closing text, the final and the settle were
        all sent while the socket was down. The row read after the reconnect
        is idle."""
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

    async def test_the_98891cbb_shape_a_drop_right_after_the_send(self):
        """2026-10-12, chat 98891cbb: the socket dropped right after the reply
        registered, so lines 1-3 never arrived. The message gets the whole
        run once, from the writer. The request, which showed nothing, leaves
        it alone: no partial save into the writer's message, no transcript
        recovery and no thinking retry (those are for a reply with no text)."""
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

    # Real Open WebUI message ids: a pipe run id names its message, and only
    # a uuid there makes the run the Pipe's own (`parse_pipe_run_id`).
    M1 = "11111111-1111-4111-8111-111111111111"
    M2 = "22222222-2222-4222-8222-222222222222"

    async def _send_now_mid_tool(self, cancel_event=False):
        """Reply 1 streams 1..10 and starts a tool call; Send now; reply 2
        brings MSG2. Returns reply 2 and the first run's id. `cancel_event`:
        before MSG2, everything the cancel set off runs, then Open WebUI's
        `chat:tasks:cancel` reaches the tab (`self.cancel_at` is its place
        among the emits, `self.cancelled_output` its output)."""
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
            # The page answered Send now. Either the run is held for MSG2, or
            # it was taken over at once and the writer has streamed it.
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
        """run's turn as the Gateway stores it on claude-cli: the user
        message, the imported assistant content, the closing message."""
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
        """What a tab that opens the chat now is shown mid-turn: the latest
        response stream of the message (Open WebUI's `overlay_response_streams`
        over the DB copy)."""
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
        """ELI-158: Send now while a tool runs. The run stays in its own
        message, its card running until the tool really ends there; the new
        message waits for the run and gets only its own answer. Before, the
        cancelled message showed the card done and the run went on in the
        next reply."""
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
        """ELI-158 screenshots (run 38880792397): after Send now the tab
        showed the reply's text twice. Its message items carry Open WebUI's
        ids, so the writer's first `output_item.added` spliced in a second
        copy. The tab folds what the reply streamed, then the writer's events
        (the vendored frontend reducer): each part shows once."""
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
        """#83 Screenshots (runs 38912098830, 38914753295): after Send now the
        kept run's card showed done for 20 s while its tool still ran. Open
        WebUI ends a cancelled reply by sending the tab `chat:tasks:cancel`
        with its own copy of the output, a running tool call stamped
        `incomplete` (drawn done), and the tab replaces the message's output
        with it. It sends that after the Pipe's cancel handling returns, and
        the page's Send now answer can come sooner: the writer that took the
        message over then had its first events overwritten, and sent nothing
        more for the card until the tool ended. The tab folds Open WebUI's
        cancel output once everything the cancel set off has run, then the
        writer's events: the card is running."""
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
        """The run kept in its own message loses frames: the turn writer
        rebuilds that message from the transcript (before ELI-158 the reply
        that took the run over kept what it lost lost)."""
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
        """The kept run is killed from outside (exit 143): its own message
        ends with the cut-off note and is not resumed, since a resume would
        race MSG2 into the session (ELI-159); MSG2 goes out after it, alone,
        in its own message."""
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
    """ELI-147 slice 2: the inline reply and the turn writer show a run's tool
    calls as the same cards. Both are fed one recorded run: a call that
    succeeds, one that fails, a result whose start was never seen, and a call
    the run ended without answering."""

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
        """The output list Open WebUI keeps from `output_item.added` events:
        an item with the id of one it holds replaces it, else it is appended
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
    """ELI-147 slice 3: the inline reply and the turn writer show a run's
    text the same way: dedup of whole-text and item events, the metadata
    filter, `MEDIA:` directives and needs-input prompts. Each case is one
    recorded run, fed to an inline reply and to a writer that saw the run
    alone (no tab answers the question on either side)."""

    asyncSetUp_inline = InlineOwnerRuleTests.asyncSetUp
    asyncTearDown = InlineOwnerRuleTests.asyncTearDown
    _queue = InlineOwnerRuleTests._queue
    _until_registered = InlineOwnerRuleTests._until_registered
    _reply = InlineOwnerRuleTests._reply
    _event = InlineOwnerRuleTests._event
    USER, CHAT = InlineOwnerRuleTests.USER, InlineOwnerRuleTests.CHAT

    PROMPT = "OpenClaw needs input: Which branch?\n1. main\n2. dev"
    # The documented form: the marker on its own line (ELI-151).
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
        # ELI-151: a question written on the marker line is kept, as the line
        # after a marker on its own line is; only the marker itself goes.
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
            # Differed on main; the expected text names the side that was right.
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
        # What `end` does when no tab answers the question, then `finalize`'s view.
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


class ApiCallerOwnerRuleTests(unittest.IsolatedAsyncioTestCase):
    """Review 2af1 R14: an interrupted reply hands its Open WebUI message to
    the turn writer, which applies the owner rule. A request through the API
    names no message, so there was nothing to hand over: after a Gateway
    restart it waited forever, and after an outside kill it ended with the
    error. With no message, the reply applies the owner rule itself, in its
    own stream: it resumes, follows the Gateway's recovery run, or ends."""

    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered
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
        """The API reply streams `Reading them.` of run-1; returns the task
        draining it."""
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
        # The recovery's first events come before the reply knows it is the
        # recovery of its run: held, not given to a turn writer.
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
        """R2's bound: the Gateway tombstones a recovery it gives up on to
        `failed` (main-session-restart-recovery-CPJjd8da.mjs:1150)."""
        task = await self._start()
        self._restart()
        await self.spin()
        self.row(hasActiveRun=False, status="failed", lastRunId="run-1",
                 lastRunError="restart recovery gave up")
        out = await asyncio.wait_for(task, 5)
        self.assertIn("Reading them.", out)
        self.assertIn("**Error:** restart recovery gave up", out)
        self.assertEqual(self.sent, ["hello"])


class SessionIdRunTests(_RoutingCase):
    """ELI-103. The cron executor runs every wake into a session with
    runId = sessionId (run-executor.runtime-ro-4Z0fM.mjs:499): the id names no
    single wake, and the row stays hasActiveRun:false while a wake runs.

    Live 2026-10-13, chat 0abe9bde, pipe 62b9927: a wake ran 03:02:39-03:26:39Z.
    The writer opened 96b8c998 = uuid5(key:sessionId). At 03:13:46Z a manager
    message queued behind the wake pushed a settle row (lastRunId = the previous
    user turn), which ended the wake's writer 13 minutes early. On main 787a471
    the next wake (03:34Z) got the first wake's tail instead of its own text.

    A wake is identified by its own admitted user turn `<turnId>:user`
    (`[cron:<job> <name>] ...`), committed before its first event on
    claude-cli (cli-runner-9MSP9PU4.mjs:1370). Its closing message
    `cli-assistant:<sessionId>` exists for the first wake only (deduplicated by
    key, session-accessor-CX17Bx59.mjs:3343): live seq 74 (wake 1d549bd7) has
    none."""

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
        # A row of this session reached the socket earlier: its sessionId is known.
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

    def wake_turn(self, key, text="Wake (worker ELI-100): deploy finished.", started=False):
        """The cron executor marks its job running (unless `started` already),
        then commits the wake's user turn before the CLI starts."""
        at = self.tick()
        if not started:
            self.jobs = [{"id": self.JOB, "sessionTarget": f"session:{self.key}",
                          "state": {"runningAtMs": at - 400}}]
        self.history["messages"].append({
            "role": "user", "content": f"[cron:{self.JOB} eli100-wake] {text}",
            "idempotencyKey": f"{key}:user", "timestamp": at - 150,
            "__openclaw": {"idempotencyKey": f"{key}:user", "recordTimestampMs": at}})

    def queued_turn(self, text="[manager] status?"):
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
        """Review 2af1 N1 (review_d22ff82_test.py): the resync ended the wake at
        the reconnect time, and the queued run's reply written since landed in
        the wake's message. The end is the job run's finish (cron.runs ts)."""
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
        """Review 2af1 N2: a stray (runId = sessionId, no running wake) was held
        by a pump that lived on, and the next wake flushed it as its own."""
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
        """Review 2af1: a fresh pump (a new connection, a held first event) read
        only the newest 50 messages; a wake that had written more before was
        never bound. The read widens until it reaches before the job's start."""
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
        """The widening is bounded by the signal, not by a guess: once the page
        reaches before the running job's start, the turn is not in the
        transcript yet, and reading further back cannot find it."""
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
        """Review (c): a node agent command or a compaction also runs with
        runId = sessionId. With no cron wake of its own it is a stray."""
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
        """The old wake ran under main, which showed it as its own bubble: no
        message carries its turn's id, so only its cron state can say a later
        sessionId event is not that wake's."""
        self.wake_turn(self.W1)
        self.job_finished()
        _turn_route_now(self.conn, self.sid_ev(_delta("compaction note")))
        _turn_route_now(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        self.assertEqual(self.new_messages(), {}, "a stray opened a message for a finished wake")

    async def test_a_sessions_send_turn_queued_during_the_wake_is_not_the_wakes(self):
        """Review (d): sessions_send turns carry provenance and their own run id."""
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
        """Review (f): skipped by its resolved identity, not by the run id
        every wake shares."""
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
        """2af1's ReviewWakeCutByResync with the wake's admitted turn in the
        transcript: a seq gap or a reconnect reads the idle row."""
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
        # The queued user message's row: idle, and naming another run.
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
        """Main 787a471, 03:34Z: the second wake's bubble got the first wake's
        tail, because `cli-assistant:<sessionId>` is the first wake's."""
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
        """Gate 2026-10-13 (a3bfac2, wake-then-user, chat 6ad6778d): with a
        user turn queued behind it, the wake's claude-cli session was never
        imported; chat.history held its text but none of its 4 tool calls. A
        rebuild from that window must keep the cards that streamed, whatever
        the item count says."""
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev(_tool("start", tcid="toolu_a")))
        # The call's result and the wake's text never reached the socket (a
        # gap): one streamed item. The transcript renders one too, its text,
        # with no tool call: an item count calls that "at least as much".
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
        """Gate 2026-10-13 (two-wakes, chat 5327e981): job b fired while wake a
        ran. Cron admission compares the session entry with its snapshot
        (isolated-agent-C_VaOV3U.mjs:885-889, any field but label, pinnedAt,
        updatedAt) and threw CronSessionLifecycleClaimError "changed while
        starting work. Retry." 143 ms in: no turn, no event, sessionId null in
        cron.runs. The scheduler retries that before execution starts
        (service-DYcTh1Yv.mjs:670, backoff 30 s / 60 s / 300 s, 3 retries); b
        ran 30 s later."""
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
        # The retry, 30 s later: its own turn, its own message.
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
        (embedded-agent-DNQn_PMM.mjs:5891): the first event can come first."""
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
        """A tab views the chat; `call` answers its question card (default:
        "Blue"). chat.send is accepted."""
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
        """The Gateway's answers for stopping a wake: chat.abort finds no run
        to abort on 2026.9 (a cron run is not a chat run, live 2026-10-18,
        ELI-192), the wake's job is a running cron task, and tasks.cancel
        aborts that run. `run_abortable`: 2026.10.5, where sessions.abort
        by the run's id aborts it and there is no tasks.list."""
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
        """ELI-192: Stop on a wake's reply ended the message, but the run went
        on to its end, its text shown nowhere. On 2026.9 a wake's run id is
        the sessionId and chat.abort finds no run under it: the wake is a
        cron job run, which its task's cancel aborts."""
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
        """2026.10.5 has no tasks.cancel: Stop on a wake's reply let its run
        go on to its end. sessions.abort with the run's id stops that run
        alone, without clearQueued, so turns queued behind it stay."""
        self.tab_on_chat()
        self.cron_task_gateway(run_abortable=True)
        self.wake_turn(self.W1)
        _turn_route_now(self.conn, self.sid_ev(_delta("Looping: 1 of 8.")))
        await self.spin()
        mid = turn_message_id(self.key, self.W1, self.CHAT)
        w = await self.stop_on_the_tab(mid)
        self.assertIn(("sessions.abort", {"key": self.key, "runId": w.run_id}), self.sent)
        self.assertNotIn("tasks.list", [m for m, _ in self.sent])
        # chat.abort answers `aborted` for a wake on 2026.10.5 but stops
        # only its running command (sandbox, 2026-10-19): never sent first.
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
        """On 2026.9 a wake ends on its own terminal event (no row names its
        run), and that end finalized the message without asking: the
        question stayed as text that nothing could answer. It asks as a
        run's end does, and the answer continues the wake's message."""
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
        """Every wake into the session shares one run id, so one pump: the
        card is asked off it, and the next wake is not held behind it."""
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
        """The Gateway's `cron` event for a job run into this session (a Pipe
        before ELI-181 ignored them)."""
        getattr(self.conn, "note_cron_event", lambda _p: None)({"action": action, "jobId": job, "runAtMs": start,
                                   **({"durationMs": end - start} if end is not None else {}),
                                   "job": {"id": job, "sessionTarget": f"session:{self.key}"}})

    def short_wake(self, key, text, job=None):
        """A wake whose whole run is over before the Pipe judges its first
        event (a ~40 ms run, on 2026.9 with the Pipe busy). Its first event
        predates its own turn's commit: the embedded runtime writes the turn
        after the run's first events. Returns the run's events, not routed."""
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
        """PR #115 CI, 2026-10-18 07:24:29: WAKE_TWO never got a message; the
        Pipe said "1 sessionId event(s) ... with no running cron wake". Its
        job had left cron.list, and its first event was older than its turn,
        so no turn matched: the wake was judged stray and the pump dropped
        the rest of the run."""
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
        """Every wake into a session shares one run id, so one pump: what is
        queued behind a wake's end is the next wake's, and must not be dropped
        with the pump."""
        first = self.short_wake(self.W1, "First wake.", job="job-1")
        second = self.short_wake(self.W2, "Second wake.", job="job-2")
        for ev in first + second:      # all queued before the pump judges any
            _turn_route_now(self.conn, ev)
        await self.spin()
        got = {k: m["content"] for k, m in self.new_messages().items()}
        self.assertEqual(got, {turn_message_id(self.key, self.W1, self.CHAT): "First wake.",
                               turn_message_id(self.key, self.W2, self.CHAT): "Second wake."})

    async def stray_judged_while_the_next_wake_runs(self, after_final, *, cron_events=True):
        """W1's repro (ELI-181): every wake leaves a trailing sessionId event
        after its final, judged stray on a pump of its own. Judging it awaits
        cron.list; wake two runs meanwhile, into that pump's queue. The
        judgment returns stray, the pump ends, and wake two's events went
        with it. `cron_events`: the Gateway's started / finished events for
        wake two reached this socket. Returns wake two's message or None."""
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
    """ELI-103 step 4. Live 2026-10-13: the rollback to main retired the
    62b9927 connection, but its writer's Open WebUI task 03904655 stayed
    registered, so efe80f6a showed generating and orphan repair skipped the
    chat until the task was stopped by hand. A retired writer ends its task;
    the successor adopts the message and finishes it once its run is over."""

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
    """Review 2af1 R2 (ReviewGatewayOwnerNeverRecovers): a settled row that
    says `running` keeps M open for the Gateway's recovery. When the Gateway
    gives up, it tombstones the row to failed
    (main-session-restart-recovery-CPJjd8da.mjs:1150): that row closes M."""

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


class MediaDirectiveCodeAwareTests(unittest.IsolatedAsyncioTestCase):
    """ELI-111 (2026-10-14, chat 0abe9bde, message 442ecfa3): a report that
    quoted media directives in inline code had one of them, inside a
    double-backtick span, rendered as an image with a line break inserted.
    The grammar matched the directive anywhere. Like the ask marker since
    ELI-107, a directive is code-aware now (CommonMark): one inside an inline
    code span (any backtick run length) or a fenced block is text; one in
    prose, mid-line or not, is still a directive."""

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
        """A streamed chunk starts where the shown text of its message item
        left off: inside a fence, or inside an open code span."""
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

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered
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
        """The chunk that resolves a directive can open a fence; the next
        chunk's code state needs that chunk's raw text."""
        self.pipe.valves.USE_OWUI_FILES = False
        self.pipe.valves.FILE_SERVER_BASE_URL = self.BASE
        out = await self._stream("Real: MEDIA:a.png\n```\nMEDIA:b.png\n```\nend\n", None, chunk=5)
        self.assertIn("![a.png](https://x/a.png)", out)
        self.assertIn("MEDIA:b.png", out)
        self.assertEqual(out.count("!["), 1)

    async def test_a_held_directive_shows_before_the_next_tool_card(self):
        """Held while a backtick run might still open a span; the item ends at
        a tool call, so the run was literal and the image shows before it."""
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
    """(content, output, live) for a pipe's yields as Open WebUI 0.12's
    streaming loop (`middleware.py`) handles them. `content` joins the text
    chunks (`content_parts`). `output` is the backend's list, which is saved:
    a text chunk appends to the last message item, or opens one after any
    other item; a dict goes through the vendored
    `handle_responses_streaming_event`; the last message's text is stripped
    when the stream ends. `live` is what the backend sends the chat tab
    (`emit_response_completion_event`): a text chunk as an
    `output_text.delta` on its target item's id and index, a dict as is."""
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
    """What Open WebUI 0.12 sends the tab (`chat:tasks:cancel`) and saves when
    a reply is cancelled (`save_cancelled_state`, utils/middleware.py:6652):
    its output with in-progress items and running tool calls (a call stamped
    completed with no output yet) stamped `incomplete`, which the tool card
    draws as done (ToolCallDisplay.svelte `isDone`)."""
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
    """The output list an Open WebUI 0.12 chat tab renders while the reply
    streams: `events` folded by the vendored frontend reducer
    (`test_fixture_owui_structured_output.ts`), run with bun."""
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "owui_live_fold.ts")
    done = subprocess.run(["bun", script], input=json.dumps(events), capture_output=True,
                          text=True, check=True, timeout=60)
    return json.loads(done.stdout)


def _item_order(output: list) -> list:
    """Each output item as (type, id or call_id, message text)."""
    return [(i.get("type"), i.get("call_id") or i.get("id"),
             "".join(str(p.get("text", "")) for p in i.get("content") or []).strip()
             if i.get("type") == "message" else None) for i in output]


class LiveToolCardOrderTests(unittest.IsolatedAsyncioTestCase):
    """ELI-122 (ELI-110 screenshots, run 38605371115): while a reply
    streamed, the text written before a tool call showed under that call's
    card; after a reload the order was right. The tab folds a tool card's
    `output_item.added` with the frontend reducer, which splices an item with
    no `output_index` in before the last one, while the backend (what is
    saved) appends it. What the tab shows live must be what is saved."""

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered

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


class ModelPatchFailureTests(unittest.IsolatedAsyncioTestCase):
    """ELI-131: a failed model patch is reported, not retried after a sleep.
    The retry (July, one burst of 8 messages in 3 s) never saved a reply: the
    burst is one patch since ELI-59's lock and cache, and every failure Open
    WebUI shows since (model not allowed, Gateway timeouts) failed twice."""

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown

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


class LongToolOnlyRunTests(unittest.IsolatedAsyncioTestCase):
    """ELI-132: a run that sends thousands of tool events before its first
    text is a live run, not a stuck one. The pipe used to end the reply after
    5000 events without text, with a "Timeout" note, while the run went on;
    its answer and the rest of its cards never reached the message."""

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered
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
        # As the receive loop does: each event waits for room in the run's
        # bounded queue, so the reply reads them as they come.
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


class LiveToolErrorMarkTests(unittest.IsolatedAsyncioTestCase):
    """ELI-124 (found during ELI-122): a failed call's card got its ❌ only
    when the reply finished. The mark was a `response.name.done`, which the
    backend applies to its last item but the chat tab's reducer ignores. The
    tab must show the mark while the reply streams, as it is saved."""

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered
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
        """The mark used to need its card to be the last item; a parallel call
        or text in between only got the banner."""
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


class MediaDirectiveLineBreakTests(unittest.IsolatedAsyncioTestCase):
    """ELI-113 (found during ELI-111, 2026-10-14): the line break after a
    streamed directive was dropped when the chunk ended right after it, so
    the next line was glued onto the image: `Real: MEDIA:c.p` + `ng\\n` +
    `Next line` was saved as `![c.png](...)Next line`. Where a stream is cut
    must not change what Open WebUI saves."""

    BASE = "https://x"
    IMG = "![c.png](https://x/c.png)"

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered

    async def _saved(self, chunks):
        """Stream `chunks` as one run's deltas; (content, message texts)
        Open WebUI saves."""
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
    """The Output a tool card shows: Open WebUI 0.12's `getToolResultText`
    (`structuredOutput.ts`), the text parts of the call's
    `function_call_output`, images left out."""
    item = next(i for i in output if i.get("type") == "function_call_output"
                and i.get("call_id") == call_id)
    return "".join(str(p.get("text", "")) for p in item.get("output") or []
                   if p.get("type") != "input_image" and p.get("text") is not None)


class ToolCardOutputTextTests(unittest.IsolatedAsyncioTestCase):
    """ELI-121 (ELI-110 screenshots, run 38605371115): a tool card's Output
    showed the result envelope, `{"content": [...], "details": {..., "cwd":
    ...}}`, instead of the command's output: the pipe and the turn renderer
    json-dumped the whole result. OpenClaw sends a tool result as a string,
    a list of content blocks, or the agent-tool envelope (`content` +
    `details`, `sanitizeToolResult`); the card shows the content's text, and
    `details` (status, exit code, cwd, ...) stays out of it."""

    # The exec tool's result as OpenClaw sends it (`ExecToolDetails`,
    # bash-tools.exec-types.ts; `sanitizeToolResult` keeps both keys).
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

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered

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
    """A pipe reply with a scripted Gateway (`NoDroppedMessagesTests`), for
    a test that also needs another harness."""

    asyncSetUp = NoDroppedMessagesTests.asyncSetUp
    asyncTearDown = NoDroppedMessagesTests.asyncTearDown
    _queue = NoDroppedMessagesTests._queue
    _delta = NoDroppedMessagesTests._delta
    _final = NoDroppedMessagesTests._final
    _until_registered = NoDroppedMessagesTests._until_registered

    def fail(self, msg):
        raise AssertionError(msg)

    async def saved(self, chunks, base_url) -> tuple[str, list]:
        """(content, output) Open WebUI saves for one run streaming `chunks`,
        a directive resolved through the file server at `base_url`."""
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
    """ELI-117 (found during ELI-113): the turn writer saved the text after a
    `MEDIA:` directive on the directive's line as written, while the inline
    reply puts it on a line of its own, so `MEDIA:a.png more` was saved
    differently by the path that finalized the reply. One rule for both:
    the same text, inline or handed over, saves the same content and output
    items."""

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
        """(content, output) the turn writer saves for a run the inline reply
        never streamed, its directives resolved through the same server."""
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
    """ELI-111, the writer side: a handed-over reply resolves a directive the
    way the inline reply does, and an image recorded for a prose directive is
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
    """GH #24: since OpenClaw 2026.10.2, `chat.history` re-emits a forwarded
    user turn (a cron run's prompt, a `sessions_send` message) as role
    "assistant", "Forwarded from ..." (dist chat-display-projection.helpers-
    pbUdgTgb.mjs `isForwardedUserMessage`, chat-display-projection.history
    `projectForwardedMessages`). It is still the admitted turn `<id>:user`
    that starts the run. The fixture is the e2e wake session's history as
    each version returned it."""

    FX = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                     "test_fixture_gh24_wake_history.json")))

    def with_cli_closers(self, messages):
        """The history a claude-cli runtime leaves: each run closes with
        `cli-assistant:<runId>` repeating its text (cli-runner-C9KAAFBI.mjs:589,
        2026.10.3). The fixture's fake model writes none."""
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
        """Read as a reply, the cron prompt and its scheduled-run preamble
        became the wake message's text when the writer finalized it from the
        transcript."""
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
    """ELI-205: a cancel inside Open WebUI's DB write (BEGIN IMMEDIATE / commit
    run in aiosqlite's thread) can wedge it and keep SQLite's write lock for
    good, so every later write fails. A Stop must never cancel the Pipe's own
    writes midway: the write finishes, then the cancel goes on."""

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
    """ELI-182: the Pipe's title replaces only the title Open WebUI set by
    itself, and shows at once in the open tab. QA 2026-10-18: a chat renamed
    to '[QA] My renamed chat' became '✅ QA Title Test One' after the next
    message; a generated title showed only after a reload.

    Open WebUI 0.12.3 creates a chat as 'New Chat'; its first-exchange title
    task (which the Pipe skips) then falls back to the first user message,
    without `<details>` blocks and images (main.py run_initial_title_generation,
    middleware.py background_tasks_handler)."""

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

    async def test_new_chat_is_replaced(self):
        self.rename("New Chat")
        await self.second_message()
        self.assertEqual(self.chat.title, self.GENERATED)

    async def test_the_first_message_title_is_known_as_open_webui_derives_it(self):
        """Text parts of a list content, `<details>` blocks and images removed,
        stripped: what Open WebUI titled the chat with."""
        self.chat.chat["history"]["messages"]["q1"]["content"] = [
            {"type": "text", "text": "<details type=\"x\">ctx</details> look ![a](b.png) "},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
            {"type": "text", "text": "here"}]
        self.rename("look  \nhere")
        await self.second_message()
        self.assertEqual(self.chat.title, self.GENERATED)
