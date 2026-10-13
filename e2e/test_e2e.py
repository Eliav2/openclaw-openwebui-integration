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
    return _chat_turn(token, prompt, model)[1]


def _chat_turn(token: str, prompt: str, model: str = DEFAULT_MODEL) -> tuple[str, dict]:
    """(chat id, saved assistant message) of one turn in a new persisted chat."""
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
            return chat_id, msg
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
    # The started item carries its id in `id` only (no `call_id`) to survive
    # OWUI 0.11.1's output_item dedup; OWUI pairs it via `call_id or id`, so
    # match it the same way. See _tool_call_started_event in gateway.py.
    calls = {i.get("call_id") or i.get("id"): i for i in output
             if i.get("type") == "function_call" and (i.get("call_id") or i.get("id"))}
    results = {i["call_id"] for i in output
               if i.get("type") == "function_call_output" and i.get("call_id")}
    dump = json.dumps(msg)[:3000]

    assert calls, f"no function_call item persisted: {dump}"
    assert any(c.get("name") == "session_status" for c in calls.values()), dump
    assert not set(calls) - results, f"call(s) without a result item (stuck spinner): {dump}"
    assert '<details type="tool_calls"' not in (msg.get("content") or ""), (
        f"markdown tool card persisted next to the native one: {dump}")
    assert "E2E_TOOL_DONE" in json.dumps(output) + (msg.get("content") or ""), dump


# ── Cron wakes into an Open WebUI chat (ELI-103) ─────────────────────────────
# Every wake into a session runs with runId = the sessionId, and only the first
# one gets a closing transcript message. Each wake must still become its own
# message, finished, with its own text.

import datetime  # noqa: E402
import subprocess  # noqa: E402

TURN_NS = uuid.UUID("6f1c3e2a-9d4b-4c6e-8a1f-0e0a1b100100")  # turns.TURN_NS
WAKE_TIMEOUT_S = float(os.environ.get("E2E_WAKE_TIMEOUT", "180"))


def _gw(method: str, params: dict) -> dict:
    out = subprocess.run(["openclaw", "gateway", "call", method, "--json",
                          "--params", json.dumps(params)],
                         capture_output=True, text=True, timeout=90)
    assert out.returncode == 0, f"{method} failed: {out.stderr[-1500:]}"
    return json.loads(out.stdout[out.stdout.find("{"):])


def _session_key(token: str, chat_id: str) -> str:
    status, me = _request("GET", "/api/v1/auths/", token=token)
    assert status == 200, me
    return f"agent:main:openwebui-{me['id']}-{chat_id}"


def _schedule_wake(key: str, message: str, in_s: float) -> str:
    at = (datetime.datetime.now(datetime.timezone.utc)
          + datetime.timedelta(seconds=in_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    job = _gw("cron.add", {"name": f"e2e-wake-{uuid.uuid4().hex[:6]}",
                           "schedule": {"kind": "at", "at": at},
                           "sessionTarget": f"session:{key}",
                           "payload": {"kind": "agentTurn", "message": message},
                           "delivery": {"mode": "none"}, "deleteAfterRun": True})
    return (job.get("job") or job).get("id")


def _chat_messages(token: str, chat_id: str) -> dict:
    status, got = _request("GET", f"/api/v1/chats/{chat_id}", token=token)
    assert status == 200, got
    return got["chat"]["history"]["messages"]


def _wait_for(token: str, chat_id: str, tokens: list[str]) -> dict:
    """The chat once every token is in a finished assistant message."""
    deadline = time.time() + WAKE_TIMEOUT_S
    msgs: dict = {}
    while time.time() < deadline:
        msgs = _chat_messages(token, chat_id)
        done = " ".join(m.get("content") or "" for m in msgs.values()
                        if m.get("role") == "assistant" and m.get("done") is True)
        if all(t in done for t in tokens):
            return msgs
        time.sleep(2)
    pytest.fail(f"{tokens} not all finished within {WAKE_TIMEOUT_S:.0f}s: "
                f"{json.dumps({k: (m.get('role'), m.get('done'), (m.get('content') or '')[:80]) for k, m in msgs.items()})[:2500]}")


def _wake_turns(key: str) -> dict:
    """{prompt token: turn id} of the wake user turns in the transcript."""
    hist = _gw("chat.history", {"sessionKey": key, "limit": 100})
    out = {}
    for m in hist.get("messages") or []:
        ikey = str(m.get("idempotencyKey") or (m.get("__openclaw") or {}).get("idempotencyKey") or "")
        text = m.get("content") if isinstance(m.get("content"), str) else " ".join(
            b.get("text") or "" for b in m.get("content") or [] if isinstance(b, dict))
        if m.get("role") == "user" and ikey.endswith(":user") and str(text).lstrip().startswith("[cron:"):
            for tok in ("WAKE_ONE", "WAKE_TWO", "WAKE_SLOW"):
                if tok in text:
                    out[tok] = ikey[:-len(":user")]
    return out


def _assert_clean(token: str, chat_id: str, msgs: dict) -> None:
    branched = [k for k, m in msgs.items() if len(m.get("childrenIds") or []) > 1]
    assert not branched, f"a node with two children: {branched}"
    open_ = [k for k, m in msgs.items() if m.get("role") == "assistant" and m.get("done") is not True]
    assert not open_, f"assistant message(s) left generating: {open_}"
    status, tasks = _request("GET", f"/api/tasks/chat/{chat_id}", token=token)
    assert status == 200 and not (tasks or {}).get("task_ids"), f"tasks left: {tasks}"


def test_two_cron_wakes_in_a_row_get_one_finished_message_each(token):
    chat_id, _ = _chat_turn(token, "Respond with exactly: E2E_WAKE_CHAT")
    key = _session_key(token, chat_id)
    _schedule_wake(key, "Reply with exactly: WAKE_ONE", 3)
    _schedule_wake(key, "Reply with exactly: WAKE_TWO", 4)
    msgs = _wait_for(token, chat_id, ["WAKE_ONE", "WAKE_TWO"])
    turns = _wake_turns(key)
    for tok in ("WAKE_ONE", "WAKE_TWO"):
        holders = [k for k, m in msgs.items() if tok in (m.get("content") or "")]
        assert len(holders) == 1, f"{tok} in {holders}"
        assert holders[0] == str(uuid.uuid5(TURN_NS, f"{key}:{turns[tok]}")), (
            f"{tok}: message {holders[0]} is not its wake turn's ({turns.get(tok)})")
        other = "WAKE_TWO" if tok == "WAKE_ONE" else "WAKE_ONE"
        assert other not in msgs[holders[0]]["content"], "one wake's text in the other's message"
    _assert_clean(token, chat_id, msgs)


def test_a_message_queued_behind_a_wake_does_not_cut_it(token):
    chat_id, _ = _chat_turn(token, "Respond with exactly: E2E_QUEUE_CHAT")
    key = _session_key(token, chat_id)
    _schedule_wake(key, "E2E_SLOW WAKE_SLOW", 2)
    # Send while the wake streams (one word a second): it queues behind it.
    deadline = time.time() + WAKE_TIMEOUT_S
    while time.time() < deadline and not any(
            "WAKE_SLOW" in (m.get("content") or "") for m in _chat_messages(token, chat_id).values()):
        time.sleep(1)
    # What the page does on send: the user message and the reply stub join the
    # chat under the current leaf (the wake's message, still streaming).
    status, got = _request("GET", f"/api/v1/chats/{chat_id}", token=token)
    chat = got["chat"]
    msgs = chat["history"]["messages"]
    leaf = next(k for k, m in msgs.items() if "WAKE_SLOW" in (m.get("content") or ""))
    now = int(time.time())
    uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
    user = {"id": uid, "parentId": leaf, "childrenIds": [aid], "role": "user",
            "content": "Respond with exactly: E2E_QUEUED_REPLY", "timestamp": now,
            "models": [DEFAULT_MODEL]}
    msgs[uid] = user
    msgs[aid] = {"id": aid, "parentId": uid, "childrenIds": [], "role": "assistant",
                 "content": "", "model": DEFAULT_MODEL, "timestamp": now, "done": False}
    msgs[leaf]["childrenIds"] = [*(msgs[leaf].get("childrenIds") or []), uid]
    chat["history"]["currentId"] = aid
    status, saved = _request("POST", f"/api/v1/chats/{chat_id}", {"chat": chat}, token)
    assert status == 200, saved
    status, ack = _request("POST", "/api/chat/completions", {
        "model": DEFAULT_MODEL, "messages": [{"role": "user", "content": user["content"]}],
        "stream": True, "chat_id": chat_id, "id": aid, "session_id": f"e2e-{uuid.uuid4().hex[:8]}",
        "parent_id": uid, "user_message": user}, token)
    assert status == 200, ack
    msgs = _wait_for(token, chat_id, ["slow-10", "E2E_QUEUED_REPLY"])
    wake = [m for m in msgs.values() if "WAKE_SLOW" in (m.get("content") or "")]
    assert len(wake) == 1 and wake[0]["content"].strip().endswith("slow-10"), (
        f"the wake was cut by the queued turn: {json.dumps(wake)[:1500]}")
    assert "E2E_QUEUED_REPLY" not in wake[0]["content"]
    _assert_clean(token, chat_id, msgs)


# ── A run that ends while Open WebUI is stopped (ELI-94) ─────────────────────
# Keep this last: it stops and starts the Open WebUI container. A graceful stop
# cancels the request streaming the reply and saves the message done:true with
# what it had. The run goes on in the Gateway and closes while Open WebUI is
# down. On the next start the reply must hold the whole run.

import shutil  # noqa: E402

DOWN_RUN_WORDS = 40


def _session_row(key: str) -> dict:
    rows = _gw("sessions.list", {"search": key[-36:], "limit": 10}).get("sessions") or []
    return next((r for r in rows if r.get("key") == key), {})


def _until(what: str, check, timeout_s: float = WAKE_TIMEOUT_S, every_s: float = 1.0):
    """Poll `check` (a real state read) until it returns a truthy value."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        got = check()
        if got:
            return got
        time.sleep(every_s)
    pytest.fail(f"timed out after {timeout_s:.0f}s waiting for {what}")


def _owui_up() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _down_state(token: str, chat_id: str, key: str) -> str:
    """What the startup reconcile reads, for a failure message."""
    row = _session_row(key)
    status, got = _request("GET", f"/api/v1/chats/{chat_id}", token=token)
    hist = _gw("chat.history", {"sessionKey": key, "limit": 100})
    lines = [f"session row: {json.dumps({k: row.get(k) for k in ('updatedAt', 'hasActiveRun', 'status', 'sessionId', 'lastRunId')})}",
             f"chat updated_at: {got.get('updated_at') if status == 200 else status}"]
    for m in hist.get("messages") or []:
        meta = m.get("__openclaw") if isinstance(m.get("__openclaw"), dict) else {}
        text = m.get("content") if isinstance(m.get("content"), str) else " ".join(
            b.get("text") or b.get("type") or "" for b in m.get("content") or [] if isinstance(b, dict))
        lines.append(f"  {m.get('role')} key={m.get('idempotencyKey')} meta_key={meta.get('idempotencyKey')} "
                     f"ts={m.get('timestamp')} meta={sorted(meta)} text={str(text)[-60:]!r}")
    return "\n".join(lines)


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the e2e stack's Open WebUI container")
def test_a_reply_whose_run_ends_while_owui_is_stopped_is_completed_on_start(token):
    now = int(time.time())
    uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
    prompt = f"E2E_SLOW:{DOWN_RUN_WORDS} E2E_DOWN"
    user = {"id": uid, "parentId": None, "childrenIds": [aid], "role": "user",
            "content": prompt, "timestamp": now, "models": [DEFAULT_MODEL]}
    assistant = {"id": aid, "parentId": uid, "childrenIds": [], "role": "assistant",
                 "content": "", "model": DEFAULT_MODEL, "timestamp": now, "done": False}
    chat = {"title": "E2E down", "models": [DEFAULT_MODEL],
            "history": {"messages": {uid: user, aid: assistant}, "currentId": aid},
            "messages": [user, assistant], "files": [], "tags": [], "timestamp": now * 1000}
    status, created = _request("POST", "/api/v1/chats/new", {"chat": chat}, token)
    assert status == 200, created
    chat_id = created["id"]
    key = _session_key(token, chat_id)
    status, ack = _request("POST", "/api/chat/completions", {
        "model": DEFAULT_MODEL, "messages": [{"role": "user", "content": prompt}],
        "stream": True, "chat_id": chat_id, "id": aid, "session_id": f"e2e-{uuid.uuid4().hex[:8]}",
        "parent_id": uid, "user_message": user}, token)
    assert status == 200, ack

    # Stop Open WebUI while the run streams (the Gateway's own state says so).
    _until("the run to start", lambda: _session_row(key).get("hasActiveRun"))
    subprocess.run(["docker", "stop", "-t", "30", "owui"], check=True, capture_output=True)
    assert _session_row(key).get("hasActiveRun"), (
        f"the run ended before Open WebUI was down; raise DOWN_RUN_WORDS ({DOWN_RUN_WORDS})")
    # The run closes while Open WebUI is down.
    _until("the run to end", lambda: not _session_row(key).get("hasActiveRun"), timeout_s=120)
    subprocess.run(["docker", "start", "owui"], check=True, capture_output=True)
    _until("Open WebUI to come back", _owui_up, timeout_s=300, every_s=2)

    # The precondition this test is about: the stop saved a partial, finished reply.
    before = _chat_messages(token, chat_id)[aid]
    assert before.get("done") is True and f"slow-{DOWN_RUN_WORDS}" not in (before.get("content") or ""), (
        f"precondition: Open WebUI's stop should leave a partial done:true reply: {json.dumps(before)[:1500]}")

    # The first model listing after a start runs the startup reconcile.
    status, _models = _request("GET", "/api/models", token=token)
    assert status == 200
    try:
        msgs = _wait_for(token, chat_id, [f"slow-{DOWN_RUN_WORDS}"])
    except pytest.fail.Exception as ex:
        pytest.fail(f"{ex}\n{_down_state(token, chat_id, key)}")
    holders = [k for k, m in msgs.items() if f"slow-{DOWN_RUN_WORDS}" in (m.get("content") or "")]
    assert holders == [aid], f"the end of the run went to {holders}, not the reply {aid}"
    assert msgs[aid]["content"].strip().startswith("E2E_DOWN slow-1"), msgs[aid]["content"][:300]
    assert [k for k, m in msgs.items() if m.get("role") == "assistant"] == [aid], "a second reply"
    _assert_clean(token, chat_id, msgs)
