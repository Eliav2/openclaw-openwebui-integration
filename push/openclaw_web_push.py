"""
title: Web Push notifications
author: eliavb43
author_url: https://github.com/Eliav2/openclaw-openwebui-integration
version: 0.1.0
requirements: pywebpush==2.0.3
description: Event function. Sends a Web Push notification to each of the user's subscribed devices when a reply finishes. Title and link only, never the reply.
"""
# Docs: docs/install/web-push.md. The page script (push/web/oc-push.js) keeps
# each device's subscription in this function's user valves; the service
# worker (push/web/oc-sw.js) shows the notification.
import asyncio
import base64
import hashlib
import json
import logging
import os

from pydantic import BaseModel, Field

log = logging.getLogger("openclaw_web_push")

# Open WebUI's event names are a closed catalog (events.py `event_name`), so the
# OpenClaw Pipe marks its own `chat.finished` by `source`.
PIPE_SOURCE = "openclaw-pipe"
# Push services take at most 4096 bytes per message after encryption.
MAX_PAYLOAD_BYTES = 3000
MAX_TITLE_CHARS = 120
# A push service that never answers: the send is given up. A safety net for a
# hung connection, never expected to expire.
PUSH_REQUEST_TIMEOUT_S = 10
# The push service says the subscription is gone (RFC 8030 section 7.3): drop it.
GONE_STATUSES = (404, 410)
DEFAULT_KEY_FILE = os.path.join(os.environ.get("DATA_DIR", "/app/backend/data"), "web-push", "vapid_private.pem")


def build_payload(event: dict) -> dict:
    """What the device gets: the chat's title and where to open it. Never the
    reply: `data.message` holds the whole reply text (Open WebUI 0.13.0 and
    the Pipe), and the push service and the lock screen are not the server."""
    data = event.get("data") or {}
    chat_id = str(data.get("chat_id") or "")
    title = str(data.get("title") or event.get("message") or "Reply finished")[:MAX_TITLE_CHARS]
    return {"v": 1, "kind": event.get("event"), "source": event.get("source"), "chat_id": chat_id,
            "message_id": data.get("message_id"), "title": title,
            "url": f"/c/{chat_id}" if chat_id else "/"}


def encode_payload(payload: dict) -> str:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if len(body.encode()) > MAX_PAYLOAD_BYTES:
        body = json.dumps({**payload, "title": payload["title"][:40]}, separators=(",", ":"), ensure_ascii=False)
    return body


def topic_for(chat_id: str) -> str:
    """RFC 8030 Topic (at most 32 base64url characters): a newer push for the
    same chat replaces one the push service still holds."""
    return base64.urlsafe_b64encode(hashlib.sha256(chat_id.encode()).digest()[:24]).decode().rstrip("=")


def parse_subscriptions(raw) -> list:
    try:
        subs = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
    except ValueError:
        return []
    return [s for s in subs if isinstance(s, dict) and s.get("endpoint") and (s.get("keys") or {}).get("p256dh")]


def should_send(event: dict, pipe_prefixes: list) -> bool:
    """`chat.finished` only. For an OpenClaw model, only the Pipe's own: Open
    WebUI's fires when the chat request ends, which for a reply the Pipe
    handed over is before the reply does, and the Pipe publishes every reply
    once it is saved (valve PUBLISH_CHAT_FINISHED)."""
    if event.get("event") != "chat.finished":
        return False
    model_id = str((event.get("data") or {}).get("model_id") or "")
    if any(model_id.startswith(p) for p in pipe_prefixes):
        return event.get("source") == PIPE_SOURCE
    return True


class Event:
    class Valves(BaseModel):
        VAPID_PRIVATE_KEY_FILE: str = Field(
            default=DEFAULT_KEY_FILE,
            description="Path, inside the Open WebUI container, of the VAPID private key (PEM). "
                        "Kept in a file because valves are stored unencrypted by default.")
        VAPID_SUBJECT: str = Field(
            default="",
            description="Contact the push services may use (mailto: or https: URL). Empty: "
                        "Open WebUI's WEBUI_URL.")
        TTL_SECONDS: int = Field(
            default=3600, ge=0,
            description="How long a push service keeps a notification for a device that is offline.")
        URGENCY: str = Field(
            default="normal",
            description="RFC 8030 Urgency: very-low, low, normal or high.")
        PIPE_MODEL_PREFIXES: str = Field(
            default="openclaw_gateway.",
            description="Comma list of model id prefixes whose replies come from the OpenClaw Pipe "
                        "(its function id plus a dot). For these, only the Pipe's own "
                        "'chat.finished' is sent. Turn on the Pipe's PUBLISH_CHAT_FINISHED.")

    class UserValves(BaseModel):
        subscriptions: str = Field(
            default="[]",
            description="Written by the page script: one Web Push subscription per device.")

    def __init__(self):
        self.valves = self.Valves()

    async def event(self, event: dict, __id__: str = "", **kwargs):
        prefixes = [p.strip() for p in self.valves.PIPE_MODEL_PREFIXES.split(",") if p.strip()]
        if not should_send(event, prefixes):
            return
        data = event.get("data") or {}
        user_id = data.get("user_id") or (event.get("actor") or {}).get("id")
        if not user_id:
            return
        from open_webui.models.functions import Functions

        fid = __id__ or "openclaw_web_push"
        valves = await Functions.get_user_valves_by_id_and_user_id(fid, user_id) or {}
        subs = parse_subscriptions(valves.get("subscriptions"))
        if not subs:
            return
        key = self._private_key()
        if key is None:
            return
        subject = await self._subject()
        body = encode_payload(build_payload(event))
        gone = await asyncio.to_thread(self.send_all, subs, body, topic_for(str(data.get("chat_id") or "")),
                                       key, subject)
        if gone:
            await self._drop(Functions, fid, user_id, gone)

    def _private_key(self):
        path = self.valves.VAPID_PRIVATE_KEY_FILE
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError as ex:
            log.error("web push: cannot read the VAPID key %s: %s", path, ex)
            return None

    async def _subject(self) -> str:
        if self.valves.VAPID_SUBJECT:
            return self.valves.VAPID_SUBJECT
        try:
            from open_webui.models.config import Config
            url = (await Config.get("webui.url") or "").rstrip("/")
        except Exception:
            url = ""
        return url or "mailto:webmaster@localhost"

    def send_all(self, subs: list, body: str, topic: str, key: bytes, subject: str) -> set:
        """Send `body` to each subscription; returns the endpoints the push
        service says are gone. `key`: the VAPID private key, PEM."""
        from py_vapid import Vapid
        from pywebpush import WebPushException, webpush

        try:
            vapid = Vapid.from_pem(key)
        except Exception as ex:
            log.error("web push: the VAPID key file is not a PEM private key: %r", ex)
            return set()
        gone = set()
        for sub in subs:
            info = {"endpoint": sub["endpoint"], "keys": sub["keys"]}
            try:
                webpush(subscription_info=info, data=body, vapid_private_key=vapid,
                        vapid_claims={"sub": subject}, ttl=self.valves.TTL_SECONDS,
                        headers={"Urgency": self.valves.URGENCY, "Topic": topic},
                        content_encoding="aes128gcm", timeout=PUSH_REQUEST_TIMEOUT_S)
                log.info("web push: sent to %s", _host(sub))
            except WebPushException as ex:
                status = getattr(ex.response, "status_code", None)
                if status in GONE_STATUSES:
                    gone.add(sub["endpoint"])
                    log.info("web push: subscription gone (%s), dropping %s", status, _host(sub))
                else:
                    log.warning("web push: %s answered %s: %s", _host(sub), status, str(ex)[:200])
            except Exception as ex:
                log.warning("web push: sending to %s failed: %r", _host(sub), ex)
        return gone

    async def _drop(self, Functions, fid: str, user_id: str, gone: set) -> None:
        # Read again: a device may have saved its subscription meanwhile.
        valves = await Functions.get_user_valves_by_id_and_user_id(fid, user_id) or {}
        subs = parse_subscriptions(valves.get("subscriptions"))
        keep = [s for s in subs if s["endpoint"] not in gone]
        if len(keep) != len(subs):
            await Functions.update_user_valves_by_id_and_user_id(fid, user_id, {**valves, "subscriptions": json.dumps(keep)})


def _host(sub: dict) -> str:
    return str(sub.get("endpoint", "")).split("/")[2:3][0] if "//" in str(sub.get("endpoint", "")) else "?"
