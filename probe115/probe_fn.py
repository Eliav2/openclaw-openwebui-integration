"""
title: ELI-115 memory probe
description: In-process measurements for the ELI-115 probe workflow. Not shipped.
"""
import asyncio
import ctypes
import gc
import json
import time
import tracemalloc


def _status():
    out = {}
    with open("/proc/self/status") as f:
        for line in f:
            k, _, v = line.partition(":")
            if k in ("RssAnon", "VmSwap", "Threads", "VmHWM", "RssFile"):
                out[k] = int(v.split()[0]) // 1024 if k != "Threads" else int(v.split()[0])
    import os
    try:
        out["db_fds"] = sum(1 for fd in os.listdir("/proc/self/fd")
                            if os.readlink(f"/proc/self/fd/{fd}").endswith("webui.db"))
    except OSError:
        pass
    if tracemalloc.is_tracing():
        cur, peak = tracemalloc.get_traced_memory()
        out["traced_mb"] = round(cur / 1048576, 1)
        out["traced_peak_mb"] = round(peak / 1048576, 1)
    return out


def _history_json(mb):
    msgs, i = [], 0
    while sum(len(m["content"][0]["text"]) for m in msgs) < mb * 1048576:
        msgs.append({"role": "assistant" if i % 2 else "user", "idempotencyKey": f"k{i}",
                     "content": [{"type": "text", "text": ("x" * 63 + " ") * 60}],
                     "__openclaw": {"seq": i, "id": f"id-{i}"}})
        i += 1
    return json.dumps({"messages": msgs})


class Pipe:
    async def pipe(self, body: dict, __user__: dict = None):
        from open_webui.models.chats import Chats

        cmd = json.loads(body["messages"][-1]["content"])
        op = cmd["op"]
        n, conc = cmd.get("n", 1), cmd.get("conc", 1)
        if op == "trace_start":
            tracemalloc.start(cmd.get("frames", 1))
            return json.dumps(_status())
        if op == "status":
            return json.dumps(_status())
        if op == "trim":
            gc.collect()
            before = _status()
            ctypes.CDLL("libc.so.6").malloc_trim(0)
            return json.dumps({"before": before, "after": _status()})
        if op == "snapshot":
            gc.collect()
            snap = tracemalloc.take_snapshot()
            top = snap.statistics(cmd.get("key", "lineno"))[:cmd.get("top", 15)]
            return json.dumps({"status": _status(),
                               "top": [[str(s.traceback), round(s.size / 1048576, 2), s.count] for s in top]})

        if op == "breakdown":
            # The steps one upsert/get takes on the chat row, each measured
            # alone with tracemalloc (peak while the step's result is held).
            import sqlite3
            from open_webui.models.chats import ChatModel
            if not tracemalloc.is_tracing():
                tracemalloc.start(1)
            raw = sqlite3.connect("/app/backend/data/webui.db").execute(
                "select chat from chat where id=?", (cmd["chat"],)).fetchone()[0]
            steps = {"raw_json_mb": round(len(raw) / 1048576, 1)}

            def step(name, fn):
                tracemalloc.reset_peak()
                base = tracemalloc.get_traced_memory()[0]
                res = fn()
                steps[name] = round((tracemalloc.get_traced_memory()[1] - base) / 1048576, 1)
                return res

            data = step("json.loads", lambda: json.loads(raw))
            shell = await Chats.get_chat_by_id(cmd["chat"])
            row = {**shell.model_dump(exclude={"chat"}), "chat": data}
            del shell
            gc.collect()
            model = step("ChatModel.model_validate", lambda: ChatModel.model_validate(row))
            step("_clean_null_bytes", lambda: Chats._clean_null_bytes(model.chat))
            step("json.dumps", lambda: json.dumps(data))
            step("model_dump", lambda: model.model_dump())
            return json.dumps({"op": op, "steps": steps, "status": _status()})

        async def one(k):
            if op == "upsert":
                await Chats.upsert_message_to_chat_by_id_and_message_id(
                    cmd["chat"], cmd["msg"], {"content": f"probe write {k} " + "y" * 2000})
            elif op == "status_event":
                await Chats.add_message_status_to_chat_by_id_and_message_id(
                    cmd["chat"], cmd["msg"], {"description": f"probe {k}", "done": False})
            elif op == "get":
                await Chats.get_chat_by_id(cmd["chat"])
            elif op == "get_message":
                await Chats.get_message_by_id_and_message_id(cmd["chat"], cmd["msg"])
            elif op == "jsonparse":
                json.loads(text)

        text = _history_json(cmd.get("mb", 5)) if op == "jsonparse" else None
        if tracemalloc.is_tracing():
            tracemalloc.reset_peak()
        before = _status()
        t0 = time.monotonic()
        for start in range(0, n, conc):
            await asyncio.gather(*(one(k) for k in range(start, min(n, start + conc))))
        took = time.monotonic() - t0
        after = _status()
        gc.collect()
        return json.dumps({"op": op, "n": n, "conc": conc, "s": round(took, 2),
                           "before": before, "after": after, "after_gc": _status()})
