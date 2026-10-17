The complete feature list, grouped by where you meet it. Each entry links to the page and section with the full explanation, the valves that control it, and a screenshot of it (or, for the parts with nothing on screen, the log lines that show them).

Applies to: Pipe, Action, Filter

## Markers

Everything here works as soon as the Pipe is installed, except entries marked:

- **needs agent setup:** the integration provides the mechanism, but your agent has to know the convention, see [agent skills](../install/agent-skills.md);
- **separate function:** ships as its own file and installs separately, see [Status button](../install/status-action.md) and [thinking control](../install/thinking-filter.md).

## In the chat

- [**Real-time streaming**](./chat.md#streaming): replies appear token by token, not all at once at the end
- [**Native tool cards**](./chat.md#tool-cards): tool calls render as Open WebUI's own two-phase cards, expandable to their input and output
- [**Sub-agent cards**](./chat.md#sub-agent-cards): a sub-agent the agent starts shows as a card named after it, with the head of its task and whether it started
- [**Failed tools marked**](./chat.md#failed-tools): failed calls are labelled live, and a call that never returns is closed rather than left spinning
- [**Questions as cards**](./questions.md) _(needs agent setup)_: agent questions become Open WebUI's question cards (text, yes/no, single choice, multi-select by number), a masked dialog for a secret, and survive a reload
- [**Per-chat thinking control**](./chat.md#thinking-level) _(separate function)_: a toggle in the composer sets the reasoning level per chat, offering only the levels your models support
- [**Images and files from the agent**](./chat.md#images-and-files-from-the-agent) _(needs agent setup)_: `MEDIA:` files are uploaded to the Open WebUI Files API and attached natively
- [**Image input**](./chat.md#images-you-attach): images you attach are forwarded to the agent
- [**Usage and goal line**](./chat.md#usage-and-goal-line): context, rate-limit and goal state under every reply
- [**Chat titles**](./chat.md#chat-titles): chats name themselves, on a separate agent so it never queues behind your conversation

## Sessions and models

- [**Persistent sessions**](./sessions-and-models.md#one-session-per-chat): one stable OpenClaw session per Open WebUI chat, so the agent remembers context across messages
- [**Model picker**](./sessions-and-models.md#model-picker): one entry per model your Gateway knows, discovered with `models.list`
- [**Picker size cap**](./sessions-and-models.md#picker-size-cap): keeps the picker from flooding with every model the Gateway knows
- [**Owner users**](./owner-users.md): only the users you list act with the Gateway owner's rights

## Status

- [**Status dialog**](./status.md#the-status-dialog) _(separate function)_: context, rate limits, background work and sub-agents, fetched live on demand, each section on its own, from the message toolbar or the composer
- [**Compact from the dialog**](./status.md#compact): run `/compact` on the session and watch the numbers change
- [**Sub-agent drawer**](./status.md#sub-agent-drawer): each sub-agent's run as a read-only chat, its tool calls as cards, growing while it runs
- [**Backend diagnostics**](../troubleshooting.md): prefixed logs for every reply, save decision and tool call

## Replies the agent starts

- [**Cron jobs and `sessions_send`**](./agent-turns.md#where-these-replies-come-from): a reply the agent starts lands in your chat as a normal reply, live, with tool cards, and no message from you
- [**Sub-agent results**](./agent-turns.md#sub-agent-results): a finished sub-agent's result reaches the chat through its parent's reply, or as a Sub-agent card when the parent stays silent
- [**Open tabs update live**](./agent-turns.md#what-you-see): the reply appears in an open tab without a reload, and Stop works on it

## Reliability

- [**Persistent connection**](./reliability.md#persistent-connection): one shared WebSocket, reconnecting on its own with backoff
- [**Messages while the agent works**](./reliability.md#messages-sent-while-the-agent-works): a message you send mid-run is in the chat at once, with no queue to click, and gets a real reply once the current work is done
- [**Reload and restart safety**](./reliability.md#reload-mid-reply): reload mid-reply and the partial answer is still there; a restart of [Open WebUI](./reliability.md#open-webui-restarts) or [the Gateway](./reliability.md#gateway-restarts) mid-reply continues the same reply
- [**No split replies**](./reliability.md#a-reply-that-ends-before-its-run): a reply whose stream ends early is finished in place, rebuilt from the run's transcript, instead of leaving its tail in a second message
- [**Recovery from lost events**](./reliability.md#lost-events): lost events are filled in from the Gateway's transcript, and silence is never mistaken for the end
- [**Stop that actually stops**](./reliability.md#stop): aborts the run and kills the tool that is still running
- [**Send now keeps the run**](./reliability.md#send-now): Send now continues the running reply and then answers your message, instead of killing it
- [**Clean redeploys**](./reliability.md#redeploys): a redeploy never leaves a duplicate connection writing every message twice
- [**Background tasks skipped**](./reliability.md#open-webui-background-tasks): Open WebUI's own title, tag, follow-up, emoji and autocomplete calls never reach your agent
- [**Actionable errors**](../troubleshooting.md): failures name the valve, the config key and the command that fixes them

## Setup

- [**Ed25519 device auth**](../install/pairing.md): challenge/response handshake, with a device identity that survives restarts and reinstalls
- [**Installer**](../install/installer.md): wizard, in-place updates that keep your valves, backups, healthcheck
- [**Valves**](../configuration.md): everything configurable from the Open WebUI admin UI, no file access and no restart

## Three functions

| File | Open WebUI type | Purpose |
| --- | --- | --- |
| [`openclaw_pipe.py`](../../openclaw_pipe.py) | [Pipe](https://docs.openwebui.com/features/extensibility/plugin/functions/pipe) (manifold) | The chat integration: almost everything above |
| [`openclaw_status_action.py`](../../openclaw_status_action.py) | [Action](https://docs.openwebui.com/features/extensibility/plugin/functions/action) | The Status button and dialog; reuses the Pipe's live connection and device identity, so there is no second pairing |
| [`openclaw_thinking_filter.py`](../../openclaw_thinking_filter.py) | [Filter](https://docs.openwebui.com/features/extensibility/plugin/functions/filter) | The per-chat thinking toggle in the composer. Optional, and imports nothing but the standard library and pydantic |

All three are generated from the same `src/openclaw_pipe_pkg/` source fragments. Contributing: [Development](../development.md).
