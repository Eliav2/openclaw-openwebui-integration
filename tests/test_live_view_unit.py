"""Live view of the agent's browser (LIVE_VIEW_URL): a reply that uses a browser
tool gets the configured page as an embed, once, for the users it is set for.

Run: python3 -m pytest -q tests/test_live_view_unit.py
"""
import json
import types
import unittest

import test_pipe_unit as T  # installs the module stubs openclaw_pipe needs

import openclaw_pipe
from openclaw_pipe import Pipe

URL = "https://replay.example/embed?chat={chat_id}&message={message_id}"
CHAT, MSG = "24c342e4-50fa-442e-9b93-c215ed135f3a", "8acdce95-1111-4222-8333-444455556666"


def live_view_url(tool="browser", args='{"action": "open"}', user="u-1", email=None, **valves):
    v = types.SimpleNamespace(**{**Pipe.Valves().model_dump(), "LIVE_VIEW_URL": URL, **valves})
    return openclaw_pipe._live_view_url(v, chat_id=CHAT, message_id=MSG, user_id=user, email=email,
                                        tool_name=tool, args_str=args)


class LiveViewUrlTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual(Pipe.Valves().LIVE_VIEW_URL, "")
        self.assertIsNone(live_view_url(LIVE_VIEW_URL=""))

    def test_the_ids_go_into_the_url(self):
        self.assertEqual(live_view_url(), f"https://replay.example/embed?chat={CHAT}&message={MSG}")

    def test_browser_use_by_tool_or_by_command(self):
        for tool, args in (("browser", "{}"), ("exec", '{"command": "python3 lease.py own -- bun shot.ts"}'),
                           ("Bash", '{"command": "npx playwright test"}'),
                           ("exec", '{"command": "/usr/bin/chromium --headless"}')):
            with self.subTest(tool=tool, args=args):
                self.assertIsNotNone(live_view_url(tool, args))
        for tool, args in (("exec", '{"command": "ls"}'), ("read", '{"path": "browser-notes.md"}'),
                           ("web_fetch", '{"url": "https://example.org"}')):
            with self.subTest(tool=tool, args=args):
                self.assertIsNone(live_view_url(tool, args))

    def test_the_default_on_real_tool_calls(self):
        # ELI-248: on the claude-cli runtime the agent drives its browser with exec
        # running `openclaw browser <verb>`; no tool is called "browser" there
        browser_use = (
            ("exec", {"command": "B=/config/.node_global/bin/openclaw; $B browser --json open https://en.wikipedia.org/wiki/Octopus 2>&1 | tail -20"}),
            ("exec", {"command": "B=x; sleep 15; for i in 1 2; do $B browser press PageDown; done"}),
            ("exec", {"command": "openclaw browser tabs"}),
            ("browser", {"action": "open", "url": "https://x"}),
            ("exec", {"command": "python3 /config/bin/browser-guard/lease.py own --caller qa -- bun shot.ts"}),
        )
        not_browser_use = (
            ("exec", {"command": "/config/.node_global/bin/openclaw browser --help 2>&1 | head -50"}),
            ("exec", {"command": "cd /config/.openclaw/workspace/projects/browser-replay && git status"}),
            ("exec", {"command": "grep -n frame-ancestors /config/.openclaw/workspace/projects/browser-replay/src/server.ts"}),
            # a private browser that is never recorded: its embed could only say so
            ("exec", {"command": "timeout 600 python3 /config/bin/browser-guard/lease.py own --no-record --caller w7-qa -- bun blank_probe.ts"}),
            ("exec", {"command": "systemctl --user restart browser-replay"}),
            ("exec", {"command": "echo the browser opened fine"}),
        )
        for tool, args in browser_use:
            with self.subTest(tool=tool, args=args):
                self.assertIsNotNone(live_view_url(tool, json.dumps(args)))
        for tool, args in not_browser_use:
            with self.subTest(tool=tool, args=args):
                self.assertIsNone(live_view_url(tool, json.dumps(args)))

    def test_only_the_users_it_is_set_for(self):
        self.assertIsNotNone(live_view_url(user="u-1", LIVE_VIEW_USERS="U-1, someone@example.org"))
        self.assertIsNotNone(live_view_url(user="u-2", email="Someone@example.org",
                                           LIVE_VIEW_USERS="u-1,someone@example.org"))
        self.assertIsNone(live_view_url(user="u-2", LIVE_VIEW_USERS="u-1"))

    def test_never_anything_open_webui_would_render_as_html(self):
        # Open WebUI loads an embed string as a URL only when it is one
        self.assertIsNone(live_view_url(LIVE_VIEW_URL="<iframe src=x></iframe>"))
        self.assertIsNone(live_view_url(LIVE_VIEW_URL="javascript:alert(1)"))

    def test_ids_that_are_not_plain_ids_are_refused(self):
        v = types.SimpleNamespace(LIVE_VIEW_URL=URL, LIVE_VIEW_USERS="", LIVE_VIEW_TOOLS="browser")
        for chat in ("a&b=c", "", None, "x" * 200):
            with self.subTest(chat=chat):
                self.assertIsNone(openclaw_pipe._live_view_url(
                    v, chat_id=chat, message_id=MSG, user_id="u", email=None, tool_name="browser", args_str=""))

    def test_a_broken_pattern_turns_it_off(self):
        self.assertIsNone(live_view_url(LIVE_VIEW_TOOLS="(browser"))


class LiveViewReplyTests(unittest.IsolatedAsyncioTestCase):
    """The reply emits the embed at its first browser tool call, and only once."""

    asyncTearDown = T.InlineOwnerRuleTests.asyncTearDown
    _queue = T.InlineOwnerRuleTests._queue
    _delta = T.InlineOwnerRuleTests._delta
    _final = T.NoDroppedMessagesTests._final
    _until_registered = T.InlineOwnerRuleTests._until_registered
    _event = T.InlineOwnerRuleTests._event
    _turn = T.InlineOwnerRuleTests._turn
    USER, CHAT = T.InlineOwnerRuleTests.USER, T.InlineOwnerRuleTests.CHAT

    async def asyncSetUp(self):
        self.events = []
        await T.InlineOwnerRuleTests.asyncSetUp(self)
        self.pipe.valves.LIVE_VIEW_URL = URL

    def _reply(self, text, msg_id):
        async def emitter(event):
            self.events.append(event)
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": text}]}
        meta = {"chat_id": self.CHAT, "user_id": self.USER, "message_id": msg_id}
        return self.pipe._pipe_impl(body, emitter, __user__={"id": self.USER}, __metadata__=meta)

    def _tool(self, phase, name, tcid, args=None):
        data = {"phase": phase, "name": name, "toolCallId": tcid, "args": args or {}}
        if phase == "result":
            data["result"] = "ok"
        return self._event("agent", stream="tool", data=data)

    def embeds(self):
        return [e["data"]["embeds"] for e in self.events if e.get("type") == "embeds"]

    async def test_the_first_browser_call_adds_the_embed_once(self):
        await self._turn(
            self._tool("start", "exec", "t1", {"command": "ls"}), self._tool("result", "exec", "t1"),
            self._tool("start", "browser", "t2", {"action": "open"}), self._tool("result", "browser", "t2"),
            self._tool("start", "browser", "t3", {"action": "snapshot"}), self._tool("result", "browser", "t3"),
            self._final("run-1"))
        self.assertEqual(self.embeds(), [[f"https://replay.example/embed?chat={self.CHAT}&message=m1"]])

    async def test_no_embed_without_browser_use(self):
        await self._turn(self._tool("start", "exec", "t1", {"command": "ls"}),
                         self._tool("result", "exec", "t1"), self._final("run-1"))
        self.assertEqual(self.embeds(), [])

    async def test_no_embed_for_a_user_it_is_not_set_for(self):
        self.pipe.valves.LIVE_VIEW_USERS = "someone-else"
        await self._turn(self._tool("start", "browser", "t1"), self._tool("result", "browser", "t1"),
                         self._final("run-1"))
        self.assertEqual(self.embeds(), [])


if __name__ == "__main__":
    unittest.main()
