// Test-only Gateway plugin for the README screenshots (scripts/screenshots).
// The screenshot stack's "anthropic" provider is served by e2e/fake_llm.py and
// has no real quota, so the Status dialog would show "no rate limits". The pipe
// falls back to `claudeCliUsage.status` (gateway-plugins/claude-cli-usage) when
// usage.status has no anthropic windows; this answers that method with fixed
// sample numbers, so the bundled anthropic provider stays untouched. The docs
// caption says they are sample data. Never load this on a real Gateway.
const HOUR = 3_600_000;

export function sampleSnapshot(now = Date.now()) {
  return {
    provider: "anthropic",
    displayName: "Anthropic",
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
  description: "Test-only: claudeCliUsage.status with fixed sample rate-limit windows.",
  register(api) {
    api.registerGatewayMethod(
      "claudeCliUsage.status",
      async ({ respond }) => {
        respond(true, sampleSnapshot());
      },
      { scope: "operator.read" },
    );
  },
};
