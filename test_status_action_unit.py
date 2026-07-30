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
    _extract_transcript_and_tools,
    _get_action_connection,
    _owui_session_key,
    _render_context_fill_js,
    _render_drawer_open_js,
    _render_drawer_overview_fill_js,
    _render_drawer_tools_fill_js,
    _render_drawer_transcript_fill_js,
    _render_limits_fill_js,
    _render_modal_open_js,
    _render_sections_error_js,
    _render_subagents_fill_js,
)


def _emitted_codes(emitted, event_type="execute"):
    return [e["data"]["code"] for e in emitted if e["type"] == event_type]


class RenderJsTests(unittest.TestCase):
    """The JSON-blob substitution (_json_for_js, used by every _render_*_js
    function here) is the one place untrusted-ish gateway text (e.g. a goal
    description) flows into JS source -- these guard the safety property
    described in action.py's module comment, exercised through each of the
    four render entry points that use it."""

    def test_open_js_embeds_identity_and_no_placeholder_left(self):
        js = _render_modal_open_js({"chatId": "c1", "messageId": "m1", "sessionId": "s1", "owuiModel": "x.y"})
        self.assertNotIn("__OPENCLAW_IDENTITY__", js)
        self.assertIn('"chatId": "c1"', js)
        self.assertTrue(js.strip().startswith("(function()"))
        self.assertTrue(js.strip().endswith("})();"))

    def test_open_js_creates_three_independent_section_skeletons(self):
        """Each query gets its own loading state. Structural guard that the three sections exist as
        distinct, independently-targetable containers from the start."""
        js = _render_modal_open_js({"chatId": None, "messageId": None, "sessionId": None, "owuiModel": None})
        for section_id in (
            "openclaw-status-section-context",
            "openclaw-status-section-limits",
            "openclaw-status-section-subagents",
        ):
            self.assertIn(section_id, js)

    def test_adversarial_strings_stay_inside_the_json_blob(self):
        """Quotes, backslashes, and a fake </script> tag must not be able to
        terminate the JS string/close the <script> the execute event runs
        inside -- json.dumps is what guarantees this."""
        nasty = 'a"b\\c</script><img src=x onerror=alert(1)>\nline2'
        js = _render_context_fill_js({
            "provider": nasty, "model": "", "context": None, "goal": None,
            "source": "pipe", "fetchedAt": "12:00:00", "error": None,
        })
        line = next(l for l in js.splitlines() if l.strip().startswith("const DATA ="))
        raw_json = line.strip()[len("const DATA = "):].rstrip(";")
        parsed = json.loads(raw_json)
        self.assertEqual(parsed["provider"], nasty)
        self.assertNotIn("</script>", js.replace("<\\/script>", ""))

    def test_bar_and_error_colors_are_inline_not_tailwind_classes(self):
        """Regression guard: 'bg-rose-500' (and friends) rendered as an
        invisible bar in production because Tailwind only ships utility
        classes it finds referenced in ITS OWN build's source -- OWUI's
        frontend never uses "rose" anywhere, so that class had zero CSS
        behind it despite looking like a normal Tailwind color utility.
        Every dynamic traffic-light color must be an inline style."""
        js = _render_modal_open_js({"chatId": None, "messageId": None, "sessionId": None, "owuiModel": None})
        for leftover in ("bg-rose", "bg-amber", "bg-emerald", "text-rose"):
            executable = "\n".join(l for l in js.splitlines() if not l.strip().startswith("//"))
            self.assertNotIn(leftover, executable)
        self.assertIn("style.backgroundColor = barColor", js)
        self.assertIn("#f43f5e", js)  # rose-500 equivalent, as a literal hex now
        self.assertIn("#f59e0b", js)  # amber-500 equivalent
        self.assertIn("#10b981", js)  # emerald-500 equivalent

    def test_limits_shows_explicit_note_when_windows_empty(self):
        """Regression guard for a reported confusion: "why does it sometimes
        show only Context and no Rate Limits?". Root cause was a
        real gateway-side gap (usage.status can transiently report zero
        windows for the active provider while a run is in flight), not a
        bug here -- the section must always show its header (given a real
        provider) with either the data or an explicit note, never nothing."""
        js = _render_limits_fill_js({
            "provider": "anthropic", "windows": [], "sessionActive": True,
            "source": "pipe", "fetchedAt": "12:00:00", "error": None,
        })
        self.assertIn("Rate Limits", js)
        self.assertIn("No rate-limit data available", js)
        self.assertIn("a response is in progress", js)

    def test_limits_note_is_gated_on_empty_windows_not_always_shown(self):
        """_render_limits_fill_js returns JS *source*, not a rendered
        result -- both the note branch and the per-window render loop are
        always present as code regardless of data, so the only thing
        checkable from Python is that the note is correctly gated behind
        `DATA.windows.length === 0` rather than unconditional."""
        js = _render_limits_fill_js({
            "provider": "anthropic",
            "windows": [{"label": "5h", "usedPercent": 50, "resetIn": None, "resetAtMs": None}],
            "sessionActive": False, "source": "pipe", "fetchedAt": "12:00:00", "error": None,
        })
        gate_idx = js.index("if (DATA.windows.length === 0)")
        note_idx = js.index("No rate-limit data available")
        loop_idx = js.index("DATA.windows.forEach")
        self.assertTrue(gate_idx < note_idx < loop_idx)

    def test_limits_reset_shows_both_relative_and_absolute(self):
        """Relative countdown from the server, absolute clock time computed
        client-side (browser's own timezone, not the gateway container's)."""
        js = _render_limits_fill_js({
            "provider": "anthropic",
            "windows": [{"label": "5h", "usedPercent": 50, "resetIn": "1h21m", "resetAtMs": 1783790000000}],
            "sessionActive": False, "source": "pipe", "fetchedAt": "12:00:00", "error": None,
        })
        self.assertIn("resetAtMs", js)
        self.assertIn("toLocaleTimeString", js)
        self.assertIn("'resets in ' + w.resetIn", js)

    def test_compact_button_gated_on_identity_fields_present(self):
        """The Compact button's click handler needs chatId/messageId/
        sessionId (embedded in IDENTITY at open time, read via
        S.identity.*) to build a working callback -- the context fill must
        gate the button's creation on all three being present."""
        js = _render_context_fill_js({
            "provider": "x", "model": "", "context": {"usedTokens": "1k", "totalTokens": "10k", "pct": 10.0},
            "goal": None, "source": "pipe", "fetchedAt": "12:00:00", "error": None,
        })
        self.assertIn("S.identity.chatId && S.identity.messageId && S.identity.sessionId", js)
        self.assertIn("compactBtn", js)
        self.assertIn("S.triggerCompact", js)

    def test_compact_fetch_targets_this_same_action_with_mode_marker(self):
        js = _render_modal_open_js({"chatId": "c1", "messageId": "m1", "sessionId": "s1", "owuiModel": "x.y"})
        self.assertIn("/api/chat/actions/openclaw_status_action", js)
        self.assertIn("mode: 'compact'", js)
        self.assertIn("localStorage.getItem('token')", js)

    def test_subagents_hidden_entirely_when_tasks_empty(self):
        """General tracking, no detail: a permanently
        visible empty section for the common idle case would be more
        clutter than signal -- gated in source, not just data-dependent
        text, so it truly renders nothing (not an empty header)."""
        js = _render_subagents_fill_js({"tasks": [], "source": "pipe", "fetchedAt": "12:00:00", "error": None})
        gate_idx = js.index("if (!DATA.tasks || !DATA.tasks.length)")
        header_idx = js.index("sectionHeader('Subagents")
        self.assertLess(gate_idx, header_idx)

    def test_subagents_shows_clickable_rows_when_nonempty(self):
        # _render_subagents_fill_js returns JS *source*: task fields are
        # runtime values substituted into the DOM-building code, not
        # literal rendered text, so check the JSON payload plus the
        # row-building/click-handler expressions rather than rendered text.
        tasks = [
            {"id": "t1", "title": "Research X", "status": "running", "progressSummary": "reading docs",
             "terminalSummary": None, "error": None, "childSessionKey": "sess-1",
             "startedAt": 1783790000000, "endedAt": None},
        ]
        js = _render_subagents_fill_js({"tasks": tasks, "source": "pipe", "fetchedAt": "12:00:00", "error": None})
        self.assertIn('"title": "Research X"', js)
        self.assertIn("S.openSubagentDrawer(t)", js)
        self.assertIn("S.elapsedTime(t.startedAt, t.endedAt)", js)

    def test_sections_error_js_uses_shared_error_helper(self):
        js = _render_sections_error_js("boom")
        self.assertIn("openclaw-status-sections", js)
        self.assertIn('"boom"', js)


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

    async def test_success_fills_all_three_sections_independently(self):
        """Open happens before any network call; each of Context, Rate
        Limits, Subagents then emits its own separate execute fill as its
        own RPC resolves -- not one combined event. Exercises context+goal,
        rate-limit windows, and a nonzero subagent count all end to end."""
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
            if method == "tasks.list":
                self.assertEqual(params["status"], ["running", "queued"])
                return {"tasks": [
                    {"kind": "subagent", "status": "running"},
                    {"kind": "subagent", "status": "queued"},
                    {"kind": "cron", "status": "running"},  # must be excluded from the count
                ]}
            raise AssertionError(f"unexpected RPC: {method}")

        fake_conn = mock.AsyncMock()
        fake_conn.send_request = mock.AsyncMock(side_effect=fake_send_request)

        async def fake_get_connection(_valves_getter):
            # By the time the connection is fetched, the open (loading
            # skeletons) must already have been emitted -- proves open
            # happens before any network call, not just before return.
            self.assertEqual(len(emitted), 1)
            self.assertEqual(emitted[0]["type"], "execute")
            self.assertIn("openclaw-status-section-context", emitted[0]["data"]["code"])
            self.assertIn("animate-pulse", emitted[0]["data"]["code"])
            return fake_conn, "pipe"

        with mock.patch("openclaw_status_action._get_action_connection", new=fake_get_connection):
            result = await action.action(
                {"chat_id": "chat-1", "id": "msg-1", "session_id": "sess-1", "model": "x.y"},
                __user__={"id": "user-1"}, __event_emitter__=emitter,
            )

        self.assertEqual(result["status"], "ok")
        codes = _emitted_codes(emitted)
        self.assertEqual(len(codes), 4)  # open + 3 independent fills

        context_code = next(c for c in codes if "getElementById('openclaw-status-section-context')" in c)
        self.assertIn("anthropic", context_code)
        self.assertIn("Pursuing goal", context_code)

        limits_code = next(c for c in codes if "getElementById('openclaw-status-section-limits')" in c)
        self.assertIn("5h", limits_code)
        self.assertIn("1783790000000", limits_code)

        subagents_code = next(c for c in codes if "getElementById('openclaw-status-section-subagents')" in c)
        # cron task excluded -- exactly the two "subagent"-kind rows come through
        self.assertEqual(subagents_code.count('"status": "running"') + subagents_code.count('"status": "queued"'), 2)
        self.assertNotIn('"kind": "cron"', subagents_code)

    async def test_connect_failure_shows_dialog_wide_error(self):
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
        codes = _emitted_codes(emitted)
        self.assertEqual(len(codes), 2)  # open, then one dialog-wide error fill
        self.assertIn("openclaw-status-sections", codes[1])
        self.assertIn("gateway unreachable", codes[1])

    async def test_per_section_rpc_failure_only_errors_that_section(self):
        """A failure fetching one section's data (here: tasks.list) must
        not prevent the other two, independently-loading sections from
        rendering their own real data."""
        action = Action()
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        async def fake_send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"modelProvider": "anthropic", "model": "claude-sonnet-5"}}
            if method == "usage.status":
                return {"providers": []}
            if method == "tasks.list":
                raise RuntimeError("tasks unavailable")
            raise AssertionError(f"unexpected RPC: {method}")

        fake_conn = mock.AsyncMock()
        fake_conn.send_request = mock.AsyncMock(side_effect=fake_send_request)

        with mock.patch("openclaw_status_action._get_action_connection",
                         new=mock.AsyncMock(return_value=(fake_conn, "pipe"))):
            result = await action.action(
                {"chat_id": "chat-1"}, __user__={"id": "user-1"}, __event_emitter__=emitter,
            )

        self.assertEqual(result["status"], "ok")
        codes = _emitted_codes(emitted)
        context_code = next(c for c in codes if "getElementById('openclaw-status-section-context')" in c)
        self.assertIn('"error": null', context_code)
        subagents_code = next(c for c in codes if "getElementById('openclaw-status-section-subagents')" in c)
        self.assertIn("tasks unavailable", subagents_code)


class CompactTests(unittest.IsolatedAsyncioTestCase):
    """The dialog's Compact button fetches back to this same Action with a
    synthetic mode="compact" marker (see triggerCompact() in
    _MODAL_OPEN_JS_TEMPLATE) -- these cover the Python-side dispatch and
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

    async def test_success_shows_compacting_then_refreshes_all_sections(self):
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
                # single-iteration happy path. Calls after that (the
                # _run_status refresh's own sessions.describe) report done.
                status = "running" if describe_calls["n"] == 1 else "done"
                return {"session": {"status": status, "modelProvider": "anthropic",
                                     "model": "claude-sonnet-5"}}
            if method == "usage.status":
                return {"providers": []}
            if method == "tasks.list":
                return {"tasks": []}
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
        codes = _emitted_codes(emitted)
        self.assertIn("Compacting", codes[0])
        self.assertIn("openclaw-status-sections", codes[1])  # skeleton reset, not full re-open
        # The refresh must reach all three sections again, not just Context.
        self.assertTrue(any("getElementById('openclaw-status-section-context')" in c for c in codes))
        self.assertTrue(any("getElementById('openclaw-status-section-limits')" in c for c in codes))
        self.assertTrue(any("getElementById('openclaw-status-section-subagents')" in c for c in codes))
        context_code = next(c for c in codes if "getElementById('openclaw-status-section-context')" in c)
        self.assertIn("anthropic", context_code)

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
        codes = _emitted_codes(emitted)
        self.assertIn("timed out", codes[-1])
        self.assertIn("openclaw-status-sections", codes[-1])


class ExtractTranscriptAndToolsTests(unittest.TestCase):
    """Message shape here mirrors what sessions_history returns for a real
    session (confirmed live, 2026-07-12): content is a list of typed parts
    ("text" / "toolcall" / "tool_result" / "thinking"), with a toolcall and
    its matching tool_result sometimes sharing one message's content array.
    chat.history is a display-normalized projection of the same underlying
    transcript, not a separate schema, so this is the expected shape there
    too."""

    def test_splits_text_from_tool_calls_and_pairs_results(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "toolcall", "id": "t1", "name": "ToolSearch", "arguments": {"query": "foo"}},
                {"type": "tool_result", "tool_use_id": "t1", "content": "found it", "name": "ToolSearch"},
            ]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "internal reasoning, never shown"},
                {"type": "text", "text": "Here is the answer."},
            ]},
            {"role": "user", "content": "What about this?"},
        ]
        text_rows, tool_calls = _extract_transcript_and_tools(messages)

        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["name"], "ToolSearch")
        self.assertIn("foo", tool_calls[0]["arguments"])
        self.assertEqual(tool_calls[0]["result"], "found it")

        # The tool-call-only message contributes no text row (goes in Tools
        # tab instead); thinking is never shown in either tab.
        self.assertEqual(len(text_rows), 2)
        self.assertEqual(text_rows[0]["text"], "Here is the answer.")
        self.assertNotIn("internal reasoning", text_rows[0]["text"])
        self.assertEqual(text_rows[1], {"role": "user", "text": "What about this?"})

    def test_empty_and_missing_content_do_not_crash(self):
        text_rows, tool_calls = _extract_transcript_and_tools([{"role": "user", "content": []}, {"role": "user"}])
        self.assertEqual(text_rows, [])
        self.assertEqual(tool_calls, [])

    def test_marks_failed_tool_result_as_error(self):
        text_rows, tool_calls = _extract_transcript_and_tools([
            {"role": "assistant", "content": [
                {"type": "toolcall", "id": "t1", "name": "Bash", "arguments": {"command": "nope"}},
                {"type": "tool_result", "tool_use_id": "t1", "content": "boom",
                 "is_error": True, "name": "Bash"},
            ]},
        ])
        self.assertTrue(tool_calls[0]["isError"])

    def test_running_session_history_yields_no_tool_calls(self):
        """The regression this whole live-buffer path exists for.

        Measured against the real gateway (2026-07-30): while a subagent is
        RUNNING, chat.history returns only its seed user message(s) -- an
        in-flight turn isn't written to the session store until it completes,
        at which point every tool call appears at once. So history alone can
        never populate the Tools tab for a working subagent, which is exactly
        the case the drawer was built to watch.
        """
        running_history = [
            {"role": "user", "content": "[Subagent Context] You are running as a subagent"},
            {"role": "user", "content": "[Inter-session message] status?"},
        ]
        text_rows, tool_calls = _extract_transcript_and_tools(running_history)
        self.assertEqual(tool_calls, [])
        self.assertEqual(len(text_rows), 2)


def _drawer_conn(send_request):
    """Fake gateway connection for the drawer's poll loop."""
    conn = mock.Mock()
    conn.send_request = mock.AsyncMock(side_effect=send_request)
    return conn


class SubagentDetailTests(unittest.IsolatedAsyncioTestCase):
    """The Subagents section's rows fetch back to this same Action with a
    synthetic mode="subagent-detail" marker (see openSubagentDrawer() in
    _MODAL_OPEN_JS_TEMPLATE) -- these cover the Python-side dispatch and the
    hold-the-request-open poll loop (same shape as CompactTests exercises
    for _run_compact, just polling tasks.get instead of sessions.describe)."""

    DETAIL_BODY = {"mode": "subagent-detail", "chat_id": "chat-1", "id": "msg-1", "session_id": "sess-1",
                   "model": "x.y", "taskId": "task-1", "taskTitle": "Research X", "taskStatus": "running"}

    async def test_mode_dispatch_routes_to_subagent_detail(self):
        action = Action()
        with mock.patch.object(action, "_run_subagent_detail",
                                new=mock.AsyncMock(return_value={"status": "ok"})) as m:
            result = await action.action(self.DETAIL_BODY, __user__={"id": "u1"}, __event_emitter__=mock.AsyncMock())
        m.assert_awaited_once()
        self.assertEqual(result["status"], "ok")

    async def test_missing_task_id_returns_error(self):
        action = Action()
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        result = await action._run_subagent_detail({"mode": "subagent-detail"}, {"id": "u1"}, emitter)
        self.assertEqual(result["status"], "error")
        self.assertIn("taskId", _emitted_codes(emitted)[0])

    async def test_polls_until_terminal_status_then_stops(self):
        action = Action()
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        get_calls = {"n": 0}

        async def fake_send_request(method, params, timeout=5):
            if method == "tasks.get":
                get_calls["n"] += 1
                self.assertEqual(params["taskId"], "task-1")
                status = "running" if get_calls["n"] == 1 else "completed"
                return {"task": {"id": "task-1", "title": "Research X", "status": status,
                                 "childSessionKey": "child-sess-1", "startedAt": 1783790000000,
                                 "endedAt": None if status == "running" else 1783790005000}}
            if method == "sessions.describe":
                return {"session": {"modelProvider": "anthropic", "model": "claude-sonnet-5",
                                     "contextTokens": 1000000, "totalTokens": 5000}}
            if method == "chat.history":
                self.assertEqual(params["sessionKey"], "child-sess-1")
                return {"messages": [{"role": "assistant", "content": [{"type": "text", "text": "working on it"}]}]}
            raise AssertionError(f"unexpected RPC: {method}")

        fake_conn = _drawer_conn(fake_send_request)

        with mock.patch("openclaw_status_action._get_action_connection",
                         new=mock.AsyncMock(return_value=(fake_conn, "pipe"))), \
             mock.patch("openclaw_status_action.asyncio.sleep", new=mock.AsyncMock()):
            result = await action._run_subagent_detail(self.DETAIL_BODY, {"id": "u1"}, emitter)

        self.assertEqual(result["status"], "ok")
        # Looped (running -> completed), not a single-iteration happy path.
        self.assertEqual(get_calls["n"], 2)
        codes = _emitted_codes(emitted)
        self.assertIn("openclaw-drawer-root", codes[0])  # initial open
        self.assertTrue(any("openclaw-drawer-section-overview" in c for c in codes))
        self.assertTrue(any("openclaw-drawer-section-transcript" in c and "working on it" in c for c in codes))
        self.assertTrue(any("openclaw-drawer-section-tools" in c for c in codes))

    async def test_running_subagent_shows_no_tool_calls_yet(self):
        """Pins the known ELI-26 gap so it can't be silently "fixed" again.

        chat.history returns what a RUNNING subagent really returns -- seed user
        messages only, zero tool calls (measured against the live gateway
        2026-07-30). The Tools tab therefore renders its empty state for the
        whole run, and that is not something the pipe can fix: a run's tool
        events go only to the connection that started it, so the drawer -- which
        starts nothing -- cannot receive them. Measured with two connections on
        one session key: initiator 4 tool events, subscriber 0.

        If a future change makes live tool calls appear here, it must come with
        a Gateway-side change; flip this test then, and re-measure first.
        """
        action = Action()
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        async def fake_send_request(method, params, timeout=5):
            if method == "tasks.get":
                return {"task": {"id": "task-1", "title": "Research X", "status": "completed",
                                 "childSessionKey": "child-sess-1", "startedAt": 1783790000000}}
            if method == "sessions.describe":
                return {"session": {"modelProvider": "anthropic", "model": "claude-sonnet-5"}}
            if method == "chat.history":
                return {"messages": [
                    {"role": "user", "content": "[Subagent Context] running as a subagent"},
                ]}
            raise AssertionError(f"unexpected RPC: {method}")

        fake_conn = _drawer_conn(fake_send_request)

        with mock.patch("openclaw_status_action._get_action_connection",
                         new=mock.AsyncMock(return_value=(fake_conn, "pipe"))), \
             mock.patch("openclaw_status_action.asyncio.sleep", new=mock.AsyncMock()):
            result = await action._run_subagent_detail(self.DETAIL_BODY, {"id": "u1"}, emitter)

        self.assertEqual(result["status"], "ok")
        tools_code = [c for c in _emitted_codes(emitted)
                      if "openclaw-drawer-section-tools" in c]
        self.assertTrue(tools_code, "no Tools tab fill was emitted")
        self.assertIn('"toolCalls": []', tools_code[-1].replace("'", '"'))

    async def test_persisted_tool_error_marks_the_card(self):
        """A flushed tool_result carrying is_error still marks the card ❌ --
        the half of the change that works, since it reads persisted rows."""
        rows = [
            {"role": "assistant", "content": [
                {"type": "toolcall", "id": "toolu_a", "name": "Read", "arguments": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_a",
                 "content": "boom", "is_error": True}]},
        ]
        _rows, calls = _extract_transcript_and_tools(rows)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["isError"])

    async def test_deadline_bounds_the_loop(self):
        action = Action()
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        async def fake_send_request(method, params, timeout=5):
            if method == "tasks.get":
                return {"task": {"id": "task-1", "status": "running", "childSessionKey": None}}
            raise AssertionError(f"unexpected RPC: {method}")

        fake_conn = _drawer_conn(fake_send_request)

        # Simulate the poll deadline elapsing without a real wait: each
        # asyncio.sleep call jumps the fake clock forward past it immediately.
        clock = {"t": 0.0}

        def fake_time():
            return clock["t"]

        async def fake_sleep(_seconds):
            clock["t"] += 10_000

        with mock.patch("openclaw_status_action._get_action_connection",
                         new=mock.AsyncMock(return_value=(fake_conn, "pipe"))), \
             mock.patch("openclaw_status_action.time.time", side_effect=fake_time), \
             mock.patch("openclaw_status_action.asyncio.sleep", new=fake_sleep):
            result = await action._run_subagent_detail(self.DETAIL_BODY, {"id": "u1"}, emitter)

        # Never reaches a terminal status, but the loop still returns
        # instead of hanging forever -- the whole point of the deadline.
        self.assertEqual(result["status"], "ok")


class SubagentProactiveMessageClickTests(unittest.IsolatedAsyncioTestCase):
    """A real OWUI toolbar click on a proactively-delivered sub-agent-finished
    message is mode-less (OWUI itself never sends "mode") but carries a
    hidden `<!-- openclaw:taskId=... -->` marker in body["content"] (see
    `_deliver_subagent_proactive_owui_message` in gateway.py). Deliberately
    no separate Action/button for this -- the
    existing default click handler detects the marker and redirects into
    the same subagent-detail drawer instead of the general status dialog."""

    async def test_marker_in_content_routes_to_subagent_detail(self):
        action = Action()
        body = {
            "chat_id": "chat-1", "id": "msg-1", "session_id": "sess-1",
            "model": "openclaw_gateway.default",
            "content": "*↳ Sub-agent finished: Fix bug*\n\nAll done.\n\n"
                       "<!-- openclaw:taskId=task-77 -->",
        }
        with mock.patch.object(action, "_run_subagent_detail",
                                new=mock.AsyncMock(return_value={"status": "ok"})) as m:
            result = await action.action(body, __user__={"id": "u1"}, __event_emitter__=mock.AsyncMock())
        m.assert_awaited_once()
        routed_body = m.await_args.args[0]
        self.assertEqual(routed_body["taskId"], "task-77")
        self.assertEqual(result["status"], "ok")

    async def test_no_marker_routes_to_normal_status(self):
        action = Action()
        body = {"chat_id": "chat-1", "id": "msg-1", "session_id": "sess-1",
                "model": "openclaw_gateway.default", "content": "just a normal reply"}
        with mock.patch.object(action, "_run_subagent_detail", new=mock.AsyncMock()) as detail_m, \
             mock.patch.object(action, "_run_status",
                                new=mock.AsyncMock(return_value={"status": "ok"})) as status_m:
            result = await action.action(body, __user__={"id": "u1"}, __event_emitter__=mock.AsyncMock())
        detail_m.assert_not_awaited()
        status_m.assert_awaited_once()
        self.assertEqual(result["status"], "ok")

    async def test_missing_content_routes_to_normal_status(self):
        action = Action()
        body = {"chat_id": "chat-1", "id": "msg-1", "session_id": "sess-1", "model": "openclaw_gateway.default"}
        with mock.patch.object(action, "_run_subagent_detail", new=mock.AsyncMock()) as detail_m, \
             mock.patch.object(action, "_run_status",
                                new=mock.AsyncMock(return_value={"status": "ok"})):
            await action.action(body, __user__={"id": "u1"}, __event_emitter__=mock.AsyncMock())
        detail_m.assert_not_awaited()

    async def test_explicit_mode_still_takes_priority_over_marker_sniffing(self):
        """A synthetic mode="compact"/"subagent-detail" click (the dialog's
        own in-page buttons) must never be reinterpreted by content
        sniffing, even if its body coincidentally carries old marker text."""
        action = Action()
        body = {"mode": "compact", "chat_id": "chat-1",
                "content": "<!-- openclaw:taskId=task-1 -->"}
        with mock.patch.object(action, "_run_compact", new=mock.AsyncMock(return_value={"status": "ok"})) as compact_m, \
             mock.patch.object(action, "_run_subagent_detail", new=mock.AsyncMock()) as detail_m:
            await action.action(body, __user__={"id": "u1"}, __event_emitter__=mock.AsyncMock())
        compact_m.assert_awaited_once()
        detail_m.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
