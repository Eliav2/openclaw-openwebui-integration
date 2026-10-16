# Mid-run rehydration and persistence

How Open WebUI 0.12 (the minimum) keeps an assistant message while its reply is still running, so a tab that reloads or opens the chat mid-turn sees the answer so far. Verified against Open WebUI 0.12.0's source and a live 0.12.0 instance.

## Open WebUI owns the in-flight message

- The streaming handler keeps the in-progress `content` and `output` in `response_streams` (`utils/middleware.py`, `save_current_response_stream`).
- `GET /api/v1/chats/{id}` overlays that copy onto the message (`routers/chats.py`, `overlay_response_streams`). Measured live on 0.12.0, the GET returned the growing text about every 2 s, so a client that reloads mid-turn is rehydrated by Open WebUI itself.
- When the reply ends, Open WebUI saves `done: true` with the final `output`: at the end of the streaming handler, or in `save_cancelled_state` when the reply is cancelled (`utils/middleware.py`).

## A done:false message cannot be written mid-run

`Chats.update_chat_by_id` (`models/chats.py`) keeps the stored copy of any assistant message whose stored or incoming `done` is false, so a whole-chat write that touches an unfinished message drops that message's change. The Pipe therefore saves no mid-run copy of its own.

## What the Pipe still sends

The Pipe sends a forced `replace` event when a reply ends or is stopped (`emit.py`, `_emit_message_snapshot`). It goes through `Chats.upsert_message_to_chat_by_id_and_message_id`, which has no `done` guard, so it is written even while the message is `done: false`. Open WebUI's end-of-reply save then sets `content` from `output` over it. So the snapshot shows only when a reply never reaches that save (Open WebUI stopped mid-reply), until the orphan reconcile finishes the message. Probed on 0.12.0 in CI: after the event the stored message read `done: false`, `content` = the snapshot; after the end-of-reply save, `done: true`, `content` = the `output` text.

## Redeploying mid-reply

Safe: a plain redeploy while a reply streams keeps the reply whole, in one message. See [redeploys](./features/reliability.md#redeploys).
