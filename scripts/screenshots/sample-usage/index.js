// Test-only Gateway plugin for the README screenshots (scripts/screenshots).
// The screenshot stack's "anthropic" provider is served by e2e/fake_llm.py and
// has no real quota, so the Status dialog would show "no rate limits". The pipe
// falls back to `claudeCliUsage.status` (gateway-plugins/claude-cli-usage) when
// usage.status has no anthropic windows; this answers that method with fixed
// sample numbers, so the bundled anthropic provider stays untouched. The docs
// caption says they are sample data. Never load this on a real Gateway.
//
// Two more props for the Status scenes (ELI-163):
// - The answer waits DELAY_FILE's milliseconds when that file exists, so a
//   scene can show the dialog with this one source still loading.
// - An `Agent` tool that answers the way Claude Code's Agent call does when it
//   starts a sub-agent in the background. The stack's model runs on the native
//   runtime, which has no such tool, and the Pipe's "Waiting on 1 background
//   agent" line keys on that answer.
import { readFileSync } from "node:fs";

const HOUR = 3_600_000;
const DELAY_FILE = "/tmp/openclaw-screenshot-usage-delay-ms";

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

function delayMs() {
  try {
    return Math.max(0, Number(readFileSync(DELAY_FILE, "utf8").trim()) || 0);
  } catch {
    return 0;
  }
}

export default {
  id: "screenshot-sample-usage",
  name: "Screenshot sample usage",
  description: "Test-only: claudeCliUsage.status with fixed sample rate-limit windows, and a sample Agent tool.",
  register(api) {
    api.registerGatewayMethod(
      "claudeCliUsage.status",
      async ({ respond }) => {
        const wait = delayMs();
        if (wait) await new Promise((resolve) => setTimeout(resolve, wait));
        respond(true, sampleSnapshot());
      },
      { scope: "operator.read" },
    );
    api.registerTool({
      name: "Agent",
      description: "Start a sub-agent on a task.",
      parameters: {
        type: "object",
        properties: {
          description: { type: "string" },
          prompt: { type: "string" },
          run_in_background: { type: "boolean" },
        },
        required: ["description", "prompt"],
      },
      async execute(_id, params) {
        const text = params.run_in_background
          ? "Async agent launched successfully.\nagentId: a51c7e0 (its report arrives when it finishes)"
          : `Report: ${params.description} is done.`;
        return { content: [{ type: "text", text }] };
      },
    });
  },
};
