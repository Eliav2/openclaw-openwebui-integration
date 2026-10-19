"""E2E: end the run at Open WebUI's first "database is locked" (ELI-204), and
run only one shard's tests when E2E_SHARD is set (ELI-246, at the end).

A task cancelled inside one of Open WebUI's DB writes can keep SQLite's write
lock for good (ELI-205): every write after it fails, so each later test waited
out its whole timeout for a reply that could not come (20 tests x 60 s on run
39136858463). A thread follows Open WebUI's log while the tests run; the
moment a line says "database is locked" it interrupts the test that is
running, and the run ends naming that test and the line.
"""

import _thread
import os
import shutil
import subprocess
import threading
import time

import pytest

CONTAINER = "owui"
LOCKED = "database is locked"
# How soon the follower looks again after `docker logs -f` ends, which it does
# whenever a test stops or restarts Open WebUI.
RESTART_POLL_S = 1.0

_state = {"test": "(between tests)", "line": None, "during": None}


def _follow(stop: threading.Event) -> None:
    since = time.time()
    while not stop.is_set():
        started = time.time()
        proc = subprocess.Popen(["docker", "logs", "-f", "--since", f"{since:.3f}", CONTAINER],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        _state["proc"] = proc
        for line in proc.stdout:
            if LOCKED in line and _state["line"] is None:
                _state["line"], _state["during"] = line.strip()[:300], _state["test"]
                _thread.interrupt_main()
                proc.kill()
                return
        proc.wait()
        since = started
        stop.wait(RESTART_POLL_S)


def _reason() -> str:
    return (f"Open WebUI's database is locked (ELI-205), so every later test would time out. "
            f"Running: {_state['during']}. Log: {_state['line']}")


@pytest.fixture(scope="session", autouse=True)
def _owui_lock_watch():
    if os.environ.get("E2E_STACK") != "1" or shutil.which("docker") is None:
        yield
        return
    stop = threading.Event()
    watcher = threading.Thread(target=_follow, args=(stop,), name="owui-lock-watch", daemon=True)
    watcher.start()
    yield
    stop.set()
    proc = _state.get("proc")
    if proc is not None and proc.poll() is None:
        proc.kill()


def pytest_runtest_logstart(nodeid, location):
    _state["test"] = nodeid


def pytest_runtest_logfinish(nodeid, location):
    _state["test"] = "(between tests)"


# The interrupt ends the session wherever it lands, in a test or between two.
def pytest_keyboard_interrupt(excinfo):
    if _state["line"] is not None:
        print(f"\n{_reason()}")


def pytest_sessionfinish(session, exitstatus):
    if _state["line"] is not None:
        session.exitstatus = 3


# ── Shards (ELI-246) ────────────────────────────────────────────────────────
# With E2E_SHARD set, only that shard's tests of e2e/shards.py run. A listed
# test this run collected nothing for fails the run: a renamed test must not
# leave its shard quietly shorter.

def _load_shards() -> dict:
    import importlib.util
    spec = importlib.util.spec_from_file_location("e2e_shards", os.path.join(os.path.dirname(__file__), "shards.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SHARDS


def pytest_collection_modifyitems(config, items):
    shard = os.environ.get("E2E_SHARD")
    if not shard:
        return
    shards = _load_shards()
    if shard not in shards:
        raise pytest.UsageError(f"E2E_SHARD={shard!r} is not a shard of e2e/shards.py: {sorted(shards)}")
    wanted = set(shards[shard])
    key = {id(it): f"{it.path.name}::{getattr(it, 'originalname', it.name)}" for it in items}
    collected = set(key.values())
    files = {k.split("::")[0] for k in collected}
    missing = sorted(w for w in wanted if w.split("::")[0] in files and w not in collected)
    if missing:
        raise pytest.UsageError(f"shard {shard} lists tests this run did not collect: {missing}")
    config.hook.pytest_deselected(items=[it for it in items if key[id(it)] not in wanted])
    items[:] = [it for it in items if key[id(it)] in wanted]
