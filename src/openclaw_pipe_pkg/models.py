# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------



_FALLBACK_MODELS = [
    {"key": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash", "tags": ["configured", "alias:DeepSeek"]},
    {"key": "openai/gpt-5.5", "name": "gpt-5.5", "tags": ["configured"]},
    {"key": "anthropic/claude-opus-4-8", "name": "claude-opus-4-8", "tags": ["configured", "alias:opus"]},
    {"key": "anthropic/claude-sonnet-5", "name": "Claude Sonnet 5", "tags": ["configured", "alias:sonnet-5"]},
    {"key": "openrouter/z-ai/glm-5.2", "name": "GLM 5.2", "tags": ["configured", "alias:openrouter-glm-5.2"]},
]


def _friendly_name(model_entry: dict) -> str:
    """Display name: catalog name, else alias tag, else last key segment.

    The name carries the version; an alias like "opus" hides it (openclaw#111884).
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
    """'anthropic/claude-opus-4-8' -> 'anthropic'."""
    return key.split("/", 1)[0] if "/" in key else ""


def _normalize_model_entry(raw: dict) -> dict:
    """Gateway `models.list` entry {id, name, provider, alias, ...} -> {key, name, tags}."""
    provider = raw.get("provider", "")
    model_id = raw.get("id", "")
    key = f"{provider}/{model_id}" if provider and model_id else (model_id or provider)
    tags = ["configured"] if raw.get("available", True) else []
    alias = raw.get("alias")
    if alias:
        tags.append(f"alias:{alias}")
    return {"key": key, "name": raw.get("name", model_id), "tags": tags}


def _usable_models(raw_models: list[dict]) -> list[dict]:
    """Normalized entries, minus those marked `available: false` (e.g. missing auth).

    The Gateway still lists allowlisted models with no credentials.
    """
    return [
        _normalize_model_entry(m) for m in raw_models
        if m.get("available", True) is not False
    ]


# Keep narrow: "not found" also appears in model errors.
# SessionPatchFailureAttributionTests assert both directions.
_AGENT_ERROR_HINTS = ("agent", "session")


# The raw side of the start timeout: for whoever runs the Gateway.
_START_TIMEOUT_ADVICE = (
    "The Gateway did not respond in time while starting this conversation. "
    "This is usually transient -- send the message again. If it keeps "
    "happening, check that the Gateway is healthy and reachable from Open WebUI."
)


def _explain_session_patch_failure(err, *, model_override, agent_id, user_text="") -> str:
    """Explain a failed sessions.patch: model, AGENT_ID, or unknown.

    This is the first agent-scoped RPC, so a bad AGENT_ID fails here too.
    Model wording wins first, since model errors often mention the agent.
    """
    # Transport errors are no valve's fault, and str(TimeoutError()) is "".
    if isinstance(err, (asyncio.TimeoutError, ConnectionError)):
        return _plain_error(ERROR_TIMEOUT, _START_TIMEOUT_ADVICE, user_text)

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

# Display-only suffix for _FALLBACK_MODELS entries; the routing `id` is untouched.
UNVERIFIED_MODEL_SUFFIX = " -- example, Gateway not reached"


async def _discover_models_with_source(valves) -> tuple[list[dict], str]:
    """Return (models, source), source being "live", "cache" or "fallback".

    `pipes()` only reuses an open connection, so a fresh install gets the
    fallback list, which the selector must label as examples.
    """
    # Live only if a connection is already open.
    global _gateway_connection
    conn = _gateway_connection
    if conn and conn._ws and conn._event_loop_task and not conn._event_loop_task.done():
        try:
            resp = await conn.send_request("models.list", {}, timeout=5)
            raw_models = resp.get("models", [])
            if raw_models:
                models = _usable_models(raw_models)
                _write_json_file(
                    os.path.join(_state_dir(getattr(valves, "STATE_DIR", "")), "models-cache.json"),
                    {"models": models, "cachedAt": time.time()}
                )
                return models, MODELS_SOURCE_LIVE
        except Exception as e:
            pipe_log(f"Live model discovery failed: {e}")

    cache = _read_json_file(os.path.join(_state_dir(getattr(valves, "STATE_DIR", "")), "models-cache.json"))
    if cache and cache.get("models"):
        pipe_log("Using cached model list")
        return cache["models"], MODELS_SOURCE_CACHE

    pipe_log(
        "Using hardcoded fallback model list -- the Gateway has not been reached "
        "yet, so these are examples, not your Gateway's models. They are labelled "
        "as such in the model selector."
    )
    return _FALLBACK_MODELS, MODELS_SOURCE_FALLBACK
