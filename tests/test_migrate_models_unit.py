#!/usr/bin/env python3
"""Unit tests for scripts/migrate_stale_chat_models.py (successor resolution)."""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "migrate_stale_chat_models",
    os.path.join(ROOT, "scripts", "migrate_stale_chat_models.py"),
)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)

PIPE = "openclaw_gateway"

# The live selector on 2026-10-11, right after the Gateway dropped the older
# Claude models.
LIVE = {PIPE + "." + k for k in (
    "default",
    "anthropic/claude-haiku-5-5",
    "anthropic/claude-opus-5-5",
    "anthropic/claude-sonnet-5-5",
    "deepinfra/zai-org/GLM-5.2",
    "deepseek/deepseek-v4-flash",
    "google/gemini-3-flash-preview",
    "openai/gpt-5.5",
    "openrouter/z-ai/glm-5.2",
)} | {"llama3.2:1b", "arena-model"}


def _chat(cid, *models):
    return {"id": cid, "title": cid, "chat": {"models": list(models)}}


class ResolveSuccessorTests(unittest.TestCase):
    def setUp(self):
        self.live = {m[len(PIPE) + 1:] for m in LIVE if m.startswith(PIPE + ".")}

    def succ(self, stale):
        return mig.resolve_successor(stale, self.live)[0]

    def test_every_stale_id_seen_on_the_live_instance(self):
        expected = {
            "claude-cli/claude-opus-5": "anthropic/claude-opus-5-5",
            "anthropic/claude-opus-5": "anthropic/claude-opus-5-5",
            "anthropic/claude-opus-4-8": "anthropic/claude-opus-5-5",
            "anthropic/claude-sonnet-5": "anthropic/claude-sonnet-5-5",
            "anthropic/claude-haiku-4-5-20251001": "anthropic/claude-haiku-5-5",
            "claude-cli/claude-opus-5-5": "anthropic/claude-opus-5-5",
            "claude-cli/claude-haiku-5-5": "anthropic/claude-haiku-5-5",
            "claude-cli/claude-sonnet-5-5": "anthropic/claude-sonnet-5-5",
            # Fable is not offered any more, and there is no sibling to pick.
            "anthropic/claude-fable-5": "default",
            # Legacy presets expand to the key they always meant first.
            "sonnet": "anthropic/claude-sonnet-5-5",
            "opus": "anthropic/claude-opus-5-5",
            "chatgpt": "openai/gpt-5.5",
            "glm": "openrouter/z-ai/glm-5.2",
            "kimi": "default",
            "": "default",
        }
        for stale, want in expected.items():
            with self.subTest(stale=stale):
                self.assertEqual(self.succ(stale), want)

    def test_next_model_bump_needs_no_code_change(self):
        # Same stale ids, a Gateway that has moved on to 6.x: picks the new
        # versions without anything in the script naming them.
        bumped = {"default", "anthropic/claude-opus-6", "anthropic/claude-sonnet-6-1",
                  "anthropic/claude-haiku-6"}
        self.assertEqual(mig.resolve_successor("anthropic/claude-opus-5-5", bumped)[0],
                         "anthropic/claude-opus-6")
        self.assertEqual(mig.resolve_successor("anthropic/claude-sonnet-5-5", bumped)[0],
                         "anthropic/claude-sonnet-6-1")
        self.assertEqual(mig.resolve_successor("anthropic/claude-haiku-5-5", bumped)[0],
                         "anthropic/claude-haiku-6")

    def test_date_stamp_does_not_outrank_a_newer_version(self):
        live = {"default", "anthropic/claude-haiku-4-5-20251001", "anthropic/claude-haiku-5-5"}
        self.assertEqual(mig.resolve_successor("anthropic/claude-haiku-4", live)[0],
                         "anthropic/claude-haiku-5-5")

    def test_family_with_nothing_listed_falls_back_to_default(self):
        self.assertEqual(mig.resolve_successor("anthropic/claude-opus-5", {"default", "openai/gpt-5.5"})[0],
                         "default")

    def test_legacy_presets_still_resolve(self):
        live = {"default", "openai/gpt-5.5", "anthropic/claude-sonnet-5-5"}
        self.assertEqual(mig.resolve_successor("chatgpt", live)[0], "openai/gpt-5.5")
        self.assertEqual(mig.resolve_successor("sonnet", live)[0], "anthropic/claude-sonnet-5-5")


class PlanMigrationTests(unittest.TestCase):
    def test_only_stale_ids_of_this_pipe_change(self):
        chats = [
            _chat("live", PIPE + ".anthropic/claude-opus-5-5"),
            _chat("stale", PIPE + ".claude-cli/claude-opus-5"),
            _chat("bare", PIPE),
            _chat("foreign", "openclaw/default"),
            _chat("other-backend", "llama3.2:1b"),
            _chat("mixed", PIPE + ".anthropic/claude-sonnet-5", "llama3.2:1b"),
            _chat("empty"),
        ]
        changes, foreign = mig.plan_migration(chats, LIVE, PIPE)
        self.assertEqual(
            {c["id"]: c["new"] for c in changes},
            {
                "stale": [PIPE + ".anthropic/claude-opus-5-5"],
                "bare": [PIPE + ".default"],
                "mixed": [PIPE + ".anthropic/claude-sonnet-5-5", "llama3.2:1b"],
            },
        )
        self.assertEqual(dict(foreign), {"openclaw/default": 1})

    def test_every_successor_is_a_listed_model(self):
        stale = ["claude-cli/claude-opus-5", "anthropic/claude-fable-5", "sonnet",
                 "anthropic/claude-haiku-4-5-20251001", "kimi"]
        changes, _ = mig.plan_migration(
            [_chat(s, PIPE + "." + s) for s in stale], LIVE, PIPE
        )
        self.assertEqual(len(changes), len(stale))
        for c in changes:
            for model_id in c["new"]:
                self.assertIn(model_id, LIVE)


if __name__ == "__main__":
    unittest.main()
