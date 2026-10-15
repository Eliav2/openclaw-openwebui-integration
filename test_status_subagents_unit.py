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
        raise AssertionError(f"unexpected RPC: {method}")
    return send_request


def _child(key, status, **kw):
    """A child session row as the Gateway's full `sessions.list` has it."""
    return {"key": key, "status": status, "spawnedBy": PARENT_KEY, **kw}


class SubagentListTests(unittest.IsolatedAsyncioTestCase):

    async def _open(self, rows, previews):
        calls, emitted, seen = [], [], []

        async def emitter(evt):
            emitted.append(evt)

        conn = mock.AsyncMock()
        conn.send_request = mock.AsyncMock(side_effect=_list_rpc(rows, previews, calls))
        real = openclaw_status_action._render_subagents_fill_js

        def spy(data):
            seen.append(data)
            return real(data)

        with mock.patch("openclaw_status_action._get_action_connection",
                        new=mock.AsyncMock(return_value=(conn, "pipe"))), \
             mock.patch("openclaw_status_action._render_subagents_fill_js", side_effect=spy):
            await _open_dialog(Action().action({"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                                               __event_emitter__=emitter), conn)
        return calls, emitted, seen[-1]

    async def test_lists_every_child_with_its_task_as_title_newest_first(self):
        rows = [
            _child(CHILD_DONE, "done", startedAt=900, endedAt=990),
            _child(CHILD_A, "running", startedAt=1000, lastMessagePreview="halfway"),
            _child(CHILD_B, "queued", label="y-summary", createdAt=1100),
        ]
        previews = {CHILD_A: [{"role": "user", "text": WRAPPED_93}, {"role": "assistant", "text": "halfway"}],
                    CHILD_B: [{"role": "user", "text": WRAPPED_103}]}
        calls, emitted, data = await self._open(rows, previews)
        methods = [m for m, _p in calls]
        self.assertNotIn("tasks.list", methods)
        self.assertIsNone(data["error"])
        self.assertEqual([t["id"] for t in data["tasks"]], [CHILD_B, CHILD_A, CHILD_DONE])
        b, a, done = data["tasks"]
        self.assertEqual((a["title"], a["status"], a["childSessionKey"], a["startedAt"], a["progressSummary"]),
                         ("Research X and report.", "running", CHILD_A, 1000, "halfway"))
        self.assertEqual((b["title"], b["status"]), ("Summarise Y.", "queued"))
        self.assertEqual((done["status"], done["startedAt"], done["endedAt"]), ("completed", 900, 990))
        # Rendered: the section's own fill carries the rows.
        code = next(c for c in _emitted_codes(emitted)
                    if "getElementById('openclaw-status-section-subagents')" in c)
        self.assertIn("Research X and report.", code)

    async def test_the_open_reads_every_session_not_the_30_minute_child_window(self):
        """ELI-152: `sessions.list {spawnedBy}` keeps an ended child for 30
        minutes only (the Gateway's shouldKeepStoreOnlyChildLink), so the
        dialog's open reads the whole list and picks the chat's children by
        `spawnedBy` or `parentSessionKey` itself."""
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
        calls, _emitted, data = await self._open(rows, {})
        opened = next(p for m, p in calls if m == "sessions.list")
        self.assertNotIn("spawnedBy", opened)
        self.assertEqual([(t["id"][-4:], t["status"]) for t in data["tasks"]],
                         [("0002", "failed"), ("0004", "ended"), ("0001", "completed")])
        self.assertEqual(data["tasks"][0]["error"], "provider 529")
        self.assertEqual(data["tasks"][1]["title"], "W1: engine")
        self.assertEqual(data["total"], 3)

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
        _calls, _emitted, data = await self._open(
            [_child(CHILD_DONE, "done", startedAt=900, endedAt=990, lastMessagePreview="Y is fine")],
            {CHILD_DONE: [{"role": "user", "text": WRAPPED_103}, {"role": "assistant", "text": "Y is fine"}]})
        (t,) = data["tasks"]
        self.assertEqual((t["title"], t["terminalSummary"]), ("Summarise Y.", "Y is fine"))

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
        with mock.patch("openclaw_status_action._get_action_connection",
                        new=mock.AsyncMock(return_value=(conn, "pipe"))), \
             mock.patch("openclaw_status_action._render_subagents_fill_js",
                        side_effect=lambda d: (seen.append(d), real(d))[1]):
            await _open_dialog(Action().action({"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                                               __event_emitter__=lambda e: emitted.append(e) or _noop()), conn)
        self.assertEqual([t["title"] for t in seen[-1]["tasks"]], ["research-x"])


async def _noop():
    return None


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

    async def _drawer(self, task_id, send_request):
        emitted = []

        async def emitter(evt):
            emitted.append(evt)

        conn = _drawer_conn(send_request)
        with mock.patch("openclaw_status_action._get_action_connection",
                        new=mock.AsyncMock(return_value=(conn, "pipe"))), \
             mock.patch("openclaw_status_action.asyncio.sleep", new=mock.AsyncMock()):
            result = await Action()._run_subagent_detail(
                {"mode": "subagent-detail", "taskId": task_id, "taskTitle": "Research X", "taskStatus": "running"},
                {"id": PARENT_USER}, emitter)
        return result, emitted, conn

    async def test_reads_the_child_session_until_it_ends(self):
        n = {"describe": 0}

        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                self.assertEqual(params["key"], CHILD_A)
                n["describe"] += 1
                done = n["describe"] > 1
                return {"session": {"key": CHILD_A, "status": "done" if done else "running",
                                    "startedAt": 1783790000000, "endedAt": 1783790005000 if done else None,
                                    "modelProvider": "anthropic", "model": "claude-sonnet-5",
                                    "contextTokens": 1000000, "totalTokens": 5000}}
            if method == "chat.history":
                self.assertEqual(params["sessionKey"], CHILD_A)
                return {"messages": [{"role": "user", "content": WRAPPED_93},
                                     {"role": "assistant", "content": [{"type": "text", "text": "X is fine"}]}]}
            raise AssertionError(f"unexpected RPC: {method}")

        result, emitted, conn = await self._drawer(CHILD_A, send_request)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(n["describe"], 2)   # polled running -> done, then stopped
        methods = [c.args[0] for c in conn.send_request.call_args_list]
        self.assertNotIn("tasks.get", methods)
        overview = [c for c in _emitted_codes(emitted) if "openclaw-drawer-section-overview" in c][-1]
        self.assertIn('"status": "completed"', overview)
        self.assertIn('"terminalSummary": "X is fine"', overview)
        self.assertIn('"endedAt": 1783790005000', overview)
        self.assertIn(f'"childSessionKey": "{CHILD_A}"', overview)

    async def test_the_transcript_opens_with_the_task_not_the_spawn_wrapper(self):
        """The child's first user message is the Gateway's spawn wrapper; the
        Transcript shows the task in it, the way the Subagents row does
        (ELI-119), on both versions and both content shapes."""
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
                    transcript = [c for c in _emitted_codes(emitted)
                                  if "openclaw-drawer-section-transcript" in c][-1]
                    self.assertIn(f'{{"role": "user", "text": "{task}"}}', transcript)
                    self.assertNotIn("Subagent", transcript)
                    self.assertNotIn("OPENCLAW_INTERNAL_CONTEXT", transcript)

    def test_a_user_message_that_is_not_a_spawn_stays_as_written(self):
        rows, _tools = openclaw_status_action._extract_transcript_and_tools(
            [{"role": "user", "content": "Mention [Subagent Task] in passing."},
             {"role": "assistant", "content": WRAPPED_93}])
        self.assertEqual([r["text"] for r in rows], ["Mention [Subagent Task] in passing.", WRAPPED_93])

    async def test_a_failed_child_shows_its_error(self):
        async def send_request(method, params, timeout=5):
            if method == "sessions.describe":
                return {"session": {"key": CHILD_A, "status": "failed", "startedAt": 1, "endedAt": 2,
                                    "lastRunError": "provider 529"}}
            if method == "chat.history":
                return {"messages": []}
            raise AssertionError(f"unexpected RPC: {method}")

        _result, emitted, _conn = await self._drawer(CHILD_A, send_request)
        overview = [c for c in _emitted_codes(emitted) if "openclaw-drawer-section-overview" in c][-1]
        self.assertIn('"error": "provider 529"', overview)

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

    def test_the_status_action_never_calls_the_tasks_api(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "openclaw_status_action.py"), encoding="utf-8") as f:
            source = f.read()
        for method in ('"tasks.list"', '"tasks.get"'):
            self.assertNotIn(method, source)


if __name__ == "__main__":
    unittest.main()


class FinishedWhileOpenTests(unittest.IsolatedAsyncioTestCase):
    """ELI-127: the dialog's ticker counts a running child up (ELI-120), and
    the dialog filled once, so a child that finished while it stayed open
    kept its "running" dot and counted up until the dialog was reopened.
    The section refills when a listed child's run state changes: the
    Gateway's pushed `sessions.changed`, no polling."""

    NOW = 1_783_790_100_000

    async def test_a_child_that_finishes_stops_counting_in_the_open_dialog(self):
        import asyncio
        import json
        import shutil
        import subprocess
        import tempfile
        from test_status_action_unit import _DIALOG_PAGE_JS, _render_modal_open_js

        runtime = shutil.which("bun") or shutil.which("node")
        if runtime is None:
            self.skipTest("no bun or node")
        rows = [_child(CHILD_A, "running", startedAt=self.NOW - 1000)]
        previews = {CHILD_A: [{"role": "user", "text": WRAPPED_93}]}
        waiters, emitted = {}, []

        def session_change_event(key):
            # As the connection does: one Event per session, popped and set
            # by the next pushed change to its run state.
            return waiters.setdefault(key, asyncio.Event())

        async def emitter(evt):
            emitted.append(evt)

        conn = mock.Mock()
        conn.send_request = mock.AsyncMock(side_effect=_list_rpc(rows, previews, []))
        conn.session_change_event = session_change_event
        with mock.patch("openclaw_status_action._get_action_connection",
                        new=mock.AsyncMock(return_value=(conn, "pipe"))):
            opened = asyncio.ensure_future(Action().action(
                {"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER}, __event_emitter__=emitter))
            # Until the dialog is filled and follows its child (or, without
            # the follow, has returned).
            while not opened.done() and CHILD_A not in waiters:
                await asyncio.sleep(0)
            rows[0] = _child(CHILD_A, "done", startedAt=self.NOW - 1000, endedAt=self.NOW)
            ev = waiters.pop(CHILD_A, None)
            if ev is not None:
                ev.set()    # the Gateway pushes the child's settle
            await asyncio.wait_for(opened, 5)   # bounds the test only

        fills = [c for c in _emitted_codes(emitted)
                 if "getElementById('openclaw-status-section-subagents')" in c]
        identity = {"chatId": "c", "messageId": "m", "sessionId": "s", "owuiModel": "x"}
        payload = {"now": self.NOW, "open": _render_modal_open_js(identity), "fills": fills,
                   "steps": [{"advance": 60}]}
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(_DIALOG_PAGE_JS)
        try:
            out = subprocess.run([runtime, fh.name], input=json.dumps(payload),
                                 capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(fh.name)
        self.assertEqual(out.returncode, 0, out.stderr)
        # A minute later the ended child is still listed (ELI-152), with
        # its own duration, not counting up.
        self.assertEqual(json.loads(out.stdout), [{"cells": ["1s"], "timers": 1}])


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
        self.assertEqual([a["title"] for a in data["agents"]],
                         ["Benchmark run", "Venue research", "Strategy research", "Broker research", "Tax research"])
        self.assertEqual(data["agentsTotal"], 5)

    async def test_another_users_chat_lists_no_agents(self):
        chats = types.ModuleType("open_webui.models.chats")
        chats.Chats = types.SimpleNamespace(
            get_chat_by_id=lambda chat_id: types.SimpleNamespace(user_id="someone-else", chat=_agents_chat()))
        mods = {"open_webui": types.ModuleType("open_webui"),
                "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.chats": chats}
        with mock.patch.dict(sys.modules, mods):
            _calls, _emitted, data = await SubagentListTests._open(self, [], {})
        self.assertEqual(data["agents"], [])


class SubagentSectionRenderTests(unittest.TestCase):
    """What the section shows for both groups, run in a fake page."""

    NOW = 1_783_790_100_000

    def _page(self, data):
        import shutil
        import subprocess
        import tempfile
        from test_status_action_unit import _FAKE_DOM_JS, _render_modal_open_js

        runtime = shutil.which("bun") or shutil.which("node")
        if runtime is None:
            self.skipTest("no bun or node")
        page = _FAKE_DOM_JS + r"""
run(input.open);
run(input.fill);
const section = document.getElementById('openclaw-status-section-subagents');
console.log(JSON.stringify(section.walk().filter((e) => e.children.length === 0 && e.text)
  .map((e) => e.text)));
"""
        identity = {"chatId": "c", "messageId": "m", "sessionId": "s", "owuiModel": "x"}
        payload = {"now": self.NOW, "open": _render_modal_open_js(identity),
                   "fill": openclaw_status_action._render_subagents_fill_js(data)}
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(page)
        try:
            out = subprocess.run([runtime, fh.name], input=json.dumps(payload),
                                 capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(fh.name)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_both_groups_with_their_counts_and_states(self):
        task = {"id": CHILD_A, "title": "Research X", "status": "completed", "childSessionKey": CHILD_A,
                "startedAt": self.NOW - 65_000, "endedAt": self.NOW - 5_000, "terminalSummary": "X is fine"}
        agents = [{"id": "t6", "title": "Benchmark run", "state": "running", "startedAt": self.NOW - 30_000},
                  {"id": "t1", "title": "Tax research", "state": "launched", "startedAt": self.NOW - 600_000}]
        texts = self._page({"tasks": [task], "total": 21, "agents": agents, "agentsTotal": 2,
                            "source": "pipe", "fetchedAt": self.NOW, "error": None})
        self.assertEqual(texts[:4], ["Subagents (1 of 21)", "Research X", "X is fine", "1m"])
        self.assertIn("In-process agents (2)", texts)
        rest = texts[texts.index("In-process agents (2)") + 1:]
        self.assertEqual(rest[:2], ["Claude Code Agent calls in this chat, as their tool cards show them.",
                                    "Benchmark run"])
        self.assertIn("running", rest)
        self.assertIn("launched in the background", rest)

    def test_nothing_to_show_hides_the_section(self):
        self.assertEqual(self._page({"tasks": [], "total": 0, "agents": [], "agentsTotal": 0,
                                     "source": "pipe", "fetchedAt": self.NOW, "error": None}), [])
