"""ELI-376: a Gateway command ("/model x", "/new") reaches the Gateway exactly as typed.
The Gateway reads a command from the message's start, so nothing the Pipe adds may go
before it: not the note about an edit or a branch switch, not the voice note. That note
is owed to the next ordinary message instead; a reset (/new, /reset) drops it, since
the session starts afresh."""
import unittest

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

from test_multi_model_unit import HAIKU, MultiModelBase  # noqa: E402

EDIT_NOTE = "[Open WebUI: the user edited their message"


class CommandAsTypedTests(MultiModelBase):

    async def edit_then(self, command):
        """'hello' answered, then edited into `command` (a sibling of it); returns that send."""
        self.message("u0", "user", None, "hello")
        await self.call(HAIKU, [HAIKU], "u0", "a0", "hello")
        self.message("a0", "assistant", "u0", "ok from a0")
        self.message("u1", "user", None, command)
        return await self.call(HAIKU, [HAIKU], "u1", "a1", command)

    async def next_message(self):
        self.message("a1", "assistant", "u1", "ok from a1")
        self.message("u2", "user", "a1", "next")
        return await self.call(HAIKU, [HAIKU], "u2", "a2", "next", parent="a1")

    async def test_a_command_after_an_edit_is_sent_as_typed(self):
        for command in ("/model opus", "  /status"):
            with self.subTest(command):
                self.tree.clear()
                self.sends.clear()
                send = await self.edit_then(command)
                self.assertEqual(send["message"], command)

    async def test_the_note_goes_with_the_next_message_instead(self):
        await self.edit_then("/model opus")
        send = await self.next_message()
        self.assertTrue(send["message"].startswith(EDIT_NOTE), send["message"])
        self.assertTrue(send["message"].endswith("next"))

    async def test_a_reset_drops_the_note(self):
        for command in ("/new", "/RESET", "/new start over"):
            with self.subTest(command):
                self.tree.clear()
                self.sends.clear()
                self.pipe.valves.STATE_DIR = __import__("tempfile").mkdtemp()
                await self.edit_then(command)
                self.assertEqual((await self.next_message())["message"], "next")

    async def test_a_command_gets_no_voice_note(self):
        self.message("u0", "user", None, "/model opus")
        send = await self.call(HAIKU, [HAIKU], "u0", "a0", "/model opus",
                               extra_meta={"features": {"voice": True}})
        self.assertEqual(send["message"], "/model opus")

    def test_the_gateway_s_reset_commands(self):
        for text, reset in (("/new", True), ("/reset", True), ("/New please", True),
                            ("/newer", False), ("/model x", False), ("new", False)):
            with self.subTest(text):
                self.assertEqual(ocp._is_gateway_reset(text), reset)


if __name__ == "__main__":
    unittest.main()
