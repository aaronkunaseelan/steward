"""Future visibility is not placement or wake news."""
from datetime import datetime, timedelta
import json
from unittest import TestCase
from unittest.mock import patch

from fixture_mind import use_settings, TEST_ZONE
from steward import briefing, senses

NOW = datetime(2026, 10, 27, 12, tzinfo=TEST_ZONE)
SETTINGS = dict(schedule_calendar_id='b', main_calendar_ids=['main'])


def event(day, title, *, calendar='main', series=None, key=None):
    start = NOW.replace(hour=9) + timedelta(days=day)
    return dict(id=title, calendar=calendar, summary=title,
                start={'dateTime': start.isoformat()}, end={'dateTime': (start + timedelta(hours=1)).isoformat()},
                recurring_event_id=series, event_key=key)


def data(events):
    return dict(events=[], schedule_calendar_id='b', ahead=dict(
        status='ok', at=NOW.isoformat(), day=NOW.date().isoformat(),
        to='2028-10-27T00:00:00+03:00', events=events))


class AheadTests(TestCase):
    setUp = use_settings

    def test_bands_series_calendar_b_and_multiday(self):
        events = [event(2, 'near'), event(3, 'edge-three'), event(30, 'edge-thirty', series='weekly'),
                  event(31, 'weekly-one', series='weekly'), event(38, 'weekly-two', series='weekly'),
                  event(40, 'non-recurring'), event(4, 'own-plan', calendar='b', key='plan'),
                  event(4, 'owner-entry', calendar='b')]
        events[3]['recurrence'] = ['RRULE:FREQ=WEEKLY']
        events.append(dict(id='trip', calendar='main', summary='multiday', all_day=True,
                           start={'date': '2026-11-02'}, end={'date': '2026-11-06'}))
        text = '\n'.join(briefing.ahead_lines(data(events), NOW))
        self.assertNotIn(': near', text)
        for title in ['edge-three', 'edge-thirty', 'non-recurring', 'owner-entry']:
            self.assertIn(title, text)
        self.assertNotIn('own-plan', text)
        self.assertIn('RRULE:FREQ=WEEKLY', text)
        self.assertIn('first 2026-11-27', text)
        self.assertIn('last 2026-12-04', text)
        self.assertIn('2 instances', text)
        self.assertNotIn('weekly-two', text)
        self.assertIn('2026-11-02 – 2026-11-06: multiday', text)
        self.assertIn('beyond is unknown', text)

    def test_series_identity_includes_calendar_and_unknown_cadence(self):
        text = '\n'.join(briefing.ahead_lines(data([
            event(31, 'first', series='id'), event(32, 'second', series='id'),
            event(33, 'other', calendar='other', series='id')]), NOW))
        self.assertIn('2 instances', text)
        self.assertIn('1 instances', text)
        self.assertEqual(text.count('cadence unknown'), 2)

    def test_cap_is_bytes_and_counts_omitted_lines(self):
        with patch.object(briefing, 'AHEAD_BYTES', 600):
            rows = briefing.ahead_lines(data([event(4, str(i) + ' é' * 100) for i in range(20)]), NOW)
        self.assertLessEqual(len(''.join('- ' + row + '\n' for row in rows).encode()), 600)
        self.assertIn(f'truncated: {20 - (len(rows) - 2)} more through 2028-10-27', rows[-1])

    def test_near_overlap_replaces_far_edits_and_deletions(self):
        original, deleted = event(4, 'original'), event(5, 'deleted')
        reading = data([original, deleted, event(40, 'far')])
        moved = dict(original, summary='changed')
        reading.update(events=[moved], **{'from': '2026-10-25T00:00:00+03:00', 'to': '2026-11-03T00:00:00+02:00'})
        text = '\n'.join(briefing.ahead_lines(reading, NOW))
        self.assertIn('changed', text)
        self.assertIn('far', text)
        self.assertNotIn('original', text)
        self.assertNotIn('deleted', text)

    def test_daily_cache_including_failure_and_leap_day_bound(self):
        calls = []
        def runner(argv):
            calls.append(argv)
            return '{"events": []}'
        first = senses.calendars(SETTINGS, NOW, runner, ahead=True)
        self.assertEqual(len(calls), 4)
        second = senses.calendars(SETTINGS, NOW + timedelta(minutes=20), runner, ahead=True, previous=first)
        self.assertEqual(len(calls), 6)
        self.assertEqual(first['ahead'], second['ahead'])
        senses.calendars(SETTINGS, NOW + timedelta(days=1), runner, ahead=True, previous=second)
        self.assertEqual(len(calls), 10)
        failed = senses.calendar_ahead(['b'], NOW, lambda argv: (_ for _ in ()).throw(OSError('private')), {})
        self.assertEqual(failed['status'], 'unknown')
        self.assertNotIn('private', failed['reason'])
        cached = senses.calendar_ahead(['b'], NOW, lambda argv: self.fail('must not retry today'), failed)
        self.assertEqual(cached, failed)
        leap = senses.calendar_ahead(['b'], datetime(2028, 2, 29, 12, tzinfo=TEST_ZONE), runner, {})
        self.assertTrue(leap['to'].startswith('2030-02-28T00:00:00'))

    def test_far_edit_does_not_change_digest_or_placement_window(self):
        changed = False
        def runner(argv):
            # Emulate Google's date filtering, plus an updated future event.
            near = event(0, 'near')
            far = event(50, 'future')
            far['summary'] = 'edited' if changed else 'future'
            end = datetime.fromisoformat(argv[argv.index('--to') + 1])
            selected = [e for e in (near, far) if senses.calendar_instant(e['start']) < end]
            return json.dumps({'events': selected})
        first = senses.calendars(SETTINGS, NOW, runner, ahead=True)
        changed = True
        second = senses.calendars(SETTINGS, NOW, runner, ahead=True)
        self.assertNotEqual(first['ahead'], second['ahead'])
        self.assertEqual(first['digest'], second['digest'])
        self.assertEqual(first['to'], second['to'])
        # Calendar-B callers never pay for the far view.
        calls = []
        senses.calendars(SETTINGS, NOW, lambda argv: calls.append(argv) or '{"events": []}')
        self.assertEqual(len(calls), 2)

    def test_unknown_far_section_and_section_order(self):
        reading = data([])
        reading['ahead'].update(status='unknown', reason='read failed: OSError')
        page = briefing.render({'senses': {'calendars': dict(status='ok', at=NOW.isoformat(), data=reading)}}, NOW, {})
        self.assertIn('## Ahead [unknown]', page)
        self.assertIn('unknown: read failed: OSError', page)
        self.assertLess(page.index('## Calendar'), page.index('## Ahead'))
        self.assertLess(page.index('## Ahead'), page.index('## Movement'))
