"""Overdue cadence recovery preserves scheduling constraints."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from fixture_mind import install
from steward import jobs, planner
from steward.state import writer


class CadenceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        install(self, Path(self.tmp.name))
        self.root = Path(self.tmp.name) / 'state'
        with writer(self.root, initialize=True):
            pass
        self.now = datetime(2026, 10, 21, 4, tzinfo=timezone.utc)

    def test_overdue_every_job_has_a_recovery_slot(self):
        from fixture_mind import command
        with writer(self.root) as st:
            jobs.job_verb(st.db, 'add', 'late-every', dict(justification='fixture', argv=[command()],
                          needs={'every': {'min': '1h', 'max': '1d'}}), now=self.now)
            spec = jobs.get(st.db, 'jobs', 'late-every')['spec_json']
            cid = jobs.change(st.db, 'fixture', self.now)
            old = (self.now - timedelta(days=2)).isoformat()
            jobs.append(st.db, 'runs', dict(run_id='old', job='late-every', state='succeeded',
                        planned_for=old, started_at=old, ended_at=old, reason='fixture', spec_json=spec), cid)
            row = next(c for c in planner.choices(st.db, self.now) if c['job'] == 'late-every')
            self.assertTrue(row['feasible'], 'missing every.max must not permanently strand the job')
            self.assertEqual(row['missed_at'], (self.now - timedelta(days=1)).isoformat())
            planned = jobs.run_verb(st.db, 'plan', 'late-every', 'Recovery', row['feasible'][0]['at'], now=self.now)
            self.assertEqual(planned['status'], 'planned')
            self.assertEqual(jobs.snapshot(st.db, now=self.now)['jobs'][0]['missed_at'], row['missed_at'])
            self.assertFalse(planner.choices(st.db, self.now)[0]['meets_needs'])
            self.assertIn('recovering missed need', row['default_reason'])

    def test_overdue_max_staleness_job_has_a_recovery_slot(self):
        from fixture_mind import command
        with writer(self.root) as st:
            jobs.job_verb(st.db, 'add', 'late-stale', dict(justification='fixture', argv=[command()],
                          needs={'max_staleness': '24h'}), now=self.now)
            spec = jobs.get(st.db, 'jobs', 'late-stale')['spec_json']
            cid = jobs.change(st.db, 'fixture', self.now)
            old = (self.now - timedelta(days=2)).isoformat()
            jobs.append(st.db, 'runs', dict(run_id='old', job='late-stale', state='succeeded',
                        planned_for=old, started_at=old, ended_at=old, reason='fixture', spec_json=spec), cid)
            row = next(c for c in planner.choices(st.db, self.now) if c['job'] == 'late-stale')
            self.assertTrue(row['feasible'], 'an overdue backup must still be runnable automatically')
            self.assertEqual(row['missed_at'], (self.now - timedelta(days=1)).isoformat())


    def test_recovery_keeps_min_gap_and_heavy_constraints(self):
        from fixture_mind import command
        with writer(self.root) as st:
            spec = dict(justification='fixture', argv=[command()], heavy=True, min_gap='3d',
                        max_minutes=60, needs={'every': {'min': '1h', 'max': '1d'}})
            jobs.job_verb(st.db, 'add', 'late', spec, now=self.now)
            job = jobs.get(st.db, 'jobs', 'late')
            old = (self.now - timedelta(days=2)).isoformat()
            run = dict(run_id='old', job='late', state='succeeded', started_at=old,
                       ended_at=(self.now - timedelta(days=2) + timedelta(hours=1)).isoformat(),
                       planned_for=old, spec_json=job['spec_json'])
            self.assertEqual(planner.rule(job, [run], self.now, self.now), 'min_gap')
            plain = json.loads(job['spec_json'])
            plain.pop('min_gap')
            job['spec_json'] = json.dumps(plain)
            busy = dict(run, run_id='busy', job='other', state='planned', started_at=None,
                        ended_at=None, planned_for=self.now.isoformat())
            self.assertEqual(planner.rule(job, [run, busy], self.now, self.now), 'heavy overlap')
            self.assertIsNone(planner.rule(job, [run, busy], self.now + timedelta(hours=1), self.now))
            reserved = dict(run, state='planned', planned_for=(self.now + timedelta(hours=1)).isoformat())
            self.assertIsNone(planner.rule(job, [run], self.now + timedelta(hours=3), self.now, reserved))
