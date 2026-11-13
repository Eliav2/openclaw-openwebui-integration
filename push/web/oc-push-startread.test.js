// node --test push/web/oc-push-startread.test.js: oc-push.js's start reads after a network change
// (ELI-362, ELI-374), run in a sandbox page. A network change fails the reads on their way; Open WebUI
// asks each start read once and goes to /error ("Backend Required") when one fails.
//   - No worker keeping the app: the page asks again at once (ELI-362, unchanged).
//   - A worker keeping the app has already asked twice, at once, on the same dead network (oc-sw.js
//     again()): the page waits for the next 'online' or network change and asks once more (ELI-374).
import test from "node:test";
import assert from "node:assert";
import fs from "node:fs";
import vm from "node:vm";

const PUSH = process.env.OC_PUSH || new URL("oc-push.js", import.meta.url);
const CONFIG = "https://chat.example/api/config";
const down = () => new TypeError("Failed to fetch");

/** A page whose fetch answers from `answers` (one per call, in order): a Response-like object or an Error to throw. */
function page({ worker = true, onLine = true, answers = [] } = {}) {
  const listeners = {}, connection = {}, calls = [];
  const on = (bag) => (type, fn) => { (bag[type] ||= []).push(fn); };
  const sandbox = {
    navigator: {
      userAgent: "Mozilla/5.0 (Linux; Android 14) Chrome/140",
      onLine,
      connection: { addEventListener: on(connection) },
      serviceWorker: worker ? { controller: { postMessage() {} }, addEventListener() {}, startMessages() {} } : undefined,
    },
    caches: { has: async (name) => worker && name === "oc-shell-v1" },
    document: { documentElement: {}, getElementById: () => null, querySelector: () => null },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    MutationObserver: class { observe() {} disconnect() {} },
    location: { href: "https://chat.example/", origin: "https://chat.example", reload() {} },
    fetch: async (input, init) => {
      calls.push(String(input));
      const a = answers[calls.length - 1];
      if (a instanceof Error) throw a;
      return a ?? { ok: true, status: 200 };
    },
    addEventListener: on(listeners),
    URL, Request: class {}, console,
  };
  sandbox.window = sandbox;
  vm.runInNewContext(fs.readFileSync(PUSH, "utf8"), sandbox);
  const settle = () => new Promise((r) => setImmediate(r));
  return {
    read: (url = CONFIG, init) => sandbox.window.fetch(url, init),
    online: async () => { sandbox.navigator.onLine = true; for (const fn of listeners.online || []) fn({}); await settle(); },
    change: async () => { for (const fn of connection.change || []) fn({}); await settle(); },
    setOnLine: (v) => { sandbox.navigator.onLine = v; },
    settle,
    get calls() { return calls; },
  };
}
/** Whether `p` has settled by now (a pending promise stays pending across a macrotask). */
const state = async (p, settle) => {
  let s = "pending";
  p.then(() => { s = "resolved"; }, () => { s = "rejected"; });
  await settle();
  return s;
};

test("no worker keeping the app: a start read that fails on the network is asked once more at once (ELI-362)", async () => {
  const pg = page({ worker: false, answers: [down()] });
  const res = await pg.read();
  assert.equal(res.ok, true);
  assert.equal(pg.calls.length, 2);
});

test("a worker keeps the app, the read failed with no network: it waits for 'online', asks once more, and returns that answer", async () => {
  const pg = page({ onLine: false, answers: [down(), { ok: true, status: 200, body: "config" }] });
  const p = pg.read();
  assert.equal(await state(p, pg.settle), "pending");
  assert.equal(pg.calls.length, 1, "no ask while the network is gone");
  await pg.online();
  assert.equal((await p).body, "config");
  assert.equal(pg.calls.length, 2);
});

test("a network change (the connection API) releases the wait too", async () => {
  const pg = page({ onLine: false, answers: [down(), { ok: true, status: 200 }] });
  const p = pg.read();
  await pg.settle();
  await pg.change();
  assert.equal((await p).ok, true);
  assert.equal(pg.calls.length, 2);
});

test("the network came back while the worker was still asking: no wait, one more ask at once", async () => {
  const pg = page({ answers: [down(), { ok: true, status: 200 }] });
  // 'online' fires between the read's start and its failure (the first ask is still failing).
  const p = pg.read();
  await pg.online();
  assert.equal((await p).ok, true);
  assert.equal(pg.calls.length, 2);
});

test("the network is up and nothing changed (the server is down): the failure goes to Open WebUI as ever", async () => {
  const pg = page({ answers: [down()] });
  await assert.rejects(pg.read(), /Failed to fetch/);
  assert.equal(pg.calls.length, 1);
});

test("the ask after the wait is the last: if it fails too, Open WebUI gets that failure", async () => {
  const pg = page({ onLine: false, answers: [down(), down()] });
  const p = pg.read();
  const settled = assert.rejects(p, /Failed to fetch/);
  await pg.settle();
  await pg.online();
  await settled;
  assert.equal(pg.calls.length, 2);
  await pg.online();
  assert.equal(pg.calls.length, 2, "a later change asks nothing more");
});

test("Open WebUI cancelling the read while it waits ends the wait with no ask", async () => {
  const pg = page({ onLine: false, answers: [down()] });
  const ctl = new AbortController();
  const p = pg.read(CONFIG, { signal: ctl.signal });
  const settled = assert.rejects(p);
  await pg.settle();
  ctl.abort();
  await settled;
  await pg.online();
  assert.equal(pg.calls.length, 1);
});

test("a read that is not a start read is left alone", async () => {
  const pg = page({ onLine: false, answers: [down(), down()] });
  await assert.rejects(pg.read("https://chat.example/api/v1/chats/new", { method: "POST" }), /Failed to fetch/);
  await assert.rejects(pg.read("https://chat.example/api/v1/tasks"), /Failed to fetch/);
  assert.equal(pg.calls.length, 2);
});
