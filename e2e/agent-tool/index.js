// Test-only Gateway plugin for the end-to-end stack (e2e/start_stack.sh): an
// `Agent` tool that answers the way Claude Code's Agent call does. The stack's
// model runs on the native runtime, which has no such tool, and the Pipe and
// the Status Action key on Claude Code's answers: "Async agent launched" is a
// sub-agent left running in the background. Never load this on a real Gateway.
// (The screenshot stack has the same tool in scripts/screenshots/sample-usage.)
//
// It also has `e2e_compact`: after a call to it, the run reports a context
// compaction (a start and a completed end on its `compaction` stream), the
// events claude-cli's runs send when Claude Code compacts. The native runtime
// sends none for a compaction a test can bring about, so this stands in for it.
//
// And `e2e_guarded`, a tool whose every call asks for a plugin approval (a
// `before_tool_call` hook's `requireApproval`), the way a plugin guards a
// sensitive action. It answers "E2E_GUARDED_RAN" only when the call is allowed.
import { emitAgentEvent } from "openclaw/plugin-sdk/agent-harness-runtime";

// Runs that already reported theirs: a call made through the deferred
// `tool_call`/`dispatch_action` may be seen under both names.
const reported = new Set();

function isCompactCall(event) {
  const p = event.params || {};
  return event.toolName === "e2e_compact" || p.id === "e2e_compact" || p.name === "e2e_compact";
}

export default {
  id: "e2e-agent-tool",
  name: "E2E Agent tool",
  description: "Test-only: an Agent tool that answers like Claude Code's Agent call.",
  register(api) {
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
          ? "Async agent launched successfully.\nagentId: e2e0001 (its report arrives when it finishes)"
          : `Report: ${params.description} is done.`;
        return { content: [{ type: "text", text }] };
      },
    });
    api.registerTool({
      name: "e2e_compact",
      description: "Test-only: the run reports a context compaction after this call.",
      parameters: { type: "object", properties: {} },
      async execute() {
        return { content: [{ type: "text", text: "compaction reported" }] };
      },
    });
    api.registerTool({
      name: "e2e_guarded",
      description: "Test-only: a tool that needs a plugin approval before it runs.",
      parameters: { type: "object", properties: { label: { type: "string" } } },
      async execute() {
        return { content: [{ type: "text", text: "E2E_GUARDED_RAN" }] };
      },
    });
    api.on(
      "before_tool_call",
      (event) => ({
        requireApproval: {
          title: "E2E guarded action",
          description: "Allow the e2e_guarded tool to run?",
          severity: "warning",
          // label "short": a call whose approval expires within seconds.
          timeoutMs: event.params?.label === "short" ? 6_000 : 120_000,
        },
      }),
      { matcher: ["e2e_guarded"], priority: 50 },
    );
    api.on("after_tool_call", (event, ctx) => {
      const runId = event.runId ?? ctx?.runId;
      if (!runId || !isCompactCall(event) || reported.has(runId)) return;
      reported.add(runId);
      emitAgentEvent({ runId, stream: "compaction", data: { phase: "start", backend: "e2e" } });
      emitAgentEvent({ runId, stream: "compaction", data: { phase: "end", completed: true, backend: "e2e" } });
    });
  },
};
