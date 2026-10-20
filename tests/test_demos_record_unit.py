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


class OpensOnEndTests(unittest.TestCase):

    def test_the_concat_opens_on_the_last_frame_then_plays_from_the_start(self):
        import tempfile
        r = record.Recorder(_Cdp())
        r.frames = [(0.0, b"empty chat", True), (1.0, b"typing", False), (2.0, b"the feature", False)]
        with tempfile.TemporaryDirectory() as work:
            concat, total = r.write(work, opens_on_end=2.0)
            files = [line.split("'")[1] for line in Path(concat).read_text().splitlines() if line.startswith("file")]
            self.assertEqual(Path(files[0]).read_bytes(), b"the feature")
            self.assertEqual(Path(files[1]).read_bytes(), b"empty chat")
            self.assertAlmostEqual(total, 2.0 + sum(r.durations()))
            _, plain = r.write(work)
            self.assertAlmostEqual(plain, sum(r.durations()))

    def test_every_demo_a_readme_tile_plays_opens_on_its_end(self):
        import json
        import re
        root = Path(__file__).resolve().parents[1]
        tiles = set(re.findall(r'<source srcset="\./docs/img/demos/([^"/]+)\.gif"', (root / "README.md").read_text()))
        self.assertTrue(tiles)
        demos = {d["name"]: d for d in json.loads((root / "scripts" / "demos" / "demos.json").read_text())["demos"]}
        self.assertEqual(sorted(t for t in tiles if not demos.get(t, {}).get("opens_on_end_ms")), [])


if __name__ == "__main__":
    unittest.main()
