#!/usr/bin/env python3
"""The Status dialog's Scheduled section: the automations bound to this chat,
read-only. One `cron.list` read per session (the Gateway's `sessionKey`
filter), shared by every dialog of the chat and read again only after the
Gateway's `cron` event; "unavailable", never "0", when it cannot be read."""

import asyncio
import json
import sys
import unittest
from unittest import mock

import test_status_action_unit  # noqa: F401  (installs the module stubs)
from _test_helpers import action_gateway, RecordingEmitter, run_page
from test_status_action_unit import _FAKE_DOM_JS, _emitted_codes, _open_dialog, _render_modal_open_js
from test_status_background_unit import CHAT, KEY, NOW, USER, _gateway, _owui

import openclaw_pipe
import openclaw_status_action as action
from openclaw_status_action import Action

H = 3_600_000


def _job(name, *, target=f"session:{KEY}", schedule=None, payload=None, owner=None, **state):
    return {"id": f"j-{name}", "name": name, "enabled": True, "sessionTarget": target,
            "schedule": schedule or {"kind": "every", "everyMs": H},
            "payload": payload or {"kind": "agentTurn", "message": f"{name}: check the build\nmore lines"},
            "nextRunAtMs": NOW + 10 * 60_000, "state": state,
            **({"owner": owner} if owner else {})}


class RowTests(unittest.TestCase):
    """_scheduled_rows: what each row says."""

    def rows(self, jobs):
        return action._scheduled_rows(jobs, KEY, USER)["rows"]

    def test_once_or_how_often(self):
        cases = [
            ({"kind": "at", "at": "2026-11-06T09:00:00Z"}, (False, "Once")),
            ({"kind": "every", "everyMs": 30 * 60_000}, (True, "Every 30 min")),
            ({"kind": "every", "everyMs": 90 * 60_000}, (True, "Every 1 h 30 min")),
            ({"kind": "every", "everyMs": 2 * 24 * H}, (True, "Every 2 days")),
            ({"kind": "cron", "expr": "0 9 * * 1-5", "tz": "Asia/Jerusalem"},
             (True, "Cron 0 9 * * 1-5 (Asia/Jerusalem)")),
            ({"kind": "cron", "expr": "*/15 * * * *"}, (True, "Cron */15 * * * *")),
            ({"kind": "event", "source": "github"}, (True, "On github events")),
            ({"kind": "stream", "command": ["tail", "-f", "x"]}, (True, "On a command's output")),
        ]
        for schedule, want in cases:
            with self.subTest(schedule=schedule):
                (row,) = self.rows([_job("x", schedule=schedule)])
                self.assertEqual((row["recurring"], row["cadence"]), want)

    def test_the_first_line_of_the_next_turns_message(self):
        turn, event, script = self.rows([
            _job("turn"),
            _job("event", payload={"kind": "systemEvent", "text": "\n  Reminder: call mom  \nlater"}),
            _job("script", payload={"kind": "script", "script": "return 1"}),
        ])
        self.assertEqual(turn["message"], "turn: check the build")
        self.assertEqual(event["message"], "Reminder: call mom")
        self.assertIsNone(script["message"])
        (long,) = self.rows([_job("long", payload={"kind": "agentTurn", "message": "x" * 500})])
        self.assertEqual(len(long["message"]), action._SCHEDULED_MESSAGE_MAX)

    def test_who_made_it_without_anyone_elses_ids(self):
        other_user = "33333333-3333-3333-3333-333333333333"
        owners = {
            "this chat": KEY,
            "another chat of yours": f"agent:main:openwebui-{USER}-chat-2",
            "the main agent's main session": "agent:main:main",
            "another session": f"agent:main:openwebui-{other_user}-chat-7",
        }
        for want, owner in owners.items():
            with self.subTest(want=want):
                (row,) = self.rows([_job("x", owner={"agentId": "main", "sessionKey": owner})])
                self.assertEqual(row["createdBy"], want)
                self.assertNotIn(other_user, json.dumps(row))
        (row,) = self.rows([_job("x")])
        self.assertIsNone(row["createdBy"])

    def test_running_first_then_by_next_run_and_a_failure_says_so(self):
        rows = self.rows([
            {**_job("later"), "nextRunAtMs": NOW + 5 * H},
            {**_job("sooner"), "nextRunAtMs": NOW + H},
            _job("now", runningAtMs=NOW - 5000),
            _job("broken", lastRunStatus="error", lastError="model 529", consecutiveErrors=3),
        ])
        self.assertEqual([r["title"] for r in rows], ["now", "broken", "sooner", "later"])
        broken = rows[1]
        self.assertEqual((broken["status"], broken["error"], broken["failedInARow"]), ("failed", "model 529", 3))

    def test_unreadable_is_not_zero(self):
        self.assertEqual(action._scheduled_rows(None, KEY, USER), {"available": False, "rows": [], "total": 0})


class _Conn:
    """The Gateway connection parts the section uses, `cron.list` recorded."""

    def __init__(self, jobs, *, no_session_filter=False, unknown=False, pages=None):
        self.calls, self.jobs, self.pages = [], jobs, pages
        self.no_session_filter, self.unknown = no_session_filter, unknown
        self.cron_revision = 0
        self._waiter = None

    def cron_event(self):
        if self._waiter is None:
            self._waiter = asyncio.Event()
        return self._waiter

    def cron_changed(self):
        self.cron_revision += 1
        waiter, self._waiter = self._waiter, None
        if waiter:
            waiter.set()

    async def send_request(self, method, params, timeout=5):
        assert method == "cron.list", method
        self.calls.append(dict(params))
        if self.unknown:
            raise RuntimeError("unknown method: cron.list")
        if "sessionKey" in params and self.no_session_filter:
            raise RuntimeError("invalid cron.list params: at root: unexpected property 'sessionKey'")
        if self.pages:
            return self.pages[params.get("offset", 0)]
        jobs = [j for j in self.jobs if "sessionKey" not in params
                or j["sessionTarget"] == f"session:{params['sessionKey']}"
                or j.get("sessionKey") == params["sessionKey"]]
        return {"jobs": jobs, "hasMore": False, "nextOffset": None}


class ReadTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        action._scheduled_cache.clear()
        action._all_jobs_cache.clear()

    async def test_the_gateway_filters_by_this_chats_session(self):
        conn = _Conn([_job("mine"), _job("theirs", target="session:agent:main:openwebui-u-c")])
        jobs = await action._read_scheduled(conn, KEY)
        self.assertEqual([j["name"] for j in jobs], ["mine"])
        (params,) = conn.calls
        self.assertEqual(params["sessionKey"], KEY)
        self.assertEqual(params["limit"], action._CRON_PAGE_LIMIT)
        self.assertIs(params["includeDeliveryPreviews"], False)

    async def test_every_page_is_read(self):
        conn = _Conn([], pages={
            0: {"jobs": [_job("a")], "hasMore": True, "nextOffset": 1},
            1: {"jobs": [_job("b")], "hasMore": False, "nextOffset": None},
        })
        self.assertEqual([j["name"] for j in await action._read_scheduled(conn, KEY)], ["a", "b"])
        self.assertEqual([c["offset"] for c in conn.calls], [0, 1])

    async def test_one_read_per_session_until_a_cron_event(self):
        conn = _Conn([_job("mine")])
        await action._read_scheduled(conn, KEY)
        await action._read_scheduled(conn, KEY)       # a second dialog, another tab
        self.assertEqual(len(conn.calls), 1)
        conn.cron_changed()                           # a job added, changed, removed, ran
        await action._read_scheduled(conn, KEY)
        self.assertEqual(len(conn.calls), 2)

    async def test_the_cache_keeps_only_current_reads_and_is_bounded(self):
        conn = _Conn([_job("mine")])
        await action._read_scheduled(conn, "agent:main:old")
        conn.cron_changed()
        with mock.patch.object(action, "_SCHEDULED_CACHE_MAX", 2):
            for key in ("agent:main:a", "agent:main:b", "agent:main:c"):
                await action._read_scheduled(conn, key)
        self.assertEqual(list(action._scheduled_cache), ["agent:main:b", "agent:main:c"])

    async def test_a_new_connection_reads_again(self):
        await action._read_scheduled(_Conn([_job("mine")]), KEY)
        other = _Conn([_job("mine")])
        await action._read_scheduled(other, KEY)
        self.assertEqual(len(other.calls), 1)

    async def test_disabled_jobs_are_not_scheduled(self):
        conn = _Conn([_job("on"), {**_job("off"), "enabled": False}])
        self.assertEqual([j["name"] for j in await action._read_scheduled(conn, KEY)], ["on"])

    async def test_without_the_filter_one_shared_list_matched_here(self):
        """2026.9.3: `sessionKey` refused, so one full list for every session,
        matched on "session:<key>" and the job's `sessionKey`, bare or prefixed."""
        other = "agent:main:openwebui-u2-c2"
        conn = _Conn([
            _job("target"), _job("made-here", target="current", sessionKey=KEY),
            _job("bare", target="session:" + KEY.split(":", 2)[2]),
            _job("heartbeat", target="main"), _job("theirs", target=f"session:{other}"),
        ], no_session_filter=True)
        conn.jobs[1]["sessionKey"] = KEY
        mine = await action._read_scheduled(conn, KEY)
        self.assertEqual([j["name"] for j in mine], ["target", "made-here", "bare"])
        theirs = await action._read_scheduled(conn, other)
        self.assertEqual([j["name"] for j in theirs], ["theirs"])
        # One filtered try per session, one full list for both.
        self.assertEqual(sum("sessionKey" not in c for c in conn.calls), 1)

    async def test_no_cron_list_is_unavailable_and_a_failure_is_an_error(self):
        self.assertIsNone(await action._read_scheduled(_Conn([], unknown=True), KEY))
        conn = _Conn([])
        conn.send_request = mock.AsyncMock(side_effect=RuntimeError("cron.list broke"))
        data = await action._scheduled_data(conn, asyncio.ensure_future(action._read_scheduled(conn, KEY)),
                                            KEY, USER, "pipe")
        self.assertFalse(data["available"])
        self.assertIn("cron.list broke", data["error"])


class GatewayCronEventTests(unittest.TestCase):
    """The connection's `cron_revision` moves on a `cron` event and on a gap."""

    def test_event_and_gap_move_the_revision_and_wake_the_waiter(self):
        conn = openclaw_pipe._GatewayConnection(lambda: None)
        start = conn.cron_revision
        waiter = conn.cron_event()
        conn._on_cron_changed()
        self.assertTrue(waiter.is_set())
        after_event = conn.cron_revision
        self.assertNotEqual(after_event, start)
        self.assertIsNot(conn.cron_event(), waiter)
        gap = conn.cron_event()
        conn._wake_session_waiters()          # a socket gap: a `cron` event may be lost
        self.assertTrue(gap.is_set())
        after_gap = conn.cron_revision
        self.assertNotIn(after_gap, (start, after_event))
        conn._wake_session_waiters("agent:main:x")   # one session's change is not a cron change
        self.assertEqual(conn.cron_revision, after_gap)

    def test_no_two_connections_share_a_revision(self):
        a = openclaw_pipe._GatewayConnection(lambda: None)
        b = openclaw_pipe._GatewayConnection(lambda: None)
        self.assertNotEqual(a.cron_revision, b.cron_revision)
        a._on_cron_changed()
        b._on_cron_changed()
        self.assertNotEqual(a.cron_revision, b.cron_revision)


class GatewayCloseTests(unittest.IsolatedAsyncioTestCase):

    async def test_closing_wakes_a_waiting_follow(self):
        conn = openclaw_pipe._GatewayConnection(lambda: None)
        start, waiter = conn.cron_revision, conn.cron_event()
        await conn.disconnect()
        self.assertTrue(waiter.is_set())
        self.assertNotEqual(conn.cron_revision, start)


def _fill(emitted, name="scheduled"):
    for code in reversed(_emitted_codes(emitted)):
        if f"getElementById('openclaw-status-section-{name}')" in code and "const DATA = " in code:
            return json.loads(code.split("const DATA = ", 1)[1].split(";\n", 1)[0])
    return None


class FollowTests(unittest.IsolatedAsyncioTestCase):
    """Mode "scheduled-rows": the page's ask after each fill, answered on the
    next `cron` event; no timer."""

    def setUp(self):
        action._scheduled_cache.clear()
        action._open_dialogs.clear()

    def dialog(self):
        dialog_id, dialog = action._dialog_open(USER, KEY)
        return dialog_id, dialog

    async def ask(self, conn, dialog_id, revision, user=USER):
        emitted = []
        self.enterContext(action_gateway(conn))      # for the whole test: the follow connects again
        task = asyncio.ensure_future(Action().action(
            {"chat_id": CHAT, "mode": "scheduled-rows", "dialogId": dialog_id, "revision": revision},
            __user__={"id": user}, __event_emitter__=RecordingEmitter(emitted)))
        for _ in range(20):
            await asyncio.sleep(0)
        return task, emitted

    async def test_it_waits_for_the_cron_event_then_fills(self):
        conn = _Conn([_job("mine")])
        dialog_id, _dialog = self.dialog()
        task, emitted = await self.ask(conn, dialog_id, conn.cron_revision)
        self.assertFalse(task.done())
        self.assertEqual(conn.calls, [])          # nothing read until something changed
        conn.jobs.append(_job("added"))
        conn.cron_changed()
        self.assertEqual(await asyncio.wait_for(task, 5), {"status": "ok"})
        data = _fill(emitted)
        self.assertEqual([r["title"] for r in data["rows"]], ["mine", "added"])
        self.assertEqual(data["revision"], conn.cron_revision)

    async def test_a_change_before_the_ask_fills_at_once(self):
        conn = _Conn([_job("mine")])
        dialog_id, _dialog = self.dialog()
        task, emitted = await self.ask(conn, dialog_id, conn.cron_revision - 1)
        self.assertEqual(await asyncio.wait_for(task, 5), {"status": "ok"})
        self.assertEqual([r["title"] for r in _fill(emitted)["rows"]], ["mine"])

    async def test_closing_the_dialog_ends_it_without_a_fill(self):
        conn = _Conn([_job("mine")])
        dialog_id, dialog = self.dialog()
        task, emitted = await self.ask(conn, dialog_id, conn.cron_revision)
        dialog["closed"].set()
        self.assertEqual(await asyncio.wait_for(task, 5), {"status": "ok"})
        self.assertIsNone(_fill(emitted))
        self.assertEqual(conn.calls, [])

    async def test_past_the_follow_bound_it_ends_without_a_fill(self):
        conn = _Conn([_job("mine")])
        dialog_id, dialog = self.dialog()
        dialog["expires"] = 0
        task, emitted = await self.ask(conn, dialog_id, conn.cron_revision)
        self.assertEqual(await asyncio.wait_for(task, 5), {"status": "ok"})
        self.assertIsNone(_fill(emitted))

    async def test_only_the_dialogs_own_user(self):
        conn = _Conn([_job("mine")])
        dialog_id, _dialog = self.dialog()
        task, emitted = await self.ask(conn, dialog_id, conn.cron_revision - 1,
                                       user="22222222-2222-2222-2222-222222222222")
        self.assertEqual((await asyncio.wait_for(task, 5))["status"], "error")
        self.assertIsNone(_fill(emitted))
        self.assertEqual(conn.calls, [])
        # The page is told, not left following nothing (another worker's dialog).
        self.assertIn("This dialog is out of date", json.dumps(_emitted_codes(emitted)))

    async def test_a_changed_revision_past_the_follow_bound_is_not_read(self):
        conn = _Conn([_job("mine")])
        dialog_id, dialog = self.dialog()
        dialog["expires"] = 0
        task, emitted = await self.ask(conn, dialog_id, conn.cron_revision - 1)
        self.assertEqual(await asyncio.wait_for(task, 5), {"status": "ok"})
        self.assertIsNone(_fill(emitted))
        self.assertEqual(conn.calls, [])

    async def test_a_closed_dialog_with_a_changed_revision_is_not_read(self):
        conn = _Conn([_job("mine")])
        dialog_id, dialog = self.dialog()
        dialog["closed"].set()
        task, emitted = await self.ask(conn, dialog_id, conn.cron_revision - 1)
        self.assertEqual(await asyncio.wait_for(task, 5), {"status": "ok"})
        self.assertIsNone(_fill(emitted))
        self.assertEqual(conn.calls, [])


class OpenTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        action._scheduled_cache.clear()

    async def test_the_open_fills_scheduled_from_one_filtered_read(self):
        calls, params = [], []
        conn = _gateway(calls, jobs=[_job("mine"), _job("gw-heartbeat", target="main")], cron_params=params)
        emitted = []
        with action_gateway(conn), mock.patch.dict(sys.modules, _owui([])):
            await _open_dialog(Action().action({"chat_id": CHAT, "id": "m1", "model": "openclaw.x"},
                                               __user__={"id": USER},
                                               __event_emitter__=RecordingEmitter(emitted)), conn)
        self.assertEqual(calls.count("cron.list"), 1)       # shared with Background
        self.assertEqual([p["sessionKey"] for p in params], [KEY])
        data = _fill(emitted)
        self.assertEqual([r["title"] for r in data["rows"]], ["mine"])
        self.assertTrue(data["available"])
        self.assertNotIn("gw-heartbeat", json.dumps(_emitted_codes(emitted)))


_SCHED_PAGE_JS = _FAKE_DOM_JS + r"""
const fetches = [];
global.localStorage = { getItem: () => 'tok' };
global.fetch = (url, opts) => { fetches.push(JSON.parse(opts.body)); return Promise.resolve({}); };
run(input.open);
input.fills.forEach(run);
const section = () => document.getElementById('openclaw-status-section-scheduled');
const leaves = () => section().walk().filter((e) => e.children.length === 0 && e.text).map((e) => e.text);
const out = { before: leaves() };
for (let s = 0; s < (input.advance || 0); s++) { now += 1000; [...timers.values()].forEach((f) => f()); }
out.after = leaves();
out.fetches = fetches;
console.log(JSON.stringify(out));
"""

_SCHED_TITLES_JS = _FAKE_DOM_JS + r"""
global.localStorage = { getItem: () => 'tok' };
global.fetch = () => new Promise(() => {});
run(input.open);
input.fills.forEach(run);
const section = document.getElementById('openclaw-status-section-scheduled');
console.log(JSON.stringify(section.walk().map((e) => e.title).filter(Boolean)));
"""

# The follow's retries: every answer after `answers` runs out is a network failure;
# a retry's wait fires at once. Prints how many requests the page made.
_SCHED_RETRY_JS = _FAKE_DOM_JS + r"""
let asks = 0;
global.localStorage = { getItem: () => 'tok' };
global.fetch = () => {
  asks++;
  const a = input.answers.shift();
  return a ? Promise.resolve({ ok: a.ok, json: () => Promise.resolve(a.body) }) : Promise.reject(new Error('net'));
};
const realTimeout = setTimeout;
global.setTimeout = (fn) => { Promise.resolve().then(fn); return 0; };
run(input.open);
input.fills.forEach(run);
realTimeout(() => console.log(JSON.stringify(asks)), 50);
"""

IDENTITY = {"chatId": "c", "messageId": "m", "sessionId": "s", "owuiModel": "x", "dialogId": "d1"}


class PageTests(unittest.TestCase):

    def page(self, data, advance=0):
        fill = action._render_scheduled_fill_js({"source": "pipe", "fetchedAt": NOW, "error": None,
                                                 "revision": 4, **data})
        return run_page(_SCHED_PAGE_JS, {"now": NOW, "open": _render_modal_open_js(IDENTITY),
                                         "fills": [fill], "advance": advance}, tz="UTC")

    def test_a_row_counts_down_to_its_exact_time(self):
        row = action._scheduled_rows([{**_job("nightly", owner={"agentId": "main", "sessionKey": KEY}),
                                       "nextRunAtMs": NOW + 2 * H + 5 * 60_000}], KEY, USER)
        seen = self.page(row, advance=60)
        self.assertIn("Scheduled (1)", seen["before"])
        self.assertIn("in 2h05m", seen["before"])
        self.assertIn("in 2h04m", seen["after"])
        self.assertIn("Every 1 h", seen["before"])
        when = next(t for t in seen["before"] if t.startswith("Next "))
        self.assertRegex(when, r"^Next \w{3}, \w{3} \d+, \d\d:\d\d( [AP]M)? · by this chat$")
        self.assertIn("nightly: check the build", seen["before"])

    def test_after_a_fill_it_asks_for_the_next_one(self):
        seen = self.page(action._scheduled_rows([], KEY, USER))
        self.assertEqual(seen["fetches"], [{**{k: IDENTITY[k] for k in ()}, "model": "x", "chat_id": "c",
                                            "id": "m", "session_id": "s", "dialogId": "d1",
                                            "mode": "scheduled-rows", "revision": 4}])
        self.assertIn("Nothing scheduled for this chat.", seen["before"])
        self.assertIn("Scheduled (0)", seen["before"])

    def test_unavailable_or_failed_never_says_zero(self):
        for data, note in (({"available": False, "rows": [], "total": 0},
                            "Scheduled automations are unavailable on this Gateway."),
                           ({"available": False, "rows": [], "total": 0, "error": "Could not read: x"},
                            "Could not read: x")):
            with self.subTest(note=note):
                seen = self.page(data)
                self.assertIn("Scheduled", seen["before"])
                self.assertFalse(any("(0)" in t for t in seen["before"]), seen["before"])
                self.assertTrue(any(note in t for t in seen["before"]), seen["before"])

    def test_a_meta_line_shows_whole_on_hover(self):
        error = "Last run failed: " + "x" * 300
        row = action._scheduled_rows([{**_job("broken"), "nextRunAtMs": NOW + H}], KEY, USER)
        row["rows"][0].update(status="failed", error="x" * 300, failedInARow=1)
        fill = action._render_scheduled_fill_js({"source": "pipe", "fetchedAt": NOW, "error": None,
                                                 "revision": 4, **row})
        seen = run_page(_SCHED_TITLES_JS, {"now": NOW, "open": _render_modal_open_js(IDENTITY), "fills": [fill]})
        self.assertIn(error, seen)

    def test_a_failed_follow_request_is_asked_again_a_few_times(self):
        for answers, asks in (([], 4),                                         # network down: 1 + 3 retries
                              ([{"ok": True, "body": {"status": "error", "detail": "x"}}], 4),
                              ([{"ok": True, "body": {"status": "ok"}}], 1),    # closed or past the bound
                              ([{"ok": True, "body": {"status": "error", "detail": "unknown dialog"}}], 1)):
            with self.subTest(answers=answers):
                fill = action._render_scheduled_fill_js({"source": "pipe", "fetchedAt": NOW, "error": None,
                                                         "revision": 4, **action._scheduled_rows([], KEY, USER)})
                seen = run_page(_SCHED_RETRY_JS, {"now": NOW, "open": _render_modal_open_js(IDENTITY),
                                                  "fills": [fill], "answers": answers})
                self.assertEqual(seen, asks)

    def test_due_says_now(self):
        row = action._scheduled_rows([{**_job("due"), "nextRunAtMs": NOW + 2000}], KEY, USER)
        seen = self.page(row, advance=3)
        self.assertIn("now", seen["after"])


if __name__ == "__main__":
    unittest.main()
