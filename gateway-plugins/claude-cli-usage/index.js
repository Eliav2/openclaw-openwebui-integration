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
import { readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const USAGE_URL = "https://api.anthropic.com/api/oauth/usage";
const PROFILE_URL = "https://api.anthropic.com/api/oauth/profile";
const FETCH_TIMEOUT_MS = 8000;
// The usage endpoint rate-limits aggressively; one fetch per minute is plenty
// for a dialog a human opens by hand.
const CACHE_TTL_MS = 60_000;
// After a failed fetch the endpoint is not asked again before the 429's own Retry-After, and never sooner
// than one cache period (the same one-fetch-per-minute the success path keeps). A Retry-After longer than
// the shortest window (5 h) is capped there: a bogus header must not blind the dialog for days.
const RETRY_AFTER_MAX_MS = 18_000_000;
const MS_PER_SECOND = 1000;

let cached = null; // { at, snapshot }, set by a successful fetch (or a first failure with nothing to fall back on)
let failure = null; // { until, snapshot }: the last failed fetch and the time before which nothing asks again
// The plan changes rarely; asking for it every few hours is enough.
const PLAN_TTL_MS = 6 * 3600_000;
// Keyed on the access token: another login (or a token refresh) asks again.
let cachedPlan = null; // { key, at, plan }

// Retry-After as an absolute time (epoch ms), counted from `receivedAt`, the moment the answer arrived: delta-seconds run from
// receipt (RFC 9110), an HTTP date is absolute. Never before receipt, never more than RETRY_AFTER_MAX_MS after it; undefined
// when absent or unparseable.
export function parseRetryAfter(raw, receivedAt = Date.now()) {
  const text = String(raw ?? "").trim();
  if (!text) return undefined;
  const at = /^\d+$/.test(text) ? receivedAt + Number(text) * MS_PER_SECOND : /[a-z]/i.test(text) ? Date.parse(text) : NaN;
  return Number.isFinite(at) ? Math.min(Math.max(at, receivedAt), receivedAt + RETRY_AFTER_MAX_MS) : undefined;
}

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
  const key = oauth.accessToken; // in-memory cache key only; never returned or logged
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

// -> { snapshot, retryUntil? }: retryUntil (epoch ms) is the failed answer's own Retry-After, counted from when it arrived.
async function fetchSnapshot(fetchFn = fetch, now = Date.now(), clock = Date.now) {
  const base = { provider: "anthropic", displayName: "Claude", source: "claude-cli" };
  let oauth;
  try {
    oauth = JSON.parse(await readFile(credentialsPath(), "utf8"))?.claudeAiOauth;
  } catch (err) {
    return { snapshot: { ...base, windows: [], error: `Claude CLI credentials unreadable: ${err.code ?? err.message}` } };
  }
  if (!oauth?.accessToken) return { snapshot: { ...base, windows: [], error: "Claude CLI is not logged in" } };
  if (typeof oauth.expiresAt === "number" && oauth.expiresAt <= Date.now()) {
    return { snapshot: { ...base, windows: [], error: "Claude CLI token expired; it refreshes on Claude CLI's next run" } };
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
  const startedAt = clock();
  const res = await fetchFn(USAGE_URL, { headers, signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
  const receivedAt = now + (clock() - startedAt); // `now` is the caller's time (a test's, or Date.now()); `clock` measures the wait for the answer
  if (!res.ok) {
    await res.body?.cancel().catch(() => {});
    return {
      snapshot: { ...base, windows: [], error: `Anthropic usage HTTP ${res.status}` },
      retryUntil: parseRetryAfter(res.headers?.get?.("retry-after"), receivedAt),
    };
  }
  const windows = buildWindows(await res.json());
  const plan = await planPromise;
  return { snapshot: { ...base, windows, ...(plan ? { plan } : {}) } };
}

// The last good windows, marked stale, rather than blanking the dialog.
function servedStale(reason) {
  return { ...cached.snapshot, stale: true, staleReason: reason };
}

export async function getClaudeCliUsage({ fetchFn, now = Date.now(), clock = Date.now } = {}) {
  if (cached && now - cached.at < CACHE_TTL_MS) return cached.snapshot;
  // A failure holds every caller off the endpoint until its Retry-After (or one cache period) is over:
  // asking again on each call while it answers 429 only prolongs the 429.
  if (failure && now < failure.until) {
    return cached?.snapshot.windows.length ? servedStale(failure.snapshot.error) : failure.snapshot;
  }
  let snapshot;
  let retryUntil;
  const startedAt = clock();
  try {
    ({ snapshot, retryUntil } = await fetchSnapshot(fetchFn, now, clock));
  } catch (err) {
    snapshot = { provider: "anthropic", displayName: "Claude", source: "claude-cli", windows: [], error: String(err?.message ?? err) };
  }
  if (snapshot.error) {
    const settledAt = now + (clock() - startedAt); // the hold runs from when the answer (or the failure) arrived, not from when we asked
    failure = { until: Math.max(settledAt + CACHE_TTL_MS, retryUntil ?? 0), snapshot };
    // A transient failure (429, timeout) keeps serving the last good windows, marked stale.
    if (cached?.snapshot.windows.length) return servedStale(snapshot.error);
  } else {
    failure = null;
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
