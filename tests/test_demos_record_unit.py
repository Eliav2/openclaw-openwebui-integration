#!/usr/bin/env python3
"""scripts/demos/record.py: a demo's length is the script's, not the runner's speed. While a
step waits on the stack, a frame held on screen is cut to WAIT_HELD_MAX (nothing moved), and a
wait with `show_ms` plays faster to fit it; typing and pauses keep their real timing."""

import sys
import types
import unittest
from pathlib import Path

# record.py imports Playwright and the screenshot helpers at the top; durations() needs neither.
_api = types.ModuleType("playwright.async_api")
_api.Page, _api.async_playwright = object, None
for name, mod in {"playwright": types.ModuleType("playwright"), "playwright.async_api": _api,
                  "capture": types.ModuleType("capture")}.items():
    sys.modules.setdefault(name, mod)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "demos"))
import record  # noqa: E402


class _Cdp:
    def on(self, *_):
        pass


def recorder(waits):
    """1 s of typing at 10 fps, then a wait: the page still for 3 s, then 5 s of a spinner at
    10 fps; then a 1 s pause."""
    r = record.Recorder(_Cdp())
    r.frames = ([(i * 0.1, b"", i == 0) for i in range(10)] + [(1.0, b"", False)]
                + [(4.0 + i * 0.1, b"", False) for i in range(50)] + [(9.0, b"", False), (10.0, b"", False)])
    r.waits = waits
    return r


class DurationsTests(unittest.TestCase):

    def test_real_timing_outside_waits(self):
        self.assertAlmostEqual(sum(recorder([]).durations()), 10.1)

    def test_a_still_frame_in_a_wait_is_cut(self):
        d = recorder([(10, 61, None)]).durations()
        self.assertAlmostEqual(d[10], record.WAIT_HELD_MAX)
        self.assertAlmostEqual(sum(d[10:61]), record.WAIT_HELD_MAX + 5.0)
        self.assertAlmostEqual(sum(d[:10]), 1.0)

    def test_a_wait_with_show_ms_fits_it_and_nothing_else_changes(self):
        d = recorder([(10, 61, 3.0)]).durations()
        self.assertAlmostEqual(sum(d[10:61]), 3.0, places=2)
        self.assertAlmostEqual(sum(d[:10]), 1.0)
        self.assertAlmostEqual(d[61], 1.0)

    def test_a_short_wait_is_not_stretched(self):
        d = recorder([(10, 61, 30.0)]).durations()
        self.assertAlmostEqual(sum(d[10:61]), record.WAIT_HELD_MAX + 5.0)


if __name__ == "__main__":
    unittest.main()
