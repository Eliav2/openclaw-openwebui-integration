"""ELI-115 replay of 2026-10-16 07:12-07:18Z: turns ending in the three big
chats (30, 16 and 12 MB) at once, as Open WebUI sees them.

Per chat, one agent turn with TOOLS tool calls. Today the Pipe sends two
`status` events per tool call ("Running X...", "X done"), and Open WebUI
saves every status by loading and rewriting the whole chat. Meanwhile the
page saves the whole chat (POST, as Chat.svelte does on send and on done)
every 15 s. MODE=today sends the statuses; MODE=fixed sends only the turn's
first and last status. Reports RssAnon (before, peak, after) and the page
saves' latency.
"""
import concurrent.futures as cf
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
import driver  # noqa: E402

MODE = os.environ.get("MODE", "today")
TOOLS = int(os.environ.get("TOOLS", "40"))
SIZES = [30, 16, 12]


def turn(cid, last, body, save_lat):
    ev = f"{driver.BASE}/api/v1/chats/{cid}/messages/{last}/event"
    H = driver.H()

    def status(desc, done=False):
        driver.C.post(ev, headers=H, json={"type": "status", "data": {
            "description": desc, "done": done}}).raise_for_status()

    stop = threading.Event()

    def page_saves():
        while not stop.is_set():
            t = time.monotonic()
            driver.C.post(f"{driver.BASE}/api/v1/chats/{cid}", headers={
                **H, "Content-Type": "application/json"}, content=body).raise_for_status()
            save_lat.append(round(time.monotonic() - t, 2))
            stop.wait(15)

    saver = threading.Thread(target=page_saves, daemon=True)
    saver.start()
    status("Thinking...")
    for i in range(TOOLS):
        if MODE == "today":
            status("Running Bash...")
        time.sleep(0.5)          # the tool runs
        if MODE == "today":
            status("Bash done", done=True)
    status("", done=True)
    stop.set()
    saver.join()


def main():
    driver.TOKEN = driver.C.post(f"{driver.BASE}/api/v1/auths/signin", json={
        "email": os.environ["OWUI_EMAIL"], "password": os.environ["OWUI_PASSWORD"]}).json()["token"]
    chats = []
    for mb in SIZES:
        cid, last, chat = driver.make_chat(mb)
        chats.append((cid, last, json.dumps({"chat": chat})))
        del chat
    time.sleep(3)
    before = driver.anon()
    save_lat = []
    t0 = time.monotonic()
    with driver.Peak() as p:
        with cf.ThreadPoolExecutor(len(chats)) as ex:
            for f in [ex.submit(turn, cid, last, body, save_lat) for cid, last, body in chats]:
                f.result()
    took = time.monotonic() - t0
    time.sleep(3)
    driver.record({"kind": "replay", "mode": MODE, "tools": TOOLS, "chats_mb": SIZES,
                   "status_writes": len(chats) * (2 * TOOLS + 2 if MODE == "today" else 2),
                   "before": before, "peak": p.peak, "after": driver.anon(), "s": round(took, 1),
                   "page_save_max_s": max(save_lat), "page_save_avg_s": round(sum(save_lat) / len(save_lat), 2),
                   "page_saves": len(save_lat)})


if __name__ == "__main__":
    main()
