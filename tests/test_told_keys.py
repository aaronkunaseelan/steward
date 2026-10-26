import io
from datetime import timedelta
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import test_jobs
from test_jobs import NOW
from steward.capabilities import told_check
from steward.cli import main
from steward.notify import flush, notify
from steward.state import StateError, writer


class ManyToldKeys(unittest.TestCase):
    setUp = test_jobs.JobsTests.setUp
    tearDown = test_jobs.JobsTests.tearDown

    def test_pending_overlap_delivery_and_retry(self):
        with writer(self.root) as st:
            db = st.db
            fail = lambda *a, **kw: (_ for _ in ()).throw(OSError())
            first = notify(db, 'Talk', 'one page', ['a', 'b', 'a'], sender=fail, now=NOW)
            self.assertEqual(first['status'], 'pending')
            self.assertEqual(told_check(db, ['a', 'b', 'c']), {'a':True, 'b':True, 'c':False})
            self.assertEqual(db.execute('SELECT count(*) FROM told').fetchone()[0], 0)
            self.assertEqual(notify(db, 'Talk', 'overlap', ['c', 'b'], now=NOW)['status'], 'already_told')
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)
            flush(db, sender=lambda *a, **kw: 42, now=NOW)
            rows = [dict(r) for r in db.execute('SELECT * FROM told ORDER BY key')]
            self.assertEqual([r['key'] for r in rows], ['a', 'b'])
            self.assertTrue(all(r['message_id'] == 42 for r in rows))
            self.assertEqual(notify(db, 'Talk', 'overlap', ['b', 'd'], now=NOW)['status'], 'already_told')

    def test_cli_repeatable_and_single_key_compatibility(self):
        output = io.StringIO()
        with patch('steward.notify.telegram_sender', return_value=7), redirect_stdout(output):
            # Digest avoids any outward call.
            code = main(['notify', '--state', str(self.root), '--topic', 'Finds', '--told-key', 'a', '--told-key', 'b', 'one page'])
        self.assertEqual(code, 0)
        with writer(self.root) as st:
            self.assertEqual(told_check(st.db, ['a', 'b']), {'a':True, 'b':True})
            notify(st.db, 'Talk', 'legacy caller', 'legacy', sender=lambda *a, **options: 8, now=NOW)
            self.assertEqual(st.db.execute("SELECT message_id FROM told WHERE key='legacy'").fetchone()[0], 8)

    def test_v12_pending_message_survives_migration(self):
        with writer(self.root) as st:
            notify(st.db, 'Finds', 'legacy queued', 'old-key', deliver='digest', now=NOW)
            st.db.execute("UPDATE outbox SET told_key='old-key'")  # Legacy rows store the key directly rather than in outbox_keys.
            st.db.execute('DROP TABLE outbox_keys')
            st.db.execute('ALTER TABLE pages DROP COLUMN public')
            st.db.execute('DROP TABLE moments'); st.db.execute("DELETE FROM changes WHERE actor=\'migration\'")
            st.db.execute('ALTER TABLE runs DROP COLUMN retry_of')
            st.db.execute("ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'result'")
            st.db.execute('PRAGMA user_version=12')
            st.db.commit()
        with writer(self.root) as st:
            self.assertEqual(st.db.execute('PRAGMA user_version').fetchone()[0], 20)
            self.assertEqual(told_check(st.db, ['old-key']), {'old-key': True})
            self.assertIsNone(st.db.execute('SELECT told_key FROM outbox').fetchone()[0])
            flush(st.db, sender=lambda *a, **kw: 99, now=NOW + timedelta(days=1))
            self.assertEqual(st.db.execute("SELECT message_id FROM told WHERE key='old-key'").fetchone()[0], 99)

    def test_url_keys_are_canonical_at_write_and_check(self):
        original = 'https://EXAMPLE.com/event?id=7&utm_source=x&fbclid=abc#details'
        canonical = 'https://example.com/event?id=7'
        with writer(self.root) as st:
            notify(st.db, 'Finds', 'event', original, deliver='digest', now=NOW)
            self.assertEqual(told_check(st.db, [canonical, original]), {canonical: True, original: True})
            self.assertEqual(notify(st.db, 'Finds', 'again', canonical, now=NOW)['status'], 'already_told')
            self.assertEqual(st.db.execute('SELECT key FROM outbox_keys').fetchone()[0], canonical)

    def test_invalid_keys(self):
        with writer(self.root) as st:
            for value in [['a', ''], ['a', 2], 2]:
                with self.subTest(value=value), self.assertRaisesRegex(StateError, '^invalid told key$'):
                    notify(st.db, 'Talk', 'page', value, now=NOW,
                           sender=lambda *a, **kw: self.fail('invalid key delivered'))
            self.assertEqual(st.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)
            self.assertEqual(st.db.execute("SELECT count(*) FROM changes WHERE actor!='migration'").fetchone()[0], 0)
