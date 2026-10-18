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

## Edit, Regenerate, Fork and Clone

The agent's memory of a chat is a single line of messages: each time you send, the Pipe passes on only your newest message, and the agent adds it to what it already has. Open WebUI's chat is a tree, so when you change what the chat shows, the Pipe tells the agent with a short note before your message (a sentence or two, never on a normal send):

- **Edit a message** and press Send: the note quotes the start of the message you replaced and says it, and everything after it, is no longer in the chat.
- **Regenerate a reply** (Try Again): the note says the agent's earlier answer is no longer in the chat, so it answers your message afresh.
- **Switch branches** with the arrows under a message, then send: the note says how many messages the chat no longer shows and how many it shows instead, quoting the first of each.
- **Delete messages**, then send: the note says how many of the messages the agent remembers were deleted.

The agent still remembers the replaced messages; the note tells it which ones the chat no longer shows. Editing a message and pressing Save (without sending) changes only what you see.

**Fork or clone a chat** (a reply's Fork chat button, or Clone in the sidebar's chat menu): the copy is a new chat with a fresh session. Its first message carries the copied messages once, so the agent continues where the copy starts. They are cut to about 8,000 tokens (32,000 characters): the newest messages are kept, the oldest left out whole (the agent is told how many), and tool-call details are left out. Background work started from a chat (scheduled jobs, sub-agents) stays tied to the original chat, never to its copy.

For a clean start, open a new chat.

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
