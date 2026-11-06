// node --test push/web/oc-push-recover.test.js: oc-push.js's reload after a start that
// a network change broke (ELI-352, ELI-360), run in a sandbox page. The failures
// come as the events the browser and Vite send; the server answers the probe or not.
import test from "node:test";
import assert from "node:assert";
import fs from "node:fs";
import vm from "node:vm";

const PUSH = process.env.OC_PUSH || new URL("oc-push.js", import.meta.url);
const LOST = "TypeError: Failed to fetch dynamically imported module: https://chat.example/_app/immutable/nodes/0.Cb3rY7DB.js";

function page({ reloadedBefore = false } = {}) {
  const listeners = {}, connection = {}, storage = new Map(), observers = [];
  if (reloadedBefore) storage.set("ocReloadedAfterError", "1");
  let splash = true, server = true, reloads = 0, probes = 0;
  const on = (bag) => (type, fn) => { (bag[type] ||= []).push(fn); };
  const sandbox = {
    navigator: { userAgent: "Mozilla/5.0 (Linux; Android 14) Chrome/140", connection: { addEventListener: on(connection) } },
    document: {
      documentElement: {},
      getElementById: (id) => (id === "splash-screen" && splash ? {} : null),
      querySelector: () => null,   // Open WebUI's error page has none of SvelteKit's static one
    },
    sessionStorage: {
      getItem: (k) => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: (k) => storage.delete(k),
    },
    MutationObserver: class {
      constructor(fn) { this.fn = fn; this.on = false; observers.push(this); }
      observe() { this.on = true; }
      disconnect() { this.on = false; }
    },
    location: { href: "https://chat.example/", origin: "https://chat.example", reload: () => { reloads += 1; } },
    fetch: async (url, init) => {
      assert.equal(url, "/_app/version.json");
      assert.equal(init.method, "HEAD");
      probes += 1;
      if (!server) throw new TypeError("Failed to fetch");
      return { ok: true };
    },
    addEventListener: on(listeners),
    URL, Request: class {}, console,
  };
  sandbox.window = sandbox;
  vm.runInNewContext(fs.readFileSync(PUSH, "utf8"), sandbox);
  const settle = () => new Promise((r) => setImmediate(r));
  const fire = async (type, event = {}) => {
    for (const fn of listeners[type] || []) fn(event);
    await settle();
  };
  return {
    fire,
    preloadError: () => fire("vite:preloadError", { payload: new TypeError(LOST) }),
    rejection: (reason) => fire("unhandledrejection", { reason }),
    networkChange: async () => { for (const fn of connection.change || []) fn({}); await settle(); },
    appUp: () => { splash = false; for (const o of observers) if (o.on) o.fn([]); },
    serverDown: () => { server = false; },
    serverUp: () => { server = true; },
    get reloads() { return reloads; },
    get probes() { return probes; },
    guard: () => storage.get("ocReloadedAfterError") ?? null,
  };
}

test("a page part that does not come (vite:preloadError): one reload once the server answers", async () => {
  const p = page();
  await p.preloadError();
  assert.equal(p.reloads, 1);
  assert.equal(p.guard(), "1");
});

test("SvelteKit's own import failing (the blank page): its unhandled rejection reloads", async () => {
  const p = page();
  await p.rejection(new TypeError(LOST.replace("nodes/0.Cb3rY7DB.js", "entry/start.BHzHmkW5.js")));
  assert.equal(p.reloads, 1);
});

test("other unhandled rejections are not a lost file", async () => {
  const p = page();
  await p.rejection(new Error("401 Unauthorized"));
  await p.rejection(undefined);
  assert.equal(p.reloads, 0);
  assert.equal(p.probes, 0);
});

test("no network yet: no reload; the next 'online' or network change reloads", async () => {
  const p = page();
  p.serverDown();
  await p.preloadError();
  assert.equal(p.reloads, 0);
  await p.fire("online");
  assert.equal(p.reloads, 0);
  p.serverUp();
  await p.networkChange();
  assert.equal(p.reloads, 1);
});

test("'online' or a network change with nothing lost reloads nothing", async () => {
  const p = page();
  await p.fire("online");
  await p.networkChange();
  assert.equal(p.reloads, 0);
  assert.equal(p.probes, 0);
});

test("a page that fails again after its reload stays as it is", async () => {
  const p = page({ reloadedBefore: true });
  await p.preloadError();
  await p.fire("online");
  assert.equal(p.reloads, 0);
});

test("once the app is up: the guard is cleared and a later lost part reloads nothing", async () => {
  const p = page({ reloadedBefore: true });
  p.appUp();
  assert.equal(p.guard(), null);
  await p.preloadError();
  await p.fire("online");
  assert.equal(p.reloads, 0);
});

test("the error page drawn after a lost part does not count as the app being up", async () => {
  const p = page();
  p.serverDown();
  await p.preloadError();
  p.appUp();   // Open WebUI's layout drew its error page: the splash is gone
  p.serverUp();
  await p.fire("online");
  assert.equal(p.reloads, 1);
});
