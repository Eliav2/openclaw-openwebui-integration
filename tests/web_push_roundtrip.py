"""The Web Push function against a local push service, with real encryption, in its
own interpreter (other suites stub `cryptography`). Prints one JSON result.

    python tests/web_push_roundtrip.py <scenario>

Scenarios: ok (two devices, both delivered), gone (one answers 410, one 500)."""
import asyncio
import base64
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import http_ece
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPLY = "SECRET-REPLY-TEXT the whole answer the agent wrote"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def device():
    """A browser's side of a subscription: its ECDH key and auth secret."""
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, os.urandom(16), pub


def main(scenario: str) -> None:
    devices = {name: device() for name in ("dev1", "dev2")}
    status = {"dev1": 201, "dev2": 201} if scenario == "ok" else {"dev1": 410, "dev2": 500}
    received = []

    class PushService(BaseHTTPRequestHandler):
        def do_POST(self):
            name = self.path.rsplit("/", 1)[-1]
            body = self.rfile.read(int(self.headers["Content-Length"]))
            key, auth, _ = devices[name]
            plain = http_ece.decrypt(body, private_key=key, auth_secret=auth, version="aes128gcm")
            received.append({"device": name, "payload": json.loads(plain), "bytes": len(body),
                             "headers": {k: self.headers.get(k) for k in
                                         ("TTL", "Urgency", "Topic", "Content-Encoding")},
                             "vapid": (self.headers.get("Authorization") or "").split(" ")[0]})
            self.send_response(status[name])
            self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), PushService)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/push"

    store = {"subscriptions": json.dumps([
        {"endpoint": f"{base}/{name}", "keys": {"p256dh": b64(pub), "auth": b64(auth)}, "device": name}
        for name, (_, auth, pub) in devices.items()])}

    class Functions:
        @staticmethod
        async def get_user_valves_by_id_and_user_id(fid, user_id):
            return None  # nothing left in user valves from an earlier version

    class Users:
        # The function's state is in the user's settings under its key (ELI-442).
        @staticmethod
        async def get_user_by_id(user_id):
            settings = {"ui": {}, "web_push:web_push": dict(store)} if user_id == "u1" else {}
            return types.SimpleNamespace(settings=types.SimpleNamespace(model_dump=lambda: settings))

        @staticmethod
        async def update_user_settings_by_id(user_id, updated):
            store.clear()
            store.update(updated["web_push:web_push"])

    for name, attrs in (("open_webui", {}), ("open_webui.models", {}),
                        ("open_webui.models.functions", {"Functions": Functions}),
                        ("open_webui.models.users", {"Users": Users})):
        sys.modules[name] = types.ModuleType(name)
        sys.modules[name].__dict__.update(attrs)

    vapid = ec.generate_private_key(ec.SECP256R1())
    keyfile = os.path.join(tempfile.mkdtemp(), "vapid_private.pem")
    with open(keyfile, "wb") as f:
        f.write(vapid.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))

    spec = importlib.util.spec_from_file_location("openclaw_web_push", os.path.join(ROOT, "push", "openclaw_web_push.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = mod.Event()
    fn.valves = fn.Valves(VAPID_PRIVATE_KEY_FILE=keyfile, VAPID_SUBJECT="mailto:test@example.com")
    event = {"event": "chat.finished", "source": "openclaw-pipe", "actor": {"id": "u1"},
             "data": {"user_id": "u1", "chat_id": "c1", "message_id": "m1", "model_id": "openclaw_gateway.main",
                      "title": "Trip plan", "url": "https://chat.example.com/c/c1", "message": REPLY}}
    asyncio.run(fn.event(event, __id__="web_push"))
    server.shutdown()
    print(json.dumps({"received": sorted(received, key=lambda r: r["device"]),
                      "left": [s["device"] for s in json.loads(store["subscriptions"])]}))


if __name__ == "__main__":
    main(sys.argv[1])
