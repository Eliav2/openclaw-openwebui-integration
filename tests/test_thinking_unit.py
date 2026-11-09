#!/usr/bin/env python3
"""Unit tests for the thinking level (ELI-85): the levels, the clamp, the
Gateway's rejection and the `thinking_default` dropdown, on the built Pipe."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "openclaw_pipe.py"

# This suite needs the real pydantic (create_model, model_json_schema);
# conftest.py imports it first. On CI a miss must fail, not skip.
try:
    from pydantic import create_model as _require_real_pydantic  # noqa: F401
except ImportError as exc:  # pragma: no cover - environment guard
    _reason = f"this suite needs the real pydantic, got: {exc}"
    if os.environ.get("CI"):
        raise RuntimeError(f"CI installs pydantic, so this is a bug: {_reason}")
    import pytest
    pytest.skip(_reason, allow_module_level=True)


def load(state_dir):
    """Import the built Pipe fresh with OPENCLAW_BRIDGE_STATE_DIR set (the
    dropdown is baked in at import). Tests restore it (`_StateDirCase`)."""
    os.environ["OPENCLAW_BRIDGE_STATE_DIR"] = str(state_dir)
    spec = importlib.util.spec_from_file_location("owui_thinking_test", ARTIFACT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FALLBACK = ["default", "off", "minimal", "low", "medium", "high"]


class _StateDirCase(unittest.TestCase):
    def setUp(self):
        before = os.environ.get("OPENCLAW_BRIDGE_STATE_DIR")

        def restore():
            if before is None:
                os.environ.pop("OPENCLAW_BRIDGE_STATE_DIR", None)
            else:
                os.environ["OPENCLAW_BRIDGE_STATE_DIR"] = before
        self.addCleanup(restore)
        self.mod = self._load()

    def _load(self, cache=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        if cache is not None:
            (Path(td.name) / "thinking-ladders.json").write_text(cache)
        return load(td.name)


class DropdownTests(_StateDirCase):
    """The `thinking_default` user valve's choices: the levels the Gateway's
    models offer, from the ladder cache the Pipe writes."""

    def _choices(self, mod):
        schema = mod.Pipe.UserValves.model_json_schema()["properties"]["thinking_default"]
        self.assertEqual(schema["enum"], mod._thinking_default_levels())
        return schema["enum"]

    def test_falls_back_when_there_is_no_cache(self):
        self.assertEqual(self._choices(self.mod), FALLBACK)

    def test_the_cache_drives_the_list_ordered_by_rank_without_ultra(self):
        mod = self._load(json.dumps({"levels": ["high", "off", "ultra", "max", "medium", "xhigh", "low",
                                                "minimal"], "updated": 1786784049}))
        self.assertEqual(self._choices(mod),
                         ["default", "off", "minimal", "low", "medium", "high", "xhigh", "max"])

    def test_unknown_level_ids_in_the_cache_are_dropped(self):
        self.assertEqual(self._choices(self._load(json.dumps({"levels": ["low", "bogus", "medium"]}))),
                         ["default", "low", "medium"])

    def test_a_corrupt_or_empty_cache_degrades_to_the_fallback(self):
        self.assertEqual(self._choices(self._load("{ not json")), FALLBACK)
        self.assertEqual(self._choices(self._load(json.dumps({"levels": []}))), FALLBACK)

    def test_the_default_is_unset_so_no_saved_valve_changes(self):
        self.assertEqual(self.mod.Pipe.UserValves().thinking_default, "default")


class NormalizeTests(_StateDirCase):
    """Advanced Params' Reasoning Effort is free text."""

    def test_case_and_spaces_are_ignored(self):
        self.assertEqual(self.mod.normalize_thinking_level(" XHigh "), "xhigh")

    def test_ultra_is_sent_as_max(self):
        self.assertEqual(self.mod.normalize_thinking_level("Ultra"), "max")

    def test_empty_and_default_mean_no_level(self):
        for value in (None, "", "  ", "default", "Default"):
            with self.subTest(value=value):
                self.assertIsNone(self.mod.normalize_thinking_level(value))


class ClampTests(_StateDirCase):
    LADDER = ["off", "minimal", "low", "medium", "high"]

    def test_a_supported_level_passes_through(self):
        c = self.mod.clamp_to_ladder
        self.assertEqual(c("medium", self.LADDER), ("medium", None))
        self.assertEqual(c("default", self.LADDER), (None, None))
        self.assertEqual(c("", self.LADDER), (None, None))
        self.assertEqual(c("minimal", self.LADDER)[0], "minimal", "clamping never goes up")

    def test_an_unsupported_level_clamps_down_and_says_so(self):
        got, note = self.mod.clamp_to_ladder("max", self.LADDER)
        self.assertEqual(got, "high")
        self.assertIn("max", note)
        self.assertIn("high", note)
        got, note = self.mod.clamp_to_ladder("xhigh", ["off"])
        self.assertEqual(got, "off")
        self.assertTrue(note)

    def test_a_ladder_with_nothing_at_or_below_yields_its_lowest(self):
        self.assertEqual(self.mod.clamp_to_ladder("low", ["medium", "high"])[0], "medium")

    def test_an_unknown_ladder_passes_the_request_through(self):
        self.assertEqual(self.mod.clamp_to_ladder("high", None), ("high", None))

    def test_an_unknown_level_is_dropped_not_guessed(self):
        got, note = self.mod.clamp_to_ladder("nonsense", self.LADDER)
        self.assertIsNone(got)
        self.assertTrue(note)


class RejectionTests(_StateDirCase):
    def test_the_real_wording_yields_level_model_and_ladder(self):
        self.assertEqual(
            self.mod.parse_thinking_rejection(
                'Thinking level "high" is not supported for claude-cli/claude-opus-5. Use one of: off.'),
            {"level": "high", "model": "claude-cli/claude-opus-5", "levels": ["off"]})

    def test_ladder_labels(self):
        p = self.mod.parse_thinking_rejection
        # The binary profile labels `low` as "on" (buildBinaryThinkingProfile).
        self.assertEqual(p('Thinking level "max" is not supported for anthropic/claude-sonnet-5.'
                           ' Use one of: off, on.')["levels"], ["off", "low"])
        self.assertEqual(p('Thinking level "ultra" is not supported for x/y.'
                           ' Use one of: high, off, medium.')["levels"], ["off", "medium", "high"])
        self.assertEqual(p('Thinking level "max" is not supported for x/y.'
                           ' Use one of: Off, High.')["levels"], ["off", "high"])
        self.assertEqual(p('Thinking level "max" is not supported for x/y. Use one of: off, low.'
                           ' Pick another level and try again.')["levels"], ["off", "low"])
        self.assertEqual(p('Thinking level "max" is not supported for x/y.'
                           ' Use one of: off, off.')["levels"], ["off"])

    def test_an_all_unknown_ladder_is_still_a_rejection(self):
        # It still says which model rejected which level: test for None.
        parsed = self.mod.parse_thinking_rejection(
            'Thinking level "max" is not supported for x/y. Use one of: turbo.')
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["levels"], [])

    def test_not_a_rejection(self):
        for text in (
            "Let me think about supported levels here.",
            "high is not supported in this context, unrelated to models.",
            # A model ref never contains a space.
            'Thinking level "high" is not supported for some models. Use one of: off.',
            'Thinking level "high" is not supported for x/y.',   # no ladder clause
            'is not supported for x/y. Use one of: off.',        # no head
            '',
            None,
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.mod.parse_thinking_rejection(text))


if __name__ == "__main__":
    unittest.main()
