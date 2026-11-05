"""How the secret test reads session transcripts (e2e/transcript_scan.py) while the
Gateway checkpoints the WAL, with `main` and `title-gen` files of the same name."""

import importlib.util
import os
import sqlite3

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "e2e", "transcript_scan.py")
_spec = importlib.util.spec_from_file_location("e2e_transcript_scan", _PATH)
scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan)

NEEDLE = b"DUMMY_SECRET_ELI96_ABC123"


def _agent(root, agent, text):
    """The agent's database as the Gateway keeps it: WAL, never checkpointed
    while it is open. Returns (path, the open connection)."""
    folder = os.path.join(root, agent, "agent")
    os.makedirs(folder)
    path = os.path.join(folder, scan.DATABASE)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("CREATE TABLE transcript_events (event_json TEXT)")
    db.execute("INSERT INTO transcript_events VALUES (?)", (text,))
    db.commit()
    return path, db


def _old_way(path, needle, between):
    """What the test did: the database file, then its WAL, as two reads."""
    with open(path, "rb") as fh:
        found = fh.read().count(needle)
    between()
    with open(path + "-wal", "rb") as fh:
        return found + fh.read().count(needle)


def test_each_agents_database_is_its_own_place(tmp_path):
    _, main = _agent(str(tmp_path), "main", f"user: E2E_SECRET_REQUEST {NEEDLE.decode()}")
    _, title = _agent(str(tmp_path), "title-gen", "user: a title please")
    got = scan.transcript_hits(str(tmp_path), NEEDLE)
    sep = os.sep
    assert sorted(got) == [f"main{sep}agent{sep}openclaw-agent.sqlite", f"title-gen{sep}agent{sep}openclaw-agent.sqlite"]
    assert got[f"main{sep}agent{sep}openclaw-agent.sqlite"]["count"] == 1
    assert got[f"title-gen{sep}agent{sep}openclaw-agent.sqlite"]["count"] == 0
    assert {f["how"] for f in got.values()} == {"snapshot"}
    assert {f["events"] for f in got.values()} == {1}
    main.close()
    title.close()


def test_what_is_only_in_the_wal_is_found(tmp_path):
    path, db = _agent(str(tmp_path), "main", f"tool call {NEEDLE.decode()}")
    with open(path, "rb") as fh:
        assert fh.read().count(NEEDLE) == 0, "the row should be in the WAL only"
    assert scan.count_in_file(path, NEEDLE)["count"] == 1
    db.close()


def test_a_checkpoint_between_the_two_file_reads_hides_the_row_from_the_old_way_not_from_the_snapshot(tmp_path):
    path, db = _agent(str(tmp_path), "main", f"tool call {NEEDLE.decode()}")

    def checkpoint():
        # The Gateway's periodic WAL maintenance: the row moves into the
        # database file and the WAL is emptied.
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    assert _old_way(path, NEEDLE, checkpoint) == 0, "the row was in neither file when each was read"
    assert scan.count_in_file(path, NEEDLE)["count"] == 1, "it was in the database all along"
    db.close()


def test_a_jsonl_transcript_is_read_as_a_file(tmp_path):
    folder = tmp_path / "main" / "sessions"
    folder.mkdir(parents=True)
    (folder / "s.jsonl").write_text(f'{{"text": "{NEEDLE.decode()}"}}\n')
    (folder / "ignored.txt").write_text(NEEDLE.decode())
    got = scan.transcript_hits(str(tmp_path), NEEDLE)
    assert list(got) == [os.path.join("main", "sessions", "s.jsonl")]
    assert got[os.path.join("main", "sessions", "s.jsonl")]["count"] == 1
    assert got[os.path.join("main", "sessions", "s.jsonl")]["how"] == "file"
