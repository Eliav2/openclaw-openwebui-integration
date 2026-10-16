#!/usr/bin/env python3
"""
OWUI API helper -- reusable module for OWUI REST API operations.
Import or run standalone for login/chat/title/files operations.

Usage as module:
    from owui_api import OwuiClient
    c = OwuiClient("http://localhost:8080", "admin@example.com", "your-password")
    c.login()
    chats = c.list_chats()
    c.set_chat_title(chat_id, "My Title")

Usage as CLI (reads OWUI_URL / OWUI_EMAIL / OWUI_PASSWORD from the environment):
    OWUI_URL=http://localhost:8080 OWUI_EMAIL=admin@example.com OWUI_PASSWORD=secret \
        python3 owui_api.py login
    python3 owui_api.py list-chats
    python3 owui_api.py set-title <chat_id> "Title"
"""

import urllib.request
import urllib.error
import json
import os
import sys
import uuid
from typing import Optional


class OwuiClient:
    """Stateless OWUI REST API client."""

    def __init__(self, base_url: str, email: str = "", password: str = ""):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.token: Optional[str] = None

    # ── Auth ────────────────────────────────────────────────────────

    def login(self) -> str:
        """Authenticate and return the bearer token."""
        data = json.dumps({"email": self.email, "password": self.password}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/auths/signin",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = self._json(urllib.request.urlopen(req, timeout=10))
        self.token = resp["token"]
        return self.token

    def _auth_headers(self) -> dict:
        if not self.token:
            self.login()
        return {"Authorization": f"Bearer {self.token}"}

    @staticmethod
    def from_env() -> "OwuiClient":
        """Create from OWUI_URL, OWUI_EMAIL, OWUI_PASSWORD env vars.

        Falls back to this repo's .env.local for any of the three that
        aren't already set in the environment -- callers that invoke a
        verify/driver script directly (not through a shell wrapper that
        sources .env.local first) would otherwise silently authenticate
        with an empty email/password and get a bare HTTP 400 from OWUI.
        """
        env = dict(os.environ)
        if not (env.get("OWUI_URL") and env.get("OWUI_EMAIL") and env.get("OWUI_PASSWORD")):
            dotenv_path = os.path.join(os.path.dirname(__file__), "..", ".env.local")
            try:
                with open(dotenv_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, _, value = line.partition("=")
                        env.setdefault(key.strip(), value.strip())
            except OSError:
                pass
        return OwuiClient(
            env.get("OWUI_URL", "http://localhost:8080"),
            env.get("OWUI_EMAIL", ""),
            env.get("OWUI_PASSWORD", ""),
        )

    # ── Low-level ────────────────────────────────────────────────────

    def _json(self, resp) -> dict:
        return json.loads(resp.read().decode("utf-8", "replace"))

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}", headers=self._auth_headers()
        )
        return self._json(urllib.request.urlopen(req, timeout=15))

    def _post(self, path: str, data: dict, timeout: int = 15) -> dict:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            method="POST",
        )
        return self._json(urllib.request.urlopen(req, timeout=timeout))

    def _delete(self, path: str) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}", headers=self._auth_headers(), method="DELETE"
        )
        resp = urllib.request.urlopen(req, timeout=15)
        if resp.status == 204:
            return {}
        return self._json(resp)

    # ── Chats ────────────────────────────────────────────────────────

    def list_chats(self, page: int = 1, limit: int = 50) -> list[dict]:
        """Return recent chats."""
        data = self._get(f"/api/v1/chats/?page={page}&limit={limit}")
        return data if isinstance(data, list) else data.get("data", [])

    def get_chat(self, chat_id: str) -> dict:
        """Get a single chat by ID (includes messages)."""
        return self._get(f"/api/v1/chats/{chat_id}")

    def create_chat(self, title: str = "") -> dict:
        """Create a new empty chat."""
        return self._post("/api/v1/chats/new", {"chat": {"title": title}})

    def set_chat_title(self, chat_id: str, title: str) -> dict:
        """Update a chat's title."""
        return self._post(f"/api/v1/chats/{chat_id}", {"title": title})

    def delete_chat(self, chat_id: str) -> dict:
        """Delete a chat by ID."""
        return self._delete(f"/api/v1/chats/{chat_id}")

    # ── Files ────────────────────────────────────────────────────────

    def list_files(self) -> list[dict]:
        """List uploaded files."""
        return self._get("/api/v1/files/")

    def delete_file(self, file_id: str) -> dict:
        """Delete a file by ID."""
        return self._delete(f"/api/v1/files/{file_id}")

    def delete_all_files(self) -> int:
        """Delete all uploaded files. Returns count deleted."""
        files = self.list_files()
        count = 0
        for f in files:
            fid = f.get("id")
            if fid:
                try:
                    self.delete_file(fid)
                    count += 1
                except Exception:
                    pass
        return count

    # ── Chat completions (for testing) ────────────────────────────────

    def send_message(
        self, chat_id: str, message: str, history: list[dict] | None = None,
        model: str = "openclaw_gateway.default", stream: bool = False,
        timeout: int = 120,
    ) -> dict:
        """Send a message to a chat and get the model's response.

        Sends the turn the way the OWUI frontend does: an assistant message
        `id` plus a `user_message` (`id`, `role`, `content`, `parentId`).
        OWUI 0.12 inserts the turn server-side (`Chats.insert_chat_turn`)
        and answers 409 when a `chat_id` arrives without those ids. The
        user message is parented on the chat's current leaf so the reply
        continues the thread instead of starting a detached branch.
        """
        if history is None:
            history = []
        history.append({"role": "user", "content": message})
        body = {
            "model": model,
            "messages": history,
            "stream": stream,
            "chat_id": chat_id,
        }
        if chat_id:
            body["id"] = str(uuid.uuid4())
            body["user_message"] = {
                "id": str(uuid.uuid4()),
                "role": "user",
                "content": message,
                "parentId": self._chat_leaf_id(chat_id),
                "models": [model],
            }
        return self._post("/api/chat/completions", body, timeout=timeout)

    def _chat_leaf_id(self, chat_id: str) -> Optional[str]:
        """The chat's current last message: `history.currentId`, walked down
        to a childless leaf (the newest child at each step)."""
        chat_history = (self.get_chat(chat_id).get("chat") or {}).get("history") or {}
        messages = chat_history.get("messages") or {}
        node_id = chat_history.get("currentId")
        seen = set()
        while node_id in messages and node_id not in seen:
            seen.add(node_id)
            children = [c for c in messages[node_id].get("childrenIds") or [] if c in messages]
            if not children:
                break
            node_id = children[-1]
        return node_id if node_id in messages else None


# ── CLI ──────────────────────────────────────────────────────────────

def _main():
    if len(sys.argv) < 2:
        print("Usage: owui_api.py <action> [args...]")
        print("Actions: login, list-chats, get-chat <id>, set-title <id> <title>, delete-chat <id>, list-files, delete-all-files")
        sys.exit(1)

    c = OwuiClient.from_env()
    action = sys.argv[1]

    if action == "login":
        c.login()
        print("Token: ***")
    elif action == "list-chats":
        chats = c.list_chats()
        for ch in chats:
            print(f"{ch['id']} | {ch.get('title','')[:60]} | {ch.get('updated_at',0)}")
    elif action == "get-chat":
        chat = c.get_chat(sys.argv[2])
        print(json.dumps(chat, indent=2, ensure_ascii=False))
    elif action == "set-title":
        c.set_chat_title(sys.argv[2], sys.argv[3])
        print(f"✓ Title set for {sys.argv[2]}")
    elif action == "delete-chat":
        c.delete_chat(sys.argv[2])
        print(f"✓ Deleted {sys.argv[2]}")
    elif action == "list-files":
        files = c.list_files()
        for f in files:
            print(f"{f.get('id','')} | {f.get('filename','')} | {f.get('meta',{}).get('size',0)} bytes")
    elif action == "delete-all-files":
        count = c.delete_all_files()
        print(f"✓ Deleted {count} files")
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)


if __name__ == "__main__":
    _main()
