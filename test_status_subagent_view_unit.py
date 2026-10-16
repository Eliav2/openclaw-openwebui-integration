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
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import test_status_action_unit  # noqa: F401  (installs the module stubs)
from test_status_action_unit import _FAKE_DOM_JS, _drawer_conn, _emitted_codes

import openclaw_status_action
from openclaw_status_action import (
    Action,
    _render_drawer_open_js,
    _render_drawer_overview_fill_js,
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
    runtime = shutil.which("bun") or shutil.which("node")
    if runtime is None:
        raise unittest.SkipTest("no bun or node")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(_PAGE_JS + body)
    try:
        out = subprocess.run([runtime, fh.name], input=json.dumps({"now": NOW, **payload}),
                             capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(fh.name)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


def _rows():
    """A labelled child with a 1,200-character task and an unlabelled one."""
    labelled = _subagent_row({"key": CHILD, "label": "invest-bot: freqtrade head-to-head", "status": "done",
                              "startedAt": NOW - 65_000, "endedAt": NOW - 5_000,
                              "lastMessagePreview": LONG_RESULT[:240]}, LONG_TASK)
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


class DrawerHeaderTests(unittest.TestCase):
    """B2, B3, B4: the header is one title line and one status line, so the
    tabs and the content keep the screen."""

    def _open(self, width):
        labelled, _ = _rows()
        return _page(r"""
run(input.drawer);
run(input.overview);
const panel = drawer().children[1];
const header = panel.children[0];
const scroller = panel.walk().find((e) => e.style.overflowY === 'auto');
console.log(JSON.stringify({
  header: header.textContent,
  title: byData(header, 'openclawTitle').map(style)[0] || null,
  chip: byData(header, 'openclawChip').map((e) => e.textContent)[0] || null,
  scroller: scroller ? { minHeight: scroller.style.minHeight || null } : null,
  width: panel.style.width || null,
  buttons: header.walk().filter((e) => e.tagName === 'BUTTON').map((e) => e.textContent),
}));
""", {"width": width, "open": _render_modal_open_js(IDENTITY),
      "drawer": _render_drawer_open_js({**labelled, "kind": "child", "controlUiUrl": None}),
      "overview": _render_drawer_overview_fill_js({
          "task": {**labelled, "result": LONG_RESULT}, "childSessionKey": CHILD, "provider": "anthropic",
          "model": "claude-opus-5-5", "context": None, "goal": None, "source": "pipe",
          "fetchedAt": NOW, "error": None})})

    def test_the_header_holds_no_task_and_no_reply(self):
        seen = self._open(1280)
        self.assertNotIn("re-clone", seen["header"])
        self.assertNotIn("two-phase matrix", seen["header"])
        self.assertIn("invest-bot: freqtrade head-to-head", seen["header"])
        self.assertEqual(seen["title"], {"whiteSpace": "nowrap", "textOverflow": "ellipsis", "lineClamp": None})
        self.assertEqual(seen["chip"], "Done")
        self.assertEqual(seen["scroller"], {"minHeight": "0"})

    def test_a_phone_gets_a_full_screen_sheet_with_a_back_button(self):
        phone = self._open(390)
        self.assertEqual(phone["width"], "100%")
        self.assertIn("‹ Back", phone["buttons"])
        self.assertNotIn("×", phone["buttons"])
        wide = self._open(1280)
        self.assertEqual(wide["width"], "min(420px, 88vw)")
        self.assertIn("×", wide["buttons"])


class OverviewTests(unittest.TestCase):
    """The Overview shows the Result (6 lines) and the Task (3 lines), each
    with "Show all", which a refill keeps open."""

    def test_result_and_task_clamped_with_show_all(self):
        labelled, _ = _rows()
        fill = _render_drawer_overview_fill_js({
            "task": {**labelled, "result": LONG_RESULT}, "childSessionKey": CHILD, "provider": "anthropic",
            "model": "m", "context": None, "goal": None, "source": "pipe", "fetchedAt": NOW, "error": None})
        seen = _page(r"""
run(input.drawer);
run(input.fill);
const blocks = () => byData(drawer(), 'openclawBlock').map((b) => [b.dataset.openclawBlock,
  byData(b, 'openclawBlockText').map((e) => [e.textContent.length, style(e).lineClamp])[0]]);
const first = blocks();
const more = byData(drawer(), 'openclawMore')[1];
if (more) more.onclick();                        // Show all on the Task
run(input.fill);                                  // a refill keeps it open
console.log(JSON.stringify({first, after: blocks()}));
""", {"open": _render_modal_open_js(IDENTITY), "fill": fill,
      "drawer": _render_drawer_open_js({**labelled, "kind": "child", "controlUiUrl": None})})
        self.assertEqual(seen["first"], [["result", [len(LONG_RESULT), "6"]], ["task", [len(LONG_TASK), "3"]]])
        self.assertEqual(seen["after"][1], ["task", [len(LONG_TASK), "unset"]])
        self.assertEqual(seen["after"][0], ["result", [len(LONG_RESULT), "6"]])


class FinalReplyTests(unittest.IsolatedAsyncioTestCase):
    """B5: the Result is the last block of the child's final reply. A
    claude-cli run ends with the Gateway's closer (`cli-assistant:<run>`),
    which repeats every message of the run joined: the progress log."""

    async def _overview(self, messages):
        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"key": CHILD, "status": "done", "startedAt": 1, "endedAt": 2}}
            if method == "chat.history":
                return {"messages": messages}
            raise AssertionError(f"unexpected RPC: {method}")

        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        with mock.patch("openclaw_status_action._get_action_connection",
                        new=mock.AsyncMock(return_value=(_drawer_conn(send_request), "pipe"))), \
             mock.patch("openclaw_status_action.asyncio.sleep", new=mock.AsyncMock()):
            await Action()._run_subagent_detail({"mode": "subagent-detail", "taskId": CHILD}, {"id": "u"}, emitter)
        code = [c for c in _emitted_codes(emitted) if "openclaw-drawer-section-overview" in c][-1]
        return json.loads(code.split("const DATA = ", 1)[1].split(";\n", 1)[0])["task"]

    async def test_a_claude_cli_run_shows_its_last_turn_not_the_closer(self):
        report = "The baseline won out-of-sample: +48.2%."
        task = await self._overview([
            {"role": "assistant", "stopReason": "tool_use",
             "content": [{"type": "toolcall", "id": "t1", "name": "Bash", "arguments": {}}]},
            {"role": "assistant", "stopReason": "end_turn", "content": [{"type": "text", "text": report}]},
            {"role": "assistant", "stopReason": "stop", "idempotencyKey": "cli-assistant:run-1",
             "__openclaw": {"idempotencyKey": "cli-assistant:run-1"},
             "content": [{"type": "text", "text": "Now rewriting run.sh.\n\nPolling.\n\n" + report}]},
        ])
        self.assertEqual(task["result"], report)

    async def test_the_gateways_own_runtime_shows_its_last_text_block(self):
        task = await self._overview([
            {"role": "assistant", "content": [
                {"type": "text", "text": "Reading the README."},
                {"type": "toolCall", "id": "c1", "name": "read", "arguments": {}},
                {"type": "text", "text": "Fly.io: about $3 a month."}]},
        ])
        self.assertEqual(task["result"], "Fly.io: about $3 a month.")


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

        with mock.patch("openclaw_status_action._get_action_connection",
                        new=mock.AsyncMock(return_value=(_drawer_conn(send_request), "pipe"))):
            result = await asyncio.wait_for(Action()._run_subagent_detail(
                {"mode": "subagent-detail", "taskId": CHILD}, {"id": "u"}, emitter), 5)  # bounds the test only
        self.assertEqual(result["status"], "ok")
        self.assertEqual(describes, [CHILD])

    def _poll_page(self, steps_js):
        _, unlabelled = _rows()
        return _page(r"""
run(input.fill);
const refreshes = () => fetches.filter((b) => b.mode === 'subagent-detail').length;
const row = document.getElementById('openclaw-status-section-subagents').walk().find((e) => e.tagName === 'BUTTON');
row.onclick();
""" + steps_js, {"open": _render_modal_open_js(IDENTITY), "fill": _section([unlabelled]),
                 "done": _render_drawer_overview_fill_js({
                     "task": {**unlabelled, "status": "completed", "endedAt": NOW, "result": "ok"},
                     "childSessionKey": OTHER, "provider": None, "model": None, "context": None,
                     "goal": None, "source": "pipe", "fetchedAt": NOW, "error": None})})

    def test_the_drawer_refreshes_past_15_minutes_until_the_child_ends(self):
        seen = self._poll_page(r"""
tick(15 * 60);
const at15 = refreshes();
tick(5 * 60);
const at20 = refreshes();
run(input.done);
tick(60);
console.log(JSON.stringify({at15, at20, after: refreshes(), timers: timers.size}));
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
console.log(JSON.stringify({before, after: refreshes(), open: !!drawer(), timers: timers.size}));
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
const t = byData(drawer(), 'openclawBlock').map((b) => b.dataset.openclawBlock + ':' + b.textContent.length);
console.log(JSON.stringify({blocks: t, chip: byData(drawer(), 'openclawChip').map((e) => e.textContent)}));
""", {"open": _render_modal_open_js(IDENTITY),
      "drawer": _render_drawer_open_js({**unlabelled, "kind": "child", "controlUiUrl": None}),
      "stale": _render_drawer_overview_fill_js({
          "task": {**labelled, "result": LONG_RESULT}, "childSessionKey": CHILD, "provider": None,
          "model": None, "context": None, "goal": None, "source": "pipe", "fetchedAt": NOW, "error": None})})
        self.assertEqual(seen["chip"], ["Running"])
        self.assertFalse(any(b.startswith("result:") for b in seen["blocks"]), seen["blocks"])


class InProcessAgentDetailTests(unittest.TestCase):
    """B10: an in-process agent opens the same sheet, from its tool card:
    its task and its result, nothing to poll."""

    def test_an_agent_row_opens_its_task_and_result(self):
        agent = {"id": "t2", "title": "Broker research", "state": "returned", "startedAt": NOW - 60_000,
                 "task": "Compare IBKR and Trading212 for a small account.", "result": "IBKR: lower fees."}
        seen = _page(r"""
run(input.fill);
const row = byData(document.getElementById('openclaw-status-section-subagents'), 'openclawAgentRow')[0];
row.onclick();
console.log(JSON.stringify({
  title: byData(drawer(), 'openclawTitle').map((e) => e.textContent),
  chip: byData(drawer(), 'openclawChip').map((e) => e.textContent),
  blocks: byData(drawer(), 'openclawBlockText').map((e) => e.textContent),
  tabs: drawer().walk().filter((e) => e.dataset.tab).length,
  fetches: fetches.length,
}));
""", {"open": _render_modal_open_js(IDENTITY), "fill": _section([], [agent])})
        self.assertEqual(seen["title"], ["Broker research"])
        self.assertEqual(seen["chip"], ["Returned"])
        self.assertEqual(seen["blocks"], ["IBKR: lower fees.", "Compare IBKR and Trading212 for a small account."])
        self.assertEqual(seen["tabs"], 0)
        self.assertEqual(seen["fetches"], 0)

    def test_agent_rows_carry_the_cards_task_and_result(self):
        from test_status_subagents_unit import _agents_chat
        rows = {r["title"]: r for r in openclaw_status_action._agent_call_rows(_agents_chat())}
        self.assertEqual(rows["Broker research"]["result"], "[Subagent hand-back] Brokers: IBKR")
        self.assertIsNone(rows["Benchmark run"]["result"])


if __name__ == "__main__":
    unittest.main()
