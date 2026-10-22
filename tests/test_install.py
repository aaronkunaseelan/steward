from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import tarfile
from unittest.mock import patch

from steward import memory, mind, runtime
from steward.state import writer, StateError
import unittest
import test_jobs
from test_jobs import NOW


class InstallTests(unittest.TestCase):
    setUp = test_jobs.JobsTests.setUp
    tick = test_jobs.JobsTests.tick

    def test_live_mind_and_missing_path(self):
        directory = Path(self.tmp.name) / 'mind'
        with writer(self.root) as store, mind.use({'mind_dir': str(directory)}):
            before = memory.system_prompt('prompt')
            (directory / 'charter.md').write_text('Changed live charter')
            self.assertIn('Changed live charter', memory.system_prompt('prompt'))
            self.assertNotIn('Changed live charter', before)
            (directory / 'soul.md').unlink()
            with self.assertRaisesRegex(StateError, str(directory / 'soul.md')):
                memory.system_prompt('')

    def test_tick_mind_commit_and_failure(self):
        directory = Path(self.tmp.name) / 'mind'
        def git(*args):
            return subprocess.run(['git', '-C', str(directory), *args], check=True, capture_output=True, text=True).stdout.strip()
        git('init')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        settings = {'mind_dir': str(directory), 'wake_times': []}
        result = self.tick(settings=settings)
        self.assertEqual(result['mind']['status'], 'ok')
        (directory / 'charter.md').write_text('changed')
        self.assertIn('charter.md', self.tick(settings=settings)['mind']['subject'])
        self.assertEqual(git('status', '--porcelain'), '')
        (directory / 'charter.md').write_text('another edit')
        hook = directory / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n'); hook.chmod(0o700)
        self.assertEqual(self.tick(settings=settings)['mind']['status'], 'unknown')
        self.assertEqual(mind.history(settings)['status'], 'ok')

    def test_scheduled_wake_once_and_missed_slots(self):
        settings = {'wake_times': ['09:00', '10:00', '11:00']}
        wakes = []
        now = datetime.fromisoformat('2026-10-17T11:15:00+03:00')
        # Event wakes are suppressed to isolate the scheduled path.
        with patch.object(runtime, 'unseen_results', return_value=False), patch.object(runtime.senses, 'events', return_value=[]):
            self.assertTrue(self.tick(at=now, settings=settings, wake_starter=lambda: wakes.append(1))['scheduled_wake'])
            with writer(self.root) as store:
                store.db.execute("INSERT INTO wakes(started_at,status,checkpoint) VALUES (?,'skipped','{}')", (now.isoformat(),))
                store.db.commit()
            self.assertFalse(self.tick(at=now+timedelta(minutes=15),settings=settings,wake_starter=lambda: wakes.append(1))['scheduled_wake'])
            self.assertEqual(wakes,[1])
            tomorrow=now+timedelta(days=1)
            self.assertTrue(self.tick(at=tomorrow,settings=settings,wake_starter=lambda: wakes.append(1))['scheduled_wake'])
            self.assertEqual(wakes,[1,1])

    def test_athens_dst(self):
        for now, expected in [('2026-03-28T07:15:00+00:00','2026-03-28T07:00:00+00:00'),('2026-03-29T06:15:00+00:00','2026-03-29T06:00:00+00:00'),('2026-10-25T07:15:00+00:00','2026-10-25T07:00:00+00:00')]:
            self.assertEqual(runtime.latest_wake(datetime.fromisoformat(now),{'wake_times':['09:00']}),datetime.fromisoformat(expected))
        # Spring's nonexistent 03:30 is skipped; autumn's repeated hour uses real instants.
        self.assertEqual(runtime.latest_wake(datetime.fromisoformat('2026-03-29T02:00:00+00:00'),{'wake_times':['03:30']}).day,28)
        self.assertEqual(runtime.latest_wake(datetime.fromisoformat('2026-10-25T01:45:00+00:00'),{'wake_times':['03:30']}).hour,1)

    def test_backup_integrity_archive_retention(self):
        data = Path(self.tmp.name) / 'backup-data'
        (data / 'memory').mkdir(parents=True)
        (data / 'memory/journal.json').write_text('filed')
        (data / 'mind/.git').mkdir(parents=True)
        (data / 'mind/.git/HEAD').write_text('fixture history')
        for _ in range(4):
            subprocess.run([str(Path('jobs/backup').resolve()),'--no-upload','--state',str(self.root),'--data',str(data)],check=True,capture_output=True)
        snapshots = sorted((data / 'backups').iterdir())
        self.assertEqual(len(snapshots),3)
        for snapshot in snapshots:
            self.assertEqual(snapshot.stat().st_mode & 0o777,0o700)
            with sqlite3.connect(next(snapshot.glob('*.sqlite'))) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            with tarfile.open(next(snapshot.glob('*.tar.gz'))) as archive:
                self.assertIn('memory/journal.json',archive.getnames())
                self.assertIn('mind/.git/HEAD',archive.getnames())
            records=[json.loads(line) for line in (snapshot/'records.jsonl').read_text().splitlines()]
            self.assertEqual(len(records),2)
            self.assertTrue(all(len(r['sha256'])==64 and r['bytes']>0 for r in records))
