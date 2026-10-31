// Gateway RPC `claudeCliUsage.status`: Claude subscription rate-limit windows
// (weekly, plus 5h on plans that have one) for setups whose only Anthropic credential is the Claude CLI
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
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const USAGE_URL = "https://api.anthropic.com/api/oauth/usage";
const PROFILE_URL = "https://api.anthropic.com/api/oauth/profile";
const FETCH_TIMEOUT_MS = 8000;
// The usage endpoint rate-limits aggressively; one fetch per minute is plenty
// for a dialog a human opens by hand.
const CACHE_TTL_MS = 60_000;

let cached = null; // { at, snapshot }
// The plan changes rarely; asking for it every few hours is enough.
const PLAN_TTL_MS = 6 * 3600_000;
// Keyed on a hash of the access token: another login (or a token refresh) asks again.
let cachedPlan = null; // { key, at, plan }

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

// The credentials file's subscriptionType is written at login and can be
// stale or coarse ("pro" on a Max 20x / Ultra account), so the label comes from
// the account profile when it can, and from the file only as a fallback.
function planLabel(oauth) {
  const base = typeof oauth.subscriptionType === "string" ? oauth.subscriptionType.trim() : "";
  if (!base) return undefined;
  const label = base.charAt(0).toUpperCase() + base.slice(1);
  const tier = typeof oauth.rateLimitTier === "string" ? oauth.rateLimitTier.match(/_(\d+x)$/i)?.[1] : undefined;
  return tier ? `${label} (${tier})` : label;
}

export function profilePlanLabel(profile) {
  const name = profile?.organization?.plan_display_name;
  return typeof name === "string" && name.trim() ? name.trim() : undefined;
}

async function fetchPlan(oauth, headers, fetchFn, now) {
  const key = createHash("sha256").update(oauth.accessToken).digest("hex");
  if (cachedPlan?.key === key && now - cachedPlan.at < PLAN_TTL_MS) return cachedPlan.plan;
  let plan;
  try {
    const res = await fetchFn(PROFILE_URL, { headers, signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
    if (res.ok) plan = profilePlanLabel(await res.json());
    else await res.body?.cancel().catch(() => {});
  } catch {
    // the profile is only a label; the windows matter more
  }
  if (plan) cachedPlan = { key, at: now, plan };
  return plan ?? planLabel(oauth);
}

async function fetchSnapshot(fetchFn = fetch, now = Date.now()) {
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

  const headers = {
    Authorization: `Bearer ${oauth.accessToken}`,
    Accept: "application/json",
    "User-Agent": "openclaw",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
  };
  // Both requests start together: two 8 s timeouts in a row would pass the
  // caller's 10 s budget and lose the windows. fetchPlan never rejects.
  const planPromise = fetchPlan(oauth, headers, fetchFn, now);
  const res = await fetchFn(USAGE_URL, { headers, signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
  if (!res.ok) {
    await res.body?.cancel().catch(() => {});
    return { ...base, windows: [], error: `Anthropic usage HTTP ${res.status}` };
  }
  const windows = buildWindows(await res.json());
  const plan = await planPromise;
  return { ...base, windows, ...(plan ? { plan } : {}) };
}

export async function getClaudeCliUsage({ fetchFn, now = Date.now() } = {}) {
  if (cached && now - cached.at < CACHE_TTL_MS) return cached.snapshot;
  let snapshot;
  try {
    snapshot = await fetchSnapshot(fetchFn, now);
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
