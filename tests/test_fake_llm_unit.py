"""The E2E fake model's scenes when a message is steered into a running scene
(OpenClaw 2026.10.10: in the same request as the tool's result)."""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("fake_llm", ROOT / "e2e" / "fake_llm.py")
fake_llm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fake_llm)

SCENES = [
    {"match": "SCENE_A", "steps": [{"say": "a1", "tool": "exec"}, {"say": "a done"}]},
    {"match": "SCENE_B", "steps": [{"say": "b1", "tool": "exec"}, {"say": "b done"}]},
    {"match": "SCENE_C", "steps": [{"say": "c done"}]},
]
TOOLS = [{"function": {"name": "exec"}}]


def user(text):
    return {"role": "user", "content": text}


def call():
    return {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]}


def result():
    return {"role": "tool", "content": "ok"}


class SteeredScenes(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, fake_llm, "SCENES", fake_llm.SCENES)
        fake_llm.SCENES = SCENES

    def plan(self, *messages):
        return fake_llm.plan_reply({"messages": list(messages), "tools": TOOLS})

    def test_a_steered_scene_without_a_tool_is_answered_with_the_running_one(self):
        self.assertEqual(self.plan(user("SCENE_A"), call(), result(), user("SCENE_C"))["text"],
                         "a done\n\nc done")

    def test_a_steered_scene_s_first_tool_call_is_made(self):
        plan = self.plan(user("SCENE_A"), call(), result(), user("SCENE_B"))
        self.assertEqual((plan["text"], plan.get("tool")), ("a done\n\nb1", "exec"))

    def test_the_steered_scene_counts_only_its_own_results(self):
        plan = self.plan(user("SCENE_A"), call(), result(), user("SCENE_B"), call(), result())
        self.assertEqual((plan["text"], plan.get("tool")), ("b done", None))


if __name__ == "__main__":
    unittest.main()
