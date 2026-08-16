#!/usr/bin/env python3
"""Unit tests for the Thinking filter (ELI-85).

Runs against the BUILT artifact, not the fragment, because the artifact is what
Open WebUI actually loads. A fragment that passes and an artifact that does not
is the failure mode a build step invites, so the build is part of the test.

    python3 test_thinking_unit.py
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
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
    print(f"\nSKIPPED: {_reason}")
    try:
        import pytest
    except ImportError:
        sys.exit(0)
    pytest.skip(_reason, allow_module_level=True)

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append(f"{name}\n     got:  {got!r}\n     want: {want!r}")
        print(f"  FAIL {name}")
    else:
        print(f"  ok   {name}")


def load(state_dir=None):
    """Import the built artifact fresh, with STATE_DIR pointed wherever we want.

    Reimported per case because the dropdown is baked into UserValves at import
    time. Testing that from a single cached import would silently only ever
    exercise the first state dir.
    """
    if state_dir is None:
        os.environ.pop("OPENCLAW_BRIDGE_STATE_DIR", None)
    else:
        os.environ["OPENCLAW_BRIDGE_STATE_DIR"] = str(state_dir)
    spec = importlib.util.spec_from_file_location("owui_thinking_test", ARTIFACT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


print("\nbuild is current")
proc = subprocess.run([sys.executable, "build.py", "--check-all"],
                      cwd=ROOT, capture_output=True, text=True)
check("built artifacts match src/ (run: python3 build.py --all)", proc.returncode, 0)
if proc.returncode != 0:
    print("   " + (proc.stdout or proc.stderr).strip()[:400])


print("\nOpen WebUI frontmatter")
# OWUI reads function metadata ONLY from `key: value` lines in the leading
# docstring. Prose parses to {} in silence, so assert with OWUI's own shape.
head = ARTIFACT.read_text()[:4000]
doc = head.split('"""')[1] if '"""' in head else ""
meta = {}
for line in doc.splitlines():
    m = re.match(r"^\s*([a-z_]+):\s*(.+)$", line)
    if m:
        meta.setdefault(m.group(1), m.group(2).strip())
check("title parsed", meta.get("title"), "OpenClaw Thinking")
check("version parsed", meta.get("version"), "0.7.0")
check("description parsed", bool(meta.get("description")), True)
check("no requirements line (stdlib plus pydantic only)", "requirements" in meta, False)


print("\ndropdown contents")
mod = load(state_dir="/nonexistent-on-purpose")
check("falls back when there is no cache",
      mod.available_levels(), ["default", "off", "minimal", "low", "medium", "high"])

with tempfile.TemporaryDirectory() as td:
    cache = Path(td) / "thinking-ladders.json"
    cache.write_text(json.dumps(
        {"levels": ["high", "off", "max", "medium", "xhigh", "low", "minimal"],
         "updated": 1786784049}))
    m2 = load(state_dir=td)
    check("cache drives the list, ordered by rank",
          m2.available_levels(),
          ["default", "off", "minimal", "low", "medium", "high", "xhigh", "max"])
    check("the built UserValves enum matches",
          m2.Filter.UserValves.model_json_schema()["properties"]["level"]["enum"],
          ["default", "off", "minimal", "low", "medium", "high", "xhigh", "max"])

    cache.write_text(json.dumps({"levels": ["low", "bogus", "medium"]}))
    check("unknown level ids in the cache are dropped",
          load(state_dir=td).available_levels(), ["default", "low", "medium"])

    cache.write_text("{ not json")
    check("corrupt cache degrades to the fallback, does not raise",
          load(state_dir=td).available_levels(),
          ["default", "off", "minimal", "low", "medium", "high"])

    cache.write_text(json.dumps({"levels": []}))
    check("empty cache degrades to the fallback",
          load(state_dir=td).available_levels(),
          ["default", "off", "minimal", "low", "medium", "high"])


print("\nclamping to a model's real ladder")
c = mod.clamp_to_ladder
LADDER = ["off", "minimal", "low", "medium", "high"]
check("a supported level passes through untouched", c("medium", LADDER), ("medium", None))
check("the unset sentinel sends nothing", c("default", LADDER), (None, None))
check("empty sends nothing", c("", LADDER), (None, None))

got, note = c("max", LADDER)
check("an unsupported level clamps DOWN to the nearest", got, "high")
check("and says so", bool(note) and "max" in note and "high" in note, True)

got, note = c("xhigh", ["off"])
check("an off-only ladder clamps all the way down", got, "off")
check("off-only clamp is explained", bool(note), True)

got, note = c("low", ["medium", "high"])
check("a ladder with nothing at or below yields its lowest", got, "medium")

got, note = c("high", None)
check("an unknown ladder passes the request through", (got, note), ("high", None))

got, note = c("nonsense", LADDER)
check("an unknown level is dropped, not guessed", got, None)
check("and the drop is explained", bool(note), True)

check("clamping never goes up",
      c("minimal", ["off", "minimal", "low", "medium", "high"])[0], "minimal")


print("\nreading the Gateway's rejection as a ladder")
# The parser lives in the shared fragment, so the filter artifact carries it
# too. Asserting it here proves the fragment stays import-free: the filter
# imports nothing beyond pydantic, and a stray `import re` in a helper the
# filter never calls would still break every message it touches.
p = mod.parse_thinking_rejection
check("the real wording yields level, model and ladder",
      p('Thinking level "high" is not supported for claude-cli/claude-opus-5.'
        ' Use one of: off.'),
      {"level": "high", "model": "claude-cli/claude-opus-5", "levels": ["off"]})

# The binary profile labels `low` as "on" (buildBinaryThinkingProfile). Taking
# the label at face value would learn a one-level ladder for a two-level model
# and clamp every request to `off`.
check("the binary profile's 'on' label maps back to its id",
      p('Thinking level "max" is not supported for anthropic/claude-sonnet-5.'
        ' Use one of: off, on.')["levels"],
      ["off", "low"])

check("a listed ladder comes back ordered by rank, not as written",
      p('Thinking level "ultra" is not supported for x/y.'
        ' Use one of: high, off, medium.')["levels"],
      ["off", "medium", "high"])
check("labels are matched case-insensitively",
      p('Thinking level "max" is not supported for x/y. Use one of: Off, High.')["levels"],
      ["off", "high"])
check("prose after the sentence is not swallowed into the ladder",
      p('Thinking level "max" is not supported for x/y. Use one of: off, low.'
        ' Pick another level and try again.')["levels"],
      ["off", "low"])
check("a duplicate label is listed once",
      p('Thinking level "max" is not supported for x/y. Use one of: off, off.')["levels"],
      ["off"])

# Empty levels and "not a rejection" are different answers: the caller still
# learns WHICH model rejected WHICH level, so it must test for None, not for
# falsiness. Returning None here would silently skip the retry.
parsed = p('Thinking level "max" is not supported for x/y. Use one of: turbo.')
check("an all-unknown ladder is still a rejection", parsed is not None, True)
check("with an empty ladder rather than a guess", parsed["levels"], [])

for text in (
    "Let me think about supported levels here.",
    "high is not supported in this context, unrelated to models.",
    # The anchors are there but the "model" is prose: a model ref never
    # contains a space, and that check is what keeps this from firing on
    # assistant output that quotes the error while discussing it.
    'Thinking level "high" is not supported for some models. Use one of: off.',
    'Thinking level "high" is not supported for x/y.',   # no ladder clause
    'is not supported for x/y. Use one of: off.',        # no head
    '',
    None,
):
    check(f"not a rejection: {str(text)[:44]!r}", p(text), None)


print("\ninlet writes the shared field")


class _UV:
    def __init__(self, level):
        self.level = level


f = mod.Filter()
check("toggle is an instance attribute", "toggle" in vars(f), True)
check("icon is an instance attribute", "icon" in vars(f), True)
check("toggle is on so the row renders", f.toggle, True)

check("a chosen level lands in reasoning_effort",
      f.inlet({}, {"valves": _UV("high")}).get("reasoning_effort"), "high")
check("valves as a plain dict work too",
      f.inlet({}, {"valves": {"level": "low"}}).get("reasoning_effort"), "low")
check("the unset sentinel leaves the body alone",
      "reasoning_effort" in f.inlet({}, {"valves": _UV("default")}), False)
check("no user context leaves the body alone",
      "reasoning_effort" in f.inlet({}, None), False)

# Single source of truth: Advanced Params writes the same key.
check("by default the filter overrides Advanced Params",
      f.inlet({"reasoning_effort": "low"}, {"valves": _UV("high")})["reasoning_effort"],
      "high")

f2 = mod.Filter()
f2.valves.override_advanced_params = False
check("with override off, Advanced Params wins",
      f2.inlet({"reasoning_effort": "low"}, {"valves": _UV("high")})["reasoning_effort"],
      "low")
check("with override off and nothing set, the filter still writes",
      f2.inlet({}, {"valves": _UV("high")})["reasoning_effort"], "high")

check("a non-dict body is returned untouched", f.inlet(None, {"valves": _UV("high")}), None)

body = {"messages": [{"role": "user", "content": "hi"}]}
out = f.inlet(body, {"valves": _UV("medium")})
check("inlet mutates and returns the same body", out is body, True)
check("existing keys survive", out["messages"][0]["content"], "hi")


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for x in FAILURES:
        print("  - " + x)
    sys.exit(1)
print("all passed")
