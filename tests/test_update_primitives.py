"""Model-written updates: live reads, shell diff, replies, and no derived messages."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from fixture_mind import use_settings
import test_jobs
from steward import capabilities, cli, jobs, memory, messages, pages, state, timing
from steward.notify import TODAY_CHARACTERS, flush, notify

NOW = test_jobs.NOW


class UpdateMigrationTests(unittest.TestCase):
    setUp = use_settings

    def test_v19_cleanup_preserves_message_and_change_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'state'
            root.mkdir()
            db = sqlite3.connect(root / 'state.sqlite')
            db.row_factory = sqlite3.Row
            db.executescript(state.SCHEMA)
            for version in range(2, 20):
                db.executescript(getattr(state, 'SCHEMA_V' + str(version)))
            with db:
                db.execute("INSERT INTO changes VALUES ('edit',?,'model','page put','Retained edit',NULL)", (NOW.isoformat(),))
                db.execute("INSERT INTO outbox(id,change_id,topic,text,state,message_id,thread_id,delivery) VALUES ('shown','edit','Today','Retained message','sent',42,7,'immediate')")
                db.execute("INSERT INTO message_changes VALUES ('shown','edit')")
                db.execute("INSERT INTO outbox(id,change_id,topic,text,state,thread_id,delivery) VALUES ('reply','edit','Today','Pending reply','pending',7,'immediate')")
                db.execute("INSERT INTO message_changes VALUES ('reply','edit')")
                db.execute("INSERT INTO subject_showings VALUES ('page:notes','shown')")
                for key in ('change_subjects:edit', 'change_context:edit', 'outbox_subjects:shown',
                            'changes_cursor', 'changes_waiting'):
                    pages.meta(db, key, ['RP1 fixture'])
                kept_meta = {'outbox_reply:reply': 42, 'changes_reported:old': True,
                             'owner_request:edit': 9, 'pages_opens_cursor': 'kept'}
                for key, value in kept_meta.items():
                    pages.meta(db, key, value)
            tables = ('outbox', 'message_changes', 'changes')
            before = {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table)] for table in tables}
            db.close()
            for _ in range(2):
                with state.writer(root) as store:
                    db = store.db
                    self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 20)
                    self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='subject_showings'").fetchone())
                    self.assertEqual({table: [tuple(r) for r in db.execute('SELECT * FROM ' + table)] for table in tables}, before)
                    self.assertEqual({r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM message_meta')}, kept_meta)
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')


class PageGetTests(unittest.TestCase):
    setUp = use_settings

    def invoke(self, transport):
        body = io.BytesIO()
        output = io.TextIOWrapper(body, encoding='utf-8')
        with patch.dict(os.environ), patch('steward.pages.request', side_effect=transport), \
                patch('steward.cli.writer', side_effect=AssertionError('read opened writer')), \
                redirect_stdout(output):
            os.environ.pop('STEWARD_ACTOR', None)
            code = cli.main(['page', 'get', 'notes'])
        output.flush()
        result = body.getvalue()
        output.detach()
        return code, result

    def test_cli_prints_live_body_without_actor_or_local_state(self):
        transport = Mock(return_value=(200, b'<h1>Live notes</h1>\n'))
        self.assertEqual(self.invoke(transport), (0, b'<h1>Live notes</h1>\n'))
        transport.assert_called_once_with('GET', '/_api/pages/notes')

    def test_cli_unknown_page_and_failed_reads_are_one_line_nonzero(self):
        for result, reason in [((404, b''), 'unknown page'), ((503, b'private response'), 'HTTP 503'),
                               (OSError('private error'), 'network or configuration')]:
            with self.subTest(result=result):
                transport = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
                code, output = self.invoke(transport)
                self.assertNotEqual(code, 0)
                self.assertEqual(len(output.splitlines()), 1)
                self.assertIn(reason.encode(), output)
                self.assertNotIn(b'private', output)

    def test_read_rejects_invalid_slug_and_oversized_body(self):
        transport = Mock(return_value=(200, b'x' * (pages.LIMIT + 1)))
        with self.assertRaisesRegex(state.StateError, 'invalid page slug'):
            pages.get('../notes', transport)
        transport.assert_not_called()
        with self.assertRaisesRegex(state.StateError, '10 MiB'):
            pages.get('notes', transport)

    def test_diff_is_available_in_the_capability_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before, after = root / 'before', root / 'after'
            before.write_text('old\n')
            after.write_text('new\n')
            bindir = capabilities.shell_bin(root, [])
            result = subprocess.run(['/bin/bash', '-c', 'diff "$1" "$2"', '--', str(before), str(after)],
                                    env=dict(os.environ, PATH=str(bindir)), capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('< old\n', result.stdout)
            self.assertIn('> new\n', result.stdout)


class ReplyTests(unittest.TestCase):
    setUp = test_jobs.JobsTests.setUp
    tick = test_jobs.JobsTests.tick
    add = test_jobs.JobsTests.add
    run_now = test_jobs.JobsTests.run_now

    def shown(self, db, topic='Today', thread=17, mid=42):
        return notify(db, topic, 'Original message', sender=lambda *a, **kw: mid,
                      now=NOW, deliver='immediate', thread_id=thread)

    def test_reply_uses_original_topic_thread_and_bot_api_parameters(self):
        with state.writer(self.root) as store:
            self.shown(store.db)
            # Different current topic binding must not move the existing thread.
            with patch.dict(os.environ, STEWARD_TELEGRAM_BOT_TOKEN='synthetic'), \
                    patch('steward.notify.binding', return_value={'chat_id': -1, 'topics': {'Today': 99}}), \
                    patch('steward.notify.api', return_value={'message_id': 43}) as api:
                sent = notify(store.db, None, 'What changed and why', reply_to=42, now=NOW)
            self.assertEqual(sent['status'], 'sent')
            method, payload = api.call_args.args
            self.assertEqual(method, 'sendMessage')
            self.assertEqual(payload['message_thread_id'], 17)
            self.assertEqual(payload['reply_parameters'], dict(message_id=42, allow_sending_without_reply=True))
            self.assertTrue(payload['disable_notification'])
            row = store.db.execute('SELECT * FROM outbox WHERE id=?', (sent['outbox_id'],)).fetchone()
            self.assertEqual((row['topic'], row['thread_id']), ('Today', 17))

    def test_reply_refuses_unknown_unsent_mismatched_topic_and_today_overflow(self):
        send = Mock(return_value=43)
        with state.writer(self.root) as store:
            db = store.db
            self.shown(db)
            with patch.dict(os.environ):
                os.environ.pop('STEWARD_TELEGRAM_BOT_TOKEN', None)
                pending = notify(db, 'Talk', 'Not sent yet', now=NOW)
            # Even a stored id is not proof of a confirmed send.
            with db:
                db.execute('UPDATE outbox SET message_id=77 WHERE id=?', (pending['outbox_id'],))
            cases = [(dict(reply_to=999), 'not a message Steward sent'),
                     (dict(reply_to=77), 'not a message Steward sent'),
                     (dict(reply_to=0), 'invalid reply message id'),
                     (dict(topic='Talk', reply_to=42), 'disagrees'),
                     (dict(reply_to=42, text='x' * (TODAY_CHARACTERS + 1)), 'Today must be')]
            count = db.execute('SELECT count(*) FROM outbox').fetchone()[0]
            for options, reason in cases:
                with self.subTest(options=options), self.assertRaisesRegex(state.StateError, reason):
                    notify(db, sender=send, now=NOW, **(dict(topic=None, text='Update') | options))
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0], count)
            send.assert_not_called()

    def test_run_cli_reply_queues_and_tick_delivers_once_including_finds(self):
        self.add()
        run_id = self.run_now()
        self.tick()
        with state.writer(self.root) as store:
            self.shown(store.db, topic='Finds', thread=24)
        output = io.StringIO()
        with patch.dict(os.environ, STEWARD_ACTOR='model', STEWARD_JOB='test', STEWARD_RUN_ID=run_id), \
                patch('steward.notify.api', side_effect=AssertionError('run must queue')), \
                redirect_stdout(output):
            os.environ.pop('STEWARD_TELEGRAM_BOT_TOKEN', None)
            code = cli.main(['notify', '--state', str(self.root), '--reply-to', '42', 'A model-written update'])
        self.assertEqual(code, 0)
        queued = json.loads(output.getvalue())
        self.assertEqual(queued['status'], 'pending')
        with state.writer(self.root) as store:
            row = store.db.execute('SELECT * FROM outbox WHERE id=?', (queued['outbox_id'],)).fetchone()
            self.assertEqual((row['job'], row['run_id'], row['topic'], row['thread_id'], row['delivery']),
                             ('test', run_id, 'Finds', 24, 'immediate'))
        sent = []
        def sender(topic, text, **options):
            sent.append((topic, text, options))
            return 43
        self.tick(sender=sender)
        self.tick(sender=sender)
        replies = [r for r in sent if r[1] == 'A model-written update']
        self.assertEqual(replies, [('Finds', 'A model-written update', dict(disable_notification=True,
            parse_mode='HTML', thread_id=24, reply_parameters=dict(message_id=42, allow_sending_without_reply=True)))])
        with state.writer(self.root) as store:
            self.assertEqual(store.db.execute('SELECT state,message_id FROM outbox WHERE id=?', (queued['outbox_id'],)).fetchone()[:], ('sent', 43))

    def test_page_reput_after_showing_sends_nothing_by_itself(self):
        objects = {}
        def transport(method, path, **options):
            if method == 'PUT':
                objects[path] = options['data']
            return (200, objects[path]) if method == 'GET' else (204, b'')
        send = Mock(return_value=42)
        with state.writer(self.root) as store:
            page = pages.put(store.db, 'Prep', b'# Before', 'prep.md', slug='prep', now=NOW, transport=transport, actor='model')
            notify(store.db, 'Today', '[Prep](' + page['url'] + ')', now=NOW, sender=send)
            send.reset_mock()
            pages.put(store.db, 'Prep', b'# After\nNew content', 'prep.md', slug='prep', now=NOW,
                      transport=transport, actor='model')
            memory.write(store.db, 'synthesis', 'current', 'Top of mind\n- A new thought', actor='model', now=NOW)
            messages.changes(store.db, NOW, send, {})
            flush(store.db, send, NOW)
            send.assert_not_called()

    def test_changes_reports_moments_even_when_previously_reported(self):
        send = Mock(return_value=42)
        with state.writer(self.root) as store:
            for start in ('07:00', '07:10'):
                timing.set_moment(store.db, 'orientation', start, '08:00', now=NOW, actor='model')
                messages.changes(store.db, NOW, send, {})
            self.assertEqual(send.call_count, 2)
            for call in send.call_args_list:
                self.assertEqual(call.args[0], 'Changes')
                self.assertIn('orientation', call.args[1])
                self.assertIn('undo', call.args[1])
                self.assertNotIn('reply_parameters', call.kwargs)
