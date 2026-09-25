// Gateway RPC `claudeCliUsage.status`: Claude subscription rate-limit windows
// (5h / weekly) for setups whose only Anthropic credential is the Claude CLI
// login.
//
// Why this exists: OpenClaw 2026.9.1 (openclaw#129052) stopped resolving the
// claude-cli auth profile for `usage.status`, because OpenClaw refreshing
// Claude CLI's OAuth token invalidated Claude's own login. With no other
// Anthropic credential, `usage.status` has no `anthropic` entry at all and the
// OWUI status dialog shows no rate limits.
//
// This reads Claude CLI's current access token READ-ONLY and never refreshes,
// writes, or copies it. If the token is expired it reports that and waits for
// Claude CLI to refresh it on its own next run.
import { readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const USAGE_URL = "https://api.anthropic.com/api/oauth/usage";
const FETCH_TIMEOUT_MS = 8000;
// The usage endpoint rate-limits aggressively; one fetch per minute is plenty
// for a dialog a human opens by hand.
const CACHE_TTL_MS = 60_000;

let cached = null; // { at, snapshot }

function credentialsPath() {
  const dir = process.env.CLAUDE_CONFIG_DIR?.trim() || path.join(os.homedir(), ".claude");
  return path.join(dir, ".credentials.json");
}

function clampPercent(value) {
  return Math.max(0, Math.min(100, value));
}

function parseResetAt(raw) {
  if (typeof raw !== "string") return undefined;
  const ms = Date.parse(raw);
  return Number.isFinite(ms) ? ms : undefined;
}

function readWindow(data, key, label) {
  const raw = data?.[key];
  if (!raw || typeof raw.utilization !== "number") return undefined;
  const resetAt = parseResetAt(raw.resets_at);
  return { label, usedPercent: clampPercent(raw.utilization), ...(resetAt ? { resetAt } : {}) };
}

// Same window selection and labels as OpenClaw's own Claude usage fetcher
// (provider-usage.fetch.claude), so the dialog reads identically either way.
export function buildWindows(data) {
  const windows = [];
  for (const [key, label] of [["five_hour", "5h"], ["seven_day", "Week"]]) {
    const w = readWindow(data, key, label);
    if (w) windows.push(w);
  }
  const modelWindow = readWindow(data, "seven_day_sonnet", "Sonnet") ?? readWindow(data, "seven_day_opus", "Opus");
  if (modelWindow) windows.push(modelWindow);
  const extra = data?.extra_usage;
  if (extra?.is_enabled === true && typeof extra.utilization === "number") {
    windows.push({ label: "Extra usage", usedPercent: clampPercent(extra.utilization) });
  }
  return windows;
}

function planLabel(oauth) {
  const base = typeof oauth.subscriptionType === "string" ? oauth.subscriptionType.trim() : "";
  if (!base) return undefined;
  const label = base.charAt(0).toUpperCase() + base.slice(1);
  const tier = typeof oauth.rateLimitTier === "string" ? oauth.rateLimitTier.match(/_(\d+x)$/i)?.[1] : undefined;
  return tier ? `${label} (${tier})` : label;
}

async function fetchSnapshot(fetchFn = fetch) {
  const base = { provider: "anthropic", displayName: "Claude", source: "claude-cli" };
  let oauth;
  try {
    oauth = JSON.parse(await readFile(credentialsPath(), "utf8"))?.claudeAiOauth;
  } catch (err) {
    return { ...base, windows: [], error: `Claude CLI credentials unreadable: ${err.code ?? err.message}` };
  }
  if (!oauth?.accessToken) return { ...base, windows: [], error: "Claude CLI is not logged in" };
  if (typeof oauth.expiresAt === "number" && oauth.expiresAt <= Date.now()) {
    return { ...base, windows: [], error: "Claude CLI token expired; it refreshes on Claude CLI's next run" };
  }

  const res = await fetchFn(USAGE_URL, {
    headers: {
      Authorization: `Bearer ${oauth.accessToken}`,
      Accept: "application/json",
      "User-Agent": "openclaw",
      "anthropic-version": "2023-06-01",
      "anthropic-beta": "oauth-2025-04-20",
    },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!res.ok) {
    await res.body?.cancel().catch(() => {});
    return { ...base, windows: [], error: `Anthropic usage HTTP ${res.status}` };
  }
  const plan = planLabel(oauth);
  return { ...base, windows: buildWindows(await res.json()), ...(plan ? { plan } : {}) };
}

export async function getClaudeCliUsage({ fetchFn, now = Date.now() } = {}) {
  if (cached && now - cached.at < CACHE_TTL_MS) return cached.snapshot;
  let snapshot;
  try {
    snapshot = await fetchSnapshot(fetchFn);
  } catch (err) {
    snapshot = { provider: "anthropic", displayName: "Claude", source: "claude-cli", windows: [], error: String(err?.message ?? err) };
  }
  // A transient failure (429, timeout) keeps serving the last good windows,
  // marked stale, rather than blanking the dialog.
  if (snapshot.error && cached?.snapshot.windows.length) {
    return { ...cached.snapshot, stale: true, staleReason: snapshot.error };
  }
  cached = { at: now, snapshot: { ...snapshot, fetchedAt: now } };
  return cached.snapshot;
}

export default {
  id: "claude-cli-usage",
  name: "Claude CLI usage",
  description: "Gateway RPC claudeCliUsage.status: Claude subscription rate-limit windows via Claude CLI's login, read-only.",
  register(api) {
    api.registerGatewayMethod(
      "claudeCliUsage.status",
      async ({ respond }) => {
        respond(true, await getClaudeCliUsage());
      },
      { scope: "operator.read" },
    );
  },
};
