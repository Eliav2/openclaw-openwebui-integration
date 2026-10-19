"""Runs inside the Open WebUI container: cancel a chat message write at a
random point, again and again, as a Stop does to a reply being saved
(ELI-205, open-webui#32741). Prints one JSON line:
{"ok": bool, "cancels": n, "what": ...}.

usage: owui_cancel_probe.py shielded|raw <cancels> <chat_id> <pipe file>

`shielded` writes through the Pipe's `_owui_upsert_message`, `raw` calls Open
WebUI's upsert directly, as the Pipe would without its shield. It runs on a
copy of the DB, so a lock it causes never reaches the running Open WebUI.
"""
import asyncio
import importlib.util
import json
import os
import random
import sqlite3
import sys

mode, cancels, chat_id, pipe_file = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
DB = "/tmp/eli205-probe.db"
src = sqlite3.connect("/app/backend/data/webui.db")
dst = sqlite3.connect(DB)
src.backup(dst)
src.close()
dst.close()
# Before Open WebUI's modules read it.
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"

from open_webui.models.chats import Chats  # noqa: E402
from open_webui.models.users import Users  # noqa: E402


def writer():
    if mode == "raw":
        return Chats.upsert_message_to_chat_by_id_and_message_id
    spec = importlib.util.spec_from_file_location("openclaw_pipe_probe", pipe_file)
    pipe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipe)
    return pipe._owui_upsert_message


def locked_for_others() -> bool:
    c = sqlite3.connect(DB, timeout=2)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.rollback()
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        c.close()


async def main() -> dict:
    write = writer()
    users = await Users.get_users()
    uid = (users["users"] if isinstance(users, dict) else users)[0].id
    for i in range(cancels):
        task = asyncio.ensure_future(write(chat_id, "eli205-probe", {"role": "assistant", "content": f"x{i}", "done": True}))
        await asyncio.sleep(random.uniform(0, 0.01))
        task.cancel()
        if random.random() < 0.5:
            await asyncio.sleep(0)
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=15)
        if not done:
            return {"ok": False, "cancels": i + 1, "what": "the cancelled write never ended",
                    "locked_for_others": locked_for_others()}
        try:
            await asyncio.wait_for(Users.update_last_active_by_id(uid), 15)
        except BaseException as ex:  # a CancelledError out of a wedged connection too
            return {"ok": False, "cancels": i + 1, "what": f"the next write failed: {type(ex).__name__} {str(ex)[:120]}",
                    "locked_for_others": locked_for_others()}
    return {"ok": True, "cancels": cancels, "what": "DB writable throughout", "locked_for_others": locked_for_others()}


try:
    result = asyncio.run(main())
except BaseException as ex:
    result = {"ok": False, "cancels": None, "what": f"probe died: {type(ex).__name__} {str(ex)[:200]}"}
print(json.dumps(result), flush=True)
os._exit(0)  # a wedged aiosqlite thread would keep the process alive
