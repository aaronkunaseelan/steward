"""Tracked calendar identities survive bounded lists and failed provider reads."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fixture_mind import install
from steward import calendar_b, jobs, senses
from steward.state import writer


class CalendarIdentityTests(unittest.TestCase):
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

    def test_owner_move_outside_list_window_does_not_duplicate_event_key(self):
        with writer(self.root) as st:
            gog, now, settings, proposal = self.calendar(st.db)
            e = gog.events['schedule'][0]
            e['start']['dateTime'] = '2026-11-02T14:00:00+02:00'
            e['end']['dateTime'] = '2026-11-02T15:00:00+02:00'
            def bounded_list(argv):
                raw = gog(argv)
                if 'events' not in argv:
                    return raw
                end = jobs.instant(argv[argv.index('--to') + 1])
                data = json.loads(raw)
                data['events'] = [e for e in data['events'] if calendar_b.event_span(e)[0] < end]
                return json.dumps(data)
            plan = calendar_b.plan(st.db, settings, now, bounded_list)
            places = [c for c in plan['candidates']['choices'] if c['action'] == 'place']
            if places:
                calendar_b.choose(st.db, settings, proposal(places[0]), now, runner=bounded_list)
            keys = [e['extendedProperties']['private']['event_key'] for e in gog.events['schedule']]
            self.assertEqual(len(keys), len(set(keys)), 'bounded absence was mistaken for deletion; stable key created twice')

    def test_past_and_far_future_tracked_events_cost_no_identity_lookup(self):
        with writer(self.root) as st:
            gog, now, settings, proposal = self.calendar(st.db)
            cid = jobs.change(st.db, 'fixture', now, 'tick')
            for key, start, end in (
                    ('past', now - timedelta(hours=2), now - timedelta(hours=1)),
                    ('beyond', now + timedelta(days=60), now + timedelta(days=60, hours=1))):
                p = dict(start=start.isoformat(), end=end.isoformat(), title=key,
                         transparency='opaque', event_id=key, event_key=key)
                jobs.append(st.db, 'blocks', dict(event_key=key, demand='focus', state='applied',
                            payload=json.dumps(p)), cid)
            gog.calls.clear()
            observation = calendar_b.read(settings, now, gog, st.db)
            self.assertFalse(any('event' in call for call in gog.calls))
            self.assertNotIn('past', observation['missing_event_keys'])
            self.assertNotIn('beyond', observation['missing_event_keys'])

    def test_known_outside_window_placement_is_not_recreated(self):
        with writer(self.root) as st:
            gog, now, settings, proposal = self.calendar(st.db)
            event = gog.events['schedule'][0]
            event['start']['dateTime'] = '2026-11-02T14:00:00+02:00'
            event['end']['dateTime'] = '2026-11-02T15:00:00+02:00'
            row = calendar_b.latest(st.db, 'blocks')[0]
            jobs.append(st.db, 'blocks', dict(row, payload=json.dumps(calendar_b.payload(event))),
                        jobs.change(st.db, 'fixture', now, 'tick'))
            def bounded_list(argv):
                raw = gog(argv)
                if 'events' not in argv:
                    return raw
                end = jobs.instant(argv[argv.index('--to') + 1])
                data = json.loads(raw)
                data['events'] = [e for e in data['events'] if calendar_b.event_span(e)[0] < end]
                return json.dumps(data)
            gog.calls.clear()
            plan = calendar_b.plan(st.db, settings, now, bounded_list)
            self.assertFalse(any('event' in call for call in gog.calls))
            self.assertFalse(any(c['task_id'] == row['event_key'] and c['action'] == 'place'
                                 for c in plan['candidates']['choices']))
            self.assertEqual(len(gog.events['schedule']), 1)

    def test_missing_identity_is_not_confused_with_failed_read(self):
        with writer(self.root) as st:
            gog, now, settings, proposal = self.calendar(st.db)
            def unreadable(argv):
                if 'events' in argv:
                    return '{"events":[]}'
                if 'event' in argv:
                    raise OSError('read failed')
                return gog(argv)
            before = calendar_b.latest(st.db, 'blocks')
            with self.assertRaisesRegex(OSError, 'read failed'):
                calendar_b.plan(st.db, settings, now, unreadable)
            self.assertEqual(calendar_b.latest(st.db, 'blocks'), before)
            self.assertEqual(len(gog.events['schedule']), 1)

    def test_tracked_identity_with_changed_key_is_not_silently_recreated(self):
        with writer(self.root) as st:
            gog, now, settings, proposal = self.calendar(st.db)
            gog.events['schedule'][0].pop('extendedProperties')
            with self.assertRaisesRegex(ValueError, 'key changed'):
                calendar_b.plan(st.db, settings, now, gog)
            self.assertEqual(len(gog.events['schedule']), 1)

    def test_gog_not_found_is_typed_but_other_read_failures_are_not_absence(self):
        import subprocess
        for code in (5, 4, 7):
            with patch('steward.senses.subprocess.run', return_value=subprocess.CompletedProcess(
                    ['gog'], code, '', 'private provider error')):
                if code == 5:
                    with self.assertRaises(senses.ReadNotFound):
                        senses.run(['gog', '--readonly', 'calendar', 'event', 'schedule', 'event-id'])
                else:
                    with self.assertRaises(subprocess.CalledProcessError) as error:
                        senses.run(['gog', '--readonly', 'calendar', 'event', 'schedule', 'event-id'])
                    self.assertNotIsInstance(error.exception, senses.ReadNotFound)
        with patch('steward.senses.subprocess.run', side_effect=FileNotFoundError('gog')):
            with self.assertRaises(FileNotFoundError):
                senses.run(['gog', '--readonly', 'calendar', 'event', 'schedule', 'event-id'])
