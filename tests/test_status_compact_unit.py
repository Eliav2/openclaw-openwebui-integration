#!/usr/bin/env python3
"""The Status dialog's Compact: it sends `/compact` and waits for the run to end. The end is the
session row's terminal `status` up to OpenClaw 2026.11.3 (the row keeps its last run's `done`). From
2026.11.5 a compaction clears the row's run fields (no `status`, no `endedAt`), so the end is the
compact run's own reply in the chat history."""

import sys
import types
import unittest
from unittest import mock

import test_status_action_unit  # noqa: F401  (installs the module stubs)
from _test_helpers import action_gateway, RecordingEmitter

import openclaw_status_action as action
from openclaw_status_action import Action

USER, CHAT = "11111111-1111-1111-1111-111111111111", "chat-1"
KEY = f"agent:main:openwebui-{USER}-{CHAT}"
REPLY = "⚙️ Compacted (15 → 66) • Context 66/200k (0%)"
REFUSAL = "⚙️ Compaction failed: the model refused • Context 15/200k (0%)"


def _history(reply, key_holder):
    """The history rows of a compaction, its own reply last (or not there yet)."""
    rows = [{"role": "user", "content": [{"type": "text", "text": "E2E_ECHO: x"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
            {"role": "system", "content": [{"type": "text", "text": "Compaction"}]},
            {"role": "user", "content": [{"type": "text", "text": "/compact"}],
             "idempotencyKey": f"{key_holder['key']}:user"}]
    if reply:
        rows.append({"role": "assistant", "content": [{"type": "text", "text": reply}]})
    return {"messages": rows}


def _gateway(describe, replies):
    """`describe`: the session rows `sessions.describe` answers in turn (the last repeats);
    `replies`: the compact reply `chat.history` shows at each read (None = not yet, an Exception =
    the read fails)."""
    sent, reads = {}, {"describe": 0, "history": 0}

    async def send_request(method, params, timeout=10):
        if method == "sessions.describe":
            row = describe[min(reads["describe"], len(describe) - 1)]
            reads["describe"] += 1
            return {"session": row}
        if method == "chat.history":
            reply = replies[min(reads["history"], len(replies) - 1)]
            reads["history"] += 1
            if isinstance(reply, Exception):
                raise reply
            return _history(reply, sent)
        raise AssertionError(f"unexpected RPC: {method}")

    conn = mock.Mock()
    conn.send_request = mock.AsyncMock(side_effect=send_request)
    conn.active_run_id_for_session = lambda _key: None
    conn._valves = lambda: types.SimpleNamespace(OWNER_USERS="")
    return conn, sent, reads


class CompactTests(unittest.IsolatedAsyncioTestCase):

    async def _compact(self, describe, replies):
        conn, sent, reads = _gateway(describe, replies)
        emitted, status_calls = [], []

        async def run_request(_conn, _method, params, **_kw):
            sent["key"] = params["idempotencyKey"]
            return {"runId": params["idempotencyKey"]}

        async def run_status(self_, body, user, emitter, **kw):
            status_calls.append(kw)
            return {"status": "ok"}

        with action_gateway(conn), mock.patch.object(action, "_run_request", run_request), \
                mock.patch.object(Action, "_run_status", run_status), \
                mock.patch.object(action, "_COMPACT_POLL_S", 0), mock.patch.object(action, "_COMPACT_DEADLINE_S", 0.3), \
                mock.patch.object(action, "_COMPACT_REPLY_WAIT_S", 0.3):
            result = await Action().action({"chat_id": CHAT, "mode": "compact"}, __user__={"id": USER},
                                           __event_emitter__=RecordingEmitter(emitted))
        return result, status_calls, emitted

    async def test_on_2026_11_3_the_rows_terminal_status_ends_the_wait(self):
        result, status_calls, _ = await self._compact([{"status": "done", "hasActiveRun": False}], [REPLY])
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(status_calls, [{"show_loading": False}])

    async def test_on_2026_11_5_a_row_without_status_ends_with_the_compact_reply(self):
        """The row has no status after a compaction: the wait ends when the run's reply is there,
        not 180 s later with "Compact timed out"."""
        result, status_calls, emitted = await self._compact(
            [{"hasActiveRun": False}], [None, None, REPLY])
        self.assertEqual(result, {"status": "ok"}, emitted)
        self.assertEqual(status_calls, [{"show_loading": False}])

    async def test_on_2026_11_5_a_refused_compaction_still_says_why(self):
        result, status_calls, _ = await self._compact([{"hasActiveRun": False}], [None, REFUSAL])
        self.assertEqual(result["status"], "error")
        self.assertEqual(status_calls, [{"show_loading": False, "notice": "Compaction failed: the model refused"}])

    async def test_the_reply_that_ended_the_wait_is_the_one_checked_for_a_refusal(self):
        """A second history read could fail and drop the notice: the poll's own read is the answer."""
        result, status_calls, _ = await self._compact(
            [{"hasActiveRun": False}], [None, REFUSAL, RuntimeError("history down")])
        self.assertEqual(result["status"], "error")
        self.assertEqual(status_calls, [{"show_loading": False, "notice": "Compaction failed: the model refused"}])

    async def test_a_done_row_before_the_reply_is_in_the_history_still_shows_the_refusal(self):
        """The row says "done" while the run's reply is not committed yet: wait for it, so a
        refused compaction is not shown as a compacted one."""
        result, status_calls, _ = await self._compact(
            [{"status": "done", "hasActiveRun": False}], [None, None, REFUSAL])
        self.assertEqual(result["status"], "error")
        self.assertEqual(status_calls, [{"show_loading": False, "notice": "Compaction failed: the model refused"}])

    async def test_a_done_row_whose_reply_never_comes_is_taken_as_compacted(self):
        result, status_calls, _ = await self._compact([{"status": "done", "hasActiveRun": False}], [None])
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(status_calls, [{"show_loading": False}])

    async def test_a_failed_poll_read_is_polled_again(self):
        result, status_calls, emitted = await self._compact(
            [{"hasActiveRun": False}], [RuntimeError("history down"), REFUSAL])
        self.assertEqual(result["status"], "error", emitted)
        self.assertEqual(status_calls, [{"show_loading": False, "notice": "Compaction failed: the model refused"}])

    async def test_on_2026_11_5_a_stopped_timed_out_or_cut_compaction_ends_the_wait_at_once(self):
        """ELI-512: 2026.11.5 ends such a run `killed`, `timeout` or `interrupted` (its enum has no
        `cancelled`): the dialog says so, instead of waiting out the deadline for "timed out"."""
        for status in ("killed", "timeout", "interrupted"):
            with self.subTest(status=status):
                result, status_calls, _ = await self._compact(
                    [{"status": status, "hasActiveRun": False}], [None])
                self.assertEqual(result, {"status": "error", "detail": status})
                self.assertEqual(status_calls, [])

    async def test_a_compaction_that_never_ends_times_out(self):
        result, status_calls, _ = await self._compact([{"hasActiveRun": False}], [None])
        self.assertEqual(result, {"status": "error", "detail": "timeout"})
        self.assertEqual(status_calls, [])


class SessionLiveTests(unittest.TestCase):
    """The Rate limits note "a response is in progress" (ELI-512): 2026.11.5 reads `queued` while
    it starts a run, and `hasActiveRun` is the row's own answer."""

    def test_a_starting_or_running_run_is_live_and_an_ended_one_is_not(self):
        for row, live in (({"status": "running"}, True), ({"status": "queued", "hasActiveRun": True}, True),
                          ({"status": None, "hasActiveRun": True}, True), ({"status": "queued"}, True),
                          ({"status": "killed", "hasActiveRun": False}, False), ({}, False)):
            with self.subTest(row=row):
                self.assertIs(action._session_row_live(row), live)


if __name__ == "__main__":
    unittest.main()
