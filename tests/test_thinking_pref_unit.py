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
from _test_helpers import RecordingEmitter, action_gateway

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

    def __init__(self, settings=None, legacy=None, legacy_error=None):
        self.settings = dict(settings or {})
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

        names = ("open_webui", "open_webui.models", "open_webui.models.users", "open_webui.models.functions")
        mods = {n: sys.modules.get(n) or types.ModuleType(n) for n in names}
        mods["open_webui.models.users"] = types.ModuleType("open_webui.models.users")
        mods["open_webui.models.functions"] = types.ModuleType("open_webui.models.functions")
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


if __name__ == "__main__":
    unittest.main()
