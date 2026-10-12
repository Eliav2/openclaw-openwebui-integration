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
| P28 | Restart resume directive `resume_after_restart` | pipe.py:1580-1675 | fold, pending one measurement (section 7, M3): the Gateway already starts its own recovery run for every session that was `running` (dist `main-session-restart-recovery-CPJjd8da.mjs:166`, `:1578-1597` with `statuses:["running"]`). That run is a non-pipe run on the same key, so the single path continues the open reply with it. A pipe directive on top of it is a second continuation. |

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

**A dropped push is detectable.** `sessions.changed` is sent with `dropIfSlow:true`. The
broadcaster still spends a sequence number on a dropped event (`server-start-BB-IxTAg.mjs:3192-3207`:
`clientSeq.set(c, nextSeq); continue`), and every event frame carries it
(`frameWithSequence`, `:3060-3061`). The pipe tracks the frame `seq`. A gap means at least one
event was dropped, and the pipe re-reads `sessions.list` (`activeRunIds`) and opens entries
for runs it does not know. A non-droppable event that overflows closes the socket with
1008 "slow consumer" (`:3209-3214`), which takes the reconnect path. Today the pipe ignores
the frame `seq` entirely.

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

**When.** M is created on R's first showable content, not on `phase:start`. The rule is the
one the relay uses today (`_relay_content_is_showable`, gateway.py:3496): non-empty text
that is not a silent sentinel or a prefix of one. Tool cards rendered before that are part
of the renderer's output and go in with M. A run that ends silent (`NO_REPLY`, heartbeat
`HEARTBEAT_OK`) never gets a message. This matches what the proactive path does today
("no assistant text, skipping").

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
`chat:active {active:false}`, and lets the Open WebUI task finish. A `yielded:true`
terminal ends this message too. The parent's resume after its sub-agents is a new run with
its own message (case 8).

### 2.6 User turns use the same writer when their request ends early

A user turn starts as today: Open WebUI creates the stub M, `pipe()` sends
`msg-<chat>-<M>-<t>`, and the reply streams through the request (`inline`). If the request
ends before R does (Open WebUI cancel or close, a graceful Open WebUI stop, a reply closed by
any other path), R's entry switches to `writer` for the **same M**, starting from the
renderer it has been feeding all along. One mechanism replaces parity finalize (P16), relay
adoption of an orphaned stub (gateway.py:3287-3299, 3622-3636) and the stub completion of
PR #19. The orphan reconcile (P20) is the same handover done at startup, for runs whose
request died with the process.

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
5. **Gateway restart mid-run (ELI-90).** The new `bootId` (gateway.py:2271) says the runs
   are gone. The Gateway's own recovery starts a run on every session that was `running`
   (dist above). That run is a non-pipe run on the same key. For an open user reply, the
   entry adopts it into the same M (one reply, as today). For an agent turn, M is finalized
   from the transcript with the existing cut-off note if the run left nothing, and the
   recovery run gets its own message. The pipe stops sending its own resume directive,
   subject to M3.
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
    the live registry. The result is one message, whatever the number of connections.

## 4. Existing chats

Read-only count on the live DB (2026-10-13): 325 chats; 214 `*↳ Proactive message*` and 33
`*↳ Sub-agent` messages in 44 chats; 87 variant groups that match the heal rule and are not
yet chained.

* Old messages keep their content and their header text. They are ordinary assistant
  messages to Open WebUI, and nothing in the new path reads or rewrites them. The Action's
  task-id click (P24) stays as a documented read-only shim for the 33 sub-agent messages.
* Variant groups: today they are healed only when the user next writes in that chat
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
| First-event wait 60 s + describe probe | pipe.py:1684-1719 | remove. A quiet queue before the first event is answered by run state (`sessions.changed` / `sessions.list`), which the queue wait already uses. |
| `no_text_deadman_s = 180`, `describe_check_interval = 45`, `max_events_without_text` | pipe.py:1408-1411 | remove. A live run is waited on, and a dead one has a terminal or a settle. |
| `rest_snapshot_interval_s = 2.5` | pipe.py:1400-1404 | remove if the minimum supported Open WebUI is 0.12, where it is already off (`_owui_owns_inflight_message`). Otherwise keep as it is, with no change in behavior. |
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

Each case is checked for:

* one assistant message per run
* a linear parent chain
* text equal to the transcript
* card count equal to the transcript's
* `done:true`
* no header

The integration suite and `test_history_guard.py --live` 16/16 must stay unchanged. The
manager re-runs at least three cases.

**Rollout:** one PR, deployed with `--dev-bundle` after the measurements in section 7 pass.

**Rollback:**

1. Revert the merge commit and redeploy main's previous artifact (install.py backs up the
   live function before every deploy: `_backup_function`, install.py:375-388).
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
* **M3** After a real Gateway restart, the recovery run's `sessions.changed` start reaches
  the pipe after its reconnect, or `sessions.list` lists it in `activeRunIds`. If neither
  holds, the pipe directive (P28) stays and is re-keyed to `msg-<chat>-<M>-resume-<t>`.
* **M4** A chat whose only active Open WebUI task is the writer's shows as generating, and
  the composer queues instead of sending. If not, case 7 still holds through `pipe()`'s
  existing wait, and this becomes a nicety.
* **M5** `create_task(..., id=chat_id)` from inside the Function is stopped by the page's
  Stop button, and the cancel reaches the writer.
