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
    ("opus") that hides which version you're actually talking to — the
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

    The gateway's actual response shape is {id, name, provider, alias, ...} —
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


def _parse_whitelist(text: str) -> set[str]:
    """Parse comma-separated model whitelist into a set."""
    if not text or not text.strip():
        return set()
    return {x.strip() for x in text.split(",") if x.strip()}


async def _discover_models(valves) -> list[dict]:
    """Discover available models from the gateway, cache, or hardcoded fallback.
    
    Tries in order:
    1. Live gateway request (only if connection already up)
    2. Cache file from STATE_DIR
    3. Hardcoded fallback list
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
                return models
        except Exception as e:
            pipe_log(f"Live model discovery failed: {e}")
    
    # 2. Cache fallback
    cache = _read_json_file(os.path.join(_state_dir(getattr(valves, "STATE_DIR", "")), "models-cache.json"))
    if cache and cache.get("models"):
        pipe_log("Using cached model list")
        return cache["models"]
    
    # 3. Hardcoded fallback
    pipe_log("Using hardcoded fallback model list")
    return _FALLBACK_MODELS
