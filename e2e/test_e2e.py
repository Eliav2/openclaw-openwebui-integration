"""End-to-end suite: real Open WebUI -> this integration -> real OpenClaw Gateway.

Only the language model is fake (`e2e/fake_llm.py`), which is what lets every
assertion here be exact. Everything between the user and the model is the
real code users run: the Open WebUI container, the installed Pipe, Action and
Filter, the Gateway with its device pairing, and the agent loop with a real
tool call.

The chat tests go through the same path the browser does: create a chat, ask
for a completion bound to it, and read back what Open WebUI *persisted*. That
is what a user sees after a reload, and it is the layer where the worst
regressions in this project have hidden (a streamed tool card that rendered
live and vanished from the saved message, a vendored fixture that kept the
unit suite green while the real UI showed nothing).

Skips unless E2E_STACK=1 (set by the CI job after it has started the stack),
so a plain `pytest` on a laptop never tries to talk to a live instance.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("E2E_STACK") != "1",
    reason="end-to-end stack not running (set E2E_STACK=1; see .github/workflows/ci.yml)",
)

BASE = os.environ.get("OWUI_URL", "http://localhost:8080").rstrip("/")
DEFAULT_MODEL = "openclaw_gateway.default"
TURN_TIMEOUT_S = float(os.environ.get("E2E_TURN_TIMEOUT", "120"))


def _request(method: str, path: str, body=None, token: str | None = None,
             timeout: float = 60, raw: bool = False):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode()
            return resp.status, (payload if raw else json.loads(payload or "null"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:2000]


@pytest.fixture(scope="session")
def token() -> str:
    status, body = _request("POST", "/api/v1/auths/signin", {
        "email": os.environ["OWUI_EMAIL"], "password": os.environ["OWUI_PASSWORD"],
    })
    assert status == 200 and body.get("token"), f"sign-in failed: {status} {body}"
    return body["token"]


def _completion_text(body: dict) -> str:
    return body["choices"][0]["message"]["content"].strip()


def _drive_chat_turn(token: str, prompt: str, model: str = DEFAULT_MODEL) -> dict:
    """Run one turn in a real, persisted chat and return the saved assistant message."""
    now = int(time.time())
    uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
    user = {"id": uid, "parentId": None, "childrenIds": [aid], "role": "user",
            "content": prompt, "timestamp": now, "models": [model]}
    assistant = {"id": aid, "parentId": uid, "childrenIds": [], "role": "assistant",
                 "content": "", "model": model, "timestamp": now, "done": False}
    chat = {"title": "E2E", "models": [model],
            "history": {"messages": {uid: user, aid: assistant}, "currentId": aid},
            "messages": [user, assistant], "files": [], "tags": [],
            "timestamp": now * 1000}
    status, created = _request("POST", "/api/v1/chats/new", {"chat": chat}, token)
    assert status == 200, f"chat create failed: {status} {created}"
    chat_id = created["id"]

    status, ack = _request("POST", "/api/chat/completions", {
        "model": model, "messages": [{"role": "user", "content": prompt}],
        "stream": True, "chat_id": chat_id, "id": aid,
        "session_id": f"e2e-{uuid.uuid4().hex[:8]}",
        "parent_id": uid, "user_message": user,
    }, token)
    # With a chat_id Open WebUI runs the turn as a background task and returns
    # an ack at once; the reply only exists in the persisted chat.
    assert status == 200, f"completion request failed: {status} {ack}"

    deadline = time.time() + TURN_TIMEOUT_S
    msg: dict = {}
    while time.time() < deadline:
        status, got = _request("GET", f"/api/v1/chats/{chat_id}", token=token)
        assert status == 200, f"chat read failed: {status} {got}"
        msg = got["chat"]["history"]["messages"].get(aid, {})
        if msg.get("done"):
            return msg
        time.sleep(1.5)
    pytest.fail(f"turn did not finish within {TURN_TIMEOUT_S:.0f}s; last saved state: "
                f"{json.dumps(msg)[:1500]}")


def test_all_three_functions_installed_and_active(token):
    status, functions = _request("GET", "/api/v1/functions/", token=token)
    assert status == 200, functions
    by_type = {(f["type"], f["id"]): f for f in functions}
    for kind in ("pipe", "action", "filter"):
        matches = [f for (t, _), f in by_type.items() if t == kind]
        assert matches, f"no {kind} function installed: {sorted(by_type)}"
        assert all(f.get("is_active") for f in matches), f"{kind} installed but inactive"


def test_function_metadata_parsed(token):
    """Open WebUI only reads `key: value` frontmatter; prose silently parses to {}."""
    status, pipe = _request("GET", "/api/v1/functions/id/openclaw_gateway", token=token)
    assert status == 200, pipe
    manifest = (pipe.get("meta") or {}).get("manifest") or {}
    assert manifest.get("title") and manifest.get("version"), (
        f"frontmatter not parsed by Open WebUI: manifest={manifest}")


def test_models_listed(token):
    status, body = _request("GET", "/api/models", token=token)
    assert status == 200, body
    ids = [m["id"] for m in body["data"]]
    assert DEFAULT_MODEL in ids, ids
    routed = [i for i in ids if i.startswith("openclaw_gateway.") and i != DEFAULT_MODEL]
    assert any("e2e-echo" in i for i in routed), (
        f"the Gateway's model was not discovered into the selector: {ids}")


def test_non_streaming_reply_is_exact(token):
    status, body = _request("POST", "/api/chat/completions", {
        "model": DEFAULT_MODEL, "stream": False,
        "messages": [{"role": "user", "content": "Respond with exactly: E2E_NONSTREAM_OK"}],
    }, token, timeout=TURN_TIMEOUT_S)
    assert status == 200, body
    assert _completion_text(body) == "E2E_NONSTREAM_OK"


def test_streaming_reply_arrives_in_chunks(token):
    status, raw = _request("POST", "/api/chat/completions", {
        "model": DEFAULT_MODEL, "stream": True,
        "messages": [{"role": "user", "content":
                      "Respond with exactly: E2E_STREAM_OK and nothing else"}],
    }, token, timeout=TURN_TIMEOUT_S, raw=True)
    assert status == 200, raw
    events = [line[6:] for line in raw.splitlines()
              if line.startswith("data: ") and line[6:].strip() != "[DONE]"]
    text = ""
    for ev in events:
        try:
            chunk = json.loads(ev)
        except json.JSONDecodeError:
            continue
        for choice in chunk.get("choices") or []:
            text += (choice.get("delta") or {}).get("content") or ""
    assert len(events) >= 2, f"expected a real stream, got {len(events)} events: {raw[:800]}"
    assert "E2E_STREAM_OK" in text, f"streamed text: {text!r}"


def test_routed_model_reaches_the_gateway(token):
    status, body = _request("GET", "/api/models", token=token)
    routed = next(m["id"] for m in body["data"]
                  if m["id"].startswith("openclaw_gateway.") and "e2e-echo" in m["id"])
    status, body = _request("POST", "/api/chat/completions", {
        "model": routed, "stream": False,
        "messages": [{"role": "user", "content": "Respond with exactly: E2E_ROUTE_OK"}],
    }, token, timeout=TURN_TIMEOUT_S)
    assert status == 200, body
    assert _completion_text(body) == "E2E_ROUTE_OK"


def test_persisted_chat_keeps_the_reply(token):
    msg = _drive_chat_turn(token, "Respond with exactly: E2E_PERSIST_OK")
    assert "E2E_PERSIST_OK" in (msg.get("content") or ""), json.dumps(msg)[:1500]


def test_tool_call_persists_as_a_native_tool_card(token):
    """A tool call must land in the saved message as a function_call plus a
    matching function_call_output: an unmatched call renders as a spinner that
    never stops, and a missing pair renders no card at all."""
    msg = _drive_chat_turn(token, "E2E_TOOL_CALL")
    output = msg.get("output") or []
    calls = {i["call_id"]: i for i in output
             if i.get("type") == "function_call" and i.get("call_id")}
    results = {i["call_id"] for i in output
               if i.get("type") == "function_call_output" and i.get("call_id")}
    dump = json.dumps(msg)[:3000]

    assert calls, f"no function_call item persisted: {dump}"
    assert any(c.get("name") == "session_status" for c in calls.values()), dump
    assert not set(calls) - results, f"call(s) without a result item (stuck spinner): {dump}"
    assert '<details type="tool_calls"' not in (msg.get("content") or ""), (
        f"markdown tool card persisted next to the native one: {dump}")
    assert "E2E_TOOL_DONE" in json.dumps(output) + (msg.get("content") or ""), dump
