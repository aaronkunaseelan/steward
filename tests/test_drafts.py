from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from steward import drafts, messages
from steward.cli import main
from steward.state import StateError, writer
import test_jobs
from test_jobs import NOW


class DraftTests(unittest.TestCase):
    setUp = test_jobs.JobsTests.setUp

    def create(self, **kwargs):
        self.argv = []
        self.sent = []
        def runner(argv, body=None):
            self.argv.append((argv, body))
            return {'draftId': 'r-123', 'message': {'id': 'abc', 'threadId': 'thread-456'},
                    'url': 'https://mail.google.com/mail/u/0/#drafts/r-123'}
        def sender(topic, text, **options):
            self.sent.append((topic, text))
            return 42
        with writer(self.root) as store:
            return drafts.create(store.db, {'gmail_account': 'owner@example.test'}, 'private email body', runner=runner,
                                 sender=sender, now=NOW, **kwargs)

    def test_reply_argv_change_and_notification(self):
        result = self.create(reply_to='gmail:owner@example.test:abc123', reply_all=True,
                             actor='model')
        argv, body = self.argv[0]
        self.assertEqual(argv[:7], ['gog', '--enable-commands-exact=gmail.drafts.create',
                         '--gmail-no-send', '-j', 'gmail', 'drafts', 'create'])
        self.assertEqual(argv[argv.index('--reply-to-message-id') + 1], 'abc123')
        self.assertIn('--auto-from-addressed-alias', argv)
        self.assertIn('--reply-all', argv)
        self.assertNotIn('--subject', argv)
        self.assertEqual(argv[-2:], ['--body-file', '-'])
        self.assertEqual(body, 'private email body')
        self.assertNotIn(body, ' '.join(argv))
        self.assertEqual(self.sent[0][0], 'Changes')
        self.assertIn('Open draft', self.sent[0][1])
        self.assertIn(result['url'], self.sent[0][1])
        self.assertNotIn(body, self.sent[0][1])
        with writer(self.root) as store:
            row = store.db.execute('SELECT * FROM changes WHERE id=?', (result['change_id'],)).fetchone()
            self.assertEqual(row['actor'], 'model')
            self.assertEqual(row['verb'], 'draft')
            self.assertNotIn(body, row['summary'])
            before = store.db.execute('SELECT count(*) FROM outbox').fetchone()[0]
            messages.changes(store.db, NOW, lambda *a, **k: self.fail('duplicate send'), {})
            self.assertEqual(store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], before)

    def test_new_draft_and_reply_subject(self):
        self.create(to=['a@example.com', 'b@example.com'], subject='Subject')
        argv = self.argv[0][0]
        self.assertEqual(argv[argv.index('--to') + 1], 'a@example.com,b@example.com')
        self.assertEqual(argv[argv.index('--subject') + 1], 'Subject')
        self.assertNotIn('--reply-to-message-id', argv)
        self.assertNotIn('--auto-from-addressed-alias', argv)
        self.create(reply_to='abc', subject='Override')
        self.assertIn('Override', self.argv[0][0])

    def test_undo_records_delete_and_never_send(self):
        result = self.create(to=['a@example.com'], subject='subject')
        calls = []
        with writer(self.root) as store:
            undo = drafts.undo(store.db, {}, result['change_id'], actor='model',
                               runner=lambda argv: calls.append(argv), now=NOW)
            self.assertEqual(calls[0][:7], ['gog', '--enable-commands-exact=gmail.drafts.delete',
                             '--gmail-no-send', '-j', 'gmail', 'drafts', 'delete'])
            self.assertIn('r-123', calls[0])
            self.assertIn('--force', calls[0])
            row = store.db.execute('SELECT * FROM changes WHERE id=?', (undo['change_id'],)).fetchone()
            self.assertEqual(row['undo_of'], result['change_id'])
            self.assertEqual(row['actor'], 'model')
            self.assertEqual(store.db.execute('SELECT state FROM drafts').fetchone()[0], 'deleted')
            with self.assertRaises(StateError):
                drafts.undo(store.db, {}, result['change_id'], runner=lambda a: self.fail('repeat delete'))
        for argv in [self.argv[0][0], *calls]:
            self.assertNotIn('send', argv)
            self.assertNotIn('gmail.send', ' '.join(argv))
        for action in ('send', 'update', 'gmail.send', 'drafts.send'):
            with self.assertRaises(StateError):
                drafts.command(action, 'a@example.com')

    def test_qmd_id_and_inputs_rejected_without_effects(self):
        invalid = [dict(reply_to='gmail:other@example.test:abc'), dict(reply_to='abc; curl bad'),
                   dict(to=['a@example.com']), dict(to=['a@example.com'], subject='s', reply_all=True),
                   dict(to=['a@example.com'], subject='s', reply_to='abc'),
                   dict(to=['a@example.com\nBcc: b@example.com'], subject='s')]
        with writer(self.root) as store:
            for kwargs in invalid:
                with self.assertRaises(StateError):
                    drafts.create(store.db, {'gmail_account': 'owner@example.test'}, 'body', runner=lambda *a: self.fail('provider call'), **kwargs)
            self.assertEqual(store.db.execute('SELECT count(*) FROM changes WHERE actor!=\'migration\'').fetchone()[0], 0)

    def test_intent_is_committed_before_provider_and_unknown_is_not_success(self):
        def fail(argv, body):
            with sqlite3.connect(self.root / 'state.sqlite') as db:
                self.assertEqual(db.execute('SELECT state FROM drafts').fetchone()[0], 'unknown')
            raise StateError('Gmail draft outcome unknown')
        with writer(self.root) as store:
            with self.assertRaises(StateError):
                drafts.create(store.db, {'gmail_account': 'owner@example.test'}, 'body', reply_to='abc', runner=fail)
            self.assertEqual(store.db.execute('SELECT state FROM drafts').fetchone()[0], 'unknown')
            self.assertEqual(store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)

    def test_account_from_config_and_thread_url_fallback(self):
        with writer(self.root) as store:
            calls = []
            def runner(argv, body):
                calls.append(argv)
                return {'draft': {'id': 'r-123', 'message': {'threadId': 'thread-456'}}}
            result = drafts.create(store.db, {'gmail_account': 'other@example.com'}, 'body',
                                   reply_to='gmail:other@example.com:abc', runner=runner,
                                   sender=lambda *a, **k: 42, now=NOW)
            self.assertEqual(calls[0][calls[0].index('--account') + 1], 'other@example.com')
            self.assertIn('#all/thread-456', result['url'])
            self.assertIn('other%40example.com', result['url'])

    def test_cli_file_stdin_and_undo_dispatch(self):
        body = Path(self.tmp.name) / 'body.txt'
        body.write_text('file body')
        for args, expected in [(['--reply-to', 'abc', '--body-file', str(body)], 'file body'),
                               (['--to', 'a@example.com', '--subject', 's', '--body-file', '-'], 'stdin body')]:
            with patch('steward.drafts.create', return_value={'status': 'created'}) as create, \
                 patch('sys.stdin', io.StringIO('stdin body')), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['draft', '--state', str(self.root), *args]), 0)
                self.assertEqual(create.call_args.args[2], expected)
        result = self.create(reply_to='abc')
        with patch('steward.drafts.undo', return_value={'status': 'deleted'}) as undo, redirect_stdout(io.StringIO()):
            self.assertEqual(main(['undo', '--state', str(self.root), result['change_id']]), 0)
            self.assertEqual(undo.call_args.args[2], result['change_id'])

    def test_telegram_reply_undo_dispatch(self):
        result = self.create(reply_to='abc')
        update = {'update_id': 1, 'message': {'message_id': 2, 'chat': {'id': 1},
                  'from': {'id': 2}, 'text': 'undo', 'reply_to_message': {'message_id': 42}}}
        sent = []
        with writer(self.root) as store, patch('steward.drafts.undo', return_value={'change_id': 'f' * 32}) as undo:
            messages.poll(store.db, NOW, {}, sender=lambda topic, text, **kw: sent.append(text) or 43,
                          fetcher=lambda offset: [update], telegram={'chat_id': 1, 'owner_user_id': 2, 'topics': {'Talk': 6}})
            self.assertEqual(undo.call_args.args[2], result['change_id'])
            self.assertIn('Deleted Gmail draft', sent[0])
            self.assertNotIn('undo ', sent[0])

    def test_provider_errors_redacted(self):
        with patch('subprocess.run', side_effect=subprocess.CalledProcessError(1, [], stderr='secret body')):
            with self.assertRaisesRegex(StateError, '^Gmail draft outcome unknown') as error:
                drafts.run(['gog'], 'body')
        self.assertNotIn('secret', str(error.exception))

    def test_schema_11_migration_preserves_state(self):
        with writer(self.root) as store:
            store.db.execute('DROP TABLE drafts')
            store.db.execute('DROP TABLE outbox_keys')
            store.db.execute('ALTER TABLE pages DROP COLUMN public')
            store.db.execute('DROP TABLE moments'); store.db.execute("DELETE FROM changes WHERE actor=\'migration\'")
            store.db.execute('ALTER TABLE runs DROP COLUMN retry_of')
            store.db.execute('PRAGMA user_version=11')
            store.db.commit()
        with writer(self.root) as store:
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 17)
            self.assertEqual(store.db.execute('SELECT count(*) FROM drafts').fetchone()[0], 0)
