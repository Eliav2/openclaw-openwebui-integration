# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built artifacts directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------

# The Filter class and its pydantic surface. Split from `thinking` so the Pipe
# can reuse the ranks and the clamp without also inheriting a Filter class it
# must not expose.
#
# pydantic is the only non-stdlib import here, and Open WebUI ships it. This
# deliberately does NOT reuse the pipe's _prelude, which pulls in websockets and
# cryptography: a filter runs on every message, and one that cannot import takes
# the whole chat down with it. A transport dependency is not worth that risk for
# a control that only writes one dict key.

import json
import os
from typing import Optional

from pydantic import BaseModel, Field, create_model


# ---------------------------------------------------------------------------
# Reading the ladder cache
# ---------------------------------------------------------------------------

def _state_dir():
    """Same default and env override as the pipe, without importing it.

    Read-only here, so unlike the pipe this never creates or falls back to a
    writable directory: a missing directory just means no cache yet.
    """
    return os.environ.get("OPENCLAW_BRIDGE_STATE_DIR") or "/data/openclaw-bridge"


def _read_ladder_cache(path=None):
    """Return the level ids the pipe has actually seen a gateway offer.

    The cache is written by the pipe (see gateway.py) as::

        {"levels": ["off", "minimal", ...], "updated": 1786784049}

    Anything unreadable, malformed, or empty falls back rather than raising:
    a broken cache must degrade to a shorter dropdown, never to a broken chat.
    """
    path = path or os.path.join(_state_dir(), LADDER_CACHE_NAME)
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    levels = data.get("levels")
    if not isinstance(levels, list):
        return None
    known = [str(x) for x in levels if str(x) in LEVEL_RANKS]
    return known or None


def available_levels(path=None):
    """The dropdown contents: UNSET first, then real levels ordered by rank.

    This is the union across the models the gateway knows about, not the ladder
    of the currently selected model. Open WebUI builds a valve dropdown once,
    from the class, so it cannot vary per message. The pipe closes that gap by
    clamping against the live session ladder at send time and saying so.
    """
    levels = _read_ladder_cache(path) or FALLBACK_LEVELS
    ordered = sorted(set(levels), key=lambda lv: (LEVEL_RANKS[lv], lv))
    return [UNSET] + ordered


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

# Black stroke on purpose: Open WebUI renders this via <img src=...> and applies
# `dark:invert-[80%]` to any `data:image/svg` icon, so currentColor never
# resolves and a themed stroke shows up invisible in one of the two themes.
ICON = (
    "data:image/svg+xml;charset=utf-8,"
    "%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2024%2024'%20"
    "fill='none'%20stroke='%23000'%20stroke-width='1.8'%20stroke-linecap='round'%20"
    "stroke-linejoin='round'%3E%3Cpath%20d='M12%203a5.5%205.5%200%200%200-3.2%209.9V15h6.4v-2.1"
    "A5.5%205.5%200%200%200%2012%203z'/%3E%3Cpath%20d='M9.5%2018h5'/%3E%3Cpath%20d='M10.5%2021h3'/%3E%3C/svg%3E"
)


def _build_user_valves(levels):
    """Build the UserValves model with the dropdown baked in.

    A pydantic Literal is fixed when the class is defined, so the choices have
    to be known at import time. create_model is what lets the list come from
    the gateway's real ladders instead of being hardcoded here.
    """
    return create_model(
        "UserValves",
        level=(
            str,
            Field(
                default=UNSET,
                description=(
                    "How hard the agent thinks before answering. "
                    "'" + UNSET + "' sends nothing and leaves the agent's own "
                    "configured level alone. Levels a model does not support "
                    "are clamped down, and the chat says so."
                ),
                json_schema_extra={"enum": levels},
            ),
        ),
        __base__=BaseModel,
    )


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=0,
            description="Filter execution order. Lower runs earlier.",
        )
        override_advanced_params: bool = Field(
            default=True,
            description=(
                "When on, the level picked here wins over Open WebUI's own "
                "Advanced Params reasoning effort. Turn it off to let Advanced "
                "Params win and use this toggle only to enable thinking."
            ),
        )

    UserValves = _build_user_valves(available_levels())

    def __init__(self):
        self.valves = self.Valves()
        # Open WebUI caches the Filter *instance*, not the module, so `toggle`
        # and `icon` have to be instance attributes. Set at class level they are
        # invisible to get_filter_items_from_module() and the row silently never
        # renders, which reads exactly like the filter not being installed.
        self.toggle = True
        self.icon = ICON

    def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        """Write the chosen level into the field the pipe reads.

        `body["reasoning_effort"]` is the single source of truth. Open WebUI's
        Advanced Params control writes the same key, so the two controls move
        one value instead of racing over two.

        Reached only when the toggle is on: Open WebUI does not run a toggled
        filter's inlet otherwise. So an untouched chat behaves exactly as if
        this filter were not installed.
        """
        if not isinstance(body, dict):
            return body

        user_valves = (__user__ or {}).get("valves")
        level = getattr(user_valves, "level", None)
        if isinstance(user_valves, dict):
            level = user_valves.get("level")

        if not level or level == UNSET:
            # Nothing chosen. Leave whatever Advanced Params set, including
            # leaving the key absent, so the pipe omits it downstream.
            return body

        if body.get("reasoning_effort") and not self.valves.override_advanced_params:
            return body

        body["reasoning_effort"] = level
        return body
