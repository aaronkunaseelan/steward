"""External tools record through the CLI; fixtures never touch live state."""
from contextlib import redirect_stdout
import io
import json
import os
import unittest
from unittest.mock import patch

import test_messages
from steward import jobs, messages
from steward.cli import main
from steward.state import StateError, writer


class ExternalChangeTests(unittest.TestCase):
    setUp = test_messages.MessagesTests.setUp
    send = test_messages.MessagesTests.send

    def invoke(self, subject='fixture tool', outcome='verified after update'):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['external-change', '--state', str(self.root), '--subject', subject, '--outcome', outcome])
        return code, json.loads(output.getvalue())

    def test_cli_records_actor_subject_outcome_without_changes_noise(self):
        with patch.dict(os.environ, STEWARD_ACTOR='maintenance'):
            code, report = self.invoke()
        self.assertEqual(code, 0)
        self.assertFalse(report['undoable'])
        with writer(self.root) as store:
            row = store.db.execute('SELECT * FROM changes WHERE id=?', (report['change_id'],)).fetchone()
            self.assertEqual(row['actor'], 'maintenance')
            self.assertEqual(row['verb'], 'external-change')
            self.assertEqual(row['summary'], 'fixture tool: verified after update')
            with self.assertRaisesRegex(StateError, 'no reversible versions'):
                jobs.undo(store.db, report['change_id'])
            messages.changes(store.db, test_messages.NOW, self.send, {})
            messages.changes(store.db, test_messages.NOW, self.send, {})
        self.assertEqual(self.sent, [])

    def test_invalid_external_record_is_rejected_without_a_change(self):
        for subject, outcome in [('', 'ok'), ('tool', ''), ('tool\nother', 'ok'), ('tool', 'ok\rsecond'), ('tool', 'bad\x00value')]:
            with self.subTest(subject=subject, outcome=outcome):
                code, _ = self.invoke(subject, outcome)
                self.assertEqual(code, 2)
        with writer(self.root) as store:
            self.assertEqual(store.db.execute("SELECT count(*) FROM changes WHERE verb='external-change'").fetchone()[0], 0)
