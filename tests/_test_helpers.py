"""Helpers shared by the unit suites. Not a suite itself (pytest collects
test_*.py only)."""

import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

# Bridge state the code under test writes (the sub-agent child index) goes to
# a scratch dir, never the machine's real /data/openclaw-bridge.
os.environ.setdefault("OPENCLAW_BRIDGE_STATE_DIR", tempfile.mkdtemp(prefix="oc-bridge-test-"))


def run_page(js: str, payload, tz: str | None = None):
    """Run `js` (a fake-DOM page script) under bun, else node, with `payload`
    as JSON on stdin, and return the JSON it prints. Skips the test when
    neither runtime is installed; a script that exits non-zero fails it with
    its stderr. `tz` sets the page's timezone."""
    runtime = shutil.which("bun") or shutil.which("node")
    if runtime is None:
        raise unittest.SkipTest("no bun or node")
    env = {**os.environ, "TZ": tz} if tz else None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(js)
    try:
        out = subprocess.run([runtime, fh.name], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=60, env=env)
    finally:
        os.unlink(fh.name)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


class RecordingEmitter:
    """An `__event_emitter__` that keeps every event it is given, in order,
    in `events` (the list passed in, or a new one)."""

    def __init__(self, events: list | None = None):
        self.events = [] if events is None else events

    async def __call__(self, event):
        self.events.append(event)


def fresh_child_index():
    """An empty sub-agent child index, already seeded (state.py), so one test's
    children never show in another's dialog and no open seeds by surprise."""
    import openclaw_status_action
    path = os.path.join(os.environ["OPENCLAW_BRIDGE_STATE_DIR"], openclaw_status_action._SUBAGENT_INDEX_NAME)
    if os.path.exists(path):
        os.unlink(path)
    openclaw_status_action._subagent_index_mark_seeded()


def action_gateway(conn, source: str = "pipe", keep_index: bool = False):
    """Patch the Status Action's Gateway lookup to hand out `conn`, as if it
    came from `source` (the Pipe's stashed connection by default). Starts from
    a fresh child index unless `keep_index`. A mock `conn` streams no reply
    unless the test says (`has_reply`)."""
    if not keep_index:
        fresh_child_index()
    if isinstance(conn, mock.Mock) and "has_reply" not in vars(conn):
        conn.has_reply = lambda _key: False
    return mock.patch("openclaw_status_action._get_action_connection",
                      new=mock.AsyncMock(return_value=(conn, source)))


def owui_modules(**main_attrs) -> dict:
    """sys.modules entries for a stand-in Open WebUI: `open_webui`,
    `open_webui.socket` and `open_webui.socket.main`, the last carrying
    `main_attrs` (sio, SESSION_POOL, ...). Add other entries to the dict."""
    owui = types.ModuleType("open_webui")
    sock = types.ModuleType("open_webui.socket")
    main = types.ModuleType("open_webui.socket.main")
    for name, value in main_attrs.items():
        setattr(main, name, value)
    owui.socket, sock.main = sock, main
    return {"open_webui": owui, "open_webui.socket": sock, "open_webui.socket.main": main}


def fake_owui(test, **main_attrs):
    """Install `owui_modules(**main_attrs)` until `test` ends (sys.modules is
    restored then) and return the `open_webui.socket.main` stand-in."""
    modules = owui_modules(**main_attrs)
    patch = mock.patch.dict(sys.modules, modules)
    patch.start()
    test.addCleanup(patch.stop)
    return modules["open_webui.socket.main"]


class FakeSio:
    """A stand-in for Open WebUI's socket.io server. Records every `call` as
    (event, data, to, timeout) and every `emit` as (event, data, to); a
    `call` returns what `reply(event, data, to, timeout)` returns (awaited
    when it is a coroutine), or None without one."""

    def __init__(self, reply=None):
        self.calls, self.emits = [], []
        self._reply = reply

    async def call(self, event, data, to=None, timeout=None):
        self.calls.append((event, data, to, timeout))
        out = self._reply(event, data, to, timeout) if self._reply else None
        return await out if inspect.isawaitable(out) else out

    async def emit(self, event, data, to=None, **kwargs):
        self.emits.append((event, data, to))
