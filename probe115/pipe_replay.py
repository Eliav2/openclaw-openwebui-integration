"""ELI-115 fixes 1-3, measured through the real Pipe on the E2E stack (Open
WebUI 0.12 in Docker, one CPU; a real OpenClaw Gateway; the fake model; the
Pipe as installed from the ref under test). The same harness for every ref.

Today's traffic in the three big chats (30, 16 and 12 MB), all at once:
  * an inline user turn with TOOLS tool calls (fix 1: the Pipe's per-tool
    status events, each a whole-chat write),
  * then a cron wake with TOOLS tool calls into the same chat, written by the
    turn writer (fix 2: its DB writes),
  * a tab that reconnects and re-joins the chat every REJOIN_S seconds (fix 3:
    the composer watch's read per join), and one that stays, as the page does,
  * the page's whole-chat save every 15 s.
Reports Open WebUI's RssAnon + VmSwap (sampled every 20 ms) before, at the
peak and after, the wall time, and what the consumer got: each turn's
persisted cards and `done`, its stored status count, and every composer push
the reconnecting tab acked, with the message it targets.
"""
import asyncio
import datetime
import json
import os
import subprocess
import threading
import time
import uuid

import httpx
import socketio

BASE = os.environ.get("OWUI_URL", "http://localhost:8080")
OUT = os.environ.get("OUT", "pipe-replay.jsonl")
MODEL = "openclaw_gateway.default"
SIZES = [30, 16, 12]
TOOLS = int(os.environ.get("TOOLS", "40"))
REJOIN_S = float(os.environ.get("REJOIN_S", "5"))
# JOINS_ONLY: no turns and no page saves, only the reconnecting tabs for
# JOINS_S seconds (fix 3 alone).
JOINS_ONLY = os.environ.get("JOINS_ONLY") == "1"
JOINS_S = 60
SCENE = "ELI115_REPLAY_TOOLS"      # replay-scene.json
DONE = "ELI115_REPLAY_DONE"
TURN_TIMEOUT_S = 900
PAGE_SAVES = os.environ.get("PAGE_SAVES", "1") == "1"


def owui_pid():
    """The Open WebUI server process: the container's biggest."""
    rows = subprocess.check_output(["docker", "top", "owui", "-eo", "pid,rss"], text=True).split("\n")[1:]
    return max((r.split() for r in rows if r.strip()), key=lambda r: int(r[1]))[0]


PID = owui_pid()


def mem():
    """(RssAnon, VmSwap) in MB."""
    got = {}
    with open(f"/proc/{PID}/status") as f:
        for line in f:
            k = line.split(":")[0]
            if k in ("RssAnon", "VmSwap"):
                got[k] = int(line.split()[1]) // 1024
    return got.get("RssAnon", 0), got.get("VmSwap", 0)


def cpu():
    """CPU seconds the Open WebUI process has used (utime + stime)."""
    with open(f"/proc/{PID}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


class Peak:
    def __enter__(self):
        a, s = mem()
        self.peak, self.anon_peak, self.stop = a + s, a, False
        self.t0, self.peak_t, self.per_s = time.monotonic(), 0.0, {}
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()
        return self

    def run(self):
        while not self.stop:
            a, s = mem()
            t = time.monotonic() - self.t0
            if a + s > self.peak:
                self.peak_t = t
            self.peak, self.anon_peak = max(self.peak, a + s), max(self.anon_peak, a)
            sec = int(t)
            self.per_s[sec] = max(self.per_s.get(sec, 0), a + s)
            time.sleep(0.02)

    def __exit__(self, *a):
        self.stop = True
        self.t.join()


def make_chat(c, h, target_mb):
    """A chat shaped like the live manager/worker chats (step 0's make_chat),
    on the Pipe's model."""
    n = 334
    per_out = int(target_mb * 1048576 / 2 / (n // 2))
    msgs, linear, parent = {}, [], None
    for i in range(n):
        mid = str(uuid.uuid4())
        m = {"id": mid, "parentId": parent, "childrenIds": [], "timestamp": 1792000000 + i,
             "role": "assistant" if i % 2 else "user", "content": "q" * 300}
        if i % 2:
            m.update(model=MODEL, modelName="E2E Default", done=True, content="a" * 3600)
            out, cid = [], 0
            while sum(len(json.dumps(o)) for o in out) < per_out:
                call = f"toolu_{i}_{cid}"
                out += [{"type": "function_call", "call_id": call, "name": "Bash",
                         "arguments": json.dumps({"command": "z" * 1500})},
                        {"type": "function_call_output", "call_id": call,
                         "output": [{"type": "input_text", "text": "o" * 30000}]}]
                cid += 1
            out.append({"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": m["content"]}]})
            m["output"] = out
        else:
            m["models"] = [MODEL]
        if parent:
            msgs[parent]["childrenIds"].append(mid)
        msgs[mid] = m
        linear.append(m)
        parent = mid
    chat = {"title": f"replay {target_mb} MB", "models": [MODEL],
            "history": {"messages": msgs, "currentId": parent}, "messages": linear}
    r = c.post(f"{BASE}/api/v1/chats/new", headers=h, json={"chat": chat}, timeout=600)
    r.raise_for_status()
    return r.json()["id"], chat


def schedule_wake(key, message, in_s):
    at = (datetime.datetime.now(datetime.timezone.utc)
          + datetime.timedelta(seconds=in_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    params = {"name": f"replay-{uuid.uuid4().hex[:6]}", "schedule": {"kind": "at", "at": at},
              "sessionTarget": f"session:{key}", "payload": {"kind": "agentTurn", "message": message},
              "delivery": {"mode": "none"}, "deleteAfterRun": True}
    subprocess.run(["openclaw", "gateway", "call", "cron.add", "--json", "--params", json.dumps(params)],
                   check=True, capture_output=True, text=True, timeout=90)


class Tab:
    """A page's socket: in the user room (every turn's events) and joined to
    one chat; acks every `execute`, as Chat.svelte does once it has run it."""

    def __init__(self, token, chat_id):
        self.token, self.chat_id, self.sid = token, chat_id, None
        self.sio = socketio.AsyncClient(reconnection=False)
        self.done: dict = {}         # message_id -> time its chat:completion done arrived
        self.pushes: list = []       # composer watch pushes to this socket: message_id
        self.changed = asyncio.Event()
        self.sio.on("events", self.on_event)

    async def on_event(self, data):
        inner = (data or {}).get("data") or {}
        if (inner.get("type") == "chat:completion" and (inner.get("data") or {}).get("done") is True
                and data.get("chat_id") == self.chat_id):
            self.done.setdefault(data.get("message_id"), time.monotonic())
            self.changed.set()
        if inner.get("type") == "execute":
            code = (inner.get("data") or {}).get("code") or ""
            if f'"sessionId": "{self.sid}"' in code and data.get("chat_id") == self.chat_id:
                self.pushes.append(data.get("message_id"))
                self.changed.set()
            return True

    async def open(self):
        await self.sio.connect(BASE, socketio_path="/ws/socket.io", transports=["websocket"],
                               auth={"token": self.token})
        self.sid = self.sio.get_sid()
        await self.sio.emit("events:chat", {"chat_id": self.chat_id, "data": {"type": "join"}})

    async def close(self):
        await self.sio.disconnect()

    async def until(self, cond, timeout):
        deadline = time.monotonic() + timeout
        while not cond():
            self.changed.clear()
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            try:
                await asyncio.wait_for(self.changed.wait(), left)
            except asyncio.TimeoutError:
                raise TimeoutError from None


async def rejoining_tab(token, chat_id, stop, joins):
    """A tab that reconnects every REJOIN_S s: each connect is a new socket
    id, so the composer watch reads the chat and pushes once more."""
    while not stop.is_set():
        tab = Tab(token, chat_id)
        t0 = time.monotonic()
        row = {"chat": chat_id, "pushed": None}
        try:
            await tab.open()
            await tab.until(lambda: tab.pushes, 8)
            row.update(pushed=tab.pushes[0], s=round(time.monotonic() - t0, 2))
        except (TimeoutError, Exception) as ex:
            row["error"] = type(ex).__name__
        finally:
            joins.append(row)
            try:
                await tab.close()
            except Exception:
                pass
        try:
            await asyncio.wait_for(stop.wait(), REJOIN_S)
        except asyncio.TimeoutError:
            pass


async def page_saves(c, h, chat_id, chat, stop, lat):
    while not stop.is_set():
        t = time.monotonic()
        r = await c.post(f"{BASE}/api/v1/chats/{chat_id}", headers=h, json={"chat": chat}, timeout=600)
        r.raise_for_status()
        lat.append(round(time.monotonic() - t, 2))
        try:
            await asyncio.wait_for(stop.wait(), 15)
        except asyncio.TimeoutError:
            pass


MARKS: dict = {}   # name -> [monotonic times], one per chat


def mark(name):
    MARKS.setdefault(name, []).append(time.monotonic())


async def chat_turns(c, h, user_id, chat_id, chat, tab):
    """The inline turn, then the wake; returns their message ids."""
    now = int(time.time())
    uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
    parent = chat["history"]["currentId"]
    prompt = f"{SCENE} inline"
    user = {"id": uid, "parentId": parent, "childrenIds": [aid], "role": "user",
            "content": prompt, "timestamp": now, "models": [MODEL]}
    stub = {"id": aid, "parentId": uid, "childrenIds": [], "role": "assistant",
            "content": "", "model": MODEL, "timestamp": now, "done": False}
    chat["history"]["messages"][parent]["childrenIds"].append(uid)
    chat["history"]["messages"].update({uid: user, aid: stub})
    chat["history"]["currentId"] = aid
    chat["messages"] = chat["messages"] + [user, stub]
    (await c.post(f"{BASE}/api/v1/chats/{chat_id}", headers=h, json={"chat": chat}, timeout=600)).raise_for_status()
    mark("inserted")
    (await c.post(f"{BASE}/api/chat/completions", headers=h, timeout=120, json={
        "model": MODEL, "messages": [{"role": "user", "content": prompt}], "stream": True,
        "chat_id": chat_id, "id": aid, "session_id": tab.sid, "parent_id": uid,
        "user_message": user})).raise_for_status()
    t0 = time.monotonic()
    await tab.until(lambda: aid in tab.done, TURN_TIMEOUT_S)
    inline_s = time.monotonic() - t0
    mark("inline_done")
    key = f"agent:main:openwebui-{user_id}-{chat_id}"
    seen = set(tab.done)
    await asyncio.to_thread(schedule_wake, key, f"{SCENE} wake", 2)
    t1 = time.monotonic()
    await tab.until(lambda: set(tab.done) - seen, TURN_TIMEOUT_S)
    wake = next(iter(set(tab.done) - seen))
    mark("wake_done")
    return {"chat": chat_id, "user_message": uid, "inline": aid, "wake": wake,
            "inline_s": round(inline_s, 1), "wake_s": round(time.monotonic() - t1, 1)}


STORED = r"""
import json, sqlite3, sys
db = sqlite3.connect("file:/app/backend/data/webui.db?mode=ro", uri=True)
out = {}
for chat_id, mid in json.loads(sys.argv[1]):
    row = db.execute("select done, content, output from chat_message where id = ?",
                     (f"{chat_id}-{mid}",)).fetchone()
    # Open WebUI keeps a message's statuses in the chat JSON only.
    statuses = db.execute("select json_array_length(json_extract(chat, ?)) from chat where id = ?",
                          (f'$.history.messages."{mid}".statusHistory', chat_id)).fetchone()
    if row is None:
        out[mid] = None
        continue
    done, content, output = row
    items = json.loads(output or "null") or []
    text = json.loads(content) if content and content.startswith('"') else (content or "")
    out[mid] = {"done": bool(done), "calls": sum(i.get("type") == "function_call" for i in items),
                "results": sum(i.get("type") == "function_call_output" for i in items),
                "ends_done": str(text).strip().endswith(sys.argv[2]),
                "statuses": (statuses or [None])[0]}
print(json.dumps(out))
"""


def stored(ids):
    """What Open WebUI persisted for these (chat id, message id) pairs
    (read-only, in the container, not through Open WebUI)."""
    out = subprocess.check_output(["docker", "exec", "owui", "python3", "-c", STORED,
                                   json.dumps(ids), DONE], text=True)
    return json.loads(out)


async def main():
    c = httpx.AsyncClient()
    token = (await c.post(f"{BASE}/api/v1/auths/signin", json={
        "email": os.environ["OWUI_EMAIL"], "password": os.environ["OWUI_PASSWORD"]})).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    user_id = (await c.get(f"{BASE}/api/v1/auths/", headers=h)).json()["id"]
    sync = httpx.Client(timeout=600)
    chats = [await asyncio.to_thread(make_chat, sync, h, mb) for mb in SIZES]
    tabs = []
    for chat_id, _chat in chats:
        tab = Tab(token, chat_id)
        await tab.open()
        tabs.append(tab)
    await asyncio.sleep(5)
    anon, swap = mem()
    before = anon + swap
    stop = asyncio.Event()
    joins, save_lat = [], []
    t0 = time.monotonic()
    cpu0 = cpu()
    if JOINS_ONLY:
        with Peak() as p:
            background = [asyncio.ensure_future(rejoining_tab(token, cid, stop, joins)) for cid, _ in chats]
            await asyncio.sleep(JOINS_S)
            stop.set()
            await asyncio.gather(*background, return_exceptions=True)
        await asyncio.sleep(5)
        anon, swap = mem()
        pushes = [j for j in joins if j.get("pushed")]
        row = {"kind": "joins-only", "variant": os.environ.get("VARIANT"), "ref": os.environ.get("REF"),
               "rep": os.environ.get("REP"), "rejoin_s": REJOIN_S, "chats_mb": SIZES,
               "before": before, "peak": p.peak, "anon_peak": p.anon_peak, "after": anon + swap,
               "cpu_s": round(cpu() - cpu0, 1), "joins": len(joins), "pushes": len(pushes),
               "push_s_avg": round(sum(j["s"] for j in pushes) / max(1, len(pushes)), 2),
               "push_last_user": all(j["pushed"] == ch["history"]["messages"][ch["history"]["currentId"]]["parentId"]
                                     for j in pushes for cid, ch in chats if cid == j["chat"])}
        print(json.dumps(row), flush=True)
        with open(OUT, "a") as f:
            f.write(json.dumps(row) + "\n")
        if not pushes or not row["push_last_user"]:
            raise SystemExit("consumer check failed: the tabs got no right push")
        return
    with Peak() as p:
        background = [asyncio.ensure_future(rejoining_tab(token, cid, stop, joins)) for cid, _ in chats]
        if PAGE_SAVES:
            background += [asyncio.ensure_future(page_saves(c, h, cid, chat, stop, save_lat))
                           for cid, chat in chats]
        turns = await asyncio.gather(*(chat_turns(c, h, user_id, cid, chat, tab)
                                       for (cid, chat), tab in zip(chats, tabs)))
        took = time.monotonic() - t0
        stop.set()
        await asyncio.gather(*background, return_exceptions=True)
    await asyncio.sleep(5)
    anon, swap = mem()
    print(json.dumps({"before": before, "peak": p.peak, "anon_peak": p.anon_peak,
                      "after": anon + swap, "s": round(took, 1), "turns": turns}), flush=True)
    db = stored([(t["chat"], t[k]) for t in turns for k in ("inline", "wake")])
    latest_user = {t["chat"]: t["user_message"] for t in turns}
    user_ids = {cid: {k for k, m in ch["history"]["messages"].items() if m["role"] == "user"}
                for cid, ch in chats}
    pushes = [j for j in joins if j.get("pushed")]
    row = {"kind": "pipe-replay", "variant": os.environ.get("VARIANT"), "ref": os.environ.get("REF"),
           "rep": os.environ.get("REP"), "page_saves_on": PAGE_SAVES, "tools": TOOLS, "chats_mb": SIZES,
           "before": before, "peak": p.peak, "anon_peak": p.anon_peak, "after": anon + swap,
           "swap_after": swap, "s": round(took, 1), "cpu_s": round(cpu() - cpu0, 1),
           "page_saves": len(save_lat), "page_save_avg_s": round(sum(save_lat) / max(1, len(save_lat)), 2),
           "page_save_max_s": max(save_lat or [0]),
           "joins": len(joins), "pushes": len(pushes),
           # Where the peak falls: seconds from the start, the per-second
           # maxima (MB), and when each chat reached each step.
           "peak_t": round(p.peak_t, 1),
           "per_s": [p.per_s.get(i, 0) for i in range(max(p.per_s, default=0) + 1)],
           "marks": {k: [round(x - p.t0, 1) for x in v] for k, v in MARKS.items()},
           # A push targets a user message of its chat: the seeded last one
           # until the inline turn's lands, that one after.
           "push_targets_user": all(j["pushed"] in user_ids[j["chat"]] for j in pushes),
           "push_latest_user": sum(j["pushed"] == latest_user[j["chat"]] for j in pushes),
           "turns": [{**t, "inline_db": db[t["inline"]], "wake_db": db[t["wake"]]}
                     for t in turns]}
    print(json.dumps(row), flush=True)
    with open(OUT, "a") as f:
        f.write(json.dumps(row) + "\n")
    bad = [t for t in row["turns"] for k in ("inline_db", "wake_db")
           if not (t[k] and t[k]["done"] and t[k]["calls"] == TOOLS and t[k]["results"] == TOOLS and t[k]["ends_done"])]
    if bad or not pushes or not row["push_targets_user"]:
        raise SystemExit(f"consumer check failed: {json.dumps(bad)[:2000]} pushes={len(pushes)}")


if __name__ == "__main__":
    asyncio.run(main())
