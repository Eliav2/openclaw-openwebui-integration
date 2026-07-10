#!/usr/bin/env python3
"""One-time splitter: openclaw_pipe.py -> src/openclaw_pipe_pkg/ modules + frontmatter.

Partitions the original single file into ordered module fragments by assigning each
top-level AST node to a module. Every source line is accounted for exactly once
(leading blank/comment/divider lines attach to the node that follows them), so no
code can be silently dropped or duplicated. Reordering across modules is limited to
constants/defs with no module-load-time cross-references, which is order-independent.
"""
from __future__ import annotations
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "openclaw_pipe.py"
PKG = ROOT / "src" / "openclaw_pipe_pkg"

# name -> module. Unnamed nodes (imports/exprs) up to and including `logger`
# (lineno <= 86) go to the prelude; the docstring goes to frontmatter.
NAME_TO_MODULE = {
    # prelude
    "pipe_log": "_prelude", "logger": "_prelude",
    # identity
    "_generate_device_identity": "identity", "_sign_challenge": "identity",
    "_parse_device_identity": "identity",
    # state
    "_state_dir": "state", "_read_json_file": "state", "_write_json_file": "state",
    # media
    "MEDIA_DIR": "media", "MEDIA_BASE_URL": "media", "_file_server_started": "media",
    "_MEDIA_TRIGGER": "media", "_MEDIA_FNAME_RE": "media",
    "_looks_like_media_filename": "media", "_advance_media_buffer": "media",
    "_resolve_media": "media", "_extract_request_bearer": "media",
    "_multipart_body": "media", "_upload_owui_file": "media",
    "_resolve_media_via_owui": "media", "_resolve_media_text": "media",
    "_start_file_server": "media",
    # emit
    "_emit_status": "emit", "_emit_message_snapshot": "emit",
    # askuser
    "_USER_INPUT_TRIGGER_PREFIXES": "askuser", "_is_user_input_prompt": "askuser",
    "_could_be_user_input_prefix": "askuser", "_advance_input_prompt_buffer": "askuser",
    "_modal_payload_from_user_input_prompt": "askuser", "_ask_user_detail_block": "askuser",
    "UserInputResult": "askuser", "_normalize_event_call_response": "askuser",
    "_live_session_id_for_user": "askuser", "_retry_modal_on_reconnect": "askuser",
    "_ask_user_input_modal": "askuser",
    # gateway (GATEWAY_SCOPES is logically gateway though physically in media region)
    "GATEWAY_SCOPES": "gateway", "GatewayError": "gateway",
    "_preview_recovery_text": "gateway", "_owui_session_key": "gateway",
    "_owui_chat_send_params": "gateway", "_resolved_model_key": "gateway",
    "_model_patch_matches": "gateway", "_coerce_text": "gateway",
    "_item_assistant_text": "gateway", "_item_delta_text": "gateway",
    "_suppress_already_shown": "gateway", "_Consumer": "gateway",
    "_GatewayConnection": "gateway", "_gateway_connection": "gateway",
    "_gateway_init_lock": "gateway", "_get_gateway_connection": "gateway",
    # models
    "_FALLBACK_MODELS": "models", "_friendly_name": "models",
    "_provider_from_key": "models", "_normalize_model_entry": "models",
    "_parse_whitelist": "models", "_discover_models": "models",
    # pipe
    "Pipe": "pipe",
}

# Concat order for the built file (module-load statements have no cross-module refs).
MODULE_ORDER = ["_prelude", "identity", "state", "media", "emit",
                "askuser", "gateway", "models", "pipe"]


def node_name(n):
    name = getattr(n, "name", None)
    if name:
        return name
    if isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name):
                return t.id
    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
        return n.target.id
    return None


def main():
    lines = SRC.read_text().splitlines(keepends=True)
    tree = ast.parse("".join(lines))
    body = tree.body

    # Compute a contiguous block [start,end] (1-based, inclusive) for every node:
    # leading blanks/comments attach to the node that follows.
    blocks = []
    prev_end = 0
    for n in body:
        start = prev_end + 1
        end = n.end_lineno
        blocks.append((n, start, end))
        prev_end = end
    total = len(lines)
    assert prev_end == total, f"last node ends at {prev_end}, file has {total} lines"

    # Sanity: no gaps/overlaps, full coverage.
    covered = 0
    for _, s, e in blocks:
        covered += (e - s + 1)
    assert covered == total, f"coverage {covered} != {total}"

    frontmatter = None
    modules = {m: [] for m in MODULE_ORDER}
    for n, s, e in blocks:
        text = "".join(lines[s - 1:e])
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and s == 1:
            frontmatter = text  # module docstring
            continue
        if e <= 86:  # prelude region: imports, basicConfig, pipe_log, logger
            modules["_prelude"].append(text)
            continue
        name = node_name(n)
        mod = NAME_TO_MODULE.get(name)
        if mod is None:
            print(f"UNMAPPED node: {name!r} at {s}-{e} ({type(n).__name__})", file=sys.stderr)
            sys.exit(2)
        modules[mod].append(text)

    assert frontmatter is not None, "frontmatter docstring not found"

    PKG.mkdir(parents=True, exist_ok=True)
    (ROOT / "src" / "frontmatter.txt").write_text(frontmatter)
    (PKG / "__init__.py").write_text(
        '"""Development source for the OpenClaw OWUI pipe.\n\n'
        'Do NOT import this package into OWUI. OWUI loads the *built* single file\n'
        '`openclaw_pipe.py`, produced by `build.py` from these fragments.\n"""\n'
    )
    header = (
        "# ------------------------------------------------------------------\n"
        "# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.\n"
        "# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py\n"
        "# ------------------------------------------------------------------\n"
    )
    for mod in MODULE_ORDER:
        content = header + "\n" + "".join(modules[mod])
        if not content.endswith("\n"):
            content += "\n"
        (PKG / f"{mod}.py").write_text(content)
        print(f"wrote {mod}.py  ({len(modules[mod])} top-level nodes)")

    print(f"frontmatter.txt ({len(frontmatter.splitlines())} lines)")
    print("split OK — every source line accounted for")


if __name__ == "__main__":
    main()
