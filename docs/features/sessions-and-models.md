Each Open WebUI chat is one stable OpenClaw session, so the agent remembers the conversation, and the model picker lists the models your Gateway actually has. Pick one per chat, or pick OpenClaw · Default to use the agent's own model.

Applies to: Pipe
Valves: `AGENT_ID`, `DEFAULT_MODEL`, `MAX_MODELS`, `STATE_DIR`

## One session per chat

Each Open WebUI chat maps to a stable OpenClaw session key:

<img src="../img/chat-titles-light.png" alt="Two messages and two replies in one chat, one OpenClaw session" width="320">

```text
agent:{AGENT_ID}:openwebui-{user_id}-{chat_id}
```

So the agent remembers context across messages, and across restarts of either side.

The agent also receives a provenance block naming the chat id, the user id and `source=openwebui`, which lets it tell an Open WebUI conversation from a Telegram one. The full contract is in [the metadata contract](../metadata-contract.md).

Valve: [`AGENT_ID`](../configuration.md#agent_id) (default `main`). Leave it as `main` unless your Gateway defines other agents.

## Edit, Regenerate and Clone

The agent's memory of a chat is a single line of messages: each time you send, the Pipe passes on only your newest message, and the agent adds it to what it already has. Open WebUI's branching tools change what you see, but not what the agent remembers:

- **Edit a message:** the edited text reaches the agent as a new message. It still remembers your original message and its answer to it.
- **Regenerate a reply:** the agent receives the same message a second time, right after its earlier answer, and it remembers both answers.
- **Switch branches** with the arrows under a message: the agent is not told. Your next message is added to everything it already remembers, whichever branch you are looking at.
- **Clone a chat** (or copy it into a new chat any other way): the copy is a new chat, so it gets a fresh session. The agent starts it with no memory of the copied messages, even though you can see them.

Background work started from a chat (scheduled jobs, sub-agents) stays tied to the original chat, never to its clone.

For a clean start, open a new chat. If the agent seems to be answering an older version of the conversation, tell it what changed in your next message.

## Model picker

<img src="../img/model-picker-light.png" alt="The model dropdown open: OpenClaw · Default, then the models discovered from the Gateway" width="320">

The Pipe is a manifold function: its `pipes()` calls the Gateway's `models.list` and returns one picker entry per model, named `<Model> (<provider>) · OpenClaw`, plus an entry that is always there, **OpenClaw · Default**.

- **Picking a model** sets that chat's OpenClaw session to that model. The override is applied with each message you send.
- **Picking OpenClaw · Default** clears the override, so the agent's configured model applies (or `DEFAULT_MODEL`, if you set it). It always works, even before the Gateway has been reached.

Where the list comes from, in order:

1. **Live:** `models.list` over the Pipe's open connection. The result is saved to `STATE_DIR/models-cache.json`.
2. **Cache:** that saved list, so your real models still show after a restart, before the first connection.
3. **Built-in examples:** a short hardcoded list, used only when there is neither. These entries are labelled "-- example, Gateway not reached", in the picker and in the `DEFAULT_MODEL` dropdown both, so a placeholder never passes for one of yours. Pick OpenClaw · Default, send one message, then reload, and your real list replaces them.

Models the Gateway reports as unavailable (`available: false` in `models.list`, for example a provider with no credentials) are left out, since picking one could only fail.

Model keys with dots (`gemini-3.1-pro-preview`) route correctly. Runtime namespaces (`claude-cli/`, `codex-cli/`, `google-gemini-cli/`) count as their vendor, so an override that did apply is never reported as failed. A burst of messages in one chat collapses into one `sessions.patch` instead of one per message.

A model missing from the picker: see [Troubleshooting](../troubleshooting.md).

## Default model

[`DEFAULT_MODEL`](../configuration.md#default_model) is the model used when OpenClaw · Default is picked. Leave it empty to use the agent's own configured model.

In the valve editor it is a dropdown of the models your Gateway knows, not a text box. It lists every known model key, whatever `MAX_MODELS` is set to, so you can always choose a default, even a model the picker hides. Before the first successful connection it shows the built-in example list, labelled as such.

## Picker size cap

[`MAX_MODELS`](../configuration.md#max_models) caps how many discovered models the picker lists: default `30`, allowed range 1 to 100. A Gateway that knows two hundred models should not produce a two-hundred-entry dropdown.
