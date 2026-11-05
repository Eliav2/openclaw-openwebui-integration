// oc-sw.js: the Web Push service worker for Open WebUI (docs/install/web-push.md).
// Serve it at /oc-sw.js. Push only: there is no fetch handler, so it never
// caches or intercepts a request.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

// iOS and iPadOS: every push must show a notification (userVisibleOnly), or
// WebKit revokes the subscription.
const MUST_SHOW = /iPhone|iPad|iPod/.test(self.navigator.userAgent) ||
  (/Macintosh/.test(self.navigator.userAgent) && self.navigator.maxTouchPoints > 1);
const HOUR_S = 3600;
const MUTE_ACTION_HOURS = 8;
const MUTE_ACTION_S = MUTE_ACTION_HOURS * HOUR_S;
const MS_PER_S = 1000;
const VISIBLE = "visible";
// A notification's buttons (`event.action`, "" for the notification itself).
const ACTION = Object.freeze({ REPLY: "reply", READ: "read", MUTE: "mute" });
// The notification tag is per chat (oc-push.js uses the same).
const TAG_PREFIX = "chat-";
// The buttons a push may ask for. "text" gives Reply a text field in the shade.
const BUTTONS = {
  [ACTION.REPLY]: { action: ACTION.REPLY, title: "Reply", type: "text", placeholder: "Reply" },
  [ACTION.READ]: { action: ACTION.READ, title: "Mark as read" },
  [ACTION.MUTE]: { action: ACTION.MUTE, title: `Mute ${MUTE_ACTION_HOURS} h` },
};
// Where a reply that could not be sent waits for the page (oc-push.js reads it).
const DRAFTS = "oc-push-drafts";

self.addEventListener("push", (e) => {
  let p = {};
  try { p = e.data ? e.data.json() : {}; } catch (_) {}
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    // Open WebUI is in front of the user on this device, whatever chat is open:
    // nothing to tell, as in a messaging app (the page has its own cues).
    const inFront = wins.some((c) => c.focused && c.visibilityState === VISIBLE);
    const shown = MUST_SHOW || !inFront;
    if (shown) {
      await self.registration.showNotification(p.title || "Open WebUI", {
        body: p.body || "New reply",
        tag: TAG_PREFIX + (p.chat_id || "none"),   // one per chat: a newer one replaces it (oc-push.js closes it when the chat is opened)
        renotify: true,
        icon: "/static/favicon.png",
        badge: "/static/favicon.png",
        data: { url: p.url || "/", chat_id: p.chat_id || "", fn: p.fn || "" },
        // The function's NOTIFICATION_ACTIONS, in order (a v2 push has none: Reply
        // and Mute). Android Chrome shows two (Notification.maxActions) and adds
        // its own "Unsubscribe" after them.
        actions: p.chat_id ? (p.actions || [ACTION.REPLY, ...(p.fn ? [ACTION.MUTE] : [])]).filter((a) => BUTTONS[a]).map((a) => BUTTONS[a]) : [],
      });
    }
    // Each decision, once acted on, to the open pages (`oc-push` messages), for tests and tools.
    for (const c of wins) c.postMessage({ type: "oc-push", shown, payload: p });
  })());
});

// Open WebUI's own API with the session cookie (same origin; Open WebUI reads
// its `token` cookie when there is no Authorization header).
async function api(path, body) {
  const r = await fetch(path, {
    method: body ? "POST" : "GET", credentials: "same-origin", redirect: "error",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}

// A message in the chat as the user, the way Open WebUI 0.13's chat page sends
// one: a single /api/chat/completions with the new user message under the
// chat's current leaf, which Open WebUI inserts with an empty reply and runs.
// A run still going is not stopped: the message reaches it like one sent from
// another tab (the OpenClaw Pipe steers it in, or queues it behind the run).
async function sendReply(chatId, text) {
  const chat = (await api("/api/v1/chats/" + chatId)).chat || {};
  const history = chat.history || {};
  const leaf = history.currentId ?? null;
  const model = (chat.models || [])[0] || history.messages?.[leaf]?.model;
  if (!model) throw new Error("no model");
  const uid = crypto.randomUUID(), aid = crypto.randomUUID();
  const userMessage = { id: uid, parentId: leaf, childrenIds: [], role: "user", content: text,
    timestamp: Math.floor(Date.now() / MS_PER_S), models: [model] };
  await api("/api/chat/completions", {
    stream: true, model, chat_id: chatId, id: aid, parent_id: leaf, user_message: userMessage,
    message_ids: [{ model_id: model, message_id: aid, modelIdx: 0 }],
    session_id: "oc-push-" + uid,
  });
  return uid;
}

// Mute a chat in the function's user valves: read, merge, write the whole set
// (Open WebUI's update replaces every user valve).
async function mute(fn, chatId, seconds) {
  const path = "/api/v1/functions/id/" + fn + "/valves/user";
  const valves = (await api(path)) || {};
  let mutes = {};
  try { mutes = JSON.parse(valves.mutes || "{}") || {}; } catch (_) {}
  mutes[chatId] = seconds ? Math.floor(Date.now() / MS_PER_S) + seconds : 0;
  await api(path + "/update", { ...valves, mutes: JSON.stringify(mutes) });
}

// Mark a chat read, as opening it does: Open WebUI 0.13 keeps the sidebar's
// unread dot on the server (chat.last_read_at), and sets it for one chat only
// through its Socket.IO event `events:chat` {type: "last_read_at"}; it then
// tells every open window of the user (`chat:list`), so the dot goes at once.
// The token comes from the session cookie (GET /api/v1/auths/), and is not kept.
const READ_TIMEOUT_MS = 15000;   // a safety net for a socket that never answers
// Engine.IO 4 packets (open, ping, pong) and Socket.IO's on the default namespace
// (connect, connect error, event): the first characters of each frame.
const EIO = Object.freeze({ OPEN: "0", PING: "2", PONG: "3" });
const SIO = Object.freeze({ CONNECT: "40", CONNECT_ERROR: "44", EVENT: "42" });
const SECURE_PROTOCOL = "https:";
// Open WebUI 0.13's read event, and its answer to the user's windows.
const READ_EVENT = "events:chat", READ_TYPE = "last_read_at";
const ANSWER_EVENT = "events", ANSWER_TYPE = "chat:list";
async function markRead(chatId) {
  const { token } = await api("/api/v1/auths/");
  if (!token) throw new Error("no session");
  const url = new URL("/ws/socket.io/?EIO=4&transport=websocket", self.location.origin);
  url.protocol = url.protocol === SECURE_PROTOCOL ? "wss:" : "ws:";
  const ws = new WebSocket(url);
  try {
    await new Promise((ok, fail) => {
      const timer = setTimeout(() => fail(new Error("socket timeout")), READ_TIMEOUT_MS);
      const done = (f, v) => { clearTimeout(timer); f(v); };
      ws.onerror = () => done(fail, new Error("socket error"));
      ws.onclose = () => done(fail, new Error("socket closed"));
      ws.onmessage = ({ data }) => {
        if (data === EIO.PING) return ws.send(EIO.PONG);
        if (data.startsWith(SIO.CONNECT_ERROR)) return done(fail, new Error("socket refused"));
        if (data.startsWith(SIO.CONNECT)) {
          return ws.send(SIO.EVENT + JSON.stringify([READ_EVENT, { chat_id: chatId, data: { type: READ_TYPE } }]));
        }
        if (data.startsWith(SIO.EVENT)) {
          const [name, ev] = JSON.parse(data.slice(SIO.EVENT.length));
          if (name === ANSWER_EVENT && ev?.chat_id === chatId && ev.data?.type === ANSWER_TYPE) done(ok);
          return;
        }
        if (data.startsWith(EIO.OPEN)) ws.send(SIO.CONNECT + JSON.stringify({ token }));
      };
    });
  } finally {
    ws.onclose = null;
    ws.close();
  }
}

async function openChat(url) {
  const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  const win = wins.find((c) => new URL(c.url).origin === self.location.origin);
  if (win) {
    // focus() is allowed only during a notification click (a user's tap).
    await win.focus().catch(() => {});
    return win.navigate(url);
  }
  return self.clients.openWindow(url);
}

// What a tap does. `action`: "" (the notification), "reply" or "mute".
async function act(action, data, reply) {
  const url = new URL(data.url || "/", self.location.origin).href;
  if (action === ACTION.MUTE && data.fn && data.chat_id) {
    try {
      await mute(data.fn, data.chat_id, MUTE_ACTION_S);
      return "muted";
    } catch (_) {
      await openChat(url);
      return "mute-failed";
    }
  }
  if (action === ACTION.READ && data.chat_id) {
    try {
      await markRead(data.chat_id);
      return "read";
    } catch (_) {
      // Not marked: opening the chat marks it read.
      await openChat(url);
      return "read-failed";
    }
  }
  if (action === ACTION.REPLY && data.chat_id) {
    const text = (reply || "").trim();
    if (text) {
      try {
        await sendReply(data.chat_id, text);
        return "sent";
      } catch (_) {
        // Not sent: the text waits for the chat page, which puts it in the
        // message box and says so. Never dropped.
        const drafts = await caches.open(DRAFTS);
        await drafts.put("/oc-push/draft/" + data.chat_id, new Response(JSON.stringify({ text, at: Date.now() })));
      }
    }
    await openChat(url + (url.includes("?") ? "&" : "?") + "oc-reply=" + (text ? "unsent" : "compose"));
    return text ? "reply-failed" : "compose";
  }
  await openChat(url);
  return "opened";
}

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(act(e.action, e.notification.data || {}, e.reply).then(async (result) => {
    for (const c of await self.clients.matchAll({ type: "window", includeUncontrolled: true })) {
      c.postMessage({ type: "oc-push-action", action: e.action, result });
    }
  }));
});

// A page of this origin can run the same actions (the e2e tests do; a page
// could call the same API itself, so this grants nothing new).
self.addEventListener("message", (e) => {
  const m = e.data || {};
  if (m.type !== "oc-push-act" || !e.source || new URL(e.source.url).origin !== self.location.origin) return;
  e.waitUntil(act(m.action, m.data || {}, m.reply).then((result) =>
    e.source.postMessage({ type: "oc-push-action", action: m.action, result })));
});

// The push service replaced the subscription: subscribe again with the same
// key. The page saves the new one on its next load (it has the user's token).
self.addEventListener("pushsubscriptionchange", (e) => {
  const options = e.oldSubscription?.options;
  if (options?.applicationServerKey) {
    e.waitUntil(self.registration.pushManager.subscribe(options));
  }
});
