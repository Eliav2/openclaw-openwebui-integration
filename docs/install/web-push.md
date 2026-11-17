# Push notifications

Push notifications moved out of this repository: the Web Push function, its page script and its service worker live in [Eliav2/open-webui-pwa-push](https://github.com/Eliav2/open-webui-pwa-push), with their own install guide, tests and releases. The copy that was here (`push/`, 0.5.0) is gone; do not install an old one.

Applies to: Pipe

Valves: the Pipe's [`PUBLISH_CHAT_FINISHED`](../configuration.md#publish_chat_finished)

What stays here is the Pipe's side. The push function listens for Open WebUI's `chat.finished` event, and the Pipe publishes it for every reply it writes (agent turns, cron and sub-agent replies included) when `PUBLISH_CHAT_FINISHED` is on, with `source` `openclaw-pipe`. Turn it on, then follow the install guide in the other repository.
