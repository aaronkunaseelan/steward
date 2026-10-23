"""X-30: model shell scope, pull packets and measured execution limits."""
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_jobs
from steward import capabilities, jobs, runtime
from steward.state import writer


class JobShapeTests(unittest.TestCase):
    setUp = test_jobs.JobsTests.setUp
    add = test_jobs.JobsTests.add
    run_now = test_jobs.JobsTests.run_now
    tick = test_jobs.JobsTests.tick

    def success(self, minutes, at=None, **overrides):
        at = at or test_jobs.NOW
        identity = self.run_now()
        with writer(self.root) as store:
            row = jobs.get(store.db, 'runs', identity)
            bound = json.loads(row['spec_json'])
            bound['_capability_sha'] = jobs.capability_sha(bound)
            bound.update(overrides)
            jobs.append(store.db, 'runs', dict(row, state='succeeded', started_at=at.isoformat(),
                ended_at=(at + timedelta(minutes=minutes)).isoformat(),
                spec_json=json.dumps(bound)), jobs.change(store.db, 'fixture', at))
            store.db.commit()
        return identity

    def profile(self):
        with writer(self.root) as store:
            return jobs.execution_profile(store.db, 'test')

    def test_first_bound_then_last_five_successes_with_floor(self):
        self.add(max_minutes=40)
        self.assertEqual(self.profile()['limit_minutes'], 40)
        self.assertEqual(self.profile()['limit_source'], 'first-run bound')
        self.assertIsNone(self.profile()['measured_at'])
        for i, minutes in enumerate((100, 2, 4, 6, 8, 10)):
            self.success(minutes, test_jobs.NOW + timedelta(hours=i * 2))
        self.assertEqual(self.profile()['limit_minutes'], 100)
        self.assertEqual(self.profile()['limit_source'], 'learned')
        for i in range(5):
            self.success(0.3, test_jobs.NOW + timedelta(days=i + 1))
        self.assertEqual(self.profile()['limit_minutes'], 5)

    def test_failures_do_not_train_and_argv_change_resets_even_if_reverted(self):
        from fixture_mind import command
        self.add(max_minutes=40)
        self.success(7)
        self.assertEqual(self.profile()['limit_minutes'], 70)
        with writer(self.root) as store:
            original = json.loads(jobs.get(store.db, 'jobs', 'test')['spec_json'])
            jobs.job_verb(store.db, 'edit', 'test', dict(original, argv=[command('/bin/false')]), now=test_jobs.NOW + timedelta(hours=1))
            jobs.job_verb(store.db, 'edit', 'test', original, now=test_jobs.NOW + timedelta(hours=2))
        self.assertEqual(self.profile()['limit_minutes'], 40)
        self.assertIsNone(self.profile()['measured_at'])
        identity = self.success(100, test_jobs.NOW + timedelta(hours=3))
        with writer(self.root) as store:
            row = jobs.get(store.db, 'runs', identity)
            jobs.append(store.db, 'runs', dict(row, state='failed'), jobs.change(store.db, 'fixture', test_jobs.NOW))
            store.db.commit()
        self.assertEqual(self.profile()['limit_source'], 'first-run bound')

    def test_capability_change_resets_measurement(self):
        with patch('steward.capabilities.validate'):
            self.add(capability='papers-from-saves')
        self.success(8)
        self.assertEqual(self.profile()['limit_minutes'], 80)
        from steward import mind
        path = mind.directory() / 'capabilities/papers-from-saves.md'
        path.write_text(path.read_text() + '\nChanged policy\n')
        self.assertEqual(self.profile()['limit_source'], 'first-run bound')
        self.success(3, test_jobs.NOW + timedelta(hours=1))
        self.assertEqual(self.profile()['limit_minutes'], 30)

    def test_state_and_dispatch_use_same_limit_and_freeze_it(self):
        self.add(max_minutes=40)
        self.success(6)
        identity = self.run_now()
        self.tick()
        with writer(self.root) as store:
            state = jobs.snapshot(store.db, now=test_jobs.NOW)['jobs'][0]
            self.assertEqual((state['limit_minutes'], state['limit_source']), (60, 'learned'))
            bound = json.loads(jobs.get(store.db, 'runs', identity)['spec_json'])
            self.assertEqual(bound['_limit_minutes'], 60)
        self.assertIn('65m', self.launched[0])
        with patch('steward.runtime.subprocess.Popen') as popen, patch('steward.runtime.archive_attempt', return_value='complete'):
            popen.return_value.wait.return_value = 0
            runtime.execute(self.root, identity, self.data, clock=lambda: test_jobs.NOW)
            popen.return_value.wait.assert_called_once_with(timeout=60 * 60)

    def test_limit_kill_archives_native_session_and_tokens(self):
        import shutil
        import sys
        from fixture_mind import command
        from steward import archive, summary
        home = Path(self.tmp.name) / 'home'
        native = home / '.codex/sessions/2026/10/16/rollout-synthetic-killed.jsonl'
        native.parent.mkdir(parents=True)
        shutil.copyfile(Path(__file__).parent / 'fixtures/transcript-codex.jsonl', native)
        script = Path(self.tmp.name) / 'job.py'
        script.write_text('import json, os, pathlib, time\n'
                          'pathlib.Path(os.environ["STEWARD_RUN_DIR"], "links.json").write_text(json.dumps({"tty_run":"killed-tty"}))\n'
                          'time.sleep(30)\n')
        self.add(argv=[command(sys.executable), str(script)])
        identity = self.run_now()
        self.tick()
        bindir = Path(self.tmp.name) / 'tty-bin'
        bindir.mkdir()
        tty = bindir / 'tty'
        tty.write_text('#!/bin/sh\nif [ "$1" = stop ]; then echo stopped > "' + str(bindir / 'stopped') + '"; else '
                       'echo \'{"agent":"codex","thread":"killed","ended":"' + test_jobs.NOW.isoformat() + '"}\'; fi\n')
        tty.chmod(0o755)
        with writer(self.root) as store, store.db:
            row = jobs.get(store.db, 'runs', identity)
            bound = dict(json.loads(row['spec_json']), _limit_minutes=0.005)
            jobs.append(store.db, 'runs', dict(row, spec_json=json.dumps(bound)), jobs.change(store.db, 'fixture', test_jobs.NOW))
            row = jobs.get(store.db, 'runs', identity)
        original = archive.job
        def collect(*args, **kwargs):
            self.assertFalse((self.data / 'runs/test' / identity / 'manifest.json').exists())
            return original(*args, **kwargs, home=home)
        with patch.dict(os.environ, PATH=str(bindir) + ':' + os.environ['PATH']), patch.object(archive, 'job', side_effect=collect):
            result = runtime.execute(self.root, identity, self.data, clock=lambda: test_jobs.NOW)
        self.assertEqual(result['exit_code'], 124)
        self.assertEqual(result['archive'], 'complete')
        self.assertTrue((bindir / 'stopped').exists())
        directory = self.data / 'runs/test' / identity
        meta = json.loads((directory / 'transcript-meta.json').read_text())
        self.assertEqual(len(meta['sessions']), 1)
        self.assertEqual((directory / 'sessions' / meta['sessions'][0]['file']).read_bytes(), native.read_bytes())
        manifest = json.loads((directory / 'manifest.json').read_text())
        self.assertGreater(runtime.learned_cost(manifest, directory)['cost_tokens'], 0)
        self.assertTrue(summary.write(self.data, directory, meta, row, [], test_jobs.NOW))
        self.assertGreater(json.loads((directory / 'summary.json').read_text())['tokens']['weighted'], 0)

    def test_no_ready_by_promise_before_measurement(self):
        from steward import intent, planner
        self.add(needs={'ready_by': ['18:30']})
        with writer(self.root) as store:
            state = jobs.snapshot(store.db, now=test_jobs.NOW)['jobs'][0]
            self.assertEqual(state['measurement'], 'measuring')
            choice = planner.choices(store.db, test_jobs.NOW)[0]
            self.assertEqual(choice['measurement'], 'measuring')
            self.assertIsNone(choice['missed_at'])
            with patch('steward.intent.ticks', return_value={}):
                doc = intent.steward_doc(store.db, test_jobs.NOW, {}, self.data)
            day = next(b for b in doc['blocks'] if b['kind'] == 'day')
            promise = next(p for p in day['promises'] if p['label'] == 'test')
            self.assertEqual(promise['state'], 'quiet')
            self.assertIn('measuring', promise['why'])


class ShellPathTests(unittest.TestCase):
    def test_shell_bin_only_contains_declared_calls_and_basics(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            bindir = capabilities.shell_bin(directory, ['date'])
            result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', 'python3 -V'],
                                    env=dict(os.environ, PATH=str(bindir)), capture_output=True)
            self.assertEqual(result.returncode, 127)
            bindir = capabilities.shell_bin(directory / 'declared', ['python3'])
            result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', 'python3 -V'],
                                    env=dict(os.environ, PATH=str(bindir)), capture_output=True)
            self.assertEqual(result.returncode, 0)

    def test_declared_env_python_tool_runs_from_restricted_shell(self):
        # gsd, exa-search, tty and pocketcasts start with "#!/usr/bin/env python3".
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / 'tools'
            tools.mkdir()
            tool = tools / 'envtool'
            tool.write_text('#!/usr/bin/env python3\nprint("ok")\n')
            tool.chmod(0o755)
            with patch.dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH']):
                bindir = capabilities.shell_bin(Path(tmp) / 'run', ['envtool'])
            result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', 'envtool; python3 -V'],
                                    env=dict(os.environ, PATH=str(bindir)), capture_output=True, text=True)
            self.assertEqual(result.stdout.splitlines()[0], 'ok')
            self.assertEqual(result.returncode, 127)  # the model's own shell still has no python3

    def test_harness_path_is_unchanged(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            original = os.environ['PATH']
            args, env = capabilities.shell_scope('sol', directory, ['date'])
            self.assertEqual(os.environ['PATH'], original)
            self.assertNotIn('PATH', env)
            self.assertIn('shell_environment_policy.set.PATH=', ' '.join(args))
            self.assertIn('shell_environment_policy.inherit=all', args)
            args, env = capabilities.shell_scope('haiku', directory / 'haiku', ['date'])
            self.assertNotIn('PATH', env)
            self.assertIn('CLAUDE_ENV_FILE', env)
            self.assertIn('export PATH=', Path(env['CLAUDE_ENV_FILE']).read_text())
