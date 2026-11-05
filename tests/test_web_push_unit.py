"""The Web Push function (push/openclaw_web_push.py): title and link only, OpenClaw
events only from the Pipe, and a real encrypted send (tests/web_push_roundtrip.py)."""
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
        """chat.finished carries the whole reply; the device gets only title and link."""
        body = web_push.encode_payload(web_push.build_payload(finished()))
        self.assertNotIn("SECRET-REPLY-TEXT", body)
        self.assertEqual(json.loads(body), {"v": 2, "kind": "chat.finished", "source": "openclaw-pipe", "chat_id": "c1", "message_id": "m1",
                                            "title": "Trip plan", "url": "/c/c1", "actions": ["reply"]})

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
        """For a handed-over reply the request ends first; the Pipe publishes that reply itself."""
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


class PreviewTests(unittest.TestCase):
    """PREVIEW_CHARS > 0: a plain-text start of the reply as the notification body."""

    def test_off_by_default_no_body(self):
        self.assertEqual(web_push.Event.Valves().PREVIEW_CHARS, 0)
        self.assertNotIn("body", web_push.build_payload(finished()))

    def test_on_the_start_of_the_reply_as_plain_text(self):
        md = ("# Plan\n\nHere is **the plan** for [Kyoto](https://x.example/k):\n\n- day one\n- day two\n\n"
              "```python\nprint('x')\n```\n\n![map](/img.png)\nMEDIA:/tmp/a.png\n"
              "<details type=\"tool_calls\"><summary>Tool</summary>\nsecret tool output\n</details>\nDone.")
        body = web_push.snippet(md, 600)
        self.assertEqual(body, "Plan Here is the plan for Kyoto: day one day two [code] Done.")

    def test_cut_on_a_word_with_an_ellipsis(self):
        self.assertEqual(web_push.snippet("one two three four", 9), "one two…")

    def test_hebrew_is_kept_as_is(self):
        self.assertEqual(web_push.snippet("**שלום** עולם, מה נשמע?", 600), "שלום עולם, מה נשמע?")

    def test_the_payload_with_a_preview_fits_a_push(self):
        p = web_push.build_payload(finished(message="א" * 5000), preview_chars=4000, fn="web_push")
        body = web_push.encode_payload(p)
        self.assertLessEqual(len(body.encode()), web_push.MAX_PAYLOAD_BYTES)
        self.assertTrue(json.loads(body)["body"].startswith("אאא"))
        self.assertEqual(json.loads(body)["fn"], "web_push")

    def test_an_empty_reply_has_no_body(self):
        self.assertNotIn("body", web_push.build_payload(finished(message="```\nx\n```"), preview_chars=600))


class ActionsTests(unittest.TestCase):
    """NOTIFICATION_ACTIONS: the buttons, in order (Android Chrome shows the first two)."""

    def test_the_default_is_reply_and_mute(self):
        self.assertEqual(web_push.Event.Valves().NOTIFICATION_ACTIONS, "reply,mute")
        self.assertEqual(web_push.build_payload(finished(), fn="web_push")["actions"], ["reply", "mute"])

    def test_the_valve_order_unknown_names_and_repeats(self):
        self.assertEqual(web_push.parse_actions(" read , reply,nope,read"), ["read", "reply"])
        self.assertEqual(web_push.parse_actions(""), [])

    def test_mute_needs_the_function_id(self):
        """Mute writes this function's user valves: without its id, no Mute button."""
        self.assertEqual(web_push.build_payload(finished(), actions=["reply", "mute", "read"])["actions"], ["reply", "read"])

    def test_no_chat_no_buttons(self):
        self.assertNotIn("actions", web_push.build_payload(finished(chat_id=""), fn="web_push"))


class MuteTests(unittest.TestCase):
    def test_always_and_until_and_expired(self):
        mutes = json.dumps({"c1": 0, "c2": 2000, "c3": 500})
        self.assertTrue(web_push.is_muted(mutes, "c1", now=1000))
        self.assertTrue(web_push.is_muted(mutes, "c2", now=1000))
        self.assertFalse(web_push.is_muted(mutes, "c3", now=1000))
        self.assertFalse(web_push.is_muted(mutes, "c4", now=1000))

    def test_malformed_mutes_mute_nothing(self):
        for raw in ("not json", "[1]", json.dumps({"c1": "soon"}), None, ""):
            self.assertFalse(web_push.is_muted(raw, "c1", now=1000), raw)


class FolderMuteTests(unittest.TestCase):
    """`folder_mutes`: {folder_id: until}; a folder's mute covers the folders inside it."""
    PARENTS = {"qa": None, "qa-old": "qa", "qa-old-2025": "qa-old", "work": None}

    def test_the_folder_itself_and_every_folder_inside_it(self):
        raw = json.dumps({"qa": 0})
        for folder in ("qa", "qa-old", "qa-old-2025"):
            self.assertEqual(web_push.muted_folder(raw, folder, self.PARENTS, now=1000), "qa", folder)
        self.assertIsNone(web_push.muted_folder(raw, "work", self.PARENTS, now=1000))

    def test_until_expired_and_no_folder(self):
        self.assertEqual(web_push.muted_folder(json.dumps({"qa-old": 2000}), "qa-old-2025", self.PARENTS, now=1000), "qa-old")
        self.assertIsNone(web_push.muted_folder(json.dumps({"qa": 500}), "qa", self.PARENTS, now=1000))
        self.assertIsNone(web_push.muted_folder(json.dumps({"qa": 0}), None, self.PARENTS, now=1000))

    def test_a_parent_cycle_ends(self):
        self.assertIsNone(web_push.muted_folder(json.dumps({"x": 0}), "a", {"a": "b", "b": "a"}, now=1000))

    def test_malformed_folder_mutes_mute_nothing(self):
        for raw in ("not json", "[1]", json.dumps({"qa": "soon"}), None, ""):
            self.assertIsNone(web_push.muted_folder(raw, "qa", self.PARENTS, now=1000), raw)


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
        # c1 is in folder "qa-old", inside "qa".
        self.folder_reads = []
        reads = self.folder_reads

        class Chats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                reads.append(chat_id)
                return types.SimpleNamespace(id=chat_id, folder_id="qa-old" if chat_id == "c1" else None)

        class Folders:
            @staticmethod
            async def get_folders_by_user_id(user_id):
                return [types.SimpleNamespace(id="qa", parent_id=None), types.SimpleNamespace(id="qa-old", parent_id="qa")]
        mods = {"open_webui": types.ModuleType("open_webui"), "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.functions": types.ModuleType("open_webui.models.functions"),
                "open_webui.models.chats": types.ModuleType("open_webui.models.chats"),
                "open_webui.models.folders": types.ModuleType("open_webui.models.folders")}
        mods["open_webui.models.functions"].Functions = Functions
        mods["open_webui.models.chats"].Chats = Chats
        mods["open_webui.models.folders"].Folders = Folders
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

    async def test_a_muted_chat_sends_nothing(self):
        self.store["mutes"] = json.dumps({"c1": 0})
        with self.assertLogs("openclaw_web_push", "INFO") as logs:
            await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(self.sent, [])
        self.assertIn("muted", "\n".join(logs.output))

    async def test_a_chat_in_a_muted_folder_sends_nothing(self):
        for muted in ("qa-old", "qa"):   # its own folder, or the folder that holds it
            self.store["folder_mutes"] = json.dumps({muted: 0})
            with self.assertLogs("openclaw_web_push", "INFO") as logs:
                await self.fn.event(finished(), __id__="web_push")
            self.assertEqual(self.sent, [], muted)
            self.assertIn(f"folder {muted[:8]} is muted", "\n".join(logs.output))

    async def test_another_folder_muted_still_sends(self):
        self.store["folder_mutes"] = json.dumps({"work": 0})
        await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(len(self.sent), 1)

    async def test_no_folder_mutes_reads_no_chat(self):
        """Without a folder mute in effect, the send costs no extra read."""
        for raw in (None, "{}", json.dumps({"qa": 500})):
            if raw is None:
                self.store.pop("folder_mutes", None)
            else:
                self.store["folder_mutes"] = raw
            await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.folder_reads, [])

    async def test_another_chat_muted_still_sends(self):
        self.store["mutes"] = json.dumps({"c9": 0})
        await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(len(self.sent), 1)

    async def test_the_preview_valve_puts_the_reply_start_in_the_body(self):
        self.fn.valves.PREVIEW_CHARS = 600
        await self.fn.event(finished(), __id__="web_push")
        (subs, body, key, subject), = self.sent
        self.assertEqual(json.loads(body)["body"], REPLY)

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
            self.assertEqual(r["payload"], {"v": 2, "kind": "chat.finished", "source": "openclaw-pipe", "chat_id": "c1", "message_id": "m1",
                                            "title": "Trip plan", "url": "/c/c1", "fn": "web_push", "actions": ["reply", "mute"]})
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
