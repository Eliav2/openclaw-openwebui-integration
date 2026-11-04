"""The Web Push event function (push/openclaw_web_push.py).

What it sends (title and link, never the reply), which events it sends for
(an OpenClaw model's only from the Pipe), and a real encrypted send to a
local push service, in its own interpreter (tests/web_push_roundtrip.py).
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("openclaw_web_push", os.path.join(ROOT, "push", "openclaw_web_push.py"))
web_push = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web_push)

REPLY = "SECRET-REPLY-TEXT the whole answer the agent wrote"
PIPE = ["openclaw_gateway."]


def finished(source="openclaw-pipe", model="openclaw_gateway.main", **data):
    return {"event": "chat.finished", "source": source, "actor": {"id": "u1"},
            "data": {"user_id": "u1", "chat_id": "c1", "message_id": "m1", "model_id": model,
                     "title": "Trip plan", "url": "https://chat.example.com/c/c1", "message": REPLY, **data}}


class PayloadTests(unittest.TestCase):
    def test_the_payload_never_contains_the_reply(self):
        """Open WebUI 0.13.0's and the Pipe's chat.finished carry the whole
        reply in data.message; the device gets the title and link only."""
        body = web_push.encode_payload(web_push.build_payload(finished()))
        self.assertNotIn("SECRET-REPLY-TEXT", body)
        self.assertEqual(json.loads(body), {"v": 1, "kind": "chat.finished", "source": "openclaw-pipe", "chat_id": "c1", "message_id": "m1",
                                            "title": "Trip plan", "url": "/c/c1"})

    def test_the_link_is_relative_whatever_the_event_says(self):
        payload = web_push.build_payload(finished(url="https://elsewhere.example/phish"))
        self.assertEqual(payload["url"], "/c/c1")

    def test_a_long_title_is_cut_and_the_payload_fits(self):
        body = web_push.encode_payload(web_push.build_payload(finished(title="x" * 10000)))
        self.assertLessEqual(len(body.encode()), web_push.MAX_PAYLOAD_BYTES)
        self.assertEqual(len(json.loads(body)["title"]), web_push.MAX_TITLE_CHARS)

    def test_a_chat_without_a_title(self):
        self.assertEqual(web_push.build_payload(finished(title=""))["title"], "Reply finished")

    def test_the_topic_is_a_short_base64url_per_chat(self):
        a, b = web_push.topic_for("c1"), web_push.topic_for("c2")
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(a), 32)
        self.assertRegex(a, r"^[A-Za-z0-9_-]+$")


class WhichEventsTests(unittest.TestCase):
    def test_the_pipes_own_chat_finished_is_sent(self):
        self.assertTrue(web_push.should_send(finished(), PIPE))

    def test_open_webuis_chat_finished_for_an_openclaw_model_is_skipped(self):
        """It fires when the request ends, which for a handed-over reply is
        before the reply does; the Pipe publishes that reply itself."""
        self.assertFalse(web_push.should_send(finished(source="api"), PIPE))

    def test_open_webuis_chat_finished_for_another_model_is_sent(self):
        self.assertTrue(web_push.should_send(finished(source="api", model="gpt-5"), PIPE))

    def test_other_events_are_not_sent(self):
        self.assertFalse(web_push.should_send({**finished(), "event": "chat.failed"}, PIPE))

    def test_no_prefixes_sends_every_chat_finished(self):
        self.assertTrue(web_push.should_send(finished(source="api"), []))

    def test_malformed_subscriptions_are_ignored(self):
        raw = json.dumps([{"endpoint": "https://p/1", "keys": {"p256dh": "k", "auth": "a"}},
                          {"endpoint": "https://p/2"}, "junk", {"keys": {"p256dh": "k"}}])
        self.assertEqual([s["endpoint"] for s in web_push.parse_subscriptions(raw)], ["https://p/1"])
        self.assertEqual(web_push.parse_subscriptions("not json"), [])


class EventTests(unittest.IsolatedAsyncioTestCase):
    """`event` with Open WebUI's Functions stubbed and the send recorded."""

    async def asyncSetUp(self):
        self.store = {"subscriptions": json.dumps([{"endpoint": "https://push.example/1",
                                                    "keys": {"p256dh": "k", "auth": "a"}}])}
        store = self.store

        class Functions:
            @staticmethod
            async def get_user_valves_by_id_and_user_id(fid, user_id):
                return dict(store)

            @staticmethod
            async def update_user_valves_by_id_and_user_id(fid, user_id, valves):
                store.update(valves)
        mods = {"open_webui": types.ModuleType("open_webui"), "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.functions": types.ModuleType("open_webui.models.functions")}
        mods["open_webui.models.functions"].Functions = Functions
        p = mock.patch.dict(sys.modules, mods)
        p.start()
        self.addCleanup(p.stop)
        self.fn = web_push.Event()
        key = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
        key.write("PEM")
        key.close()
        self.addCleanup(os.unlink, key.name)
        self.fn.valves = self.fn.Valves(VAPID_PRIVATE_KEY_FILE=key.name, VAPID_SUBJECT="mailto:a@example.com")
        self.sent = []
        self.fn.send_all = lambda subs, body, topic, key, subject: self.sent.append((subs, body, key, subject)) or set()

    async def test_a_reply_is_sent_with_the_key_from_the_file(self):
        await self.fn.event(finished(), __id__="web_push")
        (subs, body, key, subject), = self.sent
        self.assertEqual((key, subject), (b"PEM", "mailto:a@example.com"))
        self.assertNotIn("SECRET-REPLY-TEXT", body)

    async def test_no_send_without_the_key_file(self):
        self.fn.valves.VAPID_PRIVATE_KEY_FILE = "/nonexistent/vapid.pem"
        with self.assertLogs("openclaw_web_push", "ERROR"):
            await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(self.sent, [])

    async def test_no_send_without_subscriptions(self):
        self.store["subscriptions"] = "[]"
        await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(self.sent, [])

    async def test_the_middlewares_event_for_an_openclaw_model_sends_nothing(self):
        await self.fn.event(finished(source="api"), __id__="web_push")
        self.assertEqual(self.sent, [])


class RoundTripTests(unittest.TestCase):
    """Real pywebpush encryption and VAPID, decrypted by a local push service."""

    def run_scenario(self, scenario):
        out = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "web_push_roundtrip.py"), scenario],
                             capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_each_device_gets_the_title_and_link_encrypted(self):
        res = self.run_scenario("ok")
        self.assertEqual([r["device"] for r in res["received"]], ["dev1", "dev2"])
        for r in res["received"]:
            self.assertEqual(r["payload"], {"v": 1, "kind": "chat.finished", "source": "openclaw-pipe", "chat_id": "c1", "message_id": "m1",
                                            "title": "Trip plan", "url": "/c/c1"})
            self.assertNotIn("SECRET-REPLY-TEXT", json.dumps(r["payload"]))
            self.assertEqual(r["headers"]["Content-Encoding"], "aes128gcm")
            self.assertEqual((r["headers"]["TTL"], r["headers"]["Urgency"]), ("3600", "normal"))
            self.assertEqual(r["headers"]["Topic"], web_push.topic_for("c1"))
            self.assertEqual(r["vapid"], "vapid")
        self.assertEqual(res["left"], ["dev1", "dev2"])

    def test_a_gone_subscription_is_dropped_and_a_failing_one_kept(self):
        res = self.run_scenario("gone")
        self.assertEqual(len(res["received"]), 2)
        self.assertEqual(res["left"], ["dev2"], "410 dropped, 500 kept")


if __name__ == "__main__":
    unittest.main()
