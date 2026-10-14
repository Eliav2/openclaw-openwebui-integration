#!/usr/bin/env python3
"""The Status Action's Subagents section and drawer (ELI-106).

They used to read the Gateway's Tasks API (`tasks.list`, `tasks.get`), which
OpenClaw removes in 2026.9.7. They now read the parent's child sessions
(`sessions.list` with `spawnedBy`), the task text from the child's transcript
head (`sessions.preview`), and the child session itself (`sessions.describe`,
`chat.history`): RPCs both 2026.9.3 and 2026.10.3 serve.
"""

import os
import unittest
from unittest import mock

import test_status_action_unit  # noqa: F401  (installs the module stubs)
from test_status_action_unit import _drawer_conn, _emitted_codes

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
            await Action().action({"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                                  __event_emitter__=emitter)
        return calls, emitted, seen[-1]

    async def test_lists_the_running_and_queued_children_with_their_task_as_title(self):
        rows = [
            {"key": CHILD_A, "status": "running", "startedAt": 1000, "lastMessagePreview": "halfway"},
            {"key": CHILD_B, "status": "queued", "label": "y-summary"},
            {"key": CHILD_DONE, "status": "done", "startedAt": 900, "endedAt": 990},
        ]
        previews = {CHILD_A: [{"role": "user", "text": WRAPPED_93}, {"role": "assistant", "text": "halfway"}],
                    CHILD_B: [{"role": "user", "text": WRAPPED_103}]}
        calls, emitted, data = await self._open(rows, previews)
        methods = [m for m, _p in calls]
        self.assertNotIn("tasks.list", methods)
        listed = next(p for m, p in calls if m == "sessions.list")
        self.assertEqual(listed["spawnedBy"], PARENT_KEY)
        self.assertIsNone(data["error"])
        self.assertEqual([t["id"] for t in data["tasks"]], [CHILD_A, CHILD_B])
        a, b = data["tasks"]
        self.assertEqual((a["title"], a["status"], a["childSessionKey"], a["startedAt"], a["progressSummary"]),
                         ("Research X and report.", "running", CHILD_A, 1000, "halfway"))
        self.assertEqual((b["title"], b["status"]), ("Summarise Y.", "queued"))
        # Rendered: the section's own fill carries both rows.
        code = next(c for c in _emitted_codes(emitted)
                    if "getElementById('openclaw-status-section-subagents')" in c)
        self.assertIn("Research X and report.", code)

    async def test_no_preview_call_without_active_children(self):
        calls, _emitted, data = await self._open(
            [{"key": CHILD_DONE, "status": "done", "endedAt": 990}], {})
        self.assertEqual(data["tasks"], [])
        self.assertNotIn("sessions.preview", [m for m, _p in calls])

    async def test_title_falls_back_to_label_then_key(self):
        rows = [{"key": CHILD_A, "status": "running", "label": "research-x"},
                {"key": CHILD_B, "status": "running"}]
        # A long-running child's preview no longer reaches its first message.
        previews = {CHILD_A: [{"role": "assistant", "text": "step 9"}], CHILD_B: []}
        _calls, _emitted, data = await self._open(rows, previews)
        self.assertEqual([t["title"] for t in data["tasks"]], ["research-x", CHILD_B])

    async def test_a_preview_failure_keeps_the_rows(self):
        rows = [{"key": CHILD_A, "status": "running", "label": "research-x"}]

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
            await Action().action({"chat_id": PARENT_CHAT}, __user__={"id": PARENT_USER},
                                  __event_emitter__=lambda e: emitted.append(e) or _noop())
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
