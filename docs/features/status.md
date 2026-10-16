The Status dialog shows a chat's context use, your rate limits, what runs in the background for it and the sub-agents its runs spawned, fetched live when you open it. From there you can compact the session, or open any sub-agent's drawer to read its run like a chat.

Applies to: Action (the composer Status button also needs the Pipe)
Valves: `GATEWAY_URL`, `GATEWAY_TOKEN`, `DEVICE_IDENTITY`, `STATE_DIR`, `AGENT_ID`, `CONTROL_UI_URL` (Action); `COMPOSER_STATUS_BUTTON` (Pipe)

## The Status dialog

_Separate function: install the Status Action first, see [Status button](../install/status-action.md)._

![The Status dialog: context used, 5-hour and weekly rate limits, two running sub-agents](../img/status-dialog-light.png)

_The rate limits in this screenshot are sample data._

The Status Action adds a button to the message toolbar. It opens a dialog with five sections: Context, Rate limits, [Background](#background), [Subagents](#sub-agents) and In-process agents.

![The OpenClaw Status button on a reply's toolbar](../img/toolbar-button-light.png)

The dialog opens at once with every section's title. Each section then reads its own source and fills the moment that source answers, with its own loading, empty and error state, so a slow or failing source shows in its own section only:

![The Status dialog while its rate limits are still loading: Context, Background, Subagents and In-process agents already filled](../img/status-dialog-loading-light.png)

_Here the rate limits were held back on purpose, to show it._

| Section | Reads |
|---|---|
| Context | `sessions.describe` |
| Rate limits | `usage.status`, for the provider Context's read names (the same request); `claudeCliUsage.status` only when `usage.status` has no windows |
| Background | `tasks.list`, `cron.list`, and Open WebUI's copy of the chat |
| Subagents | `sessions.list` (the store's rows only); the sub-agents' transcripts (`sessions.preview`) only once the section is open |
| In-process agents | Open WebUI's copy of the chat (the same read as Background's) |

Each source is read once per open, all at the same time. Nothing is read while the dialog is closed: closing it tells the Action, which stops following its running sub-agents. In-process agents needs no Gateway, so it fills even when the Gateway cannot be reached.

Background, Subagents and In-process agents fold, with their count in the title, for example "Background (3)". Each opens when something in it is running and starts folded otherwise; a tap opens or folds it, and stays that way while the dialog is open. When the dialog is taller than the screen, its sections scroll between the title bar and the footer, which stay in view, on a phone too:

![The Status dialog on a small phone, scrolled to its end: the title bar and the footer stay on screen](../img/status-dialog-scrolled-light.png)

Usage bars are colour coded (green under 60%, amber from 60%, red from 85%), with the colours inlined so a theme cannot make them invisible.

The footer says when the dialog last refreshed and whether it used the Pipe's live connection (`via pipe`) or opened its own (`via action-own`). It reuses the Pipe's connection when one is open, and otherwise connects with the Pipe's saved device identity from `STATE_DIR`, so there is never a second device to approve. The Action's own connection only reads: it never writes a reply into a chat.

The Action's valves mirror the Pipe's (`install_action.py` copies them over): [`GATEWAY_URL`](../configuration.md#gateway_url-1), [`GATEWAY_TOKEN`](../configuration.md#gateway_token-1), [`DEVICE_IDENTITY`](../configuration.md#device_identity-1), [`STATE_DIR`](../configuration.md#state_dir-1), [`AGENT_ID`](../configuration.md#agent_id-1), under [Status Action](../configuration.md#status-action) in Configuration. Its own [`CONTROL_UI_URL`](../configuration.md#control_ui_url) is kept across re-installs: set it to the address you open the OpenClaw Control UI at (including any `gateway.controlUi.basePath`), and the dialog header links to this chat's session there, and the sub-agent drawer to the child's, each in a new tab, through the Control UI's short-id route `/chat/<agent>/<id>`. Empty (the default): no link.

## Rate limits

Rate limits come from the Gateway's `usage.status`. On a Gateway whose only Anthropic credential is a Claude CLI login, OpenClaw 2026.9.1 and later report no Anthropic windows there. The dialog then asks the optional [`gateway-plugins/claude-cli-usage`](../../gateway-plugins/claude-cli-usage) Gateway plugin, which serves the 5h and weekly windows read-only from the Claude CLI login. Without the plugin the section shows a note instead. Installing the plugin: [Status button](../install/status-action.md).

## Compact

A Compact pill in the dialog header runs `/compact` on the session. While it works, the dialog shows a distinct "Compacting..." state, then refreshes with the numbers after compaction.

![The Context section with its Compact pill](../img/compact-before-light.png)

Right after a compact, the context number can read far lower than what the next request carries: it counts the kept conversation, without the system prompt and tools. The next reply brings it up to date.

It waits up to 180 seconds, the same as the default timeout of `openclaw sessions compact`. If it takes longer, the dialog says so; compaction may still finish in the background.

While a reply is running, Compact refuses with "A response is currently in progress", rather than compacting the session under a running turn.

The `/compact` command becomes a turn in the session's stored transcript, so other tools that read that history see it, but it never appears as a message in the Open WebUI chat.

Typing `/compact` in the message box does not compact the session: Open WebUI 0.12 handles `/compact`, `/status`, `/fork` and `/model` itself, so they never reach OpenClaw. Use the Compact button in this dialog instead.

## Background

![The Background section: four station imports running, a daily automation for this chat, and the Gateway's other automations](../img/status-dialog-background-light.png)

The Background section lists what runs for this chat outside a reply, running first, then the five newest that finished:

- **Commands** left running in the background (`exec` with `background`, or one that outlived its wait), named by the command the chat's tool card shows.
- **Image, Music and Video** jobs.
- **Automations** that run in this chat: whether one is running, its next run, and its last error when it failed ("Failed 3 times in a row: ...").
- **Agents**: Claude Code Agent calls started in the background, until the reply they hold open ends. A tap shows the task and the result, as under In-process agents.

Sub-agents are listed under Subagents, not here.

Owners also see **Other automations**: the Gateway's automations that run elsewhere, running and failing ones first, up to five ("5 of N"). They fold on their own, with their count in the title, and open only when one of them is running or failed. Anyone not in the Pipe's [`OWNER_USERS`](../configuration.md#owner_users) sees only their own chat's. With `OWNER_USERS` empty, everyone is an owner, as everywhere else.

The section reads only when the dialog opens. OpenClaw 2026.10 removed the Gateway's task list (`tasks.list`): there the section says background commands and media jobs are not available on this Gateway, and still lists the chat's automations.

While a background agent holds a reply open, the reply's status row says so, so the chat does not look busy for no reason:

![A reply's status row: Waiting on 1 background agent](../img/background-agent-waiting-light.png)

It counts the background agents the reply started and stays until the reply ends.

## Sub-agents

The Subagents section lists every child session this chat's runs spawned, ended ones included, newest first, up to 20 ("20 of N" when there are more). A running one's elapsed time counts up while the dialog is open; one that finishes meanwhile shows its end as soon as the Gateway pushes the change. With none running, the section opens folded and reads the sub-agents' transcripts (their tasks and latest replies) on its first tap:

![The Status dialog with every section folded, each with its count](../img/status-dialog-folded-light.png)

**In-process agents** lists the chat's Claude Code Agent calls, which are not Gateway sessions, as their tool cards show them: running, in the background, returned, cut off or failed.

Each row is one line: the sub-agent's spawn label (else the first sentence of its task, cut to 60 characters), its status, and its time, with up to two lines of its latest reply below. The whole task is in its drawer.

## Sub-agent drawer

![A running sub-agent's drawer on a phone: its name, status and time, its model and the chat it came from, the head of its task, then its run so far with a tool card opened and the step it is on](../img/subagent-drawer-light.png)

Click a sub-agent to open its drawer: a side panel, or on a phone a full-screen sheet with a **‹ Back** button. The top is a short header: the sub-agent's name, its status and its time; its model and the chat it came from (a link; for a sub-agent spawned by another sub-agent, its parent's drawer); and the first two lines of its task, with "Show all".

Below the header is the sub-agent's run as a read-only chat, oldest first: what it said, every tool call as a collapsed card (tap it for the input and the output; a failed one is marked ❌, as in the chat), any message it was sent after its task, and its reply last. The cards are the same ones the chat shows for a run, drawn from the same transcript the same way. There is no composer.

An in-process agent opens the same sheet: its task as the message that started it, and its result as the reply.

The drawer reads the sub-agent's whole transcript (`chat.history`). A very long run shows its newest steps and says how many earlier ones are left out, with **Show earlier steps** to read them too. While the sub-agent runs, the drawer refreshes about every 2.5 seconds, adds each new step and shows which step it is on ("Working… (step 3)"); if you scrolled up to read, it leaves you there. Closing it stops the refresh. When the steps show up depends on the runtime the sub-agent runs on:

- **OpenClaw's own runtime:** each step shows as it happens.
- **claude-cli runtime:** its messages and tool calls reach the transcript when its run ends, so while it runs the drawer says so and shows its latest line. A claude-cli sub-agent that is stopped before it ends keeps none of its tool calls, and the drawer says that too.

## Status button in the composer

The Pipe also adds a **Status** button to the composer row of OpenClaw chats, right after the OpenClaw Thinking chip (or at the end of the composer's chips when that chip is off). It opens the same dialog through the Status Action, so the Action must be installed.

![The Status button in the composer, next to the integrations button](../img/composer-status-light.png)

Open WebUI has no extension point for the composer, so the button is put into the page by JavaScript. If a future Open WebUI changes the composer and the anchor is not found, the button simply does not appear. It appears as soon as a tab opens an OpenClaw chat.

Turn it off with the Pipe's [`COMPOSER_STATUS_BUTTON`](../configuration.md#composer_status_button) valve. An already open tab keeps the button until you reload it.
