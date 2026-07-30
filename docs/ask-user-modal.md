# Ask-User Modal — Design Spec

> **Status: design spec, not implemented as written.**
>
> This document proposes a `request_user_input` function-calling tool. That tool
> **does not exist** — the shipped implementation in
> [`src/openclaw_pipe_pkg/askuser.py`](../src/openclaw_pipe_pkg/askuser.py) took
> the other path: it intercepts assistant text beginning with
> `OpenClaw needs input:` or `Codex needs input:` and synthesizes the modal from
> the prompt text. Notable divergences from this spec:
>
> - **Trigger:** assistant text deltas, not a typed tool-call event.
> - **`secret`:** inferred by keyword-scanning the prompt, not a tool parameter.
> - **N-way choices:** options are scraped from numbered lines (`1. foo`) and
>   rendered via a hand-built widget, because OWUI has no native
>   "pick one of N buttons" control. Only genuine yes/no prompts become a
>   confirmation dialog.
> - **Timeouts:** 60s for the modal, 300s across a reconnect — not the single
>   5-minute timeout described below, and no `{"error": "User did not respond"}`
>   payload.
>
> Kept for the design rationale and as the reference for a future typed-event
> version. Read `askuser.py` for current behavior.

## Problem

Agents need to ask the user a question during a run. OpenClaw core supports
this via Codex `request_user_input` / Claude `AskUserQuestion`, but these are
not available in Default / model-agnostic mode. The current pipe MVP (P22)
tries to detect `"needs input:"` text patterns, which is brittle and has no
reliable typed event to trigger on.

## Solution

The pipe exposes a **function-calling tool** called `request_user_input` that
any model can call. The tool:

1. Opens a native OWUI input modal / choices UI
2. Waits for the user to answer
3. Returns the answer as the tool result
4. The model receives the answer and continues its reasoning

## Why this works cross-provider

- Function calling is supported by every mainstream provider
  (OpenAI, Anthropic, DeepSeek, Google, OpenRouter)
- The pipe defines the tool, not the Gateway — no OpenClaw core change needed
- The tool only needs `text` + optionally `choices`; no provider-specific types

## Tool schema

```
name: request_user_input
description: Ask the user a question. Returns their answer as a string.
parameters:
  type: object
  properties:
    question:
      type: string
      description: The question to ask the user. Clear, specific, one question.
    choices:
      type: array
      items:
        type: string
      description: Optional list of predefined choices. The user can pick one or
                   type a custom answer.
    secret:
      type: boolean
      description: If true, the input should not be echoed visually. Use for
                   passwords or sensitive values.
  required: [question]
```

## Pipe behavior

### When the agent calls the tool

1. The pipe receives the tool call event (`stream="tool"`, `phase="start"`)
2. If the user is in the same OWUI chat (most common case):
   - **Choices present (2–5):** render as OWUI `confirmation` event with
     button-like options
   - **Choices > 5 or free-text only:** render as an OWUI `input` event
   - **`secret=True`:** render as `input` with `type: "password"`
3. The pipe blocks (does not yield further events) until the user answers
4. When the answer arrives (as the next `chat.send` in the same session),
   yield a `tool` result event with the answer as `output`

### Timeout / safety

- If no answer arrives within 5 minutes, yield `{"error": "User did not respond"}`
- If the user switches to a different OWUI chat, the pending input is
  silently cancelled (the answer would go to the wrong session)

### Fallback

If `__event_emitter__` is unavailable or the modal call fails:

1. Yield the question as plain assistant text in the streaming output
2. Wait for the user's next message in the same session
3. Continue as if the tool had returned that message as the answer

This means even without modal support, the agent can still ask questions.

## Implementation plan

### Phase 1 — Tool registration

In `Pipe.__init__` or a new method, register `request_user_input` as an
available function tool. The Gateway must be informed of the tool schema
so it can offer it to the agent.

**Key detail:** The tool schema must be sent to the Gateway via the
`functions` / `tools` field at session start (or session patch). The pipe
already sends messages via the WS; we need to include tool definitions
at `__meta__` or in the first message.

### Phase 2 — Tool call handling

In `pipe()`, when the Gateway yields a tool-call start event for
`request_user_input`:

1. Parse the tool arguments (`question`, optional `choices`, optional `secret`)
2. Call `__event_emitter__` with the appropriate modal event
3. Save pending state (session key, run id, tool call id)
4. Block the generator (store a future, yield nothing more)
5. When the next user message arrives for this session, resolve the future
   with the user's answer text

### Phase 3 — Answer routing

The pipe already has a mechanism to handle user messages during an active run
(steering). We extend it: if there is a pending `request_user_input` for this
session+run, treat the new message as the **answer** to that tool call, not as
a new steer message.

### Phase 4 — Persistence

Pending user inputs survive only in memory (pipe instance state). If the pipe
restarts, pending inputs are lost. This is acceptable for v1; the agent will
see a timeout error and can re-ask.

## Open questions

1. **How does the pipe inform the Gateway of available tools?**
   - Is it via `__functions__` / `__tools__` in the first WS message?
   - Or does the Gateway discover tools from the agent config?
   - Needs investigation of the Gateway WS protocol for tool registration.

2. **Does the pipe need to manage tool call state across yield points?**
   - Since `pipe()` is an async generator, we can hold a `Future` between
     yields, but the OWUI event loop also runs — we need to ensure the
     answer arrives on the same run context.

3. **What happens if the user types a free-form answer when choices are given?**
   - Proposal: always accept free text; choices are UI hints, not constraints.
   - The model sees the actual user text and decides how to interpret it.
