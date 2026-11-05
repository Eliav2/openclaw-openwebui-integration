"""The screenshots stack's scripted model (scripts/screenshots/scenes.json) in the
turns OpenClaw 2026.10.10 builds around an active goal."""
import importlib
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENES = os.path.join(ROOT, "scripts", "screenshots", "scenes.json")

NUDGE = ("[Thu 2026-11-05 16:01 UTC] [System] Advance the active goal; keep it active until fully "
         "achieved. Use the current goal context, and verify the entire objective before marking it complete.")
CONTEXT = 'OpenClaw runtime context: Conversation data: "Active goal: Ship the hourly view to staging"'
BUILD = "[Thu 2026-11-05 16:01 UTC] Conversation info: ```json {} ```\n\nStart with the build."
OFFERED = ["exec", "create_goal"]


def load_fake_llm():
    """e2e/fake_llm.py reads its scenes when imported."""
    with mock.patch.dict(os.environ, {"FAKE_LLM_SCENES": SCENES, "FAKE_LLM_DEFAULT": "Sure."}):
        sys.path.insert(0, os.path.join(ROOT, "e2e"))
        try:
            sys.modules.pop("fake_llm", None)
            return importlib.import_module("fake_llm")
        finally:
            sys.path.pop(0)


def request(*messages):
    """A chat completion body: (role, text) pairs, and the tools offered."""
    return {"messages": [{"role": role, "content": text} for role, text in messages],
            "tools": [{"function": {"name": n}} for n in OFFERED]}


class GoalNudgeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fake = load_fake_llm()

    def test_the_nudge_is_answered_slowly_not_by_the_default(self):
        """A default answer came back at once and the Gateway ran its loop of
        nudge turns about 200 times in two minutes, which held back the scene's
        own message (usage-goal on 10.10). A word a second makes a turn take
        seconds, and with no tool call it leaves no tool result in the turn
        of the message after it."""
        plan = self.fake.plan_reply(request(("system", "s"), ("user", NUDGE), ("user", CONTEXT)))
        self.assertNotEqual(plan["text"], "Sure.")
        self.assertGreaterEqual(plan["delay"], 1.0)
        self.assertNotIn("tool", plan)

    def test_a_message_before_the_nudge_is_still_the_one_answered(self):
        """The newest part of a turn with a scripted answer wins: the nudge's
        scene is a fallback, or it would take the message before it."""
        plan = self.fake.plan_reply(request(("system", "s"), ("user", BUILD), ("user", CONTEXT),
                                            ("user", NUDGE), ("user", CONTEXT)))
        self.assertEqual(plan.get("tool"), "exec", plan)
        self.assertIn("compileall", json.dumps(plan))


if __name__ == "__main__":
    unittest.main()
