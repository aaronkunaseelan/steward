from fixture_mind import TEST_CONFIG
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from fixture_mind import command as released
from steward import jobs, runtime
from steward.cli import main
from steward.notify import notify
from steward.state import SCHEMA, StateError, writer, restore

UTC = timezone.utc
NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


def spec(**extra):
    return {'justification': 'keeps a test input fresh', 'argv': [released()],
            'needs': {'every': {'min': '4h', 'max': '1d'}}, **extra}


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from fixture_mind import install
        install(self, Path(self.tmp.name))
        self.root = Path(self.tmp.name) / 'state'
        self.data = Path(self.tmp.name) / 'data'
        with writer(self.root, initialize=True):
            pass
        self.launched = []
        guard = patch('steward.notify.api', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def add(self, name='test', **extra):
        with writer(self.root) as store:
            return jobs.job_verb(store.db, 'add', name, spec(**extra), now=NOW)

    def run_now(self, name='test'):
        with writer(self.root) as store:
            return jobs.run_verb(store.db, 'now', name, 'test', now=NOW)['run_id']

    def tick(self, at=NOW, **extra):
        return runtime.tick(self.root, self.data, clock=lambda: at,
                            **{'sender': lambda *a, **k: 1, 'update_fetcher': lambda offset: [], 'telegram': {'chat_id': 1, 'owner_user_id': 2, 'topics': {'Talk': 6}}, 'launcher': self.launched.append, 'wake_starter': lambda: None, 'load_reader': lambda: (0.1, 20 * 1024**3), 'lock_reader': lambda: False, **extra})

    def test_job_list_is_read_only_and_returns_current_stored_specs(self):
        self.add('active')
        self.add('paused')
        self.add('removed')
        with writer(self.root) as store:
            jobs.job_verb(store.db, 'pause', 'paused', now=NOW)
            jobs.job_verb(store.db, 'edit', 'active', spec(justification='changed'), now=NOW)
            jobs.job_verb(store.db, 'rm', 'removed', now=NOW)
            stored = {r['name']: json.loads(r['spec_json']) for r in jobs.current(store.db, 'jobs')}
        before = (self.root / 'state.sqlite').read_bytes()
        for flags in ([], ['--json']):
            output = io.StringIO()
            with patch.dict('os.environ', STEWARD_ACTOR=''), redirect_stdout(output):
                self.assertEqual(main(['job', 'list', '--state', str(self.root), *flags]), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report['schema'], 'steward.jobs.v1')
            self.assertEqual(report['jobs'], [dict(id=name, state=state, spec=stored[name])
                                            for name, state in [('active', 'active'), ('paused', 'paused')]])
        self.assertEqual((self.root / 'state.sqlite').read_bytes(), before)

    def test_job_spec_missing_path_is_named_and_help_explains_stdin(self):
        source = '{"not": "a path"}'
        self.add()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['job', 'edit', 'test', '--state', str(self.root), '--spec', source]), 2)
        self.assertIn(source, json.loads(output.getvalue())['error'])
        self.assertIn('file not found', output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            main(['job', 'edit', '--help'])
        self.assertIn('JSON file path', output.getvalue())
        self.assertIn('stdin', output.getvalue())

    def test_due_reservation_rechecks_gap_after_manual_success(self):
        self.add(min_gap='4h', needs={'max_staleness': '1d'}, recurring=False)
        manual = self.run_now()
        self.tick()
        at = NOW + timedelta(minutes=40)
        with writer(self.root) as st:
            reservation = jobs.run_verb(st.db, 'plan', 'test', 'scheduled work',
                                        at.isoformat(), now=NOW, actor='model')['run_id']
        directory = self.data / 'runs' / 'test' / manual
        directory.mkdir(parents=True)
        (directory / 'manifest.json').write_text(json.dumps(dict(
            run_id=manual, job='test', started_at=NOW.isoformat(),
            ended_at=(NOW + timedelta(minutes=20)).isoformat(), exit_code=0, status='succeeded')))
        result = self.tick(at=at)
        self.assertIn(manual, result['folded'])
        self.assertEqual(result['started'], [])
        self.assertIn(reservation, result['refused'])
        with writer(self.root) as st:
            self.assertIn('min_gap', jobs.get(st.db, 'runs', reservation)['reason'])

    def test_unplaceable_job_reports_its_outcome_without_stopping_peers(self):
        self.add('blocked', min_gap='7d', needs={'ready_by': ['18:30']})
        self.add('good', recurring=False)
        with writer(self.root) as st:
            spec = jobs.get(st.db, 'jobs', 'blocked')['spec_json']
            started = (NOW - timedelta(hours=1)).isoformat()
            jobs.append(st.db, 'runs', dict(run_id='recent', job='blocked', state='succeeded',
                planned_for=started, started_at=started, ended_at=(NOW - timedelta(minutes=30)).isoformat(),
                reason='fixture success', spec_json=spec), jobs.change(st.db, 'fixture', NOW))
            good = jobs.run_verb(st.db, 'now', 'good', 'owner requested', now=NOW)['run_id']
        result = self.tick()
        self.assertIn(good, result['started'])
        self.assertEqual(result['unplanned']['blocked'], 'no feasible time')
        from steward import planner
        with writer(self.root) as st:
            choice = next(c for c in planner.choices(st.db, NOW) if c['job'] == 'blocked')
        self.assertEqual(choice['feasible'], [])
        self.assertIsNone(choice['default_choice'])

    def test_day_filing_includes_completions_available_at_first_next_day_tick(self):
        self.add(recurring=False)
        rid = self.run_now()
        self.tick()
        directory = self.data / 'runs' / 'test' / rid
        directory.mkdir(parents=True)
        (directory / 'manifest.json').write_text(json.dumps(dict(run_id=rid, job='test',
            started_at=NOW.isoformat(), ended_at=(NOW + timedelta(minutes=1)).isoformat(),
            exit_code=0, status='succeeded')))
        self.assertIn(rid, self.tick(at=NOW + timedelta(days=1))['folded'])
        from steward import memory
        with writer(self.root) as st:
            doc = memory.journal(st.db, day=memory.local_day(NOW).isoformat())
        self.assertEqual(next(r['state'] for r in doc['runs'] if r['run_id'] == rid), 'succeeded')

    def test_invalid_json_spec_is_unknown_without_breaking_state_or_intent(self):
        self.add()
        self.run_now()
        with writer(self.root) as st, st.db:
            row = jobs.get(st.db, 'jobs', 'test')
            jobs.append(st.db, 'jobs', dict(row, spec_json='broken json'), jobs.change(st.db, 'fixture', NOW))
        result = self.tick()
        self.assertFalse(result['started'])
        with writer(self.root) as st:
            self.assertTrue(jobs.snapshot(st.db, now=NOW)['jobs'][0]['spec'].startswith('unknown: '))
            from steward.intent import steward_doc
            self.assertEqual(steward_doc(st.db, NOW, {}, self.data)['state'], 'red')

    def test_tick_calls_qmd_status_once_for_many_capabilities_and_sense(self):
        with patch('steward.capabilities.validate'):
            self.add('one', capability='sample')
            self.add('two', capability='sample')
        calls = []
        def runner(argv):
            if argv == ['qmd', 'status', '--json']:
                calls.append(argv)
                return (Path(__file__).parent / 'fixtures/qmd.status.v1.json').read_text()
            raise OSError()
        meta = dict(calls=[], reads=['qmd:instapaper'])
        with patch('steward.capabilities.load', return_value=(meta, 'body')):
            result = self.tick(sense_runner=runner)
        self.assertEqual(result['invalid_specs'], {})
        self.assertEqual(len(calls), 1)

    def test_tick_quarantines_invalid_spec_before_planning(self):
        self.add('broken')
        self.add('good')
        with writer(self.root) as st, st.db:
            row = jobs.get(st.db, 'jobs', 'broken')
            bad = json.loads(row['spec_json'])
            bad['surprise'] = True
            jobs.append(st.db, 'jobs', dict(row, spec_json=json.dumps(bad)), jobs.change(st.db, 'fixture', NOW))
        result = self.tick()
        with writer(self.root) as st:
            self.assertFalse(any(r['job'] == 'broken' for r in jobs.current(st.db, 'runs')))
            rows = {r['name']: r for r in jobs.snapshot(st.db, now=NOW)['jobs']}
            self.assertEqual(rows['broken']['spec'], 'unknown: unknown spec field')
            self.assertEqual(rows['good']['spec'], 'ok')
            from steward.intent import steward_doc
            doc = steward_doc(st.db, NOW, {}, self.data)
            self.assertEqual(doc['state'], 'red')
            self.assertIn('broken: unknown spec field', doc['headline'])
        self.assertEqual(len(result['started']), 1)

    def test_state_includes_whole_spec(self):
        env_file = Path(self.tmp.name) / 'job.env'
        env_file.write_text('PRIVATE_ENV_CONTENT=must-not-appear\n')
        expected = dict(justification='keeps a test input fresh',
                        argv=[released('/bin/echo'), 'PRIVATE_JOB_INPUT'],
                        needs={'every': {'min': '4h', 'max': '1d'}},
                        model='haiku', cwd='/tmp', env_file=str(env_file), min_gap='1h',
                        heavy=True, recurring=False, deliver='immediate', max_minutes=7)
        self.add(**expected)
        with writer(self.root) as st:
            row = jobs.snapshot(st.db, now=NOW)['jobs'][0]
        for key, value in expected.items():
            self.assertEqual(row[key], value, key)
        self.assertNotIn('PRIVATE_ENV_CONTENT', json.dumps(row))
        for key in ('capability', 'capability_sha'):
            self.assertIsNone(row[key])

    def test_state_reports_effective_defaults_for_existing_specs(self):
        self.add()
        # Older rows omit model; exercise the view without relying on a new
        # admission-time rewrite or modifying the stored job on read.
        with writer(self.root) as st, st.db:
            original = jobs.get(st.db, 'jobs', 'test')
            legacy = json.loads(original['spec_json'])
            for key in ('model', 'heavy', 'recurring', 'deliver', 'max_minutes', 'cwd'):
                legacy.pop(key, None)
            jobs.append(st.db, 'jobs', dict(original, spec_json=json.dumps(legacy)), jobs.change(st.db, 'fixture', NOW))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['state', '--state', str(self.root)]), 0)
        row = json.loads(output.getvalue())['jobs'][0]
        for key, expected in dict(model='sol', heavy=False, recurring=True, deliver='digest',
                                  max_minutes=30, cwd=str(Path.home())).items():
            self.assertEqual(row[key], expected, key)
        for key in ('min_gap', 'env_file', 'capability'):
            self.assertIsNone(row[key], key)
        with writer(self.root) as st:
            self.assertEqual(json.loads(jobs.get(st.db, 'jobs', 'test')['spec_json']), legacy)

    def test_cli_job_add_rejects_after(self):
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps(spec(after='parent')))), redirect_stdout(output):
            self.assertEqual(main(['job', 'add', 'child', '--state', str(self.root)]), 2)
        self.assertEqual(json.loads(output.getvalue())['error'], 'unknown spec field')
        with writer(self.root) as st:
            self.assertEqual(jobs.current(st.db, 'jobs'), [])

    def test_no_recurring_job_count_limit(self):
        for i in range(10):
            self.add('job-' + str(i))
        with writer(self.root) as st:
            self.assertEqual(jobs.snapshot(st.db, now=NOW)['job_count'], 10)

    def test_failed_retry_does_not_reopen_same_ready_by_target(self):
        self.add(needs={'ready_by': ['18:30']})
        with writer(self.root) as st:
            job = jobs.get(st.db, 'jobs', 'test')
        runs = [dict(job='test', state='failed', retry_of='original', planned_for=NOW.isoformat(),
                     started_at=NOW.isoformat(), ended_at=(NOW + timedelta(minutes=1)).isoformat(),
                     spec_json=job['spec_json'])]
        at, _ = runtime.placement(job, runs, NOW + timedelta(minutes=20))
        self.assertGreater(at, NOW + timedelta(hours=3, minutes=30))

    def test_ready_by_failure_retries_once_without_moving_next_reservation(self):
        self.ready_by_retry(1)

    def test_successful_retry_recovers_obligation(self):
        self.ready_by_retry(0)

    def ready_by_retry(self, retry_code):
        self.add(needs={'ready_by': ['15:00']}, recurring=False)
        identity = self.run_now()
        self.tick()
        with writer(self.root) as st:
            next_run = jobs.plan(st.db, 'test', NOW + timedelta(hours=5), 'next target', NOW,
                                 jobs.change(st.db, 'fixture', NOW, 'owner'), explicit=True, move=False)
            st.db.commit()
        def finish(run_id, code, at):
            with writer(self.root) as st:
                row = jobs.get(st.db, 'runs', run_id)
            path = runtime.manifest_path(self.data, row)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(dict(run_id=run_id, job='test', started_at=row['started_at'],
                ended_at=at.isoformat(), exit_code=code, status='failed' if code else 'succeeded')))
        finish(identity, 1, NOW + timedelta(minutes=1))
        self.tick(NOW + timedelta(minutes=20))
        with writer(self.root) as st:
            rows = jobs.current(st.db, 'runs')
            retries = [r for r in rows if r.get('retry_of') == identity]
            self.assertEqual(len(retries), 1)
            retry = retries[0]
            self.assertEqual(retry['state'], 'started')
            self.assertIn(identity, retry['reason'])
            self.assertEqual(jobs.get(st.db, 'runs', next_run['run_id'])['planned_for'], next_run['planned_for'])
        finish(retry['run_id'], retry_code, NOW + timedelta(minutes=21))
        self.tick(NOW + timedelta(minutes=40))
        with writer(self.root) as st:
            rows = jobs.current(st.db, 'runs')
            self.assertEqual(len(rows), 3)
            if retry_code == 0:
                from steward.timing import job_obligation
                self.assertEqual(job_obligation(rows, 'test', NOW, NOW + timedelta(minutes=40))['state'], 'recovered')

    def test_failed_fold_records_public_stderr_cause(self):
        self.add(recurring=False)
        identity = self.run_now()
        self.tick()
        with writer(self.root) as st:
            row = jobs.get(st.db, 'runs', identity)
        path = runtime.manifest_path(self.data, row)
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / 'stderr.txt').write_text('diagnostic\npublic cause\n')
        path.write_text(json.dumps(dict(run_id=identity, job='test', started_at=NOW.isoformat(),
            ended_at=(NOW + timedelta(minutes=1)).isoformat(), exit_code=1, status='failed')))
        self.tick(NOW + timedelta(minutes=2))
        with writer(self.root) as st:
            self.assertEqual(jobs.get(st.db, 'runs', identity)['reason'], 'exit 1: public cause')
            self.assertEqual(len(jobs.current(st.db, 'runs')), 1)  # no ready_by, no retry

    def test_jobs_only_run_from_releases(self):
        import os
        with writer(self.root) as store, tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            lib = base / '.local/lib'
            release = lib / 'steward/releases/abc/jobs/check'
            release.parent.mkdir(parents=True)
            release.write_text('#!/bin/sh\nexit 0\n')
            release.chmod(0o755)
            installed = base / 'bin/check'
            installed.parent.mkdir()
            installed.symlink_to(release)
            current = lib / 'steward/current'
            current.symlink_to(release.parents[1])
            with patch.object(jobs, 'RELEASE_ROOT', lib, create=True), patch.dict('os.environ', PATH=str(installed.parent) + ':' + os.environ['PATH']):
                for command in [str(release), str(current / 'jobs/check'), 'check']:
                    self.assertEqual(jobs.validate_spec(spec(argv=[command]), 'test')['argv'], [command])
                foreign = lib / 'gsd/releases/def/bin/gsd'
                foreign.parent.mkdir(parents=True)
                foreign.write_text('#!/bin/sh\nexit 0\n')
                foreign.chmod(0o755)
                self.assertEqual(jobs.validate_spec(spec(argv=[str(foreign)]), 'test')['argv'], [str(foreign)])
                escape = release.parent / 'escape'
                escape.symlink_to('/bin/true')
                for command in ['/bin/true', '/usr/bin/python3', str(base / 'scratch.py'), str(escape), str(lib / 'steward/releases-evil/check'), 'not-installed']:
                    with self.subTest(command=command), self.assertRaisesRegex(StateError, 'Every job runs from a release'):
                        jobs.validate_spec(spec(argv=[command]), 'test')

    def test_spec_validation_is_atomic(self):
        with writer(self.root) as store:
            for bad, reason in ((spec(justification=''), 'justification required'),
                                (spec(model='unknown'), 'job model'),
                                (spec(heavy=1), 'invalid boolean'),
                                (spec(needs={'every': {'min': '2d', 'max': '4h'}}), 'reversed every'),
                                (spec(env_file='relative'), 'absolute path required')):
                with self.assertRaisesRegex(StateError, reason):
                    jobs.job_verb(store.db, 'add', 'bad', bad, now=NOW)
                self.assertEqual(store.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
                self.assertEqual(store.db.execute('SELECT count(*) FROM changes WHERE actor!=\'migration\'').fetchone()[0], 0)
            jobs.job_verb(store.db, 'add', 'j0', spec(), now=NOW)
            self.assertEqual(store.db.execute('SELECT count(*) FROM changes WHERE actor!=\'migration\'').fetchone()[0], 1)
            jobs.job_verb(store.db, 'pause', 'j0', now=NOW)
            self.assertEqual(jobs.get(store.db, 'jobs', 'j0')['state'], 'paused')
            jobs.job_verb(store.db, 'resume', 'j0', now=NOW)
            self.assertEqual(jobs.get(store.db, 'jobs', 'j0')['state'], 'active')

    def test_append_only_and_undo(self):
        self.add()
        with writer(self.root) as store:
            db = store.db
            edit = jobs.job_verb(db, 'edit', 'test', spec(justification='new purpose'), now=NOW)
            before = [tuple(r) for r in db.execute('SELECT * FROM jobs ORDER BY version')]
            jobs.undo(db, edit['change_id'], now=NOW)
            self.assertEqual(json.loads(jobs.get(db, 'jobs', 'test')['spec_json'])['justification'], 'keeps a test input fresh')
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM jobs WHERE version<=2 ORDER BY version')], before)

    def test_plan_moves_keep_heavy_exclusion(self):
        self.add(heavy=True)
        self.add('other', heavy=True)
        with writer(self.root) as store:
            db = store.db
            first = jobs.run_verb(db, 'plan', 'test', 'reserve', NOW.isoformat(), now=NOW)
            with self.assertRaisesRegex(StateError, 'heavy overlap'):
                jobs.run_verb(db, 'plan', 'other', 'overlap', (NOW + timedelta(minutes=20)).isoformat(), now=NOW)
            jobs.run_verb(db, 'cancel', first['run_id'], 'explicit', now=NOW)
            late = jobs.run_verb(db, 'plan', 'test', 'late', (NOW + timedelta(hours=4)).isoformat(), now=NOW)
            near = jobs.run_verb(db, 'plan', 'test', 'near term', (NOW + timedelta(minutes=20)).isoformat(), now=NOW)
            self.assertEqual(late['run_id'], near['run_id'])
            moved = jobs.run_verb(db, 'plan', 'test', 'move', (NOW + timedelta(hours=5)).isoformat(), now=NOW)
            self.assertEqual(late['run_id'], moved['run_id'])

    def test_load_and_host_heavy_lock_skip_with_reason(self):
        self.add(heavy=True)
        rid = self.run_now()
        told = []
        result = self.tick(load_reader=lambda: (3.6, 20 * 1024**3), sender=lambda topic, *a, **k: told.append(topic) or 1)
        self.assertEqual(result['refused'], [rid])
        self.assertFalse(self.launched)
        self.assertNotIn('Changes', told)  # a skip is the guard working, not news
        rid2 = self.run_now()
        result = self.tick(lock_reader=lambda: True)
        self.assertIn(rid2, result['refused'])
        self.assertFalse(self.launched)
        with writer(self.root) as store:
            self.assertIn('heavy', jobs.get(store.db, 'runs', rid2)['reason'])

    def test_two_ticks_never_double_start_and_snapshot_input(self):
        self.add(heavy=True)
        rid = self.run_now()
        self.tick()
        self.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertIn('--heavy', self.launched[0])
        self.assertIn(rid, self.launched[0])
        self.add('other', heavy=True)
        # Explicit reservation cannot overlap the started heavy run.
        with writer(self.root) as store:
            with self.assertRaises(StateError):
                jobs.run_verb(store.db, 'now', 'other', 'test', now=NOW)
            jobs.job_verb(store.db, 'edit', 'test', spec(argv=[released('/bin/false')]), now=NOW)
            self.assertEqual(json.loads(jobs.get(store.db, 'runs', rid)['spec_json'])['argv'], [released()])

    def test_pages_token_is_passed_by_name_not_in_launch_argv_or_manifest(self):
        from steward import pages
        secret = 'synthetic-pages-secret-do-not-archive'
        capability = Path(self.tmp.name) / 'mind/capabilities/page-writer.md'
        capability.write_text('---\nname: "page-writer"\nreads: []\ncalls: []\nrecord: []\npermits: ["page"]\n---\nFixture')
        self.add(recurring=False, capability='page-writer')
        rid = self.run_now()
        with patch.dict('os.environ', {pages.TOKEN_ENV: secret, 'UNRELATED_SECRET': 'not-forwarded'}):
            self.tick()
            argv = self.launched[0]
            self.assertEqual(argv[argv.index('--pass') + 1], pages.TOKEN_ENV)
            self.assertEqual(argv.count('--pass'), 1)
            self.assertNotIn(secret, json.dumps(argv))
            self.assertNotIn('UNRELATED_SECRET', json.dumps(argv))
            runtime.execute(self.root, rid, self.data, clock=lambda: NOW)
        manifest = self.data / 'runs' / 'test' / rid / 'manifest.json'
        self.assertNotIn(secret, manifest.read_text())
        self.assertNotIn(pages.TOKEN_ENV, manifest.read_text())

    def test_manifest_and_fold_success_failure_links_and_tails(self):
        for name, exit_code in [('ok', 0), ('bad', 3)]:
            command = [released(sys.executable), '-c', "import os,pathlib,sys; p=pathlib.Path(os.environ['STEWARD_RUN_DIR']); (p/'links.json').write_text('[\"https://example.org/full\"]'); (p/'records.jsonl').write_text('{}\\n'); print('x'*70000); sys.exit(" + str(exit_code) + ')']
            self.add(name, argv=command, recurring=False)
            rid = self.run_now(name)
            self.tick()
            runtime.execute(self.root, rid, self.data, clock=lambda: NOW)
            directory = self.data / 'runs' / name / rid
            manifest = json.loads((directory / 'manifest.json').read_text())
            self.assertEqual(manifest['schema'], 'steward.job.v2')
            self.assertTrue({'job', 'run_id', 'host', 'argv', 'cwd', 'started_at', 'ended_at',
                             'exit_code', 'status', 'duration', 'links', 'record_errors',
                             'transcript_collection'} <= manifest.keys())
            self.assertEqual(manifest['exit_code'], exit_code)
            self.assertEqual(manifest['links'], ['https://example.org/full'])
            self.assertEqual(manifest['record_errors'], [])
            self.assertEqual((directory / 'stdout.txt').stat().st_size, 65536)
            self.assertTrue((directory / 'records.jsonl').exists())
            self.assertEqual(runtime.execute(self.root, rid, self.data)['status'], 'already_executed')
            self.tick(at=NOW + timedelta(minutes=1))
            with writer(self.root) as store:
                self.assertEqual(jobs.get(store.db, 'runs', rid)['state'], 'succeeded' if exit_code == 0 else 'failed')

    def test_missing_manifest_timeout_requires_confirmed_gone_unit(self):
        self.add(recurring=False, max_minutes=1)
        rid = self.run_now()
        self.tick()
        self.tick(at=NOW + timedelta(minutes=7), unit_reader=lambda unit: None)
        with writer(self.root) as store:
            self.assertEqual(jobs.get(store.db, 'runs', rid)['state'], 'started')
        self.tick(at=NOW + timedelta(minutes=7), unit_reader=lambda unit: False)
        with writer(self.root) as store:
            self.assertEqual(jobs.get(store.db, 'runs', rid)['state'], 'failed')

    def succeeded(self, name, started, ended, minutes):
        with writer(self.root) as store:
            rid = jobs.run_verb(store.db, 'now', name, 'history', now=started)['run_id']
            row = jobs.get(store.db, 'runs', rid)
            with store.db:
                row.update(state='succeeded', started_at=started.isoformat(), ended_at=ended.isoformat(), cost_minutes=minutes)
                jobs.append(store.db, 'runs', row, row['change_id'])

    def test_served_staleness_job_is_not_rerun_every_tick(self):
        self.add(needs={'max_staleness': '24h'})
        self.succeeded('test', NOW - timedelta(minutes=15), NOW - timedelta(minutes=15) + timedelta(seconds=2), 0.03)
        result = self.tick()
        self.assertFalse(self.launched)
        with writer(self.root) as store:
            planned = [r for r in jobs.current(store.db, 'runs') if r['state'] == 'planned']
        self.assertEqual([jobs.instant(r['planned_for']) for r in planned], [NOW + timedelta(hours=23, minutes=40)])
        self.assertNotIn('unplanned', result)

    def test_job_due_beyond_horizon_is_later_not_unplanned(self):
        self.add(needs={'every': {'min': '6d', 'max': '8d'}})
        self.succeeded('test', NOW - timedelta(hours=1), NOW - timedelta(minutes=50), 10)
        result = self.tick()
        self.assertNotIn('unplanned', result)
        self.assertEqual(result['later'], {'test': (NOW + timedelta(days=6, hours=-1)).isoformat()})

    def test_timeout_sends_term_before_kill(self):
        import select
        import subprocess
        marker = Path(self.tmp.name) / 'finalized'
        scripts = [
            f"import signal,sys,time,pathlib\ndef bye(*a):\n    pathlib.Path({str(marker)!r}).write_text('x'); sys.exit(0)\nsignal.signal(signal.SIGTERM, bye)\nprint('ready', flush=True)\ntime.sleep(60)",
            "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('ready', flush=True)\ntime.sleep(60)",
        ]
        for index, script in enumerate(scripts):
            with self.subTest(handler=index):
                proc = subprocess.Popen([sys.executable, '-c', script], start_new_session=True,
                                        stdout=subprocess.PIPE, text=True)
                self.addCleanup(proc.stdout.close)
                self.addCleanup(lambda p=proc: runtime.stop(p, grace=0.5) if p.poll() is None else None)
                self.assertTrue(select.select([proc.stdout], [], [], 10)[0], 'handler did not become ready')
                self.assertEqual(proc.stdout.readline(), 'ready\n')
                runtime.stop(proc, grace=0.5)
                self.assertEqual(proc.returncode, 0 if index == 0 else -9)
        self.assertTrue(marker.exists())

    def test_job_rm_cancels_its_reservations_and_undo_restores_them(self):
        # Removing a job must cancel its reservations, not leave them planned.
        self.add(needs={'ready_by': ['18:30']})
        self.tick()
        with writer(self.root) as store:
            cid = jobs.job_verb(store.db, 'rm', 'test', now=NOW)['change_id']
            self.assertEqual({(r['state'], r['ended_at']) for r in jobs.current(store.db, 'runs')},
                             {('cancelled', NOW.isoformat())})
            jobs.undo(store.db, cid, now=NOW)
            self.assertEqual({(r['state'], r['ended_at']) for r in jobs.current(store.db, 'runs')}, {('planned', None)})

    def test_job_reported_weighted_tokens_are_its_cost(self):
        directory = Path(self.tmp.name) / 'run'
        directory.mkdir()
        (directory / 'records.jsonl').write_text(json.dumps({'kind': 'gsd-steady', 'weighted_tokens': 1200000.25}) + '\n')
        manifest = dict(started_at=NOW.isoformat(), ended_at=(NOW + timedelta(minutes=40)).isoformat(), links=[])
        self.assertEqual(runtime.learned_cost(manifest, directory), dict(cost_minutes=40.0, cost_tokens=1200000.25))
        self.assertEqual(runtime.learned_cost(manifest, Path(self.tmp.name) / 'none')['cost_tokens'], None)

    def test_run_exec_records_errors_without_losing_valid_costs(self):
        content = '\n'.join([
            json.dumps({'schema': 'steward.record.v1', 'weighted_tokens': 10.5, 'provider': 'private-invalid', 'extra': 'allowed'}),
            json.dumps({'weighted_tokens': -1}),
            'not JSON',
            '[]',
            json.dumps({'schema': 'invalid', 'weighted_tokens': True, 'terminated': 'yes', 'links': [1], 'model_calls': 0.5}),
            json.dumps({'weighted_tokens': 0}),
            json.dumps({'weighted_tokens': 2.25}),
        ]) + '\n' + ('{"model_calls": -1}\n' * 30) + '{"weighted_tokens": 3.5}\n'
        command = [released(sys.executable), '-c', "import os,pathlib; (pathlib.Path(os.environ['STEWARD_RUN_DIR'])/'records.jsonl').write_text(" + repr(content) + ')']
        self.add(argv=command, recurring=False)
        rid = self.run_now()
        self.tick()
        result = runtime.execute(self.root, rid, self.data, clock=lambda: NOW)
        self.assertEqual(result['status'], 'succeeded')
        directory = self.data / 'runs' / 'test' / rid
        manifest = json.loads((directory / 'manifest.json').read_text())
        errors = manifest['record_errors']
        self.assertEqual(len(errors), 20)
        for expected in ('line 1: $.provider:', 'line 2: $.weighted_tokens:', 'line 3: invalid JSON', 'line 4: $: expected object', 'line 5: $.schema:', 'line 5: $.terminated:', 'line 5: $.links[0]:', 'line 5: $.model_calls:'):
            self.assertTrue(any(e.startswith(expected) for e in errors), expected)
        self.assertNotIn('private', json.dumps(errors))
        self.assertEqual(runtime.learned_cost(manifest, directory)['cost_tokens'], 16.25)
        self.tick(at=NOW + timedelta(minutes=1))
        with writer(self.root) as store:
            row = jobs.get(store.db, 'runs', rid)
            self.assertEqual(row['state'], 'succeeded')
            self.assertEqual(row['cost_tokens'], 16.25)

    def test_zero_weighted_tokens_is_known_cost(self):
        directory = Path(self.tmp.name) / 'run'
        directory.mkdir()
        (directory / 'records.jsonl').write_text('{"weighted_tokens": 0}\n')
        manifest = dict(started_at=NOW.isoformat(), ended_at=NOW.isoformat(), links=[])
        self.assertEqual(runtime.learned_cost(manifest, directory)['cost_tokens'], 0)

    def test_launch_failure_is_recorded_and_not_relaunched(self):
        self.add(recurring=False)
        rid = self.run_now()
        def fail(argv):
            raise OSError('not found')
        self.tick(launcher=fail)
        self.tick()
        self.assertFalse(self.launched)
        with writer(self.root) as store:
            self.assertEqual(jobs.get(store.db, 'runs', rid)['state'], 'failed')

    def test_notify_intent_precedes_effect_and_told_dedup(self):
        with writer(self.root) as store:
            def send(topic, text, **options):
                other = sqlite3.connect(self.root / 'state.sqlite')
                self.assertEqual(other.execute('SELECT state FROM outbox').fetchone()[0], 'pending')
                self.assertEqual(other.execute('SELECT count(*) FROM changes WHERE actor!=\'migration\'').fetchone()[0], 1)
                other.close()
                return 42
            first = notify(store.db, 'Finds', 'useful source', 'https://example.org', sender=send, now=NOW, deliver='immediate')
            second = notify(store.db, 'Finds', 'again', 'https://example.org', sender=lambda *args, **options: self.fail('duplicate send'), now=NOW)
            self.assertEqual(first['message_id'], 42)
            self.assertEqual(second['status'], 'already_told')
            self.assertEqual(store.db.execute('SELECT message_id FROM told').fetchone()[0], 42)
            with self.assertRaises(StateError):
                jobs.undo(store.db, first['change_id'], now=NOW)

    def test_notify_failure_keeps_pending_and_no_secret_in_report(self):
        def fail(*args, **options):
            raise OSError('SECRET_TOKEN')
        with writer(self.root) as store:
            result = notify(store.db, 'Alerts', 'delivery test', sender=fail, now=NOW)
            self.assertEqual(result['status'], 'pending')
            self.assertNotIn('SECRET', json.dumps(result))
            self.assertEqual(store.db.execute('SELECT count(*) FROM told').fetchone()[0], 0)

    def test_migration_drops_legacy_history_preserves_wakes_and_backup(self):
        legacy = Path(self.tmp.name) / 'legacy'
        legacy.mkdir()
        db = sqlite3.connect(legacy / 'state.sqlite')
        db.executescript(SCHEMA)
        db.execute("INSERT INTO history(at,event) VALUES ('then','preserved')")
        db.execute("INSERT INTO wakes VALUES (1,'then','idle','{}')")
        db.commit(); db.close()
        backup = Path(self.tmp.name) / 'backup'
        with writer(legacy) as store:
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 20)
            self.assertIsNone(store.db.execute("SELECT name FROM sqlite_master WHERE name='history'").fetchone())
            self.assertEqual(store.db.execute('SELECT status FROM wakes').fetchone()[0], 'skipped')
            store.backup(backup)
        restored = Path(self.tmp.name) / 'restored'
        restore(backup, restored)
        with writer(restored) as store:
            self.assertEqual(store.db.execute('SELECT status FROM wakes').fetchone()[0], 'skipped')

    def test_wake_gate_and_model_setting_and_no_writer_during_harness(self):
        with writer(self.root) as store:
            with store.db:
                store.db.execute("INSERT INTO told VALUES ('orientation:2026-10-16', '2026-10-16T04:00:00+00:00', NULL, NULL, 'Today', 1)")
        calls = []
        def runner(argv):
            calls.append(argv)
            with writer(self.root):
                pass
            return 'tty-fixture'
        kwargs = dict(clock=lambda: NOW, runner=runner, prompt='fixture prompt')
        self.assertEqual(runtime.wake(self.root, settings={'wake_model': True}, **kwargs)['model_calls'], 0)
        self.add()
        self.assertEqual(runtime.wake(self.root, settings={'wake_model': False}, **kwargs)['model_calls'], 0)
        self.assertFalse(calls)
        report = runtime.wake(self.root, settings={'wake_model': True}, **kwargs)
        self.assertEqual(report['model_calls'], 1)
        self.assertEqual(report['harness_run_id'], 'tty-fixture')
        self.assertEqual(runtime.wake(self.root, settings={'wake_model': True}, **kwargs)['model_calls'], 0)
        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertEqual(args[2:6], ['-a', 'claude', '-m', 'claude-opus-5-5'])
        self.assertEqual(args[args.index('--effort') + 1], 'medium')
        self.assertEqual(args[args.index('--permission-mode') + 1], 'bypassPermissions')
        self.assertIn('--exclude-dynamic-system-prompt-sections', args)
        self.assertEqual(args.count('--'), 1)
        self.assertLess(args.index('--'), args.index('--effort'))
        stable = args[args.index('--append-system-prompt') + 1]
        self.assertIn('fixture prompt', stable)
        self.assertIn('SYNTHETIC_SOUL_MARKER', stable)
        self.assertIn('SYNTHETIC_CHARTER_MARKER', stable)
        page_path = Path(args[args.index('-C') + 2])
        page = page_path.read_text()
        self.assertNotIn('fixture prompt', page)
        self.assertNotIn('SYNTHETIC_SOUL_MARKER', page)
        self.assertNotIn('SYNTHETIC_CHARTER_MARKER', page)
        self.assertEqual(page_path.name, 'index.md')
        self.assertIn('- What I carry:', page)
        self.assertIn('## What I carry', page)  # every wake carries the synthesis (memory law, F-67)
        self.assertIn('## What I carry', page_path.with_name('briefing.md').read_text())
        self.assertIn('## Now [', page)

    def test_only_failed_runs_are_news(self):
        for name, argv in [('ok', [released()]), ('bad', [released('/bin/false')])]:
            self.add(name, argv=argv, recurring=False)
            rid = self.run_now(name)
            self.tick()
            runtime.execute(self.root, rid, self.data, clock=lambda: NOW)
            self.tick(at=NOW + timedelta(minutes=1))
            with writer(self.root) as store:
                store.db.execute("UPDATE changes SET actor='tick'")
                self.assertEqual(runtime.gate(store.db), 'nothing new' if name == 'ok' else 'run failures')

    def test_manual_run_is_not_news(self):
        self.add(recurring=False)
        with writer(self.root) as store, store.db:
            store.db.execute("UPDATE changes SET actor='tick'")
        self.run_now()
        with writer(self.root) as store:
            self.assertEqual(runtime.gate(store.db), 'nothing new')

    def test_min_gap_follows_the_last_success_even_manual(self):
        from fixture_mind import TEST_ZONE
        from steward.planner import rule
        athens = TEST_ZONE
        job = dict(name='g', spec_json=json.dumps(spec(needs={'ready_by': ['06:30', '18:30']}, heavy=False, max_minutes=30, min_gap='6h')))
        manual = dict(job='g', state='succeeded', started_at=datetime(2026, 10, 16, 23, 30, tzinfo=athens).isoformat(),
                      ended_at=datetime(2026, 10, 16, 23, 50, tzinfo=athens).isoformat(), spec_json=job['spec_json'])
        now = datetime(2026, 10, 17, 0, 0, tzinfo=athens)
        at, reason = runtime.placement(job, [manual], now)
        self.assertEqual(at.astimezone(athens).strftime('%H:%M'), '05:40')  # not 05:00; still done by 06:30
        self.assertIn('min_gap 6h', reason)
        self.assertEqual(rule(job, [manual], datetime(2026, 10, 17, 5, 20, tzinfo=athens), now), 'min_gap')
        # A gap that would miss the deadline yields to the next one.
        manual.update(started_at=datetime(2026, 10, 16, 23, 50, tzinfo=athens).isoformat(), ended_at=datetime(2026, 10, 17, 0, 20, tzinfo=athens).isoformat())
        at, _ = runtime.placement(job, [manual], datetime(2026, 10, 17, 0, 30, tzinfo=athens))
        self.assertEqual(at.astimezone(athens).strftime('%H:%M'), '17:00')
        # A failed run does not hold back the retry.
        manual['state'] = 'failed'
        at, _ = runtime.placement(job, [manual], datetime(2026, 10, 17, 0, 30, tzinfo=athens))
        self.assertEqual(at.astimezone(athens).strftime('%H:%M'), '05:00')

    def test_wake_lock_and_event_cooldown(self):
        import fcntl
        with (self.root / 'wake.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = runtime.wake(self.root, clock=lambda: NOW)
            self.assertEqual(result['status'], 'refused')
            with writer(self.root) as st:
                row = st.db.execute('SELECT * FROM wakes ORDER BY id DESC LIMIT 1').fetchone()
                self.assertEqual(row['status'], 'refused')
                self.assertEqual(row['gate_reason'], 'wake lock busy')
        self.add(recurring=False)
        rid = self.run_now()
        self.tick()
        runtime.execute(self.root, rid, self.data, clock=lambda: NOW)
        wake_calls = []
        self.tick(at=NOW + timedelta(minutes=1), wake_starter=lambda: wake_calls.append(1))
        self.assertEqual(wake_calls, [1])
        runtime.wake(self.root, settings={'wake_model': False}, clock=lambda: NOW + timedelta(minutes=1), prompt='test')
        self.tick(at=NOW + timedelta(minutes=15), wake_starter=lambda: wake_calls.append(1))
        self.assertEqual(wake_calls, [1])

    def test_default_placement_ready_by_every_staleness(self):
        with writer(self.root) as store:
            for name, need in [('ready', {'ready_by': ['18:30']}), ('every', {'every': {'min': '4h', 'max': '1d'}}), ('fresh', {'max_staleness': '1d'})]:
                jobs.job_verb(store.db, 'add', name, spec(needs=need), now=NOW)
        self.tick()
        with writer(self.root) as store:
            runs = jobs.current(store.db, 'runs')
            self.assertEqual({r['job']: (jobs.instant(r['planned_for']), r['state']) for r in runs},
                             {'ready': (NOW + timedelta(hours=2), 'planned'),
                              'every': (NOW, 'started'), 'fresh': (NOW, 'started')})

    def test_ready_by_served_deadline_is_not_run_again(self):
        # A successful run must serve its ready_by target only once.
        from fixture_mind import TEST_ZONE
        athens = TEST_ZONE
        job = dict(name='g', spec_json=json.dumps(spec(needs={'ready_by': ['06:30', '18:30']}, heavy=False, max_minutes=30)))
        done = dict(job='g', state='succeeded', started_at=datetime(2026, 10, 17, 5, 15, tzinfo=athens).isoformat(),
                    ended_at=datetime(2026, 10, 17, 5, 15, 4, tzinfo=athens).isoformat(), spec_json=job['spec_json'], minutes=None)
        at, _ = runtime.placement(job, [done], datetime(2026, 10, 17, 5, 20, tzinfo=athens))
        self.assertEqual(at.astimezone(athens).strftime('%H:%M'), '18:00')  # not 06:15 again

    def test_ready_by_missed_slot_is_retried_before_its_deadline(self):
        # A load-guard skip at 05:15 must not lose the 06:30 deadline to the evening one.
        from fixture_mind import TEST_ZONE
        athens = TEST_ZONE
        job = dict(name='g', spec_json=json.dumps(spec(needs={'ready_by': ['06:30', '18:30']}, heavy=False, max_minutes=30)))
        at, _ = runtime.placement(job, [], datetime(2026, 10, 17, 4, 0, tzinfo=athens))
        self.assertEqual(at.astimezone(athens).strftime('%H:%M'), '05:00')  # room for one rerun
        at, _ = runtime.placement(job, [], datetime(2026, 10, 17, 5, 20, tzinfo=athens))
        self.assertEqual(at.astimezone(athens).strftime('%H:%M'), '05:20')
        at, _ = runtime.placement(job, [], datetime(2026, 10, 17, 5, 50, tzinfo=athens))
        self.assertEqual(at.astimezone(athens).strftime('%H:%M'), '17:00')  # too late to finish by 06:30

    def test_tick_and_wake_refuse_outside_their_unit(self):
        # The unit supplies credentials; shell execution would manufacture unknown reads.
        out = io.StringIO()
        with patch.dict('os.environ', {'INVOCATION_ID': 'from-another-service'}), redirect_stdout(out), redirect_stderr(out):
            for command in ('tick', 'wake'):
                self.assertEqual(main([command, '--state', str(self.root)]), 2)
        self.assertIn('systemctl --user start steward-tick.service', out.getvalue())
        self.assertIn('systemctl --user start steward-wake.service', out.getvalue())

    def test_unknown_sense_and_recovery_are_not_events(self):
        from steward import senses
        cal = lambda d: {'calendars': {'status': 'ok', 'data': {'digest': d, 'events': []}}}
        with writer(self.root) as store:
            db = store.db
            for observation in (cal('a'), {'calendars': {'status': 'unknown', 'reason': 'no keyring'}}, cal('a'), cal('b')):
                with db:
                    senses.save(db, observation, NOW, jobs.change(db, 'tick', NOW, 'tick'))
            events = [r[0] for r in db.execute("SELECT event FROM senses WHERE name='calendars' ORDER BY id")]
        self.assertEqual(events, [0, 0, 0, 1])

    def test_cli_job_add_uses_config_and_state_is_read_only(self):
        config = Path(self.tmp.name) / 'config.toml'
        config.write_text(TEST_CONFIG)
        source = Path(self.tmp.name) / 'spec.json'
        source.write_text(json.dumps(spec()))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['job', 'add', 'one', '--spec', str(source), '--state', str(self.root), '--config', str(config)]), 0)
            self.assertEqual(main(['job', 'add', 'two', '--spec', str(source), '--state', str(self.root), '--config', str(config)]), 0)
        before = (self.root / 'state.sqlite').read_bytes()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['state', '--state', str(self.root), '--config', str(config)]), 0)
        self.assertEqual(json.loads(output.getvalue())['job_count'], 2)
        self.assertEqual((self.root / 'state.sqlite').read_bytes(), before)

    def test_run_exec_can_read_intent_while_launching_tick_holds_writer(self):
        self.add(recurring=False)
        rid = self.run_now()
        def launch(argv):
            # A real transient unit can start before batch-run returns to tick.
            runtime.execute(self.root, rid, self.data, clock=lambda: NOW)
        self.tick(launcher=launch)
        self.tick()
        with writer(self.root) as store:
            self.assertEqual(jobs.get(store.db, 'runs', rid)['state'], 'succeeded')

    def test_invalid_manifest_cannot_claim_success_or_block_reconciliation(self):
        self.add(recurring=False, max_minutes=1)
        rid = self.run_now()
        self.tick()
        path = self.data / 'runs' / 'test' / rid / 'manifest.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'job': 'wrong', 'run_id': rid, 'status': 'succeeded', 'exit_code': 0, 'started_at': NOW.isoformat(), 'ended_at': NOW.isoformat()}))
        self.tick(at=NOW + timedelta(minutes=7), unit_reader=lambda unit: False)
        with writer(self.root) as store:
            self.assertEqual(jobs.get(store.db, 'runs', rid)['state'], 'failed')

    def test_no_feasible_time_does_not_starve_a_healthy_peer(self):
        self.add('blocked', min_gap='30h')
        rid = self.run_now('blocked')
        self.tick()
        runtime.execute(self.root, rid, self.data, clock=lambda: NOW + timedelta(minutes=1))
        self.launched.clear()
        self.add('good')
        result = self.tick(at=NOW + timedelta(minutes=20))
        self.assertEqual(result.get('unplanned'), {'blocked': 'no feasible time'})
        self.assertEqual(len(result['started']), 1)
        self.assertEqual(len(self.launched), 1)
        with writer(self.root) as store:
            pending = [r['job'] for r in jobs.current(store.db, 'runs') if r['state'] in ('planned', 'started')]
            self.assertEqual(pending, ['good'])

    def test_heavy_default_placement_has_no_overlap_and_started_lane_defers(self):
        self.add('first', heavy=True, max_minutes=30)
        self.add('second', heavy=True, max_minutes=30)
        self.tick()
        with writer(self.root) as store:
            runs = jobs.current(store.db, 'runs')
            second = next(r for r in runs if r['job'] == 'second')
            # New reservations may start now; heavy ones still queue.
            self.assertEqual(jobs.instant(second['planned_for']), NOW + timedelta(minutes=40))
        self.tick(at=NOW + timedelta(minutes=40), unit_reader=lambda unit: True)
        self.assertEqual(len(self.launched), 1)
        with writer(self.root) as store:
            self.assertEqual(jobs.get(store.db, 'runs', second['run_id'])['state'], 'refused')

    def test_undo_reservation_creation_records_cancellation_time(self):
        self.add(recurring=False)
        with writer(self.root) as store:
            planned = jobs.run_verb(store.db, 'plan', 'test', 'test', (NOW + timedelta(hours=4)).isoformat(), now=NOW)
            end = NOW + timedelta(minutes=1)
            jobs.undo(store.db, planned['change_id'], now=end)
            row = jobs.get(store.db, 'runs', planned['run_id'])
            self.assertEqual((row['state'], row['ended_at']), ('cancelled', end.isoformat()))
            self.assertIsNone(row['started_at'])

    def test_undo_creation_and_cancel_restore_reservation(self):
        added = self.add(recurring=False)
        with writer(self.root) as store:
            planned = jobs.run_verb(store.db, 'plan', 'test', 'test', (NOW + timedelta(hours=4)).isoformat(), now=NOW)
            cancelled = jobs.run_verb(store.db, 'cancel', planned['run_id'], 'test', now=NOW)
            jobs.undo(store.db, cancelled['change_id'], now=NOW)
            self.assertEqual(jobs.get(store.db, 'runs', planned['run_id'])['state'], 'planned')
            jobs.undo(store.db, added['change_id'], now=NOW)
            self.assertEqual(jobs.get(store.db, 'jobs', 'test')['state'], 'removed')

    def test_harness_timeout_stops_detached_tty_worker(self):
        import subprocess
        from unittest.mock import Mock
        tty_root = Path(self.tmp.name) / 'tty'
        directory = self.data / 'wakes' / '1'
        meta = tty_root / 'runs' / 'tty-test' / 'meta.json'
        meta.parent.mkdir(parents=True)
        meta.write_text(json.dumps({'cwd': str(directory), 'run': 'tty-test'}))
        child = Mock()
        child.communicate.side_effect = [subprocess.TimeoutExpired('tty', 2280), ('', None)]
        with patch.dict('os.environ', {'TTY_STATE': str(tty_root)}), patch('steward.runtime.subprocess.Popen', return_value=child), patch('steward.runtime.subprocess.run') as stop:
            with self.assertRaises(StateError) as error:
                runtime.harness(['tty', 'run', '-C', str(directory), 'briefing.md'])
            self.assertEqual(error.exception.run_id, 'tty-test')
            self.assertEqual(stop.call_args.args[0], ['tty', 'stop', 'tty-test'])

    def test_tick_records_changed_release_and_mind_before_its_change(self):
        with patch('steward.runtime.release_sha', return_value='release-fixture'), patch('steward.mind.history', return_value={'status':'ok', 'sha':'mind-fixture'}):
            self.tick()
            self.tick()
        with writer(self.root) as store:
            rows = [tuple(r) for r in store.db.execute("SELECT verb,summary FROM changes WHERE verb IN ('release','mind') ORDER BY rowid")]
            self.assertEqual(rows, [('release','release-fixture'), ('mind','mind-fixture')])

    def test_tick_is_one_change_set_including_fold_and_dispatch(self):
        self.add('one', recurring=False)
        self.add('two', recurring=False)
        rid = self.run_now('one')
        self.tick()
        runtime.execute(self.root, rid, self.data, clock=lambda: NOW)
        self.run_now('two')
        with writer(self.root) as store:
            before = store.db.execute('SELECT count(*) FROM changes WHERE actor!=\'migration\'').fetchone()[0]
        result = self.tick()
        with writer(self.root) as store:
            self.assertEqual(store.db.execute('SELECT count(*) FROM changes WHERE actor!=\'migration\'').fetchone()[0], before + 1)
            self.assertEqual(jobs.get(store.db, 'runs', rid)['change_id'], result['change_id'])
