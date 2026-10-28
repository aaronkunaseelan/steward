"""Run costs use the summary unit, never tty/raw totals plus record reports."""
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fixture_mind import use_settings
from steward import jobs, planner, runtime, summary
from steward.state import writer
from test_jobs import NOW

FIXTURES = Path(__file__).parent / 'fixtures'


class TokenCostTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(patch.dict(os.environ, STEWARD_DATA=str(self.root / 'data')))
        self.directory = self.root / 'run'
        self.directory.mkdir()
        self.manifest = dict(started_at=NOW.isoformat(), ended_at=(NOW + timedelta(minutes=10)).isoformat())

    def session(self, agent):
        sessions = self.directory / 'sessions'
        sessions.mkdir()
        shutil.copyfile(FIXTURES / f'transcript-{agent}.jsonl', sessions / 'native.jsonl')
        meta = dict(sessions=[dict(file='native.jsonl', agent=agent)])
        (self.directory / 'transcript-meta.json').write_text(json.dumps(meta))
        return meta

    def cost(self):
        return runtime.learned_cost(self.manifest, self.directory)['cost_tokens']

    def test_tty_claude_uses_weighted_sessions_not_result_usage(self):
        meta = self.session('claude')
        # Claude repeats usage across content blocks; don't charge it twice.
        path = self.directory / 'sessions/native.jsonl'
        row = json.loads(path.read_text().splitlines()[2])
        with path.open('a') as stream:
            stream.write(json.dumps(row) + '\n')
        tty = self.root / 'tty/runs/example'
        tty.mkdir(parents=True)
        (tty / 'result.json').write_text(json.dumps(dict(usage=dict(input_tokens=12, output_tokens=8))))
        self.manifest['links'] = [dict(tty_run='example')]
        with patch.dict(os.environ, TTY_STATE=str(self.root / 'tty')):
            self.assertEqual(self.cost(), 345)
        self.assertEqual(summary.session_tokens(self.directory, meta['sessions'])[1]['weighted'], self.cost())

    def test_non_tty_sessions_override_reported_weighted_tokens(self):
        self.session('claude')
        (self.directory / 'records.jsonl').write_text('{"weighted_tokens":345}\n')
        self.assertEqual(self.cost(), 345)

    def test_codex_cached_input_is_cache_read_and_totals_are_cumulative(self):
        self.session('codex')
        self.assertEqual(self.cost(), 290)

    def test_all_sessions_contribute_and_rounding_happens_once(self):
        sessions = self.directory / 'sessions'
        sessions.mkdir()
        meta = []
        for i in range(9):
            filename = f'{i}.jsonl'
            (sessions / filename).write_text(json.dumps(dict(type='assistant', message=dict(
                id=f'msg-{i}', usage=dict(input_tokens=2, output_tokens=0,
                                         cache_creation_input_tokens=1, cache_read_input_tokens=3)))) + '\n')
            meta.append(dict(file=filename, agent='claude'))
        (self.directory / 'transcript-meta.json').write_text(json.dumps(dict(sessions=meta)))
        self.assertEqual(summary.session_tokens(self.directory, meta),
                         (9, dict(input=18, output=0, cache_write=9, cache_read=27, weighted=32)))
        self.assertEqual(self.cost(), 32)

    def test_no_usage_is_unknown_and_records_are_only_a_no_session_fallback(self):
        self.assertIsNone(self.cost())
        self.assertIsNone(runtime.learned_cost(self.manifest)['cost_tokens'])
        (self.directory / 'records.jsonl').write_text('{"weighted_tokens":12.25}\n')
        self.assertEqual(self.cost(), 12.25)
        self.session('claude')
        (self.directory / 'sessions/native.jsonl').write_text('{"type":"user"}\n')
        self.assertIsNone(self.cost())

    def test_writer_recomputes_history_once_and_keeps_unknowns_out_of_medians(self):
        state = self.root / 'state'
        with writer(state, initialize=True) as store, store.db:
            db = store.db
            cid = jobs.change(db, 'fixture', NOW)
            cases = [('known', 1800, 345), ('missing', 100, None), ('zero', 200, 0),
                     ('invalid', 300, -1), ('failed', 900, 290), ('running', 777, 1)]
            for name, before, after in cases:
                directory = self.root / 'data/runs/test/failed' if name == 'failed' else self.root / name
                directory.mkdir(parents=True)
                if name != 'missing':
                    (directory / 'summary.json').write_text(json.dumps(dict(tokens=dict(weighted=after))))
                row = dict(run_id=name, job='test', state='started' if name == 'running' else 'failed' if name == 'failed' else 'succeeded',
                           planned_for=NOW.isoformat(), started_at=NOW.isoformat(), ended_at=NOW.isoformat(),
                           spec_json='{}', reason='fixture', manifest=None if name == 'failed' else str(directory / 'manifest.json'),
                           cost_minutes=10, cost_tokens=before)
                first = jobs.append(db, 'runs', row, cid)
                jobs.append(db, 'runs', first, cid)
            # This is a schema-20 database predating the token-unit correction.
            db.execute("DELETE FROM message_meta WHERE key='cost_unit'")
            self.assertEqual(planner.cost(jobs.current(db, 'runs'), 'test')['median_tokens'], 300)
        with writer(state) as store:
            db = store.db
            costs = {r['run_id']: r['cost_tokens'] for r in jobs.current(db, 'runs')}
            self.assertEqual(costs, dict(known=345, missing=None, zero=0, invalid=None, failed=290, running=777))
            self.assertEqual(planner.cost(jobs.current(db, 'runs'), 'test')['median_tokens'], 290)
            self.assertEqual([r[0] for r in db.execute("SELECT cost_tokens FROM runs WHERE run_id='known'")], [345, 345])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 20)
            self.assertEqual(json.loads(db.execute("SELECT value FROM message_meta WHERE key='cost_unit'").fetchone()[0]), 'weighted_tokens')
        # Don't rescan history on the hot writer path or overwrite later corrections.
        (self.root / 'known/summary.json').write_text('{"tokens":{"weighted":999}}')
        with writer(state) as store:
            self.assertEqual(jobs.get(store.db, 'runs', 'known')['cost_tokens'], 345)
