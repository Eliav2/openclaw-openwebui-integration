"""Helpers shared by the unit suites. Not a suite itself (pytest collects test_*.py only)."""

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

# Bridge state goes to a scratch dir, never /data/openclaw-bridge.
os.environ.setdefault("OPENCLAW_BRIDGE_STATE_DIR", tempfile.mkdtemp(prefix="oc-bridge-test-"))


def run_page(js: str, payload, tz: str | None = None):
    """Run fake-DOM page script `js` under bun or node with `payload` on stdin;
    return its JSON. Skips with neither runtime; `tz` sets the timezone."""
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
    """An `__event_emitter__` that appends every event to `events`."""

    def __init__(self, events: list | None = None):
        self.events = [] if events is None else events

    async def __call__(self, event):
        self.events.append(event)


def fresh_child_index():
    """An empty, already seeded child index (state.py), so tests do not see
    each other's children."""
    import openclaw_status_action
    path = os.path.join(os.environ["OPENCLAW_BRIDGE_STATE_DIR"], openclaw_status_action._SUBAGENT_INDEX_NAME)
    if os.path.exists(path):
        os.unlink(path)
    openclaw_status_action._subagent_index_mark_seeded()


def action_gateway(conn, source: str = "pipe", keep_index: bool = False):
    """Make the Status Action's Gateway lookup return `conn` from `source`, with a
    fresh child index unless `keep_index`; a mock streams no reply unless `has_reply`."""
    if not keep_index:
        fresh_child_index()
    if isinstance(conn, mock.Mock) and "has_reply" not in vars(conn):
        conn.has_reply = lambda _key: False
    return mock.patch("openclaw_status_action._get_action_connection",
                      new=mock.AsyncMock(return_value=(conn, source)))


def owui_modules(**main_attrs) -> dict:
    """sys.modules entries for a stand-in Open WebUI; `open_webui.socket.main`
    carries `main_attrs`."""
    owui = types.ModuleType("open_webui")
    sock = types.ModuleType("open_webui.socket")
    main = types.ModuleType("open_webui.socket.main")
    for name, value in main_attrs.items():
        setattr(main, name, value)
    owui.socket, sock.main = sock, main
    return {"open_webui": owui, "open_webui.socket": sock, "open_webui.socket.main": main}


def fake_owui(test, **main_attrs):
    """Install `owui_modules(**main_attrs)` for the test; return the
    `open_webui.socket.main` stand-in."""
    modules = owui_modules(**main_attrs)
    patch = mock.patch.dict(sys.modules, modules)
    patch.start()
    test.addCleanup(patch.stop)
    return modules["open_webui.socket.main"]


class FakeSio:
    """A stand-in socket.io server recording `call` and `emit`; a `call`
    returns (awaits) `reply(event, data, to, timeout)`, or None."""

    def __init__(self, reply=None):
        self.calls, self.emits = [], []
        self._reply = reply

    async def call(self, event, data, to=None, timeout=None):
        self.calls.append((event, data, to, timeout))
        out = self._reply(event, data, to, timeout) if self._reply else None
        return await out if inspect.isawaitable(out) else out

    async def emit(self, event, data, to=None, **kwargs):
        self.emits.append((event, data, to))
