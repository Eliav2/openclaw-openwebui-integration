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

# The user's own preferences (today the thinking default) are one key in their Open WebUI settings, not a
# user valve (ELI-442): a Pipe with UserValves makes Open WebUI draw its own Valves button in the composer,
# beside Model controls. The key is `<function id>:prefs`; the Status dialog changes it. Open WebUI's settings
# update replaces only the top-level keys it is sent, so this key is written alone. What the 0.x Pipe kept in
# its `thinking_default` user valve is read under it, and copied to the key the first time a turn finds it
# there (the old valve is left as it was, for a rollback).
PIPE_FUNCTION_ID = "openclaw_gateway"
PREFS_SUFFIX = ":prefs"
# An Open WebUI Pipe model id is `<function id>.<model>`.
MODEL_ID_SEPARATOR = "."
THINKING_PREF = "thinking_default"


def function_id_of(model):
    """The function an Open WebUI model id belongs to: `openclaw_gateway.default` -> `openclaw_gateway`."""
    return str(model or "").split(MODEL_ID_SEPARATOR)[0] or PIPE_FUNCTION_ID


async def resolve_function_id(model):
    """The Pipe's function id for an Open WebUI model id. A workspace model (Admin > Models) built on a Pipe
    model reaches the Pipe as its base model, but the Status dialog is given the workspace model's id: look the
    row up first, as Open WebUI does, so both name the same settings key. A failed lookup uses the id as given."""
    base = model
    try:
        from open_webui.models.models import Models
        row = await Models.get_model_by_id(model) if model else None
        base = getattr(row, "base_model_id", None) or model
    except Exception as ex:
        pipe_log(f"[prefs] could not look up model {model!r}: {ex}")
    return function_id_of(base) if MODEL_ID_SEPARATOR in str(base or "") else PIPE_FUNCTION_ID


def prefs_key(function_id):
    return (function_id or PIPE_FUNCTION_ID) + PREFS_SUFFIX


def merge_prefs(settings, legacy, function_id):
    """The user's prefs: their settings' key over the old user valve, field by field."""
    own = (settings or {}).get(prefs_key(function_id)) if isinstance(settings, dict) else None
    return {**(legacy if isinstance(legacy, dict) else {}), **(own if isinstance(own, dict) else {})}


def _settings_of(user):
    """The `settings` of an Open WebUI user, a dict (`__user__`) or a model."""
    raw = user.get("settings") if isinstance(user, dict) else getattr(user, "settings", None)
    if raw is not None and hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    return raw if isinstance(raw, dict) else {}


async def _legacy_valves(function_id, user_id):
    from open_webui.models.functions import Functions
    return await Functions.get_user_valves_by_id_and_user_id(function_id, user_id)


async def write_prefs(user_id, function_id, change):
    """Read the prefs again (the old valve under them), change these fields, write the key. A failed read
    of the old valve fails the write: it may hold a value this write would replace without knowing it."""
    from open_webui.models.users import Users
    current = merge_prefs(_settings_of(await Users.get_user_by_id(user_id)),
                          await _legacy_valves(function_id, user_id), function_id)
    await Users.update_user_settings_by_id(user_id, {prefs_key(function_id): {**current, **change}})


async def _move_legacy(user_id, function_id, level):
    """Copy the old valve's level to the key, only while the key still has none: the turn's snapshot may be older
    than a save from the Status dialog, which must stand. Open WebUI has no compare-and-set, so the check is a
    read right before the write; the level in effect is returned."""
    from open_webui.models.users import Users
    own = merge_prefs(_settings_of(await Users.get_user_by_id(user_id)), None, function_id)
    if THINKING_PREF in own:
        return own[THINKING_PREF]
    await Users.update_user_settings_by_id(user_id, {prefs_key(function_id): {**own, THINKING_PREF: level}})
    return level


async def thinking_default_of(user, function_id):
    """The user's thinking default (UNSET when none): their settings key, else the 0.x user valve, which is
    copied to the key once. Never raises: a turn must not fail on a preference."""
    own = merge_prefs(_settings_of(user), None, function_id)
    if THINKING_PREF in own:
        return own[THINKING_PREF]
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    if not user_id:
        return UNSET
    try:
        legacy = await _legacy_valves(function_id, user_id)
    except Exception as ex:
        pipe_log(f"[prefs] could not read the old thinking_default valve: {ex}")
        return UNSET
    level = (legacy or {}).get(THINKING_PREF) if isinstance(legacy, dict) else None
    if not isinstance(level, str) or level == UNSET:
        return UNSET
    try:
        return await _move_legacy(user_id, function_id, level)
    except Exception as ex:
        pipe_log(f"[prefs] could not move the old thinking_default valve: {ex}")
    return level


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
