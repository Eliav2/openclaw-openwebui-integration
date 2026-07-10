# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------



_USER_INPUT_TRIGGER_PREFIXES = ("Codex needs input:", "OpenClaw needs input:")


def _is_user_input_prompt(text: str) -> bool:
    """Return True for OpenClaw/Codex blocking user-input prompts."""
    normalized = (text or "").lstrip()
    return any(normalized.startswith(p) for p in _USER_INPUT_TRIGGER_PREFIXES)


def _could_be_user_input_prefix(normalized_text: str) -> bool:
    """Return True while `normalized_text` (already left-stripped) is still
    ambiguous: either a strict prefix of one of the trigger phrases (so more
    streamed text could still complete it), or already a full match.

    Used to decide whether to keep withholding assistant-delta text instead
    of yielding it immediately — real token-by-token streaming (e.g. Claude)
    delivers the trigger phrase a few characters at a time, so checking each
    raw delta in isolation (as `_is_user_input_prompt` does) never matches.
    Only once the buffered text diverges from every trigger prefix do we know
    for sure this message isn't a needs-input prompt.
    """
    if not normalized_text:
        return True
    return any(
        normalized_text.startswith(p) or p.startswith(normalized_text)
        for p in _USER_INPUT_TRIGGER_PREFIXES
    )


def _advance_input_prompt_buffer(pending: str, delta: str) -> tuple[str, str]:
    """Feed a new assistant-delta chunk into the needs-input buffering state.

    Returns `(text_to_yield_now, new_pending)`. `pending` is text already
    withheld because it might still become a needs-input trigger.

    A trigger is only meaningful as the start of a line, so once the
    combined text (`pending + delta`) diverges from every trigger prefix,
    only the text after the LAST newline is worth re-examining as a fresh
    candidate — a real streaming delta doesn't necessarily break exactly at
    a line boundary (e.g. a single chunk can contain the tail of one
    paragraph, the blank-line separator, *and* the start of the next one),
    so checking `delta.endswith("\\n")` at the yield site isn't enough on
    its own (P22 follow-up, 2026-07-08 — caught live: a reply that talked
    normally first and only asked its question in the next paragraph never
    got buffered, because the newline landed mid-delta, not at its edge).
    """
    candidate = pending + delta
    if _could_be_user_input_prefix(candidate.lstrip()):
        return "", candidate
    idx = candidate.rfind("\n")
    if idx == -1:
        return candidate, ""
    before, after = candidate[: idx + 1], candidate[idx + 1 :]
    if after and _could_be_user_input_prefix(after.lstrip()):
        return before, after
    return before + after, ""


def _modal_payload_from_user_input_prompt(prompt_text: str) -> tuple[dict, bool]:
    """Build an OWUI modal payload from OpenClaw's "needs input:" prompt text.

    Returns (payload_dict, is_confirmation) where is_confirmation is True
    if a yes/no confirmation modal was chosen instead of a free-text input.
    """
    lines = [line.strip() for line in (prompt_text or "").splitlines()]
    lines = [line for line in lines if line]
    if lines and lines[0].endswith("needs input:"):
        lines = lines[1:]

    title = "OpenClaw needs input"
    if lines and len(lines[0]) <= 80 and not re.match(r"^\d+\.", lines[0]):
        title = lines[0]
        lines = lines[1:]

    message = "\n".join(lines).strip() or "Please answer so the run can continue."

    text_lower = (prompt_text or "").lower()

    # Detect password / secret input. Kept specific on purpose: a bare "key"
    # substring matches innocent words ("monkey", "which key order?"), so we
    # only trigger on unambiguous secret markers.
    is_secret = any(
        marker in text_lower
        for marker in ("secret", "password", "may show your reply", "api key", "token")
    )

    # Detect confirmation (yes/no) questions — conservatively. Misclassifying a
    # free-text choice as a binary yes/no silently strips the real answer, so we
    # only pick confirmation when the prompt clearly reads as binary:
    #   * an explicit yes/no marker is present, OR
    #   * a strong confirm verb appears AND the prompt ends with "?".
    # An enumerated option list ("1. ... 2. ...") is a CHOICE, never yes/no.
    has_options = bool(re.search(r"(?m)^\s*\d+[.)]\s", prompt_text or ""))
    yn_markers = ("(y/n)", "[y/n]", "y/n?", "yes/no", "(yes/no)")
    confirm_verbs = (
        "confirm", "proceed", "overwrite", "are you sure", "do you want",
        "delete", "remove", "בטוח", "האם", "هل تريد",
    )
    has_yn_marker = any(m in text_lower for m in yn_markers)
    has_confirm_verb = any(w in text_lower for w in confirm_verbs)
    ends_question = message.rstrip().endswith("?") or title.rstrip().endswith("?")
    is_confirmation = (
        not is_secret
        and not has_options
        and (has_yn_marker or (has_confirm_verb and ends_question))
    )

    if is_confirmation:
        # Use yes/no confirmation dialog for quick binary choices
        data = {
            "title": title,
            "message": message,
        }
        return {"type": "confirmation", "data": data}, True

    # Default: free-text input
    data = {
        "title": title,
        "message": message,
        "placeholder": "Reply with a number or your answer",
    }
    if is_secret:
        data["type"] = "password"
    return {"type": "input", "data": data}, False


def _ask_user_detail_block(prompt_text: str, answer: str) -> str:
    """Render an answered ask-user prompt as a native OWUI tool-call block.

    OWUI's frontend only special-cases a handful of `<details type="...">`
    values with a nice icon + collapsible UI (confirmed by grepping the
    compiled frontend bundle): "tool_calls", "reasoning", "code_interpreter".
    There's no dedicated type for Q&A, so we reuse "tool_calls" (labelled as
    an "Ask User" call) to get the same familiar rendering Eliav already
    likes for real tool calls, inserted inline at the point the question
    was asked/answered — instead of showing nothing (previous behavior:
    the raw prompt text was fully suppressed once answered).
    """
    payload, _ = _modal_payload_from_user_input_prompt(prompt_text)
    data = payload.get("data", {})
    title = data.get("title", "Question")
    message = data.get("message", "")
    question_display = f"{title}\n{message}".strip() if message else title
    args_str = json.dumps({"question": question_display})
    call_id = f"ask-user-{uuid.uuid4().hex[:12]}"
    return (
        '\n<details type="tool_calls" done="true" '
        f'id="{html.escape(call_id)}" '
        'name="Ask User" '
        f'arguments="{html.escape(args_str[:3000])}" '
        f'result="{html.escape(answer[:8000])}" '
        'meta="" files="[]" embeds="[]">'
        '\n<summary>❓ Ask User</summary>\n</details>\n'
    )


@dataclass
class UserInputResult:
    """Outcome of trying to answer a needs-input prompt via an OWUI modal.

    handled=False  -> not a prompt, user cancelled, or delivery failed;
                      the caller should show the text as a fallback.
    handled=True, new_run_id=None -> answer delivered, the run resumed in
                      place (steer); keep consuming the current run.
    handled=True, new_run_id="..." -> answer spawned a new run; the caller
                      should switch its consumer to that run.
    """

    handled: bool
    new_run_id: str | None = None
    prompt_text: str | None = None
    answer: str | None = None


def _normalize_event_call_response(response) -> str:
    """Extract text from common OWUI __event_call__ return shapes.
    Returns empty string for errors, None, or unrecognized shapes.
    """
    if response is None:
        return ""
    # Confirmation modals resolve to a bare boolean: True=confirm, False=cancel.
    # (bool is a subclass of int, so this must be handled before dict/str.)
    if isinstance(response, bool):
        return "yes" if response else "no"
    if isinstance(response, str):
        return response.strip()
    if isinstance(response, dict):
        # Error responses
        if "error" in response:
            return ""
        for key in ("value", "text", "content", "message", "response"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # Confirm type with options
        value = response.get("option")
        if isinstance(value, str) and value.strip():
            return value.strip()
        if response.get("confirmed") is True:
            return "yes"
        if response.get("confirmed") is False:
            return "no"
    return ""


def _live_session_id_for_user(user_id: str, session_pool) -> str | None:
    """Find a currently-connected OWUI session_id belonging to user_id.

    session_pool is OWUI's own SESSION_POOL dict (sid -> user dict with an
    'id' field), imported live from the running process — see
    _retry_modal_on_reconnect for why this only works inside OWUI itself.
    """
    for sid, session in list(session_pool.items()):
        if session and session.get("id") == user_id:
            return sid
    return None


async def _retry_modal_on_reconnect(
    owui_user_id: str,
    owui_chat_id: str | None,
    owui_message_id: str | None,
    payload: dict,
    *,
    max_wait_s: float,
    poll_interval_s: float,
) -> str | None:
    """Wait for owui_user_id to reconnect, then re-fire the modal directly.

    OWUI's own __event_call__ closure is bound to the session_id that was
    live when the original request started; once that session disconnects
    there is no way to retarget it. But OWUI imposes no execution timeout on
    a running pipe (confirmed in docs.openwebui.com's Events page,
    "Persistence & Browser Disconnection" section: the background task
    keeps running after tab close, only killed by returning/raising, manual
    /api/tasks/stop, or a server restart) — and our pipe module runs inside
    the very same process as the OWUI backend, so we can import its live
    `sio` AsyncServer and SESSION_POOL dict directly and poll for a new
    session_id to appear for this user, then call sio.call() against it
    ourselves, bypassing the stale closure entirely.
    """
    try:
        from open_webui.socket.main import sio, SESSION_POOL
    except Exception as ex:
        pipe_log(f"  reconnect retry unavailable (not running inside OWUI process?): {ex}")
        return None

    deadline = time.monotonic() + max_wait_s
    seen_sids: set[str] = set()
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval_s)
        sid = _live_session_id_for_user(owui_user_id, SESSION_POOL)
        if not sid or sid in seen_sids:
            continue
        seen_sids.add(sid)
        pipe_log(f"  {owui_user_id[:8]}... reconnected (sid {sid[:8]}...); retrying modal")
        try:
            response = await asyncio.wait_for(
                sio.call(
                    "events",
                    {
                        "chat_id": owui_chat_id,
                        "message_id": owui_message_id,
                        "data": payload,
                    },
                    to=sid,
                    timeout=30,
                ),
                timeout=35,
            )
        except Exception as ex:
            pipe_log(f"  retry event_call failed for sid {sid[:8]}...: {ex}")
            continue
        answer = _normalize_event_call_response(response)
        if answer:
            return answer
        # Reconnected but cancelled/empty this time; keep watching in case
        # they reconnect again (e.g. an accidental tab close).
    pipe_log(f"  gave up waiting for {owui_user_id[:8]}... to reconnect after {int(max_wait_s)}s")
    return None


async def _ask_user_input_modal(
    __event_call__,
    prompt_text: str,
    *,
    timeout_s: float = 60,
    owui_user_id: str | None = None,
    owui_chat_id: str | None = None,
    owui_message_id: str | None = None,
    max_wait_s: float = 3600,
    poll_interval_s: float = 5,
    __event_emitter__=None,
) -> str | None:
    """Ask the user through OWUI's modal input API when available.

    If the live call fails (user not connected right now) and owui_user_id
    is given, keep the pipe's own background task alive and poll for the
    user to reconnect (up to max_wait_s total), retrying the modal against
    their fresh session — see _retry_modal_on_reconnect. Without
    owui_user_id, behaves exactly as before: a single attempt, exceptions
    (including TimeoutError) propagate to the caller.
    """
    if not __event_call__ or not _is_user_input_prompt(prompt_text):
        return None
    payload, _ = _modal_payload_from_user_input_prompt(prompt_text)

    try:
        response = await asyncio.wait_for(
            __event_call__(payload),
            timeout=timeout_s,
        )
        answer = _normalize_event_call_response(response)
        if answer:
            return answer
    except asyncio.TimeoutError:
        if not owui_user_id:
            raise

    if not owui_user_id:
        return None

    pipe_log(
        f"  modal not answered live; polling for {owui_user_id[:8]}... "
        f"to reconnect (up to {int(max_wait_s)}s)"
    )
    await _emit_status(
        __event_emitter__,
        "Waiting for your reply — question is pending, reconnect anytime",
        done=False,
    )
    return await _retry_modal_on_reconnect(
        owui_user_id,
        owui_chat_id,
        owui_message_id,
        payload,
        max_wait_s=max_wait_s,
        poll_interval_s=poll_interval_s,
    )
