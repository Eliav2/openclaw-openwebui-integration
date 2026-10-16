"""ELI-115 socket storm on Open WebUI 0.12: the page's socket client, as the
0.12 bundle builds it (auth fixed at page load, `connect()` again at once on
"io server disconnect"), after the stored token has stopped verifying.

Cases, 20 s each:
  dead    - handshake token dead, reconnect with the same auth (the bundle)
  refresh - handshake token dead, auth refreshed before every reconnect
            (what an `auth` callback would do)
  valid   - a good token from the start
For each: connects, server disconnects, and RssAnon of Open WebUI.
"""
import asyncio
import json
import os
import subprocess
import time

import httpx
import socketio

BASE = os.environ.get("OWUI_URL", "http://localhost:8080")
OUT = os.environ.get("OUT", "storm.jsonl")
PID = int(subprocess.check_output(["docker", "inspect", "-f", "{{.State.Pid}}", "owui"]).strip())


def anon():
    with open(f"/proc/{PID}/status") as f:
        for line in f:
            if line.startswith("RssAnon:"):
                return int(line.split()[1]) // 1024


async def run(case, good, dead, chat_id, seconds=20):
    stats = {"connects": 0, "server_disconnects": 0, "other_disconnects": 0}
    auth = {"token": good if case == "valid" else dead}
    sio = socketio.AsyncClient(reconnection=True, reconnection_delay=1, reconnection_delay_max=5)
    done = asyncio.Event()

    @sio.event
    async def connect():
        stats["connects"] += 1
        # Chat.svelte handleSocketConnect: join the chat's events at once.
        await sio.emit("events:chat", {"chat_id": chat_id, "data": {"type": "typing", "data": {"typing": False}}})

    @sio.event
    async def disconnect(reason=None):
        if reason == sio.reason.SERVER_DISCONNECT:
            stats["server_disconnects"] += 1
            if done.is_set():
                return
            # The bundle: `C==="io server disconnect"&&n.connect()`, same auth.
            if case == "refresh":
                auth["token"] = good
            asyncio.get_running_loop().create_task(reconnect())
        else:
            stats["other_disconnects"] += 1

    async def reconnect():
        try:
            await sio.connect(BASE, socketio_path="/ws/socket.io", transports=["websocket"], auth=auth)
        except Exception:
            pass

    before = anon()
    t0 = time.monotonic()
    await reconnect()
    await asyncio.sleep(seconds)
    done.set()
    await sio.disconnect()
    row = {"case": case, **stats, "per_min": round(stats["server_disconnects"] * 60 / (time.monotonic() - t0)),
           "anon_before": before, "anon_after": anon()}
    print(json.dumps(row), flush=True)
    with open(OUT, "a") as f:
        f.write(json.dumps(row) + "\n")


async def main():
    good = httpx.post(f"{BASE}/api/v1/auths/signin", json={
        "email": os.environ["OWUI_EMAIL"], "password": os.environ["OWUI_PASSWORD"]}).json()["token"]
    # A token signed with another secret: what the page holds after Open WebUI
    # restarted with a new WEBUI_SECRET_KEY and the user signed in again.
    head, body, _sig = good.split(".")
    dead = f"{head}.{body}.invalidsignatureAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    chat = httpx.post(f"{BASE}/api/v1/chats/new", headers={"Authorization": f"Bearer {good}"},
                      json={"chat": {"title": "storm", "history": {"messages": {}}}}).json()["id"]
    for case in ("dead", "refresh", "valid"):
        await run(case, good, dead, chat)


if __name__ == "__main__":
    asyncio.run(main())
