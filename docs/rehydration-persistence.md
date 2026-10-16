# Mid-run rehydration and persistence

How Open WebUI 0.12 (the minimum) keeps an assistant message while its reply is still running, so a tab that reloads or opens the chat mid-turn sees the answer so far. Verified against Open WebUI 0.12.0's source and a live 0.12.0 instance.

## Open WebUI owns the in-flight message

- The streaming handler keeps the in-progress `content` and `output` in `response_streams` (`utils/middleware.py`, `save_current_response_stream`).
- `GET /api/v1/chats/{id}` overlays that copy onto the message (`routers/chats.py`, `overlay_response_streams`). Measured live on 0.12.0, the GET returned the growing text about every 2 s, so a client that reloads mid-turn is rehydrated by Open WebUI itself.
- When the reply ends, Open WebUI saves `done: true` with the final `output`: at the end of the streaming handler, or in `save_cancelled_state` when the reply is cancelled (`utils/middleware.py`).

## A done:false message cannot be written mid-run

`Chats.update_chat_by_id` (`models/chats.py`) keeps the stored copy of any assistant message whose stored or incoming `done` is false, so a whole-chat write that touches an unfinished message drops that message's change. An inline reply therefore saves no mid-run copy of its own; the turn writer's single-message writes below have no such guard.

## What the Pipe still sends

The Pipe sends a forced `replace` event when a reply ends or is stopped (`emit.py`, `_emit_message_snapshot`). It goes through `Chats.upsert_message_to_chat_by_id_and_message_id`, which has no `done` guard, so it is written even while the message is `done: false`. Open WebUI's end-of-reply save then sets `content` from `output` over it. So the snapshot shows only when a reply never reaches that save (Open WebUI stopped mid-reply), until the orphan reconcile finishes the message. Probed on 0.12.0 in CI: after the event the stored message read `done: false`, `content` = the snapshot; after the end-of-reply save, `done: true`, `content` = the `output` text.

## The turn writer

A run the Pipe writes outside a live request (an agent turn, or a user turn whose request ended first: a handover) is shown the same way: every event updates the message's response stream (`save_response_stream`, overlaid on a GET), and the live events go to open tabs. The DB is written:

- when the writer claims the message (`_claim_turn_message`: a new message, or the writer recorded on an existing one);
- once at a handover, with what the request showed. The request's own end-of-stream save writes `done: true` over the message, so this write waits for the request's Open WebUI task to end (that task runs the save), then puts back `done: false`;
- at the end (`finalize`), from the transcript where it covers the runs, else from what streamed.

Every write rewrites the whole chat, about 14 times its JSON in Open WebUI's memory, so a write per item (two per tool call) was most of a long chat's churn. Before ELI-115 the writer also wrote on every item boundary. What that changes when a process dies mid-turn:

| Case | In the DB mid-turn, per item (before) | In the DB mid-turn (now) | How it ends (both) |
|---|---|---|---|
| Writer alive to the end, including a Gateway crash or restart while Open WebUI stays up | each item | the claim, or the handover | `finalize` from the writer's memory and the transcript; the mid-turn DB copy is never read |
| A tab reloads or opens the chat mid-turn | each item | the same | the GET overlays the response stream, so the tab sees every event either way |
| Open WebUI dies (OOM, restart), the run goes on or ends while it is down | up to the last item | the claim, or the handover | the orphan reconcile adopts the `done: false` message; its writer starts empty and `finalize` rebuilds the run from the transcript. Before, the adopted writer's first item write replaced the copy with its own partial one anyway |
| Open WebUI dies right after a handover, before the request's task ended | `done: true` if the request's save came last | `done: true` if the request's save ran, else the stub's `done: false` | a pipe run is taken back by its next event; a run that ended while down is completed by the ended-while-down pass |
| Open WebUI dies after a handover, and the run is killed too | `done: false` (adopted, then resumed) once an item followed the request's save | `done: false` (adopted, then resumed): the handover's write always follows the save | the resume run is written into the message |
| Open WebUI dies and the run is killed, and nothing continues it (no resume, no note) | the last item's copy, still `done: false` | the claim's or the handover's copy, still `done: false` | the transcript has no killed claude-cli run, so this copy is all there is: the calls after the claim or handover are lost. Accepted: it needs both processes dead and no continuation, and a write per call to cover it was most of the churn |

A writer that adopts a message after a crash does not read the copy back: its renderer starts empty, so the resume quotes nothing as already shown, and a killed run's earlier cards survive only in the last row's case.

## Redeploying mid-reply

Safe: a plain redeploy while a reply streams keeps the reply whole, in one message. See [redeploys](./features/reliability.md#redeploys).
