# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------



async def _emit_status(__event_emitter__, description, *, done=False):
    """Send an OWUI status event when the current pipe call supports events."""
    if not __event_emitter__:
        return
    await __event_emitter__(
        {"type": "status", "data": {"description": description, "done": done}}
    )


# Claude Code's in-process sub-agent tool, and what its card's result says for
# one started in the background: its report comes later, and the run stays
# open until it is in (ELI-163).
_AGENT_TOOL_NAME = "Agent"
_AGENT_LAUNCHED_PREFIX = "Async agent launched"


def _is_agent_tool(name) -> bool:
    """Claude Code calls it `Agent`; in any case, as the Gateway lowercases
    the names of some tools. OpenClaw has no tool of its own by that name."""
    return (name or "").strip().lower() == _AGENT_TOOL_NAME.lower()


def _is_background_agent_launch(name, result_text, is_error=False) -> bool:
    return (not is_error and _is_agent_tool(name)
            and (result_text or "").strip().startswith(_AGENT_LAUNCHED_PREFIX))


def _background_agents_line(n: int) -> str:
    """The status row of a reply a background agent holds open."""
    return f"Waiting on {n} background agent{'' if n == 1 else 's'}"


def _fmt_tokens(n) -> str:
    """Compact token count: 115212 -> '115k', 1000000 -> '1.0m'."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def _format_goal_line(goal: dict) -> str:
    """Mirror the TUI footer's goal phrasing (see docs/tools/goal.md)."""
    status = goal.get("status")
    tokens_used = goal.get("tokensUsed")
    budget = goal.get("tokenBudget")
    used_fmt = _fmt_tokens(tokens_used) if tokens_used is not None else "?"
    if status == "active":
        if budget:
            return f"🎯 Pursuing goal ({used_fmt}/{_fmt_tokens(budget)})"
        objective = (goal.get("objective") or "").strip()
        if len(objective) > 40:
            objective = objective[:39] + "…"
        return f"🎯 Pursuing goal: {objective}" if objective else "🎯 Pursuing goal"
    if status == "paused":
        return "🎯 Goal paused (/goal resume)"
    if status == "blocked":
        return "🎯 Goal blocked (/goal resume)"
    if status == "usage_limited":
        return "🎯 Goal hit usage limits (/goal resume)"
    if status == "budget_limited":
        return f"🎯 Goal unmet ({used_fmt}/{_fmt_tokens(budget)})"
    if status == "complete":
        return f"🎯 Goal achieved ({used_fmt})"
    return ""


def _window_bit(w: dict) -> str | None:
    used = w.get("usedPercent")
    if used is None:
        return None
    return f"{w.get('label')} {round(100 - used)}% left"


def _relative_time(reset_at_ms) -> str | None:
    """Compact relative countdown: '22m', '4h07m', '3d07h'. None if unknown/past."""
    if not reset_at_ms:
        return None
    delta_s = int((reset_at_ms - time.time() * 1000) / 1000)
    if delta_s <= 0:
        return None
    days, rem = divmod(delta_s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _window_reset_line(w: dict) -> str | None:
    """Full detail line for one rate-limit window, only when it has a known
    reset time -- e.g. '⏱ 5h 18% left (resets in 22m)'. Skipped for windows
    without a `resetAt` (e.g. Gemini's Pro/Flash aren't time-windows), since
    there'd be nothing new to say beyond what the thin combined line already
    shows.
    """
    bit = _window_bit(w)
    rel = _relative_time(w.get("resetAt"))
    if not bit or not rel:
        return None
    return f"⏱ {bit} (resets in {rel})"


async def _build_usage_status_lines(conn, session_key, timeout: float = 5,
                                    run_usage: dict | None = None) -> list[str]:
    """Best-effort final status lines: context window fill (combined with the
    provider's primary/nearest-term rate-limit window on the same line, kept
    deliberately thin -- no reset time), one full detail line per rate-limit
    window that has a known reset time (e.g. both '5h' and 'Week' for
    Anthropic/OpenAI), and the active session goal (if any) on its own line.
    Returned as separate short lines (rather than one combined line) because
    OWUI's status UI hard-clamps every line to a single row (`line-clamp-1`,
    no way to disable per-event) -- one line per fact keeps each row inside
    that clamp instead of getting cut off mid-number.

    OWUI shows only the *last* emitted status line by default (the rest
    only appear once the user expands that message's status history), so
    the thin context+primary-window line is placed last on purpose. Goal and
    the per-window reset-time detail lines still show up immediately on
    expand.

    Both RPCs are read-only and already used elsewhere by the pipe
    (`sessions.describe`) or are simple/cheap (`usage.status`); any failure
    here must not affect the actual reply, so every error is swallowed and
    just omits that line. They're fetched concurrently (not one after the
    other) so this adds at most one round trip's worth of latency, not two,
    to the moment the status line settles after the reply text is done.

    `run_usage`: the reply's own run's last-call usage. Its prompt tokens are
    the context fill, over the row's `totalTokens`: OpenClaw 2026.10 writes a
    session's first totals to its row just after the run ends, so a new
    chat's first reply found none there, and every later reply read the run
    before it (ELI-177). The window (`contextTokens`) still comes from the row.
    """
    describe_task = asyncio.ensure_future(
        conn.send_request("sessions.describe", dict(key=session_key), timeout=timeout)
    )
    usage_task = asyncio.ensure_future(conn.send_request("usage.status", {}, timeout=timeout))

    context_bit = None
    goal_line = None
    provider = None
    try:
        desc = await describe_task
        session_row = (desc or {}).get("session") or {}
        provider = session_row.get("modelProvider")
        context_tokens = session_row.get("contextTokens")
        total_tokens = _usage_prompt_tokens(run_usage) or session_row.get("totalTokens")
        if context_tokens and total_tokens is not None:
            pct = round(total_tokens / context_tokens * 100, 1)
            context_bit = f"🧠 {_fmt_tokens(total_tokens)}/{_fmt_tokens(context_tokens)} ({pct:g}%)"
        goal = session_row.get("goal")
        if goal:
            goal_line = _format_goal_line(goal) or None
    except Exception:
        pass

    primary_bit = None
    reset_lines = []
    try:
        usage = await usage_task
        if provider:
            for p in (usage or {}).get("providers", []):
                if p.get("provider") != provider:
                    continue
                windows = p.get("windows") or []
                bits = [b for b in (_window_bit(w) for w in windows) if b]
                if bits:
                    primary_bit = f"⏱ {bits[0]}"
                elif p.get("summary"):
                    primary_bit = f"⏱ {p['summary']}"
                # A full "(resets in ...)" line per window that actually has
                # a known reset time -- including the same window already
                # folded into `primary_bit`, since that one stays thin (no
                # reset time) on purpose.
                reset_lines = [
                    line for line in (_window_reset_line(w) for w in windows) if line
                ]
                break
    except Exception:
        pass

    context_and_primary = " · ".join(bit for bit in (context_bit, primary_bit) if bit) or None

    return [line for line in (goal_line, *reset_lines, context_and_primary) if line]


async def _emit_message_snapshot(__event_emitter__, content):
    """Write `content` to the reply's stored message with a `replace` event.

    Open WebUI 0.12 writes it even while the message is `done: false` (the
    message upsert has no done guard), and its end-of-reply save then sets
    `content` from `output`, over it. So it shows only when a reply never
    reaches that save. Probed on 0.12.0 (ELI-160 slice 8).
    """
    if not __event_emitter__ or not content:
        return
    await __event_emitter__(
        {"type": "replace", "data": {"content": content}}
    )



# ---------------------------------------------------------------------------
# A tab's input dialog and `execute` share one callback (ELI-195).
#
# Open WebUI 0.12's Chat.svelte keeps one callback slot for its `input` and
# `confirmation` dialogs and for `execute`: each of these events overwrites
# it on arrival, and the dialog's Confirm / Cancel answers whatever the slot
# holds then. An `execute` that reaches a tab while its dialog is open takes
# the dialog's answer, so the click is lost and the asker never hears back.
# While a dialog is open on a socket, the socket is recorded here (shared by
# the Pipe and the Action through `_shared_gateway_state`): an emitted
# `execute` skips it and is delivered once the dialog is answered, and a
# called one (a query) is not sent to it.
# ---------------------------------------------------------------------------

_SLOT_DIALOG_TYPES = ("input", "confirmation")
_local_dialog_holds: dict = {}


def _dialog_holds() -> dict:
    """sid -> {"user_id", "open": open dialogs, "held": event payloads}."""
    state = _shared_gateway_state()
    return _local_dialog_holds if state is None else state.setdefault("dialog_holds", {})


def _dialog_open_on(sid) -> bool:
    return bool(sid) and sid in _dialog_holds()


@contextlib.asynccontextmanager
async def _holding_executes(sid, user_id, kind):
    """Record `sid` as showing a dialog of `kind` for the block's duration,
    then deliver the `execute` events held for it meanwhile, in order."""
    if not sid or kind not in _SLOT_DIALOG_TYPES:
        yield
        return
    holds = _dialog_holds()
    entry = holds.get(sid)
    if entry is None:
        entry = holds[sid] = {"user_id": user_id, "open": 0, "held": []}
    entry["open"] += 1
    try:
        yield
    finally:
        entry["open"] -= 1
        if entry["open"] <= 0 and holds.get(sid) is entry:
            del holds[sid]
            if entry["held"]:
                pipe_log(f"  dialog on {str(sid)[:8]}... answered: delivering "
                         f"{len(entry['held'])} held execute event(s)")
            for payload in entry["held"]:
                try:
                    from open_webui.socket.main import sio
                    await sio.emit("events", payload, to=sid)
                except Exception as ex:
                    pipe_log(f"  held execute to {str(sid)[:8]}... failed: {ex!r}")


def _dialog_safe_emitter(emitter, user_id, chat_id, message_id):
    """`emitter`, except that an `execute` skips the user's sockets with an
    open dialog (OWUI's emitter sends to the whole `user:<id>` room) and is
    held for them."""
    if emitter is None or not user_id:
        return emitter

    async def emit(event):
        busy = []
        if isinstance(event, dict) and event.get("type") == "execute":
            holds = _dialog_holds()
            busy = [sid for sid, e in list(holds.items()) if e.get("user_id") == user_id]
        if not busy:
            return await emitter(event)
        payload = {"chat_id": chat_id, "message_id": message_id, "data": event}
        for sid in busy:
            holds[sid]["held"].append(payload)
        pipe_log(f"  execute held for {len(busy)} tab(s) with an open dialog")
        from open_webui.socket.main import sio
        await sio.emit("events", payload, room=f"user:{user_id}", skip_sid=busy)

    return emit


def _dialog_safe_event_call(event_call, sid, user_id):
    """`event_call` (bound to socket `sid`), recording its dialogs and never
    sending an `execute` while one is open there: that query answers None."""
    if event_call is None:
        return None

    async def call(payload):
        kind = (payload or {}).get("type")
        if kind == "execute" and _dialog_open_on(sid):
            pipe_log(f"  execute query not sent: {str(sid)[:8]}... shows a dialog")
            return None
        async with _holding_executes(sid, user_id, kind):
            return await event_call(payload)

    return call
