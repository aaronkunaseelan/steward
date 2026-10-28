from fixture_mind import TEST_ZONE
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from fixture_mind import command as released

from steward import senses, runtime, jobs, memory
from steward.state import writer

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def fixture(identity):
    return json.loads((Path(__file__).parent / 'fixtures' / (identity + '.json')).read_text())


def unavailable(argv):
    raise OSError('sensitive output must not escape')


class SensesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from fixture_mind import install
        install(self, Path(self.tmp.name))
        self.root = Path(self.tmp.name) / 'state'
        self.rollouts = Path(self.tmp.name) / 'sessions'
        with writer(self.root, initialize=True):
            pass

    def collect(self, runner=unavailable, settings=None):
        return senses.collect(self.root, settings or {}, NOW, runner, self.rollouts)

    def test_command_failures_and_timeouts_are_unknown(self):
        for error in (OSError('secret'), subprocess.TimeoutExpired('secret', 60)):
            def runner(argv):
                raise error
            result = self.collect(runner, {'schedule_calendar_id': 'b', 'main_calendar_ids': []})
            for name in senses.INTERVALS:
                self.assertEqual(result[name]['status'], 'unknown')
                self.assertTrue(result[name]['reason'])
                self.assertNotIn('data', result[name])
                self.assertNotIn('secret', result[name]['reason'])

    def test_nonfinite_qmd_expiry_is_unknown(self):
        raw = fixture('qmd.status.v1')
        source = next(iter(raw['sources'].values()))
        source.update(current=True, valid_until=float('inf'))
        def runner(argv):
            if argv[:2] == ['qmd', 'status']:
                return json.dumps(raw).replace('Infinity', '1e309')
            if argv[:2] == ['qmd', 'feed']:
                return json.dumps(dict(schema='qmd.feed.v1', source=argv[3], items=[],
                                       next=None, head=None, complete=True))
            raise OSError('unavailable fixture')
        result = self.collect(runner)
        self.assertEqual(result['qmd']['status'], 'unknown')
        self.assertIn('valid_until: expected number|null', result['qmd']['reason'])

    def test_missing_config_is_unknown(self):
        result = self.collect()
        self.assertEqual(result['calendars']['status'], 'unknown')
        self.assertIn('configuration', result['calendars']['reason'])
        # The producer owns its store path; the consumer needs no database configuration.
        self.assertEqual(result['gsd']['status'], 'unknown')
        self.assertIn('read failed', result['gsd']['reason'])

    def test_sunday_calendar_read_covers_whole_new_week(self):
        sunday = datetime(2026, 10, 18, 23, 30, tzinfo=TEST_ZONE)
        calls = []
        def runner(argv):
            calls.append(argv)
            return '{"events": []}'
        result = senses.calendars({'schedule_calendar_id': 'b',
                                   'main_calendar_ids': ['main']}, sunday, runner)
        self.assertEqual(result['from'], '2026-10-18T00:00:00+03:00')
        self.assertEqual(result['to'], '2026-10-25T00:00:00+03:00')
        self.assertEqual(len(calls), 2)
        for argv in calls:
            self.assertEqual(argv[argv.index('--to') + 1], result['to'])
            self.assertEqual(argv[argv.index('--from') + 1], result['from'])

    def test_collect_calendar_window_uses_live_demand_deadline(self):
        from steward import calendar_b
        due = NOW + timedelta(days=10)
        with writer(self.root) as store:
            calendar_b.demand(store.db, 'add', 'later', dict(title='Later',
                minutes=30, kind='other', transparency='opaque',
                windows={'0': [['14:00', '16:00']]}, deadline=due.isoformat(),
                source='charter', justification='Known ahead'), NOW)
        calls = []
        def runner(argv):
            if argv[0] == 'gog':
                calls.append(argv)
                return '{"events": []}'
            raise OSError('unavailable fixture')
        result = self.collect(runner, {'schedule_calendar_id': 'b', 'main_calendar_ids': []})
        self.assertEqual(result['calendars']['status'], 'ok')
        self.assertEqual(datetime.fromisoformat(result['calendars']['data']['to']), due)
        self.assertEqual(len(calls), 2)  # Near planning read plus the first daily far read.
        self.assertEqual(datetime.fromisoformat(calls[0][calls[0].index('--to') + 1]), due)

    def test_calendar_digest_tracks_movement_not_order_or_descriptions(self):
        events = [dict(id=str(i), start={'dateTime': '2026-10-16T12:00:00Z'}, end={'dateTime': '2026-10-16T13:00:00Z'}, description='private') for i in range(2)]
        own = dict(id='s', start={'dateTime': '2026-10-16T15:00:00Z'}, end={'dateTime': '2026-10-16T16:00:00Z'}, extendedProperties={'private': {'event_key': 'gym:1'}})
        events.insert(0, own)
        settings = {'schedule_calendar_id': 'b', 'main_calendar_ids': []}
        def read():
            return senses.calendars(settings, NOW, lambda argv: json.dumps({'events': events}))
        first = read()
        events.reverse()
        events[0]['description'] = 'changed private'
        self.assertEqual(first['digest'], read()['digest'])
        # Provider metadata alone is not a calendar change.
        events[0]['updated'] = '2026-10-16T11:59:00Z'
        self.assertEqual(first['digest'], read()['digest'])
        # Steward's own block moving is not news; an owner edit to it arrives as learning.
        own['start'] = {'dateTime': '2026-10-16T14:00:00Z'}
        self.assertEqual(first['digest'], read()['digest'])
        events[0]['start'] = {'dateTime': '2026-10-16T11:00:00Z'}
        self.assertNotEqual(first['digest'], read()['digest'])
        self.assertNotIn('description', first['events'][0])

    def test_gsd_digest_and_deadline_projection(self):
        payload = fixture('gsd.worklist.v1')
        calls = []
        def runner(argv):
            calls.append(argv)
            return json.dumps(payload)
        def read():
            return senses.gsd(NOW, runner)
        first = read()
        self.assertEqual(calls, [['gsd', 'worklist', '--json']])
        self.assertEqual([d['id'] for d in first['deadlines']], ['overdue', 'boundary'])
        self.assertEqual(first['deadlines'][0]['project'], 'Fixture project')
        self.assertEqual(first['past_due_older'], {'count': 0, 'oldest_due_at': None})
        self.assertEqual(first['counts']['state']['ready'], 2)
        self.assertEqual(first['counts']['next_actor']['external'], 1)
        self.assertEqual(first['cut']['until'], payload['cut']['until'])
        self.assertEqual(first['cut_age_seconds'], 21600)
        payload['at'] = '2026-10-16T13:00:00+00:00'
        self.assertEqual(first['digest'], read()['digest'])
        payload['items'][0]['title'] = 'Changed obligation'
        self.assertNotEqual(first['digest'], read()['digest'])
        payload['cut']['until'] = None
        self.assertIsNone(read()['cut_age_seconds'])

    def test_bad_worklist_is_unknown_with_contract_reason(self):
        for bad in ('missing', 'type', 'schema', 'date', 'enum', 'json'):
            with self.subTest(bad=bad):
                payload = fixture('gsd.worklist.v1')
                item = payload['items'][0]
                if bad == 'missing':
                    del item['due_at']
                elif bad == 'type':
                    item['due_at'] = 123
                elif bad == 'schema':
                    payload['schema'] = 'other'
                elif bad == 'date':
                    item['due_at'] = 'private invalid timestamp'
                elif bad == 'enum':
                    item['state'] = 'private invalid state'
                output = 'invalid JSON' if bad == 'json' else json.dumps(payload)
                result = self.collect(lambda argv: output)['gsd']
                self.assertEqual(result['status'], 'unknown')
                self.assertTrue(result['reason'].startswith('gsd.worklist.v1: '))
                self.assertNotIn('private', result['reason'])
                self.assertNotIn('data', result)

    def test_briefing_keeps_all_deadlines_and_summarizes_older_boundary(self):
        payload = fixture('gsd.worklist.v1')
        base = payload['items'][0]
        payload['items'] = [dict(base, id=str(i), due_at=(NOW + timedelta(hours=i-6)).isoformat()) for i in range(11)]
        for identity, delta in [('11', -timedelta(days=7) + timedelta(seconds=1)),
                                ('12', timedelta(days=7)),
                                ('eight-days-late', -timedelta(days=8)),
                                ('lower-boundary', -timedelta(days=7)),
                                ('beyond-upper', timedelta(days=7, seconds=1))]:
            payload['items'].append(dict(base, id=identity, due_at=(NOW + delta).isoformat()))
        data = senses.gsd(NOW, lambda argv: json.dumps(payload))
        expected_ids = ['11'] + [str(i) for i in range(11)] + ['12']
        self.assertEqual([d['id'] for d in data['deadlines']], expected_ids)
        self.assertEqual(data['past_due_older'], {'count': 2, 'oldest_due_at': (NOW - timedelta(days=8)).isoformat()})
        self.assertEqual(data['counts']['state']['ready'], 16)
        briefing = {'gsd': dict(status='ok', data=data)}
        with writer(self.root) as store:
            text = memory.briefing(store.db, NOW, {'senses': briefing})
        self.assertLessEqual(len(text.encode()), memory.BRIEFING_BYTES)
        self.assertIn('as of Fri 16 Oct 2026 09:00 EEST', text)
        self.assertIn('id: 12', text)
        self.assertIn('oldest due at:', text)
        self.assertNotIn('"id": "eight-days-late"', text)
        self.assertNotIn('"id": "lower-boundary"', text)

    def write_rollout(self, name, at, primary):
        path = self.rollouts / '2026/10/16' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(timestamp=at, payload={'rate_limits': {'primary': primary, 'plan_type': None, 'rate_limit_reached_type': None}})) + '\n')

    def test_rate_limits_newest_observation_across_files_and_nulls(self):
        self.write_rollout('rollout-z.jsonl', '2026-10-16T10:00:00Z', {'used_percent': 10})
        self.write_rollout('rollout-a.jsonl', '2026-10-16T11:00:00Z', {'used_percent': 20, 'window_minutes': None, 'resets_at': None})
        result = senses.rate_observation(self.rollouts, NOW)
        self.assertEqual(result['used_percent'], 20)
        self.assertIsNone(result['resets_at'])
        self.assertIsNone(result['window_elapsed_fraction'])
        self.write_rollout('rollout-a.jsonl', '2026-10-16T11:00:00Z', None)
        self.assertIsNone(senses.rate_observation(self.rollouts, NOW)['used_percent'])

    def save(self, values, at=NOW):
        with writer(self.root) as store:
            with store.db:
                cid = jobs.change(store.db, 'tick', at, 'tick')
                senses.save(store.db, values, at, cid)
            return cid

    def test_state_age_and_refresh_interval(self):
        self.save(self.collect())
        with writer(self.root) as store:
            state = jobs.snapshot(store.db, now=NOW + timedelta(seconds=20))
        self.assertEqual(set(state['senses']), set(senses.INTERVALS))
        self.assertTrue(all(v['age'] == 20 for v in state['senses'].values()))
        self.assertEqual(set(self.collect()), {'machine', 'sol', 'gsd'})

    def test_manual_run_landings_are_not_gsd_events(self):
        gsd = lambda d: {'gsd': dict(status='ok', data={'digest': d})}
        cal = lambda d: {'calendars': dict(status='ok', data={'events': [], 'digest': d})}
        self.save(gsd('a') | cal('a'), at=NOW - timedelta(minutes=30))
        with writer(self.root) as store:
            jobs.job_verb(store.db, 'add', 'g', dict(justification='t', argv=[released()], needs={'every': {'min': '1h', 'max': '1d'}}, recurring=False), now=NOW)
            jobs.run_verb(store.db, 'now', 'g', 'test', now=NOW - timedelta(minutes=25))
            with store.db:
                store.db.execute('UPDATE runs SET started_at=?', ((NOW - timedelta(minutes=20)).isoformat(),))
        self.save(gsd('b') | cal('b'))
        with writer(self.root) as store:
            self.assertEqual(senses.events(store.db, 0), ['calendars'])  # an owner edit stays news
            with store.db:
                store.db.execute("UPDATE runs SET state='succeeded', ended_at=?", ((NOW - timedelta(minutes=5)).isoformat(),))
        self.save(gsd('c'), at=NOW + timedelta(minutes=30))
        with writer(self.root) as store:
            self.assertEqual(set(senses.events(store.db, 0)), {'calendars', 'gsd'})

    def test_gate_and_event_wake_calendar_change_with_damping(self):
        self.save({'calendars': dict(status='ok', data={'events': [], 'digest': 'a'})})
        with writer(self.root) as store:
            with store.db:
                cursor = store.db.execute('SELECT max(rowid) FROM changes').fetchone()[0]
                store.db.execute("INSERT INTO wakes(started_at,status,checkpoint,harness_run_id,change_cursor) VALUES (?,'completed','{}','fixture',?)", ((NOW - timedelta(minutes=30)).isoformat(), cursor))
        self.save({'calendars': dict(status='ok', data={'events': [], 'digest': 'b'})})
        with writer(self.root) as store:
            self.assertIn('calendars changed', runtime.gate(store.db))
        wakes = []
        runtime.tick(self.root, Path(self.tmp.name) / 'data', clock=lambda: NOW, settings={}, update_fetcher=lambda offset: [], telegram={'chat_id': 1, 'owner_user_id': 2, 'topics': {'Talk': 6}}, sender=lambda *a, **k: 1, sense_runner=unavailable, rollout_root=self.rollouts, wake_starter=lambda: wakes.append(True))
        self.assertEqual(wakes, [True])
        with writer(self.root) as store:
            with store.db:
                store.db.execute("INSERT INTO wakes(started_at,status,checkpoint) VALUES (?,'gated','{}')", (NOW.isoformat(),))
        runtime.tick(self.root, Path(self.tmp.name) / 'data', clock=lambda: NOW + timedelta(minutes=15), settings={}, update_fetcher=lambda offset: [], telegram={'chat_id': 1, 'owner_user_id': 2, 'topics': {'Talk': 6}}, sender=lambda *a, **k: 1, sense_runner=unavailable, rollout_root=self.rollouts, wake_starter=lambda: wakes.append(True))
        self.assertEqual(wakes, [True])

    def test_owner_edit_of_steward_block_is_a_calendar_event(self):
        from test_calendar_b import Gog, SETTINGS, SPEC, NOW as MONDAY
        from steward import calendar_b
        gog = Gog()
        with writer(self.root) as store:
            calendar_b.demand(store.db, 'add', 'focus', SPEC, MONDAY)
            choice = next(c for c in calendar_b.plan(store.db, SETTINGS, MONDAY, gog)['candidates']['choices'] if c['action'] == 'place')
            calendar_b.choose(store.db, SETTINGS, {'schema_version': 1, 'selections': [{'candidate_id': choice['id'], 'reason': 'r'}]}, MONDAY, runner=gog)

        def tick(minutes):
            runtime.tick(self.root, Path(self.tmp.name) / 'data', clock=lambda: MONDAY + timedelta(minutes=minutes), settings=SETTINGS, update_fetcher=lambda offset: [], telegram={'chat_id': 1, 'owner_user_id': 2, 'topics': {'Talk': 6}}, sender=lambda *a, **k: 1, sense_runner=gog, rollout_root=self.rollouts, wake_starter=lambda: None)
            with writer(self.root) as store:
                return store.db.execute("SELECT event FROM senses WHERE name='calendars' ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(tick(20), 0)
        gog.events['schedule'][0]['summary'] = 'Owner title'
        self.assertEqual(tick(40), 1)
        self.assertEqual(tick(60), 0)

    def test_machine_read_failure_does_not_wake_and_history_is_bounded(self):
        self.save({'machine': dict(status='ok', data={})})
        self.save({'machine': dict(status='unknown', reason='read failed')})
        with writer(self.root) as store:
            # Losing sight is not a change in the world: no wake; a long unknown reaches Alerts.
            self.assertNotIn('machine changed', runtime.gate(store.db))
        for i in range(100):
            self.save({'machine': dict(status='unknown', reason='read failed')}, NOW + timedelta(minutes=i))
        with writer(self.root) as store:
            self.assertEqual(store.db.execute("SELECT count(*) FROM senses WHERE name='machine'").fetchone()[0], 96)

    @staticmethod
    def feed_page(source='x', count=1, start=0, complete=True):
        items = [dict(token=f't{i}', id=f'{source}:{i}', item=f'{source}:{i}',
                      source=source, kind='post', time=1, arrived=1792400000,
                      title='private', **{'from': None}, thread=None, chat=None,
                      version='v1', tombstone=False, reparse=False)
                 for i in range(start, start + count)]
        return dict(schema='qmd.feed.v1', source=source, items=items,
                    next=items[-1]['token'] if items else None,
                    head=items[-1]['token'] if items and complete else 'later', complete=complete)

    def feed_runner(self, pages, calls):
        def runner(argv):
            calls.append(argv)
            if argv[1] == 'status':
                return json.dumps(fixture('qmd.status.v1'))
            self.assertEqual(argv[1], 'feed')
            return json.dumps(pages[argv[argv.index('--source') + 1]])
        return runner

    def test_qmd_counts_late_arrivals_tombstones_and_versions_only(self):
        page = self.feed_page(count=3)
        page['items'][1]['tombstone'] = True
        page['items'][2].update(id='x:0', version='v2', reparse=True)
        calls = []
        result = senses.qmd({'qmd_watch_sources': ['x']},
                            self.feed_runner({'x': page}, calls), {})
        self.assertEqual(result['new_items']['x'], dict(count=2, at_least=False))
        self.assertEqual(calls[0], ['qmd', 'status', '--json'])
        self.assertFalse(result['alive'])
        self.assertEqual(result['verify']['state'], 'red')
        self.assertEqual(result['verify']['red_checks'], ['ingestd_heartbeat'])
        self.assertIsNone(result['sources']['instapaper']['fresh_through'])
        self.assertIsNone(result['sources']['instapaper']['current'])
        self.assertFalse(result['sources']['gmail']['current'])
        self.assertNotIn('private', json.dumps(result))
        for deleted in ('cursors', 'offsets', 'windows', 'cursor_initialized'):
            self.assertNotIn(deleted, result)

    def test_qmd_multiple_pages_complete_and_restart_dedupe(self):
        pages = {'x': self.feed_page(count=100, complete=False)}
        calls = []
        runner = self.feed_runner(pages, calls)
        settings = {'qmd_watch_sources': ['x']}
        first = senses.qmd(settings, runner, {})
        self.assertEqual(first['new_items']['x'], dict(count=100, at_least=True))
        self.save({'qmd': dict(status='ok', data=first)})
        # Reload through SQLite, including JSON's tuple-to-list conversion.
        with writer(self.root) as store:
            prior = senses.snapshot(store.db, NOW)['qmd']['data']
        pages['x'] = self.feed_page(count=3, start=99)
        second = senses.qmd(settings, runner, prior)
        self.assertEqual(second['new_items']['x'], dict(count=2, at_least=False))
        self.assertEqual(calls[-1][calls[-1].index('--after') + 1], 't99')
        self.assertEqual(len(second['feed']['x']['seen']), 100)
        self.assertEqual(len([c for c in calls if c[1] == 'feed']), 2)
        again = senses.qmd(settings, runner, second)
        self.assertEqual(again['new_items']['x']['count'], 0)
        self.save({'qmd': dict(status='ok', data=second)})
        with writer(self.root) as store:
            self.assertEqual(senses.events(store.db, 0), [])
            self.assertEqual(runtime.gate(store.db), 'nothing new')

    def test_qmd_complete_is_not_inferred_from_page_size(self):
        calls = []
        result = senses.qmd({'qmd_watch_sources': ['x']},
            self.feed_runner({'x': self.feed_page(count=100)}, calls), {})
        self.assertEqual(result['new_items']['x'], dict(count=100, at_least=False))

    def test_qmd_observation_and_token_roll_back_together(self):
        original = dict(feed={'x': dict(token='old', seen=[])})
        self.save({'qmd': dict(status='ok', data=original)})
        with writer(self.root) as store:
            with self.assertRaisesRegex(ValueError, 'rollback'):
                with store.db:
                    cid = jobs.change(store.db, 'tick', NOW, 'tick')
                    senses.save(store.db, {'qmd': dict(status='ok', data=dict(
                        new_items={'x': dict(count=1, at_least=False)},
                        feed={'x': dict(token='new', seen=[['x:1', 'v1']])}))}, NOW, cid)
                    raise ValueError('rollback')
            self.assertEqual(senses.snapshot(store.db, NOW)['qmd']['data'], original)

    def test_qmd_bootstrap_keeps_saved_cut_or_epoch_never_now(self):
        epoch = '2026-10-17T12:14:40.844838+00:00'
        for cut, expected in [(None, epoch), ('2026-10-15T00:00:00Z', epoch),
                              ('2026-10-18T10:00:00Z', '2026-10-18T10:00:00+00:00'),
                              (epoch, epoch)]:
            with self.subTest(cut=cut):
                calls = []
                old = dict(feed={'x': dict(since_arrival=cut, token=None, seen=[])} if cut else {})
                result = senses.qmd({'qmd_watch_sources': ['x']},
                    self.feed_runner({'x': self.feed_page()}, calls), old)
                self.assertEqual(calls[-1][calls[-1].index('--since-arrival') + 1], expected)
                self.assertEqual(result['new_items']['x']['count'], 1)

    def test_qmd_failure_or_contract_mismatch_preserves_all_checkpoints(self):
        original = dict(feed={'x': dict(token='saved-x', seen=[['x:old', 'v1']]),
                              'instapaper': dict(token='saved-i', seen=[])})
        for bad in ('command', 'json', 'schema', 'missing', 'type', 'source', 'item-source'):
            with self.subTest(bad=bad):
                self.save({'qmd': dict(status='ok', data=original)}, NOW - timedelta(hours=1))
                page = self.feed_page(source='instapaper')
                if bad == 'schema':
                    page['schema'] = 'wrong'
                elif bad == 'missing':
                    del page['items'][0]['version']
                elif bad == 'type':
                    page['complete'] = 'yes'
                elif bad == 'source':
                    page['source'] = 'x'
                elif bad == 'item-source':
                    page['items'][0]['source'] = 'x'
                def runner(argv):
                    if argv[1] == 'status':
                        return json.dumps(fixture('qmd.status.v1'))
                    if argv[argv.index('--source') + 1] == 'x':
                        return json.dumps(self.feed_page())
                    if bad == 'command':
                        raise OSError('private')
                    return 'private invalid JSON' if bad == 'json' else json.dumps(page)
                result = self.collect(runner, {'qmd_watch_sources': ['x', 'instapaper']})['qmd']
                self.assertEqual(result['status'], 'unknown')
                self.assertNotIn('private', result['reason'])
                self.save({'qmd': result})
                with writer(self.root) as store:
                    saved = senses.snapshot(store.db, NOW)['qmd']
                    self.assertEqual(saved['data'], original)
                    self.assertEqual(senses.events(store.db, 0), [])

    def test_qmd_mismatch_is_unknown_and_health_never_wakes(self):
        payload = fixture('qmd.status.v1')
        del payload['alive']
        unknown = self.collect(lambda argv: json.dumps(payload))['qmd']
        self.assertEqual(unknown['status'], 'unknown')
        self.assertIn('qmd.status.v1: $.alive: missing', unknown['reason'])
        self.save({'qmd': dict(status='ok', data={'alive': True})})
        self.save({'qmd': dict(status='ok', data={'alive': False})})
        self.save({'qmd': unknown}, NOW + timedelta(hours=3))
        with writer(self.root) as store:
            self.assertEqual(senses.events(store.db, 0), [])
            self.assertEqual(runtime.gate(store.db), 'nothing new')
            from steward import messages
            messages.alerts(store.db, NOW + timedelta(hours=6), lambda *a, **k: self.fail('qmd health alert'), {})
            self.assertEqual(store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)

    def test_burn_rate_from_history(self):
        reset = int((NOW + timedelta(days=3)).timestamp())
        self.write_rollout('rollout-a.jsonl', '2026-10-16T10:00:00Z', {'used_percent': 10, 'window_minutes': 10080, 'resets_at': reset})
        self.save(self.collect())
        self.write_rollout('rollout-a.jsonl', '2026-10-16T11:00:00Z', {'used_percent': 12, 'window_minutes': 10080, 'resets_at': reset})
        self.assertEqual(self.collect()['sol']['data']['burn_percent_per_hour'], 2)

    def test_command_adapter_bounds_reads_and_hides_provider_errors(self):
        from unittest.mock import patch
        with patch('steward.senses.subprocess.run') as command:
            command.return_value = subprocess.CompletedProcess(['fixture'], 1, '', 'private')
            for argv, timeout in [(['fixture'], 60), (['qmd', 'status', '--json'], 30)]:
                with self.subTest(argv=argv), self.assertRaisesRegex(ValueError, '^external command failed$'):
                    senses.run(argv)
                self.assertEqual(command.call_args.kwargs['timeout'], timeout)
            command.side_effect = subprocess.TimeoutExpired('private', 60, stderr='private')
            with self.assertRaises(subprocess.TimeoutExpired):
                senses.run(['fixture'])

    def test_machine_reports_timer_violation(self):
        def runner(argv):
            if 'show' in argv:
                return state
            return ''
        state = 'loaded\nenabled\n'
        result = senses.machine(runner)
        self.assertEqual(result['violation'], 'gsd-step.timer enabled')
        self.assertEqual(len(result['loadavg']), 3)
        state = 'not-found\n\n'  # never installed is a known, healthy fact
        result = senses.machine(runner)
        self.assertEqual((result['gsd_step_timer'], result['violation']), ('not-installed', None))
