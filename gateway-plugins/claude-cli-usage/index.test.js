// node --test gateway-plugins/claude-cli-usage/
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

const dir = mkdtempSync(path.join(os.tmpdir(), "claude-cli-usage-"));
process.env.CLAUDE_CONFIG_DIR = dir;
// What a Max 20x account that became Ultra had on disk: the login wrote "pro".
writeFileSync(path.join(dir, ".credentials.json"), JSON.stringify({
  claudeAiOauth: { accessToken: "t", expiresAt: Date.now() + 3600_000, subscriptionType: "pro", rateLimitTier: "default_claude_ai" },
}));

const { getClaudeCliUsage, profilePlanLabel, parseRetryAfter } = await import("./index.js");

const USAGE = { seven_day: { utilization: 4, resets_at: "2026-11-06T16:59:59Z" }, five_hour: null };

function fakeFetch(profile, calls) {
  return async (url) => {
    calls.push(url);
    if (url.endsWith("/usage")) return new Response(JSON.stringify(USAGE));
    if (profile === "fail") return new Response("{}", { status: 500 });
    return new Response(JSON.stringify(profile));
  };
}

test("the plan comes from the account profile, not the stale credentials file", async () => {
  const calls = [];
  const snap = await getClaudeCliUsage({ fetchFn: fakeFetch({ organization: { plan_display_name: "Ultra" } }, calls), now: 1e12 });
  assert.equal(snap.plan, "Ultra");
  assert.deepEqual(snap.windows.map((w) => w.label), ["Week"]);
  // the profile is cached for hours: the next usage fetch does not ask again
  const later = await getClaudeCliUsage({ fetchFn: fakeFetch("fail", calls), now: 1e12 + 120_000 });
  assert.equal(later.plan, "Ultra");
  assert.equal(calls.filter((u) => u.endsWith("/profile")).length, 1);
});

test("profilePlanLabel ignores a missing or empty name", () => {
  assert.equal(profilePlanLabel({ organization: { plan_display_name: " Max " } }), "Max");
  assert.equal(profilePlanLabel({ organization: { plan_display_name: "" } }), undefined);
  assert.equal(profilePlanLabel({}), undefined);
  assert.equal(profilePlanLabel(null), undefined);
});

test("without a profile the credentials file is the fallback label", async () => {
  const fresh = await import("./index.js?fallback");   // a module copy with empty caches
  const snap = await fresh.getClaudeCliUsage({ fetchFn: fakeFetch("fail", []), now: 1e12 });
  assert.equal(snap.plan, "Pro");
  assert.equal(snap.windows[0].usedPercent, 4);
});

test("a re-login with another account does not keep the old cached plan", async () => {
  const fresh = await import("./index.js?relogin");
  const calls = [];
  const login = (token) => writeFileSync(path.join(dir, ".credentials.json"), JSON.stringify({
    claudeAiOauth: { accessToken: token, expiresAt: Date.now() + 3600_000, subscriptionType: "pro" },
  }));
  login("token-a");
  const a = await fresh.getClaudeCliUsage({ fetchFn: fakeFetch({ organization: { plan_display_name: "Ultra" } }, calls), now: 1e12 });
  login("token-b");
  const b = await fresh.getClaudeCliUsage({ fetchFn: fakeFetch({ organization: { plan_display_name: "Pro" } }, calls), now: 1e12 + 120_000 });
  assert.equal(a.plan, "Ultra");
  assert.equal(b.plan, "Pro");
  assert.equal(calls.filter((u) => u.endsWith("/profile")).length, 2);
});

test("the profile request starts while the usage request is still in flight", async () => {
  const fresh = await import("./index.js?parallel");
  let profileStarted;
  const profileSeen = new Promise((r) => { profileStarted = r; });
  let startedBeforeUsageDone = false;
  const fetchFn = async (url) => {
    if (url.endsWith("/profile")) {
      profileStarted();
      return new Response(JSON.stringify({ organization: { plan_display_name: "Ultra" } }));
    }
    // a slow usage answer: the profile request must have started by the time it finishes
    startedBeforeUsageDone = await Promise.race([profileSeen.then(() => true), new Promise((r) => setTimeout(() => r(false), 200))]);
    return new Response(JSON.stringify(USAGE));
  };
  const snap = await fresh.getClaudeCliUsage({ fetchFn, now: 1e12 });
  assert.equal(startedBeforeUsageDone, true);
  assert.equal(snap.plan, "Ultra");
});

test("manifest declares reload.turnHandover", async () => {
  const { readFile } = await import("node:fs/promises");
  const manifest = JSON.parse(await readFile(new URL("./openclaw.plugin.json", import.meta.url), "utf8"));
  assert.equal(manifest.reload?.turnHandover, true);
});

// ---- 429 handling: the endpoint is not asked again while it is rate limiting us ----

function scriptedFetch(answers, calls) {
  // answers: the usage endpoint's replies in order, each "ok" or { status, retryAfter }; the profile always answers
  return async (url) => {
    if (!url.endsWith("/usage")) return new Response(JSON.stringify({ organization: { plan_display_name: "Max (20x)" } }));
    const a = answers.shift() ?? "ok";
    calls.push(a);
    if (a === "ok") return new Response(JSON.stringify(USAGE));
    return new Response("{}", { status: a.status, headers: a.retryAfter === undefined ? {} : { "retry-after": String(a.retryAfter) } });
  };
}

const T0 = 2e12;
// A controlled clock for the time a response takes: frozen, and a slow answer advances it by hand. No test depends on real elapsed time.
function fakeClock() {
  const c = { t: 0, now: () => c.t, advance: (ms) => { c.t += ms; } };
  return c;
}

test("a 429 with Retry-After is not retried before it is over, and the last good windows are served stale", async () => {
  const fresh = await import("./index.js?retry-after");
  const calls = [];
  const answers = ["ok", { status: 429, retryAfter: 300 }, "ok"];
  const clock = fakeClock();
  const get = (dt) => fresh.getClaudeCliUsage({ fetchFn: scriptedFetch(answers, calls), now: T0 + dt, clock: clock.now });
  assert.equal((await get(0)).stale, undefined);
  const first = await get(61_000);                       // cache expired: asks, gets the 429
  assert.equal(first.stale, true);
  assert.equal(first.staleReason, "Anthropic usage HTTP 429");
  assert.equal(first.windows[0].label, "Week");
  assert.equal(first.fetchedAt, T0);                     // still the last good fetch
  for (const dt of [100_000, 200_000, 360_000]) {        // inside the 300 s Retry-After: no request at all
    const held = await get(dt);
    assert.equal(held.stale, true);
    assert.equal(held.staleReason, "Anthropic usage HTTP 429");
  }
  assert.equal(calls.length, 2);                         // the first success and the 429, nothing else
  const back = await get(61_000 + 300_000);              // Retry-After over: asks again, and a success clears the stale flag
  assert.equal(back.stale, undefined);
  assert.equal(back.fetchedAt, T0 + 361_000);
  assert.equal(calls.length, 3);
});

test("a 429 without Retry-After holds one cache period, the same one-fetch-per-minute as the success path", async () => {
  const fresh = await import("./index.js?no-retry-after");
  const calls = [];
  const answers = ["ok", { status: 429 }, { status: 429 }, "ok"];
  const clock = fakeClock();
  const get = (dt) => fresh.getClaudeCliUsage({ fetchFn: scriptedFetch(answers, calls), now: T0 + dt, clock: clock.now });
  await get(0);
  await get(61_000);                                     // 429
  for (const dt of [70_000, 100_000, 120_000]) assert.equal((await get(dt)).stale, true);
  assert.equal(calls.length, 2);                         // held until 61 s + 60 s
  await get(121_000);                                    // the hold is over exactly one cache period after the 429: asks again, another 429
  assert.equal(calls.length, 3);
  assert.equal((await get(150_000)).stale, true);
  assert.equal(calls.length, 3);
});

test("Retry-After as seconds or an HTTP date, absolute from receipt, capped at 5 h", () => {
  const at = Date.parse("2026-11-23T10:00:00Z");
  assert.equal(parseRetryAfter("120", at), at + 120_000);
  assert.equal(parseRetryAfter("Mon, 23 Nov 2026 10:05:00 GMT", at), at + 300_000);   // absolute, whatever the receipt time
  assert.equal(parseRetryAfter("Mon, 23 Nov 2026 09:00:00 GMT", at), at);              // a date in the past: no extra hold
  assert.equal(parseRetryAfter("999999999", at), at + 5 * 3600_000);
  for (const bad of [undefined, null, "", "soon", "-5"]) assert.equal(parseRetryAfter(bad, at), undefined);
});

test("a relative Retry-After runs from when the answer arrived, not from when the request was sent", async () => {
  const fresh = await import("./index.js?receipt");
  const clock = fakeClock();
  const calls = [];
  const SLOW_MS = 7_000;                                        // the 429 takes seven seconds to come back
  let slow = false;
  const fetchFn = async (url) => {
    if (!url.endsWith("/usage")) return new Response("{}");
    calls.push(url);
    if (!slow) return new Response(JSON.stringify(USAGE));
    clock.advance(SLOW_MS);
    return new Response("{}", { status: 429, headers: { "retry-after": "300" } });
  };
  const get = (dt) => fresh.getClaudeCliUsage({ fetchFn, now: T0 + dt, clock: clock.now });
  await get(0);
  slow = true;
  await get(61_000);                                            // asked at 61 s, answered at 68 s: held until 368 s, not 361 s
  assert.equal(calls.length, 2);
  await get(61_000 + 300_000);                                  // the naive deadline (request time + 300 s): still held
  await get(61_000 + 300_000 + SLOW_MS - 1);
  assert.equal(calls.length, 2);
  slow = false;
  await get(61_000 + 300_000 + SLOW_MS);                        // receipt + 300 s: asks again
  assert.equal(calls.length, 3);
});

test("a failure with nothing cached yet is held too", async () => {
  const fresh = await import("./index.js?cold");
  const calls = [];
  const answers = [{ status: 429, retryAfter: 600 }, "ok"];
  const clock = fakeClock();
  const get = (dt) => fresh.getClaudeCliUsage({ fetchFn: scriptedFetch(answers, calls), now: T0 + dt, clock: clock.now });
  const cold = await get(0);
  assert.deepEqual(cold.windows, []);
  assert.equal(cold.error, "Anthropic usage HTTP 429");
  assert.equal((await get(200_000)).error, "Anthropic usage HTTP 429");         // after the 60 s cache, inside Retry-After: no request
  assert.equal(calls.length, 1);
  assert.equal((await get(601_000)).windows[0].label, "Week");
  assert.equal(calls.length, 2);
});

test("a thrown fetch (timeout, network) is held one cache period as well", async () => {
  const fresh = await import("./index.js?throws");
  const clock = fakeClock();
  let n = 0;
  const fetchFn = async (url) => {
    if (!url.endsWith("/usage")) return new Response("{}", { status: 500 });
    n += 1;
    if (n === 1) return new Response(JSON.stringify(USAGE));
    throw new Error("The operation was aborted due to timeout");
  };
  await fresh.getClaudeCliUsage({ fetchFn, now: T0, clock: clock.now });
  const a = await fresh.getClaudeCliUsage({ fetchFn, now: T0 + 61_000, clock: clock.now });
  assert.equal(a.stale, true);
  assert.match(a.staleReason, /timeout/);
  await fresh.getClaudeCliUsage({ fetchFn, now: T0 + 90_000, clock: clock.now });
  assert.equal(n, 2);
});
