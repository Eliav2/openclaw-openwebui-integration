#!/usr/bin/env python3
"""Unit tests for scripts/owui_api.py (the request shape send_message sends)."""

import importlib.util
import os
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "owui_api", os.path.join(ROOT, "scripts", "owui_api.py"),
)
owui_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(owui_api)


class SendMessageTurnShapeTests(unittest.TestCase):
    """OWUI 0.12 inserts a chat turn server-side (`Chats.insert_chat_turn`)
    and answers 409 when `chat_id` arrives without an assistant `id` and a
    `user_message` carrying its own `id`. The user message must be parented
    on the chat's current leaf, or the reply starts a detached branch."""

    def _client(self, chat):
        c = owui_api.OwuiClient("http://owui.test")
        c.get_chat = mock.Mock(return_value=chat)
        c._post = mock.Mock(return_value={"choices": []})
        return c

    def _sent_body(self, c):
        path, body = c._post.call_args.args
        self.assertEqual(path, "/api/chat/completions")
        return body

    def test_new_chat_sends_turn_ids_with_no_parent(self):
        c = self._client({"chat": {"history": {"messages": {}, "currentId": None}}})
        c.send_message("chat-1", "hello", model="openclaw_gateway.default")
        body = self._sent_body(c)
        self.assertTrue(body["id"])
        um = body["user_message"]
        self.assertTrue(um["id"])
        self.assertNotEqual(um["id"], body["id"])
        self.assertEqual(um["role"], "user")
        self.assertEqual(um["content"], "hello")
        self.assertIsNone(um["parentId"])
        self.assertEqual(body["chat_id"], "chat-1")

    def test_follow_up_is_parented_on_the_current_leaf(self):
        chat = {"chat": {"history": {"currentId": "u1", "messages": {
            "u1": {"role": "user", "childrenIds": ["a1"]},
            "a1": {"role": "assistant", "childrenIds": []},
        }}}}
        c = self._client(chat)
        c.send_message("chat-1", "thanks")
        self.assertEqual(self._sent_body(c)["user_message"]["parentId"], "a1")

    def test_each_call_uses_fresh_ids(self):
        c = self._client({"chat": {"history": {"messages": {}, "currentId": None}}})
        c.send_message("chat-1", "one")
        first = self._sent_body(c)
        c.send_message("chat-1", "two")
        second = self._sent_body(c)
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["user_message"]["id"], second["user_message"]["id"])

    def test_timeout_reaches_the_request(self):
        c = self._client({"chat": {"history": {"messages": {}, "currentId": None}}})
        c.send_message("chat-1", "hello", timeout=90)
        self.assertEqual(c._post.call_args.kwargs.get("timeout"), 90)


if __name__ == "__main__":
    unittest.main()
