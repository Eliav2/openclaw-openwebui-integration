"""E2E stack only: SIGUSR1 makes Open WebUI write what it is doing (ELI-204).

`start_stack.sh` puts this directory on the container's PYTHONPATH, so
Python imports it at start-up in Open WebUI's process (PID 1 in the
container). On SIGUSR1 it writes /owui-diag/dump-<pid>-<time>.txt:

- every thread's stack;
- every asyncio task, with the whole chain of coroutines it is awaiting
  (`Task.get_stack()` shows only the outermost frame of a suspended task),
  and in each frame the SQLAlchemy sessions it holds, marked when one is
  inside a transaction;
- every open sqlite3 connection, and whether it is inside a transaction;
- the POSIX locks /proc/locks shows on the database and its -shm file,
  each with the pid and command line that holds it.

A sqlite write lock that is never released (ELI-204: every later write in
the run failed with "database is locked") is held by one connection inside
a transaction. The dump names the process holding it; when that is Open
WebUI, the connection (idle inside its transaction) and the task whose
frame holds the session.
"""

import gc
import os
import signal
import sys
import time
import traceback

OUT_DIR = os.environ.get("E2E_OWUI_DIAG_DIR", "/owui-diag")
DB_PATH = os.path.join(os.environ.get("DATA_DIR", "/app/backend/data"), "webui.db")


def _sessions_in(frame):
    """The SQLAlchemy sessions a frame holds: (name, in a transaction)."""
    out = []
    try:
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy.orm import Session
    except Exception:
        return out
    for name, value in list(frame.f_locals.items()):
        if isinstance(value, (AsyncSession, Session)):
            try:
                busy = bool(value.in_transaction())
            except Exception as ex:
                busy = f"unknown ({ex!r})"
            out.append((name, busy))
    return out


def _await_chain(coro):
    """The frames of a coroutine and of everything it awaits, outermost first."""
    frames = []
    seen = set()
    while coro is not None and id(coro) not in seen:
        seen.add(id(coro))
        frame = (getattr(coro, "cr_frame", None) or getattr(coro, "ag_frame", None)
                 or getattr(coro, "gi_frame", None))
        if frame is not None:
            frames.append(frame)
        coro = (getattr(coro, "cr_await", None) or getattr(coro, "ag_await", None)
                or getattr(coro, "gi_yieldfrom", None))
    return frames


def _frame_lines(frame):
    lines = [f'    File "{frame.f_code.co_filename}", line {frame.f_lineno}, in {frame.f_code.co_name}']
    for name, busy in _sessions_in(frame):
        mark = "  <-- IN A TRANSACTION" if busy is True else ""
        lines.append(f"      holds session {name!r}: in_transaction={busy}{mark}")
    return lines


def _tasks_section():
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return ["(no running event loop in the signalled thread)"]
    lines = []
    tasks = sorted(asyncio.all_tasks(loop), key=lambda t: t.get_name())
    holders = 0
    for task in tasks:
        frames = _await_chain(task.get_coro())
        body = [line for f in frames for line in _frame_lines(f)]
        if any("IN A TRANSACTION" in line for line in body):
            holders += 1
        lines.append(f"-- task {task.get_name()} (done={task.done()})")
        lines.extend(body or ["    (no frames)"])
    lines.insert(0, f"{len(tasks)} task(s); {holders} hold a session inside a transaction")
    return lines


def _threads_section():
    lines = []
    for ident, frame in sys._current_frames().items():
        lines.append(f"-- thread {ident}")
        lines.extend("    " + line.rstrip() for line in traceback.format_stack(frame))
    return lines


def _thread_state(conn_thread):
    """What an aiosqlite connection's own thread is doing: aiosqlite runs each
    statement as `result = function()` in its worker loop (`run` before 0.20,
    `_connection_worker_thread` since) and otherwise waits on its queue. A
    connection inside a transaction whose thread is idle is the one HOLDING
    it; one running a statement may be waiting for a lock."""
    frame = sys._current_frames().get(getattr(conn_thread, "ident", None))
    if frame is None:
        return "no thread"
    where = f"{os.path.basename(frame.f_code.co_filename)}:{frame.f_lineno} in {frame.f_code.co_name}"
    try:
        import linecache
        running = frame.f_code.co_name in ("run", "_connection_worker_thread") and "function()" in linecache.getline(
            frame.f_code.co_filename, frame.f_lineno)
    except Exception:
        running = False
    return ("running a statement" if running else "idle") + f" ({where})"


def _sqlite_section():
    import sqlite3
    import threading
    conns = [o for o in gc.get_objects() if isinstance(o, sqlite3.Connection)]
    busy = [c for c in conns if c.in_transaction]
    lines = [f"{len(conns)} open sqlite3 connection(s); {len(busy)} inside a transaction"]
    for c in conns:
        refs = [r for r in gc.get_referrers(c) if r is not conns]
        kinds = sorted({type(r).__module__ + "." + type(r).__qualname__ for r in refs})
        # aiosqlite's Connection was a Thread before 0.20 and keeps one in `_thread` since.
        owner = next((r for r in refs if type(r).__module__.startswith("aiosqlite")), None)
        thread = owner if isinstance(owner, threading.Thread) else getattr(owner, "_thread", None)
        state = _thread_state(thread) if thread is not None else "no aiosqlite thread"
        mark = "  <-- HOLDS ITS TRANSACTION OPEN" if c.in_transaction and state.startswith("idle") else ""
        lines.append(f"-- {id(c):#x} in_transaction={c.in_transaction} total_changes={c.total_changes} "
                     f"thread: {state}{mark}; referred to by {kinds}")
    holding = sum(1 for line in lines if "HOLDS ITS TRANSACTION OPEN" in line)
    lines[0] += f"; {holding} idle inside one (holding it open)"
    return lines


# The lock bytes of a WAL database's -shm file (sqlite's WALINDEX_LOCK_OFFSET
# is 120): one writer at a time holds WRITE.
_SHM_LOCKS = {120: "WAL WRITE lock (the writer)", 121: "checkpoint", 122: "recovery",
              123: "read mark 0", 124: "read mark 1", 125: "read mark 2", 126: "read mark 3",
              127: "read mark 4", 128: "dead-man switch (connection open)"}
# The rollback-journal locks on the database file itself.
_DB_LOCKS = {1073741824: "PENDING", 1073741825: "RESERVED (the writer)", 1073741826: "SHARED (readers)"}


def _locks_section():
    try:
        with open("/proc/locks") as fh:
            table = [r.rstrip() for r in fh]
    except OSError as ex:
        return [f"(cannot read /proc/locks: {ex})"]
    out = []
    for path, names in ((DB_PATH, _DB_LOCKS), (DB_PATH + "-shm", _SHM_LOCKS), (DB_PATH + "-wal", {})):
        try:
            ino = os.stat(path).st_ino
        except OSError:
            continue
        rows = [r for r in table if f":{ino} " in r]
        out.append(f"{path} (inode {ino}): {len(rows)} lock(s)")
        for row in rows:
            # "17: POSIX  ADVISORY  WRITE 98 00:5f:957864 120 120": type, pid, inode, first and last byte.
            fields = row.split()
            kind, pid = (fields[3], fields[4]) if len(fields) > 6 else ("?", "?")
            first = int(fields[6]) if len(fields) > 6 and fields[6].isdigit() else -1
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    cmd = fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
            except OSError:
                cmd = "?"
            what = names.get(first, f"byte {first}")
            mine = " (this process)" if pid == str(os.getpid()) else ""
            out.append(f"  {kind:5} {what}: pid {pid}{mine}: {cmd[:200]}")
    return out or [f"(no database files at {DB_PATH})"]


def _dump(signum, frame):
    sections = [("asyncio tasks", _tasks_section), ("threads", _threads_section),
                ("sqlite3 connections", _sqlite_section), ("locks on the database files", _locks_section)]
    out = [f"Open WebUI diagnostic dump, pid {os.getpid()}, {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"]
    for title, section in sections:
        out.append(f"\n===== {title} =====")
        try:
            out.extend(section())
        except Exception:
            out.append(traceback.format_exc())
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f"dump-{os.getpid()}-{int(time.time())}.txt")
        with open(path + ".part", "w") as fh:
            fh.write("\n".join(out) + "\n")
        os.replace(path + ".part", path)
    except Exception:
        traceback.print_exc()


try:
    signal.signal(signal.SIGUSR1, _dump)
except Exception:
    pass
