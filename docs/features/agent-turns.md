A reply the agent starts without a message from you (a cron job, another session's `sessions_send`, a finished sub-agent's result) appears in your chat as a normal reply, live, with its tool cards. It sits at the end of the chat like any other reply, with no label on it.

Applies to: Pipe
Valves: `OWUI_API_KEY`

## What you see

![A reply a cron job wrote into the chat, with no user message above it](../img/agent-turn-light.png#gh-light-mode-only)
![A reply a cron job wrote into the chat, with no user message above it](../img/agent-turn-dark.png#gh-dark-mode-only)

When a run starts on a chat's session that you did not start from Open WebUI, the Pipe adds an assistant message at the end of that chat and streams the run into it, exactly as it streams your own replies: text token by token, tool cards that spin and complete, failed tools marked.

- **Tab open on the chat:** the message appears without a reload and the view moves to it. Open WebUI shows the chat as generating, and its Stop button works on it.
- **Tab open on another chat, or no tab at all:** the message is written to the chat anyway. Open it later and you see the finished reply, or the reply so far if the run is still going.
- **When the run ends,** the message is rebuilt from the run's own transcript (`chat.history`), so text or tool cards lost on the way are filled in. Anything the transcript lacks keeps what streamed.

The message appears with the run's first tool call or first real text. A run that only answers with a silent sentinel (`NO_REPLY`, `ANNOUNCE_SKIP`) gets no message; one that ran tools first keeps its tool cards, with no text.

Two such replies in a row chain one after the other in the order they started. They never become 1/2 and 2/2 variants of one message.

The design behind this is in [Agent turns](../design/agent-turns.md).

## Where these replies come from

| Source | In the chat |
| --- | --- |
| A cron job that runs in this chat's session | a reply, live |
| Another session's `sessions_send` into this chat's session | a reply, live |
| A sub-agent that finishes while its parent is idle | the parent's reply to the result, live |
| A sub-agent that finishes while its parent is still running | part of the running reply |
| A Gateway restart recovery of an interrupted reply | continues that same reply |
| A heartbeat run | nothing |

Heartbeats never get a message: the Gateway hides heartbeat output itself, and heartbeat tool cards in your chat would be noise. A cron job running in an isolated session never reaches the chat either, since it does not run on the chat's session.

## Sub-agent results

A sub-agent's own session is not a chat, so nothing is written for it directly. Its result reaches the chat through the parent:

![The parent's first reply, then its reply to the sub-agent's result, with no message from you in between](../img/subagent-result-light.png#gh-light-mode-only)
![The parent's first reply, then its reply to the sub-agent's result, with no message from you in between](../img/subagent-result-dark.png#gh-dark-mode-only)

- **Parent idle:** the Gateway hands the result to the parent as a new run. That run is an ordinary reply in the chat, and it usually quotes or summarises the result.
- **Parent still running:** the result is steered into the running parent run, and shows up in the reply you are already watching.
- **Parent answers with silence** (`NO_REPLY`): you still get the result, as one **Sub-agent** card labelled with the name the parent gave it when it spawned it, or the first sentence of its task. Expand the card to read the result.

To watch a sub-agent while it works, open it from the [Status dialog](./status.md#sub-agents).

## Stop, Send now and new messages

- **Stop** aborts the run and finishes the message with what it has.
- **Send now** (or Enter while the reply runs, with Open WebUI's message queue off) keeps the run going to its end in the same message; your message runs right after it.
- **A message you send while it streams** is queued by Open WebUI, because the chat shows as generating. If it is sent anyway, the Pipe waits for the run to finish and then sends it as its own reply. It is never mixed into the agent's run.

## Images, files and questions

A `MEDIA:` line in such a reply becomes an attached image or file, as in [your own replies](./chat.md#images-and-files-from-the-agent). There is no request of yours to borrow a login from, so the upload to Open WebUI's Files API uses the [`OWUI_API_KEY`](../configuration.md#owui_api_key) valve. Without it, the file falls back to the built-in file server link.

A reply that ends with a question asks it as a [question card](./questions.md#questions-in-replies-the-agent-starts) on the tab viewing the chat. With no such tab, the question stays as text.

## Restarts and reconnects

These replies follow the same rules as yours, see [Reliability](./reliability.md):

- **Gateway restart mid-run:** the reply is continued in the same message, by the Gateway's recovery or by a resume from the Pipe, never by both. See [Gateway restarts](./reliability.md#gateway-restarts).
- **Open WebUI restart mid-run:** when Open WebUI is back, the reply is picked up and finished.
- **Run ended while Open WebUI was down:** its reply is written from the transcript when Open WebUI starts.
- **Lost connection:** the Pipe reconnects and finishes the message from the transcript once the run is over.

## Messages from older versions

Earlier versions of the Pipe posted these replies differently: with a "↳ ..." label line on top (such as "↳ Sub-agent finished"), only after the chat had been idle for a while, and sometimes as a 1/2 and 2/2 variant. Those messages keep their text as it was. Nothing rewrites them.

An old "↳ Sub-agent finished" message carries a hidden task marker. Clicking the toolbar Status button on one still opens the sub-agent drawer instead of the Status dialog, but for most of them the drawer can only say the details are no longer available, because the old task lookup is gone from OpenClaw.
