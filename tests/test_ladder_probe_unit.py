"""The run-state probe files the thinking ladder it reads from describe once per
turn, not on every call. A queued message probes every 1.5 s for up to 30
minutes, and filing reads and writes the ladder cache file on the event loop
(review on #3).

Run: uv run --with-requirements requirements-dev.txt pytest -q tests/test_ladder_probe_unit.py
"""
import tempfile
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402


def describe(model="opus", levels=("off", "high")):
    return {"session": {"status": "running", "modelProvider": "anthropic", "model": model,
                        "thinkingLevels": [{"id": lv, "label": lv} for lv in levels]}}


class LadderFiledOncePerTurn(unittest.IsolatedAsyncioTestCase):

    def turn(self):
        turn = ocp._PipeTurn.__new__(ocp._PipeTurn)
        turn.session_key = "agent:main:openwebui-u-c"
        turn.pipe = types.SimpleNamespace(valves=types.SimpleNamespace(STATE_DIR=""))
        turn.session_runtime = None
        turn.session_thinking_ladder = None
        turn.session_model_key = None
        turn._ladder_recorded = None
        self.answers = []

        async def send_request(method, params, timeout=None):
            return self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        turn.conn = types.SimpleNamespace(send_request=send_request)
        return turn

    async def probe(self, turn, *answers, results=()):
        filed = []
        self.answers = list(answers)
        outcomes = iter(results)

        def record(levels, model_key=None, **k):
            filed.append((model_key, list(levels)))
            return next(outcomes, True)

        with mock.patch.object(ocp, "_record_thinking_ladder", record):
            for _ in answers:
                self.assertTrue(await turn._session_run_active())
        return filed

    async def test_the_same_ladder_is_filed_once_across_queue_polls(self):
        turn = self.turn()
        filed = await self.probe(turn, *[describe()] * 5)
        self.assertEqual(filed, [("anthropic/opus", ["off", "high"])])
        self.assertEqual(turn.session_thinking_ladder, ["off", "high"])

    async def test_a_changed_ladder_or_model_is_filed_again(self):
        turn = self.turn()
        filed = await self.probe(turn, describe(), describe(levels=("off", "low", "high")),
                                 describe(model="sonnet", levels=("off", "low", "high")), describe(model="sonnet",
                                                                                                    levels=("off", "low", "high")))
        self.assertEqual(filed, [("anthropic/opus", ["off", "high"]),
                                 ("anthropic/opus", ["off", "low", "high"]),
                                 ("anthropic/sonnet", ["off", "low", "high"])])

    async def test_a_failed_write_is_retried_by_the_next_probe(self):
        turn = self.turn()
        filed = await self.probe(turn, *[describe()] * 4, results=(False, False, True))
        self.assertEqual(filed, [("anthropic/opus", ["off", "high"])] * 3)

    async def test_the_recorder_reports_failure_and_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(ocp._record_thinking_ladder(["off", "high"], model_key="m", state_dir=d))
            self.assertTrue(ocp._record_thinking_ladder(["off", "high"], model_key="m", state_dir=d))
            with mock.patch.object(ocp, "_write_json_file", return_value=False):
                self.assertFalse(ocp._record_thinking_ladder(["off", "low", "high"], model_key="m", state_dir=d))


if __name__ == "__main__":
    unittest.main()
