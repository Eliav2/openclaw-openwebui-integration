"""The Open WebUI release under test, for tests whose expected outcome depends on it.

The pinned lane runs the release in e2e/versions.env; the compat watch runs `latest`. A test that
documents an upstream bug (a strict xfail) must expect the bug only on releases that have it, so
both lanes can be green: the same test is red-by-design on the old release and a plain guard on the
fixed one.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

# The first release with each upstream fix (ELI-195: open-webui#32744, ELI-205: open-webui#32617).
FIXED_IN_0_12_4 = (0, 12, 4)


def owui_version() -> tuple:
    """(major, minor, patch) from the stack's /api/version; a very large tuple when the stack
    cannot be reached (the e2e tests are skipped then, so only collection runs)."""
    base = os.environ.get("OWUI_URL", "http://localhost:8080").rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/version", timeout=3) as resp:
            version = json.load(resp)["version"]
        m = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    except Exception:
        return (9999, 0, 0)


def has_fix_0_12_4() -> bool:
    return owui_version() >= FIXED_IN_0_12_4
