"""Cron wake turns on both OpenClaw lines (ELI-211).

9.3 records a wake's prompt as a user turn starting `[cron:<jobId> <name>]`
with no provenance, and runs the wake under the session's id (ELI-103).
10.2+ records it with the cron provenance and no header; `chat.history`
returns it as role "assistant" (GH #24), and the wake runs under its own run
id with its own `cli-assistant:<runId>` closing. The 10.5 fixture is the live
gate's cron-wake chat.

Run: python3 -m pytest -q tests/test_wake_turns_unit.py
"""
import json
import os
import unittest

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
FX105 = json.load(open(os.path.join(FIXTURES, "test_fixture_105_wake_history.json")))
GH24 = json.load(open(os.path.join(FIXTURES, "test_fixture_gh24_wake_history.json")))

# The wake's prompt is the fixture's only `<id>:user` message with cron provenance.
_PROMPT_105 = next(m for m in FX105["messages"] if (m.get("provenance") or {}).get("sourceTool") == "cron")
WAKE_105 = {"key": _PROMPT_105["idempotencyKey"].removesuffix(":user"), "at": 1792366183392,
            "job": "8a33deee-b3bd-4a03-bcb6-92e50525c5e7", "shared_run": False}


class WakeTurns105Tests(unittest.TestCase):
    def test_the_105_wake_is_a_wake_turn_as_the_pipe_reads_history(self):
        msgs = ocp._transcript_roles(FX105["messages"])
        self.assertEqual(ocp.wake_turns(msgs), [WAKE_105])

    def test_the_105_wake_is_a_wake_turn_in_raw_history(self):
        self.assertEqual(ocp.wake_turns(FX105["messages"]), [WAKE_105])

    def test_a_105_wake_is_never_bound_to_a_session_id_event(self):
        """Its events carry its own run id, so the sessionId wake path
        (`_wake_bind`) never owns it."""
        msgs = ocp._transcript_roles(FX105["messages"])
        self.assertIsNone(ocp.wake_turn_at(msgs, 1792366199785))
        self.assertIsNone(ocp.wake_turn_of_run(msgs, WAKE_105["job"], 1792366183000))

    def test_the_105_wake_run_renders_its_reply_only(self):
        """The wake's run is its closing's id, and its span holds the reply
        and its tool calls, not the prompt."""
        hist = {"messages": ocp._transcript_roles(FX105["messages"])}
        span = ocp._history_span_messages(hist, FX105["lastRunId"])
        rendered = ocp._history_renderer(span)
        self.assertEqual(rendered.text.strip(), "CW-DONE")
        self.assertEqual(sum(1 for i in rendered.finished_output() if i.get("type") == "function_call"), 3)


class WakeTurnsBothLinesTests(unittest.TestCase):
    def test_93_wakes_keep_their_header_and_shared_run(self):
        got = ocp.wake_turns(GH24["2026.9.3"]["messages"])
        self.assertEqual([(w["job"], w["shared_run"]) for w in got],
                         [("0740fb6d-d0e2-4df1-9784-dabb968652a3", True),
                          ("fb8b08d8-dee2-4064-8188-2d687c324ebe", True)])

    def test_103_wakes_come_from_their_provenance(self):
        got = ocp.wake_turns(ocp._transcript_roles(GH24["2026.10.3"]["messages"]))
        self.assertEqual([(w["job"], w["shared_run"]) for w in got],
                         [("612e012e-7ed3-43dc-b056-d70c0d591f00", False),
                          ("9dc3695c-e10e-4225-8908-4e1135181301", False)])
        self.assertEqual([w["key"] for w in got],
                         [m["idempotencyKey"].removesuffix(":user") for m in GH24["2026.10.3"]["messages"]
                          if (m.get("provenance") or {}).get("sourceTool") == "cron"])

    def test_a_sessions_send_turn_is_no_wake(self):
        m = {"role": "assistant", "content": "[cron:job-1 x] forwarded", "idempotencyKey": "s1:user",
             "provenance": {"kind": "inter_session", "sourceTool": "sessions_send"}}
        self.assertEqual(ocp.wake_turns([m]), [])
        self.assertEqual(ocp.wake_turns(ocp._transcript_roles([m])), [])


if __name__ == "__main__":
    unittest.main()
