"""Slash commands as Open WebUI Prompts (SYNC_SLASH_COMMANDS): the Gateway's
commands become Workspace Prompts in the `/` menu. The sync owns only the
prompts in its ledger: it never changes or deletes a prompt someone made, a
lost ledger deletes nothing, a failed write is retried, and turning it off
deletes what it made.

Run: python3 -m pytest -q tests/test_slash_prompts_unit.py
"""
import asyncio
import os
import tempfile
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

STATUS = {"name": "status", "textAliases": ["/status"], "description": "Show current status.",
          "source": "native", "scope": "both", "acceptsArgs": False}
SKILL = {"name": "skill", "textAliases": ["/skill"], "description": "Run a skill by name.",
         "source": "native", "scope": "both", "acceptsArgs": True,
         "args": [{"name": "name", "type": "string", "required": True},
                  {"name": "input", "type": "string"}]}
EXPORT = {"name": "export-session", "textAliases": ["/export-session", "/export"],
          "description": "Export current session.", "source": "native", "scope": "both"}
STOP = {"name": "stop", "textAliases": ["/stop"], "description": "Stop the current run.",
        "source": "native", "scope": "both"}
NATIVE_ONLY = {"name": "focus", "description": "Native menu only.", "scope": "native"}


def valves(**kw):
    base = dict(SYNC_SLASH_COMMANDS=True, SLASH_COMMANDS_EXCLUDE="stop,new,reset,model",
                PROMPT_PREFIX="", SYNC_INTERVAL_MIN=30, AGENT_ID="main")
    base.update(kw)
    return types.SimpleNamespace(**base)


class FakeStore:
    """Open WebUI's prompts table: command -> row."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.fail = set()
        self.calls = []
        self._n = 0

    async def list(self):
        return {c: dict(r) for c, r in self.rows.items()}

    async def add(self, want):
        self.calls.append(("add", want["command"]))
        if want["command"] in self.fail:
            return None
        self._n += 1
        pid = f"p{self._n}"
        self.rows[want["command"]] = {"id": pid, "name": want["name"], "content": want["content"],
                                      "tags": want["tags"]}
        return pid

    async def update(self, pid, want):
        self.calls.append(("update", want["command"]))
        if want["command"] in self.fail:
            return False
        self.rows[want["command"]].update(name=want["name"], content=want["content"], tags=want["tags"])
        return True

    async def delete(self, pid):
        cmd = next(c for c, r in self.rows.items() if r["id"] == pid)
        self.calls.append(("delete", cmd))
        if cmd in self.fail:
            return False
        del self.rows[cmd]
        return True


class FakeConn:
    def __init__(self, commands, revision="r1"):
        self.commands = commands
        self.revision = revision
        self.requests = []

    async def send_request(self, method, params, timeout=10):
        self.requests.append((method, dict(params)))
        if params.get("ifRevision") == self.revision:
            return {"unchanged": True, "revision": self.revision}
        return {"commands": self.commands, "revision": self.revision}


class PromptShapeTests(unittest.TestCase):

    def test_a_command_becomes_its_own_prompt(self):
        p = ocp._slash_prompt_for(STATUS, "")
        self.assertEqual(p["command"], "status")
        self.assertEqual(p["content"], "/status ")
        self.assertEqual(p["name"], "Show current status.")
        self.assertEqual(p["tags"], ["openclaw"])

    def test_required_args_are_inputs_and_optional_ones_are_typed_after(self):
        self.assertEqual(ocp._slash_prompt_for(SKILL, "")["content"], "/skill {{name}} ")

    def test_the_prefix_and_characters_open_webui_rejects(self):
        p = ocp._slash_prompt_for({"name": "a.b c", "description": "x"}, "oc_")
        self.assertEqual(p["command"], "oc_a_b_c")
        self.assertEqual(p["content"], "/a.b c ")

    def test_a_long_description_is_cut_and_a_missing_one_named(self):
        long = ocp._slash_prompt_for(dict(STATUS, description="word " * 60), "")["name"]
        self.assertTrue(len(long) <= 120 and long.endswith("..."), long)
        self.assertEqual(ocp._slash_prompt_for({"name": "x"}, "")["name"], "OpenClaw /x")

    def test_the_first_alias_is_what_is_typed(self):
        self.assertEqual(ocp._slash_prompt_for(EXPORT, "")["content"], "/export-session ")

    def test_excluded_native_only_and_duplicate_commands_are_left_out(self):
        settings = ocp._slash_prompts_settings(valves())
        dup = dict(STATUS, name="Status")
        wanted = ocp._slash_prompts_wanted([STATUS, STOP, NATIVE_ONLY, dup, SKILL], settings)
        self.assertEqual([w["command"] for w in wanted], ["status", "skill"])

    def test_the_defaults_leave_out_what_open_webui_runs_itself(self):
        v = ocp.Pipe.Valves()
        self.assertFalse(v.SYNC_SLASH_COMMANDS)
        self.assertEqual(v.PROMPT_PREFIX, "")
        self.assertEqual(v.SYNC_INTERVAL_MIN, 30)
        self.assertEqual(ocp._slash_prompt_excludes(v.SLASH_COMMANDS_EXCLUDE),
                         {"stop", "new", "reset", "model", "status", "compact"})

    def test_the_exclude_list_takes_slashes_spaces_and_case(self):
        settings = ocp._slash_prompts_settings(valves(SLASH_COMMANDS_EXCLUDE=" /Status , skill"))
        self.assertEqual(ocp._slash_prompts_wanted([STATUS, SKILL, EXPORT], settings)[0]["command"],
                         "export-session")


class PlanTests(unittest.TestCase):

    def wanted(self, *cmds):
        return ocp._slash_prompts_wanted(cmds, ocp._slash_prompts_settings(valves()))

    def test_a_new_command_is_added(self):
        plan = ocp._slash_prompts_plan(self.wanted(STATUS), {}, {})
        self.assertEqual([w["command"] for w in plan["add"]], ["status"])

    def test_an_owned_prompt_that_changed_is_updated_and_an_equal_one_left(self):
        want = self.wanted(STATUS)[0]
        same = {"status": {"id": "p1", "name": want["name"], "content": want["content"], "tags": ["openclaw"]}}
        self.assertEqual(ocp._slash_prompts_plan([want], same, {"status": "p1"})["update"], [])
        changed = {"status": dict(same["status"], content="/status old")}
        self.assertEqual(ocp._slash_prompts_plan([want], changed, {"status": "p1"})["update"], [("p1", want)])

    def test_a_hand_made_prompt_with_the_same_command_is_skipped(self):
        mine = {"status": {"id": "hand", "name": "Mine", "content": "/status", "tags": []}}
        plan = ocp._slash_prompts_plan(self.wanted(STATUS), mine, {})
        self.assertEqual(plan["skip"], ["status"])
        self.assertEqual((plan["add"], plan["update"], plan["delete"]), ([], [], []))

    def test_a_removed_command_deletes_only_its_own_prompt(self):
        rows = {"gone": {"id": "p9", "name": "", "content": "", "tags": []}}
        plan = ocp._slash_prompts_plan([], rows, {"gone": "p9"})
        self.assertEqual(plan["delete"], [("gone", "p9")])

    def test_a_replaced_prompt_is_forgotten_not_deleted(self):
        rows = {"gone": {"id": "someone-else", "name": "", "content": "", "tags": []}}
        plan = ocp._slash_prompts_plan([], rows, {"gone": "p9"})
        self.assertEqual((plan["delete"], plan["forget"]), ([], ["gone"]))
        plan = ocp._slash_prompts_plan(self.wanted(STATUS), {"status": rows["gone"]}, {"status": "p9"})
        self.assertEqual((plan["skip"], plan["forget"], plan["update"]), (["status"], ["status"], []))

    def test_a_lost_ledger_deletes_nothing(self):
        rows = {"old": {"id": "p1", "name": "", "content": "", "tags": ["openclaw"]}}
        plan = ocp._slash_prompts_plan([], rows, {})
        self.assertEqual(plan["delete"], [])


class SyncTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="oc-slash-")
        for patcher in (mock.patch.dict(os.environ, {"OPENCLAW_BRIDGE_STATE_DIR": self.dir}),
                        mock.patch.dict(ocp._slash_prompts_state, {"legacy": False})):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def sync(self, store, conn, **kw):
        async def getter():
            return conn
        return await ocp._slash_prompts_sync(valves(**kw), getter, store)

    def ledger(self):
        return ocp._slash_prompts_ledger_read()

    async def test_first_sync_adds_records_and_keeps_hand_made_prompts(self):
        store = FakeStore({"status": {"id": "hand", "name": "Mine", "content": "/status", "tags": []}})
        conn = FakeConn([STATUS, SKILL, STOP])
        self.assertEqual(await self.sync(store, conn), "synced")
        self.assertEqual(store.rows["status"]["id"], "hand")
        self.assertEqual(store.rows["skill"]["content"], "/skill {{name}} ")
        self.assertNotIn("stop", store.rows)
        self.assertEqual(self.ledger()["prompts"], {"skill": "p1"})
        self.assertEqual(self.ledger()["revision"], "r1")

    async def test_an_unchanged_revision_is_one_small_request_and_no_writes(self):
        store, conn = FakeStore(), FakeConn([STATUS])
        await self.sync(store, conn)
        store.calls.clear()
        self.assertEqual(await self.sync(store, conn), "unchanged")
        self.assertEqual(conn.requests[-1][1].get("ifRevision"), "r1")
        self.assertEqual(store.calls, [])

    async def test_a_failed_write_keeps_the_revision_unrecorded_and_is_retried(self):
        store, conn = FakeStore(), FakeConn([STATUS, SKILL])
        store.fail.add("skill")
        self.assertEqual(await self.sync(store, conn), "partial")
        self.assertIsNone(self.ledger()["revision"])
        self.assertEqual(self.ledger()["prompts"], {"status": "p1"})
        store.fail.clear()
        self.assertEqual(await self.sync(store, conn), "synced")
        self.assertNotIn("ifRevision", conn.requests[-1][1])
        self.assertEqual(set(self.ledger()["prompts"]), {"status", "skill"})
        self.assertEqual(store.rows["status"]["id"], "p1")  # its own, not skipped

    async def test_each_add_is_on_disk_before_the_next_write(self):
        store, conn = FakeStore(), FakeConn([STATUS, SKILL])
        seen = []
        orig = store.add

        async def add(want):
            seen.append(dict(self.ledger()["prompts"]))
            return await orig(want)
        store.add = add
        await self.sync(store, conn)
        self.assertEqual(seen, [{}, {"status": "p1"}])

    async def test_a_command_the_gateway_dropped_is_deleted(self):
        store, conn = FakeStore(), FakeConn([STATUS, SKILL])
        await self.sync(store, conn)
        conn.commands, conn.revision = [STATUS], "r2"
        self.assertEqual(await self.sync(store, conn), "synced")
        self.assertEqual(set(store.rows), {"status"})
        self.assertEqual(set(self.ledger()["prompts"]), {"status"})

    async def test_a_changed_prefix_resyncs_at_once_and_renames(self):
        store, conn = FakeStore(), FakeConn([STATUS])
        await self.sync(store, conn)
        self.assertEqual(await self.sync(store, conn, PROMPT_PREFIX="oc_"), "synced")
        self.assertEqual(set(store.rows), {"oc_status"})

    async def test_turning_it_off_deletes_only_its_prompts_and_on_again_resyncs(self):
        store = FakeStore({"mine": {"id": "hand", "name": "Mine", "content": "x", "tags": []}})
        conn = FakeConn([STATUS, SKILL])
        await self.sync(store, conn)
        self.assertEqual(await self.sync(store, conn, SYNC_SLASH_COMMANDS=False), "removed")
        self.assertEqual(set(store.rows), {"mine"})
        self.assertEqual(self.ledger()["prompts"], {})
        self.assertEqual(await self.sync(store, conn, SYNC_SLASH_COMMANDS=False), "off")
        self.assertEqual(await self.sync(store, conn), "synced")
        self.assertEqual(set(store.rows), {"mine", "status", "skill"})

    async def test_a_lost_ledger_adopts_nothing_and_deletes_nothing(self):
        store, conn = FakeStore(), FakeConn([STATUS])
        await self.sync(store, conn)
        os.unlink(ocp._slash_prompts_ledger_path())
        conn.revision = "r2"
        self.assertEqual(await self.sync(store, conn), "synced")
        self.assertEqual(store.rows["status"]["id"], "p1")
        self.assertEqual(self.ledger()["prompts"], {})
        self.assertEqual(await self.sync(store, conn, SYNC_SLASH_COMMANDS=False), "off")
        self.assertIn("status", store.rows)

    async def test_an_older_gateway_is_read_with_commands_list_and_its_hash(self):
        class Older(FakeConn):
            async def send_request(self, method, params, timeout=10):
                self.requests.append((method, dict(params)))
                if method == "chat.metadata":
                    raise ocp.GatewayError("invalid chat.metadata params: at root: unexpected property 'includeModels'")
                return {"commands": self.commands}
        store, conn = FakeStore(), Older([STATUS])
        self.assertEqual(await self.sync(store, conn), "synced")
        self.assertEqual(set(store.rows), {"status"})
        store.calls.clear()
        self.assertEqual(await self.sync(store, conn), "unchanged")
        self.assertEqual([m for m, _ in conn.requests], ["chat.metadata", "commands.list", "commands.list"])
        self.assertEqual(conn.requests[-1][1], {"agentId": "main", "scope": "text", "includeArgs": True})
        conn.commands = [STATUS, SKILL]
        self.assertEqual(await self.sync(store, conn), "synced")
        self.assertEqual(set(store.rows), {"status", "skill"})

    async def test_a_gateway_error_writes_nothing(self):
        class Down:
            async def send_request(self, *a, **k):
                raise ocp.GatewayError("down")
        store = FakeStore()
        self.assertEqual(await self.sync(store, Down()), "error")
        self.assertEqual(store.calls, [])

    async def test_parallel_checks_run_one_sync(self):
        store, conn = FakeStore(), FakeConn([STATUS])
        gate = asyncio.Event()
        orig = conn.send_request

        async def slow(*a, **k):
            await gate.wait()
            return await orig(*a, **k)
        conn.send_request = slow
        first = asyncio.create_task(self.sync(store, conn))
        await asyncio.sleep(0)
        self.assertEqual(await self.sync(store, conn), "busy")
        gate.set()
        self.assertEqual(await first, "synced")
        self.assertEqual(store.calls, [("add", "status")])


class TriggerTests(unittest.TestCase):

    def setUp(self):
        self.state = mock.patch.dict(ocp._slash_prompts_state, {
            "checked_at": None, "pending": False, "settings": None, "ledger_empty": None, "valves": None})
        self.state.start()
        self.addCleanup(self.state.stop)
        self.spawned = []
        for target, value in (("spawn_background", lambda coro: (self.spawned.append(coro), coro.close())),
                              ("_slash_prompts_inside_owui", lambda: True)):
            p = mock.patch.object(ocp, target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_first_message_checks_then_only_after_the_interval(self):
        v = valves()
        ocp._ensure_slash_prompts(lambda: v)
        self.assertEqual(len(self.spawned), 1)
        ocp._slash_prompts_state["checked_at"] = ocp.time.monotonic()
        ocp._ensure_slash_prompts(lambda: v)
        self.assertEqual(len(self.spawned), 1)
        ocp._slash_prompts_state["checked_at"] -= 31 * 60
        ocp._ensure_slash_prompts(lambda: v)
        self.assertEqual(len(self.spawned), 2)

    def test_a_valve_change_checks_at_once(self):
        ocp._ensure_slash_prompts(lambda: valves())
        ocp._slash_prompts_state["checked_at"] = ocp.time.monotonic()
        ocp._ensure_slash_prompts(lambda: valves(PROMPT_PREFIX="oc_"))
        self.assertEqual(len(self.spawned), 2)

    def test_off_with_an_empty_ledger_does_nothing(self):
        ocp._slash_prompts_state["ledger_empty"] = True
        ocp._ensure_slash_prompts(lambda: valves(SYNC_SLASH_COMMANDS=False), force=True)
        self.assertEqual(self.spawned, [])

    def test_the_gateway_push_checks_unless_commands_did_not_change(self):
        v = valves()
        ocp._ensure_slash_prompts(lambda: v)
        ocp._slash_prompts_state["checked_at"] = ocp.time.monotonic()
        ocp._slash_prompts_on_metadata_changed({"commandsChanged": False})
        self.assertEqual(len(self.spawned), 1)
        ocp._slash_prompts_on_metadata_changed({})
        self.assertEqual(len(self.spawned), 2)

    def test_the_push_is_ignored_while_the_sync_is_off(self):
        ocp._slash_prompts_state["ledger_empty"] = True
        v = valves(SYNC_SLASH_COMMANDS=False)
        ocp._ensure_slash_prompts(lambda: v)
        ocp._slash_prompts_on_metadata_changed({})
        self.assertEqual(self.spawned, [])


if __name__ == "__main__":
    unittest.main()
