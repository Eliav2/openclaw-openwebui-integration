#!/usr/bin/env python3
"""Focused unit tests for pipe stream recovery behavior."""

import unittest
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
    _TurnRenderer,
    _render_tool_result_block,
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
        # A sentinel-only final leg means "nothing to show for THIS run" — it
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
        # and delivery no-ops after the preview fetch — that's fine, this test
        # only asserts the dedup guard, not the OWUI-internals write path.
        asyncio.run(_deliver_proactive_owui_message(conn, session_key, "run-1"))
        asyncio.run(_deliver_proactive_owui_message(conn, session_key, "run-1"))
        # session_preview only called once — the second call short-circuits on dedup.
        self.assertEqual(conn.session_preview.await_count, 1)

    def test_deliver_leaves_new_message_as_the_active_leaf(self):
        """Regression for the 2026-07-11 bug: the message was written to
        `history.messages` and pipe_log even reported success, but nothing
        ever showed up in OWUI. Root cause: OWUI's real
        `upsert_message_to_chat_by_id_and_message_id` sets
        `history['currentId'] = message_id` as a side effect of *every*
        call — including the second call this function used to make (to
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
        `ANNOUNCE_SKIP` must never reach OWUI's chat-write path at all —
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
        the session is still genuinely live — proactive delivery must not
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
        session — only the exact (session, run) pair that was actually
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
        consumer must NOT immediately count as idle — the next leg's HTTP
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
        register_consumer call must reset the clock — the session isn't
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
        # A consumer is (and remains) registered the whole time — session is
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
        not still be proactively delivered once the wait ends — the tab
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
                # self-referential CLI execution record — newest, returned first
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


class ParityFinalizeTests(unittest.TestCase):
    """Parity finalize: when the inline turn ends before the run does, the
    ORIGINAL assistant message is completed in place with the full content —
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

        # untouched — a live tab already persisted it
        self.assertEqual(
            chat_state["history"]["messages"]["asst-3"]["content"], "partial")


class RelinearizeProactiveVariantsTests(unittest.TestCase):
    """A proactive message must sit in the normal linear flow, not behind a
    1/2·2/2 swipe arrow (2026-07-19)."""

    def _variant_history(self):
        # assistant X has TWO children: a proactive bubble AND the user's next
        # message — exactly the observed variant group.
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
        """Real 2026-07-19 case: the proactive sibling isn't a leaf — a later
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
    *second* `_event_loop` task on success — but the original coroutine
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
    its own — self-healing without a container restart."""

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
    from what was actually recorded into `visible_message_text` — e.g.
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
        # Only a match at the very tail counts as "already shown" — a
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
        # an actual directive) got misparsed live 2026-07-10 — "fix" (no
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
        # few characters at a time — each partial prefix should still read
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
        # start of the next paragraph all at once — the newline doesn't
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
        # "monkey" contains "key" — must not be flagged as a secret.
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

        with self.assertRaises(TimeoutError):
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

    def test_friendly_name_alias(self):
        self.assertEqual(_friendly_name({"key": "a/b", "name": "x", "tags": ["alias:opus"]}), "Opus")
        self.assertEqual(_friendly_name({"key": "a/b", "name": "x", "tags": ["alias:sonnet-5"]}), "Sonnet-5")

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
        # Real gateway `models.list` entries use id/name/provider/alias —
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
        """Live gateway responses use id/name/provider/alias, not key/tags —
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
    turn into chat history as a spurious proactive message — creating a second
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
        must be backfilled into the *already-existing* dict via setdefault —
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
