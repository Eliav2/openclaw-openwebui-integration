#!/usr/bin/env python3
"""Deterministic OpenAI-compatible model server for the end-to-end suite.

The E2E job runs the real stack (Open WebUI -> this integration -> OpenClaw
Gateway -> agent) and only swaps the language model for this script, so every
assertion can be exact instead of "the model said something". The Gateway is
pointed at it as a custom `openai-completions` provider.

Behaviour, driven by markers in the latest user message (the agent wraps the
text Open WebUI sent in its own envelope, so markers are searched, not matched
whole):

* `exactly: TOKEN` / `exactly TOKEN`  -> replies with TOKEN alone. Covers the
  installer's smoke test ("reply with exactly OK_CONFIG_TEST").
* `Create a concise title`            -> `TITLE` below: the Pipe's own chat-title
  run (the `title-gen` agent), after a chat's second exchange.
* `E2E_ECHO: any text`                 -> replies with the rest of that line,
  byte for byte (Hebrew, emoji, markup), so a round trip can be exact.
* `E2E_TOOL_CALL`                      -> first answers with a call to the
  `session_status` tool (any harmless tool the agent offers, if that one is
  missing), then, once the tool result is in the transcript, replies
  `E2E_TOOL_DONE`. Exercises tool cards end to end.
* `session_status` + `model line`      -> calls `session_status`, then replies
  with the `Model:` line of its result. Covers the installer's route smoke
  test, which checks that the reply names the model the Gateway actually
  routed to, so the answer has to come from the tool and not from here.

A tool the agent does not offer directly is called through `tool_call`
(OpenClaw 2026.10.1 to 10.4) or `dispatch_action` (from 2026.10.10) when that
is offered: from OpenClaw 2026.10 most tools are deferred behind `tool_search`
and only a core set is sent as real schemas.
* `E2E_BG_EXEC`                        -> starts `sleep 25` with `exec` in the
  background, then replies `E2E_BG_STARTED`.
* `E2E_SLOW TOKEN`                     -> replies `TOKEN slow-1 ... slow-10`, one
  word a second, so a turn can be queued behind a run that is still going.
  `E2E_SLOW:<n> TOKEN` sends n words instead of 10 (a run that outlasts an
  Open WebUI stop).
* `E2E_DEPLOY_SCENE`                   -> a long reply with two tool calls:
  `E2E_DEPLOY_START` and a call, then `E2E_DEPLOY_MID mid-1 ... mid-20` one
  word a second and a second call, then `E2E_DEPLOY_END end-1 ... end-10` one
  word a second. Long enough for the Pipe to be redeployed while it streams.
* `E2E_ASK_SINGLE` / `E2E_ASK_MULTI`   -> asks a question: `ASK_SINGLE` or
  `ASK_MULTI` below. The next message answers it: the reply is
  `E2E_ANSWERED <that message> E2E_ANSWER_END`, so a test sees what reached
  the agent once the reply has ended.
* a scene from $FAKE_LLM_SCENES (JSON)  -> scripted multi-step replies for the
  README screenshots (scripts/screenshots/scenes.json). A scene is picked by a
  phrase in the user's own message, so the request in a screenshot reads like
  a real one. Step n of a scene answers the request that carries n tool
  results; a step can say something and call a tool in the same message.
* anything else                        -> `E2E fake model reply.` (or
  $FAKE_LLM_DEFAULT)

Replies stream word by word so the streaming path sees several deltas.

Every request is appended to $FAKE_LLM_LOG (JSON lines) for the CI artifact.
Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = os.environ.get("FAKE_LLM_MODEL_ID", "e2e-echo")
LOG_PATH = os.environ.get("FAKE_LLM_LOG", "")
DEFAULT_REPLY = os.environ.get("FAKE_LLM_DEFAULT", "E2E fake model reply.")
PREFERRED_TOOLS = ("session_status", "sessions_list", "memory_search")
# The tool that calls a deferred one by id or name: `tool_call` on 2026.10.1 to
# 10.4, `dispatch_action` from 2026.10.10.
DEFERRED_CALL_TOOLS = ("dispatch_action", "tool_call")

_EXACTLY = re.compile(r"exactly:?\s+([A-Za-z0-9_]{3,})")
_ECHO = re.compile(r"E2E_ECHO: ([^\n]+)")
_SLOW = re.compile(r"E2E_SLOW(?::(\d{1,3}))?\s+([A-Za-z0-9_]{3,})")
SLOW_WORDS = 10
SPAWN_TASK = "E2E_SLOW:20 E2E_CHILDTASK"   # the sub-agent's task for E2E_SPAWN
TITLE = "🧪 E2E chat title"
DEPLOY_MID = "E2E_DEPLOY_MID " + " ".join(f"mid-{i}" for i in range(1, 21))
DEPLOY_END = "E2E_DEPLOY_END " + " ".join(f"end-{i}" for i in range(1, 11))
ANSWER_END = "E2E_ANSWER_END"   # the answer's echo is complete
ASK_SINGLE = "OpenClaw needs input:\n\nColor\nWhich color?\n1. Red\n2. Green\n3. Blue"
ASK_MULTI = ("OpenClaw needs input:\n\nToppings (multiselect)\nWhich toppings?\n"
             "1. Cheese\n2. Olives\n3. Mushrooms")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "input_text")
        )
    return ""


def _call(name: str, offered: list) -> dict | None:
    """Plan a call to `name`, directly or through the deferred `tool_call`."""
    if name in offered:
        return {"tool": name}
    for wrapper in DEFERRED_CALL_TOOLS:
        if wrapper in offered:
            return {"tool": wrapper, "args": {"id": name, "args": {}}}
    return None


def _harmless_call(offered: list) -> dict | None:
    for name in PREFERRED_TOOLS:
        if name in offered:
            return {"tool": name}
    return _call(PREFERRED_TOOLS[0], offered)


def _load_scenes() -> list:
    path = os.environ.get("FAKE_LLM_SCENES", "")
    if not path:
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["scenes"]


SCENES = _load_scenes()


def _scene_step(user_text: str, n_results: int, offered: list) -> dict | None:
    """The scripted reply for this request, if the user's message names a scene.
    Returns {"text": str, "tool"?: name, "args"?: dict, "delay"?: s}."""
    for scene in SCENES:
        if scene["match"] not in user_text:
            continue
        steps = scene["steps"]
        step = steps[min(n_results, len(steps) - 1)]
        out = {"text": step.get("say", ""), "delay": step.get("delay", 0.02)}
        if "tool" in step and n_results < len(steps) - 1:
            call = _call(step["tool"], offered)
            if call:
                if step["tool"] in offered:
                    call["args"] = step.get("args") or {}
                else:
                    call["args"]["args"] = step.get("args") or {}
                out.update(call)
        return out
    return None


def plan_reply(body: dict) -> dict:
    """Return {"text": str}, {"tool": name, "args"?: dict}, or both (text, then
    the call) for one request."""
    messages = body.get("messages") or []
    # The current turn is the run of user messages after the last assistant
    # reply: the Gateway appends its own context block as a separate user
    # message after the real one, so the last user message alone is not it.
    last_assistant = max(
        (i for i, m in enumerate(messages) if m.get("role") == "assistant"
         and not m.get("tool_calls")),
        default=-1,
    )
    turn = messages[last_assistant + 1:]
    user_text = "\n".join(_text_of(m.get("content")) for m in turn if m.get("role") == "user")
    after_user = turn
    tool_results = [_text_of(m.get("content")) for m in after_user if m.get("role") == "tool"]
    offered = [
        (t.get("function") or {}).get("name")
        for t in body.get("tools") or []
    ]

    scripted = _scene_step(user_text, len(tool_results), offered)
    if scripted is not None:
        return scripted

    if "Create a concise title" in user_text:
        return {"text": TITLE}

    asked = messages[last_assistant] if last_assistant >= 0 else {}
    if "needs input:" in _text_of(asked.get("content")):
        # The answer is the first user message, in the agent's envelope.
        first = next((_text_of(m.get("content")) for m in turn if m.get("role") == "user"), "")
        return {"text": f"E2E_ANSWERED {' '.join(first.split())} {ANSWER_END}"}
    if "E2E_ASK_SINGLE" in user_text:
        return {"text": ASK_SINGLE}
    if "E2E_ASK_MULTI" in user_text:
        return {"text": ASK_MULTI}

    if "session_status" in user_text and "model line" in user_text:
        if tool_results:
            line = next((ln.strip() for ln in "\n".join(tool_results).splitlines()
                         if "Model:" in ln), "E2E_NO_MODEL_LINE")
            return {"text": line}
        call = _call("session_status", offered)
        if call:
            return call
        return {"text": f"E2E_NO_TOOL_OFFERED: {sorted(n for n in offered if n)}"}

    if "E2E_TOOL_CALL" in user_text:
        if tool_results:
            return {"text": "E2E_TOOL_DONE"}
        for name in PREFERRED_TOOLS:
            if name in offered:
                return {"tool": name}
        call = _call(PREFERRED_TOOLS[0], offered)
        if call:
            return call
        return {"text": f"E2E_NO_TOOL_OFFERED: {sorted(n for n in offered if n)}"}

    if "E2E_DEPLOY_SCENE" in user_text:
        call = _harmless_call(offered)
        if not call:
            return {"text": f"E2E_NO_TOOL_OFFERED: {sorted(n for n in offered if n)}"}
        if not tool_results:
            return {"text": "E2E_DEPLOY_START", **call}
        if len(tool_results) == 1:
            return {"text": DEPLOY_MID, "delay": 1.0, **call}
        return {"text": DEPLOY_END, "delay": 1.0}

    if "E2E_BG_EXEC" in user_text:
        # One shell command left running in the background, for the Status
        # Action's Background section (ELI-163).
        if tool_results:
            return {"text": "E2E_BG_STARTED"}
        args = {"command": "sleep 25; echo e2e-bg-done", "background": True}
        if "exec" in offered:
            return {"tool": "exec", "args": args}
        for wrapper in DEFERRED_CALL_TOOLS:
            if wrapper in offered:
                return {"tool": wrapper, "args": {"id": "exec", "args": args}}
        return {"text": f"E2E_NO_TOOL_OFFERED: {sorted(n for n in offered if n)}"}

    if "E2E_SPAWN" in user_text:
        # Spawn one sub-agent whose own run is slow, so it is still running
        # while the test reads the Status Action's Subagents section.
        if tool_results:
            return {"text": "E2E_SPAWNED"}
        args = {"task": SPAWN_TASK}
        if "sessions_spawn" in offered:
            return {"tool": "sessions_spawn", "args": args}
        for wrapper in DEFERRED_CALL_TOOLS:
            if wrapper in offered:
                return {"tool": wrapper, "args": {"id": "sessions_spawn", "args": args}}
        return {"text": f"E2E_NO_TOOL_OFFERED: {sorted(n for n in offered if n)}"}

    m = _SLOW.search(user_text)
    if m:
        count = int(m.group(1) or SLOW_WORDS)
        words = " ".join(f"slow-{i}" for i in range(1, count + 1))
        return {"text": f"{m.group(2)} {words}", "delay": 1.0}
    m = _ECHO.search(user_text)
    if m:
        return {"text": m.group(1)}
    m = _EXACTLY.search(user_text)
    if m:
        return {"text": m.group(1)}
    return {"text": DEFAULT_REPLY}


def _usage(body: dict, plan: dict) -> dict:
    """Token counts for the reply. Fixed and tiny by default; with
    FAKE_LLM_USAGE=estimate, about one token per four characters of the
    request and the reply, so context numbers on screen look like a real
    session's (the README screenshots)."""
    if os.environ.get("FAKE_LLM_USAGE") != "estimate":
        return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    prompt = len(json.dumps(body.get("messages") or [])) // 4 + len(json.dumps(body.get("tools") or [])) // 4
    completion = max(1, len(plan.get("text") or "") // 4 + len(json.dumps(plan.get("args") or {})) // 4)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _chunks(text: str) -> list[str]:
    words = text.split(" ")
    return [w if i == 0 else " " + w for i, w in enumerate(words)]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet; the JSONL log is the record
        sys.stderr.write("fake-llm: " + (fmt % args) + "\n")

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [
                {"id": MODEL_ID, "object": "model", "owned_by": "e2e"}
            ]})
        elif self.path.rstrip("/") in ("", "/health"):
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": {"message": "invalid JSON"}})
            return
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": f"no route {self.path}"}})
            return

        plan = plan_reply(body)
        if LOG_PATH:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "path": self.path,
                                     "request": body, "reply": plan}) + "\n")

        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model = body.get("model") or MODEL_ID
        usage = _usage(body, plan)

        if "tool" in plan:
            call = {"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                    "function": {"name": plan["tool"],
                                 "arguments": json.dumps(plan.get("args") or {})}}
            message = {"role": "assistant", "content": plan.get("text") or None,
                       "tool_calls": [call]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": plan["text"]}
            finish = "stop"

        if not body.get("stream"):
            self._json(200, {"id": cid, "object": "chat.completion", "created": created,
                             "model": model, "usage": usage,
                             "choices": [{"index": 0, "message": message,
                                          "finish_reason": finish}]})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(delta: dict, finish_reason=None, extra=None) -> None:
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": model,
                     "choices": [{"index": 0, "delta": delta,
                                  "finish_reason": finish_reason}]}
            if extra:
                chunk.update(extra)
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()

        emit({"role": "assistant", "content": ""})
        for piece in _chunks(plan["text"]) if plan.get("text") else []:
            emit({"content": piece})
            time.sleep(plan.get("delay", 0.02))
        if "tool" in plan:
            call = message["tool_calls"][0]
            emit({"tool_calls": [{"index": 0, **call}]})
        emit({}, finish_reason=finish)
        if (body.get("stream_options") or {}).get("include_usage"):
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": model, "choices": [], "usage": usage}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


def main() -> None:
    port = int(os.environ.get("FAKE_LLM_PORT", "18999"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"fake-llm listening on 127.0.0.1:{port} (model {MODEL_ID})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
