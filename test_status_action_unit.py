#!/usr/bin/env python3
"""Focused unit tests for the companion Action Function (openclaw_status_action.py).

Mirrors test_pipe_unit.py's dependency-stubbing pattern so this runs without
a real OWUI/pydantic/websockets/cryptography environment.
"""

import asyncio
import json
import sys
import types
import unittest
from unittest import mock

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

from openclaw_status_action import (  # noqa: E402
    Action,
    _get_action_connection,
    _owui_session_key,
    _render_modal_fill_js,
)


class RenderModalJsTests(unittest.TestCase):
    """The JSON-blob substitution is the one place untrusted-ish gateway
    text (e.g. a goal description) flows into JS source -- these guard the
    safety property described in action.py's module comment."""

    def test_placeholder_fully_replaced(self):
        js = _render_modal_fill_js({"provider": "x", "model": "", "context": None, "windows": [], "goal": None, "source": "pipe", "fetchedAt": "12:00:00"})
        self.assertNotIn("__OPENCLAW_STATUS_DATA__", js)

    def test_adversarial_strings_stay_inside_the_json_blob(self):
        """Quotes, backslashes, and a fake </script> tag must not be able to
        terminate the JS string/close the <script> the execute event runs
        inside -- json.dumps is what guarantees this, this test guards
        against someone "simplifying" _render_modal_fill_js into raw %-formatting
        or str concatenation later."""
        nasty = 'a"b\\c</script><img src=x onerror=alert(1)>\nline2'
        js = _render_modal_fill_js({
            "provider": nasty, "model": "", "context": None,
            "windows": [{"label": nasty, "usedPercent": 50, "resetIn": None}],
            "goal": None, "source": "pipe", "fetchedAt": "12:00:00",
        })
        # The DATA assignment line must be valid JSON on its own.
        line = next(l for l in js.splitlines() if l.strip().startswith("const DATA ="))
        raw_json = line.strip()[len("const DATA = "):].rstrip(";")
        parsed = json.loads(raw_json)
        self.assertEqual(parsed["provider"], nasty)
        self.assertEqual(parsed["windows"][0]["label"], nasty)
        # And the literal raw '</script>' must never appear unescaped.
        self.assertNotIn("</script>", js.replace("<\\/script>", ""))

    def test_output_is_self_contained_iife(self):
        js = _render_modal_fill_js({"provider": "x", "model": "", "context": None, "windows": [], "goal": None, "source": "pipe", "fetchedAt": "12:00:00"})
        self.assertTrue(js.strip().startswith("(function()"))
        self.assertTrue(js.strip().endswith("})();"))

    def test_bar_and_error_colors_are_inline_not_tailwind_classes(self):
        """Regression guard: 'bg-rose-500' (and friends) rendered as an
        invisible bar in production because Tailwind only ships utility
        classes it finds referenced in ITS OWN build's source -- OWUI's
        frontend never uses "rose" anywhere, so that class had zero CSS
        behind it despite looking like a normal Tailwind color utility.
        Every dynamic traffic-light color must be an inline style, which
        has no dependency on what OWUI's own frontend does or doesn't use,
        so this can't silently regress again."""
        js = _render_modal_fill_js({"provider": "x", "model": "", "context": None, "windows": [], "goal": None, "source": "pipe", "fetchedAt": "12:00:00"})
        for leftover in ("bg-rose", "bg-amber", "bg-emerald", "text-rose"):
            # Only the explanatory comment may mention these strings; the
            # executable JS itself must not.
            executable = "\n".join(l for l in js.splitlines() if not l.strip().startswith("//"))
            self.assertNotIn(leftover, executable)
        self.assertIn("style.backgroundColor = barColor", js)
        self.assertIn("#f43f5e", js)  # rose-500 equivalent, as a literal hex now
        self.assertIn("#f59e0b", js)  # amber-500 equivalent
        self.assertIn("#10b981", js)  # emerald-500 equivalent

    def test_compact_button_only_wired_when_identity_fields_present(self):
        """The Compact button's click handler needs DATA.chatId/messageId/
        sessionId to build a working callback request -- the render must
        gate the button on all three being present rather than rendering
        a button that silently no-ops (or crashes) on click for a status
        response that came from somewhere those weren't threaded through."""
        with_ids = _render_modal_fill_js({
            "provider": "x", "model": "", "context": {"usedTokens": "1k", "totalTokens": "10k", "pct": 10.0},
            "windows": [], "goal": None, "source": "pipe", "fetchedAt": "12:00:00",
            "chatId": "c1", "messageId": "m1", "sessionId": "s1", "owuiModel": "x.y",
        })
        self.assertIn("compactBtn", with_ids)
        self.assertIn("triggerCompact", with_ids)

        without_ids = _render_modal_fill_js({
            "provider": "x", "model": "", "context": {"usedTokens": "1k", "totalTokens": "10k", "pct": 10.0},
            "windows": [], "goal": None, "source": "pipe", "fetchedAt": "12:00:00",
            "chatId": None, "messageId": None, "sessionId": None, "owuiModel": None,
        })
        # triggerCompact is always defined (harmless), but the button
        # creation must be gated -- check the guard condition is present.
        self.assertIn("DATA.chatId && DATA.messageId && DATA.sessionId", without_ids)

    def test_compact_fetch_targets_this_same_action_with_mode_marker(self):
        js = _render_modal_fill_js({
            "provider": "x", "model": "", "context": None, "windows": [], "goal": None,
            "source": "pipe", "fetchedAt": "12:00:00",
        })
        self.assertIn("/api/chat/actions/openclaw_status_action", js)
        self.assertIn("mode: 'compact'", js)
        self.assertIn("localStorage.getItem('token')", js)


class GetActionConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_pipe_connection_when_stashed(self):
        fake_conn = mock.AsyncMock()
        fake_module = types.SimpleNamespace(_openclaw_gateway_connection_v1=fake_conn)
        # `import open_webui.socket.main` requires the parent packages to
        # also resolve via sys.modules -- patching only the leaf module
        # isn't enough, the import statement fails on the (unstubbed)
        # `open_webui` top-level package first and silently falls through
        # to the fallback path, which is not what this test means to check.
        stub_modules = {
            "open_webui": types.ModuleType("open_webui"),
            "open_webui.socket": types.ModuleType("open_webui.socket"),
            "open_webui.socket.main": fake_module,
        }
        with mock.patch.dict(sys.modules, stub_modules):
            conn, source = await _get_action_connection(lambda: None)
        self.assertIs(conn, fake_conn)
        self.assertEqual(source, "pipe")
        fake_conn.ensure_connected.assert_awaited_once()

    async def test_falls_back_to_own_connection_when_nothing_stashed(self):
        import openclaw_status_action as mod

        mod._action_fallback_connection = None
        fake_conn = mock.AsyncMock()
        with mock.patch("openclaw_status_action._GatewayConnection", return_value=fake_conn):
            conn, source = await _get_action_connection(lambda: None)
        self.assertIs(conn, fake_conn)
        self.assertEqual(source, "action-own")
        fake_conn.ensure_connected.assert_awaited_once()
        mod._action_fallback_connection = None  # reset for other tests


class SessionKeyTests(unittest.TestCase):
    def test_matches_pipe_format(self):
        """Must stay byte-identical to gateway.py's _owui_session_key (same
        fragment, shared by both artifacts) so a session the Pipe created is
        the same session this Action looks up."""
        self.assertEqual(
            _owui_session_key("main", "user-1", "chat-1"),
            "agent:main:openwebui-user-1-chat-1",
        )


class ActionEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_chat_id_notifies_and_returns_error(self):
        action = Action()
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        result = await action.action({}, __user__={"id": "u1"}, __event_emitter__=emitter)
        self.assertEqual(result["status"], "error")
        self.assertEqual(emitted[0]["type"], "notification")
        self.assertEqual(emitted[0]["data"]["type"], "error")

    async def test_success_path_opens_immediately_then_fills(self):
        """Two execute emits: the loading-skeleton open happens before any
        network call, the data fill happens after -- this is the whole
        point of the open/fill split (instant feedback on click, no
        waiting for the gateway round trip before anything visible
        happens). Also exercises all three sections (context, rate limits,
        goal) end to end."""
        action = Action()
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        async def fake_send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {
                    "session": {
                        "modelProvider": "anthropic", "model": "claude-sonnet-5",
                        "contextTokens": 1000000, "totalTokens": 200000,
                        "goal": {"status": "active", "tokensUsed": 5000, "tokenBudget": 20000},
                    }
                }
            if method == "usage.status":
                return {"providers": [{"provider": "anthropic", "windows": [
                    {"label": "5h", "usedPercent": 72, "resetAt": 1783790000000},
                ]}]}
            raise AssertionError(f"unexpected RPC: {method}")

        fake_conn = mock.AsyncMock()
        fake_conn.send_request = mock.AsyncMock(side_effect=fake_send_request)

        async def fake_get_connection(_valves_getter):
            # By the time the connection is fetched, the loading modal must
            # already have been emitted -- this is what actually proves the
            # open happens before the network call, not just before the
            # function returns.
            self.assertEqual(len(emitted), 1)
            self.assertEqual(emitted[0]["type"], "execute")
            self.assertIn("openclaw-status-modal-body", emitted[0]["data"]["code"])
            self.assertIn("animate-pulse", emitted[0]["data"]["code"])
            return fake_conn, "pipe"

        with mock.patch("openclaw_status_action._get_action_connection", new=fake_get_connection):
            result = await action.action(
                {"chat_id": "chat-1"}, __user__={"id": "user-1"}, __event_emitter__=emitter,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["context"]["pct"], 20.0)
        self.assertEqual(len(result["windows"]), 1)
        self.assertEqual(len(emitted), 2)
        fill_code = emitted[1]["data"]["code"]
        payload = json.loads(fill_code.split("const DATA = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual(payload["provider"], "anthropic")
        self.assertEqual(payload["context"]["pct"], 20.0)
        self.assertEqual(payload["windows"][0]["label"], "5h")
        self.assertEqual(payload["windows"][0]["resetAtMs"], 1783790000000)
        self.assertIn("Pursuing goal", payload["goal"]["line"])
        self.assertEqual(payload["goal"]["pct"], 25.0)

    async def test_reset_time_shows_both_relative_and_absolute(self):
        """The reset line must carry both the server-computed relative
        countdown and the raw timestamp for the browser to format as a
        local-timezone absolute clock time -- computed client-side, not
        server-side, since the gateway container and whoever is looking at
        the dialog aren't guaranteed to share a timezone."""
        js = _render_modal_fill_js({
            "provider": "x", "model": "", "context": None,
            "windows": [{"label": "5h", "usedPercent": 50, "resetIn": "1h21m", "resetAtMs": 1783790000000}],
            "goal": None, "source": "pipe", "fetchedAt": "12:00:00",
        })
        self.assertIn("resetAtMs", js)
        self.assertIn("toLocaleTimeString", js)
        self.assertIn("'resets in ' + w.resetIn", js)

    async def test_fetch_error_fills_modal_with_error_not_a_bare_notification(self):
        """A failure after the loading modal is already open must update
        that same modal to an error state, not leave it spinning forever
        while a separate toast fires instead."""
        action = Action()
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        with mock.patch("openclaw_status_action._get_action_connection",
                         new=mock.AsyncMock(side_effect=RuntimeError("gateway unreachable"))):
            result = await action.action(
                {"chat_id": "chat-1"}, __user__={"id": "user-1"}, __event_emitter__=emitter,
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(len(emitted), 2)
        self.assertEqual(emitted[0]["type"], "execute")  # the loading open
        self.assertEqual(emitted[1]["type"], "execute")  # the error fill
        self.assertIn("gateway unreachable", emitted[1]["data"]["code"])


class CompactTests(unittest.IsolatedAsyncioTestCase):
    """The dialog's Compact button fetches back to this same Action with a
    synthetic mode="compact" marker (see triggerCompact() in
    _MODAL_FILL_JS_TEMPLATE) -- these cover the Python-side dispatch and
    the poll-for-completion flow (chat.send + sessions.describe polling,
    not the Pipe's full event-consumer loop -- see action.py's
    _run_compact docstring for why)."""

    COMPACT_BODY = {"mode": "compact", "chat_id": "chat-1", "id": "msg-1", "session_id": "sess-1",
                     "model": "openclaw_gateway.anthropic/claude-sonnet-5"}

    async def test_mode_dispatch_routes_to_run_compact(self):
        action = Action()
        with mock.patch.object(action, "_run_compact", new=mock.AsyncMock(return_value={"status": "ok"})) as m:
            result = await action.action(self.COMPACT_BODY, __user__={"id": "u1"}, __event_emitter__=mock.AsyncMock())
        m.assert_awaited_once()
        self.assertEqual(result["status"], "ok")

    async def test_default_mode_does_not_route_to_compact(self):
        action = Action()
        with mock.patch.object(action, "_run_compact", new=mock.AsyncMock()) as m, \
             mock.patch.object(action, "_run_status", new=mock.AsyncMock(return_value={"status": "ok"})):
            await action.action({"chat_id": "chat-1"}, __user__={"id": "u1"}, __event_emitter__=mock.AsyncMock())
        m.assert_not_awaited()

    async def test_blocked_when_a_run_is_already_active(self):
        action = Action()
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        fake_conn = mock.Mock()
        fake_conn.active_run_id_for_session = mock.Mock(return_value="some-active-run")
        fake_conn.send_request = mock.AsyncMock()  # must never be called

        with mock.patch("openclaw_status_action._get_action_connection",
                         new=mock.AsyncMock(return_value=(fake_conn, "pipe"))):
            result = await action._run_compact(self.COMPACT_BODY, {"id": "u1"}, emitter)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["detail"], "session busy")
        fake_conn.send_request.assert_not_awaited()
        self.assertEqual(len(emitted), 1)
        self.assertIn("currently in progress", emitted[0]["data"]["code"])

    async def test_success_shows_compacting_then_refreshes_status(self):
        action = Action()
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        describe_calls = {"n": 0}

        async def fake_send_request(method, params, timeout=5):
            if method == "chat.send":
                self.assertEqual(params["message"], "/compact")
                self.assertEqual(params["deliver"], False)
                return {"runId": "compact-run-1"}
            if method == "sessions.describe":
                describe_calls["n"] += 1
                # Still running on the first poll, done on the second --
                # exercises the loop actually looping, not just the
                # single-iteration happy path. Any call after that (the
                # _run_status refresh triggers its own sessions.describe
                # too) also reports done/finished data.
                status = "running" if describe_calls["n"] == 1 else "done"
                return {"session": {"status": status, "modelProvider": "anthropic",
                                     "model": "claude-sonnet-5"}}
            if method == "usage.status":
                # Hit during the post-compact _run_status refresh.
                return {"providers": []}
            raise AssertionError(f"unexpected RPC: {method}")

        fake_conn = mock.Mock()
        fake_conn.active_run_id_for_session = mock.Mock(return_value=None)
        fake_conn.send_request = mock.AsyncMock(side_effect=fake_send_request)

        with mock.patch("openclaw_status_action._get_action_connection",
                         new=mock.AsyncMock(return_value=(fake_conn, "pipe"))), \
             mock.patch("openclaw_status_action.asyncio.sleep", new=mock.AsyncMock()):
            result = await action._run_compact(self.COMPACT_BODY, {"id": "u1"}, emitter)

        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(describe_calls["n"], 2)
        self.assertEqual(emitted[0]["type"], "execute")
        self.assertIn("Compacting", emitted[0]["data"]["code"])
        # Final emit is the normal status fill (refresh), not another
        # "Compacting..." state or a bare notification.
        self.assertIn("anthropic", emitted[-1]["data"]["code"])

    async def test_timeout_shows_clear_message_not_infinite_spin(self):
        action = Action()
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        async def fake_send_request(method, params, timeout=5):
            if method == "chat.send":
                return {"runId": "compact-run-1"}
            if method == "sessions.describe":
                return {"session": {"status": "running"}}  # never finishes
            raise AssertionError(f"unexpected RPC: {method}")

        fake_conn = mock.Mock()
        fake_conn.active_run_id_for_session = mock.Mock(return_value=None)
        fake_conn.send_request = mock.AsyncMock(side_effect=fake_send_request)

        # Simulate 180s+ elapsing without a real wait: each asyncio.sleep
        # call jumps the fake clock forward past the deadline immediately.
        clock = {"t": 0.0}

        def fake_time():
            return clock["t"]

        async def fake_sleep(_seconds):
            clock["t"] += 200

        with mock.patch("openclaw_status_action._get_action_connection",
                         new=mock.AsyncMock(return_value=(fake_conn, "pipe"))), \
             mock.patch("openclaw_status_action.time.time", side_effect=fake_time), \
             mock.patch("openclaw_status_action.asyncio.sleep", new=fake_sleep):
            result = await action._run_compact(self.COMPACT_BODY, {"id": "u1"}, emitter)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["detail"], "timeout")
        self.assertIn("timed out", emitted[-1]["data"]["code"])


if __name__ == "__main__":
    unittest.main()
