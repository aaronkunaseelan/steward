"""Jobs queue immediate notifications; the credentialed tick delivers them."""
from contextlib import redirect_stdout
import io
import json
import os
import unittest
from unittest.mock import patch

import test_jobs
from steward import cli, intent
from steward.notify import api as actual_api, flush, interrupt_data, interrupt_status, notification_status, notify
from steward.state import writer

NOW = test_jobs.NOW


class CredentiallessNotifyTests(unittest.TestCase):
    setUp = test_jobs.JobsTests.setUp

    def job_environment(self):
        self.enterContext(patch.dict(os.environ, STEWARD_JOB='parcel-watch'))
        os.environ.pop('STEWARD_TELEGRAM_BOT_TOKEN', None)
        self.enterContext(patch('steward.notify.binding', return_value={'chat_id': -1, 'topics': {'Talk': 6}}))
        return self.enterContext(patch('steward.notify.api', side_effect=AssertionError('provider attempted')))

    def test_immediate_talk_cli_queues_without_attempt_then_tick_sends_once(self):
        provider = self.job_environment()
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(['notify', '--state', str(self.root), '--topic', 'Talk',
                             '--kind', 'result', '--deliver', 'immediate', '--told-key', 'parcel:fixture',
                             'A parcel update'])
        self.assertEqual(code, 0)
        queued = json.loads(output.getvalue())
        self.assertEqual(queued['status'], 'pending')
        with writer(self.root) as st:
            row = st.db.execute('SELECT * FROM outbox WHERE id=?', (queued['outbox_id'],)).fetchone()
            self.assertEqual(row['state'], 'pending')
            self.assertEqual(row['job'], 'parcel-watch')
            self.assertEqual(row['delivery'], 'immediate')
            self.assertEqual(st.db.execute("SELECT count(*) FROM message_meta WHERE key LIKE 'outbox_error:%'").fetchone()[0], 0)
            self.assertEqual(notification_status(st.db)['pending'], [])
            provider.assert_not_called()
            sent = []
            def sender(topic, text, **options):
                sent.append((topic, text))
                return 41
            flush(st.db, sender=sender, now=NOW)
            flush(st.db, sender=sender, now=NOW)
            self.assertEqual(sent, [('Talk', 'A parcel update')])
            row = st.db.execute('SELECT state,message_id FROM outbox WHERE id=?', (queued['outbox_id'],)).fetchone()
            self.assertEqual(tuple(row), ('sent', 41))
            self.assertEqual(st.db.execute('SELECT message_id FROM told WHERE key=?', ('parcel:fixture',)).fetchone()[0], 41)
        provider.assert_not_called()

    def test_credentialless_tick_flush_records_visible_error(self):
        provider = self.job_environment()
        provider.side_effect = actual_api
        now = NOW.replace(hour=2)
        healthy = dict(kind='chain', layer='process', hops=[])
        with writer(self.root) as st, patch('steward.intent.chain', return_value=healthy), \
             patch('steward.intent.ticks', return_value={}), patch('urllib.request.urlopen') as transport:
            queued = notify(st.db, 'Talk', 'result', 'Queued for tick', told_key='tick:fixture',
                            now=now, deliver='immediate')
            self.assertEqual(queued['status'], 'pending')
            self.assertEqual(notification_status(st.db)['pending'], [])
            self.assertEqual(intent.steward_doc(st.db, now, {})['state'], 'green')
            flush(st.db, now=now)
            pending = notification_status(st.db)['pending']
            self.assertEqual(pending, [dict(outbox_id=queued['outbox_id'], topic='Talk',
                                           code='send_outcome_unknown', at=now.isoformat())])
            doc = intent.steward_doc(st.db, now, {})
            self.assertEqual(doc['state'], 'red')
            self.assertIn('send_outcome_unknown', doc['headline'])
            self.assertEqual(st.db.execute('SELECT state FROM outbox WHERE id=?',
                                          (queued['outbox_id'],)).fetchone()[0], 'pending')
            self.assertEqual(st.db.execute('SELECT count(*) FROM told').fetchone()[0], 0)
            transport.assert_not_called()
        provider.assert_called_once()

    def test_credentialed_immediate_send_still_uses_default_sender(self):
        provider = self.job_environment()
        provider.side_effect = None
        provider.return_value = {'message_id': 42}
        with patch.dict(os.environ, STEWARD_TELEGRAM_BOT_TOKEN='synthetic-fixture'), writer(self.root) as st:
            sent = notify(st.db, 'Talk', 'result', 'Credentialed tick', now=NOW, deliver='immediate')
            self.assertEqual(sent['status'], 'sent')
            self.assertEqual(sent['message_id'], 42)
        provider.assert_called_once()

    def test_credentialless_interrupt_does_not_reserve_a_send_or_budget(self):
        provider = self.job_environment()
        with writer(self.root) as st:
            queued = notify(st.db, 'Talk', 'result', 'An owner action', told_key='action:fixture',
                            now=NOW, deliver='immediate', interrupt='Fixture deadline')
            self.assertEqual(queued['status'], 'pending')
            row = st.db.execute('SELECT * FROM outbox WHERE id=?', (queued['outbox_id'],)).fetchone()
            self.assertIsNone(interrupt_data(row)['at'])
            self.assertEqual(interrupt_status(st.db, NOW)['used'], 0)
            self.assertEqual(notification_status(st.db)['pending'], [])
        provider.assert_not_called()
