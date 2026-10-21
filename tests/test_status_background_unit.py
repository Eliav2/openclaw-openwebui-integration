#!/usr/bin/env python3
"""The Status dialog's Background section and its per-source loading (ELI-163).

Background lists what runs for this chat outside a reply: background shell
commands, media jobs and automation runs (`tasks.list` on 2026.9), the
automations bound to this chat (`cron.list`), and Claude Code background
agents (the chat's own Agent tool cards). Automations that run elsewhere
are not listed, for anyone (ELI-175). Every section of the dialog fills from its own source, once per
open: a slow or failing source shows in its own section only.
"""

import asyncio
import collections
import json
import sys
import types
import unittest
from unittest import mock

import test_status_action_unit  # noqa: F401  (installs the module stubs)
from _test_helpers import action_gateway, RecordingEmitter, run_page
from test_status_action_unit import _FAKE_DOM_JS, _emitted_codes, _open_dialog, _render_modal_open_js

import openclaw_status_action as action
from openclaw_status_action import Action

# Open WebUI ids are UUIDs: the owner check reads the user from the key.
USER, CHAT = "11111111-1111-1111-1111-111111111111", "chat-1"
OWNER = "00000000-0000-0000-0000-00000000000a"
INTRUDER = "22222222-2222-2222-2222-222222222222"
KEY = f"agent:main:openwebui-{USER}-{CHAT}"
OTHER_KEY = "agent:main:openwebui-user-2-chat-9"
NOW = 1_792_183_200_000
CHILD = "agent:main:subagent:aaaaaaaa-0000-0000-0000-000000000001"


# What the 9.3 Gateway answers, as the probe recorded it
# (projects/session-manager/eli163/probe-9.3.json), trimmed.
def _task(kind, status, **kw):
    return {"id": f"t-{kind}-{status}", "kind": kind, "runtime": "cli", "status": status,
            "sessionKey": KEY, "ownerKey": KEY, "createdAt": NOW - 60_000, "startedAt": NOW - 60_000, **kw}


def _job(name, target, **state):
    return {"id": f"j-{name}", "name": name, "enabled": True, "sessionTarget": target,
            "schedule": {"kind": "every", "everyMs": 3_600_000}, "nextRunAtMs": NOW + 600_000,
            "state": state}


EXEC_CARD = [
    {"type": "function_call", "id": "c1", "call_id": "c1", "name": "exec",
     "arguments": json.dumps({"command": "sleep 25; echo done", "background": True})},
    {"type": "function_call_output", "id": "o1", "call_id": "c1",
     "output": [{"type": "output_text", "text": "Command still running (session good-lagoon, pid 2816)."}]},
]
# ELI-232: an agent's exec names its purpose in `title`.
TITLED_COMMAND = ("cd /home/user/.openclaw/workspace/projects/session-manager && "
                  "python3 ci_wake.py 163 --sha 2ef4275 > /tmp/w.log 2>&1")
TITLED_CARD = [
    {"type": "function_call", "id": "c2", "call_id": "c2", "name": "exec",
     "arguments": json.dumps({"command": TITLED_COMMAND, "title": "ci_wake for PR 163", "background": True})},
    {"type": "function_call_output", "id": "o2", "call_id": "c2",
     "output": [{"type": "output_text", "text": "Command still running (session calm-reef, pid 2817)."}]},
]
AGENT_CARD = [
    {"type": "function_call", "id": "a1", "call_id": "a1", "name": "Agent",
     "arguments": json.dumps({"description": "Tax research", "run_in_background": True})},
    {"type": "function_call_output", "id": "ao1", "call_id": "a1",
     "output": [{"type": "output_text", "text": "Async agent launched successfully."}]},
]


def _chat(done=False):
    msgs = {"u": {"id": "u", "parentId": None, "role": "user", "content": "go", "timestamp": 1000},
            "a": {"id": "a", "parentId": "u", "role": "assistant", "timestamp": 1001, "done": done,
                  "output": EXEC_CARD + TITLED_CARD + AGENT_CARD}}
    return {"history": {"currentId": "a", "messages": msgs}}


class ShortCommandTests(unittest.TestCase):
    """ELI-232: a command without a title reads as its first real command."""

    def test_short_commands(self):
        cases = [
            ("sleep 25; echo done", "sleep 25"),
            ("cd /home/user/.openclaw/workspace/projects/x && python3 ci_wake.py 136", "python3 ci_wake.py 136"),
            ('cd /tmp; python3 -c "\nimport json;d=json.load(open(\'x\'))\nprint(d)"', "python3 -c …"),
            ("export GH_CONFIG_DIR=/home/user/.config/gh; R=Eliav2/x; /usr/local/bin/gh run watch 123 --exit-status",
             "gh run watch 123 --exit-status"),
            ("GH_CONFIG_DIR=/home/user/.config/gh /usr/local/bin/gh pr view 5", "gh pr view 5"),
            ("cd /tmp && python3 - <<'EOF'\nprint(\"unbalanced)\nEOF", "python3 - …"),
            ("setsid nohup timeout 60 bin/import-stations north > /tmp/l 2>&1 &", "bin/import-stations north"),
            ("cd /x\nnode -e 'console.log(1)'", "node -e …"),
            ("git log --oneline | head -5", "git log --oneline"),
            ("make build", "make build"),
            ("cd /r && git -c user.name=bot commit -q -m 'x'", "git -c user.name=bot commit -q -m x"),
            ("grep -c TODO src/a.py", "grep -c TODO src/a.py"),
            ("ls /x 2>/dev/null; sleep 2 > /tmp/o", "ls /x"),
            ("sleep 2 > /tmp/o", "sleep 2"),
            ("for n in 142 136; do gh pr view $n; done", "for n in 142 136 …"),
            ("cd /tmp", ""),
            ("echo 'unbalanced", "echo 'unbalanced"),
            ("python3 run.py " + "a" * 100, "python3 run.py " + "a" * 64 + "…"),
        ]
        self.assertEqual([action._short_command(c) for c, _ in cases], [w for _, w in cases])


class RowTests(unittest.TestCase):
    """_background_rows: what belongs to this chat, and how it reads."""

    def rows(self, tasks=(), jobs=(), chat=None):
        chat = chat or _chat()
        return action._background_rows(list(tasks), list(jobs), action._agent_call_rows(chat), KEY,
                                       commands=action._exec_commands(chat))

    def test_a_background_command_is_titled_by_its_cards_title(self):
        """ELI-232: the title the agent gave the exec call, the command's
        first words under it while it runs, the whole command for a tap.
        The Gateway titles it "CLI command"; its sourceId is the exec
        session the chat's tool card names."""
        data = self.rows([_task("exec", "running", title="CLI command", sourceId="calm-reef",
                                progressSummary="Command running")])
        cmd = next(r for r in data["rows"] if r["kind"] == "Command")
        self.assertEqual((cmd["title"], cmd["status"], cmd["preview"], cmd["command"], cmd["endedAt"]),
                         ("ci_wake for PR 163", "running", "python3 ci_wake.py 163 --sha 2ef4275",
                          TITLED_COMMAND, None))

    def test_without_a_title_the_command_reads_short(self):
        """No title on the card: its first real command names it, the whole
        command still a tap away. Its progress is always "Command running",
        which its chip says."""
        data = self.rows([_task("exec", "running", title="CLI command", sourceId="good-lagoon",
                                progressSummary="Command running")])
        cmd = next(r for r in data["rows"] if r["kind"] == "Command")
        self.assertEqual((cmd["title"], cmd["preview"], cmd["command"]),
                         ("sleep 25", None, "sleep 25; echo done"))

    def test_a_finished_command_says_how_it_ended(self):
        """A failure's exit code and its last line of output, without the
        terminal's colors; 2026.9's "Command completed" says nothing the
        chip does not."""
        failed = action._process_task({"processId": "good-lagoon", "name": "sleep 25;", "status": "failed",
                                       "startedAt": NOW - 9000, "endedAt": NOW, "exitCode": 2,
                                       "tail": "building\n\x1b[31mError:\x1b[0m   no   such  file\n\n"}, KEY)
        killed = action._process_task({"processId": "x", "name": "make", "status": "killed",
                                       "startedAt": NOW - 9000, "endedAt": NOW, "exitSignal": "SIGTERM"}, KEY)
        # 2026.10.5 says "completed" whatever the exit code.
        nonzero = action._process_task({"processId": "z", "name": "ls /nope", "status": "completed",
                                        "startedAt": NOW - 9500, "endedAt": NOW, "exitCode": 1, "tail": ""}, KEY)
        data = self.rows([failed, killed, nonzero, _task("exec", "completed", id="t9", sourceId="y", endedAt=NOW,
                                                         terminalSummary="Command completed")])
        self.assertEqual([(r["title"], r["status"], r["preview"]) for r in data["rows"] if r["kind"] == "Command"],
                         [("sleep 25", "failed", "exit 2 · Error: no such file"), ("make", "cancelled", "SIGTERM"),
                          ("ls /nope", "failed", "exit 1"), ("Shell command", "completed", None)])

    def test_kinds_this_section_lists_and_the_ones_it_leaves_to_others(self):
        data = self.rows([
            _task("exec", "completed", sourceId="x", endedAt=NOW, terminalSummary="Command completed"),
            _task("image_generation", "running", title="image_generate"),
            _task("subagent", "running", childSessionKey=CHILD),       # the Subagents section's
            _task("exec", "running", sessionKey=OTHER_KEY, ownerKey=OTHER_KEY),   # another chat's
        ])
        self.assertEqual(sorted((r["kind"], r["status"]) for r in data["rows"]),
                         [("Agent", "running"), ("Command", "completed"), ("Image", "running")])
        done = next(r for r in data["rows"] if r["kind"] == "Command")
        self.assertEqual((done["title"], done["preview"]), ("Shell command", None))

    def test_running_first_then_the_newest_five_finished(self):
        finished = [_task("exec", "completed", id=f"f{n}", startedAt=NOW - n * 1000, sourceId="")
                    for n in range(8)]
        data = self.rows(finished + [_task("exec", "running", id="live", startedAt=NOW - 99_000)],
                         chat={"history": {}})
        self.assertEqual([r["id"] for r in data["rows"]], ["live", "f0", "f1", "f2", "f3", "f4"])
        self.assertEqual(data["total"], 9)

    def test_an_automation_bound_to_this_chat_says_how_it_last_went(self):
        mine = _job("nightly", f"session:{KEY}", lastRunStatus="error", lastError="model 529",
                    consecutiveErrors=3)
        run = _task("automation_run", "completed", sourceId="j-nightly")    # told by its job's row
        data = self.rows([run], [mine, _job("elsewhere", f"session:{OTHER_KEY}")], chat={"history": {}})
        (row,) = data["rows"]
        self.assertEqual((row["title"], row["status"], row["error"], row["failedInARow"], row["job"]),
                         ("nightly", "failed", "model 529", 3, True))

    def test_a_background_agent_runs_until_its_reply_is_done(self):
        self.assertEqual([r["status"] for r in self.rows()["rows"] if r["kind"] == "Agent"], ["running"])
        self.assertEqual([r["status"] for r in self.rows(chat=_chat(done=True))["rows"] if r["kind"] == "Agent"],
                         ["completed"])

    def test_no_task_list_on_this_gateway_is_said_not_failed(self):
        data = action._background_rows(None, [], [], KEY, commands={})
        self.assertFalse(data["tasksAvailable"])
        self.assertTrue(data["automationsAvailable"])


def _gateway(calls, *, fail=(), slow=None, unknown=(), running_child=False, jobs=None, processes=None):
    """A 9.3 Gateway for one open: every request recorded; `fail`: methods
    that raise; `unknown`: methods this Gateway lacks; `slow`: method ->
    Event its answer waits for."""
    children = [{"key": CHILD, "status": "running" if running_child else "done", "spawnedBy": KEY,
                 "label": "research", "startedAt": NOW - 9000, "endedAt": None if running_child else NOW}]

    async def send_request(method, params, timeout=5):
        calls.append(method)
        if slow and method in slow:
            await slow[method].wait()
        if method in fail:
            raise RuntimeError(f"{method} broke")
        if method in unknown:
            raise RuntimeError(f"unknown method: {method}")
        if method == "sessions.describe":
            return {"session": {"modelProvider": "anthropic", "model": "claude-opus-5-5",
                                "contextTokens": 1_000_000, "totalTokens": 34_000}}
        if method == "usage.status":
            return {"providers": [{"provider": "anthropic", "windows": [{"label": "5h", "usedPercent": 38}]}]}
        if method == "sessions.list":
            return {"sessions": children}
        if method == "sessions.preview":
            return {"previews": [{"key": k, "items": [{"role": "assistant", "text": "halfway"}]}
                                 for k in params["keys"]]}
        if method == "tasks.list":
            return {"tasks": [_task("exec", "running", title="CLI command", sourceId="good-lagoon")]}
        if method == "sessions.processes.list":
            assert params == {"key": KEY}, params
            return {"sessionId": "s1", "processes": processes or [], "truncated": False}
        if method == "cron.list":
            return {"jobs": jobs if jobs is not None else [_job("heartbeat", "main")]}
        raise AssertionError(f"unexpected RPC: {method}")

    conn = mock.Mock()
    conn.send_request = mock.AsyncMock(side_effect=send_request)
    conn._valves = lambda: types.SimpleNamespace(OWNER_USERS=OWNER)
    return conn


def _owui(chat_reads):
    chats = types.ModuleType("open_webui.models.chats")

    def get_chat_by_id(chat_id):
        chat_reads.append(chat_id)
        return types.SimpleNamespace(user_id=USER, chat=_chat())
    chats.Chats = types.SimpleNamespace(get_chat_by_id=get_chat_by_id)
    return {"open_webui": types.ModuleType("open_webui"),
            "open_webui.models": types.ModuleType("open_webui.models"),
            "open_webui.models.chats": chats}


def _fills(emitted):
    """{section: the data of its last fill (or {"error": ...})}, in the order
    they first filled, from the execute events a dialog got."""
    out = {}
    for code in _emitted_codes(emitted):
        marker = "if (S && S.sectionError) S.sectionError("
        if marker in code:
            name, message = json.loads("[" + code.split(marker, 1)[1].split(");\n", 1)[0] + "]")
            out[name] = {"error": message}
            continue
        for name in ("context", "limits", "background", "subagents", "agents"):
            if f"getElementById('openclaw-status-section-{name}')" in code and "const DATA = " in code:
                out[name] = json.loads(code.split("const DATA = ", 1)[1].split(";\n", 1)[0])
    return out


class OpenTests(unittest.IsolatedAsyncioTestCase):

    async def _open(self, conn, user=USER, chat_reads=None):
        emitted = []
        with action_gateway(conn), mock.patch.dict(sys.modules, _owui(chat_reads if chat_reads is not None else [])):
            result = await _open_dialog(Action().action({"chat_id": CHAT, "id": "m1", "model": "openclaw.x"},
                                                        __user__={"id": user},
                                                        __event_emitter__=RecordingEmitter(emitted)), conn)
        return result, emitted

    async def test_one_request_per_source_per_open(self):
        """An idle chat: each source read once, the sub-agents' transcripts
        not at all (their section opens folded), Open WebUI's chat once for
        the two sections that use it."""
        calls, chat_reads = [], []
        await self._open(_gateway(calls), chat_reads=chat_reads)
        self.assertEqual(collections.Counter(calls), {
            "sessions.describe": 1, "usage.status": 1, "sessions.list": 1, "tasks.list": 1, "cron.list": 1})
        self.assertEqual(chat_reads, [CHAT])

    async def test_with_a_running_child_the_open_reads_its_transcript_and_follows(self):
        """Its section opens unfolded, so its transcripts are read at once;
        the follow's first read (the child's current row, with its last
        message) is the guard it takes its change events against (ELI-127)."""
        calls = []
        result, emitted = await self._open(_gateway(calls, running_child=True))
        self.assertEqual(result, "following")
        self.assertEqual(collections.Counter(calls), {
            "sessions.describe": 1, "usage.status": 1, "sessions.list": 2, "sessions.preview": 1,
            "tasks.list": 1, "cron.list": 1})
        self.assertEqual(_fills(emitted)["subagents"]["tasks"][0]["progressSummary"], "halfway")

    async def test_every_section_fills(self):
        _result, emitted = await self._open(_gateway([]))
        fills = _fills(emitted)
        self.assertEqual(sorted(fills), ["agents", "background", "context", "limits", "subagents"])
        self.assertTrue(all(not f.get("error") for f in fills.values()), fills)
        bg = fills["background"]
        self.assertEqual([(r["kind"], r["title"]) for r in bg["rows"]],
                         [("Command", "sleep 25"), ("Agent", "Tax research")])

    async def test_a_failing_source_leaves_the_other_sections_intact(self):
        """Each source in turn fails; only the sections that read it say so."""
        readers = {"sessions.describe": {"context", "limits"}, "usage.status": {"limits"},
                   "sessions.list": {"subagents"}, "tasks.list": {"background"}, "cron.list": {"background"}}
        for method, hit in readers.items():
            with self.subTest(failing=method):
                _result, emitted = await self._open(_gateway([], fail=(method,)))
                fills = _fills(emitted)
                self.assertEqual(sorted(fills), ["agents", "background", "context", "limits", "subagents"])
                for name, data in fills.items():
                    broken = bool(data.get("error") or data.get("errors"))
                    self.assertEqual(broken, name in hit, (name, data))

    async def test_a_slow_source_holds_up_its_own_section_only(self):
        """tasks.list answers last: every other section is filled first."""
        gate = asyncio.Event()
        emitted = []
        conn = _gateway([], slow={"tasks.list": gate})
        with action_gateway(conn), mock.patch.dict(sys.modules, _owui([])):
            opening = asyncio.ensure_future(_open_dialog(Action().action(
                {"chat_id": CHAT}, __user__={"id": USER}, __event_emitter__=RecordingEmitter(emitted)), conn))
            for _ in range(50):
                await asyncio.sleep(0)
            before = sorted(_fills(emitted))
            gate.set()
            await asyncio.wait_for(opening, 5)    # bounds the test only
        self.assertEqual(before, ["agents", "context", "limits", "subagents"])
        self.assertIn("background", _fills(emitted))

    async def test_on_2026_10_commands_come_from_the_sessions_processes(self):
        """2026.10 removed `tasks.*`; a session's background commands are
        `sessions.processes.list` (ELI-163). Titled by the card's command."""
        def proc(pid, name, status, **kw):
            return {"processId": pid, "instanceId": "i-" + pid, "name": name, "status": status,
                    "startedAt": NOW - 60_000, "tail": "", "truncated": False, "canStop": False, **kw}
        calls = []
        _result, emitted = await self._open(_gateway(calls, unknown=("tasks.list",), processes=[
            proc("good-lagoon", "sleep 25;", "running", canStop=True),
            proc("calm-reef", "cd /home/user/.openclaw", "failed", endedAt=NOW - 50_000, exitCode=2,
                 tail="ls: cannot access '/nope'\n"),
            proc("old-pine", "make build", "killed", startedAt=NOW - 45_000, endedAt=NOW - 40_000),
        ]))
        bg = _fills(emitted)["background"]
        self.assertTrue(bg["tasksAvailable"])
        self.assertEqual(bg["errors"], [])
        self.assertIn("sessions.processes.list", calls)
        self.assertEqual([(r["kind"], r["title"], r["status"], r["preview"]) for r in bg["rows"]
                          if r["kind"] == "Command"], [
            ("Command", "sleep 25", "running", None),
            ("Command", "make build", "cancelled", None),
            ("Command", "ci_wake for PR 163", "failed", "exit 2 · ls: cannot access '/nope'"),
        ])

    async def test_a_gateway_with_neither_list_says_so_and_still_lists_automations(self):
        mine = _job("mine", f"session:{KEY}")
        _result, emitted = await self._open(
            _gateway([], unknown=("tasks.list", "sessions.processes.list"), jobs=[mine]))
        bg = _fills(emitted)["background"]
        self.assertFalse(bg["tasksAvailable"])
        self.assertEqual(bg["errors"], [])
        self.assertIn("mine", [r["title"] for r in bg["rows"]])

    async def test_no_one_gets_the_gateways_other_automations(self):
        """ELI-175: the dialog is about this chat. Automations that run
        elsewhere are not listed, for an owner (OWNER_USERS) or anyone else;
        the chat's own automation is. Through the Action's own path."""
        for user in (OWNER, INTRUDER):
            with self.subTest(user=user):
                jobs = [_job("gw-heartbeat", "main"), _job("gw-watch", "isolated", lastRunStatus="error"),
                        _job("mine", f"session:agent:main:openwebui-{user}-{CHAT}")]
                _result, emitted = await self._open(_gateway([], jobs=jobs), user=user)
                bg = _fills(emitted)["background"]
                self.assertEqual([r["title"] for r in bg["rows"] if r.get("job")], ["mine"])
                self.assertNotIn("gw-", json.dumps(_emitted_codes(emitted)))
                seen = run_page(_BG_PAGE_JS, {"now": NOW, "open": _render_modal_open_js(IDENTITY),
                                              "fills": [action._render_background_fill_js(bg)],
                                              "read": ["background", "other-automations"], "steps": []})
                self.assertIn("mine", seen["background"]["texts"])
                self.assertFalse(any("Other automations" in t for t in seen["background"]["texts"]))
                self.assertIsNone(seen["other-automations"]["open"], "no fold of other automations")

    async def test_the_page_reads_the_background_section_again(self):
        """ELI-256, mode "background-rows": the open dialog's ask reads only
        the Background section's sources and fills that section alone."""
        calls, chat_reads, emitted = [], [], []
        with action_gateway(_gateway(calls)), mock.patch.dict(sys.modules, _owui(chat_reads)):
            result = await Action().action({"chat_id": CHAT, "mode": "background-rows", "dialogId": "d1"},
                                           __user__={"id": USER}, __event_emitter__=RecordingEmitter(emitted))
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(collections.Counter(calls), {"tasks.list": 1, "cron.list": 1})
        self.assertEqual(chat_reads, [CHAT])
        fills = _fills(emitted)
        self.assertEqual(sorted(fills), ["background"])
        self.assertEqual([(r["kind"], r["title"]) for r in fills["background"]["rows"]],
                         [("Command", "sleep 25"), ("Agent", "Tax research")])
        self.assertIs(fills["background"]["refill"], True)

    async def test_a_refill_that_could_not_read_leaves_the_section_to_the_page(self):
        """ELI-261: a failed read is a refill with its error (the page keeps
        its rows); with no connection nothing is filled at all, so the rows
        shown stay and the page asks again."""
        emitted = []
        with action_gateway(_gateway([], fail=("tasks.list",))), mock.patch.dict(sys.modules, _owui([])):
            await Action().action({"chat_id": CHAT, "mode": "background-rows", "dialogId": "d1"},
                                  __user__={"id": USER}, __event_emitter__=RecordingEmitter(emitted))
        bg = _fills(emitted)["background"]
        self.assertTrue(bg["refill"] and bg["errors"], bg)
        emitted = []
        with mock.patch.object(action, "_get_action_connection", mock.AsyncMock(side_effect=OSError("down"))), \
                mock.patch.dict(sys.modules, _owui([])):
            result = await Action().action({"chat_id": CHAT, "mode": "background-rows", "dialogId": "d1"},
                                           __user__={"id": USER}, __event_emitter__=RecordingEmitter(emitted))
        self.assertEqual(result["status"], "error")
        self.assertEqual(_fills(emitted), {})

    async def test_closing_the_dialog_stops_its_follow(self):
        """The page tells the Action it was dismissed (mode "status-closed"):
        the open request stops at once and reads nothing more."""
        calls, emitted = [], []
        conn = _gateway(calls, running_child=True)
        followed = asyncio.Event()

        class _Never:
            async def wait(self):
                followed.set()
                await asyncio.Future()
        conn.session_change_event = conn.session_child_event = lambda _key: _Never()
        with action_gateway(conn), mock.patch.dict(sys.modules, _owui([])):
            opening = asyncio.ensure_future(Action().action(
                {"chat_id": CHAT}, __user__={"id": USER}, __event_emitter__=RecordingEmitter(emitted)))
            await asyncio.wait_for(followed.wait(), 5)
            code = next(c for c in _emitted_codes(emitted) if "const IDENTITY = " in c)
            dialog_id = json.loads(code.split("const IDENTITY = ", 1)[1].split(";\n", 1)[0])["dialogId"]
            # Someone else's close is ignored.
            await Action().action({"chat_id": CHAT, "mode": "status-closed", "dialogId": dialog_id},
                                  __user__={"id": INTRUDER}, __event_emitter__=RecordingEmitter([]))
            await asyncio.sleep(0)
            self.assertFalse(opening.done())
            seen = len(calls)
            await Action().action({"chat_id": CHAT, "mode": "status-closed", "dialogId": dialog_id},
                                  __user__={"id": USER}, __event_emitter__=RecordingEmitter([]))
            self.assertEqual(await asyncio.wait_for(opening, 5), {"status": "ok"})
        self.assertEqual(len(calls), seen)


_BG_PAGE_JS = _FAKE_DOM_JS + r"""
const fetches = [];
global.localStorage = { getItem: () => 'tok' };
global.fetch = (url, opts) => { fetches.push(JSON.parse(opts.body)); return Promise.resolve({}); };
// Timeouts the steps fire by hand ({"wait": true} fires every pending one).
const timeouts = new Map();
global.setTimeout = (fn) => { const id = nextTimer++; timeouts.set(id, fn); return id; };
global.clearTimeout = (id) => { timeouts.delete(id); };
run(input.open);
input.fills.forEach(run);
const section = (n) => document.getElementById('openclaw-status-section-' + n);
const texts = (n) => section(n) ? section(n).walk().filter((e) => e.children.length === 0 && e.text).map((e) => e.text) : null;
// A fold by name.
const fold = (n) => document.body.walk().find((e) => e.dataset.openclawFold === n);
const out = {};
for (const step of input.steps || []) {
  if (step.tap) fold(step.tap).children[0].onclick();
  if (step.refill) run(input.fills[step.refill]);
  if (step.tapRow != null) section('background').walk().filter((e) => e.dataset.openclawBgRow)[step.tapRow].onclick();
  if (step.close) document.getElementById('openclaw-status-modal-root').walk()
    .find((e) => e.tagName === 'BUTTON' && e.textContent === '×').onclick();
  if (step.wait) {
    const due = [...timeouts.values()];
    timeouts.clear();
    due.forEach((fn) => fn());
  }
}
out.timeouts = timeouts.size;
for (const n of input.read) out[n] = { open: fold(n) ? fold(n).dataset.open : null, texts: texts(n) };
if (section('background')) out.hovers = section('background').walk().filter((e) => e.dataset.openclawBgRow)
  .map((e) => [e.tagName, e.title || null]);
out.fetches = fetches;
console.log(JSON.stringify(out));
"""

IDENTITY = {"chatId": "c", "messageId": "m", "sessionId": "s", "owuiModel": "x", "dialogId": "d1"}


def _bg(rows, **kw):
    return action._render_background_fill_js({
        "rows": rows, "total": len(rows),
        "tasksAvailable": True, "automationsAvailable": True, "errors": [], "source": "pipe",
        "fetchedAt": NOW, "error": None, **kw})


class BackgroundPageTests(unittest.TestCase):

    def page(self, fills, read, steps=()):
        return run_page(_BG_PAGE_JS, {"now": NOW, "open": _render_modal_open_js(IDENTITY), "fills": fills,
                                      "read": read, "steps": list(steps)})

    CMD = {"id": "t1", "kind": "Command", "title": "sleep 25; echo done", "status": "running",
           "startedAt": NOW - 65_000, "endedAt": None, "preview": "Command running"}
    JOB = {"id": "j1", "kind": "Automation", "job": True, "title": "nightly", "status": "failed",
           "startedAt": None, "endedAt": None, "error": "model 529", "failedInARow": 3,
           "nextRunAtMs": NOW + 3_600_000}

    def test_open_while_something_runs_with_the_count_in_its_title(self):
        seen = self.page([_bg([self.CMD, self.JOB])], ["background"])["background"]
        self.assertEqual(seen["open"], "true")
        self.assertEqual(seen["texts"][:5], ["Background (2)", "sleep 25; echo done", "Running", "1m",
                                             "Command · Command running"])
        self.assertEqual(seen["texts"][5:8], ["nightly", "Failed", "Automation · failed 3 times in a row: model 529"])

    def test_a_tap_on_a_command_shows_the_whole_command(self):
        """ELI-232: the title says what it is for; hover or a tap gives the
        command itself, and a second tap hides it."""
        cmd = {**self.CMD, "title": "ci_wake for PR 163", "preview": "python3 ci_wake.py 163",
               "command": TITLED_COMMAND}
        shown = self.page([_bg([cmd, self.JOB])], ["background"], [{"tapRow": 0}])
        self.assertEqual(shown["hovers"], [["BUTTON", TITLED_COMMAND], ["DIV", None]])
        self.assertEqual(shown["background"]["texts"][1:6],
                         ["ci_wake for PR 163", "Running", "1m", "Command · python3 ci_wake.py 163", TITLED_COMMAND])
        hidden = self.page([_bg([cmd])], ["background"], [{"tapRow": 0}, {"tapRow": 0}])
        self.assertNotIn(TITLED_COMMAND, hidden["background"]["texts"])

    def test_folded_when_nothing_runs_and_a_tap_wins_over_a_refill(self):
        idle = _bg([{**self.CMD, "status": "completed", "endedAt": NOW}])
        seen = self.page([idle, _bg([self.CMD])], ["background"], [{"tap": "background"}, {"refill": 1}])
        self.assertEqual(seen["background"]["open"], "true")
        self.assertEqual(self.page([idle], ["background"])["background"]["open"], "false")

    def test_while_a_row_runs_the_open_dialog_reads_the_section_again(self):
        """ELI-256: the Gateway pushes nothing when a background task ends,
        and the dialog left open kept its row "Running" until it was opened
        again. While a row runs the page asks for the section again, one ask
        at a time; a fill with nothing running stops it, and so does closing
        the dialog."""
        modes = lambda seen: [f["mode"] for f in seen["fetches"]]  # noqa: E731
        done = _bg([{**self.CMD, "status": "completed", "endedAt": NOW}])
        running = self.page([_bg([self.CMD])], ["background"], [{"wait": True}])
        self.assertEqual(modes(running), ["background-rows"])
        self.assertEqual(running["fetches"][0]["dialogId"], "d1")
        self.assertEqual(running["timeouts"], 0, "a second ask while the first is out")
        ended = self.page([_bg([self.CMD]), done], ["background"], [{"refill": 1}, {"wait": True}])
        self.assertEqual(modes(ended), [])
        self.assertIn("Done", ended["background"]["texts"])
        closed = self.page([_bg([self.CMD])], [], [{"close": True}, {"wait": True}])
        self.assertEqual(modes(closed), ["status-closed"])
        self.assertEqual(self.page([done], [], [{"wait": True}])["fetches"], [])

    GONE = "Could not read background tasks: Session process access changed; refresh the conversation."

    def test_a_refill_that_failed_keeps_the_rows_and_the_follow(self):
        """ELI-261: on OpenClaw 2026.10.10 a read mid-follow can fail
        ("Session process access changed"). Its fill had no running row, so
        the follow ended and the row never turned Done. A failed refill keeps
        the rows shown and asks again; only a good read with nothing running
        ends the follow."""
        modes = lambda seen: [f["mode"] for f in seen["fetches"]]  # noqa: E731
        failed = _bg([self.JOB], errors=[self.GONE], refill=True)
        done = _bg([{**self.CMD, "status": "completed", "endedAt": NOW}], refill=True)
        kept = self.page([_bg([self.CMD]), failed], ["background"], [{"refill": 1}, {"wait": True}])
        self.assertEqual(modes(kept), ["background-rows"])
        self.assertEqual(kept["background"]["texts"][1:3], ["sleep 25; echo done", "Running"])
        self.assertNotIn(self.GONE, kept["background"]["texts"])
        ended = self.page([_bg([self.CMD]), failed, done], ["background"],
                          [{"refill": 1}, {"wait": True}, {"refill": 2}, {"wait": True}])
        self.assertEqual(modes(ended), ["background-rows"])
        self.assertIn("Done", ended["background"]["texts"])
        self.assertEqual(ended["timeouts"], 0)

    def test_an_open_whose_read_failed_says_so_and_follows(self):
        seen = self.page([_bg([], errors=[self.GONE])], ["background"], [{"wait": True}])
        self.assertIn(self.GONE, seen["background"]["texts"])
        self.assertEqual([f["mode"] for f in seen["fetches"]], ["background-rows"])

    def test_a_command_shown_stays_shown_when_the_section_is_read_again(self):
        cmd = {**self.CMD, "command": TITLED_COMMAND}
        seen = self.page([_bg([cmd]), _bg([{**cmd, "status": "completed", "endedAt": NOW}])], ["background"],
                         [{"tapRow": 0}, {"refill": 1}])
        self.assertIn(TITLED_COMMAND, seen["background"]["texts"])

    def test_nothing_and_not_available_are_said(self):
        seen = self.page([_bg([], tasksAvailable=False)], ["background"])["background"]
        self.assertEqual(seen["texts"], ["Background (0)",
                                         "Background commands and media jobs are not available on this Gateway.",
                                         "Nothing in the background for this chat."])

    def test_a_folded_subagents_section_asks_for_its_rows_once_on_its_first_tap(self):
        pending = action._render_subagents_fill_js({
            "tasks": [{"id": CHILD, "title": "research", "status": "completed", "startedAt": NOW - 9000,
                       "endedAt": NOW}], "total": 1, "pending": True, "source": "pipe", "fetchedAt": NOW,
            "error": None})
        seen = self.page([pending], ["subagents"], [{"tap": "subagents"}, {"tap": "subagents"},
                                                    {"tap": "subagents"}])
        self.assertEqual([f["mode"] for f in seen["fetches"]], ["subagent-rows"])
        self.assertEqual(seen["fetches"][0]["dialogId"], "d1")
        self.assertEqual(seen["subagents"]["texts"][0], "Subagents (1)")

    def test_drawn_again_still_waiting_while_open_it_asks_again(self):
        """ELI-268: a tap whose ask was lost (a dropped phone connection),
        then the section drawn again still waiting for its rows: the fold is
        open and was asked once, so a skeleton waited for a tap already made.
        Folded, drawing it again asks nothing."""
        pending = action._render_subagents_fill_js({
            "tasks": [{"id": CHILD, "title": "research", "status": "completed", "startedAt": NOW - 9000,
                       "endedAt": NOW}], "total": 1, "pending": True, "source": "pipe", "fetchedAt": NOW,
            "error": None})
        seen = self.page([pending, pending], ["subagents"], [{"tap": "subagents"}, {"refill": 1}])
        self.assertEqual([f["mode"] for f in seen["fetches"]], ["subagent-rows", "subagent-rows"])
        self.assertEqual(self.page([pending, pending], ["subagents"], [{"refill": 1}])["fetches"], [])

    def test_closing_the_dialog_tells_the_action(self):
        seen = self.page([], [], [{"close": True}])
        self.assertEqual([(f["mode"], f["dialogId"]) for f in seen["fetches"]], [("status-closed", "d1")])

    def test_one_failing_section_shows_its_error_under_its_title(self):
        seen = self.page([_bg([self.CMD]), action._render_section_error_js("limits", "usage.status broke")],
                         ["background"])
        self.assertEqual(seen["background"]["texts"][0], "Background (1)")
        out = run_page(_FAKE_DOM_JS + r"""
run(input.open); run(input.fill);
console.log(JSON.stringify(document.getElementById('openclaw-status-section-limits').textContent));
""", {"now": NOW, "open": _render_modal_open_js(IDENTITY),
      "fill": action._render_section_error_js("limits", "usage.status broke")})
        self.assertEqual(out, "Rate limitsusage.status broke")


if __name__ == "__main__":
    unittest.main()
