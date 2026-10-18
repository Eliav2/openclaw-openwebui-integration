"""The E2E stack's Open WebUI dump (e2e/owui_diag/sitecustomize.py, ELI-204)
names the task that holds a sqlite transaction open."""

import asyncio
import importlib.util
import os
import signal
import sqlite3
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_diag():
    """The hook under another module name; it registers SIGUSR1 on import."""
    before = signal.getsignal(signal.SIGUSR1)
    spec = importlib.util.spec_from_file_location(
        "owui_diag_under_test", os.path.join(ROOT, "e2e", "owui_diag", "sitecustomize.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    signal.signal(signal.SIGUSR1, before)
    return mod


class OwuiDiagDumpTests(unittest.TestCase):
    def test_the_dump_names_the_task_holding_a_write_transaction(self):
        diag = _load_diag()
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "webui.db")
            sqlite3.connect(db).close()
            diag.OUT_DIR, diag.DB_PATH = os.path.join(tmp, "out"), db

            async def holds_the_write_lock(started, release):
                conn = sqlite3.connect(db, isolation_level=None)
                conn.execute("BEGIN IMMEDIATE")
                started.set()
                await release.wait()
                conn.rollback()
                conn.close()

            async def main():
                started, release = asyncio.Event(), asyncio.Event()
                holder = asyncio.create_task(holds_the_write_lock(started, release), name="the-holder")
                await started.wait()
                diag._dump(signal.SIGUSR1, None)  # the handler runs on the loop's thread
                release.set()
                await holder

            asyncio.run(main())
            (name,) = os.listdir(diag.OUT_DIR)
            with open(os.path.join(diag.OUT_DIR, name)) as fh:
                text = fh.read()
        self.assertRegex(name, r"^dump-\d+-\d+\.txt$")
        self.assertIn("1 inside a transaction", text)
        tasks = text.split("===== asyncio tasks =====")[1].split("=====")[0]
        holder = tasks.split("-- task the-holder")[1].split("-- task ")[0]
        self.assertIn("in holds_the_write_lock", holder)
        self.assertIn("in wait", holder, "the chain goes down to what the holder awaits")
        locks = text.split("===== locks on the database files =====")[1]
        self.assertRegex(locks, r"RESERVED \(the writer\): pid \d+ \(this process\)")


if __name__ == "__main__":
    unittest.main()
