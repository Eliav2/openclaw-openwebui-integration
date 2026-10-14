What a reply looks like in the chat: live text, the agent's tool calls as real cards, failures marked, images and files both ways, a usage line under each reply, and chat titles. Everything here works as soon as the Pipe is installed, except where a section says otherwise.

Applies to: Pipe (the thinking level also needs the thinking filter)
Valves: `USE_OWUI_FILES`, `OWUI_BASE_URL`, `OWUI_API_KEY`, `ENABLE_FILE_SERVER`, `FILE_SERVER_BASE_URL`, `AUTO_TITLE`, `TITLE_GEN_AGENT_ID`

## Streaming

Text appears token by token as the Gateway sends it, so a long answer starts showing at once instead of landing in one block at the end.

Two provider quirks are handled so you never see them:

- Some runtimes resend the whole reply as one final cumulative frame. The Pipe recognises text it has already shown and drops the repeat, so the answer is never printed twice.
- Other events carry their text as a whole item rather than as deltas. That text is shown too, not dropped.

Internal scaffolding never reaches the reply: the Sender provenance block (see [the metadata contract](../metadata-contract.md)) and the silent sentinels an agent uses to mean "say nothing" (`NO_REPLY`, `ANNOUNCE_SKIP`) are filtered out.

Always on.

## Tool cards

![A reply mid-run: two finished tool cards and one still running](../img/hero-tool-cards-light.png#gh-light-mode-only)
![A reply mid-run: two finished tool cards and one still running](../img/hero-tool-cards-dark.png#gh-dark-mode-only)

Tool calls are sent as Responses-API output items, so Open WebUI draws them with its own two-phase tool card: a spinner while the tool runs, the result when it finishes. Nothing is hand-rolled HTML, so the cards match the rest of the UI and survive a reload.

Expand a card to see the arguments the agent passed and the result it got back. Arguments are capped at 3000 characters and results at 8000, so one huge tool result cannot bury the conversation.

![A tool card opened: the command it ran and its output](../img/tool-card-expanded-light.png#gh-light-mode-only)
![A tool card opened: the command it ran and its output](../img/tool-card-expanded-dark.png#gh-dark-mode-only)

A call that arrives without a tool-call id still shows: it falls back to a Markdown `<details type="tool_calls">` card rather than disappearing. The status row under the message tracks the same events in words: "Running \<tool\>..." then "\<tool\> done".

Always on. Native cards need a recent Open WebUI; on older releases they fall back quietly rather than erroring. See [Versions](../versions.md).

## Failed tools

![A reply streaming: a read card marked with a cross because the file is missing, and an exec card still running](../img/failed-tool-live-light.png#gh-light-mode-only)
![A reply streaming: a read card marked with a cross because the file is missing, and an exec card still running](../img/failed-tool-live-dark.png#gh-dark-mode-only)

A tool call that fails gets its card renamed to `<name> ❌` the moment the result arrives, on any card, not only the last one. The mark is part of the saved message, so it is still there after a reload. The expanded output opens with a `❌ tool call failed` line, so the reason is visible once you open the card.

A tool that was announced but never returned, because the run was cancelled or crashed, is closed at the end of the reply with "(no result -- the run ended first)". When the run was cut by a restart and will continue, the note reads "(interrupted -- the reply continues when the run resumes)" instead. A card left spinning forever looks exactly like a hung UI, so it is never left that way.

Always on.

## Thinking level

With the [thinking filter](../install/thinking-filter.md) installed, a toggle in the composer sets the reasoning level per chat: `default`, `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`.

- `default` is not a level, it is the absence of one. No `thinking` field is sent, and the agent's own setting applies.
- `off` is an explicit instruction not to think, and it does override the agent's setting.

The list only offers levels a model on your Gateway supports. The Pipe reads each session's real ladder from the `sessions.describe` it already makes before every send, so this costs no extra round trip. Until the Pipe has run once, the list falls back to the five levels every observed provider supports (`off` to `high`).

Open WebUI builds the dropdown once, so it is the union across your Gateway's models, not the ladder of the model you picked. At send time the Pipe clamps the request to the nearest level the current model supports and says so once in the chat:

> _This model does not support thinking level 'ultra', using 'high' instead._

Clamping lands on the nearest level at or below what you asked for, since quietly asking for more shows up on someone's bill. The one exception: if a model has nothing at or below the request, it uses that model's lowest level, which can be higher than what you asked for.

The filter and Open WebUI's own Advanced Params reasoning-effort control write the same `reasoning_effort` field, so they are one setting, not two that fight. The filter's [`override_advanced_params`](../configuration.md#override_advanced_params) valve (on by default) decides who wins when both are set. Install steps and the filter's valves: [thinking filter](../install/thinking-filter.md).

## Images and files from the agent

_Needs agent setup: the agent must emit `MEDIA:<bare-filename>`, see [agent skills](../install/agent-skills.md)._

![A reply from a cron job: a chart image, then text under it](../img/media-turn-light.png#gh-light-mode-only)
![A reply from a cron job: a chart image, then text under it](../img/media-turn-dark.png#gh-dark-mode-only)

When the agent emits a `MEDIA:<filename>` line and the file exists in the Pipe's media directory (`/tmp/openclaw-pipe-media`), the Pipe:

1. Uploads it to Open WebUI's Files API (`POST /api/v1/files/?process=false`).
2. Emits a `files` event, so the file appears as a real attachment chip on the message.
3. Shows it with a same-origin link such as `![image.png](/api/v1/files/<id>/content)`.

Because the URL is same-origin, it works over HTTPS with no mixed-content blocking. Images render inline, other file types render as a link, and a file the upload could not resolve falls back to inline code rather than a broken embed. Several `MEDIA:` lines in one reply all resolve, not just the first.

The directive is recognised even when streaming splits it mid-prefix or mid-filename, and one still buffered when the run ends is flushed, not dropped. A directive inside a code block is left alone, and writing "the MEDIA: fix" in ordinary prose does not turn the next word into a broken image link.

The upload uses your own login from the request. `OWUI_API_KEY` is only needed if uploads fail with an auth error, or for replies the agent starts on its own (see [Replies the agent starts](./agent-turns.md#images-files-and-questions)).

If the upload is off or fails, the Pipe falls back to a small built-in HTTP file server on port 18791, which the agent can also `PUT` files to. Its `FILE_SERVER_BASE_URL` (default `http://localhost:18791`) is resolved by your **browser**, not by Open WebUI, so the default only works when you browse Open WebUI from the same host. That server is unauthenticated: read [Security](../security.md) before you rely on it.

Valves: [`USE_OWUI_FILES`](../configuration.md#use_owui_files) (on), [`OWUI_BASE_URL`](../configuration.md#owui_base_url) (default `http://127.0.0.1:8080`, correct for a standard single-container install), [`OWUI_API_KEY`](../configuration.md#owui_api_key), [`ENABLE_FILE_SERVER`](../configuration.md#enable_file_server) (on), [`FILE_SERVER_BASE_URL`](../configuration.md#file_server_base_url).

## Images you attach

An image you attach or paste reaches the Pipe as a list of content blocks, not the plain string Open WebUI sends for text. The Pipe takes each `image_url` block, parses the `data:<mime>;base64,<data>` URI Open WebUI sends, and forwards it to the agent as a `chat.send` attachment with its MIME type and a matching file extension. A vision-capable model gets the real image bytes, not a description.

Only that inline base64 form is handled, because Open WebUI does not send plain HTTP(S) image URLs for attached or pasted images. An image the Pipe cannot forward produces a visible note that it could not be relayed, so you never get an answer confidently describing a picture the agent never received.

Always on.

## Usage and goal line

After every reply, a status row shows where you stand:

> 🧠 45k/200k (22.5%) · ⏱ 5h 18% left (resets in 22m)

You get one row per rate-limit window that reports a reset, and a goal line when the session has one (`🎯 Pursuing goal (12k/50k)`, with paused, blocked, usage-limited, unmet and achieved variants).

Each fact is its own status event, because Open WebUI clamps a status row to one line and a combined string loses its numbers off the right edge.

The data comes from `sessions.describe` and `usage.status`, fetched together. It is best effort: if either call fails, the reply is unaffected. For the full picture on demand, use the [Status dialog](./status.md).

Always on.

## Chat titles

The chat names itself with a short title and an emoji. When you send your second message, the Pipe titles the chat from the first exchange (your first message and the first reply).

Generation runs on a separate agent, `title-gen` by default, as a background task, so it never queues behind your conversation or adds to its context, and a failure never shows. The model's output is trimmed of quotes, extra lines and anything past 80 characters before it becomes the title. If that agent does not exist on your Gateway, titles are skipped silently: define a `title-gen` agent (a fast model is enough) or point `TITLE_GEN_AGENT_ID` at one you have.

Open WebUI's own title, tag and follow-up calls never reach your agent, see [Open WebUI background tasks](./reliability.md#open-webui-background-tasks).

Valves: [`AUTO_TITLE`](../configuration.md#auto_title) (on), [`TITLE_GEN_AGENT_ID`](../configuration.md#title_gen_agent_id) (default `title-gen`).
