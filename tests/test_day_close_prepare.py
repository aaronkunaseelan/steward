"""The evening close waits for preparation, not for a general job dependency."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from fixture_mind import use_settings
from steward import jobs, timing
from steward.state import writer


class DayClosePrepareTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = self.enterContext(writer(Path(tmp.name), initialize=True)).db
        self.now = datetime(2026, 10, 23, 15, 40, tzinfo=timezone.utc)  # 18:40 Athens
        self.spec = dict(needs=dict(ready_by=['19:30']))

    def prepare(self, state='active', clocks=None):
        if clocks is not None:
            self.spec['needs'] = dict(ready_by=clocks)
        cid = jobs.change(self.db, 'fixture', self.now)
        jobs.append(self.db, 'jobs', dict(name='prepare', spec_json=json.dumps(self.spec), state=state), cid)

    def prepare_run(self, state='started', start=None, end=None, name='evening', version=0):
        start = start or self.now - timedelta(minutes=20)
        cid = jobs.change(self.db, 'fixture', self.now)
        jobs.append(self.db, 'runs', dict(run_id=name, version=version, job='prepare', state=state,
                    planned_for=start.isoformat(), started_at=start.isoformat(),
                    ended_at=end.isoformat() if end else None, reason='fixture',
                    spec_json=json.dumps(self.spec)), cid)

    def assert_close(self, due, now=None):
        for view in (timing.due, timing.undelivered):
            self.assertEqual('day_close' in view(self.db, now or self.now), due, view.__name__)

    def test_waits_while_prepare_is_running_then_due_on_success(self):
        self.prepare()
        self.prepare_run()
        self.assert_close(False)
        self.prepare_run('succeeded', end=self.now, version=1)
        self.assert_close(True)

    def test_failed_prepare_releases_close_without_waiting_for_ready_by(self):
        self.prepare()
        self.prepare_run('failed', end=self.now)
        self.assert_close(True)

    def test_ready_by_releases_running_or_never_started_prepare(self):
        self.prepare()
        self.assert_close(False)
        self.assert_close(False, self.now.replace(hour=16, minute=29, second=59))
        self.assert_close(True, self.now.replace(hour=16, minute=30))
        self.prepare_run()
        self.assert_close(True, self.now.replace(hour=16, minute=30))

    def test_no_prepare_job_or_inactive_prepare_is_unchanged(self):
        self.assert_close(True)
        self.prepare('paused')
        self.assert_close(True)

    def test_morning_and_yesterday_runs_do_not_release_evening_close(self):
        self.prepare(clocks=['06:45', '19:30'])
        self.prepare_run('succeeded', start=self.now.replace(hour=3), end=self.now.replace(hour=3, minute=30), name='morning')
        self.prepare_run('succeeded', start=self.now - timedelta(days=1, hours=1),
                 end=self.now - timedelta(days=1), name='yesterday')
        self.assert_close(False)

    def test_finish_cannot_release_close_before_configured_start(self):
        self.prepare()
        early = self.now.replace(hour=15, minute=20)
        self.prepare_run('succeeded', start=early - timedelta(minutes=20), end=early)
        self.assert_close(False, early)
        self.assert_close(True, self.now.replace(hour=15, minute=30))

    def test_wake_during_prepare_does_not_consume_close_ritual(self):
        self.prepare()
        self.prepare_run()
        checkpoint = json.dumps(dict(model_calls=1))
        self.db.execute("INSERT INTO wakes(started_at,status,checkpoint,harness_run_id) VALUES (?,'completed',?,'fixture')",
                        (self.now.isoformat(), checkpoint))
        self.assert_close(False)
        finished = self.now + timedelta(minutes=10)
        self.prepare_run('succeeded', end=finished, version=1)
        self.assert_close(True, finished)
        moment = jobs.get(self.db, 'moments', 'day_close')
        self.assertEqual(timing.moment_obligation(self.db, moment, finished)['state'], 'due')
        self.db.execute("INSERT INTO wakes(started_at,status,checkpoint,harness_run_id) VALUES (?,'completed',?,'fixture')",
                        (finished.isoformat(), checkpoint))
        self.assertNotIn('day_close', timing.due(self.db, finished))

    def test_ready_by_wake_ignores_earlier_completed_wake(self):
        self.prepare()
        self.prepare_run()
        self.db.execute("INSERT INTO wakes(started_at,status,checkpoint,harness_run_id) VALUES (?,'completed','{}','fixture')",
                        (self.now.isoformat(),))
        self.assert_close(True, self.now.replace(hour=16, minute=30))

    def test_ready_by_uses_athens_after_dst_change(self):
        self.now = datetime(2026, 10, 25, 16, 40, tzinfo=timezone.utc)  # 18:40 EET
        self.prepare()
        self.prepare_run()
        self.assert_close(False)
        self.assert_close(True, self.now.replace(hour=17, minute=30))
