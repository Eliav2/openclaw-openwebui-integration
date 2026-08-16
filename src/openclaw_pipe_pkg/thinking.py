# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built artifacts directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------

# Shared by BOTH the Thinking filter and the Pipe: the filter offers the levels
# and the pipe enforces them against the live session ladder, so the ranks and
# the clamping rule have to be one implementation, not two that agree today.
#
# Deliberately has no imports, no classes, and no module-level names beyond the
# five below. It is concatenated into the Pipe artifact, where every name in it
# lands in the same flat namespace as every other fragment: a helper named like
# one of the pipe's own (a `_state_dir`, say) would silently shadow it
# depending on fragment order. Filter-only helpers live in `thinking_filter`
# for exactly that reason.

# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------

# Ranks mirror the gateway's own table (off is genuinely a level, not an
# absence). Used to order the dropdown and to clamp downward, never upward:
# asking for less thinking than requested is a safe degradation, asking for
# more is a surprise on someone's bill.
LEVEL_RANKS = {
    "off": 0,
    "minimal": 10,
    "low": 20,
    "medium": 30,
    "adaptive": 30,
    "high": 40,
    "xhigh": 60,
    "max": 70,
    "ultra": 80,
}

# The sentinel meaning "do not send the field at all". Distinct from "off",
# which is an explicit instruction to not think. "default" leaves the agent's
# own configured level alone; "off" overrides it.
UNSET = "default"

# Used until the pipe has run once and written a real ladder cache. Every
# provider observed supports at least these, so nothing here can be a lie.
FALLBACK_LEVELS = ["off", "minimal", "low", "medium", "high"]

LADDER_CACHE_NAME = "thinking-ladders.json"

# The Gateway's rejection lists LABELS, not ids, and for most profiles the two
# are identical (`label: id`). The one divergence is the binary profile, which
# labels `low` as "on". Mapping it back is the difference between learning a
# model's real two-level ladder and learning a one-level lie.
LEVEL_LABEL_ALIASES = {"on": "low"}

# The exact anchors of the Gateway's hard validation error, e.g.:
#   Thinking level "high" is not supported for claude-cli/claude-opus-5. Use one of: off.
# All three must be present and in order. That is deliberately at least as
# narrow as matching the whole sentence: it can never fire on genuine assistant
# text that happens to discuss thinking levels.
_REJECT_HEAD = 'Thinking level "'
_REJECT_MID = '" is not supported for '
_REJECT_TAIL = ". Use one of: "


def parse_thinking_rejection(text):
    """Read the Gateway's own rejection as an authoritative per-model ladder.

    This exists because `sessions.describe` cannot be trusted for this. Describe
    builds its ladder with no model catalog in scope, so `resolveThinkingProfile`
    never sees the catalog's `reasoning: false` and falls through to the generic
    base profile -- it reported all 8 levels for `claude-cli/claude-opus-5`,
    whose real ladder is `["off"]`. The send path resolves the same question
    WITH the catalog and rejects. So the rejection is the only place the truth
    is stated, and throwing it away is what made this misfire once per turn
    forever instead of once per model.

    Returns None when `text` is not that rejection. Otherwise a dict:
    `{"level": requested, "model": "provider/model", "levels": [ids]}`.
    `levels` may be empty if every listed label is unrecognised -- the caller
    still learns which model rejected which level, so it must check for None
    rather than for falsiness.

    Pure string parsing on purpose: this fragment is shared with the Thinking
    filter, which imports nothing (not even `re`) so that a filter running on
    every message can never fail on an import it did not need.
    """
    if not text:
        return None
    head = text.find(_REJECT_HEAD)
    if head < 0:
        return None
    level_start = head + len(_REJECT_HEAD)
    mid = text.find(_REJECT_MID, level_start)
    if mid < 0:
        return None
    tail = text.find(_REJECT_TAIL, mid)
    if tail < 0:
        return None

    level = text[level_start:mid].strip().lower()
    model = text[mid + len(_REJECT_MID):tail].strip()
    if not level or not model or " " in model:
        # A model ref never contains a space. Anything that does means the
        # anchors matched something that merely reads like the rejection.
        return None

    listed = text[tail + len(_REJECT_TAIL):]
    stop = listed.find(".")
    if stop >= 0:
        listed = listed[:stop]
    levels = []
    for token in listed.split(","):
        name = token.strip().lower()
        name = LEVEL_LABEL_ALIASES.get(name, name)
        if name in LEVEL_RANKS and name not in levels:
            levels.append(name)
    levels.sort(key=lambda lv: LEVEL_RANKS[lv])
    return {"level": level, "model": model, "levels": levels}


def clamp_to_ladder(level, ladder):
    """Fit a requested level to what a model actually supports.

    Returns (resolved_level, note). `note` is None when the request went
    through untouched, otherwise a short human sentence explaining what
    changed, which the caller is expected to show rather than swallow.

    Clamping is always downward to the nearest supported rank. If the model
    supports nothing at or below the request (a ladder of only higher levels,
    which no observed provider has, but which costs nothing to handle), the
    lowest supported level is used.
    """
    if not level or level == UNSET:
        return None, None
    if not ladder:
        # No ladder known: pass the request through and let the gateway rule on
        # it. Silently dropping a level the user explicitly picked is worse.
        return level, None
    if level in ladder:
        return level, None

    want = LEVEL_RANKS.get(level)
    if want is None:
        return None, f"Unknown thinking level {level!r}, ignoring it."

    at_or_below = [lv for lv in ladder if LEVEL_RANKS.get(lv, 0) <= want]
    if at_or_below:
        best = max(at_or_below, key=lambda lv: LEVEL_RANKS.get(lv, 0))
    else:
        best = min(ladder, key=lambda lv: LEVEL_RANKS.get(lv, 0))
    return best, (
        f"This model does not support thinking level {level!r}, "
        f"using {best!r} instead."
    )
