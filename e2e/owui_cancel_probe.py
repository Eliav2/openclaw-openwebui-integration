"""Runs inside the Open WebUI container: a Stop cancels a chat message write
while aiosqlite's thread is running one of its statements, as it does to a
reply being saved (ELI-205, open-webui#32741). Prints one JSON line:
{"bug": bool|None, "attempts": n, "landed": {"in_thread": n}, "what": ...}.

usage: owui_cancel_probe.py shielded|raw <attempts> <chat_id> <pipe file>

The window is made, not waited for: another connection holds SQLite's write
lock, so the write's `BEGIN IMMEDIATE` waits inside aiosqlite's thread (Open
WebUI's busy timeout is 5 s); the cancel lands there 50 ms later (every
other attempt twice, as a Stop then a closed page), then the lock is
released. A random delay missed that window on a fast CI runner and passed
on an Open WebUI that has the bug. `landed.in_thread` counts the cancels
that landed while the statement was still in the thread: the test refuses a
run where none did.

`shielded` writes through the Pipe's `_owui_upsert_message` (the cancel hits
its caller, the write goes on); `raw` calls Open WebUI's upsert directly, as
the Pipe would without its shield. It runs on a copy of the DB, so what it
breaks never reaches the running Open WebUI.

The bug is present when a cancelled write never ends, a write cannot start
in 10 s, three writes after one fail in a row, or SQLite stays locked for
another connection for 10 s.
"""
import asyncio
import importlib.util
import json
import os
import sqlite3
import sys

mode, attempts, chat_id, pipe_file = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
DB = "/tmp/eli205-probe.db"
src = sqlite3.connect("/app/backend/data/webui.db")
dst = sqlite3.connect(DB)
src.backup(dst)
src.close()
dst.close()
# Before Open WebUI's modules read it.
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
sys.path.insert(0, "/app/backend")

from open_webui.internal import db as owui_db  # noqa: E402
from open_webui.models.chats import Chats  # noqa: E402
from open_webui.models.users import Users  # noqa: E402
from sqlalchemy import event  # noqa: E402

# Per attempt: the write's `BEGIN IMMEDIATE` reached aiosqlite's thread
# (`sent`) and came back (`back`).
seen = {"sent": None, "back": None}
landed = {"in_thread": 0}


@event.listens_for(owui_db.async_engine.sync_engine, "before_cursor_execute")
def _sent(_conn, _cursor, statement, *_args):
    if statement.lstrip().upper().startswith("BEGIN IMMEDIATE") and seen["sent"] is not None:
        seen["sent"].set()


@event.listens_for(owui_db.async_engine.sync_engine, "after_cursor_execute")
def _back(_conn, _cursor, statement, *_args):
    if statement.lstrip().upper().startswith("BEGIN IMMEDIATE") and seen["back"] is not None:
        seen["back"].set()


def writer():
    """(write, pending): `pending()` is the shielded writes still running
    after their caller was cancelled (none for `raw`)."""
    if mode == "raw":
        return Chats.upsert_message_to_chat_by_id_and_message_id, set
    spec = importlib.util.spec_from_file_location("openclaw_pipe_probe", pipe_file)
    pipe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipe)
    return pipe._owui_upsert_message, lambda: set(pipe._BACKGROUND_TASKS)


def locked_for_others() -> bool:
    """A lock nobody releases: a write in flight holds it for milliseconds."""
    c = sqlite3.connect(DB, timeout=10)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.rollback()
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        c.close()


async def main() -> dict:
    write, pending = writer()
    users = await Users.get_users()
    uid = (users["users"] if isinstance(users, dict) else users)[0].id
    out = {"mode": mode, "bug": False, "attempts": 0, "landed": landed, "what": "DB writable throughout"}
    for i in range(attempts):
        out["attempts"] = i + 1
        seen.update(sent=asyncio.Event(), back=asyncio.Event())
        # Another connection holds SQLite's write lock, so the write's
        # `BEGIN IMMEDIATE` waits in aiosqlite's thread (busy timeout 5 s):
        # the cancel lands while the thread runs it, then the lock goes.
        holder = sqlite3.connect(DB, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            task = asyncio.ensure_future(write(chat_id, "eli205-probe", {"role": "assistant", "content": f"x{i}", "done": True}))
            try:
                await asyncio.wait_for(seen["sent"].wait(), 10)
            except asyncio.TimeoutError:
                # Earlier cancelled writes left the pool no usable connection.
                task.cancel()
                out.update(bug=True, what="a write could not start in 10 s: the pool's connections are stuck")
                break
            await asyncio.sleep(0.05)
            if not seen["back"].is_set():
                landed["in_thread"] += 1
            task.cancel()
            if i % 2:
                # A second cancel, as a Stop followed by a closed page sends.
                await asyncio.sleep(0)
                task.cancel()
        finally:
            holder.rollback()
            holder.close()
        # A shielded write goes on in the background after its caller is
        # cancelled: that write must end too.
        done, _ = await asyncio.wait({task, *pending()}, timeout=15)
        if pending() - done or task not in done:
            out.update(bug=True, what="the cancelled write never ended")
            break
        # A broken connection can fail the one write that next draws it from
        # the pool; the bug is a DB that stays unwritable.
        failures = []
        for _ in range(3):
            try:
                await asyncio.wait_for(Users.update_last_active_by_id(uid), 15)
                break
            except BaseException as ex:  # a CancelledError out of a wedged connection too
                failures.append(f"{type(ex).__name__} {str(ex)[:80]}")
        if len(failures) == 3:
            out.update(bug=True, what=f"the next writes failed: {failures}")
            break
    if locked_for_others():
        out.update(bug=True, what=out["what"] + "; SQLite stays locked for other connections")
    return out


# Not asyncio.run: its shutdown waits for a wedged write forever.
loop = asyncio.new_event_loop()
try:
    result = loop.run_until_complete(main())
except BaseException as ex:
    result = {"mode": mode, "bug": None, "attempts": None, "landed": landed,
              "what": f"probe died: {type(ex).__name__} {str(ex)[:200]}"}
print(json.dumps(result), flush=True)
os._exit(0)  # a wedged aiosqlite thread would keep the process alive
