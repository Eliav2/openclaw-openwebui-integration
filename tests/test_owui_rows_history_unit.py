"""Open WebUI 0.13.5 keeps a chat's messages only in chat_message rows (ELI-538, issue #434).

From 0.13.5 the stored chat.chat holds no history (`Chats._without_history`): readers that go through
`Chats.get_chat_by_id` get one rebuilt from the rows, but code that opens the raw row under
`Chats._chat_transaction` finds none. The fake below follows 0.13.5's own code for what the Pipe calls
(`upsert_message_to_history`, `ChatMessages.upsert_message` replacing a row's data, the two message maps
rebuilding childrenIds from parent links), so each writer is checked against the shape 0.13.5 stores.
The same writers on a blob-history Open WebUI (0.13.4 and older) are covered by the existing tests."""
import asyncio
import contextlib
import copy
import sys
import time
import types
import unittest
from unittest import mock

import test_pipe_unit  # noqa: F401  (installs the module stubs openclaw_pipe needs)
from _test_helpers import owui_modules

import openclaw_pipe

CHAT, USER = "44444444-4444-4444-4444-444444444444", "u1"
OLD_TS = 1790000000


def _add_child_id_to_parent(messages, parent_id, child_id):
    if parent_id and parent_id in messages:
        children = messages[parent_id].setdefault("childrenIds", [])
        if child_id not in children:
            children.append(child_id)


def upsert_message_to_history(history, message_id, message):
    """Open WebUI 0.13.5 `ChatTable.upsert_message_to_history` (models/chats.py:1178), without the meta merge."""
    messages = history.setdefault("messages", {})
    if message_id in messages:
        messages[message_id] = {**messages[message_id], **message}
    else:
        parent_id = message.get("parentId")
        if parent_id is None:
            parent_id = next((i for i, m in messages.items() if message_id in m.get("childrenIds", [])), None)
        messages[message_id] = {
            **message, "id": message.get("id") or message_id,
            "parentId": message.get("parentId") if message.get("parentId") is not None else parent_id,
            "childrenIds": message.get("childrenIds") if isinstance(message.get("childrenIds"), list) else [],
            "role": message.get("role") or "assistant", "timestamp": message.get("timestamp") or int(time.time()),
        }
        history["currentId"] = message_id
    _add_child_id_to_parent(messages, messages[message_id].get("parentId"), message_id)
    return messages[message_id]


class RowsOwui:
    """A chat on Open WebUI 0.13.5: the blob without history, the messages as rows."""

    def __init__(self, test):
        self.chat = types.SimpleNamespace(
            id=CHAT, user_id=USER, title="New Chat", meta={}, updated_at=0, current_message_id="a1",
            chat={"title": "New Chat", "models": ["openclaw_gateway.m"]})
        self.rows = {
            "u1": {"id": "u1", "role": "user", "parentId": None, "childrenIds": ["a1"], "content": "hello there",
                   "timestamp": OLD_TS},
            "a1": {"id": "a1", "role": "assistant", "parentId": "u1", "childrenIds": [], "content": "",
                   "model": "openclaw_gateway.m", "done": False, "timestamp": OLD_TS + 1,
                   "meta": {"openclaw_run": "r1"}},
        }
        lock = asyncio.Lock()
        owui = self

        class Chats:
            upsert_message_to_history = staticmethod(upsert_message_to_history)
            _clean_null_bytes = staticmethod(lambda obj: obj)

            @staticmethod
            def _without_history(chat):
                return {k: v for k, v in chat.items() if k not in ("history", "messages")}

            @staticmethod
            @contextlib.asynccontextmanager
            async def _chat_transaction(chat_id):
                async with lock:
                    await asyncio.sleep(0)
                    yield ("db", owui.chat if chat_id == CHAT else None)

        def linked(ids):
            out = {i: {**copy.deepcopy(owui.rows[i]), "childrenIds": []} for i in ids if i in owui.rows}
            for mid, row in owui.rows.items():
                if row.get("parentId") in out and mid not in out[row["parentId"]]["childrenIds"]:
                    out[row["parentId"]]["childrenIds"].append(mid)
            return out

        class ChatMessages:
            @staticmethod
            async def upsert_message(message_id, chat_id, user_id, data, db=None):
                # 0.13.5 replaces the row with `data`: a partial dict resets the rest.
                owui.rows[message_id] = copy.deepcopy(data)

            @staticmethod
            async def get_messages_map_by_message_ids(chat_id, message_ids, db=None):
                return linked(message_ids)

            @staticmethod
            async def get_messages_map_by_chat_id(chat_id, db=None):
                return linked(list(owui.rows)) or None

        chats_mod = types.ModuleType("open_webui.models.chats")
        chats_mod.Chats = Chats
        cm_mod = types.ModuleType("open_webui.models.chat_messages")
        cm_mod.ChatMessages = ChatMessages
        attrs = types.ModuleType("sqlalchemy.orm.attributes")
        attrs.flag_modified = lambda obj, name: None
        mods = {**owui_modules(), "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.chats": chats_mod, "open_webui.models.chat_messages": cm_mod,
                "sqlalchemy": types.ModuleType("sqlalchemy"), "sqlalchemy.orm": types.ModuleType("sqlalchemy.orm"),
                "sqlalchemy.orm.attributes": attrs}
        p = mock.patch.dict(sys.modules, mods)
        p.start()
        test.addCleanup(p.stop)

    def assert_no_stray_history(self, test):
        test.assertNotIn("history", self.chat.chat, "a history written into the 0.13.5 blob is a stray copy")


class EndSnapshotTests(unittest.IsolatedAsyncioTestCase):
    """Issue #434's repro: the end snapshot of a reply on 0.13.5."""

    def setUp(self):
        self.owui = RowsOwui(self)

    async def test_the_finished_reply_keeps_its_parent_and_time(self):
        ok = await openclaw_pipe._owui_upsert_message_with_statuses_now(
            CHAT, "a1", {"content": "x", "done": True}, [{"description": "done", "done": True}])
        self.assertTrue(ok)
        a1 = self.owui.rows["a1"]
        self.assertEqual(a1["parentId"], "u1", "every reply lost its parent (#434)")
        self.assertEqual(a1["timestamp"], OLD_TS + 1)
        self.assertEqual((a1["content"], a1["done"], a1["model"]), ("x", True, "openclaw_gateway.m"))
        self.assertEqual(a1["meta"], {"openclaw_run": "r1"}, "the row keeps what the snapshot did not name")
        self.assertEqual(a1["statusHistory"], [{"description": "done", "done": True}])
        self.assertEqual(self.owui.chat.current_message_id, "a1")
        self.owui.assert_no_stray_history(self)

    async def test_two_exchanges_stay_one_branch(self):
        await openclaw_pipe._owui_upsert_message_with_statuses_now(CHAT, "a1", {"content": "x", "done": True}, [])
        self.owui.rows["u2"] = {"id": "u2", "role": "user", "parentId": "a1", "childrenIds": ["a2"],
                                "content": "again", "timestamp": OLD_TS + 2}
        self.owui.rows["a2"] = {"id": "a2", "role": "assistant", "parentId": "u2", "childrenIds": [],
                                "content": "", "done": False, "timestamp": OLD_TS + 3}
        self.owui.chat.current_message_id = "a2"
        await openclaw_pipe._owui_upsert_message_with_statuses_now(CHAT, "a2", {"content": "y", "done": True}, [])
        chain, mid = [], "a2"
        while mid:
            chain.append(mid)
            mid = self.owui.rows[mid].get("parentId")
        self.assertEqual(chain, ["a2", "u2", "a1", "u1"], "a reload shows the whole branch")
        self.owui.assert_no_stray_history(self)


class ClaimTests(unittest.IsolatedAsyncioTestCase):
    """An agent-started turn (cron, sessions_send, a sub-agent result) claims its message under the leaf."""

    def setUp(self):
        self.owui = RowsOwui(self)
        self.owui.rows["a1"]["done"] = True

    async def test_a_new_message_goes_under_the_leaf_from_the_rows(self):
        r = await openclaw_pipe._claim_turn_message_now(CHAT, USER, "m1", "run-2", "w1", False)
        self.assertEqual((r["claimed"], r["how"], r["parent"]), (True, "new", "a1"))
        self.assertEqual(self.owui.rows["m1"]["parentId"], "a1")
        self.assertEqual(self.owui.rows["a1"]["childrenIds"], ["m1"])
        self.assertEqual(self.owui.rows["a1"]["content"], "", "the parent is saved whole")
        self.assertEqual(self.owui.chat.current_message_id, "m1")
        self.owui.assert_no_stray_history(self)

    async def test_a_message_that_has_a_row_is_taken_over_not_duplicated(self):
        r = await openclaw_pipe._claim_turn_message_now(CHAT, USER, "a1", "run-3", "w2", True)
        self.assertEqual((r["claimed"], r["how"], r["parent"]), (True, "takeover", "u1"))
        self.assertEqual(self.owui.rows["a1"]["meta"]["openclaw_run"], "run-3")
        self.assertEqual(self.owui.rows["a1"]["parentId"], "u1")


class TurnMetaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owui = RowsOwui(self)

    async def test_the_meta_is_saved_on_the_row_and_nothing_else_is_lost(self):
        self.assertTrue(await openclaw_pipe._set_turn_meta_now(CHAT, "a1", {"openclaw_writer": "w9"}))
        a1 = self.owui.rows["a1"]
        self.assertEqual(a1["meta"], {"openclaw_run": "r1", "openclaw_writer": "w9"})
        self.assertEqual((a1["parentId"], a1["timestamp"], a1["model"]), ("u1", OLD_TS + 1, "openclaw_gateway.m"))
        self.owui.assert_no_stray_history(self)

    async def test_a_message_without_a_row_is_not_invented(self):
        self.assertFalse(await openclaw_pipe._set_turn_meta_now(CHAT, "nope", {"x": 1}))
        self.assertNotIn("nope", self.owui.rows)


class PendingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owui = RowsOwui(self)

    async def test_the_edit_sees_every_message_and_its_saves_land_on_the_rows(self):
        seen = {}

        def edit(messages, chat_meta):
            seen.update(messages)
            chat_meta["openclaw_pending"] = ["a1"]
            meta = (messages.get("a1") or {}).get("meta") or {}
            return "ok", {"a1": {"meta": {**meta, "delivery": "pending"}}}
        self.assertEqual(await openclaw_pipe._pending_txn_now(CHAT, edit), "ok")
        self.assertEqual(set(seen), {"u1", "a1"})
        self.assertEqual(self.owui.rows["a1"]["meta"], {"openclaw_run": "r1", "delivery": "pending"})
        self.assertEqual(self.owui.rows["a1"]["parentId"], "u1")
        self.assertEqual(self.owui.chat.meta, {"openclaw_pending": ["a1"]})
        self.owui.assert_no_stray_history(self)


class TitleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owui = RowsOwui(self)

    async def test_a_title_open_webui_took_from_the_first_message_is_replaced(self):
        self.owui.chat.title = openclaw_pipe._owui_title_from_message("hello there")
        self.assertTrue(await openclaw_pipe._set_title_unless_chosen_now(CHAT, "Greeting"))
        self.assertEqual(self.owui.chat.title, "Greeting")
        self.owui.assert_no_stray_history(self)

    async def test_a_title_the_user_chose_is_kept(self):
        self.owui.chat.title = "My own title"
        self.assertFalse(await openclaw_pipe._set_title_unless_chosen_now(CHAT, "Greeting"))


class ModelIdxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owui = RowsOwui(self)

    async def test_the_model_index_is_read_from_the_message_rows_data(self):
        # The 0.13.5 blob holds no history: a read of the blob's modelIdx finds nothing.
        @contextlib.asynccontextmanager
        async def db_context():
            yield types.SimpleNamespace(execute=mock.AsyncMock(return_value=mock.Mock(first=lambda: None)))
        sa, db_mod = types.ModuleType("sqlalchemy"), types.ModuleType("open_webui.internal.db")
        sa.select = mock.MagicMock()
        db_mod.get_async_db_context = db_context
        sys.modules["open_webui.models.chats"].Chat = mock.MagicMock()
        p = mock.patch.dict(sys.modules, {"sqlalchemy": sa, "open_webui.internal": types.ModuleType("open_webui.internal"),
                                          "open_webui.internal.db": db_mod})
        p.start()
        self.addCleanup(p.stop)
        rows = openclaw_pipe._OwuiHistoryRows(CHAT)

        async def row(message_id, *columns):
            self.assertEqual(columns, ("data",))
            return ({"modelIdx": 1},) if message_id == "a1" else None
        with mock.patch.object(rows, "_row", side_effect=row):
            self.assertEqual(await rows.model_idx("a1"), 1)
            self.assertIsNone(await rows.model_idx("gone"))


class BlobModeIsUnchangedTests(unittest.IsolatedAsyncioTestCase):
    """Without `_without_history` (Open WebUI 0.13.4 and older) the history is still the blob's."""

    def setUp(self):
        self.owui = RowsOwui(self)
        del sys.modules["open_webui.models.chats"].Chats._without_history
        self.owui.chat.chat["history"] = {"currentId": "a1", "messages": copy.deepcopy(self.owui.rows)}

    async def test_the_snapshot_still_writes_the_blob(self):
        await openclaw_pipe._owui_upsert_message_with_statuses_now(CHAT, "a1", {"content": "x", "done": True}, [])
        self.assertEqual(self.owui.chat.chat["history"]["messages"]["a1"]["content"], "x")
        self.assertEqual(self.owui.chat.chat["history"]["messages"]["a1"]["parentId"], "u1")


if __name__ == "__main__":
    unittest.main()
