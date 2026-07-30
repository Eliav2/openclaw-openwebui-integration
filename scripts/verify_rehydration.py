#!/usr/bin/env python3
"""Live verification for the mid-run rehydration snapshot behavior (P34).

DEPLOY-GATED: only meaningful after the pipe is installed to the live OWUI
function AND against a real streaming turn. Do NOT run while another agent is
mid-deploy on the shared OWUI. See docs/rehydration-persistence.md.

What it checks
--------------
1. REHYDRATION: while the turn is running (message.done == False), the assistant
   message's persisted `content` grows above empty. That is only possible if the
   pipe's mid-run `replace` snapshots are landing in the DB — i.e. a client that
   reconnects mid-turn would see partial text. (Root bug: this used to stay empty
   until done.)
2. NO DUPLICATION: once done, neither the flat `content` nor the text extracted
   from `output` is a byte-identical whole-message double (ABCABC with no
   separator) — the 820f6cc signature.

Usage
-----
    # In one terminal, send a message in an OWUI chat that streams a longish,
    # tool-using reply, then grab its chat_id (from the /c/<id> URL) and run:
    python3 scripts/verify_rehydration.py <chat_id> [--poll 0.4] [--timeout 300]

It polls until the latest assistant message reports done, printing a verdict.
Auth/creds come from the sibling owui_api.py's OwuiClient (OWUI_URL /
OWUI_EMAIL / OWUI_PASSWORD env vars, with the same LAN defaults).
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from owui_api import OwuiClient  # noqa: E402

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OwuiClient.from_env()
        _client.login()
    return _client


def output_text(output):
    if not output:
        return ""
    parts = []
    for blk in output:
        c = blk.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for cb in c:
                parts.append(cb.get("text", ""))
    return "".join(parts)


def is_doubled(s):
    """True if s is exactly X+X with no separator (the dup signature)."""
    s = s.strip()
    n = len(s)
    if n < 40 or n % 2 != 0:  # ignore trivially short strings
        return False
    return s[: n // 2] == s[n // 2:]


def latest_assistant(chat_id):
    chat = _get_client().get_chat(chat_id)["chat"]
    msgs = chat.get("history", {}).get("messages", {}) or {}
    cur = chat.get("history", {}).get("currentId")
    m = msgs.get(cur)
    if not m or m.get("role") != "assistant":
        # fall back to the newest assistant message
        cand = [x for x in msgs.values() if x.get("role") == "assistant"]
        m = max(cand, key=lambda x: x.get("timestamp", 0)) if cand else None
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chat_id")
    ap.add_argument("--poll", type=float, default=0.4)
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    started = time.time()
    saw_partial_before_done = False
    max_midrun_len = 0
    samples = 0

    print(f"polling chat {args.chat_id} (Ctrl-C to stop)…")
    while True:
        if time.time() - started > args.timeout:
            print("TIMEOUT waiting for done")
            return 2
        try:
            m = latest_assistant(args.chat_id)
        except Exception as e:  # transient API hiccup
            print(f"  (poll error: {e})")
            time.sleep(args.poll)
            continue
        if m is None:
            time.sleep(args.poll)
            continue

        content = m.get("content") or ""
        done = bool(m.get("done"))
        samples += 1
        if not done:
            if content.strip():
                saw_partial_before_done = True
                max_midrun_len = max(max_midrun_len, len(content))
            sys.stdout.write(
                f"\r  mid-run: content={len(content):>6}  output={'yes' if m.get('output') else 'no'}   "
            )
            sys.stdout.flush()
            time.sleep(args.poll)
            continue

        # done
        print()
        otext = output_text(m.get("output"))
        print("── done ──")
        print(f"  samples polled           : {samples}")
        print(f"  rehydration (partial<done): {'PASS' if saw_partial_before_done else 'FAIL'}"
              f"  (max mid-run content len={max_midrun_len})")
        dup_content = is_doubled(content)
        dup_output = is_doubled(otext)
        print(f"  content doubled          : {'FAIL(dup!)' if dup_content else 'ok'}  (len={len(content)})")
        print(f"  output  doubled          : {'FAIL(dup!)' if dup_output else 'ok'}  (len={len(otext)})")
        ok = saw_partial_before_done and not dup_content and not dup_output
        print(f"\n  VERDICT: {'PASS ✅' if ok else 'FAIL ❌'}")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
