# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built artifacts directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------

# The Filter class, split from `thinking` so the Pipe does not expose a Filter.
# Does not use the Pipe's _prelude (websockets, cryptography): a filter that
# fails to import takes the whole chat down. pydantic ships with Open WebUI.

import json
import os
from typing import Optional

from pydantic import BaseModel, Field, create_model


def _state_dir():
    """The Pipe's state dir, read-only: a missing dir means no cache yet."""
    return os.environ.get("OPENCLAW_BRIDGE_STATE_DIR") or "/data/openclaw-bridge"


def _read_ladder_cache(path=None):
    """Level ids the Pipe cached (gateway.py writes {"levels": [...], "updated": ts}).

    Returns None on any bad cache, never raises.
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
    """Dropdown: UNSET, then the union of all models' levels by rank.

    OWUI builds the dropdown once per class, so the Pipe clamps per model.
    """
    levels = _read_ladder_cache(path) or FALLBACK_LEVELS
    ordered = sorted(set(levels), key=lambda lv: (LEVEL_RANKS[lv], lv))
    return [UNSET] + ordered


# Black stroke: OWUI shows it via <img> with `dark:invert-[80%]`, so
# currentColor never resolves.
ICON = (
    "data:image/svg+xml;charset=utf-8,"
    "%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2024%2024'%20"
    "fill='none'%20stroke='%23000'%20stroke-width='1.8'%20stroke-linecap='round'%20"
    "stroke-linejoin='round'%3E%3Cpath%20d='M12%203a5.5%205.5%200%200%200-3.2%209.9V15h6.4v-2.1"
    "A5.5%205.5%200%200%200%2012%203z'/%3E%3Cpath%20d='M9.5%2018h5'/%3E%3Cpath%20d='M10.5%2021h3'/%3E%3C/svg%3E"
)


def _build_user_valves(levels):
    """UserValves with the level dropdown, built at import from the cache."""
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
        # Must be instance attributes: OWUI's get_filter_items_from_module()
        # does not see class-level ones and the toggle never renders.
        self.toggle = True
        self.icon = ICON

    def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        """Write the level into body["reasoning_effort"], the key Advanced Params also sets.

        OWUI runs this only while the toggle is on.
        """
        if not isinstance(body, dict):
            return body

        user_valves = (__user__ or {}).get("valves")
        level = getattr(user_valves, "level", None)
        if isinstance(user_valves, dict):
            level = user_valves.get("level")

        if not level or level == UNSET:
            return body

        if body.get("reasoning_effort") and not self.valves.override_advanced_params:
            return body

        body["reasoning_effort"] = level
        return body
