#!/usr/bin/env python3
"""The PR media check (scripts/pr_media.py): a PR that changes what users see
shows an image in its description, or carries the `no-visual-change` label.

AGENTS.md asked for a Before and After pair on every visual PR, and a merged
fix to the sub-agent drawer still went in with only coordinates in its
description. The check runs in .github/workflows/pr-media.yml.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import pr_media  # noqa: E402

IMG = '<img src="https://example.com/after.png" alt="After" width="320">'
DRAWER = ["src/openclaw_pipe_pkg/action.py", "openclaw_status_action.py", "tests/test_x_unit.py"]


class WhichFilesAreVisual(unittest.TestCase):
    def test_ui_sources_and_docs_images_are_visual(self):
        self.assertEqual(pr_media.visual_files(DRAWER + ["docs/img/subagent-drawer-light.png",
                                                         "docs/img/demos/status-dialog.gif"]),
                         ["src/openclaw_pipe_pkg/action.py", "docs/img/subagent-drawer-light.png",
                          "docs/img/demos/status-dialog.gif"])

    def test_backend_tests_and_docs_text_are_not(self):
        self.assertEqual(pr_media.visual_files(["src/openclaw_pipe_pkg/gateway.py", "tests/test_pipe_unit.py",
                                                "docs/features/index.md", ".github/workflows/ci.yml"]), [])

    def test_every_listed_source_exists(self):
        # A renamed module would silently drop out of the check.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for path in pr_media.VISUAL:
            self.assertTrue(os.path.isfile(os.path.join(root, path)), path)


class WhatCountsAsAnImage(unittest.TestCase):
    def test_an_img_tag_counts(self):
        self.assertTrue(pr_media.has_image("## Before / After\n\n" + IMG))
        self.assertTrue(pr_media.has_image("<IMG SRC=x width=320>"))

    def test_no_body_or_plain_link_does_not(self):
        self.assertFalse(pr_media.has_image(None))
        self.assertFalse(pr_media.has_image(""))
        self.assertFalse(pr_media.has_image("See [the shot](https://example.com/after.png)."))

    def test_a_markdown_image_counts(self):
        # gh --attach rewrites ![alt](./file.png) into a user-attachments URL.
        self.assertTrue(pr_media.has_image("![after](https://github.com/user-attachments/assets/abc-123)"))
        self.assertTrue(pr_media.has_image("![before](./before.jpg)\n![after](./after.jpg)"))
        self.assertTrue(pr_media.has_image("![](https://example.com/a.png)"))

    def test_a_markdown_image_in_a_comment_or_code_does_not(self):
        self.assertFalse(pr_media.has_image("<!-- Use ![after](./after.png) -->"))
        self.assertFalse(pr_media.has_image("Write `![after](./after.png)` for an image."))
        self.assertFalse(pr_media.has_image("```md\n![after](./after.png)\n```\n"))
        self.assertFalse(pr_media.has_image("~~~\n![after](./after.png)\n~~~"))

    def test_branch_links_are_found_outside_comments_and_code(self):
        raw = "![a](https://raw.githubusercontent.com/o/r/pr-media/1/a.png)"
        self.assertEqual(pr_media.branch_links(raw), ["raw.githubusercontent.com"])
        self.assertEqual(pr_media.branch_links("![a](https://github.com/o/r/blob/b/a.png?raw=true)"),
                         ["?raw=true", "github.com/o/r/blob/"])
        self.assertEqual(pr_media.branch_links("![a](https://github.com/user-attachments/assets/abc)"), [])
        self.assertEqual(pr_media.branch_links("<!-- " + raw + " -->"), [])

    def test_an_img_in_a_comment_or_code_does_not(self):
        self.assertFalse(pr_media.has_image("<!-- Before/after as " + IMG + " -->"))
        self.assertFalse(pr_media.has_image("Shown with an `<img>` tag."))
        self.assertFalse(pr_media.has_image("```html\n" + IMG + "\n```\n"))

    def test_an_img_after_a_code_span_still_counts(self):
        self.assertTrue(pr_media.has_image("The `drawer-long-parent` shot:\n\n" + IMG))


class TheCheck(unittest.TestCase):
    def test_a_visual_change_without_an_image_fails(self):
        why = pr_media.check(DRAWER, "Link box before [506..626], after [254..374].", [])
        self.assertIn("src/openclaw_pipe_pkg/action.py", why)
        self.assertIn(pr_media.SKIP_LABEL, why)

    def test_an_image_passes(self):
        self.assertIsNone(pr_media.check(DRAWER, IMG, []))

    def test_the_label_passes(self):
        self.assertIsNone(pr_media.check(DRAWER, "", ["bug", pr_media.SKIP_LABEL]))

    def test_a_change_with_nothing_visual_passes(self):
        self.assertIsNone(pr_media.check(["src/openclaw_pipe_pkg/gateway.py"], "", []))


class FromTheEvent(unittest.TestCase):
    def run_main(self, body, labels, paths):
        with tempfile.TemporaryDirectory() as tmp:
            event, files = os.path.join(tmp, "event.json"), os.path.join(tmp, "files.txt")
            with open(event, "w", encoding="utf-8") as fh:
                json.dump({"pull_request": {"body": body, "labels": [{"name": n} for n in labels]}}, fh)
            with open(files, "w", encoding="utf-8") as fh:
                fh.write("\n".join(paths) + "\n")
            return pr_media.main(["--event", event, "--files", files])

    def test_exit_codes(self):
        self.assertEqual(self.run_main(None, [], DRAWER), 1)
        self.assertEqual(self.run_main(IMG, [], DRAWER), 0)
        self.assertEqual(self.run_main(None, [pr_media.SKIP_LABEL], DRAWER), 0)
        self.assertEqual(self.run_main(None, [], ["README.md"]), 0)
        self.assertEqual(self.run_main("![after](https://github.com/user-attachments/assets/abc)", [], DRAWER), 0)

    def test_a_branch_link_warns_but_does_not_fail(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.run_main("![a](https://raw.githubusercontent.com/o/r/b/a.png)", [], DRAWER)
        self.assertEqual(rc, 0)
        self.assertIn("::warning", out.getvalue())


if __name__ == "__main__":
    unittest.main()
