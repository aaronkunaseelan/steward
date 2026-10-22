from fixture_mind import use_settings
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from steward.state import StateError, WriterBusy, restore, writer


def competing_writer(root, queue):
    try:
        with writer(root):
            queue.put('unsafe')
    except WriterBusy:
        queue.put('busy')


class CoreTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'state'
        with writer(self.root, initialize=True):
            pass

    def test_single_writer_real_process(self):
        with writer(self.root):
            q = multiprocessing.Queue()
            p = multiprocessing.Process(target=competing_writer, args=(self.root,q))
            p.start(); p.join(5)
            self.assertEqual(q.get(timeout=2), 'busy')
            self.assertEqual(p.exitcode, 0)
        with writer(self.root):
            pass

    def test_wal_backup_restore_and_resume(self):
        backup = Path(self.tmp.name)/'backup'
        target = Path(self.tmp.name)/'restored'
        with writer(self.root) as store:
            with store.db:
                store.db.execute("INSERT INTO changes VALUES ('one','2026-10-20T00:00:00Z','owner','fixture','original',NULL)")
            store.backup(backup)
            with store.db:
                store.db.execute("UPDATE changes SET summary='changed'")
        self.assertEqual(restore(backup,target)['status'], 'restored_isolated')
        with writer(target) as store:
            self.assertEqual(store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(store.db.execute('SELECT summary FROM changes WHERE actor!=\'migration\'').fetchone()[0], 'original')
        with self.assertRaises(StateError):
            restore(backup,self.root)
        (backup/'state.sqlite').write_bytes(b'corrupt')
        with self.assertRaises(StateError):
            restore(backup,Path(self.tmp.name)/'bad')

    def test_schema14_removes_legacy_tables_preserving_log(self):
        with writer(self.root) as store:
            tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertFalse({'work', 'history'} & tables)
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 17)

    def test_schema15_drops_retirements_and_preserves_jobs(self):
        with writer(self.root) as store:
            store.db.execute('CREATE TABLE retirements (job TEXT PRIMARY KEY, justification TEXT, cutoff TEXT)')
            store.db.execute("INSERT INTO retirements VALUES ('old', 'reason', '2026-10-20')")
            store.db.execute('DROP TABLE moments'); store.db.execute("DELETE FROM changes WHERE actor=\'migration\'")
            store.db.execute('ALTER TABLE runs DROP COLUMN retry_of')
            store.db.execute('PRAGMA user_version=14')
            store.db.commit()
        with writer(self.root) as store:
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 17)
            self.assertIsNone(store.db.execute("SELECT name FROM sqlite_master WHERE name='retirements'").fetchone())
            self.assertEqual(store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_missing_state_and_unsupported_schema(self):
        with self.assertRaises(StateError):
            with writer(Path(self.tmp.name)/'absent'):
                pass
        db=sqlite3.connect(self.root/'state.sqlite')
        db.execute('PRAGMA user_version=999'); db.close()
        with self.assertRaises(StateError):
            with writer(self.root):
                pass


class WriterWaitTests(unittest.TestCase):
    setUp = use_settings

    """2026-10-17 live: a job launched by the tick failed WriterBusy; jobs and verbs now wait."""

    def test_waiting_writer_gets_lock_after_release_and_default_fails_fast(self):
        import threading, time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'state'
            with writer(root, initialize=True):
                pass
            held = threading.Event()
            def hold():
                with writer(root):
                    held.set()
                    time.sleep(1.5)
            t = threading.Thread(target=hold)
            t.start()
            held.wait()
            with self.assertRaises(WriterBusy):
                with writer(root):
                    pass
            start = time.monotonic()
            with writer(root, wait=10):
                self.assertGreater(time.monotonic() - start, 0.5)
            t.join()
