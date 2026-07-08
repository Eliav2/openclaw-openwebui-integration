#!/usr/bin/env python3
"""Test the auto-title feature end-to-end.

Creates a new chat, sends two messages (triggering auto-title on the second),
and verifies the title was generated.

Usage:
    python3 test_auto_title.py
"""

import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
from owui_api import OwuiClient


def test_auto_title():
    c = OwuiClient.from_env()

    # Create new chat
    chat = c.create_chat()
    chat_id = chat.get("id")
    print(f"✓ Created chat: {chat_id}")

    # First exchange
    first_msg = "שלום, מה השעה? תענה בקצרה"
    r1 = c.send_message(chat_id, first_msg)
    content1 = r1.get("choices", [{}])[0].get("message", {}).get("content", "")
    print(f"✓ Msg1: {content1[:80]}")

    time.sleep(3)

    # Second exchange (triggers auto-title)
    r2 = c.send_message(chat_id, "תודה!", history=[
        {"role": "user", "content": first_msg},
        {"role": "assistant", "content": content1},
    ])
    content2 = r2.get("choices", [{}])[0].get("message", {}).get("content", "")
    print(f"✓ Msg2: {content2[:80]}")

    # Wait for background auto-title task
    print("  Waiting for title generation...")
    time.sleep(20)

    # Verify
    info = c.get_chat(chat_id)
    title = info.get("title", "")
    if title and title != first_msg:
        print(f"✅ PASS: title = '{title}'")
        return True
    elif title == first_msg:
        print(f"⚠️  WARN: title is raw first message = '{title}'")
        return False
    else:
        print(f"❌ FAIL: title = '{title}' (empty or missing)")
        return False


if __name__ == "__main__":
    success = test_auto_title()
    sys.exit(0 if success else 1)
