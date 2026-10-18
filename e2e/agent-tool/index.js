// Test-only Gateway plugin for the end-to-end stack (e2e/start_stack.sh): an
// `Agent` tool that answers the way Claude Code's Agent call does. The stack's
// model runs on the native runtime, which has no such tool, and the Pipe and
// the Status Action key on Claude Code's answers: "Async agent launched" is a
// sub-agent left running in the background. Never load this on a real Gateway.
// (The screenshot stack has the same tool in scripts/screenshots/sample-usage.)
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
  },
};
