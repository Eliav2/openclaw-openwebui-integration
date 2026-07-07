#!/usr/bin/env python3
"""Focused unit tests for pipe stream recovery behavior."""

import unittest
import sys
import types

if "websockets" not in sys.modules:
    websockets_stub = types.SimpleNamespace(
        WebSocketClientProtocol=object,
        exceptions=types.SimpleNamespace(ConnectionClosed=Exception),
    )
    sys.modules["websockets"] = websockets_stub

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
    _coerce_text,
    _emit_message_snapshot,
    _emit_status,
    _item_assistant_text,
    _item_delta_text,
    _model_patch_matches,
    _owui_chat_send_params,
    _owui_session_key,
    _preview_recovery_text,
    _resolve_media,
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


if __name__ == "__main__":
    unittest.main()
