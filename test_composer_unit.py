#!/usr/bin/env python3
"""Unit tests for the composer Status button (src/openclaw_pipe_pkg/composer.py).

Covers both built artifacts that carry it: the Pipe (turn-start delivery and
the composer watcher) and the Status Action (delivery on every run), plus a
syntax and idempotence check of the JS snippet under node when node exists.

Same dependency-stubbing pattern as test_pipe_unit.py, so this runs without a
real OWUI/websockets/cryptography environment.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before this file's stub can shadow it)

if "websockets" not in sys.modules:
    sys.modules["websockets"] = types.SimpleNamespace(
        WebSocketClientProtocol=object,
        exceptions=types.SimpleNamespace(ConnectionClosed=Exception),
    )

if "cryptography" not in sys.modules:
    crypto = types.ModuleType("cryptography")
    hazmat = types.ModuleType("cryptography.hazmat")
    primitives = types.ModuleType("cryptography.hazmat.primitives")
    asymmetric = types.ModuleType("cryptography.hazmat.primitives.asymmetric")
    ed25519 = types.ModuleType("cryptography.hazmat.primitives.asymmetric.ed25519")
    serialization = types.ModuleType("cryptography.hazmat.primitives.serialization")
    backends = types.ModuleType("cryptography.hazmat.backends")

    class _DummyPrivateKey:
        @staticmethod
        def generate():
            raise RuntimeError("cryptography stub cannot generate keys")

    ed25519.Ed25519PrivateKey = _DummyPrivateKey
    serialization.Encoding = types.SimpleNamespace(Raw="Raw", PEM="PEM")
    serialization.PublicFormat = types.SimpleNamespace(Raw="Raw")
    serialization.PrivateFormat = types.SimpleNamespace(PKCS8="PKCS8")
    serialization.NoEncryption = lambda: None
    serialization.load_pem_private_key = lambda *args, **kwargs: None
    backends.default_backend = lambda: None

    sys.modules["cryptography"] = crypto
    sys.modules["cryptography.hazmat"] = hazmat
    sys.modules["cryptography.hazmat.primitives"] = primitives
    sys.modules["cryptography.hazmat.primitives.asymmetric"] = asymmetric
    sys.modules["cryptography.hazmat.primitives.asymmetric.ed25519"] = ed25519
    sys.modules["cryptography.hazmat.primitives.serialization"] = serialization
    sys.modules["cryptography.hazmat.backends"] = backends

if "pydantic" not in sys.modules:
    pydantic = types.ModuleType("pydantic")

    class _BaseModel:
        def __init__(self, **kwargs):
            for name, value in self.__class__.__dict__.items():
                if name.startswith("_") or callable(value):
                    continue
                setattr(self, name, kwargs.get(name, value))

    def _field(*, default=None, **kwargs):
        return default

    pydantic.BaseModel = _BaseModel
    pydantic.Field = _field
    sys.modules["pydantic"] = pydantic

import openclaw_pipe  # noqa: E402
import openclaw_status_action  # noqa: E402
from openclaw_pipe import (  # noqa: E402
    GatewayError,
    Pipe,
    _COMPOSER_BUTTON_VERSION,
    _ComposerWatcher,
    _composer_chat_is_openclaw,
    _composer_latest_user_message_id,
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
    """rows: (id, role, parentId)."""
    messages = {mid: {"id": mid, "role": role, "parentId": parent} for mid, role, parent in rows}
    return {"messages": messages, "currentId": current or rows[-1][0]}


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


class EmitHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_one_execute_event(self):
        events = []

        async def emitter(e):
            events.append(e)

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


class ChatShapeHelperTests(unittest.TestCase):
    def test_latest_user_message_skips_assistant_leaf(self):
        h = _history(("u1", "user", None), ("a1", "assistant", "u1"),
                     ("u2", "user", "a1"), ("a2", "assistant", "u2"))
        self.assertEqual(_composer_latest_user_message_id(h), "u2")

    def test_latest_user_message_falls_back_to_leaf(self):
        h = _history(("a1", "assistant", None))
        self.assertEqual(_composer_latest_user_message_id(h), "a1")
        self.assertIsNone(_composer_latest_user_message_id({}))
        self.assertIsNone(_composer_latest_user_message_id(None))

    def test_openclaw_chat_detection(self):
        prefixes = ["openclaw_gateway."]
        self.assertTrue(_composer_chat_is_openclaw({"models": ["openclaw_gateway.default"]}, prefixes))
        self.assertFalse(_composer_chat_is_openclaw({"models": ["llama3.2:1b"]}, prefixes))
        leaf_only = {"models": [], "history": {"currentId": "a", "messages": {
            "a": {"model": "openclaw_gateway.x"}}}}
        self.assertTrue(_composer_chat_is_openclaw(leaf_only, prefixes))
        self.assertFalse(_composer_chat_is_openclaw(None, prefixes))


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

        async def emitter(e):
            events.append(e)

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

        async def emitter(e):
            events.append(e)

        action = openclaw_status_action.Action()

        async def fake_status(*args, **kwargs):
            return {"status": "ok"}

        with mock.patch.object(action, "_run_status", fake_status):
            await action.action({"chat_id": "c1", "id": "m1", "model": model},
                                __user__={"id": "u"}, __event_emitter__=emitter)
        return _config_of(events[-1]["data"]["code"])

    async def test_vouches_only_for_openclaw_chats(self):
        # The Action is global: its spark also shows under other models'
        # replies, and a dotted id like llama3.2 must not become a prefix.
        cfg = await self._status_cfg("openclaw_gateway.default")
        self.assertEqual(cfg["chatId"], "c1")
        cfg = await self._status_cfg("llama3.2:1b")
        self.assertIsNone(cfg["chatId"])
        self.assertEqual(cfg["modelPrefixes"], ["openclaw_gateway."])

    async def test_compact_and_drawer_runs_do_not_emit(self):
        events = []

        async def emitter(e):
            events.append(e)

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

        async def emitter(e):
            events.append(e)

        action = openclaw_status_action.Action()

        async def boom(*args, **kwargs):
            raise RuntimeError("dialog failed")

        with mock.patch.object(action, "_run_status", boom):
            with self.assertRaises(RuntimeError):
                await action.action({}, __user__={"id": "u"}, __event_emitter__=emitter)
        self.assertTrue(_is_composer_event(events[-1]))


class _FakeChat:
    def __init__(self, user_id, models, history):
        self.user_id = user_id
        self.chat = {"models": models, "history": history}


class _TestWatcher(_ComposerWatcher):
    def __init__(self, pool, chats, enabled=True, acks=None):
        super().__init__(lambda: types.SimpleNamespace(COMPOSER_STATUS_BUTTON=enabled),
                         lambda: ["openclaw_gateway."])
        self.pool = pool
        self.chats = chats
        self.sent = []
        # Per-call ack results; the tab acks by default.
        self.acks = list(acks or [])

    def _session_pool_batches(self):
        return [list(self.pool.items())]

    async def _load_chat(self, chat_id):
        return self.chats.get(chat_id)

    async def _call_sid(self, sid, payload):
        self.sent.append((sid, payload))
        return self.acks.pop(0) if self.acks else True


class ComposerWatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.history = _history(("u1", "user", None), ("a1", "assistant", "u1"))
        self.chats = {
            "oc": _FakeChat("alice", ["openclaw_gateway.default"], self.history),
            "llama": _FakeChat("alice", ["llama3.2:1b"], self.history),
            "bobs": _FakeChat("bob", ["openclaw_gateway.default"], self.history),
        }

    async def test_pushes_once_after_settle_to_that_sid_targeting_user_message(self):
        pool = {"sidA": {"id": "alice", "chat_ids": ["oc"]}}
        w = _TestWatcher(pool, self.chats)
        self.assertEqual(await w.tick(now=100.0), 0)  # first sighting: settle
        self.assertEqual(await w.tick(now=102.0), 1)
        self.assertEqual(await w.tick(now=104.0), 0)  # handled already
        self.assertEqual(len(w.sent), 1)
        sid, payload = w.sent[0]
        self.assertEqual(sid, "sidA")
        self.assertEqual(payload["chat_id"], "oc")
        self.assertEqual(payload["message_id"], "u1")
        self.assertEqual(payload["data"]["type"], "execute")
        cfg = _config_of(payload["data"]["data"]["code"])
        self.assertEqual(cfg["sessionId"], "sidA")
        self.assertEqual(cfg["chatId"], "oc")

    async def test_no_ack_retries_then_gives_up(self):
        # The tab drops the event (no ack) while it has not loaded the chat yet.
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

        async def flaky(chat_id):
            calls.append(chat_id)
            if len(calls) == 1:
                raise RuntimeError("db hiccup")
            return self.chats.get(chat_id)

        w._load_chat = flaky
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
        for name in ("open_webui.socket.main", "open_webui.socket", "open_webui"):
            sys.modules.pop(name, None)

    def _install_fake_owui(self):
        owui = types.ModuleType("open_webui")
        sock = types.ModuleType("open_webui.socket")
        main = types.ModuleType("open_webui.socket.main")
        main.SESSION_POOL = {}
        owui.socket = sock
        sock.main = main
        sys.modules.update({"open_webui": owui, "open_webui.socket": sock,
                            "open_webui.socket.main": main})
        return main

    async def test_outside_owui_does_nothing(self):
        sys.modules.pop("open_webui", None)
        with mock.patch.dict(sys.modules, {"open_webui": None}):
            self.assertIsNone(_ensure_composer_watcher(lambda: Pipe.Valves()))

    async def test_starts_once_and_cancels_a_previous_deploys_task(self):
        main = self._install_fake_owui()
        stale = asyncio.get_running_loop().create_task(asyncio.sleep(60))
        setattr(main, openclaw_pipe._COMPOSER_WATCH_ATTR, stale)
        task = _ensure_composer_watcher(lambda: Pipe.Valves())
        self.assertIsNotNone(task)
        self.assertIs(_ensure_composer_watcher(lambda: Pipe.Valves()), task)
        self.assertIs(getattr(main, openclaw_pipe._COMPOSER_WATCH_ATTR), task)
        await asyncio.sleep(0)
        self.assertTrue(stale.cancelled())

    async def test_disabled_valve_does_not_start(self):
        self._install_fake_owui()
        valves = Pipe.Valves()
        valves.COMPOSER_STATUS_BUTTON = False
        self.assertIsNone(_ensure_composer_watcher(lambda: valves))


# Runs the snippet the way OWUI 0.12's Chat.svelte does
# (`new Function('return (async () => { ' + code + ' })()')`) against a minimal
# fake DOM with no composer in it. The real DOM placement is validated live
# (see the PR); this proves the snippet parses inside that wrapper, installs
# one observer, treats a repeat delivery as a config refresh, never throws
# without a composer, and fetches a chat at most once.
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


if __name__ == "__main__":
    unittest.main()
