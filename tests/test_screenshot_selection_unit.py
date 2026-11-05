"""capture.py: `--only` on a scene runs the scene it builds on first, without its
images (review on #238)."""
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

    def test_every_guarded_scene_is_declared_by_the_scene_that_makes_its_input(self):
        """Each scene run under `if scenes.<input>:` is listed in `needed_by` of
        the scene producing <input>."""
        with open(os.path.join(ROOT, "scripts", "screenshots", "capture.py"), encoding="utf-8") as fh:
            src = fh.read()
        main = src[src.index("scenes = Scenes(token, shooter)"):]
        producers = {"hero_chat": "hero-tool-cards", "long_chat": "subagents-long",
                     "background_chat": "status-background", "spawn_chat": "subagent-result"}
        import re
        for attr, producer in producers.items():
            m = re.search(r'shooter\.run\("%s",[^\n]*?(?:\n[^\n]*?)?needed_by=\(([^)]*)\)' % re.escape(producer), main)
            self.assertTrue(m, f"{producer} declares needed_by")
            declared = set(re.findall(r'"([^"]+)"', m.group(1)))
            guarded = set()
            for g in re.finditer(r"if scenes\.%s:\n((?:[ ]{16,}[^\n]*\n)+)" % attr, main):
                guarded |= set(re.findall(r'shooter\.run\("([^"]+)"', g.group(1)))
            self.assertTrue(guarded, f"scenes guarded by {attr}")
            self.assertEqual(guarded - declared, set(), f"{producer} must list them in needed_by")
        # thinking-level also needs the filter thinking-toggle attaches
        self.assertRegex(main, r'"thinking-toggle", scenes\.thinking, page, needed_by=\("thinking-level",\)')


if __name__ == "__main__":
    unittest.main()
