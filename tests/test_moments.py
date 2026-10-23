from fixture_mind import TEST_CONFIG
from fixture_mind import use_settings
from fixture_mind import TEST_ZONE
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from steward import jobs, timing
from steward.state import writer, StateError


class MomentTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'state'
        self.now = datetime(2026, 10, 20, 4, tzinfo=timezone.utc)

    def test_notify_moment_builds_key_and_rejects_unknown_before_effects(self):
        from steward.notify import notify
        with writer(self.root, initialize=True) as st:
            now = self.now.replace(hour=15, minute=40)
            notify(st.db, 'Today', 'result', 'Day close', sender=lambda *a, **options: 1,
                   now=now, moment='day_close')
            self.assertEqual(timing.moment_obligation(st.db, jobs.get(st.db, 'moments', 'day_close'), now)['state'], 'kept')
            self.assertEqual(st.db.execute('SELECT key FROM told').fetchone()[0], 'day_close:2026-10-20')
            before = st.db.execute('SELECT count(*) FROM outbox').fetchone()[0]
            with self.assertRaises(StateError):
                notify(st.db, 'Today', 'result', 'Unknown', sender=lambda *a, **options: 1,
                       now=now, moment='missing')
            with self.assertRaises(StateError):
                notify(st.db, 'Talk', 'result', 'Wrong route', sender=lambda *a, **options: 1,
                       now=now, moment='day_close')
            self.assertEqual(st.db.execute('SELECT count(*) FROM outbox').fetchone()[0], before)

    def test_cli_notify_moment(self):
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from steward import cli
        import io
        with writer(self.root, initialize=True):
            pass
        config = Path(self.tmp.name) / 'config.toml'
        config.write_text(TEST_CONFIG)
        now = self.now.replace(hour=15, minute=40)
        with patch.dict('os.environ', STEWARD_TELEGRAM_BOT_TOKEN='synthetic-fixture'), \
             patch('steward.notify.binding', return_value={'chat_id': -1, 'topics': {'Today': 2}}), \
             patch('steward.notify.api', return_value={'message_id': 1}) as send, \
             patch('steward.notify.now_utc', return_value=now), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['notify', '--state', str(self.root), '--config', str(config),
                                      '--topic', 'Today', '--kind', 'result', '--moment', 'day_close', 'Close']), 0)
        self.assertEqual(send.call_count, 1)
        with writer(self.root) as st:
            self.assertEqual(timing.moment_obligation(st.db, jobs.get(st.db, 'moments', 'day_close'), now)['state'], 'kept')

    def test_completed_model_wake_stops_retry_but_failed_wake_does_not(self):
        import json
        from datetime import timedelta
        from steward import intent
        with writer(self.root, initialize=True) as st:
            now = self.now.replace(hour=15, minute=40)
            checkpoint = json.dumps(dict(model_calls=1, ended_at=now.isoformat(), briefing=dict(complete=True)))
            st.db.execute("INSERT INTO wakes(started_at,status,checkpoint,harness_run_id) VALUES (?,'failed',?,'model')", (now.isoformat(), checkpoint))
            retry = now + timedelta(minutes=20)
            self.assertIn('day_close', timing.due(st.db, retry))
            st.db.execute("UPDATE wakes SET status='completed'")
            self.assertNotIn('day_close', timing.due(st.db, retry))
            promises = next(b['promises'] for b in intent.steward_doc(st.db, now, {})['blocks'] if b['kind'] == 'day')
            self.assertEqual(next(p['state'] for p in promises if p['label'] == 'Day close'), 'missed')

    def test_wake_that_started_before_the_moment_does_not_stop_it(self):
        import json
        with writer(self.root, initialize=True) as st:
            started, ended = self.now.replace(hour=15, minute=27), self.now.replace(hour=15, minute=33)
            checkpoint = json.dumps(dict(model_calls=1, ended_at=ended.isoformat()))
            st.db.execute("INSERT INTO wakes(started_at,status,checkpoint,harness_run_id) VALUES (?,'completed',?,'model')", (started.isoformat(), checkpoint))
            # Its briefing was built at 18:27 Athens, before the day close was due.
            self.assertIn('day_close', timing.due(st.db, self.now.replace(hour=15, minute=40)))

    def test_midnight_cutover_has_nothing_due_until_orientation(self):
        from datetime import timedelta
        with writer(self.root, initialize=True) as st:
            old = self.now.replace(hour=16, minute=5).isoformat()
            st.db.execute("INSERT INTO wakes(started_at,status,checkpoint,harness_run_id) VALUES (?,'completed','{}','old-model')", (old,))
            midnight = self.now.astimezone(TEST_ZONE).replace(hour=0) + timedelta(days=1)
            for minutes in (0, 60, 419):
                self.assertEqual(timing.due(st.db, midnight + timedelta(minutes=minutes)), [])
            self.assertEqual(timing.due(st.db, midnight + timedelta(hours=7)), ['orientation'])

    def test_digest_release_follows_edited_moment(self):
        from steward.notify import notify, flush
        with writer(self.root, initialize=True) as st:
            timing.set_moment(st.db, 'day_close', '17:00', '17:30', self.now)
            sent = []
            def send(*args, **options):
                sent.append(args)
                return 1
            notify(st.db, 'Finds', 'result', 'A find', 'find:1', send, self.now)
            flush(st.db, send, self.now.replace(hour=13, minute=59))
            self.assertEqual(sent, [])
            flush(st.db, send, self.now.replace(hour=14, minute=0))
            self.assertEqual(len(sent), 1)

    def test_page_schedule_reads_edited_moments_and_limit(self):
        from steward import memory
        with writer(self.root, initialize=True) as st:
            timing.set_moment(st.db, 'orientation', '08:00', '08:50', self.now)
            page = memory.briefing(st.db, self.now, {})
            schedule = page.split('## Schedule', 1)[1].split('## ', 1)[0]
            self.assertIn('08:00', schedule)
            self.assertIn('08:50', schedule)
            self.assertIn('--moment orientation', schedule)
            self.assertNotIn('orientation:2026-10-20', schedule)
            self.assertIn('digest', schedule)
            self.assertIn('1200', page.split('## Limits', 1)[1])
            self.assertNotIn('07:00', schedule)

    def test_other_today_messages_do_not_keep_moments(self):
        from steward.notify import notify
        from datetime import timedelta
        with writer(self.root, initialize=True) as st:
            send = lambda *a, **kw: 1
            notify(st.db, 'Today', 'result', 'Week gist', 'week:gist', send,
                   self.now - timedelta(hours=2))
            self.assertIn('orientation', timing.due(st.db, self.now))
            self.assertEqual(timing.moment_obligation(st.db, jobs.get(st.db, 'moments', 'orientation'), self.now)['state'], 'due')
            notify(st.db, 'Today', 'result', 'Orientation', 'orientation:2026-10-20', send, self.now)
            self.assertNotIn('orientation', timing.due(st.db, self.now))
            evening = self.now.replace(hour=15, minute=40)
            notify(st.db, 'Today', 'result', 'A reply', None, send, evening)
            self.assertIn('day_close', timing.due(st.db, evening))
            self.assertEqual(timing.moment_obligation(st.db, jobs.get(st.db, 'moments', 'day_close'), evening)['state'], 'due')
            late = evening.replace(hour=16, minute=1)
            notify(st.db, 'Today', 'result', 'Late close', 'day_close:2026-10-20', send, late)
            self.assertEqual(timing.moment_obligation(st.db, jobs.get(st.db, 'moments', 'day_close'), late)['state'], 'recovered')
            self.assertNotIn('day_close', timing.due(st.db, late))

    def test_missed_moment_still_fires_until_its_own_message_is_sent(self):
        with writer(self.root, initialize=True) as st:
            late = self.now.replace(hour=16, minute=5)
            self.assertIn('day_close', timing.due(st.db, late))

    def test_own_early_message_keeps_promise_but_late_one_does_not(self):
        from steward.notify import notify
        from datetime import timedelta
        with writer(self.root, initialize=True) as st:
            notify(st.db, 'Today', 'result', 'Own orientation', 'orientation:2026-10-20',
                   lambda *a, **options: 1, self.now - timedelta(minutes=5))
            self.assertEqual(timing.moment_obligation(st.db, jobs.get(st.db, 'moments', 'orientation'), self.now)['state'], 'kept')
            self.assertNotIn('orientation', timing.due(st.db, self.now))

    def test_job_kept_checks_finish_in_shared_deadline_window(self):
        from datetime import timedelta
        target = self.now
        self.assertEqual(timing.kept([target], target), target)
        self.assertIsNone(timing.kept([target + timedelta(seconds=1)], target))
        self.assertIsNone(timing.kept([target - timedelta(hours=7)], target))

    def test_seed_edit_actor_and_undo(self):
        with writer(self.root, initialize=True) as st:
            before = timing.moments(st.db)
            self.assertEqual(before[0]['start'], '07:00')
            result = timing.set_moment(st.db, 'orientation', '08:00', '08:45', self.now, 'model')
            self.assertEqual(timing.moments(st.db)[0]['start'], '08:00')
            self.assertEqual(st.db.execute('SELECT actor FROM changes WHERE id=?', (result['change_id'],)).fetchone()[0], 'model')
            jobs.undo(st.db, result['change_id'], now=self.now)
            self.assertEqual(timing.moments(st.db)[0]['start'], '07:00')
            with self.assertRaises(StateError):
                timing.set_moment(st.db, 'orientation', '25:00', '08:45', self.now)
