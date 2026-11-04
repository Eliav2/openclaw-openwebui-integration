// oc-push.js: the Web Push page script for Open WebUI (docs/install/web-push.md).
// Serve it as Open WebUI's /static/loader.js (every page loads that file), or
// append it to yours. Replace the two placeholders when you serve it:
//   __VAPID_PUBLIC_KEY__  the VAPID public key, base64url (not a secret)
//   __FUNCTION_ID__       the id of the Web Push event function in Open WebUI
// It registers /oc-sw.js and keeps this device's push subscription in that
// function's user valves, through Open WebUI's own API with the user's token.
// In a chat it adds a bell (mute this chat for every device of the user) and
// puts a reply that could not be sent from a notification into the message box.
(() => {
  const VAPID = "__VAPID_PUBLIC_KEY__", FN = "__FUNCTION_ID__";
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) return;
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  // iOS has Web Push only in a web app added to the Home Screen.
  if (ios && !standalone) return;

  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch (_) { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} },
  };
  const keyBytes = (b64) => Uint8Array.from(atob(b64.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  const sameKey = (buf) => {
    if (!buf) return false;
    const a = new Uint8Array(buf), b = keyBytes(VAPID);
    return a.length === b.length && a.every((x, i) => x === b[i]);
  };
  const call = (path, body) => fetch(path, {
    method: body ? "POST" : "GET",
    headers: { Authorization: "Bearer " + store.get("token"), "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(path + " " + r.status))));
  const valves = (suffix, body) => call("/api/v1/functions/id/" + FN + "/valves/user" + suffix, body);
  // Open WebUI's update replaces every user valve: read them all right before
  // the write and change one. Two devices writing at the same moment can still
  // lose one write; the next write of either heals it.
  async function updateValves(change) {
    const now = (await valves("")) || {};
    const next = { ...now, ...change(now) };
    await valves("/update", next);
    return next;
  }
  const parse = (raw, empty) => { try { return JSON.parse(raw || empty) || JSON.parse(empty); } catch (_) { return JSON.parse(empty); } };

  async function save(sub) {
    const s = sub.toJSON();
    let result = "kept";
    let device = store.get("ocPushDevice");
    if (!device) { device = crypto.randomUUID(); store.set("ocPushDevice", device); }
    await updateValves((now) => {
      let list = parse(now.subscriptions, "[]");
      if (list.some((x) => x.endpoint === s.endpoint)) return {};
      result = "saved";
      // This device's earlier subscription is replaced, not kept beside the new one.
      list = list.filter((x) => x.device !== device);
      list.push({ ...s, device, ua: navigator.userAgent.slice(0, 120), at: Date.now() });
      return { subscriptions: JSON.stringify(list) };
    });
    return result;
  }

  async function ensure() {
    if (!store.get("token")) return "no-token";
    // Open WebUI unregisters every service worker when it updates; this
    // registers ours again on the next page load.
    const reg = await navigator.serviceWorker.register("/oc-sw.js", { scope: "/" });
    await navigator.serviceWorker.ready;
    if (Notification.permission !== "granted") return "no-permission";
    const user = (await call("/api/v1/auths/"))?.id;
    let sub = await reg.pushManager.getSubscription();
    // Another user signed in on this device, or the key changed: a new
    // subscription, so the old one stops receiving (its push service answers
    // 410 and the function drops it).
    if (sub && (store.get("ocPushUser") !== user || !sameKey(sub.options?.applicationServerKey))) {
      await sub.unsubscribe();
      sub = null;
    }
    sub ||= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(VAPID) });
    store.set("ocPushUser", user);
    return save(sub);
  }

  // The permission prompt needs a click (iOS requires one).
  async function enable() {
    return (await Notification.requestPermission()) === "granted" ? ensure() : "denied";
  }

  const pill = "position:fixed;top:60px;left:50%;transform:translateX(-50%);z-index:9999;display:flex;gap:4px;" +
    "font:14px system-ui,sans-serif;background:#111;color:#fff;border-radius:999px;padding:4px;box-shadow:0 2px 8px #0004";

  function offer() {
    if (Notification.permission !== "default" || store.get("ocPushOffer") === "dismissed" || !store.get("token") ||
      document.getElementById("oc-push-offer")) return;
    const bar = document.createElement("div");
    bar.id = "oc-push-offer";
    bar.setAttribute("role", "region");
    bar.setAttribute("aria-label", "Notifications");
    bar.style.cssText = pill;
    const btn = (text, label) => {
      const b = document.createElement("button");
      b.textContent = text;
      b.setAttribute("aria-label", label);
      b.style.cssText = "background:none;border:0;color:inherit;font:inherit;padding:6px 10px;border-radius:999px;cursor:pointer;white-space:nowrap";
      bar.appendChild(b);
      return b;
    };
    btn("Enable notifications", "Enable notifications for replies").onclick = async () => {
      bar.remove();
      window.ocPushState = await enable().catch((e) => "error:" + e.message);
      mutes = null;
      chatPage();
    };
    btn("×", "Dismiss").onclick = () => { store.set("ocPushOffer", "dismissed"); bar.remove(); };
    document.body.appendChild(bar);
  }

  // A short line at the top of the page (a reply that was not sent, a failed mute).
  function notice(text) {
    document.getElementById("oc-push-notice")?.remove();
    const n = document.createElement("div");
    n.id = "oc-push-notice";
    n.setAttribute("role", "status");
    n.dir = "auto";
    n.style.cssText = pill + ";padding:8px 14px;max-width:min(90vw,420px);text-align:center";
    n.textContent = text;
    n.onclick = () => n.remove();
    document.body.appendChild(n);
    setTimeout(() => n.remove(), 8000);
  }

  // --- In a chat: the mute bell, and a reply that came back from a notification.

  const chatIdNow = () => (location.pathname.match(/^\/c\/([^/]+)/) || [])[1] || "";
  let mutes = null, hasDevices = false;
  const muteUntil = (id) => {
    const until = mutes?.[id];
    if (typeof until !== "number") return null;
    return until === 0 || until * 1000 > Date.now() ? until : null;
  };

  async function loadMutes() {
    const v = (await valves("")) || {};
    mutes = parse(v.mutes, "{}");
    hasDevices = parse(v.subscriptions, "[]").length > 0;
  }

  // seconds: 0 always, null unmute.
  async function setMute(id, seconds) {
    const next = await updateValves((now) => {
      const m = parse(now.mutes, "{}");
      const nowS = Math.floor(Date.now() / 1000);
      for (const k of Object.keys(m)) if (m[k] !== 0 && m[k] <= nowS) delete m[k];   // expired
      if (seconds === null) delete m[id];
      else m[id] = seconds ? nowS + seconds : 0;
      return { mutes: JSON.stringify(m) };
    });
    mutes = parse(next.mutes, "{}");
    drawBell();
  }

  const BELL = '<path stroke-linecap="round" stroke-linejoin="round" d="M14.857 17.082a23.848 23.848 0 0 0 5.454-1.31A8.967 8.967 0 0 1 18 9.75V9A6 6 0 0 0 6 9v.75a8.967 8.967 0 0 1-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 0 1-5.714 0m5.714 0a3 3 0 1 1-5.714 0"/>';
  const SLASH = '<path stroke-linecap="round" d="M3.5 3.5l17 17"/>';
  const fmt = (until) => until === 0 ? "Muted" :
    "Muted until " + new Date(until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  function drawBell() {
    const b = document.getElementById("oc-push-bell");
    if (!b) return;
    const until = muteUntil(b.dataset.chat);
    const label = until === null ? "Notifications on" : fmt(until);
    b.setAttribute("aria-label", label + ": change");
    b.title = label;
    b.dataset.muted = until === null ? "" : String(until);
    b.querySelector("svg").innerHTML = BELL + (until === null ? "" : SLASH);
  }

  function menu(b) {
    document.getElementById("oc-push-mute-menu")?.remove();
    const id = b.dataset.chat;
    const m = document.createElement("div");
    m.id = "oc-push-mute-menu";
    m.setAttribute("role", "menu");
    m.dir = document.documentElement.dir || "auto";
    const r = b.getBoundingClientRect();
    const rtl = getComputedStyle(b).direction === "rtl";
    const dark = document.documentElement.classList.contains("dark");
    m.style.cssText = "position:fixed;z-index:10000;min-width:170px;padding:4px;border-radius:12px;" +
      `font:14px system-ui,sans-serif;background:${dark ? "#222" : "#fff"};color:${dark ? "#eee" : "#111"};` +
      "box-shadow:0 4px 16px #0003;" + `top:${Math.round(r.bottom + 6)}px;` +
      (rtl ? `left:${Math.round(r.left)}px` : `right:${Math.round(innerWidth - r.right)}px`);
    const until = muteUntil(id);
    const head = document.createElement("div");
    head.textContent = until === null ? "Notifications on" : fmt(until);
    head.style.cssText = "padding:6px 10px;opacity:.6;font-size:12px";
    m.appendChild(head);
    const item = (text, seconds) => {
      const i = document.createElement("button");
      i.setAttribute("role", "menuitem");
      i.textContent = text;
      i.style.cssText = "display:block;width:100%;text-align:start;background:none;border:0;color:inherit;font:inherit;padding:8px 10px;border-radius:8px;cursor:pointer";
      i.onclick = async () => {
        close();
        await setMute(id, seconds).catch(() => notice("Could not change notifications for this chat."));
      };
      m.appendChild(i);
    };
    item("Mute for 1 hour", 3600);
    item("Mute for 8 hours", 8 * 3600);
    item("Mute always", 0);
    if (until !== null) item("Unmute", null);
    const away = (e) => {
      if (e.type === "keydown" ? e.key === "Escape" : !m.contains(e.target) && !b.contains(e.target)) close();
    };
    function close() {
      m.remove();
      removeEventListener("pointerdown", away, true);
      removeEventListener("keydown", away, true);
    }
    addEventListener("pointerdown", away, true);
    addEventListener("keydown", away, true);
    document.body.appendChild(m);
  }

  // Before Open WebUI's "Chat actions" button, styled like it (its classes).
  // Open WebUI redraws its header freely: an observer puts the bell back.
  function placeBell() {
    const id = chatIdNow();
    const anchor = document.getElementById("chat-context-menu-button");
    const old = document.getElementById("oc-push-bell");
    if (!id || !anchor || !hasDevices || mutes === null) { old?.remove(); return; }
    const spot = anchor.parentElement;
    if (old && old.dataset.chat === id && old.nextElementSibling === spot) return;
    old?.remove();
    const b = document.createElement("button");
    b.id = "oc-push-bell";
    b.type = "button";
    b.dataset.chat = id;
    b.className = anchor.className;
    b.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" style="width:1.125rem;height:1.125rem"></svg>';
    b.onclick = (e) => { e.stopPropagation(); menu(b); };
    spot.before(b);
    drawBell();
  }

  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    queueMicrotask(() => { queued = false; placeBell(); });
  }).observe(document.documentElement, { childList: true, subtree: true });

  // A reply from a notification: "compose" focuses the message box, "unsent"
  // puts the text that could not be sent into it and says so.
  async function pendingReply() {
    const id = chatIdNow();
    const q = new URLSearchParams(location.search);
    const mode = q.get("oc-reply");
    if (!id || !mode) return;
    q.delete("oc-reply");
    history.replaceState(history.state, "", location.pathname + (q.size ? "?" + q : "") + location.hash);
    let text = "";
    try {
      const drafts = await caches.open("oc-push-drafts");
      const r = await drafts.match("/oc-push/draft/" + id);
      if (r) { text = (await r.json()).text || ""; await drafts.delete("/oc-push/draft/" + id); }
    } catch (_) {}
    const box = await new Promise((ok) => {
      const find = () => document.querySelector("#chat-input");
      if (find()) return ok(find());
      const o = new MutationObserver(() => { if (find()) { o.disconnect(); ok(find()); } });
      o.observe(document.documentElement, { childList: true, subtree: true });
      setTimeout(() => { o.disconnect(); ok(find()); }, 15000);
    });
    if (box) {
      box.focus();
      if (text) document.execCommand("insertText", false, text);
    }
    if (text) notice("Your reply was not sent. It is in the message box.");
    window.ocPushReply = { mode, text };
  }

  async function chatPage() {
    if (!store.get("token")) return;
    if (mutes === null) await loadMutes().catch(() => {});
    placeBell();
    pendingReply();
  }

  // One run at a time; each run is for the token it started with.
  let running = null, ranFor;
  const run = () => running ||= (ranFor = store.get("token"), ensure())
    .catch((e) => "error:" + e.message).then((state) => {
      running = null;
      // The token changed while this run was on its way: the result is stale.
      const t = store.get("token");
      if (t && t !== ranFor) return run();
      window.ocPushState = state;
      if (state === "no-permission") offer();
      chatPage();
      return state;
    });

  // Open WebUI signs in, signs up and signs out without a page load: it stores
  // the token, then navigates on the client (SvelteKit calls history.pushState
  // or replaceState). Each navigation checks whether the token changed since the
  // last run, and runs again if so.
  const navigated = () => {
    const t = store.get("token");
    if (t && t !== ranFor) run();
    else chatPage();
  };
  for (const k of ["pushState", "replaceState"]) {
    const orig = history[k];
    history[k] = function (...args) {
      const r = orig.apply(this, args);
      navigated();
      return r;
    };
  }
  addEventListener("popstate", navigated);

  // An app left open in the background picks up a new service worker when it
  // comes back to the front (install, skipWaiting, claim), without a reload.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    navigator.serviceWorker.getRegistration("/").then((r) => r && r.update()).catch(() => {});
    // Mutes may have changed on another device.
    if (store.get("token") && chatIdNow()) loadMutes().then(drawBell).catch(() => {});
  });

  window.ocPushEnable = enable;
  window.ocPushMute = (seconds, id = chatIdNow()) => setMute(id, seconds);
  window.ocPushReady = run();
})();
