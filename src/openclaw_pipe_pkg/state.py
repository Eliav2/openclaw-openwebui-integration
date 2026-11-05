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




# Sub-agent child index (ELI-152). The Gateway has no "children of" read and
# `sessions.list {spawnedBy}` drops ended children after 30 min, so the Pipe
# and Status Action share this flock-guarded file. `tasks` title a child before
# its transcript has the spawn message (ELI-178). `runs` let a cleanup "delete"
# child be read via `agent.wait` (ELI-276, ELI-277).

_SUBAGENT_INDEX_NAME = "subagent-children.json"
_SUBAGENT_INDEX_KEEP_MS = 180 * 86400 * 1000
_SUBAGENT_INDEX_PER_PARENT = 200
_SUBAGENT_SNAPSHOT_KEYS = ("key", "label", "status", "startedAt", "createdAt", "updatedAt", "endedAt",
                           "lastRunError", "spawnedBy", "parentSessionKey", "gone")


def _subagent_ended(row) -> bool:
    """Whether a child row's run is over; keep it, as cleanup "delete" removes
    the session soon after (ELI-276)."""
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
    """{child key: {"runId", "label", "error"}} of `parent`'s spawns."""
    entry = _subagent_index_read()["parents"].get(parent) or {}
    return dict(entry.get("runs") or {})


def _subagent_record(child) -> dict | None:
    """{"parent", "row", "task", "run"} for one child, or None if unknown."""
    for parent, entry in _subagent_index_read()["parents"].items():
        if child in (entry.get("children") or {}):
            return {"parent": parent, "row": entry["children"][child],
                    "task": (entry.get("tasks") or {}).get(child),
                    "run": (entry.get("runs") or {}).get(child) or {}}
    return None


def _subagent_remember(parent, rows=(), keys=(), tasks=None, runs=None) -> None:
    """Merge spawned `keys`, read `rows`, `tasks` and `runs` into `parent`'s entry."""
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
