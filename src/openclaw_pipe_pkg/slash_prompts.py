# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Slash commands as Open WebUI Prompts (SYNC_SLASH_COMMANDS valve)
#
# Open WebUI's `/` menu has a hardcoded Commands section, and a Prompts
# section that lists the Workspace Prompts a user can read. This mirrors the
# Gateway's slash commands into Prompts, one per command, readable by every
# user, so typing `/` in a chat offers them.
#
# Ownership: a ledger file in the bridge state dir lists the prompts the sync
# created, by command and prompt id. The sync only ever updates or deletes a
# prompt in the ledger whose id still matches; a prompt someone made by hand
# with the same command is left alone and the command skipped. A lost ledger
# deletes nothing. Each Pipe Function has its own ledger, so a second copy
# of the Pipe, its sync off, does not delete the first copy's prompts; one
# lock file for all copies keeps their syncs from running at once. A prompt
# an admin turned off in the Workspace stays the sync's, and stays off.
#
# Cost: the Gateway's `chat.metadata` carries the command list and a
# revision; asked with `ifRevision` it answers `unchanged: true` and nothing
# else. A check runs on a page load, at most every SYNC_INTERVAL_MIN after a
# message (the message path only compares a timestamp), and on the Gateway's
# `chat.metadata.changed` push. The revision is recorded only after every
# Open WebUI write succeeded, so a failed write is retried by the next check.
# Turning the valve off deletes the prompts in the ledger.
# ---------------------------------------------------------------------------

_SLASH_PROMPTS_LEDGER_NAME = "slash-command-prompts.json"  # before the ledger was per Function
_SLASH_PROMPTS_LOCK_NAME = "slash-command-prompts.lock"
_SLASH_PROMPTS_TAG = "openclaw"
_SLASH_PROMPT_NAME_MAX = 120
# Open WebUI's prompt editor accepts these in a command; the backend checks nothing.
_SLASH_PROMPT_CMD_BAD = re.compile(r"[^A-Za-z0-9_-]")
# A push right after a check is folded into a check this long after it.
_SLASH_PROMPTS_PUSH_SETTLE_S = 2.0
# How a Gateway older than 2026.10 refuses `chat.metadata`'s params
# (2026.9.3: "invalid chat.metadata params: at root: unexpected property
# 'includeModels'"), or one without the method at all.
_SLASH_PROMPTS_OLD_GATEWAY = re.compile(
    r"unexpected property '(?:includeModels|ifRevision)'|unknown method:? chat\.metadata\b")
# Once a Gateway refused them, how long `commands.list` is used before
# `chat.metadata` is tried again (the Gateway may have been upgraded).
_SLASH_PROMPTS_LEGACY_RETRY_S = 24 * 3600

_slash_prompts_lock = asyncio.Lock()
_slash_prompts_state = {
    # When the last check ran (monotonic), and whether one is queued behind it.
    "checked_at": None,
    "pending": False,
    # A `_slash_prompts_run` loop is going (between its checks too).
    "running": False,
    # The settings the last check ran with; a change runs a full sync at once.
    "settings": None,
    # Whether the ledger is known to be empty (so a disabled sync has nothing to delete).
    "ledger_empty": None,
    "valves": None,
    # When the Gateway last refused `chat.metadata`'s `ifRevision` (before 2026.10), monotonic.
    "legacy_at": None,
}


def _slash_prompt_excludes(raw) -> set:
    return {p.strip().lstrip("/").lower() for p in str(raw or "").split(",") if p.strip()}


def _slash_prompts_settings(valves) -> dict:
    """What the synced prompts depend on besides the Gateway's list."""
    return {
        "agent": str(getattr(valves, "AGENT_ID", "") or "main"),
        "prefix": str(getattr(valves, "PROMPT_PREFIX", "") or ""),
        "exclude": sorted(_slash_prompt_excludes(getattr(valves, "SLASH_COMMANDS_EXCLUDE", ""))),
    }


def _slash_command_text(cmd: dict) -> str:
    """What the user types to run `cmd`: its first text alias, else /name."""
    for alias in cmd.get("textAliases") or []:
        if isinstance(alias, str) and alias.startswith("/") and len(alias) > 1:
            return alias
    return "/" + str(cmd.get("name") or "")


def _slash_prompt_for(cmd: dict, prefix: str) -> "dict | None":
    """The prompt for one Gateway command, or None when it cannot be one.

    Command: the OpenClaw name with characters Open WebUI's editor rejects
    as `_`, after PROMPT_PREFIX. Name: the command's description. Content: the command as typed, then a
    `{{arg}}` input for each required argument (Open WebUI asks for those
    when the prompt is picked); optional arguments are typed after it."""
    name = str(cmd.get("name") or "").strip()
    if not name:
        return None
    command = _SLASH_PROMPT_CMD_BAD.sub("_", prefix + name)
    parts = [_slash_command_text(cmd)]
    for arg in cmd.get("args") or []:
        arg_name = _SLASH_PROMPT_CMD_BAD.sub("_", str((arg or {}).get("name") or ""))
        if arg_name and arg.get("required"):
            parts.append("{{" + arg_name + "}}")
    # The menu shows the name beside the command, cut to its width.
    description = " ".join(str(cmd.get("description") or "").split())
    if len(description) > _SLASH_PROMPT_NAME_MAX:
        description = description[:_SLASH_PROMPT_NAME_MAX - 3].rstrip() + "..."
    return {
        "command": command,
        "name": description or f"OpenClaw /{name}",
        "content": " ".join(parts) + " ",
        "tags": [_SLASH_PROMPTS_TAG],
        "meta": {"openclaw": {"command": name, "source": cmd.get("source") or "",
                              "function": _slash_prompts_owner()}},
    }


def _slash_prompts_wanted(commands, settings: dict) -> list:
    """The prompts the Gateway's command list calls for, in its order: text
    commands only, minus SLASH_COMMANDS_EXCLUDE, one per command (a second
    command that maps to a taken prompt command is dropped)."""
    exclude = set(settings.get("exclude") or ())
    out, seen = [], set()
    for cmd in commands or []:
        if not isinstance(cmd, dict):
            continue
        if cmd.get("scope") not in (None, "text", "both"):
            continue
        if str(cmd.get("name") or "").lower() in exclude:
            continue
        prompt = _slash_prompt_for(cmd, settings.get("prefix") or "")
        if prompt is None or prompt["command"].lower() in seen:
            continue
        seen.add(prompt["command"].lower())
        out.append(prompt)
    return out


def _slash_prompt_differs(row: dict, want: dict) -> bool:
    return (row.get("name") != want["name"] or row.get("content") != want["content"]
            or list(row.get("tags") or []) != want["tags"])


def _slash_prompts_plan(wanted: list, existing: dict, owned: dict) -> dict:
    """What to write to make Open WebUI match `wanted`.

    `existing`: Open WebUI's prompts by command, each {"id", "name",
    "content", "tags"}. `owned`: the ledger, command -> prompt id.
    Returns lists of `add` (wanted prompts), `update` ((id, wanted)),
    `delete` ((command, id)), `skip` (commands taken by a prompt the sync
    does not own) and `forget` (ledger commands whose prompt is gone or was
    replaced: dropped from the ledger, nothing deleted)."""
    plan = {"add": [], "update": [], "delete": [], "skip": [], "forget": []}
    wanted_cmds = set()
    for want in wanted:
        cmd = want["command"]
        wanted_cmds.add(cmd)
        row = existing.get(cmd)
        if row is None:
            if cmd in owned:
                plan["forget"].append(cmd)
            plan["add"].append(want)
        elif owned.get(cmd) == row.get("id"):
            if _slash_prompt_differs(row, want):
                plan["update"].append((row["id"], want))
        else:
            if cmd in owned:
                plan["forget"].append(cmd)
            plan["skip"].append(cmd)
    for cmd, pid in owned.items():
        if cmd in wanted_cmds:
            continue
        row = existing.get(cmd)
        if row is not None and row.get("id") == pid:
            plan["delete"].append((cmd, pid))
        else:
            plan["forget"].append(cmd)
    return plan


def _slash_prompts_owner() -> str:
    """This Pipe Function, as Open WebUI loads it: `function_<id>`."""
    return _SLASH_PROMPT_CMD_BAD.sub("_", _function_module_prefix(__name__))


def _slash_prompts_ledger_path():
    return os.path.join(_state_dir(), f"slash-command-prompts.{_slash_prompts_owner()}.json")


def _slash_prompts_ledger_read() -> dict:
    path = _slash_prompts_ledger_path()
    old = os.path.join(_state_dir(), _SLASH_PROMPTS_LEDGER_NAME)
    if not os.path.exists(path) and os.path.exists(old):
        # The ledger from before it was per Function: the first copy to sync
        # takes it, under the lock, so its prompts stay owned.
        try:
            os.replace(old, path)
        except OSError as ex:
            pipe_log(f"[slash prompts] could not take over {old}: {ex!r}")
    data = _read_json_file(path)
    if not isinstance(data, dict) or not isinstance(data.get("prompts"), dict):
        return {"revision": None, "settings": None, "prompts": {}}
    return data


def _slash_prompts_ledger_write(ledger: dict) -> bool:
    _slash_prompts_state["ledger_empty"] = not ledger.get("prompts")
    return _write_json_file(_slash_prompts_ledger_path(), ledger)


class _OwuiPromptStore:
    """Open WebUI's Prompts table, in process. Only what the sync needs."""

    def __init__(self):
        from open_webui.models.prompts import Prompts, PromptForm
        self._prompts = Prompts
        self._form = PromptForm
        self._owner = None

    async def owner_id(self) -> "str | None":
        """The prompts' owner: the first admin. Everyone reads them through
        the public grant; the owner is who may edit them in the Workspace."""
        if self._owner is None:
            from open_webui.models.users import Users
            user = None
            getter = getattr(Users, "get_super_admin_user", None)
            if getter is not None:
                user = await _owui_call(getter)
            if user is None:
                user = await _owui_call(Users.get_first_user)
            self._owner = getattr(user, "id", None)
        return self._owner

    async def list(self) -> dict:
        """Every prompt, turned off ones too: `get_prompts` leaves those out,
        but their command is still taken (it is unique across both)."""
        try:
            from open_webui.internal.db import get_async_db_context
            from open_webui.models.prompts import Prompt
            from sqlalchemy import select
        except ImportError:
            rows = await _owui_call(self._prompts.get_prompts) or []
        else:
            async with get_async_db_context() as session:
                rows = (await session.execute(select(Prompt))).scalars().all()
        return {r.command: {"id": r.id, "name": r.name, "content": r.content, "tags": r.tags or [],
                            "active": getattr(r, "is_active", True) is not False}
                for r in rows}

    async def find(self, command: str) -> "dict | None":
        """The prompt with `command`, whoever made it: {"id", "user_id", "meta"}."""
        row = await _owui_call(self._prompts.get_prompt_by_command, command)
        if row is None:
            return None
        return {"id": row.id, "user_id": row.user_id, "meta": row.meta or {}}

    def _form_for(self, want: dict):
        return self._form(command=want["command"], name=want["name"], content=want["content"],
                          tags=want["tags"], meta=want["meta"], data={},
                          access_grants=[{"principal_type": "user", "principal_id": "*",
                                          "permission": "read"}],
                          commit_message="Synced from the OpenClaw Gateway")

    async def add(self, want: dict) -> "str | None":
        owner = await self.owner_id()
        if not owner:
            return None
        row = await _owui_call(self._prompts.insert_new_prompt, owner, self._form_for(want))
        return getattr(row, "id", None)

    async def update(self, prompt_id: str, want: dict) -> bool:
        owner = await self.owner_id()
        row = await _owui_call(self._prompts.update_prompt_by_id, prompt_id, self._form_for(want), owner)
        return row is not None

    async def delete(self, prompt_id: str) -> bool:
        return bool(await _owui_call(self._prompts.delete_prompt_by_id, prompt_id))


async def _slash_prompts_apply(store, plan: dict, ledger: dict, save=None) -> bool:
    """Write `plan` to Open WebUI, recording each add and delete in the
    ledger file (`save`) as it lands: a prompt added but not recorded, by a
    restart mid-sync say, would be skipped as someone else's forever after.
    True when every write succeeded."""
    owned = ledger["prompts"]
    save = save or (lambda: None)
    ok = True
    for cmd in plan["forget"]:
        owned.pop(cmd, None)
    for cmd, pid in plan["delete"]:
        try:
            done = await store.delete(pid)
        except Exception as ex:
            pipe_log(f"[slash prompts] delete /{cmd} failed: {ex!r}")
            done = False
        if done:
            owned.pop(cmd, None)
            save()
        else:
            ok = False
    for want in plan["add"]:
        try:
            pid = await store.add(want)
        except Exception as ex:
            pipe_log(f"[slash prompts] add /{want['command']} failed: {ex!r}")
            pid = None
        if not pid:
            pid = await _slash_prompts_half_added(store, want)
        if pid:
            owned[want["command"]] = pid
            save()
        else:
            ok = False
    for pid, want in plan["update"]:
        try:
            done = await store.update(pid, want)
        except Exception as ex:
            pipe_log(f"[slash prompts] update /{want['command']} failed: {ex!r}")
            done = False
        ok = ok and done
    return ok


async def _slash_prompts_half_added(store, want: dict) -> "str | None":
    """The id of the prompt a failed add left behind, now recorded and written
    again; None when there is none.

    Open WebUI commits the prompt row before its read grant and first
    version, so an add can fail with the row in place. The command had no
    prompt when the plan was made, and the lock keeps other syncs out, so a
    row this sync's owner made with this Function's mark is that add's."""
    mark = want["meta"]["openclaw"]
    try:
        row = await store.find(want["command"])
        if row is None or row.get("user_id") != await store.owner_id():
            return None
        theirs = (row.get("meta") or {}).get("openclaw") or {}
        if (theirs.get("command"), theirs.get("function")) != (mark["command"], mark["function"]):
            return None
        # Writing it again sets the grant and the version the add did not.
        if not await store.update(row["id"], want):
            return None
    except Exception as ex:
        pipe_log(f"[slash prompts] could not check /{want['command']} after its add failed: {ex!r}")
        return None
    pipe_log(f"[slash prompts] /{want['command']}: recorded the prompt its failed add left")
    return row["id"]


def _slash_prompts_legacy() -> bool:
    at = _slash_prompts_state.get("legacy_at")
    return at is not None and time.monotonic() - at < _SLASH_PROMPTS_LEGACY_RETRY_S


async def _slash_prompts_fetch(conn, agent_id: str, revision) -> "tuple[list | None, str | None]":
    """The Gateway's commands and their revision; (None, revision) when the
    revision is unchanged.

    A Gateway older than 2026.10 takes neither `includeModels` nor
    `ifRevision`: there the full list comes from `commands.list` and the
    revision is its hash, so an unchanged list still writes nothing. Any
    other refusal (a wrong AGENT_ID, say) is an error, not an older Gateway."""
    if not _slash_prompts_legacy():
        params = {"agentId": agent_id, "includeModels": False}
        if revision:
            params["ifRevision"] = revision
        try:
            resp = await conn.send_request("chat.metadata", params, timeout=30)
        except GatewayError as ex:
            if not _SLASH_PROMPTS_OLD_GATEWAY.search(str(ex)):
                raise
            pipe_log(f"[slash prompts] chat.metadata refused its params ({ex}); using commands.list")
            _slash_prompts_state["legacy_at"] = time.monotonic()
        else:
            if resp.get("unchanged"):
                return None, resp.get("revision") or revision
            commands = resp.get("commands")
            if not isinstance(commands, list):
                raise GatewayError("chat.metadata returned no command list")
            return commands, resp.get("revision")
    resp = await conn.send_request("commands.list", {"agentId": agent_id, "scope": "text",
                                                     "includeArgs": True}, timeout=30)
    commands = resp.get("commands")
    if not isinstance(commands, list):
        raise GatewayError("commands.list returned no command list")
    digest = "list:" + hashlib.sha256(json.dumps(commands, sort_keys=True).encode()).hexdigest()
    return (None, digest) if digest == revision else (commands, digest)


async def _slash_prompts_sync(valves, conn_getter, store=None) -> str:
    """One check. Returns what it did, for the log and the tests: "busy",
    "unchanged", "synced", "partial", "removed", "off" or "error".

    A "busy" check leaves `pending` set, so the run that holds the lock, or
    `_slash_prompts_run` after its settle delay, checks again."""
    if _slash_prompts_lock.locked():
        _slash_prompts_state["pending"] = True
        return "busy"
    async with _slash_prompts_lock:
        lock_file = None
        try:
            try:
                import fcntl
            except ImportError:
                fcntl = None
            if fcntl is not None:
                lock_file = open(_slash_prompts_lock_path(), "a")
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    # Another Open WebUI worker, or another copy of the Pipe, is syncing.
                    _slash_prompts_state["pending"] = True
                    return "busy"
            # A trigger from here on is not covered by this check.
            _slash_prompts_state["pending"] = False
            return await _slash_prompts_sync_locked(valves, conn_getter, store)
        finally:
            if lock_file is not None:
                lock_file.close()


def _slash_prompts_lock_path():
    return os.path.join(_state_dir(), _SLASH_PROMPTS_LOCK_NAME)


async def _slash_prompts_sync_locked(valves, conn_getter, store) -> str:
    enabled = bool(getattr(valves, "SYNC_SLASH_COMMANDS", False))
    ledger = _slash_prompts_ledger_read()
    if not enabled:
        if not ledger["prompts"]:
            _slash_prompts_state["ledger_empty"] = True
            return "off"
        store = store or _OwuiPromptStore()
        try:
            existing = await store.list()
        except Exception as ex:
            pipe_log(f"[slash prompts] could not read Open WebUI's prompts: {ex!r}")
            return "error"
        plan = _slash_prompts_plan([], existing, dict(ledger["prompts"]))
        ok = await _slash_prompts_apply(store, plan, ledger, lambda: _slash_prompts_ledger_write(ledger))
        ledger["revision"] = None
        ledger["settings"] = None
        _slash_prompts_ledger_write(ledger)
        pipe_log(f"[slash prompts] sync off: removed {len(plan['delete'])} prompt(s)"
                 + ("" if ok else "; some deletes failed, retried next time"))
        return "removed" if ok else "partial"
    settings = _slash_prompts_settings(valves)
    revision = ledger.get("revision") if ledger.get("settings") == settings else None
    try:
        conn = await conn_getter()
        commands, new_revision = await _slash_prompts_fetch(conn, settings["agent"], revision)
    except Exception as ex:
        pipe_log(f"[slash prompts] could not read the Gateway's commands: {ex!r}")
        return "error"
    if commands is None:
        return "unchanged"
    store = store or _OwuiPromptStore()
    try:
        existing = await store.list()
    except Exception as ex:
        pipe_log(f"[slash prompts] could not read Open WebUI's prompts: {ex!r}")
        return "error"
    wanted = _slash_prompts_wanted(commands, settings)
    plan = _slash_prompts_plan(wanted, existing, dict(ledger["prompts"]))
    # Not a complete sync until the end: a restart mid-way runs it in full again.
    ledger["revision"] = None
    ok = await _slash_prompts_apply(store, plan, ledger, lambda: _slash_prompts_ledger_write(ledger))
    ledger["settings"] = settings
    # Only a complete sync records the revision; otherwise the next check
    # runs in full and retries what failed.
    ledger["revision"] = new_revision if ok else None
    _slash_prompts_ledger_write(ledger)
    for cmd in plan["skip"]:
        pipe_log(f"[slash prompts] /{cmd}: a prompt with this command exists and was not made "
                 "by the sync; left alone")
    pipe_log(f"[slash prompts] {len(wanted)} command(s): {len(plan['add'])} added, "
             f"{len(plan['update'])} updated, {len(plan['delete'])} removed, "
             f"{len(plan['skip'])} skipped" + ("" if ok else "; some writes failed, retried next time"))
    return "synced" if ok else "partial"


async def _slash_prompts_run(valves_getter) -> None:
    """A check, and one more if a trigger arrived while it ran or another
    worker held the lock. One per process: a trigger meanwhile only sets
    `pending`."""
    state = _slash_prompts_state
    state["running"] = True
    try:
        while True:
            state["checked_at"] = time.monotonic()
            try:
                await _slash_prompts_sync(valves_getter(),
                                          lambda: _get_gateway_connection(valves_getter))
            except Exception as ex:
                pipe_log(f"[slash prompts] check failed: {ex!r}")
                state["pending"] = False
            if not state["pending"]:
                return
            await asyncio.sleep(_SLASH_PROMPTS_PUSH_SETTLE_S)
    finally:
        state["running"] = False


def _slash_prompts_inside_owui() -> bool:
    try:
        import open_webui.models.prompts  # noqa: F401
        return True
    except Exception:
        return False


def _ensure_slash_prompts(valves_getter, force: bool = False) -> None:
    """Start a check when one is due. Cheap enough for every message: it
    compares settings and a timestamp, and any Gateway or Open WebUI call
    runs in the background. `force`: a page load or a Gateway push."""
    try:
        _ensure_slash_prompts_now(valves_getter, force)
    except Exception as ex:
        pipe_log(f"[slash prompts] could not start a check: {ex!r}")


def _ensure_slash_prompts_now(valves_getter, force: bool) -> None:
    state = _slash_prompts_state
    state["valves"] = valves_getter
    valves = valves_getter()
    enabled = bool(getattr(valves, "SYNC_SLASH_COMMANDS", False))
    if enabled:
        # Known again once a check runs: another worker's sync may fill the ledger.
        state["ledger_empty"] = None
    elif state["ledger_empty"]:
        return
    settings = (enabled, _slash_prompts_settings(valves))
    due = force or settings != state["settings"] or state["checked_at"] is None
    if not due and enabled:
        interval = max(1.0, float(getattr(valves, "SYNC_INTERVAL_MIN", 30) or 30)) * 60
        due = time.monotonic() - state["checked_at"] >= interval
    if not due:
        return
    state["settings"] = settings
    if state["running"] or _slash_prompts_lock.locked():
        state["pending"] = True
        return
    if not _slash_prompts_inside_owui():
        return
    state["running"] = True  # before the task starts, so a second trigger cannot start another
    try:
        spawn_background(_slash_prompts_run(valves_getter))
    except BaseException:
        state["running"] = False
        raise


def _slash_prompts_on_metadata_changed(payload: dict) -> None:
    """The Gateway's `chat.metadata.changed`: its commands may have changed."""
    if (payload or {}).get("commandsChanged") is False:
        return
    getter = _slash_prompts_state["valves"]
    if getter is None or not bool(getattr(getter(), "SYNC_SLASH_COMMANDS", False)):
        return
    _ensure_slash_prompts(getter, force=True)
