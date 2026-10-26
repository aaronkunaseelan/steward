from datetime import timedelta
import json
from pathlib import Path
import unittest

from fixture_mind import command
import test_jobs
from steward import jobs, runtime
from steward.state import writer


class RunBindingTests(unittest.TestCase):
    def setUp(self):
        test_jobs.JobsTests.setUp(self)
        self.now = test_jobs.NOW

    add = test_jobs.JobsTests.add
    tick = test_jobs.JobsTests.tick
    run_now = test_jobs.JobsTests.run_now

    def test_capacity_edit_replans_an_unstarted_reservation(self):
        for capacity in ('max_minutes', 'heavy'):
            with self.subTest(capacity=capacity), writer(self.root) as st:
                name = 'capacity-' + capacity.replace('_', '-')
                original = test_jobs.spec(needs={'ready_by': ['18:30']})
                jobs.job_verb(st.db, 'add', name, original, now=self.now)
                reserved = jobs.run_verb(st.db, 'plan', name, 'fixture',
                                        (self.now + timedelta(hours=2)).isoformat(), now=self.now)
                edited = dict(original, **{capacity: 90 if capacity == 'max_minutes' else True})
                jobs.job_verb(st.db, 'edit', name, edited, now=self.now)
                rows = [r for r in jobs.current(st.db, 'runs') if r['job'] == name]
                self.assertEqual(jobs.get(st.db, 'runs', reserved['run_id'])['state'], 'cancelled')
                planned = [r for r in rows if r['state'] == 'planned']
                self.assertEqual(len(planned), 1)
                self.assertNotEqual(planned[0]['run_id'], reserved['run_id'])
                if capacity == 'max_minutes':
                    self.assertLess(jobs.instant(planned[0]['planned_for']), self.now + timedelta(hours=2))

    def test_noncapacity_edit_preserves_slot_and_dispatch_binds_current_environment(self):
        self.add(env_file='/tmp/old-fixture.env')
        identity = self.run_now()
        with writer(self.root) as st:
            before = jobs.get(st.db, 'runs', identity)
            jobs.job_verb(st.db, 'edit', 'test', test_jobs.spec(
                argv=[command('/bin/echo'), 'new'], env_file='/tmp/new-fixture.env'), now=self.now)
            planned = jobs.get(st.db, 'runs', identity)
            self.assertEqual(planned['planned_for'], before['planned_for'])
            self.assertEqual(json.loads(planned['spec_json'])['argv'][-1], 'new')
        self.tick()
        self.assertIn('/tmp/new-fixture.env', self.launched[0])
        result = runtime.execute(self.root, identity, self.data, clock=lambda: self.now)
        self.assertEqual(result['status'], 'succeeded')
        directory = self.data / 'runs' / 'test' / identity
        self.assertEqual(json.loads((directory / 'manifest.json').read_text())['argv'][-1], 'new')
        self.assertEqual((directory / 'stdout.txt').read_text(), 'new\n')
        with writer(self.root) as st:
            bound = jobs.get(st.db, 'runs', identity)
            jobs.job_verb(st.db, 'edit', 'test', test_jobs.spec(argv=[command('/bin/false')]), now=self.now)
            self.assertEqual(jobs.get(st.db, 'runs', identity)['spec_json'], bound['spec_json'])

    def test_capacity_edit_undo_restores_original_reservation(self):
        with writer(self.root) as st:
            original = test_jobs.spec(heavy=True, needs={'ready_by': ['18:30']})
            jobs.job_verb(st.db, 'add', 'test', original, now=self.now)
            at = (self.now + timedelta(minutes=40)).isoformat()
            reserved = jobs.run_verb(st.db, 'plan', 'test', 'fixture', at, now=self.now)
            edited = jobs.job_verb(st.db, 'edit', 'test', dict(original, max_minutes=90), now=self.now)
            jobs.undo(st.db, edited['change_id'], now=self.now)
            planned = [r for r in jobs.current(st.db, 'runs') if r['state'] == 'planned']
            self.assertEqual([r['run_id'] for r in planned], [reserved['run_id']])
            self.assertEqual(planned[0]['planned_for'], at)
            self.assertEqual(json.loads(planned[0]['spec_json'])['max_minutes'], 30)

    def test_dispatch_passes_exactly_the_capability_permit_credentials(self):
        for name, permits, expected in (
                ('prepare-like', ['page', 'draft', 'page'], ['STEWARD_PAGES_TOKEN', 'GOG_KEYRING_PASSWORD']),
                ('no-permits', [], []),
                ('page-only', ['page', 'alert', 'health'], ['STEWARD_PAGES_TOKEN']),
                ('plain-job', None, [])):
            if permits is not None:
                path = Path(self.tmp.name) / 'mind/capabilities' / (name + '.md')
                path.write_text('---\nname: ' + json.dumps(name) +
                                '\nreads: []\ncalls: []\nrecord: []\npermits: ' + json.dumps(permits) +
                                '\n---\nFixture')
            self.add(name, **({'capability': name} if permits is not None else {}))
            self.run_now(name)
            self.tick()
            argv = self.launched[-1]
            passed = [argv[i + 1] for i, value in enumerate(argv) if value == '--pass']
            with self.subTest(name=name):
                self.assertEqual(passed, expected)
                self.assertEqual(argv[0], 'batch-run')
                self.assertNotIn('--env-file', argv)

    def test_dispatch_validates_current_spec_before_binding_execution(self):
        self.add()
        identity = self.run_now()
        Path(command()).unlink()
        result = self.tick()
        self.assertEqual(result['started'], [])
        self.assertIn(identity, result['refused'])
        self.assertFalse(self.launched)
