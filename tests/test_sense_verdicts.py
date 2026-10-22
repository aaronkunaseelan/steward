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


class SenseVerdictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        install(self, Path(self.tmp.name))
        self.root = Path(self.tmp.name) / 'state'
        with writer(self.root, initialize=True):
            pass
        self.now = datetime(2026, 10, 21, 4, tzinfo=timezone.utc)
        self.sent = []

    def healthy(self, db, now):
        messages.poll(db, now, {}, fetcher=lambda offset: [], telegram={'chat_id': -1})
        senses.save(db, {name: dict(status='ok', data={'events': [], 'digest': 'fixture'})
                        for name in senses.INTERVALS if name not in ('health', 'place')},
                    now, jobs.change(db, 'fixture', now, 'tick'))

    def test_ok_transport_with_stale_qmd_verdict_is_not_green(self):
        now = self.now.replace(hour=2)
        with writer(self.root) as st:
            self.healthy(st.db, now)
            senses.save(st.db, {'qmd': dict(status='ok', data=dict(alive=False,
                sources={'x': dict(cadence_s=300, fresh_through=now.timestamp(), current=False, limit_s=600)}))},
                now, jobs.change(st.db, 'fixture', now, 'tick'))
            self.assertEqual(senses.snapshot(st.db, now)['qmd']['verdict'], 'stale')
            with patch('steward.intent.ticks', return_value={}):
                doc = intent.steward_doc(st.db, now, {})
            self.assertNotEqual(doc['state'], 'green', 'chain ignores the existing shared freshness verdict')

    def test_fallback_labels_stale_gsd_before_printing_priorities(self):
        from fixture_mind import command
        with writer(self.root) as st:
            self.healthy(st.db, self.now)
            jobs.job_verb(st.db, 'add', 'gsd-steady', dict(justification='fixture', argv=[command()],
                          needs={'ready_by': ['06:30']}), now=self.now)
            senses.save(st.db, {'gsd': dict(status='ok', data=dict(
                digest='fixture', waiting_on_you=[dict(id='external', title='Unverified old priority')],
                cut={'until': self.now.isoformat(), 'committed_at': self.now.isoformat()}))},
                self.now, jobs.change(st.db, 'fixture', self.now, 'tick'))
            self.assertEqual(senses.snapshot(st.db, self.now)['gsd']['verdict'], 'stale')
            text = messages.fallback(st.db, self.now)
            self.assertIn('stale', text.lower(), 'fallback prints stale priorities without their shared verdict')


    def test_chain_uses_oldest_relevant_read_and_verdict_reasons(self):
        readings = {name: dict(status='ok', verdict='fresh', reason='read succeeded', age=0)
                    for name in senses.INTERVALS}
        readings['qmd'].update(verdict='stale', reason='producer says non-current', age=7200)
        readings['gsd'].update(verdict='unknown', reason='cut unknown', age=300)
        readings['health']['age'] = 86400  # excluded, as is place
        with patch('steward.intent.snapshot', return_value=readings):
            hop = intent.chain(None, self.now, dict(status='ok', reason=None,
                               last_poll_ok_at=self.now.isoformat()))['hops'][0]
        self.assertEqual(hop['lag_s'], 7200)
        self.assertTrue(hop['warn'])
        self.assertIn('qmd stale: producer says non-current', hop['note'])
        self.assertIn('gsd unknown: cut unknown', hop['note'])

    def test_fallback_qualifies_unknown_gsd_before_cached_priority(self):
        readings = {name: dict(status='ok', verdict='fresh', reason='read succeeded', data={})
                    for name in senses.INTERVALS}
        readings['gsd'].update(verdict='unknown', reason='cut unknown',
                              data={'waiting_on_you': [dict(id='cached', title='Old priority')]})
        with writer(self.root) as st, patch('steward.messages.senses.snapshot', return_value=readings):
            text = messages.fallback(st.db, self.now)
        self.assertLess(text.index('gsd unknown: cut unknown'), text.index('Old priority'))
