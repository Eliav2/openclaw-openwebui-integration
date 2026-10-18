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
    of yielding it immediately -- real token-by-token streaming (e.g. Claude)
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


# CommonMark 4.5: a fence is 3+ backticks or tildes indented up to 3 spaces;
# a backtick fence's info string has no backtick; only a run of the same
# character, at least as long and followed by nothing but spaces, closes it.
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")


def _fence_of(state) -> str:
    """A fence state as the open fence's run ("" when none). `True` is an
    open backtick fence of unknown length (older callers passed a bool)."""
    if state is True:
        return "```"
    return state or ""


def _fence_step(line: str, fence: str) -> str:
    """The fence state after `line` when `fence` was open before it."""
    if fence:
        m = _FENCE_CLOSE_RE.match(line)
        if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
            return ""
        return fence
    m = _FENCE_OPEN_RE.match(line)
    if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
        return m.group(1)
    return ""


def _fence_open_after(text: str, in_fence=False) -> str:
    """The fenced code block open at the end of `text` (its fence run, "" when
    none), when `in_fence` was open where `text` starts. Truthy when open."""
    fence = _fence_of(in_fence)
    for line in (text or "").split("\n"):
        fence = _fence_step(line, fence)
    return fence


def _marker_line_index(text: str, in_fence=False):
    """Index of the first line that opens a needs-input block, else None.

    The marker only counts at the start of a line, but it does NOT have to be
    the start of the message: a reply usually explains itself first and asks at
    the end. Every line is checked, not only the text after the last newline,
    because a provider can deliver the whole reply as one cumulative chunk with
    the marker mid-string.

    A line inside a fenced code block is a quote, not a prompt (ELI-107).
    `in_fence`: a fence is open where `text` starts.
    """
    start, fence = 0, _fence_of(in_fence)
    while True:
        nl = text.find("\n", start)
        line = text[start:] if nl == -1 else text[start:nl]
        before, fence = fence, _fence_step(line, fence)
        if not before and not fence and _is_user_input_prompt(line):
            return start
        if nl == -1:
            return None
        start = nl + 1


def _advance_input_prompt_buffer(pending: str, delta: str,
                                 in_fence=False, at_line_start=True) -> tuple[str, str]:
    """Feed a new assistant-delta chunk into the needs-input buffering state.

    Returns `(text_to_yield_now, new_pending)`. `pending` is text already
    withheld because it might still become a needs-input trigger.

    A trigger is only meaningful as the start of a line, so once the
    combined text (`pending + delta`) diverges from every trigger prefix,
    only the text after the LAST newline is worth re-examining as a fresh
    candidate -- a real streaming delta doesn't necessarily break exactly at
    a line boundary (e.g. a single chunk can contain the tail of one
    paragraph, the blank-line separator, *and* the start of the next one),
    so checking `delta.endswith("\\n")` at the yield site isn't enough on
    its own.

    `in_fence`: a fenced code block is open where `pending + delta` starts
    (the shown text left it open); nothing in a fence is held (ELI-107).
    `at_line_start`: the shown text ends at the start of a line. When it
    does not, the first line of `delta` continues a line and opens no
    prompt, wherever the deltas happen to split (ELI-147).
    """
    candidate = pending + delta
    if not (pending or at_line_start):
        nl = candidate.find("\n")
        if nl == -1:
            return candidate, ""
        head = candidate[:nl + 1]
        flush, held = _advance_input_prompt_buffer("", candidate[nl + 1:],
                                                   _fence_open_after(head, in_fence))
        return head + flush, held
    if not in_fence and _could_be_user_input_prefix(candidate.lstrip()):
        return "", candidate
    # A complete marker may sit mid-candidate when the whole reply arrives at
    # once. Flush what precedes it and hold from the marker onward.
    marker_at = _marker_line_index(candidate, in_fence)
    if marker_at is not None:
        return candidate[:marker_at], candidate[marker_at:]
    idx = candidate.rfind("\n")
    if idx == -1:
        return candidate, ""
    before, after = candidate[: idx + 1], candidate[idx + 1 :]
    if after and not _fence_open_after(before, in_fence) and _could_be_user_input_prefix(after.lstrip()):
        return before, after
    return before + after, ""


def _strip_input_marker_line(text: str) -> str:
    """Remove a leading needs-input marker ("OpenClaw needs input:" /
    "Codex needs input:") and keep the prompt after it. The one place a
    prompt is split from its marker: the card, the modal and the text
    fallback all read the prompt through it.

    A needs-input prompt is normally intercepted into a modal and never
    rendered raw. But the fallback paths -- a cancelled/dismissed modal, a run
    that ended before the modal could fire -- can still reach a raw ``yield``
    with the marker intact, leaking the literal directive line into the chat.
    The marker is only ever meaningful at the start, so drop exactly the
    marker and keep the rest (the actual question), including a question
    written on the marker line itself (ELI-151).
    """
    if not text:
        return text
    stripped = text.lstrip()
    lead_ws = text[: len(text) - len(stripped)]
    for prefix in _USER_INPUT_TRIGGER_PREFIXES:
        if stripped.startswith(prefix):
            rest = stripped[len(prefix):].lstrip(" \t\r")
            if rest.startswith("\n"):
                rest = rest.lstrip("\n")
            return lead_ws + rest
    return text


def _strip_needs_input_lines(text: str) -> str:
    """Drop every needs-input directive (and the blank lines after a lone
    one) that is outside a fenced code block: the question it opens stays as
    readable text, a question on the marker line included (ELI-151), and a
    quoted block in a fence stays as written (ELI-107)."""
    if not text:
        return text
    out, fence, skip_blank = [], "", False
    for line in text.split("\n"):
        before, fence = fence, _fence_step(line, fence)
        if not before and not fence and _is_user_input_prompt(line):
            line = _strip_input_marker_line(line)
            skip_blank = not line.strip()
            if skip_blank:
                continue
        if skip_blank and not line.strip():
            continue
        skip_blank = False
        out.append(line)
    return "\n".join(out)


def _truncate_at_repeated_marker(text: str) -> str:
    """Keep only the FIRST needs-input block.

    An agent that emits the marker block twice (seen live: the second copy glued
    straight onto the last option, "3. Park itOpenClaw needs input: ...") turned
    a 3-option question into a 6-button dialog, one of whose labels was the raw
    internal directive. The agent misbehaved, but the pipe owns what the user
    sees, so it should not render that. Everything from a second marker onward
    is a repeat, not part of the prompt.
    """
    if not text:
        return text
    first_end = -1
    for prefix in _USER_INPUT_TRIGGER_PREFIXES:
        i = text.find(prefix)
        if i != -1 and (first_end == -1 or i + len(prefix) < first_end):
            first_end = i + len(prefix)
    if first_end == -1:
        return text
    nxt = -1
    for prefix in _USER_INPUT_TRIGGER_PREFIXES:
        i = text.find(prefix, first_end)
        if i != -1 and (nxt == -1 or i < nxt):
            nxt = i
    return text[:nxt].rstrip() if nxt != -1 else text


def _dedupe_options(options):
    """Drop repeated option labels, keeping first-seen order.

    Belt and braces behind _truncate_at_repeated_marker: a dialog that offers
    the same answer twice is always wrong, whatever produced it.
    """
    seen, out = set(), []
    for opt in options:
        key = opt.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(opt)
    return out


def _extract_numbered_options(prompt_text: str) -> list[tuple[str, str]]:
    """Return [(index_str, label), ...] for a numbered option list, in prompt
    order, or [] if there are none. Matches lines like "1. foo" / "2) bar"."""
    pattern = re.compile(r"(?m)^\s*(\d+)[.)]\s+(.+)$")
    return [(m.group(1), m.group(2).strip()) for m in pattern.finditer(prompt_text or "")]


# What the Ask User block shows for a secret's answer.
SECRET_ANSWER_SHOWN = "•••• (secret entered)"
# What the agent gets once when the secret's dialog got no answer: the reply
# tells the user not to type it into the chat, so no answer will come there.
SECRET_NOT_RECEIVED_FOR_AGENT = (
    "[Open WebUI] No answer: the masked dialog for the secret you asked for was not "
    "answered, so nothing was received. Do not ask for it in the chat, which stores "
    "what the user types. Use masked credential entry, or ask again when the user "
    "has this chat open."
)
# What the agent gets when the user cancels the secret's dialog (ELI-189),
# and what the Ask User block shows for it.
SECRET_CANCELLED_FOR_AGENT = (
    "[Open WebUI] Cancelled: the user closed the masked dialog for the secret you "
    "asked for without entering it, so nothing was received. Do not ask for it in "
    "the chat, which stores what the user types."
)
SECRET_CANCELLED_SHOWN = "Cancelled (nothing entered)"

_MULTI_MARKERS = (
    "(multiselect)", "(multi-select)", "(multi)", "[multiselect]", "[multi]",
    "select all that apply", "choose all that apply", "check all that apply",
    "בחר כמה", "בחירה מרובה", "אפשר לבחור כמה", "בחר את כל",
)


def _parse_text_prompt(prompt_text: str) -> dict:
    """The fields of a text needs-input prompt: `title`, `message`, `secret`,
    `confirm` (a yes/no question), `options` (the numbered list) and `multi`
    (a multi-select marker, stripped from the title and message)."""
    prompt_text = _truncate_at_repeated_marker(prompt_text or "")
    # The marker goes as in the text fallback: a question on its line stays (ELI-151).
    lines = [line.strip() for line in _strip_input_marker_line(prompt_text).splitlines()]
    lines = [line for line in lines if line]

    title = "OpenClaw needs input"
    if lines and len(lines[0]) <= 80 and not re.match(r"^\d+\.", lines[0]):
        title = lines[0]
        lines = lines[1:]
    message = "\n".join(lines).strip() or "Please answer so the run can continue."
    text_lower = prompt_text.lower()

    multi = any(m in text_lower for m in _MULTI_MARKERS)
    if multi:
        for mk in _MULTI_MARKERS:
            title = re.sub(re.escape(mk), "", title, flags=re.IGNORECASE).strip()
            message = re.sub(re.escape(mk), "", message, flags=re.IGNORECASE).strip()
        title = title or "OpenClaw needs input"
        message = message or "Please answer so the run can continue."

    # Kept specific on purpose: a bare "key" matches innocent words ("monkey").
    secret = any(
        marker in text_lower
        for marker in ("secret", "password", "may show your reply", "api key", "token")
    )

    # Yes/no only when the prompt clearly reads as binary: a free-text question
    # turned into yes/no loses the real answer. A numbered list is a choice.
    has_options = bool(re.search(r"(?m)^\s*\d+[.)]\s", prompt_text))
    yn_markers = ("(y/n)", "[y/n]", "y/n?", "yes/no", "(yes/no)")
    confirm_verbs = (
        "confirm", "proceed", "overwrite", "are you sure", "do you want",
        "delete", "remove", "בטוח", "האם", "هل تريد",
    )
    ends_question = message.rstrip().endswith("?") or title.rstrip().endswith("?")
    confirm = not secret and not has_options and (
        any(m in text_lower for m in yn_markers)
        or (any(w in text_lower for w in confirm_verbs) and ends_question)
    )
    options = [] if secret or confirm else _dedupe_options(
        [label for _, label in _extract_numbered_options(prompt_text)])
    return {
        "title": title,
        "message": message,
        "secret": secret,
        "confirm": confirm,
        "options": options if len(options) >= 2 else [],
        "multi": multi,
    }


def _is_secret_prompt(prompt_text: str) -> bool:
    return not _parse_ask_user_json(prompt_text) and _parse_text_prompt(prompt_text)["secret"]


def _secret_not_received_text(prompt_text: str) -> str:
    """A secret nobody entered in its dialog, as the text that ends the
    reply. Never the agent's own question: that asks for the value, and an
    answer typed into the chat is stored in it."""
    title = _parse_text_prompt(prompt_text)["title"]
    return (f"**{title}**: not received. A secret is entered only in its masked dialog, "
            "never in the chat, which keeps what you type. To enter it, open this chat "
            "in a tab and ask the agent to try again: the dialog shows again.")


def _masked_input_payload(title: str, message: str, placeholder: str = "Type your answer") -> dict:
    """OWUI's input dialog with a masked field (`<input type="password">`)."""
    return {"type": "input", "data": {
        "title": title,
        "message": message,
        "placeholder": placeholder,
        "type": "password",
    }}


def _secret_input_payload(prompt_text: str) -> dict:
    """OWUI's masked input dialog for a prompt asking for a secret: the card
    has no masked field."""
    fields = _parse_text_prompt(prompt_text)
    return _masked_input_payload(fields["title"], fields["message"])


_OPTION_DESCRIPTION_SEPARATORS = (" - ", " – ", " — ")

# OWUI clamps a request:user_input event call to 60..240s and, when it times
# out, removes the card itself. We ask for the maximum; a card not answered
# by then ends the reply with the question as text (ELI-142).
_NATIVE_CARD_TIMEOUT_MS = 240_000


def _split_option_description(text: str) -> tuple[str, str]:
    """"Postgres - robust, needs a server" -> ("Postgres", "robust, needs a server")."""
    for sep in _OPTION_DESCRIPTION_SEPARATORS:
        label, found, description = text.partition(sep)
        if found and label.strip() and description.strip():
            return label.strip(), description.strip()
    return text.strip(), ""


def _parse_ask_user_json(prompt_text: str) -> list[dict] | None:
    """Questions from a JSON needs-input body, or None for the text form.

    The body after the marker line may be a JSON object (optionally in a code
    fence) shaped like OWUI's ask_user arguments:
    {"questions": [{"header": ..., "question": ..., "options": [...]}]}.
    Options are strings or {"label", "description"} objects.
    """
    body = _strip_input_marker_line(_truncate_at_repeated_marker(prompt_text or "")).strip()
    fence = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", body, re.DOTALL)
    if fence:
        body = fence.group(1).strip()
    if not body.startswith("{"):
        return None
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    raw_questions = parsed.get("questions") if isinstance(parsed, dict) else None
    if not isinstance(raw_questions, list) or not raw_questions:
        return None

    allow_other = parsed.get("allow_other", True) is not False
    questions, seen_ids = [], set()
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("question") or "").strip()
        if not text:
            continue
        qid = str(raw.get("id") or "").strip()[:64] or f"q{index + 1}"
        while qid in seen_ids:
            qid = f"{qid}_{index + 1}"
        seen_ids.add(qid)
        options, seen_labels = [], set()
        for opt in raw.get("options") or []:
            if isinstance(opt, dict):
                label = str(opt.get("label") or "").strip()
                description = str(opt.get("description") or "").strip()
            else:
                label, description = _split_option_description(str(opt))
            if label and label.lower() not in seen_labels:
                seen_labels.add(label.lower())
                options.append({"label": label[:80], "description": description[:240]})
        questions.append({
            "id": qid,
            "header": str(raw.get("header") or "").strip()[:48] or f"Question {index + 1}",
            "question": text[:500],
            "options": options,
            # A question with no options is free text, so it must accept typing.
            "allow_other": not options or raw.get("allow_other", allow_other) is not False,
        })
    return questions or None


def _ask_user_fallback_text(prompt_text: str) -> str:
    """A prompt nobody answered in a card, as the text that ends the reply.

    The JSON form reads as its questions, numbered when there are several,
    each with its options listed under it, then one line on how to answer
    (ELI-142: it showed as the raw JSON). Any other prompt is its own text
    without the marker line."""
    questions = _parse_ask_user_json(prompt_text)
    if not questions and _is_secret_prompt(prompt_text):
        return _secret_not_received_text(prompt_text)
    if not questions:
        return _strip_input_marker_line(prompt_text)
    numbered = len(questions) > 1
    lines = []
    for n, q in enumerate(questions, 1):
        auto_header = re.fullmatch(r"Question \d+", q["header"])
        line = q["question"] if auto_header else f"**{q['header']}**: {q['question']}"
        lines.append(f"{n}. {line}" if numbered else line)
        for opt in q["options"]:
            label = f"{opt['label']} - {opt['description']}" if opt["description"] else opt["label"]
            lines.append(("   - " if numbered else "- ") + label)
    return "\n".join(lines) + "\n\n_Answer in your next message._"


# The card picks one option per question, so a multi-select question lists
# its options by number in the question text and takes typed numbers. The
# labels ride along under this key for mapping the answer back.
_MULTI_CHOICES_KEY = "openclaw_choices"


def _native_ask_user_payload(prompt_text: str) -> dict | None:
    """Build OWUI's native `request:user_input` question card for a prompt.

    Returns None for a prompt asking for a secret: the card has no masked
    input (see _secret_input_payload).
    """
    questions = _parse_ask_user_json(prompt_text)
    if questions is None:
        fields = _parse_text_prompt(prompt_text)
        if fields["secret"]:
            return None
        question = {
            "id": "answer",
            "header": fields["title"],
            "question": fields["message"],
            "options": [],
            "allow_other": True,
        }
        # The numbered list moves out of the text: into `options`, or into
        # one line of numbered choices for a multi-select question.
        message = re.sub(r"(?m)^\s*\d+[.)]\s+.*$\n?", "", fields["message"]).strip()
        if fields["confirm"]:
            question["options"] = [
                {"label": "Yes", "description": ""},
                {"label": "No", "description": ""},
            ]
            question["allow_other"] = False
        elif fields["options"] and fields["multi"]:
            # The card shows the question as one line of plain text.
            listed = " · ".join(f"{n}. {label}" for n, label in enumerate(fields["options"], 1))
            question["question"] = (f"{message or fields['title']} {listed}. "
                                    "Type the numbers you pick, like 1, 3")
            question[_MULTI_CHOICES_KEY] = list(fields["options"])
        elif fields["options"]:
            for label in fields["options"]:
                label, description = _split_option_description(label)
                question["options"].append({"label": label, "description": description})
            question["question"] = message or fields["title"]
        questions = [question]
    return {
        "type": "request:user_input",
        "data": {
            "questions": questions,
            "allow_other": True,
            "timeout_ms": _NATIVE_CARD_TIMEOUT_MS,
        },
    }


def _map_multi_answer(text: str, choices: list[str]) -> str:
    """A typed multi-select answer as the chosen options, comma-joined.

    "1,3", "1 3", "Cheese, Mushrooms" and "1, Mushrooms" all name options;
    an option's short label (before " - ") names it too. Anything that does
    not name options only (a number out of range, other text) is the
    answer as typed.
    """
    text = (text or "").strip()
    by_name = {}
    for choice in choices:
        by_name.setdefault(choice.lower(), choice)
        by_name.setdefault(_split_option_description(choice)[0].lower(), choice)
    picked = []
    for part in re.split(r"[,;،]", text):
        part = part.strip()
        if not part:
            continue
        if part.lower() in by_name:
            names = [by_name[part.lower()]]
        elif re.fullmatch(r"\d+(\s+\d+)*", part):
            numbers = [int(n) for n in part.split()]
            if not all(1 <= n <= len(choices) for n in numbers):
                return text
            names = [choices[n - 1] for n in numbers]
        else:
            return text
        picked += [name for name in names if name not in picked]
    return ", ".join(picked) if picked else text


def _format_native_answers(questions: list[dict], answers: dict) -> str:
    """Turn the card's {question_id: answer} into the text sent to the agent.

    One question returns the bare answer (the label, or what was typed); a
    Yes/No confirmation returns "yes"/"no"; a multi-select question returns
    the chosen options, comma-joined. Several questions return one
    "Header: answer" line each.
    """
    lines = []
    for question in questions:
        answer = answers.get(question["id"]) if isinstance(answers, dict) else None
        if not isinstance(answer, dict):
            continue
        if answer.get("type") == "other":
            text = str(answer.get("text") or "").strip()
        else:
            text = str(answer.get("label") or "").strip()
        labels = [o["label"] for o in question.get("options", [])]
        if labels == ["Yes", "No"] and text in labels:
            text = text.lower()
        if question.get(_MULTI_CHOICES_KEY):
            text = _map_multi_answer(text, question[_MULTI_CHOICES_KEY])
        if text:
            lines.append((question.get("header") or question["id"], text))
    if not lines:
        return ""
    if len(questions) == 1:
        return lines[0][1]
    return "\n".join(f"{header}: {text}" for header, text in lines)


async def _call_on_chat_tab(
    owui_user_id: str, owui_chat_id: str | None, owui_message_id: str | None, payload: dict,
    timeout_s: float,
):
    """Show `payload` on the user's socket viewing the chat, or return None
    when there is none. A question card goes as OWUI's own get_event_call
    sends it: a fresh interaction_id, and the closing request:interaction:done
    event that removes the card from the screen."""
    try:
        from open_webui.socket.main import sio, SESSION_POOL
    except Exception as ex:
        pipe_log(f"  question on the chat's tab unavailable (not running inside OWUI process?): {ex}")
        return None
    sid = _live_session_id_for_user(owui_user_id, SESSION_POOL, owui_chat_id)
    if not sid:
        return None
    pipe_log(f"  question: showing it on session {sid[:8]}...")
    card = payload.get("type") == "request:user_input"
    interaction_id = str(uuid.uuid4())
    if card:
        payload = {**payload, "data": {**payload["data"], "interaction_id": interaction_id}}
    try:
        async with _holding_executes(sid, owui_user_id, payload.get("type")):
            return await sio.call(
                "events",
                {"chat_id": owui_chat_id, "message_id": owui_message_id, "data": payload},
                to=sid,
                timeout=timeout_s,
            )
    except Exception as ex:
        return {"error": f"Event call timed out: {ex!r}"}
    finally:
        if card:
            try:
                await sio.emit(
                    "events",
                    {
                        "chat_id": owui_chat_id,
                        "message_id": owui_message_id,
                        "data": {
                            "type": "request:interaction:done",
                            "data": {"interaction_id": interaction_id},
                        },
                    },
                    to=sid,
                )
            except Exception:
                pass


async def _ask_where_the_chat_is(
    __event_call__,
    payload: dict,
    timeout_s: float,
    *,
    grace_s: float = 0,
    owui_user_id: str | None,
    owui_chat_id: str | None,
    owui_message_id: str | None,
    poll_interval_s: float,
    owui_session_id: str | None,
):
    """Ask once on the socket the turn started on (`owui_session_id`). Once
    it is gone (a reload; no socket for an API caller) the question moves
    once to the tab viewing the chat. Never re-shown after a timeout: that
    keeps a reply open with nothing on screen (ELI-142)."""
    response = await _event_call_while_session_lives(
        __event_call__, payload, owui_session_id, timeout_s + grace_s, poll_interval_s
    )
    if owui_user_id and isinstance(response, dict) and "disconnected" in str(response.get("error", "")):
        response = await _call_on_chat_tab(owui_user_id, owui_chat_id, owui_message_id, payload, timeout_s)
    return response


def _unanswered(response) -> str:
    if response is None:
        return "no tab views the chat"
    if isinstance(response, dict):
        return str(response.get("error") or response.get("status"))[:80]
    return repr(response)[:80]


async def _ask_user_native_card(
    __event_call__,
    payload: dict,
    *,
    owui_user_id: str | None,
    owui_chat_id: str | None,
    owui_message_id: str | None,
    poll_interval_s: float,
    owui_session_id: str | None = None,
) -> str | None:
    """Ask once through OWUI's native question card.

    Only an answer or a cancel shows the card was seen. Anything else (OWUI's
    timeout, a cancel the moment it arrives on a tab viewing another chat, a
    socket that never answers) returns None, as a cancel does: the reply then
    ends with the question as text (ELI-107).
    """
    response = await _ask_where_the_chat_is(
        __event_call__, payload, payload["data"]["timeout_ms"] / 1000, grace_s=15,
        owui_user_id=owui_user_id, owui_chat_id=owui_chat_id, owui_message_id=owui_message_id,
        poll_interval_s=poll_interval_s, owui_session_id=owui_session_id,
    )
    if isinstance(response, dict) and response.get("status") == "answered":
        return _format_native_answers(payload["data"]["questions"], response.get("answers") or {}) or None
    pipe_log(f"  question card not answered ({_unanswered(response)}); the question ends the reply as text")
    return None


async def _ask_user_secret(
    __event_call__,
    prompt_text: str,
    *,
    timeout_s: float,
    owui_user_id: str | None,
    owui_chat_id: str | None,
    owui_message_id: str | None,
    poll_interval_s: float,
    owui_session_id: str | None = None,
) -> str | None:
    """Ask for a secret through OWUI's masked input dialog, as the card is
    asked. OWUI puts no timeout on this dialog, so `timeout_s` ends the wait;
    unanswered, the question ends the reply as text."""
    try:
        response = await _ask_where_the_chat_is(
            __event_call__, _secret_input_payload(prompt_text), timeout_s,
            owui_user_id=owui_user_id, owui_chat_id=owui_chat_id, owui_message_id=owui_message_id,
            poll_interval_s=poll_interval_s, owui_session_id=owui_session_id,
        )
    except asyncio.TimeoutError:
        response = {"error": f"no answer in {timeout_s:g} s"}
    if response is False:
        # The dialog's Cancel (EventConfirmDialog resolves it with False),
        # not a "no": the agent hears that the user cancelled.
        pipe_log("  secret dialog cancelled by the user")
        return SECRET_CANCELLED_FOR_AGENT
    answer = _normalize_event_call_response(response)
    if not answer:
        pipe_log(f"  secret dialog not answered ({_unanswered(response)}); the question ends the reply as text")
    return answer or None


def _ask_user_detail_block(prompt_text: str, answer: str) -> str:
    """Render an answered ask-user prompt as a native OWUI tool-call block.

    OWUI's frontend only special-cases a handful of `<details type="...">`
    values with a nice icon + collapsible UI (confirmed by grepping the
    compiled frontend bundle): "tool_calls", "reasoning", "code_interpreter".
    There's no dedicated type for Q&A, so we reuse "tool_calls" (labelled as
    an "Ask User" call) to get the same familiar rendering users already
    know from real tool calls, inserted inline at the point the question
    was asked/answered -- instead of showing nothing (previous behavior:
    the raw prompt text was fully suppressed once answered).
    """
    json_questions = _parse_ask_user_json(prompt_text)
    if SECRET_NOT_RECEIVED_FOR_AGENT in answer:
        # The agent was told nothing came: the reply shows the question as
        # not received, as an unanswered secret ends any reply.
        return "\n" + _secret_not_received_text(prompt_text) + "\n"
    if SECRET_CANCELLED_FOR_AGENT in answer:
        answer = SECRET_CANCELLED_SHOWN
    elif _is_secret_prompt(prompt_text):
        # The block is written into the chat, so into Open WebUI's database.
        answer = SECRET_ANSWER_SHOWN
    if json_questions:
        question_display = "\n\n".join(
            f"{q['header']}\n{q['question']}" for q in json_questions
        )
    else:
        fields = _parse_text_prompt(prompt_text)
        question_display = f"{fields['title']}\n{fields['message']}"
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
    # The answer run's consumer queue, registered before its send.
    queue: object = None


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


def _live_session_id_for_user(user_id: str, session_pool, chat_id: str | None = None) -> str | None:
    """Find a currently-connected OWUI session_id belonging to user_id.

    session_pool is OWUI's own SESSION_POOL dict (sid -> user dict with an
    'id' field), imported live from the running process: the pipe runs
    inside the OWUI backend, so it can import its `sio` and SESSION_POOL.

    With `chat_id`, only a socket viewing that chat: OWUI 0.12 records the
    chats a socket has joined (`chat_ids`, events:chat join/leave). The
    user's first socket can be another tab or another client, where the card
    never shows (ELI-58). The newest such socket is the tab the
    user opened last; with none, None, so the card waits for the user to
    open the chat. An OWUI that records no `chat_ids` gets any socket.
    """
    mine = [(sid, s) for sid, s in list(session_pool.items()) if s and s.get("id") == user_id]
    if chat_id and any("chat_ids" in s for _sid, s in mine):
        viewing = [sid for sid, s in mine if chat_id in (s.get("chat_ids") or [])]
        return viewing[-1] if viewing else None
    return mine[0][0] if mine else None


async def _event_call_while_session_lives(__event_call__, payload: dict, session_id: str | None,
                                          timeout_s: float, poll_interval_s: float):
    """`__event_call__(payload)`, given up once the socket it is bound to
    leaves OWUI's SESSION_POOL (the tab was reloaded or closed): OWUI answers
    a call on a gone socket only at its own timeout, up to 240 s, and the
    card can be shown on the user's new socket only after that (ELI-58).
    Answers as OWUI does for a gone socket."""
    call = asyncio.ensure_future(asyncio.wait_for(__event_call__(payload), timeout=timeout_s))
    try:
        from open_webui.socket.main import SESSION_POOL
    except Exception:
        SESSION_POOL = None
    if not session_id or SESSION_POOL is None:
        return await call
    while True:
        done, _ = await asyncio.wait({call}, timeout=poll_interval_s)
        if done:
            return call.result()
        if session_id not in SESSION_POOL:
            call.cancel()
            pipe_log(f"  question card: session {session_id[:8]}... is gone; showing it on the user's tab")
            return {"error": "Client session disconnected."}


async def _ask_user_input_modal(
    __event_call__,
    prompt_text: str,
    *,
    timeout_s: float = _NATIVE_CARD_TIMEOUT_MS / 1000,
    owui_user_id: str | None = None,
    owui_chat_id: str | None = None,
    owui_message_id: str | None = None,
    poll_interval_s: float = 5,
    owui_session_id: str | None = None,
) -> str | None:
    """Ask the user a needs-input prompt in the chat's tab: OWUI's question
    card (see _ask_user_native_card), or for a secret its masked input
    dialog (_ask_user_secret). None when unanswered: the question then ends
    the reply as text."""
    if not __event_call__ or not _is_user_input_prompt(prompt_text):
        return None
    native_payload = _native_ask_user_payload(prompt_text)
    if native_payload is not None:
        return await _ask_user_native_card(
            __event_call__,
            native_payload,
            owui_user_id=owui_user_id,
            owui_chat_id=owui_chat_id,
            owui_message_id=owui_message_id,
            poll_interval_s=poll_interval_s,
            owui_session_id=owui_session_id,
        )
    return await _ask_user_secret(
        __event_call__,
        prompt_text,
        timeout_s=timeout_s,
        owui_user_id=owui_user_id,
        owui_chat_id=owui_chat_id,
        owui_message_id=owui_message_id,
        poll_interval_s=poll_interval_s,
        owui_session_id=owui_session_id,
    )


# ---------------------------------------------------------------------------
# Secret requests from the Gateway (ELI-96)
# ---------------------------------------------------------------------------
# The agent's `secrets` tool, action=request, is a Gateway question with one
# store-bound secret question: {questionId: "secret_value", isSecret: true,
# secretStore: {name, kind: "secret", allowedHosts?, reason?}}. The Gateway
# broadcasts it as `question.requested`. `question.resolve {id, answers:
# {answers: {secret_value: [value]}}}` from an operator makes the Gateway
# itself write the value into its secret store, register it for log
# redaction and answer the agent "stored"; `{id, cancel: true}` answers it
# "cancelled". `question.resolved` follows either way, and on expiry
# (2026.10.10-beta.1 dist: secrets-tool-f-CystZd.mjs:1253 request, :1298-1301
# the agent's statuses; question-CtUDaC2R.mjs:230 broadcast, :289-370 resolve).
#
# It is asked as the ask-user secret is (ELI-148): the masked input dialog on
# the reply's own socket, moved once to the tab viewing the chat. The typed
# value goes from the dialog's answer straight into question.resolve. It is
# never yielded, emitted, persisted or logged, so the chat, the database, the
# transcript and the model only ever see the request and "stored".
#
# A stored secret is usable by the agent's runs only from OpenClaw 2026.10
# (ELI-97), so an older Gateway's request is left to the Control UI, as before.

SECRET_REQUESTS_FROM = (2026, 10, 10)
# question id -> the task asking for it
_SECRET_REQUESTS: dict = {}
# session key -> the reply streaming its run, which asks on its own socket
_SECRET_ASKERS: dict = {}


def _gateway_release(version) -> tuple:
    """(year, month, patch) of a Gateway version like "2026.10.10-beta.1"."""
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", str(version or ""))
    return tuple(int(n) for n in m.groups()) if m else (0, 0, 0)


def _secret_request_question(record) -> dict | None:
    """The store-bound secret question of a `question.requested` record."""
    if not isinstance(record, dict):
        return None
    questions = record.get("questions") or []
    if len(questions) != 1 or not isinstance(questions[0], dict):
        return None
    question = questions[0]
    store = question.get("secretStore")
    if not question.get("isSecret") or not isinstance(store, dict) or not store.get("name") \
            or not question.get("questionId"):
        return None
    return question


def _secret_request_payload(question: dict) -> dict:
    """The masked dialog for a secret request. Built from the request's
    metadata only: there is no value to put in it."""
    store = question["secretStore"]
    lines = [str(store.get("reason") or "The agent asks for a secret.")]
    hosts = store.get("allowedHosts") or []
    lines.append("Allowed hosts: " + (", ".join(hosts) if hosts
                                       else "none (usable only as a config SecretRef)"))
    lines.append("It goes straight to OpenClaw's secret store, not into this chat.")
    return _masked_input_payload(f"Secret: {store['name']}", "\n\n".join(lines),
                                 placeholder=f"Paste {store['name']}")


def _secret_asker_join(turn) -> None:
    _SECRET_ASKERS[turn.session_key] = turn


def _secret_asker_leave(turn) -> None:
    if _SECRET_ASKERS.get(getattr(turn, "session_key", None)) is turn:
        del _SECRET_ASKERS[turn.session_key]


def secret_request_seen(conn, record) -> bool:
    """`question.requested`: ask a secret request made in an Open WebUI chat.
    False for anything else, or on a Gateway whose runs cannot use it."""
    question = _secret_request_question(record)
    if question is None or record.get("status", "pending") != "pending" or not record.get("id"):
        return False
    if _gateway_release(conn.gateway_version) < SECRET_REQUESTS_FROM:
        return False
    parsed = conn.parse_owui_session_key(record.get("sessionKey") or "")
    if not parsed or record["id"] in _SECRET_REQUESTS:
        return False
    _SECRET_REQUESTS[record["id"]] = spawn_background(
        _answer_secret_request(conn, record, question, *parsed))
    return True


def secret_request_resolved(payload) -> None:
    """`question.resolved`: answered elsewhere (the Control UI), cancelled or
    expired. The dialog's wait ends; whatever it gets later is dropped."""
    task = _SECRET_REQUESTS.pop((payload or {}).get("id"), None)
    if task is not None:
        task.cancel()


async def _answer_secret_request(conn, record: dict, question: dict, user_id: str, chat_id: str) -> None:
    qid, name = record["id"], question["secretStore"]["name"]
    timeout_s = max(1.0, (record.get("expiresAtMs") or 0) / 1000 - time.time())
    payload = _secret_request_payload(question)
    if not await _sender_is_owner(conn, record.get("sessionKey") or ""):
        # The store is the owner's: a guest's chat cannot write into it.
        _SECRET_REQUESTS.pop(qid, None)
        pipe_log(f"  secret request: {name} in a chat of a user not in OWNER_USERS; left to the Control UI")
        return
    turn = _SECRET_ASKERS.get(record.get("sessionKey"))
    pipe_log(f"  secret request: {name} in chat {chat_id[:8]}... (question {qid[:8]}...)")
    try:
        if turn is not None:
            ask = _ask_where_the_chat_is(
                turn.__event_call__, payload, timeout_s,
                owui_user_id=user_id, owui_chat_id=chat_id, owui_message_id=turn.owui_message_id,
                poll_interval_s=5, owui_session_id=(turn.__metadata__ or {}).get("session_id"))
        else:
            ask = _call_on_chat_tab(user_id, chat_id, None, payload, timeout_s)
        response = await asyncio.wait_for(ask, timeout_s)
    except asyncio.TimeoutError:
        response = None
    if _SECRET_REQUESTS.get(qid) is not asyncio.current_task():
        return
    del _SECRET_REQUESTS[qid]
    if response is None:
        # Expired with the dialog open, or no tab shows the chat: the Gateway
        # expires it and the agent hears "No credential arrived".
        pipe_log(f"  secret request: {name} not answered in Open WebUI")
        return
    # Only typed text is a value: a cancelled dialog answers False.
    value = response.strip() if isinstance(response, str) else ""
    params = ({"id": qid, "answers": {"answers": {question["questionId"]: [value]}},
               "resolvedBy": "openwebui"} if value
              else {"id": qid, "cancel": True, "resolvedBy": "openwebui"})
    value = response = None
    try:
        result = await conn.send_request("question.resolve", params, timeout=30)
        shown = "stored" if (result or {}).get("status") == "answered" else str((result or {}).get("status"))
    except Exception as ex:
        shown = f"not stored ({type(ex).__name__})"
    finally:
        params = None
    pipe_log(f"  secret request: {name} {shown}")
    if turn is not None and turn.__event_emitter__:
        try:
            await turn.__event_emitter__({"type": "notification", "data": {
                "type": "success" if shown == "stored" else "info", "content": f"{name}: {shown}"}})
        except Exception:
            pass
