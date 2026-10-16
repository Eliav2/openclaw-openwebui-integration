"""ELI-115: how much of the replay's peak is chat size alone. The same three
chats as pipe_replay.py (30, 16 and 12 MB, the same shape) on the same stack,
then single Open WebUI operations on them, each in a freshly restarted Open
WebUI (no high-water left from the one before): one GET of the 30 MB chat,
one page save of it, one GET of the 16 MB chat, the three chats read at once,
and a save and a read of the 30 MB chat at once. Reports the rise of RssAnon +
VmSwap (sampled every 20 ms) over the value before the operation.
"""
import asyncio
import json
import os
import subprocess
import time

import httpx

import pipe_replay as pr

OUT = os.environ.get("OUT", "chat-size.jsonl")
BASE = pr.BASE


def restart_owui():
    subprocess.run(["docker", "restart", "owui"], check=True, capture_output=True, timeout=180)
    for _ in range(150):
        try:
            if httpx.get(f"{BASE}/health", timeout=5).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(2)
    pr.PID = pr.owui_pid()


async def signin(c):
    r = await c.post(f"{BASE}/api/v1/auths/signin", json={
        "email": os.environ["OWUI_EMAIL"], "password": os.environ["OWUI_PASSWORD"]})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def get(c, h, chat_id):
    r = await c.get(f"{BASE}/api/v1/chats/{chat_id}", headers=h, timeout=600)
    r.raise_for_status()


async def save(c, h, chat_id, chat):
    r = await c.post(f"{BASE}/api/v1/chats/{chat_id}", headers=h, json={"chat": chat}, timeout=600)
    r.raise_for_status()


async def main():
    sync = httpx.Client(timeout=600)
    async with httpx.AsyncClient() as c:
        h = await signin(c)
    chats = [pr.make_chat(sync, h, mb) for mb in pr.SIZES]
    (c30, chat30), (c16, _), (c12, _) = chats
    ops = {
        "get_30": lambda c, h: get(c, h, c30),
        "save_30": lambda c, h: save(c, h, c30, chat30),
        "get_16": lambda c, h: get(c, h, c16),
        "get_30_16_12_at_once": lambda c, h: asyncio.gather(get(c, h, c30), get(c, h, c16), get(c, h, c12)),
        "save_30_and_get_30_at_once": lambda c, h: asyncio.gather(save(c, h, c30, chat30), get(c, h, c30)),
    }
    for name, op in ops.items():
        await asyncio.to_thread(restart_owui)
        async with httpx.AsyncClient() as c:
            h = await signin(c)
            # The process settles after its start (imports, first requests).
            await asyncio.sleep(20)
            anon, swap = pr.mem()
            before = anon + swap
            t0 = time.monotonic()
            with pr.Peak() as p:
                await op(c, h)
            took = time.monotonic() - t0
            await asyncio.sleep(5)
            anon, swap = pr.mem()
        row = {"kind": "chat-size", "op": name, "rep": os.environ.get("REP"), "chats_mb": pr.SIZES,
               "before": before, "peak": p.peak, "rise": p.peak - before, "after": anon + swap,
               "retained": anon + swap - before, "s": round(took, 2)}
        print(json.dumps(row), flush=True)
        with open(OUT, "a") as f:
            f.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
