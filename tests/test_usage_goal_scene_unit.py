"""capture.py: the usage-goal scene ends the session's goal at the Gateway (ELI-361,
review on #348): the session is found by key past the first page, the wait is for an
idle row that was seen, and a failed reply wait clears the goal too."""
import time
import unittest
from unittest import mock

from _paged_sessions import PagedSessions, crowd
from test_screenshot_selection_unit import capture

KEY = "agent:main:openwebui-user-me-chat1"
REAL_SLEEP = time.sleep   # the patched one in the tests below does nothing


class FakeClock:
    """capture's `time`: sleep advances it, nothing else does."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    monotonic = time

    def sleep(self, seconds):
        self.now += seconds


class Gateway:
    """sessions.list and sessions.goal.clear over a PagedSessions: a clear ends the
    goal at once, the session's last turn a few reads later."""

    def __init__(self, *, goal=True, busy_reads=2, rows=None, read_s=0.0):
        row = {"key": KEY, "hasActiveRun": True, "status": "running"}
        if goal:
            row["goal"] = {"id": "g1", "status": "active"}
        self.sessions = PagedSessions(rows if rows is not None else crowd(KEY, busy=True))
        self.row = next(r for r in self.sessions.rows if r["key"] == KEY)
        self.row.update(row)
        self.busy_reads, self.cleared, self.read_s = busy_reads, [], read_s

    def __call__(self, method, params):
        if method == "sessions.goal.clear":
            self.cleared.append(params)
            self.row.pop("goal", None)
            return {}
        REAL_SLEEP(self.read_s)   # a Gateway that takes real time to answer, as on a busy CI runner
        out = self.sessions.list(params)
        if not self.row.get("goal") and self.row["hasActiveRun"] and "search" in params:
            self.busy_reads -= 1
            if self.busy_reads <= 0:
                self.row["hasActiveRun"] = False
        return out


class UsageGoalScene(unittest.TestCase):

    def setUp(self):
        # A clock that only the waiters' own sleeps move: the wait ends by the number of polls it made, never by
        # how long the machine took (a 50 ms wall-clock deadline timed the wait out on a busy CI runner).
        clock = FakeClock()
        for patch in (mock.patch.object(capture, "time", clock), mock.patch.object(capture, "TURN_TIMEOUT_MS", 60_000)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_session_is_found_by_its_key_past_the_first_page(self):
        gateway = Gateway()
        with mock.patch.object(capture, "gateway_call", gateway):
            self.assertEqual(capture.session_row(KEY)["key"], KEY)
        self.assertTrue(all("search" in c for c in gateway.sessions.calls))

    def test_the_goal_is_cleared_and_the_wait_is_for_an_idle_row(self):
        gateway = Gateway(busy_reads=3)
        with mock.patch.object(capture, "gateway_call", gateway):
            capture.end_goal(KEY)
        self.assertEqual([c["goalId"] for c in gateway.cleared], ["g1"])
        self.assertFalse(gateway.row["hasActiveRun"])

    def test_a_slow_gateway_does_not_run_the_wait_out(self):
        # Three reads of 30 ms each: the wait is counted in polls (the fake clock), never in the wall clock.
        gateway = Gateway(busy_reads=3, read_s=0.03)
        with mock.patch.object(capture, "gateway_call", gateway):
            capture.end_goal(KEY)
        self.assertFalse(gateway.row["hasActiveRun"])

    def test_no_row_is_not_an_idle_session(self):
        with mock.patch.object(capture, "gateway_call", lambda m, p: {"sessions": []}):
            with self.assertRaises(TimeoutError):
                capture.end_goal(KEY)

    def test_a_session_that_never_had_a_goal_is_waited_on_without_a_clear(self):
        gateway = Gateway(goal=False, busy_reads=1)
        with mock.patch.object(capture, "gateway_call", gateway):
            capture.end_goal(KEY)
        self.assertEqual(gateway.cleared, [])

    def test_a_failed_reply_wait_still_ends_the_goal(self):
        scenes = capture.Scenes.__new__(capture.Scenes)
        scenes.token = "t"
        ended = []
        with mock.patch.object(capture, "new_chat"), \
                mock.patch.object(capture, "send", return_value="chat1"), \
                mock.patch.object(capture, "api", return_value={"id": "user-me"}), \
                mock.patch.object(capture, "reply_with", return_value="answer"), \
                mock.patch.object(capture, "reply_done_js", return_value="js"), \
                mock.patch.object(capture, "wait_js", side_effect=TimeoutError("no reply")), \
                mock.patch.object(capture, "end_goal", side_effect=ended.append):
            with self.assertRaises(TimeoutError):
                scenes.usage_goal(object())
        self.assertEqual(ended, [KEY])


if __name__ == "__main__":
    unittest.main()
