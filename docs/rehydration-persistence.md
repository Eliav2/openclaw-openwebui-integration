# Mid-run rehydration & the snapshot/duplication tradeoff

_Verified by reading a running Open WebUI container's own source, not inferred.
Line references are `file:line` under that container's `/app`, against Open
WebUI 0.10.x. Line numbers drift between releases, so treat them as pointers to
the right function, not exact coordinates, and re-check against your own
version before relying on them._

> **2026-08-26 re-verification against live OWUI 0.11.x — read this first.**
> The 0.10.x persistence model below (`ENABLE_REALTIME_CHAT_SAVE` gating
> mid-run DB writes) is superseded by OWUI's own delta-based streaming, and
> separately the pipe's own snapshot call sites have moved since the "Current
> approach (2026-07-11, P34)" section below was written. See
> **[Update: verified against live OWUI 0.11.x](#update-2026-08-26-verified-against-live-owui-011x)**
> at the end of this doc for what was actually observed on a live turn and
> what currently does/doesn't get mid-run DB rehydration.

## The user-visible bug this addresses

Opening (or reloading) a chat **while a turn is still running** showed nothing at all:
no streamed text and no tool-call blocks until the turn finished. Reconnecting
clients had to wait for `done`. Root cause below.

## How OWUI persists an assistant message

1. **`ENABLE_REALTIME_CHAT_SAVE` defaults to `False`** (`env.py:360`). Unless
   your deployment overrides it, OWUI writes **no assistant content to the DB
   during a run**: streamed deltas only go over the websocket to
   already-connected clients.
2. At `done`, OWUI persists `{'done': True, 'output': output, ...}`
   (`utils/middleware.py:5227`). That writes the structured **`output`** blocks.
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

## The duplication that scared off snapshots (820f6cc): real, but narrow

Client socket handler (`Chat.svelte`):

```js
650  else if (type === 'chat:message:delta' || type === 'message') message.content += data.content; // APPEND
652  else if (type === 'chat:message' || type === 'replace')       message.content  = data.content; // OVERWRITE
...
1964 message.content = getOutputText(output);   // at done: content reset from output
```

`replace` is a pure **overwrite** and never touches `output`. But **yields travel
the HTTP streaming response and `replace` events travel the websocket, the two
transports are unordered.** A `replace("ABC")` can arrive *before* the matching
HTTP deltas, then the deltas append: `content = "ABC"` then `+= "ABC"` →
**`ABCABC`** on the actively-streaming tab.

Scope of that duplicate:
- **Active (streaming) client only.** Reconnecting clients never replay the HTTP
  stream, so they cannot double-append; they render the DB `content` once.
- **Self-heals at `done`**, where the client resets `content = getOutputText(output)`
  (`Chat.svelte:1964`), and on reload `output` wins anyway.

The "persistent, byte-identical" duplicate that 820f6cc chased was, by the pipe's
own later admission (`pipe.py`, P27 comment), largely a **testing artifact of
redeploying the pipe mid-stream in the same live chat used to verify it.**

## Current approach (2026-07-11, P34)

> **Superseded 2026-07-18 (commit `6e5f372`) for the plain assistant-stream
> branch — see the 2026-08-26 update below.** The throttled per-delta call
> described here was pulled back out of that branch one week after this was
> written, for reasons unrelated to OWUI's own version. Read this section as
> history, not current behavior.

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
is still streaming**: that reintroduces the false-alarm duplicate.

## Update: 2026-08-26, verified against live OWUI 0.11.x

Filed from autonomy card `bridge-rehydration-doc-011-reverify`, triggered by
`OWUI-0.11.1-ASK-USER-ASSESSMENT.md`'s regression-risk flag on this doc's
`ENABLE_REALTIME_CHAT_SAVE` assumption. **Read-only verification** — no pipe
code was changed for this card.

**Deployed version at test time:** `GET /api/version` on the live instance
reported `0.11.0`, not the `0.11.1` the triggering assessment researched from
the OWUI blog. The delta-streaming behavior described below was already
present at `0.11.0`, so it predates (or is unrelated to) the specific `0.11.1`
point release — treat "0.11.x" as the operative scope, not `0.11.1`
specifically, until re-checked after the next upgrade. Pipe source checked
against was `openclaw-openwebui-pipe` HEAD `d514ed2` (2026-08-22).

**Method:** drove real streaming turns through the live pipe with a raw
socket.io client (`projects/owui-stream-robustness/owui_socket.py`) to capture
every event OWUI sent, while concurrently polling `GET /api/v1/chats/{id}`
(the same DB-backed read a reconnecting/reloading client uses to rehydrate)
on a shared clock. Scripts: `projects/owui-stream-robustness/verify_011_snapshot*.py`
in the workspace repo (not this repo — throwaway verification scripts, not
shipped here).

### Finding 1 — OWUI's delta-streaming claim is confirmed live

During a plain-text, no-tool-call turn (~17s of generation, 5488 chars),
`chat:completion` websocket events carried a steadily growing `output` block
(44 → 178 → 305 → … → 5488 chars, one event roughly every 0.3s) to the
already-connected socket the whole time. This is OWUI's own incremental
delta stream, independent of anything the pipe does. **`content` in every one
of those events was empty** — OWUI is not writing plain `content` mid-run at
all in this architecture, matching the 0.11.1 blog's description.

### Finding 2 — the DB never saw partial content during either test turn

`GET /api/v1/chats/{id}`, polled every 0.3–0.5s throughout two separate
turns (17s and 28s of generation), returned `content_len=0` and no `output`
for the whole run, then flipped straight to full content + `output` +
`done=True` in the same ~0.1–0.4s window as the turn's `done` event. A client
that disconnects and reconnects mid-run — which rehydrates via this same
endpoint — currently **sees nothing until the turn is over**, on this live
instance, for an ordinary text turn. That is the exact pre-fix symptom this
doc's whole design exists to prevent.

### Finding 3 — root cause is a pipe-side change, not an OWUI 0.11.x break

The pipe only emitted **one** `replace` snapshot event per turn in both
tests, arriving within ~0.1s of `done` — not the throttled cadence
(`snapshot_interval_s = 1.0`, `snapshot_min_delta_chars = 250`) the
"Current approach (2026-07-11, P34)" section above describes. Reading
`pipe.py`'s current source explains why: the per-delta `maybe_emit_snapshot()`
call on the plain assistant-stream path (`assistant_stream_text += delta`)
was **deliberately removed** in commit `6e5f372` ("revert(pipe): remove
per-delta text back-sync snapshot (froze stream mid-message)"), dated
**2026-07-18** — one week after P34 added it, and over a month before OWUI
0.11.1 shipped. The inline comment at that call site explains the reason:

> A mid-stream `replace` carrying the full accumulated text makes OWUI's own
> `_suppress_already_shown` treat the *continuing* delta stream as
> already-shown and suppress it — the visible stream freezes mid-message
> (regression 2026-07-18, exactly the P23/P25 hazard).

So the P34 "restored throttled snapshots" design was itself walked back a
week later for a worse bug, and **this doc's "Current approach" section was
never updated to reflect that.** The `0.11.x` upgrade did not cause the
mid-run-rehydration gap observed here — it has existed since 2026-07-18,
this re-verification pass is simply the first time it was checked live
end-to-end against `GET /api/v1/chats/{id}`.

### What still gets a mid-run snapshot today

Grepping `pipe.py` for `maybe_emit_snapshot`, every remaining call site
except one passes `force=True`, and fires only at specific milestones, not
on a time/char throttle:
- a tool-call block being yielded,
- an ask-user answer being rendered,
- assorted recovery/timeout/no-response fallback text,
- the final catch-all right before the turn ends.
- The one non-forced call site left is the `MEDIA:` directive resolution
  branch — an uncommon case, not plain text streaming.

So turns that hit one of those paths (tool calls in particular) still get
real mid-run DB writes and can rehydrate on reconnect; an ordinary
tool-free text turn — the common case, and what both test turns above
were — does not, until it finishes.

### Bottom line / disposition

- **Confirmed regression-risk area, and confirmed live**, but the actual
  cause is the pipe's own 2026-07-18 change, not OWUI 0.11.x's delta
  streaming. OWUI 0.11.x's behavior (no DB write mid-run regardless of
  `ENABLE_REALTIME_CHAT_SAVE`) is consistent with what this doc already
  expected in spirit — the surprise is that the pipe stopped exercising its
  own workaround for ordinary text turns a month before this instance was
  even upgraded.
- **No pipe code changed under this card** — per its scope, a live break
  triggers a stop-and-report, not a same-card fix. This is being reported
  rather than patched here.
- **Suggested follow-up** (separate card, not executed here): decide whether
  to reinstate a *safe* mid-run text-turn snapshot now that OWUI 0.11.x's own
  delta stream already gives already-connected clients a live view — the
  remaining gap is specifically new/reconnecting clients. Any reinstated
  mechanism must not repeat the 2026-07-18 freeze (i.e. not a `replace` event
  racing the live delta stream on the same client); the comment at the
  removed call site suggests "a single terminal snapshot, or replace-only
  streaming that never also yields" as the direction to investigate, neither
  of which was implemented as of `d514ed2`.
- **Duplication tradeoff analysis above (the `ABCABC` race) is unaffected**
  by any of this — it was already scoped to snapshot events that still fire
  concurrently with the delta stream (tool blocks, forced fallbacks), and
  those call sites are unchanged.
