#!/usr/bin/env python3
"""Focused unit tests for the companion Action Function (openclaw_status_action.py).

Mirrors test_pipe_unit.py's dependency-stubbing pattern so this runs without
a real OWUI/pydantic/websockets/cryptography environment.
"""

import asyncio
import datetime
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before this file's stub can shadow it)

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
    _control_ui_session_url,
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
    """The dialog's own execute codes. Excludes the composer Status button
    snippet every run also delivers (covered by test_composer_unit.py), so
    these tests keep counting only what the dialog itself renders."""
    return [
        e["data"]["code"] for e in emitted
        if e["type"] == event_type and "__openclawComposerBtn" not in e["data"]["code"]
    ]


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


# A page just big enough for the Status dialog's own JS: elements with
# children, ids, dataset, `[data-*]` lookups, and a clock and timers the test
# drives by hand. Reads {"open": js, "fills": [js, ...], "steps": [...]} on
# stdin and prints what each step saw.
# A page just big enough to run the dialog's JS under bun or node: the
# elements, ids, data-* selectors and timers it uses.
_FAKE_DOM_JS = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
let now = input.now;
Date.now = () => now;
const timers = new Map();
let nextTimer = 1;
global.setInterval = (fn) => { const id = nextTimer++; timers.set(id, fn); return id; };
global.clearInterval = (id) => { timers.delete(id); };
const camel = (s) => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parent = null;
    this.style = {}; this.dataset = {}; this.className = ''; this.id = ''; this.text = '';
  }
  appendChild(c) { c.remove(); c.parent = this; this.children.push(c); return c; }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this);
    this.parent = null;
  }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === document.body; }
  set innerHTML(v) { this.children = []; this.text = ''; }
  set textContent(v) { this.children = []; this.text = String(v); }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(''); }
  addEventListener() {}
  walk(out = []) { for (const c of this.children) { out.push(c); c.walk(out); } return out; }
  querySelectorAll(sel) {
    const m = /^\[data-([a-z-]+)\]$/.exec(sel);
    if (!m) throw new Error('unsupported selector ' + sel);
    return this.walk().filter((e) => camel(m[1]) in e.dataset);
  }
}
global.window = global;
global.document = {
  body: new El('body'),
  documentElement: { classList: { contains: () => false } },
  createElement: (t) => new El(t),
  getElementById: (id) => document.body.walk().find((e) => e.id === id) || null,
  addEventListener() {}, removeEventListener() {},
};
const run = (js) => new Function(js)();
"""

_DIALOG_PAGE_JS = _FAKE_DOM_JS + r"""
const elapsedCells = () => {
  const section = document.getElementById('openclaw-status-section-subagents');
  return section ? section.walk().filter((e) => e.tagName === 'SPAN' && e.style.fontSize === '10px')
    .map((e) => e.textContent) : [];
};
const seen = [];
run(input.open);
input.fills.forEach(run);
for (const step of input.steps) {
  if (step.advance) {
    for (let s = 0; s < step.advance; s++) { now += 1000; [...timers.values()].forEach((f) => f()); }
  }
  if (step.close) document.getElementById('openclaw-status-modal-root').remove();
  if (step.reopen) { run(input.open); input.fills.forEach(run); }
  seen.push({ cells: elapsedCells(), timers: timers.size });
}
console.log(JSON.stringify(seen));
"""


_DRAWER_TOOLS_PAGE_JS = _FAKE_DOM_JS + r"""
run(input.open);
run(input.drawer);
const cards = () => document.getElementById('openclaw-drawer-section-tools').walk()
  .filter((e) => e.tagName === 'DETAILS');
const seen = [];
for (const step of input.steps) {
  if (step.fill) run(step.fill);
  if (step.toggle) {
    // What a click on a card's summary does: flip that <details>.
    const card = cards().find((d) => d.children[0].textContent.endsWith(' ' + step.toggle));
    card.open = !card.open;
  }
  seen.push(cards().map((d) => [d.children[0].textContent, !!d.open]));
}
console.log(JSON.stringify(seen));
"""


class DrawerToolCardsStayOpenTests(unittest.TestCase):
    """ELI-133 (ELI-110 screenshots, run 38636471099): while a sub-agent
    runs, the drawer refills its Tools tab every 2.5 s, and each refill
    rebuilt every card closed, so a card the user opened snapped shut. A
    card keeps its open or closed state across refills, keyed by its tool
    call id, as the Transcript keeps its scroll."""

    TASK = {"id": "t1", "title": "Child", "status": "running", "childSessionKey": "agent:main:subagent:c",
            "startedAt": 1, "endedAt": None, "progressSummary": None, "terminalSummary": None, "error": None}

    @staticmethod
    def _call(cid, name):
        return {"id": cid, "name": name, "arguments": "{}", "result": "ok", "isError": False}

    def _fill(self, calls):
        return _render_drawer_tools_fill_js({"childSessionKey": self.TASK["childSessionKey"],
                                             "toolCalls": calls, "error": None})

    def _page(self, steps):
        import shutil
        import subprocess
        runtime = shutil.which("bun") or shutil.which("node")
        if runtime is None:
            self.skipTest("no bun or node")
        identity = {"chatId": "c", "messageId": "m", "sessionId": "s", "owuiModel": "x"}
        payload = {"now": 1, "open": _render_modal_open_js(identity),
                   "drawer": _render_drawer_open_js(self.TASK), "steps": steps}
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(_DRAWER_TOOLS_PAGE_JS)
        try:
            out = subprocess.run([runtime, fh.name], input=json.dumps(payload),
                                 capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(fh.name)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_an_opened_card_stays_open_across_a_refill(self):
        a, b, c = self._call("call-a", "read"), self._call("call-b", "exec"), self._call("call-c", "write")
        seen = self._page([{"fill": self._fill([a, b])}, {"toggle": "exec"},
                           {"fill": self._fill([a, b, c])}])
        self.assertEqual(seen[1], [["🔧 read", False], ["🔧 exec", True]])
        self.assertEqual(seen[2], [["🔧 read", False], ["🔧 exec", True], ["🔧 write", False]])

    def test_a_closed_card_stays_closed(self):
        a, b = self._call("call-a", "read"), self._call("call-b", "exec")
        seen = self._page([{"fill": self._fill([a, b])}, {"toggle": "read"}, {"toggle": "read"},
                           {"fill": self._fill([a, b])}])
        self.assertEqual(seen[3], [["🔧 read", False], ["🔧 exec", False]])

    def test_the_state_follows_the_call_not_its_position(self):
        """The history read is a window: when an older call drops out of it,
        the open card is still the same call, now one place up."""
        a, b, c = self._call("call-a", "read"), self._call("call-b", "exec"), self._call("call-c", "write")
        seen = self._page([{"fill": self._fill([a, b])}, {"toggle": "exec"},
                           {"fill": self._fill([b, c])}])
        self.assertEqual(seen[2], [["🔧 exec", True], ["🔧 write", False]])


_DRAWER_TRANSCRIPT_PAGE_JS = _FAKE_DOM_JS + r"""
run(input.open);
run(input.drawer);
run(input.fill);
const section = document.getElementById('openclaw-drawer-section-transcript');
console.log(JSON.stringify(section.children.map((r) => r.children.map((c) => c.textContent))));
"""


class DrawerTranscriptWithoutToolResultsTests(unittest.TestCase):
    """ELI-128: a tool's output shows once, in its Tools card. In OpenClaw's
    own shape (any runtime but claude-cli) the result is a `toolResult`
    message of its own, and the Transcript showed it again as a TOOLRESULT
    row; claude-cli's `tool_result` items never reached the Transcript."""

    TASK = DrawerToolCardsStayOpenTests.TASK

    def _transcript(self, history):
        import shutil
        import subprocess
        runtime = shutil.which("bun") or shutil.which("node")
        if runtime is None:
            self.skipTest("no bun or node")
        rows, _calls = _extract_transcript_and_tools(history)
        identity = {"chatId": "c", "messageId": "m", "sessionId": "s", "owuiModel": "x"}
        payload = {"now": 1, "open": _render_modal_open_js(identity),
                   "drawer": _render_drawer_open_js(self.TASK),
                   "fill": _render_drawer_transcript_fill_js(
                       {"childSessionKey": self.TASK["childSessionKey"], "messages": rows, "error": None})}
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(_DRAWER_TRANSCRIPT_PAGE_JS)
        try:
            out = subprocess.run([runtime, fh.name], input=json.dumps(payload),
                                 capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(fh.name)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_the_gateways_own_shape_shows_no_result_rows(self):
        shown = self._transcript([
            {"role": "user", "content": "Read the staging deploy log."},
            {"role": "assistant", "content": [
                {"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "README.md"}}]},
            {"role": "toolResult", "toolCallId": "call_1", "toolName": "read",
             "content": [{"type": "text", "text": "# Forecast"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "The staging log is clean."}]},
        ])
        self.assertEqual(shown, [["user", "Read the staging deploy log."],
                                 ["assistant", "The staging log is clean."]])

    def test_claude_cli_shape_is_unchanged(self):
        shown = self._transcript([
            {"role": "user", "content": "Read the staging deploy log."},
            {"role": "assistant", "content": [
                {"type": "toolcall", "id": "t1", "name": "read", "arguments": {"path": "README.md"}},
                {"type": "tool_result", "tool_use_id": "t1", "content": "# Forecast"},
                {"type": "text", "text": "The staging log is clean."}]},
        ])
        self.assertEqual(shown, [["user", "Read the staging deploy log."],
                                 ["assistant", "The staging log is clean."]])


class ElapsedTickTests(unittest.TestCase):
    """ELI-120: a running sub-agent's elapsed time counts up while the Status
    dialog stays open, instead of freezing at whatever it was when the
    dialog opened ("1s" for a child spawned a moment earlier)."""

    NOW = 1_783_790_100_000

    def _page(self, steps, tasks):
        import shutil
        import subprocess
        runtime = shutil.which("bun") or shutil.which("node")
        if runtime is None:
            self.skipTest("no bun or node")
        identity = {"chatId": "c", "messageId": "m", "sessionId": "s", "owuiModel": "x"}
        fill = _render_subagents_fill_js({"tasks": tasks, "source": "pipe", "fetchedAt": "12:00:00", "error": None})
        payload = {"now": self.NOW, "open": _render_modal_open_js(identity), "fills": [fill], "steps": steps}
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(_DIALOG_PAGE_JS)
        try:
            out = subprocess.run([runtime, fh.name], input=json.dumps(payload),
                                 capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(fh.name)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def _task(self, tid, status, started, ended=None):
        return {"id": tid, "title": tid, "status": status, "progressSummary": None, "terminalSummary": None,
                "error": None, "childSessionKey": tid, "startedAt": started, "endedAt": ended}

    def test_running_rows_count_up_and_finished_rows_stay(self):
        iso = datetime.datetime.fromtimestamp((self.NOW - 2000) / 1000, datetime.timezone.utc).isoformat()
        tasks = [
            self._task("running-ms", "running", self.NOW - 1000),
            self._task("running-iso", "running", iso),
            self._task("done", "completed", self.NOW - 60_000, self.NOW - 30_000),
        ]
        seen = self._page([{}, {"advance": 1}, {"advance": 64}], tasks)
        self.assertEqual([s["cells"] for s in seen], [
            ["1s", "2s", "30s"],
            ["2s", "3s", "30s"],
            ["1m", "1m", "30s"],
        ])

    def test_the_ticker_stops_with_the_dialog_and_a_reopen_keeps_one(self):
        tasks = [self._task("running", "running", self.NOW - 1000)]
        seen = self._page([{"reopen": True}, {"reopen": True}, {"close": True}, {"advance": 1}], tasks)
        self.assertEqual([s["timers"] for s in seen], [1, 1, 1, 0])


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
            if method == "sessions.list":
                self.assertTrue(params["spawnedBy"].startswith("agent:main:openwebui-"))
                return {"sessions": [
                    {"key": "agent:main:subagent:a", "status": "running"},
                    {"key": "agent:main:subagent:b", "status": "queued"},
                    {"key": "agent:main:subagent:c", "status": "done"},  # must be excluded from the count
                ]}
            if method == "sessions.preview":
                return {"previews": []}
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
            result = await _open_dialog(action.action(
                {"chat_id": "chat-1", "id": "msg-1", "session_id": "sess-1", "model": "x.y"},
                __user__={"id": "user-1"}, __event_emitter__=emitter,
            ), fake_conn)

        # Two children are running, so the dialog stays open to follow them.
        self.assertEqual(result, "following")
        codes = _emitted_codes(emitted)
        # open + 3 independent fills + the Subagents re-read that starts the
        # follow (ELI-127)
        self.assertEqual(len(codes), 5)

        context_code = next(c for c in codes if "getElementById('openclaw-status-section-context')" in c)
        self.assertIn("anthropic", context_code)
        self.assertIn("Pursuing goal", context_code)

        limits_code = next(c for c in codes if "getElementById('openclaw-status-section-limits')" in c)
        self.assertIn("5h", limits_code)
        self.assertIn("1783790000000", limits_code)

        subagents_code = next(c for c in codes if "getElementById('openclaw-status-section-subagents')" in c)
        # the ended child excluded -- exactly the running and queued rows come through
        self.assertEqual(subagents_code.count('"status": "running"') + subagents_code.count('"status": "queued"'), 2)
        self.assertNotIn("agent:main:subagent:c", subagents_code)

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
        """A failure fetching one section's data (here: the child sessions) must
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
            if method == "sessions.list":
                raise RuntimeError("children unavailable")
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
        self.assertIn("children unavailable", subagents_code)


class ClaudeCliUsageFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Since OpenClaw 2026.9.1 usage.status has no `anthropic` entry on a
    Claude-CLI-only setup; the Rate Limits section falls back to the
    companion plugin's claudeCliUsage.status."""

    async def _run(self, usage_status, cli_usage):
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        async def fake_send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"modelProvider": "anthropic", "model": "claude-opus-5-5"}}
            if method == "usage.status":
                return usage_status
            if method == "sessions.list":
                return {"sessions": []}
            if method == "claudeCliUsage.status":
                if isinstance(cli_usage, Exception):
                    raise cli_usage
                return cli_usage
            raise AssertionError(f"unexpected RPC: {method}")

        fake_conn = mock.AsyncMock()
        fake_conn.send_request = mock.AsyncMock(side_effect=fake_send_request)
        with mock.patch("openclaw_status_action._get_action_connection",
                         new=mock.AsyncMock(return_value=(fake_conn, "pipe"))):
            await Action().action({"chat_id": "chat-1"}, __user__={"id": "user-1"}, __event_emitter__=emitter)
        methods = [c.args[0] for c in fake_conn.send_request.call_args_list]
        limits_code = next(c for c in _emitted_codes(emitted)
                           if "getElementById('openclaw-status-section-limits')" in c)
        return methods, limits_code

    async def test_missing_anthropic_entry_uses_claude_cli_usage(self):
        methods, limits_code = await self._run(
            {"providers": [{"provider": "openai", "windows": [{"label": "720h", "usedPercent": 0}]}]},
            {"provider": "anthropic", "source": "claude-cli", "windows": [
                {"label": "5h", "usedPercent": 39, "resetAt": 1790376599786},
                {"label": "Week", "usedPercent": 3, "resetAt": 1790960399786},
            ]},
        )
        self.assertIn("claudeCliUsage.status", methods)
        self.assertIn('"label": "5h"', limits_code)
        self.assertIn('"label": "Week"', limits_code)
        self.assertNotIn("720h", limits_code)

    async def test_native_anthropic_windows_skip_the_fallback(self):
        methods, limits_code = await self._run(
            {"providers": [{"provider": "anthropic", "windows": [{"label": "5h", "usedPercent": 72}]}]},
            AssertionError("fallback must not be called"),
        )
        self.assertNotIn("claudeCliUsage.status", methods)
        self.assertIn('"usedPercent": 72', limits_code)

    async def test_gateway_without_plugin_keeps_empty_note(self):
        methods, limits_code = await self._run(
            {"providers": []}, RuntimeError("unknown method: claudeCliUsage.status"),
        )
        self.assertIn("claudeCliUsage.status", methods)
        self.assertIn('"windows": []', limits_code)
        self.assertIn('"error": null', limits_code)


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
            if method == "sessions.list":
                return {"sessions": []}
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

    def test_a_result_shows_its_output_not_the_raw_envelope(self):
        """ELI-125: a tool_result's content can be a list of blocks or the
        agent-tool envelope {content, details}. The Tools tab shows the
        output, the same rule as the chat's tool cards (ELI-121), not the
        whole thing as JSON with the tool's metadata in it."""
        results = {
            "text blocks": ([{"type": "text", "text": "a1b2 Fix the cache"}, {"type": "text", "text": "c3d4 Docs"}],
                            "a1b2 Fix the cache\nc3d4 Docs"),
            "envelope": ({"content": [{"type": "text", "text": "build ok"}],
                          "details": {"status": "completed", "exitCode": 0, "cwd": "/home/sam/forecast"}},
                         "build ok"),
            "an image": ([{"type": "image", "mimeType": "image/png"}], "[image/png image]"),
            "a string": ("plain output", "plain output"),
        }
        for label, (content, shown) in results.items():
            with self.subTest(label):
                _rows, tool_calls = _extract_transcript_and_tools([
                    {"role": "assistant", "content": [
                        {"type": "toolcall", "id": "t1", "name": "exec", "arguments": {"command": "git log"}},
                        {"type": "tool_result", "tool_use_id": "t1", "content": content},
                    ]},
                ])
                self.assertEqual(tool_calls[0]["result"], shown)

    def test_the_gateways_own_tool_shape_fills_the_tools_tab(self):
        """Outside claude-cli imports, chat.history keeps OpenClaw's own shape
        (seen in the screenshots stack, 2026-10-14): a `toolCall` block in the
        assistant message and the result as a `toolResult` message with
        `toolCallId`. The Tools tab only knew claude-cli's shape and said
        "No tool calls yet." for every such child."""
        history = [
            {"role": "user", "content": "Price out Fly.io."},
            {"role": "assistant", "content": [
                {"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "README.md"}}]},
            {"role": "toolResult", "toolCallId": "call_1", "toolName": "read",
             "content": [{"type": "text", "text": "# Forecast"}, {"type": "text", "text": "A small weather app."}]},
            {"role": "assistant", "content": [
                {"type": "toolCall", "id": "call_2", "name": "exec", "arguments": {"command": "false"}}]},
            {"role": "toolResult", "toolCallId": "call_2", "toolName": "exec", "isError": True,
             "content": [{"type": "text", "text": "exit 1"}]},
        ]
        _rows, tool_calls = _extract_transcript_and_tools(history)
        self.assertEqual([(c["name"], c["result"], c["isError"]) for c in tool_calls], [
            ("read", "# Forecast\nA small weather app.", False),
            ("exec", "exit 1", True),
        ])
        self.assertIn("README.md", tool_calls[0]["arguments"])

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


async def _open_dialog(opening, conn):
    """Run a status dialog Action until it returns, or until it holds open
    to follow listed sub-agents that never change (ELI-127); then stop it,
    as Open WebUI does when the request goes away. Returns the Action's
    result, or "following"."""
    import asyncio
    followed = asyncio.Event()

    class _Unchanged:
        async def wait(self):
            followed.set()
            await asyncio.Future()

    conn.session_change_event = lambda _key: _Unchanged()
    task = asyncio.ensure_future(opening)
    waiting = asyncio.ensure_future(followed.wait())
    await asyncio.wait([task, waiting], return_when=asyncio.FIRST_COMPLETED)
    waiting.cancel()
    if task.done():
        return task.result()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return "following"


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
    for _run_compact, polling the child session)."""

    CHILD = "agent:main:subagent:child-sess-1"
    DETAIL_BODY = {"mode": "subagent-detail", "chat_id": "chat-1", "id": "msg-1", "session_id": "sess-1",
                   "model": "x.y", "taskId": CHILD, "taskTitle": "Research X", "taskStatus": "running"}

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
            if method == "sessions.describe":
                get_calls["n"] += 1
                self.assertEqual(params["key"], self.CHILD)
                status = "running" if get_calls["n"] == 1 else "done"
                return {"session": {"modelProvider": "anthropic", "model": "claude-sonnet-5",
                                     "contextTokens": 1000000, "totalTokens": 5000, "status": status,
                                     "startedAt": 1783790000000,
                                     "endedAt": None if status == "running" else 1783790005000}}
            if method == "chat.history":
                self.assertEqual(params["sessionKey"], self.CHILD)
                return {"messages": [{"role": "assistant", "content": [{"type": "text", "text": "working on it"}]}]}
            raise AssertionError(f"unexpected RPC: {method}")

        fake_conn = _drawer_conn(fake_send_request)

        with mock.patch("openclaw_status_action._get_action_connection",
                         new=mock.AsyncMock(return_value=(fake_conn, "pipe"))), \
             mock.patch("openclaw_status_action.asyncio.sleep", new=mock.AsyncMock()):
            result = await action._run_subagent_detail(self.DETAIL_BODY, {"id": "u1"}, emitter)

        self.assertEqual(result["status"], "ok")
        # Looped (running -> done), not a single-iteration happy path.
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
            if method == "sessions.describe":
                return {"session": {"modelProvider": "anthropic", "model": "claude-sonnet-5",
                                     "status": "done", "startedAt": 1783790000000}}
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
            if method == "sessions.describe":
                return {"session": {"status": "running"}}
            if method == "chat.history":
                return {"messages": []}
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
    """A real OWUI toolbar click on an old sub-agent-finished message (written
    by bridge versions before ELI-100; read-only shim `_SUBAGENT_TASK_ID_MARKER_RE`)
    is mode-less (OWUI itself never sends "mode") but carries a hidden
    `<!-- openclaw:taskId=... -->` marker in body["content"]. Deliberately
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


class UpdatedTimeIsTheBrowsersClockTests(unittest.IsolatedAsyncioTestCase):
    """ELI-140 (2026-10-15): the footer's "Updated HH:MM:SS" came from the
    Open WebUI server's clock (`time.strftime`), while the rate-limit resets
    beside it are formatted in the browser. With the container on UTC and
    the reader in Asia/Jerusalem, "Updated" was 3 hours off. The dialog's
    own fill, rendered in the reader's timezone, must show the reader's
    time."""

    FETCHED = 1792054800  # 2026-10-15 09:00:00 UTC, 12:00:00 in Jerusalem

    async def _context_fill(self):
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        async def fake_send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"modelProvider": "anthropic", "model": "m",
                                    "contextTokens": 1000, "totalTokens": 100}}
            if method == "usage.status":
                return {"providers": []}
            if method == "sessions.list":
                return {"sessions": []}
            return {}

        fake_conn = mock.AsyncMock()
        fake_conn.send_request = mock.AsyncMock(side_effect=fake_send_request)

        async def fake_get_connection(_valves_getter):
            return fake_conn, "pipe"

        with mock.patch("openclaw_status_action._get_action_connection", new=fake_get_connection), \
                mock.patch("openclaw_status_action.time.time", return_value=self.FETCHED):
            await _open_dialog(Action().action(
                {"chat_id": "chat-1", "id": "msg-1", "session_id": "sess-1", "model": "x.y"},
                __user__={"id": "user-1"}, __event_emitter__=emitter,
            ), fake_conn)
        codes = _emitted_codes(emitted)
        return codes[0], next(c for c in codes if "openclaw-status-section-context" in c and c != codes[0])

    async def test_updated_is_on_the_readers_clock(self):
        import shutil
        import subprocess
        runtime = shutil.which("bun") or shutil.which("node")
        if runtime is None:
            self.skipTest("no bun or node")
        opening, fill = await self._context_fill()
        page = _FAKE_DOM_JS + r"""
run(input.open);
run(input.fill);
const f = document.getElementById('openclaw-status-footer');
process.stdout.write(JSON.stringify(f ? f.textContent : null));
"""
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(page)
        try:
            out = subprocess.run([runtime, fh.name], input=json.dumps({"now": 1, "open": opening, "fill": fill}),
                                 capture_output=True, text=True, timeout=60,
                                 env={**os.environ, "TZ": "Asia/Jerusalem"})
        finally:
            os.unlink(fh.name)
        self.assertEqual(out.returncode, 0, out.stderr)
        footer = json.loads(out.stdout)
        self.assertTrue(footer.startswith("Updated 12:00:00"), footer)


class ControlUiLinkTests(unittest.IsolatedAsyncioTestCase):
    """ELI-141 (Eliav, 2026-10-15): the Status dialog links to this session in
    the OpenClaw Control UI, and the sub-agent drawer to the child's. The
    route is the Control UI's short-id form, `/chat/<agent>/<hex of the key's
    trailing UUID>`, which its `sessions.resolve {shortId}` maps back to the
    key (checked live on 9.3 for chat 56b98d09)."""

    BASE = "https://claw.example.com/"
    CHAT = "56b98d09-f3b7-4e4f-b2bb-688ce63bfa53"
    USER = "2d206eab-acac-4945-bd2f-7e8b6365de8c"

    def test_the_session_url(self):
        key = _owui_session_key("main", self.USER, self.CHAT)
        self.assertEqual(_control_ui_session_url(self.BASE, key),
                         "https://claw.example.com/chat/main/56b98d09f3b74e4fb2bb688ce63bfa53")
        self.assertEqual(
            _control_ui_session_url("https://x.example/openclaw",
                                    "agent:research:subagent:0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0"),
            "https://x.example/openclaw/chat/research/0f1e2d3c4b5a69788796a5b4c3d2e1f0")

    def test_no_url_without_a_web_base_or_a_uuid_key(self):
        key = _owui_session_key("main", self.USER, self.CHAT)
        for base in ("", "   ", "javascript:alert(1)//", "claw.example.com"):
            self.assertIsNone(_control_ui_session_url(base, key), base)
        for key in ("agent:main:main", "agent:main:telegram:12345", "not-a-key", ""):
            self.assertIsNone(_control_ui_session_url(self.BASE, key), key)

    async def _open_code(self, control_ui_url):
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        fake_conn = mock.AsyncMock()
        fake_conn.send_request = mock.AsyncMock(side_effect=lambda method, params, timeout=5: {
            "sessions.describe": {"session": {}}, "usage.status": {"providers": []},
            "sessions.list": {"sessions": []}}.get(method, {}))

        async def fake_get_connection(_valves_getter):
            return fake_conn, "pipe"

        action = Action()
        action.valves.CONTROL_UI_URL = control_ui_url
        with mock.patch("openclaw_status_action._get_action_connection", new=fake_get_connection):
            await _open_dialog(action.action(
                {"chat_id": self.CHAT, "id": "msg-1", "session_id": "sess-1", "model": "x.y"},
                __user__={"id": self.USER}, __event_emitter__=emitter,
            ), fake_conn)
        return _emitted_codes(emitted)[0]

    def _links(self, opening, drawer=None):
        import shutil
        import subprocess
        runtime = shutil.which("bun") or shutil.which("node")
        if runtime is None:
            self.skipTest("no bun or node")
        page = _FAKE_DOM_JS + r"""
run(input.open);
if (input.drawer) run(input.drawer);
const links = (id) => {
  const root = document.getElementById(id);
  return root ? root.walk().filter((e) => e.tagName === 'A')
    .map((a) => [a.textContent, a.href, a.target, a.rel]) : null;
};
process.stdout.write(JSON.stringify({dialog: links('openclaw-status-modal-root'),
                                     drawer: links('openclaw-drawer-root')}));
"""
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(page)
        try:
            out = subprocess.run([runtime, fh.name], input=json.dumps({"now": 1, "open": opening, "drawer": drawer}),
                                 capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(fh.name)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    async def test_the_dialog_and_drawer_link_to_their_sessions(self):
        url = "https://claw.example.com/chat/main/56b98d09f3b74e4fb2bb688ce63bfa53"
        child = "https://claw.example.com/chat/main/0f1e2d3c4b5a69788796a5b4c3d2e1f0"
        seen = self._links(await self._open_code(self.BASE),
                           _render_drawer_open_js({"id": "agent:main:subagent:x", "title": "Child",
                                                   "status": "running", "controlUiUrl": child}))
        self.assertEqual(seen["dialog"][0], ["Open in OpenClaw ↗", url, "_blank", "noopener noreferrer"])
        self.assertEqual(seen["drawer"], [["Open in OpenClaw ↗", child, "_blank", "noopener noreferrer"]])

    async def test_no_link_while_the_valve_is_empty(self):
        seen = self._links(await self._open_code(""),
                           _render_drawer_open_js({"id": "agent:main:subagent:x", "title": "Child",
                                                   "status": "running", "controlUiUrl": None}))
        self.assertEqual(seen["dialog"], [])
        self.assertEqual(seen["drawer"], [])
