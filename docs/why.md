Why this integration exists, what the Gateway's native protocol gives you that a text-in, text-out API cannot, and when OpenClaw's built-in `/v1` endpoint is the better choice.

Applies to: Pipe, Action, Filter

## Why I built this

I built this because I wanted OpenClaw on my phone.

OpenClaw's built-in chat UI was never somewhere I wanted to hold a long conversation, least of all on mobile. Open WebUI is polished and mobile-first. It feels like using ChatGPT or Claude, and I wanted that same experience for my own agents. So I built this, used it daily for a few months, and I'm now sharing it.

## The connection is the difference

The usual ways to reach OpenClaw from outside, such as WhatsApp or Telegram, go through OpenClaw's public API, which is deliberately narrow: text in, text out. This integration speaks the [Gateway's native v4 WebSocket protocol](https://docs.openclaw.ai/gateway/protocol) instead, the same one OpenClaw's own clients use.

That is the reason the rest is possible. A text-in, text-out API can carry the agent's answer. Only the native protocol carries the agent's work:

- every tool call as it happens, as a [live tool card](./features/chat.md);
- the [questions](./features/questions.md) it stops to ask you;
- the [files and images](./features/chat.md) it emits;
- the [sub-agents](./features/status.md) it starts;
- the [replies it writes on its own](./features/agent-turns.md), from a cron job, a heartbeat or a finished sub-agent.

The [feature list](./features/index.md) has everything.

## How it connects

The chat integration is an Open WebUI [Pipe](https://docs.openwebui.com/features/extensibility/plugin/functions/pipe) function. It connects to your Gateway as an [external app](https://docs.openclaw.ai/gateway/external-apps): a plain protocol client that adds nothing to your OpenClaw install and needs nothing else running. That connection is what gives you real-time streaming, native tool cards and one persistent agent session per chat.

Two optional companions install the same way:

- the Status [Action](https://docs.openwebui.com/features/extensibility/plugin/functions/action) adds a Status button that fetches context, rate limits and sub-agents live from the Gateway ([install](./install/status-action.md));
- the thinking [Filter](https://docs.openwebui.com/features/extensibility/plugin/functions/filter) adds a per-chat reasoning-effort control ([install](./install/thinking-filter.md)).

How the pieces fit: [Architecture](./architecture.md).

## Your history lives in Open WebUI

Separate from the protocol: your chats are stored in Open WebUI's own database. They survive Gateway restarts and agent crashes, stay searchable and exportable, and a turn the Gateway loses mid-flight is still there in the chat. See [Reliability](./features/reliability.md).

## This or `/v1`?

You can point Open WebUI at OpenClaw's `/v1` endpoint instead, and for many people that is the right answer. OpenClaw ships an official OpenAI-compatible endpoint, and Open WebUI is a documented, CI-tested client for it:

1. Enable `gateway.http.endpoints.chatCompletions` in your OpenClaw config.
2. In Open WebUI, add the connection `http://your-gateway:18789/v1`, with your Gateway token as the API key.
3. Pick the model `openclaw/default`.

No install, nothing to maintain. See OpenClaw's [Open WebUI quick setup](https://docs.openclaw.ai/gateway/openai-http-api#open-webui-quick-setup).

The difference is what each one gives you:

| | `/v1` endpoint | This integration |
|---|---|---|
| Setup | Change one config flag | One install command for the Pipe; the Status button and thinking control are optional |
| Streams assistant text | Yes | Yes |
| Client-declared OpenAI function tools | Yes | Not applicable |
| **The agent's own tool calls:** its shell commands, file edits and searches | Not surfaced | Rendered live as native tool cards |
| **Sub-agents** | Not surfaced | Listed with status, a transcript drawer per sub-agent |
| **Questions mid-run** | No | Question cards and dialogs |
| **Images and files the agent sends** | No | Attached natively |
| **Context and rate-limit usage** | No | On demand, live |
| Model switching | One model id | Every model your Gateway knows, per conversation |
| Auth | Shared bearer token | Ed25519 device pairing |

Put simply: **`/v1` gives you the agent as a model. This gives you the agent as an agent.** If you want OpenClaw in a model dropdown, use `/v1`. If you want to watch it work, use this, starting with the [Quick start](./install/quickstart.md).
