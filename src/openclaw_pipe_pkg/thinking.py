# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built artifacts directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------

# Thinking levels: their ranking, the clamp and the Gateway's rejection text.
# Shared by the Pipe and the Status Action (its ladder cache helper).

# Mirrors the Gateway's table. Clamp only downward: less thinking is safe,
# more costs money.
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

# Means "send no level": keep the agent's own. "off" is an explicit level.
UNSET = "default"

# Levels sent as another (Eliav, 2026-11-09: `ultra` goes out as `max`).
LEVEL_SENT_AS = {"ultra": "max"}


def normalize_thinking_level(value):
    """A level as typed (Advanced Params is free text) or picked: lowercased,
    an alias sent as its level, None for empty or UNSET."""
    level = str(value or "").strip().lower()
    level = LEVEL_SENT_AS.get(level, level)
    return None if not level or level == UNSET else level

# Used until the Pipe writes a ladder cache. Every known provider has these.
FALLBACK_LEVELS = ["off", "minimal", "low", "medium", "high"]

LADDER_CACHE_NAME = "thinking-ladders.json"

# The Gateway's rejection lists labels, not ids. The binary profile labels
# `low` as "on".
LEVEL_LABEL_ALIASES = {"on": "low"}

# Anchors of the Gateway's validation error, all required and in order:
#   Thinking level "high" is not supported for claude-cli/claude-opus-5. Use one of: off.
_REJECT_HEAD = 'Thinking level "'
_REJECT_MID = '" is not supported for '
_REJECT_TAIL = ". Use one of: "


def parse_thinking_rejection(text):
    """Parse the Gateway's level rejection into the model's real ladder.

    `sessions.describe` builds its ladder without the model catalog, so it
    over-reports levels; only the send path's rejection is accurate.
    Returns None, or `{"level", "model", "levels"}`; `levels` may be empty, so
    check for None.
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
        # A model ref has no space, so this is not the real rejection.
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
    """Fit a level to a model's ladder. Returns (level, note to show or None).

    Clamps down to the nearest supported rank, else to the lowest one.
    """
    if not level or level == UNSET:
        return None, None
    if not ladder:
        # Unknown ladder: let the Gateway rule rather than drop the user's pick.
        return level, None
    if level in ladder:
        return level, None

    want = LEVEL_RANKS.get(level)
    if want is None:
        return None, f"Unknown thinking level {level!r}, ignoring it."

    ranked_ladder = [lv for lv in ladder if lv in LEVEL_RANKS]
    if not ranked_ladder:
        return None, "This model reported no recognized thinking levels; ignoring the selection."

    at_or_below = [lv for lv in ranked_ladder if LEVEL_RANKS[lv] <= want]
    if at_or_below:
        best = max(at_or_below, key=lambda lv: LEVEL_RANKS[lv])
    else:
        best = min(ranked_ladder, key=lambda lv: LEVEL_RANKS[lv])
    return best, (
        f"This model does not support thinking level {level!r}, "
        f"using {best!r} instead."
    )
