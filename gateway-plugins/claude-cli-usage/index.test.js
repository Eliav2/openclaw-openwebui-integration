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

const { getClaudeCliUsage, profilePlanLabel } = await import("./index.js");

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
