"""A run's qmd reads never include Steward's or gsd's own sessions (O-114)."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_jobs
from steward import runtime


class HarnessEnvTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.links = Path(temp.name) / 'links.json'

    def background(self, **kwargs):
        """Run the tty branch, returning the env its launch was given."""
        seen = {}

        def run(argv, **kw):
            if argv[:2] == ['tty', 'wait']:
                return subprocess.CompletedProcess(argv, 0, json.dumps({'run': 'fixture-bg', 'status': 'ok'}), '')
            seen.update(kw['env'])
            return subprocess.CompletedProcess(argv, 0, json.dumps({'run': 'fixture-bg'}), '')

        with patch.object(runtime.subprocess, 'run', side_effect=run):
            self.assertEqual(runtime.harness(['tty', 'run', '--no-accept'], links_path=self.links, **kwargs), 'fixture-bg')
        return seen

    def foreground(self, **kwargs):
        """Run the foreground branch, returning the env its launch was given."""
        seen = {}
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps({'run': 'fixture-fg', 'status': 'ok'}), '')

        def popen(argv, **kw):
            seen.update(kw['env'])
            return process

        with patch.object(runtime.subprocess, 'Popen', side_effect=popen):
            self.assertEqual(runtime.harness(['tty', 'run', '--no-accept'], **kwargs), 'fixture-fg')
        return seen

    def test_both_branches_mark_the_run_as_model_and_exclude_own_origins(self):
        for name, branch in (('tty', self.background), ('foreground', self.foreground)):
            with self.subTest(branch=name):
                env = branch()
                self.assertEqual(env['STEWARD_ACTOR'], 'model')
                self.assertEqual(env['QMD_NOT_ORIGIN'], 'steward,gsd')
                self.assertEqual(env['CLAUDE_CODE_PROMPT_CACHE_TTL'], '5m')

    def test_caller_cache_ttl_overrides_instead_of_raising(self):
        for name, branch in (('tty', self.background), ('foreground', self.foreground)):
            with self.subTest(branch=name):
                env = branch(env={'CLAUDE_CODE_PROMPT_CACHE_TTL': '1h'})
                self.assertEqual(env['CLAUDE_CODE_PROMPT_CACHE_TTL'], '1h')
                self.assertEqual(env['QMD_NOT_ORIGIN'], 'steward,gsd')


class ExecuteEnvTests(unittest.TestCase):
    setUp = test_jobs.JobsTests.setUp
    add = test_jobs.JobsTests.add
    run_now = test_jobs.JobsTests.run_now
    tick = test_jobs.JobsTests.tick

    def child_env(self):
        self.add(recurring=False)
        identity = self.run_now()
        self.tick()
        with patch('steward.runtime.subprocess.Popen') as popen, \
                patch('steward.runtime.archive_attempt', return_value='complete'):
            popen.return_value.wait.return_value = 0
            runtime.execute(self.root, identity, self.data, clock=lambda: test_jobs.NOW)
        return popen.call_args.kwargs['env']

    def test_run_env_excludes_steward_and_gsd_origins(self):
        env = self.child_env()
        self.assertEqual(env['QMD_NOT_ORIGIN'], 'steward,gsd')
        self.assertEqual(env['CLAUDE_CODE_PROMPT_CACHE_TTL'], '5m')
        self.assertEqual(env['STEWARD_ACTOR'], 'model')


if __name__ == '__main__':
    unittest.main()
