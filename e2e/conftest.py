"""E2E: end the run at Open WebUI's first "database is locked" (ELI-204), and
run only one shard's tests when E2E_SHARD is set (ELI-246, at the end).

A task cancelled inside one of Open WebUI's DB writes can keep SQLite's write
lock for good (ELI-205): every write after it fails, so each later test waited
out its whole timeout for a reply that could not come (20 tests x 60 s on run
39136858463). A thread follows Open WebUI's log while the tests run. When a
line says "database is locked", it asks the database for its write lock from
another process, waiting up to LOCK_WAIT_S. Still locked: it interrupts the
test that is running, and the run ends naming that test and the line. Free: the
lock was contention that cleared (two writers at once; the busy one gave up),
not a wedge. The test it hit fails on its own if it lost a write, the run goes
on, and the summary lists every such line.
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
DB = "/app/backend/data/webui.db"
# How long the check waits for the write lock: twice Open WebUI's own busy
# timeout (5 s), so a writer that held it as long as Open WebUI allows is done.
LOCK_WAIT_S = 10
# How soon the follower looks again after `docker logs -f` ends, which it does
# whenever a test stops or restarts Open WebUI.
RESTART_POLL_S = 1.0

_state = {"test": "(between tests)", "line": None, "during": None, "cleared": []}

_CHECK = (f"import sqlite3; c = sqlite3.connect({DB!r}, timeout={LOCK_WAIT_S}); "
          "c.execute('BEGIN IMMEDIATE'); c.rollback(); print('free')")


def _still_locked() -> bool:
    """Whether another process still cannot take the database's write lock
    after waiting LOCK_WAIT_S. A check that cannot run counts as locked: the
    run ends as it did before this check existed."""
    try:
        out = subprocess.run(["docker", "exec", CONTAINER, "python3", "-c", _CHECK],
                             capture_output=True, text=True, timeout=LOCK_WAIT_S + 20)
    except Exception:
        return True
    return out.returncode != 0 or out.stdout.strip() != "free"


def _on_lock_line(line: str, still_locked=_still_locked) -> bool:
    """Handle one "database is locked" line. True: a wedge, end the run."""
    line, test = line.strip()[:300], _state["test"]
    if still_locked():
        _state["line"], _state["during"] = line, test
        return True
    _state["cleared"].append((test, line))
    return False


def _follow(stop: threading.Event) -> None:
    since = time.time()
    while not stop.is_set():
        started = time.time()
        proc = subprocess.Popen(["docker", "logs", "-f", "--since", f"{since:.3f}", CONTAINER],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        _state["proc"] = proc
        for line in proc.stdout:
            if LOCKED in line and _state["line"] is None and _on_lock_line(line):
                _thread.interrupt_main()
                proc.kill()
                return
        proc.wait()
        since = started
        stop.wait(RESTART_POLL_S)


def _reason() -> str:
    return (f"Open WebUI's database is locked (ELI-205) and still was {LOCK_WAIT_S} s later, "
            f"so every later test would time out. Running: {_state['during']}. Log: {_state['line']}")


def pytest_terminal_summary(terminalreporter):
    cleared = _state["cleared"]
    if not cleared:
        return
    terminalreporter.section("Open WebUI database lock that cleared")
    terminalreporter.write_line(f"{len(cleared)} \"{LOCKED}\" line(s); another process could write "
                                f"within {LOCK_WAIT_S} s each time, so the run went on:")
    for test, line in cleared:
        terminalreporter.write_line(f"  {test}: {line}")


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
