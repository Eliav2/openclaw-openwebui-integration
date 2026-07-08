#!/usr/bin/env python3
"""Focused unit tests for pipe stream recovery behavior."""

import unittest
import sys
import types
import asyncio

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
    _FALLBACK_MODELS,
    _advance_input_prompt_buffer,
    _ask_user_input_modal,
    _coerce_text,
    _could_be_user_input_prefix,
    _discover_models,
    _emit_message_snapshot,
    _emit_status,
    _friendly_name,
    _is_user_input_prompt,
    _item_assistant_text,
    _item_delta_text,
    _modal_payload_from_user_input_prompt,
    _model_patch_matches,
    _normalize_event_call_response,
    _normalize_model_entry,
    _owui_chat_send_params,
    _owui_session_key,
    _parse_whitelist,
    _preview_recovery_text,
    _provider_from_key,
    _resolve_media,
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
        payload, is_confirmation = _modal_payload_from_user_input_prompt(
            "Codex needs input:\n\nPackage\nChoose a package style\n1. curl\n2. pipx"
        )

        self.assertEqual(payload["type"], "input")
        self.assertEqual(payload["data"]["title"], "Package")
        self.assertIn("Choose a package style", payload["data"]["message"])
        self.assertIn("1. curl", payload["data"]["message"])
        self.assertEqual(
            payload["data"]["placeholder"],
            "Reply with a number or your answer",
        )
        self.assertFalse(is_confirmation)

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
        self.assertEqual(payload["type"], "input")

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
            return {"value": "2"}

        answer = await _ask_user_input_modal(
            event_call,
            "Codex needs input:\n\nPackage\nChoose\n1. curl\n2. pipx",
        )

        self.assertEqual(answer, "2")
        self.assertEqual(calls[0]["type"], "input")

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


if __name__ == "__main__":
    unittest.main()
