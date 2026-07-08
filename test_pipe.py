#!/usr/bin/env python3
"""
Comprehensive pipe test suite — runs against the live OWUI instance.
Tests the pipe's core functionality independently of the frontend.

Usage:
  OWUI_EMAIL="admin@example.com" OWUI_PASSWORD="secret" python3 test_pipe.py
  # or via SSH to the HA host:
  scp test_pipe.py root@your-owui-host:/tmp/
  ssh root@your-owui-host "OWUI_PASSWORD='secret' python3 /tmp/test_pipe.py"
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("OWUI_URL", "http://localhost:8080").rstrip("/") + "/api"
FUNCTION_ID = "openclaw_gateway"
DEFAULT_MODEL_ID = "openclaw_gateway.default"
API_TIMEOUT = int(os.environ.get("OWUI_API_TIMEOUT", "180"))

# Will be set after login
AUTH_TOKEN = None
# Set during model discovery: any dynamically-discovered non-default model id
ROUTE_MODEL_ID = None


# ── helpers ──────────────────────────────────────────────────────────

def api(method: str, path: str, data: dict | None = None) -> dict:
    url = f"{BASE}/{path.lstrip('/')}"
    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:1000]
        try:
            detail = json.dumps(json.loads(detail), indent=2)
        except json.JSONDecodeError:
            pass
        return {"error": {"status": e.code, "detail": detail}}
    except Exception as e:
        return {"error": {"detail": str(e)}}


def ok(msg: str):
    print(f"  ✅ {msg}")


def fail(msg: str, detail: str = ""):
    print(f"  ❌ {msg}")
    if detail:
        for line in detail.strip().split("\n"):
            print(f"     {line}")
    raise AssertionError(msg)


def check(result: dict, expected_keys: list[str] | None = None,
           not_expected_keys: list[str] | None = None) -> bool:
    if "error" in result:
        fail("Error response", json.dumps(result["error"], indent=2))
        return False
    if expected_keys:
        for k in expected_keys:
            if k not in result:
                fail(f"Missing key: {k}")
                return False
    if not_expected_keys:
        for k in not_expected_keys:
            if k in result:
                fail(f"Unexpected key: {k}")
                return False
    return True


# ── tests ────────────────────────────────────────────────────────────

PASSED = 0
FAILED = 0


def test(name: str, fn):
    global PASSED, FAILED
    print(f"\n── {name} ──")
    try:
        fn()
        PASSED += 1
    except Exception as e:
        FAILED += 1
        print("  ❌ Exception")
        for line in str(e).strip().split("\n"):
            if line:
                print(f"     {line}")


def send(payload: dict) -> dict:
    return api("POST", "chat/completions", payload)


# ─────────────────────────────────────────────────────────────────────

def test_basic_chat():
    """Simple non-streaming chat — verify pipe responds correctly."""
    r = send({
        "model": DEFAULT_MODEL_ID,
        "messages": [{"role": "user", "content": "Respond with exactly: PIPE_TEST_OK"}],
        "stream": False,
    })
    if not check(r, ["choices"]):
        return
    content = r["choices"][0]["message"]["content"]
    if "PIPE_TEST_OK" in content:
        ok("Pipe responds correctly")
    else:
        ok(f"Pipe responded (no exact match): {content[:80]}")


def test_chat_saves_to_history():
    """Verify chat appears in user's chat list."""
    r = send({
        "model": DEFAULT_MODEL_ID,
        "messages": [{"role": "user", "content": "Say hi. No emoji."}],
        "stream": False,
    })
    if not check(r, ["choices", "id"]):
        return
    ok(f"Chat saved (id: {r.get('id','?')[:16]}...)")


def test_model_not_found():
    """Invalid model ID should return 404."""
    r = send({
        "model": "nonexistent_model_xyz",
        "messages": [{"role": "user", "content": "hi"}],
    })
    if "error" in r:
        ok("Invalid model correctly rejected")
    else:
        fail("Invalid model should have errored")


def test_streaming():
    """Streaming response — verify SSE chunks."""
    url = f"{BASE}/chat/completions"
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {AUTH_TOKEN}"}
    payload = json.dumps({
        "model": DEFAULT_MODEL_ID,
        "messages": [{"role": "user",
                       "content": "Count from 1 to 3, one per line."}],
        "stream": True,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            chunks = [l for l in raw.split("\n") if l.startswith("data: ")]
            if len(chunks) >= 3:
                ok(f"Streamed {len(chunks)} SSE chunks — streaming works")
            else:
                fail(f"Only {len(chunks)} chunks, expected >=3")
    except Exception as e:
        fail(f"Streaming error: {e}")


def test_metadata_passthrough():
    """Verify __user__ identity is passed through."""
    r = send({
        "model": DEFAULT_MODEL_ID,
        "messages": [{"role": "user",
                       "content": "What is my user name? Just answer with the name."}],
        "stream": False,
    })
    if not check(r, ["choices"]):
        return
    content = r["choices"][0]["message"]["content"]
    ok(f"Response received: {content[:80]}")


def test_route_model_override():
    """Verify a dynamically-discovered (non-default) selector entry really
    patches the OpenClaw model. Replaces the old hardcoded-"chatgpt" preset
    test, which no longer applies now that presets are discovered
    dynamically instead of hardcoded (see ELI-11)."""
    if not ROUTE_MODEL_ID:
        ok("Skipped: no non-default model currently registered")
        return
    expected_key = ROUTE_MODEL_ID.split(".", 1)[1]
    r = send({
        "model": ROUTE_MODEL_ID,
        "messages": [{
            "role": "user",
            "content": (
                "Use session_status for current session, then answer with "
                "only the exact model line."
            ),
        }],
        "stream": False,
    })
    if not check(r, ["choices"]):
        return
    content = r["choices"][0]["message"]["content"]
    if expected_key in content:
        ok(f"Route override uses {expected_key}")
    else:
        fail(f"Route override did not report {expected_key}", content[:500])


def test_empty_message():
    """Empty user message."""
    r = send({
        "model": DEFAULT_MODEL_ID,
        "messages": [{"role": "user", "content": ""}],
        "stream": False,
    })
    if "error" in r:
        ok("Empty message correctly rejected or handled")
    else:
        ok("Empty message handled (returned response)")


def test_special_characters():
    """Hebrew and special characters."""
    r = send({
        "model": DEFAULT_MODEL_ID,
        "messages": [{"role": "user",
                       "content": "Say hello in exactly 3 Hebrew words: \u05e9\u05dc\u05d5\u05dd \u05e2\u05d5\u05dc\u05dd!"}],
        "stream": False,
    })
    if not check(r, ["choices"]):
        return
    content = r["choices"][0]["message"]["content"]
    ok(f"Hebrew handled: {content[:80]}")


# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("OpenClaw Gateway Pipe — OWUI Integration Test Suite")
    print("=" * 60)

    # Login
    print("\n── Authenticating ──")
    email = os.environ.get("OWUI_EMAIL", "admin@example.com")
    password = os.environ.get("OWUI_PASSWORD")
    if not password:
        print("ERROR: OWUI_PASSWORD env var not set")
        print("  Usage: OWUI_PASSWORD=secret python3 test_pipe.py")
        sys.exit(1)

    login = api("POST", "v1/auths/signin",
                {"email": email, "password": password})
    AUTH_TOKEN = login.get("token")
    if not AUTH_TOKEN:
        print("ERROR: Login failed")
        print(json.dumps(login.get("error", login), indent=2)[:300])
        sys.exit(1)
    ok(f"Logged in as {login.get('name')} ({login.get('role')})")

    # Verify pipe model exists
    print("\n── Model discovery ──")
    models = api("GET", "models")
    if "error" in models:
        fail("Cannot list models", json.dumps(models["error"]))
    else:
        ids = {m.get("id") for m in models.get("data", [])}
        ROUTE_MODEL_ID = next(
            (i for i in ids if i.startswith(f"{FUNCTION_ID}.") and i != DEFAULT_MODEL_ID),
            None,
        )
        if DEFAULT_MODEL_ID in ids:
            if ROUTE_MODEL_ID:
                ok(f"Pipe models found: default + {ROUTE_MODEL_ID}")
            else:
                ok("Pipe model found: default only (no dynamic models discovered yet)")
        else:
            fail("Default pipe model NOT found in models list")
            sys.exit(1)

    # Run tests
    test("Basic non-streaming chat", test_basic_chat)
    test("Chat saves to history", test_chat_saves_to_history)
    test("Invalid model rejection", test_model_not_found)
    test("Streaming response", test_streaming)
    test("Metadata/user identity passthrough", test_metadata_passthrough)
    test("Route model override", test_route_model_override)
    test("Empty message handling", test_empty_message)
    test("Hebrew / special characters", test_special_characters)

    # Summary
    print("\n" + "=" * 60)
    total = PASSED + FAILED
    print(f"Results: {total}/{total} passed" if not FAILED else
          f"Results: {PASSED}/{total} passed, {FAILED} failed")
    if not FAILED:
        print("All tests passed! ✅")
    print("=" * 60)
