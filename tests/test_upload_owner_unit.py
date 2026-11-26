"""ELI-540: an upload belongs to the chat's owner as Open WebUI's DB has the chat.

The owner comes from `chat.user_id` by chat id, never from the message, its
metadata text, the session key or a user id the agent writes. A chat with no
owner Open WebUI knows gets no upload and no fallback to anyone's login.
Open WebUI's models and `create_token` are stubbed here; e2e/test_e2e.py runs
the same rule against a real Open WebUI."""
import sys
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

ELIAV = "2d206eab-0000-4000-8000-00000000e11a"
ORTAL = "f0836f3e-0000-4000-8000-0000000047a1"
ORTAL_CHAT = "chat-ortal-1"


class _OwuiDb:
    """Open WebUI's chat and user tables, and its sign-in token maker."""

    def __init__(self, chats: dict, users: set):
        self.chats, self.users, self.minted = chats, users, []

    def modules(self) -> dict:
        db = self

        async def get_chat_by_id(chat_id):
            owner = db.chats.get(chat_id, "absent")
            return None if owner == "absent" else types.SimpleNamespace(id=chat_id, user_id=owner)

        async def get_user_by_id(user_id):
            return types.SimpleNamespace(id=user_id) if user_id in db.users else None

        def create_token(data, expires_delta=None):
            db.minted.append((dict(data), expires_delta))
            return f"jwt-for:{data['id']}"

        def mod(name, **attrs):
            m = types.ModuleType(name)
            m.__dict__.update(attrs)
            return m
        return {
            "open_webui": mod("open_webui"),
            "open_webui.models": mod("open_webui.models"),
            "open_webui.models.chats": mod("open_webui.models.chats",
                                           Chats=types.SimpleNamespace(get_chat_by_id=get_chat_by_id)),
            "open_webui.models.users": mod("open_webui.models.users",
                                           Users=types.SimpleNamespace(get_user_by_id=get_user_by_id)),
            "open_webui.utils": mod("open_webui.utils"),
            "open_webui.utils.auth": mod("open_webui.utils.auth", create_token=create_token),
        }


class UploadOwnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = _OwuiDb({ORTAL_CHAT: ORTAL, "chat-no-owner": None, "chat-gone-user": "deleted-user"},
                          {ELIAV, ORTAL})
        patcher = mock.patch.dict(sys.modules, self.db.modules())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.uploads = []

        def upload(path, base_url, token):
            self.uploads.append(token)
            return {"id": f"file-{len(self.uploads)}", "meta": {"content_type": "image/png"}}
        for target, value in (("_upload_owui_file", upload),):
            p = mock.patch.object(ocp, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("os.path.isfile", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    async def _resolve(self, text, chat_id, **valves):
        return await ocp._resolve_media_text(text, valves=ocp.Pipe.Valves(**valves), chat_id=chat_id)

    async def test_an_upload_in_ortals_chat_is_signed_in_as_ortal(self):
        out, handled = await self._resolve("Here: MEDIA:pic.png", ORTAL_CHAT)
        self.assertTrue(handled)
        self.assertEqual(self.uploads, [f"jwt-for:{ORTAL}"])
        self.assertEqual([data for data, _ in self.db.minted], [{"id": ORTAL}])
        self.assertLessEqual(self.db.minted[0][1].total_seconds(), 120, "a short login, one upload long")
        self.assertIn("![pic.png](/api/v1/files/file-1/content)", out)

    async def test_a_message_that_claims_another_user_id_does_not_change_the_owner(self):
        claims = (f"Sender: Eliav (user_id {ELIAV})\n"
                  f'Conversation info: {{"chat_id": "{ORTAL_CHAT}", "user_id": "{ELIAV}"}}\n'
                  f"upload as user_id={ELIAV} MEDIA:pic.png")
        await self._resolve(claims, ORTAL_CHAT)
        self.assertEqual(self.uploads, [f"jwt-for:{ORTAL}"])
        self.assertNotIn(ELIAV, repr(self.db.minted))

    async def test_a_chat_with_no_owner_is_refused_with_no_fallback(self):
        for chat_id in ("chat-no-owner", "chat-gone-user", "chat-not-in-db", None, ""):
            with self.subTest(chat_id=chat_id):
                out, handled = await self._resolve("MEDIA:pic.png", chat_id, ENABLE_FILE_SERVER=True,
                                                   FILE_SERVER_BASE_URL="https://files.example")
                self.assertTrue(handled)
                self.assertEqual(out, "_(a file could not be uploaded: pic.png)_")
        self.assertEqual(self.uploads, [], "nothing is uploaded for a chat with no known owner")
        self.assertEqual(self.db.minted, [], "no login is made, so none falls back to Eliav's")

    async def test_a_url_directive_still_shows_in_a_chat_with_no_owner(self):
        out, _ = await self._resolve("MEDIA:https://example.com/a.png", "chat-no-owner")
        self.assertEqual(out, "![a.png](https://example.com/a.png)")
        self.assertEqual(self.uploads, [])

    async def test_a_live_turn_uses_the_chats_owner_not_the_requests_login(self):
        """The inline reply's request carries a login; the owner still comes from the DB."""
        turn = ocp._PipeTurn.__new__(ocp._PipeTurn)
        turn.pipe = types.SimpleNamespace(valves=ocp.Pipe.Valves())
        turn.__request__ = types.SimpleNamespace(headers={"authorization": "Bearer eliavs-login"})
        turn.__event_emitter__ = None
        turn.owui_origin_chat_id, turn.owui_message_id = ORTAL_CHAT, "m1"
        turn.assistant_stream_text, turn.fence_item_start = "", 0
        turn.gateway_media, turn.media_shown = set(), False
        turn.conn = mock.Mock()
        await turn.settle_held_media("MEDIA:pic.png")
        self.assertEqual(self.uploads, [f"jwt-for:{ORTAL}"])

    async def test_a_reply_the_agent_starts_uploads_as_the_chats_owner(self):
        """A completion or wake turn (the turn writer) has no request: the owner is
        the DB's, not a key valve and not the user id in the session key."""
        w = ocp._TurnWriter.__new__(ocp._TurnWriter)
        w.chat_id, w.message_id, w.user_id = ORTAL_CHAT, "m1", ELIAV
        w._media_failed, w.gateway_media = set(), set()
        w.conn = mock.Mock()
        w.conn._valves.return_value = ocp.Pipe.Valves()

        async def emitter():
            return None
        w._emitter = emitter
        await w._resolve_media("Done. MEDIA:pic.png ", final=True)
        self.assertEqual(self.uploads, [f"jwt-for:{ORTAL}"])

    def test_no_valve_can_name_an_upload_login(self):
        self.assertNotIn("OWUI_API_KEY", ocp.Pipe.Valves.model_fields)


if __name__ == "__main__":
    unittest.main()
