# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------



def _state_dir(path=None):
    """Return the persistent bridge state directory, creating it if possible."""
    root = path or os.environ.get("OPENCLAW_BRIDGE_STATE_DIR") or "/data/openclaw-bridge"
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
        return root
    except Exception as ex:
        fallback = "/tmp/openclaw-bridge"
        os.makedirs(fallback, mode=0o700, exist_ok=True)
        pipe_log(f"STATE_DIR unavailable ({root}: {ex}); using {fallback}")
        return fallback


def _read_json_file(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as ex:
        pipe_log(f"Failed reading {path}: {ex}")
        return None


def _write_json_file(path, data):
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except Exception as ex:
        pipe_log(f"Failed writing {path}: {ex}")
        return False


# DEV-ONLY-START
# Cross-agent deploy coordination (ELI-24). Dev/internal tooling only --
# lets install.py know when it's safe to hot-swap this module without
# orphaning an in-flight turn. Stripped from openclaw_pipe.py by build.py;
# never present in the artifact end users install.

def _devcoord_root_dir():
    d = os.path.join(_state_dir(), "deploy-coord")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
    except Exception:
        pass
    return d


def _devcoord_dir():
    d = os.path.join(_devcoord_root_dir(), "inflight")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
    except Exception:
        pass
    return d


def _devcoord_pending_path():
    return os.path.join(_devcoord_root_dir(), "deploy-pending.json")


def _devcoord_turn_begin():
    """Mark a turn as in-flight. Returns the marker path for _devcoord_turn_end."""
    try:
        marker = os.path.join(_devcoord_dir(), f"{uuid.uuid4().hex}.json")
        now = time.time()
        with open(marker, "w") as f:
            json.dump({"started": now, "beat": now}, f)
        os.chmod(marker, 0o600)
        return marker
    except Exception as ex:
        pipe_log(f"devcoord: failed to write inflight marker: {ex}")
        return None


def _devcoord_turn_end(marker):
    if not marker:
        return
    try:
        os.remove(marker)
    except FileNotFoundError:
        pass
    except Exception as ex:
        pipe_log(f"devcoord: failed to clear inflight marker: {ex}")


# Markers from before heartbeats existed carry only `started`.
_DEVCOORD_STALE_MARKER_S = 1200  # 20 minutes
# A live turn refreshes its marker's `beat` this often...
_DEVCOORD_HEARTBEAT_S = 30
# ...so one whose beat is older than this belongs to a turn that is gone.
_DEVCOORD_STALE_BEAT_S = 180


async def _devcoord_heartbeat(marker):
    """Keep `marker` fresh for as long as its turn runs; cancelled at turn
    end. Liveness has to come from the turn itself: a turn's age says
    nothing about whether it is still running (2026-10-11, a 20-minute
    turn was reaped as stale, a deploy went ahead under it, and its live
    stream was cut)."""
    if not marker:
        return
    while True:
        await asyncio.sleep(_DEVCOORD_HEARTBEAT_S)
        data = _read_json_file(marker)
        if not data:
            return  # reaped or ended
        data["beat"] = time.time()
        try:
            with open(marker, "w") as f:
                json.dump(data, f)
        except Exception as ex:
            pipe_log(f"devcoord: heartbeat write failed: {ex}")


def _devcoord_inflight_count():
    """Count in-flight turns, reaping markers whose turn is gone first.

    A turn's `finally` normally removes its own marker, but a process that
    dies mid-turn (killed, crashed, container restarted) can leave one
    behind permanently -- and since install.py's own deploy_guard waits
    for this count to hit zero with no forced timeout of its own (ELI-24
    follow-up), a single leaked marker would otherwise block every future
    deploy forever. A live turn heartbeats its marker, so a marker is
    stale once its heartbeat stops, however long the turn has been
    running. Markers written before heartbeats existed fall back to age.
    """
    now = time.time()
    count = 0
    for name in os.listdir(_devcoord_dir()):
        path = os.path.join(_devcoord_dir(), name)
        data = _read_json_file(path) or {}
        if "beat" in data:
            seen, limit = data["beat"], _DEVCOORD_STALE_BEAT_S
        else:
            seen, limit = data.get("started"), _DEVCOORD_STALE_MARKER_S
        if seen is not None and now - seen > limit:
            pipe_log(f"devcoord: reaping stale inflight marker {name} "
                     f"(no sign of life for {int(now - seen)}s)")
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            continue
        count += 1
    return count


async def _devcoord_wait_if_deploy_pending(*, poll_interval_s=0.25, on_wait=None,
                                           status_interval_s=3.0):
    """Wait, with no upper bound, for a pending-deploy flag to clear before
    admitting a new turn.

    Mirrors install.py's own devcoord_deploy_guard policy (ELI-24): no forced
    timeout, because admitting a turn anyway after some arbitrary cutoff is
    exactly how a deploy's quiet window keeps getting missed -- every newly
    admitted turn joins the in-flight count and pushes the window further
    out. Waiting here instead means in-flight turns can actually drain to
    zero once no new ones are joining.

    `on_wait(waited_s)`, if given, is called roughly every status_interval_s
    while waiting, so the caller (pipe.py) can surface this to the user
    instead of it looking like the turn is simply not starting.
    """
    if not _read_json_file(_devcoord_pending_path()):
        return
    start = time.time()
    next_status = start + status_interval_s
    while _read_json_file(_devcoord_pending_path()):
        now = time.time()
        if on_wait and now >= next_status:
            await on_wait(now - start)
            next_status = now + status_interval_s
        await asyncio.sleep(poll_interval_s)

# DEV-ONLY-END
