from fixture_mind import use_settings
from fixture_mind import TEST_ZONE
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from steward import calendar_b as b, fulfilment as f, jobs, senses, intent
import test_calendar_b as fixtures

NOW = fixtures.NOW


def session(kind='strength_training', day=19, hour=7, minutes=34, label=None):
    return dict(start=f'2026-10-{day:02}T{hour:02}:00:00+03:00', type=kind,
                duration_s=minutes*60, label=label, aerobic_te=None,
                anaerobic_te=None, zone_s=[None]*5, load=None)


class FulfilmentTests(unittest.TestCase):
    setUp = use_settings

    def dose(self, kind, week, minutes=30, count=3):
        return f.fulfilment(dict(kind=kind, minutes=minutes, count=count), week,
                            NOW + timedelta(days=4))

    def test_other_counts_as_strength_and_one_match(self):
        week = [session('other', label='VO2MAX'), session(day=22, label='VO2MAX')]
        gym = self.dose('gym', week)
        self.assertEqual(len(gym['done']), 2)
        self.assertEqual(gym['unclassified'], [])
        self.assertEqual(gym['remaining'], 1)
        self.assertEqual(self.dose('vo2max', week)['done'], [])

    def test_cardio_labels_duration_and_null(self):
        week = [session('treadmill_running', label='VO2MAX'),
                session('running', minutes=27, label='AEROBIC_BASE'),
                session('running', minutes=42, label='AEROBIC_BASE'),
                session('running', minutes=60, label=None)]
        self.assertEqual(len(self.dose('vo2max', week)['done']), 1)
        endurance = self.dose('endurance', week)
        self.assertEqual([s['duration_s'] for s in endurance['done']], [42*60])
        self.assertIn(week[-1], endurance['unclassified'])

    def test_anaerobic_labels_do_not_classify_other_or_strength(self):
        for label in ('SPEED', 'ANAEROBIC_CAPACITY'):
            week = [session('treadmill_running', label=label), session('other', label=label)]
            dose = self.dose('anaerobic', week, count=1)
            self.assertEqual(dose['done'], week[:1])
            self.assertEqual(dose['unclassified'], [])
            self.assertTrue(dose['complete'])

    def test_endurance_minutes_not_session_count(self):
        dose = self.dose('endurance', [session('treadmill_running', minutes=60,
                                             label='AEROBIC_BASE')], count=5)
        self.assertEqual((dose['done_min'], dose['target_min'], dose['remaining_blocks']),
                         (60, 150, 3))

    def test_current_week_only_and_past_only(self):
        result = f.fulfilment(dict(kind='gym', minutes=30, count=3),
                             [session(day=18), session(), session(hour=10)], NOW)
        self.assertEqual(result['done'], [session(day=18), session()])

    def test_saturday_and_sunday_are_different_weeks(self):
        from steward.memory import local_day, week_start
        sessions = [session(day=24, hour=20), session(day=25, hour=1)]
        for at, expected in [('2026-10-24T23:00:00+03:00', sessions[:1]),
                             ('2026-10-25T04:00:00+02:00', sessions[1:])]:
            now = datetime.fromisoformat(at)
            dose = f.fulfilment(dict(kind='gym', count=3, minutes=50), sessions, now)
            self.assertEqual(dose['done'], expected)
            self.assertEqual(week_start(local_day(now)).isoformat(),
                             '2026-10-18' if len(expected) and expected[0]['start'][8:10] == '24' else '2026-10-25')

    def test_kinds_validate(self):
        for kind in ('movement', 'gym', 'vo2max', 'anaerobic', 'endurance'):
            self.assertEqual(b.validate(dict(fixtures.SPEC, kind=kind))['kind'], kind)


class FulfilmentReaderTests(unittest.TestCase):
    setUp = fixtures.CalendarBTests.setUp
    tearDown = fixtures.CalendarBTests.tearDown
    plan = fixtures.CalendarBTests.plan
    choose = fixtures.CalendarBTests.choose

    def health(self, week=None, unknown=None, now=NOW):
        observation = dict(status='unknown', reason=unknown) if unknown else dict(
            status='ok', data=dict(date=NOW.date().isoformat(), week_start='2026-10-18', auth='ok', watch_uploaded_at=NOW.isoformat(), week=week or [],
                                  watch_fresh_until=(NOW + timedelta(days=1)).isoformat()))
        senses.save(self.db, {'health': observation}, now,
                    b.change(self.db, 'fixture', now, 'owner'))

    def program(self):
        b.demand(self.db, 'rm', 'focus', now=NOW)
        for kind, minutes, count in [('movement', 120, 4), ('gym', 50, 3),
                                     ('vo2max', 45, 2), ('anaerobic', 30, 1),
                                     ('endurance', 30, 5)]:
            b.demand(self.db, 'add', kind, dict(fixtures.SPEC, kind=kind,
                title=kind.title(), minutes=minutes, count=count,
                transparency='transparent',
                windows={str(i): [['09:00', '21:00']] for i in range(7)}), NOW)

    def place_movement(self):
        return self.choose(next(c for c in self.plan()['candidates']['choices']
                                if c['action'] == 'place'))

    def legacy(self, kind='gym'):
        key = kind + ':2026-10-18:0'
        e = fixtures.event('legacy', '2026-10-20T14:00:00+03:00',
            '2026-10-20T15:30:00+03:00',
            extendedProperties={'private': {'event_key': key, 'steward': '1'}})
        self.gog.events['schedule'].append(e)
        p = b.payload(e)
        b.append(self.db, 'blocks', dict(event_key=key, version=0, demand=kind,
            state='applied', payload=json.dumps(p)),
            b.change(self.db, 'fixture', NOW, 'owner'))
        return key

    def test_old_monday_cache_is_unknown_until_sunday_week_read(self):
        self.program()
        self.health([session()])
        row = self.db.execute("SELECT id,payload FROM senses WHERE name='health' ORDER BY id DESC LIMIT 1").fetchone()
        payload = json.loads(row['payload'])
        del payload['data']['week_start']
        self.db.execute('UPDATE senses SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
        self.assertIn('Sunday week', b.fulfilments(self.db, NOW)['gym']['unknown'])
        self.health([session(day=18), session()])
        self.assertEqual(len(b.fulfilments(self.db, NOW)['gym']['done']), 2)

    def test_movement_four_two_hour_blocks_and_inventory(self):
        self.program()
        self.gog.events['main'] = [fixtures.event(f'busy-{i}',
            f'2026-10-{19+i}T20:45:00+03:00',
            f'2026-10-{19+i}T21:00:00+03:00') for i in range(6)] + [
            fixtures.event('busy-sunday', '2026-10-25T20:45:00+02:00',
                           '2026-10-25T21:00:00+02:00')]
        plan = self.plan()
        self.assertEqual(len(plan['context']['tasks']), 1)
        self.assertEqual(len(plan['candidates']['choices']), 60)  # Monday–Saturday remain.
        for i in range(4):
            plan = self.plan()
            choices = plan['candidates']['choices']
            self.assertLessEqual(len(choices), 150)
            places = [c for c in choices if c['action'] == 'place']
            self.assertEqual({c['task_id'] for c in places}, {f'movement:2026-10-18:{i}'})
            c = next(c for c in places if c['start'][11:16] == '09:00'
                     and c['start'][:10] == f'2026-10-{19+i}')
            self.choose(c)
        self.assertEqual({c['action'] for c in self.plan()['candidates']['choices']}, {'keep', 'drop'})
        self.assertLessEqual(len(self.plan()['candidates']['choices']), 150)
        self.assertEqual(len(self.gog.events['schedule']), 4)
        for e in self.gog.events['schedule']:
            a, z = b.event_span(e)
            self.assertEqual(z-a, timedelta(minutes=120))
            self.assertIn('1. VO2 max, 45 min: 0/2 this week', e['description'])
        self.assertFalse(any(c['action'] == 'place' for c in self.plan()['candidates']['choices']
                             if c['task_id'].split(':')[0] != 'movement'))

    def test_steward_drop_and_owner_deletion_can_both_be_replaced(self):
        self.program()
        for _ in range(4):
            self.place_movement()
        drop = [c for c in self.plan()['candidates']['choices'] if c['action'] == 'drop'][-1]
        self.choose(drop)
        places = [c for c in self.plan()['candidates']['choices'] if c['action'] == 'place']
        self.assertTrue(places)
        self.assertEqual({c['task_id'] for c in places}, {'movement:2026-10-18:4'})
        self.choose(places[0])
        self.gog.events['schedule'].pop()  # Owner deletion is a suggestion, not a consumed slot.
        self.assertTrue(any(c['action'] == 'place' for c in self.plan()['candidates']['choices']))

    def test_daily_lengths_quiet_normal_squeezed_and_too_short(self):
        self.program()
        self.gog.events['main'] = [
            fixtures.event('normal', '2026-10-20T20:00:00+03:00', '2026-10-20T21:00:00+03:00'),
            fixtures.event('squeeze', '2026-10-21T10:40:00+03:00', '2026-10-21T21:00:00+03:00'),
            fixtures.event('short', '2026-10-22T10:20:00+03:00', '2026-10-22T21:00:00+03:00')]
        plan = self.plan()
        lengths = {}
        for c in plan['candidates']['choices']:
            if c['action'] != 'place':
                continue
            minutes = (jobs.instant(c['end']) - jobs.instant(c['start'])).total_seconds()/60
            lengths.setdefault(c['start'][:10], set()).add(minutes)
        self.assertEqual(lengths['2026-10-19'], {150})
        self.assertEqual(lengths['2026-10-20'], {120})
        self.assertEqual(lengths['2026-10-21'], {90})
        self.assertNotIn('2026-10-22', lengths)
        for day in ('2026-10-19', '2026-10-20', '2026-10-21'):
            c = next(c for c in plan['candidates']['choices'] if c['start'] and c['start'].startswith(day))
            proposal = dict(schema_version=1, selections=[dict(candidate_id=c['id'], reason='Fit the day')])
            from steward import scheduler
            scheduler.check(plan['context'], plan['candidates'], proposal, NOW.isoformat())
        # Rehashing a fabricated shorter span cannot bypass the independent duration check.
        import copy
        from steward import scheduler
        inventory = copy.deepcopy(plan['candidates'])
        bad = inventory['choices'][0]
        bad['end'] = (jobs.instant(bad['start']) + timedelta(minutes=60)).astimezone(TEST_ZONE).isoformat()
        bad['id'] = scheduler.choice_id({k: v for k, v in bad.items() if k != 'id'})
        with self.assertRaisesRegex(scheduler.InvalidPlan, 'invalid duration'):
            scheduler.check(plan['context'], inventory, dict(schema_version=1,
                selections=[dict(candidate_id=bad['id'], reason='Too short')]), NOW.isoformat())

    def test_hourly_grid_plus_exact_fit_after_busy(self):
        self.program()
        self.gog.events['main'] = [
            fixtures.event('before', '2026-10-19T09:00:00+03:00', '2026-10-19T10:40:00+03:00'),
            fixtures.event('after', '2026-10-19T12:10:00+03:00', '2026-10-19T21:00:00+03:00')]
        plan = self.plan()
        monday = [c for c in plan['candidates']['choices']
                  if c['action'] == 'place' and c['start'][:10] == '2026-10-19']
        self.assertEqual(len(monday), 1)
        self.assertEqual(monday[0]['start'][11:16], '10:40')
        self.assertEqual(monday[0]['end'][11:16], '12:10')
        other_days = [c for c in plan['candidates']['choices']
                      if c['action'] == 'place' and c['start'][:10] != '2026-10-19']
        self.assertTrue(other_days)
        self.assertTrue(all(c['start'][14:16] == '00' for c in other_days))
        self.choose(monday[0])
        self.assertEqual(len(self.gog.events['schedule']), 1)
        drop = next(c for c in self.plan()['candidates']['choices'] if c['action'] == 'drop')
        result = self.choose(drop)
        b.undo(self.db, fixtures.SETTINGS, result['change_id'], NOW, runner=self.gog)
        restored = self.gog.events['schedule'][0]
        self.assertEqual(restored['start']['dateTime'][11:16], '10:40')
        self.assertEqual(restored['end']['dateTime'][11:16], '12:10')

    def test_doses_never_place_legacy_drop_and_undo(self):
        self.program()
        key = self.legacy()
        plan = self.plan()
        choices = [c for c in plan['candidates']['choices'] if c['task_id'] == key]
        self.assertEqual({c['action'] for c in choices}, {'keep', 'drop'})
        result = self.choose(next(c for c in choices if c['action'] == 'drop'))
        self.assertFalse(self.gog.events['schedule'])
        b.undo(self.db, fixtures.SETTINGS, result['change_id'], NOW, runner=self.gog)
        self.assertEqual(len(self.gog.events['schedule']), 1)

    def test_ranking_ties_and_endurance_minutes_ignore_legacy_plans(self):
        self.program()
        self.legacy()
        self.health([session(), session('running', minutes=60, label='AEROBIC_BASE')])
        ranking = self.plan()['movement']
        self.assertEqual([o['kind'] for o in ranking['options']],
                         ['vo2max', 'anaerobic', 'gym', 'endurance'])
        self.assertIn('Strength, 50 min: 1/3', ranking['description'])
        self.assertIn('Long-duration, ≥30 min: 60/150 min', ranking['description'])
        self.health()
        self.assertEqual([o['kind'] for o in self.plan()['movement']['options']],
                         ['vo2max', 'gym', 'anaerobic', 'endurance'])

    def test_all_watch_doses_complete_drop_movement(self):
        self.program()
        self.place_movement()
        self.health([session() for _ in range(3)] +
            [session('running', label='VO2MAX') for _ in range(2)] +
            [session('running', label='SPEED'),
             session('running', minutes=150, label='AEROBIC_BASE')])
        plan = self.plan()
        self.assertTrue(plan['movement']['complete'])
        self.assertEqual({c['action'] for c in plan['candidates']['choices']}, {'keep', 'drop'})
        result = self.choose(next(c for c in plan['candidates']['choices'] if c['action'] == 'drop'))
        self.assertFalse(self.gog.events['schedule'])
        self.assertIn('deleted from', self.db.execute('SELECT summary FROM changes WHERE id=?',
                                                     (result['change_id'],)).fetchone()[0])

    def test_cached_watch_expires_at_planning_time(self):
        self.program()
        self.choose(next(c for c in self.plan()['candidates']['choices']
                         if c['action'] == 'place' and c['start'].startswith('2026-10-20')))
        expiry = NOW + timedelta(seconds=1)
        senses.save(self.db, {'health': dict(status='ok', data=dict(
            date=NOW.date().isoformat(), week_start='2026-10-18', auth='ok', watch_uploaded_at=NOW.isoformat(), week=[],
            watch_fresh_until=expiry.isoformat(), watch_upload_age_s=43199))}, NOW,
            b.change(self.db, 'fixture', NOW, 'owner'))
        self.assertIsNone(self.plan()['movement']['unknown'])
        for at in (expiry, NOW + timedelta(hours=2)):
            plan = self.plan(at)
            self.assertIn('stale', plan['movement']['unknown'])
            self.assertEqual({c['action'] for c in plan['candidates']['choices']}, {'keep'})
            self.assertIsNone(plan['fulfilment']['movement']['remaining'])
        for bad in (None, 'not-a-time', '2026-10-19T12:00:00'):
            senses.save(self.db, {'health': dict(status='ok', data=dict(
                date=NOW.date().isoformat(), auth='ok', watch_uploaded_at=NOW.isoformat(), week=[], watch_fresh_until=bad))}, NOW,
                b.change(self.db, 'fixture', NOW, 'owner'))
            self.assertTrue(self.plan()['movement']['unknown'])

    def test_unknown_watch_holds_existing_dose_and_movement(self):
        self.program()
        self.place_movement()
        self.legacy()
        self.health(unknown='watch unavailable')
        plan = self.plan()
        self.assertEqual({c['action'] for c in plan['candidates']['choices']}, {'keep'})
        self.assertEqual(len(plan['description_refreshes']), 1)
        self.assertEqual(plan['movement']['options'], [])
        self.assertEqual(plan['movement']['description'], 'Watch unknown: watch unavailable')
        self.assertIsNone(plan['fulfilment']['movement']['remaining'])
        self.choose(plan['candidates']['choices'][0])
        movement = next(e for e in self.gog.events['schedule'] if e['id'] != 'legacy')
        self.assertEqual(movement['description'], 'Watch unknown: watch unavailable')
        self.assertEqual(self.plan()['description_refreshes'], {})
        self.health([dict(session(), zone_s=[0])])
        self.assertEqual({c['action'] for c in self.plan()['candidates']['choices']}, {'keep'})
        self.assertIn('five zones', self.plan()['movement']['unknown'])

    def test_refresh_only_on_text_change_through_checked_keep_and_changes(self):
        from steward import messages
        self.program()
        self.place_movement()
        self.assertEqual(self.plan()['description_refreshes'], {})
        self.health([session()])
        plan = self.plan()
        key, text = next(iter(plan['description_refreshes'].items()))
        self.assertIn('Strength, 50 min: 1/3', text)
        keep = next(c for c in plan['candidates']['choices']
                    if c['task_id'] == key and c['action'] == 'keep')
        old_span = b.event_span(self.gog.events['schedule'][0])
        result = b.choose(self.db, fixtures.SETTINGS, dict(schema_version=1,
            selections=[dict(candidate_id=keep['id'], reason='Refresh watch options')]),
            NOW, actor='model', runner=self.gog)
        self.assertEqual(b.event_span(self.gog.events['schedule'][0]), old_span)
        self.assertEqual(self.gog.events['schedule'][0]['description'], text)
        self.assertEqual(self.plan()['description_refreshes'], {})
        sent = []
        messages.changes(self.db, NOW, lambda topic, text, **options:
                         sent.append((topic, text)) or {'message_id': 1}, fixtures.SETTINGS)
        self.assertEqual(len(sent), 1)
        self.assertIn('options updated at ' + b.block_time(*(t.isoformat() for t in old_span)), sent[0][1])
        writes = sum('update' in call for call in self.gog.calls)
        self.choose(keep)
        self.assertEqual(sum('update' in call for call in self.gog.calls), writes)
        # Undo restores the exact previous description, through checked keep.
        # The no-op above has no block versions, so it does not prevent undo.
        b.undo(self.db, fixtures.SETTINGS, result['change_id'], NOW, runner=self.gog)
        self.assertNotEqual(self.gog.events['schedule'][0]['description'], text)

    def test_owner_suggestions_allow_dose_drop_and_description_refresh(self):
        self.program()
        key = self.legacy()
        self.place_movement()
        movement = next(e for e in self.gog.events['schedule'] if e['id'] != 'legacy')
        movement['description'] = 'Owner choice'
        self.health([session()])
        plan = self.plan()
        self.assertIn(key, plan['demands'])
        self.assertTrue(plan['description_refreshes'])
        self.assertEqual(movement['description'], 'Owner choice')
        self.assertFalse(next(block for block in plan['context']['blocks']
                              if block['task_id'] == movement['extendedProperties']['private']['event_key'])['pinned'])

    def test_complete_watch_can_drop_owner_edited_containers_and_doses(self):
        self.program()
        self.legacy()
        self.place_movement()
        e = next(e for e in self.gog.events['schedule'] if e['id'] != 'legacy')
        e['summary'] = 'Owner suggestion'
        self.health([session() for _ in range(3)] +
            [session('running', label='VO2MAX') for _ in range(2)] +
            [session('running', label='SPEED'),
             session('running', minutes=150, label='AEROBIC_BASE')])
        choices = self.plan()['candidates']['choices']
        self.assertEqual({c['action'] for c in choices}, {'keep', 'drop'})
        self.assertEqual({c['task_id'] for c in choices if c['action'] == 'drop'},
                         {'gym:2026-10-18:0', e['extendedProperties']['private']['event_key']})
        self.assertFalse(self.plan()['description_refreshes'])
        self.assertEqual(len(self.gog.events['schedule']), 2)

    def test_description_readback_is_verified_and_can_resume(self):
        self.program()
        def wrong_description(argv):
            raw = self.gog(argv)
            if 'calendar' in argv and argv[argv.index('calendar')+1] == 'event':
                doc = json.loads(raw)
                doc['event']['description'] = 'Wrong options'
                return json.dumps(doc)
            return raw
        c = next(c for c in self.plan()['candidates']['choices'] if c['action'] == 'place')
        from steward.state import StateError
        with self.assertRaisesRegex(StateError, 'read-back mismatch'):
            b.choose(self.db, fixtures.SETTINGS, dict(schema_version=1,
                selections=[dict(candidate_id=c['id'], reason='Reserve movement')]),
                NOW, runner=wrong_description)
        row = b.latest(self.db, 'blocks')[0]
        self.assertEqual(row['state'], 'written')
        b.resume(self.db, fixtures.SETTINGS, row['change_id'], NOW, runner=self.gog)
        self.assertEqual(b.latest(self.db, 'blocks')[0]['state'], 'applied')

    def test_quiet_duration_is_preserved_when_later_busy_changes(self):
        self.program()
        self.gog.events['main'] = []
        self.place_movement()
        old_span = b.event_span(self.gog.events['schedule'][0])
        self.assertEqual(old_span[1] - old_span[0], timedelta(minutes=150))
        self.gog.events['main'].append(fixtures.event('later',
            '2026-10-19T20:00:00+03:00', '2026-10-19T21:00:00+03:00'))
        plan = self.plan()
        keep = next(c for c in plan['candidates']['choices'] if c['action'] == 'keep')
        self.choose(keep)
        self.assertEqual(b.event_span(self.gog.events['schedule'][0]), old_span)

    def test_owner_calendar_a_never_fulfils_even_with_a_key(self):
        self.program()
        self.gog.events['main'].append(fixtures.event('owner-movement',
            '2026-10-19T07:00:00+03:00', '2026-10-19T08:00:00+03:00',
            extendedProperties={'private': {'event_key': 'movement:2026-10-18:0'}}))
        plan = self.plan()
        self.assertEqual(plan['fulfilment']['movement']['remaining'], 4)
        self.assertEqual(plan['fulfilment']['gym']['remaining'], 3)

    def test_readers_share_ranking(self):
        self.program()
        self.health([session('running', minutes=60, label='AEROBIC_BASE')])
        plan = self.plan()
        self.assertEqual(plan['movement'], b.briefing(self.db, NOW)['movement'])
        self.assertEqual(plan['movement'], jobs.snapshot(self.db, now=NOW)['calendar_b']['movement'])
        with patch.object(intent, 'ticks', return_value={}):
            doc = intent.steward_doc(self.db, NOW, {})
        learned = next(x for x in doc['blocks'] if x['kind'] == 'learned')
        self.assertEqual(plan['movement']['description'].splitlines(),
                         [row[1] for row in learned['demands'] if row[0] == 'Movement options'])

    def test_movement_count_is_calendar_not_watch(self):
        self.program()
        self.place_movement()
        later = NOW + timedelta(hours=3)
        self.health([session()], now=later)
        plan = self.plan(later)
        self.assertEqual(len(plan['fulfilment']['movement']['done']), 1)
        self.assertEqual(plan['fulfilment']['movement']['remaining'], 3)
        self.assertEqual(len(plan['context']['tasks']), 1)
