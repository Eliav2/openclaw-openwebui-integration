#!/usr/bin/env python3
"""Ask the E2E stack's Open WebUI for a diagnostic dump (ELI-204).

Sends SIGUSR1 to the container's PID 1 (Open WebUI's server process), which
`e2e/owui_diag/sitecustomize.py` answers by writing a dump of its tasks,
threads and sqlite connections into the logs directory's `owui-diag/`.
Prints the dump's path, or why there is none.

    python3 e2e/owui_dump.py [<owui-diag dir>]
"""

import os
import subprocess
import sys
import time

CONTAINER = "owui"
# Writing the dump walks every object once: well under a second on the stack.
WRITE_DEADLINE_S = 30


def diag_dir() -> str:
    """`owui-diag/` next to the stack's other logs (start_stack.sh mounts it)."""
    log = os.environ.get("E2E_FAKE_LLM_LOG")
    root = os.path.dirname(log) if log else os.environ.get("E2E_LOG_DIR", os.path.join(os.getcwd(), "e2e-logs"))
    return os.path.join(root, "owui-diag")


def _dumps(directory: str) -> set:
    try:
        return {n for n in os.listdir(directory) if n.startswith("dump-") and n.endswith(".txt")}
    except OSError:
        return set()


def dump(directory: str | None = None) -> str:
    directory = directory or diag_dir()
    before = _dumps(directory)
    sent = subprocess.run(["docker", "kill", "--signal", "USR1", CONTAINER],
                          capture_output=True, text=True, timeout=60)
    if sent.returncode != 0:
        return f"no dump: signalling Open WebUI failed: {(sent.stderr or sent.stdout).strip()}"
    deadline = time.monotonic() + WRITE_DEADLINE_S
    while time.monotonic() < deadline:
        new = _dumps(directory) - before
        if new:
            return os.path.join(directory, sorted(new)[-1])
        time.sleep(0.2)
    return f"no dump in {directory} within {WRITE_DEADLINE_S} s of the signal"


if __name__ == "__main__":
    print(dump(sys.argv[1] if len(sys.argv) > 1 else None))
