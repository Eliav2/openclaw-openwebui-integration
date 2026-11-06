// node --test push/web/oc-sw-chats.test.js: kept chats (oc-sw.js), run in a sandbox
// with a fake Cache Storage and a network each test answers by hand. The worker is
// served as the proxy serves it, with its switches set (OFFLINE, CHAT_CACHE).
import test from "node:test";
import assert from "node:assert";
import fs from "node:fs";
import vm from "node:vm";

const SW = process.env.OC_SW || new URL("oc-sw.js", import.meta.url);
const ORIGIN = "https://chat.example";
const CHAT = "11111111-2222-4333-8444-555555555555";
const OTHER = "66666666-7777-4888-9999-aaaaaaaaaaaa";
const chatUrl = (id) => ORIGIN + "/api/v1/chats/" + id;
const TOKEN_A = "Bearer token-of-a", TOKEN_B = "Bearer token-of-b";
const KEPT = "x-oc-kept";
const MIB = 1024 * 1024;
const OK = 200, NOT_FOUND = 404, BAD_GATEWAY = 502;

// An answer of this site, as a browser gives it (node's own is "default", never kept).
class SiteResponse extends Response {
  get type() { return "basic"; }
  clone() { return Object.setPrototypeOf(super.clone(), SiteResponse.prototype); }
}

// Cache Storage as Chrome keeps it: a copy per match, keys in the order they were
// (last) put.
function cacheStorage() {
  const stores = new Map();
  const open = async (name) => {
    if (!stores.has(name)) stores.set(name, new Map());
    const m = stores.get(name);
    const url = (k) => new URL(typeof k === "string" ? k : k.url, ORIGIN).href;
    return {
      async match(k) {
        const e = m.get(url(k));
        return e && new SiteResponse(e.body, { status: e.status, headers: e.headers });
      },
      async put(k, res) {
        const body = await res.blob();
        m.delete(url(k));
        m.set(url(k), { body, status: res.status, headers: [...res.headers] });
      },
      async delete(k) { return m.delete(url(k)); },
      async keys() { return [...m.keys()].map((u) => ({ url: u })); },
    };
  };
  return {
    stores,
    open,
    has: async (name) => stores.has(name),
    keys: async () => [...stores.keys()],
    delete: async (name) => stores.delete(name),
  };
}

// A network whose answers the test gives: each request waits until answered.
function network() {
  const asked = [];
  const fetch = (input, init) => new Promise((resolve, reject) => {
    const url = new URL(typeof input === "string" ? input : input.url, ORIGIN).href;
    asked.push({ url, init, resolve, reject });
  });
  const waiting = (url) => asked.filter((a) => a.url === url && !a.done);
  return {
    asked,
    fetch,
    count: (url) => asked.filter((a) => a.url === url).length,
    // Answers every request for `url` that is waiting.
    answer(url, body, status = OK) {
      for (const a of waiting(url)) {
        a.done = true;
        a.resolve(new SiteResponse(body, { status, headers: { "content-type": "application/json" } }));
      }
    },
    fail(url) {
      for (const a of waiting(url)) { a.done = true; a.reject(new TypeError("Failed to fetch")); }
    },
  };
}

function worker({ chats = "1", offline = "1" } = {}) {
  const listeners = {};
  const caches = cacheStorage();
  const net = network();
  const pending = new Set();
  const self = {
    navigator: { userAgent: "Mozilla/5.0 (Linux; Android 14) Chrome/140", maxTouchPoints: 0 },
    location: { origin: ORIGIN },
    registration: { getNotifications: async () => [], showNotification: async () => {} },
    clients: { matchAll: async () => [], claim: async () => {}, get: async () => null },
    addEventListener: (type, fn) => { listeners[type] = fn; },
  };
  const source = fs.readFileSync(SW, "utf8").replaceAll("__OC_OFFLINE__", offline).replaceAll("__OC_CHAT_CACHE__", chats);
  vm.runInNewContext(source, {
    self, URL, console, setTimeout, clearTimeout, caches, fetch: net.fetch,
    Response: SiteResponse, Request, Headers, Blob, TextEncoder, crypto, JSON, Promise, Map, Set, Math, Date, Object,
  });
  // A request of the page `client`; resolves to the worker's answer (null: not answered by it).
  // `open`: Open WebUI's first read after the page went to the chat (oc-push.js marks it).
  function read(url, { auth = TOKEN_A, method = "GET", own = false, open = false } = {}) {
    let answer = null;
    const headers = { ...(auth ? { authorization: auth } : {}), ...(own ? { "x-oc-own": "1" } : {}),
      ...(open ? { "x-oc-open": "1" } : {}) };
    const request = new Request(url, { method, headers });
    listeners.fetch({
      request, clientId: "page",
      respondWith: (p) => { answer = p; },
      waitUntil: (p) => { pending.add(p); p.finally(() => pending.delete(p)); },
    });
    return answer;
  }
  // Every piece of work the worker started is done (its cache writes included).
  async function settled() {
    while (pending.size) await Promise.allSettled([...pending]);
  }
  return { read, net, caches, settled };
}

const chat = (id, text, size = 0) => JSON.stringify({ id, chat: { history: { text, pad: "x".repeat(size) } } });
const text = async (res) => JSON.parse(await (await res).text()).chat.history.text;
// The network gets the request, or the test waits for nothing: a microtask turn
// for the worker's own awaits (opening the cache, the token's hash).
const asked = async (w, url, n) => {
  for (let i = 0; w.net.count(url) < n; i++) {
    await new Promise((r) => setImmediate(r));
    assert.ok(i < 1000, `never asked ${url} ${n} times (${w.net.count(url)})`);
  }
};

// Does `act` to the network (answer or fail what is waiting) until `answer` is in.
async function until(answer, act) {
  let done = false;
  answer.finally(() => { done = true; });
  for (let i = 0; !done; i++) {
    act();
    await new Promise((r) => setImmediate(r));
    assert.ok(i < 1000, "the worker never answered");
  }
  return answer;
}

// The first open of a chat: the network; kept for the next.
async function opened(w, id, body, opts) {
  const res = w.read(chatUrl(id), opts);
  await asked(w, chatUrl(id), w.net.count(chatUrl(id)) + 1);
  w.net.answer(chatUrl(id), body);
  const r = await res;
  await w.settled();
  return r;
}

test("a chat opened before: the kept copy at once, marked; the next read waits for the network's", async () => {
  const w = worker();
  await opened(w, CHAT, chat(CHAT, "old"));
  const first = await w.read(chatUrl(CHAT), { open: true });   // loadChat: no network yet
  assert.equal(first.headers.get(KEPT), "1");
  assert.equal(await text(first), "old");
  const refresh = w.read(chatUrl(CHAT));                     // refreshChat, on the socket's join
  await asked(w, chatUrl(CHAT), 2);
  w.net.answer(chatUrl(CHAT), chat(CHAT, "new"));
  const fresh = await refresh;
  assert.equal(fresh.headers.get(KEPT), null);
  assert.equal(await text(fresh), "new");
  assert.equal(w.net.count(chatUrl(CHAT)), 2, "one download behind the kept copy");
  await w.settled();
  const again = await w.read(chatUrl(CHAT), { open: true });
  assert.equal(await text(again), "new", "the network's copy is kept for the next open");
});

test("the page script's own read (for the chat's folder) is not the page's: Open WebUI's read still gets the kept copy", async () => {
  const w = worker();
  await opened(w, CHAT, chat(CHAT, "old"));
  const own = await w.read(chatUrl(CHAT), { own: true });
  assert.equal(await text(own), "old");
  assert.equal(w.net.count(chatUrl(CHAT)), 1, "no download for it");
  const load = await w.read(chatUrl(CHAT), { open: true });
  assert.equal(load.headers.get(KEPT), "1");
  assert.equal(await text(load), "old");
});

test("a read that is not an open (Open WebUI reloading the chat) never gets the kept copy: the network's", async () => {
  const w = worker();
  await opened(w, CHAT, chat(CHAT, "old"));
  const reload = w.read(chatUrl(CHAT));
  await asked(w, chatUrl(CHAT), 2);
  w.net.answer(chatUrl(CHAT), chat(CHAT, "new"));
  const res = await reload;
  assert.equal(res.headers.get(KEPT), null);
  assert.equal(await text(res), "new");
});

test("an open while the page script's own read is fetching the chat: that fetch, one download", async () => {
  const w = worker();
  const own = w.read(chatUrl(CHAT), { own: true });
  await asked(w, chatUrl(CHAT), 1);
  const open = w.read(chatUrl(CHAT), { open: true });
  w.net.answer(chatUrl(CHAT), chat(CHAT, "first"));
  assert.equal(await text(own), "first");
  const res = await open;
  assert.equal(res.headers.get(KEPT), null);
  assert.equal(await text(res), "first");
  assert.equal(w.net.count(chatUrl(CHAT)), 1);
});

test("the network's copy came before the page's next read: that read gets it, no second download", async () => {
  const w = worker();
  await opened(w, CHAT, chat(CHAT, "old"));
  await w.read(chatUrl(CHAT), { open: true });
  await asked(w, chatUrl(CHAT), 2);
  w.net.answer(chatUrl(CHAT), chat(CHAT, "new"));
  await w.settled();
  const refresh = await w.read(chatUrl(CHAT));
  assert.equal(refresh.headers.get(KEPT), null);
  assert.equal(await text(refresh), "new");
  assert.equal(w.net.count(chatUrl(CHAT)), 2);
});

test("no network for the refresh: the kept copy again, never an error (Open WebUI would leave the chat)", async () => {
  const w = worker();
  await opened(w, CHAT, chat(CHAT, "old"));
  await w.read(chatUrl(CHAT), { open: true });
  // Every attempt fails (the worker asks a failed read once more at once).
  const res = await until(w.read(chatUrl(CHAT)), () => w.net.fail(chatUrl(CHAT)));
  assert.equal(res.status, OK);
  assert.equal(res.headers.get(KEPT), "1");
  assert.equal(await text(res), "old");
});

test("the server down for the refresh (502): the kept copy; back up, the page's next read gets the network's", async () => {
  const w = worker();
  await opened(w, CHAT, chat(CHAT, "old"));
  await w.read(chatUrl(CHAT), { open: true });
  const refresh = until(w.read(chatUrl(CHAT)), () => w.net.answer(chatUrl(CHAT), "bad gateway", BAD_GATEWAY));
  assert.equal(await text(refresh), "old");
  await w.settled();
  // A reconnect: Open WebUI refreshes again.
  const later = until(w.read(chatUrl(CHAT)), () => w.net.answer(chatUrl(CHAT), chat(CHAT, "new")));
  assert.equal(await text(later), "new");
});

test("a chat deleted or no longer the user's (404): the answer as it is, and the kept copy goes", async () => {
  const w = worker();
  await opened(w, CHAT, chat(CHAT, "old"));
  await w.read(chatUrl(CHAT), { open: true });
  const refresh = w.read(chatUrl(CHAT));
  await asked(w, chatUrl(CHAT), 2);
  w.net.answer(chatUrl(CHAT), JSON.stringify({ detail: "not found" }), NOT_FOUND);
  assert.equal((await refresh).status, NOT_FOUND);
  await w.settled();
  const next = w.read(chatUrl(CHAT), { open: true });
  await asked(w, chatUrl(CHAT), 3);
  w.net.answer(chatUrl(CHAT), JSON.stringify({ detail: "not found" }), NOT_FOUND);
  assert.equal((await next).status, NOT_FOUND, "not opened from a kept copy");
});

test("a chat bigger than the per-chat limit is not kept", async () => {
  const w = worker();
  await opened(w, CHAT, chat(CHAT, "big", 33 * MIB));
  const next = w.read(chatUrl(CHAT), { open: true });
  await asked(w, chatUrl(CHAT), 2);
  w.net.answer(chatUrl(CHAT), chat(CHAT, "big again"));
  const res = await next;
  assert.equal(res.headers.get(KEPT), null);
  assert.equal(await text(res), "big again");
});

test("over the total limit, the chat opened longest ago goes first", async () => {
  const w = worker();
  const ids = Array.from({ length: 5 }, (_, i) => `${i}0000000-0000-4000-8000-000000000000`);
  for (const id of ids) await opened(w, id, chat(id, "c" + id, 30 * MIB));   // 150 MiB, over 128
  // The first one opened is gone; the others are kept.
  const store = [...w.caches.stores.values()].find((m) => m.size > 1);
  const kept = ids.filter((id) => store.has(chatUrl(id)));
  assert.deepEqual(kept, ids.slice(1));
  // Opening one again makes it the most recent: the next one in goes past it.
  const again = await w.read(chatUrl(ids[1]), { open: true });
  assert.equal(again.headers.get(KEPT), "1");
  await asked(w, chatUrl(ids[1]), 2);
  w.net.answer(chatUrl(ids[1]), chat(ids[1], "c" + ids[1], 30 * MIB));
  await w.settled();
  const extra = "f0000000-0000-4000-8000-000000000000";
  await opened(w, extra, chat(extra, "extra", 30 * MIB));
  assert.ok(store.has(chatUrl(ids[1])), "opened again: kept");
  assert.ok(!store.has(chatUrl(ids[2])), "the least recently opened went");
});

test("another user on the same browser: never the first user's kept chat", async () => {
  const w = worker();
  await opened(w, CHAT, chat(CHAT, "a's chat"));
  const asB = w.read(chatUrl(CHAT), { auth: TOKEN_B, open: true });
  await asked(w, chatUrl(CHAT), 2);
  w.net.answer(chatUrl(CHAT), JSON.stringify({ detail: "not found" }), NOT_FOUND);
  const res = await asB;
  assert.equal(res.status, NOT_FOUND);
  assert.equal(res.headers.get(KEPT), null);
  await w.settled();
  for (const [, m] of w.caches.stores) assert.ok(!m.has(chatUrl(CHAT)), "A's kept chat went with A's cache");
});

test("a sign-out drops every kept chat of the user", async () => {
  const w = worker();
  await opened(w, CHAT, chat(CHAT, "mine"));
  await opened(w, OTHER, chat(OTHER, "mine too"));
  assert.ok((await w.caches.keys()).some((k) => k.startsWith("oc-api-")));
  w.read(ORIGIN + "/api/v1/auths/signout", { method: "POST" });
  await w.settled();
  assert.ok(!(await w.caches.keys()).some((k) => k.startsWith("oc-api-")));
});

test("the switch off (CHAT_CACHE unset): a chat opened before waits for the network, as before", async () => {
  const w = worker({ chats: "0" });
  await opened(w, CHAT, chat(CHAT, "old"));
  const next = w.read(chatUrl(CHAT), { open: true });
  await asked(w, chatUrl(CHAT), 2);
  w.net.answer(chatUrl(CHAT), chat(CHAT, "new"));
  const res = await next;
  assert.equal(res.headers.get(KEPT), null);
  assert.equal(await text(res), "new");
});
