# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------



async def _emit_status(__event_emitter__, description, *, done=False):
    """Send an OWUI status event when the current pipe call supports events."""
    if not __event_emitter__:
        return
    await __event_emitter__(
        {"type": "status", "data": {"description": description, "done": done}}
    )


async def _emit_message_snapshot(__event_emitter__, content):
    """Persist the in-flight assistant message content for OWUI reloads.

    Pipe yielded content is still the final source of truth, but OWUI only saves
    that content when the pipe completes. A short-name `replace` event updates
    the message content in the DB while the run is still active, so navigating
    away and back can rehydrate the partial response.
    """
    if not __event_emitter__ or not content:
        return
    await __event_emitter__(
        {"type": "replace", "data": {"content": content}}
    )

