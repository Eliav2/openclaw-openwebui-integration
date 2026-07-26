#!/usr/bin/env python3
"""
OWUI API helper — reusable module for OWUI REST API operations.
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
        """Create from OWUI_URL, OWUI_EMAIL, OWUI_PASSWORD env vars."""
        return OwuiClient(
            os.environ.get("OWUI_URL", "http://localhost:8080"),
            os.environ.get("OWUI_EMAIL", ""),
            os.environ.get("OWUI_PASSWORD", ""),
        )

    # ── Low-level ────────────────────────────────────────────────────

    def _json(self, resp) -> dict:
        return json.loads(resp.read().decode("utf-8", "replace"))

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}", headers=self._auth_headers()
        )
        return self._json(urllib.request.urlopen(req, timeout=15))

    def _post(self, path: str, data: dict) -> dict:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            method="POST",
        )
        return self._json(urllib.request.urlopen(req, timeout=15))

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

        Note: messages sent via the completions API may not persist to
        the chat DB — this is for testing pipe behavior, not full chat UX.
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
        return self._post("/api/chat/completions", body)


# ── CLI ──────────────────────────────────────────────────────────────

def _main():
    if len(sys.argv) < 2:
        print("Usage: owui_api.py <action> [args...]")
        print("Actions: login, list-chats, get-chat <id>, set-title <id> <title>, delete-chat <id>, list-files, delete-all-files")
        sys.exit(1)

    c = OwuiClient.from_env()
    action = sys.argv[1]

    if action == "login":
        t = c.login()
        print(f"Token: ***")
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
