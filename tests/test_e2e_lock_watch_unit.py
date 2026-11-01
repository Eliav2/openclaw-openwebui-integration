#!/usr/bin/env python3
"""e2e/conftest.py's lock watch: a "database is locked" line ends the run only when another process
still cannot take the write lock; a lock that cleared is recorded and the run goes on."""

import importlib.util
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("e2e_conftest", ROOT / "e2e" / "conftest.py")
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

LINE = "2026-11-01 11:15:52.008 | ERROR | open_webui.main:process_chat - sqlite3.OperationalError: database is locked\n"


class LockLineTests(unittest.TestCase):

    def setUp(self):
        watch._state.update(test="e2e/x.py::test_y", line=None, during=None, cleared=[])

    def test_a_lock_still_held_ends_the_run_naming_the_test_and_line(self):
        self.assertTrue(watch._on_lock_line(LINE, still_locked=lambda: True))
        self.assertEqual(watch._state["during"], "e2e/x.py::test_y")
        self.assertIn("database is locked", watch._state["line"])
        self.assertIn("still was 10 s later", watch._reason())
        self.assertEqual(watch._state["cleared"], [])

    def test_a_lock_that_cleared_is_recorded_and_the_run_goes_on(self):
        self.assertFalse(watch._on_lock_line(LINE, still_locked=lambda: False))
        self.assertIsNone(watch._state["line"])
        self.assertEqual(watch._state["cleared"], [("e2e/x.py::test_y", LINE.strip())])


class StillLockedTests(unittest.TestCase):
    """_still_locked runs its check in the container; here the same check runs on a local file."""

    def run_locally(self, db: str):
        real = subprocess.run

        def fake(argv, **kw):
            self.assertEqual(argv[:5], ["docker", "exec", watch.CONTAINER, "python3", "-c"])
            code = argv[5].replace(repr(watch.DB), repr(db)).replace(f"timeout={watch.LOCK_WAIT_S}", "timeout=0.2")
            return real([sys.executable, "-c", code], **kw)
        return fake

    def db(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "webui.db")
        sqlite3.connect(path).execute("CREATE TABLE t (x)").connection.commit()
        return path

    def test_a_free_database_is_not_locked(self):
        db = self.db()
        orig, watch.subprocess.run = watch.subprocess.run, self.run_locally(db)
        try:
            self.assertFalse(watch._still_locked())
        finally:
            watch.subprocess.run = orig

    def test_a_held_write_lock_is_locked(self):
        db = self.db()
        holder = sqlite3.connect(db)
        holder.execute("BEGIN IMMEDIATE")
        self.addCleanup(holder.close)
        orig, watch.subprocess.run = watch.subprocess.run, self.run_locally(db)
        try:
            self.assertTrue(watch._still_locked())
        finally:
            watch.subprocess.run = orig

    def test_a_check_that_cannot_run_counts_as_locked(self):
        def boom(*a, **kw):
            raise FileNotFoundError("docker")
        orig, watch.subprocess.run = watch.subprocess.run, boom
        try:
            self.assertTrue(watch._still_locked())
        finally:
            watch.subprocess.run = orig


if __name__ == "__main__":
    unittest.main()
