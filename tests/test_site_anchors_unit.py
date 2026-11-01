#!/usr/bin/env python3
"""scripts/check_site_anchors.py, on small built-site fixtures: a link to a heading id that is
missing fails, one that is there passes, and links outside the site are left alone."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_site_anchors", ROOT / "scripts" / "check_site_anchors.py")
anchors = importlib.util.module_from_spec(spec)
spec.loader.exec_module(anchors)


class SiteAnchorTests(unittest.TestCase):

    def site(self, pages: dict) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dist = Path(tmp.name)
        for name, html in pages.items():
            (dist / name).parent.mkdir(parents=True, exist_ok=True)
            (dist / name).write_text(html, encoding="utf-8")
        return dist

    def test_a_link_to_a_missing_id_fails(self):
        # The case that shipped: VitePress made `owner-users` of "OWNER_USERS", the link says `owner_users`.
        dist = self.site({
            "configuration.html": '<h3 id="owner-users">OWNER_USERS</h3>',
            "features/owner-users.html": '<a href="./../configuration#owner_users">OWNER_USERS</a>',
        })
        bad, checked = anchors.broken(dist)
        self.assertEqual(checked, 1)
        self.assertEqual(len(bad), 1)
        self.assertIn("owner_users", bad[0])
        self.assertEqual(anchors.main(["x", str(dist)]), 1)

    def test_links_that_land_pass(self):
        dist = self.site({
            "security.html": '<h2 id="the-agents-privileges">The agent\'s privileges</h2>'
                             '<a href="#the-agents-privileges">here</a>',
            "configuration.html": '<h3 id="owner_users">OWNER_USERS</h3>',
            "install/index.html": '<h2 id="before-you-start">Before</h2>',
            "features/owner-users.html":
                '<a href="./../security#the-agents-privileges">a</a>'
                '<a href="/openclaw-openwebui-integration/configuration#owner_users">b</a>'
                '<a href="../install/#before-you-start">c</a>',
        })
        self.assertEqual(anchors.broken(dist), ([], 4))
        self.assertEqual(anchors.main(["x", str(dist)]), 0)

    def test_links_outside_the_site_are_not_checked(self):
        dist = self.site({"index.html":
                          '<a href="https://github.com/x/y#nope">a</a>'
                          '<a href="/elsewhere/page#nope">b</a>'
                          '<a href="./img/logo.svg#nope">c</a>'
                          '<a href="./page">no fragment</a>'})
        self.assertEqual(anchors.broken(dist), ([], 0))

    def test_no_heading_links_at_all_fails(self):
        # An empty or wrong directory must not pass as "everything resolves".
        self.assertEqual(anchors.main(["x", str(self.site({"index.html": "<p>hi</p>"}))]), 1)


if __name__ == "__main__":
    unittest.main()
