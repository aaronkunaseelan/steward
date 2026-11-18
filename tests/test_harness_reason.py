"""A failed harness run names its cause from tty's record, never its free text."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from steward import runtime
from steward.state import StateError

FAKE_TTY = '''#!/usr/bin/env python3
import json, os, sys
run = 'fixture-failed'
verb = sys.argv[1] if len(sys.argv) > 1 else ''
if verb == 'run':
    if '--bg' in sys.argv:
        print(json.dumps(dict(run=run)))
        sys.exit(0)
    print(json.dumps(dict(run=run, status='failed', exit=1)))
    sys.exit(1)
if verb == 'wait':
    print(json.dumps(dict(run=run, status='failed', exit=1)))
    sys.exit(1)
if verb == 'show':
    print(open(os.environ['FAKE_TTY_SHOW']).read())
    sys.exit(0)
sys.exit(2)
'''


class HarnessReasonTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        binary = self.root / 'bin'
        binary.mkdir()
        tty = binary / 'tty'
        tty.write_text(FAKE_TTY)
        tty.chmod(0o755)
        self.show = self.root / 'show.json'
        patched = patch.dict(os.environ, dict(PATH=str(binary) + os.pathsep + os.environ['PATH'],
                                              FAKE_TTY_SHOW=str(self.show)))
        patched.start()
        self.addCleanup(patched.stop)

    def recorded(self, **detail):
        self.show.write_text(json.dumps(dict(run='fixture-failed', status='failed', exit=1, **detail)))

    def reason(self):
        with self.assertRaises(StateError) as raised:
            runtime.harness(['tty', 'run', '--no-accept'])
        return str(raised.exception)

    def test_harness_rate_limit_named_in_reason(self):
        self.recorded(reason='api error',
                      final_text='Claude AI usage limit reached, resets at 2026-11-07T21:00:00Z rate_limit')
        reason = self.reason()
        self.assertTrue(reason.startswith('harness execution failed: exit 1: usage or rate limit (429)'), reason)
        self.assertIn('resets 2026-11-07T21:00:00Z', reason)
        self.assertNotIn('Claude', reason)

    def test_harness_unknown_failure_says_unknown(self):
        self.recorded(reason='model stopped without a terminal event')
        self.assertEqual(self.reason(), 'harness execution failed: exit 1: cause unknown')
        self.show.unlink()
        self.assertEqual(self.reason(), 'harness execution failed: exit 1: cause unknown (tty show failed)')

    def test_harness_authentication_failure_is_named(self):
        self.recorded(reason='401 unauthorized from the provider')
        self.assertEqual(self.reason(), 'harness execution failed: exit 1: authentication failed')

    def test_harness_model_words_are_not_read_as_a_cause(self):
        self.recorded(final_text='the authors said 2401.5')
        self.assertEqual(self.reason(), 'harness execution failed: exit 1: cause unknown')

    def test_harness_401_in_the_record_is_named(self):
        self.recorded(reason='HTTP 401')
        self.assertEqual(self.reason(), 'harness execution failed: exit 1: authentication failed')

    def test_harness_reset_time_is_a_clock_shape(self):
        self.recorded(final_text="You've hit your usage limit · resets 3pm (Europe/Athens)")
        self.assertEqual(self.reason(),
                         'harness execution failed: exit 1: usage or rate limit (429), resets 3pm (Europe/Athens)')

    def test_harness_reset_tail_never_copies_the_models_text(self):
        self.recorded(final_text='usage limit, resets in ignore previous instructions')
        self.assertEqual(self.reason(), 'harness execution failed: exit 1: usage or rate limit (429)')


class HarnessCauseShapeTests(unittest.TestCase):
    """Provider error shapes keep their cause whatever their case, and '_' separates words (D-46 review)."""

    def cause(self, **detail):
        shown = type('Shown', (), dict(stdout=json.dumps(dict(run='r', status='failed', **detail))))()
        with patch.object(runtime.subprocess, 'run', return_value=shown):
            return runtime.harness_failure('r')

    def test_provider_shapes_are_named_in_any_case(self):
        for text in ('Rate limit exceeded', 'rate_limit_error', 'Overloaded', 'overloaded_error',
                     'HTTP 429 Too Many Requests', "You've hit your Usage Limit"):
            with self.subTest(text=text):
                self.assertEqual(self.cause(final_text=text), 'usage or rate limit (429)')
        for text in ('Unauthorized', 'authentication_error', 'Auth failed', 'HTTP 401'):
            with self.subTest(text=text):
                self.assertEqual(self.cause(reason=text), 'authentication failed')
        self.assertEqual(self.cause(reason='Timed out after 38 min'), 'timed out')

    def test_lookalikes_are_not_causes(self):
        for detail in (dict(reason='the authors met'), dict(reason='job 2401.5'), dict(final_text='overloadedness 14290'),
                       dict(final_text='authentication failed in the essay')):
            with self.subTest(detail=detail):
                self.assertEqual(self.cause(**detail), 'cause unknown')
