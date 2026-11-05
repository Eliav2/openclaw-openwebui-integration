// node --test push/web/oc-sw.test.js: the service worker's push handler, run in a
// sandbox with a fake registration (Chrome's tag rules: a new notification with a
// tag replaces the shown one with that tag; close() removes it).
import test from "node:test";
import assert from "node:assert";
import fs from "node:fs";
import vm from "node:vm";

const SW = process.env.OC_SW || new URL("oc-sw.js", import.meta.url);
const MAX_LINES = 5;

function worker() {
  const listeners = {};
  const shown = new Map();   // tag -> notification
  const registration = {
    async getNotifications({ tag } = {}) {
      return [...shown.values()].filter((n) => !tag || n.tag === tag);
    },
    async showNotification(title, options) {
      const n = { title, ...options, close: () => shown.delete(options.tag) };
      shown.set(options.tag, n);
    },
  };
  const self = {
    navigator: { userAgent: "Mozilla/5.0 (Linux; Android 14) Chrome/140", maxTouchPoints: 0 },
    location: { origin: "https://chat.example" },
    registration,
    clients: { matchAll: async () => [], claim: async () => {} },
    addEventListener: (type, fn) => { listeners[type] = fn; },
  };
  vm.runInNewContext(fs.readFileSync(SW, "utf8"), { self, URL, console, setTimeout, clearTimeout });
  async function push(payload) {
    let done;
    listeners.push({ data: { json: () => payload }, waitUntil: (pr) => { done = pr; } });
    await done;
  }
  return { push, shown, tag: (id) => shown.get("chat-" + id) };
}

const reply = (chat, body, title = "Trip plan") => ({ chat_id: chat, title, body, url: "/c/" + chat });

test("one reply: the chat's title and the preview, as before", async () => {
  const w = worker();
  await w.push(reply("a", "First answer"));
  assert.equal(w.tag("a").title, "Trip plan");
  assert.equal(w.tag("a").body, "First answer");
});

test("replies in one chat stack in one notification: the count in the title, newest last", async () => {
  const w = worker();
  await w.push(reply("a", "First answer"));
  await w.push(reply("a", "Second answer"));
  await w.push(reply("a", "Third answer"));
  assert.equal(w.shown.size, 1);
  assert.equal(w.tag("a").title, "Trip plan · 3 new messages");
  assert.equal(w.tag("a").body, "First answer\nSecond answer\nThird answer");
  assert.equal(w.tag("a").renotify, true);
});

test("the body keeps the latest lines only, each on one trimmed line", async () => {
  const w = worker();
  for (let i = 1; i <= MAX_LINES + 2; i++) await w.push(reply("a", `  answer\n ${i}  `));
  const lines = w.tag("a").body.split("\n");
  assert.equal(lines.length, MAX_LINES);
  assert.equal(lines[0], "answer 3");
  assert.equal(lines.at(-1), `answer ${MAX_LINES + 2}`);
  assert.match(w.tag("a").title, new RegExp(`${MAX_LINES + 2} new messages`));
});

test("a long preview is cut to one line with an ellipsis", async () => {
  const w = worker();
  await w.push(reply("a", "word ".repeat(100)));
  await w.push(reply("a", "short"));
  const [first] = w.tag("a").body.split("\n");
  assert.ok(first.endsWith("…") && first.length < 200, first);
});

test("another chat has its own notification and count", async () => {
  const w = worker();
  await w.push(reply("a", "one"));
  await w.push(reply("a", "two"));
  await w.push(reply("b", "other", "Groceries"));
  assert.equal(w.shown.size, 2);
  assert.equal(w.tag("b").title, "Groceries");
  assert.equal(w.tag("b").body, "other");
});

test("a closed notification (chat opened, marked read, swiped) starts the stack again", async () => {
  const w = worker();
  await w.push(reply("a", "one"));
  await w.push(reply("a", "two"));
  w.tag("a").close();
  await w.push(reply("a", "three"));
  assert.equal(w.tag("a").title, "Trip plan");
  assert.equal(w.tag("a").body, "three");
});

test("without previews the body stays 'New reply' and the title still counts", async () => {
  const w = worker();
  await w.push({ chat_id: "a", title: "Trip plan", url: "/c/a" });
  await w.push({ chat_id: "a", title: "Trip plan", url: "/c/a" });
  assert.equal(w.tag("a").body, "New reply");
  assert.equal(w.tag("a").title, "Trip plan · 2 new messages");
});
