"""Screenshots (scripts/screenshots/capture.py): a scene selected with `--only`
that builds on another scene gets it run first, without that scene's images.

`--only drawer-long-parent` skipped `subagents-long`, which makes the chat the
drawer shot needs, and exited 0 without writing the image (review on #238).

Run: uv run --with-requirements requirements-dev.txt pytest -q tests/test_screenshot_selection_unit.py
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_capture():
    """capture.py without Playwright, which unit runs do not install."""
    fake = types.ModuleType("playwright.sync_api")
    fake.Page, fake.sync_playwright = object, None
    with mock.patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.sync_api": fake}):
        sys.path.insert(0, os.path.join(ROOT, "scripts", "screenshots"))
        try:
            sys.modules.pop("capture", None)
            import capture
        finally:
            sys.path.pop(0)
    return capture


capture = load_capture()


class FakePage:
    def evaluate(self, *a, **k):
        return None

    def screenshot(self, path, **k):
        open(path, "wb").close()


class PrerequisiteScenes(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("visible_text", lambda page, selector: ""), ("deny_hits", lambda text, extra: [])):
            patch = mock.patch.object(capture, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.ran = []

    def shooter(self, only):
        shooter = capture.Shooter(self.tmp.name, only)
        self.state = {}

        def long_chat(page):
            self.ran.append("subagents-long")
            shooter.shot(page, "subagents-long-dialog")
            self.state["chat"] = "c1"

        def drawer(page):
            self.ran.append("drawer-long-parent")
            shooter.shot(page, "drawer-long-parent")

        def run_all():
            page = FakePage()
            shooter.run("subagents-long", long_chat, page, needed_by=("drawer-long-parent",))
            if self.state.get("chat"):
                shooter.run("drawer-long-parent", drawer, page)
        return shooter, run_all

    def written(self, shooter):
        return sorted(os.path.basename(p) for p in shooter.written)

    def test_a_dependent_scene_alone_runs_its_prerequisite_without_its_images(self):
        shooter, run_all = self.shooter({"drawer-long-parent"})
        run_all()
        self.assertEqual(self.ran, ["subagents-long", "drawer-long-parent"])
        self.assertEqual(self.written(shooter), ["drawer-long-parent-light.png"])
        self.assertEqual(shooter.failures, [])

    def test_the_prerequisite_alone_writes_its_images_and_skips_the_dependent(self):
        shooter, run_all = self.shooter({"subagents-long"})
        run_all()
        self.assertEqual(self.ran, ["subagents-long"])
        self.assertEqual(self.written(shooter), ["subagents-long-dialog-light.png"])

    def test_no_selection_runs_and_writes_everything(self):
        shooter, run_all = self.shooter(None)
        run_all()
        self.assertEqual(self.written(shooter), ["drawer-long-parent-light.png", "subagents-long-dialog-light.png"])

    def test_an_unrelated_selection_runs_neither(self):
        shooter, run_all = self.shooter({"model-picker"})
        run_all()
        self.assertEqual(self.ran, [])

    def test_the_real_entry_points_declare_their_prerequisites(self):
        with open(os.path.join(ROOT, "scripts", "screenshots", "capture.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('needed_by=("drawer-long-parent",)', src)
        self.assertIn('needed_by=("status-dialog-scrolled",)', src)
        self.assertIn('"tool-card-expanded", "composer-status", "thinking-toggle"', src)


if __name__ == "__main__":
    unittest.main()
