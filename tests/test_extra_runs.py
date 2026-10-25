"""Extra runs use the real dispatcher/executor without consuming the schedule."""
from contextlib import redirect_stdout
from datetime import timedelta
import io
import json
import unittest
from unittest.mock import patch

from steward import jobs, planner, runtime
from steward.cli import main
from steward.state import StateError, writer
import test_jobs
from test_jobs import NOW


class ExtraRunTests(unittest.TestCase):
    setUp = test_jobs.JobsTests.setUp
    add = test_jobs.JobsTests.add
    tick = test_jobs.JobsTests.tick

    def reserve(self, at=None):
        at = at or NOW + timedelta(hours=13, minutes=20)
        with writer(self.root) as st:
            result = jobs.run_verb(st.db, 'plan', 'test', 'normal reservation',
                                   at.isoformat(), now=NOW)
            return jobs.get(st.db, 'runs', result['run_id'])

    def extra(self, actor='maintenance'):
        with writer(self.root) as st:
            result = jobs.run_verb(st.db, 'now', 'test', 'daytime check', now=NOW, actor=actor)
            return jobs.get(st.db, 'runs', result['run_id'])

    def finish(self, run_id, start, minutes=5, code=0):
        clocks = iter((start, start + timedelta(minutes=minutes)))
        # Only the fixture's immutable /bin/true wrapper runs; no live CLI/state.
        result = runtime.execute(self.root, run_id, self.data, clock=lambda: next(clocks))
        self.assertEqual(result['exit_code'], 0)
        path = self.data / 'runs' / 'test' / run_id / 'manifest.json'
        manifest = json.loads(path.read_text())
        if code:
            manifest.update(exit_code=code, status='failed')
            path.write_text(json.dumps(manifest))
        return manifest

    def test_cli_non_model_extra_preserves_ready_by_reservation(self):
        self.add(needs={'ready_by': ['05:00']}, min_gap='20h', max_minutes=5)
        reservation = self.reserve()
        for actor in ('owner', 'maintenance'):
            with self.subTest(actor=actor):
                output = io.StringIO()
                with patch.dict('os.environ', STEWARD_ACTOR=actor), patch('steward.jobs.now_utc', return_value=NOW), redirect_stdout(output):
                    self.assertEqual(main(['run', 'now', 'test', '--state', str(self.root)]), 0)
                identity = json.loads(output.getvalue())['run_id']
                self.assertNotEqual(identity, reservation['run_id'])
                with writer(self.root) as st:
                    extra = jobs.get(st.db, 'runs', identity)
                    self.assertTrue(jobs.is_extra(extra))
                    self.assertEqual(extra['planned_for'], NOW.isoformat())
                    self.assertEqual(jobs.get(st.db, 'runs', reservation['run_id']), reservation)
                    jobs.run_verb(st.db, 'cancel', identity, 'fixture cleanup', now=NOW)

    def test_model_cannot_create_extra_and_rejection_is_atomic(self):
        self.add()
        self.reserve()
        with writer(self.root) as st:
            before = jobs.current(st.db, 'runs')
            changes = st.db.execute('SELECT count(*) FROM changes').fetchone()[0]
        output = io.StringIO()
        with patch.dict('os.environ', STEWARD_ACTOR='model'), redirect_stdout(output):
            self.assertEqual(main(['run', 'now', 'test', '--state', str(self.root)]), 2)
        self.assertIn('non-model', json.loads(output.getvalue())['error'])
        with writer(self.root) as st:
            self.assertEqual(jobs.current(st.db, 'runs'), before)
            self.assertEqual(st.db.execute('SELECT count(*) FROM changes').fetchone()[0], changes)

    def test_extra_success_keeps_reservation_measurements_and_next_schedule(self):
        self.add(needs={'ready_by': ['05:00']}, min_gap='20h', max_minutes=5)
        reservation = self.reserve()
        extra = self.extra()
        self.assertEqual(self.tick()['started'], [extra['run_id']])
        manifest = self.finish(extra['run_id'], NOW)
        self.assertTrue(manifest['extra'])
        self.tick(NOW + timedelta(minutes=20))
        with writer(self.root) as st:
            runs = jobs.current(st.db, 'runs')
            self.assertEqual(jobs.get(st.db, 'runs', reservation['run_id']), reservation)
            job = jobs.get(st.db, 'jobs', 'test')
            self.assertIsNone(planner.gap_floor(runs, 'test', json.loads(job['spec_json'])))
            profile = jobs.execution_profile(st.db, 'test')
            self.assertEqual(profile['measurement'], 'measured')
            self.assertEqual(profile['limit_minutes'], 50)
            self.assertEqual(planner.cost(runs, 'test')['run_count'], 1)
            self.assertIsNone(planner.rule(job, runs, jobs.instant(reservation['planned_for']), NOW + timedelta(minutes=20), reservation))
        due = jobs.instant(reservation['planned_for'])
        self.assertEqual(self.tick(due)['started'], [reservation['run_id']])
        self.finish(reservation['run_id'], due)
        self.tick(due + timedelta(minutes=20))
        with writer(self.root) as st:
            runs = jobs.current(st.db, 'runs')
            planned = [r for r in runs if r['state'] == 'planned']
            self.assertEqual(len(planned), 1)
            self.assertFalse(jobs.is_extra(planned[0]))
            self.assertEqual(jobs.instant(planned[0]['planned_for']), due + timedelta(days=1))

    def test_extra_success_does_not_serve_ready_by(self):
        self.add(needs={'ready_by': ['17:00']}, max_minutes=5)
        reservation = self.reserve(NOW + timedelta(hours=1, minutes=20))
        extra = self.extra()
        self.tick()
        self.finish(extra['run_id'], NOW)
        self.tick(NOW + timedelta(minutes=20))
        with writer(self.root) as st:
            jobs.run_verb(st.db, 'cancel', reservation['run_id'], 'test replanning', now=NOW + timedelta(minutes=20))
        self.tick(NOW + timedelta(minutes=40))
        with writer(self.root) as st:
            planned = [r for r in jobs.current(st.db, 'runs') if r['state'] == 'planned']
            self.assertEqual(len(planned), 1)
            self.assertEqual(planned[0]['planned_for'], reservation['planned_for'])

    def test_min_gap_and_cadence_ignore_extra_but_measurements_include_it(self):
        for needs, deadline, placement in (
                ({'every': {'min': '4h', 'max': '1d'}}, NOW + timedelta(hours=1), NOW),
                ({'max_staleness': '24h'}, NOW + timedelta(hours=1, minutes=5), NOW + timedelta(hours=1))):
            with self.subTest(needs=needs):
                spec = dict(needs=needs, min_gap='20h', max_minutes=5, heavy=False)
                job = dict(name='test', spec_json=json.dumps(spec))
                regular = dict(job='test', state='succeeded', started_at=(NOW - timedelta(hours=23)).isoformat(),
                               ended_at=(NOW - timedelta(hours=23) + timedelta(minutes=5)).isoformat(), spec_json='{}')
                extra = dict(regular, started_at=(NOW - timedelta(hours=1)).isoformat(),
                             ended_at=(NOW - timedelta(minutes=55)).isoformat(), spec_json='{"_extra":true}')
                runs = [regular, extra]
                self.assertEqual(planner.gap_floor(runs, 'test', spec), NOW - timedelta(hours=3))
                self.assertEqual(planner.cadence_deadline('test', spec, runs), deadline)
                self.assertEqual(runtime.placement(job, runs, NOW)[0], placement)
                self.assertIsNone(planner.rule(job, runs, NOW, NOW))
                self.assertEqual(planner.cost(runs, 'test')['run_count'], 2)

    def test_extra_heavy_exclusion_at_reservation_and_dispatch(self):
        self.add(heavy=True)
        reservation = self.reserve()
        extra = self.extra()
        result = self.tick(lock_reader=lambda: True)
        self.assertIn(extra['run_id'], result['refused'])
        self.assertEqual(self.launched, [])
        with writer(self.root) as st:
            self.assertEqual(jobs.get(st.db, 'runs', reservation['run_id']), reservation)
        self.add('other', heavy=True)
        with writer(self.root) as st:
            other = jobs.run_verb(st.db, 'now', 'other', 'occupy lane', now=NOW)
        self.assertIn(other['run_id'], self.tick()['started'])
        with self.assertRaisesRegex(StateError, 'heavy runs overlap'):
            self.extra()
        with writer(self.root) as st:
            self.assertEqual(jobs.get(st.db, 'runs', reservation['run_id']), reservation)

    def test_extra_heavy_overlap_uses_expected_duration_not_runaway_limit(self):
        self.add(heavy=True, max_minutes=60)
        reservation = self.reserve(NOW + timedelta(hours=2))
        first = self.extra()
        self.assertEqual(self.tick()['started'], [first['run_id']])
        self.finish(first['run_id'], NOW, minutes=15)
        self.tick(NOW + timedelta(minutes=20))
        with writer(self.root) as st:
            self.assertEqual(jobs.execution_profile(st.db, 'test')['limit_minutes'], 150)
        self.assertTrue(jobs.is_extra(self.extra()))
        with writer(self.root) as st:
            self.assertEqual(jobs.get(st.db, 'runs', reservation['run_id']), reservation)

    def test_extra_keeps_load_disk_and_single_started_job_guards(self):
        self.add()
        reservation = self.reserve()
        for load, free in ((3.6, 20 * 1024**3), (0.1, 7 * 1024**3)):
            extra = self.extra()
            self.assertIn(extra['run_id'], self.tick(load_reader=lambda: (load, free))['refused'])
        first = self.extra()
        self.assertIn(first['run_id'], self.tick()['started'])
        second = self.extra()
        result = self.tick()
        self.assertIn(second['run_id'], result['refused'])
        self.assertEqual(len(self.launched), 1)
        with writer(self.root) as st:
            self.assertEqual(jobs.get(st.db, 'runs', reservation['run_id']), reservation)
            self.assertEqual(jobs.get(st.db, 'runs', second['run_id'])['reason'], 'job already started')

    def test_extra_retry_retains_marker_and_reservation(self):
        self.add(needs={'ready_by': ['05:00']}, min_gap='20h', max_minutes=5)
        reservation = self.reserve()
        extra = self.extra()
        self.tick()
        self.finish(extra['run_id'], NOW, code=1)
        result = self.tick(NOW + timedelta(minutes=20))
        with writer(self.root) as st:
            retry = next(r for r in jobs.current(st.db, 'runs') if r['retry_of'] == extra['run_id'])
            self.assertIn(retry['run_id'], result['started'])
            self.assertTrue(jobs.is_extra(retry))
            self.assertEqual(jobs.get(st.db, 'runs', reservation['run_id']), reservation)
        self.finish(retry['run_id'], NOW + timedelta(minutes=20), code=1)
        self.tick(NOW + timedelta(minutes=40))
        with writer(self.root) as st:
            runs = jobs.current(st.db, 'runs')
            self.assertEqual(len(runs), 3)
            job = jobs.get(st.db, 'jobs', 'test')
            at, _ = runtime.placement(job, [r for r in runs if r['run_id'] != reservation['run_id']], NOW + timedelta(minutes=40))
            self.assertEqual(at, jobs.instant(reservation['planned_for']))

    def test_plan_and_cancel_still_target_normal_reservation(self):
        self.add()
        reservation = self.reserve()
        extra = self.extra()
        with writer(self.root) as st:
            at = NOW + timedelta(hours=14)
            moved = jobs.run_verb(st.db, 'plan', 'test', 'adapt', at.isoformat(), now=NOW, actor='model')
            self.assertEqual(moved['run_id'], reservation['run_id'])
            self.assertEqual(jobs.get(st.db, 'runs', extra['run_id']), extra)
            jobs.run_verb(st.db, 'cancel', extra['run_id'], 'cancel check', now=NOW, actor='model')
            self.assertEqual(jobs.get(st.db, 'runs', reservation['run_id'])['planned_for'], at.isoformat())

    def test_no_reservation_keeps_existing_now_semantics(self):
        self.add(needs={'ready_by': ['05:00']})
        with writer(self.root) as st:
            with self.assertRaisesRegex(StateError, 'ready_by'):
                jobs.run_verb(st.db, 'now', 'test', 'outside window', now=NOW, actor='maintenance')
            result = jobs.run_verb(st.db, 'now', 'test', 'owner override', now=NOW)
            self.assertFalse(jobs.is_extra(jobs.get(st.db, 'runs', result['run_id'])))
