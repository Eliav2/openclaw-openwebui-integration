# Agent turns as normal chat replies (ELI-100, phase 1 design)

Status: design only, no pipe code changes. Needs manager approval before phase 2.
Baseline: main `787a471`. Every `file:line` below is in `src/openclaw_pipe_pkg/` at that
commit unless it names a dist file (OpenClaw 2026.9.3, `node_modules/openclaw/dist/`)
or an Open WebUI file (0.12.0, container `/app/backend/open_webui/` and
`/app/build/_app/immutable/chunks/`). ELI-95 is folded in (section 5).

## 0. The design in one paragraph

A run on an OWUI chat's session key gets exactly one assistant message: the message whose
id follows from the run id. For a run the pipe started from a user message, that id is
already in the run id (`msg-<chat>-<message>-<t>`, pipe.py:1206). For a run the pipe did not
start (cron, `sessions_send`, sub-agent completion, restart recovery), the id is
`uuid5(NS, "<session_key>:<run_id>")`. The pipe learns that a run started from the
Gateway's lifecycle push, not from a quiet period. It inserts the message at the chat's
true tail inside Open WebUI's own row-locked chat transaction, so a second connection or
deploy that tries the same run finds the message already there and backs off. It tells open
tabs about the message with Open WebUI's own `chat:messages` event, which moves their view
to it. It streams the same Responses items the inline path streams, and on the run's
terminal event it finalizes from the run's own transcript by run id. There is no header, no
debounce, no idle clock, no reload nudge, and no later rewrite. Whoever started the run, the
same renderer, writer and finalizer handle it.

## 1. Inventory (main 787a471)

Verdicts: **delete**, **fold** (the behavior moves into the single path, the code goes),
**keep** (with the reason given).

### 1.1 Code paths

| # | Path | Where | Verdict |
|---|---|---|---|
| P1 | Post-idle proactive delivery, event-loop branch | gateway.py:2630-2676 | delete |
| P2 | `_maybe_deliver_proactive_after_debounce` (120 s idle, 5 s poll, 600 s give-up) | gateway.py:3036-3087 | delete |
| P3 | `_deliver_proactive_owui_message` (`*↳ Proactive message*` label, gateway.py:4299) | gateway.py:4255-4320 | fold: text and cards come from the transcript by run id (P11, P12), and the write goes through the single writer (section 2.4) |
| P4 | Sub-agent delivery, event-loop branch | gateway.py:2678-2705 | delete |
| P5 | `_maybe_deliver_subagent_proactive` (same 120/5/600 timers) | gateway.py:4334-4382 | delete |
| P6 | `_deliver_subagent_proactive_owui_message` (`*↳ Sub-agent finished*`, taskId marker) | gateway.py:4385-4429 | delete. The completion reaches the parent's session as its own run (section 3, case 8), which the single path shows. Posting the child's raw text as well shows the same result twice. |
| P7 | `resolve_subagent_parent_task`, `is_subagent_session_key` | gateway.py:1912-1962 | delete (P6 is their only caller) |
| P8 | `_append_proactive_message_to_chat` | gateway.py:3234-3334 | fold into the single writer's insert (section 2.3). Anchoring on the deepest leaf, writing the parent's `childrenIds` first, and inheriting the model are kept; the process lock becomes the DB transaction. |
| P9 | `_emit_live_bootstrap_reload` (`location.reload()` on the old leaf) + `LIVE_STREAM_BOOTSTRAP_ENABLED` | gateway.py:3345-3398 | delete. Replaced by Open WebUI's `chat:messages` event (section 2.3). |
| P10 | Live relay: `LIVE_STREAM_RELAY_ENABLED`, `_RelayState`, `_relay_begin/_introduce/_feed_event/_finalize/_finalize_settled/_abort`, event-loop branches | gateway.py:3425-3837, 2553-2590, 1540-1551, 1626-1629 | fold. This is the single path's streaming core; the gates (idle clock, `_delivered_proactive`) go. |
| P11 | `_proactive_run_text` (preview + clipped-preview completion + newest-reply fallback) | gateway.py:2945-3008 | fold: the run's text comes only from `_history_text_for_run` by id. The preview/newest-reply fallbacks go, because they are what delivered a later run's text for an earlier run (2026-10-12). |
| P12 | `_proactive_run_output` | gateway.py:3011-3033 | fold (no label argument): it becomes "finished output of run R" from `_history_run_messages` + `_history_renderer` (gateway.py:2875-2942, keep). |
| P13 | `_last_assistant_text_from_preview`, `PROACTIVE_HISTORY_LIMIT`, `_preview_clip_prefix`, `_history_text_completing_preview` | gateway.py:2763-2837 | delete (callers are P11 only) |
| P14 | `_is_proactive_message`, `_relinearize_proactive_variants`, `_heal_proactive_variants`, call in pipe.py:971-974 | gateway.py:3118-3231 | delete from the pipe. The logic moves to a one-off migration script outside the pipe (section 4). |
| P15 | `_deepest_leaf_id` | gateway.py:3090-3115 | keep (the writer's anchor) |
| P16 | Parity finalize `_finalize_inline_message`, event-loop branch | gateway.py:4170-4252, 2609-2628 | fold: "the inline reply ended before its run" is a handover to the single writer for the same message id (section 2.6). Its fallback that appends a new tail message goes. |
| P17 | `register_run_target` / `unregister_run_target` / `_run_targets` / `_run_renderers` / `seed_run_renderer` | gateway.py:1635-1655, 1496-1507, 2174-2193; pipe.py:1289, 1665, 2446, 2529 | fold: one run table keyed by run id that renders every run on an OWUI key (section 2.2) |
| P18 | `mark_delivered_live` / `was_delivered_live` / `_delivered_live` | gateway.py:1821-1835, 1432; pipe.py:2445, gateway.py:2072, 2601 | delete. "Was this run shown" becomes "does the run's message exist", a DB fact. |
| P19 | `has_any_consumer_for_session`, `session_idle_for`, `_touch_session_activity`, `_session_last_activity` | gateway.py:1847-1891, 1814-1819, 1453 | delete. `has_any_consumer_for_session` is used only by gates that go. |
| P20 | Orphan stub reconcile (ELI-90): `_stub_turn`, `_orphan_stub_for_run`, `_reconcile_stub`, `_reconcile_orphan_stubs`, `_ensure_orphan_reconcile`, notes | gateway.py:3840-4167; pipe.py:369, 859 | keep, re-keyed: a stub is the message of a run by id. It runs on the same writer, and it also covers runs that ended while OWUI was down (section 3, case 4). |
| P21 | Gap repair (ELI-88/PR #17): `_align_whole_text`, `_gap_repair_*`, `_GapRepairSink`, `_mark_event_gap` | gateway.py:629-836, 2250-2270 | keep. It is the inline path's repair. Agent turns get the same result by finalizing from the transcript. |
| P22 | Retire/reap of older deploys' connections (PR #14) | gateway.py:2214-2230, 4445-4546 | keep. The DB claim (section 2.3) makes a leftover connection harmless even if one survives. |
| P23 | `action.py` switches `PROACTIVE_DELIVERY_ENABLED = False`, `LIVE_STREAM_RELAY_ENABLED = False` and their comment | action.py:36-52 | delete. The Action's connection must not run the writer; that becomes a constructor flag (`_GatewayConnection(writer=False)`), not a module constant. |
| P24 | Action: sub-agent task-id marker click redirect | action.py:1213-1225, `_SUBAGENT_TASK_ID_MARKER_RE` gateway.py:4331 | keep as the documented **read-only compatibility shim** for the 33 old `↳ Sub-agent` messages (section 4); nothing new writes the marker |
| P25 | `PROACTIVE_DELIVERY_ENABLED = True` (gateway.py:1249) and its history comment | gateway.py:1225-1249 | delete |
| P26 | `_SILENT_SENTINELS` | gateway.py:2760 | keep (a run that says only `NO_REPLY` gets no message) |
| P27 | Inline idle close on `sessions.describe` status | pipe.py:2235-2269, `gateway_run_status` pipe.py:1422-1446 | delete. This is the suspect for "reply closed mid-run" (ELI-88 repro 1): `describe.status` reads `done` during a sub-agent yield gap (gateway.py comment at 1073-1079, memory `gateway-active-run-signal`). A reply ends only on its run's terminal (section 2.5). |
| P28 | Restart resume directive `resume_after_restart` | pipe.py:1580-1675 | **keep, re-keyed and re-triggered** (M3 window 2026-10-12 23:54Z, manager decision 1). Key `msg-<chat>-<M>-resume-<t>` so the identity rule (2.2) puts it in M. It fires on the owner rule (2.7), not on `_is_restart_signature` (pipe.py:66-76, any `process exited with code`, `connection closed`) plus the 30 s idle probe and epoch bump (pipe.py:1696-1720, 1888-1891). It covers agent turns too (2.7). |

### 1.2 Flags, shared maps, locks

| Item | Where | Verdict |
|---|---|---|
| `PROACTIVE_DELIVERY_ENABLED` | gateway.py:1249, action.py:48 | delete |
| `LIVE_STREAM_RELAY_ENABLED` | gateway.py:3425, action.py:52 | delete |
| `LIVE_STREAM_BOOTSTRAP_ENABLED` | gateway.py:3345 | delete |
| shared `delivered_proactive` | gateway.py:1292, 1418 | delete (DB claim) |
| shared `delivered_live` | gateway.py:1291, 1432 | delete |
| shared `session_last_activity` | gateway.py:1293, 1453 | delete |
| shared `pending_proactive_debounce` | gateway.py:1294, 1466 | delete |
| shared `proactive_runs` | gateway.py:1295, 1473 | delete |
| shared `last_final_run` | gateway.py:1296, 1479, 2565-2566 | delete (fed only the newest-reply fallback) |
| shared `chat_write_locks` / `_chat_write_lock` | gateway.py:1297, 1492, 1657-1664 | delete. Open WebUI's `_chat_transaction` (`models/chats.py:400-407`: `BEGIN IMMEDIATE` on SQLite plus `with_for_update`) serializes writers across connections, deploys and processes. |
| `_relay_sessions` | gateway.py:1516 | fold into the run table |
| `_SHARED_STATE_ATTR` / `_shared_gateway_state` | gateway.py:1268-1298 | delete once the maps above are gone. Nothing left needs process-wide memory except the writer registry (section 2.3), which sits on the same `open_webui.socket.main` anchor. |

### 1.3 Timers

See section 5 for every timer and its decision.

### 1.4 Tests (test_pipe_unit.py unless named)

| Class (line) | Covers | Verdict, old -> new |
|---|---|---|
| ProactiveDeliveryFullTextTests (332) | clipped preview completed from history | replace: `RunTextTests`, the run's text by id only. The clipped-preview cases go with P13. |
| ProactiveDeliveryTests (539) | label, dedup, adopt, tool cards | replace: `AgentTurnWriterTests` (no header, cards from the transcript, one message per run id) |
| LiveBootstrapReloadTests (971) | reload nudge | replace: `IntroduceEventTests` (`chat:messages` payload shape, room, currentId) |
| LiveRelayTests (1093) | relay streaming/finalize | keep scenarios, retarget to the writer |
| RelayOutputEventTests (1822) | `_relay_output_events` | keep (renamed with the function) |
| EventConsumerMatchingTests (1973) | consumer routing + proactive gates | keep routing cases, drop gate cases |
| ProactiveDebounceWatcherTests (2092) | debounce | replace: `AgentTurnStartTests`, start keyed on `sessions.changed` phase start and on seq-gap recovery |
| ParkedCancelTests (2296), NoDroppedMessagesTests (2466) | Send now / queue | keep unchanged (user-turn path) |
| SubagentSessionKeyTests (2985), ResolveSubagentParentTaskTests (3007), SubagentProactiveDeliveryTests (3100), MaybeDeliverSubagentProactiveTests (3220) | P4-P7 | replace: `SubagentCompletionTests`, the announce run on the parent key renders as an agent turn, and the child's own final renders nothing |
| SubagentTaskIdMarkerRegexTests (3341) | marker regex | keep (shim P24) |
| ParityFinalizeTests (3694) | P16 | replace: `HandoverTests`, the inline reply ends early and the writer finishes the same message |
| RelinearizeProactiveVariantsTests (3804) | P14 | move with the migration script (section 4) |
| OrphanStubReconcileTests (1590) | P20 | keep, add the "ended while OWUI was down" cases |
| GatewayReloadReapTests (4121), SharedProactiveStateAcrossConnectionsTests (5955) | redeploy/zombie | replace: `TwoConnectionsOneMessageTests`, two connections race one run id and produce one message |
| test_build.py:223 `test_proactive_delivery_disabled_for_fallback_connection` | action flag | replace: the Action's connection is built with `writer=False` |
| test_status_action_unit.py:869 `SubagentProactiveMessageClickTests` | shim | keep |
| SessionToolEventTests (1382), SessionActiveSignalsTests (2959) | `session.tool`, run state | keep |

Docs: README.md (11 mentions) and docs/rehydration-persistence.md (1) get rewritten in phase 2.

## 2. The single path

### 2.1 Start signal (gateway, not timers)

Every agent run goes through one handler, `createAgentEventHandler` (dist
`server-chat-CbnqM5f6.mjs:238`). On lifecycle `start` it pushes, to every connection that
called `sessions.subscribe`:

```
sessions.changed {sessionKey, phase:"start", runId, clientRunId?, ts,
                  ...snapshot: hasActiveRun, activeRunIds, status}
```

(`server-chat-CbnqM5f6.mjs:1015-1035`; snapshot fields from
`session-event-payload-6u50DozL.mjs:150-160`). The push does not depend on who started the
run: `chat.send`, the `agent` method used by sub-agent announces
(`subagent-announce-delivery-CWxOCC67.mjs:672`), cron, and restart recovery all reach it.
Only runs flagged `projectSessionLifecycle:false` skip it, and those are internal runs
that must not be shown anyway (memory flush `agent-runner-memory-B2QTx17z.mjs:981`,
experience review `experience-review-CWxdWP7a.mjs:108`). `chat.run.started`
(`chat-send-handler-kZQfUabO.mjs:3070`) is **not** the signal, because only `chat.send`
emits it.

The pipe already subscribes (gateway.py:1530-1535) and already handles `sessions.changed`
(gateway.py:2517, 1618). Phase 2 adds: on `phase:"start"` for an OWUI key
(`parse_owui_session_key`, gateway.py:1893), open a run entry for `clientRunId or runId`.
The start push alone misses runs that start before the pipe is subscribed, for example
restart recovery at boot. The handshake below covers them.

**A dropped push is detectable.** `sessions.changed` is sent with `dropIfSlow:true`. The
broadcaster still spends a sequence number on a dropped event (`server-start-BB-IxTAg.mjs:3192-3207`:
`clientSeq.set(c, nextSeq); continue`), and every event frame carries it
(`frameWithSequence`, `:3060-3061`). The pipe tracks the frame `seq`. A gap means at least one
event was dropped, and the pipe re-reads `sessions.list` (`activeRunIds`) and opens entries
for runs it does not know. A non-droppable event that overflows closes the socket with
1008 "slow consumer" (`:3209-3214`), which takes the reconnect path. Today the pipe ignores
the frame `seq` entirely.

**Every `hello-ok` subscribes and lists in one request (M3-crash).** In the crash window
(2026-10-13 00:33-00:37Z) the pipe never saw the recovery runs' `phase:"start"`. The Gateway
starts recovery at boot, before any client has connected and subscribed. The watcher saw
only their `chat final` and `phase:"end"`. So the start push is not enough on its own:

1. On every `hello-ok` the pipe sends `sessions.subscribe` **with list params**. The handler
   registers the subscriber first (`context.subscribeSessionEvents(connId)`,
   `sessions-subscriptions-CeMquS--.mjs:21-22`) and then runs `sessionsListHandler` in the
   same request (`:27-36`). The reply is the list: subscribe, then list, with no gap between
   the two calls.
2. **A run that starts around the subscribe is caught by one of the two.**
   * The start push and `activeRunIds` both come from the same function,
     `resolveVisibleActiveSessionRunState` (`session-active-runs-BURU1M8_.mjs:62`). The push
     reaches it through `resolveSessionActiveRunState` (`server-runtime-subscriptions-B1FHK14L.mjs:1401`);
     `sessions.list` uses the projector built on it (`session-active-runs-BURU1M8_.mjs:95`,
     `sessions-read-CtBfOtH4.mjs:576-597`).
   * Every start push recorded carries `hasActiveRun: true` (2 of 2 in the crash window,
     `runs/m3-watch-window2-crash.jsonl`). So a run is visible in `activeRunIds` no later
     than its start push is built: t(visible) <= t(push).
   * Push after the registration: the pipe gets the push. Push before the registration:
     t(visible) <= t(push) < t(register) < t(list). The run is therefore in the list,
     unless it has already ended.
   * **Measured live (2026-10-13 ~01:00Z): for claude-cli runs the list gives
     `hasActiveRun: true` but no `activeRunIds`.** The function omits `runIds` whenever
     the session has an embedded run state (`session-active-runs-BURU1M8_.mjs:90`). So the
     guarantee is "the session has an active run", not its id. The pipe records the session
     as active-unknown and opens the run's message on the run's first event, which carries
     its `runId`: `chat` deltas and finals reach every connection (the M3 watcher saw
     other sessions' finals). Rule E then maps that id to M.
   * **No inference from the transcript (manager note 1).** A queued follow-up runs with no
     transcript turn at all: `04351f67` ran after recovery `f2445aa8` and left nothing,
     checked live. "Run R still active" and "R failed unclosed, and a queued follow-up F
     is active" therefore produce the same transcript. The pipe does not guess, so nothing
     is opened before the first event (`test_the_transcript_cannot_name_the_active_run`).
3. **A run that ended before the list** (for example a short recovery) is covered by the
   ended-while-away rule, case 4: the session's `updatedAt` is newer than the chat's
   `updated_at`, so `chat.history` is read, and every closed `cli-assistant:<R>` without a
   finished message gets one. A recovery run maps to its source message through rule E.
   The session row is bumped when a run ends: M2 measured this, and in the crash window the
   agent session's `updatedAt` 1791851832978 is at or after the recovery's closing message
   `cli-assistant:8f27e4c8` at 1791851832894. So a recovery that ended before the pipe
   reconnected still gets its message, without a start push.

### 2.2 Identity: which message belongs to run R

```
message_id(R) = M                        if R == "msg-<chat36>-<M36>-<suffix>"  (pipe-started)
              = uuid5(NS, f"{key}:{R}")   otherwise                               (agent turn)
no message                                 if R starts with "owui-action-" (Status Action's /compact)
```

Every pipe-started run must carry its reply id. Today two do not: `user-input-{chat}-{t}`
(pipe.py:1561) and `resume-{chat}-{t}` (pipe.py:1635). They become
`msg-<chat>-<M>-input-<t>` and `msg-<chat>-<M>-resume-<t>`. The Action's
`compact-{chat}-{t}` (action.py:1512) becomes `owui-action-compact-...`, a prefix that never
gets a message; ELI-98 may later turn it into a separator. With this rule, any connection,
deploy or restarted process can map a run to its message from the run id alone, without
shared memory.

One run table per connection, `runs[(session_key, R)]`, holds a `_TurnRenderer` fed by
every event of R (as `_run_renderers` already does, gateway.py:2536-2549) and records who is
writing that message:

* `inline`: a live `pipe()` request in this process holds R's consumer and streams through
  Open WebUI's own request (unchanged user-turn path).
* `writer`: this connection's writer task writes M (agent turns, and user turns whose
  request ended before the run).
* `none`: not claimed here.

### 2.3 Claim and anchor: one message per run, at the tail

The claim lives in the DB, not in a process map. Inside `Chats._chat_transaction(chat_id)`
(models/chats.py:400-407, row lock) the writer:

1. If `history.messages[M]` exists, the run is already claimed, so it stops. The exception
   is a handover of its own reply (2.6), keyed by `meta.openclaw_writer`, below.
2. Otherwise it takes `leaf = _deepest_leaf_id(messages, currentId)` (gateway.py:3090) and
   writes `M = {role:"assistant", parentId:leaf, childrenIds:[], content:"", output:[],
   done:false, model:<leaf's model>, timestamp, meta:{task_id, openclaw_run:R,
   openclaw_writer:<conn id>}}`, appends M to `leaf.childrenIds`, and sets `currentId=M`.
   It also mirrors the `chat_message` row (`ChatMessages.upsert_message`), as
   `insert_chat_turn` does (models/chats.py:1297-1320).

Two connections, an old deploy and a new one, can race the same R. The row lock serializes
them, and the second finds M and backs off. Two different runs that start together each
anchor on the tail they read under the lock, so they chain and never become siblings.
That is the variant-group cause P14 was healing, removed at the source. `openclaw_writer`
plus a process-wide registry of live connection ids (on `open_webui.socket.main`) lets a
writer take over a message only when its recorded writer is no longer alive.

**When (condition B).** M is created on R's first showable item, not on `phase:start`.
Showable means either of these, whichever comes first:

* a tool call start (`stream:"tool"`, `phase:"start"`), so an agent turn that runs tools
  for minutes shows its cards live from the first one;
* assistant text that is not a silent sentinel or a prefix of one
  (`_relay_content_is_showable`, gateway.py:3496).

Exception: a run whose agent payload carries `isHeartbeat:true`
(`server-chat-CbnqM5f6.mjs:822`) never gets a message. The Gateway hides heartbeat chat
output itself (`shouldHideHeartbeatChatOutput`, `:593`), and heartbeat tool cards in a user
chat would be noise.

A run that ends with only a sentinel (`NO_REPLY`, `ANNOUNCE_SKIP`) behaves as follows:

* No tool calls: it never gets a message. That matches what today's path does ("no
  assistant text, skipping").
* After tool calls: M already exists, so it is finalized with its tool cards and no text,
  `done:true`. The sentinel itself is never written. The cards stay, because the user
  already watched them stream and they are the record of what the run did. Deleting a
  message the user has seen would be a worse surprise than a card-only message.

**Showing it in an open tab without a reload.** Open WebUI 0.12 has a native multi-tab
protocol. Tabs that have a chat open join room `chat:{chat_id}` (`socket/main.py:715-742`,
`events:chat` join). An event `{chat_id, message_id, user_id, shared:true, data:{type:
"chat:messages", data:{messages, currentId}}}` is merged by the page (`rt()` in chunk
`CoHf6c6A.js`): unknown ids are added, `childrenIds` are unioned, and `currentId` moves to
the new tail unless the tab is mid-generation of its own message. Open WebUI's backend emits
exactly this for its own new turns (`main.py:1577`). Shared events from the viewer's own
`user_id` are ignored except `chat:messages`, so this event type is the right one. After it,
streaming events for M apply, because M is now a known id. This replaces the
`location.reload()` nudge (P9) and the 1.5 s handshake sleep in `owui_stream.py`. Measurement
M1 (section 7) proves it live before anything else is built on it.

**Native Stop and "generating" state.** The writer task is registered with Open WebUI's
task registry, `create_task(redis, coro, id=chat_id)` (`tasks.py:157-178`), with the task id
stored in `M.meta.task_id`. Open WebUI then lists it for the chat (`main.py:2171`, ownership
by `meta.task_id`, `models/chats.py:1947-1954`), and the Stop button's
`POST /api/tasks/chat/{id}/stop` (`main.py:2192`) cancels it (section 3, case 7).
`save_response_stream(... content, output)` (`tasks.py:199`) on each item boundary lets a tab
that opens the chat mid-turn see the current content through `overlay_response_streams`
(`routers/chats.py:1431`). That replaces the 1 s snapshot timer (section 5).

### 2.4 Streaming and tool cards

The writer feeds R's events into the renderer (`_TurnRenderer.feed`, gateway.py:1167).
Tool events reach a non-initiator connection as `session.tool`, given a transcript
subscription (`server-chat-CbnqM5f6.mjs:930-941`). The pipe opens that subscription on run
start (gateway.py:1630-1633). The renderer's output diff becomes
`response:completion` events (`_relay_output_events`, gateway.py:3460), the same item
shapes Open WebUI folds for its own turns. They are emitted through
`get_event_emitter({chat_id, message_id:M, user_id})` (`socket/main.py:1314`), so they reach
the user's room and, throttled by Open WebUI itself (0.15 s, `:1366-1371`), the shared chat
room. The DB gets `content`/`output` at each item boundary (a new item, a tool result) and
at the end. Text deltas inside an item are carried by the response stream (2.3).

### 2.5 End: the run's own terminal, nothing else

The writer ends R on whichever comes first:

* the `chat` terminal for R (`emitChatTerminal`, from `finalizeLifecycleEvent`,
  `server-chat-CbnqM5f6.mjs:353-398`), state `final|error|aborted`, carrying `yielded:true`
  when the run paused for sub-agents;
* `sessions.changed {phase:"end"|"error", runId:R}`, pushed after the session row is
  persisted (`:430-455`);
* after a seq gap or reconnect, `sessions.list` no longer listing R in `activeRunIds`.

Then it reads `chat.history` once and finalizes M from `cli-assistant:<R>` and R's tool
messages (`_history_run_messages` + `_history_renderer`, gateway.py:2875-2942). If the
transcript does not have R yet (claude-cli sometimes writes only the closing text), it keeps
what streamed, which is complete unless a gap was recorded. It writes
`done:true, content, output` under the transaction, emits a final `chat:completion` and
`chat:active {active:false}`, and lets the Open WebUI task finish.

**Yield (condition A).** A `yielded:true` terminal ends this message too, and the parent's
resume after its sub-agents is a new run with its own message. **This is today's behavior
on main, so it is no regression.** Evidence: three runs of an inline user turn that calls
`sessions_spawn` + `sessions_yield` and then resumes.

* `runs/deploy-main/yield-main.log`: the 18:51 subagent-wait test, chat `0ee165a9`, on
  main's code.
  * Reply `02eac227` closed `done:true` at the yield ("...and I've yielded. It is still
    running, so I'll reply with SUBAGENT-DONE... when its completion event arrives").
  * The resume came as a separate `*↳ Proactive message*` bubble, `2d65853b`
    ("SUBAGENT-DONE-d6013359").
  * The child's own text came as a third bubble, `↳ Sub-agent`, `31e95c10`.
* `runs/accept-composer-2.log` (20:20Z): same shape. Reply `6bfad4fd` done at the yield,
  resume `77fc7ec8` separate, sub-agent bubble `bf27c30c`.
* `runs/accept-composer3-1.log` (21:23Z): same shape. Reply `83ecadfb` done at the yield,
  resume `326f0de9` separate, sub-agent bubble `75770bf8`.

What changes:

* The resume message loses its `*↳ Proactive message*` header.
* The third `↳ Sub-agent` bubble no longer appears. The child's result reaches the chat
  through the parent's resume, which quotes it. That is the duplicate P6 removes, and it
  was approved with the design.
* Merging the resume into the yielded reply would be a behavior change. It is not
  proposed.

### 2.6 User turns use the same writer when their request ends early

A user turn starts as today: Open WebUI creates the stub M, `pipe()` sends
`msg-<chat>-<M>-<t>`, and the reply streams through the request (`inline`). If the request
ends before R does (Open WebUI cancel or close, a graceful Open WebUI stop, a reply closed by
any other path), R's entry switches to `writer` for the **same M**, starting from the
renderer it has been feeding all along. One mechanism replaces parity finalize (P16), relay
adoption of an orphaned stub (gateway.py:3287-3299, 3622-3636) and the stub completion of
PR #19. The orphan reconcile (P20) is the same handover done at startup, for runs whose
request died with the process.

An interrupted run is one more such path. The inline turn never resumes a run itself. On an
outside kill of the CLI (the 2.7 exit text), an abort after a `shutdown` notice, or a new
`bootId` after a reconnect, it hands M to the writer (`_hand_over_run`) and ends its request.
R's settled row then picks the owner by 2.7, read from the next push, or read at once when
the handover comes after the post-hello resync. `resume_after_restart`, the epoch-bump probe
and `_is_restart_signature` are gone.

### 2.7 Interrupted runs: one owner per run (M3 result, ELI-90)

M3 (window 2026-10-12 23:54Z, `ha apps restart`) showed that the Gateway does **not** recover
every interrupted run. Both runs ended `chat state:error` + `sessions.changed phase:error` in
the old boot, before the shutdown, so at boot neither session was `running` and the Gateway
started no recovery run. Main's pipe resumed the user turn (`resume-0df93712-...`, reply
`c153f06b` done, 7 cards). The agent turn stayed `status: failed`, `lastRunError: "Claude
Code process exited with code 143"`, with no continuation (its relay message `988dc34e` has 1
card and no text). Case 5 as first written ("the pipe stops sending its own resume") is wrong
for this path.

**Dist facts the rule stands on.**

* F1. Recovery touches only rows whose persisted status is `running`. At boot,
  `markStartupOrphanedMainSessionsForRecovery` scans `statuses:["running"]`
  (`main-session-restart-recovery-marking-CvQ2iWW3.mjs:222-262`, `statuses` at :235, :240). Recovery dispatch needs
  `isMainSessionRecoveryPending`: `status === "running" && abortedLastRun === true`
  (`main-session-recovery-state-CLSHE2xG.mjs:119-121`).
* F2. A run's terminal persists `status` from its outcome. The one exception is a cancel
  with `stopReason:"restart"`, which keeps `running` and sets `abortedLastRun`
  (`session-lifecycle-state-C7hBzJwC.mjs:89-90, 102`). A user or RPC abort gives `killed`;
  an error gives `failed`, with `lastRunError`.
* F3. The four ways a run is cut:
  * **Restart mode.** SIGUSR1, a restart intent on SIGTERM, or an in-process restart:
    `server.close({restartExpectedMs:1500})` (`run-CiAIvodT.mjs:1082-1086`).
    `markActiveRunsForRestartRecovery` marks the live runs, then `abortActiveRuns(restart)`
    cancels them with `stopReason:"restart"` (`server-close.runtime.js:148-186, 189-218,
    222-250`). The rows stay `running`, so **the Gateway recovers**.
  * **Gateway stop mode.** SIGTERM with no restart intent goes to `request("stop")`
    (`run-CiAIvodT.mjs:1215-1225`), and `prepareGatewayRunShutdown` with `restart:false`
    only aborts, `stopReason:"rpc"` (`server-close.runtime.js:222-226`). Nothing is
    marked, the rows go `killed`, and **nobody recovers**.
  * **Container stop (`ha apps restart`).** Every process gets SIGTERM. The claude-cli
    child exits 143 before the Gateway closes: the error push came 0.06 s after the
    signal and the 1012 close 0.26 s after (`runs/m3-watch-window1-graceful.jsonl`). Its
    error text is `Claude Code process exited with code 143` (`cli-transport-CXSP7FlN.mjs:96`),
    so the row is `failed` and **nobody recovers**. That is what M3 saw.
  * **Crash (SIGKILL, OOM).** No terminal is persisted, so the row is still `running` at
    boot and **the Gateway recovers** (F1).
* F4. Before any run is aborted, the Gateway broadcasts `shutdown {reason,
  restartExpectedMs?}` to every client (`server-start-BB-IxTAg.mjs:1595-1603`, first step of
  `prepareClose`, `:1699-1700`; `server-shutdown-Dd8ozitC.mjs:21-26`). `restartExpectedMs` is
  present only in restart mode.
* F5. The terminal `sessions.changed` phase `end|error` push is built with the active-run
  overlay (`buildSessionEventSnapshot(..., includeActiveRunState=true)`,
  `server-chat-CbnqM5f6.mjs:311-327, 455`). It still says `status: running,
  hasActiveRun: true` (measured in the crash dry run, 2026-10-13 00:14Z,
  `runs/m3-watch.jsonl`). The settled row comes in the next push for the key, which has
  `hasActiveRun: false` and carries `status`, `abortedLastRun`, `lastRunId` and
  `lastRunError` (`session-event-payload-6u50DozL.mjs:81, 103-105`). There are two
  sources, and both fire after the run's dispatch has finished:
  * `reason: "chat.run.settled"` for `chat.send` runs, from a `finally`
    (`chat-send-handler-kZQfUabO.mjs:3214-3222`). Measured: `status: done,
    hasActiveRun: false, lastRunId: R`.
  * `reason: "agent.input.settled"` for `agent`-method runs (`principal-BVWseYi9.mjs:3007-3027`).

**The rule.** The pipe reads the persisted row of R's session from one of two places:
* the settled push after R's terminal (F5);
* the first `sessions.list` after the reconnect, when no settled push reached it (crash,
  dropped frame).

The stop-mode notice in row 3 must come before R's terminal on the same connection.

| R's row | Owner | Pipe does |
|---|---|---|
| `status: running`, or no terminal at all | Gateway (F1-F3) | keeps M open and links the recovery run by rule E (case 5) |
| `failed` and `lastRunError` matches `process exited with (code 143\|code 137\|signal SIG*)` | pipe | resume directive `msg-<chat>-<M>-resume-<t>` into M |
| `killed` after a `shutdown` without `restartExpectedMs` on this connection (F4) | pipe | the same resume |
| `killed` with no such notice | nobody (a Stop) | finalizes M |
| `failed` with any other error | nobody (a real error) | shows the error in M, as today |
| `lastRunId` is a newer run | that run | nothing; rule E links a recovery run |

**Why there is never a double continuation.** The pipe resumes only rows the Gateway has
already persisted as terminal, and the Gateway recovers only rows that are `running`. A row
cannot be both. A resume run is an ordinary run, so if it is cut too, the same table applies
to it. If a crash then cuts the resume run, the recovery's prior turn is
`msg-<chat>-<M>-resume-<t>`, which rule E maps to M.

**Never into a busy session (manager note 2).** If the session has an active run
(`hasActiveRun: true`), no resume is sent, whatever R's own error was. `next_continuation`
returns `wait`, and the decision is taken again on that run's settle. A resume sent into a busy
session is queued as a follow-up, and after a restart recovery such a follow-up fails ("Reply
operation has no active tool authority snapshot", `04351f67`). That is the failure main hit
(`test_no_resume_while_a_recovery_holds_the_session`).

**When the resume is sent.** At once, on the classifying row. If `chat.send` is
refused (the Gateway is closing, the socket is gone), the resume stays on R's entry and is
sent on the next `hello-ok`. Both triggers are events: there is no idle probe and no wait.
The bound is today's `max_resume_attempts = 3` per M (pipe.py:1416), which is a count, not
a timer.

**Agent turns (manager decision 3).** They follow the same rule as user turns: the same
mechanism, the same directive, written into the agent turn's M. Today on main such a turn
is lost (above). The suggested heartbeat/cron exception is already covered by the Gateway's
own scope, and the pipe reuses it so that both sides agree on what is worth continuing:

* Cron runs never reach an OWUI key as a session push (`server-chat-CbnqM5f6.mjs:445`
  returns for cron-run-scoped keys).
* Recovery skips sub-agent, cron and ACP keys (`isMainRestartRecoveryCandidate`,
  `main-session-recovery-state-CLSHE2xG.mjs:114-118`).

The pipe resumes only on keys that predicate accepts. Every OWUI key passes it, so no
separate exception list exists.

**The text match (approval condition).** The dist has no structured exit field. The row
schema has only `lastRunError: string` (`sessions-row-BVZA0ilu.mjs:154`). It is filled by
`resolveSessionRunError` (`session-run-error-BdVGP1JC.mjs:37-40`), which passes
`outcome.error` through `renderUserFacingText` and redaction (`:9-12`). The child's exit
reaches it only as the `Error` text built at `cli-transport-CXSP7FlN.mjs:96`. So the pipe
matches text, and option (b) guards the match:

* a checked-in fixture holds that exact source line, extracted from the installed dist
  (version recorded), plus the row text seen live in window 1;
* the unit test renders the fixture's templates for 143, 137, 1 and `SIGKILL`, and checks
  the classifier on each;
* when the installed dist is present, the test extracts the line again and fails if it
  differs from the fixture. An OpenClaw upgrade that rewords the error therefore fails the
  suite instead of silently dropping resumes.

**OOM loop.** A claude child is the first OOM victim (`oom_score_adj=1000`, ELI-89). One that
is OOM-killed with the Gateway alive leaves `failed` + exit 137, so the pipe resumes. If the
resume is OOM-killed too, the same row comes back. `max_resume_attempts = 3` per M is counted
on M, not per run. After the third, M is finalized with what it has and a cut-off note, and
no fourth run is sent. A unit test drives four consecutive 137 rows.

**M3 results.**

* Window 1, 2026-10-12 23:54Z, container stop (`ha apps restart`): both rows were terminal
  before the shutdown, and no recovery ran.
  * Owner rule: pipe for both runs. Main resumed the user turn and lost the agent turn.
* Window 2, 2026-10-13 00:33-00:37Z, crash (ELI-99 kill test, PASS, relaunch 50 s): boots 2,
  1006 close, no `shutdown` notice. `owner_rule_holds=true`, `E_rule_holds=true`.
  * Agent turn: recovery `8f27e4c8` links to `m3-agent-91c29446`, and reply `df1c154f` is
    done with 6 cards and `AGENT-DONE`.
  * User turn: recovery `f2445aa8` links to the source. On main, though, the user's content
    was lost:
    * Main's pipe also sent `resume-0c5d682c-...`, which is the double continuation. It
      arrived while the recovery was active, so the Gateway queued it as a follow-up: an
      immediate `chat final` (`chat-send-handler-kZQfUabO.mjs:3206`) and no transcript turn.
    * When the recovery ended (log 00:37:18.148 "restart recovery terminal ...
      f2445aa8"), the follow-up ran as `04351f67` and failed 67 ms later: "Embedded agent
      failed before reply: Reply operation has no active tool authority snapshot". It is
      now the session's `lastRunId`, with `status: failed`.
    * Main's reply `7371727a` closed on the queued final, `done:true`, 1 card and "_(This
      turn produced no reply text.)_". The recovery's 6 cards and `USER-DONE` went nowhere.
  * `M3_pass` was false only because neither recovery's `phase:"start"` reached the
    watcher. That is the handshake gap closed in 2.1.

**Fails on main** (each is a test in section 6):

* The container stop: the agent turn is lost.
* A crash: main's idle probe sees the epoch bump and resumes while the Gateway also
  recovers, which is a double continuation. M3-crash measures this.
* `_is_restart_signature` resumes a CLI that exited with code 1.

## 3. Cases

1. **Tab open on this chat.** `chat:messages` adds M at the tail and moves the view to it.
   Responses events stream text and cards live. Native Stop works through the registered
   task. Reload mid-turn shows content from the response-stream overlay.
2. **Tab open on another chat.** Events for other chats are dropped by the page (it checks
   `event.chat_id === $chatId`). M and its content are in the DB and the response stream.
   Opening the chat loads it mid-stream through the overlay, joins `chat:{id}`, and gets the
   throttled shared `chat:completion` updates from then on.
3. **No tab open.** Same writes, nobody to emit to. Opening later shows the finished or
   in-progress message from the DB plus the overlay.
4. **OWUI down during the run (ELI-94).** Run still going when OWUI is back: the startup
   reconcile (pipe.py:369 runs at the first model list after start) reads `sessions.list`
   and opens entries for active runs on OWUI keys. The handover rule (2.6) or the claim
   (2.3) finds or creates M and continues; the end rebuilds the whole run from the
   transcript. Run ended while OWUI was down: today nothing covers this. New rule: for each
   OWUI session whose `sessions.list` `updatedAt` is newer than its chat's `updated_at`,
   read `chat.history` and give every closed run (`cli-assistant:<R>`) that lacks a
   finished `message_id(R)` its message, from the transcript. This is a comparison of two
   recorded timestamps, not a wait. M2 verifies that `updatedAt` moves at run end.
5. **Gateway restart mid-run (ELI-90).** Who continues R is decided by the owner rule
   (2.7), on R's persisted row:
   * **Gateway.** In a crash or a restart-mode shutdown the row is still `running`, and the
     Gateway's recovery run continues M. That run is a non-pipe run on the same key, linked
     by rule E below.
   * **Pipe.** After a container stop or a gateway stop-mode shutdown the row is terminal,
     and the pipe's resume run `msg-<chat>-<M>-resume-<t>` continues M.

   Either way it is one continuation, into the same message, for user and agent turns
   alike.

   **Link rule (condition E).** Recovery run R' belongs to the message of source run S when
   all three hold:

   1. **The Gateway restarted.** `hello-ok.server.bootId` differs from the one S was sent
      under (gateway.py:2271-2287).
   2. **R' is a restart recovery.** Its admitted user turn `R':user` in `chat.history`
      carries `provenance:{kind:"internal_system", sourceTool:
      "main_session_restart_recovery"}`. Dist: the recovery dispatch sets
      `idempotencyKey: recoveryRunId` and that `inputProvenance`
      (`main-session-restart-recovery-CPJjd8da.mjs:441`, `:452-456`). It is seen live in
      this session's own transcript: `eaf12616-...:user` and `42641a9c-...:user`, both with
      that provenance.
   3. **S is the run R' recovers.** The Gateway recovers the session's
      `restartRecoveryDeliverySourceRunId`. Every admitted run overwrites that field with
      its own id: `chat.send` writes `clientRunId` (`chat-send-handler-kZQfUabO.mjs:635-636`)
      and the `agent` method writes `sourceRunId` (`agent-command-C9QUtzmi.mjs:1019`). So S
      is the newest admitted run on the key before R', and in the transcript S is the newest
      `<id>:user` before `R':user` that is not itself a recovery.

   M is then `message_id(S)` by the identity rule (2.2): the `M` inside `msg-<chat>-<M>-...`
   for a user turn, or `uuid5(key:S)` for an agent turn. Every input is a recorded field.
   The session row does not expose the source-run field (`sessions.describe` checked live;
   none of the `restartRecovery*` keys are in it), which is why the transcript order is used.
   M3 proves all three conditions on a real restart.
6. **WS reconnect, same `bootId`.** Runs continue. Every open entry gets a gap mark
   (gateway.py:2250). `_subscribe_all` re-reads `sessions.list`; a run that ended during
   the drop is finalized from the transcript (2.5). Gap repair (P21) stays for inline
   replies.
7. **User sends while an agent turn streams (ELI-88).** With the writer registered as an
   Open WebUI task, the page shows the chat as generating and queues the message in its own
   queue. M4 verifies this. If the page sends anyway, `pipe()` already waits for the
   session's run to settle on `sessions.changed` (pipe.py:1172-1198, no timer) and sends
   its own run after. It never steers into the agent run.
8. **Send now / Stop during an agent turn.** Both reach the pipe as a cancel of the writer
   task. The writer installs the page intent hook (`_INTENT_HOOK_JS`, pipe.py:120) when it
   creates M, and asks the page which button was pressed (as `_settle_cancel_intent`,
   pipe.py:164, does). If the answer is Stop, it sends `chat.abort` and finalizes M. If the
   answer is Send now or unknown, it keeps writing M to the run's end, and the queued
   message runs after (case 7). One run keeps one message, and the agent turn needs no grace
   timer. Escape and the Stop button both report as Stop through the same hook.
9. **Two agent turns back to back.** Each run has its own id and its own M. The second
   anchors under the first (row lock, 2.3), so they chain in run-start order and are never
   variants.
10. **Sub-agent completion while the parent runs.** If the parent is active, the completion
    is steered into the running parent run (`subagent-announce-delivery-CWxOCC67.mjs:1205-1224`,
    `path:"steered"`). That is part of R and appears in R's message. If the parent is idle,
    an `agent` run is dispatched on the parent's key (`:672`). That is a normal agent turn
    with its own message. The child's own session is never an OWUI key, so it renders
    nothing (P4-P7 deleted).
11. **Several pipe connections after a redeploy (PR #14).** Every connection may see R's
    start. Only the one whose insert wins the row lock writes M, and a retired connection
    never claims (`_retired`, gateway.py:1380). Handover checks `openclaw_writer` against
    the live registry. A retired connection still serves the reply it streams; the new
    load routes nothing such a connection has a consumer for (`_served_by_retired`), and
    takes the run from the next event after that reply ends (PR #72). The result is one
    message, whatever the number of connections.

## 4. Existing chats

Read-only count on the live DB (2026-10-13): 325 chats; 214 `*↳ Proactive message*` and 33
`*↳ Sub-agent` messages in 44 chats; 87 variant groups that match the heal rule and are not
yet chained.

* Old messages keep their content and their header text. They are ordinary assistant
  messages to Open WebUI, and nothing in the new path reads or rewrites them. The Action's
  task-id click (P24) stays as a documented read-only shim for the 33 sub-agent messages.
* Variant groups (condition G: a separate step after the merge, not in the phase 2 PR,
  with the copy diff going to the manager first): today they are healed only when the user
  next writes in that chat
  (pipe.py:974). After the delete, the 87 stay as 1/2 and 2/2 swipes unless migrated.
  Proposal: move `_relinearize_proactive_variants` into a workspace script, not the pipe.
  1. Copy the DB.
  2. Run the script on the copy and diff every changed chat (parent chain, `currentId`,
     no message lost, all `done:true`).
  3. Show the diff to the manager.
  4. Only then apply it chat by chat through `ChatGuard` with a backup.
  If the copy run shows any group it would break, the script skips that chat.
* New agent turns cannot create variant groups (2.3), so the heal is never needed again.

## 5. Timers (ELI-95 folded in)

Turn path, pipe.py and gateway.py:

| Timer | Where | Decision |
|---|---|---|
| Proactive debounce 120 s idle / 5 s poll / 600 s give-up | gateway.py:3036-3068 | remove (2.1 start push + DB claim) |
| Sub-agent debounce, same values | gateway.py:4334-4375 | remove |
| `_RELAY_MIN_IDLE_S = 120` via `session_idle_for` | gateway.py:3432, 1866-1891, 2585 | remove |
| `_RELAY_SNAPSHOT_MIN_INTERVAL_S = 1.0` | gateway.py:3439, 3704-3723 | remove. Persist on item boundaries and keep live text in the response stream (2.3, 2.4). |
| Inline idle close, `idle_probe_s = 30` + describe | pipe.py:1407, 2235-2269 | remove (P27; end only on terminal, 2.5) |
| Restart resume trigger: the same 30 s idle probe plus the epoch bump, after a deferred `_is_restart_signature` error | pipe.py:1696-1720, 1885-1891 | remove. The resume fires on the owner rule (2.7): R's terminal push or the first `sessions.list` after `hello-ok`. |
| First-event wait 60 s + describe probe | pipe.py:1684-1719 | remove. A quiet queue before the first event is answered by run state (`sessions.changed` / `sessions.list`), which the queue wait already uses. |
| `no_text_deadman_s = 180`, `describe_check_interval = 45`, `max_events_without_text` | pipe.py:1408-1411 | remove. A live run is waited on, and a dead one has a terminal or a settle. |
| Auto-title: `sleep(3)` x 7 preview poll | pipe.py:531-545 | remove. Register a consumer for the title run and wait for its `chat` terminal, which is broadcast like any run's. |
| Event-loop `recv` timeout 90 s | gateway.py:2479 | keep, given a meaning. The Gateway ticks every 30 s (`server-constants-BrVEC7RW.mjs:9`, `TICK_INTERVAL_MS = 3e4`), so 90 s without a frame is three missed ticks, which means a dead socket: reconnect. Today it just loops. A half-open TCP socket has no event of its own, and the tick contract is the Gateway's own liveness signal. |
| Reconnect backoff 1..30 s | gateway.py:2738-2751 | keep. With no socket there is no push to wait for; trying is the only signal. |
| RPC response timeouts (connect 10 s, `send_request`, `ORPHAN_HISTORY_TIMEOUT_S = 60`) | gateway.py:21, 1706, 3994 | keep. A response frame is the only signal for an RPC. These bound a hung server and do not decide anything when the server answers. |
| Cancel grace `INTERRUPT_GRACE_SECONDS`, `_SEND_NOW_HOLD_S = 120`, `_INTENT_QUERY_TIMEOUT_S = 3`, claim wait 3 s | pipe.py:274, 160-161; gateway.py:2095-2114, 2145 | unchanged by ELI-100. This is timer (a), under its own review. Agent turns do not use it (case 8). |

Not on the turn path, unchanged by ELI-100, listed for completeness: the ask-user card
re-show and poll (askuser.py:605-648, 796, 887), the composer watcher tick
(composer_watch.py:30), and the Action's sub-agent poll and compact wait (action.py:1105-1106,
1533-1536).

## 6. Regression plan and rollback

**Tests first (deterministic, no sleeps).** Each of these must fail on 787a471 and pass on
the branch:

* `AgentTurnRendersNormallyTest`: a `sessions.changed` start, deltas, two tool calls and a
  final for a foreign run on an OWUI key produce one message at the tail with id
  `uuid5(key:R)`, no `Proactive message` text, 2 `function_call` items, and `done:true`,
  with no time advance. On main this needs 120 s of idle.
* `TwoConnectionsOneMessageTest`: two connections share one fake chat store with a real
  lock, receive the same run, and the result is exactly one message.
* `ToolCardsFromTranscriptTest`: a run whose events were partly lost is finalized from
  `cli-assistant:<R>` plus tool messages, and the card count equals the transcript's.
* `SeqGapRecoveryTest`: a missing frame `seq` triggers `sessions.list`, and a run started
  during the gap gets its message.
* `NoMidRunCloseTest`: `describe.status = "done"` while `hasActiveRun` is true does not
  end an inline reply. On main it does after 30 s idle.
* `OrphanStubAdoptedWithoutHistoryTest` (the 2026-10-12 23:29Z incident, this manager chat
  `0abe9bde`, Open WebUI OOM-restarted mid-turn).
  * On main: the startup reconcile marked stub `8acdce95` "its run is still going". When
    the run settled, `_orphan_stub_for_run` failed with `chat.history failed: ` (an
    exception with an empty message, 4 times, at gateway.py:3976-3980) and returned None.
    The relay then introduced a new message `32e095ae` (5872 chars, 7 cards) under an empty
    `done:false` stub.
  * Test: a chat holds stub M with `done:false`, run `msg-<chat>-<M>-<t>` arrives on a
    connection with no consumer, and `session_history` raises `TimeoutError()`. Expected:
    the run writes into M (identity from the run id, 2.2, no transcript read needed) and M
    ends `done:true` with the run's text and cards. No second message.
* `UserTurnNoTabTest` (side finding of the M3 dry run, 2026-10-12, main, no tab open): a user
  turn sent through the API with no page left the stub `done:false` and empty, and the answer
  arrived as a second bubble with 1 of 5 cards. The agent turn got 2 of 5 cards. Expected:
  one message M, `done:true`, 5 cards.
* `OwnerRuleTest` (2.7), one case per row of the table. Each is fed the dist-shaped event
  sequence: the terminal push with the `running` overlay, then the settled push.
  * container stop (failed, exit 143) -> resume into M;
  * restart mode (running + abortedLastRun) -> no resume, recovery linked;
  * crash (no terminal, row `running` after hello) -> no resume;
  * gateway stop (killed after `shutdown` without `restartExpectedMs`) -> resume;
  * user Stop (killed, no notice) -> no resume;
  * real error, and CLI exit 1 -> no resume.

  On main, the crash case resumes (a double continuation), the agent-turn container stop
  is lost, and exit 1 resumes.
* `CrashDoubleContinuationTest` (M3-crash, user turn): a crash, then recovery `f2445aa8`
  starts at boot. On main the pipe also sends `resume-...`, which the Gateway queues behind
  the recovery. The reply closes on the queued run's immediate final, with no text and 1 of
  6 cards. Expected: no pipe resume (the row is `running`), and M gets the recovery's 6 cards
  and text.
* `RecoveryStartedBeforeSubscribeTest`: the recovery's start push never arrives. The run is
  in the `sessions.subscribe` list reply (still active), or it has already ended and is
  found by the case 4 rule. Either way it writes into M.
* `ExitTextFixtureTest` and `OomResumeLoopTest` (2.7).
* `ResumeQueuedUntilHelloTest`: `chat.send` for the resume is refused while the Gateway
  closes, and it is sent on the next `hello-ok`, with no time advance.
* Plus the old -> new list in 1.4, which goes in the PR body.

**Grep gate on the merged tree:** zero `Proactive message`, zero `PROACTIVE_`, zero
`_proactive`, except the shim in action.py and its test, named in the PR.

**Live gate** (scratch OWUI chat, real pipe, DB plus a reloaded page each time), per the
ticket. Cases:

* cron wake into the chat
* `sessions_send` into the chat
* sub-agent completion announce
* tab closed then opened mid-turn
* user message while an agent turn streams
* gateway reconnect mid agent turn
* Stop during an agent turn (condition C): the run is aborted and M is finalized with what it
  had
* Send now during an agent turn (C): M keeps the whole run, and the queued message gets its
  own reply after
* two agent turns back to back (C): two messages chained, no variant group
* OWUI down during the run (case 4) and gateway restart mid-run (case 5), for both an agent
  turn and a user turn: these need a restart, which the manager runs in a window. The worker
  prepares the scenario and the checks and hands over with NEEDS-MANAGER.

Each case is checked for:

* one assistant message per run
* a linear parent chain
* text equal to the transcript
* card count equal to the transcript's
* `done:true`
* no header

The integration suite and `test_history_guard.py --live` 16/16 must stay unchanged. The
manager re-runs at least three cases.

**Rollout:** one PR, deployed after the measurements in section 7 pass.

**Rollback:**

1. Revert the merge commit and redeploy main's previous artifact (install.py backs up the
   live function before every deploy: `backup_function`, install.py:377).
2. Messages written by the new path are plain assistant messages with extra `meta` keys,
   which the old code reads as normal messages. No DB change needs undoing.
3. The variant migration (section 4) is separate, applied only with its own backup, and
   reversible from that backup.

## 7. Measurements phase 2 must pass before building on them

These come from reading the Open WebUI bundle and the dist, not from a live run. Each one
gets a live proof in a scratch chat first. If one fails, the design changes at that point
and comes back for approval.

* **M1** `chat:messages` (shared, room `chat:{id}`) adds a REST-created assistant message
  to an open tab and moves `currentId`, and the next user message parents to it. Proof:
  DB `parentId` plus the page.
* **M2** `sessions.list` `updatedAt` for an OWUI session moves when a run ends (case 4,
  ended during downtime).
* **M3** (needs a Gateway restart; the manager runs it) After a real Gateway restart with
  one user turn and one agent turn in flight, each of these must hold:
  * the recovery run's `sessions.changed` start reaches the pipe after its reconnect, or
    `sessions.list` lists it in `activeRunIds`;
  * `R':user` carries the `main_session_restart_recovery` provenance;
  * the newest non-recovery `<id>:user` before it is the interrupted run.

  If the first does not hold, the pipe directive (P28) stays and is re-keyed to
  `msg-<chat>-<M>-resume-<t>`.

  **Result, window 2026-10-12 23:54Z (container stop): FAIL for "the Gateway recovers every
  interrupted run".** No recovery run at all. Both rows were terminal before the shutdown
  (2.7). Two boots, 0 seq gaps, smoke keys 0. P28 stays, re-keyed, and is triggered by the
  owner rule.

  **M3-crash** (the ELI-99 kill test as the restart, `m3_orchestrate.sh crash`, prepared,
  not yet run). The rule says the Gateway owns both runs. Passes if, for both turns:
  * a recovery run exists;
  * rule E links it to the source run;
  * its start push reaches a reconnected client.

  On main, a pipe resume next to the recovery is expected for the user turn (a double
  continuation). That is recorded as a fails-on-main datum, not as an M3 failure.
* **M4** A chat whose only active Open WebUI task is the writer's shows as generating, and
  the composer queues instead of sending. If not, case 7 still holds through `pipe()`'s
  existing wait, and this becomes a nicety.
* **M5** `create_task(..., id=chat_id)` from inside the Function is stopped by the page's
  Stop button, and the cancel reaches the writer.
