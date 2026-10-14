// Test-only Gateway plugin for the README screenshots (scripts/screenshots).
// The screenshot stack's model provider ("local", served by e2e/fake_llm.py)
// has no real quota, so `usage.status` would report no windows and the Status
// dialog would show "no rate limits". This serves fixed sample numbers through
// OpenClaw's provider usage hooks instead. The docs caption says they are
// sample data. Never load this on a real Gateway.
const HOUR = 3_600_000;

export function sampleSnapshot(now = Date.now()) {
  return {
    provider: "local",
    displayName: "Local",
    plan: "Pro",
    windows: [
      { label: "5h", usedPercent: 38, resetAt: now + 2 * HOUR + 14 * 60_000 },
      { label: "Week", usedPercent: 61, resetAt: now + 3 * 24 * HOUR + 5 * HOUR },
    ],
  };
}

export default {
  id: "screenshot-sample-usage",
  name: "Screenshot sample usage",
  description: "Test-only: fixed rate-limit windows for the screenshot stack's \"local\" provider.",
  register(api) {
    api.registerProvider({
      id: "local",
      label: "Local",
      auth: [],
      resolveUsageAuth: async () => ({ token: "sample" }),
      fetchUsageSnapshot: async () => sampleSnapshot(),
    });
  },
};
