#!/usr/bin/env python3
"""The stack's stand-in for the `claude` CLI behind the Gateway's claude-cli runtime.

Speaks the part of `--input-format stream-json --output-format stream-json` that steering
needs (OpenClaw 2026.10.10, extensions/anthropic cli.runtime): the init message advertises
`msg_lifecycle_v1`, each user input gets `command_lifecycle` events by its uuid, and the
prompt goes through the host's `UserPromptSubmit` hook, which is what lets the Gateway inject
a message into the running turn.

Replies:
* `E2E_CLI_HOLD TOKEN` -> says `E2E_CLI_HOLD_START`, then waits for the file
  `$E2E_FAKE_CLAUDE_DIR/hold-TOKEN` or for a message injected into the turn. An injected
  message is taken in (its `command_lifecycle` started) and answered as `E2E_CLI_STEERED:
  <its text>`; with none, the turn ends with `E2E_CLI_HOLD_END`.
* `E2E_CLI_TOOL_HOLD TOKEN` -> runs tool step 1, says `E2E_CLI_HOLD_START`, waits for the
  hold file, then runs tool step 2 and ends with `E2E_CLI_HOST_DONE`. A cron wake fired
  meanwhile runs after the turn; its prompt carries the time it fired, between the steps.
* the installer's route smoke ("... answer with only the exact model line") -> the model's
  ref, `anthropic/<--model>`, as session_status would give it.
* anything else -> `E2E_CLI_REPLY`.
"""
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

if sys.argv[1:2] == ["auth"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                      "email": "sandbox@example.invalid", "subscriptionType": "max"}))
    sys.exit(0)
if "--version" in sys.argv:
    print("2.1.302 (Claude Code)")
    sys.exit(0)

HOLD = re.compile(r"E2E_CLI_HOLD\s+([A-Za-z0-9_]{3,})")
TOOL_HOLD = re.compile(r"E2E_CLI_TOOL_HOLD\s+([A-Za-z0-9_]{3,})")
DIR = os.environ["E2E_FAKE_CLAUDE_DIR"]  # e2e/start_stack.sh: its log dir, where the hold files are
sid = next((sys.argv[i + 1] for i, a in enumerate(sys.argv[:-1]) if a in ("--session-id", "--resume")),
           str(uuid.uuid4()))
model = next((sys.argv[i + 1] for i, a in enumerate(sys.argv[:-1]) if a == "--model"), "claude-opus-5-5")
log = open(os.path.join(DIR, "fake-claude.log"), "a")
# Claude Code's own transcript of the session, which the Gateway imports into
# chat.history (cli-session-history.claude, claude-cli-project-dir).
project = os.path.join(os.environ["HOME"], ".claude", "projects", re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(os.getcwd())))
os.makedirs(project, exist_ok=True)
transcript = open(os.path.join(project, f"{sid}.jsonl"), "a")
out_lock = threading.Lock()
inputs: "queue.Queue[dict]" = queue.Queue()   # user messages, in order
responses: dict = {}                          # request_id -> control_response
answered = threading.Condition()


def emit(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def note(text):
    log.write(text + "\n")
    log.flush()


def read_stdin():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        note("stdin: " + line.strip()[:300])
        if msg.get("type") == "control_request":
            emit({"type": "control_response", "response": {"subtype": "success",
                                                          "request_id": msg.get("request_id"), "response": {}}})
        elif msg.get("type") == "control_response":
            with answered:
                responses[(msg.get("response") or {}).get("request_id")] = msg
                answered.notify_all()
        elif msg.get("type") == "user":
            inputs.put(msg)
    inputs.put({"type": "eof"})


def text_of(msg):
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content or "")


def record(role, content, **message):
    transcript.write(json.dumps({"type": role, "uuid": str(uuid.uuid4()), "sessionId": sid,
                                 "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                                 "message": {"role": role, "content": content, **message}}) + "\n")
    transcript.flush()


def lifecycle(state, command_uuid):
    emit({"type": "command_lifecycle", "state": state, "command_uuid": command_uuid, "session_id": sid})


def prompt_hook(prompt):
    """Ask the host's UserPromptSubmit hook, as Claude Code does for every prompt."""
    rid = "req_" + uuid.uuid4().hex[:12]
    emit({"type": "control_request", "request_id": rid, "request": {
        "subtype": "hook_callback", "callback_id": "UserPromptSubmit",
        "input": {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "session_id": sid}}})
    with answered:
        answered.wait_for(lambda: rid in responses, timeout=60)


def say(text, stop="end_turn"):
    """One assistant message, streamed first as `--include-partial-messages`
    does (the Gateway shows a claude-cli reply from these deltas)."""
    for event in ({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
                  {"type": "content_block_stop", "index": 0}):
        emit({"type": "stream_event", "event": event, "uuid": str(uuid.uuid4()), "session_id": sid,
              "parent_tool_use_id": None})
    message = {"id": "msg_" + uuid.uuid4().hex[:20], "type": "message", "role": "assistant", "model": model,
               "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
               "usage": {"input_tokens": 10, "output_tokens": 2, "cache_creation_input_tokens": 0,
                         "cache_read_input_tokens": 0}}
    emit({"type": "assistant", "message": message, "session_id": sid, "parent_tool_use_id": None})
    record("assistant", message["content"], model=model, stop_reason=stop, usage=message["usage"])


def result(text):
    emit({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 5, "duration_api_ms": 5,
          "num_turns": 1, "result": text, "session_id": sid, "stop_reason": "end_turn",
          "usage": {"input_tokens": 10, "output_tokens": 2}})


def tool_step(n, token):
    """One Bash call and its result, as Claude Code streams and records them."""
    call = {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": "Bash",
            "input": {"command": f"echo e2e-step-{n}-{token}"}}
    message = {"id": "msg_" + uuid.uuid4().hex[:20], "type": "message", "role": "assistant", "model": model,
               "content": [call], "stop_reason": "tool_use", "stop_sequence": None,
               "usage": {"input_tokens": 10, "output_tokens": 2, "cache_creation_input_tokens": 0,
                         "cache_read_input_tokens": 0}}
    emit({"type": "assistant", "message": message, "session_id": sid, "parent_tool_use_id": None})
    record("assistant", message["content"], model=model, stop_reason="tool_use", usage=message["usage"])
    output = [{"type": "tool_result", "tool_use_id": call["id"], "content": f"e2e-step-{n}-{token}"}]
    emit({"type": "user", "message": {"role": "user", "content": output}, "session_id": sid,
          "parent_tool_use_id": None})
    record("user", output)


def take_in(msg):
    """Start a user input: its lifecycle, its replay, its prompt hook."""
    lifecycle("started", msg.get("uuid"))
    emit(msg | {"session_id": sid, "isReplay": True})
    record("user", (msg.get("message") or {}).get("content"))
    prompt_hook(text_of(msg))


note(f"start argv={sys.argv[1:]}")
emit({"type": "system", "subtype": "init", "session_id": sid, "model": model, "tools": [], "mcp_servers": [],
      "cwd": os.getcwd(), "permissionMode": "default", "apiKeySource": "none",
      "capabilities": ["msg_lifecycle_v1"]})
threading.Thread(target=read_stdin, daemon=True).start()
while True:
    msg = inputs.get()
    if msg.get("type") == "eof":
        break
    take_in(msg)
    tool_hold = TOOL_HOLD.search(text_of(msg))
    if tool_hold:
        token = tool_hold.group(1)
        tool_step(1, token)
        say("E2E_CLI_HOLD_START")
        note(f"holding {token}")
        while not os.path.exists(os.path.join(DIR, f"hold-{token}")):
            time.sleep(0.2)
        tool_step(2, token)
        say("E2E_CLI_HOST_DONE")
        lifecycle("completed", msg.get("uuid"))
        result("E2E_CLI_HOST_DONE")
        continue
    hold = HOLD.search(text_of(msg))
    if not hold:
        reply = f"anthropic/{model}" if "exact model line" in text_of(msg) else "E2E_CLI_REPLY"
        say(reply)
        lifecycle("completed", msg.get("uuid"))
        result(reply)
        continue
    say("E2E_CLI_HOLD_START")
    note(f"holding {hold.group(1)}")  # the test waits for this line before it sends into the turn
    gate = os.path.join(DIR, f"hold-{hold.group(1)}")
    steered = []
    while not os.path.exists(gate):
        try:
            steered.append(inputs.get(timeout=0.2))
            break
        except queue.Empty:
            continue
    if steered and steered[0].get("type") != "eof":
        injected = steered[0]
        take_in(injected)
        answer = f"E2E_CLI_STEERED: {text_of(injected)}"
        say(answer)
        lifecycle("completed", injected.get("uuid"))
        lifecycle("completed", msg.get("uuid"))
        result(answer)
        continue
    say("E2E_CLI_HOLD_END")
    lifecycle("completed", msg.get("uuid"))
    result("E2E_CLI_HOLD_END")
    if steered:
        break
