"""Terminal harness failures must reach jobs and wakes unchanged."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

from fixture_mind import install
from steward import runtime
from steward.state import StateError


class HarnessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        install(self, self.root)

    def test_background_terminal_failure_is_not_success(self):
        def run(argv, **kw):
            if argv[:2] == ['tty', 'wait']:
                result = subprocess.CompletedProcess(argv, 1, '', '')
                if kw.get('check'):
                    raise subprocess.CalledProcessError(1, argv)
                return result
            return subprocess.CompletedProcess(argv, 0, json.dumps({'run': 'fixture-failed'}), '')
        with patch.object(runtime.subprocess, 'run', side_effect=run):
            with self.assertRaises((StateError, subprocess.CalledProcessError),
                                   msg='tty wait exit 1 was returned as a successful harness identity'):
                runtime.harness(['tty', 'run', '--no-accept'],
                                links_path=self.root / 'links.json')

    def test_foreground_terminal_failure_is_not_success(self):
        process = Mock(returncode=1)
        process.communicate.return_value = (json.dumps(dict(
            run='fixture-failed', status='failed', exit=1, reason='synthetic agent failure')), '')
        with patch.object(runtime.subprocess, 'Popen', return_value=process):
            with self.assertRaises((StateError, subprocess.CalledProcessError),
                                   msg='failed foreground tty result was returned as success'):
                runtime.harness(['tty', 'run', '--no-accept'])

    def test_failure_status_and_unrequested_unproven_are_rejected(self):
        for code, status, flags in ((0, 'failed', []), (3, 'failed', ['--no-accept']),
                                    (3, 'unproven', [])):
            with self.subTest(code=code, status=status, flags=flags):
                process = Mock(returncode=code)
                process.communicate.return_value = (json.dumps(dict(run='fixture', status=status, exit=code)), '')
                with patch.object(runtime.subprocess, 'Popen', return_value=process):
                    with self.assertRaises(StateError) as raised:
                        runtime.harness(['tty', 'run', *flags])
                    self.assertEqual(raised.exception.run_id, 'fixture')

    def test_no_accept_completed_unproven_exit_three_remains_usable(self):
        terminal = json.dumps(dict(run='fixture-unproven', status='unproven', exit=3))
        process = Mock(returncode=3)
        process.communicate.return_value = (terminal, '')
        with patch.object(runtime.subprocess, 'Popen', return_value=process):
            self.assertEqual(runtime.harness(['tty', 'run', '--no-accept']), 'fixture-unproven')

        def run(argv, **kw):
            if argv[:2] == ['tty', 'wait']:
                if kw.get('check'):
                    raise subprocess.CalledProcessError(3, argv)
                return subprocess.CompletedProcess(argv, 3, terminal, '')
            return subprocess.CompletedProcess(argv, 0, json.dumps({'run': 'fixture-unproven'}), '')
        with patch.object(runtime.subprocess, 'run', side_effect=run):
            self.assertEqual(runtime.harness(['tty', 'run', '--no-accept'],
                                            links_path=self.root / 'links.json'), 'fixture-unproven')

