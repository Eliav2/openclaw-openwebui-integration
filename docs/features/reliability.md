What keeps a reply whole when things go wrong: one self-healing connection, messages that queue instead of colliding, replies that survive a reload or a restart of either side, a Stop that really stops, and redeploys that leave nothing behind.

Applies to: Pipe
Valves: `GATEWAY_URL`, `GATEWAY_TOKEN`, `STATE_DIR`, `SEND_STOP_ON_CANCEL`, `INTERRUPT_GRACE_SECONDS`

## Persistent connection

One WebSocket connection to the Gateway is shared by every chat, rather than one per message.

It reconnects on its own with exponential backoff, from 1 second up to 30, so a Gateway restart needs nothing from you. The Gateway sends a keepalive tick (every 30 seconds by default); a socket that sends no frame for two tick intervals is treated as dead and reconnected, rather than waited on forever.

The Pipe's log while the Gateway restarts (an excerpt):

```text
[openclaw-pipe] WS disconnected -- reconnecting... (code 1006, reason '')
[openclaw-pipe]   reconnect in 1s (attempt 1)
[openclaw-pipe] Connecting to ws://localhost:18789
[openclaw-pipe]   reconnect failed: Could not connect to the OpenClaw Gateway at localhost:18789 (ConnectionRefusedError: [Errno 111] Connection refused). Is the Gateway running, and is the GATEWAY_URL valve pointing at it?
[openclaw-pipe]   reconnect in 2s (attempt 2)
[openclaw-pipe] Connecting to ws://localhost:18789
[openclaw-pipe]   reconnect failed: Could not connect to the OpenClaw Gateway at localhost:18789 (ConnectionRefusedError: [Errno 111] Connection refused). Is the Gateway running, and is the GATEWAY_URL valve pointing at it?
[openclaw-pipe]   reconnect in 4s (attempt 3)
[openclaw-pipe] Connecting to ws://localhost:18789
[openclaw-pipe]   Gateway restarted (bootId 9b526b46 -> 6dafeee8)
[openclaw-pipe] Connected to Gateway (persistent) [epoch 2]
```

Valves: [`GATEWAY_URL`](../configuration.md#gateway_url), [`GATEWAY_TOKEN`](../configuration.md#gateway_token). The full message path is in [Architecture](../architecture.md).

## Messages sent while the agent works

Send a message while the agent is still working (from another tab, or while a run you did not start is active) and it waits for the current run to finish, then gets a real reply of its own. The status line says "Queued: waiting for the current run to finish...". What you do not get is a blank reply, which is what the naive implementation produces.

![A message sent while a reply runs, held above the composer by Open WebUI](../img/message-queued-light.png)

In the tab where the reply is running, Open WebUI holds the new message itself, as above, and sends it when the reply ends. The Pipe's own queue is for a message from another tab or device, or one sent during a run you did not start.

Whether a run is active comes from the Gateway: `sessions.describe`, confirmed against `sessions.list`'s `hasActiveRun`, which holds across several Open WebUI processes. A parent that is paused waiting on a sub-agent counts as busy, not finished. The wait ends when the Gateway pushes that the run settled, with no polling and no time limit. A per-session send lock means two messages released at the same moment cannot merge into one run.

Open WebUI sometimes calls the Pipe twice for the same reply. The second call does not send your message again: it follows the run the first call started and ends with that run's answer, so the agent never answers the same message twice and the real answer is never replaced.

Always on.

## Reload mid-reply

Reload the page mid-reply, or open the chat on another device, and you see the answer so far instead of an empty reply. On Open WebUI 0.12, Open WebUI keeps the in-progress reply itself and shows it on reload. On older releases the Pipe saves the reply to the chat as it streams, at most every 2.5 seconds.

![The same reply right after a page reload: the answer so far is there and still growing](../img/reload-mid-reply-light.png)

If your tab or socket dies mid-stream, a forced final save means the finished text is in the database when you come back.

The device identity and token persist in `STATE_DIR`, so restarting Open WebUI or reloading the function never asks you to pair again. If `STATE_DIR` is not writable, the Pipe falls back to `/tmp/openclaw-bridge` and logs `STATE_DIR unavailable` loudly rather than failing. Details and the fix: [Device pairing](../install/pairing.md).

How Open WebUI persists a message mid-run, and why the Pipe saves it the way it does: [Rehydration](../rehydration-persistence.md).

Valve: [`STATE_DIR`](../configuration.md#state_dir), default `/data/openclaw-bridge`.

## Open WebUI restarts

Restarting or shutting down Open WebUI does not stop your agent. The run keeps going on the Gateway, and nothing is aborted. When Open WebUI is back, the Pipe finds the replies that were left unfinished:

- **Run still going:** the reply is picked up and streamed to the end in the same message.
- **Run ended while Open WebUI was down:** the reply is written from the run's transcript.

## Gateway restarts

A Gateway restart in the middle of a reply does not cut it short. The reply continues in the same message, with no new message from you, and only one side continues it:

- **The Gateway recovers the run** after a crash or a planned restart, because the run is still marked running. The Pipe follows the recovery run into the same message.
- **The Pipe resumes the run** when the Gateway will not: the run's process was killed from outside (it exited with code 143 or 137, or a signal, as in a container stop), or the Gateway was shut down rather than restarted.

On a resume, the agent is told what you can already see and what you asked, so it continues rather than starting over, and your original message is never sent twice. No resume is sent into a session that is busy with another run.

Resumes are capped at 3 per reply. Past that, the reply is finished with what it has and a note: "_(This reply was cut off: the run was interrupted again after 3 attempts to continue it.)_".

## A reply that ends before its run

Sometimes the reply's request ends while the run is still going: Open WebUI closed the stream, the request was torn down, or a restart was detected. The reply is not left short, and the rest does not show up in a second message. The Pipe has been following the same run on its persistent connection all along, so it keeps streaming text and tool cards into the same message until the run ends. Then it rebuilds the message from the run's transcript (`chat.history`), so it ends exactly as an uninterrupted reply would have. Images from `MEDIA:` lines and questions still work on this path.

This needs the Open WebUI message id, which a normal chat always has. It is always on.

## Lost events

If events are lost on the way (a dropped frame, a socket gap), the reply is rebuilt from the Gateway's own transcript (`chat.history`), which holds each message whole: text and tool cards lost in the middle are put back where they belong, and a run that ended while its final event was lost is closed from its settled session row. A reply with no Open WebUI message (an API request) keeps streaming instead, and what the gap lost there stays lost.

What the Pipe logs when the Gateway socket drops mid-reply (an excerpt):

```text
[openclaw-pipe] WS disconnected -- reconnecting... (code 1006, reason '')
[openclaw-pipe]   marked an event gap in 1 run queue(s)
```

Silence is never treated as the end of a reply. A long tool call or a long stretch of silent reasoning is waited on; the reply ends only on the run's own end, its settled session row, or a restart.

There is no timer and no event count: thousands of tool calls before the first word keep the reply open too. When a run ends without text, the reason comes from its real final state, so you see **Stopped.**, the run's error, or **Failed:** the run did not complete, rather than a generic timeout. With no reason at all, the reply says "_(This turn produced no reply text.)_".

Always on.

## Stop

Pressing Stop in Open WebUI cancels the Pipe's reply. The Pipe first saves the partial answer, so what you already saw is not lost, then sends the Gateway a `chat.abort` for the run. Abort alone only ends the stream: a tool the agent started keeps running on the Gateway unless something also tells the agent to stop.

![A reply stopped while its command was running: the card is closed, not spinning](../img/stop-light.png)

That is what `/stop` is for. With `SEND_STOP_ON_CANCEL` on (the default), the Pipe sends it right after the abort, so a long shell command or browser action is killed instead of running on against a chat nobody is watching.

The run's remaining output is never added to the stopped message later, and never posted as a message of its own.

Valve: [`SEND_STOP_ON_CANCEL`](../configuration.md#send_stop_on_cancel) (on).

## Send now

Open WebUI implements "Send now" on a queued message (and sending while a reply runs with its message queue off) as a Stop followed by the new message. Both reach the server as the same cancel, and the queue lives only in the browser, so the server cannot tell them apart. Aborting on that cancel would kill the running reply and every sub-agent it spawned.

![After Send now: the running reply finished, then the new message's own answer](../img/send-now-light.png)

So the Pipe asks the page. It installs a small listener in the chat page that records which you pressed: Stop or Escape, versus Send now or Enter while a reply runs. On a cancel it reads that answer through Open WebUI's `execute` event:

- **Stop:** the run is aborted at once.
- **Send now:** the run is kept alive (up to 120 seconds) until your new message arrives. The new reply first streams the rest of the run (its remaining tool calls and text), then answers your message. The status line says "Continuing the current task; your message is next...".

While the page's answer is on its way, the run is held. So a Stop followed by Open WebUI auto-sending the next queued message starts that message fresh, and does not continue the run you stopped.

Nothing is dropped along the way. The run's events are buffered from the moment of the cancel, so the new reply continues exactly where the old one stopped. A reply interrupted before it could send its own message (Send now twice in a row, or while it waits behind a running reply) hands that message on, and the next reply sends it together with its own. Open WebUI ends a reply either by cancelling it or by closing its stream, depending on timing; both are handled the same way.

When the page cannot answer, `INTERRUPT_GRACE_SECONDS` applies as a fallback: a new message inside that window keeps the run, otherwise it is aborted. That happens when Open WebUI is in a language other than English (Stop is recognised by its icon, but Send now only by its English label) or for a client calling the API directly. `0` aborts at once.

Valve: [`INTERRUPT_GRACE_SECONDS`](../configuration.md#interrupt_grace_seconds) (default `10`).

## Redeploys

Open WebUI's function loader runs every redeploy as a brand-new Python module, with no teardown hook for the old one. Left alone, the previous deploy's Gateway connection would live on: still receiving events, and able to write every message into your chat a second time next to the new one.

The Pipe stores its live connection on Open WebUI's own socket module, which a redeploy never reloads, instead of in a module variable that resets each time. The next deploy finds it (and any other connection an older load of the same function left running), retires it with a short timeout, and only then opens its own. Redeploys clean up after themselves with no container restart. A reply that was streaming during the redeploy is finished by the retired connection, which closes once that reply is done. Until then the new connection leaves that reply alone, so it is written once. If the reply ends before its run does, the new connection writes the rest of the run into the same message. The same pattern reaps the fallback media file server on its port, so a redeploy never leaves two processes fighting over it.

Always on.

## Open WebUI background tasks

Open WebUI quietly calls the selected model for chores: a title after the first exchange, tags, a follow-up suggestion, an emoji, autocomplete as you type, a search query for its built-in search, an image prompt, and tool selection. Each call reaches the Pipe tagged with its task name: `title_generation`, `tags_generation`, `follow_up_generation`, `emoji_generation`, `autocomplete_generation`, `query_generation`, `image_prompt_generation`, `function_calling`.

In the log:

```text
[openclaw-pipe] Skipping OWUI background task: follow_up_generation
```

The Pipe checks that tag before anything else: a match is logged and the Pipe returns at once, so none of these ever reaches your agent. Unhandled, each would be an extra run in your real session: wasted tokens, a title prompt in the agent's context, and confusing entries in its history. Open WebUI falls back on its own for an empty answer to each of these. "Merge Responses" is not skipped, since its only output is the answer itself.

This is separate from the Pipe's own [chat titles](./chat.md#chat-titles), which run on a dedicated agent instead.

Always on, not configurable.

## Errors and logs

Errors name the valve, the config key or the command that fixes them, and every reply logs to the Open WebUI backend log with an `[openclaw-pipe]` prefix. Both are covered in [Troubleshooting](../troubleshooting.md).
