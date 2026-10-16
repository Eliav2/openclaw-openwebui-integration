"""ELI-115 probe driver: what Open WebUI 0.12's anonymous memory does under
the requests a big chat gets. Runs on a GitHub runner against OWUI in Docker.

Every scenario reports RssAnon of the uvicorn process before, at its peak
(sampled every 20 ms from the host's /proc), and after.
"""
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import threading
import time
import uuid

import httpx

BASE = os.environ.get("OWUI_URL", "http://localhost:8080")
OUT = os.environ.get("OUT", "probe-results.jsonl")
TRACE = os.environ.get("TRACE") == "1"
PID = int(subprocess.check_output(["docker", "inspect", "-f", "{{.State.Pid}}", "owui"]).strip())
C = httpx.Client(timeout=600)
TOKEN = None


def anon():
    with open(f"/proc/{PID}/status") as f:
        for line in f:
            if line.startswith("RssAnon:"):
                return int(line.split()[1]) // 1024


class Peak:
    def __enter__(self):
        self.peak, self.stop = anon(), False
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()
        return self

    def run(self):
        while not self.stop:
            self.peak = max(self.peak, anon())
            time.sleep(0.02)

    def __exit__(self, *a):
        self.stop = True
        self.t.join()


def H():
    return {"Authorization": f"Bearer {TOKEN}"}


def record(row):
    print(json.dumps(row), flush=True)
    with open(OUT, "a") as f:
        f.write(json.dumps(row) + "\n")


def probe(cmd):
    r = C.post(f"{BASE}/api/chat/completions", headers=H(), json={
        "model": "probe", "stream": False,
        "messages": [{"role": "user", "content": json.dumps(cmd)}]})
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def make_chat(target_mb):
    """A chat shaped like the live manager/worker chats (2026-10-16, 0abe9bde:
    334 messages, history 18 MB of which 16 MB tool-card `output`, plus the
    linear `messages` copy)."""
    n = 334
    per_out = int(target_mb * 1048576 / 2 / (n // 2))
    msgs, linear, parent = {}, [], None
    for i in range(n):
        mid = str(uuid.uuid4())
        m = {"id": mid, "parentId": parent, "childrenIds": [], "timestamp": 1792000000 + i,
             "role": "assistant" if i % 2 else "user", "content": "q" * 300}
        if i % 2:
            m.update(model="probe", done=True, content="a" * 3600)
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
        if parent:
            msgs[parent]["childrenIds"].append(mid)
        msgs[mid] = m
        linear.append(m)
        parent = mid
    chat = {"title": f"probe {target_mb} MB", "models": ["probe"],
            "history": {"messages": msgs, "currentId": parent}, "messages": linear}
    r = C.post(f"{BASE}/api/v1/chats/new", headers=H(), json={"chat": chat})
    r.raise_for_status()
    return r.json()["id"], parent, chat


def http_scenario(name, fn, n, conc):
    before = anon()
    t0 = time.monotonic()
    lat = []
    with Peak() as p:
        with cf.ThreadPoolExecutor(conc) as ex:
            for start in range(0, n, conc):
                futs = [ex.submit(timed, fn) for _ in range(start, min(n, start + conc))]
                lat += [f.result() for f in futs]
    time.sleep(2)
    record({"kind": "http", "name": name, "n": n, "conc": conc, "before": before, "peak": p.peak,
            "after": anon(), "s": round(time.monotonic() - t0, 1),
            "lat_max": round(max(lat), 2), "lat_avg": round(sum(lat) / len(lat), 2)})


def timed(fn):
    t = time.monotonic()
    fn()
    return time.monotonic() - t


def inproc(name, cmd):
    with Peak() as p:
        res = probe(cmd)
    res.update(kind="inproc", name=name, peak=p.peak)
    record(res)


def main():
    global TOKEN
    TOKEN = C.post(f"{BASE}/api/v1/auths/signin", json={
        "email": os.environ["OWUI_EMAIL"], "password": os.environ["OWUI_PASSWORD"]}).json()["token"]
    code = open(os.path.join(os.path.dirname(__file__), "probe_fn.py")).read()
    C.post(f"{BASE}/api/v1/functions/create", headers=H(), json={
        "id": "probe", "name": "probe", "content": code,
        "meta": {"description": "ELI-115 probe"}}).raise_for_status()
    C.post(f"{BASE}/api/v1/functions/id/probe/toggle", headers=H()).raise_for_status()
    if TRACE:
        record({"kind": "trace", **probe({"op": "trace_start", "frames": 6})})
    record({"kind": "start", "anon": anon()})

    for mb in [int(x) for x in os.environ.get("SIZES", "1 30").split()]:
        before = anon()
        with Peak() as p:
            cid, last, chat = make_chat(mb)
        body = json.dumps({"chat": chat})
        record({"kind": "seed", "mb": mb, "json_mb": round(len(body) / 1048576, 1),
                "before": before, "peak": p.peak, "after": anon()})
        get = lambda: C.get(f"{BASE}/api/v1/chats/{cid}", headers=H()).raise_for_status()
        save = lambda: C.post(f"{BASE}/api/v1/chats/{cid}", headers={**H(), "Content-Type": "application/json"},
                              content=body).raise_for_status()
        ev = f"{BASE}/api/v1/chats/{cid}/messages/{last}/event"
        status = lambda: C.post(ev, headers=H(), json={"type": "status", "data": {
            "description": "Thinking...", "done": False}}).raise_for_status()
        replace = lambda: C.post(ev, headers=H(), json={"type": "replace", "data": {
            "content": "r" * 4000}}).raise_for_status()
        listing = lambda: C.get(f"{BASE}/api/v1/chats/?page=1", headers=H()).raise_for_status()
        tag = f"{mb}MB"
        http_scenario(f"{tag} list x20", listing, 20, 4)
        http_scenario(f"{tag} page load (GET chat) seq", get, 10, 1)
        http_scenario(f"{tag} page load (GET chat) conc4", get, 8, 4)
        http_scenario(f"{tag} page save (POST whole chat) seq", save, 5, 1)
        http_scenario(f"{tag} page save (POST whole chat) conc3", save, 6, 3)
        http_scenario(f"{tag} status event seq", status, 20, 1)
        http_scenario(f"{tag} replace event conc4", replace, 12, 4)
        inproc(f"{tag} upsert seq", {"op": "upsert", "chat": cid, "msg": last, "n": 10})
        inproc(f"{tag} upsert conc4", {"op": "upsert", "chat": cid, "msg": last, "n": 8, "conc": 4})
        inproc(f"{tag} get_message", {"op": "get_message", "chat": cid, "msg": last, "n": 10})
        inproc(f"{tag} Chats.get_chat_by_id", {"op": "get", "chat": cid, "n": 10})
        record({"kind": "breakdown", "mb": mb, **probe({"op": "breakdown", "chat": cid})})
    inproc("chat.history-sized json.loads 5MB x10", {"op": "jsonparse", "mb": 5, "n": 10})
    if TRACE:
        record({"kind": "snapshot", **probe({"op": "snapshot", "top": 20})})
    record({"kind": "trim", **probe({"op": "trim"})})
    record({"kind": "end", "anon": anon()})


if __name__ == "__main__":
    sys.exit(main())
