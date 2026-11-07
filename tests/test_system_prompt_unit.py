"""The chat's system prompt reaches the agent: the prompt set on the model, on the
chat's folder and by the user, read from where Open WebUI keeps them and joined in
Open WebUI's order, goes before every message as a note. Open WebUI's own
instructions in the request's system message (voice mode, code interpreter, skills,
knowledge) do not."""
import sys
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

from test_multi_model_unit import HAIKU, MultiModelBase, _key  # noqa: E402

MODEL = "MODEL_PROMPT be brief"
USER = {"chat_context": {"messages": [{"role": "system", "content": "USER_PROMPT call me Eli"}]}}
FOLDER = "FOLDER_PROMPT answer in French"
# What Open WebUI adds to the request's system message on its own.
OWUI_OWN = "CODE_INTERPRETER use <code_interpreter> tags"


def _folder(prompt, model=""):
    """The folder's and the model's prompt, as their rows would give them."""
    async def folder(metadata, chat_id, user_id):
        return prompt

    async def model_row(metadata):
        return model
    patches = mock.patch.multiple(ocp, _folder_system_prompt=folder, _model_system_prompt=model_row)
    return patches


async def _as_is(text, metadata, user_id):
    return text


class SystemPromptNoteTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        patcher = mock.patch.object(ocp, "_resolved_prompt", _as_is)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def note(self, metadata, folder="", model=""):
        with _folder(folder, model):
            return await ocp._system_prompt_note(metadata, "c1", "u1")

    async def test_the_folder_prompt_is_framed_as_the_chat_s_system_prompt(self):
        note = await self.note({}, FOLDER)
        self.assertTrue(note.startswith("[Open WebUI system prompt for this chat"))
        self.assertIn(f"\n{FOLDER}\n", note)
        self.assertTrue(note.endswith("The user's message follows.]\n\n"))

    async def test_open_webui_s_order_model_then_folder_then_user(self):
        note = await self.note(USER, FOLDER, MODEL)
        model, folder, user = (note.index(p) for p in ("MODEL_PROMPT", "FOLDER_PROMPT", "USER_PROMPT"))
        self.assertLess(model, folder)
        self.assertLess(folder, user)

    async def test_each_alone(self):
        self.assertIn("MODEL_PROMPT", await self.note({}, model=MODEL))
        self.assertIn("USER_PROMPT", await self.note(USER))
        self.assertNotIn("MODEL_PROMPT", await self.note(USER, FOLDER))

    async def test_nothing_set_gives_nothing(self):
        for metadata in ({}, {"model": None}, {"chat_context": None}, {"chat_context": {"messages": [{"role": "system", "content": " "}]}},
                         {"chat_context": {"messages": [{"role": "user", "content": "hi"}]}}):
            self.assertEqual(await self.note(metadata), "", metadata)

    async def test_variables_are_filled_in_as_open_webui_does(self):
        async def fill(text, metadata, user_id):
            return text.replace("{{USER_NAME}}", "Eli")
        with mock.patch.object(ocp, "_resolved_prompt", fill):
            note = await self.note({}, "call me {{USER_NAME}}")
        self.assertIn("call me Eli", note)


class LookupTests(unittest.IsolatedAsyncioTestCase):
    """The stubs have no open_webui.models.folders or .models: a lookup that fails
    is logged and gives no prompt."""

    async def test_a_folder_lookup_that_fails_gives_no_folder_prompt(self):
        self.assertEqual(await ocp._folder_system_prompt({"folder_id": "f1"}, "c1", "u1"), "")

    async def test_a_model_lookup_that_fails_gives_no_model_prompt(self):
        self.assertEqual(await ocp._model_system_prompt({"model": {"id": HAIKU}}), "")

    async def test_no_model_in_the_request_gives_no_model_prompt(self):
        for metadata in ({}, {"model": None}, {"model": {}}):
            self.assertEqual(await ocp._model_system_prompt(metadata), "", metadata)


def _owui_folder_modules(role, allowed, prompt=FOLDER, chat_folder="f1"):
    """sys.modules entries for the Open WebUI rows `_folder_system_prompt` reads: a
    user with `role`, a folder with `prompt`, and an access check answering `allowed`."""
    mods = {name: types.ModuleType(name) for name in (
        "open_webui", "open_webui.models", "open_webui.models.chats", "open_webui.models.folders",
        "open_webui.models.users", "open_webui.utils", "open_webui.utils.access_control",
        "open_webui.utils.access_control.folders")}
    asked = []

    class Chats:
        @staticmethod
        async def get_chat_folder_id(chat_id, user_id):
            return chat_folder

    class Folders:
        @staticmethod
        async def get_folder_by_id(folder_id):
            return types.SimpleNamespace(id=folder_id, data={"system_prompt": prompt})

    class Users:
        @staticmethod
        async def get_user_by_id(user_id):
            return types.SimpleNamespace(id=user_id, role=role)

    async def has_folder_access(user_id, folder, permission, db=None):
        asked.append((user_id, folder.id, permission))
        return allowed
    mods["open_webui.models.chats"].Chats = Chats
    mods["open_webui.models.folders"].Folders = Folders
    mods["open_webui.models.users"].Users = Users
    mods["open_webui.utils.access_control.folders"].has_folder_access = has_folder_access
    return mods, asked


class FolderAccessTests(unittest.IsolatedAsyncioTestCase):
    """Open WebUI's rule (middleware.py): a non-admin gets a folder's prompt only
    with read access to the folder; an admin always does."""

    async def prompt(self, role, allowed, **kw):
        mods, asked = _owui_folder_modules(role, allowed, **kw)
        with mock.patch.dict(sys.modules, mods):
            return await ocp._folder_system_prompt({}, "c1", "u1"), asked

    async def test_a_user_without_read_access_gets_no_folder_prompt(self):
        prompt, asked = await self.prompt("user", False)
        self.assertEqual(prompt, "")
        self.assertEqual(asked, [("u1", "f1", "read")])

    async def test_a_user_with_read_access_gets_it(self):
        prompt, _ = await self.prompt("user", True)
        self.assertEqual(prompt, FOLDER)

    async def test_an_admin_gets_it_without_the_access_check(self):
        prompt, asked = await self.prompt("admin", False)
        self.assertEqual(prompt, FOLDER)
        self.assertEqual(asked, [])

    async def test_a_chat_outside_any_folder_gets_none(self):
        prompt, asked = await self.prompt("admin", True, chat_folder=None)
        self.assertEqual(prompt, "")

    async def test_a_folder_without_a_prompt_gives_none(self):
        prompt, _ = await self.prompt("admin", True, prompt=None)
        self.assertEqual(prompt, "")


class SystemPromptTurnTests(MultiModelBase):

    def setUp(self):
        super().setUp()
        for name, value in (("_resolved_prompt", _as_is),):
            patcher = mock.patch.object(ocp, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_a_chat_in_a_folder_with_a_prompt_sends_it_before_the_message(self):
        self.message("u1", "user", None, "bonjour")
        with _folder(FOLDER):
            send = await self.call(HAIKU, [HAIKU], "u1", "a1", "bonjour")
        self.assertTrue(send["message"].startswith("[Open WebUI system prompt for this chat"))
        self.assertIn(FOLDER, send["message"])
        self.assertTrue(send["message"].endswith("The user's message follows.]\n\nbonjour"))

    async def test_every_turn_carries_it(self):
        with _folder(FOLDER):
            self.message("u1", "user", None, "one")
            s1 = await self.call(HAIKU, [HAIKU], "u1", "a1", "one")
            self.message("a1", "assistant", "u1", "ok from a1")
            self.message("u2", "user", "a1", "two")
            s2 = await self.call(HAIKU, [HAIKU], "u2", "a2", "two", parent="a1")
        for send in (s1, s2):
            self.assertIn(FOLDER, send["message"])

    async def test_it_comes_before_the_voice_note(self):
        self.message("u1", "user", None, "hi")
        with _folder(FOLDER):
            send = await self.call(HAIKU, [HAIKU], "u1", "a1", "hi", extra_meta={"features": {"voice": True}})
        self.assertLess(send["message"].index(FOLDER), send["message"].index("[Open WebUI voice call:"))

    async def test_open_webui_s_own_instructions_in_the_request_are_not_sent(self):
        self.message("u1", "user", None, "hi")
        with _folder(FOLDER):
            send = await self.call(HAIKU, [HAIKU], "u1", "a1", "hi", extra_meta=USER,
                                   request_system=f"{FOLDER}\nUSER_PROMPT call me Eli\n{OWUI_OWN}")
        self.assertIn("USER_PROMPT call me Eli", send["message"])
        self.assertNotIn(OWUI_OWN, send["message"])

    async def test_a_gateway_command_is_sent_without_it(self):
        """The Gateway reads a command from the message's start: a note before it
        would turn "/model x" into a prompt for the agent."""
        parent = None
        for n, command in enumerate(("/model opus", "/new", "  /status")):
            with self.subTest(command):
                self.message(f"u{n}", "user", parent, command)
                with _folder(FOLDER):
                    send = await self.call(HAIKU, [HAIKU], f"u{n}", f"a{n}", command, parent=parent)
                self.assertEqual(send["message"], command)
            self.message(f"a{n}", "assistant", f"u{n}", f"ok from a{n}")
            parent = f"a{n}"

    async def test_the_duplicate_check_compares_only_the_user_s_message(self):
        """ELI-82: a repeat call for the same reply is told apart by what the user
        sent; a folder prompt changed in between must not make it look new."""
        self.message("u1", "user", None, "bonjour")
        with _folder(FOLDER):
            send = await self.call(HAIKU, [HAIKU], "u1", "a1", "bonjour")
        self.assertIn(FOLDER, send["message"])
        self.assertEqual(self.conn.reply_sent(_key(0), "a1")["text"], "bonjour")

    async def test_a_chat_without_one_is_sent_as_it_is(self):
        self.message("u1", "user", None, "hi")
        with _folder(""):
            send = await self.call(HAIKU, [HAIKU], "u1", "a1", "hi")
        self.assertEqual(send["message"], "hi")



class ResumeTests(unittest.IsolatedAsyncioTestCase):
    """A run the Gateway lost is resumed with a new message (DESIGN 2.7); it
    carries the system prompt like any other."""

    async def resume_message(self, note):
        turn = object.__new__(ocp._PipeTurn)
        conn = mock.Mock()
        conn.register_consumer.return_value = object()
        for name, value in (("conn", conn), ("session_key", "agent:main:openwebui-u1-c1"),
                            ("chat_id", "c1"), ("owui_origin_chat_id", "c1"),
                            ("owui_origin_user_id", "u1"), ("resolved_thinking", None),
                            ("owner_notice_before", False), ("owner_resumes", 0),
                            ("visible_message_text", "Step 1 done."), ("text", "check the logs"),
                            ("system_note", note)):
            setattr(turn, name, value)
        sent = []

        async def run_request(_conn, _route, params, timeout=None):
            sent.append(params["message"])
            return {"runId": "run-2"}
        with mock.patch.object(ocp, "_run_request", run_request):
            step = await turn.owner_step({"status": "failed", "hasActiveRun": False,
                                          "lastRunError": "Claude Code process exited with code 137"})
        self.assertEqual(step[0], "switch")
        return sent[0]

    async def test_the_resume_carries_the_system_prompt(self):
        note = "[Open WebUI system prompt for this chat:]\nFOLDER_PROMPT\n[End]\n\n"
        message = await self.resume_message(note)
        self.assertTrue(message.startswith(note))
        self.assertIn(ocp._resume_directive("Step 1 done.", "check the logs"), message)

    async def test_without_one_the_resume_is_unchanged(self):
        self.assertEqual(await self.resume_message(""),
                         ocp._resume_directive("Step 1 done.", "check the logs"))


if __name__ == "__main__":
    unittest.main()
