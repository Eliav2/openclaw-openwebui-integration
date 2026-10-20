#!/usr/bin/env python3
"""The Status Action's Subagents section and drawer (ELI-106).

They used to read the Gateway's Tasks API (`tasks.list`, `tasks.get`), which
OpenClaw removes in 2026.9.7. They now read the parent's child sessions
(`sessions.list` with `spawnedBy`), the task text from the child's transcript
head (`sessions.preview`), and the child session itself (`sessions.describe`,
`chat.history`): RPCs both 2026.9.3 and 2026.10.3 serve.
"""

import json
import os
import sys
import types
import unittest
from unittest import mock

import test_status_action_unit  # noqa: F401  (installs the module stubs)
from _test_helpers import action_gateway, fresh_child_index, RecordingEmitter, run_page
from test_status_action_unit import _drawer_conn, _emitted_codes, _open_dialog

import openclaw_status_action
from openclaw_status_action import Action

PARENT_CHAT, PARENT_USER = "chat-1", "user-1"
PARENT_KEY = f"agent:main:openwebui-{PARENT_USER}-{PARENT_CHAT}"
CHILD_A = "agent:main:subagent:aaaaaaaa-0000-0000-0000-000000000001"
CHILD_B = "agent:main:subagent:bbbbbbbb-0000-0000-0000-000000000002"
CHILD_DONE = "agent:main:subagent:cccccccc-0000-0000-0000-000000000003"

# What a spawn's first user message looks like on each version.
WRAPPED_93 = ("[Subagent Context] You are running as a subagent (depth 1/2).\n\n[Subagent Task]\n\n"
              "Research X and report.\n\nBegin. Execute the assigned task to completion.")
WRAPPED_103 = ("<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\n\n[Subagent Context] You are running as a subagent "
               "(depth 1/2). Complete the current [Subagent Task]; inherited conversation is background "
               "context.\n\n[Subagent Task]\n\n<<<END_OPENCLAW_INTERNAL_CONTEXT>>>\n\nSummarise Y.\n\n"
               "<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\n\nBegin. Execute the assigned task to completion.\n\n"
               "<<<END_OPENCLAW_INTERNAL_CONTEXT>>>")


def _list_rpc(rows, previews, calls):
    async def send_request(method, params, timeout=5):
        calls.append((method, params))
        if method == "sessions.describe":
            return {"session": {"modelProvider": "anthropic", "model": "claude-sonnet-5"}}
        if method == "usage.status":
            return {"providers": []}
        if method == "claudeCliUsage.status":
            return {}
        if method == "sessions.list":
            return {"sessions": rows}
        if method == "sessions.preview":
            return {"previews": [{"key": k, "status": "ok", "items": previews.get(k, [])}
                                 for k in params["keys"]]}
        if method == "tasks.list":
            return {"tasks": []}
        if method == "cron.list":
            return {"jobs": []}
        raise AssertionError(f"unexpected RPC: {method}")
    return send_request


def _child(key, status, **kw):
    """A child session row as the Gateway's full `sessions.list` has it."""
    return {"key": key, "status": status, "spawnedBy": PARENT_KEY, **kw}


class SubagentListTests(unittest.IsolatedAsyncioTestCase):

    async def _open(self, rows, previews, *, unfold=False):
        """Open the dialog; `unfold`: then tap a folded Subagents section
        open, as the page does (mode "subagent-rows"). Returns the RPCs,
        the emitted events and the last Subagents fill's data; the In-process
        agents fill's data is on `self.agents_data`."""
        calls, emitted, seen, agents = [], [], [], []

        emitter = RecordingEmitter(emitted)

        conn = mock.AsyncMock()
        conn.send_request = mock.AsyncMock(side_effect=_list_rpc(rows, previews, calls))
        real = openclaw_status_action._render_subagents_fill_js
        real_agents = openclaw_status_action._render_agents_fill_js

        def spy(data):
            seen.append(data)
            return real(data)

        with action_gateway(conn), \
             mock.patch("openclaw_status_action._render_subagents_fill_js", side_effect=spy), \
             mock.patch("openclaw_status_action._render_agents_fill_js",
                        side_effect=lambda d: (agents.append(d), real_agents(d))[1]):
            await _open_dialog(Action().action({"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                                               __event_emitter__=emitter), conn)
            if unfold:
                await Action().action({"chat_id": PARENT_CHAT, "mode": "subagent-rows",
                                       "dialogId": _dialog_id(emitted)},
                                      __user__={"id": PARENT_USER}, __event_emitter__=emitter)
        self.agents_data = agents[-1] if agents else None
        return calls, emitted, seen[-1]

    async def test_lists_every_child_titled_by_its_label_else_its_task_newest_first(self):
        rows = [
            _child(CHILD_DONE, "done", startedAt=900, endedAt=990),
            _child(CHILD_A, "running", startedAt=1000, lastMessagePreview="halfway"),
            _child(CHILD_B, "queued", label="y-summary", createdAt=1100),
        ]
        previews = {CHILD_A: [{"role": "user", "text": WRAPPED_93}, {"role": "assistant", "text": "halfway"}],
                    CHILD_B: [{"role": "user", "text": WRAPPED_103}]}
        calls, emitted, data = await self._open(rows, previews)
        self.assertIsNone(data["error"])
        self.assertEqual([t["id"] for t in data["tasks"]], [CHILD_B, CHILD_A, CHILD_DONE])
        b, a, done = data["tasks"]
        self.assertEqual((a["title"], a["status"], a["childSessionKey"], a["startedAt"], a["progressSummary"]),
                         ("Research X and report.", "running", CHILD_A, 1000, "halfway"))
        # The spawn label wins; the task is the drawer's (ELI-155).
        self.assertEqual((b["title"], b["task"], b["status"]), ("y-summary", "Summarise Y.", "queued"))
        self.assertEqual((done["status"], done["startedAt"], done["endedAt"]), ("completed", 900, 990))
        # Rendered: the section's own fill carries the rows.
        code = next(c for c in _emitted_codes(emitted)
                    if "getElementById('openclaw-status-section-subagents')" in c)
        self.assertIn("Research X and report.", code)

    async def test_the_newest_twenty_are_listed(self):
        rows = [_child(f"agent:main:subagent:{n:08d}-0000-0000-0000-000000000000", "done",
                       startedAt=n * 1000, endedAt=n * 1000 + 5) for n in range(1, 26)]
        _calls, _emitted, data = await self._open(rows, {})
        self.assertEqual(len(data["tasks"]), 20)
        self.assertEqual(data["tasks"][0]["startedAt"], 25000)
        self.assertEqual(data["tasks"][-1]["startedAt"], 6000)
        self.assertEqual(data["total"], 25)

    async def test_an_ended_child_without_an_end_time_ends_at_its_last_update(self):
        """Its duration must not count up to now."""
        _calls, _emitted, data = await self._open(
            [_child(CHILD_A, "done", startedAt=1000, updatedAt=4000)], {})
        self.assertEqual(data["tasks"][0]["endedAt"], 4000)

    async def test_a_child_whose_last_message_is_its_spawn_shows_no_subtitle(self):
        """Right after a spawn the child's newest message is the spawn itself,
        so lastMessagePreview is the "[Subagent Context] ... Begin. Execute the
        assigned task" wrapper, raw or flattened to one line. That is the task
        again, not progress (ELI-119)."""
        flat_93, flat_103 = " ".join(WRAPPED_93.split()), " ".join(WRAPPED_103.split())
        for preview in (WRAPPED_93, WRAPPED_103, flat_93, flat_103):
            with self.subTest(preview=preview[:40]):
                rows = [_child(CHILD_A, "running", startedAt=1000, lastMessagePreview=preview),
                        _child(CHILD_B, "running", lastMessagePreview="Fly.io: about $3 a month")]
                previews = {CHILD_A: [{"role": "user", "text": WRAPPED_93}]}
                _calls, emitted, data = await self._open(rows, previews)
                a, b = data["tasks"]
                self.assertEqual(a["title"], "Research X and report.")
                self.assertIsNone(a["progressSummary"])
                self.assertEqual(b["progressSummary"], "Fly.io: about $3 a month")
                code = next(c for c in _emitted_codes(emitted)
                            if "getElementById('openclaw-status-section-subagents')" in c)
                self.assertNotIn("Subagent Context", code)

    async def test_no_preview_call_without_children(self):
        calls, _emitted, data = await self._open([], {})
        self.assertEqual(data["tasks"], [])
        self.assertNotIn("sessions.preview", [m for m, _p in calls])

    async def test_an_ended_child_has_its_task_as_title_and_its_last_reply_as_summary(self):
        """Nothing runs, so the section opens folded (ELI-163) and its
        transcripts wait for the reader's tap; the tap reads them once and
        lists the rows the open already read, without listing again."""
        rows = [_child(CHILD_DONE, "done", startedAt=900, endedAt=990)]
        previews = {CHILD_DONE: [{"role": "user", "text": WRAPPED_103}, {"role": "assistant", "text": "Y is fine"}]}
        calls, _emitted, data = await self._open(rows, previews)
        self.assertTrue(data["pending"])
        self.assertNotIn("sessions.preview", [m for m, _p in calls])
        calls, _emitted, data = await self._open(rows, previews, unfold=True)
        self.assertFalse(data["pending"])
        (t,) = data["tasks"]
        self.assertEqual((t["title"], t["terminalSummary"]), ("Summarise Y.", "Y is fine"))
        self.assertEqual([m for m, _p in calls].count("sessions.list"), 1)
        self.assertEqual([m for m, _p in calls].count("sessions.preview"), 1)

    async def test_a_tap_on_someone_elses_dialog_reads_nothing(self):
        rows = [_child(CHILD_DONE, "done", startedAt=900, endedAt=990)]
        calls, emitted = [], []
        conn = mock.AsyncMock()
        conn.send_request = mock.AsyncMock(side_effect=_list_rpc(rows, {}, calls))
        with action_gateway(conn):
            await _open_dialog(Action().action({"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                                               __event_emitter__=RecordingEmitter(emitted)), conn)
            calls.clear()
            result = await Action().action({"chat_id": PARENT_CHAT, "mode": "subagent-rows",
                                            "dialogId": _dialog_id(emitted)},
                                           __user__={"id": "someone-else"}, __event_emitter__=RecordingEmitter([]))
        self.assertEqual(result["status"], "error")
        self.assertEqual(calls, [])

    async def test_title_falls_back_to_label_then_key(self):
        rows = [_child(CHILD_A, "running", label="research-x", startedAt=2),
                _child(CHILD_B, "running", startedAt=1)]
        # A long-running child's preview no longer reaches its first message.
        previews = {CHILD_A: [{"role": "assistant", "text": "step 9"}], CHILD_B: []}
        _calls, _emitted, data = await self._open(rows, previews)
        self.assertEqual([t["title"] for t in data["tasks"]], ["research-x", CHILD_B])

    async def test_a_preview_failure_keeps_the_rows(self):
        rows = [_child(CHILD_A, "running", label="research-x")]

        async def send_request(method, params, timeout=5):
            if method == "sessions.preview":
                raise RuntimeError("preview down")
            return await _list_rpc(rows, {}, [])(method, params, timeout)

        emitted, seen = [], []
        conn = mock.AsyncMock()
        conn.send_request = mock.AsyncMock(side_effect=send_request)
        real = openclaw_status_action._render_subagents_fill_js
        with action_gateway(conn), \
             mock.patch("openclaw_status_action._render_subagents_fill_js",
                        side_effect=lambda d: (seen.append(d), real(d))[1]):
            await _open_dialog(Action().action({"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                                               __event_emitter__=lambda e: emitted.append(e) or _noop()), conn)
        self.assertEqual([t["title"] for t in seen[-1]["tasks"]], ["research-x"])


async def _noop():
    return None


def _dialog_id(emitted):
    """The id the open handed the page (its IDENTITY)."""
    code = next(c for c in _emitted_codes(emitted) if "const IDENTITY = " in c)
    return json.loads(code.split("const IDENTITY = ", 1)[1].split(";\n", 1)[0])["dialogId"]


class TaskTextTests(unittest.TestCase):

    def test_both_wrapper_formats(self):
        self.assertEqual(openclaw_status_action._subagent_task_text(WRAPPED_93), "Research X and report.")
        self.assertEqual(openclaw_status_action._subagent_task_text(WRAPPED_103), "Summarise Y.")

    def test_not_a_spawn_message(self):
        self.assertIsNone(openclaw_status_action._subagent_task_text("just a follow-up"))

    def test_status_words_match_what_the_section_colours(self):
        m = openclaw_status_action._subagent_status
        self.assertEqual([m(s) for s in ("queued", "running", "done", "failed", "killed", "timeout")],
                         ["queued", "running", "completed", "failed", "cancelled", "timed_out"])
        self.assertEqual(m("interrupted"), "interrupted")


class SubagentDrawerTests(unittest.IsolatedAsyncioTestCase):

    async def _drawer(self, task_id, send_request, keep_index=False):
        emitted = []
        emitter = RecordingEmitter(emitted)

        conn = _drawer_conn(send_request)
        with action_gateway(conn, keep_index=keep_index), \
             mock.patch("openclaw_status_action.asyncio.sleep", new=mock.AsyncMock()):
            result = await Action()._run_subagent_detail(
                {"mode": "subagent-detail", "taskId": task_id, "taskTitle": "Research X", "taskStatus": "running"},
                {"id": PARENT_USER}, emitter)
        return result, emitted, conn

    @staticmethod
    def _fill(emitted):
        code = [c for c in _emitted_codes(emitted) if "S.fillDrawer(DATA)" in c][-1]
        return json.loads(code.split("const DATA = ", 1)[1].split(";\n", 1)[0])

    async def test_reads_the_child_session_once_per_request(self):
        """The open drawer asks again while the child runs (ELI-155)."""
        n = {"describe": 0}

        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                self.assertEqual(params["key"], CHILD_A)
                n["describe"] += 1
                return {"session": {"key": CHILD_A, "status": "done",
                                    "startedAt": 1783790000000, "endedAt": 1783790005000,
                                    "modelProvider": "anthropic", "model": "claude-sonnet-5",
                                    "contextTokens": 1000000, "totalTokens": 5000}}
            if method == "chat.history":
                self.assertEqual(params["sessionKey"], CHILD_A)
                return {"messages": [{"role": "user", "content": WRAPPED_93},
                                     {"role": "assistant", "content": [{"type": "text", "text": "X is fine"}]}]}
            raise AssertionError(f"unexpected RPC: {method}")

        result, emitted, conn = await self._drawer(CHILD_A, send_request)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(n["describe"], 1)
        methods = [c.args[0] for c in conn.send_request.call_args_list]
        self.assertNotIn("tasks.get", methods)
        data = self._fill(emitted)
        self.assertEqual(data["childSessionKey"], CHILD_A)
        self.assertEqual(data["task"]["status"], "completed")
        self.assertEqual(data["task"]["endedAt"], 1783790005000)
        self.assertEqual(data["items"], [{"kind": "text", "text": "X is fine"}])

    async def test_the_task_is_the_headers_not_a_turn_of_the_run(self):
        """The child's first user message is the Gateway's spawn wrapper: the
        header shows the task in it, the way the Subagents row does (ELI-119),
        and the run does not repeat it, on both versions and both shapes."""
        for wrapped, task in ((WRAPPED_93, "Research X and report."), (WRAPPED_103, "Summarise Y.")):
            for content in (wrapped, [{"type": "text", "text": wrapped}]):
                with self.subTest(task=task, shape=type(content).__name__):
                    async def send_request(method, params, timeout=5, content=content):
                        if method == "sessions.describe":
                            return {"session": {"key": CHILD_A, "status": "done", "startedAt": 1, "endedAt": 2}}
                        if method == "chat.history":
                            return {"messages": [{"role": "user", "content": content},
                                                 {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]}
                        raise AssertionError(f"unexpected RPC: {method}")

                    _result, emitted, _conn = await self._drawer(CHILD_A, send_request)
                    data = self._fill(emitted)
                    self.assertEqual(data["task"]["task"], task)
                    self.assertEqual(data["items"], [{"kind": "text", "text": "ok"}])

    async def test_a_child_whose_transcript_lacks_its_spawn_yet_shows_the_task_it_was_given(self):
        """ELI-178: on 2026.10 the drawer can open before the spawn message is written."""
        fresh_child_index()
        openclaw_status_action._subagent_remember(PARENT_KEY, keys=[CHILD_A], tasks={CHILD_A: "Research X and report."})

        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"key": CHILD_A, "status": "running", "startedAt": 1, "spawnedBy": PARENT_KEY}}
            if method == "chat.history":
                return {"messages": []}
            raise AssertionError(f"unexpected RPC: {method}")

        _result, emitted, _conn = await self._drawer(CHILD_A, send_request, keep_index=True)
        self.assertEqual(self._fill(emitted)["task"]["task"], "Research X and report.")

    def test_a_user_message_that_is_not_a_spawn_stays_as_written(self):
        items = openclaw_status_action._drawer_run_items(
            [{"role": "user", "content": "Mention [Subagent Task] in passing."},
             {"role": "assistant", "content": WRAPPED_93}])
        self.assertEqual([i["text"] for i in items], ["Mention [Subagent Task] in passing.", WRAPPED_93])

    async def test_a_failed_child_shows_its_error(self):
        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"key": CHILD_A, "status": "failed", "startedAt": 1, "endedAt": 2,
                                    "lastRunError": "provider 529"}}
            if method == "chat.history":
                return {"messages": []}
            raise AssertionError(f"unexpected RPC: {method}")

        _result, emitted, _conn = await self._drawer(CHILD_A, send_request)
        self.assertEqual(self._fill(emitted)["task"]["error"], "provider 529")

    async def test_an_unknown_child_says_so(self):
        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {}
            if method == "chat.history":
                return {"messages": []}
            raise AssertionError(f"unexpected RPC: {method}")

        result, emitted, _conn = await self._drawer(CHILD_A, send_request)
        self.assertEqual(result["status"], "error")
        self.assertIn("Sub-agent not found.", _emitted_codes(emitted)[-1])

    async def test_a_legacy_task_id_is_no_longer_available(self):
        """Messages written before ELI-100 carry a Tasks API id, which nothing
        but the removed `tasks.get` can resolve."""
        async def send_request(method, params, timeout=5):
            raise AssertionError(f"unexpected RPC: {method}")

        result, emitted, _conn = await self._drawer("cc50ea4c-2631-4157-bf51-e2b4863fedf4", send_request)
        self.assertEqual(result["status"], "error")
        self.assertIn("no longer available", _emitted_codes(emitted)[-1])


class NoTasksApiTests(unittest.TestCase):
    """Sub-agents never come from the Tasks API, which 2026.10 removed. The
    Background section reads `tasks.list` behind one adapter that degrades
    on a Gateway without it (ELI-163)."""

    def test_only_the_background_adapter_calls_the_tasks_api(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "openclaw_status_action.py"), encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn('"tasks.get"', source)
        self.assertEqual(source.count('"tasks.list"'), 1)
        adapter = source.split("async def _read_background_tasks", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"tasks.list"', adapter)


class FinishedWhileOpenTests(unittest.IsolatedAsyncioTestCase):
    """ELI-127: the dialog's ticker counts a running child up (ELI-120), and
    the dialog filled once, so a child that finished while it stayed open
    kept its "running" dot and counted up until the dialog was reopened.
    The section refills when a listed child's run state changes: the
    Gateway's pushed `sessions.changed`, no polling."""

    NOW = 1_783_790_100_000

    async def test_a_child_that_finishes_stops_counting_in_the_open_dialog(self):
        import asyncio
        from test_status_action_unit import _DIALOG_PAGE_JS, _render_modal_open_js

        rows = [_child(CHILD_A, "running", startedAt=self.NOW - 1000)]
        previews = {CHILD_A: [{"role": "user", "text": WRAPPED_93}]}
        waiters, emitted = {}, []

        def session_change_event(key):
            # As the connection does: one Event per session, popped and set
            # by the next pushed change to its run state.
            return waiters.setdefault(key, asyncio.Event())

        emitter = RecordingEmitter(emitted)

        conn = mock.Mock()
        conn.send_request = mock.AsyncMock(side_effect=_list_rpc(rows, previews, []))
        conn.session_change_event = session_change_event
        conn.session_child_event = lambda key: asyncio.Event()
        with action_gateway(conn):
            opened = asyncio.ensure_future(Action().action(
                {"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER}, __event_emitter__=emitter))
            # Until the dialog is filled and follows its child (or, without
            # the follow, has returned).
            while not opened.done() and CHILD_A not in waiters:
                await asyncio.sleep(0)
            rows[0] = _child(CHILD_A, "done", startedAt=self.NOW - 1000, endedAt=self.NOW)
            previews[CHILD_A] = previews[CHILD_A] + [{"role": "assistant", "text": "Done."}]   # its reply
            ev = waiters.pop(CHILD_A, None)
            if ev is not None:
                ev.set()    # the Gateway pushes the child's settle
            await asyncio.wait_for(opened, 5)   # bounds the test only

        fills = [c for c in _emitted_codes(emitted)
                 if "getElementById('openclaw-status-section-subagents')" in c]
        identity = {"chatId": "c", "messageId": "m", "sessionId": "s", "owuiModel": "x"}
        payload = {"now": self.NOW, "open": _render_modal_open_js(identity), "fills": fills,
                   "steps": [{"advance": 60}]}
        # A minute later the ended child is still listed (ELI-152), with
        # its own duration, not counting up.
        self.assertEqual(run_page(_DIALOG_PAGE_JS, payload), [{"cells": ["1s"], "timers": 1}])

    async def test_a_child_opened_before_its_task_is_in_its_transcript_is_titled_once_it_is(self):
        """ELI-253: on 2026.10 the spawn message reaches the child's transcript
        after the spawn returns. A dialog opened in between, with no task
        recorded from the spawn call (none is, on a claude-cli Gateway), read
        no task and titled the running child by its session key for as long
        as it stayed open. The transcript append is pushed (`session.message`
        on a transcript subscription); the section refills on it."""
        import asyncio

        rows = [_child(CHILD_A, "running", startedAt=self.NOW - 1000)]
        previews = {CHILD_A: []}
        changes, appends, subscribed, emitted, seen = {}, {}, [], [], []

        conn = mock.Mock()
        conn.send_request = mock.AsyncMock(side_effect=_list_rpc(rows, previews, []))
        conn.session_change_event = lambda key: changes.setdefault(key, asyncio.Event())
        conn.session_message_event = lambda key: appends.setdefault(key, asyncio.Event())
        conn.session_child_event = lambda key: asyncio.Event()
        conn.ensure_message_subscription = mock.AsyncMock(side_effect=subscribed.append)
        real = openclaw_status_action._render_subagents_fill_js

        async def settle():
            # Let the dialog run until it waits (bounded: a dialog that
            # never waits on the append still gets checked).
            for _ in range(2000):
                await asyncio.sleep(0)

        with action_gateway(conn), mock.patch(
                "openclaw_status_action._render_subagents_fill_js",
                side_effect=lambda d: (seen.append(d), real(d))[1]):
            opened = asyncio.ensure_future(Action().action(
                {"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                __event_emitter__=RecordingEmitter(emitted)))
            await settle()
            self.assertEqual(seen[0]["tasks"][0]["title"], CHILD_A)   # the race: no task yet
            previews[CHILD_A] = [{"role": "user", "text": WRAPPED_103}]
            ev = appends.pop(CHILD_A, None)
            if ev is not None:
                ev.set()    # the Gateway pushes the spawn message's append
            await settle()
            titled = [t["title"] for d in seen for t in d["tasks"] if t["status"] == "running"]
            rows[0] = _child(CHILD_A, "done", startedAt=self.NOW - 1000, endedAt=self.NOW)
            previews[CHILD_A] = previews[CHILD_A] + [{"role": "assistant", "text": "Done."}]   # its reply
            ev = changes.pop(CHILD_A, None)
            if ev is not None:
                ev.set()    # the child's settle ends the follow
            await asyncio.wait_for(opened, 5)   # bounds the test only

        self.assertEqual(subscribed, [CHILD_A])
        self.assertEqual(titled[-1], "Summarise Y.", titled)

    async def test_a_child_spawned_after_the_dialog_opened_is_listed_while_it_stays_open(self):
        """ELI-260: the dialog followed only the children listed at its open,
        so with none listed it stopped at once, and a child the chat's run
        spawned after the open stayed out of it until a reopen. While the
        chat's own session runs, the dialog follows it, and a new child's
        row change (pushed with `spawnedBy` naming this chat's session)
        refills the section."""
        import asyncio

        rows, parent = [], {"busy": True}
        changes, spawns, emitted, seen = {}, {}, [], []
        transcript = {CHILD_A: [{"role": "user", "text": WRAPPED_93}]}
        listed = _list_rpc(rows, transcript, [])

        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"key": PARENT_KEY, "hasActiveRun": parent["busy"]}}
            return await listed(method, params, timeout)

        conn = mock.Mock()
        conn.send_request = mock.AsyncMock(side_effect=send_request)
        conn.session_change_event = lambda key: changes.setdefault(key, asyncio.Event())
        conn.session_message_event = lambda key: asyncio.Event()
        conn.session_child_event = lambda key: spawns.setdefault(key, asyncio.Event())
        conn.ensure_message_subscription = mock.AsyncMock()
        real = openclaw_status_action._render_subagents_fill_js

        async def settle():
            for _ in range(2000):
                await asyncio.sleep(0)

        def push(waiters, key):
            ev = waiters.pop(key, None)
            if ev is not None:
                ev.set()

        with action_gateway(conn), mock.patch(
                "openclaw_status_action._render_subagents_fill_js",
                side_effect=lambda d: (seen.append(d), real(d))[1]):
            opened = asyncio.ensure_future(Action().action(
                {"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                __event_emitter__=RecordingEmitter(emitted)))
            await settle()
            self.assertEqual(seen[-1]["tasks"], [])
            rows.append(_child(CHILD_A, "running", startedAt=self.NOW))
            push(spawns, PARENT_KEY)    # the Gateway pushes the new child's row
            await settle()
            listed_while_open = [t["status"] for t in seen[-1]["tasks"]]
            following = not opened.done()
            rows[0] = _child(CHILD_A, "done", startedAt=self.NOW, endedAt=self.NOW + 1000)
            transcript[CHILD_A] = transcript[CHILD_A] + [{"role": "assistant", "text": "Done."}]   # its reply
            parent["busy"] = False
            push(changes, CHILD_A)
            push(changes, PARENT_KEY)
            await asyncio.wait_for(opened, 5)   # bounds the test only

        self.assertEqual(listed_while_open, ["running"])
        self.assertTrue(following)
        self.assertEqual([t["status"] for t in seen[-1]["tasks"]], ["completed"])

    def _follow_conn(self, rows, describe, previews=None):
        """A connection whose events are kept per session key, as the real
        one does: (conn, {kind: {key: Event}})."""
        import asyncio

        events = {"change": {}, "message": {}, "child": {}}
        listed = _list_rpc(rows, previews or {}, [])

        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": dict(describe)}
            return await listed(method, params, timeout)

        conn = mock.Mock()
        conn.send_request = mock.AsyncMock(side_effect=send_request)
        conn.session_change_event = lambda key: events["change"].setdefault(key, asyncio.Event())
        conn.session_message_event = lambda key: events["message"].setdefault(key, asyncio.Event())
        conn.session_child_event = lambda key: events["child"].setdefault(key, asyncio.Event())
        conn.ensure_message_subscription = mock.AsyncMock()
        conn.has_reply = lambda key: False
        return conn, events

    @staticmethod
    def _push(waiters, key):
        ev = waiters.pop(key, None)
        if ev is not None:
            ev.set()

    @staticmethod
    async def _settle():
        import asyncio
        for _ in range(2000):
            await asyncio.sleep(0)

    async def test_a_dialog_opened_before_the_chats_run_starts_follows_it(self):
        """ELI-260 reopened (QA C27, 1 in 5): opened 0.3 s after a send, the
        dialog read the chat's session before the Gateway had its run
        (`hasActiveRun` false; the Pipe's chat.send was still in flight), so
        it stopped at once and the child the run spawned never showed. The
        Pipe streaming the reply in this process already has its consumer
        registered: the dialog follows that reply too."""
        import asyncio

        rows, describe, reply = [], {"key": PARENT_KEY, "hasActiveRun": False}, {"on": True}
        transcript = {CHILD_A: [{"role": "user", "text": WRAPPED_93}]}
        conn, events = self._follow_conn(rows, describe, transcript)
        conn.has_reply = lambda key: reply["on"] and key == PARENT_KEY
        emitted, seen = [], []
        real = openclaw_status_action._render_subagents_fill_js
        with action_gateway(conn), mock.patch(
                "openclaw_status_action._render_subagents_fill_js",
                side_effect=lambda d: (seen.append(d), real(d))[1]):
            opened = asyncio.ensure_future(Action().action(
                {"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                __event_emitter__=RecordingEmitter(emitted)))
            await self._settle()
            describe["hasActiveRun"] = True
            self._push(events["change"], PARENT_KEY)    # the run starts
            await self._settle()
            rows.append(_child(CHILD_A, "running", startedAt=self.NOW))
            self._push(events["child"], PARENT_KEY)     # it spawns a child
            await self._settle()
            listed_while_open = [t["status"] for t in seen[-1]["tasks"]]
            rows[0] = _child(CHILD_A, "done", startedAt=self.NOW, endedAt=self.NOW + 1000)
            transcript[CHILD_A] = transcript[CHILD_A] + [{"role": "assistant", "text": "SUB-51"}]   # its reply
            describe["hasActiveRun"], reply["on"] = False, False
            self._push(events["change"], CHILD_A)
            self._push(events["change"], PARENT_KEY)
            await asyncio.wait_for(opened, 5)   # bounds the test only

        self.assertEqual(listed_while_open, ["running"])

    async def test_a_folded_section_stays_folded_through_a_refill_until_the_tap(self):
        """ELI-266: opened while the chat's run was ending, with its children
        done, the section opened folded with its transcripts left for the tap
        (ELI-163); the run's end refilled it as read, so the tap asked for
        nothing (the docs' dropped-tap shot timed out on main). The refill
        keeps it folded; the tap reads the transcripts, once."""
        import asyncio

        rows = [_child(CHILD_A, "done", startedAt=self.NOW, endedAt=self.NOW + 1000),
                _child(CHILD_B, "done", startedAt=self.NOW + 1, endedAt=self.NOW + 2000)]
        describe = {"key": PARENT_KEY, "hasActiveRun": True}
        previews = {CHILD_A: [{"role": "user", "text": WRAPPED_93}, {"role": "assistant", "text": "X is fine"}]}
        conn, events = self._follow_conn(rows, describe, previews)
        emitted, seen = [], []
        real = openclaw_status_action._render_subagents_fill_js

        def previews_read():
            return [c.args[0] for c in conn.send_request.call_args_list].count("sessions.preview")

        with action_gateway(conn), mock.patch(
                "openclaw_status_action._render_subagents_fill_js",
                side_effect=lambda d: (seen.append(d), real(d))[1]):
            opened = asyncio.ensure_future(Action().action(
                {"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                __event_emitter__=RecordingEmitter(emitted)))
            await self._settle()
            self.assertTrue(seen[-1]["pending"])
            fills = len(seen)
            describe["hasActiveRun"] = False
            self._push(events["change"], PARENT_KEY)    # the chat's run ends
            await asyncio.wait_for(opened, 5)   # bounds the test only
            self.assertGreater(len(seen), fills)    # the section was refilled
            self.assertTrue(seen[-1]["pending"])
            self.assertEqual(previews_read(), 0)
            await Action().action({"chat_id": PARENT_CHAT, "mode": "subagent-rows",
                                   "dialogId": _dialog_id(emitted)},
                                  __user__={"id": PARENT_USER}, __event_emitter__=RecordingEmitter(emitted))

        self.assertFalse(seen[-1]["pending"])
        self.assertEqual(previews_read(), 1)
        self.assertEqual(seen[-1]["tasks"][-1]["terminalSummary"], "X is fine")

    async def test_a_child_done_in_the_open_dialog_gets_its_result_and_the_chat_its_context(self):
        """QA C31: a child that ended while the dialog followed it showed Done
        with no result line, and the dialog stopped there: a claude-cli
        child's messages reach its transcript after its run settles. Context,
        read at an open right after a send, had no data yet and never got it.
        The dialog follows an ended child until its last message is in its
        row (its transcript append is pushed), and refills Context on each
        change of the chat's run."""
        import asyncio

        rows = [_child(CHILD_A, "running", startedAt=self.NOW)]
        describe = {"key": PARENT_KEY, "hasActiveRun": True}
        transcript = {CHILD_A: [{"role": "user", "text": WRAPPED_93}]}
        conn, events = self._follow_conn(rows, describe, transcript)
        emitted, seen, contexts = [], [], []
        real = openclaw_status_action._render_subagents_fill_js
        real_context = openclaw_status_action._render_context_fill_js
        with action_gateway(conn), mock.patch(
                "openclaw_status_action._render_subagents_fill_js",
                side_effect=lambda d: (seen.append(d), real(d))[1]), mock.patch(
                "openclaw_status_action._render_context_fill_js",
                side_effect=lambda d: (contexts.append(d), real_context(d))[1]):
            opened = asyncio.ensure_future(Action().action(
                {"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                __event_emitter__=RecordingEmitter(emitted)))
            await self._settle()
            # The chat's run ends with its context known; the child goes on.
            describe.update(hasActiveRun=False, contextTokens=500_000, totalTokens=101_000)
            self._push(events["change"], PARENT_KEY)
            await self._settle()
            # The child settles before its reply reaches its transcript.
            rows[0] = _child(CHILD_A, "done", startedAt=self.NOW, endedAt=self.NOW + 1000)
            self._push(events["change"], CHILD_A)
            await self._settle()
            following = not opened.done()
            transcript[CHILD_A] = transcript[CHILD_A] + [{"role": "assistant", "text": "SUB-51: 17 x 3 = 51"}]   # its reply
            self._push(events["message"], CHILD_A)      # the reply is appended
            await asyncio.wait_for(opened, 5)   # bounds the test only

        self.assertTrue(following, "stopped at the child's settle, before its reply")
        self.assertEqual(seen[-1]["tasks"][0]["terminalSummary"], "SUB-51: 17 x 3 = 51")
        self.assertEqual(contexts[-1]["context"]["usedTokens"], "101k", contexts[-1])
        conn.ensure_message_subscription.assert_any_await(CHILD_A)

    async def test_a_childs_result_reads_the_same_in_the_open_dialog_and_a_fresh_open(self):
        """ELI-264 (QA C32): the Gateway gives a child's last message two ways.
        `sessions.list` with `includeLastMessage` has it as `lastMessagePreview`,
        markdown flattened (2026.10.5 session-transcript-title-reader:
        flattenMarkdown); `sessions.preview` has the transcript's text as
        written. The open dialog's refills list with the former, a fresh
        open's list did not, so its tap showed the latter: the same result
        read "The command cd /config/x ran" in place and "The command
        `cd /config/x` ran" after a reopen."""
        import asyncio

        raw, flat = "The command `cd /config/x` ran.", "The command cd /config/x ran."
        done = _child(CHILD_A, "done", startedAt=self.NOW, endedAt=self.NOW + 1000)
        rows = [_child(CHILD_A, "running", startedAt=self.NOW)]
        transcript = {CHILD_A: [{"role": "user", "text": WRAPPED_93}]}

        async def gateway(method, params, timeout=5):
            # As 2026.10.5 answers: the list's preview only when asked for.
            if method == "sessions.list":
                last = params.get("includeLastMessage") and transcript[CHILD_A][-1]["role"] == "assistant"
                return {"sessions": [dict(r, lastMessagePreview=flat) if last else r for r in rows]}
            if method == "sessions.describe":
                return {"session": {"key": PARENT_KEY, "hasActiveRun": False}}
            return await _list_rpc(rows, transcript, [])(method, params, timeout)

        def connection():
            conn, events = self._follow_conn(rows, {})
            conn.send_request = mock.AsyncMock(side_effect=gateway)
            return conn, events

        seen = []
        real = openclaw_status_action._render_subagents_fill_js
        spy = mock.patch("openclaw_status_action._render_subagents_fill_js",
                         side_effect=lambda d: (seen.append(d), real(d))[1])
        # In place: open while the child runs, then it ends with its result.
        conn, events = connection()
        with action_gateway(conn), spy:
            opened = asyncio.ensure_future(Action().action(
                {"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER}, __event_emitter__=RecordingEmitter([])))
            await self._settle()
            rows[0] = done
            transcript[CHILD_A] = transcript[CHILD_A] + [{"role": "assistant", "text": raw}]
            self._push(events["change"], CHILD_A)
            await asyncio.wait_for(opened, 5)   # bounds the test only
        in_place = seen[-1]["tasks"][0]["terminalSummary"]

        # A fresh open of the same child, then the tap that unfolds it.
        conn, _events = connection()
        emitted = []
        with action_gateway(conn), spy:
            await Action().action({"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                                  __event_emitter__=RecordingEmitter(emitted))
            await Action().action({"chat_id": PARENT_CHAT, "mode": "subagent-rows", "dialogId": _dialog_id(emitted)},
                                  __user__={"id": PARENT_USER}, __event_emitter__=RecordingEmitter([]))
        reopened = seen[-1]["tasks"][0]["terminalSummary"]

        self.assertEqual(in_place, reopened)

    async def test_a_claude_cli_childs_result_reads_the_same_in_place_as_after_a_reopen(self):
        """ELI-264 on live (QA session 35, 4/4): the open dialog kept "ran
        `cd /config` and got `SUB-51`" while every reopen read "ran cd /config
        and got SUB-51". What the dialog gets when a claude-cli child ends:
        its settle, with nothing in its transcript yet; then the append of its
        reply (`session.message`), which the transcript read has at once while
        the list's `lastMessagePreview` (read from the transcript index,
        reconciled later, and markdown flattened) is still empty; then nothing:
        the list catches up with no push. The in-place row was filled from the
        transcript, a reopen from the list."""
        import asyncio

        raw, flat = "ran `cd /config` and got `SUB-51`", "ran cd /config and got SUB-51"
        rows = [_child(CHILD_A, "running", startedAt=self.NOW)]
        transcript = {CHILD_A: [{"role": "user", "text": WRAPPED_93}]}
        indexed = {"text": None}     # the list's lastMessagePreview, once indexed

        async def gateway(method, params, timeout=5):
            if method == "sessions.list":
                last = params.get("includeLastMessage") and indexed["text"]
                return {"sessions": [dict(r, lastMessagePreview=last) if last else r for r in rows]}
            if method == "sessions.describe":
                return {"session": {"key": PARENT_KEY, "hasActiveRun": False}}
            return await _list_rpc(rows, transcript, [])(method, params, timeout)

        def connection():
            conn, events = self._follow_conn(rows, {})
            conn.send_request = mock.AsyncMock(side_effect=gateway)
            return conn, events

        seen = []
        real = openclaw_status_action._render_subagents_fill_js
        spy = mock.patch("openclaw_status_action._render_subagents_fill_js",
                         side_effect=lambda d: (seen.append(d), real(d))[1])
        conn, events = connection()
        with action_gateway(conn), spy:
            opened = asyncio.ensure_future(Action().action(
                {"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER}, __event_emitter__=RecordingEmitter([])))
            await self._settle()
            rows[0] = _child(CHILD_A, "done", startedAt=self.NOW, endedAt=self.NOW + 1000)
            self._push(events["change"], CHILD_A)       # its settle
            await self._settle()
            transcript[CHILD_A] = transcript[CHILD_A] + [{"role": "assistant", "text": raw}]
            self._push(events["message"], CHILD_A)      # its reply's append
            await asyncio.wait_for(opened, 5)   # bounds the test only
        in_place = seen[-1]["tasks"][0]["terminalSummary"]

        indexed["text"] = flat      # the list caught up, with no push
        conn, _events = connection()
        emitted = []
        with action_gateway(conn), spy:
            await Action().action({"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                                  __event_emitter__=RecordingEmitter(emitted))
            await Action().action({"chat_id": PARENT_CHAT, "mode": "subagent-rows", "dialogId": _dialog_id(emitted)},
                                  __user__={"id": PARENT_USER}, __event_emitter__=RecordingEmitter([]))
        reopened = seen[-1]["tasks"][0]["terminalSummary"]

        self.assertEqual(in_place, reopened)
        self.assertEqual(in_place, raw)     # the transcript's text, as the drawer shows it


class _WindowGateway:
    """A Gateway's session store as 2026.9.3 lists it: `spawnedBy` returns a
    parent's children that run or ended in the last 30 minutes only
    (`current`), `search` matches keys, and no filter returns every row."""

    def __init__(self, rows, current=(), fail_spawned_by=False):
        self.rows, self.current, self.fail = rows, set(current), fail_spawned_by
        self.calls = []

    def has_reply(self, _key):
        return False

    async def send_request(self, method, params, timeout=5):
        self.calls.append((method, dict(params)))
        if method == "sessions.list":
            if "spawnedBy" in params:
                if self.fail:
                    raise TimeoutError("sessions.list timed out")
                return {"sessions": [r for r in self.rows if r["key"] in self.current
                                     and params["spawnedBy"] in (r.get("spawnedBy"), r.get("parentSessionKey"))]}
            if "search" in params:
                return {"sessions": [r for r in self.rows if params["search"] in r["key"]]}
            return {"sessions": list(self.rows)}
        return await _list_rpc([], {}, [])(method, params, timeout)

    def full_reads(self):
        return [p for m, p in self.calls if m == "sessions.list" and not ({"spawnedBy", "search"} & set(p))]


class ChildIndexTests(unittest.IsolatedAsyncioTestCase):
    """Subagents lists the chat's children without reading every session:
    the children the Pipe saw it spawn (its child index), the current ones
    (`spawnedBy`), and a direct lookup of a recorded child the 30-minute
    window dropped. Ended children stay listed (ELI-152)."""

    def setUp(self):
        self.state = os.environ["OPENCLAW_BRIDGE_STATE_DIR"]
        fresh_child_index()

    async def _open(self, gw, *, unfold=False):
        emitted, seen = [], []
        real = openclaw_status_action._render_subagents_fill_js
        with action_gateway(gw, keep_index=True), mock.patch(
                "openclaw_status_action._render_subagents_fill_js",
                side_effect=lambda d: (seen.append(d), real(d))[1]):
            await _open_dialog(Action().action({"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                                               __event_emitter__=RecordingEmitter(emitted)), gw)
            if unfold:
                await Action().action({"chat_id": PARENT_CHAT, "mode": "subagent-rows",
                                       "dialogId": _dialog_id(emitted)},
                                      __user__={"id": PARENT_USER}, __event_emitter__=RecordingEmitter(emitted))
        return seen

    async def test_a_child_whose_transcript_lacks_its_spawn_yet_is_titled_by_its_task(self):
        """On 2026.10 a child's spawn message reaches its transcript after the
        spawn returns, so a dialog opened at once read no task and titled the
        running child by its key until it ended (ELI-178). The task the spawn
        call was given, which the Pipe recorded, titles it instead."""
        task = "Research X and report."
        openclaw_status_action._subagent_remember(PARENT_KEY, keys=[CHILD_A], tasks={CHILD_A: task})
        for status, unfold in (("running", False), ("done", True)):
            with self.subTest(status):
                gw = _WindowGateway([_child(CHILD_A, status, startedAt=1000)], current={CHILD_A})
                row = (await self._open(gw, unfold=unfold))[-1]["tasks"][0]
                self.assertEqual((row["title"], row["task"]),
                                 (openclaw_status_action._short_title(task), task))

    async def test_an_ended_child_out_of_the_window_is_listed_without_reading_every_session(self):
        old = _child(CHILD_A, "done", startedAt=1000, endedAt=1050)
        openclaw_status_action._subagent_remember(PARENT_KEY, rows=[old])
        gw = _WindowGateway([old, _child(CHILD_B, "running", startedAt=2000)], current={CHILD_B})
        seen = await self._open(gw)
        self.assertEqual([t["id"] for t in seen[-1]["tasks"]], [CHILD_B, CHILD_A])
        self.assertEqual(gw.full_reads(), [])
        self.assertFalse(any("search" in p for _m, p in gw.calls))   # a final snapshot needs no lookup

    async def test_a_recorded_child_the_window_dropped_is_looked_up_by_its_key(self):
        openclaw_status_action._subagent_remember(PARENT_KEY, keys=[CHILD_A])
        gw = _WindowGateway([_child(CHILD_A, "failed", startedAt=1000, endedAt=1100, lastRunError="529")])
        seen = await self._open(gw)
        self.assertEqual([(t["id"], t["status"]) for t in seen[-1]["tasks"]], [(CHILD_A, "failed")])
        self.assertEqual(gw.full_reads(), [])
        self.assertIn(("sessions.list", {"search": CHILD_A.rsplit(":", 1)[-1], "limit": 10}), gw.calls)
        # Its ended row is kept: the next open needs no lookup.
        self.assertEqual(openclaw_status_action._subagent_children(PARENT_KEY)[CHILD_A]["status"], "failed")

    async def test_the_list_failing_still_shows_the_known_children(self):
        openclaw_status_action._subagent_remember(PARENT_KEY, rows=[_child(CHILD_A, "done", startedAt=1, endedAt=2)])
        seen = await self._open(_WindowGateway([], fail_spawned_by=True))
        self.assertIsNone(seen[-1]["error"])
        self.assertEqual([t["id"] for t in seen[-1]["tasks"]], [CHILD_A])

    async def test_a_new_index_is_seeded_once_in_the_background(self):
        os.unlink(os.path.join(self.state, openclaw_status_action._SUBAGENT_INDEX_NAME))
        rows = [_child(CHILD_A, "done", startedAt=1000, endedAt=1050),
                _child(CHILD_B, "running", startedAt=2000)]
        gw = _WindowGateway(rows, current={CHILD_B})
        seen = await self._open(gw)
        self.assertEqual([t["id"] for t in seen[0]["tasks"]], [CHILD_B])          # first fill: no wait
        self.assertEqual([t["id"] for t in seen[-1]["tasks"]], [CHILD_B, CHILD_A])  # then the seed's
        self.assertEqual(len(gw.full_reads()), 1)
        gw2 = _WindowGateway(rows, current={CHILD_B})
        await self._open(gw2)
        self.assertEqual(gw2.full_reads(), [])

    async def test_the_seed_picks_the_chats_children_by_either_parent_field(self):
        """ELI-152's rule for the one full list: a child names its parent in
        `spawnedBy` or `parentSessionKey`; read from the store alone (a last
        message reads every session's transcript)."""
        os.unlink(os.path.join(self.state, openclaw_status_action._SUBAGENT_INDEX_NAME))
        other = "agent:main:openwebui-user-1-other-chat"
        rows = [
            _child(CHILD_A, "done", startedAt=1000, endedAt=1050),
            {"key": CHILD_B, "status": "failed", "parentSessionKey": PARENT_KEY, "startedAt": 2000,
             "endedAt": 2100, "lastRunError": "provider 529"},
            {"key": CHILD_DONE, "status": "done", "spawnedBy": other, "startedAt": 3000},
            {"key": PARENT_KEY, "status": "running"},
            # A visible spawn (`visible: true`): no run status, but ended.
            _child("agent:main:dashboard:dddddddd-0000-0000-0000-000000000004", None,
                   label="W1: engine", startedAt=1500, endedAt=1800),
        ]
        gw = _WindowGateway(rows)
        data = (await self._open(gw))[-1]
        self.assertEqual(gw.full_reads(), [{"limit": 1000}])
        self.assertEqual([(t["id"][-4:], t["status"]) for t in data["tasks"]],
                         [("0002", "failed"), ("0004", "ended"), ("0001", "completed")])
        self.assertEqual(data["tasks"][0]["error"], "provider 529")
        self.assertEqual(data["tasks"][1]["title"], "W1: engine")
        self.assertEqual(data["total"], 3)

    def test_the_pipe_records_the_task_an_accepted_spawn_was_given(self):
        """The receipt carries no task: the call's start event does, called
        directly or through 2026.10's `dispatch_action` wrapper."""
        receipt = json.dumps({"status": "accepted", "childSessionKey": CHILD_A, "runId": "r1"})
        envelope = {"content": [{"type": "text", "text": json.dumps(
            {"tool": {"id": "openclaw:core:sessions_spawn"}, "result": {"content": [{"type": "text", "text": receipt}]}})}]}
        args = {"task": "Research X and report.", "runtime": "subagent"}
        for name, call_args, result in (
                ("sessions_spawn", args, receipt),
                ("mcp__openclaw__sessions_spawn", args, receipt),
                ("dispatch_action", {"id": "openclaw:core:sessions_spawn", "args": args}, envelope)):
            with self.subTest(name):
                conn = openclaw_status_action._GatewayConnection(lambda: types.SimpleNamespace())
                seen = [conn._spawn_seen({"sessionKey": PARENT_KEY, "stream": "tool", "data": {
                    "phase": phase, "name": name, "toolCallId": "t", **extra}})
                    for phase, extra in (("start", {"args": call_args}), ("result", {"result": result}))]
                self.assertEqual(seen, [None, (PARENT_KEY, CHILD_A, "Research X and report.")])
        conn = openclaw_status_action._GatewayConnection(lambda: types.SimpleNamespace())
        self.assertEqual(conn._spawn_seen({"sessionKey": PARENT_KEY, "stream": "tool", "data": {
            "phase": "result", "name": "sessions_spawn", "toolCallId": "t", "result": receipt}}),
            (PARENT_KEY, CHILD_A, None), "a result whose start was missed still records the child")

    def test_the_pipe_records_an_accepted_spawn(self):
        receipt = {"status": "accepted", "childSessionKey": CHILD_A, "runId": "r1"}
        for name, result in (("sessions_spawn", json.dumps(receipt)),
                             ("mcp__openclaw__sessions_spawn",
                              {"content": [{"type": "text", "text": json.dumps(receipt)}]})):
            payload = {"sessionKey": PARENT_KEY, "runId": "r", "stream": "tool",
                       "data": {"phase": "result", "name": name, "toolCallId": "t", "result": result}}
            self.assertEqual(openclaw_status_action._spawned_child(payload), (PARENT_KEY, CHILD_A))
        refused = {"sessionKey": PARENT_KEY, "stream": "tool", "data": {
            "phase": "result", "name": "sessions_spawn", "result": json.dumps({"status": "forbidden"})}}
        self.assertIsNone(openclaw_status_action._spawned_child(refused))


def _card(call_id, name, args, result=None):
    """An Agent (or other) tool card as the Pipe stores it in a reply's output."""
    items = [{"type": "function_call", "id": call_id, "call_id": call_id, "name": name,
              "arguments": json.dumps(args), "status": "completed"}]
    if result is not None:
        items.append({"type": "function_call_output", "id": f"fco_{call_id}", "call_id": call_id,
                      "output": [{"type": "output_text", "text": result}], "status": "completed"})
    return items


def _agents_chat():
    """A chat with Agent calls on its current branch and one off it."""
    no_result = openclaw_status_action.TOOL_NO_RESULT_NOTE
    failed = "Agent " + openclaw_status_action.TOOL_ERROR_MARK
    msgs = {
        "u1": {"id": "u1", "parentId": None, "role": "user", "content": "research", "timestamp": 1000},
        "a1": {"id": "a1", "parentId": "u1", "role": "assistant", "timestamp": 1001, "done": True, "output":
               _card("t1", "Agent", {"description": "Tax research", "run_in_background": True},
                     "Async agent launched successfully. (internal metadata)")
               + _card("t2", "Agent", {"description": "Broker research", "subagent_type": "general-purpose"},
                       "[Subagent hand-back] Brokers: IBKR")
               + _card("t3", "Bash", {"command": "ls"}, "a b")},
        "u2": {"id": "u2", "parentId": "a1", "role": "user", "content": "more", "timestamp": 2000},
        # Off the current branch: a regenerated sibling.
        "a2x": {"id": "a2x", "parentId": "u2", "role": "assistant", "timestamp": 2001, "done": True,
                "output": _card("t9", "Agent", {"description": "Discarded branch"}, "x")},
        "a2": {"id": "a2", "parentId": "u2", "role": "assistant", "timestamp": 2002, "done": True,
               "output": _card("t4", "Agent", {"description": "Strategy research"}, no_result)
               + _card("t5", failed, {"description": "Venue research"}, "Agent crashed")},
        "u3": {"id": "u3", "parentId": "a2", "role": "user", "content": "go", "timestamp": 3000},
        "a3": {"id": "a3", "parentId": "u3", "role": "assistant", "timestamp": 3001, "done": False,
               "output": _card("t6", "Agent", {"description": "Benchmark run"})},
    }
    return {"history": {"currentId": "a3", "messages": msgs}}


class InProcessAgentTests(unittest.IsolatedAsyncioTestCase):
    """ELI-152: most sub-agents are Claude Code's in-process Agent calls,
    which the Gateway never lists as child sessions. The section lists them
    from the chat's own Agent tool cards: what the card can tell."""

    def test_agent_cards_on_the_current_branch_newest_first(self):
        rows = openclaw_status_action._agent_call_rows(_agents_chat())
        self.assertEqual([(r["title"], r["state"]) for r in rows], [
            ("Benchmark run", "running"),
            ("Venue research", "failed"),
            ("Strategy research", "cut off"),
            ("Broker research", "returned"),
            ("Tax research", "launched"),
        ])
        self.assertEqual(rows[0]["startedAt"], 3001000)
        self.assertEqual(rows[3]["subagentType"], "general-purpose")

    def test_a_call_with_no_result_in_a_finished_reply_was_cut_off(self):
        chat = _agents_chat()
        chat["history"]["messages"]["a3"]["done"] = True
        self.assertEqual(openclaw_status_action._agent_call_rows(chat)[0]["state"], "cut off")

    def test_the_newest_twenty(self):
        msgs = {"a": {"id": "a", "parentId": None, "role": "assistant", "timestamp": 5, "done": True,
                      "output": [i for n in range(25) for i in _card(f"t{n}", "Agent", {"description": f"job {n}"}, "ok")]}}
        rows = openclaw_status_action._agent_call_rows({"history": {"currentId": "a", "messages": msgs}})
        self.assertEqual([r["title"] for r in rows[:2]], ["job 24", "job 23"])
        self.assertEqual(len(rows), 25)   # the caller caps what it shows

    def test_no_chat_no_rows(self):
        self.assertEqual(openclaw_status_action._agent_call_rows(None), [])
        self.assertEqual(openclaw_status_action._agent_call_rows({"history": {}}), [])

    async def test_the_dialog_lists_them_beside_the_gateway_children(self):
        chats = types.ModuleType("open_webui.models.chats")

        class Chats:
            @staticmethod
            def get_chat_by_id(chat_id):
                assert chat_id == PARENT_CHAT
                return types.SimpleNamespace(user_id=PARENT_USER, chat=_agents_chat())
        chats.Chats = Chats
        mods = {"open_webui": types.ModuleType("open_webui"),
                "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.chats": chats}
        with mock.patch.dict(sys.modules, mods):
            _calls, emitted, data = await SubagentListTests._open(
                self, [_child(CHILD_DONE, "done", startedAt=900, endedAt=990)], {})
        self.assertEqual([t["id"] for t in data["tasks"]], [CHILD_DONE])
        agents = self.agents_data
        self.assertEqual([a["title"] for a in agents["agents"]],
                         ["Benchmark run", "Venue research", "Strategy research", "Broker research", "Tax research"])
        self.assertEqual(agents["total"], 5)

    async def test_another_users_chat_lists_no_agents(self):
        chats = types.ModuleType("open_webui.models.chats")
        chats.Chats = types.SimpleNamespace(
            get_chat_by_id=lambda chat_id: types.SimpleNamespace(user_id="someone-else", chat=_agents_chat()))
        mods = {"open_webui": types.ModuleType("open_webui"),
                "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.chats": chats}
        with mock.patch.dict(sys.modules, mods):
            await SubagentListTests._open(self, [], {})
        self.assertEqual(self.agents_data["agents"], [])


def _chain(n, missing_rows=()):
    """A chat of `n` messages in one branch, m0 (oldest) to m{n-1}, each
    assistant message with one Agent call; and readers over it as the narrow
    read gets them: parent links of the messages with a row, the rows, and
    the chat's own copy for the ones without."""
    msgs = {}
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        msgs[f"m{i}"] = {"parentId": f"m{i - 1}" if i else None, "role": role, "timestamp": 1000 + i,
                         "done": True, "output": _card(f"t{i}", "Agent", {"description": f"job {i}"}, "ok")
                         if role == "assistant" else []}
    reads = []
    links = {k: v["parentId"] for k, v in msgs.items() if k not in missing_rows}

    async def read_rows(ids):
        reads.append(sorted(ids))
        return {i: msgs[i] for i in ids}

    async def read_blob_message(mid):
        reads.append(("blob", mid))
        return msgs.get(mid)
    return links, read_rows, read_blob_message, reads


class RecentBranchTests(unittest.IsolatedAsyncioTestCase):
    """The In-process agents and Background sections read the chat's last 40
    messages, not the whole chat: their time stays flat as a chat grows."""

    async def test_a_long_chat_is_cut_to_the_recent_messages(self):
        links, rows, blob, reads = _chain(100)
        chat = await openclaw_status_action._recent_branch("m99", links, rows, blob, limit=40)
        self.assertTrue(chat["truncated"])
        self.assertEqual(len(chat["history"]["messages"]), 40)
        self.assertEqual(reads, [sorted(f"m{i}" for i in range(60, 100))])   # one read, those rows only
        titles = [r["title"] for r in openclaw_status_action._agent_call_rows(chat)]
        self.assertEqual(titles[0], "job 99")
        self.assertEqual(titles[-1], "job 61")

    async def test_a_short_chat_is_whole(self):
        links, rows, blob, _reads = _chain(6)
        chat = await openclaw_status_action._recent_branch("m5", links, rows, blob, limit=40)
        self.assertFalse(chat["truncated"])
        self.assertEqual(len(chat["history"]["messages"]), 6)

    async def test_a_message_with_no_row_yet_comes_from_the_chats_copy(self):
        links, rows, blob, reads = _chain(6, missing_rows={"m5"})
        chat = await openclaw_status_action._recent_branch("m5", links, rows, blob, limit=40)
        self.assertIn(("blob", "m5"), reads)
        self.assertEqual(len(chat["history"]["messages"]), 6)
        self.assertEqual(openclaw_status_action._agent_call_rows(chat)[0]["title"], "job 5")

    async def test_a_running_reply_lists_what_the_pipe_shows(self):
        """ELI-186: Open WebUI saves a reply's calls only when it ends, so a
        running reply's row holds none; the Pipe streaming it has them."""
        links, read_rows, blob, _reads = _chain(6)
        live = types.SimpleNamespace(output=_card("t5", "Agent", {"description": "background job"}, None))

        async def rows_mid_reply(ids):
            got = await read_rows(ids)
            got["m5"] = {**got["m5"], "done": False, "output": []}
            return got
        replies = openclaw_status_action._live_replies()
        with mock.patch.dict(replies, {("chat-1", "m5"): live}):
            chat = await openclaw_status_action._recent_branch("m5", links, rows_mid_reply, blob, chat_id="chat-1")
            self.assertEqual([(r["title"], r["state"], r["replyDone"])
                              for r in openclaw_status_action._agent_call_rows(chat)[:1]],
                             [("background job", "running", False)])
            # A reply that has ended is read as Open WebUI saved it; another
            # chat's reply is not this one.
            chat = await openclaw_status_action._recent_branch("m5", links, read_rows, blob, chat_id="chat-1")
            self.assertEqual(openclaw_status_action._agent_call_rows(chat)[0]["title"], "job 5")
            chat = await openclaw_status_action._recent_branch("m5", links, rows_mid_reply, blob, chat_id="chat-2")
            self.assertEqual(openclaw_status_action._agent_call_rows(chat)[0]["title"], "job 3")

    async def test_the_dialog_says_recent_when_it_cut(self):
        chats = types.ModuleType("open_webui.models.chats")
        whole = {}
        for i in range(60):
            role = "user" if i % 2 == 0 else "assistant"
            whole[f"m{i}"] = {"id": f"m{i}", "parentId": f"m{i - 1}" if i else None, "role": role,
                              "timestamp": 1000 + i, "done": True,
                              "output": _card(f"t{i}", "Agent", {"description": f"job {i}"}, "ok")
                              if role == "assistant" else []}
        chats.Chats = types.SimpleNamespace(get_chat_by_id=lambda chat_id: types.SimpleNamespace(
            user_id=PARENT_USER, chat={"history": {"currentId": "m59", "messages": whole}}))
        mods = {"open_webui": types.ModuleType("open_webui"),
                "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.chats": chats}
        with mock.patch.dict(sys.modules, mods):
            await SubagentListTests._open(self, [], {})
        self.assertEqual(self.agents_data["total"], 20)       # the 20 Agent calls in the last 40 messages
        self.assertEqual(self.agents_data["recent"], 40)


class SubagentSectionRenderTests(unittest.TestCase):
    """What the section shows for both groups, run in a fake page."""

    NOW = 1_783_790_100_000

    def _page(self, data, section="subagents"):
        from test_status_action_unit import _FAKE_DOM_JS, _render_modal_open_js

        page = _FAKE_DOM_JS + r"""
run(input.open);
run(input.fill);
const section = document.getElementById('openclaw-status-section-' + input.section);
console.log(JSON.stringify(section.walk().filter((e) => e.children.length === 0 && e.text)
  .map((e) => e.text)));
"""
        identity = {"chatId": "c", "messageId": "m", "sessionId": "s", "owuiModel": "x"}
        render = (openclaw_status_action._render_subagents_fill_js if section == "subagents"
                  else openclaw_status_action._render_agents_fill_js)
        payload = {"now": self.NOW, "open": _render_modal_open_js(identity), "fill": render(data),
                   "section": section}
        return run_page(page, payload)

    def test_both_groups_with_their_counts_and_states(self):
        task = {"id": CHILD_A, "title": "Research X", "status": "completed", "childSessionKey": CHILD_A,
                "startedAt": self.NOW - 65_000, "endedAt": self.NOW - 5_000, "terminalSummary": "X is fine"}
        agents = [{"id": "t6", "title": "Benchmark run", "state": "running", "startedAt": self.NOW - 30_000},
                  {"id": "t1", "title": "Tax research", "state": "launched", "startedAt": self.NOW - 600_000}]
        texts = self._page({"tasks": [task], "total": 21, "source": "pipe", "fetchedAt": self.NOW, "error": None})
        self.assertEqual(texts[:5], ["Subagents (1 of 21)", "Research X", "Done", "1m", "X is fine"])
        texts = self._page({"agents": agents, "total": 2, "fetchedAt": self.NOW, "error": None}, "agents")
        self.assertEqual(texts[:3], ["In-process agents (2)",
                                     "Claude Code Agent calls in this chat, as their tool cards show them.",
                                     "Benchmark run"])
        # Their states as chips (ELI-155).
        self.assertIn("Running", texts)
        self.assertIn("Background", texts)

    def test_nothing_to_show_is_a_folded_count_of_zero(self):
        """Every section keeps its title from the open on (ELI-163)."""
        self.assertEqual(self._page({"tasks": [], "total": 0, "source": "pipe", "fetchedAt": self.NOW,
                                     "error": None}), ["Subagents (0)", "No sub-agents in this chat."])
        self.assertEqual(self._page({"agents": [], "total": 0, "fetchedAt": self.NOW, "error": None}, "agents"),
                         ["In-process agents (0)", "No Claude Code Agent calls in this chat."])

    def test_a_long_chat_says_its_agents_are_the_recent_ones(self):
        agents = [{"id": "t6", "title": "Benchmark run", "state": "returned", "startedAt": self.NOW}]
        texts = self._page({"agents": agents, "total": 1, "recent": 40, "fetchedAt": self.NOW, "error": None},
                           "agents")
        self.assertEqual(texts[1], "Claude Code Agent calls in this chat's last 40 messages, "
                                   "as their tool cards show them.")
        texts = self._page({"agents": [], "total": 0, "recent": 40, "fetchedAt": self.NOW, "error": None},
                           "agents")
        self.assertEqual(texts[1], "No Claude Code Agent calls in this chat's last 40 messages.")

    def test_a_long_dialog_scrolls_instead_of_running_off_the_screen(self):
        """Up to 20 rows of each kind outgrow a phone screen: seen on the
        runner, the dialog's top and bottom were cut off with no scroll."""
        from test_status_action_unit import _FAKE_DOM_JS, _render_modal_open_js

        page = _FAKE_DOM_JS + r"""
run(input.open);
const panel = document.getElementById('openclaw-status-modal-root').children[0];
const body = document.getElementById('openclaw-status-sections');
const [head, , foot] = panel.children;
console.log(JSON.stringify({maxHeight: panel.style.maxHeight, display: panel.style.display,
  bodyScroll: body.style.overflowY, bodyMin: body.style.minHeight,
  pinned: [head.style.flexShrink, foot.style.flexShrink]}));
"""
        identity = {"chatId": "c", "messageId": "m", "sessionId": "s", "owuiModel": "x"}
        # The sections scroll; the title bar (with its close button) and
        # the footer stay in view.
        self.assertEqual(run_page(page, {"now": self.NOW, "open": _render_modal_open_js(identity)}),
                         {"maxHeight": "88vh", "display": "flex", "bodyScroll": "auto", "bodyMin": "0",
                          "pinned": ["0", "0"]})


if __name__ == "__main__":
    unittest.main()
