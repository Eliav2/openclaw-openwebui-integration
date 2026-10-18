"""E2E: end the run at Open WebUI's first "database is locked" (ELI-204).

A sqlite write lock that is never released fails every write after it, so
each later test waited out its whole timeout for a reply that could not
come (20 tests x 60 s on run 39136858463). After each test this reads Open
WebUI's new log lines; on the first "database is locked" it asks Open WebUI
for a diagnostic dump (e2e/owui_dump.py) and stops the run, naming the dump.
"""

import os
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import owui_dump  # noqa: E402

LOCKED = "database is locked"
# From this run's start: an earlier run's lines are not this run's lock.
_last_read = {"at": time.time()}


def _new_owui_log() -> str:
    """Open WebUI's log lines since the previous call."""
    since = _last_read["at"]
    _last_read["at"] = time.time()
    out = subprocess.run(["docker", "logs", "--since", f"{since:.3f}", owui_dump.CONTAINER],
                         capture_output=True, text=True, timeout=60)
    return out.stdout + out.stderr


@pytest.fixture(autouse=True)
def _owui_database_not_locked():
    yield
    if os.environ.get("E2E_STACK") != "1" or shutil.which("docker") is None:
        return
    log = _new_owui_log()
    if LOCKED not in log:
        return
    line = next(ln for ln in log.splitlines() if LOCKED in ln)
    pytest.exit(f"Open WebUI's database is locked, so every later test would time out: {line.strip()[:300]} "
                f"| diagnostic dump: {owui_dump.dump()}", returncode=3)
