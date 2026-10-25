from fixture_mind import use_settings
from contextlib import redirect_stdout
import io
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from steward import runtime
from steward.cli import main
from steward.state import StateError


class SettingsTests(unittest.TestCase):
    setUp = use_settings

    def test_missing_unknown_and_wrong_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            with self.assertRaisesRegex(StateError, 'missing settings file'):
                runtime.config(path)
            for text, reason in [('bogus = "UTC"', 'unknown setting: bogus'),
                                 ('load_max = true', 'invalid setting: load_max'),
                                 ('wake_times = [1]', 'invalid setting: wake_times')]:
                path.write_text(text)
                with self.subTest(text=text), self.assertRaisesRegex(StateError, reason):
                    runtime.config(path)

    def test_host_settings_are_strict(self):
        for key in ('timezone', 'gmail_account', 'pages_host', 'backup_remote'):
            for value in ('', ' ', False, 123, []):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(StateError, key):
                    runtime.validate_settings({key: value})
        for key, value in [('timezone', 'Invalid/Zone'), ('pages_host', 'http://example.test'),
                           ('pages_host', 'https://example.test/path'),
                           ('pages_host', 'https://user:password@example.test'),
                           ('pages_host', 'https://example.test:bad')]:
            with self.subTest(value=value), self.assertRaisesRegex(StateError, key):
                runtime.validate_settings({key: value})

    def test_missing_host_values_name_the_key_without_effects(self):
        from steward import drafts, settings
        with patch.object(settings, '_active') as active:
            active.get.return_value = {}
            for key, call in [('timezone', settings.local_zone), ('pages_host', settings.pages_host),
                              ('backup_remote', lambda: settings.required('backup_remote')),
                              ('gmail_account', lambda: drafts.create(None, {}, 'body', to=['a@example.test'],
                                  subject='s', runner=lambda *a: self.fail('provider effect')))]:
                with self.subTest(key=key), self.assertRaisesRegex(StateError, 'missing setting: ' + key):
                    call()
            with patch('steward.runtime.data_root', side_effect=AssertionError('effect')):
                with self.assertRaisesRegex(StateError, 'missing setting: timezone'):
                    runtime.tick(Path('/unused'), settings={})

    def test_loaded_timezone_controls_owner_calendar_and_page_clocks(self):
        from datetime import datetime, timezone
        from steward import settings, timing, calendar_b, intent
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            path.write_text('timezone = "Etc/UTC"\npages_host = "https://other.example.test"\n')
            runtime.config(path)
            now = datetime(2026, 10, 24, 23, tzinfo=timezone.utc)
            self.assertEqual(timing.owner_time(now, '%H:%M'), '23:00 UTC')
            self.assertEqual(intent.hm(now), '23:00')
            self.assertEqual(calendar_b.event_span({'start': {'date': '2026-10-24'},
                'end': {'date': '2026-10-25'}})[0].isoformat(), '2026-10-24T00:00:00+00:00')
            self.assertEqual(settings.pages_host(), 'https://other.example.test')

    def test_exception_reason_is_bounded_and_private(self):
        errors = [
            (ValueError('invalid input\nprivate file contents'), 'ValueError: invalid input'),
            (ValueError('x' * 400), 'ValueError: ' + 'x' * 200),
            (ValueError('token=private-token'), 'ValueError: token="[REDACTED]"'),
            (FileNotFoundError(2, 'No such file or directory', 'private-path'),
             'FileNotFoundError: No such file or directory'),
            (UnicodeDecodeError('utf-8', b'private-file-contents', 0, 1, 'bad byte'),
             'UnicodeDecodeError: invalid text encoding'),
            (KeyError('private-key'), 'KeyError: missing required key'),
            (subprocess.CalledProcessError(1, ['provider', 'private-env'],
                                           output='private output', stderr='private stderr'),
             'CalledProcessError: external command failed'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for exc, expected in errors:
                with self.subTest(exc=type(exc).__name__), patch('steward.runtime.config', side_effect=exc):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = main(['init-state', '--state', tmp])
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(output.getvalue()),
                                     {'status': 'rejected', 'error': expected})

    def test_tick_settings_precede_every_effect(self):
        with patch('steward.runtime.data_root', side_effect=AssertionError('effect')):
            with self.assertRaisesRegex(StateError, 'unknown setting: bogus'):
                runtime.tick(Path('/unused'), settings={'bogus': 1})
