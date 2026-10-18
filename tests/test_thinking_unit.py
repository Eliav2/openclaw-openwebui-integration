#!/usr/bin/env python3
"""Unit tests for the Thinking filter (ELI-85).

Runs against the BUILT artifact, not the fragment, because the artifact is what
Open WebUI actually loads. A fragment that passes and an artifact that does not
is the failure mode a build step invites, so the build is part of the test.

    python3 -m pytest test_thinking_unit.py
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARTIFACT = ROOT / "openclaw_thinking_filter.py"

# Precondition, checked before anything else: this suite exercises the real
# pydantic surface (create_model, then model_json_schema on what it built), so
# a stubbed or missing pydantic makes every assertion below meaningless. A
# sibling suite's stub used to shadow the real library here; conftest.py now
# imports it first, and this guard says so out loud if that ever stops working.
# On CI the dependency is installed, so a miss there is a real breakage and has
# to fail: a suite that quietly skips itself is indistinguishable from one that
# passes.
try:
    from pydantic import create_model as _require_real_pydantic  # noqa: F401
except ImportError as exc:  # pragma: no cover - environment guard
    _reason = f"this suite needs the real pydantic, got: {exc}"
    if os.environ.get("CI"):
        raise RuntimeError(f"CI installs pydantic, so this is a bug: {_reason}")
    import pytest
    pytest.skip(_reason, allow_module_level=True)


def load(state_dir=None):
    """Import the built artifact fresh, with STATE_DIR pointed wherever we want.

    Reimported per case because the dropdown is baked into UserValves at import
    time: testing that from one cached import would only ever exercise the
    first state dir. `available_levels()` reads the variable live, so it stays
    set; each test restores it (`_StateDirCase`).
    """
    if state_dir is None:
        os.environ.pop("OPENCLAW_BRIDGE_STATE_DIR", None)
    else:
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
        self.mod = load(state_dir="/nonexistent-on-purpose")


class BuildTests(unittest.TestCase):
    def test_built_artifacts_match_src(self):
        proc = subprocess.run([sys.executable, "build.py", "--check-all"],
                              cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         "run: python3 build.py --all\n" + (proc.stdout or proc.stderr)[:400])

    def test_open_webui_frontmatter(self):
        # OWUI reads function metadata ONLY from `key: value` lines in the
        # leading docstring. Prose parses to {} in silence, so assert with
        # OWUI's own shape.
        head = ARTIFACT.read_text()[:4000]
        doc = head.split('"""')[1] if '"""' in head else ""
        meta = {}
        for line in doc.splitlines():
            m = re.match(r"^\s*([a-z_]+):\s*(.+)$", line)
            if m:
                meta.setdefault(m.group(1), m.group(2).strip())
        self.assertEqual(meta.get("title"), "OpenClaw Thinking")
        self.assertEqual(meta.get("version"), "0.7.0")
        self.assertTrue(meta.get("description"))
        self.assertNotIn("requirements", meta, "stdlib plus pydantic only")


class DropdownTests(_StateDirCase):
    def _cache(self, content):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        (Path(td.name) / "thinking-ladders.json").write_text(content)
        return load(state_dir=td.name)

    def test_falls_back_when_there_is_no_cache(self):
        self.assertEqual(self.mod.available_levels(), FALLBACK)

    def test_the_cache_drives_the_list_ordered_by_rank(self):
        mod = self._cache(json.dumps({"levels": ["high", "off", "max", "medium", "xhigh", "low", "minimal"],
                                      "updated": 1786784049}))
        want = ["default", "off", "minimal", "low", "medium", "high", "xhigh", "max"]
        self.assertEqual(mod.available_levels(), want)
        self.assertEqual(mod.Filter.UserValves.model_json_schema()["properties"]["level"]["enum"], want)

    def test_unknown_level_ids_in_the_cache_are_dropped(self):
        mod = self._cache(json.dumps({"levels": ["low", "bogus", "medium"]}))
        self.assertEqual(mod.available_levels(), ["default", "low", "medium"])

    def test_a_corrupt_or_empty_cache_degrades_to_the_fallback(self):
        self.assertEqual(self._cache("{ not json").available_levels(), FALLBACK)
        self.assertEqual(self._cache(json.dumps({"levels": []})).available_levels(), FALLBACK)


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
    """The parser lives in the shared fragment, so the filter artifact
    carries it too. Asserting it here proves the fragment stays import-free:
    the filter imports nothing beyond pydantic."""

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
        # The caller still learns WHICH model rejected WHICH level, so it
        # tests for None, not for falsiness.
        parsed = self.mod.parse_thinking_rejection(
            'Thinking level "max" is not supported for x/y. Use one of: turbo.')
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["levels"], [])

    def test_not_a_rejection(self):
        for text in (
            "Let me think about supported levels here.",
            "high is not supported in this context, unrelated to models.",
            # The anchors are there but the "model" is prose: a model ref
            # never contains a space.
            'Thinking level "high" is not supported for some models. Use one of: off.',
            'Thinking level "high" is not supported for x/y.',   # no ladder clause
            'is not supported for x/y. Use one of: off.',        # no head
            '',
            None,
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.mod.parse_thinking_rejection(text))


class _UV:
    def __init__(self, level):
        self.level = level


class InletTests(_StateDirCase):
    def test_toggle_and_icon_are_instance_attributes(self):
        f = self.mod.Filter()
        self.assertIn("toggle", vars(f))
        self.assertIn("icon", vars(f))
        self.assertIs(f.toggle, True)

    def test_a_chosen_level_lands_in_reasoning_effort(self):
        f = self.mod.Filter()
        self.assertEqual(f.inlet({}, {"valves": _UV("high")}).get("reasoning_effort"), "high")
        self.assertEqual(f.inlet({}, {"valves": {"level": "low"}}).get("reasoning_effort"), "low")
        self.assertNotIn("reasoning_effort", f.inlet({}, {"valves": _UV("default")}))
        self.assertNotIn("reasoning_effort", f.inlet({}, None))

    def test_advanced_params_override(self):
        f = self.mod.Filter()
        self.assertEqual(f.inlet({"reasoning_effort": "low"}, {"valves": _UV("high")})["reasoning_effort"],
                         "high")
        f2 = self.mod.Filter()
        f2.valves.override_advanced_params = False
        self.assertEqual(f2.inlet({"reasoning_effort": "low"}, {"valves": _UV("high")})["reasoning_effort"],
                         "low")
        self.assertEqual(f2.inlet({}, {"valves": _UV("high")})["reasoning_effort"], "high")

    def test_the_body_is_mutated_in_place(self):
        f = self.mod.Filter()
        self.assertIsNone(f.inlet(None, {"valves": _UV("high")}))
        body = {"messages": [{"role": "user", "content": "hi"}]}
        out = f.inlet(body, {"valves": _UV("medium")})
        self.assertIs(out, body)
        self.assertEqual(out["messages"][0]["content"], "hi")


if __name__ == "__main__":
    unittest.main()
