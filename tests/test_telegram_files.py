from fixture_mind import use_settings
import json
import signal
import time
import unittest
from unittest.mock import patch

from steward import telegram_files as files


class TelegramFilesTests(unittest.TestCase):
    setUp = use_settings

    def test_download_uses_get_file_then_bytes(self):
        responses = [(json.dumps(dict(ok=True, result=dict(file_path='voice/a.ogg', file_size=3))).encode(), {}),
                     (b'raw', {})]
        with patch('steward.notify.bot_token', return_value='fake'), patch.object(files, 'request', side_effect=responses) as request:
            self.assertEqual(files.bounded(dict(file_id='id')), b'raw')
            self.assertEqual(request.call_count, 2)
            self.assertTrue(request.call_args_list[0].args[0].endswith('/getFile'))
            self.assertTrue(request.call_args_list[1].args[0].endswith('/voice/a.ogg'))

    def test_provider_size_path_and_error_are_safe(self):
        for info, reason in [(dict(file_size=files.LIMIT+1), 'too_large'),
                             (dict(file_path='../bad'), 'download')]:
            with patch('steward.notify.bot_token', return_value='fake'), patch.object(files, 'request', return_value=(json.dumps(dict(ok=True, result=info)).encode(), {})):
                with self.assertRaisesRegex(files.Failed, reason):
                    files.bounded(dict(file_id='id'))
        with patch('steward.notify.bot_token', side_effect=OSError('secret')):
            with self.assertRaisesRegex(files.Failed, '^download$'):
                files.bounded(dict(file_id='id'))

    def test_actual_download_size_is_capped_and_alarm_restored(self):
        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaisesRegex(files.Failed, 'too_large'):
            files.bounded({}, lambda *a: b'x' * (files.LIMIT+1))
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_download_wall_timer_and_timeout(self):
        def expired(ref, deadline):
            signal.raise_signal(signal.SIGALRM)
        with self.assertRaisesRegex(files.Failed, 'timeout'):
            files.bounded({}, expired)
        with self.assertRaisesRegex(files.Failed, 'timeout'):
            files.remaining(time.monotonic()-1)
