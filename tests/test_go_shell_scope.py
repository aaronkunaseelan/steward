import json
import os
import subprocess
import tempfile
from pathlib import Path
import unittest

from steward import capabilities as c


def bash(prefix, command, path):
    # What pi's bash tool does with shellCommandPrefix: bash -c "<prefix>\n<command>".
    return subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', prefix + '\n' + command],
                          env=dict(os.environ, PATH=path), capture_output=True, text=True)


class GoShellScopeTests(unittest.TestCase):
    def test_go_shell_scope_limits_path(self):
        original = os.environ['PATH']
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            args, env = c.shell_scope('go', run, ['date'])
            bindir = run / 'bin'
            # The Claude-shaped return is kept (pi ignores this variable, but the caller
            # and the harness stay one shape); the go scoping lives in the run's project settings.
            self.assertEqual(args, [])
            self.assertEqual(env, {'CLAUDE_ENV_FILE': str(run / 'shell-env.sh')})
            self.assertEqual((run / 'shell-env.sh').read_text(), f'export PATH={bindir}\n')
            self.assertEqual((run / '.pi' / 'settings.json').read_text(),
                             '{"shellCommandPrefix": "export PATH=' + str(bindir) + '"}\n')
            # tty run ... packet -- --approve: pi trusts this run's directory for one process.
            argv = ['tty', 'run', *c.AGENTS['go'], '--no-accept', '-C', str(run), str(run / 'packet.md'),
                    *c.VENDOR_ARGS['go'], *args]
            self.assertEqual(argv[argv.index('--') + 1:], ['--approve'])
            self.assertNotIn('claude', ' '.join(argv))
            # The mechanism, run for real: pi's own PATH is untouched, the command's is the bin dir.
            self.assertEqual(os.environ['PATH'], original)
            prefix = json.loads((run / '.pi' / 'settings.json').read_text())['shellCommandPrefix']
            self.assertNotEqual(bash(prefix, 'date', original).returncode, 127)
            python3 = bash(prefix, 'python3 -V', original)
            self.assertEqual(python3.returncode, 127)
            self.assertIn(str(bindir), prefix)

    def test_claude_shell_scope_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            for model in ('haiku', 'opus'):
                with self.subTest(model=model):
                    directory = Path(tmp) / model
                    args, env = c.shell_scope(model, directory, ['date'])
                    self.assertEqual(args, [])
                    self.assertEqual(env, {'CLAUDE_ENV_FILE': str(directory / 'shell-env.sh'),
                                           'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'})
                    self.assertEqual((directory / 'shell-env.sh').read_text(),
                                     f'export PATH={directory / "bin"}\n')
                    self.assertFalse((directory / '.pi').exists())


if __name__ == '__main__':
    unittest.main()
