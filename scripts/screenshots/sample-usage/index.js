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
// - A context compaction mid-reply (ELI-283): after a tool call that reads
//   COMPACT_AFTER, the run reports one on its `compaction` stream, as a
//   claude-cli run does when Claude Code compacts. The native runtime sends
//   none a scene can bring about.
// - A `deploy_release` tool that a `before_tool_call` hook holds for a plugin
//   approval, as a plugin guards a sensitive action, for the approval card.
import { readFileSync } from "node:fs";
import { emitAgentEvent } from "openclaw/plugin-sdk/agent-harness-runtime";

const HOUR = 3_600_000;
const DELAY_FILE = "/tmp/openclaw-screenshot-usage-delay-ms";
const COMPACT_AFTER = "ci/build-logs/this-week.log";
const compacted = new Set();

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
  description: "Test-only: claudeCliUsage.status with fixed sample rate-limit windows, a sample Agent tool, and a sample compaction.",
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
    api.registerTool({
      name: "deploy_release",
      description: "Deploy a release of the forecast app.",
      parameters: { type: "object", properties: { version: { type: "string" }, target: { type: "string" } } },
      async execute(_id, params) {
        return { content: [{ type: "text", text: `Deployed ${params.version ?? "the release"} to ${params.target ?? "production"}.` }] };
      },
    });
    api.on(
      "before_tool_call",
      (event) => ({
        requireApproval: {
          title: "Deploy to production",
          description: `Deploy forecast-app ${event.params?.version ?? ""} to production? Users see it at once.`,
          severity: "warning",
          timeoutMs: 600_000,
        },
      }),
      { matcher: ["deploy_release"], priority: 50 },
    );
    api.on("after_tool_call", (event, ctx) => {
      const runId = event.runId ?? ctx?.runId;
      if (!runId || compacted.has(runId) || !JSON.stringify(event.params ?? {}).includes(COMPACT_AFTER)) return;
      compacted.add(runId);
      emitAgentEvent({ runId, stream: "compaction", data: { phase: "start", backend: "sample" } });
      emitAgentEvent({ runId, stream: "compaction", data: { phase: "end", completed: true, backend: "sample" } });
    });
  },
};
