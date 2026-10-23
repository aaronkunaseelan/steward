from contextlib import redirect_stdout
import io
import os
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from steward import drafts, jobs, messages
from steward.cli import main
from steward.state import StateError, writer
import test_jobs
from test_jobs import NOW, spec


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
            undo = drafts.undo(store.db, result['change_id'], actor='model',
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
                drafts.undo(store.db, result['change_id'], runner=lambda a: self.fail('repeat delete'))
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
            self.assertEqual(undo.call_args.args[1], result['change_id'])

    def job_context(self, permits):
        meta = dict(name='sample', reads=[], calls=['steward'], record=['key'], permits=permits)
        path = Path(self.tmp.name) / 'mind/capabilities/sample.md'
        path.write_text('---\n' + '\n'.join(k + ': ' + json.dumps(v) for k, v in meta.items()) + '\n---\nFixture task')
        test_jobs.JobsTests.add(self, capability='sample')
        rid = test_jobs.JobsTests.run_now(self)
        test_jobs.JobsTests.tick(self)
        return dict(STEWARD_ACTOR='model', STEWARD_JOB='test', STEWARD_RUN_ID=rid,
                    STEWARD_RUN_DIR=str(self.data / 'runs/test' / rid))

    def draft_env(self, context):
        env = {k: v for k, v in os.environ.items()
               if k not in ('STEWARD_ACTOR', 'STEWARD_JOB', 'STEWARD_RUN_ID', 'STEWARD_RUN_DIR')}
        return patch.dict(os.environ, dict(env, **context), clear=True)

    def check_cli_draft(self, context, error=None):
        output = io.StringIO()
        with self.draft_env(context), patch('steward.drafts.create', return_value={'status': 'created'}) as create, \
             patch('sys.stdin', io.StringIO('body')), redirect_stdout(output):
            code = main(['draft', '--state', str(self.root), '--reply-to', 'abc', '--body-file', '-'])
        if error:
            self.assertEqual(code, 2, output.getvalue())
            self.assertIn(error, json.loads(output.getvalue())['error'])
            create.assert_not_called()
        else:
            self.assertEqual(code, 0, output.getvalue())
            create.assert_called_once()

    def test_cli_capability_without_draft_permit_cannot_claim_authority(self):
        context = self.job_context([])
        # Neither the actor nor a self-declared capability grants this run a permit.
        context.update(STEWARD_ACTOR='owner', STEWARD_CAPABILITY='prepare')
        self.check_cli_draft(context, 'lacks draft permit')

    def test_cli_permitted_job_reaches_draft_only_provider(self):
        context = self.job_context(['draft'])
        output = io.StringIO()
        with self.draft_env(context), patch('subprocess.run', return_value=subprocess.CompletedProcess(
                [], 0, '{"draftId":"r-123"}')) as provider, \
             patch('steward.drafts.notify'), patch('sys.stdin', io.StringIO('body')), redirect_stdout(output):
            self.assertEqual(main(['draft', '--state', str(self.root), '--reply-to', 'abc', '--body-file', '-']), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'created')
        provider.assert_called_once()
        self.assertEqual(provider.call_args.args[0][:7], ['gog', '--enable-commands-exact=gmail.drafts.create',
                         '--gmail-no-send', '-j', 'gmail', 'drafts', 'create'])
        with writer(self.root) as store:
            self.assertEqual(store.db.execute('SELECT state FROM drafts').fetchone()[0], 'created')

    def test_cli_draft_missing_unknown_or_mismatched_job_context_refuses(self):
        context = self.job_context(['draft'])
        test_jobs.JobsTests.add(self, 'other')
        for keys in (('STEWARD_RUN_ID',), ('STEWARD_JOB',), ('STEWARD_RUN_ID', 'STEWARD_JOB')):
            with self.subTest(missing=keys):
                self.check_cli_draft({k: v for k, v in context.items() if k not in keys},
                                     'requires STEWARD_RUN_ID and STEWARD_JOB')
        self.check_cli_draft(dict(context, STEWARD_RUN_ID='missing'), 'unknown runs')
        self.check_cli_draft(dict(context, STEWARD_JOB='missing'), 'unknown jobs')
        self.check_cli_draft(dict(context, STEWARD_JOB='other'), 'invalid draft job context')
        with writer(self.root) as store:
            row = jobs.get(store.db, 'runs', context['STEWARD_RUN_ID'])
            jobs.append(store.db, 'runs', dict(row, state='succeeded'), row['change_id'])
            store.db.commit()
        self.check_cli_draft(context, 'invalid draft job context')

    def test_cli_draft_uses_captured_run_capability_not_edited_job(self):
        context = self.job_context([])
        path = Path(self.tmp.name) / 'mind/capabilities/permitted.md'
        path.write_text('---\nname: "permitted"\nreads: []\ncalls: []\nrecord: []\npermits: ["draft"]\n---\nFixture')
        with writer(self.root) as store:
            jobs.job_verb(store.db, 'edit', 'test', spec(capability='permitted'), now=NOW)
        self.check_cli_draft(context, 'lacks draft permit')

    def test_cli_draft_invalid_or_absent_capability_refuses(self):
        context = self.job_context(['draft'])
        path = Path(self.tmp.name) / 'mind/capabilities/sample.md'
        path.write_text('invalid frontmatter')
        self.check_cli_draft(context, 'invalid capability frontmatter')
        with writer(self.root) as store:
            row = jobs.get(store.db, 'runs', context['STEWARD_RUN_ID'])
            captured = json.loads(row['spec_json'])
            captured.pop('capability')
            jobs.append(store.db, 'runs', dict(row, spec_json=json.dumps(captured)), row['change_id'])
            store.db.commit()
        self.check_cli_draft(context, 'requires a capability job')

    def test_cli_owner_and_wake_drafts_keep_authority_without_job_context(self):
        for context in ({}, {'STEWARD_ACTOR': 'model'}):
            with self.subTest(context=context):
                self.check_cli_draft(context)

    def test_telegram_reply_undo_dispatch(self):
        result = self.create(reply_to='abc')
        update = {'update_id': 1, 'message': {'message_id': 2, 'chat': {'id': 1},
                  'from': {'id': 2}, 'text': 'undo', 'reply_to_message': {'message_id': 42}}}
        sent = []
        with writer(self.root) as store, patch('steward.drafts.undo', return_value={'change_id': 'f' * 32}) as undo:
            messages.poll(store.db, NOW, {}, sender=lambda topic, text, **kw: sent.append(text) or 43,
                          fetcher=lambda offset: [update], telegram={'chat_id': 1, 'owner_user_id': 2, 'topics': {'Talk': 6}})
            self.assertEqual(undo.call_args.args[1], result['change_id'])
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
