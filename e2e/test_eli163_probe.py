"""ELI-163 probe (not a test): what the Pipe's own Gateway connection sees of
background work. Records every event frame while a chat spawns a sub-agent,
another starts a background shell command, and an automation bound to a chat
runs; then reads tasks.list, cron.list/runs, sessions.list rows,
sessions.processes.list and health. Writes $E2E_LOG_DIR/eli163-probe.json."""

import asyncio
import json
import os
import sys
import time
import uuid
from collections import Counter, defaultdict

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from test_e2e import _chat_turn, _request, _session_key, token  # noqa: E402,F401

OUT = os.path.join(os.environ.get("E2E_LOG_DIR") or os.environ.get("RUNNER_TEMP", "/tmp"), "eli163-probe.json")


def _trim(v, n=1500):
    s = json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "...(trimmed)"


async def _probe(tok):
    import openclaw_pipe
    status, valves = _request("GET", "/api/v1/functions/id/openclaw_gateway/valves", token=tok)
    assert status == 200, valves
    pipe_valves = openclaw_pipe.Pipe.Valves(**{k: v for k, v in valves.items() if v is not None})
    conn = openclaw_pipe._GatewayConnection(lambda: pipe_valves, writes_turns=False)
    try:
        await conn.ensure_connected()
    except Exception as ex:   # a new device identity on this host: approve it, as install.py does
        import subprocess
        print("connect failed, approving pending devices:", ex)
        devs = json.loads(subprocess.run(["openclaw", "devices", "list", "--json"], capture_output=True,
                                         text=True).stdout or "{}")
        for req in devs.get("pending", []):
            subprocess.run(["openclaw", "devices", "approve", req.get("requestId") or req.get("id")])
        await conn.ensure_connected()
    frames = []
    t0 = time.time()
    real_recv = conn._ws.recv

    async def recv(*a, **kw):
        raw = await real_recv(*a, **kw)
        try:
            msg = json.loads(raw)
        except Exception:
            return raw
        if msg.get("type") == "event" and msg.get("event") not in ("tick", "health", "presence", "node.hostStats"):
            frames.append({"t": round(time.time() - t0, 2), "event": msg.get("event"), "payload": msg.get("payload")})
        return raw
    conn._ws.recv = recv

    out = {"openclaw": os.environ.get("OPENCLAW_VERSION"), "rpc": {}, "notes": []}

    async def rpc(name, method, params):
        try:
            out["rpc"][name] = await conn.send_request(method, params, timeout=30)
        except Exception as ex:
            out["rpc"][name] = {"error": f"{type(ex).__name__}: {ex}"}
        return out["rpc"][name]

    loop = asyncio.get_running_loop()
    # 1. a sub-agent (its own run is slow, about 20 s)
    spawn_chat, _ = await loop.run_in_executor(None, _chat_turn, tok, "E2E_SPAWN for eli163")
    k_spawn = _session_key(tok, spawn_chat)
    await rpc("tasks_after_spawn", "tasks.list", {"sessionKey": k_spawn, "limit": 20})
    # 2. a background shell command (about 25 s)
    bg_chat, bg_msg = await loop.run_in_executor(None, _chat_turn, tok, "E2E_BG_EXEC for eli163")
    k_bg = _session_key(tok, bg_chat)
    out["bg_reply"] = _trim(bg_msg, 3000)
    await rpc("tasks_bg_running", "tasks.list", {"sessionKey": k_bg, "limit": 20})
    await rpc("tasks_running_all", "tasks.list", {"status": ["queued", "running"], "limit": 50})
    await rpc("processes_bg", "sessions.processes.list", {"key": k_bg})
    await rpc("rows_while_running", "sessions.list", {"search": bg_chat, "limit": 5})
    await rpc("health_while_running", "health", {})
    # 3. an automation bound to the spawn chat, due in 5 s
    at = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 5))
    added = await rpc("cron_add", "cron.add", {
        "name": f"eli163-probe-{uuid.uuid4().hex[:6]}", "schedule": {"kind": "at", "at": at},
        "sessionTarget": f"session:{k_spawn}",
        "payload": {"kind": "agentTurn", "message": "E2E_SLOW:5 E2E_CRONPROBE"},
        "delivery": {"mode": "none"}})
    job_id = (added or {}).get("id") or ((added or {}).get("job") or {}).get("id")
    await asyncio.sleep(12)
    await rpc("cron_list_running", "cron.list", {"includeDisabled": True})
    await rpc("tasks_running_all_2", "tasks.list", {"status": ["queued", "running"], "limit": 50})
    await asyncio.sleep(30)
    await rpc("cron_list_after", "cron.list", {"includeDisabled": True})
    if job_id:
        await rpc("cron_runs", "cron.runs", {"id": job_id, "limit": 5})
    await rpc("tasks_bg_after", "tasks.list", {"sessionKey": k_bg, "limit": 20})
    await rpc("tasks_spawn_after", "tasks.list", {"sessionKey": k_spawn, "limit": 20})
    await rpc("tasks_all_after", "tasks.list", {"limit": 50})
    await rpc("processes_bg_after", "sessions.processes.list", {"key": k_bg})
    await rpc("rows_after", "sessions.list", {"limit": 20})
    status, chat = _request("GET", f"/api/v1/chats/{bg_chat}", token=tok)
    out["bg_chat_after"] = _trim(chat.get("chat", {}).get("history", {}) if status == 200 else chat, 6000)

    names = Counter(f["event"] for f in frames)
    by = defaultdict(list)
    for f in frames:
        if len(by[f["event"]]) < 8:
            by[f["event"]].append({"t": f["t"], "payload": _trim(f["payload"], 1200)})
    out["keys"] = {"spawn": k_spawn, "bg": k_bg, "cron_job": job_id}
    out["event_counts"] = dict(names)
    out["event_samples"] = by
    out["task_events"] = [{"t": f["t"], "payload": _trim(f["payload"], 800)} for f in frames if f["event"] == "task"][:60]
    out["cron_events"] = [{"t": f["t"], "payload": _trim(f["payload"], 800)} for f in frames if f["event"] == "cron"][:60]
    for k, v in list(out["rpc"].items()):
        out["rpc"][k] = json.loads(_trim(v, 12000)) if len(json.dumps(v, default=str)) <= 12000 else _trim(v, 12000)
    return out


def test_eli163_probe(token):
    out = asyncio.run(_probe(token))
    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print(json.dumps({"event_counts": out["event_counts"], "keys": out["keys"],
                      "task_events": len(out["task_events"]), "cron_events": len(out["cron_events"])}, indent=1))
