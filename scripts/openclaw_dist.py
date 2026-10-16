"""Where the installed OpenClaw dist is, for the extract_openclaw_* scripts
and the fixture checks that re-run them: $OPENCLAW_DIST, else the global npm
install's (`npm root -g`/openclaw/dist), else "" (none found)."""
import os
import shutil
import subprocess


def default_dist() -> str:
    if os.environ.get("OPENCLAW_DIST"):
        return os.environ["OPENCLAW_DIST"]
    npm = shutil.which("npm")
    if not npm:
        return ""
    root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True).stdout.strip()
    return os.path.join(root, "openclaw", "dist") if root else ""
