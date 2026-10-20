#!/usr/bin/env python3
"""The sub-agent list and drawer on a phone (ELI-155).

A sub-agent whose task was a long prompt filled the whole drawer: its title
was the task, its status line the child's whole last reply, nothing was
clamped and the header sat outside the scroller (B1-B4, B7). For a claude-cli
child that reply was the run's closer, every message of the run joined (B5).
The drawer stopped refreshing after 15 minutes without a word (B6), kept
polling after it was closed, and a closed drawer's poll filled the next one;
in-process agents had no detail at all (B10).
"""

import asyncio
import json
import unittest
from unittest import mock

import test_status_action_unit  # noqa: F401  (installs the module stubs)
from _test_helpers import action_gateway, RecordingEmitter, run_page
from test_status_action_unit import _FAKE_DOM_JS, _drawer_conn, _emitted_codes

import openclaw_status_action
from openclaw_status_action import (
    Action,
    _render_drawer_fill_js,
    _render_drawer_open_js,
    _render_modal_open_js,
    _render_subagents_fill_js,
    _subagent_row,
)

CHILD = "agent:main:subagent:aaaaaaaa-0000-0000-0000-000000000001"
OTHER = "agent:main:subagent:bbbbbbbb-0000-0000-0000-000000000002"
NOW = 1_783_790_100_000

# Eliav's sub-agent, shortened: a 1,200-character prompt as its task.
LONG_TASK = ("Repo: private GitHub repo example/invest-bot (gh: GH_CONFIG_DIR=/config/gh; the local clone "
             "is STALE with no commits: re-clone into your scratchpad). Workflow "
             ".github/workflows/bench.yml, code in experiments/bench/. ") + "Goal: a head-to-head, net of fees. " * 31
LONG_RESULT = "Now rewriting run.sh and the workflow into a two-phase matrix. Polling. " * 14
IDENTITY = {"chatId": "c", "messageId": "m", "sessionId": "s", "owuiModel": "x"}

# The fake page, plus what these tests need: the viewport width, a recording
# fetch whose promise settles at once, and a token store.
_PAGE_JS = _FAKE_DOM_JS + r"""
global.innerWidth = input.width || 1280;
global.localStorage = { getItem: () => 'tok' };
const fetches = [];
global.fetch = (url, opts) => {
  fetches.push(JSON.parse(opts.body));
  const p = { then(f) { try { f({ ok: true }); } catch (e) {} return p; }, catch() { return p; },
              finally(f) { f(); return p; } };
  return p;
};
const byData = (root, key) => root ? root.walk().filter((e) => key in e.dataset) : [];
const drawer = () => document.getElementById('openclaw-drawer-root');
const tick = (seconds) => { for (let s = 0; s < seconds; s++) { now += 1000; [...timers.values()].forEach((f) => f()); } };
const style = (e) => e ? { whiteSpace: e.style.whiteSpace || null, textOverflow: e.style.textOverflow || null,
                           lineClamp: e.style.webkitLineClamp || null } : null;
run(input.open);
"""


def _page(body: str, payload: dict) -> dict:
    return run_page(_PAGE_JS + body, {"now": NOW, **payload})


def _rows():
    """A labelled child with a 1,200-character task and an unlabelled one."""
    labelled = _subagent_row({"key": CHILD, "label": "invest-bot: freqtrade head-to-head", "status": "done",
                              "startedAt": NOW - 65_000, "endedAt": NOW - 5_000}, LONG_TASK, LONG_RESULT[:240])
    unlabelled = _subagent_row({"key": OTHER, "status": "running", "startedAt": NOW - 30_000,
                                "lastMessagePreview": "Reading the noise table."}, LONG_TASK)
    return labelled, unlabelled


def _section(tasks, agents=()):
    return _render_subagents_fill_js({"tasks": list(tasks), "total": len(tasks), "agents": list(agents),
                                      "agentsTotal": len(agents), "source": "pipe", "fetchedAt": NOW,
                                      "error": None})


class TitleTests(unittest.TestCase):
    """B1: the title is the spawn label, else the task's first sentence."""

    def test_a_labelled_child_is_titled_with_its_label(self):
        labelled, _ = _rows()
        self.assertEqual(labelled["title"], "invest-bot: freqtrade head-to-head")
        self.assertEqual(labelled["task"], LONG_TASK)    # the drawer's Task section

    def test_an_unlabelled_child_gets_its_tasks_first_sentence_cut_to_60(self):
        _, unlabelled = _rows()
        self.assertLessEqual(len(unlabelled["title"]), 60)
        self.assertTrue(unlabelled["title"].startswith("Repo: private GitHub repo example/invest-bot"),
                        unlabelled["title"])
        self.assertTrue(unlabelled["title"].endswith("…"), unlabelled["title"])

    def test_a_short_first_sentence_is_the_title(self):
        row = _subagent_row({"key": CHILD, "status": "running"}, "Price out Fly.io. Then compare it with Render.")
        self.assertEqual(row["title"], "Price out Fly.io.")

    def test_no_task_and_no_label_is_the_key(self):
        self.assertEqual(_subagent_row({"key": CHILD, "status": "running"})["title"], CHILD)


class DialogRowTests(unittest.TestCase):
    """B2, B4, B7: one-line title, a status chip, the time, a 2-line preview."""

    def test_rows_are_one_line_with_a_chip_and_a_clamped_preview(self):
        seen = _page(r"""
run(input.fill);
const section = document.getElementById('openclaw-status-section-subagents');
console.log(JSON.stringify(byData(section, 'openclawRow').map((row) => ({
  title: byData(row, 'openclawTitle').map((e) => [e.textContent, style(e)])[0] || null,
  chip: byData(row, 'openclawChip').map((e) => e.textContent)[0] || null,
  preview: byData(row, 'openclawPreview').map((e) => style(e).lineClamp)[0] || null,
}))));
""", {"open": _render_modal_open_js(IDENTITY), "fill": _section(_rows())})
        one_line = {"whiteSpace": "nowrap", "textOverflow": "ellipsis", "lineClamp": None}
        self.assertEqual(seen[0]["title"], ["invest-bot: freqtrade head-to-head", one_line])
        self.assertEqual(seen[0]["chip"], "Done")
        self.assertEqual(seen[0]["preview"], "2")
        self.assertLessEqual(len(seen[1]["title"][0]), 60)
        self.assertEqual(seen[1]["title"][1], one_line)
        self.assertEqual(seen[1]["chip"], "Running")


def _fill(key, task, items=(), **extra):
    return _render_drawer_fill_js({"childSessionKey": key, "task": task, "items": list(items), "parent": None,
                                   "earlier": None, "note": None, "latest": None, "source": "pipe",
                                   "fetchedAt": NOW, "error": None, **extra})


class DrawerHeaderTests(unittest.TestCase):
    """B2, B3, B4: the header is one title line with the status and time, a
    model and "from" line, and the head of the task, two lines with "Show
    all"; the run has the rest of the screen."""

    def _open(self, width):
        labelled, _ = _rows()
        return _page(r"""
run(input.drawer);
run(input.fill);
const panel = drawer().children[1];
const header = panel.children[0];
const scroller = panel.walk().find((e) => e.style.overflowY === 'auto');
console.log(JSON.stringify({
  header: header.textContent,
  title: byData(header, 'openclawTitle').map(style)[0] || null,
  chip: byData(header, 'openclawChip').map((e) => e.textContent)[0] || null,
  task: byData(header, 'openclawBlockText').map(style)[0] || null,
  scroller: scroller ? { minHeight: scroller.style.minHeight || null } : null,
  width: panel.style.width || null,
  buttons: header.walk().filter((e) => e.tagName === 'BUTTON').map((e) => e.textContent),
  tabs: drawer().walk().filter((e) => e.dataset.tab).length,
}));
""", {"width": width, "open": _render_modal_open_js(IDENTITY),
      "drawer": _render_drawer_open_js({**labelled, "kind": "child", "controlUiUrl": None}),
      "fill": _fill(CHILD, {**labelled, "model": "claude-opus-5-5"},
                    [{"kind": "text", "text": LONG_RESULT}],
                    parent={"title": "Investing upkeep", "chatId": "c"})})

    def test_the_header_holds_the_task_head_and_no_reply(self):
        seen = self._open(1280)
        self.assertIn("re-clone", seen["header"])                  # the task, clamped
        self.assertEqual(seen["task"]["lineClamp"], "2")
        self.assertNotIn("two-phase matrix", seen["header"])       # the reply is the run's
        self.assertIn("invest-bot: freqtrade head-to-head", seen["header"])
        self.assertIn("claude-opus-5-5 · from Investing upkeep", seen["header"])
        self.assertEqual(seen["title"], {"whiteSpace": "nowrap", "textOverflow": "ellipsis", "lineClamp": None})
        self.assertEqual(seen["chip"], "Done")
        self.assertEqual(seen["scroller"], {"minHeight": "0"})
        self.assertEqual(seen["tabs"], 0)

    def test_a_phone_gets_a_full_screen_sheet_with_a_back_button(self):
        phone = self._open(390)
        self.assertEqual(phone["width"], "100%")
        self.assertIn("‹ Back", phone["buttons"])
        self.assertNotIn("×", phone["buttons"])
        wide = self._open(1280)
        self.assertEqual(wide["width"], "min(480px, 92vw)")
        self.assertIn("×", wide["buttons"])

    def test_show_all_on_the_task_survives_a_refill(self):
        labelled, _ = _rows()
        seen = _page(r"""
run(input.drawer);
run(input.fill);
const clamp = () => byData(drawer(), 'openclawBlockText').map((e) => style(e).lineClamp)[0];
const first = clamp();
byData(drawer(), 'openclawMore')[0].onclick();
run(input.fill);
console.log(JSON.stringify({first, after: clamp()}));
""", {"open": _render_modal_open_js(IDENTITY),
      "drawer": _render_drawer_open_js({**labelled, "kind": "child", "controlUiUrl": None}),
      "fill": _fill(CHILD, labelled)})
        self.assertEqual(seen, {"first": "2", "after": "unset"})


# What a filled drawer's run shows: each item's kind and text, and its notes.
_RUN_JS = r"""
const stream = () => document.getElementById('openclaw-drawer-stream');
const shown = () => stream().children.map((e) => {
  if ('openclawUser' in e.dataset) return ['user', e.textContent];
  if ('openclawText' in e.dataset) return ['text', e.textContent];
  if ('openclawCard' in e.dataset) return ['tool', byData(e, 'openclawCardName')[0].textContent];
  if ('openclawNote' in e.dataset) return ['note', e.dataset.openclawNote, e.textContent];
  if ('openclawEarlier' in e.dataset) return ['earlier', e.textContent];
  if ('openclawRunError' in e.dataset) return ['error', e.textContent];
  return ['other', e.textContent];
});
"""


class RunTests(unittest.TestCase):
    """The run below the header: a read-only chat of the sub-agent's turns,
    tool cards and reply, and where it stands."""

    def _shown(self, fills, task=None, width=390):
        _, unlabelled = _rows()
        return _page(_RUN_JS + r"""
run(input.drawer);
const out = [];
for (const f of input.fills) { run(f); out.push(shown()); }
console.log(JSON.stringify(out));
""", {"width": width, "open": _render_modal_open_js(IDENTITY),
      "drawer": _render_drawer_open_js({**(task or unlabelled), "kind": "child", "controlUiUrl": None}),
      "fills": fills})

    def test_turns_cards_and_the_reply_in_order(self):
        _, unlabelled = _rows()
        seen = self._shown([_fill(OTHER, {**unlabelled, "status": "completed"}, [
            {"kind": "text", "text": "Reading the noise table."},
            {"kind": "tool", "id": "c1", "name": "read", "args": '{"path": "noise.csv"}', "result": "a,b"},
            {"kind": "user", "text": "Also check station 4."},
            {"kind": "text", "text": "Station 4 changed from 0.10 to 0.08."}])])
        self.assertEqual(seen[0], [["text", "Reading the noise table."], ["tool", "read"],
                                   ["user", "Also check station 4."],
                                   ["text", "Station 4 changed from 0.10 to 0.08."]])

    def test_a_running_child_says_which_step_it_is_on(self):
        _, unlabelled = _rows()
        call = {"kind": "tool", "id": "c1", "name": "read", "args": "{}", "result": "ok"}
        seen = self._shown([_fill(OTHER, unlabelled, [call]),
                            _fill(OTHER, {**unlabelled, "status": "completed", "endedAt": NOW}, [call])])
        self.assertEqual(seen[0][-1], ["note", "working", "Working… (step 2)"])
        self.assertEqual(seen[1], [["tool", "read"]])

    def test_a_running_claude_cli_child_says_its_calls_come_at_the_end(self):
        _, unlabelled = _rows()
        seen = self._shown([_fill(OTHER, unlabelled, [], note="cli-running", latest="Re-cloning the repo.")])
        self.assertEqual(seen[0], [["note", "working",
                                    "Working… tool calls appear when this run endsRe-cloning the repo."]])

    def test_a_stopped_claude_cli_child_says_its_calls_were_not_saved(self):
        _, unlabelled = _rows()
        seen = self._shown([_fill(OTHER, {**unlabelled, "status": "cancelled"},
                                  [{"kind": "text", "text": "Running the probes."}], note="cli-stopped")])
        self.assertEqual(seen[0][-1][:2], ["note", "stopped"])
        self.assertIn("its tool calls were not saved", seen[0][-1][2])

    def test_a_failed_childs_error_closes_the_run(self):
        labelled, _ = _rows()
        seen = self._shown([_fill(CHILD, {**labelled, "status": "failed", "error": "provider 529"})], labelled)
        self.assertEqual(seen[0], [["error", "provider 529"]])

    def test_text_is_never_html(self):
        _, unlabelled = _rows()
        seen = _page(_RUN_JS + r"""
run(input.drawer);
run(input.fill);
const text = byData(stream(), 'openclawText')[0];
console.log(JSON.stringify({
  tags: text.walk().map((e) => e.tagName),
  strong: text.walk().filter((e) => e.tagName === 'STRONG').map((e) => e.textContent),
  code: text.walk().filter((e) => e.tagName === 'CODE').map((e) => e.textContent),
  links: text.walk().filter((e) => e.tagName === 'A').map((e) => [e.textContent, e.href]),
  all: text.textContent,
}));
""", {"open": _render_modal_open_js(IDENTITY),
      "drawer": _render_drawer_open_js({**unlabelled, "kind": "child", "controlUiUrl": None}),
      "fill": _fill(OTHER, {**unlabelled, "status": "completed"}, [{"kind": "text", "text":
          "Done: **config.toml is missing**, see `settings.py` and [the run](https://example.com/r/1).\n\n"
          "<img src=x onerror=alert(1)> [x](javascript:alert(1))\n\n- one\n- two"}])})
        self.assertNotIn("IMG", seen["tags"])
        self.assertEqual(seen["strong"], ["config.toml is missing"])
        self.assertEqual(seen["code"], ["settings.py"])
        self.assertEqual(seen["links"], [["the run", "https://example.com/r/1"]])
        self.assertIn("<img src=x onerror=alert(1)> [x](javascript:alert(1))", seen["all"])
        self.assertEqual(seen["tags"].count("LI"), 2)

    def test_the_reader_keeps_their_place(self):
        """At the bottom the run follows new steps; scrolled up it stays."""
        _, unlabelled = _rows()
        step = lambda n: [{"kind": "text", "text": f"Step {i}."} for i in range(n)]  # noqa: E731
        seen = _page(r"""
run(input.drawer);
const scroller = document.getElementById('openclaw-drawer-stream').parent;
scroller.clientHeight = 400;
scroller.scrollHeight = 1000;
run(input.fills[0]);
const opened = scroller.scrollTop;                // a first fill shows the newest
scroller.scrollTop = 100;                         // the reader scrolls up
scroller.scrollHeight = 1200;
run(input.fills[1]);
const up = scroller.scrollTop;
scroller.scrollTop = 800;                         // back at the bottom
run(input.fills[2]);
console.log(JSON.stringify({opened, up, bottom: scroller.scrollTop}));
""", {"open": _render_modal_open_js(IDENTITY),
      "drawer": _render_drawer_open_js({**unlabelled, "kind": "child", "controlUiUrl": None}),
      "fills": [_fill(OTHER, unlabelled, step(n)) for n in (3, 4, 5)]})
        self.assertEqual(seen, {"opened": 1000, "up": 100, "bottom": 1200})

    def test_earlier_steps_left_out_can_be_shown(self):
        _, unlabelled = _rows()
        seen = _page(_RUN_JS + r"""
run(input.drawer);
run(input.fill);
const before = shown()[0];
const asked = fetches.length;
byData(stream(), 'openclawShowEarlier')[0].onclick();
console.log(JSON.stringify({before, request: fetches.slice(asked)}));
""", {"open": _render_modal_open_js(IDENTITY),
      "drawer": _render_drawer_open_js({**unlabelled, "kind": "child", "controlUiUrl": None, "server": True}),
      "fill": _fill(OTHER, {**unlabelled, "status": "completed"}, [{"kind": "text", "text": "Last."}],
                    earlier={"count": 39, "more": False})})
        self.assertEqual(seen["before"], ["earlier", "39 earlier steps not shownShow earlier steps"])
        self.assertEqual(len(seen["request"]), 1)
        self.assertEqual(seen["request"][0]["mode"], "subagent-detail")
        self.assertTrue(seen["request"][0]["earlier"])


class FinalReplyTests(unittest.IsolatedAsyncioTestCase):
    """B5: the run ends with the child's reply, once. A claude-cli run ends
    with the Gateway's closer (`cli-assistant:<run>`), which repeats every
    message of the run joined: the progress log."""

    async def _items(self, messages):
        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"key": CHILD, "status": "done", "startedAt": 1, "endedAt": 2}}
            if method == "chat.history":
                return {"messages": messages}
            raise AssertionError(f"unexpected RPC: {method}")

        emitted = []
        emitter = RecordingEmitter(emitted)

        with action_gateway(_drawer_conn(send_request)):
            await Action()._run_subagent_detail({"mode": "subagent-detail", "taskId": CHILD}, {"id": "u"}, emitter)
        code = [c for c in _emitted_codes(emitted) if "S.fillDrawer(DATA)" in c][-1]
        return json.loads(code.split("const DATA = ", 1)[1].split(";\n", 1)[0])["items"]

    async def test_a_claude_cli_run_ends_with_its_last_turn_not_the_closer(self):
        report = "The baseline won out-of-sample: +48.2%."
        items = await self._items([
            {"role": "assistant", "stopReason": "tool_use",
             "content": [{"type": "toolcall", "id": "t1", "name": "Bash", "arguments": {}}]},
            {"role": "assistant", "stopReason": "end_turn", "content": [{"type": "text", "text": report}]},
            {"role": "assistant", "stopReason": "stop", "idempotencyKey": "cli-assistant:run-1",
             "__openclaw": {"idempotencyKey": "cli-assistant:run-1"},
             "content": [{"type": "text", "text": "Now rewriting run.sh.\n\nPolling.\n\n" + report}]},
        ])
        self.assertEqual([(i["kind"], i.get("text") or i.get("name")) for i in items],
                         [("tool", "Bash"), ("text", report)])

    async def test_the_gateways_own_runtime_ends_with_its_last_text(self):
        items = await self._items([
            {"role": "assistant", "content": [
                {"type": "text", "text": "Reading the README."},
                {"type": "toolCall", "id": "c1", "name": "read", "arguments": {}},
                {"type": "text", "text": "Fly.io: about $3 a month."}]},
        ])
        self.assertEqual([i["kind"] for i in items], ["text", "tool", "text"])
        self.assertEqual(items[-1]["text"], "Fly.io: about $3 a month.")


class PollTests(unittest.IsolatedAsyncioTestCase):
    """B6, B10: the open drawer refreshes until its sub-agent ends, with no
    15-minute stop; a closed drawer stops; a stale fill never paints another
    sub-agent's drawer."""

    async def test_one_request_fills_once_and_returns(self):
        """The poll is the page's: a request no longer holds open for the
        child's run, so nothing keeps polling for a closed drawer."""
        describes = []

        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                describes.append(params["key"])
                return {"session": {"key": CHILD, "status": "running", "startedAt": 1}}
            if method == "chat.history":
                return {"messages": []}
            raise AssertionError(f"unexpected RPC: {method}")

        async def emitter(_evt):
            return None

        with action_gateway(_drawer_conn(send_request)):
            result = await asyncio.wait_for(Action()._run_subagent_detail(
                {"mode": "subagent-detail", "taskId": CHILD}, {"id": "u"}, emitter), 5)  # bounds the test only
        self.assertEqual(result["status"], "ok")
        self.assertEqual(describes, [CHILD])

    def _poll_page(self, steps_js):
        _, unlabelled = _rows()
        return _page(r"""
run(input.fill);
const refreshes = () => fetches.filter((b) => b.mode === 'subagent-detail').length;
const row = byData(document.getElementById('openclaw-status-section-subagents'), 'openclawRow')[0];
const base = timers.size;    // the dialog's own elapsed ticker
row.onclick();
""" + steps_js, {"open": _render_modal_open_js(IDENTITY), "fill": _section([unlabelled]),
                 "done": _fill(OTHER, {**unlabelled, "status": "completed", "endedAt": NOW})})

    def test_the_drawer_refreshes_past_15_minutes_until_the_child_ends(self):
        seen = self._poll_page(r"""
tick(15 * 60);
const at15 = refreshes();
tick(5 * 60);
const at20 = refreshes();
run(input.done);
tick(60);
console.log(JSON.stringify({at15, at20, after: refreshes(), timers: timers.size - base}));
""")
        self.assertGreater(seen["at15"], 300)
        self.assertGreater(seen["at20"], seen["at15"] + 100)
        self.assertLessEqual(seen["after"], seen["at20"] + 1)
        self.assertEqual(seen["timers"], 0)

    def test_closing_the_drawer_stops_its_refresh(self):
        seen = self._poll_page(r"""
tick(10);
const before = refreshes();
const close = drawer().walk().find((e) => e.tagName === 'BUTTON' && (e.textContent === '×' || e.textContent === '‹ Back'));
close.onclick();
tick(60);
console.log(JSON.stringify({before, after: refreshes(), open: !!drawer(), timers: timers.size - base}));
""")
        self.assertGreater(seen["before"], 1)
        self.assertEqual(seen["after"], seen["before"])
        self.assertFalse(seen["open"])
        self.assertEqual(seen["timers"], 0)

    def test_a_fill_for_another_sub_agent_leaves_the_open_drawer_alone(self):
        labelled, unlabelled = _rows()
        seen = _page(r"""
run(input.drawer);
run(input.stale);
console.log(JSON.stringify({run: document.getElementById('openclaw-drawer-stream').textContent,
                            chip: byData(drawer(), 'openclawChip').map((e) => e.textContent)}));
""", {"open": _render_modal_open_js(IDENTITY),
      "drawer": _render_drawer_open_js({**unlabelled, "kind": "child", "controlUiUrl": None}),
      "stale": _fill(CHILD, labelled, [{"kind": "text", "text": LONG_RESULT}])})
        self.assertEqual(seen["chip"], ["Running"])
        self.assertNotIn("two-phase matrix", seen["run"])


async def _drawer_fill(send_request, body=None, user=None):
    """Run one drawer request against a fake Gateway; returns its fill's DATA
    and the RPCs it made."""
    emitted = []
    emitter = RecordingEmitter(emitted)

    conn = _drawer_conn(send_request)
    with action_gateway(conn):
        await Action()._run_subagent_detail({"mode": "subagent-detail", "taskId": CHILD, "opened": True,
                                             **(body or {})}, user or {"id": "u"}, emitter)
    code = [c for c in _emitted_codes(emitted) if "S.fillDrawer(DATA)" in c][-1]
    return (json.loads(code.split("const DATA = ", 1)[1].split(";\n", 1)[0]),
            [(c.args[0], c.args[1]) for c in conn.send_request.call_args_list])


class RunReadTests(unittest.IsolatedAsyncioTestCase):
    """How much of the run one read carries, and what a claude-cli child's
    run can and cannot show."""

    async def test_a_run_over_the_byte_budget_keeps_its_newest_steps(self):
        big = "x" * 7000
        history = []
        for i in range(80):
            history.append({"role": "assistant", "content": [
                {"type": "text", "text": f"Step {i}."},
                {"type": "toolCall", "id": f"call_{i}", "name": "read", "arguments": {}}]})
            history.append({"role": "toolResult", "toolCallId": f"call_{i}", "toolName": "read",
                            "content": [{"type": "text", "text": big}]})
        history.append({"role": "assistant", "content": [{"type": "text", "text": "Step 80."}]})

        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"key": CHILD, "status": "done", "startedAt": 1, "endedAt": 2}}
            if method == "chat.history":
                return {"messages": history, "totalMessages": 80, "hasMore": False}
            raise AssertionError(method)

        data, _rpcs = await _drawer_fill(send_request)
        self.assertLess(len(json.dumps(data["items"])), openclaw_status_action._DRAWER_RUN_BYTES)
        self.assertEqual(data["items"][-1]["text"], "Step 80.")
        self.assertEqual(data["earlier"], {"count": 161 - len(data["items"]), "more": False})
        self.assertGreater(data["earlier"]["count"], 0)

    def test_one_step_over_the_budget_keeps_its_end(self):
        items, dropped = openclaw_status_action._cap_run_items(
            [{"kind": "text", "text": "a" * 10}, {"kind": "text", "text": "b" * 5000 + "END"}], 1000)
        self.assertEqual(dropped, 1)
        self.assertTrue(items[0]["text"].startswith("…"))
        self.assertTrue(items[0]["text"].endswith("END"))
        self.assertLess(len(json.dumps(items)), 1000)

    async def test_show_earlier_steps_reads_every_page(self):
        def step(text):
            return [{"role": "user", "content": "Go on."},
                    {"role": "assistant", "content": [{"type": "text", "text": text}]}]
        pages = {0: (step("Newest."), 1200), 1000: (step("Oldest."), None)}

        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"key": CHILD, "status": "done", "startedAt": 1, "endedAt": 2}}
            if method == "chat.history":
                msgs, nxt = pages[params.get("offset", 0)]
                return {"messages": msgs, "hasMore": nxt is not None, "nextOffset": 1000 if nxt else None}
            raise AssertionError(method)

        first, _ = await _drawer_fill(send_request)
        self.assertEqual([i["text"] for i in first["items"]], ["Go on.", "Newest."])
        self.assertEqual(first["earlier"], {"count": 0, "more": True})
        everything, rpcs = await _drawer_fill(send_request, {"earlier": True})
        self.assertEqual([i["text"] for i in everything["items"]], ["Go on.", "Oldest.", "Go on.", "Newest."])
        self.assertIsNone(everything["earlier"])
        self.assertEqual([p.get("offset") for m, p in rpcs if m == "chat.history"], [None, 1000])

    async def test_a_running_claude_cli_child_shows_its_latest_line(self):
        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"key": CHILD, "status": "running", "startedAt": 1,
                                    "agentRuntime": {"id": "claude-cli"}}}
            if method == "chat.history":
                return {"messages": [{"role": "user", "content": "[Subagent Context] x\n[Subagent Task]\nDo X."}]}
            if method == "sessions.list":
                return {"sessions": [{"key": CHILD, "lastMessagePreview": "Re-cloning the repo."}]}
            raise AssertionError(method)

        data, _ = await _drawer_fill(send_request)
        self.assertEqual((data["note"], data["latest"], data["items"]), ("cli-running", "Re-cloning the repo.", []))

    async def test_a_stopped_claude_cli_child_says_its_calls_were_lost(self):
        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"key": CHILD, "status": "killed", "startedAt": 1, "endedAt": 2,
                                    "agentRuntime": {"id": "claude-cli"}}}
            if method == "chat.history":
                return {"messages": [{"role": "assistant", "content": [{"type": "text", "text": "Running."}]}]}
            raise AssertionError(method)

        data, _ = await _drawer_fill(send_request)
        self.assertEqual((data["task"]["status"], data["note"]), ("cancelled", "cli-stopped"))

    async def test_a_native_child_needs_no_note(self):
        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"key": CHILD, "status": "killed", "startedAt": 1, "endedAt": 2,
                                    "agentRuntime": {"id": "openclaw"}}}
            if method == "chat.history":
                return {"messages": []}
            raise AssertionError(method)

        data, _ = await _drawer_fill(send_request)
        self.assertIsNone(data["note"])

    async def test_the_first_fill_names_where_it_came_from(self):
        chat = "11111111-2222-3333-4444-555555555555"
        user = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        parent_child = "agent:main:subagent:cccccccc-0000-0000-0000-000000000003"

        def gateway(spawned_by):
            async def send_request(method, params, timeout=5):
                if method == "sessions.describe" and params["key"] == CHILD:
                    return {"session": {"key": CHILD, "status": "done", "startedAt": 1, "endedAt": 2,
                                        "spawnedBy": spawned_by}}
                if method == "sessions.describe":
                    return {"session": {"key": parent_child, "label": "Benchmark lead"}}
                if method == "chat.history":
                    return {"messages": []}
                raise AssertionError(method)
            return send_request

        with mock.patch("openclaw_status_action._load_owui_chat",
                        new=mock.AsyncMock(return_value={"title": "Forecast app upkeep"})) as load:
            data, _ = await _drawer_fill(gateway(f"agent:main:openwebui-{user}-{chat}"), {"first": True},
                                         {"id": user})
            later, rpcs = await _drawer_fill(gateway(f"agent:main:openwebui-{user}-{chat}"), {}, {"id": user})
        self.assertEqual(data["parent"], {"title": "Forecast app upkeep", "chatId": chat})
        load.assert_awaited_once_with(chat, user)
        self.assertIsNone(later["parent"])                       # read once, with the first fill
        nested, _ = await _drawer_fill(gateway(parent_child), {"first": True})
        self.assertEqual(nested["parent"], {"title": "Benchmark lead", "childKey": parent_child})


class InProcessAgentDetailTests(unittest.TestCase):
    """B10: an in-process agent opens the same sheet, from its tool card:
    its task and its result, nothing to poll."""

    def test_an_agent_row_opens_its_task_and_result(self):
        """The same sheet: its task as the turn that started it, its result
        as the reply."""
        agent = {"id": "t2", "title": "Broker research", "state": "returned", "startedAt": NOW - 60_000,
                 "task": "Compare IBKR and Trading212 for a small account.", "result": "IBKR: lower fees."}
        seen = _page(_RUN_JS + r"""
run(input.fill);
const row = byData(document.getElementById('openclaw-status-section-agents'), 'openclawAgentRow')[0];
row.onclick();
console.log(JSON.stringify({
  title: byData(drawer(), 'openclawTitle').map((e) => e.textContent),
  chip: byData(drawer(), 'openclawChip').map((e) => e.textContent),
  run: shown(),
  taskHead: byData(drawer(), 'openclawBlock').length,
  fetches: fetches.length,
  timers: timers.size,
}));
""", {"open": _render_modal_open_js(IDENTITY),
                     "fill": openclaw_status_action._render_agents_fill_js(
                         {"agents": [agent], "total": 1, "fetchedAt": NOW, "error": None})})
        self.assertEqual(seen["title"], ["Broker research"])
        self.assertEqual(seen["chip"], ["Returned"])
        self.assertEqual(seen["run"], [["user", "Compare IBKR and Trading212 for a small account."],
                                       ["text", "IBKR: lower fees."]])
        self.assertEqual(seen["taskHead"], 0)
        self.assertEqual(seen["fetches"], 0)

    def test_agent_rows_carry_the_cards_task_and_result(self):
        from test_status_subagents_unit import _agents_chat
        rows = {r["title"]: r for r in openclaw_status_action._agent_call_rows(_agents_chat())}
        self.assertEqual(rows["Broker research"]["result"], "[Subagent hand-back] Brokers: IBKR")
        self.assertIsNone(rows["Benchmark run"]["result"])


if __name__ == "__main__":
    unittest.main()


class SpawnMessageShapeTests(unittest.TestCase):
    """ELI-164: on OpenClaw 2026.10.10 `chat.history` shows a child's spawn
    message as its bare task, and keeps the wrapped prompt the model got in
    `__openclaw.modelPromptProjection.text`. The drawer read no task from it
    and showed the task again as a user turn in the run."""

    WRAPPED = ("<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\n\n[Subagent Context] You are running as a subagent "
               "(depth 1/5). Complete the current [Subagent Task]; inherited conversation is background "
               "context, not your assignment.\n\n[Subagent Task]\n\n<<<END_OPENCLAW_INTERNAL_CONTEXT>>>\n\n"
               "Price out Fly.io.\n\n<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\n\nBegin. Execute the assigned "
               "task to completion.\n\n<<<END_OPENCLAW_INTERNAL_CONTEXT>>>")

    def messages(self):
        return [
            {"role": "user", "content": "Price out Fly.io.", "idempotencyKey": "r1:user",
             "__openclaw": {"senderIsOwner": False,
                            "modelPromptProjection": {"version": 1, "text": self.WRAPPED}}},
            {"role": "assistant", "content": [{"type": "text", "text": "Fly.io: $5 a month."}]},
            {"role": "user", "content": "Also Render."},
            {"role": "assistant", "content": [{"type": "text", "text": "Render: $7."}]},
        ]

    def test_the_task_comes_from_the_projected_prompt(self):
        self.assertEqual(openclaw_status_action._spawn_task_of(self.messages()), "Price out Fly.io.")

    def test_the_spawn_message_is_not_a_turn_of_the_run(self):
        items = openclaw_status_action._drawer_run_items(self.messages())
        self.assertEqual([(i["kind"], i.get("text")) for i in items],
                         [("text", "Fly.io: $5 a month."), ("user", "Also Render."), ("text", "Render: $7.")])

    def test_the_9x_shape_still_reads(self):
        old = [{"role": "user", "content": self.WRAPPED}] + self.messages()[1:]
        self.assertEqual(openclaw_status_action._spawn_task_of(old), "Price out Fly.io.")
        self.assertEqual(openclaw_status_action._drawer_run_items(old)[0]["kind"], "text")
