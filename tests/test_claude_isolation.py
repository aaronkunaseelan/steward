"""Steward's Claude launches take gsd's isolation flags (owner ruling X-66 item 5, F-87)."""
import io
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

from fixture_mind import install
from test_jobs import NOW, spec
from steward import capabilities as c, jobs, runtime
from steward.state import writer


def claude_argv(argv):
    """The part tty passes to the vendor: everything after the one '--'."""
    return argv[argv.index('--') + 1:]


class ClaudeJobIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        install(self, self.root)
        (self.root / 'mind/capabilities/sample.md').write_text(
            '---\nname: "sample"\nreads: []\ncalls: []\nrecord: []\n---\nDo the task.')

    def launch(self, model):
        # The argv and env agent-run builds for one started run; tty never runs.
        with writer(self.root / 'state', initialize=True) as store:
            jobs.job_verb(store.db, 'add', model, spec(capability='sample', model=model))
            jobs.append(store.db, 'runs', dict(run_id=model, job=model, state='started',
                planned_for=NOW.isoformat(), started_at=NOW.isoformat(), reason='fixture',
                spec_json=jobs.get(store.db, 'jobs', model)['spec_json']), jobs.change(store.db, 'fixture', NOW))
            store.db.commit()
        directory = self.root / model
        with patch.dict(os.environ, STEWARD_RUN_DIR=str(directory),
                        STEWARD_STATE=str(self.root / 'state'), STEWARD_JOB=model, STEWARD_RUN_ID=model), \
             patch.object(sys, 'argv', ['agent-run', 'sample']), \
             patch.object(runtime, 'harness', return_value='fixture-run') as harness, \
             redirect_stdout(io.StringIO()):
            runpy.run_path(str(Path('jobs/agent-run').resolve()), run_name='__main__')
        return harness.call_args.args[0], harness.call_args.kwargs['env']

    def test_claude_job_argv_isolated(self):
        for model, effort in (('haiku', 'high'), ('opus', 'medium')):
            with self.subTest(model=model):
                argv, env = self.launch(model)
                vendor = claude_argv(argv)
                self.assertEqual(vendor[vendor.index('--setting-sources') + 1], '')
                self.assertEqual(vendor[vendor.index('--mcp-config') + 1], '{"mcpServers":{}}')
                self.assertEqual(vendor[vendor.index('--tools') + 1], 'Bash,Read,Write,Edit,WebSearch,WebFetch')
                for flag in ('--strict-mcp-config', '--disable-slash-commands'):
                    self.assertEqual(vendor.count(flag), 1)
                # The existing flags stay first, so nothing about effort changed.
                self.assertEqual(vendor[:2], ['--permission-mode', 'bypassPermissions'])
                self.assertEqual(vendor[vendor.index('--effort') + 1], effort)
                self.assertEqual(claude_argv(argv), vendor)
                self.assertEqual(argv.count('--'), 1)
                self.assertEqual(env['CLAUDE_CODE_DISABLE_AUTO_MEMORY'], '1')
                self.assertEqual(env['CLAUDE_ENV_FILE'], str(self.root / model / 'shell-env.sh'))

    def test_go_argv_unchanged(self):
        argv, env = self.launch('go')
        self.assertEqual(claude_argv(argv), ['--approve'])
        joined = ' '.join(argv)
        for flag in ('--setting-sources', '--strict-mcp-config', '--mcp-config',
                     '--disable-slash-commands', '--tools'):
            self.assertNotIn(flag, joined)
        self.assertNotIn('CLAUDE_CODE_DISABLE_AUTO_MEMORY', env)
        self.assertEqual(env, {'CLAUDE_ENV_FILE': str(self.root / 'go' / 'shell-env.sh')})


class ClaudeWakeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        install(self, self.root)
        self.state = self.root / 'state'
        with writer(self.state, initialize=True):
            pass
        self.sent = []

    def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return len(self.sent)

    def test_wake_argv_isolated(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 21, 4, tzinfo=timezone.utc)
        # The real harness identity, so the wake hands it the isolation env.
        with patch.object(runtime, 'harness', return_value='fixture-harness') as harness:
            report = runtime.wake(self.state, clock=lambda: now, settings={'wake_model': True},
                                  runner=runtime.harness, prompt='fixture', sender=self.send)
        self.assertEqual(report['model_calls'], 1)
        argv = harness.call_args.args[0]
        vendor = claude_argv(argv)
        self.assertEqual(vendor[vendor.index('--setting-sources') + 1], '')
        self.assertEqual(vendor[vendor.index('--mcp-config') + 1], '{"mcpServers":{}}')
        self.assertEqual(vendor[vendor.index('--tools') + 1], 'Bash,Read,Write,Edit,WebSearch,WebFetch')
        for flag in ('--strict-mcp-config', '--disable-slash-commands'):
            self.assertEqual(vendor.count(flag), 1)
        # The wake's own flags stay after the shared ones, once each.
        self.assertEqual(vendor.count('--exclude-dynamic-system-prompt-sections'), 1)
        self.assertEqual(vendor.count('--append-system-prompt'), 1)
        self.assertLess(vendor.index('--mcp-config'), vendor.index('--append-system-prompt'))
        self.assertLess(vendor.index('--append-system-prompt'), len(vendor) - 1)
        self.assertEqual(argv.count('--'), 1)
        self.assertEqual(harness.call_args.kwargs['env'], c.CLAUDE_ENV)

    def test_auto_memory_disabled_for_claude(self):
        # One definition, read by both launches and by nothing else.
        self.assertEqual(c.CLAUDE_ENV, {'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'})
        self.assertEqual(c.VENDOR_ARGS['haiku'][-len(c.CLAUDE_ISOLATION):], c.CLAUDE_ISOLATION)
        self.assertEqual(c.VENDOR_ARGS['opus'][-len(c.CLAUDE_ISOLATION):], c.CLAUDE_ISOLATION)
        self.assertEqual(c.VENDOR_ARGS['go'], ['--', '--approve'])
        self.assertEqual(c.shell_scope('haiku', self.root / 'h', [])[1]['CLAUDE_CODE_DISABLE_AUTO_MEMORY'], '1')
        self.assertEqual(c.shell_scope('opus', self.root / 'o', [])[1]['CLAUDE_CODE_DISABLE_AUTO_MEMORY'], '1')
        self.assertNotIn('CLAUDE_CODE_DISABLE_AUTO_MEMORY', c.shell_scope('go', self.root / 'g', [])[1])


if __name__ == '__main__':
    unittest.main()
