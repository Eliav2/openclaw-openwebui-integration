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




# ---------------------------------------------------------------------------
# Sub-agent child index: the child sessions each chat's session spawned.
#
# The Status dialog lists a chat's children, ended ones included (ELI-152).
# The Gateway has no "children of" read: `sessions.list {spawnedBy}` keeps an
# ended child for 30 minutes only, and a read without it returns every session
# (1.35 MB for 422 rows live). So the Pipe records each spawn it sees accepted
# (`_spawned_child`), and the Status Action keeps each child's last row here,
# so an ended child is listed from this file and a child the window dropped is
# looked up by its own key. One file in the bridge state dir, shared by the
# Pipe and the Action (separate modules in one Open WebUI process), so it
# survives restarts and redeploys. Each change is a read-modify-write under an
# flock. Parents untouched for 180 days are dropped, and each keeps its 200
# newest children. `tasks`: the task each spawn call was given, by child key,
# which titles the child before its transcript holds the spawn message (on
# 2026.10 that is written after the spawn returns, ELI-178). `runs`: each
# spawn's run id and label, by child key. A child spawned with cleanup
# "delete" has no session once its run is done, so its row comes from its run
# (`agent.wait`), and so does a failed child's real error (ELI-276, ELI-277).
# `seeded`: children spawned before this file existed were folded in once,
# from one full list.
# ---------------------------------------------------------------------------

_SUBAGENT_INDEX_NAME = "subagent-children.json"
_SUBAGENT_INDEX_KEEP_MS = 180 * 86400 * 1000
_SUBAGENT_INDEX_PER_PARENT = 200
# What a Subagents row needs of a child session's row.
_SUBAGENT_SNAPSHOT_KEYS = ("key", "label", "status", "startedAt", "createdAt", "updatedAt", "endedAt",
                           "lastRunError", "spawnedBy", "parentSessionKey", "gone")


def _subagent_ended(row) -> bool:
    """A child session row (or `sessions.changed` push) whose run is over:
    the row to keep, since a cleanup "delete" removes the session seconds
    later (ELI-276)."""
    return (row or {}).get("status") not in (None, "running", "queued") and row.get("hasActiveRun") is not True


def _subagent_index_path():
    return os.path.join(_state_dir(), _SUBAGENT_INDEX_NAME)


def _subagent_index_read() -> dict:
    data = _read_json_file(_subagent_index_path())
    if not isinstance(data, dict) or not isinstance(data.get("parents"), dict):
        return {"seeded": False, "parents": {}}
    return data


def _subagent_index_update(change) -> None:
    """Apply `change(index)` to the file under its lock, then prune it."""
    path = _subagent_index_path()
    try:
        import fcntl
    except ImportError:
        fcntl = None
    try:
        with open(path + ".lock", "a") as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            index = _subagent_index_read()
            change(index)
            now = int(time.time() * 1000)
            parents = index["parents"]
            for parent in [p for p, e in parents.items() if now - (e.get("at") or 0) > _SUBAGENT_INDEX_KEEP_MS]:
                del parents[parent]
            for entry in parents.values():
                children = entry["children"]
                if len(children) > _SUBAGENT_INDEX_PER_PARENT:
                    # A key with no row yet is the newest spawn: kept first.
                    newest = sorted(children, key=lambda k: -((children[k] or {}).get("startedAt")
                                                              or (children[k] or {}).get("createdAt")
                                                              or now))
                    entry["children"] = {k: children[k] for k in newest[:_SUBAGENT_INDEX_PER_PARENT]}
                for field in ("tasks", "runs"):
                    if entry.get(field):
                        entry[field] = {k: t for k, t in entry[field].items() if k in entry["children"]}
            _write_json_file(path, index)
    except Exception as ex:
        pipe_log(f"[subagents] could not update the child index: {ex}")


def _subagent_children(parent) -> dict:
    """{child key: its last row (or None, a spawn not read yet)} of `parent`."""
    entry = _subagent_index_read()["parents"].get(parent) or {}
    return dict(entry.get("children") or {})


def _subagent_tasks(parent) -> dict:
    """{child key: the task its spawn call was given} of `parent`."""
    entry = _subagent_index_read()["parents"].get(parent) or {}
    return dict(entry.get("tasks") or {})


def _subagent_runs(parent) -> dict:
    """{child key: {"runId", "label", "error"}} of `parent`'s spawns: the run
    each was started with, and that run's own error once read."""
    entry = _subagent_index_read()["parents"].get(parent) or {}
    return dict(entry.get("runs") or {})


def _subagent_record(child) -> dict | None:
    """What the index holds of one child, under whichever parent spawned it:
    {"parent", "row" (or None), "task", "run"}; None for a child it never saw."""
    for parent, entry in _subagent_index_read()["parents"].items():
        if child in (entry.get("children") or {}):
            return {"parent": parent, "row": entry["children"][child],
                    "task": (entry.get("tasks") or {}).get(child),
                    "run": (entry.get("runs") or {}).get(child) or {}}
    return None


def _subagent_remember(parent, rows=(), keys=(), tasks=None, runs=None) -> None:
    """Record `parent`'s children: `keys` seen spawned, `rows` as just read,
    `tasks` {child key: the task its spawn call was given}, `runs` {child
    key: fields of its run}, merged into what is kept."""
    rows, keys, tasks, runs = list(rows), list(keys), dict(tasks or {}), dict(runs or {})
    if not parent or not (rows or keys or tasks or runs):
        return

    def change(index):
        entry = index["parents"].setdefault(parent, {"at": 0, "children": {}})
        entry["at"] = int(time.time() * 1000)
        for key in [*keys, *tasks, *runs]:
            entry["children"].setdefault(key, None)
        if tasks:
            entry.setdefault("tasks", {}).update(tasks)
        for key, fields in runs.items():
            entry.setdefault("runs", {}).setdefault(key, {}).update(fields)
        for row in rows:
            entry["children"][row["key"]] = {k: row[k] for k in _SUBAGENT_SNAPSHOT_KEYS if row.get(k) is not None}
    _subagent_index_update(change)


def _subagent_index_seeded() -> bool:
    return bool(_subagent_index_read().get("seeded"))


def _subagent_index_mark_seeded(rows=()) -> None:
    """Fold every child in `rows` (one full list) in by its parent, once."""
    rows = list(rows)

    def change(index):
        now = int(time.time() * 1000)
        for row in rows:
            parent = row.get("spawnedBy") or row.get("parentSessionKey")
            if not parent or not row.get("key") or row["key"] == parent:
                continue
            entry = index["parents"].setdefault(parent, {"at": now, "children": {}})
            entry["children"].setdefault(
                row["key"], {k: row[k] for k in _SUBAGENT_SNAPSHOT_KEYS if row.get(k) is not None})
        index["seeded"] = True
    _subagent_index_update(change)
