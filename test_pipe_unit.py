#!/usr/bin/env python3
"""Focused unit tests for pipe stream recovery behavior."""

import unittest
import sys
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
    _FALLBACK_MODELS,
    _advance_input_prompt_buffer,
    _advance_media_buffer,
    _ask_user_detail_block,
    _ask_user_input_modal,
    _coerce_text,
    _could_be_user_input_prefix,
    _deliver_proactive_owui_message,
    _discover_models,
    _emit_message_snapshot,
    _emit_status,
    _friendly_name,
    _is_user_input_prompt,
    _item_assistant_text,
    _item_delta_text,
    _last_assistant_text_from_preview,
    _live_session_id_for_user,
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
    _resolve_media_via_owui,
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


if __name__ == "__main__":
    unittest.main()
