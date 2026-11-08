"""CI checks the unversioned vendored Open WebUI copy against the pinned ref
(scripts/check_owui_fixture.py, scripts/check_owui_frontend_fixture.py), and fails
on a `_v<tag>` copy left for the pinned ref."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class HandlerCopies(unittest.TestCase):
    def setUp(self):
        self.m = _load("check_owui_fixture")
        self.pinned = self.m.FIXTURE
        # The next release's own copy: none is vendored while the pin is the newest release.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.next = Path(tmp.name) / "test_fixture_owui_streaming_handler_v0_14_0.py"
        self.next.write_text(self.pinned.read_text())
        self.m.FIXTURES = [self.pinned, self.next]

    def test_pinned_checks_only_the_copy_the_tests_run(self):
        self.assertEqual(self.m.copies_to_check("v0.13.0", "pinned"), [self.pinned])

    def test_a_versioned_copy_for_the_pinned_ref_fails(self):
        why = self.m.copies_to_check("v0.14.0", "pinned")
        self.assertIsInstance(why, str)
        self.assertIn(self.next.name, why)

    def test_next_checks_its_own_copy_else_the_unversioned_one(self):
        self.assertEqual(self.m.copies_to_check("v0.14.0", "next"), [self.next])
        self.assertEqual(self.m.copies_to_check("v0.15.0", "next"), [self.pinned])

    def test_the_compat_watch_takes_any_copy(self):
        self.assertEqual(self.m.copies_to_check("main", "any"), [self.pinned, self.next])

    def test_a_stale_pinned_copy_fails_although_another_copy_matches(self):
        """main() with --copy pinned compares only the unversioned copy."""
        m = self.m
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stale = Path(tmp.name) / "test_fixture_owui_streaming_handler.py"
        stale.write_text("def handle_responses_streaming_event(event, output):\n    return 'stale'\n")
        m.FIXTURE, m.FIXTURES = stale, [stale, self.next]
        orig = m.urllib.request.urlopen

        class _Resp:
            def __init__(self, text): self.text = text
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.text.encode()

        # Upstream serves exactly the next release's copy: only the versioned copy matches it.
        m.urllib.request.urlopen = lambda url, timeout=0: _Resp(self.next.read_text())
        self.addCleanup(setattr, m.urllib.request, "urlopen", orig)
        argv = sys.argv
        self.addCleanup(setattr, sys, "argv", argv)
        sys.argv = ["x", "--ref", "v0.13.0", "--copy", "pinned"]
        self.assertEqual(m.main(), 1)
        sys.argv = ["x", "--ref", "v0.13.0"]
        self.assertEqual(m.main(), 0)


class FrontendCopies(unittest.TestCase):
    def setUp(self):
        self.m = _load("check_owui_frontend_fixture")
        self.pinned = self.m.FIXTURE
        self.later = self.pinned.with_name("test_fixture_owui_structured_output_v0.14.0.ts")
        self.m.FIXTURES = [self.pinned, self.later]

    def test_pinned_and_next(self):
        self.assertEqual(self.m.copies_to_check("v0.13.0", "pinned"), [self.pinned])
        self.assertIsInstance(self.m.copies_to_check("v0.14.0", "pinned"), str)
        self.assertEqual(self.m.copies_to_check("v0.14.0", "next"), [self.later])
        self.assertEqual(self.m.copies_to_check("v0.15.0", "next"), [self.pinned])

    def test_the_vendored_copies_today(self):
        """The pinned copy (0.12.4's reducer, the same through 0.13.0), 0.13.1's, whose only change is
        three optional OutputItem fields, and 0.13.2's for CI's next lane (the same as 0.13.1's)."""
        _load("check_owui_frontend_fixture")
        self.assertEqual([p.name for p in sys.modules["check_owui_frontend_fixture"].FIXTURES],
                         ["test_fixture_owui_structured_output.ts", "test_fixture_owui_structured_output_v0.13.1.ts",
                          "test_fixture_owui_structured_output_v0.13.2.ts"])


if __name__ == "__main__":
    unittest.main()
