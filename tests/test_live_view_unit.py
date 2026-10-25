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
        for tool, args in (("browser", "{}"), ("exec", '{"command": "google-chrome --headless --screenshot https://x"}'),
                           ("Bash", '{"command": "npx playwright test"}'),
                           ("exec", '{"command": "/usr/bin/chromium --headless"}')):
            with self.subTest(tool=tool, args=args):
                self.assertIsNotNone(live_view_url(tool, args))
        for tool, args in (("exec", '{"command": "ls"}'), ("read", '{"path": "browser-notes.md"}'),
                           ("web_fetch", '{"url": "https://example.org"}')):
            with self.subTest(tool=tool, args=args):
                self.assertIsNone(live_view_url(tool, args))

    def test_only_a_real_browser_launch_shows_the_card(self):
        # 248 of 269 live-view cards on one deployment over a week had no browser behind them.
        # The default matched "playwright"/"chromium" anywhere in any tool's arguments: a file being
        # written, a message, a heredoc body, a grep. A card means the agent starts or drives a browser.
        launches = (
            ("browser", {"action": "open", "url": "https://x"}),
            ("exec", {"command": "openclaw browser --json open https://example.org"}),
            ("exec", {"command": "B=/opt/openclaw/bin/openclaw; $B browser screenshot t3"}),
            ("exec", {"command": "google-chrome --headless --screenshot https://x"}),
            ("exec", {"command": "cd app && /usr/bin/chromium --headless --dump-dom https://x"}),
            ("Bash", {"command": "npx playwright test e2e/login.spec.ts"}),
            ("exec", {"command": "uv run --with playwright==1.64.0 python shot.py https://x out.png"}),
        )
        mentions = (
            ("Write", {"file_path": "/tmp/run.py", "content": "from playwright.sync_api import sync_playwright\n"}),
            ("Edit", {"file_path": "src/owners.ts", "old_string": "chromium", "new_string": "chrome"}),
            ("Read", {"file_path": "notes/playwright-tips.md"}),
            ("message", {"action": "send", "message": "The chromium run passed, 3 of 3"}),
            ("sessions_send", {"sessionKey": "s", "message": "Use playwright for the shot"}),
            ("web_search", {"query": "playwright webapp-testing skill"}),
            ("exec", {"command": "cat > run.sh <<'EOF'\n# browser test with playwright\nEOF"}),
            ("Bash", {"command": "grep -n 'pytest\\|playwright' .github/workflows/*.yml"}),
            ("exec", {"command": "echo 'chromium is down' >> notes.txt"}),
        )
        for tool, args in launches:
            with self.subTest(tool=tool, args=args):
                self.assertIsNotNone(live_view_url(tool, json.dumps(args)))
        for tool, args in mentions:
            with self.subTest(tool=tool, args=args):
                self.assertIsNone(live_view_url(tool, json.dumps(args)))

    def test_a_browser_word_counts_only_in_command_position(self):
        launches = (
            "FOO=1 chromium --headless https://x",
            "cd /tmp && chrome --headless https://x",
            "sleep 1; google-chrome --headless https://x",
            "ls | xargs echo; chromium https://x",
            "echo ok || chromium https://x",
            "x=$(chromium --headless --dump-dom https://x)",
            "timeout 60 chromium --headless https://x",
            "nohup env DISPLAY=:1 chromium https://x &",
            "for i in 1 2; do $B browser press PageDown; done",
            "bunx playwright screenshot https://x out.png",
            "playwright codegen https://x",
        )
        not_launches = (
            "grep chromium notes.txt",
            "ls chromium/",
            "cat <<EOF\nchromium https://x\nEOF",
            "echo chromium --headless",
            "echo \"npx playwright test\"",
            "git commit -m 'run playwright test in CI'",
            "python3 -c 'import playwright'",
            "npx playwright install",
            "openclaw browser --help | head -50",
            "pip install playwright",
            "google-chrome --version",
            "/usr/bin/chromium --help",
            "openclaw browser screenshot --help",
            "command -v chromium",
            "which google-chrome",
            "pkill chromium",
        )
        for cmd in launches:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(live_view_url("exec", json.dumps({"command": cmd})))
        for cmd in not_launches:
            with self.subTest(cmd=cmd):
                self.assertIsNone(live_view_url("exec", json.dumps({"command": cmd})))

    def test_only_the_command_argument_is_judged(self):
        # a launch quoted in another argument of a command tool is still not a launch
        self.assertIsNone(live_view_url("exec", json.dumps({"command": "ls", "note": "chromium --headless"})))
        # and an argument string that is no JSON object leaves only the tool's name
        self.assertIsNone(live_view_url("exec", "chromium --headless"))
        self.assertIsNotNone(live_view_url("browser", "not json"))

    def test_the_valve_still_widens_the_default_in_command_position_only(self):
        default = Pipe.Valves().LIVE_VIEW_TOOLS
        valve = default + r"|lease\.py own"
        for cmd in ("python3 /opt/tools/lease.py own --caller qa -- bun shot.ts",
                    "cd /srv && python3 /opt/tools/lease.py own -- bun shot.ts"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(live_view_url("exec", json.dumps({"command": cmd}), LIVE_VIEW_TOOLS=valve))
        for cmd in ("grep -n 'lease.py own' notes.md", "cat > x <<'EOF'\nlease.py own\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(live_view_url("exec", json.dumps({"command": cmd}), LIVE_VIEW_TOOLS=valve))

    def test_the_default_on_real_tool_calls(self):
        # ELI-248: on the claude-cli runtime the agent drives its browser with exec
        # running `openclaw browser <verb>`; no tool is called "browser" there
        browser_use = (
            ("exec", {"command": "B=/opt/openclaw/bin/openclaw; $B browser --json open https://en.wikipedia.org/wiki/Octopus 2>&1 | tail -20"}),
            ("exec", {"command": "B=x; sleep 15; for i in 1 2; do $B browser press PageDown; done"}),
            ("exec", {"command": "openclaw browser tabs"}),
            ("browser", {"action": "open", "url": "https://x"}),
        )
        not_browser_use = (
            ("exec", {"command": "/opt/openclaw/bin/openclaw browser --help 2>&1 | head -50"}),
            ("exec", {"command": "cd /home/user/.openclaw/workspace/projects/browser-tools && git status"}),
            ("exec", {"command": "grep -n frame-ancestors /home/user/.openclaw/workspace/projects/browser-tools/src/server.ts"}),
            # a wrapper script that starts a browser is not known to the default
            ("exec", {"command": "python3 /opt/tools/lease.py own --caller qa -- bun shot.ts"}),
            ("exec", {"command": "systemctl --user restart browser-tools"}),
            ("exec", {"command": "echo the browser opened fine"}),
        )
        for tool, args in browser_use:
            with self.subTest(tool=tool, args=args):
                self.assertIsNotNone(live_view_url(tool, json.dumps(args)))
        for tool, args in not_browser_use:
            with self.subTest(tool=tool, args=args):
                self.assertIsNone(live_view_url(tool, json.dumps(args)))

    def test_the_default_names_no_setup_of_ours(self):
        # ELI-265: the default is generic; a deployment's own wrapper goes in its own valve
        default = Pipe.Valves().LIVE_VIEW_TOOLS
        self.assertNotIn("lease", default)
        cmd = json.dumps({"command": "python3 /opt/tools/lease.py own -- bun shot.ts"})
        self.assertIsNone(live_view_url("exec", cmd))
        self.assertIsNotNone(live_view_url("exec", cmd, LIVE_VIEW_TOOLS=default + r"|lease\.py own"))

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
