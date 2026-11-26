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
// And `map`, a stand-in for the Gateway plugin of the same name (gateway-plugins/map in the
// openclaw-workspace repo): it answers every call with one fixed map and serves it on `map.get`,
// so the Pipe's map embed is tested with no network.
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

// A map as `map.get` returns it: Kyoto, three stops, one walking route (polyline precision 5).
const E2E_MAP_ID = "00000000e2e00001";
const E2E_MAP = {
  ok: true,
  title: "E2E Kyoto",
  stops: [
    { n: 1, label: "Fushimi Inari", lat: 34.9671, lon: 135.7727, group: "Day 1", place: "Fushimi Inari Taisha, Kyoto" },
    { n: 2, label: "Kiyomizu-dera", lat: 34.9949, lon: 135.785, group: "Day 1", note: "Go early" },
    { n: 3, label: "Gion", lat: 35.0037, lon: 135.7756, group: "Day 1" },
  ],
  unresolved: [],
  routes: [{ group: "Day 1", mode: "walk", distanceM: 5860.4, durationS: 4689.9, polyline: "koltEkbu{XsoAsl@c|Ag^_v@vy@" }],
};

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
      name: "map",
      description: "Test-only: shows a fixed map of Kyoto.",
      parameters: { type: "object", properties: { stops: { type: "array" } } },
      async execute() {
        const { title, stops, unresolved, routes } = E2E_MAP;
        const result = { mapId: E2E_MAP_ID, title, stops, routes: routes.map(({ polyline, ...r }) => r), unresolved, note: "shown" };
        return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
      },
    });
    api.registerGatewayMethod(
      "map.get",
      ({ params, respond }) => respond(true, params?.mapId === E2E_MAP_ID ? E2E_MAP : { ok: false, error: "unknown or expired map id" }),
      { scope: "operator.read" },
    );
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
