from fixture_mind import use_settings
"""Regression cases for review findings S6, S7 and S9."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from steward import calendar_b as b, cli
from steward.state import writer, StateError
from test_calendar_b import NOW, SETTINGS, SPEC


class UrgentFixTests(unittest.TestCase):
    setUp = use_settings

    def invoke(self, root, *args):
        output = io.StringIO()
        with redirect_stdout(output), patch('steward.runtime.config', return_value={}):
            code = cli.main([*args, '--state', str(root)])
        return code, json.loads(output.getvalue())

    def test_init_reports_the_created_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, result = self.invoke(root, 'init-state')
            with writer(root) as store:
                schema = store.db.execute('PRAGMA user_version').fetchone()[0]
            self.assertEqual(code, 0)
            self.assertEqual(result, {'status': 'initialized', 'schema': schema})

    def test_missing_spec_names_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.invoke(root, 'init-state')
            code, result = self.invoke(root, 'block', 'demand', 'add', 'test',
                                       '--spec', str(root / 'missing.json'))
            self.assertEqual(code, 2)
            self.assertEqual(result['status'], 'rejected')
            self.assertTrue(result['error'].startswith('FileNotFoundError: '), result)

    def test_malformed_spec_names_json_error_without_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.invoke(root, 'init-state')
            spec = root / 'bad.json'
            spec.write_text('private-file-contents')
            code, result = self.invoke(root, 'block', 'demand', 'add', 'test',
                                       '--spec', str(spec))
            self.assertEqual(code, 2)
            self.assertTrue(result['error'].startswith('JSONDecodeError: '), result)
            self.assertNotIn('private-file-contents', result['error'])

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
                        code = cli.main(['init-state', '--state', tmp])
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(output.getvalue()),
                                     {'status': 'rejected', 'error': expected})

    def test_demand_readd_undo_preserves_versions_and_owner_removed(self):
        with tempfile.TemporaryDirectory() as tmp, writer(Path(tmp), initialize=True) as store:
            db = store.db
            b.demand(db, 'add', 'gym', SPEC, NOW)
            with self.assertRaises(StateError):
                b.demand(db, 'add', 'gym', SPEC, NOW)
            cid = b.change(db, 'fixture owner deletion', NOW, 'owner')
            b.append(db, 'blocks', dict(event_key='gym:2026-10-19:0', version=0,
                     demand='gym', state='removed', payload='{}'), cid)
            removed = [dict(r) for r in db.execute('SELECT * FROM blocks')]
            b.demand(db, 'rm', 'gym', now=NOW)
            readded = b.demand(db, 'add', 'gym', dict(SPEC, minutes=90), NOW)
            self.assertEqual(b.latest(db, 'demands')[0]['state'], 'active')
            b.undo(db, SETTINGS, readded['change_id'], NOW)
            rows = [dict(r) for r in db.execute('SELECT * FROM demands ORDER BY version')]
            self.assertEqual([r['version'] for r in rows], [1, 2, 3, 4])
            self.assertEqual([r['state'] for r in rows], ['active', 'removed', 'active', 'removed'])
            self.assertEqual(rows[-1]['spec_json'], rows[1]['spec_json'])
            self.assertEqual([dict(r) for r in db.execute('SELECT * FROM blocks')], removed)

    def test_wake_prompt_does_not_redefine_gym(self):
        prompt = (Path(__file__).resolve().parents[1] / 'prompts/wake.md').read_text()
        self.assertNotIn('12:30–18:00', prompt)
        self.assertNotIn('90-minute', prompt)
        self.assertNotIn('weak evidence about timing', prompt)  # the charter holds the rule, once
        self.assertIn('planning.md before scheduling', prompt)  # pull judgement; do not duplicate it
