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
# newest children. `seeded`: children spawned before this file existed were
# folded in once, from one full list.
# ---------------------------------------------------------------------------

_SUBAGENT_INDEX_NAME = "subagent-children.json"
_SUBAGENT_INDEX_KEEP_MS = 180 * 86400 * 1000
_SUBAGENT_INDEX_PER_PARENT = 200
# What a Subagents row needs of a child session's row.
_SUBAGENT_SNAPSHOT_KEYS = ("key", "label", "status", "startedAt", "createdAt", "updatedAt", "endedAt",
                           "lastRunError", "spawnedBy", "parentSessionKey")


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
            _write_json_file(path, index)
    except Exception as ex:
        pipe_log(f"[subagents] could not update the child index: {ex}")


def _subagent_children(parent) -> dict:
    """{child key: its last row (or None, a spawn not read yet)} of `parent`."""
    entry = _subagent_index_read()["parents"].get(parent) or {}
    return dict(entry.get("children") or {})


def _subagent_remember(parent, rows=(), keys=()) -> None:
    """Record `parent`'s children: `keys` seen spawned, `rows` as just read."""
    rows, keys = list(rows), list(keys)
    if not parent or not (rows or keys):
        return

    def change(index):
        entry = index["parents"].setdefault(parent, {"at": 0, "children": {}})
        entry["at"] = int(time.time() * 1000)
        for key in keys:
            entry["children"].setdefault(key, None)
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
