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
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_helpers import action_gateway, RecordingEmitter, run_page  # noqa: E402
from _test_stubs import install_stubs  # noqa: E402

install_stubs()

from openclaw_status_action import (  # noqa: E402
    Action,
    _control_ui_session_url,
    _drawer_run_items,
    _get_action_connection,
    _owui_session_key,
    _render_context_fill_js,
    _render_drawer_fill_js,
    _render_drawer_open_js,
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

    def test_traffic_light_colours_are_inline_not_tailwind_classes(self):
        """'bg-rose-500' rendered as an invisible bar: OWUI's Tailwind build
        ships only the utilities its own source uses. The colours are
        inline styles (DialogSectionTests checks the values)."""
        js = _render_modal_open_js({"chatId": None, "messageId": None, "sessionId": None, "owuiModel": None})
        executable = "\n".join(l for l in js.splitlines() if not l.strip().startswith("//"))
        for leftover in ("bg-rose", "bg-amber", "bg-emerald", "text-rose"):
            self.assertNotIn(leftover, executable)


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
  get parentElement() { return this.parent; }
  set innerHTML(v) { this.children = []; this.text = ''; }
  set textContent(v) { this.children = []; this.text = String(v); }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(''); }
  addEventListener() {}
  setAttribute() {}
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
  createTextNode: (t) => { const e = new El('#text'); e.text = String(t); return e; },
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


_DRAWER_CARDS_PAGE_JS = _FAKE_DOM_JS + r"""
run(input.open);
run(input.drawer);
const cards = () => document.getElementById('openclaw-drawer-stream').walk()
  .filter((e) => 'openclawCard' in e.dataset);
const nameOf = (card) => card.walk().find((e) => 'openclawCardName' in e.dataset).textContent;
const seen = [];
for (const step of input.steps) {
  if (step.fill) run(step.fill);
  if (step.toggle) {
    // What a tap on a card's row does.
    cards().find((c) => nameOf(c) === step.toggle).walk().find((e) => 'openclawCardButton' in e.dataset).onclick();
  }
  seen.push(cards().map((c) => [nameOf(c), c.dataset.open === 'true']));
}
console.log(JSON.stringify(seen));
"""


_SECTIONS_PAGE_JS = _FAKE_DOM_JS + r"""
const fetches = [];
global.localStorage = { getItem: (k) => (k === 'token' ? 'tok-1' : null) };
global.fetch = (url, opts) => {
  fetches.push({ url, auth: opts.headers.Authorization, body: JSON.parse(opts.body) });
  return Promise.resolve({});
};
run(input.open);
input.fills.forEach(run);
const el = (id) => document.getElementById(id);
const text = (id) => (el(id) ? el(id).textContent : null);
const ctx = el('openclaw-status-section-context');
const compact = ctx ? ctx.walk().find((e) => e.tagName === 'BUTTON' && e.textContent === 'Compact') : null;
if (input.click && compact) compact.onclick();
console.log(JSON.stringify({
  sections: ['context', 'limits', 'subagents'].map((n) => !!el('openclaw-status-section-' + n)),
  context: text('openclaw-status-section-context'),
  limits: text('openclaw-status-section-limits'),
  wrap: text('openclaw-status-sections'),
  bars: ctx ? ctx.walk().filter((e) => e.style.width).map((e) => e.style.backgroundColor) : [],
  compact: !!compact,
  fetches,
}));
"""

FULL_IDENTITY = {"chatId": "c1", "messageId": "m1", "sessionId": "s1", "owuiModel": "x.y"}


class DialogSectionTests(unittest.TestCase):
    """The Status dialog's Context, Rate Limits and error fills, run on a
    fake page: what a reader sees, not what the JS source contains."""

    def _page(self, fills, identity=FULL_IDENTITY, click=False, tz="UTC"):
        payload = {"now": 1, "open": _render_modal_open_js(identity), "fills": fills, "click": click}
        return run_page(_SECTIONS_PAGE_JS, payload, tz=tz)

    @staticmethod
    def _context(pct):
        return _render_context_fill_js({
            "provider": "anthropic", "model": "claude-opus-5-5",
            "context": {"usedTokens": "34k", "totalTokens": "1.0m", "pct": pct},
            "goal": None, "source": "pipe", "fetchedAt": 1, "error": None})

    @staticmethod
    def _limits(windows, session_active=False):
        return _render_limits_fill_js({"provider": "anthropic", "windows": windows,
                                       "sessionActive": session_active, "source": "pipe",
                                       "fetchedAt": 1, "error": None})

    def test_the_dialog_opens_with_three_sections(self):
        self.assertEqual(self._page([])["sections"], [True, True, True])

    def test_the_context_bar_colour_follows_usage(self):
        for pct, colour in ((95, "#f43f5e"), (70, "#f59e0b"), (10, "#10b981")):
            with self.subTest(pct=pct):
                seen = self._page([self._context(pct)])
                self.assertEqual(seen["bars"], [colour])
                self.assertIn("34k / 1.0m tokens", seen["context"])

    def test_no_windows_shows_a_note_and_why(self):
        seen = self._page([self._limits([], session_active=True)])
        self.assertIn("Rate Limits", seen["limits"])
        self.assertIn("No rate-limit data available right now (a response is in progress).", seen["limits"])

    def test_windows_show_without_the_note(self):
        seen = self._page([self._limits([{"label": "5h", "usedPercent": 50, "resetIn": None,
                                           "resetAtMs": None}])])
        self.assertIn("5h50% used", seen["limits"])
        self.assertNotIn("No rate-limit data", seen["limits"])

    def test_a_reset_shows_relative_and_the_readers_clock(self):
        reset_at = int(datetime.datetime(2026, 10, 16, 9, 30, tzinfo=datetime.timezone.utc).timestamp() * 1000)
        window = {"label": "5h", "usedPercent": 50, "resetIn": "1h21m", "resetAtMs": reset_at}
        self.assertIn("resets in 1h21m · 09:30", self._page([self._limits([window])])["limits"])
        self.assertIn("resets in 1h21m · 12:30",
                      self._page([self._limits([window])], tz="Asia/Jerusalem")["limits"])

    def test_compact_needs_the_whole_identity(self):
        self.assertTrue(self._page([self._context(10)])["compact"])
        for missing in ("chatId", "messageId", "sessionId"):
            with self.subTest(missing=missing):
                self.assertFalse(self._page([self._context(10)], {**FULL_IDENTITY, missing: None})["compact"])

    def test_compact_posts_back_to_this_action(self):
        seen = self._page([self._context(10)], click=True)
        self.assertEqual(seen["fetches"], [{
            "url": "/api/chat/actions/openclaw_status_action", "auth": "Bearer tok-1",
            "body": {"model": "x.y", "chat_id": "c1", "id": "m1", "session_id": "s1", "mode": "compact"}}])

    def test_a_dialog_error_replaces_every_section(self):
        seen = self._page([self._context(10), _render_sections_error_js("Gateway unreachable")])
        self.assertEqual(seen["wrap"], "Gateway unreachable")
        self.assertEqual(seen["sections"], [False, False, False])


class DrawerToolCardsStayOpenTests(unittest.TestCase):
    """ELI-133 (ELI-110 screenshots, run 38636471099): while a sub-agent
    runs, the drawer refills every 2.5 s, and each refill rebuilt every card
    closed, so a card the user opened snapped shut. A card keeps its open or
    closed state across refills, keyed by its tool call id."""

    TASK = {"id": "t1", "title": "Child", "status": "running", "childSessionKey": "agent:main:subagent:c",
            "startedAt": 1, "endedAt": None, "progressSummary": None, "terminalSummary": None, "error": None}

    @staticmethod
    def _call(cid, name):
        return {"kind": "tool", "id": cid, "name": name, "args": "{}", "result": "ok"}

    def _fill(self, calls):
        return _render_drawer_fill_js({"childSessionKey": self.TASK["childSessionKey"], "items": calls,
                                       "task": {"status": "running"}, "source": "pipe", "fetchedAt": 1,
                                       "error": None})

    def _page(self, steps):
        identity = {"chatId": "c", "messageId": "m", "sessionId": "s", "owuiModel": "x"}
        payload = {"now": 1, "open": _render_modal_open_js(identity),
                   "drawer": _render_drawer_open_js(self.TASK), "steps": steps}
        return run_page(_DRAWER_CARDS_PAGE_JS, payload)

    def test_an_opened_card_stays_open_across_a_refill(self):
        a, b, c = self._call("call-a", "read"), self._call("call-b", "exec"), self._call("call-c", "write")
        seen = self._page([{"fill": self._fill([a, b])}, {"toggle": "exec"},
                           {"fill": self._fill([a, b, c])}])
        self.assertEqual(seen[1], [["read", False], ["exec", True]])
        self.assertEqual(seen[2], [["read", False], ["exec", True], ["write", False]])

    def test_a_closed_card_stays_closed(self):
        a, b = self._call("call-a", "read"), self._call("call-b", "exec")
        seen = self._page([{"fill": self._fill([a, b])}, {"toggle": "read"}, {"toggle": "read"},
                           {"fill": self._fill([a, b])}])
        self.assertEqual(seen[3], [["read", False], ["exec", False]])

    def test_the_state_follows_the_call_not_its_position(self):
        """When an older step drops out of the read, the open card is still
        the same call, now one place up."""
        a, b, c = self._call("call-a", "read"), self._call("call-b", "exec"), self._call("call-c", "write")
        seen = self._page([{"fill": self._fill([a, b])}, {"toggle": "exec"},
                           {"fill": self._fill([b, c])}])
        self.assertEqual(seen[2], [["exec", True], ["write", False]])


class DrawerRunShowsEachResultOnceTests(unittest.TestCase):
    """ELI-128: a tool's output shows once, in its card. In OpenClaw's own
    shape (any runtime but claude-cli) the result is a `toolResult` message
    of its own; it is never a turn of the run."""

    def test_the_gateways_own_shape(self):
        items = _drawer_run_items([
            {"role": "user", "content": "Read the staging deploy log."},
            {"role": "assistant", "content": [
                {"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "README.md"}}]},
            {"role": "toolResult", "toolCallId": "call_1", "toolName": "read",
             "content": [{"type": "text", "text": "# Forecast"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "The staging log is clean."}]},
        ])
        self.assertEqual([(i["kind"], i.get("text") or i.get("result")) for i in items], [
            ("user", "Read the staging deploy log."), ("tool", "# Forecast"),
            ("text", "The staging log is clean.")])

    def test_claude_cli_shape(self):
        items = _drawer_run_items([
            {"role": "user", "content": "Read the staging deploy log."},
            {"role": "assistant", "content": [
                {"type": "toolcall", "id": "t1", "name": "read", "arguments": {"path": "README.md"}},
                {"type": "tool_result", "tool_use_id": "t1", "content": "# Forecast"},
                {"type": "text", "text": "The staging log is clean."}]},
        ])
        self.assertEqual([(i["kind"], i.get("text") or i.get("result")) for i in items], [
            ("user", "Read the staging deploy log."), ("tool", "# Forecast"),
            ("text", "The staging log is clean.")])


class ElapsedTickTests(unittest.TestCase):
    """ELI-120: a running sub-agent's elapsed time counts up while the Status
    dialog stays open, instead of freezing at whatever it was when the
    dialog opened ("1s" for a child spawned a moment earlier)."""

    NOW = 1_783_790_100_000

    def _page(self, steps, tasks):
        identity = {"chatId": "c", "messageId": "m", "sessionId": "s", "owuiModel": "x"}
        fill = _render_subagents_fill_js({"tasks": tasks, "source": "pipe", "fetchedAt": "12:00:00", "error": None})
        payload = {"now": self.NOW, "open": _render_modal_open_js(identity), "fills": [fill], "steps": steps}
        return run_page(_DIALOG_PAGE_JS, payload)

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


class _RecordingOwuiModule(types.ModuleType):
    """An Open WebUI module that records every name read from it. Nothing is
    stashed on it (`_openclaw_*`): no Pipe connection is in the process."""

    def __init__(self, name, touched):
        super().__init__(name)
        self._touched = touched

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        self._touched.append((self.__name__, attr))
        if attr.startswith("_openclaw_"):
            raise AttributeError(attr)
        return mock.MagicMock(name=f"{self.__name__}.{attr}")


class _ScriptedSocket:
    """A Gateway socket that plays a list of frames. A request is answered at
    once (from `answers`, by method); `None` in the script drops the socket.
    When the script of the last socket is done, `on_end` runs and the
    connection stops."""

    def __init__(self, conn, frames, answers, sent, on_end=None):
        self.conn, self.frames, self.answers, self.sent = conn, list(frames), answers, sent
        self.on_end = on_end

    async def send(self, text):
        req = json.loads(text)
        self.sent.append(req["method"])
        self.conn._pending_reqs[req["id"]].set_result(self.answers.get(req["method"], {}))

    async def recv(self):
        import openclaw_status_action as mod

        if self.frames:
            frame = self.frames.pop(0)
            if frame is None:
                raise mod.websockets.exceptions.ConnectionClosed(None, None)
            return json.dumps(frame)
        await self.on_end()
        self.conn._stopped = True
        return json.dumps({"type": "event", "event": "tick", "payload": {}})


class ActionOwnConnectionWritesNothingTests(unittest.IsolatedAsyncioTestCase):
    """ELI-149: the Action's fallback connection is the only place the Action
    runs gateway.py's receive loop. Fed every kind of frame a Gateway sends it,
    with no Pipe connection in the process, it raises nothing and writes
    nothing: no turn, no chat.send, no Open WebUI write."""

    USER = "2d206eab-acac-4945-bd2f-7e8b6365de8c"
    CHAT = "0abe9bde-9b86-4ef3-8a46-a3a98ed9410a"
    READ_ONLY = {"sessions.subscribe", "sessions.messages.subscribe", "sessions.list",
                 "cron.list", "chat.history"}

    async def test_frames_it_can_meet(self):
        import openclaw_status_action as mod

        key = _owui_session_key("main", self.USER, self.CHAT)

        def agent(seq, run, **payload):
            return {"type": "event", "event": "agent", "seq": seq,
                    "payload": {"runId": run, "sessionKey": key, **payload}}

        def event(seq, name, **payload):
            return {"type": "event", "event": name, "seq": seq, "payload": payload}

        first = [
            event(1, "tick"),
            agent(2, "hb-run", isHeartbeat=True, stream="lifecycle", data={"phase": "start"}),
            agent(3, "run-1", stream="assistant", data={"text": "hi", "delta": "hi"}),
            event(4, "session.tool", runId="run-1", sessionKey=key, stream="tool",
                  data={"phase": "start", "name": "Read", "toolCallId": "t1", "args": {}}),
            # seq 5 dropped: a frame gap, resynced
            event(6, "chat", runId="run-1", sessionKey=key, state="final",
                  message={"role": "assistant", "content": [{"type": "text", "text": "hi"}]}),
            event(7, "sessions.changed", sessionKey=key, hasActiveRun=False),
            event(8, "shutdown", restartExpectedMs=5000),
            None,  # the socket drops; the Gateway comes back restarted
        ]
        second = [
            event(1, "sessions.changed", sessionKey=key, hasActiveRun=True),
            agent(2, "run-2", stream="lifecycle", data={"phase": "start"}),
            event(3, "chat", runId="run-2", sessionKey=key, state="final",
                  message={"role": "assistant", "content": [{"type": "text", "text": "ok"}]}),
            event(4, "sessions.changed", sessionKey=key, hasActiveRun=False),
        ]
        answers = {
            "sessions.subscribe": {"list": {"sessions": [
                {"key": key, "hasActiveRun": True, "activeRunIds": ["run-1"]}]}},
            "sessions.list": {"sessions": [{"key": key, "hasActiveRun": False}]},
            "cron.list": {"jobs": []},
        }
        sent, logs, tasks, touched = [], [], [], []
        sockets = [first, second]
        boots = iter(["boot-a", "boot-b"])

        async def settle_background():
            while not all(t.done() for t in tasks):
                await asyncio.gather(*tasks, return_exceptions=True)

        async def fake_connect(conn):
            # What a handshake leaves behind (`_connect_and_start`).
            frames = sockets.pop(0)
            conn._ws = _ScriptedSocket(conn, frames, answers, sent,
                                       on_end=None if sockets else settle_background)
            conn._frame_seq = None
            conn.connection_epoch += 1
            conn._note_gateway_boot(next(boots))
            conn._subscriptions_stale = not conn.requests_only

        def spawn(coro):
            task = asyncio.ensure_future(coro)
            tasks.append(task)
            return task

        owui = {name: _RecordingOwuiModule(name, touched) for name in (
            "open_webui", "open_webui.socket", "open_webui.socket.main", "open_webui.main",
            "open_webui.tasks", "open_webui.models", "open_webui.models.chats",
            "open_webui.models.chat_messages", "open_webui.models.users",
            "open_webui.models.functions", "open_webui.internal", "open_webui.internal.db")}
        for name, module in owui.items():
            parent, _, leaf = name.rpartition(".")
            if parent:
                owui[parent].__dict__[leaf] = module
        mod._action_fallback_connection = None
        try:
            with mock.patch.dict(sys.modules, owui), \
                 mock.patch.object(mod._GatewayConnection, "_connect_and_start", fake_connect), \
                 mock.patch.object(mod._GatewayConnection, "_reconnect", fake_connect), \
                 mock.patch.object(mod, "spawn_background", spawn), \
                 mock.patch.object(mod, "pipe_log", logs.append):
                conn, source = await _get_action_connection(
                    lambda: types.SimpleNamespace(AGENT_ID="main"))
                await conn._event_loop_task
                await settle_background()
        finally:
            mod._action_fallback_connection = None

        self.assertEqual(source, "action-own")
        self.assertFalse(conn.writes_turns)
        self.assertFalse(sockets, "both sockets played")
        self.assertEqual(conn.gateway_restarts, 1)
        self.assertEqual([t.exception() for t in tasks if t.exception()], [])
        self.assertEqual([line for line in logs
                          if "error" in line.lower() or "failed" in line.lower()], [])
        # Each path was reached: both hellos, the frame gap, the restart, the
        # settle's subscription.
        self.assertEqual(sent.count("sessions.subscribe"), 2)
        self.assertIn("sessions.messages.subscribe", sent)
        for reached in ("frame seq gap", "WS disconnected", "Gateway restarted"):
            self.assertTrue(any(reached in line for line in logs), (reached, logs))
        self.assertEqual(sorted(set(sent) - self.READ_ONLY), [])
        # Only this function's own stash was looked up: no Open WebUI API.
        self.assertEqual([t for t in touched if not (
            t[0] == "open_webui.socket.main" and t[1].startswith("_openclaw_"))], [])
        self.assertEqual(dict(getattr(conn, "_turn_pumps", {})), {})
        self.assertEqual(dict(getattr(conn, "_turn_writers", {})), {})


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
        emitter = RecordingEmitter(emitted)

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
        parent = "agent:main:openwebui-user-1-chat-1"

        emitter = RecordingEmitter(emitted)

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
                # The open reads every session (ELI-152); a refill follows
                # the chat's current children.
                if "spawnedBy" in params:
                    self.assertEqual(params["spawnedBy"], parent)
                return {"sessions": [
                    {"key": "agent:main:subagent:a", "status": "running", "spawnedBy": parent},
                    {"key": "agent:main:subagent:b", "status": "queued", "spawnedBy": parent},
                    {"key": "agent:main:subagent:c", "status": "done", "spawnedBy": parent},
                    {"key": "agent:main:subagent:d", "status": "running", "spawnedBy": "another-session"},
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
        # Every child of this chat, the ended one included; another chat's not.
        self.assertEqual(subagents_code.count('"status": "running"') + subagents_code.count('"status": "queued"'), 2)
        self.assertIn('"status": "completed"', subagents_code)
        self.assertNotIn("agent:main:subagent:d", subagents_code)

    async def test_connect_failure_shows_dialog_wide_error(self):
        action = Action()
        emitted = []
        emitter = RecordingEmitter(emitted)

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
        emitter = RecordingEmitter(emitted)

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

        with action_gateway(fake_conn):
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
        emitter = RecordingEmitter(emitted)

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
        with action_gateway(fake_conn):
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
        emitter = RecordingEmitter(emitted)

        fake_conn = mock.Mock()
        fake_conn.active_run_id_for_session = mock.Mock(return_value="some-active-run")
        fake_conn.send_request = mock.AsyncMock()  # must never be called

        with action_gateway(fake_conn):
            result = await action._run_compact(self.COMPACT_BODY, {"id": "u1"}, emitter)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["detail"], "session busy")
        fake_conn.send_request.assert_not_awaited()
        self.assertEqual(len(emitted), 1)
        self.assertIn("currently in progress", emitted[0]["data"]["code"])

    async def test_success_shows_compacting_then_refreshes_all_sections(self):
        action = Action()
        emitted = []
        emitter = RecordingEmitter(emitted)

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

        with action_gateway(fake_conn), \
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
        emitter = RecordingEmitter(emitted)

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

        with action_gateway(fake_conn), \
             mock.patch("openclaw_status_action.time.time", side_effect=fake_time), \
             mock.patch("openclaw_status_action.asyncio.sleep", new=fake_sleep):
            result = await action._run_compact(self.COMPACT_BODY, {"id": "u1"}, emitter)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["detail"], "timeout")
        codes = _emitted_codes(emitted)
        self.assertIn("timed out", codes[-1])
        self.assertIn("openclaw-status-sections", codes[-1])


class DrawerRunItemsTests(unittest.TestCase):
    """The drawer's run (ELI-155): chat.history read the way the chat's own
    rebuild reads it (`_history_renderer`), as text, user turns and a card
    per tool call with its result."""

    @staticmethod
    def _tools(items):
        return [i for i in items if i["kind"] == "tool"]

    def test_text_and_tool_calls_in_order_with_their_results(self):
        items = _drawer_run_items([
            {"role": "assistant", "content": [
                {"type": "toolcall", "id": "t1", "name": "ToolSearch", "arguments": {"query": "foo"}},
                {"type": "tool_result", "tool_use_id": "t1", "content": "found it", "name": "ToolSearch"},
            ]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "internal reasoning, never shown"},
                {"type": "text", "text": "Here is the answer."},
            ]},
            {"role": "user", "content": "What about this?"},
        ])
        self.assertEqual([i["kind"] for i in items], ["tool", "text", "user"])
        self.assertEqual(items[0]["name"], "ToolSearch")
        self.assertIn("foo", items[0]["args"])
        self.assertEqual(items[0]["result"], "found it")
        self.assertEqual(items[1]["text"], "Here is the answer.")
        self.assertNotIn("internal reasoning", json.dumps(items))
        self.assertEqual(items[2], {"kind": "user", "text": "What about this?"})

    def test_empty_and_missing_content_do_not_crash(self):
        self.assertEqual(_drawer_run_items([{"role": "user", "content": []}, {"role": "user"}]), [])

    def test_a_failed_call_is_marked_as_the_chat_marks_it(self):
        from openclaw_status_action import TOOL_ERROR_MARK
        items = _drawer_run_items([
            {"role": "assistant", "content": [
                {"type": "toolcall", "id": "t1", "name": "Bash", "arguments": {"command": "nope"}},
                {"type": "tool_result", "tool_use_id": "t1", "content": "boom",
                 "is_error": True, "name": "Bash"},
            ]},
        ])
        self.assertEqual(items[0]["name"], f"Bash {TOOL_ERROR_MARK}")
        self.assertTrue(items[0]["result"].startswith(f"{TOOL_ERROR_MARK} tool call failed"))
        self.assertTrue(items[0]["result"].endswith("boom"))

    def test_a_result_shows_its_output_not_the_raw_envelope(self):
        """ELI-125: a tool_result's content can be a list of blocks or the
        agent-tool envelope {content, details}. A card shows the output, the
        chat tool cards' rule (ELI-121)."""
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
                items = _drawer_run_items([
                    {"role": "assistant", "content": [
                        {"type": "toolcall", "id": "t1", "name": "exec", "arguments": {"command": "git log"}},
                        {"type": "tool_result", "tool_use_id": "t1", "content": content},
                    ]},
                ])
                self.assertEqual(items[0]["result"], shown)

    def test_the_gateways_own_tool_shape(self):
        """Outside claude-cli imports, chat.history keeps OpenClaw's own shape
        (seen in the screenshots stack, 2026-10-14): a `toolCall` block in the
        assistant message and the result as a `toolResult` message."""
        from openclaw_status_action import TOOL_ERROR_MARK
        items = _drawer_run_items([
            {"role": "user", "content": "Price out Fly.io."},
            {"role": "assistant", "content": [
                {"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "README.md"}}]},
            {"role": "toolResult", "toolCallId": "call_1", "toolName": "read",
             "content": [{"type": "text", "text": "# Forecast"}, {"type": "text", "text": "A small weather app."}]},
            {"role": "assistant", "content": [
                {"type": "toolCall", "id": "call_2", "name": "exec", "arguments": {"command": "false"}}]},
            {"role": "toolResult", "toolCallId": "call_2", "toolName": "exec", "isError": True,
             "content": [{"type": "text", "text": "exit 1"}]},
        ])
        tools = self._tools(items)
        self.assertEqual([(c["id"], c["name"]) for c in tools], [("call_1", "read"), ("call_2", f"exec {TOOL_ERROR_MARK}")])
        self.assertEqual(tools[0]["result"], "# Forecast\nA small weather app.")
        self.assertIn("README.md", tools[0]["args"])

    def test_a_running_claude_cli_child_has_no_tool_calls_yet(self):
        """Measured against the real Gateway (2026-07-30, again 2026-10-16 on
        2026.9.3): while a claude-cli child runs, chat.history holds only the
        messages it was sent; its run reaches the transcript when it ends."""
        items = _drawer_run_items([
            {"role": "user", "content": "[Subagent Context] You are running as a subagent.\n[Subagent Task]\nDo X."},
            {"role": "user", "content": "[Inter-session message] status?"},
        ])
        self.assertEqual(items, [{"kind": "user", "text": "[Inter-session message] status?"}])

    def test_the_drawer_and_the_chat_rebuild_read_the_same_calls(self):
        """One transcript parser (ELI-147): the drawer's cards are the ones
        the chat's rebuild makes, in both shapes. A recorded DeepSeek run (the
        Gateway's own runtime) and a claude-cli run with a failed call."""
        from openclaw_status_action import _history_renderer
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_fixture_native_history.json")
        with open(path) as f:
            native = json.load(f)["messages"]
        cli = [
            {"role": "assistant", "content": [
                {"type": "toolcall", "id": "toolu_1", "name": "Bash", "arguments": {"command": "tail log"}},
                {"type": "tool_result", "tool_use_id": "toolu_1", "name": "Bash", "content": "boom",
                 "is_error": True}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Found it."}]},
        ]
        for label, history in (("native", native), ("claude-cli", cli)):
            with self.subTest(label):
                cards = [i for i in _history_renderer(history).output if i["type"] == "function_call"]
                self.assertTrue(cards)
                runs = [m for m in history if m.get("role") != "user"]
                self.assertEqual([(c["id"], c["name"]) for c in self._tools(_drawer_run_items(runs))],
                                 [(c["id"], c["name"]) for c in cards])


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
        emitter = RecordingEmitter(emitted)

        result = await action._run_subagent_detail({"mode": "subagent-detail"}, {"id": "u1"}, emitter)
        self.assertEqual(result["status"], "error")
        self.assertIn("taskId", _emitted_codes(emitted)[0])

    async def test_one_request_opens_and_fills_the_drawer_once(self):
        """Opened from here (no `opened`): the drawer, then one fill of its
        header and run. The page asks again while the child runs (ELI-155)."""
        action = Action()
        emitted = []
        emitter = RecordingEmitter(emitted)

        get_calls = {"n": 0}

        async def fake_send_request(method, params, timeout=5):
            if method == "sessions.describe":
                get_calls["n"] += 1
                self.assertEqual(params["key"], self.CHILD)
                return {"session": {"modelProvider": "anthropic", "model": "claude-sonnet-5",
                                     "contextTokens": 1000000, "totalTokens": 5000, "status": "running",
                                     "startedAt": 1783790000000, "endedAt": None}}
            if method == "chat.history":
                self.assertEqual(params["sessionKey"], self.CHILD)
                return {"messages": [{"role": "assistant", "content": [{"type": "text", "text": "working on it"}]}]}
            raise AssertionError(f"unexpected RPC: {method}")

        fake_conn = _drawer_conn(fake_send_request)

        with action_gateway(fake_conn):
            result = await action._run_subagent_detail(self.DETAIL_BODY, {"id": "u1"}, emitter)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(get_calls["n"], 1)
        codes = _emitted_codes(emitted)
        self.assertIn("openDrawer", codes[0])  # initial open
        fills = [c for c in codes if "S.fillDrawer(DATA)" in c]
        self.assertEqual(len(fills), 1)
        self.assertIn('"model": "claude-sonnet-5"', fills[0])
        self.assertIn('{"kind": "text", "text": "working on it"}', fills[0])

        emitted.clear()
        with action_gateway(fake_conn):
            await action._run_subagent_detail({**self.DETAIL_BODY, "opened": True}, {"id": "u1"}, emitter)
        self.assertFalse(any("openDrawer" in c for c in _emitted_codes(emitted)))   # a page refresh


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
        emitter = RecordingEmitter(emitted)

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

        with action_gateway(fake_conn), \
                mock.patch("openclaw_status_action.time.time", return_value=self.FETCHED):
            await _open_dialog(Action().action(
                {"chat_id": "chat-1", "id": "msg-1", "session_id": "sess-1", "model": "x.y"},
                __user__={"id": "user-1"}, __event_emitter__=emitter,
            ), fake_conn)
        codes = _emitted_codes(emitted)
        return codes[0], next(c for c in codes if "openclaw-status-section-context" in c and c != codes[0])

    async def test_updated_is_on_the_readers_clock(self):
        opening, fill = await self._context_fill()
        page = _FAKE_DOM_JS + r"""
run(input.open);
run(input.fill);
const f = document.getElementById('openclaw-status-footer');
process.stdout.write(JSON.stringify(f ? f.textContent : null));
"""
        footer = run_page(page, {"now": 1, "open": opening, "fill": fill}, tz="Asia/Jerusalem")
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
        emitter = RecordingEmitter(emitted)

        fake_conn = mock.AsyncMock()
        fake_conn.send_request = mock.AsyncMock(side_effect=lambda method, params, timeout=5: {
            "sessions.describe": {"session": {}}, "usage.status": {"providers": []},
            "sessions.list": {"sessions": []}}.get(method, {}))

        action = Action()
        action.valves.CONTROL_UI_URL = control_ui_url
        with action_gateway(fake_conn):
            await _open_dialog(action.action(
                {"chat_id": self.CHAT, "id": "msg-1", "session_id": "sess-1", "model": "x.y"},
                __user__={"id": self.USER}, __event_emitter__=emitter,
            ), fake_conn)
        return _emitted_codes(emitted)[0]

    def _links(self, opening, drawer=None):
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
        return run_page(page, {"now": 1, "open": opening, "drawer": drawer})

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


if __name__ == "__main__":
    unittest.main()
