The Status dialog shows a chat's context use, your rate limits and the sub-agents its runs spawned, fetched live when you open it. From there you can compact the session, or open any sub-agent's drawer to see its task, transcript and tool calls.

Applies to: Action (the composer Status button also needs the Pipe)
Valves: `GATEWAY_URL`, `GATEWAY_TOKEN`, `DEVICE_IDENTITY`, `STATE_DIR`, `AGENT_ID` (Action); `COMPOSER_STATUS_BUTTON` (Pipe)

## The Status dialog

_Separate function: install the Status Action first, see [Status button](../install/status-action.md)._

![The Status dialog: context used, 5-hour and weekly rate limits, two running sub-agents](../img/status-dialog-light.png#gh-light-mode-only)
![The Status dialog: context used, 5-hour and weekly rate limits, two running sub-agents](../img/status-dialog-dark.png#gh-dark-mode-only)

_The rate limits in this screenshot are sample data._

The Status Action adds a button to the message toolbar. It opens a dialog with three sections: Context, Rate Limits and Subagents.

Each section has its own skeleton and fills the moment its own request returns, so a slow sub-agent list does not hold up the context number. Usage bars are colour coded (green under 60%, amber from 60%, red from 85%), with the colours inlined so a theme cannot make them invisible.

The footer says when the dialog last refreshed and whether it used the Pipe's live connection (`via pipe`) or opened its own (`via action-own`). It reuses the Pipe's connection when one is open, and otherwise connects with the Pipe's saved device identity from `STATE_DIR`, so there is never a second device to approve. The Action's own connection only reads: it never writes a reply into a chat.

The Action's valves mirror the Pipe's (`install_action.py` copies them over): [`GATEWAY_URL`](../configuration.md#gateway_url-1), [`GATEWAY_TOKEN`](../configuration.md#gateway_token-1), [`DEVICE_IDENTITY`](../configuration.md#device_identity-1), [`STATE_DIR`](../configuration.md#state_dir-1), [`AGENT_ID`](../configuration.md#agent_id-1), under [Status Action](../configuration.md#status-action) in Configuration.

## Rate limits

Rate limits come from the Gateway's `usage.status`. On a Gateway whose only Anthropic credential is a Claude CLI login, OpenClaw 2026.9.1 and later report no Anthropic windows there. The dialog then asks the optional [`gateway-plugins/claude-cli-usage`](../../gateway-plugins/claude-cli-usage) Gateway plugin, which serves the 5h and weekly windows read-only from the Claude CLI login. Without the plugin the section shows a note instead. Installing the plugin: [Status button](../install/status-action.md).

## Compact

A Compact pill in the dialog header runs `/compact` on the session. While it works, the dialog shows a distinct "Compacting..." state, then refreshes with the numbers after compaction, so you can see what it bought you.

It waits up to 180 seconds, the same as the default timeout of `openclaw sessions compact`. If it takes longer, the dialog says so; compaction may still finish in the background.

While a reply is running, Compact refuses with "A response is currently in progress", rather than compacting the session under a running turn.

The `/compact` command becomes a turn in the session's stored transcript, so other tools that read that history see it, but it never appears as a message in the Open WebUI chat.

## Sub-agents

The Subagents section lists the child sessions this chat's runs spawned: the ones still active and the ones that ended in the last 30 minutes, with their status and elapsed time. A running sub-agent's elapsed time counts up while the dialog is open.

Each row is titled with the sub-agent's task, else its spawn label. While a sub-agent has just started, the row shows its task rather than the Gateway's spawn wrapper.

## Sub-agent drawer

![A running sub-agent's drawer, Transcript tab: its task and what it has read so far](../img/subagent-drawer-light.png#gh-light-mode-only)
![A running sub-agent's drawer, Transcript tab: its task and what it has read so far](../img/subagent-drawer-dark.png#gh-dark-mode-only)

Click a sub-agent to open a side drawer with three tabs:

- **Overview:** its status, when it started and how long it ran, its own context fill, its goal line, and its provider and model, which are often not the parent's.
- **Transcript:** the sub-agent's messages. Its first message shows the task it was given, not the spawn wrapper around it.
- **Tools:** each tool call with its arguments and result, failed ones marked.

The drawer reads the sub-agent's own transcript (`chat.history`) and refreshes about every 2.5 seconds until the sub-agent finishes, for at most 15 minutes. When tool calls show up depends on the runtime the sub-agent runs on:

- **OpenClaw's own runtime:** tool calls show as they happen.
- **claude-cli runtime:** its tool calls reach the transcript when its run ends, so the Tools tab fills at the end.

The Tools tab reads both transcript shapes, so it works the same for either runtime.

## Status button in the composer

The Pipe also adds a **Status** button to the composer row of OpenClaw chats, right after the OpenClaw Thinking chip (or at the end of the composer's chips when that chip is off). It opens the same dialog through the Status Action, so the Action must be installed.

Open WebUI has no extension point for the composer, so the button is put into the page by JavaScript. If a future Open WebUI changes the composer and the anchor is not found, the button simply does not appear. On Open WebUI 0.12 it appears as soon as a tab opens an OpenClaw chat; on older releases, after the next reply or the next click on the toolbar button.

Turn it off with the Pipe's [`COMPOSER_STATUS_BUTTON`](../configuration.md#composer_status_button) valve. An already open tab keeps the button until you reload it.
