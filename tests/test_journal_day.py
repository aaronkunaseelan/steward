"""A run appears in exactly one day's journal: the day of its anchor (F-42 / F-144)."""
from datetime import datetime
import tempfile
from pathlib import Path
import unittest

from steward import memory, jobs
from steward.state import writer


class JournalDayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from fixture_mind import install
        install(self, Path(self.tmp.name))
        self.root = Path(self.tmp.name) / 'state'
        self.ctx = writer(self.root, initialize=True)
        self.store = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.db = self.store.db
        self.changes = []

    def at(self, *rows):
        """Insert runs, one row per (run_id, planned_for, started_at, ended_at), each in its own change."""
        with self.db:
            for run_id, planned_for, started_at, ended_at in rows:
                change = jobs.change(self.db, 'fixture', datetime.fromisoformat(planned_for))
                self.changes.append(change)
                self.db.execute(
                    'INSERT INTO runs (run_id,version,change_id,job,state,planned_for,reason,'
                    'started_at,ended_at,unit,exit_code,manifest,spec_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (run_id, 1, change, 'fixture', 'succeeded', planned_for, '', started_at, ended_at,
                     None, 0, None, '{}'))

    def touch(self, run_id, planned_for, started_at, ended_at, at):
        """A later change edits the run row, as a state or cost update does."""
        with self.db:
            change = jobs.change(self.db, 'fixture', datetime.fromisoformat(at))
            self.changes.append(change)
            self.db.execute(
                'INSERT INTO runs (run_id,version,change_id,job,state,planned_for,reason,'
                'started_at,ended_at,unit,exit_code,manifest,spec_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (run_id, 2, change, 'fixture', 'succeeded', planned_for, '', started_at, ended_at,
                 None, 0, None, '{}'))

    def day(self, key):
        return memory.journal(self.db, day=key)

    def run_ids(self, key):
        return [r['run_id'] for r in self.day(key)['runs']]

    def test_run_counted_in_one_day_only(self):
        self.at(('shot', '2027-02-01T09:00:00+02:00', '2027-02-01T09:00:00+02:00',
                 '2027-02-01T09:20:00+02:00'))
        self.touch('shot', '2027-02-01T09:00:00+02:00', '2027-02-01T09:00:00+02:00',
                   '2027-02-01T09:20:00+02:00', '2027-02-02T10:00:00+02:00')
        self.assertEqual(self.run_ids('2027-02-01'), ['shot'])
        self.assertEqual(self.run_ids('2027-02-02'), [])
        # The later change still shows in its own day, with the run named as its subject.
        second = self.day('2027-02-02')
        self.assertEqual([c['subject'] for c in second['changes']], [['shot']])

    def test_run_across_midnight_belongs_to_its_start(self):
        self.at(('night', '2027-02-01T23:50:00+02:00', '2027-02-01T23:50:00+02:00',
                 '2027-02-02T00:10:00+02:00'))
        self.assertEqual(self.run_ids('2027-02-01'), ['night'])
        self.assertEqual(self.run_ids('2027-02-02'), [])

    def test_unstarted_run_belongs_to_planned_day(self):
        self.at(('planned', '2027-02-02T07:00:00+02:00', None, None))
        self.assertEqual(self.run_ids('2027-02-02'), ['planned'])
        self.assertEqual(self.run_ids('2027-02-01'), [])


if __name__ == '__main__':
    unittest.main()
