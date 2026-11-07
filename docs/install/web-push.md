# Push notifications

Push notifications, offline open and instant chat open for Open WebUI are a separate add-on with its own repo: **[open-webui-pwa-push](https://github.com/Eliav2/open-webui-pwa-push)**. It works with any Open WebUI, OpenClaw or not: a service worker and a page script served by a proxy in front of Open WebUI (a Cloudflare Worker or nginx), and an Open WebUI event function that sends the notifications. Install it from its README.

Applies to: Pipe

Valves: [`PUBLISH_CHAT_FINISHED`](../configuration.md#publish_chat_finished)

## What the Pipe adds for OpenClaw models

Open WebUI's own `chat.finished` fires when the chat request ends. For OpenClaw that misses the replies the agent starts (cron, `sessions_send`, sub-agent results) and fires too early for a reply that outlives its request. Turn on the Pipe's [`PUBLISH_CHAT_FINISHED`](../configuration.md#publish_chat_finished): the Pipe then publishes its own `chat.finished` once each reply is saved (see [Telling other functions a reply finished](../features/agent-turns.md#telling-other-functions-a-reply-finished)), and the add-on's function notifies that one for OpenClaw models (its `PIPE_MODEL_PREFIXES` valve, `openclaw_gateway.` by default). Other models need nothing: Open WebUI's own event is right for them.

Turn it off and OpenClaw replies are no longer notified; other models still are.
