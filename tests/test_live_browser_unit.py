#!/usr/bin/env python3
"""scripts/demos/live_browser.py: the stand-in for the live browser view that the `live-browser`
demo runs. Its pages and endpoints answer without a browser; Chromium only drives the local site."""

import asyncio
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "demos"))
import live_browser  # noqa: E402


class StandInTests(unittest.TestCase):
    def serve(self):
        async def run():
            live = await live_browser.LiveBrowser(None, port=0).start()
            live.port = live._server.server_port
            return live
        return self.loop.run_until_complete(run())

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.live = self.serve()
        self.addCleanup(lambda: self.loop.run_until_complete(self.live.stop()))

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.live.port}{path}", timeout=10) as r:
            return r.status, r.headers, r.read()

    def test_the_embed_page_is_framed_by_an_opaque_origin_and_reports_its_height(self):
        status, headers, body = self.get("/embed?chat=c&message=m")
        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], "*")
        text = body.decode()
        self.assertIn("iframe:height", text)
        self.assertIn("Waiting for this message", text)
        self.assertIn("LIVE", text)
        self.assertNotIn("@WAIT@", text)
        self.assertNotIn("@HOST@", text)

    def test_the_site_has_a_home_a_search_and_the_article(self):
        for path, needle in (("/site/", "Find a mountain"), ("/site/search?q=Kilimanjaro", "Results for"),
                             ("/site/peaks/kilimanjaro", "5,895 m (19,341 ft)")):
            with self.subTest(path=path):
                self.assertIn(needle, self.get(path)[2].decode())

    def test_no_frame_before_the_browser_runs_and_the_state_is_idle(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/frame.jpg")
        self.assertEqual(e.exception.code, 404)
        self.assertEqual(json.loads(self.get("/state")[2]), {"phase": "idle", "path": "/"})

    def test_done_answers_when_the_browser_has_finished(self):
        self.live.finished.set()
        self.assertIn("height 5,895 m (19,341 ft)", self.get("/done")[2].decode())


if __name__ == "__main__":
    unittest.main()
