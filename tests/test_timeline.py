from fixture_mind import use_settings
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from steward import jobs, runtime
from steward.state import writer

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


class TimelineTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'state'
        self.store = self.enterContext(writer(self.root, initialize=True))
        self.db = self.store.db

    def test_revisions_are_logged_once_and_returning_to_a_sha_is_a_change(self):
        with patch('steward.runtime.release_sha', return_value='a' * 40):
            runtime.log_revisions(self.db, {'status': 'ok', 'sha': 'mind-a'}, NOW)
            runtime.log_revisions(self.db, {'status': 'ok', 'sha': 'mind-a'}, NOW)
        with patch('steward.runtime.release_sha', return_value='b' * 40):
            runtime.log_revisions(self.db, {'status': 'unknown'}, NOW)
        with patch('steward.runtime.release_sha', return_value='a' * 40):
            runtime.log_revisions(self.db, {'status': 'ok', 'sha': 'mind-b'}, NOW)
        rows = self.db.execute("SELECT verb,actor,summary FROM changes WHERE actor!='migration' ORDER BY rowid").fetchall()
        self.assertEqual([tuple(r) for r in rows], [
            ('release', 'tick', 'a' * 40), ('mind', 'tick', 'mind-a'),
            ('release', 'tick', 'b' * 40), ('release', 'tick', 'a' * 40),
            ('mind', 'tick', 'mind-b')])

    def test_wake_reads_only_its_cursor_and_interval(self):
        from steward.memory import wake_journal
        with patch('steward.runtime.release_sha', return_value='a'*40):
            runtime.log_revisions(self.db, {'status': 'ok', 'sha': 'mind-a'}, NOW)
        previous_cursor = self.db.execute('SELECT max(rowid) FROM changes').fetchone()[0]
        cid = jobs.change(self.db, 'memory write', NOW)
        self.db.execute("INSERT INTO memories VALUES ('synthesis','current',1,?,'then')", (cid,))
        cursor = self.db.execute('SELECT max(rowid) FROM changes').fetchone()[0]
        self.db.execute("INSERT INTO wakes(id,started_at,status,checkpoint,change_cursor) VALUES (1,?,'completed','{}',?)", (NOW.isoformat(), previous_cursor))
        self.db.execute("INSERT INTO wakes(id,started_at,status,checkpoint,change_cursor) VALUES (2,?,'completed','{}',?)", (NOW.isoformat(), cursor))
        cid = jobs.change(self.db, 'memory write', NOW)
        self.db.execute("INSERT INTO memories VALUES ('synthesis','current',2,?,'later')", (cid,))
        report = wake_journal(self.db, 2)
        self.assertEqual(report['release']['summary'], 'a'*40)
        self.assertEqual(report['mind']['summary'], 'mind-a')
        self.assertEqual(report['synthesis']['version'], 1)
        self.assertEqual([r['verb'] for r in report['changes']], ['memory write'])
        self.assertIsNone(wake_journal(self.db, 1)['synthesis'])
        self.assertEqual(report['previous_cursor'], previous_cursor)

    def test_release_identity_is_the_running_archive_not_current_symlink(self):
        with patch('steward.runtime.__file__', '/opt/steward/releases/' + 'a'*40 + '/src/steward/runtime.py'):
            self.assertEqual(runtime.release_sha(), 'a'*40)
