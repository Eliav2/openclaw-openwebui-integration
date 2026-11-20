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


class EchoOfSteeredMessages(unittest.TestCase):
    """Messages sent while a run works reach the model in one call: as separate user messages
    (OpenClaw 2026.10.10, 2026.11.5) or merged into one user message that holds each message
    after the other (2026.11.6). The scripted model answers the newest message either way."""

    def echo(self, *messages):
        return fake_llm.plan_reply({"messages": list(messages), "tools": TOOLS})["text"]

    def test_separate_user_messages_are_answered_with_the_newest_echo(self):
        self.assertEqual(self.echo(user("E2E_ECHO: A"), user("E2E_ECHO: B"), user("E2E_ECHO: C")), "C")

    def test_one_merged_user_message_is_answered_with_its_last_echo(self):
        merged = "[t] Conversation info\n\nE2E_ECHO: A [t] Conversation info\n\nE2E_ECHO: B [t] Conversation info\n\nE2E_ECHO: C"
        self.assertEqual(self.echo(user(merged)), "C")

    def test_one_echo_is_still_echoed_whole(self):
        self.assertEqual(self.echo(user("E2E_ECHO: שלום 🙂 <b>x</b>")), "שלום 🙂 <b>x</b>")


if __name__ == "__main__":
    unittest.main()
