"""Where a string shows up in the Gateway's session transcripts, for the
secret test (`_hits` in e2e/test_e2e.py).

A transcript is `*.jsonl` up to OpenClaw 2026.9 and, from 2026.10, one SQLite
database per agent (`agents/<id>/agent/openclaw-agent.sqlite`) that the
Gateway writes and checkpoints while the test reads it. Counting a live
database's raw bytes file by file is not one read: a checkpoint between the
reads of the database and its WAL moves what the test looks for from one into
the other. So a database is read as SQLite reads it, into a snapshot taken in
one read transaction, and every agent's database is its own place: `main` and
`title-gen` both have a file named `openclaw-agent.sqlite`, and a place keyed
by file name alone held only the last one walked.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

DATABASE = "openclaw-agent.sqlite"


def transcript_files(root: str) -> dict[str, str]:
    """Every transcript file under `root` (`~/.openclaw/agents`), by its path
    relative to it. A database's WAL and SHM are not listed: the snapshot of
    the database holds what is in them."""
    found = {}
    for folder, _, names in sorted(os.walk(root)):
        for name in sorted(names):
            if name.endswith(".jsonl") or name == DATABASE:
                path = os.path.join(folder, name)
                found[os.path.relpath(path, root)] = path
    return found


def snapshot(path: str, into: str) -> None:
    """The database at `path` as one read transaction sees it, written to
    `into` (a closed file the backup replaces)."""
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        target = sqlite3.connect(into)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def count_in_file(path: str, needle: bytes) -> dict:
    """How often `needle` is in the file, and how it was read: {count, how,
    bytes, events}. `events` is a database's transcript event count, so a miss
    shows whether the database was empty or only lacked the string."""
    if os.path.basename(path) != DATABASE:
        with open(path, "rb") as fh:
            data = fh.read()
        return {"count": data.count(needle), "how": "file", "bytes": len(data)}
    with tempfile.TemporaryDirectory() as folder:
        copy = os.path.join(folder, DATABASE)
        snapshot(path, copy)
        with open(copy, "rb") as fh:
            data = fh.read()
        db = sqlite3.connect(copy)
        try:
            events = db.execute("SELECT count(*) FROM transcript_events").fetchone()[0]
        finally:
            db.close()
    return {"count": data.count(needle), "how": "snapshot", "bytes": len(data), "events": events}


def transcript_hits(root: str, needle: bytes) -> dict[str, dict]:
    """{transcript file relative to `root`: count_in_file(...)} for each one."""
    return {name: count_in_file(path, needle) for name, path in transcript_files(root).items()}
