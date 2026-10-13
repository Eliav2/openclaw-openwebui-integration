#!/usr/bin/env python3
"""Focused unit tests for pipe stream recovery behavior."""

import unittest
import ast
import html
import os
import tempfile
import textwrap
import contextlib
import copy
import inspect
import json
import re
import sys
import time
import types
import asyncio
import uuid
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before this file's stub can shadow it)

if "websockets" not in sys.modules:
    websockets_stub = types.SimpleNamespace(
        WebSocketClientProtocol=object,
        exceptions=types.SimpleNamespace(ConnectionClosed=Exception),
    )
    sys.modules["websockets"] = websockets_stub

import websockets  # noqa: E402  (must come after the stub is installed above)

if "cryptography" not in sys.modules:
    crypto = types.ModuleType("cryptography")
    hazmat = types.ModuleType("cryptography.hazmat")
    primitives = types.ModuleType("cryptography.hazmat.primitives")
    asymmetric = types.ModuleType("cryptography.hazmat.primitives.asymmetric")
    ed25519 = types.ModuleType("cryptography.hazmat.primitives.asymmetric.ed25519")
    serialization = types.ModuleType("cryptography.hazmat.primitives.serialization")
    backends = types.ModuleType("cryptography.hazmat.backends")

    class _DummyPrivateKey:
        @staticmethod
        def generate():
            raise RuntimeError("cryptography stub cannot generate keys")

    ed25519.Ed25519PrivateKey = _DummyPrivateKey
    serialization.Encoding = types.SimpleNamespace(Raw="Raw", PEM="PEM")
    serialization.PublicFormat = types.SimpleNamespace(Raw="Raw")
    serialization.PrivateFormat = types.SimpleNamespace(PKCS8="PKCS8")
    serialization.NoEncryption = lambda: None
    serialization.load_pem_private_key = lambda *args, **kwargs: None
    backends.default_backend = lambda: None

    sys.modules["cryptography"] = crypto
    sys.modules["cryptography.hazmat"] = hazmat
    sys.modules["cryptography.hazmat.primitives"] = primitives
    sys.modules["cryptography.hazmat.primitives.asymmetric"] = asymmetric
    sys.modules["cryptography.hazmat.primitives.asymmetric.ed25519"] = ed25519
    sys.modules["cryptography.hazmat.primitives.serialization"] = serialization
    sys.modules["cryptography.hazmat.backends"] = backends

if "pydantic" not in sys.modules:
    pydantic = types.ModuleType("pydantic")

    class _BaseModel:
        def __init__(self, **kwargs):
            for name, value in self.__class__.__dict__.items():
                if name.startswith("_") or callable(value):
                    continue
                setattr(self, name, kwargs.get(name, value))

    def _field(*, default=None, **kwargs):
        return default

    pydantic.BaseModel = _BaseModel
    pydantic.Field = _field
    sys.modules["pydantic"] = pydantic

import openclaw_pipe  # noqa: E402
from openclaw_pipe import (
    ACTION_RUN_PREFIX, TURN_NS, _run_id_sent_at, parse_pipe_run_id, pipe_run_id, turn_message_id,
    _claim_turn_message, _register_turn_writer, _retire_turn_writer,
    MAX_TURN_RESUMES, next_continuation, turn_owner,
    recovery_source_run, run_message_id, _TurnWriter, _INTENT_HOOK_JS,
    _turn_route_event, _turn_after_hello, _turn_resync, _history_span_messages,
    wake_turns, wake_turn_at, wake_span, _wake_resync, _adopt_turn_message,
    _gateway_run_status,
    _GatewayConnection,
    GatewayError,
    _parse_gateway_url,
    _explain_connect_rejection,
    _explain_session_patch_failure,
    _catch_all_delta,
    _align_whole_text,
    _gap_repair_fields,
    _GapRepairSink,
    _marker_line_index,
    _discover_models_with_source,
    _modal_payload_from_user_input_prompt,
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
    _FALLBACK_MODELS,
    _advance_input_prompt_buffer,
    _advance_media_buffer,
    _ask_user_detail_block,
    _ask_user_input_modal,
    _build_choice_modal_js,
    _build_usage_status_lines,
    _extract_numbered_options,
    _coerce_text,
    _content_has_image,
    _could_be_user_input_prefix,
    _deepest_leaf_id,
    _discover_models,
    _emit_message_snapshot,
    _emit_status,
    _extract_image_attachments,
    _fmt_tokens,
    _friendly_name,
    _is_user_input_prompt,
    _item_assistant_text,
    _item_delta_text,
    _live_session_id_for_user,
    _modal_payload_from_user_input_prompt,
    _native_ask_user_payload,
    _format_native_answers,
    _owui_supports_native_ask_user,
    _model_patch_matches,
    _normalize_event_call_response,
    _normalize_model_entry,
    _owui_chat_send_params,
    _owui_session_key,
    _preview_recovery_text,
    _history_recovery_text,
    _recover_turn_text,
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


class PreviewRecoveryTests(unittest.TestCase):
    def test_recovers_assistant_after_matching_user_message(self):
        preview = {
            "previews": [{
                "key": "agent:main:test",
                "items": [
                    {"role": "user", "text": "old"},
                    {"role": "assistant", "text": "old answer"},
                    {"role": "user", "text": "current question"},
                    {"role": "assistant", "text": "current answer"},
                ],
            }]
        }
        self.assertEqual(
            _preview_recovery_text(preview, "agent:main:test", "current question"),
            "current answer",
        )

    def test_does_not_recover_without_matching_user_message(self):
        preview = {
            "previews": [{
                "key": "agent:main:test",
                "items": [{"role": "assistant", "text": "answer"}],
            }]
        }
        self.assertIsNone(
            _preview_recovery_text(preview, "agent:main:test", "current question")
        )


class InterruptedTurnTailRecoveryTests(unittest.TestCase):
    """ELI-74: a turn whose live stream was lost is read back from the session
    transcript. It used to come from `sessions.preview`, which the Gateway
    clips per item server-side (2000 chars then, 800 on 2026.9.x) whatever
    `maxChars` asks for, so long replies landed cut mid-sentence with "...".
    """

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

    @staticmethod
    def _clipping_preview(full_text, cap=800):
        """What the Gateway's sessions.preview returns: every item clipped."""
        def clip(t):
            return t if len(t) <= cap else t[:cap - 3] + "..."

        async def session_preview(session_key, limit=8, max_chars=4000):
            return {"previews": [{"key": session_key, "items": [
                {"role": "user", "text": InterruptedTurnTailRecoveryTests.QUESTION},
                {"role": "assistant", "text": clip(full_text)},
            ]}]}
        return session_preview

    def _conn(self, history=None, history_error=None):
        conn = types.SimpleNamespace()
        if history_error is not None:
            conn.session_history = mock.AsyncMock(side_effect=history_error)
        else:
            conn.session_history = mock.AsyncMock(return_value=history)
        conn.session_preview = mock.AsyncMock(side_effect=self._clipping_preview(self.LONG))
        return conn

    def test_long_tail_survives_intact(self):
        self.assertGreater(len(self.LONG), 2000)
        conn = self._conn(self._history())
        recovered = asyncio.run(_recover_turn_text(conn, self.KEY, self.QUESTION))
        self.assertEqual(recovered, self.LONG)
        self.assertFalse(recovered.endswith("..."))
        conn.session_preview.assert_not_awaited()

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

    def test_falls_back_to_the_preview_when_history_fails(self):
        conn = self._conn(history_error=GatewayError("unknown method chat.history"))
        recovered = asyncio.run(_recover_turn_text(conn, self.KEY, self.QUESTION))
        self.assertTrue(recovered.endswith("..."))  # clipped, but not nothing
        conn.session_preview.assert_awaited_once()

    def test_falls_back_to_the_preview_when_history_has_no_match(self):
        conn = self._conn({"messages": []})
        self.assertIsNotNone(asyncio.run(_recover_turn_text(conn, self.KEY, self.QUESTION)))
        conn.session_preview.assert_awaited_once()

    def test_no_match_anywhere_is_none(self):
        conn = self._conn({"messages": []})
        self.assertIsNone(asyncio.run(_recover_turn_text(conn, self.KEY, "never asked")))


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
        frames = list(frames)

        class ScriptedWS:
            async def recv(self_):
                if frames:
                    return json.dumps(frames.pop(0))
                conn._stopped = True
                raise asyncio.CancelledError  # stop, not a tick timeout (which marks a gap)

        conn._ws = ScriptedWS()
        await conn._event_loop()
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
         "content": [{"type": "text", "text": " Found it."}]},
        {"role": "assistant", "idempotencyKey": f"cli-assistant:{run_id}",
         "content": [{"type": "text", "text": "Reading them. Found it."}]},
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
        self.assertEqual(r.text, "Reading them. Found it.")
        self.assertEqual([i["type"] for i in r.output],
                         ["message", "function_call", "function_call_output", "message"])
        self.assertEqual(r.output[1]["arguments"], json.dumps({"command": "tail log"}))
        self.assertEqual(r.output[2]["output"][0]["text"], "ERROR x")

    def test_tool_free_reply_comes_from_the_closing_message(self):
        r = _history_renderer(_history_run_messages(_cli_history(), "run-1"))
        self.assertEqual(r.text, "old answer")
        self.assertEqual([i["type"] for i in r.output], ["message"])


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
             "content": [{"type": "text", "text": " Found it."}]},
            {"role": "assistant", "idempotencyKey": f"cli-assistant:{run}",
             "content": [{"type": "text", "text": "Reading them. Found it."}]},
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
        self.assertEqual(r.text, "Reading them. Found it.")
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
                        "parentId": "u1", "childrenIds": [], "model": "openclaw_gateway.m"},
        }}}
        self.history = _turn_history(closed=False)
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
        socket_main = types.ModuleType("open_webui.socket.main")
        self.sio = mock.AsyncMock()
        socket_main.sio = self.sio
        self.modules = {"open_webui": types.ModuleType("open_webui"),
                        "open_webui.models": types.ModuleType("open_webui.models"),
                        "open_webui.models.chats": chats_mod,
                        "open_webui.socket": types.ModuleType("open_webui.socket"),
                        "open_webui.socket.main": socket_main}
        self.conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))

        async def send_request(method, params, timeout=10):
            if method == "chat.history":
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
        self.assertEqual(self._stub()["content"], "Reading them. Found it.")
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
        self.assertEqual(self._stub()["content"], "Reading them. Found it.")

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

    def test_failed_call_is_marked_and_explained(self):
        r = _TurnRenderer()
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "Bash",
                                           "toolCallId": "t2", "result": "boom",
                                           "isError": True}})
        self.assertEqual(r.output[0]["name"], f"Bash {TOOL_ERROR_MARK}")
        self.assertIn("tool call failed", r.output[1]["output"][0]["text"])

    def test_finished_output_completes_open_items_without_touching_the_live_copy(self):
        r = _TurnRenderer()
        r.feed({"stream": "assistant", "data": {"delta": "Hi"}})
        r.feed({"stream": "tool", "data": {"phase": "start", "name": "Bash",
                                           "toolCallId": "t3", "args": {}}})
        done = r.finished_output()
        self.assertEqual({i["status"] for i in done}, {"completed"})
        self.assertEqual(r.output[1]["status"], "in_progress")


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
        source = inspect.getsource(Pipe._pipe_turn)
        self.assertIn("should_announce_thinking_note(session_key, thinking_note)", source)
        self.assertIn('pipe_log(f"thinking: {thinking_note}")', source)


class OwuiTaskNoopTests(unittest.TestCase):
    """O10 (follow-up to O8): image_prompt_generation and function_calling
    join the background-task no-op set; moa_response_generation must not."""

    def _noop_task_values(self):
        source = inspect.getsource(Pipe._pipe_turn)
        m = re.search(
            r"if __task__ and __task__ in \(\s*((?:\"[a-z_]+\",?\s*)+)\)",
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

    async def test_claim_waits_for_a_stop_answer_in_flight(self):
        # Stop, then OWUI auto-sends the next queued message: that message
        # must start fresh, not continue the run the user stopped.
        conn, calls = self._conn()
        conn.park_cancelled_turn("s", "run-1", None, [], 30, send_stop=False)
        claim = asyncio.ensure_future(conn.claim_cancelled_turn("s"))
        await asyncio.sleep(0.05)
        conn.settle_parked_turn("s", "run-1", "stop", hold_s=120)
        self.assertIsNone(await claim)
        self.assertEqual(calls, [("abort", "s", "run-1")])

    def test_intent_query_reads_and_clears(self):
        from openclaw_pipe import _intent_query_js
        js = _intent_query_js("chat-1")
        self.assertIn('"chat-1"', js)
        self.assertIn("delete all[c]", js)

    def test_pipe_claims_before_model_patch(self):
        # The grace window is seconds; sessions.patch alone can take 10s+ with
        # its retry, so the claim must come before it. And the cancel handler
        # must park, never abort directly.
        src = inspect.getsource(Pipe._pipe_turn)
        claim = src.index("await conn.claim_cancelled_turn(session_key)")
        patch = src.index('"sessions.patch"')
        self.assertLess(claim, patch)
        self.assertIn("conn.park_cancelled_turn(", src)
        self.assertNotIn("await conn.abort(", src)

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
        n = {"run": 0}

        async def send_request(method, params, timeout=10):
            self.rpc.append(method)
            if method == "chat.send":
                n["run"] += 1
                self.sent.append(params.get("message"))
                return {"runId": f"run-{n['run']}"}
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

    async def _read(self, gen, want):
        out = ""
        while want not in out:
            out += await asyncio.wait_for(gen.__anext__(), 2)
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
        duplicates. Now only what follows the received text is new, and the
        lost lines go back into the reply once OWUI has saved it."""
        applied = []

        async def fake_apply(conn, repair):
            applied.append(repair)
            return True

        with mock.patch.object(self.ocp, "_apply_gap_repair", fake_apply), \
                mock.patch.object(self.ocp, "_gateway_connection", self.conn):
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
            # The reply was still streaming: its repair waits for the save.
            self.assertEqual(applied, [])
            await self.ocp._on_chat_finished({"chat_id": "chat-1", "message_id": "m1"})
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["message_id"], "m1")
        self.assertEqual(applied[0]["chunk"], self._nums(51, 60))
        self.assertTrue(applied[0]["before"].startswith("61\n"))

    async def test_socket_drop_before_a_send_now_repairs_the_first_reply(self):
        """The 98891cbb shape: the drop hit right after reply 1 registered
        (lines 1..3 lost), then two Send nows moved the run to replies 2 and
        3, where the catch-all arrived. The lost lines belong to reply 1,
        which OWUI has already saved, so they go back in at once."""
        applied = []

        async def fake_apply(conn, repair):
            applied.append(repair)
            return True

        with mock.patch.object(self.ocp, "_apply_gap_repair", fake_apply):
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
        self.assertEqual([(r["message_id"], r["chunk"]) for r in applied],
                         [("m1", self._nums(1, 3))])
        self.assertTrue(applied[0]["before"].startswith("4\n5\n"))
        self.assertNotIn("abort", self.rpc)

    async def test_send_now_twice_loses_nothing(self):
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

    def test_relabel_uses_the_generic_field_done_shape(self):
        """`response.output_item.done` is unreachable in OWUI's handler (a
        broader `.done` branch matches first and explicitly skips it); the
        generic `<field>.done` arm of that same broader branch is the one
        that actually runs and broadcasts."""
        ev = _tool_call_error_relabel_event("Bash")
        self.assertEqual(ev["type"], "response.name.done")

    def test_relabel_marks_the_name(self):
        ev = _tool_call_error_relabel_event("Bash")
        self.assertEqual(ev["name"], "Bash ❌")

    def test_relabel_sends_no_output_index(self):
        """Deliberate: OWUI defaults to the last item, and the caller only
        relabels when the started item IS last. A wrong explicit index would
        overwrite a text item and eat visible message content."""
        ev = _tool_call_error_relabel_event("Bash")
        self.assertNotIn("output_index", ev)

    def test_relabel_is_json_serializable(self):
        json.dumps(_tool_call_error_relabel_event("Bash"))

    def test_banner_precedes_the_original_output(self):
        """The banner is the fallback when the card can't be relabeled, so it
        must never cost the actual error text."""
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
        started = _tool_call_started_event("Read", "call-1", "{}")
        relabel = _tool_call_error_relabel_event("Read")
        result = _tool_call_result_event("call-1", "BANNER")

        output, metadata_seq = self._replay([started, relabel, result])

        function_call = next(item for item in output if item["type"] == "function_call")
        self.assertEqual(function_call["name"], "Read ❌")
        # The relabel step must itself trigger a broadcast (non-None
        # metadata), not just silently mutate an output list nobody sees.
        self.assertIsNotNone(metadata_seq[1])

    def test_relabel_lands_with_preceding_text(self):
        """Same replay, but with a streamed text item ahead of the tool call
        -- the generic field-done arm targets by output_index (default: last
        item), so a leading item must not shift what gets relabeled."""
        preceding_text = {
            "type": "message",
            "id": "msg_1",
            "status": "in_progress",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi"}],
        }
        started = _tool_call_started_event("Read", "call-1", "{}")
        relabel = _tool_call_error_relabel_event("Read")
        result = _tool_call_result_event("call-1", "BANNER")

        output, _ = self._replay(
            [started, relabel, result], initial_output=[preceding_text]
        )

        function_call = next(item for item in output if item["type"] == "function_call")
        self.assertEqual(function_call["name"], "Read ❌")

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


class RestSnapshotOwui012Tests(unittest.TestCase):
    """The REST mid-run snapshot is a no-op on OWUI 0.12 (its
    `update_chat_by_id` keeps the stored copy of an unfinished assistant
    message) and redundant there (GET /api/v1/chats/{id} overlays the live
    stream). Measured live on 0.12.0: snapshot POSTs returned 200 while the
    DB content stayed empty until `done`."""

    @staticmethod
    def _owui_modules(with_turn_insert: bool):
        class FakeChats:
            pass
        if with_turn_insert:
            async def insert_chat_turn(chat_id, user, user_message, message_ids):
                return {}
            FakeChats.insert_chat_turn = staticmethod(insert_chat_turn)
        mod = types.ModuleType("open_webui.models.chats")
        mod.Chats = FakeChats
        return {
            "open_webui": types.ModuleType("open_webui"),
            "open_webui.models": types.ModuleType("open_webui.models"),
            "open_webui.models.chats": mod,
        }

    def _run_snapshot(self, modules):
        pipe = Pipe()
        with mock.patch.dict(sys.modules, modules), \
                mock.patch("urllib.request.urlopen",
                           side_effect=OSError("no network in tests")) as urlopen:
            asyncio.run(pipe._rest_snapshot_message("chat-1", "msg-1", "partial", "tok"))
        return urlopen

    def test_skipped_on_owui_012(self):
        urlopen = self._run_snapshot(self._owui_modules(with_turn_insert=True))
        urlopen.assert_not_called()

    def test_still_runs_on_older_owui(self):
        urlopen = self._run_snapshot(self._owui_modules(with_turn_insert=False))
        urlopen.assert_called()


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

    def _install_fake_owui_socket_module(self):
        fake_module = types.ModuleType("open_webui.socket.main")
        sys.modules.setdefault("open_webui", types.ModuleType("open_webui"))
        sys.modules.setdefault("open_webui.socket", types.ModuleType("open_webui.socket"))
        sys.modules["open_webui.socket.main"] = fake_module
        self.addCleanup(sys.modules.pop, "open_webui.socket.main", None)
        return fake_module

    async def test_reaps_and_disconnects_a_stale_connection_from_a_prior_deploy(self):
        fake_socket_module = self._install_fake_owui_socket_module()

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
        fake_socket_module = self._install_fake_owui_socket_module()
        conn = _GatewayConnection(lambda: None)

        disconnect_calls = {"n": 0}
        conn.disconnect = mock.AsyncMock(side_effect=lambda: disconnect_calls.__setitem__("n", disconnect_calls["n"] + 1))
        fake_socket_module._openclaw_gateway_connection_v1 = conn

        with mock.patch("openclaw_pipe._gateway_connection", conn):
            await _reap_stale_gateway_connection()

        self.assertEqual(disconnect_calls["n"], 0)

    async def test_reap_is_a_noop_when_no_anchor_present(self):
        self._install_fake_owui_socket_module()
        with mock.patch("openclaw_pipe._gateway_connection", None):
            await _reap_stale_gateway_connection()  # must not raise

    async def test_reap_is_a_noop_outside_owui(self):
        sys.modules.pop("open_webui.socket.main", None)
        sys.modules.pop("open_webui.socket", None)
        sys.modules.pop("open_webui", None)
        with mock.patch("openclaw_pipe._gateway_connection", None):
            await _reap_stale_gateway_connection()  # must not raise

    async def test_disconnect_failure_is_swallowed_not_fatal(self):
        fake_socket_module = self._install_fake_owui_socket_module()

        class _FakeStaleConn:
            async def disconnect(self):
                raise RuntimeError("socket already dead")

        fake_socket_module._openclaw_gateway_connection_v1 = _FakeStaleConn()

        with mock.patch("openclaw_pipe._gateway_connection", None):
            await _reap_stale_gateway_connection()  # must not raise

    async def test_old_code_stale_connection_cannot_be_revived_after_reap(self):
        # Connections built before `retire()` existed get pinned shut on the
        # instance, so an in-flight run of the old module can't reconnect it.
        fake_socket_module = self._install_fake_owui_socket_module()

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
        fake_socket_module = self._install_fake_owui_socket_module()
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
        self._install_fake_owui_socket_module()
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
        fake_socket_module = self._install_fake_owui_socket_module()
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

        async def emitter(event):
            events.append(event)

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

        async def emitter(event):
            events.append(event)

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

        async def emitter(event):
            events.append(event)

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

    def test_builds_owui_input_modal_payload(self):
        # A free-text prompt with no numbered options stays a text input.
        payload, is_confirmation = _modal_payload_from_user_input_prompt(
            "Codex needs input:\n\nPackage\nName the package you want"
        )

        self.assertEqual(payload["type"], "input")
        self.assertEqual(payload["data"]["title"], "Package")
        self.assertIn("Name the package", payload["data"]["message"])
        self.assertEqual(
            payload["data"]["placeholder"],
            "Reply with a number or your answer",
        )
        self.assertFalse(is_confirmation)

    def test_numbered_options_build_choice_payload(self):
        # An enumerated option list becomes a clickable "choice" payload.
        payload, is_confirmation = _modal_payload_from_user_input_prompt(
            "OpenClaw needs input:\n\nFavorite color?\nPick one\n1. Red\n2. Green\n3. Blue"
        )
        self.assertFalse(is_confirmation)
        self.assertEqual(payload["type"], "choice")
        self.assertEqual(payload["data"]["title"], "Favorite color?")
        self.assertEqual(payload["data"]["options"], ["Red", "Green", "Blue"])

    def test_extract_numbered_options_parses_dot_and_paren(self):
        opts = _extract_numbered_options("intro\n1. alpha\n2) beta\n  3. gamma\nfooter")
        self.assertEqual(opts, [("1", "alpha"), ("2", "beta"), ("3", "gamma")])

    def test_extract_numbered_options_none(self):
        self.assertEqual(_extract_numbered_options("no options here"), [])

    def test_build_choice_modal_js_returns_execute_promise(self):
        payload = _build_choice_modal_js("Title", "Message", ["Red", "Green"])
        self.assertEqual(payload["type"], "execute")
        code = payload["data"]["code"]
        # Must resolve INSIDE a Promise -- a bare top-level resolve() silently
        # ReferenceErrors under OWUI's execute wrapper (learned live).
        self.assertIn("new Promise", code)
        self.assertIn("resolve(", code)
        # Option labels round-trip into the embedded JSON config.
        self.assertIn("Red", code)
        self.assertIn("Green", code)

    def test_choice_modal_answer_normalizes_to_label(self):
        # Button clicks resolve to {value: <label>}.
        self.assertEqual(_normalize_event_call_response({"value": "Green"}), "Green")

    def test_multiselect_marker_sets_multi_flag(self):
        # A numbered-option prompt with a multiselect marker is still a choice,
        # but flagged multi=True and the marker stripped from the display text.
        payload, is_confirmation = _modal_payload_from_user_input_prompt(
            "OpenClaw needs input:\n\nToppings (multiselect)\n"
            "Pick any\n1. Cheese\n2. Olives\n3. Mushrooms"
        )
        self.assertFalse(is_confirmation)
        self.assertEqual(payload["type"], "choice")
        self.assertTrue(payload["data"]["multi"])
        self.assertEqual(
            payload["data"]["options"], ["Cheese", "Olives", "Mushrooms"]
        )
        # The raw "(multiselect)" directive must not leak into the shown title.
        self.assertNotIn("multiselect", payload["data"]["title"].lower())
        self.assertEqual(payload["data"]["title"], "Toppings")

    def test_multiselect_hebrew_marker(self):
        payload, _ = _modal_payload_from_user_input_prompt(
            "OpenClaw needs input:\n\nמה בא לך? בחר כמה\n1. פיצה\n2. סושי"
        )
        self.assertEqual(payload["type"], "choice")
        self.assertTrue(payload["data"]["multi"])

    def test_single_select_default_not_multi(self):
        # No marker => single-select (multi False), unchanged behavior.
        payload, _ = _modal_payload_from_user_input_prompt(
            "OpenClaw needs input:\n\nColor?\nPick\n1. Red\n2. Green"
        )
        self.assertEqual(payload["type"], "choice")
        self.assertFalse(payload["data"]["multi"])

    def test_build_choice_modal_js_multi_has_submit_and_join(self):
        payload = _build_choice_modal_js(
            "Title", "Message", ["Red", "Green"], multi=True
        )
        code = payload["data"]["code"]
        # Multi mode accumulates a selection and resolves a joined string on
        # an explicit Submit, rather than one-click-and-done. The shared JS
        # body branches on the embedded cfg.multi flag at runtime.
        self.assertIn("selected.join(', ')", code)
        self.assertIn("Submit", code)
        self.assertIn("new Promise", code)
        self.assertIn(r'multi\": true', code)

    def test_build_choice_modal_js_single_flags_multi_false(self):
        # Single-select (default) keeps one-click-and-done: cfg.multi is false,
        # so the shared JS never enters the checkbox/Submit branch at runtime.
        payload = _build_choice_modal_js("Title", "Message", ["Red", "Green"])
        code = payload["data"]["code"]
        self.assertIn(r'multi\": false', code)
        self.assertIn("finish({value: label})", code)

    def test_multiselect_answer_normalizes_to_joined_labels(self):
        # Submit resolves {value: "a, b"}; passes through verbatim.
        self.assertEqual(
            _normalize_event_call_response({"value": "Cheese, Olives"}),
            "Cheese, Olives",
        )

    def test_marks_secret_prompts_as_password_inputs(self):
        payload, is_confirmation = _modal_payload_from_user_input_prompt(
            "Codex needs input:\n\nToken\nThis channel may show your reply to other participants."
        )

        self.assertEqual(payload["data"]["type"], "password")
        self.assertFalse(is_confirmation)

    def test_normalizes_event_call_response_shapes(self):
        self.assertEqual(_normalize_event_call_response("  answer  "), "answer")
        self.assertEqual(_normalize_event_call_response({"value": "1"}), "1")
        self.assertEqual(_normalize_event_call_response({"confirmed": True}), "yes")
        self.assertEqual(_normalize_event_call_response({"confirmed": False}), "no")

    def test_normalizes_bare_boolean_confirmation_response(self):
        # OWUI confirmation modals resolve to a bare bool, not a dict.
        self.assertEqual(_normalize_event_call_response(True), "yes")
        self.assertEqual(_normalize_event_call_response(False), "no")

    def test_confirmation_modal_for_explicit_yes_no(self):
        payload, is_confirmation = _modal_payload_from_user_input_prompt(
            "OpenClaw needs input:\n\nProceed?\nRun the deploy now? (y/n)"
        )
        self.assertTrue(is_confirmation)
        self.assertEqual(payload["type"], "confirmation")

    def test_confirmation_modal_for_confirm_verb_question(self):
        payload, is_confirmation = _modal_payload_from_user_input_prompt(
            "OpenClaw needs input:\n\nDelete file?\nDelete config.json?"
        )
        self.assertTrue(is_confirmation)
        self.assertEqual(payload["type"], "confirmation")

    def test_choice_question_is_not_confirmation(self):
        # A short "?" title that is really a free-text choice must NOT become
        # a yes/no dialog (would silently strip the real answer).
        payload, is_confirmation = _modal_payload_from_user_input_prompt(
            "OpenClaw needs input:\n\nWhich model?\nWhich model should I use?"
        )
        self.assertFalse(is_confirmation)
        self.assertEqual(payload["type"], "input")

    def test_numbered_options_never_confirmation(self):
        # An enumerated option list is a choice even if a confirm verb appears.
        payload, is_confirmation = _modal_payload_from_user_input_prompt(
            "OpenClaw needs input:\n\nProceed how?\nProceed?\n1. rebase\n2. merge"
        )
        self.assertFalse(is_confirmation)
        self.assertEqual(payload["type"], "choice")
        self.assertEqual(payload["data"]["options"], ["rebase", "merge"])

    def test_secret_marker_does_not_match_innocent_key_substring(self):
        # "monkey" contains "key" -- must not be flagged as a secret.
        payload, _ = _modal_payload_from_user_input_prompt(
            "OpenClaw needs input:\n\nName the monkey\nWhat should we call the monkey?"
        )
        self.assertNotEqual(payload["data"].get("type"), "password")

    async def test_ask_user_input_modal_returns_answer(self):
        calls = []

        async def event_call(event):
            calls.append(event)
            return {"value": "pipx"}

        answer = await _ask_user_input_modal(
            event_call,
            "Codex needs input:\n\nPackage\nChoose\n1. curl\n2. pipx",
        )

        self.assertEqual(answer, "pipx")
        # A numbered-option prompt is delivered as a clickable-button overlay
        # (an "execute" event), not a free-text input.
        self.assertEqual(calls[0]["type"], "execute")
        self.assertIn("resolve(", calls[0]["data"]["code"])

    async def test_ask_user_input_modal_noops_without_event_call(self):
        answer = await _ask_user_input_modal(
            None,
            "Codex needs input:\n\nPackage\nChoose",
        )

        self.assertIsNone(answer)

    async def test_ask_user_input_modal_times_out(self):
        async def event_call(event):
            await asyncio.sleep(0.05)
            return {"value": "late"}

        # asyncio.TimeoutError, not the builtin: the two are the same class only
        # from 3.11 on. `asyncio.wait_for` raises asyncio's on every version, so
        # asserting the builtin passes on 3.11+ and fails on 3.10. The runtime
        # code catches asyncio.TimeoutError throughout and is version-correct;
        # this assertion was the only version-fragile spot.
        with self.assertRaises(asyncio.TimeoutError):
            await _ask_user_input_modal(
                event_call,
                "Codex needs input:\n\nPackage\nChoose",
                timeout_s=0.01,
            )

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

    def _install_fake_owui_socket_module(self, fake_sio, session_pool):
        fake_module = types.ModuleType("open_webui.socket.main")
        fake_module.sio = fake_sio
        fake_module.SESSION_POOL = session_pool
        sys.modules.setdefault("open_webui", types.ModuleType("open_webui"))
        sys.modules.setdefault("open_webui.socket", types.ModuleType("open_webui.socket"))
        sys.modules["open_webui.socket.main"] = fake_module

    async def test_ask_user_input_modal_retries_after_reconnect(self):
        session_pool = {}

        class _FakeSio:
            def __init__(self):
                self.calls = []

            async def call(self, event, data, to=None, timeout=None):
                self.calls.append((event, data, to, timeout))
                return {"value": "42"}

        fake_sio = _FakeSio()
        self._install_fake_owui_socket_module(fake_sio, session_pool)
        self.addCleanup(sys.modules.pop, "open_webui.socket.main", None)

        async def event_call(payload):
            raise asyncio.TimeoutError()

        async def populate_session_soon():
            await asyncio.sleep(0.02)
            session_pool["sid-123"] = {"id": "user-1"}

        populate_task = asyncio.ensure_future(populate_session_soon())
        try:
            answer = await _ask_user_input_modal(
                event_call,
                "OpenClaw needs input:\n\nQ\nAnswer?\n1. a\n2. b",
                timeout_s=0.01,
                owui_user_id="user-1",
                owui_chat_id="chat-1",
                owui_message_id="msg-1",
                max_wait_s=1,
                poll_interval_s=0.01,
            )
        finally:
            await populate_task

        self.assertEqual(answer, "42")
        self.assertEqual(fake_sio.calls[0][2], "sid-123")
        self.assertEqual(fake_sio.calls[0][1]["chat_id"], "chat-1")
        self.assertEqual(fake_sio.calls[0][1]["message_id"], "msg-1")

    async def test_reconnect_refires_to_same_session_after_a_timeout(self):
        # Regression (2026-07-18): a reconnect modal that popped but wasn't
        # clicked in time used to time out and then NEVER re-fire (seen_sids
        # blocked the same session forever). Now a timed-out fire must be
        # retried to the same live session on a later poll.
        session_pool = {"sid-123": {"id": "user-1"}}

        class _FakeSio:
            def __init__(self):
                self.calls = 0

            async def call(self, event, data, to=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise asyncio.TimeoutError()  # away / didn't click in time
                return {"value": "clicked"}

        fake_sio = _FakeSio()
        self._install_fake_owui_socket_module(fake_sio, session_pool)
        self.addCleanup(sys.modules.pop, "open_webui.socket.main", None)

        async def event_call(payload):
            raise asyncio.TimeoutError()

        answer = await _ask_user_input_modal(
            event_call,
            "OpenClaw needs input:\n\nQ\nAnswer?\n1. a\n2. b",
            timeout_s=0.01,
            owui_user_id="user-1",
            owui_chat_id="chat-1",
            owui_message_id="msg-1",
            max_wait_s=1,
            poll_interval_s=0.01,
        )

        self.assertEqual(answer, "clicked")
        self.assertGreaterEqual(fake_sio.calls, 2)  # re-fired after the timeout

    async def test_reconnect_gives_modal_a_human_interaction_timeout(self):
        # The re-fired sio.call must wait a real human-interaction budget for a
        # click (OWUI's WEBSOCKET_EVENT_CALLER_TIMEOUT, default 300s), not the
        # old 30s that made modals vanish before they could be answered.
        session_pool = {"sid-123": {"id": "user-1"}}
        captured = {}

        class _FakeSio:
            async def call(self, event, data, to=None, timeout=None):
                captured["timeout"] = timeout
                return {"value": "ok"}

        self._install_fake_owui_socket_module(_FakeSio(), session_pool)
        self.addCleanup(sys.modules.pop, "open_webui.socket.main", None)

        async def event_call(payload):
            raise asyncio.TimeoutError()

        await _ask_user_input_modal(
            event_call,
            "OpenClaw needs input:\n\nQ\nAnswer?\n1. a\n2. b",
            timeout_s=0.01,
            owui_user_id="user-1",
            max_wait_s=1,
            poll_interval_s=0.01,
        )

        self.assertGreaterEqual(captured.get("timeout", 0), 300)

    async def test_ask_user_input_modal_gives_up_if_never_reconnects(self):
        session_pool = {}

        class _FakeSio:
            async def call(self, *args, **kwargs):
                raise AssertionError("should never be called if user never reconnects")

        self._install_fake_owui_socket_module(_FakeSio(), session_pool)
        self.addCleanup(sys.modules.pop, "open_webui.socket.main", None)

        async def event_call(payload):
            raise asyncio.TimeoutError()

        answer = await _ask_user_input_modal(
            event_call,
            "OpenClaw needs input:\n\nQ\nAnswer?\n1. a\n2. b",
            timeout_s=0.01,
            owui_user_id="user-1",
            max_wait_s=0.05,
            poll_interval_s=0.01,
        )

        self.assertIsNone(answer)


class NativeAskUserCardTests(unittest.IsolatedAsyncioTestCase):
    """OWUI >= 0.11.1 renders `request:user_input` as its native question card."""

    def setUp(self):
        sys.modules.setdefault("open_webui", types.ModuleType("open_webui"))
        env = types.ModuleType("open_webui.env")
        env.VERSION = "0.12.0"
        sys.modules["open_webui.env"] = env
        self.addCleanup(sys.modules.pop, "open_webui.env", None)
        # Treat every cancel as seen and every call as fast, so a test decides
        # the outcome through the response alone.
        patcher = mock.patch.object(openclaw_pipe, "_NATIVE_CARD_UNSEEN_CANCEL_S", -1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_version_gate(self):
        self.assertTrue(_owui_supports_native_ask_user())
        sys.modules["open_webui.env"].VERSION = "0.11.0"
        self.assertFalse(_owui_supports_native_ask_user())
        sys.modules.pop("open_webui.env")
        self.assertFalse(_owui_supports_native_ask_user())

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

    def test_secret_and_multiselect_keep_the_legacy_modal(self):
        self.assertIsNone(_native_ask_user_payload(
            "OpenClaw needs input:\n\nToken\nPaste your API key"
        ))
        self.assertIsNone(_native_ask_user_payload(
            "OpenClaw needs input:\n\nToppings (multiselect)\nPick\n1. Cheese\n2. Olives"
        ))

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

        with mock.patch.object(openclaw_pipe, "_NATIVE_CARD_UNSEEN_CANCEL_S", 0):
            answer = await _ask_user_input_modal(
                event_call, "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b",
                owui_user_id="user-1", max_wait_s=1, poll_interval_s=0.01,
            )
        self.assertIsNone(answer)
        self.assertEqual(len(calls), 1)

    async def test_timeout_and_unseen_cancel_reask(self):
        responses = [
            {"error": "Event call timed out. The browser tab may be inactive or closed."},
            {"status": "cancelled", "answers": {}},  # instant: viewing another chat
            {"status": "answered", "answers": {"answer": {"type": "option", "label": "b"}}},
        ]

        async def event_call(event):
            return responses.pop(0)

        with mock.patch.object(openclaw_pipe, "_NATIVE_CARD_UNSEEN_CANCEL_S", 10):
            answer = await _ask_user_input_modal(
                event_call, "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b",
                owui_user_id="user-1", max_wait_s=1, poll_interval_s=0.01,
            )
        self.assertEqual(answer, "b")
        self.assertEqual(responses, [])

    async def test_disconnected_session_moves_to_the_users_new_socket(self):
        sent = {"calls": [], "emits": []}

        class _FakeSio:
            async def call(self, event, data, to=None, timeout=None):
                sent["calls"].append((data, to, timeout))
                return {"status": "answered", "answers": {"answer": {"type": "option", "label": "a"}}}

            async def emit(self, event, data, to=None):
                sent["emits"].append(data)

        socket_main = types.ModuleType("open_webui.socket.main")
        socket_main.sio = _FakeSio()
        socket_main.SESSION_POOL = {"sid-new": {"id": "user-1"}}
        sys.modules.setdefault("open_webui.socket", types.ModuleType("open_webui.socket"))
        sys.modules["open_webui.socket.main"] = socket_main
        self.addCleanup(sys.modules.pop, "open_webui.socket.main", None)

        async def event_call(event):
            return {"error": "Client session disconnected."}

        answer = await _ask_user_input_modal(
            event_call, "OpenClaw needs input:\n\nQ\nWhich?\n1. a\n2. b",
            owui_user_id="user-1", owui_chat_id="chat-1", owui_message_id="msg-1",
            max_wait_s=1, poll_interval_s=0.01,
        )
        self.assertEqual(answer, "a")
        data, to, timeout = sent["calls"][0]
        self.assertEqual(to, "sid-new")
        self.assertEqual(timeout, 240)
        self.assertEqual(data["chat_id"], "chat-1")
        interaction_id = data["data"]["data"]["interaction_id"]
        # The card is removed from the screen once the call settles.
        self.assertEqual(sent["emits"][0]["data"], {
            "type": "request:interaction:done", "data": {"interaction_id": interaction_id}})


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
            models = await _discover_models(FakeValves())
            self.assertEqual(len(models), 5)
            keys = {m["key"] for m in models}
            self.assertIn("deepseek/deepseek-v4-flash", keys)
            self.assertIn("anthropic/claude-opus-4-8", keys)
        finally:
            ocp._gateway_connection = orig_conn

    async def test_discover_models_normalizes_live_gateway_response(self):
        """Live gateway responses use id/name/provider/alias, not key/tags --
        _discover_models must normalize them before returning/caching."""
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
            models = await _discover_models(FakeValves())
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
            models = await _discover_models(FakeValves())
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
        self._fake_owui = types.ModuleType("open_webui.socket.main")
        self._saved = {
            k: sys.modules.get(k)
            for k in ("open_webui", "open_webui.socket", "open_webui.socket.main")
        }
        sys.modules["open_webui"] = types.ModuleType("open_webui")
        sys.modules["open_webui.socket"] = types.ModuleType("open_webui.socket")
        sys.modules["open_webui.socket.main"] = self._fake_owui

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

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
        data = _modal_payload_from_user_input_prompt(self.REAL)[0]["data"]
        self.assertEqual(data["options"],
                         ["Dev-tool micro-product",
                          "Prediction markets, small bankroll",
                          "Park it"])

    def test_no_option_label_leaks_the_internal_marker(self):
        data = _modal_payload_from_user_input_prompt(self.REAL)[0]["data"]
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
        data = _modal_payload_from_user_input_prompt(
            "OpenClaw needs input:\nPick one\n1. Alpha\n2. Beta\n3. Gamma")[0]["data"]
        self.assertEqual(data["title"], "Pick one")
        self.assertEqual(data["options"], ["Alpha", "Beta", "Gamma"])

    def test_dedupe_preserves_first_seen_order(self):
        self.assertEqual(_dedupe_options(["b", "a", "B", "a", "c"]), ["b", "a", "c"])

    def test_dedupe_drops_blanks(self):
        self.assertEqual(_dedupe_options(["a", "  ", "", "a"]), ["a"])


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
        data = _modal_payload_from_user_input_prompt(pending)[0]["data"]
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
        data = _modal_payload_from_user_input_prompt(pending)[0]["data"]
        self.assertEqual(data["title"], "Pick a theme")
        self.assertEqual(data["options"], ["Dark", "Light", "Auto"])


class _HandshakeWS:
    """Minimal Gateway handshake partner: challenge, then the connect reply."""

    def __init__(self, ok=True, boot_id=None):
        hello = {"server": {"bootId": boot_id}} if boot_id else {}
        self._frames = [
            json.dumps({"event": "connect.challenge",
                        "payload": {"nonce": "n", "ts": 1}}),
            json.dumps({"ok": ok, "payload": hello,
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

    async def _connect(self, conn, ok=True, boot_id=None):
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

    async def test_without_a_boot_id_a_reconnect_counts_as_a_restart(self):
        """Older Gateways send no bootId; keep the old behaviour there."""
        conn = self._conn()
        await self._connect(conn)
        self.assertEqual(conn.gateway_restarts, 0)
        await self._connect(conn)
        self.assertEqual(conn.gateway_restarts, 1)


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

        async def main():
            openclaw_pipe.spawn_background(work())
            gc.collect()
            self.assertEqual(len(openclaw_pipe._BACKGROUND_TASKS), 1)
            for _ in range(5):
                await asyncio.sleep(0)

        asyncio.run(main())
        self.assertEqual(ran, [True])
        self.assertEqual(openclaw_pipe._BACKGROUND_TASKS, set())

    def test_no_bare_create_task_left_in_sources(self):
        # A create_task whose result is discarded is exactly the bug; ruff's
        # RUF006 enforces this in CI, this keeps it true without ruff.
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
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
        src = open(os.path.join(os.path.dirname(__file__), "src", "openclaw_pipe_pkg",
                                "action.py")).read()
        self.assertIn('idempotency_key=f"{ACTION_RUN_PREFIX}compact-', src)



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
        self.socket_main = types.ModuleType("open_webui.socket.main")
        mods = {"open_webui": types.ModuleType("open_webui"),
                "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.chats": chats_mod,
                "open_webui.models.chat_messages": cm_mod,
                "open_webui.socket": types.ModuleType("open_webui.socket"),
                "open_webui.socket.main": self.socket_main,
                "sqlalchemy": types.ModuleType("sqlalchemy"),
                "sqlalchemy.orm": types.ModuleType("sqlalchemy.orm"),
                "sqlalchemy.orm.attributes": attrs}
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


class ExitTextFixtureTest(unittest.TestCase):
    """The owner rule matches the Gateway's exit-error TEXT: the dist has no
    structured exit field (DESIGN 2.7). The fixture is the exact source line
    extracted from the installed dist; this test renders its templates and,
    when the dist is installed here, fails if the line has changed."""

    FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "test_fixture_openclaw_exit_errors.json")
    DIST = os.environ.get("OPENCLAW_DIST", "/config/.node_global/lib/node_modules/openclaw/dist")

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
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
        try:
            from extract_openclaw_exit_errors import extract
        finally:
            sys.path.pop(0)
        if not os.path.isdir(self.DIST):
            self.skipTest(f"no OpenClaw dist at {self.DIST}")
        got = extract(self.DIST)
        self.assertIsNotNone(got, "the exit-error line is gone from the dist: re-check the owner rule")
        self.assertEqual(got["source"], self.fx["source"],
                         f"OpenClaw {got['openclaw_version']} changed the exit-error text "
                         f"(fixture: {self.fx['openclaw_version']}); re-check turn_owner, then "
                         "re-extract with scripts/extract_openclaw_exit_errors.py --write")


class CronPromptFixtureTest(unittest.TestCase):
    """ELI-103: a wake is found by its admitted user turn's TEXT, the prefix
    `[cron:<jobId> <name>]`; the turn has no structured cron field. The
    fixture holds the exact dist lines that build it; this test renders them
    and, when the dist is installed here, fails if they have changed."""

    FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "test_fixture_openclaw_cron_prompt.json")
    DIST = ExitTextFixtureTest.DIST
    JOB = "1542eaee-50d8-4583-8d9b-d28bb12e0145"

    def setUp(self):
        self.fx = json.load(open(self.FIXTURE))

    def render(self, name, message, time_line="Current time: x"):
        base = re.search(r"`([^`]*)`", self.fx["base"]["source"]).group(1)
        base = (base.replace("${input.job.id}", self.JOB).replace("${input.job.name}", name)
                .replace("${message}", message)).strip()
        body = re.search(r"`([^`]*)`", self.fx["command_body"]["source"]).group(1)
        self.assertNotIn("${", body.replace("${base}", "").replace("${timeLine}", ""), body)
        return body.replace("${base}", base).replace("\\n", "\n").replace("${timeLine}", time_line)

    def turn(self, text):
        return {"role": "user", "content": text, "idempotencyKey": "turn-1:user", "timestamp": 5}

    def test_a_rendered_wake_prompt_is_a_wake_turn_of_its_job(self):
        for name in ("eli103-wake", "nightly digest (v2)"):
            with self.subTest(name):
                got = wake_turns([self.turn(self.render(name, "Check the queue."))])
                self.assertEqual([t["job"] for t in got], [self.JOB])

    def test_the_text_seen_live_is_a_wake_turn(self):
        for label, text in self.fx["observed_turn_texts"].items():
            with self.subTest(label):
                self.assertEqual([t["job"] for t in wake_turns([self.turn(text)])], [self.JOB])

    def test_the_installed_dist_still_has_the_fixture_lines(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
        try:
            from extract_openclaw_cron_prompt import extract
        finally:
            sys.path.pop(0)
        if not os.path.isdir(self.DIST):
            self.skipTest(f"no OpenClaw dist at {self.DIST}")
        got = extract(self.DIST)
        self.assertIsNotNone(got, "the cron prompt lines are gone from the dist: re-check wake_turns")
        for part in ("base", "command_body"):
            self.assertEqual(got[part]["source"], self.fx[part]["source"],
                             f"OpenClaw {got['openclaw_version']} changed the cron prompt "
                             f"(fixture: {self.fx['openclaw_version']}); re-check _CRON_PROMPT_RE, "
                             "then re-extract with scripts/extract_openclaw_cron_prompt.py --write")


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
        lock = asyncio.Lock()
        fake = self

        class Chats:
            @staticmethod
            @contextlib.asynccontextmanager
            async def _chat_transaction(chat_id):
                async with lock:
                    await asyncio.sleep(0)
                    yield ("db", fake.chat)

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                msgs = fake.chat.chat["history"]["messages"]
                msgs[message_id] = {**msgs.get(message_id, {}), **message}
                return fake.chat

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
                                        save_response_stream=save_response_stream),
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

    async def test_the_db_is_written_on_item_boundaries_and_the_stream_on_every_event(self):
        w = self.writer()
        await w.feed(_delta("Reading"))           # opens: first item
        writes_after_open = self.owui.msgs()["m1"].get("content")
        await w.feed(_delta(" the logs"))         # same item: stream only
        self.assertEqual(self.owui.msgs()["m1"].get("content"), writes_after_open)
        await w.feed(_tool("start"))              # new item: boundary
        self.assertEqual(self.owui.msgs()["m1"]["content"].strip()[:16], "Reading the logs")
        self.assertEqual([i["type"] for i in self.owui.msgs()["m1"]["output"]],
                         ["message", "function_call"])
        await w.feed(_tool("result"))             # tool result: boundary
        self.assertIn("function_call_output", [i["type"] for i in self.owui.msgs()["m1"]["output"]])
        self.assertIs(self.owui.msgs()["m1"]["done"], False)
        self.assertEqual(len(self.owui.streams), 4, "the response stream gets every event")
        self.assertEqual(self.owui.streams[-1][0], w.task_id)



class TurnWriterEndTests(_TurnWriterCase):
    """ELI-100 DESIGN 2.5 (end on the run's settled row, from the transcript),
    2.7 (resume / gateway / wait, budget on M) and Stop."""

    RUN = "agent-run-1"

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

    async def test_a_settled_run_is_finalized_from_its_transcript(self):
        w = await self.opened_writer()
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
        self.assertIs(self.m()["done"], True)
        self.assertEqual(self.m()["content"], "Reading them. Found it.")
        self.assertEqual([i["type"] for i in self.m()["output"]],
                         ["message", "function_call", "function_call_output", "message"])
        self.assertEqual(self.owui.events("chat:active")[-1]["data"], {"active": False})
        self.assertTrue((await self.task_done(w)).done(), "the Open WebUI task ends with the run")
        self.assertTrue(w.closed)

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
        self.assertIn(_INTENT_HOOK_JS, [e["data"]["code"] for e in self.owui.events("execute")])

    async def test_stop_aborts_the_run_and_finalizes_what_it_has(self):
        w = await self.opened_writer()
        await self.cancel_from_the_page(w, "stop")
        (_e, call, sid), = self.owui.calls
        self.assertEqual((sid, call["message_id"]), ("sid-1", "m1"),
                         "only a tab showing this chat is asked, about M")
        self.assertIn(("chat.abort", {"sessionKey": self.KEY, "runId": self.RUN}), self.sent)
        self.assertIs(self.m()["done"], True)
        self.assertTrue(w.closed)

    async def test_send_now_keeps_writing_the_agent_turn(self):
        """DESIGN case 8: Send now cancels the task too, but the run keeps
        its message to the end; the queued message runs after (case 7)."""
        w = await self.opened_writer()
        await self.cancel_from_the_page(w, "send_now")
        self.assertNotIn("chat.abort", [m for m, _ in self.sent])
        self.assertFalse(w.closed)
        await w.feed(_delta(" Found it."))
        await w.end({"status": "done", "hasActiveRun": False, "lastRunId": self.RUN})
        self.assertIs(self.m()["done"], True)
        self.assertEqual(self.m()["content"], "Reading them. Found it.")

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

    async def run_turn(self, run, *, text=("Reading them.", " Found it."), settle=None):
        self.conn._on_sessions_changed({"sessionKey": self.key, "phase": "start", "runId": run,
                                        "hasActiveRun": True, "status": "running"})
        handled = [await _turn_route_event(self.conn, self.ev(run, _delta(text[0])))]
        handled.append(await _turn_route_event(self.conn, self.ev(run, _tool("start", tcid="toolu_1"))))
        handled.append(await _turn_route_event(self.conn, self.ev(run, _tool("result", tcid="toolu_1"))))
        handled.append(await _turn_route_event(self.conn, self.ev(run, _delta(text[1]))))
        handled.append(await _turn_route_event(self.conn, self.ev(run, {"state": "final"})))
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
        return await _turn_route_event(self.conn, self.ev(run, payload))

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
             "content": [{"type": "text", "text": " Found it."}]},
            {"role": "assistant", "idempotencyKey": f"cli-assistant:{self.REC}",
             "content": [{"type": "text", "text": "Found it."}]}]}


class AgentTurnRoutingTests(_RoutingCase):
    """ELI-100 DESIGN 2.1-2.5 wired: a run on an OWUI key that no live request
    consumes is written by a turn writer, as one ordinary message."""

    async def test_an_agent_turn_renders_as_one_ordinary_message(self):
        handled = await self.run_turn(self.RUN)
        self.assertTrue(all(handled))
        (mid, m), = self.new_messages().items()
        self.assertEqual(mid, turn_message_id(self.key, self.RUN, self.CHAT))
        self.assertEqual((m["parentId"], m["done"]), ("a1", True))
        self.assertEqual(m["content"], "Reading them. Found it.")
        self.assertEqual(sum(1 for i in m["output"] if i["type"] == "function_call"), 1)
        self.assertIn(("sessions.messages.subscribe", {"key": self.key}), self.sent,
                      "tool events of a run this socket did not start come as session.tool")

    async def test_a_recovery_continues_the_interrupted_message(self):
        # The interrupted run streamed, then the Gateway crashed: the row is running.
        self.history = {"messages": [
            {"role": "user", "content": "x", "idempotencyKey": f"{self.RUN}:user"},
            {"role": "user", "content": "[System] continue", "idempotencyKey": f"{self.REC}:user",
             "provenance": {"kind": "internal_system", "sourceTool": "main_session_restart_recovery"}}]}
        await _turn_route_event(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": True,
                                        "status": "running"})
        await self.spin()
        await self.run_turn(self.REC, text=("Again.", " Found it."),
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
        await _turn_route_event(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        await self.spin()
        self.history = self.crashed_then_recovered_history()
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.REC})
        await self.spin()
        (mid, m), = self.new_messages().items()
        self.assertEqual(mid, turn_message_id(self.key, self.RUN, self.CHAT))
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Reading them. Found it.")
        self.assertEqual(sum(1 for i in m["output"] if i["type"] == "function_call"), 1,
                         "the recovery's tool card comes from the transcript")

    async def test_after_hello_an_idle_session_ends_its_open_writer(self):
        """No settle reached this socket (it was down). The first hello's
        snapshot does not list the session as active, so its row is read and
        ends the writer: no wait for a push that already happened."""
        await _turn_route_event(self.conn, self.ev(self.RUN, _delta("Reading them.")))
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
        self.assertEqual(m["content"], "Reading them. Found it.")

    async def test_after_hello_an_active_session_waits_for_its_settle(self):
        await _turn_route_event(self.conn, self.ev(self.RUN, _delta("Reading them.")))
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
        await self.dispatch(run, _delta(" Found it."))
        await self.dispatch(run, {"state": "final"})
        self.history = {"messages": []}
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": run})
        await self.spin()
        self.assertEqual(self.new_messages().keys(), {self.STUB}, "no second message")
        m = self.owui.msgs()[self.STUB]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Reading them. Found it.")

    async def test_handover_an_inline_reply_that_ends_early_continues_in_its_own_message(self):
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB, content="Reading them.")
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB)
        self.conn._run_renderers[f"{self.key}:{run}"].feed(_delta("Reading them."))
        # The request is gone (no consumer): the run's next events.
        self.assertTrue(await self.dispatch(run, _tool("start", tcid="toolu_1")))
        await self.dispatch(run, _tool("result", tcid="toolu_1"))
        await self.dispatch(run, _delta(" Found it."))
        await self.dispatch(run, {"state": "final"})
        self.history = _turn_history(run=run)
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": run})
        await self.spin()
        self.assertEqual(self.new_messages().keys(), {self.STUB}, "no second message")
        m = self.owui.msgs()[self.STUB]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Reading them. Found it.")
        self.assertEqual(sum(1 for i in m["output"] if i["type"] == "function_call"), 1)
        self.assertEqual(m["meta"]["openclaw_writer"], self.conn.writer_id)
        self.assertNotIn(f"{self.key}:{run}", self.conn._run_targets, "the shadow is retired")

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
        self.assertTrue(await _turn_route_event(self.conn, self.ev(self.RUN, hb)))
        self.assertTrue(await _turn_route_event(self.conn, self.ev(self.RUN, _tool("start", tcid="t"))))
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.RUN})
        await self.spin()
        self.assertEqual(self.new_messages(), {})
        self.assertEqual(self.conn._turn_pumps, {})

    async def test_a_run_a_live_request_consumes_is_not_routed(self):
        q = self.conn.register_consumer(self.key, "msg-run")
        self.addCleanup(self.conn.unregister_consumer, self.key, "msg-run", queue=q)
        self.assertFalse(await _turn_route_event(self.conn, self.ev("msg-run", _delta("hi"))))

    async def test_the_action_connection_never_writes_turns(self):
        conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"), writes_turns=False)
        self.assertFalse(await _turn_route_event(conn, self.ev(self.RUN, _delta("hi"))))

    async def test_a_retired_connection_writes_no_turns(self):
        self.conn._retired = True
        self.assertFalse(await _turn_route_event(self.conn, self.ev(self.RUN, _delta("hi"))))
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
        self.assertTrue(await _turn_route_event(
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
        frames = list(frames)

        class ScriptedWS:
            async def recv(self_):
                if frames:
                    return json.dumps(frames.pop(0))
                conn._stopped = True
                raise asyncio.CancelledError  # stop, not a tick timeout (which marks a gap)

        conn._ws = ScriptedWS()
        await conn._event_loop()
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
                        _tool("result", tcid="toolu_1"), _delta(" Found it."), {"state": "final"}):
            for conn in (self.conn, other):
                self.assertTrue(await _turn_route_event(conn, self.ev(self.RUN, payload)))
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
        self.assertEqual((m["content"], m["done"], self.cards(m)), ("Reading them. Found it.", True, 1))
        self.assertEqual(n_tasks, 1, "one Open WebUI task: only one connection writes M")
        self.assertEqual(n_intro, 1, "M introduced once")
        self.assertEqual(len(self.owui.events("chat:completion")), 1, "M finalized once")

    async def test_tool_cards_from_the_transcript(self):
        """ToolCardsFromTranscriptTest: events after the first tool call were
        lost; M ends with the transcript's cards."""
        await _turn_route_event(self.conn, self.ev(self.RUN, _tool("start", tcid="toolu_0")))
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
        await _turn_route_event(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        self.conn._stop_notice_seen = stop_notice
        await _turn_route_event(self.conn, self.ev(self.RUN, {"state": terminal}))
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
        await _turn_route_event(self.conn, self.ev(self.RUN, _delta("Reading them.")))
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
        await _turn_route_event(self.conn, self.ev(self.RUN, _delta("Reading them.")))
        self.conn.active_run_snapshot = {self.key: []}
        await _turn_after_hello(self.conn)
        self.history = self.crashed_then_recovered_history()
        for payload in (_tool("start", tcid="toolu_9"), _tool("result", tcid="toolu_9"),
                        _delta(" Found it."), {"state": "final"}):
            await _turn_route_event(self.conn, self.ev(self.REC, payload))
        self.settled(self.REC, status="done")
        await self.spin()
        self.assertNotIn("chat.send", [m for m, _ in self.sent])
        (mid, m), = self.new_messages().items()
        self.assertEqual(mid, turn_message_id(self.key, self.RUN, self.CHAT))
        self.assertEqual((m["content"], self.cards(m), m["done"]), ("Reading them. Found it.", 1, True))

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
        self.assertEqual((m["content"], m["done"]), ("Reading them. Found it.", True))

    async def test_a_heartbeat_runs_session_tool_events_open_nothing(self):
        """The receive loop drops events flagged isHeartbeat before routing;
        the run's session.tool copies carry no flag and must not open M."""
        hb = {"type": "event", "event": "agent",
              "payload": {**self.ev(self.RUN, _delta("checking")), "isHeartbeat": True}}
        tool = {"type": "event", "event": "session.tool",
                "payload": self.ev(self.RUN, _tool("start", tcid="toolu_1"))}
        await self.scripted_loop(self.conn, [hb, tool])
        self.assertEqual(self.new_messages(), {})


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

    async def test_a_routed_handover_keeps_the_turns_thinking_level(self):
        """R6: the handover that `_turn_route_event` makes (the request ended
        first) carries the level like `hand_over_run` does, so a resume keeps it."""
        run = pipe_run_id(self.CHAT, self.STUB)
        self.add_stub(self.STUB)
        self.conn.register_run_target(self.key, run, self.CHAT, self.STUB, thinking="high")
        self.conn._run_renderers[f"{self.key}:{run}"].feed(_delta("Reading."))
        await self.dispatch(run, _delta(" More."))
        await self.spin()
        self.assertEqual(self.conn._turn_writers[(self.CHAT, self.STUB)].thinking, "high")


class InlineRewriteWiringTests(unittest.TestCase):
    """The inline reply records each rewrite the writer must repeat, and moves
    its target to an answer run (R4, R9). Checked on the source: the inline
    loop has no unit harness (e2e drives it)."""

    def test_the_inline_reply_records_its_rewrites_and_answer_runs(self):
        src = inspect.getsource(openclaw_pipe.Pipe._pipe_turn)
        self.assertEqual(src.count("conn.continue_run_target("), 2, "both answer switches")
        self.assertGreaterEqual(src.count("conn.note_turn_rewrite("), 5,
                                "2 MEDIA sites, 2 answered cards, 1 fallback prompt")
        self.assertIn("thinking=resolved_thinking", src.split("conn.register_run_target(")[1][:200])



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

    def test_no_silence_timer_is_left_in_the_inline_loop(self):
        src = inspect.getsource(openclaw_pipe.Pipe._pipe_turn)
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

    def parent_history(self, reply):
        imported = {"importedFrom": "claude-cli"}
        return {"messages": [
            {"role": "user", "content": "spawn it", "idempotencyKey": "msg-x-1.0:user"},
            {"role": "assistant", "__openclaw": imported, "content": [
                {"type": "toolcall", "id": "toolu_s", "name": "mcp__openclaw__sessions_spawn",
                 "arguments": {"task": "Reply with exactly SUB-DONE-R10.", "label": "R10 probe sub-agent"}},
                {"type": "tool_result", "tool_use_id": "toolu_s", "content": [{"type": "text", "text":
                    json.dumps({"status": "accepted", "childSessionKey": self.CHILD,
                                "runId": self.CHILD_RUN})}]}]},
            {"role": "assistant", "content": [{"type": "text", "text": "SPAWNED"}],
             "idempotencyKey": "cli-assistant:msg-x-1.0"},
            {"role": "assistant", "content": [{"type": "text", "text": reply}],
             "idempotencyKey": f"cli-assistant:{self.announce}"}]}

    async def announce_run(self, reply):
        self.history = self.parent_history(reply)
        await _turn_route_event(self.conn, self.ev(self.announce, _delta(reply)))
        await _turn_route_event(self.conn, self.ev(self.announce, {"state": "final"}))
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

    async def test_an_announce_the_parent_answered_shows_only_the_answer(self):
        await self.announce_run("The sub-agent returned SUB-DONE-R10.")
        (m,) = self.new_messages().values()
        self.assertEqual(m["content"], "The sub-agent returned SUB-DONE-R10.")
        self.assertFalse([i for i in m["output"] if i["type"] == "function_call"])

    async def test_a_silent_announce_whose_child_is_gone_stays_silent(self):
        self.child_history = None
        await self.announce_run("NO_REPLY")
        self.assertEqual(self.new_messages(), {})

    async def test_a_silent_requester_settle_wake_stays_silent(self):
        """`announce:requester-settle:...` wakes a yielded parent for a batch
        of children; its own silence is the parent's, not a missing result."""
        self.announce = f"announce:requester-settle:main:{self.key}:{self.CHILD_RUN}"
        await self.announce_run("NO_REPLY")
        self.assertEqual(self.new_messages(), {})


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


class NoMidRunCloseTests(unittest.IsolatedAsyncioTestCase):
    """NoMidRunCloseTest: `sessions.describe` says "done" during a sub-agent
    yield gap while `sessions.list` still has the run active (ELI-56). The
    inline reply's idle probe must not read that as the end of the run; on
    main it closed the reply after 30 s of silence."""

    SK = _owui_session_key("main", "11111111-1111-1111-1111-111111111111",
                           "22222222-2222-2222-2222-222222222222")

    def conn(self, describe, active):
        conn = _GatewayConnection(lambda: None)

        async def send_request(method, params, timeout=10):
            if method == "sessions.describe":
                return {"session": {"status": describe}}
            if method == "sessions.list":
                return {"sessions": [{"key": self.SK, "hasActiveRun": active}]}
            raise AssertionError(method)
        conn.send_request = send_request
        return conn

    async def test_done_while_the_run_is_active_is_running(self):
        self.assertEqual(await _gateway_run_status(self.conn("done", True), self.SK), "running")

    async def test_done_and_idle_is_done(self):
        self.assertEqual(await _gateway_run_status(self.conn("done", False), self.SK), "done")

    async def test_an_active_describe_needs_no_list(self):
        self.assertEqual(await _gateway_run_status(self.conn("running", False), self.SK), "running")


if __name__ == "__main__":
    unittest.main()


class EventGapTests(unittest.IsolatedAsyncioTestCase):
    """Events lost while the Gateway socket reconnects (no replay exists:
    sequence numbers are per connection)."""

    def test_align_finds_the_lost_chunks_and_the_tail(self):
        whole = "a" * 30 + "LOST1" + "b" * 30 + "LOST2" + "c" * 30 + "TAIL"
        received = "a" * 30 + "b" * 30 + "c" * 30
        tail, missing = _align_whole_text(whole, received, [30, 60])
        self.assertEqual(tail, "TAIL")
        self.assertEqual(missing, [(30, "LOST1"), (60, "LOST2")])

    def test_align_gap_at_the_start(self):
        tail, missing = _align_whole_text("123456", "456", [0])
        self.assertEqual((tail, missing), ("", [(0, "123")]))

    def test_align_refuses_a_different_text(self):
        self.assertIsNone(_align_whole_text("something else", "hello there", [5]))

    def test_whole_text_with_a_hole_yields_only_what_follows(self):
        whole = "".join(f"{i}\n" for i in range(1, 400))
        received = "".join(f"{i}\n" for i in range(184, 400))
        self.assertEqual(_item_delta_text(whole, "", received), "")
        self.assertEqual(_item_delta_text(whole + "END", "", received), "END")

    def test_short_baseline_keeps_the_old_rule(self):
        # "OK." in a later, different text must not swallow it.
        self.assertEqual(_item_delta_text("Fine. Then more text. OK.", "", "OK."),
                         "Fine. Then more text. OK.")

    def test_repair_fields_put_the_chunk_into_output_and_content(self):
        msg = {"content": "4\n5\n6\n",
               "output": [{"type": "message", "content": [
                   {"type": "output_text", "text": "4\n5\n6\n"}]}]}
        fields = _gap_repair_fields(msg, "1\n2\n3\n", "4\n5\n", "")
        self.assertEqual(fields["content"], "1\n2\n3\n4\n5\n6\n")
        self.assertEqual(fields["output"][0]["content"][0]["text"], "1\n2\n3\n4\n5\n6\n")
        self.assertEqual(msg["output"][0]["content"][0]["text"], "4\n5\n6\n", "input untouched")

    def test_repair_fields_after_anchor_and_no_anchor(self):
        msg = {"content": "x\ny\n", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "x\ny\n"}]}]}
        self.assertEqual(_gap_repair_fields(msg, "z\n", "", "x\ny\n")["content"], "x\ny\nz\n")
        self.assertIsNone(_gap_repair_fields(msg, "z\n", "nope", "nope"))

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

    async def test_sink_applies_waiting_repairs_on_chat_finished(self):
        import openclaw_pipe as ocp
        seen = []

        async def fake_apply(conn, repair):
            seen.append(repair["chunk"])
            return True

        ocp._take_gap_repairs("c", "m")
        ocp._gap_repair_registry()["c:m"] = [{"chunk": "lost", "chat_id": "c", "message_id": "m"}]
        event = types.SimpleNamespace(event="chat.finished", data={"chat_id": "c", "message_id": "m"})
        with mock.patch.object(ocp, "_apply_gap_repair", fake_apply), \
                mock.patch.object(ocp, "_gateway_connection", _GatewayConnection(lambda: None)):
            await _GapRepairSink().handle_event(None, types.SimpleNamespace(event="chat.started", data={}))
            self.assertEqual(seen, [])
            await _GapRepairSink().handle_event(None, event)
        self.assertEqual(seen, ["lost"])
        self.assertEqual(ocp._take_gap_repairs("c", "m"), [])


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

    def wake_turn(self, key, text="Wake (worker ELI-100): deploy finished."):
        """The cron executor marks its job running, then commits the wake's
        user turn before the CLI starts."""
        at = self.tick()
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
            await _turn_route_event(self.conn, self.sid_ev(_delta(text)))
        self.imported("".join(texts))
        self.closing("".join(texts))
        await _turn_route_event(self.conn, self.sid_ev({"state": "final"}))
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

    async def test_a_sessionid_run_with_no_running_wake_opens_nothing(self):
        """Review (c): a node agent command or a compaction also runs with
        runId = sessionId. With no cron wake of its own it is a stray."""
        await self.wake(self.W1, "First wake.")
        first = copy.deepcopy(self.new_messages())
        await _turn_route_event(self.conn, self.sid_ev(_tool("start", tcid="toolu_9")))
        await _turn_route_event(self.conn, self.sid_ev(_delta("compaction note")))
        await _turn_route_event(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        self.assertEqual(self.new_messages(), first, "a stray reopened or added a message")
        self.assertFalse(self.conn._turn_pumps)

    async def test_a_fresh_connection_does_not_bind_a_stray_to_an_old_wake(self):
        await self.wake(self.W1, "First wake.")
        first = copy.deepcopy(self.new_messages())
        self.conn._wake_turns_bound.clear()  # a new deploy's connection knows no wake
        await _turn_route_event(self.conn, self.sid_ev(_delta("late stray")))
        await _turn_route_event(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        self.assertEqual(self.new_messages(), first)

    async def test_a_stray_opens_no_message_for_an_old_wake_shown_elsewhere(self):
        """The old wake ran under main, which showed it as its own bubble: no
        message carries its turn's id, so only its cron state can say a later
        sessionId event is not that wake's."""
        self.wake_turn(self.W1)
        self.job_finished()
        await _turn_route_event(self.conn, self.sid_ev(_delta("compaction note")))
        await _turn_route_event(self.conn, self.sid_ev({"state": "final"}))
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
        await _turn_route_event(self.conn, self.sid_ev(_delta("Wake text.")))
        await _turn_route_event(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        self.assertEqual(list(self.new_messages()), [turn_message_id(self.key, self.W1, self.CHAT)])

    async def test_a_heartbeat_flagged_wake_id_opens_nothing_and_frees_the_id(self):
        """Review (f): skipped by its resolved identity, not by the run id
        every wake shares."""
        self.wake_turn(self.W1)
        await _turn_route_event(self.conn, self.sid_ev({**_delta("HEARTBEAT_OK"), "isHeartbeat": True}))
        await _turn_route_event(self.conn, self.sid_ev({"state": "final", "isHeartbeat": True}))
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
        await _turn_route_event(self.conn, self.sid_ev(_delta("Reruns all passed.")))
        await self.spin()
        await _turn_resync(self.conn)
        await self.spin()
        await _turn_route_event(self.conn, self.sid_ev(_delta(" Tests written.")))
        await _turn_route_event(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        self.assertEqual([m["content"] for m in self.new_messages().values()],
                         ["Reruns all passed. Tests written."])

    async def test_another_runs_settle_row_does_not_end_the_wake(self):
        self.wake_turn(self.W1)
        await _turn_route_event(self.conn, self.sid_ev(_delta("Reruns all passed.")))
        await self.spin()
        # The queued user message's row: idle, and naming another run.
        self.queued_turn()
        self.conn._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False,
                                        "status": "done", "lastRunId": self.R})
        await self.spin()
        await _turn_route_event(self.conn, self.sid_ev(_delta(" Tests written.")))
        await _turn_route_event(self.conn, self.sid_ev({"state": "final"}))
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
        await _turn_route_event(self.conn, self.sid_ev(_delta("Second wake.")))
        # " All done." never reached the socket; the import has it.
        self.imported("Second wake. All done.")
        await _turn_route_event(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        second = self.new_messages()[turn_message_id(self.key, self.W2, self.CHAT)]
        self.assertEqual(second["content"], "Second wake. All done.")

    async def test_a_turn_queued_behind_the_wake_is_not_the_wakes(self):
        self.wake_turn(self.W1)
        self.queued_turn()  # committed between the wake's turn and its first event
        await _turn_route_event(self.conn, self.sid_ev(_delta("Reruns all passed.")))
        await _turn_route_event(self.conn, self.sid_ev({"state": "final"}))
        await self.spin()
        self.assertEqual(list(self.new_messages()), [turn_message_id(self.key, self.W1, self.CHAT)])

    async def test_a_wake_whose_turn_is_not_committed_yet_waits_for_it(self):
        """The embedded runtime commits the user turn without awaiting it
        (embedded-agent-DNQn_PMM.mjs:5891): the first event can come first."""
        await self.wake(self.W1, "First wake.")
        await _turn_route_event(self.conn, self.sid_ev(_delta("Second ")))
        await self.spin()
        self.assertEqual(len(self.new_messages()), 1, "bound to the previous wake's turn")
        self.wake_turn(self.W2)
        await _turn_route_event(self.conn, self.sid_ev(_delta("wake.")))
        await _turn_route_event(self.conn, self.sid_ev({"state": "final"}))
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
        await _turn_route_event(self.conn, self.sid_ev(_delta("Working.")))
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
        self.assertEqual(wake_turns(msgs), [{"key": "w1", "at": 10, "job": "job-1"}])

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
                return {"entries": []}
            return await plain(method, params, timeout)
        self.jobs = []
        self.send_request = send_request
        self.conn.send_request = send_request

    def successor(self):
        conn = _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        conn.send_request = self.send_request
        return conn

    async def start_turn_then_retire(self):
        """A's writer streamed half a turn, then a deploy retired A."""
        self.active = True
        await _turn_route_event(self.conn, self.ev(self.RUN, _delta("Reading them.")))
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
        self.assertEqual(m["content"], "Reading them. Found it.")

    async def test_a_successor_continues_a_run_still_going(self):
        mid, _ = await self.start_turn_then_retire()
        b = self.successor()
        await self.adopt(b, mid)
        self.assertIs(self.owui.msgs()[mid]["done"], False, "a running run was finished")
        await _turn_route_event(b, self.ev(self.RUN, _delta(" Found it.")))
        await _turn_route_event(b, self.ev(self.RUN, {"state": "final"}))
        self.active = False
        b._on_sessions_changed({"sessionKey": self.key, "hasActiveRun": False, "status": "done",
                                "lastRunId": self.RUN, "reason": "chat.run.settled"})
        await self.spin()
        m = self.owui.msgs()[mid]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Reading them. Found it.")
        self.assertEqual(list(self.new_messages()), [mid], "the successor opened a second message")

    async def test_a_wake_is_adopted_with_its_own_turn(self):
        sid, w = "ad9e461b-724a-4797-9b3f-befa1090e36b", "1d549bd7-80a0-448a-ab9e-3dcbcad0015b"
        self.conn.note_session_id(self.key, sid)
        self.history = {"messages": [
            {"role": "user", "content": "[cron:job-9 wake] go", "idempotencyKey": f"{w}:user",
             "__openclaw": {"recordTimestampMs": 1000}}]}
        self.jobs = [{"id": "job-9", "state": {"runningAtMs": 900}}]
        await _turn_route_event(self.conn, {"sessionKey": self.key, "runId": sid, "ts": 2000,
                                            **_delta("Smoke ")})
        await self.spin()
        (mid, m), = self.new_messages().items()
        self.assertEqual(m["meta"]["openclaw_wake"], {"key": w, "at": 1000, "job": "job-9"})
        await self.conn.retire()
        await self.spin()
        self.history["messages"].append({"role": "assistant", "timestamp": 3000,
                                         "__openclaw": {"importedFrom": "claude-cli"},
                                         "content": [{"type": "text", "text": "Smoke PASS."}]})
        self.jobs = []  # the one-shot job ran and was deleted
        b = self.successor()
        await self.adopt(b, mid)  # cron.list: the job is gone, the wake is over
        m = self.owui.msgs()[mid]
        self.assertIs(m["done"], True)
        self.assertEqual(m["content"], "Smoke PASS.")

    async def test_a_message_another_pump_already_writes_is_left_to_it(self):
        mid, _ = await self.start_turn_then_retire()
        b = self.successor()
        await _turn_route_event(b, self.ev(self.RUN, _delta(" Found it.")))
        self.assertEqual(await self.adopt(b, mid), "skip")



class GatewayOwnerNeverRecoversTests(_RoutingCase):
    """Review 2af1 R2 (ReviewGatewayOwnerNeverRecovers): a settled row that
    says `running` keeps M open for the Gateway's recovery. When the Gateway
    gives up, it tombstones the row to failed
    (main-session-restart-recovery-CPJjd8da.mjs:1150): that row closes M."""

    async def keep_open(self):
        await _turn_route_event(self.conn, self.ev(self.RUN, _delta("Working on it.")))
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
        await self.run_turn(self.REC, text=("Again.", " Found it."),
                            settle={"sessionKey": self.key, "hasActiveRun": False, "status": "done",
                                    "lastRunId": self.REC, "reason": "chat.run.settled"})
        (m,) = self.new_messages().values()
        self.assertIs(m["done"], True)
        self.assertFalse(self.conn._turn_pumps, "the interrupted run's pump leaked")
