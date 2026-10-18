"""ELI-205 probe: cancel the Pipe's own `_set_turn_meta` at random moments and
check that Open WebUI's SQLite stays writable.

Run it inside an Open WebUI container on a COPY of a database (never a live
one), with a built Pipe artifact:

    docker run --rm --network none -e WEBUI_SECRET_KEY=probe \
      -v $PWD/copy.db:/app/backend/data/webui.db -v $PWD:/probe:ro -w /app/backend \
      --entrypoint python3 ghcr.io/open-webui/open-webui:v0.12.3 \
      /probe/scripts/probe_cancelled_owui_write.py /probe/openclaw_pipe.py 300

A Pipe whose write a cancel cuts short wedges it and leaves the database
locked within a few dozen tries ("next write FAILED ... db LOCKED").
"""
import asyncio, importlib.util, random, sqlite3, sys
sys.path.insert(0, "/app/backend")
spec = importlib.util.spec_from_file_location("pipe_under_test", sys.argv[1])
pipe = importlib.util.module_from_spec(spec); spec.loader.exec_module(pipe)

def db_state():
    c = sqlite3.connect("/app/backend/data/webui.db", timeout=2)
    try:
        c.execute("BEGIN IMMEDIATE"); c.rollback(); return "free"
    except Exception as e:
        return f"LOCKED ({e})"

async def main(n):
    row = sqlite3.connect("/app/backend/data/webui.db").execute(
        "select id, current_message_id from chat where current_message_id is not null limit 1").fetchone()
    chat_id, msg_id = row
    for i in range(n):
        t = asyncio.ensure_future(pipe._set_turn_meta(chat_id, msg_id, {"probe": i}))
        await asyncio.sleep(random.uniform(0, 0.01))
        t.cancel()
        done, _ = await asyncio.wait({t}, timeout=15)
        if not done:
            print(f"iteration {i}: the cancelled write never ended; db {db_state()}", flush=True); return
        try:
            await asyncio.wait_for(pipe._set_turn_meta(chat_id, msg_id, {"after": i}), 15)
        except BaseException as ex:
            print(f"iteration {i}: next write FAILED: {type(ex).__name__}; db {db_state()}", flush=True); return
    print(f"{n} cancels, DB writable throughout; db {db_state()}")

asyncio.run(main(int(sys.argv[2])))
