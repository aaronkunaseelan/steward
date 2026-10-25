import unittest
from unittest.mock import patch

from fixture_mind import use_settings
from steward import senses


class SealedIOTests(unittest.TestCase):
    setUp = use_settings

    def test_unpatched_external_cli_fails_loudly_before_execution(self):
        for argv in (['qmd', 'status', '--json'], ['gsd', 'closures', '--json'],
                     ['tty', 'show', 'fixture'], ['journalctl', '--user'],
                     ['garmin', 'today', '--json'], ['phone', 'where', '--json']):
            with self.subTest(command=argv[0]):
                with self.assertRaisesRegex(AssertionError, 'live external CLI forbidden; inject a fake: ' + argv[0]):
                    senses.run(argv)

    def test_explicit_fake_reaches_the_real_command_adapter(self):
        import subprocess
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'synthetic')) as runner:
            self.assertEqual(senses.run(['qmd', 'status', '--json']), 'synthetic')
        runner.assert_called_once_with(['qmd', 'status', '--json'],
                                       capture_output=True, text=True, timeout=30)
