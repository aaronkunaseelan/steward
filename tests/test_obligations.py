from fixture_mind import use_settings
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from steward import timing, senses
from fixture_mind import TEST_ZONE


def dt(text):
    return datetime.fromisoformat(text)


class ObligationTests(unittest.TestCase):
    setUp = use_settings

    target = dt('2026-10-21T18:30:00+03:00')
    now = dt('2026-10-21T20:00:00+03:00')

    def run_row(self, end='2026-10-21T19:23:00+03:00', state='succeeded'):
        return dict(job='gsd-steady', state=state, ended_at=end,
                    started_at='2026-10-21T18:40:00+03:00')

    def test_late_success_without_later_target_and_later_failure(self):
        runs = [self.run_row(), self.run_row('2026-10-21T19:50:00+03:00', 'failed')]
        result = timing.job_obligation(runs, 'gsd-steady', self.target, self.now)
        self.assertEqual(result['state'], 'recovered')
        self.assertEqual(dt(result['completed_at']), dt(runs[0]['ended_at']))
        self.assertEqual(result['started_at'], runs[0]['started_at'])

    def test_no_completion_and_future_finish(self):
        for runs in ([], [self.run_row('2026-10-22T01:00:00+03:00')]):
            result = timing.job_obligation(runs, 'gsd-steady', self.target, self.now)
            self.assertEqual(result['state'], 'missed')
            self.assertIsNone(result['completed_at'])
        self.assertEqual(timing.job_obligation([], 'gsd-steady', self.target,
                         self.target - timedelta(minutes=1))['state'], 'due')

    def test_cut_must_follow_completing_run(self):
        promise = timing.job_obligation([self.run_row()], 'gsd-steady', self.target, self.now)
        sense = dict(status='ok', at=self.now.isoformat(), data=dict(
            promise=promise, cut=dict(until=self.target.isoformat(),
                                     committed_at='2026-10-21T18:39:00+03:00')))
        self.assertEqual(senses.freshness('gsd', sense, self.now)['verdict'], 'stale')
        sense['data']['cut']['committed_at'] = '2026-10-21T18:45:00+03:00'
        verdict = senses.freshness('gsd', sense, self.now)
        self.assertEqual(verdict['verdict'], 'fresh')
        self.assertIn('missed: Wed 21 Oct 18:30 EEST; recovered 19:23 EEST', verdict['reason'])

    def gsd_sense(self, read_at, committed_at):
        return dict(status='ok', at=read_at, data=dict(
            promise=dict(target='2026-11-09T06:30:00+02:00', state='kept',
                         completed_at='2026-11-09T04:27:44Z', started_at='2026-11-09T03:40:00Z'),
            cut=dict(until='2026-11-09T04:22:28Z', committed_at=committed_at)))

    def test_read_before_the_finish_cannot_call_a_kept_run_stale(self):
        sense = self.gsd_sense('2026-11-09T04:20:00Z', '2026-11-08T15:52:29Z')
        verdict = senses.freshness('gsd', sense, dt('2026-11-09T04:33:00Z'))
        self.assertEqual(verdict['verdict'], 'fresh')
        self.assertIn("kept: Mon 09 Nov 06:30 EET; cut read 06:20 EET precedes the run's finish 06:27 EET",
                      verdict['reason'])

    def test_read_after_the_finish_still_finds_no_newer_cut(self):
        sense = self.gsd_sense('2026-11-09T04:40:00Z', '2026-11-08T15:52:29Z')
        verdict = senses.freshness('gsd', sense, dt('2026-11-09T04:41:00Z'))
        self.assertEqual(verdict['verdict'], 'stale')
        self.assertEqual(verdict['reason'], 'no newer cut for gsd ready_by: Mon 09 Nov 06:30 EET')

    def test_read_after_the_finish_with_a_newer_cut_is_kept(self):
        sense = self.gsd_sense('2026-11-09T04:40:00Z', '2026-11-09T04:22:28Z')
        verdict = senses.freshness('gsd', sense, dt('2026-11-09T04:41:00Z'))
        self.assertEqual(verdict['verdict'], 'fresh')
        self.assertEqual(verdict['reason'], 'gsd ready_by kept: Mon 09 Nov 06:30 EET')

    def test_late_receipt_recovers_but_does_not_keep(self):
        result = timing.obligation(self.target, [(dt('2026-10-21T19:23:00+03:00'), None)], self.now,
                                   start=self.target.replace(hour=0, minute=0))
        self.assertEqual(result['state'], 'recovered')
        self.assertNotEqual(result['state'], 'kept')

    def test_latest_target_day_at_midnight_and_dst(self):
        job = dict(name='gsd-steady', state='active', spec_json=json.dumps(
            {'needs': {'ready_by': ['06:30', '18:30']}}))
        cases = [('2026-10-25T00:10:00+03:00', '2026-10-24T18:30:00+03:00'),
                 ('2026-10-25T06:40:00+02:00', '2026-10-25T06:30:00+02:00'),
                 ('2026-10-26T00:10:00+02:00', '2026-10-25T18:30:00+02:00'),
                 ('2026-03-29T06:40:00+03:00', '2026-03-29T06:30:00+03:00')]
        with patch('steward.jobs.current', side_effect=lambda db, table: [job] if table == 'jobs' else []), \
             patch('steward.jobs.execution_profile', return_value={'measured_at': '2026-01-01T00:00:00+00:00'}):
            for now, target in cases:
                with self.subTest(now=now):
                    result = timing.latest_job_obligation(None, 'gsd-steady', dt(now))
                    self.assertEqual(result['target'], target)
                    self.assertEqual(result['state'], 'missed')

    def test_target_day_midnight_and_dst(self):
        for text, lower in (('2026-10-22T00:10:00+03:00', '2026-10-21T15:10:00+00:00'),
                            ('2026-10-25T06:30:00+02:00', '2026-10-24T22:30:00+00:00'),
                            ('2026-03-29T06:30:00+03:00', '2026-03-28T21:30:00+00:00')):
            target = dt(text).astimezone(TEST_ZONE)
            lo = dt(lower)
            self.assertEqual(timing.obligation(target, [(lo, None)], target)['state'], 'kept')
            self.assertEqual(timing.obligation(target, [(lo - timedelta(seconds=1), None)], target)['state'], 'missed')
            late = target + timedelta(minutes=10)
            self.assertEqual(timing.obligation(target, [(late, None)], late)['state'], 'recovered')


if __name__ == '__main__':
    unittest.main()
