"""ELI-166 (d): the ELI-115 reconnect storm, in a real browser.

0.12.0's page builds its socket with `auth: {token: localStorage.token}`,
read once at page load. After the page's token dies (an Open WebUI restart
with a new WEBUI_SECRET_KEY) and the user signs in again in the same page,
every reconnect still carries the dead token: the server accepts the connect
without a session, the chat join gets it server-disconnected, and the page
reconnects at once. 0.12.3 reads the token per connect (`auth: (cb) => ...`).

Steps: sign in (token A), open a chat; restart Open WebUI with a new secret
key (A dies); sign in again from inside the page (localStorage.token = B, as
the /auth page does, no reload); count the page's socket.io connects and
/api/version calls for WINDOW_S seconds. Prints one JSON line.
"""
import json
import os
import subprocess
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("OWUI_URL", "http://localhost:8080")
EMAIL, PASSWORD = os.environ["OWUI_EMAIL"], os.environ["OWUI_PASSWORD"]
WINDOW_S = int(os.environ.get("WINDOW_S", "60"))


def api(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def signin():
    return api("POST", "/api/v1/auths/signin", {"email": EMAIL, "password": PASSWORD})["token"]


def healthy():
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def restart_with_new_secret():
    image = subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}", "owui"],
                           capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["docker", "commit", "owui", "owui-snap"], check=True, capture_output=True)
    subprocess.run(["docker", "rm", "-f", "owui"], check=True, capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", "owui", "--network", "host",
                    "-e", f"WEBUI_SECRET_KEY=probe-{os.urandom(8).hex()}", "-e", "ENABLE_OLLAMA_API=false",
                    "-e", "ENABLE_OPENAI_API=false", "-e", "OFFLINE_MODE=true", "owui-snap"],
                   check=True, capture_output=True)
    t0 = time.time()
    while not healthy():
        if time.time() - t0 > 300:
            raise SystemExit("Open WebUI did not come back")
        time.sleep(2)
    return image


def main():
    a = signin()
    now = int(time.time())
    chat = api("POST", "/api/v1/chats/new", {"chat": {"title": "storm probe", "models": [], "history":
               {"messages": {}, "currentId": None}, "messages": [], "timestamp": now * 1000}}, a)["id"]
    ev = {"ws": [], "version": []}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(storage_state={"cookies": [], "origins": [{"origin": BASE, "localStorage": [
            {"name": "token", "value": a}]}]})
        page = ctx.new_page()
        page.on("websocket", lambda w: ev["ws"].append(time.time()))
        page.on("request", lambda r: ev["version"].append(time.time()) if r.url.endswith("/api/version") else None)
        page.goto(f"{BASE}/c/{chat}")
        page.wait_for_selector("#chat-input", timeout=60_000)
        time.sleep(5)
        before = {k: len(v) for k, v in ev.items()}
        image = restart_with_new_secret()
        try:
            api("GET", "/api/v1/auths/", token=a)
            a_dead = False
        except Exception:
            a_dead = True
        time.sleep(10)  # the page notices the restart and reconnects with token A
        b = signin()
        page.evaluate("(t) => { localStorage.token = t; }", b)  # what /auth does on sign-in
        t_login = time.time()
        url_at_login = page.url
        time.sleep(WINDOW_S)
        ws_after = [t for t in ev["ws"] if t >= t_login]
        ver_after = [t for t in ev["version"] if t >= t_login]
        out = {"image": image, "a_dead_after_restart": a_dead, "url_at_login": url_at_login,
               "before_restart": before, "window_s": WINDOW_S,
               "socket_connects_after_signin": len(ws_after), "api_version_after_signin": len(ver_after),
               "socket_connects_last_10s": sum(1 for t in ws_after if t >= t_login + WINDOW_S - 10),
               "url_end": page.url}
        print(json.dumps(out), flush=True)
        with open(os.environ.get("OUT", "storm.json"), "w") as f:
            json.dump(out, f)
        browser.close()


if __name__ == "__main__":
    main()
