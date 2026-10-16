When the agent needs an answer mid-run, you get a real question card or dialog in Open WebUI instead of a wall of text nobody can answer. It survives a reload, and once answered the run continues in the same reply.

Applies to: Pipe

## How the agent asks

_Needs agent setup: the agent has to know the convention below. Install the skill or add it to your agent's instructions, see [agent skills](../install/agent-skills.md)._

The agent writes a line that starts with `OpenClaw needs input:` (or `Codex needs input:`), followed by the question. The Pipe catches it and asks you, instead of letting the raw prompt leak into the chat.

It catches the trigger however it arrives: streamed one character at a time, or in the middle of one cumulative chunk. A prompt emitted twice does not become two dialogs, and duplicate options are collapsed. A trigger line inside a code block is a quote, not a question.

The implementation is in [`src/openclaw_pipe_pkg/askuser.py`](../../src/openclaw_pipe_pkg/askuser.py).

## What the question looks like

![A question from the agent as a card: three options with descriptions, the first tagged Recommended, and an Other field](../img/question-card-light.png)

The question renders as Open WebUI's own inline question card, the one its built-in `ask_user` tool uses: a header, the question, one row per option with an optional description, the first option tagged Recommended, and an Other field for a typed answer. Secrets and multi-select get a dialog instead.

| Question looks like | Shown as |
| --- | --- |
| anything else | card with a typed answer |
| mentions secret, password, api key, token | dialog: a masked password field |
| `(y/n)`, or a confirm verb plus `?` | card with Yes / No |
| two or more numbered options | card, one row per option |
| `(multiselect)` or "select all that apply" | dialog: checkboxes plus a Submit button |
| a JSON body with up to three `questions` | one card that steps through each |

Secrets and multi-select get a dialog because the card has no masked input and picks one option per question.

An option written as `1. Postgres - needs a server` shows `Postgres` as the choice and the rest as its description.

Confirmation and multi-select markers are recognised in English, Hebrew and Arabic.

## Answering, and coming back later

Answer the card and the run continues in the same reply. The exchange stays in the reply as a collapsible ❓ Ask User card that shows both the question and your answer.

While it waits you can reload or switch away and come back:

- **Question card:** the card is shown once, on the tab that views the chat; a reload moves it to the reloaded tab. If it is not answered by Open WebUI's card timeout (4 minutes), or no tab views the chat (for example a client calling the API), the reply ends with the question as plain text.
- **Dialogs (secrets, multi-select):** the dialog pops again on your new session each time you reconnect, for up to an hour, with the status "Waiting for your reply -- question is pending, reconnect anytime".

If you cancel the question, or it ends as text, the question stays in the reply as ordinary text with the trigger removed (a JSON form as its numbered questions with their options), so nothing is lost. Answer it with a normal message.

## Questions in replies the agent starts

A reply the agent starts on its own (a cron job, a sub-agent result, see [Replies the agent starts](./agent-turns.md)) can end with a question too. It is asked the same way, as a card on the tab that is viewing the chat. With no such tab open, it stays in the reply as text.
