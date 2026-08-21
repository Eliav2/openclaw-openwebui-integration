# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Pipe class (Open WebUI entry point)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dynamic model discovery helpers
# ---------------------------------------------------------------------------

_FALLBACK_MODELS = [
    {"key": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash", "tags": ["configured", "alias:DeepSeek"]},
    {"key": "openai/gpt-5.5", "name": "gpt-5.5", "tags": ["configured"]},
    {"key": "anthropic/claude-opus-4-8", "name": "claude-opus-4-8", "tags": ["configured", "alias:opus"]},
    {"key": "anthropic/claude-sonnet-5", "name": "Claude Sonnet 5", "tags": ["configured", "alias:sonnet-5"]},
    {"key": "openrouter/z-ai/glm-5.2", "name": "GLM 5.2", "tags": ["configured", "alias:openrouter-glm-5.2"]},
]


def _friendly_name(model_entry: dict) -> str:
    """Return a human-friendly name for a model entry.

    Priority: model name field → alias tag → last segment of key.

    The catalog `name` is preferred because it carries the *version*
    ("Claude Opus 4.8"), while the alias is a deliberately short handle
    ("opus") that hides which version you're actually talking to -- the
    selector then shows two indistinguishable "Opus"/"Sonnet" entries as
    new versions land. Same complaint as upstream openclaw#111884.
    Alias remains the fallback for models the gateway returns unnamed.
    """
    name = model_entry.get("name", "")
    if name:
        return name
    tags = model_entry.get("tags", [])
    alias = next((t for t in tags if t.startswith("alias:")), None)
    if alias:
        alias_name = alias.split(":", 1)[1]
        return alias_name[0].upper() + alias_name[1:] if alias_name else alias_name
    return model_entry["key"].rsplit("/", 1)[-1]


def _provider_from_key(key: str) -> str:
    """Extract the provider/vendor from a model key like 'anthropic/claude-opus-4-8'."""
    return key.split("/", 1)[0] if "/" in key else ""


def _normalize_model_entry(raw: dict) -> dict:
    """Normalize a raw gateway `models.list` entry into the {key, name, tags}
    shape used elsewhere in this module (matches _FALLBACK_MODELS).

    The gateway's actual response shape is {id, name, provider, alias, ...} --
    there is no combined "key" or "tags" field, so this bridges the two.
    """
    provider = raw.get("provider", "")
    model_id = raw.get("id", "")
    key = f"{provider}/{model_id}" if provider and model_id else (model_id or provider)
    tags = ["configured"] if raw.get("available", True) else []
    alias = raw.get("alias")
    if alias:
        tags.append(f"alias:{alias}")
    return {"key": key, "name": raw.get("name", model_id), "tags": tags}


# Wording that points at the agent/session rather than the model. Kept NARROW
# on purpose -- an earlier version also matched "not found", which misfiled
# "model 'x/y' not found" as an AGENT_ID problem. Do not re-broaden these; the
# tests in SessionPatchFailureAttributionTests assert both directions.
_AGENT_ERROR_HINTS = ("agent", "session")


def _explain_session_patch_failure(err, *, model_override, agent_id) -> str:
    """Describe a failed sessions.patch without misattributing the cause.

    The session key embeds AGENT_ID and this patch is the turn's first
    agent-scoped RPC, so a mistyped AGENT_ID surfaces here -- and used to be
    reported as "Model selection error", sending the user to fix
    DEFAULT_MODEL, which was never the problem.

    Classifying by substring is genuinely ambiguous, so the ordering matters and
    is deliberate:

    * "model" (or the model key) present -> a MODEL error. Checked FIRST and
      allowed to win outright, because gateway model errors routinely mention
      the agent too ("model x/y is not configured for this agent"), whereas an
      agent error rarely mentions a model. An earlier version of this matched
      "not found" as an agent hint, which misfiled "model 'x/y' not found" --
      the exact misattribution this function exists to prevent, just pointed
      the other way.
    * otherwise, agent/session wording -> an AGENT error.
    * otherwise -> say we don't know, and name both valves. Guessing wrong is
      worse than admitting ambiguity: it sends the user to edit a valve that
      was correct.
    """
    # Transient and transport failures are neither valve's fault, and the retry
    # comment in pipe() says they are the common ones under load. Classify by
    # TYPE first: substring matching cannot see them, and str(TimeoutError()) is
    # "", which rendered as a dangling colon followed by advice to edit two
    # valves that were both correct.
    if isinstance(err, (asyncio.TimeoutError, ConnectionError)):
        return (
            "**The Gateway did not respond in time while starting this "
            "conversation.** This is usually transient -- send the message "
            "again. If it keeps happening, check that the Gateway is healthy "
            "and reachable from Open WebUI."
        )

    detail = str(err).strip() or repr(err)
    low = detail.lower()
    wanted = model_override or "agent default"

    looks_like_model = "model" in low or (
        bool(model_override) and model_override.lower() in low
    )
    if looks_like_model:
        return f"**Model selection error:** could not apply `{wanted}`: {detail}"

    if any(h in low for h in _AGENT_ERROR_HINTS):
        return (
            f"**Could not start a session on agent `{agent_id}`:** {detail}\n\n"
            f"Check the `AGENT_ID` valve -- it must name an agent your OpenClaw "
            f"Gateway actually defines (`main` unless you configured others). "
            f"This surfaces here because applying the model is the first thing "
            f"the pipe asks the agent to do; the model (`{wanted}`) may be fine."
        )

    return (
        f"**Could not start this conversation's session:** {detail}\n\n"
        f"The pipe was applying model `{wanted}` on agent `{agent_id}`. Check "
        f"the `AGENT_ID` valve names an agent your Gateway defines, and that "
        f"the model is one it offers."
    )


MODELS_SOURCE_LIVE = "live"
MODELS_SOURCE_CACHE = "cache"
MODELS_SOURCE_FALLBACK = "fallback"

# Appended to selector entries built from _FALLBACK_MODELS. Display text only --
# the entry's `id` (the routing key) is untouched, so this cannot affect routing.
UNVERIFIED_MODEL_SUFFIX = " -- example, Gateway not reached"


async def _discover_models(valves) -> list[dict]:
    """Back-compat wrapper for callers that don't care where the list came from."""
    models, _source = await _discover_models_with_source(valves)
    return models


async def _discover_models_with_source(valves) -> tuple[list[dict], str]:
    """Discover models, and report where the list came from.

    Returns (models, source), source being "live", "cache" or "fallback".

    Callers need that distinction because the three are not interchangeable to
    a user. `pipes()` never initiates a connection -- it only reuses one that a
    previous chat established -- so on a fresh install the live branch is skipped
    and no cache exists yet, and the selector filled up with five hardcoded
    models bearing no relationship to the user's Gateway, rendered identically
    to genuinely discovered ones. Picking one the Gateway doesn't have then
    failed with a bare "Model selection error", with nothing to suggest the list
    itself had been fabricated.
    """
    # 1. Try live gateway (fast path only if already connected)
    global _gateway_connection
    conn = _gateway_connection
    if conn and conn._ws and conn._event_loop_task and not conn._event_loop_task.done():
        try:
            resp = await conn.send_request("models.list", {}, timeout=5)
            raw_models = resp.get("models", [])
            if raw_models:
                models = [_normalize_model_entry(m) for m in raw_models]
                _write_json_file(
                    os.path.join(_state_dir(getattr(valves, "STATE_DIR", "")), "models-cache.json"),
                    {"models": models, "cachedAt": time.time()}
                )
                return models, MODELS_SOURCE_LIVE
        except Exception as e:
            pipe_log(f"Live model discovery failed: {e}")

    # 2. Cache fallback
    cache = _read_json_file(os.path.join(_state_dir(getattr(valves, "STATE_DIR", "")), "models-cache.json"))
    if cache and cache.get("models"):
        pipe_log("Using cached model list")
        return cache["models"], MODELS_SOURCE_CACHE

    # 3. Hardcoded fallback
    pipe_log(
        "Using hardcoded fallback model list -- the Gateway has not been reached "
        "yet, so these are examples, not your Gateway's models. They are labelled "
        "as such in the model selector."
    )
    return _FALLBACK_MODELS, MODELS_SOURCE_FALLBACK
