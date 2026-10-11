#!/usr/bin/env python3
"""Repoint chats whose saved model the Gateway no longer offers.

Why this is a script and not pipe logic
---------------------------------------
Open WebUI decides whether a model exists before the Pipe ever runs.
`chat_completion` (open_webui/main.py) raises "Model not found" for any id
missing from `app.state.MODELS`, which is built from `pipes()`. The browser
goes further: when a chat loads, `Chat.svelte` filters `chat.models` against
the live model list and silently substitutes the user's default (or the first
listed model). So a chat pinned to a model the Gateway dropped never reaches
the Pipe with that id, and a substitution table inside the Pipe would be dead
code. The only place the fix can live is the stored chat itself.

What it does
------------
For every chat of the signed-in user whose `models` list holds a
`<pipe_id>.<key>` the Pipe no longer lists, pick a successor from the CURRENT
model list (resolved live, nothing hardcoded per version, so the next model
bump needs a re-run, not a code change):

  1. a legacy preset (`.sonnet`, `.chatgpt`, ...) is first expanded to the key
     it always meant (the Pipe's `_LEGACY_PRESET_MAP`);
  2. the same model under another provider prefix
     (`claude-cli/claude-opus-5-5` -> `anthropic/claude-opus-5-5`);
  3. the newest listed model of the same Claude family (opus, sonnet, haiku);
  4. anything else (fable, retired non-Claude models, the bare pipe id) goes to
     `<pipe_id>.default`, which follows the Gateway's own default.

Only `chat.models` changes. Per-message `model` fields are history and stay as
they are. The update omits `history`, so OWUI does not bump `updated_at` and
the sidebar order is untouched.

Usage
-----
  OWUI_URL=http://localhost:8080 OWUI_EMAIL=... OWUI_PASSWORD=... \\
      python3 scripts/migrate_stale_chat_models.py            # dry run
  ... python3 scripts/migrate_stale_chat_models.py --apply    # writes a backup first
  ... python3 scripts/migrate_stale_chat_models.py --restore <backup.json>
"""

import argparse
import collections
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from owui_api import OwuiClient  # noqa: E402

DEFAULT_SUFFIX = "default"

# Mirrors Pipe._LEGACY_PRESET_MAP in src/openclaw_pipe_pkg/pipe.py. A unit test
# asserts the two stay equal.
LEGACY_PRESETS = {
    "chatgpt": "openai/gpt-5.5",
    "opus": "anthropic/claude-opus-4-8",
    "sonnet": "anthropic/claude-sonnet-5",
    "glm": "openrouter/z-ai/glm-5.2",
}

_CLAUDE_FAMILY_RE = re.compile(r"claude-(opus|sonnet|haiku)-([0-9][0-9-]*)")


def _model_name(key: str) -> str:
    """`anthropic/claude-opus-5-5` -> `claude-opus-5-5`."""
    return key.rsplit("/", 1)[-1]


def _provider(key: str) -> str:
    return key.split("/", 1)[0] if "/" in key else ""


def _claude_family(key: str):
    """Return (family, version tuple) for a Claude key, else None.

    The version keeps only short numeric parts, so a date-stamped id like
    `claude-haiku-4-5-20251001` ranks as 4.5, not above 5.5.
    """
    m = _CLAUDE_FAMILY_RE.search(_model_name(key))
    if not m:
        return None
    parts = [int(p) for p in m.group(2).split("-") if p.isdigit() and len(p) <= 3]
    return m.group(1), tuple(parts)


def resolve_successor(stale_key: str, live_keys) -> tuple[str, str]:
    """Pick the live key a chat pinned to `stale_key` should move to.

    `stale_key` and `live_keys` are pipe-relative (no `<pipe_id>.` prefix).
    Returns (successor_key, reason).
    """
    live = [k for k in live_keys if k != DEFAULT_SUFFIX]
    if stale_key in live_keys:
        return stale_key, "still listed"
    if not stale_key:
        return DEFAULT_SUFFIX, "bare pipe id"

    key = stale_key
    reason_prefix = ""
    if key in LEGACY_PRESETS:
        key = LEGACY_PRESETS[key]
        reason_prefix = f"legacy preset -> {key}; "
        if key in live:
            return key, reason_prefix + "still listed"

    same_name = [k for k in live if _model_name(k) == _model_name(key)]
    if len(same_name) == 1:
        return same_name[0], reason_prefix + "same model, other provider"

    family = _claude_family(key)
    if family:
        candidates = []
        for k in live:
            fam = _claude_family(k)
            if fam and fam[0] == family[0]:
                same_provider = _provider(k) == _provider(key)
                candidates.append((fam[1], same_provider, k))
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][2], reason_prefix + f"newest listed {family[0]}"

    return DEFAULT_SUFFIX, reason_prefix + "no listed successor"


def plan_migration(chats, live_model_ids, pipe_id):
    """Return (changes, foreign_counts) without touching anything.

    `changes` is a list of dicts {id, title, old, new, reasons}. Ids that do not
    belong to this pipe (another function, a direct connection) are counted in
    `foreign_counts` and never rewritten.
    """
    prefix = pipe_id + "."
    live_keys = {m[len(prefix):] for m in live_model_ids if m.startswith(prefix)}
    changes = []
    foreign = collections.Counter()
    for chat in chats:
        old = list(((chat.get("chat") or {}).get("models")) or [])
        new, reasons = [], []
        for model_id in old:
            if model_id in live_model_ids:
                new.append(model_id)
                continue
            if model_id == pipe_id:
                stale = ""
            elif model_id.startswith(prefix):
                stale = model_id[len(prefix):]
            else:
                foreign[model_id] += 1
                new.append(model_id)
                continue
            successor, reason = resolve_successor(stale, live_keys)
            new.append(prefix + successor)
            reasons.append(reason)
        if new != old:
            changes.append({
                "id": chat.get("id"),
                "title": chat.get("title", ""),
                "old": old,
                "new": new,
                "reasons": reasons,
            })
    return changes, foreign


# ── OWUI I/O ─────────────────────────────────────────────────────────

def _fetch_all_chats(client: OwuiClient) -> list[dict]:
    """All of the signed-in user's chats, archived included.

    OWUI 0.11 streams `/api/v1/chats/all` as NDJSON; older releases return one
    JSON array. Accept both.
    """
    req = urllib.request.Request(
        f"{client.base_url}/api/v1/chats/all", headers=client._auth_headers()
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8", "replace").strip()
    if raw.startswith("["):
        return json.loads(raw)
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _live_model_ids(client: OwuiClient) -> set[str]:
    data = client._get("/api/models")
    rows = data.get("data", data) if isinstance(data, dict) else data
    return {m["id"] for m in rows}


def _set_models(client: OwuiClient, chat_id: str, models: list[str]) -> list[str]:
    # No `history` key -> OWUI's update_chat_by_id leaves updated_at alone.
    resp = client._post(f"/api/v1/chats/{chat_id}", {"chat": {"models": models}})
    return ((resp or {}).get("chat") or {}).get("models") or []


def _print_plan(changes, foreign):
    by_pair = collections.Counter(
        (" , ".join(c["old"]), " , ".join(c["new"])) for c in changes
    )
    print(f"{len(changes)} chat(s) to repoint:")
    for (old, new), n in by_pair.most_common():
        print(f"  {n:4d}  {old}  ->  {new}")
    if foreign:
        print("Left alone (not this pipe's ids):")
        for model_id, n in foreign.most_common():
            print(f"  {n:4d}  {model_id}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pipe-id", default="openclaw_gateway",
                    help="OWUI function id of the pipe (default: openclaw_gateway)")
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    ap.add_argument("--backup", default="",
                    help="backup path for --apply (default: ./stale-model-backup-<ts>.json)")
    ap.add_argument("--restore", metavar="BACKUP",
                    help="put back the models saved in a backup written by --apply")
    args = ap.parse_args(argv)

    client = OwuiClient.from_env()
    client.login()

    if args.restore:
        with open(args.restore) as f:
            saved = json.load(f)
        for row in saved["chats"]:
            _set_models(client, row["id"], row["chat"].get("models") or [])
        print(f"restored models on {len(saved['chats'])} chat(s) from {args.restore}")
        return 0

    chats = _fetch_all_chats(client)
    live = _live_model_ids(client)
    changes, foreign = plan_migration(chats, live, args.pipe_id)
    print(f"{len(chats)} chats scanned, {len(live)} models listed.")
    _print_plan(changes, foreign)

    if not args.apply:
        print("\nDry run, nothing written. Re-run with --apply to repoint these chats.")
        return 0
    if not changes:
        return 0

    backup = args.backup or f"stale-model-backup-{int(time.time())}.json"
    by_id = {c["id"]: c for c in chats}
    with open(backup, "w") as f:
        json.dump({"pipe_id": args.pipe_id, "created_at": time.time(),
                   "chats": [by_id[c["id"]] for c in changes]}, f, ensure_ascii=False)
    print(f"\nbackup of {len(changes)} chat(s) written to {backup}")

    failed = 0
    for c in changes:
        try:
            if _set_models(client, c["id"], c["new"]) != c["new"]:
                raise RuntimeError("update did not stick")
        except Exception as ex:
            failed += 1
            print(f"  FAILED {c['id']}: {ex}")
    print(f"repointed {len(changes) - failed} chat(s), {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
