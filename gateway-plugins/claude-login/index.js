// Chat command /claude_login: re-login the Claude CLI from any chat surface,
// handled by the Gateway itself without an LLM turn.
//
// Why this exists: the agent's model runs through claude-cli, so when the
// Claude CLI login expires (the refresh token has a hard ~30 day lifetime) the
// agent cannot answer the very message that would fix it. Plugin commands
// bypass the LLM, so this works exactly when the model is down.
//
// Flow:
//   /claude_login           start `claude auth login` in a PTY, reply with the URL
//   /claude_login <code>    paste the code from the callback page, verify, report
//   /claude_login status    login state + refresh-token expiry
//   /claude_login cancel    kill a pending login
//
// The pending login lives in this process only. A Gateway restart in between
// drops it; start again. Tokens are never printed: the reply carries only the
// account email and expiry dates.
import { spawn, execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const PENDING_TTL_MS = 10 * 60_000;
const URL_WAIT_MS = 20_000;
const EXCHANGE_WAIT_MS = 45_000;
const MAX_OUTPUT_CHARS = 64_000;

let pending = null; // { child, output, url, startedAt, timer, exited, exitCode, exitWaiters }

function claudeConfigDir() {
  return process.env.CLAUDE_CONFIG_DIR?.trim() || path.join(os.homedir(), ".claude");
}

// Strip ANSI CSI and OSC sequences (including OSC 8 hyperlinks) and CRs.
export function stripTerminal(text) {
  return text
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, "")
    .replace(/\r/g, "");
}

// The CLI prints the authorize URL as an OSC 8 hyperlink whose visible text is
// the same URL, so after stripping it appears twice glued together. Take the
// hyperlink target when present, the first plain URL otherwise.
export function extractAuthorizeUrl(raw) {
  const osc = raw.match(/\x1b\]8;;(https:\/\/[^\x07\x1b\s]+\/oauth\/authorize\?[^\x07\x1b\s]+)/);
  if (osc) return osc[1];
  const plain = stripTerminal(raw).match(/https:\/\/\S+\/oauth\/authorize\?\S+/);
  if (!plain) return undefined;
  // Cut a glued duplicate: a second "https://" inside the match.
  const url = plain[0];
  const dup = url.indexOf("https://", 8);
  return dup > 0 ? url.slice(0, dup) : url;
}

// Never let anything token-shaped out into chat.
function redact(text) {
  return text.replace(/sk-ant-[A-Za-z0-9_-]+/g, "sk-ant-<redacted>");
}

function outputTail(p, lines = 6) {
  const clean = stripTerminal(p.output)
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && !l.includes("/oauth/authorize?"));
  return redact(clean.slice(-lines).join("\n"));
}

function clearPending(kill = true) {
  if (!pending) return;
  clearTimeout(pending.timer);
  if (kill && !pending.exited) {
    try {
      pending.child.kill("SIGTERM");
    } catch {}
  }
  pending = null;
}

function waitFor(predicate, timeoutMs, p) {
  return new Promise((resolve) => {
    if (predicate()) return resolve(true);
    const started = Date.now();
    const tick = setInterval(() => {
      if (predicate()) {
        clearInterval(tick);
        resolve(true);
      } else if (Date.now() - started > timeoutMs || (p && p.exited)) {
        clearInterval(tick);
        resolve(predicate());
      }
    }, 200);
  });
}

function startLogin() {
  // `script` gives the CLI a real PTY; it forwards our stdin into it, which is
  // how the code gets pasted at the "Paste code here" prompt.
  const child = spawn("script", ["-qfc", "claude auth login --claudeai", "/dev/null"], {
    env: { ...process.env, BROWSER: "/bin/true", TERM: "xterm-256color" },
    stdio: ["pipe", "pipe", "pipe"],
    detached: false,
  });
  const p = { child, output: "", url: undefined, startedAt: Date.now(), exited: false, exitCode: null };
  const onData = (buf) => {
    p.output = (p.output + buf.toString("utf8")).slice(-MAX_OUTPUT_CHARS);
    if (!p.url) p.url = extractAuthorizeUrl(p.output);
  };
  child.stdout.on("data", onData);
  child.stderr.on("data", onData);
  child.on("error", (err) => {
    p.output += `\n[spawn error: ${err.message}]`;
    p.exited = true;
  });
  child.on("exit", (code) => {
    p.exited = true;
    p.exitCode = code;
  });
  p.timer = setTimeout(() => {
    if (pending === p) clearPending(true);
  }, PENDING_TTL_MS);
  p.timer.unref?.();
  return p;
}

function run(cmd, args, timeoutMs = 15_000) {
  return new Promise((resolve) => {
    execFile(cmd, args, { timeout: timeoutMs, env: process.env }, (err, stdout, stderr) => {
      resolve({ code: err ? (err.code ?? 1) : 0, stdout: String(stdout), stderr: String(stderr) });
    });
  });
}

// In the Gateway host's time zone (TZ), the only one this process knows.
function fmtDate(ms) {
  return new Date(ms).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short", timeZoneName: "short" });
}

export async function loginStatusText() {
  const st = await run("claude", ["auth", "status"]);
  let status;
  try {
    status = JSON.parse(st.stdout);
  } catch {
    return `Could not read Claude CLI auth status: ${redact(st.stderr || st.stdout).trim().slice(0, 300)}`;
  }
  if (!status.loggedIn) return "❌ Claude CLI is NOT logged in. Send /claude_login to sign in.";
  const lines = [`✅ Claude CLI logged in as ${status.email ?? "unknown"} (${status.subscriptionType ?? status.authMethod ?? "?"})`];
  try {
    const oauth = JSON.parse(await readFile(path.join(claudeConfigDir(), ".credentials.json"), "utf8"))?.claudeAiOauth;
    const r = oauth?.refreshTokenExpiresAt;
    if (typeof r === "number") {
      const days = (r - Date.now()) / 86_400_000;
      lines.push(`Login valid until ${fmtDate(r)} (${days.toFixed(1)} days).`);
    }
  } catch {}
  return lines.join("\n");
}

async function handleStart() {
  clearPending(true);
  const p = startLogin();
  pending = p;
  const got = await waitFor(() => Boolean(p.url), URL_WAIT_MS, p);
  if (!got) {
    const tail = outputTail(p);
    clearPending(true);
    return `Login did not produce a sign-in URL.\n${tail}`;
  }
  return [
    "Claude CLI login started.",
    "",
    `1. Open: ${p.url}`,
    "2. Approve, then copy the code shown on the page.",
    "3. Send: /claude_login <code>",
    "",
    `Expires in ${PENDING_TTL_MS / 60_000} minutes. /claude_login cancel to abort.`,
  ].join("\n");
}

async function handleCode(code) {
  const p = pending;
  if (!p || p.exited) {
    clearPending(false);
    return "No login in progress (or it expired). Send /claude_login first.";
  }
  const mark = p.output.length;
  p.child.stdin.write(`${code}\r`);
  await waitFor(() => p.exited, EXCHANGE_WAIT_MS, p);
  const exitedCleanly = p.exited && p.exitCode === 0;
  const after = { ...p, output: p.output.slice(mark) };
  clearPending(true);
  const status = await loginStatusText();
  if (exitedCleanly && status.startsWith("✅")) return `Login complete.\n${status}`;
  return [
    exitedCleanly ? "Login process finished, but the CLI does not report a login." : "Login did not complete.",
    outputTail(after) || "(no output)",
    "",
    status,
  ].join("\n");
}

export default {
  id: "claude-login",
  name: "Claude CLI login",
  description: "Chat command /claude_login: re-login the Claude CLI without an LLM turn.",
  register(api) {
    api.registerCommand({
      name: "claude_login",
      description: "Re-login the Claude CLI (start | <code> | status | cancel).",
      acceptsArgs: true,
      requireAuth: true,
      requiredScopes: ["operator.admin"],
      handler: async (ctx) => {
        const arg = (ctx.args ?? "").trim();
        try {
          if (!arg || arg === "start") return { text: await handleStart() };
          if (arg === "status") return { text: await loginStatusText() };
          if (arg === "cancel") {
            const had = Boolean(pending);
            clearPending(true);
            return { text: had ? "Pending login cancelled." : "No login in progress." };
          }
          return { text: await handleCode(arg) };
        } catch (err) {
          return { text: `claude_login failed: ${redact(String(err?.message ?? err))}` };
        }
      },
    });
  },
};
