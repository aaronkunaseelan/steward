import errno
import json
import subprocess
import unittest
from urllib.error import HTTPError, URLError

from steward.errors import safe_reason


class FailureReasonTests(unittest.TestCase):
    def test_structural_facts_survive_without_private_inputs(self):
        secret = 'planted-secret'
        cases = [
            (ValueError(secret), 'ValueError'),
            (OSError(errno.EACCES, secret, secret), 'EACCES'),
            (subprocess.CalledProcessError(7, [secret], secret, secret), 'exit 7'),
            (subprocess.TimeoutExpired([secret], 30, secret, secret), 'command timed out'),
            (HTTPError('https://example.test/?key=' + secret, 503, secret,
                       {'Content-Type': 'text/html; token=' + secret}, None), 'HTTP 503 text/html'),
            (HTTPError(secret, 500, secret, {'Content-Type': secret}, None), 'unknown content type'),
            (URLError(secret), 'network request failed'),
            (json.JSONDecodeError(secret, secret, 0), 'non-JSON output (14 bytes)'),
            (json.JSONDecodeError(secret, ' \n', 0), 'empty output'),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                reason = safe_reason(error)
                self.assertIn(expected, reason)
                self.assertNotIn(secret, reason)
                self.assertLessEqual(len(reason), 200)
