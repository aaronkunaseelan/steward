from fixture_mind import use_settings
import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from steward import calendar_b as b, scheduler
from steward.state import writer, StateError
from fixture_mind import TEST_ZONE

NOW = datetime(2026, 10, 19, 9, tzinfo=TEST_ZONE)
SETTINGS = dict(schedule_calendar_id='schedule', main_calendar_ids=['main', 'second'])
SPEC = dict(title='Focus', minutes=60, kind='focus', transparency='opaque', windows={'0': [['09:00', '18:00']], '1': [['09:00', '18:00']]}, count=1, source='charter', justification='Protected progress')


def event(identity, start, end, **kw):
    return dict(id=identity, summary='Focus', start={'dateTime': start}, end={'dateTime': end}, updated=NOW.isoformat(), transparency='opaque', **kw)


class Gog:
    def __init__(self):
        self.events = {'main': [event('focus', '2026-10-19T09:00:00+03:00', '2026-10-19T18:00:00+03:00', eventType='focusTime')], 'second': [], 'schedule': []}
        self.calls = []
        self.before = lambda: None
        self.mismatch = False
        self.crash = False

    def __call__(self, argv):
        self.calls.append(argv)
        i = argv.index('calendar') if 'calendar' in argv else argv.index('cal')
        command, cal = argv[i+1:i+3]
        args = argv[i+3:]
        if command == 'events':
            return json.dumps({'events': copy.deepcopy(self.events[cal])})
        if command == 'event':
            e = next((e for e in self.events[cal] if e['id'] == args[0]), None)
            if e is None:
                from steward.senses import ReadNotFound
                raise ReadNotFound('fixture missing identity')
            e = copy.deepcopy(e)
            if self.mismatch:
                e['summary'] = 'wrong'
            return json.dumps({'event': e})
        self.before()
        assert cal == 'schedule'
        if command == 'delete':
            self.events[cal] = [e for e in self.events[cal] if e['id'] != args[0]]
            return '{}'
        def flag(name):
            return args[args.index(name)+1]
        key = next(args[n+1].split('=',1)[1] for n, v in enumerate(args) if v == '--private-prop' and args[n+1].startswith('event_key='))
        e = event(args[0] if command == 'update' else 'e'+str(len(self.events[cal])+1), flag('--from'), flag('--to'))
        e.update(summary=flag('--summary'), transparency=flag('--transparency'), extendedProperties={'private': {'event_key': key, 'steward': '1'}}, reminders={})  # what gog returns for --no-reminders (observed live 2026-10-16)
        if '--description' in args:
            e['description'] = flag('--description')
        if command == 'update':
            self.events[cal] = [x for x in self.events[cal] if x['id'] != e['id']]
        self.events[cal].append(e)
        if self.crash:
            raise ValueError('simulated lost create response')
        return json.dumps({'event': e})


class CalendarBTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = writer(Path(self.tmp.name), initialize=True)
        self.db = self.store.__enter__().db
        self.gog = Gog()
        from steward import senses
        senses.save(self.db, {'health': dict(status='ok', data=dict(
            date=NOW.date().isoformat(), week_start='2026-10-18', week=[], auth='ok', watch_uploaded_at=NOW.isoformat(),
            watch_fresh_until=(NOW + timedelta(days=1)).isoformat()))}, NOW,
            b.change(self.db, 'fixture', NOW, 'owner'))
        b.demand(self.db, 'add', 'focus', SPEC, NOW)

    def tearDown(self):
        self.store.__exit__(None, None, None)
        self.tmp.cleanup()

    def plan(self, now=NOW, settings=SETTINGS):
        return b.plan(self.db, settings, now, self.gog)

    def choose(self, candidate, now=NOW, settings=SETTINGS):
        return b.choose(self.db, settings, {'schema_version': 1, 'selections': [{'candidate_id': candidate['id'], 'reason': 'Make progress'}]}, now, runner=self.gog)

    def place(self):
        c = next(c for c in self.plan()['candidates']['choices'] if c['action'] == 'place')
        return self.choose(c)

    def test_distant_deadline_and_many_tasks_have_bounded_inventory(self):
        spec = dict(SPEC, kind='other', deadline=(NOW + timedelta(days=40)).isoformat(),
                    windows={str(i): [['09:00', '21:00']] for i in range(7)})
        spec.pop('count')
        b.demand(self.db, 'add', 'distant', spec, NOW)
        plan = self.plan()
        self.assertGreater(len(next(t for t in plan['context']['tasks']
                                    if t['id'] == 'distant:once:0')['windows']), 32)
        self.assertLessEqual(len(plan['candidates']['choices']), 2048)
        self.assertEqual({c['task_id'] for c in plan['candidates']['choices']},
                         {'distant:once:0', 'focus:2026-10-18:0'})
        for i in range(40):
            b.demand(self.db, 'add', f'near-{i}', dict(SPEC, kind='other'), NOW)
        plan = self.plan()
        self.assertGreater(len(plan['context']['tasks']), 32)
        self.assertLessEqual(len(plan['candidates']['choices']), 2048)
        represented = {c['task_id'] for c in plan['candidates']['choices']}
        self.assertTrue({f'near-{i}:2026-10-18:0' for i in range(40)} <= represented)
        self.choose(plan['candidates']['choices'][0])

    def test_sunday_late_tuesday_window_has_candidate(self):
        sunday = datetime(2026, 10, 18, 23, 30, tzinfo=TEST_ZONE)
        spec = dict(SPEC, kind='other', windows={'1': [['14:00', '16:00']]})
        spec.pop('count')
        b.demand(self.db, 'edit', 'focus', spec, sunday)
        result = self.plan(sunday)
        places = [c for c in result['candidates']['choices'] if c['action'] == 'place']
        self.assertTrue(places)
        self.assertTrue(all(c['start'][:10] == '2026-10-20' for c in places))
        self.assertEqual(result['candidates']['horizon_end'], '2026-10-21T00:00:00+03:00')
        self.choose(places[0], now=sunday)

    def test_weekly_gym_places_whole_current_week(self):
        b.demand(self.db, 'rm', 'focus', now=NOW)
        gym = dict(SPEC, title='Gym', kind='other', minutes=90, count=3,
                   transparency='transparent', windows={
                       day: [['14:00', '16:00']] for day in ('1', '3', '4')})
        b.demand(self.db, 'add', 'gym', gym, NOW)
        result = self.plan()
        self.assertEqual(result['candidates']['horizon_end'], '2026-10-25T00:00:00+03:00')
        places = [c for c in result['candidates']['choices'] if c['action'] == 'place']
        self.assertEqual({c['start'][:10] for c in places},
                         {'2026-10-20', '2026-10-22', '2026-10-23'})
        self.assertEqual({t['id'] for t in result['context']['tasks']},
                         {f'gym:2026-10-18:{i}' for i in range(3)})
        reads = [call for call in self.gog.calls if 'events' in call]
        self.assertTrue(reads)
        self.assertTrue(all(call[call.index('--to')+1] ==
                            result['candidates']['horizon_end'] for call in reads))

    def test_weekly_deadline_preserves_sunday_planning_minimum(self):
        sunday = datetime(2026, 10, 25, 23, 30, tzinfo=TEST_ZONE)
        result = self.plan(sunday)
        self.assertEqual(b.deadlines(self.db, sunday), ['2026-11-01T00:00:00+02:00'])
        self.assertEqual(result['candidates']['horizon_end'], '2026-11-01T00:00:00+02:00')

    def test_weekly_deadline_does_not_cut_off_minimum_horizon(self):
        friday = datetime(2026, 10, 23, 9, tzinfo=TEST_ZONE)
        result = self.plan(friday)
        self.assertEqual(result['candidates']['horizon_end'], '2026-10-26T00:00:00+02:00')
        self.assertEqual({t['id'] for t in result['context']['tasks']},
                         {'focus:2026-10-18:0', 'focus:2026-10-25:0'})

    def test_third_horizon_day_new_week_count_keys(self):
        saturday = datetime(2026, 10, 17, 23, 30, tzinfo=TEST_ZONE)
        b.demand(self.db, 'edit', 'focus', dict(SPEC, kind='other',
                 windows={'0': [['14:00', '16:00']]}), saturday)
        result = self.plan(saturday)
        self.assertEqual({t['id'] for t in result['context']['tasks']},
                         {'focus:2026-10-11:0', 'focus:2026-10-18:0'})
        places = [c for c in result['candidates']['choices'] if c['action'] == 'place']
        self.assertTrue(places)
        self.assertTrue(all(c['task_id'] == 'focus:2026-10-18:0' and
                            c['start'][:10] == '2026-10-19' for c in places))

    def test_changes_names_moved_block_old_and_new_times(self):
        from steward import messages
        self.place()
        old = copy.deepcopy(self.gog.events['schedule'][0])
        move = next(c for c in self.plan()['candidates']['choices']
                    if c['action'] == 'place' and c['start'][11:16] == '14:00')
        result = b.choose(self.db, SETTINGS, {'schema_version': 1, 'selections': [
            {'candidate_id': move['id'], 'reason': 'A real change'}]},
            NOW, actor='model', runner=self.gog)
        sent = []
        messages.changes(self.db, NOW, lambda topic, text, **options: sent.append((topic, text)) or
                         {'message_id': 1}, SETTINGS)
        self.assertEqual(len(sent), 1)
        topic, text = sent[0]
        self.assertEqual(topic, 'Changes')
        for expected in ('Focus', 'moved from', 'Mon 19 Oct 09:00 EEST–10:00 EEST',
                         'Mon 19 Oct 14:00 EEST–15:00 EEST',
                         'A real change'):
            self.assertIn(expected, text)
        self.assertNotIn(result['change_id'][:8], text)

    def test_changes_names_deleted_block_and_old_times(self):
        from steward import messages
        self.dead_event()
        old = copy.deepcopy(self.gog.events['schedule'][0])
        drop = next(c for c in self.plan()['candidates']['choices'] if c['action'] == 'drop')
        b.choose(self.db, SETTINGS, {'schema_version': 1, 'selections': [
            {'candidate_id': drop['id'], 'reason': 'Demand retired'}]},
            NOW, actor='model', runner=self.gog)
        sent = []
        messages.changes(self.db, NOW, lambda topic, text, **options: sent.append(text) or
                         {'message_id': 1}, SETTINGS)
        self.assertEqual(len(sent), 1)
        for expected in ('Focus', 'deleted from', 'Mon 19 Oct 10:00 EEST–11:00 EEST', 'Demand retired'):
            self.assertIn(expected, sent[0])

    def test_live_demands_shared_by_briefing_and_planner(self):
        once = {k: v for k, v in SPEC.items() if k != 'count'}
        for name, deadline in [('expired', NOW - timedelta(days=1)), ('boundary', NOW),
                               ('future', NOW + timedelta(hours=4))]:
            b.demand(self.db, 'add', name, dict(once, deadline=deadline.isoformat()), NOW)
        b.demand(self.db, 'add', 'removed', SPEC, NOW)
        b.demand(self.db, 'rm', 'removed', now=NOW)
        before = self.db.total_changes
        self.assertEqual({r['name'] for r in b.briefing(self.db, NOW)['demands']},
                         {'focus', 'future'})
        self.assertEqual(set(self.plan()['demands'].values()), {'focus', 'future'})
        state = b.jobs.snapshot(self.db, now=NOW)['calendar_b']
        self.assertEqual({r['name'] for r in state['demands']}, {'focus', 'future'})
        self.assertEqual({r['name']: r['state'] for r in state['inactive_demands']},
                         {'expired': 'expired', 'boundary': 'expired', 'removed': 'removed'})
        self.assertEqual(self.db.total_changes, before)

    def test_briefing_keeps_today_history_not_yesterday_or_deleted(self):
        cid = b.change(self.db, 'fixture', NOW, 'owner')
        for key, end, state in [('yesterday', NOW.replace(hour=0) - timedelta(seconds=1), 'applied'),
                                ('midnight', NOW.replace(hour=0), 'applied'),
                                ('today', NOW - timedelta(hours=1), 'applied'),
                                ('future', NOW + timedelta(days=1), 'applied'),
                                ('gone', NOW, 'removed'), ('deleted', NOW, 'deleted')]:
            b.append(self.db, 'blocks', dict(event_key=key, version=0, demand='focus',
                     state=state, payload=json.dumps(dict(start=(end-timedelta(hours=1)).isoformat(), end=end.isoformat()))), cid)
        self.assertEqual({r['event_key'] for r in b.briefing(self.db, NOW)['blocks']},
                         {'today', 'future'})

    def dead_event(self, expired=False):
        if expired:
            spec = dict(SPEC)
            spec.pop('count')
            spec['deadline'] = (NOW + timedelta(hours=5)).isoformat()
            b.demand(self.db, 'edit', 'focus', spec, NOW)
        self.choose(next(c for c in self.plan()['candidates']['choices']
                         if c['action'] == 'place' and c['start'][11:16] == '10:00'))
        if expired:
            spec['deadline'] = NOW.isoformat()
            b.demand(self.db, 'edit', 'focus', spec, NOW)
        else:
            b.demand(self.db, 'rm', 'focus', now=NOW)

    def test_removed_demand_delete_readback_changes_undo_and_second_plan(self):
        self.dead_event()
        drop = next(c for c in self.plan()['candidates']['choices'] if c['action'] == 'drop')
        result = self.choose(drop)
        self.assertEqual(self.gog.events['schedule'], [])
        self.assertEqual(b.latest(self.db, 'blocks')[0]['state'], 'deleted')
        summary = self.db.execute('SELECT summary FROM changes WHERE id=?', (result['change_id'],)).fetchone()[0]
        self.assertIn('Focus: deleted from Mon 19 Oct 10:00 EEST–11:00 EEST', summary)
        self.assertNotIn('+03:00', summary)
        self.assertIn('Make progress', summary)
        self.assertEqual(self.plan()['candidates']['choices'], [])
        b.undo(self.db, SETTINGS, result['change_id'], NOW, runner=self.gog)
        self.assertEqual(len(self.gog.events['schedule']), 1)

    def test_block_reasons_are_one_per_line_in_athens(self):
        self.dead_event()
        first = self.gog.events['schedule'][0]
        second = copy.deepcopy(first)
        second.update(id='another', summary='Another')
        second['extendedProperties']['private']['event_key'] = 'focus:2026-10-18:1'
        self.gog.events['schedule'].append(second)
        drops = [c for c in self.plan()['candidates']['choices'] if c['action'] == 'drop']
        result = b.choose(self.db, SETTINGS, {'schema_version': 1, 'selections': [
            {'candidate_id': c['id'], 'reason': 'No longer needed'} for c in drops]}, NOW, runner=self.gog)
        summary = self.db.execute('SELECT summary FROM changes WHERE id=?', (result['change_id'],)).fetchone()[0]
        self.assertEqual(len([line for line in summary.splitlines() if 'deleted from Mon 19 Oct' in line]), 2)
        self.assertEqual(b.block_time('2026-10-22T09:30:00Z', '2026-10-22T11:00:00Z'),
                         'Thu 22 Oct 12:30 EEST–14:00 EEST')
        self.assertEqual(b.block_time('2026-10-24T20:30:00Z', '2026-10-25T02:30:00Z'),
                         'Sat 24 Oct 23:30 EEST–Sun 25 Oct 04:30 EET')

    def test_expired_demand_future_event_delete(self):
        self.dead_event(expired=True)
        result = self.choose(next(c for c in self.plan()['candidates']['choices'] if c['action'] == 'drop'))
        self.assertEqual(self.gog.events['schedule'], [])
        b.undo(self.db, SETTINGS, result['change_id'], NOW, runner=self.gog)
        self.assertEqual(len(self.gog.events['schedule']), 1)

    def test_dead_demand_near_event_can_be_dropped(self):
        self.dead_event()
        self.assertEqual({c['action'] for c in self.plan(NOW + timedelta(minutes=1))['candidates']['choices']}, {'keep', 'drop'})

    def test_dead_demand_elapsed_event_is_history_not_obstacle(self):
        self.dead_event()
        for now in (NOW + timedelta(hours=2), NOW + timedelta(hours=3)):
            result = self.plan(now)
            self.assertEqual(result['candidates']['choices'], [])
            self.assertEqual(result['context']['blocks'], [])
        self.assertEqual(len(self.gog.events['schedule']), 1)

    def test_dead_demand_owner_edit_can_be_dropped(self):
        self.dead_event()
        self.gog.events['schedule'][0]['summary'] = 'Owner title'
        result = self.plan()
        self.assertEqual({c['action'] for c in result['candidates']['choices']}, {'keep', 'drop'})
        self.assertEqual(result['context']['blocks'][0]['kind'], 'scheduled')

    def test_unkeyed_event_remains_obstacle(self):
        b.demand(self.db, 'rm', 'focus', now=NOW)
        self.gog.events['schedule'].append(event('untracked', '2026-10-19T10:00:00+03:00', '2026-10-19T11:00:00+03:00'))
        result = self.plan()
        self.assertEqual(result['candidates']['choices'], [])
        self.assertEqual(result['context']['blocks'][0]['kind'], 'busy')

    def test_dead_demand_forged_place(self):
        self.dead_event()
        result = self.plan()
        drop = next(c for c in result['candidates']['choices'] if c['action'] == 'drop')
        key = drop['task_id']
        forged = scheduler.choice(key, 'place', key, '2026-10-19T11:00:00+03:00', '2026-10-19T12:00:00+03:00')
        with self.assertRaises(scheduler.InvalidPlan):
            scheduler.check(result['context'], dict(result['candidates'], choices=[forged]),
                dict(schema_version=1, selections=[dict(candidate_id=forged['id'], reason='forge')]), NOW.isoformat())

    def test_untracked_keyed_dead_event_delete_and_undo(self):
        self.gog.events['schedule'].append(event('orphan', '2026-10-19T14:00:00+03:00',
            '2026-10-19T15:00:00+03:00', extendedProperties={'private': {'event_key': 'gone:once:0'}}))
        result = self.choose(next(c for c in self.plan()['candidates']['choices'] if c['task_id'] == 'gone:once:0' and c['action'] == 'drop'))
        b.undo(self.db, SETTINGS, result['change_id'], NOW, runner=self.gog)
        self.assertEqual(len(self.gog.events['schedule']), 1)

    def test_restricted_argv_and_intent_before_effect(self):
        def before():
            # Separate connection proves intent committed before provider call.
            import sqlite3
            with sqlite3.connect(Path(self.tmp.name)/'state.sqlite') as db:
                self.assertEqual(db.execute('SELECT state FROM blocks ORDER BY rowid DESC LIMIT 1').fetchone()[0], 'intended')
        self.gog.before = before
        self.place()
        for argv in self.gog.calls:
            self.assertTrue(any(x.startswith('--enable-commands-exact=calendar.') for x in argv))
            self.assertFalse(any(any(t in x for t in ('attendee', 'invite', 'meet', 'zoom')) for x in argv))
            if 'create' in argv or 'update' in argv:
                self.assertIn('--send-updates=none', argv)
                self.assertIn('--no-reminders', argv)
        with self.assertRaises(StateError):
            self.plan(settings=dict(SETTINGS, schedule_calendar_id='main'))
        self.assertEqual(b.latest(self.db, 'blocks')[0]['state'], 'applied')

    def test_union_focus_and_out_of_office(self):
        meeting = event('m', '2026-10-19T12:00:00+03:00', '2026-10-19T13:00:00+03:00')
        self.gog.events['main'].append(meeting)
        self.gog.events['second'] = [meeting, event('overlap', '2026-10-19T12:30:00+03:00', '2026-10-19T14:00:00+03:00'), event('ooo', '2026-10-19T16:00:00+03:00', '2026-10-19T18:00:00+03:00', eventType='outOfOffice')]
        result = self.plan()
        busy = [x for x in result['context']['blocks'] if x['kind'] == 'busy']
        self.assertEqual(len(busy), 2)
        spans = [c for c in result['candidates']['choices'] if c['action'] == 'place']
        self.assertTrue(spans)
        self.assertTrue(all(c['start'][:10] == '2026-10-19' and (c['end'][11:16] <= '12:00' or ('14:00' <= c['start'][11:16] and c['end'][11:16] <= '16:00')) for c in spans))

    def test_weekly_count_and_transparent_birthday(self):
        spec = dict(SPEC, title='Gym', kind='other', count=3, transparency='transparent', windows={'0': [['12:30','18:00']], '1': [['12:30','18:00']]})
        b.demand(self.db, 'edit', 'focus', spec, NOW)
        self.gog.events['main'] = [dict(id='birthday', start={'date':'2026-10-19'}, end={'date':'2026-10-20'}, transparency='transparent')]
        for i in range(2):
            self.gog.events['schedule'].append(event('old'+str(i), '2026-10-18T12:30:00+03:00', '2026-10-18T13:30:00+03:00', extendedProperties={'private': {'event_key':f'focus:2026-10-18:{i}'}}))
        result = self.plan()
        # Containers use normal keyed-count accounting; occupied keys stay consumed.
        self.assertEqual(len(result['context']['tasks']), 1)
        self.assertTrue(result['candidates']['choices'])

    def test_owner_edit_and_deletion_are_learned_suggestions(self):
        self.place()
        self.gog.events['schedule'][0]['summary'] = 'Owner title'
        observation = b.read(SETTINGS, NOW, self.gog, self.db)
        with self.db:
            cid = b.change(self.db, 'tick', NOW, 'tick')
            b.reconcile(self.db, observation, SETTINGS, cid)
        self.assertEqual(b.latest(self.db, 'blocks')[0]['state'], 'applied')
        self.assertIn('You moved', self.db.execute('SELECT summary FROM changes WHERE id=?', (cid,)).fetchone()[0])
        moved = next(c for c in self.plan()['candidates']['choices'] if c['action'] == 'place')
        before = self.gog.events['schedule'][0]['start']['dateTime']
        self.choose(moved)
        self.assertNotEqual(self.gog.events['schedule'][0]['start']['dateTime'], before)
        self.gog.events['schedule'].clear()
        with self.db:
            b.reconcile(self.db, b.read(SETTINGS, NOW, self.gog, self.db), SETTINGS, cid)
        self.assertEqual(b.latest(self.db, 'blocks')[0]['state'], 'removed')
        replacement = next(c for c in self.plan()['candidates']['choices'] if c['action'] == 'place')
        self.choose(replacement)
        self.assertEqual(b.latest(self.db, 'blocks')[0]['state'], 'applied')
        self.assertEqual(len(self.gog.events['schedule']), 1)

    def test_learned_move_not_retold_for_payload_shape_or_metadata(self):
        self.place()
        e = self.gog.events['schedule'][0]
        e['start']['dateTime'] = '2026-10-20T11:00:00+03:00'
        e['end']['dateTime'] = '2026-10-20T12:00:00+03:00'
        def learn():
            with self.db:
                cid = b.change(self.db, 'tick', NOW, 'tick')
                return b.reconcile(self.db, b.read(SETTINGS, NOW, self.gog, self.db), SETTINGS, cid)
        self.assertEqual(len(learn()), 1)
        # Before S3, owner-edited payloads had no description key. The first read
        # after promotion added an empty one and told the same owner move again.
        old = dict(b.latest(self.db, 'blocks')[0])
        p = json.loads(old['payload'])
        p.pop('description', None)
        cid = b.change(self.db, 'fixture', NOW, 'owner')
        b.append(self.db, 'blocks', dict(old, payload=json.dumps(p, sort_keys=True)), cid)
        self.assertEqual(learn(), [])
        self.assertEqual(learn(), [])
        e['updated'] = (NOW + timedelta(hours=1)).isoformat()
        self.assertEqual(learn(), [])
        self.assertEqual(json.loads(b.latest(self.db, 'blocks')[0]['payload'])['updated'], e['updated'])
        # A genuinely new edit and a deletion are still learned once each.
        e['summary'] = 'Owner retitled'
        self.assertEqual(len(learn()), 1)
        self.assertEqual(learn(), [])
        self.gog.events['schedule'].clear()
        self.assertEqual(len(learn()), 1)
        self.assertEqual(learn(), [])

    def test_newer_updated_alone_is_still_applied(self):
        self.place()
        self.gog.events['schedule'][0]['updated'] = (NOW+timedelta(minutes=1)).isoformat()
        self.assertTrue(any(c['action'] == 'place' for c in self.plan()['candidates']['choices']))

    def test_mismatch_not_applied(self):
        self.gog.mismatch = True
        with self.assertRaises(StateError):
            self.place()
        self.assertEqual(b.latest(self.db, 'blocks')[0]['state'], 'written')

    def test_crash_reconciles_key_without_duplicate(self):
        self.gog.crash = True
        with self.assertRaises(ValueError):
            self.place()
        row = b.latest(self.db, 'blocks')[0]
        self.gog.crash = False
        b.resume(self.db, SETTINGS, row['change_id'], NOW, self.gog)
        self.assertEqual(len(self.gog.events['schedule']), 1)
        self.assertEqual(sum('create' in c for c in self.gog.calls), 1)
        self.assertFalse(any('update' in c for c in self.gog.calls))  # Verified post-image, no second mutation.

    def test_near_block_can_move_for_new_overlapping_meeting(self):
        choice = next(c for c in self.plan()['candidates']['choices']
                      if c['action'] == 'place' and c['start'][11:16] == '09:30')
        self.choose(choice)
        self.gog.events['main'].append(event('new-meeting',
            '2026-10-19T09:30:00+03:00', '2026-10-19T10:30:00+03:00'))
        move = next(c for c in self.plan()['candidates']['choices']
                    if c['action'] == 'place' and c['start'][11:16] == '11:00')
        self.choose(move)
        self.assertEqual(self.gog.events['schedule'][0]['start']['dateTime'], move['start'])
        # Further useful changes and undo remain checked, with no daily move cap.
        for clock in ('12:00', '13:00', '14:00', '15:00'):
            move = next(c for c in self.plan()['candidates']['choices']
                        if c['action'] == 'place' and c['start'][11:16] == clock)
            result = self.choose(move)
        b.undo(self.db, SETTINGS, result['change_id'], NOW, runner=self.gog)
        self.assertEqual(self.gog.events['schedule'][0]['start']['dateTime'],
                         '2026-10-19T14:00:00+03:00')

    def test_deadline_extends_planning_and_read_nine_days_ahead(self):
        now = datetime(2026, 10, 18, 23, 30, tzinfo=TEST_ZONE)
        due = now + timedelta(days=10)
        spec = dict(SPEC, kind='other', windows={'1': [['14:00', '16:00']]},
                    deadline=due.isoformat())
        spec.pop('count')
        b.demand(self.db, 'edit', 'focus', spec, now)
        result = self.plan(now)
        choice = next(c for c in result['candidates']['choices']
                      if c['action'] == 'place' and c['start'][:10] == '2026-10-27')
        self.assertEqual(result['candidates']['horizon_end'], due.isoformat())
        for argv in self.gog.calls:
            if 'events' in argv:
                self.assertEqual(argv[argv.index('--to') + 1], due.isoformat())
        self.choose(choice, now=now)

    def test_live_deadline_still_extends_horizon_when_placed_block_elapsed(self):
        due = NOW + timedelta(days=10)
        spec = dict(SPEC, kind='other', deadline=due.isoformat())
        spec.pop('count')
        b.demand(self.db, 'edit', 'focus', spec, NOW)
        self.place()
        result = self.plan(NOW + timedelta(hours=2))
        self.assertEqual(result['context']['tasks'], [])
        self.assertEqual(result['candidates']['horizon_end'], due.isoformat())

    def test_count_keys_cover_all_weeks_of_extended_horizon(self):
        b.demand(self.db, 'add', 'later', dict(title='Later', minutes=30,
                 kind='other', transparency='opaque', windows={'1': [['14:00', '16:00']]},
                 deadline=(NOW + timedelta(days=16)).isoformat(), source='charter',
                 justification='Known ahead'), NOW)
        keys = set(self.plan()['demands'])
        self.assertTrue({'focus:2026-10-18:0', 'focus:2026-10-25:0',
                         'focus:2026-11-01:0'} <= keys)

    def test_undo_restores_and_created_block_deletes(self):
        first = self.place()
        original = self.gog.events['schedule'][0]['start']
        moved = self.choose(next(c for c in self.plan()['candidates']['choices'] if c['action']=='place' and c['start'][11:16]=='14:00'))
        result = b.undo(self.db, SETTINGS, moved['change_id'], NOW, runner=self.gog)
        self.assertEqual(self.gog.events['schedule'][0]['start'], original)
        self.assertEqual(self.db.execute('SELECT undo_of FROM changes WHERE id=?', (result['change_id'],)).fetchone()[0], moved['change_id'])
        # A fresh fixture for undo of creation (the old change has newer versions).
        self.gog.events['schedule'].clear()

    def test_undo_creation_and_deletion(self):
        first = self.place()
        # Inject the clock used for delete absence verification via a read fixture;
        # the fixture returns all events regardless of requested interval.
        result = b.undo(self.db, SETTINGS, first['change_id'], NOW, runner=self.gog)
        self.assertEqual(self.gog.events['schedule'], [])
        b.undo(self.db, SETTINGS, result['change_id'], NOW, runner=self.gog)
        self.assertEqual(len(self.gog.events['schedule']), 1)

    def test_task_windows_checker_independent(self):
        result = self.plan()
        c = next(c for c in result['candidates']['choices'] if c['start'][11:16]=='11:00')
        result['context']['tasks'][0]['windows'] = [{'start':'2026-10-19T15:00:00+03:00','end':'2026-10-19T18:00:00+03:00'}]
        with self.assertRaises(scheduler.InvalidPlan):
            scheduler.check(result['context'], result['candidates'], {'schema_version':1,'selections':[{'candidate_id':c['id'],'reason':'forged window'}]}, NOW.isoformat())

    def test_failed_wake_does_not_keep_a_moment(self):
        from steward import timing, runtime
        from steward.notify import notify
        for hour, minute, name in [(7, 0, 'orientation'), (18, 30, 'day_close')]:
            start = NOW.replace(hour=hour, minute=minute)
            self.db.execute("INSERT INTO wakes(started_at,status,checkpoint,harness_run_id) "
                            "VALUES (?,'failed','{}','harness')", (start.isoformat(),))
            retry = start + timedelta(minutes=20)
            self.assertIn(name, timing.due(self.db, retry))
            self.assertIn(name, runtime.gate(self.db, retry))
            key = name + ':' + str(start.date())
            notify(self.db, 'Today', 'result', name, key, lambda *a, **options: 1, retry)
            self.assertNotIn(name, timing.due(self.db, retry))

    def test_demand_undo(self):
        edited = b.demand(self.db, 'edit', 'focus', dict(SPEC, title='New'), NOW)
        b.undo(self.db, SETTINGS, edited['change_id'], NOW, runner=self.gog)
        self.assertEqual(json.loads(b.latest(self.db, 'demands')[0]['spec_json'])['title'], 'Focus')

class CalendarBEdgeTests(unittest.TestCase):
    setUp = CalendarBTests.setUp
    tearDown = CalendarBTests.tearDown
    plan = CalendarBTests.plan
    choose = CalendarBTests.choose
    place = CalendarBTests.place

    def test_unknown_read_has_no_intent_or_write(self):
        def unknown(argv):
            raise ValueError('unavailable')
        with self.assertRaises(ValueError):
            b.plan(self.db, SETTINGS, NOW, unknown)
        self.assertEqual(b.latest(self.db, 'blocks'), [])

    def test_plan_is_read_only(self):
        before = self.db.total_changes
        self.plan()
        self.assertEqual(self.db.total_changes, before)
        self.assertFalse(any('create' in a or 'update' in a or 'delete' in a for a in self.gog.calls))

    def test_deadline_limits_and_stale_inventory_rejected(self):
        b.demand(self.db, 'edit', 'focus', dict({k:v for k,v in SPEC.items() if k != 'count'}, deadline='2026-10-19T12:00:00+03:00'), NOW)
        choices = self.plan()['candidates']['choices']
        self.assertTrue(all(c['end'][11:16] <= '12:00' for c in choices))
        self.gog.events['main'].append(event('new-meeting', choices[0]['start'], choices[0]['end']))
        with self.assertRaises(scheduler.InvalidPlan):
            self.choose(choices[0])
        self.assertEqual(b.latest(self.db, 'blocks'), [])

    def test_undo_can_replace_unpersisted_owner_suggestion(self):
        first = self.place()
        self.gog.events['schedule'][0]['summary'] = 'Owner edit'
        b.undo(self.db, SETTINGS, first['change_id'], NOW, runner=self.gog)
        self.assertEqual(self.gog.events['schedule'], [])

    def test_week_boundary_keys_and_windows(self):
        sunday = datetime(2026, 10, 18, 9, tzinfo=TEST_ZONE)
        result = self.plan(sunday)
        places = [c for c in result['candidates']['choices'] if c['action']=='place']
        self.assertTrue(places)
        self.assertTrue(all('2026-10-18' in c['task_id'] and c['start'][:10]=='2026-10-19' for c in places))

    def test_keys_from_previous_week_count_in_actual_week(self):
        spec = dict(SPEC, kind='other', count=1)
        b.demand(self.db, 'edit', 'focus', spec, NOW)
        self.gog.events['schedule'].append(event('moved', '2026-10-19T15:00:00+03:00','2026-10-19T16:00:00+03:00', extendedProperties={'private': {'event_key':'focus:2026-10-12:0'}}))
        self.assertEqual(self.plan()['candidates']['choices'], [])

    def test_weekly_remaining_budget_with_foreign_week_key(self):
        b.demand(self.db, 'edit', 'focus', dict(SPEC, kind='other', count=2), NOW)
        self.gog.events['schedule'].append(event('moved', '2026-10-19T15:00:00+03:00','2026-10-19T16:00:00+03:00', extendedProperties={'private': {'event_key':'focus:2026-10-12:0'}}))
        self.assertEqual(len(self.plan()['context']['tasks']), 1)

    def test_mutation_boundary_refuses_main_calendar(self):
        for cmd in ('create', 'update', 'delete'):
            with self.assertRaises(StateError):
                b.gog(dict(SETTINGS, schedule_calendar_id='second'), cmd, [], self.gog)
        self.assertEqual(self.gog.calls, [])


if __name__ == '__main__':
    unittest.main()
