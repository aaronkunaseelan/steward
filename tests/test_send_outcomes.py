"""BR2 counterexamples; fixtures only, never production state or providers."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fixture_mind import install
from steward import calendar_b, intent, jobs, messages, pages, planner, runtime, senses, timing
from steward.capabilities import told_check
from steward.notify import notification_status, notify, flush
from steward.state import writer


class SendOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        install(self, Path(self.tmp.name))
        self.root = Path(self.tmp.name) / 'state'
        with writer(self.root, initialize=True):
            pass
        self.now = datetime(2026, 10, 21, 4, tzinfo=timezone.utc)
        self.sent = []

    def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return len(self.sent)


    def test_outbound_transport_failure_is_not_green(self):
        now = self.now.replace(hour=2)
        healthy = dict(kind='chain', layer='process', hops=[
            dict(name='fixture', lag_s=0, note='fresh', warn=False)])
        with writer(self.root) as st, patch('steward.intent.chain', return_value=healthy), patch('steward.intent.ticks', return_value={}):
            self.assertEqual(intent.steward_doc(st.db, now, {})['state'], 'green')
            def fail(*args, **kwargs):
                raise OSError('SECRET fixture exception')
            queued = notify(st.db, 'Talk', 'result', 'PRIVATE reply', 'reply:one', fail, now)
            self.assertEqual(queued['status'], 'pending')
            status = notification_status(st.db)
            self.assertEqual(status['pending'][0]['code'], 'send_outcome_unknown')
            self.assertNotIn('SECRET', json.dumps(status))
            self.assertNotIn('PRIVATE', json.dumps(status))
            doc = intent.steward_doc(st.db, now, {})
            self.assertEqual(doc['state'], 'red')
            self.assertIn('send_outcome_unknown', doc['headline'])
            self.assertTrue(told_check(st.db, ['reply:one'])['reply:one'])
            flush(st.db, self.send, now)
            self.assertEqual(notification_status(st.db)['pending'], [])
            self.assertEqual(intent.steward_doc(st.db, now, {})['state'], 'green')

    def test_interrupted_send_is_visible_and_digest_queue_is_not_failure(self):
        with writer(self.root) as st:
            notify(st.db, 'Finds', 'result', 'A digest', 'digest:one', self.send, self.now)
            self.assertEqual(notification_status(st.db).get('pending', []), [])
            def interrupted(*args, **kwargs):
                # Like a process interruption after the provider may have accepted.
                raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                notify(st.db, 'Talk', 'result', 'A reply', 'reply:two', interrupted, self.now)
            pending = notification_status(st.db)['pending']
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]['code'], 'send_outcome_unknown')

    def test_only_pending_and_sent_keys_count_as_told(self):
        with writer(self.root) as st:
            row = notify(st.db, 'Finds', 'result', 'Not to send', 'silent:key', self.send, self.now, deliver='silent')
            self.assertFalse(told_check(st.db, ['silent:key'])['silent:key'])
            queued = notify(st.db, 'Finds', 'result', 'Queued', 'pending:key', self.send, self.now)
            self.assertTrue(told_check(st.db, ['pending:key'])['pending:key'])
            st.db.execute("UPDATE outbox SET state='failed' WHERE id=?", (queued['outbox_id'],))
            self.assertFalse(told_check(st.db, ['pending:key'])['pending:key'])
