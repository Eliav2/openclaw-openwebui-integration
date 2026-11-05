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

  // With no network, while the service worker opens the app from what it kept
  // (offline open on: its app cache is there), the chats are read-only: a pill
  // says so and the message box is locked (inert: no typing, no keyboard, no
  // send), so nothing typed offline is lost on a send that cannot go. Text already
  // in the box stays there and can be sent once the network is back.
  const OFFLINE_PILL = "oc-offline";
  const KEPT_APP = "oc-shell-v1";   // oc-sw.js SHELL
  const LOCKED = "ocOfflineLocked";   // data-oc-offline-locked on the locked composer
  const LOCKED_OPACITY = "0.5";
  const composer = () => document.getElementById("chat-input")?.closest("form");
  function lockComposer(on) {
    for (const f of document.querySelectorAll("[data-oc-offline-locked]")) {
      if (on && f === composer()) continue;
      f.inert = false;
      f.style.opacity = "";
      delete f.dataset[LOCKED];
    }
    const f = on && composer();
    if (!f || f.dataset[LOCKED]) return;
    f.inert = true;
    f.style.opacity = LOCKED_OPACITY;
    f.dataset[LOCKED] = "1";
  }
  // Open WebUI draws a new message box on each chat, and its first render can
  // replace what is in the page: both again while offline.
  const relock = new MutationObserver(() => { lockComposer(true); showPill(); });
  async function offlinePill() {
    const offline = !navigator.onLine && await caches.has(KEPT_APP).catch(() => false) && !navigator.onLine;
    lockComposer(offline);
    if (!offline) {
      relock.disconnect();
      return document.getElementById(OFFLINE_PILL)?.remove();
    }
    relock.observe(document.documentElement, { childList: true, subtree: true });
    showPill();
  }
  function showPill() {
    if (document.getElementById(OFFLINE_PILL) || !document.body) return;
    const p = document.createElement("div");
    p.id = OFFLINE_PILL;
    p.setAttribute("role", "status");
    p.style.cssText = pill + ";padding:6px 14px;pointer-events:none";
    p.textContent = "Offline: saved chats, read-only";
    document.body.appendChild(p);
  }
  addEventListener("online", offlinePill);
  addEventListener("offline", offlinePill);
  offlinePill();

  // --- In a chat: the mute bell, and a reply that came back from a notification.

  const VISIBLE = "visible";
  const chatIdNow = () => (location.pathname.match(/^\/c\/([^/]+)/) || [])[1] || "";
  // The two mute lists in the function's user valves, {id: until} (0 always):
  // chats, and folders (a folder's mute covers the folders inside it).
  const CHATS = "mutes", FOLDERS = "folder_mutes";
  const HOUR_S = 3600;
  const MUTE_FOR = [["for 1 hour", HOUR_S], ["for 8 hours", 8 * HOUR_S], ["always", 0]];
  let mutes = null, hasDevices = false;
  // This chat's folder and the user's folders, read when the bell is placed.
  let chatFolder = { chat: "", folder: null }, folders = {};
  const until = (list, id) => {
    const u = mutes?.[list]?.[id];
    if (typeof u !== "number") return null;
    return u === 0 || u * 1000 > Date.now() ? u : null;
  };
  // The muted folder holding this chat's folder (itself or one above it).
  function mutedFolder(id) {
    for (const seen = new Set(); id && !seen.has(id); id = folders[id]?.parent_id) {
      if (until(FOLDERS, id) !== null) return id;
      seen.add(id);
    }
    return null;
  }

  // Open WebUI navigates a few times while it starts (each runs chatPage): they
  // share the one read on its way instead of each asking the server.
  let loadingMutes = null;
  function loadMutes() {
    return loadingMutes ||= valves("").then((v) => {
      v ||= {};
      mutes = { [CHATS]: parse(v[CHATS], "{}"), [FOLDERS]: parse(v[FOLDERS], "{}") };
      hasDevices = parse(v.subscriptions, "[]").length > 0;
    }).finally(() => { loadingMutes = null; });
  }

  async function loadFolders() {
    folders = Object.fromEntries(((await call("/api/v1/folders/")) || []).map((f) => [f.id, f]));
  }

  async function loadFolder(id) {
    const folder = (await call("/api/v1/chats/" + id))?.folder_id || null;
    if (folder) await loadFolders();
    chatFolder = { chat: id, folder };
  }

  // seconds: 0 always, null unmute.
  async function setMute(list, id, seconds) {
    const next = await updateValves((now) => {
      const m = parse(now[list], "{}");
      const nowS = Math.floor(Date.now() / 1000);
      for (const k of Object.keys(m)) if (m[k] !== 0 && m[k] <= nowS) delete m[k];   // expired
      if (seconds === null) delete m[id];
      else m[id] = seconds ? nowS + seconds : 0;
      return { [list]: JSON.stringify(m) };
    });
    mutes = { ...mutes, [list]: parse(next[list], "{}") };
    drawBell();
    window.ocPushMuted = { list, id, seconds };   // for tests and tools
  }

  const BELL = '<path stroke-linecap="round" stroke-linejoin="round" d="M14.857 17.082a23.848 23.848 0 0 0 5.454-1.31A8.967 8.967 0 0 1 18 9.75V9A6 6 0 0 0 6 9v.75a8.967 8.967 0 0 1-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 0 1-5.714 0m5.714 0a3 3 0 1 1-5.714 0"/>';
  const SLASH = '<path stroke-linecap="round" d="M3.5 3.5l17 17"/>';
  // In the user's locale; the day too when it is not today (an 8 hour mute set in the evening).
  const CLOCK = { hour: "2-digit", minute: "2-digit" };
  const MS_PER_S = 1000;
  const fmt = (until) => {
    if (until === 0) return "Muted";
    const d = new Date(until * MS_PER_S);
    const today = d.toDateString() === new Date().toDateString();
    return "Muted until " + (today ? d.toLocaleTimeString([], CLOCK) : d.toLocaleString([], { weekday: "short", ...CLOCK }));
  };

  const folderName = (id) => folders[id]?.name || "this folder";
  // What the bell says: the chat's own mute first, else its folder's.
  function bellState(id) {
    const own = until(CHATS, id);
    if (own !== null) return { muted: own, label: fmt(own) };
    const f = chatFolder.chat === id ? mutedFolder(chatFolder.folder) : null;
    if (f) return { muted: until(FOLDERS, f), label: fmt(until(FOLDERS, f)) + " (folder " + folderName(f) + ")" };
    return { muted: null, label: "Notifications on" };
  }

  function drawBell() {
    const b = document.getElementById("oc-push-bell");
    if (!b) return;
    const { muted, label } = bellState(b.dataset.chat);
    b.setAttribute("aria-label", label + ": change");
    b.title = label;
    b.dataset.muted = muted === null ? "" : String(muted);
    b.querySelector("svg").innerHTML = BELL + (muted === null ? "" : SLASH);
  }

  // The mute menu, under `anchor` (a DOMRect): the chat's own mute and its
  // folder's (`chat`), or only a folder's (`folder`). It opens in the browser's
  // top layer (a popover), so no toast or dialog of Open WebUI can sit over it
  // and take a tap meant for it; elsewhere it gets the highest z-index.
  const GUTTER = 8;   // px kept free at the screen's edges, as Open WebUI's own menus do
  const GAP = 6;      // px between the anchor and the menu
  const KEYDOWN = "keydown", RTL = "rtl", TEXT = "string";
  const TOP_LAYER = "2147483647";
  const MENU_ID = "oc-push-mute-menu";
  let closedBy = null;   // what the press that last closed the menu landed on
  function muteMenu(anchor, { chat = null, folder = null } = {}) {
    document.getElementById(MENU_ID)?.remove();
    const m = document.createElement("div");
    m.id = MENU_ID;
    m.setAttribute("role", "menu");
    const dir = document.documentElement.dir || "ltr";
    m.dir = dir;
    const dark = document.documentElement.classList.contains("dark");
    m.style.cssText = "position:fixed;inset:auto;margin:0;border:0;z-index:" + TOP_LAYER + ";min-width:170px;" +
      "max-width:calc(100vw - " + GUTTER + "px - " + GUTTER + "px);padding:4px;border-radius:12px;" +
      `font:14px system-ui,sans-serif;background:${dark ? "#222" : "#fff"};color:${dark ? "#eee" : "#111"};` +
      "box-shadow:0 4px 16px #0003;max-height:calc(100vh - " + GUTTER + "px - " + GUTTER + "px);overflow-y:auto;";
    // A line of text; a folder's name is isolated (<bdi>), so a Hebrew name keeps
    // its own direction inside the sentence and the punctuation stays outside it.
    const line = (parts, el) => {
      for (const p of parts) {
        if (typeof p === TEXT) el.append(p);
        else { const b = document.createElement("bdi"); b.textContent = p.name; el.append(b); }
      }
      return el;
    };
    const head = (...parts) => {
      const h = line(parts, document.createElement("div"));
      h.style.cssText = "padding:6px 10px;opacity:.6;font-size:12px;overflow-wrap:anywhere";
      m.appendChild(h);
    };
    const item = (parts, list, target, seconds) => {
      const i = line(parts, document.createElement("button"));
      i.setAttribute("role", "menuitem");
      i.style.cssText = "display:block;width:100%;text-align:start;background:none;border:0;color:inherit;font:inherit;padding:8px 10px;border-radius:8px;cursor:pointer;overflow-wrap:anywhere";
      i.onclick = async () => {
        close();
        await setMute(list, target, seconds).catch(() => notice("Could not change notifications."));
      };
      m.appendChild(i);
    };
    const name = (id) => ({ name: folderName(id) });
    if (chat) {
      const own = until(CHATS, chat);
      head(own === null ? "This chat: notifications on" : "This chat: " + fmt(own));
      for (const [text, s] of MUTE_FOR) item(["Mute " + text], CHATS, chat, s);
      if (own !== null) item(["Unmute"], CHATS, chat, null);
    }
    // The folder: mute it (and the folders inside it), or unmute the folder
    // that mutes it, which may be one above.
    if (folder) {
      const by = mutedFolder(folder);
      if (chat) {
        const sep = document.createElement("hr");
        sep.style.cssText = "margin:4px 0;border:0;border-top:1px solid currentColor;opacity:.15";
        m.appendChild(sep);
      }
      head("Folder ", name(folder), ": ", ...(by ? [fmt(until(FOLDERS, by)), ...(by === folder ? [] : [" (by ", name(by), ")"])] : ["notifications on"]));
      for (const [text, s] of MUTE_FOR) item(["Mute folder " + text], FOLDERS, folder, s);
      if (by) item(["Unmute folder ", name(by)], FOLDERS, by, null);
    }
    const away = (e) => {
      if (e.type === KEYDOWN) { if (e.key === "Escape") close(); }
      else if (!m.contains(e.target)) { closedBy = e.target; close(); }   // that press must not open it again
    };
    function close() {
      m.remove();
      removeEventListener("pointerdown", away, true);
      removeEventListener("keydown", away, true);
    }
    // After this press (the one that opened it) has finished.
    setTimeout(() => { addEventListener("pointerdown", away, true); addEventListener("keydown", away, true); });
    document.body.appendChild(m);
    if (m.showPopover) { m.popover = "manual"; m.showPopover(); }
    // Below the anchor, toward the start of the line; inside the screen with a gutter,
    // and above the anchor when there is no room below.
    const w = m.offsetWidth, h = m.offsetHeight;
    const rtl = getComputedStyle(m).direction === RTL;
    const clamp = (v, max) => Math.max(GUTTER, Math.min(v, max - GUTTER));
    const left = clamp(rtl ? anchor.left : anchor.right - w, innerWidth - w);
    const below = anchor.bottom + GAP;
    const top = below + h <= innerHeight - GUTTER ? below : clamp(anchor.top - GAP - h, innerHeight - h);
    m.style.left = Math.round(left) + "px";
    m.style.top = Math.round(top) + "px";
    // The first choice gets the focus, after a native menu that opened this one has
    // given the focus back to its own trigger.
    requestAnimationFrame(() => setTimeout(() => m.isConnected && m.querySelector(MENU_ITEM)?.focus({ preventScroll: true })));
    return m;
  }

  // First in the header's right-hand controls (before the chat menu ⋯, which
  // Open WebUI shows only to users who may share), styled like its buttons.
  // Open WebUI redraws its header freely: an observer puts the bell back.
  const HEADER_BUTTON = "flex shrink-0 cursor-pointer items-center justify-center size-6 rounded-lg text-gray-500 transition " +
    "hover:bg-gray-50/40 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800/40 dark:hover:text-gray-200";
  function placeBell() {
    const id = chatIdNow();
    const menuButton = document.getElementById("chat-context-menu-button");
    const box = menuButton?.closest("nav div.flex-none") || document.querySelector("nav div.flex-none.items-center.self-center");
    const old = document.getElementById("oc-push-bell");
    if (!id || !box || !hasDevices || mutes === null) { old?.remove(); return; }
    if (old && old.dataset.chat === id && box.firstElementChild === old) return;
    old?.remove();
    const b = document.createElement("button");
    b.id = "oc-push-bell";
    b.type = "button";
    b.dataset.chat = id;
    b.className = menuButton?.className || HEADER_BUTTON;
    b.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" style="width:1.125rem;height:1.125rem"></svg>';
    // The chat may have moved to another folder since: read it again first.
    b.onclick = (e) => {
      e.stopPropagation();
      if (closedBy && b.contains(closedBy)) { closedBy = null; return; }   // that press closed it
      loadFolder(id).catch(() => {}).then(() => muteMenu(b.getBoundingClientRect(), { chat: id, folder: chatFolder.chat === id ? chatFolder.folder : null }));
    };
    box.prepend(b);
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
    let text = "", drafts = null;
    const key = "/oc-push/draft/" + id;
    try {
      drafts = await caches.open("oc-push-drafts");
      const r = await drafts.match(key);
      if (r) text = (await r.json()).text || "";
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
      if (text) {
        document.execCommand("insertText", false, text);
        // Dropped only once it is in the box: with no box, the next open finds it again.
        await drafts?.delete(key).catch(() => {});
      }
    }
    if (text) notice("Your reply was not sent. It is in the message box.");
    window.ocPushReply = { mode, text };
  }

  // A chat on screen, however it was opened: its notifications on this device
  // are read, close them (the tag is oc-sw.js's, one per chat).
  const TAG_PREFIX = "chat-";
  async function seen(id) {
    if (!id || document.visibilityState !== VISIBLE) return;
    const reg = await navigator.serviceWorker.getRegistration("/");
    for (const n of (await reg?.getNotifications({ tag: TAG_PREFIX + id })) || []) n.close();
    window.ocPushRead = id;   // for tests and tools, like ocPushState
  }

  async function chatPage() {
    seen(chatIdNow()).catch(() => {});
    if (!store.get("token")) return;
    if (mutes === null) await loadMutes().catch(() => {});
    placeBell();
    pendingReply();
    const id = chatIdNow();
    if (id && chatFolder.chat !== id) loadFolder(id).then(drawBell).catch(() => {});
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
    if (document.visibilityState !== VISIBLE) return;
    navigator.serviceWorker.getRegistration("/").then((r) => r && r.update()).catch(() => {});
    const id = chatIdNow();
    seen(id).catch(() => {});
    // Mutes may have changed on another device.
    if (store.get("token") && id) Promise.all([loadMutes(), loadFolder(id)]).then(drawBell).catch(() => {});
  });

  // A tap on a notification while this page is open: the service worker asks
  // the page to open the chat (oc-sw.js "oc-open-chat"). It opens like a link in
  // the sidebar: SvelteKit handles a click on any same-origin link in the
  // document, so the page stays (no reload). Already on it: nothing to do.
  const OPEN_CHAT = "oc-open-chat", OPEN_CHAT_ACK = "oc-open-chat-ack";
  navigator.serviceWorker.addEventListener("message", (e) => {
    const m = e.data || {};
    if (m.type !== OPEN_CHAT) return;
    // To the worker that asked (it holds the request), not the page's controller:
    // a page can have none, or a newer one.
    e.source?.postMessage({ type: OPEN_CHAT_ACK, id: m.id });
    const to = new URL(m.url, location.origin);
    if (to.origin !== location.origin) return;
    if (to.pathname === location.pathname && !to.search) {
      window.ocPushOpenedSame = true;
      seen(chatIdNow()).catch(() => {});
      return;
    }
    const a = document.createElement("a");
    a.href = to.pathname + to.search;
    a.hidden = true;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.ocPushOpened = to.pathname.slice("/c/".length);
  });
  navigator.serviceWorker.startMessages?.();

  // --- Items in Open WebUI's own ⋯ menus: a chat's (Mark as read, Mute
  // notifications) and a folder's (Mute folder), in the sidebar and on a folder's page.
  // The DOM contract with Open WebUI 0.13.0 (push/e2e tests it), found by structure
  // and icon, not by text (the menus are translated):
  // - a chat in the sidebar is a box whose child #sidebar-chat-item holds the
  //   chat's link, with its ⋯ (button[aria-haspopup]) further inside the box;
  // - a folder in the sidebar is #folder-<id>-button holding its ⋯; on the
  //   folder's page (/folders/<id>) the ⋯ is outside the sidebar;
  // - the menu is a [role=menu] with .app-dropdown-menu holding
  //   button[role=menuitem] items; a chat's has "Mark as unread", a folder's
  //   "Mark all as read", each known by its icon path.
  // Our items are copies of that native item (same look), placed after it.
  // When a menu we expect does not match, nothing is added and the console says
  // so once: the native items are never changed.
  const MENU_BOX = ".app-dropdown-menu";
  const MENU_ITEM = "button[role=menuitem]";
  const UNREAD_ICON = "M8 12L11 15L16 10";
  const READ_ALL_ICON = "m4.5 12.75 6 6 9-13.5";
  const CHAT_LINK = ':scope > #sidebar-chat-item a[href^="/c/"]';
  const CHAT_PATH = "/c/";
  const FOLDER_ROW = /^folder-(.+)-button$/;
  const FOLDER_PAGE = /^\/folders\/([^/]+)/;
  // How far up from the ⋯ button its chat's box may be (Open WebUI 0.13: 5).
  const TRIGGER_DEPTH = 8;
  // A menu that opens later than this after the press is not that chat's.
  const MENU_AFTER_MS = 2000;
  const OURS = "ocPushItem";   // data-oc-push-item on what we add
  let pressed = null;
  const logged = new Set();

  // What the pressed ⋯ belongs to, by a pointer or from the keyboard (the
  // menu also opens on Enter and Space).
  const OPEN_KEYS = new Set(["Enter", " "]);
  function press(e) {
    const b = e.target instanceof Element && e.target.closest("button[aria-haspopup]");
    if (!b) return;
    const row = b.closest('[id^="folder-"]');
    const rowId = row && FOLDER_ROW.exec(row.id);
    if (rowId) { pressed = { folder: rowId[1], at: Date.now() }; return; }
    for (let box = b, i = 0; box && i < TRIGGER_DEPTH; i++, box = box.parentElement) {
      const link = box.querySelector(CHAT_LINK);
      if (link) {
        pressed = { chat: link.getAttribute("href").slice(CHAT_PATH.length), at: Date.now() };
        return;
      }
    }
    // On a folder's page, a ⋯ outside the sidebar may be the folder's own (or
    // another, like its icon picker: that menu has no "Mark all as read", and is left alone).
    const page = FOLDER_PAGE.exec(location.pathname);
    if (page && !b.closest("#sidebar")) pressed = { folder: page[1], page: true, at: Date.now() };
  }
  addEventListener("pointerdown", press, true);
  addEventListener("keydown", (e) => { if (OPEN_KEYS.has(e.key)) press(e); }, true);

  // A menu just appeared: once it is drawn (next frame), it gets our items.
  function menuOpened(menu) {
    if (!pressed || Date.now() - pressed.at > MENU_AFTER_MS || menu.dataset.ocPush) return;
    const what = pressed;
    pressed = null;
    menu.dataset.ocPush = "seen";
    requestAnimationFrame(() => addItems(menu, what));
  }

  // The native item ours go after, if this menu is the one we know.
  function nativeItem(menu, icon) {
    const it = [...menu.querySelectorAll(MENU_BOX + " > " + MENU_ITEM)]
      .find((b) => !b.dataset[OURS] && b.querySelector("path")?.getAttribute("d")?.startsWith(icon));
    // Its label: the item's last div, holding all of the item's text.
    const text = it?.textContent.trim();
    const label = it && [...it.querySelectorAll("div")].pop();
    return it && text && label?.textContent.trim() === text ? it : null;
  }

  // For tests and tools: "ok" or "missing", per kind of menu.
  const CONTRACT = Object.freeze({ chat: "ocPushMenuContract", folder: "ocPushFolderMenuContract" });
  function contractMissing(kind) {
    window[CONTRACT[kind]] = "missing";
    if (logged.has(kind)) return;
    logged.add(kind);
    console.info(`oc-push: Open WebUI's ${kind} menu is not the one this script knows (0.13.0); nothing added.`);
  }

  // Our copies have none of the menu's own handlers: close it as a user does, with
  // Escape and, where that is not heard, a press outside it.
  const closeNative = () => {
    document.dispatchEvent(new KeyboardEvent(KEYDOWN, { key: "Escape", bubbles: true }));
    for (const type of ["pointerdown", "pointerup"]) document.body.dispatchEvent(new PointerEvent(type, { bubbles: true }));
  };

  // A copy of a native item, with our label and icon, after `prev`.
  function ourItem(native, prev, key, label, icon, onclick) {
    const item = native.cloneNode(true);
    item.dataset[OURS] = key;
    [...item.querySelectorAll("div")].pop().textContent = label;
    if (icon) {
      const svg = item.querySelector("svg");
      svg.setAttribute("stroke", "currentColor");
      svg.innerHTML = icon;
    }
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      const r = item.getBoundingClientRect();
      closeNative();
      onclick(r);
    });
    prev.after(item);
    return item;
  }

  const muteLabel = (state) => state.muted === null ? null : state.label;

  async function addItems(menu, what) {
    const chat = what.chat;
    const native = nativeItem(menu, chat ? UNREAD_ICON : READ_ALL_ICON);
    if (!native) {
      // The folder page's other ⋯ menus (its icon picker) are not a folder menu.
      if (!what.page) contractMissing(chat ? "chat" : "folder");
      return;
    }
    window[CONTRACT[chat ? "chat" : "folder"]] = "ok";
    if (!store.get("token")) return;
    if (mutes === null) await loadMutes().catch(() => {});
    if (chat) await loadFolder(chat).catch(() => {});   // a chat can move to another folder
    if (!chat) await loadFolders().catch(() => {});
    const put = () => {
      if (!menu.isConnected) return;
      let prev = native;
      if (chat && navigator.serviceWorker.controller) {
        // The service worker sends Open WebUI's own read event (as the notification's
        // button does); if that fails it opens the chat, which reads it.
        prev = ourItem(native, prev, "read", "Mark as read", null, () =>
          navigator.serviceWorker.controller?.postMessage({ type: "oc-push-act", action: "read", data: { chat_id: chat, url: CHAT_PATH + chat } }));
      }
      if (!hasDevices) return;   // nothing to mute: no device of this user gets pushes
      if (chat) {
        const state = bellState(chat);
        ourItem(native, prev, "mute", muteLabel(state) || "Mute notifications", BELL + (state.muted === null ? "" : SLASH),
          (r) => muteMenu(r, { chat, folder: chatFolder.chat === chat ? chatFolder.folder : null }));
      } else {
        const by = mutedFolder(what.folder);
        ourItem(native, prev, "mute", by ? fmt(until(FOLDERS, by)) + (by === what.folder ? "" : " (" + folderName(by) + ")") : "Mute folder",
          BELL + (by ? SLASH : ""), (r) => muteMenu(r, { folder: what.folder }));
      }
    };
    put();
    // Open WebUI may redraw the menu's items while it is open: put ours back.
    const watch = new MutationObserver(() => {
      if (!menu.isConnected) return watch.disconnect();
      if (!menu.querySelector("[data-oc-push-item]") && nativeItem(menu, chat ? UNREAD_ICON : READ_ALL_ICON)) {
        watch.disconnect();
        addItems(menu, what);
      }
    });
    watch.observe(menu, { childList: true, subtree: true });
  }

  new MutationObserver((records) => {
    for (const r of records) for (const n of r.addedNodes) {
      if (!(n instanceof Element)) continue;
      const menu = n.closest("[role=menu]") || n.querySelector("[role=menu]");
      if (menu) menuOpened(menu);
    }
  }).observe(document.documentElement, { childList: true, subtree: true });

  window.ocPushEnable = enable;
  window.ocPushMute = (seconds, id = chatIdNow()) => setMute(CHATS, id, seconds);
  window.ocPushMuteFolder = (seconds, id = chatFolder.folder) => setMute(FOLDERS, id, seconds);
  window.ocPushReady = run();
})();

// --- Open WebUI's error page after a network change (ELI-352). A network change
// while the app loads fails Open WebUI's own files ("Failed to fetch dynamically
// imported module"), and SvelteKit then puts its "500 Internal Error" page in
// place of the app, which stays there. This reloads the app once, when the server
// can be reached again: at once, or on the next 'online' or network change.
// A guard in sessionStorage, cleared by a load that gets to the app, keeps it to
// one reload: a page that fails again stays on the error page. Apart from the
// push part above, so it also runs where push is not offered. The service worker
// also asks for a reload when the app it gave from its cache turns out to be from
// another Open WebUI version (oc-sw.js "oc-new-version").
(() => {
  const RELOADED = "ocReloadedAfterError";
  const NEW_VERSION = "oc-new-version";
  const PROBE = "/_app/version.json";   // HEAD: the service worker leaves it to the network
  const session = {
    get: (k) => { try { return sessionStorage.getItem(k); } catch (_) { return null; } },
    set: (k, v) => { try { sessionStorage.setItem(k, v); } catch (_) {} },
    drop: (k) => { try { sessionStorage.removeItem(k); } catch (_) {} },
  };
  // SvelteKit's own error page (its error.html), not one of Open WebUI's.
  const errorPage = () => !!document.querySelector("body > .error > .status");
  const appUp = () => !document.getElementById("splash-screen") && !errorPage();

  let probing = false;
  async function recover() {
    if (probing || !errorPage() || session.get(RELOADED)) return;
    probing = true;
    try {
      const r = await fetch(PROBE, { method: "HEAD", cache: "no-store" });
      if (!r.ok) return;
    } catch (_) {
      return;   // still no network: the next 'online' or network change tries again
    } finally {
      probing = false;
    }
    session.set(RELOADED, "1");
    location.reload();
  }

  const watch = new MutationObserver(() => {
    if (errorPage()) { watch.disconnect(); recover(); } else if (appUp()) {
      watch.disconnect();
      session.drop(RELOADED);
      session.drop(VERSION_RELOADED);
    }
  });
  watch.observe(document.documentElement, { childList: true, subtree: true });
  recover();
  addEventListener("online", recover);
  navigator.connection?.addEventListener?.("change", recover);
  // The kept app turned out to be another Open WebUI version: reload once onto the
  // server's own (the service worker dropped what it kept). Its own guard, cleared
  // the same way, so a version reload and an error-page reload never block each other.
  const VERSION_RELOADED = "ocReloadedForVersion";
  navigator.serviceWorker?.addEventListener("message", (e) => {
    if (e.data?.type !== NEW_VERSION || session.get(VERSION_RELOADED)) return;
    session.set(VERSION_RELOADED, "1");
    location.reload();
  });
  navigator.serviceWorker?.startMessages?.();
  navigator.serviceWorker?.controller?.postMessage({ type: "oc-version-check" });
})();

// --- Open WebUI's start reads after a network change (ELI-362). A change of
// network fails every request on its way (ERR_NETWORK_CHANGED), and Open WebUI
// asks its start reads (config, user, settings, models, the chat list, the chat)
// only once: one that fails leaves the app half drawn for good, with no error
// page to recover from. A start read that fails on the network (no answer, not an
// HTTP error) is asked once more at once, on the new network. With offline open
// on, the service worker already does the same (oc-sw.js again()); this covers
// offline open off, where the worker answers nothing itself. Only these reads,
// only GET, and only once each.
(() => {
  const START_READS = [
    /^\/api\/config$/, /^\/api\/version$/, /^\/api\/models$/, /^\/api\/v1\/auths\/$/,
    /^\/api\/v1\/users\/user\/settings$/, /^\/api\/v1\/configs\/banners$/, /^\/api\/v1\/folders\/$/,
    /^\/api\/v1\/chats\/$/, /^\/api\/v1\/chats\/pinned$/, /^\/api\/v1\/chats\/all\/tags$/,
    /^\/api\/v1\/chats\/[\w-]+$/, /^\/api\/v1\/chats\/[\w-]+\/tags$/,
  ];
  const GET = "GET";
  const startRead = (input, init) => {
    const req = input instanceof Request ? input : null;
    const method = (init?.method || req?.method || GET).toUpperCase();
    if (method !== GET) return false;
    const url = new URL(req ? req.url : String(input), location.href);
    return url.origin === location.origin && START_READS.some((r) => r.test(url.pathname));
  };
  const original = window.fetch;
  window.fetch = function (input, init) {
    if (!startRead(input, init)) return original.call(this, input, init);
    // A Request's body is used once: the second attempt gets a fresh copy.
    const again = input instanceof Request ? input.clone() : input;
    return original.call(this, input, init).catch((e) => {
      if (init?.signal?.aborted || (input instanceof Request && input.signal.aborted)) throw e;   // Open WebUI cancelled it
      return original.call(this, again, init);
    });
  };
})();
