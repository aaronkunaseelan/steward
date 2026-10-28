"""Calendar update recovery distinguishes preimages, postimages and owner edits."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from fixture_mind import install
from steward import calendar_b, intent, jobs, senses
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

    def test_failed_create_is_not_a_confirmed_placement(self):
        from test_calendar_b import Gog, NOW, SETTINGS, SPEC
        with writer(self.root) as st:
            gog = Gog()
            calendar_b.demand(st.db, 'add', 'focus', dict(SPEC, kind='other'), NOW)
            choice = next(c for c in calendar_b.plan(st.db, SETTINGS, NOW, gog)['candidates']['choices']
                          if c['action'] == 'place')
            def fail_create(argv):
                if 'create' in argv:
                    raise OSError('before provider write')
                return gog(argv)
            with self.assertRaises(OSError):
                calendar_b.choose(st.db, SETTINGS, dict(schema_version=1, selections=[dict(
                    candidate_id=choice['id'], reason='fixture')]), NOW, runner=fail_create)
            doc = intent.steward_doc(st.db, NOW, SETTINGS)
        self.assertFalse(next(b for b in doc['blocks'] if b['kind'] == 'day')['yours'])
        self.assertIn('unconfirmed', doc['headline'])

    def test_pending_update_keeps_last_confirmed_placement(self):
        with writer(self.root) as st:
            gog, now, settings, proposal = self.calendar(st.db)
            before = gog.events['schedule'][0]['start']['dateTime'][11:16]
            move = next(c for c in calendar_b.plan(st.db, settings, now, gog)['candidates']['choices']
                        if c['action'] == 'place' and c['start'][11:16] == '14:00')
            def fail_update(argv):
                if 'update' in argv:
                    raise OSError('before provider write')
                return gog(argv)
            with self.assertRaises(OSError):
                calendar_b.choose(st.db, settings, proposal(move), now, runner=fail_update)
            doc = intent.steward_doc(st.db, now, settings)
        yours = next(b for b in doc['blocks'] if b['kind'] == 'day')['yours']
        self.assertEqual([b['from'] for b in yours], [before])

    def pending_create(self, written=False):
        from test_calendar_b import Gog, NOW, SETTINGS, SPEC
        gog = Gog()
        calendar_b.demand(self.db, 'add', 'first', dict(SPEC, kind='other'), NOW)
        choice = next(c for c in calendar_b.plan(self.db, SETTINGS, NOW, gog)['candidates']['choices']
                      if c['action'] == 'place' and c['start'][11:16] == '10:00')
        def failed(argv):
            if 'create' in argv:
                if written:
                    gog(argv)
                raise OSError('response unavailable')
            return gog(argv)
        with self.assertRaises(OSError):
            calendar_b.choose(self.db, SETTINGS, dict(schema_version=1, selections=[dict(
                candidate_id=choice['id'], reason='fixture')]), NOW, runner=failed)
        return gog, NOW, SETTINGS, calendar_b.latest(self.db, 'blocks')[0]['change_id']

    def test_expired_absent_create_is_retired_and_other_work_can_continue(self):
        with writer(self.root) as st:
            self.db = st.db
            gog, now, settings, cid = self.pending_create()
            later = now + timedelta(hours=3)
            calendar_b.resume(st.db, settings, cid, later, gog)
            self.assertEqual(calendar_b.latest(st.db, 'blocks')[0]['state'], 'removed')
            choice = next(c for c in calendar_b.plan(st.db, settings, later, gog)['candidates']['choices']
                          if c['action'] == 'place')
            calendar_b.choose(st.db, settings, dict(schema_version=1, selections=[dict(
                candidate_id=choice['id'], reason='still useful')]), later, runner=gog)
            self.assertEqual(len(gog.events['schedule']), 1)

    def test_expired_verified_postimage_is_confirmed_without_rewriting(self):
        with writer(self.root) as st:
            self.db = st.db
            gog, now, settings, cid = self.pending_create(written=True)
            calendar_b.resume(st.db, settings, cid, now + timedelta(hours=3), gog)
            self.assertEqual(calendar_b.latest(st.db, 'blocks')[0]['state'], 'applied')
            self.assertEqual(sum('create' in call for call in gog.calls), 1)

    def test_recovery_keeps_owner_changed_or_unreadable_effect_pending(self):
        with writer(self.root) as st:
            self.db = st.db
            gog, now, settings, cid = self.pending_create(written=True)
            gog.events['schedule'][0]['description'] = 'new owner edit'
            with self.assertRaisesRegex(ValueError, 'pending event changed'):
                calendar_b.resume(st.db, settings, cid, now + timedelta(hours=3), gog)
            self.assertEqual(calendar_b.latest(st.db, 'blocks')[0]['state'], 'intended')
            def unreadable(argv):
                if '--private-prop-filter' in argv:
                    raise OSError('provider unavailable')
                return gog(argv)
            with self.assertRaises(OSError):
                calendar_b.resume(st.db, settings, cid, now + timedelta(hours=3), unreadable)
            self.assertEqual(calendar_b.latest(st.db, 'blocks')[0]['state'], 'intended')

    def test_key_lookup_finds_owner_move_outside_planning_window(self):
        with writer(self.root) as st:
            self.db = st.db
            gog, now, settings, cid = self.pending_create(written=True)
            event = gog.events['schedule'][0]
            event['start']['dateTime'] = (now + timedelta(days=14)).isoformat()
            event['end']['dateTime'] = (now + timedelta(days=14, hours=1)).isoformat()
            def bounded(argv):
                raw = gog(argv)
                if 'events' not in argv:
                    return raw
                if '--private-prop-filter' in argv:
                    self.assertEqual(argv[argv.index('--from') + 1], '0001-01-01T00:00:00Z')
                    self.assertEqual(argv[argv.index('--to') + 1], '9999-12-31T23:59:59Z')
                end = jobs.instant(argv[argv.index('--to') + 1])
                return json.dumps({'events': [e for e in json.loads(raw)['events']
                                              if calendar_b.event_span(e)[0] < end]})
            with self.assertRaisesRegex(ValueError, 'pending event changed'):
                calendar_b.resume(st.db, settings, cid, now + timedelta(hours=3), bounded)
            self.assertEqual(calendar_b.latest(st.db, 'blocks')[0]['state'], 'intended')
            self.assertEqual(sum('create' in call for call in gog.calls), 1)

    def test_expired_unwritten_update_restores_confirmed_baseline(self):
        with writer(self.root) as st:
            gog, now, settings, proposal = self.calendar(st.db)
            before = calendar_b.payload(gog.events['schedule'][0])
            move = next(c for c in calendar_b.plan(st.db, settings, now, gog)['candidates']['choices']
                        if c['action'] == 'place' and c['start'][11:16] == '14:00')
            def fail_update(argv):
                if 'update' in argv:
                    raise OSError('request never reached provider')
                return gog(argv)
            with self.assertRaises(OSError):
                calendar_b.choose(st.db, settings, proposal(move), now, runner=fail_update)
            pending = calendar_b.latest(st.db, 'blocks')[0]
            calendar_b.resume(st.db, settings, pending['change_id'], now + timedelta(hours=7), gog)
            row = calendar_b.latest(st.db, 'blocks')[0]
            self.assertEqual(row['state'], 'applied')
            self.assertEqual(json.loads(row['payload']), before)
            self.assertFalse(any('update' in call for call in gog.calls))

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
