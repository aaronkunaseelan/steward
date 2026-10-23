from fixture_mind import TEST_ZONE
from datetime import timedelta
import io
import json
from contextlib import redirect_stdout
import time
import unittest
from unittest.mock import patch

import test_jobs
from test_jobs import NOW
from steward import jobs, runtime, planner, messages, tick_skips
from steward.cli import main
from steward.state import StateError, writer


class SkipTests(unittest.TestCase):
    setUp = test_jobs.JobsTests.setUp
    add = test_jobs.JobsTests.add
    tick = test_jobs.JobsTests.tick
    run_now = test_jobs.JobsTests.run_now

    def skip(self, start=NOW, end=NOW+timedelta(hours=2)):
        with writer(self.root) as store:
            return tick_skips.skip(store.db, start.isoformat(), end.isoformat(), 'quiet night', now=NOW, actor='model')

    def test_skip_refreshes_qmd_without_any_wake_and_keeps_intent_green(self):
        from steward import senses, intent
        at = NOW.replace(hour=2)
        later = at + timedelta(hours=1)
        with writer(self.root) as store:
            tick_skips.skip(store.db, at.isoformat(), (at + timedelta(hours=2)).isoformat(), 'quiet night', now=at)
        def readings(now):
            result = {name: dict(status='ok', data={}) for name in senses.INTERVALS
                      if name not in ('health', 'place')}
            result['calendars']['data'] = dict(events=[], digest='unchanged')
            result['gsd']['data'] = dict(cut=dict(until=now.isoformat(), committed_at=now.isoformat()),
                                         waiting_on_you=[], notion_urls={}, digest='unchanged')
            result['qmd']['data'] = dict(alive=True, sources={'files': dict(
                cadence_s=1200, current=True, fresh_through=now.timestamp(), valid_until=now.timestamp() + 1800)})
            result['sol']['data'] = dict(observed_at=now.isoformat())
            return result
        with writer(self.root) as store:
            jobs.job_verb(store.db, 'add', 'gsd-steady', test_jobs.spec(needs={'ready_by': ['00:01']}),
                          now=at - timedelta(days=1))
        with writer(self.root) as store, store.db:
            cid = jobs.change(store.db, 'fixture', at)
            served = at.astimezone(TEST_ZONE).replace(hour=0, minute=1).isoformat()
            jobs.append(store.db, 'runs', dict(run_id='kept', job='gsd-steady', state='succeeded',
                planned_for=served, started_at=served, ended_at=served, reason='fixture',
                spec_json=jobs.get(store.db, 'jobs', 'gsd-steady')['spec_json']), cid)
            senses.save(store.db, readings(at), at, cid)
        wakes = []
        with patch('steward.senses.collect', return_value=readings(later)) as collect:
            result = self.tick(at=later, wake_starter=lambda: wakes.append(True), settings={'wake_times': ['05:00'], 'schedule_calendar_id': 'schedule', 'main_calendar_ids': ['main']})
        collect.assert_called_once()
        self.assertEqual(wakes, [])
        self.assertIsNone(result['wake'])
        self.assertFalse(result['scheduled_wake'])
        with writer(self.root) as store:
            self.assertEqual(senses.snapshot(store.db, later)['qmd']['at'], later.isoformat())
            with patch('steward.intent.ticks', return_value=dict(window_h=24, total=1, idle=1, launched=0, woke=0, skipped=1)):
                doc = intent.steward_doc(store.db, later, {}, self.data)
                self.assertEqual(doc['state'], 'green', doc['headline'])

    def test_skip_still_plans_and_launches_recurring_work(self):
        self.add()
        self.skip()
        wakes = []
        result = self.tick(wake_starter=lambda: wakes.append(True))
        self.assertEqual(len(result['started']), 1)
        self.assertEqual(wakes, [])
        self.assertIsNone(result['wake'])

    def test_skip_does_not_pause_read_ages(self):
        from steward import senses
        self.skip()
        with writer(self.root) as store, store.db:
            senses.save(store.db, {'machine': dict(status='ok', data={})}, NOW, jobs.change(store.db, 'fixture', NOW))
            self.assertEqual(senses.snapshot(store.db, NOW + timedelta(hours=1))['machine']['age'], 3600)

    def test_limits_and_cli_undo_not_told(self):
        for start,end,reason in [(NOW,NOW,'after'), (NOW-timedelta(hours=2),NOW-timedelta(hours=1),'future'), (NOW,NOW+timedelta(hours=8,seconds=1),'8 hours')]:
            with self.assertRaisesRegex(StateError, reason):
                self.skip(start,end)
        with patch('steward.jobs.now_utc', return_value=NOW), patch('steward.tick_skips.now_utc', return_value=NOW), patch.dict('os.environ', STEWARD_ACTOR='model'):
            output=io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['tick','--state',str(self.root),'skip','--from',NOW.isoformat(),'--to',(NOW+timedelta(hours=8)).isoformat(),'--reason','quiet']),0)
            cid=json.loads(output.getvalue())['change_id']
        with writer(self.root) as store:
            db=store.db
            self.assertEqual(jobs.snapshot(db,now=NOW)['tick_skips']['current']['change_id'],cid)
            self.assertEqual(db.execute('SELECT actor FROM changes WHERE id=?',(cid,)).fetchone()[0],'model')
            sent=[]
            messages.changes(db, NOW,lambda *a,**k: sent.append(a),{})
            self.assertEqual(sent,[])
            jobs.undo(db,cid,now=NOW,actor='model')
            self.assertIsNone(tick_skips.status(db,NOW)['current'])
            messages.changes(db, NOW,lambda *a,**k: sent.append(a),{})
            self.assertEqual(sent,[])
            self.assertEqual(db.execute('SELECT count(*) FROM tick_skips').fetchone()[0],2)

    def test_ready_by_crossing_is_allowed_because_planning_continues(self):
        self.add(needs={'ready_by':['18:30']},max_minutes=30)
        end=NOW+timedelta(hours=4)
        self.assertIn('change_id', self.skip(end=end))
        # Existing reservations and successes also remain compatible with a skip.
        self.skip(end=NOW+timedelta(minutes=20))
        with writer(self.root) as store:
            db=store.db
            rid=jobs.run_verb(db,'plan','test','promised',(NOW+timedelta(hours=1)).isoformat(),now=NOW)['run_id']
        self.skip(end=end)
        with writer(self.root) as store:
            db=store.db
            row=jobs.get(db,'runs',rid)
            self.assertEqual(row['planned_for'],(NOW+timedelta(hours=1)).isoformat())
            with db:
                row.update(state='succeeded',started_at=NOW.isoformat(),ended_at=(NOW+timedelta(minutes=1)).isoformat())
                jobs.append(db,'runs',row,row['change_id'])
        self.skip(end=end)

    def test_skip_preserves_deterministic_work_and_suppresses_wakes(self):
        self.add(recurring=False)
        rid=self.run_now()
        self.skip()
        order=[]
        def fetch(offset):
            order.append('poll')
            return [dict(update_id=1,message=dict(message_id=7,message_thread_id=6,chat={'id':1},**{'from':{'id':2}},text='awake'))]
        with patch('steward.senses.collect', wraps=runtime.senses.collect) as collect, patch('steward.runtime.placement',side_effect=AssertionError('nonrecurring planner')):
            begin=time.perf_counter()
            result=self.tick(update_fetcher=fetch,launcher=lambda a: order.append('launch'),wake_starter=lambda: order.append('wake'))
            elapsed=time.perf_counter()-begin
        self.assertLess(elapsed,1)
        print(f'Injected skip tick: {elapsed:.6f}s')
        collect.assert_called_once()
        self.assertEqual(order,['poll','launch'])
        self.assertFalse(result['scheduled_wake'])
        self.assertIsNone(result['wake'])
        self.assertEqual(result['started'],[rid])
        self.assertEqual(result['skipping'],(NOW+timedelta(hours=2)).isoformat())
        path=self.data/'runs'/'test'/rid/'manifest.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(run_id=rid,job='test',started_at=NOW.isoformat(),ended_at=(NOW+timedelta(minutes=1)).isoformat(),exit_code=0,status='succeeded')))
        self.assertEqual(self.tick(at=NOW+timedelta(minutes=20))['folded'],[rid])
        with patch('steward.senses.collect', wraps=runtime.senses.collect) as collect:
            result=self.tick(at=NOW+timedelta(hours=2))
            self.assertTrue(collect.called)
        self.assertEqual(result['folded'],[])
        self.assertNotIn('skipping',result)
        with writer(self.root) as store:
            self.assertEqual(messages.inbox(store.db)[0]['text'],'awake')

    def test_compute_grid_and_calendar_b_grid(self):
        self.add()
        with writer(self.root) as store:
            job=jobs.get(store.db,'jobs','test')
            self.assertEqual(planner.rule(job,[],NOW+timedelta(minutes=15),NOW),'20-minute grid')
            self.assertIsNone(planner.rule(job,[],NOW+timedelta(minutes=20),NOW))
            for choice in planner.choices(store.db,NOW)[0]['feasible']:
                self.assertEqual(jobs.instant(choice['at']).timestamp()%1200,0)
        self.assertEqual(runtime.grid(NOW+timedelta(minutes=1),True),NOW+timedelta(minutes=20))
        from steward.scheduler import generate
        from pathlib import Path
        fixture=json.loads(Path(__file__).with_name('fixture.json').read_text())
        starts=[jobs.instant(c['start']) for c in generate(fixture)['choices'] if c['action']=='place']
        self.assertTrue(starts)
        self.assertTrue(all(t.timestamp()%900==0 for t in starts))
        self.assertTrue(any(t.timestamp()%1200!=0 for t in starts))

    def test_talk_event_without_scheduled_wake_and_guarded_off_grid_run(self):
        self.add(recurring=False)
        rid=self.run_now()
        with writer(self.root) as store:
            db=store.db
            with db:
                row=jobs.get(db,'runs',rid)
                row['planned_for']=(NOW+timedelta(minutes=15)).isoformat()
                jobs.append(db,'runs',row,row['change_id'])
        self.skip()
        wakes=[]
        result=self.tick(at=NOW+timedelta(minutes=20),settings={'wake_times':[]},
            load_reader=lambda:(4,20*1024**3),wake_starter=lambda:wakes.append(1),
            update_fetcher=lambda offset:[dict(update_id=8,message=dict(message_id=8,message_thread_id=6,chat={'id':1},**{'from':{'id':2}},text='hello'))])
        self.assertFalse(result['scheduled_wake'])
        self.assertIsNone(result['wake'])
        self.assertEqual(wakes,[])
        self.assertEqual(result['refused'],[rid])
        self.assertEqual(self.launched,[])

    def test_future_skip_and_schema_8_migration(self):
        import sqlite3
        db=sqlite3.connect(self.root/'state.sqlite')
        db.execute('ALTER TABLE inbox DROP COLUMN attachments')
        db.execute('DROP TABLE pages')
        db.execute('DROP TABLE drafts')
        db.execute('DROP TABLE outbox_keys')
        db.execute('DROP TABLE tick_skips')
        db.execute('DROP TABLE moments'); db.execute("DELETE FROM changes WHERE actor=\'migration\'")
        db.execute('ALTER TABLE runs DROP COLUMN retry_of')
        db.execute('PRAGMA user_version=8')
        db.commit()
        db.close()
        self.skip(start=NOW+timedelta(hours=1))
        with writer(self.root) as store:
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 17)
            state=jobs.snapshot(store.db,now=NOW)['tick_skips']
            self.assertIsNone(state['current'])
            self.assertEqual(state['next']['from_at'],(NOW+timedelta(hours=1)).isoformat())
