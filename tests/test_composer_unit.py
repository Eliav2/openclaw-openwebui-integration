#!/usr/bin/env python3
"""Unit tests for the composer Status button (src/openclaw_pipe_pkg/composer.py)
in the Pipe and the Status Action."""

import asyncio
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_helpers import FakeSio, RecordingEmitter, fake_owui  # noqa: E402
from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe  # noqa: E402
import openclaw_status_action  # noqa: E402
from openclaw_pipe import (  # noqa: E402
    GatewayError,
    Pipe,
    _COMPOSER_BUTTON_VERSION,
    _ComposerWatcher,
    _composer_chat_head,
    _composer_model_prefixes,
    _composer_note_model,
    _composer_own_function_id,
    _emit_composer_button,
    _ensure_composer_watcher,
    _render_composer_button_js,
)

GUARD = "__openclawComposerBtn"


def _is_composer_event(event):
    return (
        event.get("type") == "execute"
        and GUARD in ((event.get("data") or {}).get("code") or "")
    )


def _config_of(code):
    """Pull the embedded config JSON back out of a rendered snippet."""
    start = code.index("const CFG = ") + len("const CFG = ")
    end = code.index(";\n", start)
    return json.loads(code[start:end])


def _history(*rows, current=None):
    """rows: (id, role, parentId[, model])."""
    messages = {r[0]: {"id": r[0], "role": r[1], "parentId": r[2],
                       "model": r[3] if len(r) > 3 else None} for r in rows}
    return {"messages": messages, "currentId": current or rows[-1][0]}


def _rows(history, reads=None):
    """`message(id)` for `_composer_chat_head`: a row's (role, parent_id, model_id)."""
    async def message(mid):
        if reads is not None:
            reads.append(mid)
        m = history["messages"].get(mid)
        return (m["role"], m["parentId"], m["model"]) if m else None
    return message


class SnippetRenderTests(unittest.TestCase):
    def test_carries_versioned_window_guard(self):
        code = _render_composer_button_js()
        self.assertIn(f"const KEY = '{GUARD}'", code)
        self.assertIn("prev.version === CFG.version", code)
        self.assertNotIn("__OPENCLAW_COMPOSER_CONFIG__", code)
        cfg = _config_of(code)
        self.assertEqual(cfg["version"], _COMPOSER_BUTTON_VERSION)
        self.assertEqual(cfg["actionId"], "openclaw_status_action")
        self.assertEqual(cfg["thinkingChipLabel"], "OpenClaw Thinking")

    def test_config_is_json_and_escapes_angle_brackets(self):
        code = _render_composer_button_js(session_id="</script><b>", model_prefixes=["x."])
        self.assertNotIn("</script>", code)
        cfg = _config_of(code)
        self.assertEqual(cfg["sessionId"], "</script><b>")
        self.assertEqual(cfg["modelPrefixes"], ["x."])

    def test_empty_session_id_becomes_null(self):
        self.assertIsNone(_config_of(_render_composer_button_js(session_id=""))["sessionId"])

    def test_model_prefixes_add_the_serving_pipe_id(self):
        self.assertEqual(_composer_model_prefixes(None), ["openclaw_gateway."])
        self.assertEqual(_composer_model_prefixes("openclaw_gateway.default"), ["openclaw_gateway."])
        self.assertEqual(
            _composer_model_prefixes("my_claw.anthropic/x"), ["openclaw_gateway.", "my_claw."],
        )
        self.assertEqual(_composer_model_prefixes("no-dot-model"), ["openclaw_gateway."])
        self.assertEqual(
            _composer_model_prefixes("my_claw.x", own_id="my_claw"), ["openclaw_gateway.", "my_claw."],
        )
        self.assertEqual(_composer_model_prefixes(own_id="claw2"), ["openclaw_gateway.", "claw2."])

    def test_own_function_id_comes_from_owui_module_name(self):
        # Open WebUI execs a function as module `function_<id>_<uuid hex>`.
        as_loaded = types.FunctionType(
            _composer_own_function_id.__code__,
            {**openclaw_pipe.__dict__, "__name__": "function_my_claw_" + "a" * 32},
        )
        self.assertEqual(as_loaded(), "my_claw")
        self.assertIsNone(_composer_own_function_id())  # imported normally: not inside OWUI

    def test_chat_id_is_carried_as_a_vouch(self):
        self.assertEqual(_config_of(_render_composer_button_js(chat_id="c9"))["chatId"], "c9")
        self.assertIsNone(_config_of(_render_composer_button_js(chat_id=""))["chatId"])

    def test_action_and_pipe_render_the_same_snippet(self):
        self.assertEqual(
            openclaw_status_action._render_composer_button_js(session_id="s"),
            _render_composer_button_js(session_id="s"),
        )


class StatusLabelWidthTests(unittest.TestCase):
    """ELI-123: at 390 px the "Status" label was cut off. Below `sm` the button
    is icon-only with its accessible name."""

    def test_label_is_hidden_below_sm_and_the_button_keeps_its_name(self):
        code = _render_composer_button_js()
        self.assertIn('<span class="pr-0.5 hidden sm:inline">Claw</span>', code)
        self.assertIn("b.setAttribute('aria-label', 'OpenClaw status');", code)

    def test_a_button_cut_off_by_the_chip_row_moves_to_the_left_group(self):
        """With 0.13's Model controls, a 375 px chip row is too narrow: move after Integrations."""
        code = _render_composer_button_js()
        self.assertIn("if (state.narrowAt && window.innerWidth <= state.narrowAt) return leftSlot(c);", code)
        self.assertIn("state.narrowAt = window.innerWidth;", code)
        self.assertIn("window.addEventListener('resize', onResize);", code)
        self.assertIn("window.removeEventListener('resize', onResize);", code)

    def test_a_cut_off_button_moves_at_once_and_a_narrower_chip_row_is_checked_again(self):
        """The move happens in the same task as the check, and a ResizeObserver
        catches the chip row narrowing without a childList change."""
        code = _render_composer_button_js()
        fits = code[code.index("function checkFits(btn)"):code.index("function onResize()")]
        self.assertIn("if (s) s.parent.appendChild(btn);", fits)
        self.assertNotIn("schedule()", fits)
        self.assertIn("state.resizeObserver.observe(chips);", fits)
        self.assertIn("if (state.resizeObserver) state.resizeObserver.disconnect();", code)

    def test_a_resize_checks_the_fit_and_its_moves_do_not_count_as_a_fight(self):
        """Dragging the width back and forth must not reach LOOP_LIMIT and remove the button."""
        code = _render_composer_button_js()
        on = code[code.index("function onResize()"):code.index("function removeAll()")]
        self.assertIn("if (state.narrowAt && window.innerWidth > state.narrowAt) {", on)
        self.assertIn("state.narrowAt = 0;", on)
        self.assertIn("state.inserts = [];", on)
        self.assertEqual(on.count("schedule();"), 1)
        self.assertLess(on.index("}\n    schedule();"), len(on))


class ClawNameTests(unittest.TestCase):
    """ELI-202: chip, tooltip and dialog read "Claw" with one icon; ELI-231
    made it a crab claw. The aria-labels page scripts find buttons by stay."""

    # ELI-202's paw: four toe pads over a pad
    PAW_TOE = "M11 6a1.5 2 0 1 1-3 0"

    def test_the_buttons_and_the_dialog_say_claw_with_a_crab_claw(self):
        import inspect
        import re
        import urllib.parse
        code = _render_composer_button_js()
        self.assertIn('<span class="pr-0.5 hidden sm:inline">Claw</span>', code)
        self.assertIn("b.title = 'Claw';", code)
        self.assertIn("b.setAttribute('aria-label', 'OpenClaw status');", code)
        self.assertNotIn(self.PAW_TOE, code, "the composer chip still shows the paw")
        claw = re.search(r"const CLAW_PATH = '([^']+)';", code)
        self.assertIsNotNone(claw, "the composer chip has no claw")
        self.assertIn("d=\"' + CLAW_PATH + '\"", code)
        icon = getattr(openclaw_status_action.Action(), "icon_url", None) or ""
        self.assertTrue(icon.startswith("data:image/svg+xml"), f"the toolbar button has no icon of its own: {icon[:60]!r}")
        self.assertIn(f"d='{claw.group(1)}'", urllib.parse.unquote(icon))
        self.assertNotIn(self.PAW_TOE, urllib.parse.unquote(icon), "the toolbar button still shows the paw")
        source = inspect.getsource(openclaw_status_action)
        self.assertIn("title.textContent = 'Claw';", source)
        self.assertIn("title: OpenClaw Status\n", source)


class EmitHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_one_execute_event(self):
        events = []
        emitter = RecordingEmitter(events)

        ok = await _emit_composer_button(emitter, session_id="sid1", model_id="mine.x")
        self.assertTrue(ok)
        self.assertEqual(len(events), 1)
        self.assertTrue(_is_composer_event(events[0]))
        cfg = _config_of(events[0]["data"]["code"])
        self.assertEqual(cfg["sessionId"], "sid1")
        self.assertIn("mine.", cfg["modelPrefixes"])

    async def test_no_emitter_is_a_noop(self):
        self.assertFalse(await _emit_composer_button(None))

    async def test_emitter_failure_is_swallowed(self):
        async def emitter(e):
            raise RuntimeError("socket gone")

        self.assertFalse(await _emit_composer_button(emitter))


class ChatHeadTests(unittest.IsolatedAsyncioTestCase):
    """ELI-115: the watcher reads a chat's head, never the whole chat (~14x its JSON)."""

    async def head(self, history, reads=None):
        return await _composer_chat_head("alice", history["currentId"], _rows(history, reads))

    async def test_the_latest_user_message_and_the_reply_model_in_two_reads(self):
        h = _history(("u1", "user", None), ("a1", "assistant", "u1", "llama3.2:1b"),
                     ("u2", "user", "a1"), ("a2", "assistant", "u2", "openclaw_gateway.default"))
        reads = []
        self.assertEqual(await self.head(h, reads), {
            "user_id": "alice", "message_id": "u2", "model": "openclaw_gateway.default"})
        self.assertEqual(reads, ["a2", "u2"])

    async def test_a_user_message_last_takes_the_model_of_the_reply_before_it(self):
        h = _history(("u1", "user", None), ("a1", "assistant", "u1", "openclaw_gateway.x"),
                     ("u2", "user", "a1"))
        reads = []
        self.assertEqual(await self.head(h, reads), {
            "user_id": "alice", "message_id": "u2", "model": "openclaw_gateway.x"})
        self.assertEqual(reads, ["u2", "a1"])

    async def test_with_no_user_message_the_current_one_is_the_target(self):
        h = _history(("a1", "assistant", None, "openclaw_gateway.x"))
        self.assertEqual((await self.head(h))["message_id"], "a1")
        self.assertEqual(await _composer_chat_head("alice", None, _rows(h)),
                         {"user_id": "alice", "message_id": None, "model": None})

    async def test_a_missing_row_ends_the_walk(self):
        h = _history(("u1", "user", None), ("a1", "assistant", "u1", "openclaw_gateway.x"),
                     current="gone")
        self.assertEqual(await self.head(h), {"user_id": "alice", "message_id": "gone", "model": None})


class PipeTurnStartTests(unittest.IsolatedAsyncioTestCase):
    """The Pipe emits the snippet once at the start of a real turn."""

    def setUp(self):
        self.pipe = Pipe()
        self.pipe.valves.ENABLE_FILE_SERVER = False
        self.events = []

    async def _emitter(self, e):
        self.events.append(e)

    async def _run(self, body, **kwargs):
        async def no_gateway(_getter):
            raise GatewayError("offline in tests")

        with mock.patch.object(openclaw_pipe, "_get_gateway_connection", no_gateway), \
                mock.patch.object(openclaw_pipe, "_ensure_composer_watcher") as ensure:
            out = [chunk async for chunk in self.pipe.pipe(body, self._emitter, **kwargs)]
        return out, ensure

    async def test_emitted_right_after_thinking_status(self):
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "hi"}]}
        out, ensure = await self._run(body, __metadata__={"chat_id": "c1", "session_id": "sock1"})
        self.assertIn("Gateway connection error", "".join(out))
        self.assertEqual(self.events[0]["type"], "status")
        self.assertEqual(self.events[0]["data"]["description"], "Thinking...")
        self.assertTrue(_is_composer_event(self.events[1]))
        self.assertEqual(sum(_is_composer_event(e) for e in self.events), 1)
        cfg = _config_of(self.events[1]["data"]["code"])
        self.assertEqual(cfg["sessionId"], "sock1")
        self.assertEqual(cfg["chatId"], "c1")
        self.assertIn("openclaw_gateway.", cfg["modelPrefixes"])
        ensure.assert_called_once()

    async def test_pipe_under_another_id_adds_its_model_prefix(self):
        body = {"model": "claw_b.default", "messages": [{"role": "user", "content": "hi"}]}
        await self._run(body, __metadata__={"chat_id": "c1"})
        cfg = _config_of(next(e for e in self.events if _is_composer_event(e))["data"]["code"])
        self.assertIn("claw_b.", cfg["modelPrefixes"])
        self.assertIn("claw_b.", _composer_note_model())

    async def test_valve_off_suppresses_it(self):
        self.pipe.valves.COMPOSER_STATUS_BUTTON = False
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "hi"}]}
        _, ensure = await self._run(body, __metadata__={"chat_id": "c1"})
        self.assertFalse(any(_is_composer_event(e) for e in self.events))
        ensure.assert_not_called()

    async def test_background_tasks_do_not_emit(self):
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "hi"}]}
        await self._run(body, __task__="title_generation")
        self.assertEqual(self.events, [])

    async def test_empty_message_does_not_emit(self):
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": ""}]}
        await self._run(body)
        self.assertFalse(any(_is_composer_event(e) for e in self.events))


class ActionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """The Status Action emits the snippet on every run, after its own work."""

    async def test_status_run_emits_snippet_last(self):
        events = []
        emitter = RecordingEmitter(events)

        action = openclaw_status_action.Action()
        result = await action.action({"session_id": "sockA"}, __user__={"id": "u"},
                                     __event_emitter__=emitter)
        self.assertEqual(result["status"], "error")  # no chat_id: the dialog's own error path
        self.assertEqual(events[0]["type"], "notification")
        self.assertTrue(_is_composer_event(events[-1]))
        self.assertEqual(sum(_is_composer_event(e) for e in events), 1)
        cfg = _config_of(events[-1]["data"]["code"])
        self.assertEqual(cfg["sessionId"], "sockA")
        self.assertEqual(cfg["modelPrefixes"], ["openclaw_gateway."])

    async def _status_cfg(self, model):
        events = []
        emitter = RecordingEmitter(events)

        action = openclaw_status_action.Action()

        async def fake_status(*args, **kwargs):
            return {"status": "ok"}

        with mock.patch.object(action, "_run_status", fake_status):
            await action.action({"chat_id": "c1", "id": "m1", "model": model},
                                __user__={"id": "u"}, __event_emitter__=emitter)
        return _config_of(events[-1]["data"]["code"])

    async def test_vouches_only_for_openclaw_chats(self):
        # The Action is global: a dotted id like llama3.2 must not become a prefix.
        cfg = await self._status_cfg("openclaw_gateway.default")
        self.assertEqual(cfg["chatId"], "c1")
        cfg = await self._status_cfg("llama3.2:1b")
        self.assertIsNone(cfg["chatId"])
        self.assertEqual(cfg["modelPrefixes"], ["openclaw_gateway."])

    async def test_compact_and_drawer_runs_do_not_emit(self):
        events = []
        emitter = RecordingEmitter(events)

        action = openclaw_status_action.Action()

        async def fake(*args, **kwargs):
            return {"status": "ok"}

        with mock.patch.object(action, "_run_compact", fake), \
                mock.patch.object(action, "_run_subagent_detail", fake):
            await action.action({"mode": "compact"}, __user__={"id": "u"}, __event_emitter__=emitter)
            await action.action({"mode": "subagent-detail"}, __user__={"id": "u"},
                                __event_emitter__=emitter)
        self.assertFalse(any(_is_composer_event(e) for e in events))

    async def test_emitted_even_when_the_run_raises(self):
        events = []
        emitter = RecordingEmitter(events)

        action = openclaw_status_action.Action()

        async def boom(*args, **kwargs):
            raise RuntimeError("dialog failed")

        with mock.patch.object(action, "_run_status", boom):
            with self.assertRaises(RuntimeError):
                await action.action({}, __user__={"id": "u"}, __event_emitter__=emitter)
        self.assertTrue(_is_composer_event(events[-1]))


class _FakeChat:
    def __init__(self, user_id, model, history):
        self.user_id = user_id
        self.history = copy.deepcopy(history)
        for m in self.history["messages"].values():
            if m["role"] == "assistant":
                m["model"] = model


class _TestWatcher(_ComposerWatcher):
    def __init__(self, pool, chats, enabled=True, acks=None):
        super().__init__(lambda: types.SimpleNamespace(COMPOSER_STATUS_BUTTON=enabled),
                         lambda: ["openclaw_gateway."])
        self.pool = pool
        self.chats = chats
        self.sent = []
        self.acks = list(acks or [])

    def _session_pool_batches(self):
        return [list(self.pool.items())]

    async def _chat_head(self, chat_id):
        chat = self.chats.get(chat_id)
        if chat is None:
            return None
        return await _composer_chat_head(chat.user_id, chat.history["currentId"],
                                         _rows(chat.history))

    async def _call_sid(self, sid, payload):
        self.sent.append((sid, payload))
        return self.acks.pop(0) if self.acks else True


class ComposerWatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.history = _history(("u1", "user", None), ("a1", "assistant", "u1"))
        self.chats = {
            "oc": _FakeChat("alice", "openclaw_gateway.default", self.history),
            "llama": _FakeChat("alice", "llama3.2:1b", self.history),
            "bobs": _FakeChat("bob", "openclaw_gateway.default", self.history),
        }

    async def test_pushes_once_after_settle_to_that_sid_targeting_user_message(self):
        pool = {"sidA": {"id": "alice", "chat_ids": ["oc"]}}
        w = _TestWatcher(pool, self.chats)
        self.assertEqual(await w.tick(now=100.0), 0)  # first sighting: settle
        self.assertEqual(await w.tick(now=102.0), 1)
        self.assertEqual(await w.tick(now=104.0), 0)
        self.assertEqual(len(w.sent), 1)
        sid, payload = w.sent[0]
        self.assertEqual(sid, "sidA")
        self.assertEqual(payload["chat_id"], "oc")
        self.assertEqual(payload["message_id"], "u1")
        self.assertEqual(payload["data"]["type"], "execute")
        cfg = _config_of(payload["data"]["data"]["code"])
        self.assertEqual(cfg["sessionId"], "sidA")
        self.assertEqual(cfg["chatId"], "oc")

    async def test_a_newly_opened_chat_gets_the_script_within_a_second(self):
        """Until it lands, a toolbar click takes Open WebUI's slow path."""
        w = _TestWatcher({"sidA": {"id": "alice", "chat_ids": ["oc"]}}, self.chats)
        self.assertEqual(await w.tick(now=100.0), 0)
        self.assertEqual(await w.tick(now=100.0 + openclaw_pipe._COMPOSER_WATCH_INTERVAL_S), 1)
        self.assertLessEqual(openclaw_pipe._COMPOSER_WATCH_INTERVAL_S * 2, 1.0)

    async def test_the_push_installs_the_stop_send_now_intent_hook(self):
        """ELI-100 case 8: every OpenClaw chat tab gets the intent hook, so Stop aborts."""
        pool = {"sidA": {"id": "alice", "chat_ids": ["oc"]}}
        w = _TestWatcher(pool, self.chats)
        await w.tick(now=100.0)
        await w.tick(now=102.0)
        code = w.sent[0][1]["data"]["data"]["code"]
        self.assertIn("__ocIntentHook", code)
        fd, path = tempfile.mkstemp(suffix=".js")
        with os.fdopen(fd, "w") as fh:
            fh.write(code)
        self.addCleanup(os.unlink, path)
        proc = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    async def test_no_ack_retries_then_gives_up(self):
        # No ack while the tab has not loaded the chat.
        pool = {"sidA": {"id": "alice", "chat_ids": ["oc"]}}
        w = _TestWatcher(pool, self.chats, acks=[False, False, False, False])
        await w.tick(now=0.0)
        self.assertEqual(await w.tick(now=2.0), 0)   # attempt 1, no ack
        self.assertEqual(await w.tick(now=4.0), 0)   # too early for the retry
        self.assertEqual(await w.tick(now=5.5), 0)   # attempt 2
        self.assertEqual(await w.tick(now=9.0), 0)   # attempt 3, last one
        self.assertEqual(await w.tick(now=20.0), 0)  # given up until re-join
        self.assertEqual(len(w.sent), 3)

    async def test_retry_succeeds_on_late_ack(self):
        pool = {"sidA": {"id": "alice", "chat_ids": ["oc"]}}
        w = _TestWatcher(pool, self.chats, acks=[False, True])
        await w.tick(now=0.0)
        await w.tick(now=2.0)
        self.assertEqual(await w.tick(now=6.0), 1)
        await w.tick(now=20.0)
        self.assertEqual(len(w.sent), 2)

    async def test_a_failing_push_is_logged_and_retried(self):
        pool = {"sidA": {"id": "alice", "chat_ids": ["oc"]}}
        w = _TestWatcher(pool, self.chats)
        calls = []

        head = w._chat_head

        async def flaky(chat_id):
            calls.append(chat_id)
            if len(calls) == 1:
                raise RuntimeError("db hiccup")
            return await head(chat_id)

        w._chat_head = flaky
        await w.tick(now=0.0)
        self.assertEqual(await w.tick(now=2.0), 0)
        self.assertEqual(await w.tick(now=6.0), 1)

    async def test_skips_non_openclaw_and_foreign_chats(self):
        pool = {"sidA": {"id": "alice", "chat_ids": ["llama", "bobs", "missing"]}}
        w = _TestWatcher(pool, self.chats)
        await w.tick(now=0.0)
        self.assertEqual(await w.tick(now=5.0), 0)
        self.assertEqual(w.sent, [])

    async def test_rejoining_rearms_the_pair(self):
        pool = {"sidA": {"id": "alice", "chat_ids": ["oc"]}}
        w = _TestWatcher(pool, self.chats)
        await w.tick(now=0.0)
        await w.tick(now=2.0)
        pool["sidA"]["chat_ids"] = []
        await w.tick(now=4.0)
        pool["sidA"]["chat_ids"] = ["oc"]
        await w.tick(now=6.0)
        await w.tick(now=8.0)
        self.assertEqual(len(w.sent), 2)

    async def test_disabled_valve_pushes_nothing(self):
        pool = {"sidA": {"id": "alice", "chat_ids": ["oc"]}}
        w = _TestWatcher(pool, self.chats, enabled=False)
        await w.tick(now=0.0)
        await w.tick(now=5.0)
        self.assertEqual(w.sent, [])

    async def test_a_tab_that_acks_the_push_is_ready_for_an_ask_user_card(self):
        """The ack says the tab shows the chat, so an ask_user card can go there (ELI-216)."""
        openclaw_pipe._READY_CHAT_TABS.clear()
        self.addCleanup(openclaw_pipe._READY_CHAT_TABS.clear)
        pool = {"sidA": {"id": "alice", "chat_ids": ["oc"]}}
        w = _TestWatcher(pool, self.chats, acks=[False])
        await w.tick(now=0.0)
        await w.tick(now=1.0)
        self.assertEqual(openclaw_pipe._READY_CHAT_TABS, {}, "no ack yet: still loading")
        await w.tick(now=5.0)
        self.assertEqual(openclaw_pipe._READY_CHAT_TABS, {"oc": ["sidA"]})
        pool["sidA"]["chat_ids"] = []
        await w.tick(now=6.0)
        self.assertEqual(openclaw_pipe._READY_CHAT_TABS, {})

    async def test_sessions_without_chat_ids_are_ignored(self):
        pool = {"sidA": {"id": "alice"}, "sidB": None}
        w = _TestWatcher(pool, self.chats)
        await w.tick(now=0.0)
        self.assertEqual(await w.tick(now=5.0), 0)


class WatcherLivenessTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        for name in ("open_webui.models.functions", "open_webui.models", "open_webui"):
            sys.modules.pop(name, None)

    def _install_functions(self, fn):
        owui = types.ModuleType("open_webui")
        models = types.ModuleType("open_webui.models")
        functions = types.ModuleType("open_webui.models.functions")

        class Functions:
            @staticmethod
            async def get_function_by_id(fid):
                return fn

        functions.Functions = Functions
        sys.modules.update({"open_webui": owui, "open_webui.models": models,
                            "open_webui.models.functions": functions})

    def _watcher(self):
        return _ComposerWatcher(lambda: Pipe.Valves(), lambda: ["openclaw_gateway."],
                                function_id="openclaw_gateway")

    async def test_function_states(self):
        self._install_functions(types.SimpleNamespace(is_active=True))
        self.assertEqual(await self._watcher().function_state(), "active")
        self._install_functions(types.SimpleNamespace(is_active=False))
        self.assertEqual(await self._watcher().function_state(), "inactive")
        self._install_functions(None)
        self.assertEqual(await self._watcher().function_state(), "gone")
        no_id = _ComposerWatcher(lambda: Pipe.Valves(), lambda: [])
        self.assertEqual(await no_id.function_state(), "unknown")

    async def test_run_stops_when_the_pipe_is_deleted(self):
        self._install_functions(None)
        w = self._watcher()
        ticks = []

        async def tick(now=None):
            ticks.append(now)

        w.tick = tick
        await asyncio.wait_for(w.run(), timeout=2)
        self.assertEqual(ticks, [])

    async def test_run_idles_while_the_pipe_is_disabled(self):
        self._install_functions(types.SimpleNamespace(is_active=False))
        w = self._watcher()
        ticks = []

        async def tick(now=None):
            ticks.append(now)

        w.tick = tick
        with mock.patch.object(openclaw_pipe, "_COMPOSER_WATCH_INTERVAL_S", 0.01):
            task = asyncio.get_running_loop().create_task(w.run())
            await asyncio.sleep(0.1)
            task.cancel()
        self.assertEqual(ticks, [])


class EnsureWatcherTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        task = openclaw_pipe._composer_watcher_task
        if task is not None:
            task.cancel()
        openclaw_pipe._composer_watcher_task = None

    async def test_outside_owui_does_nothing(self):
        sys.modules.pop("open_webui", None)
        with mock.patch.dict(sys.modules, {"open_webui": None}):
            self.assertIsNone(_ensure_composer_watcher(lambda: Pipe.Valves()))

    async def test_starts_once_and_cancels_a_previous_deploys_task(self):
        main = fake_owui(self, SESSION_POOL={})
        stale = asyncio.get_running_loop().create_task(asyncio.sleep(60))
        setattr(main, openclaw_pipe._COMPOSER_WATCH_ATTR, stale)
        task = _ensure_composer_watcher(lambda: Pipe.Valves())
        self.assertIsNotNone(task)
        self.assertIs(_ensure_composer_watcher(lambda: Pipe.Valves()), task)
        self.assertIs(getattr(main, openclaw_pipe._COMPOSER_WATCH_ATTR), task)
        await asyncio.sleep(0)
        self.assertTrue(stale.cancelled())

    async def test_a_second_pipe_copy_keeps_the_first_copys_watcher(self):
        # Each Pipe copy has its own watcher; a start cancels only its own earlier load.
        fake_owui(self, SESSION_POOL={})
        with mock.patch.object(openclaw_pipe, "_composer_own_function_id",
                               return_value="openclaw_gateway"):
            first = _ensure_composer_watcher(lambda: Pipe.Valves())
        self.addCleanup(first.cancel)
        openclaw_pipe._composer_watcher_task = None  # the second copy's own module state
        with mock.patch.object(openclaw_pipe, "_composer_own_function_id",
                               return_value="openclaw_gateway_restricted"):
            second = _ensure_composer_watcher(lambda: Pipe.Valves())
        self.assertIsNot(first, second)
        await asyncio.sleep(0)
        self.assertFalse(first.cancelled())

    async def test_disabled_valve_does_not_start(self):
        fake_owui(self, SESSION_POOL={})
        valves = Pipe.Valves()
        valves.COMPOSER_STATUS_BUTTON = False
        self.assertIsNone(_ensure_composer_watcher(lambda: valves))


# Runs the snippet as OWUI 0.12's Chat.svelte does (`new Function(...)`) on a
# fake DOM with no composer.
_NODE_HARNESS = r"""
const code = require('fs').readFileSync(process.argv[2], 'utf8');
const chatModels = JSON.parse(process.argv[3]);
let observers = 0;
const warnings = [];
console.warn = function () { warnings.push(Array.prototype.join.call(arguments, ' ')); };
globalThis.window = globalThis;
window.addEventListener = function () {};
window.removeEventListener = function () {};
globalThis.location = { pathname: '/c/chat-1' };
globalThis.localStorage = { getItem: function () { return 'tok'; } };
globalThis.MutationObserver = class {
  constructor(cb) { observers++; this.cb = cb; }
  observe() {}
  disconnect() {}
};
globalThis.document = {
  body: { appendChild: function () {} },
  documentElement: { classList: { contains: function () { return false; } } },
  getElementById: function () { return null; },
  querySelectorAll: function () { return []; },
  createElement: function () { throw new Error('no composer in this fake DOM'); },
  addEventListener: function () {},
  removeEventListener: function () {},
};
let fetches = 0;
globalThis.fetch = function () {
  fetches++;
  return Promise.resolve({ ok: true, json: function () {
    return Promise.resolve({ chat: { models: chatModels, history: {} } });
  } });
};
async function run(c) { return new Function('return (async () => { ' + c + ' })()')(); }
(async function () {
  await run(code);
  const first = window.__openclawComposerBtn;
  await run(code.replace('"sessionId": null', '"sessionId": "second"'));
  await new Promise(function (r) { setTimeout(r, 500); });
  window.__openclawComposerBtn.ensure();
  await new Promise(function (r) { setTimeout(r, 300); });
  const dbg = window.__openclawComposerBtn.debug();
  console.log(JSON.stringify({
    version: first && first.version,
    same: first === window.__openclawComposerBtn,
    observers: observers,
    fetches: fetches,
    cachedOk: dbg.cached && dbg.cached.ok,
    buttons: dbg.buttons,
    warnings: warnings,
  }));
})().catch(function (e) { console.log('ERR ' + e); process.exit(1); });
"""


# A composer with the button's slot and a button of `input.version` in it.
_NODE_SLOT_HARNESS = r"""
const code = require('fs').readFileSync(process.argv[2], 'utf8');
const oldVersion = process.argv[3];
globalThis.window = globalThis;
window.addEventListener = function () {};
window.removeEventListener = function () {};
globalThis.location = { pathname: '/c/chat-1' };
globalThis.localStorage = { getItem: function () { return 'tok'; } };
globalThis.MutationObserver = class { observe() {} disconnect() {} };
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.parent = null;
                     this.dataset = {}; this.attrs = {}; this.id = ''; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener() {}
  appendChild(c) { if (c.parent) c.remove(); c.parent = this; this.children.push(c); return c; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this);
             this.parent = null; }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
  set innerHTML(v) {}
}
const group = new El('div');
const chips = { querySelectorAll: function () { return [group]; }, contains: function () { return false; } };
const row = { querySelector: function () { return chips; } };
const form = { querySelectorAll: function () { return []; }, contains: function () { return true; } };
const integ = { closest: function (sel) { return sel === 'form' ? form : row; } };
const old = new El('button');
old.id = 'openclaw-composer-status-button';
old.dataset.openclawVersion = oldVersion;
group.appendChild(old);
globalThis.document = {
  body: { appendChild: function () {} },
  documentElement: { classList: { contains: function () { return false; } } },
  getElementById: function (id) { return id === 'input-menu-button' ? integ : null; },
  querySelectorAll: function (sel) { return group.children.filter(function (e) { return '#' + e.id === sel; }); },
  createElement: function (t) { return new El(t); },
  addEventListener: function () {},
  removeEventListener: function () {},
};
globalThis.fetch = function () { return Promise.reject(new Error('no fetch expected')); };
(async function () {
  await new Function('return (async () => { ' + code + ' })()')();
  window.__openclawComposerBtn.ensure();
  console.log(JSON.stringify({
    oldKept: old.parent === group,
    versions: group.children.map(function (b) { return b.dataset.openclawVersion; }),
  }));
  process.exit(0);
})().catch(function (e) { console.log('ERR ' + e); process.exit(1); });
"""


# The composer row: [left group with Integrations] [chip row > chip group].
# ResizeObserver records its callback so a test can fire it.
_NODE_FIT_HARNESS = r"""
const code = require('fs').readFileSync(process.argv[2], 'utf8');
globalThis.window = globalThis;
window.innerWidth = 375;
window.addEventListener = function () {};
window.removeEventListener = function () {};
globalThis.location = { pathname: '/c/chat-1' };
globalThis.localStorage = { getItem: function () { return 'tok'; } };
globalThis.MutationObserver = class { observe() {} disconnect() {} };
let resized = null;
const observed = [];
globalThis.ResizeObserver = class {
  constructor(cb) { resized = cb; }
  observe(el) { observed.push(el); }
  disconnect() { observed.length = 0; }
};
const rects = { chips: { left: 40, right: 100, width: 60 }, button: { left: 50, right: 78, width: 28 } };
class El {
  constructor(tag, rect) { this.tagName = tag.toUpperCase(); this.children = []; this.parent = null;
                           this.dataset = {}; this.attrs = {}; this.id = ''; this.rect = rect; }
  get parentElement() { return this.parent; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener() {}
  appendChild(c) { if (c.parent) c.remove(); c.parent = this; this.children.push(c); return c; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this);
             this.parent = null; }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
  contains(e) { for (; e; e = e.parent) if (e === this) return true; return false; }
  getBoundingClientRect() { return rects[this.rect || 'button']; }
  set innerHTML(v) {}
}
const row = new El('div');
const left = row.appendChild(new El('div'));
const integ = left.appendChild(new El('button'));
const chips = row.appendChild(new El('div', 'chips'));
const group = chips.appendChild(new El('div', 'chips'));
const form = { querySelectorAll: function () { return []; }, contains: function () { return true; } };
integ.closest = function (sel) { return sel === 'form' ? form : row; };
row.querySelector = function () { return chips; };
chips.querySelectorAll = function () { return [group]; };
const BTN = 'openclaw-composer-status-button';
function button() {
  for (const g of [left, group]) for (const c of g.children) if (c.id === BTN) return c;
  return null;
}
globalThis.document = {
  body: { appendChild: function () {} },
  documentElement: { classList: { contains: function () { return false; } } },
  getElementById: function (id) { return id === 'input-menu-button' ? integ : id === BTN ? button() : null; },
  querySelectorAll: function (sel) { const b = button(); return b && sel === '#' + BTN ? [b] : []; },
  createElement: function (t) { return new El(t); },
  addEventListener: function () {},
  removeEventListener: function () {},
};
globalThis.fetch = function () { return Promise.reject(new Error('no fetch expected')); };
(async function () {
  await new Function('return (async () => { ' + code + ' })()')();
  const api = window.__openclawComposerBtn;
  api.ensure();
  const before = button().parent === group ? 'chips' : 'left';
  const watched = observed.includes(chips);
  rects.chips = { left: 40, right: 60, width: 20 };  // the model's name filled in: no DOM change
  if (resized) resized([]);
  console.log(JSON.stringify({
    before: before, watched: watched,
    after: button().parent === left ? 'left' : 'chips', narrowAt: api.debug().narrowAt,
  }));
  process.exit(0);
})().catch(function (e) { console.log('ERR ' + e); process.exit(1); });
"""

# The composer as each Open WebUI version draws it (argv[3]): "0.13.0" has
# More (+) and Integrations in the left group, "0.13.2" only the + (Integrations
# moved into its menu), "channel" is a channel's composer, whose + sits in the
# row's flex-1 group. Prints where the Status button went.
_NODE_ANCHOR_HARNESS = r"""
const code = require('fs').readFileSync(process.argv[2], 'utf8');
const layout = process.argv[3];
globalThis.window = globalThis;
window.innerWidth = 1280;
window.addEventListener = function () {};
window.removeEventListener = function () {};
globalThis.location = { pathname: '/c/chat-1' };
globalThis.localStorage = { getItem: function () { return 'tok'; } };
globalThis.MutationObserver = class { observe() {} disconnect() {} };
globalThis.ResizeObserver = class { observe() {} disconnect() {} };
class El {
  constructor(tag, cls, parent) {
    this.tagName = tag.toUpperCase(); this.cls = (cls || '').split(' ').filter(Boolean);
    this.children = []; this.parent = null; this.dataset = {}; this.attrs = {}; this.id = '';
    if (parent) parent.appendChild(this);
  }
  get parentElement() { return this.parent; }
  matches(sel) {
    const [tag, ...cls] = sel.split('.');
    return (!tag || this.tagName === tag.toUpperCase()) && cls.every((c) => this.cls.includes(c));
  }
  closest(sel) { for (let e = this; e; e = e.parent) if (e.matches(sel)) return e; return null; }
  contains(e) { for (; e; e = e.parent) if (e === this) return true; return false; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const direct = sel.startsWith(':scope > ');
    const want = direct ? sel.slice(9) : sel;
    if (want === 'img[alt]') return [];
    const out = [];
    const walk = (e) => { for (const c of e.children) { if (c.matches(want)) out.push(c); if (!direct) walk(c); } };
    walk(this);
    return out;
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener() {}
  appendChild(c) { if (c.parent) c.remove(); c.parent = this; this.children.push(c); return c; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this);
             this.parent = null; }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
  getBoundingClientRect() { return { left: 0, right: 10, width: 10 }; }
  set innerHTML(v) {}
}
const root = new El('div');
const form = new El('form', '', root);
const row = new El('div', 'flex justify-between', form);
let group = null;
if (layout === 'channel') {
  const left = new El('div', 'flex items-center flex-1 min-w-0', row);
  new El('button', '', left).id = 'input-menu-button';
} else {
  const left = new El('div', 'ml-1 self-end flex items-center', row);
  new El('button', '', left).id = 'input-menu-button';
  if (layout === '0.13.0') new El('button', '', left).id = 'integration-menu-button';
  group = new El('div', 'shrink-0', new El('div', 'flex-1', row));
}
const byId = (id) => {
  let f = null;
  const walk = (e) => { for (const c of e.children) { if (c.id === id) f = f || c; walk(c); } };
  walk(root);
  return f;
};
const BTN = 'openclaw-composer-status-button';
globalThis.document = {
  body: { appendChild: function () {} },
  documentElement: { classList: { contains: function () { return false; } } },
  getElementById: byId,
  querySelectorAll: function (sel) { const b = byId(BTN); return b && sel === '#' + BTN ? [b] : []; },
  createElement: function (t) { return new El(t); },
  addEventListener: function () {},
  removeEventListener: function () {},
};
globalThis.fetch = function () { return Promise.reject(new Error('no fetch expected')); };
(async function () {
  await new Function('return (async () => { ' + code + ' })()')();
  window.__openclawComposerBtn.ensure();
  const b = byId(BTN);
  console.log(JSON.stringify({ placed: !b ? 'none' : b.parent === group ? 'chips' : 'elsewhere' }));
  process.exit(0);
})().catch(function (e) { console.log('ERR ' + e); process.exit(1); });
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class SnippetUnderNodeTests(unittest.TestCase):
    def _write(self, text, suffix):
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def _run(self, code, chat_models):
        snippet = self._write(code, ".js")
        harness = self._write(_NODE_HARNESS, ".cjs")
        proc = subprocess.run(["node", harness, snippet, json.dumps(chat_models)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_node_check_parses_the_snippet(self):
        path = self._write(_render_composer_button_js(), ".js")
        proc = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _slot(self, old_version):
        snippet = self._write(_render_composer_button_js(chat_id="chat-1"), ".js")
        harness = self._write(_NODE_SLOT_HARNESS, ".cjs")
        proc = subprocess.run(["node", harness, snippet, old_version],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_a_button_of_an_older_version_is_replaced(self):
        # An older button (the clipped label of ELI-123) is replaced, once.
        self.assertEqual(self._slot("2"), {"oldKept": False, "versions": [_COMPOSER_BUTTON_VERSION]})

    def test_a_chip_row_that_narrows_without_a_dom_change_moves_the_button_left(self):
        snippet = self._write(_render_composer_button_js(chat_id="chat-1"), ".js")
        harness = self._write(_NODE_FIT_HARNESS, ".cjs")
        proc = subprocess.run(["node", harness, snippet], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout.strip().splitlines()[-1]),
                         {"before": "chips", "watched": True, "after": "left", "narrowAt": 375})

    def _placed(self, layout):
        snippet = self._write(_render_composer_button_js(chat_id="chat-1"), ".js")
        harness = self._write(_NODE_ANCHOR_HARNESS, ".cjs")
        proc = subprocess.run(["node", harness, snippet, layout], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])["placed"]

    def test_the_button_finds_the_composer_of_open_webui_0_13_0(self):
        self.assertEqual(self._placed("0.13.0"), "chips")

    def test_the_button_finds_the_composer_without_integrations_from_0_13_1(self):
        """0.13.1 moved Integrations into the + menu: the row is found from the +."""
        self.assertEqual(self._placed("0.13.2"), "chips")

    def test_a_channels_composer_gets_no_button(self):
        self.assertEqual(self._placed("channel"), "none")

    def test_a_button_of_this_version_is_kept(self):
        self.assertEqual(self._slot(_COMPOSER_BUTTON_VERSION),
                         {"oldKept": True, "versions": [_COMPOSER_BUTTON_VERSION]})

    def test_installs_once_and_is_fail_safe_without_a_composer(self):
        result = self._run(_render_composer_button_js(), ["openclaw_gateway.default"])
        self.assertEqual(result["version"], _COMPOSER_BUTTON_VERSION)
        self.assertTrue(result["same"])
        self.assertEqual(result["observers"], 1)
        self.assertEqual(result["fetches"], 1)  # chat model classified once, then cached
        self.assertTrue(result["cachedOk"])
        self.assertEqual(result["buttons"], 0)  # no composer anchors: no button
        self.assertEqual(result["warnings"], [])  # and nothing thrown or logged

    def test_other_models_chat_gets_no_button(self):
        result = self._run(_render_composer_button_js(), ["llama3.2:1b"])
        self.assertEqual(result["fetches"], 1)
        self.assertFalse(result["cachedOk"])
        self.assertEqual(result["warnings"], [])

    def test_a_vouched_chat_needs_no_fetch(self):
        result = self._run(_render_composer_button_js(chat_id="chat-1"), ["llama3.2:1b"])
        self.assertEqual(result["fetches"], 0)
        self.assertTrue(result["cachedOk"])


class ChatModelDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """Each delivery names the chat's model, so a click needs no chat download."""

    def test_rendered_with_the_vouch_only(self):
        cfg = _config_of(_render_composer_button_js(chat_id="c9", chat_model="openclaw_gateway.x"))
        self.assertEqual(cfg["chatModel"], "openclaw_gateway.x")
        self.assertIsNone(_config_of(_render_composer_button_js(chat_model="openclaw_gateway.x"))["chatModel"])

    async def test_the_action_names_the_clicked_messages_model(self):
        cfg = await ActionDeliveryTests._status_cfg(self, "openclaw_gateway.default")
        self.assertEqual(cfg["chatModel"], "openclaw_gateway.default")
        self.assertIsNone((await ActionDeliveryTests._status_cfg(self, "llama3.2:1b"))["chatModel"])

    async def test_the_pipe_names_the_turns_model(self):
        t = PipeTurnStartTests()
        t.setUp()
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": "hi"}]}
        await t._run(body, __metadata__={"chat_id": "c1", "session_id": "sock1"})
        cfg = _config_of(next(e for e in t.events if _is_composer_event(e))["data"]["code"])
        self.assertEqual(cfg["chatModel"], "openclaw_gateway.default")

    async def test_the_watcher_names_the_chats_reply_model(self):
        history = _history(("u1", "user", None), ("a1", "assistant", "u1"))
        w = _TestWatcher({"sidA": {"id": "alice", "chat_ids": ["oc"]}},
                         {"oc": _FakeChat("alice", "openclaw_gateway.default", history)})
        await w.tick(now=100.0)
        await w.tick(now=102.0)
        cfg = _config_of(w.sent[0][1]["data"]["data"]["code"])
        self.assertEqual(cfg["chatModel"], "openclaw_gateway.default")


# A chat page with the composer button and the toolbar "OpenClaw Status"
# button. Records each click's requests and whether Open WebUI's handler ran.
_NODE_CLICK_HARNESS = r"""
const code = require('fs').readFileSync(process.argv[2], 'utf8');
const which = process.argv[3];
globalThis.window = globalThis;
window.addEventListener = function () {};
window.removeEventListener = function () {};
globalThis.location = { pathname: '/c/chat-1', origin: 'http://owui' };
globalThis.localStorage = { getItem: function () { return 'tok'; } };
globalThis.MutationObserver = class { observe() {} disconnect() {} };
const listeners = [];
class El {
  constructor(tag, attrs, parent) {
    this.tagName = tag.toUpperCase(); this.attrs = attrs || {}; this.parent = parent || null;
    this.id = this.attrs.id || ''; this.className = this.attrs.class || ''; this.dataset = {}; this.style = {};
    this.children = []; if (parent) parent.children.push(this);
  }
  getAttribute(k) { return this.attrs[k]; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(t, f) { this.onclick = f; }
  matches(sel) {
    if (sel === '.chat-user') return this.className === 'chat-user';
    if (sel === '[id^="message-"]') return this.id.indexOf('message-') === 0;
    if (sel === 'button[aria-label="OpenClaw Status"]')
      return this.tagName === 'BUTTON' && this.attrs['aria-label'] === 'OpenClaw Status';
    return false;
  }
  closest(sel) { for (let e = this; e; e = e.parent) if (e.matches(sel)) return e; return null; }
  querySelector(sel) {
    const m = /^img\[src\*="([^"]+)"\]$/.exec(sel);
    if (!m) throw new Error('unsupported selector ' + sel);
    const walk = (e) => { for (const c of e.children) {
      if (c.tagName === 'IMG' && (c.attrs.src || '').indexOf(m[1]) !== -1) return c;
      const f = walk(c); if (f) return f; } return null; };
    return walk(this);
  }
  get lastElementChild() { return null; }
  set innerHTML(v) {}
}
const root = new El('div');
const userMsg = new El('div', { id: 'message-u1' }, root);
new El('div', { class: 'chat-user' }, userMsg);
const asstMsg = new El('div', { id: 'message-a1' }, root);
if (process.argv[4]) new El('img', { src: '/api/v1/models/model/profile/image?id=' + process.argv[4] + '&lang=en-US' }, asstMsg);
const toolbar = new El('button', { 'aria-label': 'OpenClaw Status' }, asstMsg);
const icon = new El('svg', {}, toolbar);
let owuiHandled = 0;
globalThis.document = {
  body: { appendChild: function () {} },
  documentElement: { classList: { contains: function () { return false; } } },
  getElementById: function () { return null; },
  querySelectorAll: function (sel) { return sel === '.chat-user' ? [userMsg.children[0]] : []; },
  createElement: function (t) { return new El(t); },
  addEventListener: function (t, f, capture) { listeners.push({ t: t, f: f, capture: capture }); },
  removeEventListener: function () {},
};
const requests = [];
globalThis.fetch = function (url, opts) {
  requests.push({ url: url, method: (opts && opts.method) || 'GET', body: opts && opts.body ? JSON.parse(opts.body) : null });
  return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
};
function click(target) {
  let stopped = false, prevented = false;
  const ev = { type: 'click', target: target, preventDefault: function () { prevented = true; },
               stopPropagation: function () { stopped = true; }, stopImmediatePropagation: function () { stopped = true; } };
  listeners.filter(function (l) { return l.t === 'click' && l.capture; }).forEach(function (l) { if (!stopped) l.f(ev); });
  if (!stopped) owuiHandled++;
  return prevented;
}
(async function () {
  await new Function('return (async () => { ' + code + ' })()')();
  let prevented = false;
  if (which === 'toolbar') prevented = click(icon);
  else window.__openclawComposerBtn.openStatus({ style: {} });
  await new Promise(function (r) { setTimeout(r, 50); });
  console.log(JSON.stringify({ requests: requests, owuiHandled: owuiHandled, prevented: prevented }));
  process.exit(0);
})().catch(function (e) { console.log('ERR ' + e); process.exit(1); });
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ClickWithoutTheChatTests(unittest.TestCase):
    """Opening the dialog sends a small body: no chat download, upload or save."""

    def _click(self, which, avatar_model="", **cfg):
        paths = []
        for text, suffix in ((_render_composer_button_js(**cfg), ".js"), (_NODE_CLICK_HARNESS, ".cjs")):
            fd, path = tempfile.mkstemp(suffix=suffix)
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            self.addCleanup(os.unlink, path)
            paths.append(path)
        proc = subprocess.run(["node", paths[1], paths[0], which, avatar_model],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_the_composer_button_posts_without_reading_the_chat(self):
        out = self._click("composer", chat_id="chat-1", chat_model="openclaw_gateway.default", session_id="sock1")
        self.assertEqual(out["requests"], [{
            "url": "/api/chat/actions/openclaw_status_action", "method": "POST",
            "body": {"model": "openclaw_gateway.default", "chat_id": "chat-1", "id": "u1", "session_id": "sock1"},
        }])

    def test_the_toolbar_button_is_taken_over_with_a_small_body(self):
        out = self._click("toolbar", chat_id="chat-1", chat_model="openclaw_gateway.default", session_id="sock1")
        self.assertEqual(out["owuiHandled"], 0)
        self.assertTrue(out["prevented"])
        self.assertEqual(out["requests"], [{
            "url": "/api/chat/actions/openclaw_status_action", "method": "POST",
            "body": {"model": "openclaw_gateway.default", "chat_id": "chat-1", "id": "a1", "session_id": "sock1"},
        }])

    def test_the_toolbar_takes_the_model_from_the_messages_own_avatar(self):
        """A chat the script was not delivered for yet: the message shows its model."""
        out = self._click("toolbar", avatar_model="openclaw_gateway.default", session_id="sock1")
        self.assertEqual(out["owuiHandled"], 0)
        self.assertEqual(out["requests"][0]["body"]["model"], "openclaw_gateway.default")

    def test_another_models_message_is_left_to_open_webui(self):
        out = self._click("toolbar", avatar_model="llama3.2:1b", session_id="sock1")
        self.assertEqual(out["owuiHandled"], 1)
        self.assertEqual(out["requests"], [])

    def test_the_toolbar_is_left_to_open_webui_when_the_model_is_unknown(self):
        out = self._click("toolbar", chat_id="chat-1", session_id="sock1")
        self.assertEqual(out["owuiHandled"], 1)
        self.assertFalse(out["prevented"])
        self.assertEqual(out["requests"], [])


if __name__ == "__main__":
    unittest.main()
