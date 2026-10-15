#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "click>=8.1",
#     "rich>=13",
# ]
# ///
"""
Install/update the OpenClaw Thinking companion Filter Function in Open WebUI.

A spec for install.py's installer core (ELI-146). The Filter has no valves to
copy from the Pipe, and unlike the Action it is never made global: a global
filter would write ``reasoning_effort`` into every chat with every model,
including non-OpenClaw ones (see src/frontmatter-filter.txt). Attaching it to
the OpenClaw models stays a manual step (Workspace > Models > (model) >
Filters), as the README documents. It must be run from a clone: it imports
install.py.

Usage:
    uv run install_filter.py install
    uv run install_filter.py status
"""

from __future__ import annotations

from install import ROOT, FunctionSpec, function_cli

SPEC = FunctionSpec(
    "openclaw_thinking", "OpenClaw Thinking", "filter", "Filter",
    "Per-chat thinking / reasoning-effort control for the OpenClaw Gateway pipe "
    "-- companion Filter to the Gateway Pipe",
    artifact=ROOT / "openclaw_thinking_filter.py", build_artifact="FILTER",
    global_=False,
    global_warning=(
        "Filter is global -- it will write reasoning_effort into every chat "
        "with every model. Consider Admin Panel > Functions > toggle Global "
        "off, then attach it per-model instead (Workspace > Models > (model) "
        "> Filters)."
    ),
)
cli = function_cli(SPEC)

if __name__ == "__main__":
    cli()
