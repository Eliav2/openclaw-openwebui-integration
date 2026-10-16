The Status dialog shows a chat's context use, your rate limits and the sub-agents its runs spawned, fetched live when you open it. From there you can compact the session, or open any sub-agent's drawer to see its task, transcript and tool calls.

Applies to: Action (the composer Status button also needs the Pipe)
Valves: `GATEWAY_URL`, `GATEWAY_TOKEN`, `DEVICE_IDENTITY`, `STATE_DIR`, `AGENT_ID`, `CONTROL_UI_URL` (Action); `COMPOSER_STATUS_BUTTON` (Pipe)

## The Status dialog

_Separate function: install the Status Action first, see [Status button](../install/status-action.md)._

![The Status dialog: context used, 5-hour and weekly rate limits, two running sub-agents](../img/status-dialog-light.png#gh-light-mode-only)
![The Status dialog: context used, 5-hour and weekly rate limits, two running sub-agents](../img/status-dialog-dark.png#gh-dark-mode-only)

_The rate limits in this screenshot are sample data._

The Status Action adds a button to the message toolbar. It opens a dialog with three sections: Context, Rate Limits and Subagents.

![The OpenClaw Status button on a reply's toolbar](../img/toolbar-button-light.png#gh-light-mode-only)
![The OpenClaw Status button on a reply's toolbar](../img/toolbar-button-dark.png#gh-dark-mode-only)

Each section has its own skeleton and fills the moment its own request returns, so a slow sub-agent list does not hold up the context number. Usage bars are colour coded (green under 60%, amber from 60%, red from 85%), with the colours inlined so a theme cannot make them invisible.

The footer says when the dialog last refreshed and whether it used the Pipe's live connection (`via pipe`) or opened its own (`via action-own`). It reuses the Pipe's connection when one is open, and otherwise connects with the Pipe's saved device identity from `STATE_DIR`, so there is never a second device to approve. The Action's own connection only reads: it never writes a reply into a chat.

The Action's valves mirror the Pipe's (`install_action.py` copies them over): [`GATEWAY_URL`](../configuration.md#gateway_url-1), [`GATEWAY_TOKEN`](../configuration.md#gateway_token-1), [`DEVICE_IDENTITY`](../configuration.md#device_identity-1), [`STATE_DIR`](../configuration.md#state_dir-1), [`AGENT_ID`](../configuration.md#agent_id-1), under [Status Action](../configuration.md#status-action) in Configuration. Its own [`CONTROL_UI_URL`](../configuration.md#control_ui_url) is kept across re-installs: set it to the address you open the OpenClaw Control UI at (including any `gateway.controlUi.basePath`), and the dialog header links to this chat's session there, and the sub-agent drawer to the child's, each in a new tab, through the Control UI's short-id route `/chat/<agent>/<id>`. Empty (the default): no link.

## Rate limits

Rate limits come from the Gateway's `usage.status`. On a Gateway whose only Anthropic credential is a Claude CLI login, OpenClaw 2026.9.1 and later report no Anthropic windows there. The dialog then asks the optional [`gateway-plugins/claude-cli-usage`](../../gateway-plugins/claude-cli-usage) Gateway plugin, which serves the 5h and weekly windows read-only from the Claude CLI login. Without the plugin the section shows a note instead. Installing the plugin: [Status button](../install/status-action.md).

## Compact

A Compact pill in the dialog header runs `/compact` on the session. While it works, the dialog shows a distinct "Compacting..." state, then refreshes with the numbers after compaction.

![The Context section with its Compact pill](../img/compact-before-light.png#gh-light-mode-only)
![The Context section with its Compact pill](../img/compact-before-dark.png#gh-dark-mode-only)

Right after a compact, the context number can read far lower than what the next request carries: it counts the kept conversation, without the system prompt and tools. The next reply brings it up to date.

It waits up to 180 seconds, the same as the default timeout of `openclaw sessions compact`. If it takes longer, the dialog says so; compaction may still finish in the background.

While a reply is running, Compact refuses with "A response is currently in progress", rather than compacting the session under a running turn.

The `/compact` command becomes a turn in the session's stored transcript, so other tools that read that history see it, but it never appears as a message in the Open WebUI chat.

Typing `/compact` in the message box does not compact the session: Open WebUI 0.12 handles `/compact`, `/status`, `/fork` and `/model` itself, so they never reach OpenClaw. Use the Compact button in this dialog instead.

## Sub-agents

The Subagents section lists every child session this chat's runs spawned, ended ones included, newest first, up to 20 ("20 of N" when there are more). A running one's elapsed time counts up while the dialog is open; one that finishes meanwhile shows its end as soon as the Gateway pushes the change. Below them, **In-process agents** lists the chat's Claude Code Agent calls, which are not Gateway sessions, as their tool cards show them: running, in the background, returned, cut off or failed.

Each row is one line: the sub-agent's spawn label (else the first sentence of its task, cut to 60 characters), its status, and its time, with up to two lines of its latest reply below. The whole task is in its drawer.

## Sub-agent drawer

![A running sub-agent's drawer on a phone: a one-line header, its reply so far as Latest, and its Task](../img/subagent-drawer-light.png#gh-light-mode-only)
![A running sub-agent's drawer on a phone: a one-line header, its reply so far as Latest, and its Task](../img/subagent-drawer-dark.png#gh-dark-mode-only)

Click a sub-agent to open its drawer: a side panel, or on a phone a full-screen sheet with a **‹ Back** button. Its header is the title, the status, the time and the link to the sub-agent in the Control UI, with three tabs below:

- **Overview:** its **Result** (the last block of its final reply, six lines, "Show all" for the rest; while it runs, its reply so far, as **Latest**) and its **Task** (three lines, "Show all"), then when it started and how long it ran, its own context fill, its goal line, and its provider and model, which are often not the parent's.
- **Transcript:** the sub-agent's messages. Its first message shows the task it was given, not the spawn wrapper around it.
- **Tools:** each tool call with its arguments and result, failed ones marked.

An in-process agent opens the same sheet with its task and result from its tool card, and no tabs.

The drawer reads the sub-agent's own transcript (`chat.history`) and refreshes about every 2.5 seconds for as long as it is open and the sub-agent runs; closing it stops the refresh. For a claude-cli sub-agent the Result is its last turn, not the run's closing message, which repeats every message of the run. When tool calls show up depends on the runtime the sub-agent runs on:

- **OpenClaw's own runtime:** tool calls show as they happen.
- **claude-cli runtime:** its tool calls reach the transcript when its run ends, so the Tools tab fills at the end.

The Tools tab reads both transcript shapes, so it works the same for either runtime.

## Status button in the composer

The Pipe also adds a **Status** button to the composer row of OpenClaw chats, right after the OpenClaw Thinking chip (or at the end of the composer's chips when that chip is off). It opens the same dialog through the Status Action, so the Action must be installed.

![The Status button in the composer, next to the integrations button](../img/composer-status-light.png#gh-light-mode-only)
![The Status button in the composer, next to the integrations button](../img/composer-status-dark.png#gh-dark-mode-only)

Open WebUI has no extension point for the composer, so the button is put into the page by JavaScript. If a future Open WebUI changes the composer and the anchor is not found, the button simply does not appear. On Open WebUI 0.12 it appears as soon as a tab opens an OpenClaw chat; on older releases, after the next reply or the next click on the toolbar button.

Turn it off with the Pipe's [`COMPOSER_STATUS_BUTTON`](../configuration.md#composer_status_button) valve. An already open tab keeps the button until you reload it.
