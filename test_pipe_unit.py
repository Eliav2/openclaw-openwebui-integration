#!/usr/bin/env python3
"""Focused unit tests for pipe stream recovery behavior."""

import unittest
import html
import os
import tempfile
import inspect
import json
import re
import sys
import time
import types
import asyncio
from unittest import mock

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

from openclaw_pipe import (
    _GatewayConnection,
    GatewayError,
    _parse_gateway_url,
    _explain_connect_rejection,
    _explain_session_patch_failure,
    _catch_all_delta,
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
    _append_proactive_message_to_chat,
    _emit_live_bootstrap_reload,
    LIVE_STREAM_BOOTSTRAP_ENABLED,
    LIVE_STREAM_RELAY_ENABLED,
    _RelayState,
    _relay_content_is_showable,
    _relay_begin,
    _relay_feed_event,
    _relay_finalize,
    _finalize_inline_message,
    _relinearize_proactive_variants,
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
    _deliver_proactive_owui_message,
    _deliver_subagent_proactive_owui_message,
    _discover_models,
    _emit_message_snapshot,
    _emit_status,
    _extract_image_attachments,
    _fmt_tokens,
    _friendly_name,
    _is_user_input_prompt,
    _item_assistant_text,
    _item_delta_text,
    _last_assistant_text_from_preview,
    _live_session_id_for_user,
    _maybe_deliver_proactive_after_debounce,
    _maybe_deliver_subagent_proactive,
    _modal_payload_from_user_input_prompt,
    _model_patch_matches,
    _normalize_event_call_response,
    _normalize_model_entry,
    _owui_chat_send_params,
    _owui_session_key,
    _parse_whitelist,
    _preview_recovery_text,
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


class ProactiveDeliveryTests(unittest.TestCase):
    """P33/ELI-17: persisting a proactive (idle-session) turn into OWUI."""

    def _conn(self, agent_id="main"):
        return _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID=agent_id))

    def test_parses_own_session_key(self):
        conn = self._conn()
        session_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        self.assertEqual(
            conn.parse_owui_session_key(session_key),
            (
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ),
        )

    def test_rejects_session_key_for_a_different_agent(self):
        conn = self._conn(agent_id="main")
        session_key = _owui_session_key(
            "other-agent", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        self.assertIsNone(conn.parse_owui_session_key(session_key))

    def test_rejects_non_owui_session_key(self):
        conn = self._conn()
        self.assertIsNone(conn.parse_owui_session_key("agent:main:some-other-channel-key"))

    def test_last_assistant_text_from_preview_takes_latest_assistant_turn(self):
        preview = {
            "previews": [{
                "key": "agent:main:test",
                "items": [
                    {"role": "assistant", "text": "older"},
                    {"role": "user", "text": "(no live turn, cron-triggered)"},
                    {"role": "assistant", "text": "the background check finished: all green"},
                ],
            }]
        }
        self.assertEqual(
            _last_assistant_text_from_preview(preview, "agent:main:test"),
            "the background check finished: all green",
        )

    def test_last_assistant_text_from_preview_none_when_no_assistant_turn(self):
        preview = {"previews": [{"key": "agent:main:test", "items": [{"role": "user", "text": "hi"}]}]}
        self.assertIsNone(_last_assistant_text_from_preview(preview, "agent:main:test"))

    def test_last_assistant_text_from_preview_filters_announce_skip_sentinel(self):
        preview = {
            "previews": [{
                "key": "agent:main:test",
                "items": [{"role": "assistant", "text": "ANNOUNCE_SKIP"}],
            }]
        }
        self.assertIsNone(_last_assistant_text_from_preview(preview, "agent:main:test"))

    def test_last_assistant_text_from_preview_filters_no_reply_sentinels(self):
        for sentinel in ("NO_REPLY", "no_reply"):
            preview = {
                "previews": [{
                    "key": "agent:main:test",
                    "items": [{"role": "assistant", "text": sentinel}],
                }]
            }
            self.assertIsNone(
                _last_assistant_text_from_preview(preview, "agent:main:test"),
                f"sentinel {sentinel!r} should be filtered",
            )

    def test_last_assistant_text_from_preview_sentinel_does_not_fall_back_to_older_turn(self):
        # A sentinel-only final leg means "nothing to show for THIS run" -- it
        # must not fall through to an older, already-delivered assistant
        # message from an earlier leg.
        preview = {
            "previews": [{
                "key": "agent:main:test",
                "items": [
                    {"role": "assistant", "text": "an earlier real reply"},
                    {"role": "user", "text": "(no live turn, cron-triggered)"},
                    {"role": "assistant", "text": "ANNOUNCE_SKIP"},
                ],
            }]
        }
        self.assertIsNone(_last_assistant_text_from_preview(preview, "agent:main:test"))

    def test_deliver_skips_non_owui_session_without_touching_gateway(self):
        conn = self._conn()
        conn.session_preview = mock.AsyncMock(side_effect=AssertionError("should not be called"))
        asyncio.run(_deliver_proactive_owui_message(conn, "agent:main:slack-some-channel", "run-1"))
        conn.session_preview.assert_not_called()

    def test_deliver_dedups_same_session_and_run(self):
        conn = self._conn()
        session_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn.session_preview = mock.AsyncMock(
            return_value={"previews": [{"key": session_key, "items": [
                {"role": "assistant", "text": "hello from cron"},
            ]}]}
        )
        # `open_webui` isn't installed in this test environment (it only exists
        # inside a running OWUI process), so the internal Chats import fails
        # and delivery no-ops after the preview fetch -- that's fine, this test
        # only asserts the dedup guard, not the OWUI-internals write path.
        asyncio.run(_deliver_proactive_owui_message(conn, session_key, "run-1"))
        asyncio.run(_deliver_proactive_owui_message(conn, session_key, "run-1"))
        # session_preview only called once -- the second call short-circuits on dedup.
        self.assertEqual(conn.session_preview.await_count, 1)

    def test_deliver_leaves_new_message_as_the_active_leaf(self):
        """Regression for the 2026-07-11 bug: the message was written to
        `history.messages` and pipe_log even reported success, but nothing
        ever showed up in OWUI. Root cause: OWUI's real
        `upsert_message_to_chat_by_id_and_message_id` sets
        `history['currentId'] = message_id` as a side effect of *every*
        call -- including the second call this function used to make (to
        patch the *old* leaf's `childrenIds`), which silently reverted
        `currentId` back to the old leaf right after the new message was
        set as current. The new message became an orphan branch: present
        in `history.messages`, but unreachable from `currentId`, so neither
        OWUI's frontend nor any API-based reconstruction ever showed it.
        This test replicates that real side effect via a fake `Chats` model
        and asserts `currentId` ends up on the new message, not the old one.
        """
        conn = self._conn()
        session_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn.session_preview = mock.AsyncMock(
            return_value={"previews": [{"key": session_key, "items": [
                {"role": "assistant", "text": "hello from cron"},
            ]}]}
        )

        old_leaf_id = "old-leaf"
        chat_state = {
            "history": {
                "currentId": old_leaf_id,
                "messages": {old_leaf_id: {"role": "user", "childrenIds": []}},
            }
        }

        class FakeChat:
            def __init__(self, chat):
                self.chat = chat

        class FakeChats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                return FakeChat(chat_state)

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                # Mirrors the real OWUI method: merge fields into the
                # message dict, then unconditionally repoint currentId at
                # whatever message_id was just touched.
                history = chat_state["history"]
                messages = history.setdefault("messages", {})
                messages[message_id] = {**messages.get(message_id, {}), **message}
                history["currentId"] = message_id
                return FakeChat(chat_state)

        fake_chats_module = types.ModuleType("open_webui.models.chats")
        fake_chats_module.Chats = FakeChats
        fake_models_module = types.ModuleType("open_webui.models")
        fake_owui_module = types.ModuleType("open_webui")
        with mock.patch.dict(sys.modules, {
            "open_webui": fake_owui_module,
            "open_webui.models": fake_models_module,
            "open_webui.models.chats": fake_chats_module,
        }):
            asyncio.run(_deliver_proactive_owui_message(conn, session_key, "run-1"))

        history = chat_state["history"]
        new_id = history["currentId"]
        self.assertNotEqual(new_id, old_leaf_id, "currentId was clobbered back to the old leaf")
        self.assertIn("hello from cron", history["messages"][new_id]["content"])
        self.assertIn("Proactive message", history["messages"][new_id]["content"])
        self.assertIn(new_id, history["messages"][old_leaf_id]["childrenIds"])

    def test_deepest_leaf_walks_past_currentid_with_existing_child(self):
        """Pure-function guard for the 2026-07-13 sibling bug: OWUI's
        `currentId` can point at an ancestor that already has a child (e.g.
        the frontend reset it right after a proactive message was appended).
        Anchoring there again forks a 1/2·2/2 variant; the leaf walk must skip
        down to the real childless tail. Also cycle- and multi-branch-safe."""
        messages = {
            "a": {"childrenIds": ["b"]},
            "b": {"childrenIds": ["c1", "c2"]},   # branched: follow newest
            "c1": {"childrenIds": []},
            "c2": {"childrenIds": ["d"]},
            "d": {"childrenIds": []},
        }
        self.assertEqual(_deepest_leaf_id(messages, "a"), "d")
        self.assertEqual(_deepest_leaf_id(messages, "d"), "d")
        self.assertIsNone(_deepest_leaf_id(messages, None))
        # Malformed self-cycle must terminate, not spin.
        self.assertEqual(_deepest_leaf_id({"x": {"childrenIds": ["x"]}}, "x"), "x")

    def test_deliver_chains_off_leaf_not_stale_currentid(self):
        """Integration regression for the observed variant-group bug: a prior
        proactive message (`existing`) is already a child of `anchor`, but
        `currentId` was reset back to `anchor`. The next proactive delivery
        must chain *below* `existing` (linear), not append a second child of
        `anchor` (which OWUI renders as swipeable 1/2·2/2 variants)."""
        conn = self._conn()
        session_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn.session_preview = mock.AsyncMock(
            return_value={"previews": [{"key": session_key, "items": [
                {"role": "assistant", "text": "second proactive"},
            ]}]}
        )

        chat_state = {
            "history": {
                # currentId reset to an ancestor that already has a child.
                "currentId": "anchor",
                "messages": {
                    "anchor": {"role": "assistant", "childrenIds": ["existing"]},
                    "existing": {"role": "assistant", "childrenIds": [],
                                 "content": "*↳ Sub-agent finished: x*"},
                },
            }
        }

        class FakeChat:
            def __init__(self, chat):
                self.chat = chat

        class FakeChats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                return FakeChat(chat_state)

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                history = chat_state["history"]
                messages = history.setdefault("messages", {})
                messages[message_id] = {**messages.get(message_id, {}), **message}
                history["currentId"] = message_id
                return FakeChat(chat_state)

        fake_chats_module = types.ModuleType("open_webui.models.chats")
        fake_chats_module.Chats = FakeChats
        fake_models_module = types.ModuleType("open_webui.models")
        fake_owui_module = types.ModuleType("open_webui")
        with mock.patch.dict(sys.modules, {
            "open_webui": fake_owui_module,
            "open_webui.models": fake_models_module,
            "open_webui.models.chats": fake_chats_module,
        }):
            asyncio.run(_deliver_proactive_owui_message(conn, session_key, "run-2"))

        history = chat_state["history"]
        new_id = history["currentId"]
        # New message chains below `existing`, NOT as a second child of anchor.
        self.assertEqual(history["messages"]["existing"]["childrenIds"], [new_id])
        self.assertEqual(history["messages"][new_id]["parentId"], "existing")
        self.assertEqual(history["messages"]["anchor"]["childrenIds"], ["existing"])
        self.assertIn("second proactive", history["messages"][new_id]["content"])

    def test_deliver_stamps_model_from_immediately_preceding_message(self):
        """Regression for the 2026-07-13 bug: proactively-delivered messages
        had no `model` field, so OWUI's frontend
        (`$models.find((m) => m.id === message.model)`) could never resolve
        any actions for them -- the Status button (and any future
        proactive-specific action) silently never rendered. The old leaf's
        own `model`/`modelName` (the branch this proactive message actually
        continues) is the most locally-accurate source, preferred over the
        chat's globally-selected `models` list.
        """
        conn = self._conn()
        session_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn.session_preview = mock.AsyncMock(
            return_value={"previews": [{"key": session_key, "items": [
                {"role": "assistant", "text": "hello from cron"},
            ]}]}
        )

        old_leaf_id = "old-leaf"
        chat_state = {
            "models": ["openclaw_gateway.other"],
            "history": {
                "currentId": old_leaf_id,
                "messages": {old_leaf_id: {
                    "role": "assistant",
                    "childrenIds": [],
                    "model": "openclaw_gateway.default",
                    "modelName": "OpenClaw · Default",
                }},
            },
        }

        class FakeChat:
            def __init__(self, chat):
                self.chat = chat

        class FakeChats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                return FakeChat(chat_state)

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                history = chat_state["history"]
                messages = history.setdefault("messages", {})
                messages[message_id] = {**messages.get(message_id, {}), **message}
                history["currentId"] = message_id
                return FakeChat(chat_state)

        fake_chats_module = types.ModuleType("open_webui.models.chats")
        fake_chats_module.Chats = FakeChats
        fake_models_module = types.ModuleType("open_webui.models")
        fake_owui_module = types.ModuleType("open_webui")
        with mock.patch.dict(sys.modules, {
            "open_webui": fake_owui_module,
            "open_webui.models": fake_models_module,
            "open_webui.models.chats": fake_chats_module,
        }):
            asyncio.run(_deliver_proactive_owui_message(conn, session_key, "run-1"))

        history = chat_state["history"]
        new_msg = history["messages"][history["currentId"]]
        self.assertEqual(new_msg["model"], "openclaw_gateway.default")
        self.assertEqual(new_msg["modelName"], "OpenClaw · Default")

    def test_deliver_falls_back_to_chat_selected_models_when_prior_leaf_has_none(self):
        """A user message (or a fresh chat with no prior assistant turn) has
        no `model` field of its own -- falls back to the chat's
        globally-selected `models` list so the button still appears rather
        than silently omitting `model` again."""
        conn = self._conn()
        session_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn.session_preview = mock.AsyncMock(
            return_value={"previews": [{"key": session_key, "items": [
                {"role": "assistant", "text": "hello from cron"},
            ]}]}
        )

        old_leaf_id = "old-leaf"
        chat_state = {
            "models": ["openclaw_gateway.default"],
            "history": {
                "currentId": old_leaf_id,
                "messages": {old_leaf_id: {"role": "user", "childrenIds": []}},
            },
        }

        class FakeChat:
            def __init__(self, chat):
                self.chat = chat

        class FakeChats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                return FakeChat(chat_state)

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                history = chat_state["history"]
                messages = history.setdefault("messages", {})
                messages[message_id] = {**messages.get(message_id, {}), **message}
                history["currentId"] = message_id
                return FakeChat(chat_state)

        fake_chats_module = types.ModuleType("open_webui.models.chats")
        fake_chats_module.Chats = FakeChats
        fake_models_module = types.ModuleType("open_webui.models")
        fake_owui_module = types.ModuleType("open_webui")
        with mock.patch.dict(sys.modules, {
            "open_webui": fake_owui_module,
            "open_webui.models": fake_models_module,
            "open_webui.models.chats": fake_chats_module,
        }):
            asyncio.run(_deliver_proactive_owui_message(conn, session_key, "run-1"))

        history = chat_state["history"]
        new_msg = history["messages"][history["currentId"]]
        self.assertEqual(new_msg["model"], "openclaw_gateway.default")

    def test_deliver_never_persists_announce_skip_sentinel(self):
        """End-to-end regression for the 2026-07-11 incident: a preview
        whose last assistant text is the literal protocol sentinel
        `ANNOUNCE_SKIP` must never reach OWUI's chat-write path at all --
        not just return non-matching text. Asserts the fake `Chats.get_chat_by_id`
        is never called, i.e. delivery bails out before touching chat history.
        """
        conn = self._conn()
        session_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn.session_preview = mock.AsyncMock(
            return_value={"previews": [{"key": session_key, "items": [
                {"role": "assistant", "text": "ANNOUNCE_SKIP"},
            ]}]}
        )

        class FakeChats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                raise AssertionError("should never touch chat history for a sentinel-only reply")

        fake_chats_module = types.ModuleType("open_webui.models.chats")
        fake_chats_module.Chats = FakeChats
        fake_models_module = types.ModuleType("open_webui.models")
        fake_owui_module = types.ModuleType("open_webui")
        with mock.patch.dict(sys.modules, {
            "open_webui": fake_owui_module,
            "open_webui.models": fake_models_module,
            "open_webui.models.chats": fake_chats_module,
        }):
            asyncio.run(_deliver_proactive_owui_message(conn, session_key, "run-1"))


class LiveBootstrapReloadTests(unittest.TestCase):
    """ELI-62 PoC: `_emit_live_bootstrap_reload` and its wiring into the two
    proactive-delivery call sites. Kill switch (`LIVE_STREAM_BOOTSTRAP_ENABLED`)
    defaults to False in production; these tests patch it True to exercise
    the emit path without needing it flipped on for real."""

    def _conn(self, agent_id="main"):
        return _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID=agent_id))

    def _fake_socket_main(self):
        """A minimal `open_webui.socket.main` stub with a mock `sio`,
        matching the shape `_emit_live_bootstrap_reload` imports from."""
        fake_socket_main = types.ModuleType("open_webui.socket.main")
        fake_socket_main.sio = mock.AsyncMock()
        fake_socket_module = types.ModuleType("open_webui.socket")
        fake_socket_module.main = fake_socket_main
        fake_owui_module = types.ModuleType("open_webui")
        return fake_socket_main, {
            "open_webui": fake_owui_module,
            "open_webui.socket": fake_socket_module,
            "open_webui.socket.main": fake_socket_main,
        }

    def test_noop_when_target_message_id_is_none(self):
        fake_socket_main, modules = self._fake_socket_main()
        with mock.patch.dict(sys.modules, modules):
            asyncio.run(_emit_live_bootstrap_reload("user-1", "chat-1", None))
        fake_socket_main.sio.emit.assert_not_called()

    def test_emits_execute_event_targeting_given_message_id(self):
        fake_socket_main, modules = self._fake_socket_main()
        with mock.patch.dict(sys.modules, modules):
            asyncio.run(_emit_live_bootstrap_reload("user-1", "chat-1", "old-leaf"))
        fake_socket_main.sio.emit.assert_awaited_once()
        args, kwargs = fake_socket_main.sio.emit.await_args
        self.assertEqual(args[0], "events")
        payload = args[1]
        self.assertEqual(payload["chat_id"], "chat-1")
        # Must target a message id ALREADY known to the frontend (the old
        # leaf), never the brand-new proactive message id -- OWUI's
        # chatEventHandler silently drops events for unknown message ids.
        self.assertEqual(payload["message_id"], "old-leaf")
        self.assertEqual(payload["data"]["type"], "execute")
        self.assertIn("location.reload()", payload["data"]["data"]["code"])
        self.assertEqual(kwargs["room"], "user:user-1")

    def test_survives_missing_open_webui_socket_module(self):
        # No stub installed at all -- simulates running outside OWUI's
        # process (e.g. a stray import in a non-OWUI context). Must not raise.
        with mock.patch.dict(sys.modules, {}, clear=False):
            sys.modules.pop("open_webui.socket.main", None)
            asyncio.run(_emit_live_bootstrap_reload("user-1", "chat-1", "old-leaf"))

    def _deliver_with_fakes(self, *, enabled: bool):
        conn = self._conn()
        session_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn.session_preview = mock.AsyncMock(
            return_value={"previews": [{"key": session_key, "items": [
                {"role": "assistant", "text": "hello from cron"},
            ]}]}
        )

        old_leaf_id = "old-leaf"
        chat_state = {
            "history": {
                "currentId": old_leaf_id,
                "messages": {old_leaf_id: {"role": "user", "childrenIds": []}},
            }
        }

        class FakeChat:
            def __init__(self, chat):
                self.chat = chat

        class FakeChats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                return FakeChat(chat_state)

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                history = chat_state["history"]
                messages = history.setdefault("messages", {})
                messages[message_id] = {**messages.get(message_id, {}), **message}
                history["currentId"] = message_id
                return FakeChat(chat_state)

        fake_chats_module = types.ModuleType("open_webui.models.chats")
        fake_chats_module.Chats = FakeChats
        fake_models_module = types.ModuleType("open_webui.models")
        fake_owui_module = types.ModuleType("open_webui")
        fake_socket_main, socket_modules = self._fake_socket_main()

        modules = {
            "open_webui": fake_owui_module,
            "open_webui.models": fake_models_module,
            "open_webui.models.chats": fake_chats_module,
            **socket_modules,
        }
        with mock.patch.dict(sys.modules, modules), \
             mock.patch("openclaw_pipe.LIVE_STREAM_BOOTSTRAP_ENABLED", enabled):
            asyncio.run(_deliver_proactive_owui_message(conn, session_key, "run-1"))
        return fake_socket_main, old_leaf_id, chat_state

    def test_bootstrap_disabled_by_default_never_touches_sio(self):
        fake_socket_main, _old_leaf_id, _chat_state = self._deliver_with_fakes(enabled=False)
        fake_socket_main.sio.emit.assert_not_called()

    def test_bootstrap_enabled_targets_the_old_leaf_after_persisting(self):
        fake_socket_main, old_leaf_id, chat_state = self._deliver_with_fakes(enabled=True)
        fake_socket_main.sio.emit.assert_awaited_once()
        _args, kwargs = fake_socket_main.sio.emit.await_args
        payload = _args[1]
        # The new message became currentId; the emit must target the OLD
        # leaf (already known to any open tab), not the new message.
        self.assertNotEqual(payload["message_id"], chat_state["history"]["currentId"])
        self.assertEqual(payload["message_id"], old_leaf_id)


class LiveRelayTests(unittest.TestCase):
    """ELI-62 PoC slice 2: true live token relay of a proactive run
    (`_relay_begin`/`_relay_feed_event`/`_relay_finalize`). Kill switch
    (`LIVE_STREAM_RELAY_ENABLED`) defaults False in production; these tests
    drive the helpers directly (which don't gate on the flag -- the event loop
    does), with fake OWUI Chats + sio, to exercise introduce/stream/finalize."""

    USER = "11111111-1111-1111-1111-111111111111"
    CHAT = "22222222-2222-2222-2222-222222222222"

    def _conn(self, agent_id="main"):
        return _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID=agent_id))

    def _session_key(self):
        return _owui_session_key("main", self.USER, self.CHAT)

    def _fakes(self):
        """Fake `open_webui.models.chats.Chats` (records appends + upserts) and
        `open_webui.socket.main.sio` (records emits). Returns (modules, chat_state,
        fake_sio)."""
        old_leaf_id = "old-leaf"
        chat_state = {
            "history": {
                "currentId": old_leaf_id,
                "messages": {old_leaf_id: {"role": "user", "childrenIds": []}},
            }
        }

        class FakeChat:
            def __init__(self, chat):
                self.chat = chat

        class FakeChats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                return FakeChat(chat_state)

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                messages = chat_state["history"].setdefault("messages", {})
                messages[message_id] = {**messages.get(message_id, {}), **message}
                chat_state["history"]["currentId"] = message_id
                return FakeChat(chat_state)

        fake_chats_module = types.ModuleType("open_webui.models.chats")
        fake_chats_module.Chats = FakeChats
        fake_models_module = types.ModuleType("open_webui.models")
        fake_owui_module = types.ModuleType("open_webui")
        fake_socket_main = types.ModuleType("open_webui.socket.main")
        fake_sio = mock.AsyncMock()
        fake_socket_main.sio = fake_sio
        fake_socket_module = types.ModuleType("open_webui.socket")
        fake_socket_module.main = fake_socket_main
        modules = {
            "open_webui": fake_owui_module,
            "open_webui.models": fake_models_module,
            "open_webui.models.chats": fake_chats_module,
            "open_webui.socket": fake_socket_module,
            "open_webui.socket.main": fake_socket_main,
        }
        return modules, chat_state, fake_sio, old_leaf_id

    @staticmethod
    def _evt(session_key, run_id, delta=None, *, final=False):
        payload = {"sessionKey": session_key, "runId": run_id}
        if final:
            payload["state"] = "final"
        if delta is not None:
            payload["stream"] = "assistant"
            payload["data"] = {"delta": delta}
        return payload

    @staticmethod
    def _emitted(fake_sio, ev_type):
        """All `events` emits of a given data.type, as their payload dicts."""
        out = []
        for call in fake_sio.emit.await_args_list:
            args, _kwargs = call
            if args and args[0] == "events" and args[1].get("data", {}).get("type") == ev_type:
                out.append(args[1])
        return out

    # ── static invariants ───────────────────────────────────────────────

    def test_kill_switch_is_on(self):
        # Flipped ON after the PoC phase; if you need to disable the relay,
        # flip the constant in src/ and update this test with it.
        self.assertTrue(LIVE_STREAM_RELAY_ENABLED)

    def test_content_is_showable_filters_sentinels_and_prefixes(self):
        self.assertFalse(_relay_content_is_showable(""))
        self.assertFalse(_relay_content_is_showable("   "))
        self.assertFalse(_relay_content_is_showable("NO_REPLY"))
        self.assertFalse(_relay_content_is_showable("ANNOUNCE_SKIP"))
        # "N" is a prefix of NO_REPLY -- could still resolve to a sentinel-only
        # run, so not yet showable.
        self.assertFalse(_relay_content_is_showable("N"))
        # A real reply diverges from every sentinel prefix immediately.
        self.assertTrue(_relay_content_is_showable("Nope, here's the answer"))
        self.assertTrue(_relay_content_is_showable("Hello"))

    # ── introduce + stream + finalize ───────────────────────────────────

    def test_full_run_introduces_streams_and_finalizes(self):
        conn = self._conn()
        sk = self._session_key()
        modules, chat_state, fake_sio, old_leaf_id = self._fakes()
        with mock.patch.dict(sys.modules, modules):
            asyncio.run(self._drive(conn, sk, "run-1", fake_sio,
                                    ["Hello", " world"], finalize=True))

        # Exactly one assistant bubble appended, done:true, full content.
        assistants = [
            m for m in chat_state["history"]["messages"].values()
            if m.get("role") == "assistant"
        ]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["content"], "Hello world")
        self.assertTrue(assistants[0]["done"])

        # Bootstrap reload nudge targeted the OLD leaf (known to the tab).
        execs = self._emitted(fake_sio, "execute")
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]["message_id"], old_leaf_id)
        self.assertIn("location.reload()", execs[0]["data"]["data"]["code"])

        # Streamed live: post-introduce text goes out as append deltas (the
        # introduce-time prefix rides the DB message the reloaded tab loads,
        # not a duplicate append), a final full `replace` re-anchors the tab to
        # the complete content, then a terminal chat:active false settles it.
        appends = self._emitted(fake_sio, "message")
        self.assertTrue(appends, "expected at least one append `message` event")
        streamed = "".join(a["data"]["data"]["content"] for a in appends)
        self.assertTrue(streamed, "append deltas should carry the post-introduce text")
        self.assertTrue("Hello world".endswith(streamed))
        replaces = self._emitted(fake_sio, "replace")
        self.assertTrue(replaces, "expected a final `replace` snapshot")
        self.assertEqual(replaces[-1]["data"]["data"]["content"], "Hello world")
        self.assertEqual(len(self._emitted(fake_sio, "chat:active")), 1)

        # Relay state cleaned up; run's proactive-dedup identity claimed so the
        # post-hoc path won't ALSO deliver it.
        self.assertEqual(conn._relay_sessions, {})
        self.assertIn(f"{sk}:run-1", conn._delivered_proactive)

    def test_sentinel_only_run_never_introduces_a_bubble(self):
        conn = self._conn()
        sk = self._session_key()
        modules, chat_state, fake_sio, _old = self._fakes()
        with mock.patch.dict(sys.modules, modules):
            asyncio.run(self._drive(conn, sk, "run-2", fake_sio,
                                    ["NO_REPLY"], finalize=True))
        # No assistant message, no socket emits at all, state cleaned up, and
        # the run was NOT claimed (post-hoc path stays free to no-op on it).
        assistants = [
            m for m in chat_state["history"]["messages"].values()
            if m.get("role") == "assistant"
        ]
        self.assertEqual(assistants, [])
        fake_sio.emit.assert_not_called()
        self.assertEqual(conn._relay_sessions, {})
        self.assertNotIn(f"{sk}:run-2", conn._delivered_proactive)

    def test_introduce_deferred_until_showable_content(self):
        conn = self._conn()
        sk = self._session_key()
        modules, chat_state, fake_sio, _old = self._fakes()
        with mock.patch.dict(sys.modules, modules):
            # First event carries a bare sentinel-prefix; must not introduce.
            asyncio.run(_relay_begin(conn, sk, "run-3", self._evt(sk, "run-3", "N")))
            self.assertFalse(fake_sio.emit.await_args_list)
            state = conn._relay_sessions[f"{sk}:run-3"]
            self.assertFalse(state.introduced)
            # Diverging into real text introduces the bubble.
            asyncio.run(_relay_feed_event(conn, sk, "run-3",
                                          self._evt(sk, "run-3", "ope!")))
            self.assertTrue(conn._relay_sessions[f"{sk}:run-3"].introduced)
            self.assertEqual(len(self._emitted(fake_sio, "execute")), 1)

    async def _drive(self, conn, sk, run_id, fake_sio, deltas, *, finalize):
        await _relay_begin(conn, sk, run_id, self._evt(sk, run_id, deltas[0]))
        for d in deltas[1:]:
            await _relay_feed_event(conn, sk, run_id, self._evt(sk, run_id, d))
        if finalize:
            await _relay_feed_event(conn, sk, run_id, self._evt(sk, run_id, final=True))


class SubagentToolEventRoutingTests(unittest.TestCase):
    """Documents, in the suite, why the drawer has no live tool-call watch.

    Measured against the live gateway 2026-07-30 with two device-signed
    connections on one session key while a real turn ran 2 Bash calls:

      * the connection that STARTED the run    -> 4 `agent`/stream="tool" events
      * a `sessions.messages.subscribe` observer -> 0 tool events
        (it did receive 15 thinking + 5 assistant + lifecycle events, so the
        subscription itself was live -- tool events specifically are withheld)

    There is also no `session.tool` event family; an earlier attempt at this
    feature subscribed for one and buffered nothing. Tool activity is `agent`
    with payload.stream == "tool", phase start/result, which `_TurnRenderer`
    already consumes for the pipe's OWN runs (see TurnRendererTests).

    Consequence: the reader loop must NOT grow a branch that buffers another
    session's tool events -- it would be dead code. Closing the gap needs the
    Gateway to fan tool events out to session-message subscribers, or a new RPC.
    """

    def test_reader_loop_dispatches_only_agent_and_chat(self):
        """The event filter is deliberately ("agent", "chat"). A third family
        for tool watching would never fire, so nothing should add one."""
        source = inspect.getsource(_GatewayConnection._event_loop)
        self.assertIn('msg.get("event") not in ("agent", "chat")', source)
        self.assertNotIn("session.tool\"", source)

    def test_no_live_tool_watch_surface_exists(self):
        conn = _GatewayConnection(lambda: None)
        for attr in ("watch_session_tools", "unwatch_session_tools",
                     "live_tool_calls", "_feed_tool_watch"):
            self.assertFalse(hasattr(conn, attr),
                             f"{attr} is unreachable by design -- see class docstring")


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
        source = inspect.getsource(Pipe._pipe_impl)
        self.assertIn("should_announce_thinking_note(session_key, thinking_note)", source)
        self.assertIn('pipe_log(f"thinking: {thinking_note}")', source)


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

    def test_has_any_consumer_false_for_genuinely_idle_session(self):
        conn = _GatewayConnection(lambda: None)
        conn.register_consumer("session-a", "run-1")
        self.assertFalse(conn.has_any_consumer_for_session("session-b"))

    def test_has_any_consumer_true_even_for_a_different_run_id(self):
        """Regression for the 2026-07-11 live incident (P33/ELI-17/ELI-19):
        an event for run-1 fails to match `consumers_for_event` (e.g. a
        steering handoff already re-registered the session under run-2), but
        the session is still genuinely live -- proactive delivery must not
        treat this as an idle wake just because *this specific run_id*
        has no consumer.
        """
        conn = _GatewayConnection(lambda: None)
        conn.register_consumer("session-a", "run-2")
        self.assertTrue(conn.has_any_consumer_for_session("session-a"))

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

    def test_session_idle_for_false_while_consumer_active(self):
        conn = _GatewayConnection(lambda: None)
        conn.register_consumer("session-a", "run-1")
        self.assertFalse(conn.session_idle_for("session-a", min_idle_s=120))

    def test_session_idle_for_true_immediately_for_never_seen_session(self):
        conn = _GatewayConnection(lambda: None)
        self.assertTrue(conn.session_idle_for("session-never-seen", min_idle_s=120))

    def test_session_idle_for_false_right_after_unregister(self):
        """Regression for the second 2026-07-11 incident: unregistering a
        consumer must NOT immediately count as idle -- the next leg's HTTP
        request (e.g. answering an ask-user modal) can still land a few
        seconds later under a brand-new run_id."""
        conn = _GatewayConnection(lambda: None)
        conn.register_consumer("session-a", "run-1")
        conn.unregister_consumer("session-a", "run-1")
        self.assertFalse(conn.session_idle_for("session-a", min_idle_s=120))

    def test_session_idle_for_true_after_min_idle_s_elapses(self):
        conn = _GatewayConnection(lambda: None)
        conn.register_consumer("session-a", "run-1")
        conn.unregister_consumer("session-a", "run-1")
        conn._session_last_activity["session-a"] = time.time() - 200
        self.assertTrue(conn.session_idle_for("session-a", min_idle_s=120))

    def test_session_idle_for_false_again_if_a_new_leg_reregisters(self):
        """Even after the quiet period has elapsed once, a fresh
        register_consumer call must reset the clock -- the session isn't
        idle again until the *new* leg also finishes and settles."""
        conn = _GatewayConnection(lambda: None)
        conn.register_consumer("session-a", "run-1")
        conn.unregister_consumer("session-a", "run-1")
        conn._session_last_activity["session-a"] = time.time() - 200
        conn.register_consumer("session-a", "run-2")
        self.assertFalse(conn.session_idle_for("session-a", min_idle_s=120))


class ProactiveDebounceWatcherTests(unittest.IsolatedAsyncioTestCase):
    """P33/ELI-19 (second incident, 2026-07-11): a debounce watcher must
    delay proactive delivery until the session has been genuinely idle for
    a sustained period, and must never deliver if the session stays busy."""

    def _conn(self, agent_id="main"):
        return _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID=agent_id))

    async def test_delivers_once_session_settles_idle(self):
        conn = self._conn()
        session_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        # Simulate: a leg just unregistered (not idle yet at min_idle_s=0.05s).
        conn.register_consumer(session_key, "run-1")
        conn.unregister_consumer(session_key, "run-1")

        delivered = []

        async def fake_deliver(c, sk, rid):
            delivered.append((sk, rid))

        with mock.patch("openclaw_pipe.PROACTIVE_DELIVERY_ENABLED", True), mock.patch(
            "openclaw_pipe._deliver_proactive_owui_message", side_effect=fake_deliver
        ):
            await _maybe_deliver_proactive_after_debounce(
                conn, session_key, "run-1",
                min_idle_s=0.05, poll_interval_s=0.02, max_wait_s=2,
            )

        self.assertEqual(delivered, [(session_key, "run-1")])
        # Watcher must clear itself from the pending-set on completion.
        self.assertNotIn(session_key, conn._pending_proactive_debounce)

    async def test_never_delivers_if_session_stays_busy_until_timeout(self):
        conn = self._conn()
        session_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        # A consumer is (and remains) registered the whole time -- session is
        # never idle, simulating an ongoing live conversation.
        conn.register_consumer(session_key, "run-2")
        conn._pending_proactive_debounce.add(session_key)

        with mock.patch("openclaw_pipe.PROACTIVE_DELIVERY_ENABLED", True), mock.patch(
            "openclaw_pipe._deliver_proactive_owui_message",
            side_effect=AssertionError("must not deliver into a busy session"),
        ):
            await _maybe_deliver_proactive_after_debounce(
                conn, session_key, "run-1",
                min_idle_s=120, poll_interval_s=0.02, max_wait_s=0.1,
            )

        self.assertNotIn(session_key, conn._pending_proactive_debounce)

    async def test_bails_immediately_if_kill_switch_flipped_off_mid_wait(self):
        conn = self._conn()
        session_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn.register_consumer(session_key, "run-1")  # never idle on its own
        conn._pending_proactive_debounce.add(session_key)

        with mock.patch("openclaw_pipe.PROACTIVE_DELIVERY_ENABLED", False), mock.patch(
            "openclaw_pipe._deliver_proactive_owui_message",
            side_effect=AssertionError("must not deliver once disabled"),
        ):
            await _maybe_deliver_proactive_after_debounce(
                conn, session_key, "run-1",
                min_idle_s=120, poll_interval_s=0.02, max_wait_s=5,
            )

        self.assertNotIn(session_key, conn._pending_proactive_debounce)

    async def test_skips_delivery_if_marked_delivered_live_during_wait(self):
        """A duplicate/retried final event for a run that gets shown to a
        (re)connected live tab *while* the debounce watcher is waiting must
        not still be proactively delivered once the wait ends -- the tab
        already persists it itself. This is the identity-based guard
        (`was_delivered_live`), not the content-matching approach, so it
        can't false-positive on coincidentally-identical text and doesn't
        depend on debounce timing."""
        conn = self._conn()
        session_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        # Session is idle from the start (min_idle_s=0), so the watcher's
        # only loop iteration is the post-loop was_delivered_live check.
        conn.mark_delivered_live(session_key, "run-1")

        with mock.patch("openclaw_pipe.PROACTIVE_DELIVERY_ENABLED", True), mock.patch(
            "openclaw_pipe._deliver_proactive_owui_message",
            side_effect=AssertionError("must not deliver a run already shown live"),
        ):
            await _maybe_deliver_proactive_after_debounce(
                conn, session_key, "run-1",
                min_idle_s=0, poll_interval_s=0.02, max_wait_s=2,
            )

        self.assertNotIn(session_key, conn._pending_proactive_debounce)


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


class SubagentSessionKeyTests(unittest.TestCase):
    """A sub-agent's own session key (`agent:*:subagent:*`) is never an OWUI
    session, so it must never be mistaken for one -- and vice versa."""

    def _conn(self, agent_id="main"):
        return _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID=agent_id))

    def test_recognizes_subagent_session_key(self):
        conn = self._conn()
        self.assertTrue(
            conn.is_subagent_session_key("agent:main:subagent:11111111-1111-1111-1111-111111111111")
        )

    def test_rejects_owui_session_key(self):
        conn = self._conn()
        session_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        self.assertFalse(conn.is_subagent_session_key(session_key))


class ResolveSubagentParentTaskTests(unittest.IsolatedAsyncioTestCase):
    """`resolve_subagent_parent_task`: `tasks.list` matches params.sessionKey
    against a task's requesterSessionKey, childSessionKey, *or* ownerKey, so
    passing the *child's own* session key still returns the task record --
    whose `sessionKey` field is the parent's, not the child's."""

    def _conn(self, agent_id="main"):
        return _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID=agent_id))

    async def test_finds_task_by_child_session_key(self):
        conn = self._conn()
        child_key = "agent:main:subagent:11111111-1111-1111-1111-111111111111"
        parent_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )

        async def fake_send_request(method, params, timeout=None):
            self.assertEqual(method, "tasks.list")
            self.assertEqual(params.get("sessionKey"), child_key)
            return {"tasks": [
                {"id": "task-1", "childSessionKey": "some-other-child", "sessionKey": "irrelevant"},
                {"id": "task-2", "childSessionKey": child_key, "sessionKey": parent_key, "title": "Do the thing"},
            ]}

        conn.send_request = mock.AsyncMock(side_effect=fake_send_request)
        task = await conn.resolve_subagent_parent_task(child_key)
        self.assertEqual(task, {
            "id": "task-2", "childSessionKey": child_key, "sessionKey": parent_key, "title": "Do the thing",
        })

    async def test_returns_none_when_no_task_matches(self):
        conn = self._conn()
        conn.send_request = mock.AsyncMock(return_value={"tasks": []})
        self.assertIsNone(await conn.resolve_subagent_parent_task("agent:main:subagent:x"))

    async def test_returns_none_on_rpc_failure(self):
        conn = self._conn()
        conn.send_request = mock.AsyncMock(side_effect=RuntimeError("gateway unreachable"))
        self.assertIsNone(await conn.resolve_subagent_parent_task("agent:main:subagent:x"))

    async def test_skips_self_referential_cli_task_and_returns_owui_parent(self):
        """Production reality: one sub-agent run yields TWO records whose
        childSessionKey is this child -- a self-referential runtime="cli"
        execution record (its mapped sessionKey is the child's OWN key) that
        tasks.list returns *first* because it's created a few ms later
        (newest-first), and the real spawn record whose sessionKey is the
        OWUI parent. Returning the first childSessionKey match hands back the
        self-referential one and delivery aborts. The resolver must skip any
        record whose sessionKey isn't a real OWUI session and return the
        OWUI-parent record instead."""
        conn = self._conn()
        child_key = "agent:main:subagent:d4baed2f-044c-45d6-b0a6-ba4343947ada"
        parent_key = _owui_session_key(
            "main", "2d206eab-acac-4945-bd2f-7e8b6365de8c",
            "c201663d-211a-4054-9be9-718eef3eb308",
        )

        async def fake_send_request(method, params, timeout=None):
            self.assertEqual(method, "tasks.list")
            return {"tasks": [
                # self-referential CLI execution record -- newest, returned first
                {"id": "task-cli", "childSessionKey": child_key,
                 "sessionKey": child_key, "runtime": "cli"},
                # real spawn record linking back to the OWUI parent
                {"id": "task-spawn", "childSessionKey": child_key,
                 "sessionKey": parent_key, "runtime": "subagent",
                 "title": "e2e-subagent-proof"},
            ]}

        conn.send_request = mock.AsyncMock(side_effect=fake_send_request)
        task = await conn.resolve_subagent_parent_task(child_key)
        self.assertIsNotNone(task)
        self.assertEqual(task["id"], "task-spawn")
        self.assertEqual(task["sessionKey"], parent_key)

    async def test_returns_none_when_only_self_referential_record_exists(self):
        """If the *only* childSessionKey match is self-referential (no OWUI
        parent link recorded at all -- e.g. a nested sub-agent), give up
        rather than returning a record that can't resolve to a chat."""
        conn = self._conn()
        child_key = "agent:main:subagent:99999999-9999-9999-9999-999999999999"

        async def fake_send_request(method, params, timeout=None):
            return {"tasks": [
                {"id": "task-cli", "childSessionKey": child_key,
                 "sessionKey": child_key, "runtime": "cli"},
            ]}

        conn.send_request = mock.AsyncMock(side_effect=fake_send_request)
        self.assertIsNone(await conn.resolve_subagent_parent_task(child_key))


class SubagentProactiveDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """Sub-agent finish -> proactive delivery into the *parent's* OWUI chat:
    content comes from the child's own transcript, the message is tagged
    distinctly, and carries a hidden taskId marker the Action endpoint can
    parse back out to open the drawer directly (see action.py)."""

    def _conn(self, agent_id="main"):
        return _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID=agent_id))

    def _fake_chats(self, chat_state):
        class FakeChat:
            def __init__(self, chat):
                self.chat = chat

        class FakeChats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                return FakeChat(chat_state)

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                history = chat_state["history"]
                messages = history.setdefault("messages", {})
                messages[message_id] = {**messages.get(message_id, {}), **message}
                history["currentId"] = message_id
                return FakeChat(chat_state)

        fake_chats_module = types.ModuleType("open_webui.models.chats")
        fake_chats_module.Chats = FakeChats
        fake_models_module = types.ModuleType("open_webui.models")
        fake_owui_module = types.ModuleType("open_webui")
        return mock.patch.dict(sys.modules, {
            "open_webui": fake_owui_module,
            "open_webui.models": fake_models_module,
            "open_webui.models.chats": fake_chats_module,
        })

    async def test_delivers_child_text_into_parent_chat_with_marker_and_label(self):
        conn = self._conn()
        child_key = "agent:main:subagent:11111111-1111-1111-1111-111111111111"
        parent_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn.session_preview = mock.AsyncMock(
            return_value={"previews": [{"key": child_key, "items": [
                {"role": "assistant", "text": "done: found 3 bugs"},
            ]}]}
        )
        old_leaf_id = "old-leaf"
        chat_state = {
            "models": ["openclaw_gateway.default"],
            "history": {
                "currentId": old_leaf_id,
                "messages": {old_leaf_id: {
                    "role": "assistant", "childrenIds": [],
                    "model": "openclaw_gateway.default", "modelName": "OpenClaw · Default",
                }},
            },
        }

        with self._fake_chats(chat_state):
            await _deliver_subagent_proactive_owui_message(
                conn, child_key, "run-1", parent_key, "task-42", "Fix the bug",
            )

        history = chat_state["history"]
        new_msg = history["messages"][history["currentId"]]
        self.assertIn("Sub-agent finished: Fix the bug", new_msg["content"])
        self.assertIn("done: found 3 bugs", new_msg["content"])
        self.assertIn("<!-- openclaw:taskId=task-42 -->", new_msg["content"])
        self.assertEqual(new_msg["model"], "openclaw_gateway.default")

    async def test_dedups_same_child_session_and_run(self):
        conn = self._conn()
        child_key = "agent:main:subagent:11111111-1111-1111-1111-111111111111"
        parent_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn._delivered_proactive[f"{child_key}:run-1"] = True
        conn.session_preview = mock.AsyncMock(side_effect=AssertionError("should not be called"))
        await _deliver_subagent_proactive_owui_message(
            conn, child_key, "run-1", parent_key, "task-1", None,
        )
        conn.session_preview.assert_not_called()

    async def test_skips_when_parent_is_not_an_owui_session(self):
        conn = self._conn()
        child_key = "agent:main:subagent:11111111-1111-1111-1111-111111111111"
        conn.session_preview = mock.AsyncMock(side_effect=AssertionError("should not be called"))
        await _deliver_subagent_proactive_owui_message(
            conn, child_key, "run-1", "agent:main:some-nested-subagent-parent", "task-1", None,
        )
        conn.session_preview.assert_not_called()

    async def test_falls_back_to_generic_label_without_title(self):
        conn = self._conn()
        child_key = "agent:main:subagent:11111111-1111-1111-1111-111111111111"
        parent_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn.session_preview = mock.AsyncMock(
            return_value={"previews": [{"key": child_key, "items": [
                {"role": "assistant", "text": "done"},
            ]}]}
        )
        chat_state = {"models": ["openclaw_gateway.default"], "history": {"currentId": None, "messages": {}}}

        with self._fake_chats(chat_state):
            await _deliver_subagent_proactive_owui_message(
                conn, child_key, "run-1", parent_key, "task-1", None,
            )

        new_msg = next(iter(chat_state["history"]["messages"].values()))
        self.assertIn("Sub-agent finished*", new_msg["content"])
        self.assertNotIn("Sub-agent finished:", new_msg["content"])


class MaybeDeliverSubagentProactiveTests(unittest.IsolatedAsyncioTestCase):
    """The debounce/lookup wrapper: must resolve the parent via
    `resolve_subagent_parent_task`, wait out the *parent's* idle-debounce
    (never the child's -- no tab ever talks to a sub-agent session directly,
    so checking the child's own idleness would always read True and skip
    the debounce entirely), and always clear the pending-set on exit."""

    def _conn(self, agent_id="main"):
        return _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID=agent_id))

    async def test_delivers_once_parent_settles_idle(self):
        conn = self._conn()
        child_key = "agent:main:subagent:11111111-1111-1111-1111-111111111111"
        parent_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn._pending_proactive_debounce.add(child_key)
        conn.resolve_subagent_parent_task = mock.AsyncMock(
            return_value={"id": "task-1", "sessionKey": parent_key, "title": "Do it"}
        )
        # Parent has a registered consumer initially (not idle), then clears.
        conn.register_consumer(parent_key, "parent-run")
        conn.unregister_consumer(parent_key, "parent-run")

        delivered = []

        async def fake_deliver(c, child, run_id, parent, task_id, title):
            delivered.append((child, run_id, parent, task_id, title))

        with mock.patch("openclaw_pipe.PROACTIVE_DELIVERY_ENABLED", True), mock.patch(
            "openclaw_pipe._deliver_subagent_proactive_owui_message", side_effect=fake_deliver
        ):
            await _maybe_deliver_subagent_proactive(
                conn, child_key, "run-1",
                min_idle_s=0.05, poll_interval_s=0.02, max_wait_s=2,
            )

        self.assertEqual(delivered, [(child_key, "run-1", parent_key, "task-1", "Do it")])
        self.assertNotIn(child_key, conn._pending_proactive_debounce)

    async def test_gives_up_quietly_when_no_task_found(self):
        conn = self._conn()
        child_key = "agent:main:subagent:11111111-1111-1111-1111-111111111111"
        conn._pending_proactive_debounce.add(child_key)
        conn.resolve_subagent_parent_task = mock.AsyncMock(return_value=None)

        with mock.patch(
            "openclaw_pipe._deliver_subagent_proactive_owui_message",
            side_effect=AssertionError("must not deliver without a resolved task"),
        ):
            await _maybe_deliver_subagent_proactive(conn, child_key, "run-1")

        self.assertNotIn(child_key, conn._pending_proactive_debounce)

    async def test_gives_up_quietly_when_parent_is_not_an_owui_session(self):
        conn = self._conn()
        child_key = "agent:main:subagent:11111111-1111-1111-1111-111111111111"
        conn._pending_proactive_debounce.add(child_key)
        # Nested sub-agent: the "parent" is itself another sub-agent session.
        conn.resolve_subagent_parent_task = mock.AsyncMock(
            return_value={"id": "task-1", "sessionKey": "agent:main:subagent:nested-parent"}
        )

        with mock.patch(
            "openclaw_pipe._deliver_subagent_proactive_owui_message",
            side_effect=AssertionError("must not deliver to a non-OWUI parent"),
        ):
            await _maybe_deliver_subagent_proactive(conn, child_key, "run-1")

        self.assertNotIn(child_key, conn._pending_proactive_debounce)

    async def test_never_delivers_if_parent_stays_busy_until_timeout(self):
        conn = self._conn()
        child_key = "agent:main:subagent:11111111-1111-1111-1111-111111111111"
        parent_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn._pending_proactive_debounce.add(child_key)
        conn.resolve_subagent_parent_task = mock.AsyncMock(
            return_value={"id": "task-1", "sessionKey": parent_key}
        )
        conn.register_consumer(parent_key, "parent-run")  # never idle

        with mock.patch("openclaw_pipe.PROACTIVE_DELIVERY_ENABLED", True), mock.patch(
            "openclaw_pipe._deliver_subagent_proactive_owui_message",
            side_effect=AssertionError("must not deliver into a busy parent"),
        ):
            await _maybe_deliver_subagent_proactive(
                conn, child_key, "run-1",
                min_idle_s=120, poll_interval_s=0.02, max_wait_s=0.1,
            )

        self.assertNotIn(child_key, conn._pending_proactive_debounce)

    async def test_bails_immediately_if_kill_switch_flipped_off_mid_wait(self):
        conn = self._conn()
        child_key = "agent:main:subagent:11111111-1111-1111-1111-111111111111"
        parent_key = _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        conn._pending_proactive_debounce.add(child_key)
        conn.resolve_subagent_parent_task = mock.AsyncMock(
            return_value={"id": "task-1", "sessionKey": parent_key}
        )
        conn.register_consumer(parent_key, "parent-run")  # never idle on its own

        with mock.patch("openclaw_pipe.PROACTIVE_DELIVERY_ENABLED", False), mock.patch(
            "openclaw_pipe._deliver_subagent_proactive_owui_message",
            side_effect=AssertionError("must not deliver once disabled"),
        ):
            await _maybe_deliver_subagent_proactive(
                conn, child_key, "run-1",
                min_idle_s=120, poll_interval_s=0.02, max_wait_s=5,
            )

        self.assertNotIn(child_key, conn._pending_proactive_debounce)


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
        self.assertEqual(item["call_id"], "call-1")
        self.assertEqual(item["name"], "Bash")
        self.assertEqual(item["arguments"], '{"command": "sleep 8"}')
        # in_progress + no matching output item is what renders as a spinner.
        self.assertEqual(item["status"], "in_progress")
        self.assertNotIn("output", item)

    def test_result_event_matches_the_start_event_by_call_id(self):
        """The shared call_id is the entire flip mechanism -- if these diverge
        the card spins forever."""
        start = _tool_call_started_event("Bash", "call-1", "{}")
        result = _tool_call_result_event("call-1", "total 4\ndrwxr-xr-x")
        self.assertEqual(start["item"]["call_id"], result["item"]["call_id"])
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

    def test_the_old_shape_is_confirmed_dead_code(self):
        """Documents WHY the fix was needed: replaying the old
        `response.output_item.done` shape through the real handler is a
        no-op. If this ever starts passing, OWUI has fixed the dead-code
        branch upstream and the relabel could move back to it."""
        from test_fixture_owui_streaming_handler import (
            handle_responses_streaming_event,
        )

        started = _tool_call_started_event("Read", "call-1", "{}")
        old_shape_relabel = {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_call-1",
                "call_id": "call-1",
                "name": "Read ❌",
                "arguments": "{}",
                "status": "failed",
            },
        }

        output = [started["item"]]
        output, metadata = handle_responses_streaming_event(old_shape_relabel, output)

        self.assertIsNone(metadata)
        self.assertEqual(output[0]["name"], "Read")


class ParityFinalizeTests(unittest.TestCase):
    """Parity finalize: when the inline turn ends before the run does, the
    ORIGINAL assistant message is completed in place with the full content --
    never a user message, and never when a live tab already has it."""

    def _conn(self, agent_id="main"):
        return _GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID=agent_id))

    def _session(self):
        return _owui_session_key(
            "main", "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )

    def _run_with_fake_chats(self, conn, session_key, run_id, chat_state):
        class FakeChat:
            def __init__(self, chat):
                self.chat = chat

        class FakeChats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                return FakeChat(chat_state)

            @staticmethod
            async def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, message):
                history = chat_state["history"]
                messages = history.setdefault("messages", {})
                messages[message_id] = {**messages.get(message_id, {}), **message}
                history["currentId"] = message_id
                return FakeChat(chat_state)

        fake_chats_module = types.ModuleType("open_webui.models.chats")
        fake_chats_module.Chats = FakeChats
        with mock.patch.dict(sys.modules, {
            "open_webui": types.ModuleType("open_webui"),
            "open_webui.models": types.ModuleType("open_webui.models"),
            "open_webui.models.chats": fake_chats_module,
        }):
            asyncio.run(_finalize_inline_message(conn, session_key, run_id))

    def test_completes_assistant_message_in_place(self):
        conn = self._conn()
        sk = self._session()
        conn.register_run_target(sk, "run-1", "chat-1", "asst-1")
        r = conn._run_renderers[f"{sk}:run-1"]
        r.feed({"stream": "assistant", "data": {"delta": "Full answer text."}})
        r.feed({"stream": "tool", "data": {"phase": "result", "name": "Bash",
                                           "toolCallId": "t1", "result": "ok"}})

        chat_state = {"history": {"currentId": "asst-1", "messages": {
            "user-1": {"role": "user", "content": "hi", "childrenIds": ["asst-1"]},
            "asst-1": {"role": "assistant", "content": "Full ans", "done": True,
                       "parentId": "user-1", "childrenIds": []},
        }}}
        self._run_with_fake_chats(conn, sk, "run-1", chat_state)

        msg = chat_state["history"]["messages"]["asst-1"]
        self.assertIn("Full answer text.", msg["content"])
        self.assertIn('<details type="tool_calls"', msg["content"])
        self.assertTrue(msg["done"])
        # target cleaned up
        self.assertNotIn(f"{sk}:run-1", conn._run_targets)

    def test_never_overwrites_a_user_message(self):
        """Critical safety: if the target id resolves to a user message (should
        never happen, but must never corrupt one), finalize appends a new tail
        instead of overwriting it in place."""
        conn = self._conn()
        sk = self._session()
        conn.register_run_target(sk, "run-2", "chat-1", "user-1")  # wrong id -> user
        conn._run_renderers[f"{sk}:run-2"].feed(
            {"stream": "assistant", "data": {"delta": "assistant tail content"}})

        chat_state = {"history": {"currentId": "user-1", "messages": {
            "user-1": {"role": "user", "content": "original user text",
                       "childrenIds": []},
        }}}
        self._run_with_fake_chats(conn, sk, "run-2", chat_state)

        # the user message must be untouched
        self.assertEqual(
            chat_state["history"]["messages"]["user-1"]["content"], "original user text")
        # and the content landed as a NEW assistant message, not lost
        assts = [m for m in chat_state["history"]["messages"].values()
                 if m.get("role") == "assistant"]
        self.assertTrue(any("assistant tail content" in (m.get("content") or "") for m in assts))

    def test_noop_when_delivered_live(self):
        conn = self._conn()
        sk = self._session()
        conn.register_run_target(sk, "run-3", "chat-1", "asst-3")
        conn._run_renderers[f"{sk}:run-3"].feed(
            {"stream": "assistant", "data": {"delta": "tail"}})
        conn.mark_delivered_live(sk, "run-3")

        chat_state = {"history": {"currentId": "asst-3", "messages": {
            "asst-3": {"role": "assistant", "content": "partial", "done": True,
                       "childrenIds": []},
        }}}
        self._run_with_fake_chats(conn, sk, "run-3", chat_state)

        # untouched -- a live tab already persisted it
        self.assertEqual(
            chat_state["history"]["messages"]["asst-3"]["content"], "partial")


class RelinearizeProactiveVariantsTests(unittest.TestCase):
    """A proactive message must sit in the normal linear flow, not behind a
    1/2·2/2 swipe arrow (2026-07-19)."""

    def _variant_history(self):
        # assistant X has TWO children: a proactive bubble AND the user's next
        # message -- exactly the observed variant group.
        return {
            "currentId": "user2",
            "messages": {
                "user1": {"role": "user", "content": "hi", "parentId": None,
                          "childrenIds": ["asstX"]},
                "asstX": {"role": "assistant", "content": "answer", "parentId": "user1",
                          "childrenIds": ["proac", "user2"], "timestamp": 100},
                "proac": {"role": "assistant", "content": "*↳ Proactive message*\n\nheads up",
                          "parentId": "asstX", "childrenIds": [], "timestamp": 110},
                "user2": {"role": "user", "content": "next", "parentId": "asstX",
                          "childrenIds": ["asstY"], "timestamp": 120},
                "asstY": {"role": "assistant", "content": "reply2", "parentId": "user2",
                          "childrenIds": []},
            },
        }

    def test_linearizes_proactive_then_user(self):
        h = self._variant_history()
        self.assertTrue(_relinearize_proactive_variants(h))
        m = h["messages"]
        # asstX now has exactly one child: the proactive message
        self.assertEqual(m["asstX"]["childrenIds"], ["proac"])
        # proactive chains to the user message
        self.assertEqual(m["proac"]["childrenIds"], ["user2"])
        self.assertEqual(m["user2"]["parentId"], "proac")
        # user message keeps its own reply
        self.assertEqual(m["user2"]["childrenIds"], ["asstY"])
        # no node has >1 child anymore (no variant)
        self.assertTrue(all(len(msg.get("childrenIds") or []) <= 1 for msg in m.values()))
        # view points at the true tail
        self.assertEqual(h["currentId"], "asstY")

    def test_idempotent(self):
        h = self._variant_history()
        self.assertTrue(_relinearize_proactive_variants(h))
        self.assertFalse(_relinearize_proactive_variants(h))  # already linear

    def test_leaves_genuine_regeneration_variants_untouched(self):
        # two non-proactive assistant children = a real regeneration variant
        h = {"currentId": "b", "messages": {
            "u": {"role": "user", "childrenIds": ["a", "b"]},
            "a": {"role": "assistant", "content": "v1", "childrenIds": []},
            "b": {"role": "assistant", "content": "v2", "childrenIds": []},
        }}
        self.assertFalse(_relinearize_proactive_variants(h))
        self.assertEqual(sorted(h["messages"]["u"]["childrenIds"]), ["a", "b"])

    def test_chains_continuation_below_proactive_that_already_has_a_subtree(self):
        """Real 2026-07-19 case: the proactive sibling isn't a leaf -- a later
        message already chained under it. The user continuation must attach at
        the proactive's deepest leaf, not get skipped."""
        h = {"currentId": "u2", "messages": {
            "x": {"role": "assistant", "childrenIds": ["proac", "u2"]},
            "proac": {"role": "assistant", "content": "*↳ Proactive message*\n\nyo",
                      "childrenIds": ["c1"], "timestamp": 10},
            "c1": {"role": "user", "content": "under proactive", "childrenIds": []},
            "u2": {"role": "user", "content": "sibling", "childrenIds": ["a2"]},
            "a2": {"role": "assistant", "content": "reply", "childrenIds": []},
        }}
        self.assertTrue(_relinearize_proactive_variants(h))
        m = h["messages"]
        self.assertEqual(m["x"]["childrenIds"], ["proac"])
        self.assertEqual(m["proac"]["childrenIds"], ["c1"])
        self.assertEqual(m["c1"]["childrenIds"], ["u2"])   # continuation below proactive's leaf
        self.assertEqual(m["u2"]["parentId"], "c1")
        self.assertEqual(m["u2"]["childrenIds"], ["a2"])   # keeps its own reply
        self.assertTrue(all(len(v.get("childrenIds") or []) <= 1 for v in m.values()))
        self.assertEqual(h["currentId"], "a2")

    def test_orders_multiple_proactive_by_timestamp(self):
        h = {"currentId": "u2", "messages": {
            "x": {"role": "assistant", "childrenIds": ["p2", "p1", "u2"]},
            "p1": {"role": "assistant", "content": "*↳ Proactive message*\n\na",
                   "childrenIds": [], "timestamp": 10},
            "p2": {"role": "assistant", "content": "*↳ Proactive message*\n\nb",
                   "childrenIds": [], "timestamp": 20},
            "u2": {"role": "user", "content": "hey", "childrenIds": []},
        }}
        self.assertTrue(_relinearize_proactive_variants(h))
        m = h["messages"]
        self.assertEqual(m["x"]["childrenIds"], ["p1"])   # oldest proactive first
        self.assertEqual(m["p1"]["childrenIds"], ["p2"])
        self.assertEqual(m["p2"]["childrenIds"], ["u2"])


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


class DynamicModelSelectorTests(unittest.TestCase):
    """Tests for ELI-11: dynamic model discovery, whitelist, and legacy compat."""

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

    def test_parse_whitelist_empty(self):
        self.assertEqual(_parse_whitelist(""), set())
        self.assertEqual(_parse_whitelist("   "), set())

    def test_parse_whitelist_commas(self):
        self.assertEqual(_parse_whitelist(" a , b, c "), {"a", "b", "c"})

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

    def test_pipe_selected_preset_legacy_chatgpt(self):
        pipe = Pipe()
        self.assertEqual(
            pipe._selected_preset({"model": "openclaw_gateway.chatgpt"}),
            "chatgpt",
        )

    def test_pipe_model_override_default_empty(self):
        pipe = Pipe()
        self.assertIsNone(pipe._model_override_for_preset("default"))

    def test_pipe_model_override_default_set(self):
        pipe = Pipe()
        pipe.valves.DEFAULT_MODEL = "openai/gpt-5.5"
        self.assertEqual(pipe._model_override_for_preset("default"), "openai/gpt-5.5")

    def test_pipe_model_override_legacy_custom(self):
        pipe = Pipe()
        pipe.valves.CHATGPT_MODEL = "openai/gpt-5.5-pro"
        self.assertEqual(
            pipe._model_override_for_preset("chatgpt"),
            "openai/gpt-5.5-pro",
        )

    def test_pipe_model_override_legacy_default(self):
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

    def test_two_connections_share_bookkeeping_objects(self):
        live, zombie = self._conn(), self._conn()
        self.assertIs(live._delivered_live, zombie._delivered_live)
        self.assertIs(live._delivered_proactive, zombie._delivered_proactive)
        self.assertIs(live._session_last_activity, zombie._session_last_activity)
        self.assertIs(
            live._pending_proactive_debounce, zombie._pending_proactive_debounce
        )

    def test_zombie_sees_live_delivery_and_wont_redeliver(self):
        live, zombie = self._conn(), self._conn()
        # Live connection dispatches the final event to its open tab.
        live.mark_delivered_live("session-x", "run-1")
        # The zombie, receiving the same broadcast, must recognise it was
        # already shown live and skip proactive delivery.
        self.assertTrue(zombie.was_delivered_live("session-x", "run-1"))

    def test_zombie_sees_live_consumer_activity_as_non_idle(self):
        live, zombie = self._conn(), self._conn()
        q = live.register_consumer("session-x", "run-1")
        # Live connection is actively servicing the session, so from the
        # zombie's perspective it is NOT idle and proactive must not fire.
        self.assertFalse(zombie.session_idle_for("session-x", min_idle_s=120))
        live.unregister_consumer("session-x", "run-1", q)
        # Immediately after unregister the shared activity timestamp is fresh,
        # so the session is still within its quiet-period guard.
        self.assertFalse(zombie.session_idle_for("session-x", min_idle_s=120))

    def test_isolated_when_not_inside_owui(self):
        # Without OWUI's module present, each connection keeps private state so
        # unrelated processes/tests can't leak into each other.
        for k in ("open_webui.socket.main", "open_webui.socket", "open_webui"):
            sys.modules.pop(k, None)
        self.assertIsNone(_shared_gateway_state())
        a, b = self._conn(), self._conn()
        self.assertIsNot(a._delivered_live, b._delivered_live)


if __name__ == "__main__":
    unittest.main()


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
        p.valves.CONFIGURED_MODELS = ""
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
            p.valves.CONFIGURED_MODELS = ""
            _write_json_file(
                os.path.join(_state_dir(custom), "models-cache.json"),
                {"models": [{"key": "vendor/real", "name": "Real", "tags": []}]},
            )
            asyncio.run(p.pipes())
            labels = [o["label"] for o in Pipe.get_model_options()]
            self.assertTrue(any("Real" in l for l in labels), labels)
            for l in labels:
                self.assertNotIn(UNVERIFIED_MODEL_SUFFIX, l)

    def test_warning_survives_a_whitelist_that_filters_everything(self):
        """CONFIGURED_MODELS set to real Gateway keys + an unreachable Gateway
        emptied the example list, taking the warning with it -- leaving exactly
        the user who most needs it with no explanation."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Pipe()
            p.valves.STATE_DIR = tmp
            p.valves.CONFIGURED_MODELS = "vendor/only-mine"
            entries = asyncio.run(p.pipes())
            self.assertEqual(len(entries), 1)
            self.assertIn(UNVERIFIED_MODEL_SUFFIX, entries[0]["name"])
            self.assertEqual(entries[0]["id"], "default")


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
    text only surfaces via `recover_from_preview()` at `_pipe_impl`'s final
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
        # resolved_thinking=None regardless of what recover_from_preview()
        # returns -- the natural termination this fix relies on instead of
        # an explicit recursion-guard flag.
        first_body = {"reasoning_effort": "high"}
        retried = _thinking_rejection_retry_body(
            first_body, self.REJECTION_TEXT, "high")
        self.assertIsNotNone(retried)
        second = _thinking_rejection_retry_body(
            retried, self.REJECTION_TEXT, None)
        self.assertIsNone(second)
