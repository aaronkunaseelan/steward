"""Calendar update recovery distinguishes preimages, postimages and owner edits."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from fixture_mind import install
from steward import calendar_b, jobs, senses
from steward.state import writer


class CalendarRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        install(self, Path(self.tmp.name))
        self.root = Path(self.tmp.name) / 'state'
        with writer(self.root, initialize=True):
            pass
        self.now = datetime(2026, 10, 21, 4, tzinfo=timezone.utc)
        self.sent = []

    def calendar(self, db):
        from test_calendar_b import Gog, NOW, SETTINGS, SPEC
        gog = Gog()
        senses.save(db, {'health': dict(status='ok', data=dict(
            date=NOW.date().isoformat(), week=[], auth='ok', watch_uploaded_at=NOW.isoformat(),
            watch_fresh_until=(NOW + timedelta(days=1)).isoformat()))}, NOW,
            jobs.change(db, 'fixture', NOW, 'tick'))
        calendar_b.demand(db, 'add', 'focus', SPEC, NOW)
        plan = calendar_b.plan(db, SETTINGS, NOW, gog)
        choice = next(c for c in plan['candidates']['choices'] if c['action'] == 'place')
        proposal = lambda c: {'schema_version': 1, 'selections': [{'candidate_id': c['id'], 'reason': 'fixture'}]}
        calendar_b.choose(db, SETTINGS, proposal(choice), NOW, runner=gog)
        return gog, NOW, SETTINGS, proposal

    def test_update_failure_before_provider_effect_is_resumable(self):
        with writer(self.root) as st:
            gog, now, settings, proposal = self.calendar(st.db)
            plan = calendar_b.plan(st.db, settings, now, gog)
            move = next(c for c in plan['candidates']['choices']
                        if c['action'] == 'place' and c['start'][11:16] == '14:00')
            def fail_update(argv):
                if 'update' in argv:
                    raise OSError('fixture: request never reached provider')
                return gog(argv)
            with self.assertRaises(OSError):
                calendar_b.choose(st.db, settings, proposal(move), now, runner=fail_update)
            pending = calendar_b.latest(st.db, 'blocks')[0]
            result = calendar_b.resume(st.db, settings, pending['change_id'], now, gog)
            self.assertEqual(result['status'], 'applied')
            self.assertEqual(gog.events['schedule'][0]['start']['dateTime'], '2026-10-19T14:00:00+03:00')
            self.assertEqual(calendar_b.latest(st.db, 'blocks')[0]['state'], 'applied')
            self.assertEqual(sum('update' in call for call in gog.calls), 1)

    def test_lost_update_response_accepts_postimage_without_second_mutation(self):
        with writer(self.root) as st:
            gog, now, settings, proposal = self.calendar(st.db)
            move = next(c for c in calendar_b.plan(st.db, settings, now, gog)['candidates']['choices']
                        if c['action'] == 'place' and c['start'][11:16] == '14:00')
            def lost_response(argv):
                result = gog(argv)
                if 'update' in argv:
                    raise OSError('provider accepted; response lost')
                return result
            with self.assertRaises(OSError):
                calendar_b.choose(st.db, settings, proposal(move), now, runner=lost_response)
            pending = calendar_b.latest(st.db, 'blocks')[0]
            result = calendar_b.resume(st.db, settings, pending['change_id'], now, gog)
            self.assertEqual(result['status'], 'applied')
            self.assertEqual(sum('update' in call for call in gog.calls), 1)
            self.assertEqual(gog.events['schedule'][0]['start']['dateTime'], '2026-10-19T14:00:00+03:00')
            self.assertEqual(calendar_b.latest(st.db, 'blocks')[0]['state'], 'applied')

    def test_recovery_refuses_unrelated_owner_edit(self):
        with writer(self.root) as st:
            gog, now, settings, proposal = self.calendar(st.db)
            move = next(c for c in calendar_b.plan(st.db, settings, now, gog)['candidates']['choices']
                        if c['action'] == 'place' and c['start'][11:16] == '14:00')
            def fail_update(argv):
                if 'update' in argv:
                    raise OSError('request never reached provider')
                return gog(argv)
            with self.assertRaises(OSError):
                calendar_b.choose(st.db, settings, proposal(move), now, runner=fail_update)
            gog.events['schedule'][0]['summary'] = 'Owner changed this independently'
            pending = calendar_b.latest(st.db, 'blocks')[0]
            with self.assertRaisesRegex(ValueError, 'pending event changed'):
                calendar_b.resume(st.db, settings, pending['change_id'], now, gog)
            self.assertEqual(gog.events['schedule'][0]['summary'], 'Owner changed this independently')
