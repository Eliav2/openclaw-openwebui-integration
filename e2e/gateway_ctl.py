"""Stop and start the E2E stack's own Gateway, for the tests that need it down (ELI-504).

Only the pid in E2E_GATEWAY_PID_FILE (written by e2e/start_stack.sh) and its children are
signalled, so a Gateway the stack did not start is never touched. A stop is a clean one
(SIGTERM): the Gateway's crash-loop guard counts unclean boots only."""
from __future__ import annotations

import os
import signal
import subprocess
import time

PID_FILE = os.environ.get("E2E_GATEWAY_PID_FILE", "")
STOP_TIMEOUT_S = 60
START_TIMEOUT_S = 180
POLL_S = 1.0
CALL_TIMEOUT_S = 30


def available() -> bool:
    return bool(PID_FILE) and os.path.exists(PID_FILE)


def up() -> bool:
    try:
        return subprocess.run(["openclaw", "gateway", "call", "sessions.list", "--json", "--params", "{}"],
                              capture_output=True, timeout=CALL_TIMEOUT_S).returncode == 0
    except Exception:
        return False


def _alive(pid: int) -> bool:
    """Any thread of `pid` still runs: a killed process turns zombie first while its other
    threads hold the port."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rsplit(")", 1)[1].split()[0] != "Z":
                return True
        return len(os.listdir(f"/proc/{pid}/task")) > 1
    except OSError:
        return False


def _tree(pid: int) -> list[int]:
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        kids = subprocess.run(["pgrep", "-P", str(p)], capture_output=True, text=True).stdout.split()
        todo.extend(int(k) for k in kids)
    return [p for p in out if _alive(p)]


def _wait(what: str, check, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if check():
            return
        time.sleep(POLL_S)
    raise AssertionError(f"timed out waiting for {what}")


def stop() -> None:
    """The stack's Gateway, stopped cleanly and gone."""
    with open(PID_FILE) as f:
        pids = _tree(int(f.read().strip()))
    for p in pids:
        os.kill(p, signal.SIGTERM)
    _wait("the Gateway to be gone", lambda: not any(_alive(p) for p in pids), STOP_TIMEOUT_S)


def start() -> None:
    """The stack's Gateway, started again as e2e/start_stack.sh does, and answering. Safe after
    a stop that did not finish (it timed out, or left part of the process tree): what is left of
    the old one is killed first, and a Gateway that already answers is left alone."""
    if up():
        return
    try:
        with open(PID_FILE) as f:
            left = _tree(int(f.read().strip()))
    except (OSError, ValueError):
        left = []
    for p in left:
        os.kill(p, signal.SIGKILL)
    _wait("the rest of the old Gateway to be gone", lambda: not any(_alive(p) for p in left), STOP_TIMEOUT_S)
    log = os.path.join(os.path.dirname(PID_FILE), "gateway-after-stop.log")
    env = {**os.environ, "E2E_FAKE_CLAUDE_DIR": os.path.dirname(PID_FILE)}
    proc = subprocess.Popen(["openclaw", "gateway", "run"], stdout=open(log, "a"),
                            stderr=subprocess.STDOUT, start_new_session=True, env=env)
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))
    _wait("the Gateway to answer", up, START_TIMEOUT_S)
