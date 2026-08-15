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
        best = max(at_or_below, key=lambda lv: LEVEL_RANKS[lv])
    else:
        best = min(ladder, key=lambda lv: LEVEL_RANKS.get(lv, 0))
    return best, (
        f"This model does not support thinking level {level!r}, "
        f"using {best!r} instead."
    )
