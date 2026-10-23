from fixture_mind import TEST_ZONE
"""Found by the BR2 review: the orientation fallback after a completed wake, and DST-safe windows."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fixture_mind import install
from steward import intent, jobs, messages, runtime, senses, timing
from steward.notify import notify, flush
from steward.state import writer


class MomentFallbackAndDstTests(unittest.TestCase):
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

    def test_completed_model_without_orientation_still_gets_fallback(self):
        runtime.wake(self.root, clock=lambda: self.now, settings={'wake_model': True},
                     runner=lambda argv: 'fixture-model', prompt='fixture', sender=self.send)
        self.assertEqual(len(self.sent), 1, 'a successful harness without Today must still get the documented fallback')

    def test_deadline_window_is_six_elapsed_hours_on_dst_change(self):
        target = datetime(2026, 10, 25, 6, 30, tzinfo=TEST_ZONE)
        finish = datetime(2026, 10, 24, 22, tzinfo=timezone.utc)
        self.assertEqual(timing.kept([finish], target),
                         timing.kept([finish], target.astimezone(timezone.utc)),
                         'judgement must not depend on the timezone representation of one target')

    def healthy(self, db, now):
        messages.poll(db, now, {}, fetcher=lambda offset: [], telegram={'chat_id': -1})
        senses.save(db, {name: dict(status='ok', data={'events': [], 'digest': 'fixture'})
                        for name in senses.INTERVALS if name not in ('health', 'place')},
                    now, jobs.change(db, 'fixture', now, 'tick'))

    def network_down(self, *args, **kwargs):
        raise OSError('fixture network failure')

    def test_unrelated_pending_today_does_not_displace_orientation_fallback(self):
        with writer(self.root) as st:
            notify(st.db, 'Today', 'result', 'Week gist', 'week:2026-10-18', self.network_down, self.now - timedelta(hours=2))
        runtime.wake(self.root, clock=lambda: self.now, settings={'wake_model': True},
                     runner=lambda argv: 'fixture-model', prompt='fixture', sender=self.send)
        with writer(self.root) as st:
            self.assertEqual(timing.moment_obligation(st.db, jobs.get(st.db, 'moments', 'orientation'), self.now)['state'], 'kept')

    def test_failed_pre_model_briefing_is_not_green(self):
        now = self.now.replace(hour=2)
        with writer(self.root) as st:
            self.healthy(st.db, now)
            st.db.execute("INSERT INTO wakes(started_at,status,checkpoint) VALUES (?,'failed',?)",
                          (now.isoformat(), json.dumps({'briefing': {'complete': False, 'error': 'oversized page'}})))
            with patch('steward.intent.ticks', return_value={}):
                self.assertNotEqual(intent.steward_doc(st.db, now, {})['state'], 'green')

    def test_today_accepted_before_the_limit_is_delivered_as_accepted(self):
        with writer(self.root) as st:
            with patch('steward.notify.TODAY_CHARACTERS', 1999):
                queued = notify(st.db, 'Today', 'result', 'x' * 1317, told_key='orientation:2026-10-21', sender=self.network_down, now=self.now)
            flush(st.db, self.send, self.now)
            self.assertEqual(st.db.execute('SELECT state FROM outbox WHERE id=?', (queued['outbox_id'],)).fetchone()[0], 'sent')

    def test_a_failed_row_is_not_told(self):
        with writer(self.root) as st:
            queued = notify(st.db, 'Today', 'result', 'Orientation', told_key='orientation:2026-10-21', sender=self.network_down, now=self.now)
            st.db.execute("UPDATE outbox SET state='failed' WHERE id=?", (queued['outbox_id'],))
            result = notify(st.db, 'Today', 'result', 'Corrected orientation', told_key='orientation:2026-10-21', sender=self.send, now=self.now)
            self.assertEqual(result['status'], 'sent')


if __name__ == '__main__':
    unittest.main()
