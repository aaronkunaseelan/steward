from fixture_mind import use_settings
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from steward import briefing, jobs, senses, tick_skips
from steward.state import writer

NOW = datetime(2026, 10, 21, 1, 20, tzinfo=timezone.utc)


class SenseAgeTests(unittest.TestCase):
    setUp = use_settings

    def test_calendar_and_every_tick_senses_have_one_read_age_limit(self):
        for name, minutes, expected in (('calendars', 30, 'fresh'), ('calendars', 31, 'stale'),
                                         ('machine', 40, 'fresh'), ('machine', 41, 'stale'),
                                         ('place', 20, 'fresh'), ('place', 21, 'stale')):
            with self.subTest(name=name, minutes=minutes):
                reading = dict(status='ok', at=(NOW - timedelta(minutes=minutes)).isoformat(),
                               data={'state': 'unknown'})
                self.assertEqual(senses.freshness(name, reading, NOW)['verdict'], expected)

    def test_planned_skip_cannot_make_old_cached_reading_fresh(self):
        for overlapping in (False, True):
            with self.subTest(overlapping=overlapping), tempfile.TemporaryDirectory() as root, writer(Path(root), initialize=True) as st:
                observed = NOW - timedelta(hours=3)
                end = observed + timedelta(hours=3, minutes=20)
                tick_skips.skip(st.db, observed.isoformat(), end.isoformat(), 'planned night', now=observed)
                if overlapping:
                    tick_skips.skip(st.db, (observed - timedelta(hours=1)).isoformat(),
                                    (observed + timedelta(hours=1)).isoformat(),
                                    'overlapping window', now=observed - timedelta(hours=2))
                senses.save(st.db, {'calendars': dict(status='ok', data={'events': []})}, observed,
                            jobs.change(st.db, 'fixture', observed, 'tick'))
                reading = senses.snapshot(st.db, NOW)['calendars']
                self.assertEqual(reading['age'], 10800)
                self.assertEqual(reading['verdict'], 'stale')
                page = briefing.render({'senses': senses.snapshot(st.db, NOW)}, NOW, {})
                self.assertIn('## Calendar [stale]', page)
                self.assertEqual(senses.snapshot(st.db, end + timedelta(minutes=30))['calendars']['verdict'], 'stale')
                self.assertEqual(senses.snapshot(st.db, end + timedelta(minutes=31))['calendars']['verdict'], 'stale')

    def test_read_age_never_overrides_failed_reads_or_producer_expiry(self):
        failed = dict(status='unknown', at=NOW.isoformat(), reason='read failed')
        self.assertEqual(senses.freshness('place', failed, NOW)['verdict'], 'unknown')
        health = dict(status='ok', at=NOW.isoformat(), age=0, data=dict(auth='ok',
                      watch_uploaded_at=(NOW - timedelta(hours=13)).isoformat(),
                      watch_fresh_until=(NOW - timedelta(hours=1)).isoformat()))
        self.assertEqual(senses.freshness('health', health, NOW)['verdict'], 'stale')
