"""Helpers shared by the unit suites. Not a suite itself (pytest collects
test_*.py only)."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest


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
