from datetime import date, datetime, timezone
import unittest
from steward import timing


class OwnerClockTests(unittest.TestCase):
    from fixture_mind import use_settings as setUp

    def test_zoned_owner_time(self):
        self.assertEqual(timing.owner_time(datetime(2026, 10, 24, 10, tzinfo=timezone.utc), '%H:%M'), '13:00 EEST')
        self.assertEqual(timing.owner_time(datetime(2026, 10, 26, 10, tzinfo=timezone.utc), '%H:%M'), '12:00 EET')

    def test_gap_fold_and_midnight(self):
        self.assertIsNone(timing.clock_on(date(2026, 3, 29), '03:30'))
        a = timing.clock_on(date(2026, 10, 25), '03:30')
        b = timing.clock_on(date(2026, 10, 25), '03:30', fold=1)
        self.assertEqual((b-a).total_seconds(), 3600)
        self.assertEqual(timing.clock_on(date(2026, 10, 26), '00:00').isoformat(), '2026-10-25T22:00:00+00:00')
