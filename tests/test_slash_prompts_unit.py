"""Slash commands as Open WebUI Prompts (SYNC_SLASH_COMMANDS): the Gateway's
commands become Workspace Prompts in the `/` menu. The sync owns only the
prompts in its ledger: it never changes or deletes a prompt someone made, a
lost ledger deletes nothing, a failed write is retried, and turning it off
deletes what it made.

Run: python3 -m pytest -q tests/test_slash_prompts_unit.py
"""
import asyncio
import json
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

    OWNER = "admin"

    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.fail = set()
        # Commands whose add commits the row, then fails (as Open WebUI's
        # insert does when its grant or first version fails).
        self.half_fail = set()
        self.calls = []
        self._n = 0

    async def owner_id(self):
        return self.OWNER

    async def list(self):
        return {c: {k: v for k, v in r.items() if k in ("id", "name", "content", "tags", "active")}
                for c, r in self.rows.items()}

    async def find(self, command):
        row = self.rows.get(command)
        return row and {"id": row["id"], "user_id": row.get("user_id"), "meta": row.get("meta") or {}}

    async def add(self, want):
        self.calls.append(("add", want["command"]))
        if want["command"] in self.fail:
            return None
        self._n += 1
        pid = f"p{self._n}"
        self.rows[want["command"]] = {"id": pid, "name": want["name"], "content": want["content"],
                                      "tags": want["tags"], "user_id": self.OWNER, "meta": want["meta"]}
        return None if want["command"] in self.half_fail else pid

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

    def test_a_prompt_turned_off_stays_owned_and_someone_elses_is_skipped(self):
        want = self.wanted(STATUS)[0]
        off = {"status": {"id": "p1", "name": want["name"], "content": want["content"],
                          "tags": ["openclaw"], "active": False}}
        plan = ocp._slash_prompts_plan([want], off, {"status": "p1"})
        self.assertEqual(plan, {"add": [], "update": [], "delete": [], "skip": [], "forget": []})
        theirs = {"status": dict(off["status"], id="hand")}
        plan = ocp._slash_prompts_plan([want], theirs, {})
        self.assertEqual((plan["skip"], plan["add"]), (["status"], []))

    def test_a_lost_ledger_deletes_nothing(self):
        rows = {"old": {"id": "p1", "name": "", "content": "", "tags": ["openclaw"]}}
        plan = ocp._slash_prompts_plan([], rows, {})
        self.assertEqual(plan["delete"], [])


class SyncTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="oc-slash-")
        for patcher in (mock.patch.dict(os.environ, {"OPENCLAW_BRIDGE_STATE_DIR": self.dir}),
                        mock.patch.dict(ocp._slash_prompts_state, {"legacy_at": None, "pending": False})):
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

    async def test_a_prompt_an_admin_turned_off_is_kept_and_not_added_again(self):
        store, conn = FakeStore(), FakeConn([STATUS, SKILL])
        await self.sync(store, conn)
        store.rows["status"]["active"] = False
        conn.revision = "r2"
        store.calls.clear()
        self.assertEqual(await self.sync(store, conn), "synced")
        self.assertEqual(store.calls, [])
        self.assertEqual(self.ledger()["prompts"], {"status": "p1", "skill": "p2"})
        self.assertEqual(self.ledger()["revision"], "r2")
        self.assertEqual(await self.sync(store, conn, SYNC_SLASH_COMMANDS=False), "removed")
        self.assertEqual(store.rows, {})

    async def test_someone_elses_prompt_turned_off_is_skipped(self):
        store = FakeStore({"status": {"id": "hand", "name": "Mine", "content": "x", "tags": [],
                                      "active": False}})
        self.assertEqual(await self.sync(store, FakeConn([STATUS])), "synced")
        self.assertEqual(store.calls, [])
        self.assertEqual(self.ledger()["prompts"], {})

    async def test_a_half_failed_add_is_recorded_and_written_again(self):
        store, conn = FakeStore(), FakeConn([STATUS])
        store.half_fail.add("status")
        self.assertEqual(await self.sync(store, conn), "synced")
        self.assertEqual(store.calls, [("add", "status"), ("update", "status")])
        self.assertEqual(self.ledger()["prompts"], {"status": "p1"})

    async def test_a_failed_add_leaves_a_row_that_is_not_its_own_alone(self):
        for row in ({"user_id": "someone"},
                    {"meta": {"openclaw": {"command": "status", "function": "function_other"}}}):
            store, conn = FakeStore(), FakeConn([STATUS])
            store.half_fail.add("status")
            orig = store.add

            async def add(want, row=row, store=store, orig=orig):
                pid = await orig(want)
                store.rows[want["command"]].update(row)
                return pid
            store.add = add
            self.assertEqual(await self.sync(store, conn), "partial")
            self.assertEqual(self.ledger()["prompts"], {})
            os.unlink(ocp._slash_prompts_ledger_path())

    async def test_each_pipe_function_keeps_its_own_ledger(self):
        store, conn = FakeStore({"mine": {"id": "hand", "name": "", "content": "", "tags": []}}), FakeConn([STATUS])
        with mock.patch.object(ocp, "_slash_prompts_owner", lambda: "function_a"):
            self.assertEqual(await self.sync(store, conn), "synced")
        self.assertEqual(store.rows["status"]["meta"]["openclaw"]["function"], "function_a")
        with mock.patch.object(ocp, "_slash_prompts_owner", lambda: "function_b"):
            self.assertEqual(await self.sync(store, conn, SYNC_SLASH_COMMANDS=False), "off")
            self.assertEqual(await self.sync(store, conn, PROMPT_PREFIX="b_"), "synced")
        self.assertEqual(set(store.rows), {"mine", "status", "b_status"})
        with mock.patch.object(ocp, "_slash_prompts_owner", lambda: "function_a"):
            self.assertEqual(await self.sync(store, conn, SYNC_SLASH_COMMANDS=False), "removed")
        self.assertEqual(set(store.rows), {"mine", "b_status"})

    async def test_the_ledger_from_before_it_was_per_function_is_taken_over(self):
        store, conn = FakeStore(), FakeConn([STATUS])
        await self.sync(store, conn)
        os.replace(ocp._slash_prompts_ledger_path(), os.path.join(self.dir, "slash-command-prompts.json"))
        self.assertEqual(await self.sync(store, conn, SYNC_SLASH_COMMANDS=False), "removed")
        self.assertEqual(store.rows, {})
        self.assertFalse(os.path.exists(os.path.join(self.dir, "slash-command-prompts.json")))

    async def test_another_worker_holding_the_lock_makes_it_busy_and_pending(self):
        import fcntl
        with open(ocp._slash_prompts_lock_path(), "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            store = FakeStore()
            self.assertEqual(await self.sync(store, FakeConn([STATUS])), "busy")
            self.assertTrue(ocp._slash_prompts_state["pending"])
            self.assertEqual(store.calls, [])
        self.assertEqual(await self.sync(store, FakeConn([STATUS])), "synced")
        self.assertFalse(ocp._slash_prompts_state["pending"])

    async def test_an_unrelated_invalid_params_error_is_an_error_not_an_older_gateway(self):
        class WrongAgent(FakeConn):
            async def send_request(self, method, params, timeout=10):
                self.requests.append((method, dict(params)))
                raise ocp.GatewayError('invalid agent id "nope"')
        store, conn = FakeStore(), WrongAgent([STATUS])
        self.assertEqual(await self.sync(store, conn, AGENT_ID="nope"), "error")
        self.assertEqual([m for m, _ in conn.requests], ["chat.metadata"])
        self.assertIsNone(ocp._slash_prompts_state["legacy_at"])

    async def test_a_gateway_without_chat_metadata_uses_commands_list_and_retries_a_day_later(self):
        class NoMethod(FakeConn):
            async def send_request(self, method, params, timeout=10):
                self.requests.append((method, dict(params)))
                if method == "chat.metadata":
                    raise ocp.GatewayError("unknown method: chat.metadata")
                return {"commands": self.commands}
        store, conn = FakeStore(), NoMethod([STATUS])
        self.assertEqual(await self.sync(store, conn), "synced")
        self.assertEqual(await self.sync(store, conn), "unchanged")
        self.assertEqual([m for m, _ in conn.requests], ["chat.metadata", "commands.list", "commands.list"])
        ocp._slash_prompts_state["legacy_at"] -= ocp._SLASH_PROMPTS_LEGACY_RETRY_S
        await self.sync(store, conn)
        self.assertEqual(conn.requests[-2][0], "chat.metadata")

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
            "checked_at": None, "pending": False, "running": False, "settings": None,
            "ledger_empty": None, "valves": None})
        self.state.start()
        self.addCleanup(self.state.stop)
        self.spawned = []

        def spawn(coro):  # the run, done at once
            self.spawned.append(coro)
            coro.close()
            ocp._slash_prompts_state["running"] = False
        for target, value in (("spawn_background", spawn),
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

    def test_a_trigger_while_a_run_goes_only_marks_it_pending(self):
        ocp._slash_prompts_state["running"] = True
        ocp._ensure_slash_prompts(lambda: valves(), force=True)
        self.assertEqual(self.spawned, [])
        self.assertTrue(ocp._slash_prompts_state["pending"])

    def test_turned_on_forgets_that_the_ledger_was_empty(self):
        # Another worker's sync may have filled it since this one looked.
        ocp._slash_prompts_state["ledger_empty"] = True
        ocp._ensure_slash_prompts(lambda: valves())
        self.assertIsNone(ocp._slash_prompts_state["ledger_empty"])
        ocp._slash_prompts_state["ledger_empty"] = None
        ocp._ensure_slash_prompts(lambda: valves(SYNC_SLASH_COMMANDS=False))
        self.assertEqual(len(self.spawned), 2)

    def test_the_push_is_ignored_while_the_sync_is_off(self):
        ocp._slash_prompts_state["ledger_empty"] = True
        v = valves(SYNC_SLASH_COMMANDS=False)
        ocp._ensure_slash_prompts(lambda: v)
        ocp._slash_prompts_on_metadata_changed({})
        self.assertEqual(self.spawned, [])


class RunLoopTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        p = mock.patch.dict(ocp._slash_prompts_state, {"pending": False, "running": False, "checked_at": None})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(ocp, "_SLASH_PROMPTS_PUSH_SETTLE_S", 0)
        p.start()
        self.addCleanup(p.stop)

    async def test_a_trigger_during_a_check_runs_one_more_then_it_stops(self):
        calls = []

        async def sync(v, getter, store=None):
            # The first check finds another worker syncing (busy, pending);
            # the second gets a push mid-check (pending); the third is quiet.
            calls.append(v)
            ocp._slash_prompts_state["pending"] = len(calls) < 3
            return "busy" if len(calls) == 1 else "synced"
        with mock.patch.object(ocp, "_slash_prompts_sync", sync):
            await ocp._slash_prompts_run(lambda: "v")
        self.assertEqual(len(calls), 3)
        self.assertFalse(ocp._slash_prompts_state["running"])

    async def test_a_check_that_raises_ends_the_loop(self):
        async def sync(*a, **k):
            ocp._slash_prompts_state["pending"] = True
            raise RuntimeError("boom")
        with mock.patch.object(ocp, "_slash_prompts_sync", sync):
            await ocp._slash_prompts_run(lambda: "v")
        self.assertFalse(ocp._slash_prompts_state["running"])


class GatewayEventTests(unittest.IsolatedAsyncioTestCase):
    """gateway.py hands `chat.metadata.changed` to the sync, on the connection
    that writes turns only."""

    async def play(self, conn, frames):
        frames = list(frames)

        class ScriptedWS:
            async def recv(self_):
                if frames:
                    return json.dumps(frames.pop(0))
                conn._stopped = True
                raise asyncio.CancelledError
        conn._ws = ScriptedWS()
        await conn._event_loop()

    async def test_the_push_reaches_the_hook(self):
        frame = {"type": "event", "event": "chat.metadata.changed", "payload": {"commandsChanged": True}}
        hook = mock.Mock()
        with mock.patch.object(ocp, "_SLASH_PROMPTS_HOOK", hook):
            await self.play(ocp._GatewayConnection(lambda: None), [frame])
            self.assertEqual(hook.call_args_list, [mock.call({"commandsChanged": True})])
            await self.play(ocp._GatewayConnection(lambda: None, writes_turns=False), [frame])
            self.assertEqual(len(hook.call_args_list), 1)


if __name__ == "__main__":
    unittest.main()
