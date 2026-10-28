from fixture_mind import use_settings
import unittest
from datetime import datetime, timedelta, timezone
from steward import senses

NOW = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)


class PageTests(unittest.TestCase):
    setUp = use_settings

    def test_known_absence_is_not_failed_reading(self):
        from steward.briefing import render
        from steward.state import writer
        from steward import jobs
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as root, writer(Path(root), initialize=True) as store:
            state = jobs.snapshot(store.db, now=NOW)
        self.assertEqual(state['provocation'], [])
        state['senses']['calendars'] = dict(status='ok', at=NOW.isoformat(), data={'events': []})
        page = render(state, NOW, {})
        self.assertIn('- provocation:\n  - none', page)
        self.assertIn('- current: none', page)
        self.assertIn('## Calendar [fresh]', page)
        state['senses']['calendars'] = dict(status='unknown', reason='provider unreachable')
        page = render(state, NOW, {})
        calendar = page.split('## Calendar ', 1)[1].split('## Movement', 1)[0]
        self.assertIn('unknown: provider unreachable', calendar)
        self.assertNotIn('- none', calendar)

    def test_calendar_keeps_three_full_days_sorted_without_ten_event_cut(self):
        from steward.briefing import render
        events = [dict(summary='event ' + str(i), start={'dateTime': f'2026-10-22T{i:02}:00:00+03:00'},
                       end={'dateTime': f'2026-10-22T{i:02}:30:00+03:00'}) for i in range(23)]
        events += [dict(summary='all day', start={'date': '2026-10-20'}, end={'date': '2026-10-21'}),
                   dict(summary='outside', start={'date': '2026-10-23'}, end={'date': '2026-10-24'})]
        page = render({'senses': {'calendars': dict(status='ok', at=NOW.isoformat(), data={'events': list(reversed(events))})}}, NOW, {})
        self.assertIn('event 22', page)
        self.assertNotIn('outside', page)
        self.assertLess(page.index('all day'), page.index('event 0'))
        self.assertLess(page.index('event 2\n'), page.index('event 22'))
        self.assertIn('Thu 22 Oct 2026 22:00 EEST', page)


    def test_page_is_ordered_and_overflow_is_not_eviction(self):
        from steward import memory
        from steward.state import writer, StateError
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root, writer(Path(root), initialize=True) as store:
            with patch('steward.mind.read', return_value='stable'):
                receipt = {}
                page = memory.briefing(store.db, NOW, {'inbox': ['important']}, receipt=receipt)
                headings = ['Now', 'Inbox', 'Calendar', 'Movement', 'Blocks', 'Your list', 'Waiting on others', 'Deadlines',
                            'Compute', 'Finds waiting', 'Health', 'Place', 'Machine and qmd']
                positions = [page.index('## ' + h + ' [') for h in headings]
                self.assertEqual(positions, sorted(positions))
                self.assertIn('important', page)
                self.assertTrue(receipt['complete'])
                self.assertIn('Health', receipt['unknown_sections'])
                with self.assertRaisesRegex(StateError, 'briefing page exceeds'):
                    memory.briefing(store.db, NOW, {'inbox': ['x' * 200000]}, receipt=receipt)
                self.assertFalse(receipt['complete'])
                self.assertGreater(receipt['bytes'], memory.BRIEFING_BYTES)


class DayCloseTests(unittest.TestCase):
    setUp = use_settings

    def test_evening_has_four_parts_and_honest_missing_done_and_horizon(self):
        from steward import memory
        from steward.state import writer
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as root, writer(Path(root), initialize=True) as store:
            page = memory.briefing(store.db, NOW, {'moments': ['day_close']})
        for title in ('Done today', 'Next two days', 'Horizon', 'Long form'):
            self.assertIn('## Day close — ' + title, page)
        self.assertIn('unknown: gsd done not supplied', page)
        self.assertIn('See Ahead for future events and its observed coverage.', page)
        self.assertIn('unknown: gsd horizon not supplied', page)
        self.assertIn('handled is not proof of an answer', page)

    def test_outcome_projection_is_one_line_and_does_not_expose_message_bodies(self):
        from steward.briefing import day_close
        from unittest.mock import patch
        journal = dict(truncated=False, changes=[], calendar_b=[],
                       runs=[dict(job='backup', state='succeeded', ended_at=NOW.isoformat(),
                                  started_at=None, planned_for=NOW.isoformat(), run_id='hidden-run')],
                       messages=[dict(topic='Today', sent_at=NOW.isoformat(),
                                      text='first line ' + 'x' * 100 + '\nPRIVATE SECOND LINE', message_id=123)],
                       inbox=[dict(handled=1, text='PRIVATE OWNER BODY'), dict(handled=0)])
        with patch('steward.memory.journal', return_value=journal):
            evidence = day_close(None, NOW)
        self.assertEqual(evidence['runs'], ['12:00 EEST backup succeeded'])
        self.assertEqual(evidence['owner_messages'], '2 received; 1 handled')
        self.assertEqual(len(evidence['deliveries'][0].split('Today: ', 1)[1]), 80)
        self.assertNotIn('PRIVATE', str(evidence))
        self.assertNotIn('hidden-run', str(evidence))

    def test_evening_72_ticks_are_not_outcomes_and_overhead_is_small(self):
        from steward import memory, jobs
        from steward.state import writer
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as root, writer(Path(root), initialize=True) as store:
            for i in range(72):
                jobs.change(store.db, 'tick', NOW - timedelta(minutes=i))
            for verb in ('release', 'mind commit'):
                jobs.change(store.db, verb, NOW)
            cid = jobs.change(store.db, 'block choose', NOW)
            store.db.execute('UPDATE changes SET summary=? WHERE id=?',
                             ('Moved movement to tomorrow. ' + 'detail ' * 100, cid))
            state = {'moments': []}
            normal = memory.briefing(store.db, NOW, state)
            evening = memory.briefing(store.db, NOW, dict(state, moments=['day_close']))
        extra = evening.split('## Day close — Done today', 1)[1].split('## Health', 1)[0]
        for forbidden in ('tick', 'release', 'mind commit', cid, 'change id:', 'undo of:', 'actor:', 'subject:'):
            self.assertNotIn(forbidden, extra)
        outcome = next(line for line in extra.splitlines() if 'block choose:' in line)
        self.assertTrue(outcome.lstrip().startswith('- 12:00 EEST block choose: Moved movement'))
        self.assertLessEqual(len(outcome.split('block choose: ', 1)[1]), 100)
        self.assertLessEqual(len(evening.encode()) - len(normal.encode()), 8192)

    def test_evening_uses_full_today_journal_not_recent_change_cutoff(self):
        from steward import memory, jobs
        from steward.state import writer
        from pathlib import Path
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as root, writer(Path(root), initialize=True) as store:
            for i in range(25):
                jobs.change(store.db, 'change ' + str(i), NOW)
            cid = jobs.change(store.db, 'block choose', NOW)
            store.db.execute('UPDATE changes SET summary=? WHERE id=?', ('Deleted movement block', cid))
            jobs.append(store.db, 'blocks', dict(event_key='removed-block', demand='movement',
                        state='deleted', payload=json.dumps({'start': NOW.isoformat(),
                        'end': (NOW + timedelta(hours=1)).isoformat()})), cid)
            state = dict(moments=['day_close'], senses={'gsd': dict(status='ok', at=NOW.isoformat(),
                         data={'done': [], 'horizon_deadlines': []}), 'calendars': dict(status='ok',
                         at=NOW.isoformat(), data={'events': [], 'from': NOW.isoformat(),
                                                  'to': (NOW + timedelta(days=7)).isoformat()})})
            page = memory.briefing(store.db, NOW, state)
        self.assertIn('Deleted movement block', page)
        self.assertNotIn('removed-block', page)
        self.assertIn('12:00 EEST change 0: change 0', page)
        self.assertEqual(page.count('12:00 EEST change 0: change 0'), 1)
        self.assertNotIn('unknown: gsd done not supplied', page)
        self.assertIn('See Ahead for future events and its observed coverage.', page)

    def test_horizon_keeps_calendar_events_beyond_next_two_days(self):
        from steward.briefing import render
        state = dict(moments=['day_close'], senses={'calendars': dict(status='ok', at=NOW.isoformat(),
                     data={'events': [], 'ahead': dict(status='ok', at=NOW.isoformat(),
                         to='2028-10-20T00:00:00+03:00', events=[dict(id='trip', calendar='main',
                             summary='trip', start={'date': '2026-10-24'}, end={'date': '2026-10-25'})])})})
        page = render(state, NOW, {})
        self.assertIn('trip', page.split('## Ahead', 1)[1].split('## Movement', 1)[0])
        self.assertNotIn('trip', page.split('## Day close — Horizon', 1)[1])


class FreshnessTests(unittest.TestCase):
    setUp = use_settings

    def test_qmd_cached_currency_uses_producer_deadline_not_limit_arithmetic(self):
        source = dict(cadence_s=900, fresh_through=NOW.timestamp(), current=True,
                      limit_s=999999, valid_until=NOW.timestamp() + 1)
        reading = dict(status='ok', at=NOW.isoformat(), data=dict(alive=True, sources={'x': source}))
        self.assertEqual(senses.freshness('qmd', reading, NOW)['verdict'], 'fresh')
        self.assertEqual(senses.freshness('qmd', reading, NOW + timedelta(seconds=1))['verdict'], 'fresh')
        self.assertEqual(senses.freshness('qmd', reading, NOW + timedelta(seconds=2))['verdict'], 'stale')
        del source['valid_until']
        self.assertEqual(senses.freshness('qmd', reading, NOW)['verdict'], 'unknown')

    def sense(self, data):
        return dict(status='ok', at=NOW.isoformat(), data=data)

    def test_waiting_includes_undated_owner_items_in_priority_order(self):
        import json
        from pathlib import Path
        raw = json.loads(Path('tests/fixtures/gsd.worklist.v1.json').read_text())
        template = raw['items'][0]
        raw['items'] = [dict(template, id=str(i), title='task ' + str(i), priority=str(i * 10),
                             due_at=None if i % 2 else template['due_at'],
                             next_actor='user' if i < 7 else 'agent') for i in range(8)]
        raw['priorities']['mine'] = [dict(id=str(i), title='task ' + str(i), priority=str(i * 10),
            due_at=None if i % 2 else template['due_at'], project=raw['projects'][0]['name'])
            for i in (6, 5, 4, 3, 2, 1, 0)]
        data = senses.gsd(NOW, lambda argv: json.dumps(raw))
        self.assertEqual([i['priority'] for i in data['waiting_on_you']], ['60', '50', '40', '30', '20', '10', '0'])
        self.assertIsNone(data['waiting_on_you'][1]['due_at'])
        self.assertEqual(data['waiting_on_you'][0]['project'], raw['projects'][0]['name'])

    def test_gsd_done_filters_athens_day_and_does_not_count_cancellations(self):
        import json
        from pathlib import Path
        raw = json.loads(Path('tests/fixtures/gsd.worklist.v1.json').read_text())
        event = dict(id='closed', title='Completed', project=None, state='done')
        raw['done'] = [dict(event, at='2026-10-19T20:59:59Z'),
                       dict(event, id='athens-today', at='2026-10-19T21:00:00Z'),
                       dict(event, id='later-today', at='2026-10-20T09:01:00Z'),
                       dict(event, id='canceled', state='canceled', at='2026-10-20T08:00:00Z')]
        data = senses.gsd(NOW, lambda argv: json.dumps(raw))
        self.assertEqual([event['id'] for event in data['done']], ['athens-today'])
        self.assertEqual([event['id'] for event in data['canceled_today']], ['canceled'])
        raw['done'][0]['at'] = 'invalid'
        with self.assertRaisesRegex(ValueError, 'done.at'):
            senses.gsd(NOW, lambda argv: json.dumps(raw))

    def test_gsd_missing_done_is_unknown_and_distant_deadlines_are_kept(self):
        import json
        from pathlib import Path
        raw = json.loads(Path('tests/fixtures/gsd.worklist.v1.json').read_text())
        from steward.contracts import ContractError
        missing = dict(raw)
        missing.pop('done', None)
        with self.assertRaisesRegex(ContractError, 'done'):
            senses.gsd(NOW, lambda argv: json.dumps(missing))
        raw['items'] = [dict(raw['items'][0], due_at='2027-01-01T12:00:00Z')]
        data = senses.gsd(NOW, lambda argv: json.dumps(raw))
        self.assertEqual(data['horizon_deadlines'][0]['due_at'], '2027-01-01T12:00:00Z')
        self.assertEqual(data['deadlines'], [])

    def test_gsd_target_uses_live_schedule_and_previous_day_before_first_target(self):
        import json
        from unittest.mock import patch
        from steward.timing import latest_job_obligation, GSD_JOB
        job = dict(name='gsd-steady', state='active', spec_json=json.dumps({'needs': {'ready_by': ['06:30', '18:30']}}))
        run = dict(job='gsd-steady', state='succeeded', started_at='2026-10-19T14:40:00Z',
                   ended_at='2026-10-19T15:00:00Z')
        with patch('steward.jobs.current', side_effect=lambda db, table: [job] if table == 'jobs' else [run]), \
             patch('steward.jobs.execution_profile', return_value={'measured_at': '2026-01-01T00:00:00+00:00'}):
            before_morning = latest_job_obligation(None, GSD_JOB, datetime.fromisoformat('2026-10-20T02:00:00Z'))
            self.assertEqual(before_morning['target'], '2026-10-19T18:30:00+03:00')
            self.assertEqual(datetime.fromisoformat(before_morning['started_at']), datetime.fromisoformat(run['started_at']))
            morning = latest_job_obligation(None, GSD_JOB, NOW)
            self.assertEqual(morning['target'], '2026-10-20T06:30:00+03:00')
            self.assertIsNone(morning['completed_at'])
            self.assertEqual(morning['state'], 'missed')

    def test_gsd_uses_kept_ready_by_and_cut_commit_not_age(self):
        promise = dict(target='2026-10-20T06:30:00+03:00', state='kept', completed_at='2026-10-20T03:00:00Z',
                       started_at='2026-10-20T02:40:00Z')
        sense = self.sense(dict(cut=dict(until='2026-10-20T02:30:00Z', committed_at='2026-10-20T02:59:00Z'), promise=promise))
        self.assertEqual(senses.freshness('gsd', sense, NOW)['verdict'], 'fresh')
        sense['data']['cut']['committed_at'] = '2026-10-19T02:59:00Z'
        verdict = senses.freshness('gsd', sense, NOW)
        self.assertEqual(verdict['verdict'], 'stale')
        self.assertIn('06:30', verdict['reason'])
        promise.update(state='missed', completed_at=None)
        self.assertEqual(senses.freshness('gsd', sense, NOW)['verdict'], 'stale')

    def test_producer_verdicts(self):
        health = self.sense(dict(auth='ok', watch_uploaded_at=NOW.isoformat(),
                                 watch_fresh_until=(NOW + timedelta(seconds=1)).isoformat()))
        self.assertEqual(senses.freshness('health', health, NOW)['verdict'], 'fresh')
        self.assertEqual(senses.freshness('health', health, NOW + timedelta(seconds=1))['verdict'], 'stale')
        qmd = self.sense(dict(alive=True, sources={'x': dict(fresh_through=NOW.timestamp(), current=False)}))
        self.assertEqual(senses.freshness('qmd', qmd, NOW)['verdict'], 'stale')
        qmd['data']['sources']['x']['fresh_through'] = None
        self.assertEqual(senses.freshness('qmd', qmd, NOW)['verdict'], 'unknown')
        # Unscheduled lanes (null cadence_s, e.g. manual) make no currency claim and don't make qmd unknown.
        qmd = self.sense(dict(alive=True, sources={
            'x': dict(fresh_through=NOW.timestamp(), current=True, valid_until=NOW.timestamp() + 3600, cadence_s=900),
            'manual': dict(fresh_through=None, current=None, limit_s=3600, cadence_s=None)}))
        self.assertEqual(senses.freshness('qmd', qmd, NOW)['verdict'], 'fresh')
        self.assertEqual(senses.freshness('gsd', self.sense({'cut': {'until': None}}), NOW)['verdict'], 'unknown')
        self.assertEqual(senses.freshness('calendars', {'status': 'unknown'}, NOW)['verdict'], 'unknown')
        self.assertEqual(senses.freshness('place', self.sense({'state': 'unknown'}), NOW)['verdict'], 'fresh')
        self.assertEqual(senses.freshness('place', self.sense({'state': 'out'}), NOW)['verdict'], 'fresh')
