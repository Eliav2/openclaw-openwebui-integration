#!/usr/bin/env python3
"""The user's thinking default is a key in their own Open WebUI settings, not a user valve (ELI-442): a Pipe
with user valves makes Open WebUI draw its own Valves button in the composer, beside Model controls. Moved
from the 0.x user valve `thinking_default` with a lazy migration; changed in the Status dialog."""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import test_status_action_unit  # noqa: F401  (installs the module stubs)
from _test_helpers import RecordingEmitter, action_gateway, run_page
from test_status_action_unit import _FAKE_DOM_JS

import openclaw_status_action as action

ROOT = Path(__file__).resolve().parents[1]
FID, KEY = "openclaw_gateway", "openclaw_gateway:prefs"
USER = "11111111-1111-1111-1111-111111111111"


def load_pipe():
    spec = importlib.util.spec_from_file_location("owui_pref_test", ROOT / "openclaw_pipe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Owui:
    """Open WebUI's Users and Functions for one user: `settings` (what the user's settings hold, as the
    API merges top-level keys), `legacy` (what the 0.x Pipe kept in the user valve, None: no record)."""

    def __init__(self, settings=None, legacy=None, legacy_error=None, models=None):
        self.settings = dict(settings or {})
        self.models = dict(models or {})   # workspace model id -> base_model_id
        self.legacy = legacy
        self.legacy_error = legacy_error
        self.writes = []
        self.legacy_reads = 0
        owui = self

        class Users:
            @staticmethod
            async def get_user_by_id(user_id):
                return types.SimpleNamespace(settings=types.SimpleNamespace(model_dump=lambda: dict(owui.settings)))

            @staticmethod
            async def update_user_settings_by_id(user_id, updated):
                owui.writes.append(json.loads(json.dumps(updated)))
                owui.settings.update(json.loads(json.dumps(updated)))

        class Functions:
            @staticmethod
            async def get_user_valves_by_id_and_user_id(fid, user_id):
                owui.legacy_reads += 1
                if owui.legacy_error:
                    raise owui.legacy_error
                return dict(owui.legacy) if owui.legacy is not None else None

        class Models:
            @staticmethod
            async def get_model_by_id(model_id):
                base = owui.models.get(model_id)
                return types.SimpleNamespace(id=model_id, base_model_id=base) if model_id in owui.models else None

        names = ("open_webui", "open_webui.models", "open_webui.models.users", "open_webui.models.functions",
                 "open_webui.models.models")
        mods = {n: sys.modules.get(n) or types.ModuleType(n) for n in names}
        mods["open_webui.models.users"] = types.ModuleType("open_webui.models.users")
        mods["open_webui.models.functions"] = types.ModuleType("open_webui.models.functions")
        mods["open_webui.models.models"] = types.ModuleType("open_webui.models.models")
        mods["open_webui.models.models"].Models = Models
        mods["open_webui.models.users"].Users = Users
        mods["open_webui.models.functions"].Functions = Functions
        self.patch = mock.patch.dict(sys.modules, mods)

    def user(self):
        """What a Pipe turn gets as `__user__`: the user as a dict, with their settings."""
        return {"id": USER, "settings": dict(self.settings)}


class _Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        env = mock.patch.dict(os.environ, {"OPENCLAW_BRIDGE_STATE_DIR": td.name})
        env.start()
        self.addCleanup(env.stop)
        self.pipe = load_pipe()

    def owui(self, **kw):
        o = Owui(**kw)
        o.patch.start()
        self.addCleanup(o.patch.stop)
        return o


class NoUserValvesTests(_Case):
    def test_the_pipe_and_the_action_have_no_user_valves(self):
        """Open WebUI reports has_user_valves for a pipe with UserValves, and draws a Valves button for it."""
        self.assertFalse(hasattr(self.pipe.Pipe, "UserValves"))
        self.assertFalse(hasattr(action.Action, "UserValves"))


class MergeTests(_Case):
    def test_the_prefs_key_is_the_functions_id_and_prefs(self):
        self.assertEqual(self.pipe.prefs_key(FID), KEY)
        self.assertEqual(self.pipe.function_id_of("openclaw_gateway.default"), FID)
        self.assertEqual(self.pipe.function_id_of(None), FID)

    def test_the_prefs_key_wins_over_the_old_user_valve_field_by_field(self):
        merged = self.pipe.merge_prefs({KEY: {"thinking_default": "low"}}, {"thinking_default": "high", "x": 1}, FID)
        self.assertEqual(merged, {"thinking_default": "low", "x": 1})

    def test_a_malformed_key_or_valve_is_ignored(self):
        self.assertEqual(self.pipe.merge_prefs({KEY: "high"}, ["high"], FID), {})
        self.assertEqual(self.pipe.merge_prefs(None, None, FID), {})


class ReadTests(_Case):
    async def test_the_prefs_key_is_read_without_touching_the_old_valve(self):
        o = self.owui(settings={KEY: {"thinking_default": "xhigh"}}, legacy={"thinking_default": "low"})
        self.assertEqual(await self.pipe.thinking_default_of(o.user(), FID), "xhigh")
        self.assertEqual(o.legacy_reads, 0)
        self.assertEqual(o.writes, [])

    async def test_the_0_x_user_valve_is_read_and_moved_once(self):
        """The lazy migration: no key yet, the old valve says `high`: that is the level, and it is copied
        under the key (a write of that one key only); the next turn finds the key."""
        o = self.owui(settings={"ui": {"theme": "dark"}}, legacy={"thinking_default": "high"})
        self.assertEqual(await self.pipe.thinking_default_of(o.user(), FID), "high")
        self.assertEqual(o.writes, [{KEY: {"thinking_default": "high"}}])
        self.assertEqual(o.settings["ui"], {"theme": "dark"})
        self.assertEqual(o.legacy, {"thinking_default": "high"}, "the old valve is left as it was")
        self.assertEqual(await self.pipe.thinking_default_of(o.user(), FID), "high")
        self.assertEqual(len(o.writes), 1, "moved once")

    async def test_an_old_valve_at_default_or_none_moves_nothing(self):
        for legacy in ({"thinking_default": "default"}, {}, None):
            o = self.owui(legacy=legacy)
            self.assertEqual(await self.pipe.thinking_default_of(o.user(), FID), "default", legacy)
            self.assertEqual(o.writes, [], legacy)

    async def test_an_unreadable_old_valve_still_answers_and_writes_nothing(self):
        o = self.owui(legacy_error=RuntimeError("db busy"))
        self.assertEqual(await self.pipe.thinking_default_of(o.user(), FID), "default")
        self.assertEqual(o.writes, [])

    async def test_a_failed_move_still_uses_the_old_level(self):
        o = self.owui(legacy={"thinking_default": "high"})

        async def boom(user_id, updated):
            raise RuntimeError("read-only")
        sys.modules["open_webui.models.users"].Users.update_user_settings_by_id = boom
        self.assertEqual(await self.pipe.thinking_default_of(o.user(), FID), "high")


class MigrationRaceTests(_Case):
    async def test_an_explicit_save_that_lands_before_the_move_is_not_overwritten(self):
        """The turn's snapshot has no key and the old valve says `high`; the Status dialog saved `low` meanwhile.
        The move inserts the field only while it is still absent, so `low` stands."""
        o = self.owui(legacy={"thinking_default": "high"})
        user = o.user()                                  # the turn's snapshot: no prefs key yet
        o.settings[KEY] = {"thinking_default": "low"}    # the dialog's save, after the snapshot
        self.assertEqual(await self.pipe.thinking_default_of(user, FID), "low")
        self.assertEqual(o.writes, [], "nothing written over it")
        self.assertEqual(o.settings[KEY], {"thinking_default": "low"})


class ResolvedFunctionIdTests(_Case):
    """A workspace model (Admin > Models) built on a Pipe model reaches the Pipe as its base model; the Status
    dialog gets the workspace model's id, so it resolves the same way before it names the settings key."""

    async def test_a_workspace_model_resolves_to_its_base_models_function(self):
        self.owui(models={"forecast-assistant": "openclaw_gateway.default"})
        self.assertEqual(await self.pipe.resolve_function_id("forecast-assistant"), FID)

    async def test_a_pipe_model_and_an_unknown_one_resolve_without_a_row(self):
        self.owui()
        self.assertEqual(await self.pipe.resolve_function_id("openclaw_gateway.default"), FID)
        self.assertEqual(await self.pipe.resolve_function_id("my_gateway.default"), "my_gateway")
        self.assertEqual(await self.pipe.resolve_function_id("forecast-assistant"), FID)
        self.assertEqual(await self.pipe.resolve_function_id(None), FID)

    async def test_a_failed_model_lookup_falls_back_to_the_models_own_prefix(self):
        self.owui()
        sys.modules["open_webui.models.models"].Models.get_model_by_id = mock.AsyncMock(side_effect=RuntimeError("db"))
        self.assertEqual(await self.pipe.resolve_function_id("my_gateway.default"), "my_gateway")


class WriteTests(_Case):
    async def test_a_write_carries_the_old_valve_and_touches_only_its_key(self):
        o = self.owui(settings={"ui": {"theme": "dark"}}, legacy={"thinking_default": "high", "x": 1})
        await self.pipe.write_prefs(USER, FID, {"thinking_default": "low"})
        self.assertEqual(o.writes, [{KEY: {"thinking_default": "low", "x": 1}}])

    async def test_a_failed_read_of_the_old_valve_fails_the_write(self):
        """It may hold a value this write would replace without knowing it."""
        o = self.owui(legacy_error=RuntimeError("db busy"))
        with self.assertRaises(RuntimeError):
            await self.pipe.write_prefs(USER, FID, {"thinking_default": "low"})
        self.assertEqual(o.writes, [])


class DialogTests(_Case):
    """The Status dialog is where the user changes it: a Thinking section with the levels, and a mode that saves."""

    async def test_the_dialog_fills_a_thinking_section_with_the_levels_and_the_current_one(self):
        o = self.owui(settings={KEY: {"thinking_default": "high"}})
        self.assertIn(("thinking", "Thinking level"), action._DIALOG_SECTIONS)
        code = action._render_thinking_fill_js({"levels": ["default", "off", "low", "high"], "current": "high"})
        self.assertIn("openclaw-status-section-thinking", code)
        self.assertIn('"current": "high"', code)
        self.assertNotIn("__OPENCLAW", code)
        self.assertEqual(o.writes, [])

    async def test_the_mode_saves_a_known_level_under_the_prefs_key(self):
        o = self.owui(settings={"ui": {}})
        emitted = RecordingEmitter()
        res = await action.Action().action(
            {"mode": "thinking-default", "level": "high", "model": "openclaw_gateway.default", "chat_id": "c1"},
            __user__={"id": USER}, __event_emitter__=emitted)
        self.assertEqual(res, {"status": "ok", "level": "high"})
        self.assertEqual(o.writes, [{KEY: {"thinking_default": "high"}}])

    async def test_the_mode_refuses_what_is_not_a_level(self):
        o = self.owui()
        for bad in ("bogus", "", None, 7, ["high"]):
            res = await action.Action().action(
                {"mode": "thinking-default", "level": bad, "model": "openclaw_gateway.default", "chat_id": "c1"},
                __user__={"id": USER}, __event_emitter__=RecordingEmitter())
            self.assertEqual(res["status"], "error", bad)
            self.assertIn("level", res["detail"], bad)
        self.assertEqual(o.writes, [])

    async def test_the_save_from_a_workspace_models_chat_uses_the_pipes_key(self):
        o = self.owui(models={"forecast-assistant": "openclaw_gateway.default"})
        res = await action.Action().action(
            {"mode": "thinking-default", "level": "high", "model": "forecast-assistant", "chat_id": "c1"},
            __user__={"id": USER}, __event_emitter__=RecordingEmitter())
        self.assertEqual(res["status"], "ok")
        self.assertEqual(o.writes, [{KEY: {"thinking_default": "high"}}])

    async def test_the_section_reads_the_pipes_key_for_a_workspace_model(self):
        o = self.owui(settings={KEY: {"thinking_default": "low"}}, models={"forecast-assistant": "openclaw_gateway.default"})
        emitted = RecordingEmitter()
        await action.Action()._fill_thinking({"model": "forecast-assistant"}, o.user(), lambda code: emitted({"code": code}))
        self.assertIn('"current": "low"', emitted.events[0]["code"])

    async def test_the_default_level_is_saved_as_default(self):
        o = self.owui(settings={KEY: {"thinking_default": "high"}})
        res = await action.Action().action(
            {"mode": "thinking-default", "level": "default", "model": "openclaw_gateway.default", "chat_id": "c1"},
            __user__={"id": USER}, __event_emitter__=RecordingEmitter())
        self.assertEqual(res["status"], "ok")
        self.assertEqual(o.writes, [{KEY: {"thinking_default": "default"}}])

    async def test_a_failed_save_says_so(self):
        self.owui(legacy_error=RuntimeError("db busy"))
        res = await action.Action().action(
            {"mode": "thinking-default", "level": "low", "model": "openclaw_gateway.default", "chat_id": "c1"},
            __user__={"id": USER}, __event_emitter__=RecordingEmitter())
        self.assertEqual(res["status"], "error")
        self.assertIn("save", res["detail"])


_ROLLBACK_PAGE_JS = _FAKE_DOM_JS + r"""
const answers = [...input.answers];
global.localStorage = { getItem: () => 'tok' };
global.fetch = () => Promise.resolve({ json: () => Promise.resolve(answers.shift()) });
run(input.open);
run(input.fill);
const select = document.getElementById('openclaw-thinking-default');
(async () => {
  const seen = [];
  for (const level of input.picks) {
    select.value = level;
    await select.onchange();
    seen.push(select.value);
  }
  console.log(JSON.stringify(seen));
})();
"""


class DialogRollbackTests(unittest.TestCase):
    def test_a_failed_save_restores_the_last_saved_level_not_the_one_the_dialog_opened_with(self):
        """Opened at low, saved medium, then a failed save of high: the list goes back to medium (what is stored)."""
        fill = action._render_thinking_fill_js({"levels": ["default", "low", "medium", "high"], "current": "low"})
        open_js = action._render_modal_open_js(
            {"chatId": "c1", "messageId": "m1", "sessionId": "s1", "owuiModel": "openclaw_gateway.default"})
        seen = run_page(_ROLLBACK_PAGE_JS, {
            "open": open_js, "fill": fill, "picks": ["medium", "high"],
            "answers": [{"status": "ok", "level": "medium"}, {"status": "error", "detail": "could not save it"}]})
        self.assertEqual(seen, ["medium", "medium"])


if __name__ == "__main__":
    unittest.main()
