# Mid-run rehydration & the snapshot/duplication tradeoff

_Verified by reading a running Open WebUI container's own source, not inferred.
Line references are `file:line` under that container's `/app`, against Open
WebUI 0.10.x. Line numbers drift between releases — treat them as pointers to
the right function, not exact coordinates, and re-check against your own
version before relying on them._

## The user-visible bug this addresses

Opening (or reloading) a chat **while a turn is still running** showed nothing —
no streamed text, no tool-call blocks — until the turn finished. Reconnecting
clients had to wait for `done`. Root cause below.

## How OWUI persists an assistant message

1. **`ENABLE_REALTIME_CHAT_SAVE` defaults to `False`** (`env.py:360`). Unless
   your deployment overrides it, OWUI writes **no assistant content to the DB
   during a run** — streamed deltas only go over the websocket to
   already-connected clients.
2. At `done`, OWUI persists `{'done': True, 'output': output, ...}`
   (`utils/middleware.py:5227`) — it writes the structured **`output`** blocks.
3. Therefore the **only** writer of assistant text to the DB *mid-run* is the
   pipe's own `replace` snapshot event (`emit.py::_emit_message_snapshot`). No
   snapshot ⇒ nothing in the DB mid-run ⇒ reconnectors see nothing until `done`.

## The render rule (decides everything)

`ContentRenderer.svelte:266`:

```svelte
{#if output?.length}      <!-- render OUTPUT blocks -->
{:else if …}              <!-- else render CONTENT -->
```

**`output` wins over `content` whenever `output` is non-empty.** `content` is a
fallback, only rendered while `output` is empty (i.e. mid-run).

Confirmed by browser probe (divergent marker text):
- completed msg, `output` set, `content` = different string → renders `output`.
- mid-run msg, `output = []`, `content` set → renders `content`.

Consequence: a full-text `content` left by snapshots is **ignored on reload**
once `output` is populated at `done`. Snapshots cannot cause a *reload-time*
duplicate.

## The duplication that scared off snapshots (820f6cc) — real, but narrow

Client socket handler (`Chat.svelte`):

```js
650  else if (type === 'chat:message:delta' || type === 'message') message.content += data.content; // APPEND
652  else if (type === 'chat:message' || type === 'replace')       message.content  = data.content; // OVERWRITE
...
1964 message.content = getOutputText(output);   // at done: content reset from output
```

`replace` is a pure **overwrite** and never touches `output`. But **yields travel
the HTTP streaming response and `replace` events travel the websocket — the two
transports are unordered.** A `replace("ABC")` can arrive *before* the matching
HTTP deltas, then the deltas append: `content = "ABC"` then `+= "ABC"` →
**`ABCABC`** on the actively-streaming tab.

Scope of that duplicate:
- **Active (streaming) client only.** Reconnecting clients never replay the HTTP
  stream, so they cannot double-append — they render the DB `content` once.
- **Self-heals at `done`**, where the client resets `content = getOutputText(output)`
  (`Chat.svelte:1964`), and on reload `output` wins anyway.

The "persistent, byte-identical" duplicate that 820f6cc chased was —per the pipe's
own later admission (`pipe.py`, P27 comment)— largely a **testing artifact of
redeploying the pipe mid-stream in the same live chat used to verify it.**

## Current approach (2026-07-11, P34)

- **Re-enabled** throttled mid-run `replace` snapshots in the assistant-stream and
  item-delta branches (`pipe.py`). Restores reconnect rehydration.
- Throttle: `snapshot_interval_s = 1.0`, `snapshot_min_delta_chars = 250`.
- The tool-result forced snapshot and the tool-block final forced snapshot are
  unchanged.

### The one thing left to confirm on live (deploy-gated)

Whether the active-client transport-race transient is (a) actually observable and
(b) truly transient / self-healing. Run `scripts/verify_rehydration.py` (see its
header) against a real streaming turn:
1. reconnect-mid-turn → DB `content` grows before `done` (rehydration works);
2. after `done` → no byte-identical duplication in `output` text or `content`.

**Never redeploy this pipe while a message in the chat you're using to verify it
is still streaming** — that reintroduces the false-alarm duplicate.
