"""Runs inside the Open WebUI container: a Stop's cancel lands on a chat
message write while it commits, and a second cancel lands while SQLAlchemy
throws away the connection the first one broke, as a Stop followed by a
closed page does (ELI-205, open-webui#32741). Prints one JSON line:
{"bug": bool|None, "attempts": n, "landed": {"commit": n, "second": n}, "what": ...}.

usage: owui_cancel_probe.py shielded|raw <attempts> <chat_id> <pipe file>

Both cancels fire from SQLAlchemy's own events on Open WebUI's engine (the
connection's `commit`, the pool's `invalidate`), so they land at the same
point on any machine. A random delay missed the window on a fast CI runner
and passed on an Open WebUI that has the bug. `landed` counts the cancels
that fired there: the test refuses a run where none landed in a commit.

`shielded` writes through the Pipe's `_owui_upsert_message` (the cancel hits
its caller, the write goes on); `raw` calls Open WebUI's upsert directly, as
the Pipe would without its shield. It runs on a copy of the DB, so what it
breaks never reaches the running Open WebUI.

The bug is present when a cancelled write never ends, the next write fails,
or SQLite stays locked for another connection for 10 s.
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

landed = {"commit": 0, "second": 0}
# The attempt's task: cancelled at its commit, then again at the invalidate.
armed = {"first": None, "second": None}


@event.listens_for(owui_db.async_engine.sync_engine, "commit")
def _cancel_at_commit(*_args):
    task = armed["first"]
    if task is None or task.done():
        return
    armed["first"], armed["second"] = None, task
    landed["commit"] += 1
    # Delivered at the commit's await, while aiosqlite's thread runs it.
    task.cancel()


@event.listens_for(owui_db.async_engine.sync_engine.pool, "invalidate")
def _cancel_again(*_args):
    task = armed["second"]
    if task is None or task.done():
        return
    armed["second"] = None
    landed["second"] += 1
    # Delivered while the broken connection is being closed.
    asyncio.get_running_loop().call_soon(task.cancel)


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
        task = asyncio.ensure_future(write(chat_id, "eli205-probe", {"role": "assistant", "content": f"x{i}", "done": True}))
        armed.update(first=task, second=None)
        # A shielded write goes on in the background after its caller is
        # cancelled: that write must end too.
        done, _ = await asyncio.wait({task, *pending()}, timeout=15)
        armed.update(first=None, second=None)
        if pending() - done or task not in done:
            out.update(bug=True, what="the cancelled write never ended")
            break
        try:
            await asyncio.wait_for(Users.update_last_active_by_id(uid), 15)
        except BaseException as ex:  # a CancelledError out of a wedged connection too
            out.update(bug=True, what=f"the next write failed: {type(ex).__name__} {str(ex)[:120]}")
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
