#!/usr/bin/env python3
"""How long the Status dialog takes to open and fill, by chat size.

Runs against the E2E stack (the fake model, the installed Pipe and Status
Action). For each chat size it makes one real chat whose run spawned a
sub-agent, inflates the chat's history to the size with filler turns placed
before that run, then opens the Status dialog from the message toolbar and
from the composer button a few times each and records, per open:

  * the click to the dialog's shell, and to each section filled;
  * every request the page made for it: its body size and its time;
  * Open WebUI's own chat reads and writes around it.

Prints one JSON line per open and a summary table (median of the opens).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "screenshots"))
import capture as cap  # noqa: E402  (api, signin, signed_in_page, dismiss_popups)
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402

SECTIONS = ("context", "limits", "background", "subagents", "agents")
ROOT = "#openclaw-status-modal-root"

# Installed once per page: when the dialog's shell and each section appear.
OBSERVER_JS = r"""() => {
  if (window.__perf) return;
  const P = window.__perf = {marks: {}, start: null, resources: []};
  const sections = %s;
  const tick = () => {
    if (P.start === null) return;
    const now = performance.now();
    if (!P.marks.shell && document.querySelector('%s')) P.marks.shell = now;
    for (const s of sections) {
      if (P.marks[s]) continue;
      const el = document.getElementById('openclaw-status-section-' + s);
      // Filled: nothing loading, or a closed fold waiting for its tap.
      const fold = el && el.querySelector('[data-openclaw-fold][data-open="false"]');
      if (el && (fold || !el.querySelector('[data-openclaw-loading]'))) P.marks[s] = now;
      if (s === 'subagents' && P.foldAt && !P.marks.fold && el && !el.querySelector('[data-openclaw-loading]'))
        P.marks.fold = now;
    }
  };
  new MutationObserver(tick).observe(document.documentElement, {childList: true, subtree: true});
  setInterval(tick, 25);
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) {
      if (/\/api\/(chat\/actions|v1\/chats\/)/.test(e.name))
        P.resources.push({url: e.name.replace(location.origin, ''), start: e.startTime,
                          end: e.responseEnd, bytesIn: e.transferSize});
    }
  }).observe({type: 'resource', buffered: false});
}""" % (json.dumps(SECTIONS), ROOT)

CLICK_JS = r"""(sel) => {
  const P = window.__perf;
  P.marks = {}; P.resources = []; P.foldAt = null;
  const btn = [...document.querySelectorAll(sel)].pop();
  if (!btn) return 'no button ' + sel;
  P.start = performance.now();
  btn.click();
  return null;
}"""


# Taps the Subagents fold when it is closed with rows not read yet.
FOLD_JS = r"""() => {
  const el = document.getElementById('openclaw-status-section-subagents');
  const head = el && el.querySelector('[data-openclaw-fold][data-open="false"] [data-openclaw-fold-head]');
  if (!head || !el.querySelector('[data-openclaw-loading]')) return false;
  window.__perf.foldAt = performance.now();
  head.click();
  return true;
}"""

# What an open that never filled looks like: the dialog, and each section's text.
STUCK_JS = r"""() => {
  const root = document.querySelector('%s');
  const out = {modal: !!root, sections: {}};
  for (const s of %s) {
    const el = document.getElementById('openclaw-status-section-' + s);
    out.sections[s] = el ? (el.querySelector('[data-openclaw-loading]') ? 'loading: ' : '')
      + el.innerText.slice(0, 160) : null;
  }
  return out;
}""" % (ROOT, json.dumps(SECTIONS))


def filler(n_bytes: int) -> str:
    para = ("The forecast service keeps a rolling window of readings and a small cache in front of the "
            "provider, so a burst of requests never reaches it twice. ") * 8
    out = []
    while sum(len(p) for p in out) < n_bytes:
        out.append(para)
    return "\n\n".join(out)[:n_bytes]


def spawn_chat(token: str) -> str:
    """A new chat with one real turn whose run spawns a sub-agent."""
    model = cap.MODEL
    now = int(time.time())
    uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
    prompt = "E2E_SPAWN one helper"
    user = {"id": uid, "parentId": None, "childrenIds": [aid], "role": "user", "content": prompt,
            "timestamp": now, "models": [model]}
    asst = {"id": aid, "parentId": uid, "childrenIds": [], "role": "assistant", "content": "",
            "model": model, "timestamp": now, "done": False}
    chat = {"title": "perf", "models": [model], "history": {"messages": {uid: user, aid: asst}, "currentId": aid},
            "messages": [user, asst], "files": [], "tags": [], "timestamp": now * 1000}
    chat_id = cap.api("POST", "/api/v1/chats/new", {"chat": chat}, token)["id"]
    cap.api("POST", "/api/chat/completions", {
        "model": model, "messages": [{"role": "user", "content": prompt}], "stream": True,
        "chat_id": chat_id, "id": aid, "session_id": f"perf-{uuid.uuid4().hex[:8]}",
        "parent_id": uid, "user_message": user}, token)
    deadline = time.time() + 180
    while time.time() < deadline:
        msg = cap.api("GET", f"/api/v1/chats/{chat_id}", token=token)["chat"]["history"]["messages"][aid]
        if msg.get("done") and "E2E_SPAWNED" in (msg.get("content") or ""):
            return chat_id
        time.sleep(2)
    raise TimeoutError(f"the spawn turn in {chat_id} did not finish")


def inflate(token: str, chat_id: str, size: int) -> int:
    """Filler turns before the chat's real turn, to about `size` bytes of chat."""
    got = cap.api("GET", f"/api/v1/chats/{chat_id}", token=token)["chat"]
    hist = got["history"]
    real = [m for m in hist["messages"].values() if m["parentId"] is None][0]
    per = 50_000
    pairs = max(0, size // (2 * per))
    prev = None
    model = cap.MODEL
    now = int(time.time()) - pairs * 60
    for i in range(pairs):
        u, a = str(uuid.uuid4()), str(uuid.uuid4())
        hist["messages"][u] = {"id": u, "parentId": prev, "childrenIds": [a], "role": "user",
                               "content": f"Question {i}: " + filler(per // 10), "timestamp": now + i * 60,
                               "models": [model]}
        hist["messages"][a] = {"id": a, "parentId": u, "childrenIds": [], "role": "assistant",
                               "content": filler(per), "model": model, "timestamp": now + i * 60 + 30,
                               "done": True}
        if prev:
            hist["messages"][prev]["childrenIds"] = [u]
        prev = a
    if prev:
        hist["messages"][prev]["childrenIds"] = [real["id"]]
        real["parentId"] = prev
    chain, at = [], hist["currentId"]
    while at:
        chain.insert(0, hist["messages"][at])
        at = hist["messages"][at]["parentId"]
    got["messages"] = chain
    cap.api("POST", f"/api/v1/chats/{chat_id}", {"chat": got}, token)
    return len(json.dumps(got))


# Network and CPU of a phone on mobile data, through Chrome DevTools.
PROFILES = {
    "lan": None,
    "phone": {"net": {"offline": False, "latency": 60, "downloadThroughput": 4_000_000,
                      "uploadThroughput": 1_000_000}, "cpu": 4},
}


def throttle(page, profile: str) -> None:
    cfg = PROFILES[profile]
    if not cfg:
        return
    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.emulateNetworkConditions", cfg["net"])
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": cfg["cpu"]})


def open_once(page, path: str, chat_id: str, log: list) -> dict:
    sel = 'button[aria-label="OpenClaw Status"]' if path == "toolbar" else 'button[aria-label="OpenClaw status"]'
    if path == "toolbar":
        page.locator(".chat-assistant").last.hover()
        page.locator(sel).last.wait_for(timeout=30_000)
    else:
        page.locator(sel).wait_for(timeout=60_000)
    log.clear()
    clicked = time.time() * 1000
    err = page.evaluate(CLICK_JS, sel)
    if err:
        raise RuntimeError(err)
    stuck = None
    try:
        page.wait_for_function("() => { const m = window.__perf.marks; return %s; }"
                               % " && ".join(f"m.{s}" for s in SECTIONS), timeout=60_000)
    except PlaywrightTimeoutError:
        # Record what never filled instead of aborting the run.
        stuck = page.evaluate(STUCK_JS)
    fold_text = None
    if stuck is None and page.evaluate(FOLD_JS):
        # The folded Subagents section, tapped: its rows come from a follow-up
        # request (mode "subagent-rows").
        try:
            page.wait_for_function("() => window.__perf.marks.fold", timeout=30_000)
        except PlaywrightTimeoutError:
            pass
        fold_text = page.evaluate(
            "() => (document.getElementById('openclaw-status-section-subagents') || {}).innerText || ''")[:200]
    perf = page.evaluate("() => window.__perf")
    sub = page.evaluate("() => (document.getElementById('openclaw-status-section-subagents') || {}).innerText || ''")
    t0 = perf["start"]
    marks = {k: round(v - t0) for k, v in perf["marks"].items()}
    if perf.get("foldAt"):
        marks["foldAt"] = round(perf["foldAt"] - t0)
    # Closing ends the open request; Open WebUI then saves the whole chat for a
    # toolbar action. Wait for the requests to settle.
    page.keyboard.press("Escape")
    page.wait_for_timeout(4000)
    reqs = [{**q, "start": round(q["start"] - clicked), "end": round(q["end"] - clicked) if q["end"] else None}
            for q in log]
    out = {"path": path, "marks": marks, "requests": reqs, "subagents": sub[:200], "fold": fold_text}
    if stuck is not None:
        out["stuck"] = stuck
        page.reload()
        page.locator(".chat-assistant").last.wait_for(timeout=120_000)
        page.evaluate(OBSERVER_JS)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="0,2000000,13000000")
    ap.add_argument("--opens", type=int, default=3)
    ap.add_argument("--out", default="")
    ap.add_argument("--profiles", default="lan,phone")
    args = ap.parse_args(argv)
    token = cap.signin()
    cap.quiet_background_tasks(token)
    results = []
    with cap.sync_playwright() as p:
        browser = p.chromium.launch()
        for size in [int(s) for s in args.sizes.split(",")]:
            chat_id = spawn_chat(token)
            actual = inflate(token, chat_id, size)
            print(f"chat {chat_id}: {actual} bytes", flush=True)
            page = cap.signed_in_page(browser, token, cap.DESKTOP)
            log: list = []
            console: list = []

            def done(r, failed=False):
                if not ("/api/chat/actions/" in r.url or "/api/v1/chats/" in r.url):
                    return
                t = r.timing
                sizes = {} if failed else r.sizes()
                log.append({"url": r.method + " " + r.url.split("8080", 1)[-1].split("?")[0], "start": t["startTime"],
                            "end": t["startTime"] + t["responseEnd"] if t["responseEnd"] > 0 else None,
                            "bytesOut": sizes.get("requestBodySize"), "bytesIn": sizes.get("responseBodySize"),
                            "failed": r.failure if failed else None})
            page.on("requestfinished", done)
            page.on("requestfailed", lambda r: done(r, True))
            page.on("console", lambda m: console.append(m.text) if m.type in ("error", "warning") else None)
            t = time.time()
            page.goto(f"{cap.BASE}/c/{chat_id}")
            page.locator(".chat-assistant").last.wait_for(timeout=120_000)
            print(f"  chat page loaded in {time.time() - t:.1f}s", flush=True)
            page.evaluate(OBSERVER_JS)
            for profile in args.profiles.split(","):
                throttle(page, profile)
                for path in ("toolbar", "composer"):
                    for i in range(args.opens):
                        r = {"size": actual, "profile": profile, "chat": chat_id, "n": i,
                             **open_once(page, path, chat_id, log)}
                        print(json.dumps(r), flush=True)
                        results.append(r)
            if console:
                print("  console: " + json.dumps(console[-10:])[:1500], flush=True)
            page.context.close()
        browser.close()
    rows = ["| chat | net | path | shell ms | " + " | ".join(SECTIONS)
            + " | fold tap | bytes up (action) | chat bytes moved | requests |",
            "|---|---|---|---|" + "---|" * len(SECTIONS) + "---|---|---|---|"]
    for size in sorted({r["size"] for r in results}):
        for profile in args.profiles.split(","):
            for path in ("toolbar", "composer"):
                rs = [r for r in results if r["size"] == size and r["path"] == path and r["profile"] == profile]
                if not rs:
                    continue
                med = lambda k: round(statistics.median([r["marks"].get(k, -1) for r in rs]))  # noqa: E731
                up = max([q["bytesOut"] or 0 for r in rs for q in r["requests"] if "/chat/actions/" in q["url"]] or [0])
                moved = max(sum((q["bytesOut"] or 0) + (q["bytesIn"] or 0) for q in r["requests"]
                                if "/v1/chats/" in q["url"]) for r in rs)
                n = max(len(r["requests"]) for r in rs)
                taps = [r["marks"]["fold"] - r["marks"]["foldAt"] for r in rs if "fold" in r["marks"]]
                tapped = [r for r in rs if r.get("fold") is not None]
                tap = (f"{round(statistics.median(taps))}" if taps else "never") if tapped else "-"
                rows.append(f"| {size / 1e6:.2f} MB | {profile} | {path} | {med('shell')} | "
                            + " | ".join(str(med(s)) for s in SECTIONS) + f" | {tap} | {up} | {moved} | {n} |")
    table = "\n".join(rows)
    print(table)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=1)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write("### Status dialog open\n\n" + table + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
