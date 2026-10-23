from datetime import timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_jobs
from test_jobs import NOW, spec
from steward import jobs, planner, runtime
from steward.state import StateError, writer


class PlanTests(unittest.TestCase):
    setUp = test_jobs.JobsTests.setUp
    add = test_jobs.JobsTests.add
    run_now = test_jobs.JobsTests.run_now
    tick = test_jobs.JobsTests.tick
    def test_infeasible_and_every_max(self):
        self.add()
        with writer(self.root) as store:
            db = store.db
            rid = jobs.run_verb(db, 'now', 'test', 'start', now=NOW)['run_id']
            row = jobs.get(db, 'runs', rid)
            with db:
                row.update(state='succeeded', started_at=NOW.isoformat(), ended_at=(NOW + timedelta(minutes=20)).isoformat())
                jobs.append(db, 'runs', row, row['change_id'])
            with self.assertRaisesRegex(StateError, 'every.min/max'):
                jobs.run_verb(db, 'plan', 'test', 'too late', (NOW + timedelta(hours=25)).isoformat(), now=NOW)
            with self.assertRaisesRegex(StateError, 'every.min/max'):
                jobs.run_verb(db, 'plan', 'test', 'too early', (NOW + timedelta(hours=2)).isoformat(), now=NOW)

    def test_learned_ready_by_and_stable_choices(self):
        self.add(needs={'ready_by': ['18:30']}, max_minutes=120)
        with writer(self.root) as store:
            db = store.db
            rid = jobs.run_verb(db, 'now', 'test', 'learn', now=NOW - timedelta(days=1))['run_id']
            row = jobs.get(db, 'runs', rid)
            with db:
                row.update(state='succeeded', started_at=(NOW-timedelta(days=1)).isoformat(), ended_at=(NOW-timedelta(days=1)+timedelta(minutes=15)).isoformat(), cost_minutes=15)
                jobs.append(db, 'runs', row, row['change_id'])
            output = planner.choices(db, NOW)
            self.assertEqual(output, planner.choices(db, NOW))
            self.assertEqual(output[0]['expected_minutes'], 15)
            at = NOW + timedelta(hours=3)
            self.assertIsNone(planner.rule(jobs.get(db, 'jobs', 'test'), jobs.current(db, 'runs'), at, NOW))
            self.assertEqual(planner.rule(jobs.get(db, 'jobs', 'test'), jobs.current(db, 'runs'), at+timedelta(minutes=20), NOW), 'ready_by')

    def test_churn_moves_cancels_not_creation_and_frozen(self):
        self.add()
        self.add('other', heavy=True)
        with writer(self.root) as store:
            db = store.db
            def reserve(name, hours):
                return jobs.run_verb(db, 'plan', name, 'reason', (NOW+timedelta(hours=hours)).isoformat(), now=NOW, actor='model', churn_cap=2)
            first = reserve('test', 4)
            reserve('other', 6)
            reserve('test', 5)
            jobs.run_verb(db, 'cancel', first['run_id'], 'reason', now=NOW, actor='model', churn_cap=2)
            reserve('test', 7)  # creations do not consume churn
            with self.assertRaisesRegex(StateError, 'churn cap'):
                reserve('test', 8)
            with self.assertRaisesRegex(StateError, 'frozen horizon'):
                reserve('test', 1)

    def test_tty_cost_fold(self):
        self.add(recurring=False)
        rid = self.run_now()
        self.tick()
        directory = self.data / 'runs' / 'test' / rid
        directory.mkdir(parents=True)
        (directory/'manifest.json').write_text(json.dumps(dict(job='test', run_id=rid, started_at=NOW.isoformat(), ended_at=(NOW+timedelta(minutes=12)).isoformat(), exit_code=0, status='succeeded', links=[{'tty_run':'fixture'}])))
        tty = self.root/'tty'
        path = tty/'runs/fixture/result.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'usage': {'input_tokens': 100, 'output_tokens': 20, 'cached_input_tokens': 80}}))
        with patch.dict('os.environ', {'TTY_STATE': str(tty)}):
            self.tick(at=NOW+timedelta(minutes=15))
        with writer(self.root) as store:
            learned = jobs.snapshot(store.db, now=NOW)['jobs'][0]['cost']
            self.assertEqual(learned, dict(median_minutes=12, median_tokens=120, run_count=1, last_status='succeeded'))

    def test_model_cancel_frozen_and_owner_exception(self):
        self.add()
        with writer(self.root) as store:
            db = store.db
            rid = jobs.run_verb(db, 'now', 'test', 'owner', now=NOW)['run_id']
            with self.assertRaisesRegex(StateError, 'frozen horizon'):
                jobs.run_verb(db, 'cancel', rid, 'model', now=NOW, actor='model')
            jobs.run_verb(db, 'cancel', rid, 'owner', now=NOW)

    def test_cli_plan_read_only_and_rule_error(self):
        import io
        from contextlib import redirect_stdout
        from steward.cli import main
        self.add()
        before = (self.root / 'state.sqlite').read_bytes()
        outputs = []
        with patch('steward.jobs.now_utc', return_value=NOW):
            for _ in range(2):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    self.assertEqual(main(['plan', '--state', str(self.root)]), 0)
                outputs.append(stream.getvalue())
            self.assertEqual((self.root / 'state.sqlite').read_bytes(), before)
            stream = io.StringIO()
            with redirect_stdout(stream):
                self.assertEqual(main(['run', 'plan', 'test', '--at', (NOW+timedelta(hours=4, minutes=7)).isoformat(), '--reason', 'bad', '--state', str(self.root)]), 2)
            self.assertEqual(json.loads(stream.getvalue())['error'], '20-minute grid')
        self.assertEqual(outputs[0], outputs[1])
        # A rejected write can touch SQLite metadata; read-only plan itself cannot.
        self.assertIsInstance(json.loads(outputs[0]), list)

    def test_staleness(self):
        self.add(needs={'max_staleness': '4h'})
        with writer(self.root) as store:
            db = store.db
            rid = jobs.run_verb(db, 'now', 'test', 'learn', now=NOW-timedelta(hours=1))['run_id']
            row = jobs.get(db, 'runs', rid)
            with db:
                row.update(state='succeeded', started_at=(NOW-timedelta(hours=1)).isoformat(), ended_at=NOW.isoformat(), cost_minutes=60)
                jobs.append(db, 'runs', row, row['change_id'])
            with self.assertRaisesRegex(StateError, 'max_staleness'):
                jobs.run_verb(db, 'plan', 'test', 'late', (NOW+timedelta(hours=4)).isoformat(), now=NOW)
            jobs.run_verb(db, 'plan', 'test', 'on time', (NOW+timedelta(hours=2)).isoformat(), now=NOW)
