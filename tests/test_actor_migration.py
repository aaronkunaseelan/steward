"""Actor identity, owner-visible reporting and the v17 -> v18 upgrade."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from fixture_mind import install
from steward import jobs, memory, messages, pages, state
from steward.cli import main
from steward.notify import TOPICS, flush, notify
from test_pages import NOW


class ActorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        install(self, Path(self.tmp.name))
        self.root = Path(self.tmp.name) / 'state'

    def test_writes_require_an_explicit_public_actor_before_opening_state(self):
        for actor in (None, '', 'other', 'tick', 'migration'):
            with self.subTest(actor=actor), patch.dict(os.environ):
                os.environ.pop('STEWARD_ACTOR', None)
                if actor is not None:
                    os.environ['STEWARD_ACTOR'] = actor
                output = io.StringIO()
                with redirect_stdout(output), patch('steward.cli.writer') as writer:
                    code = main(['init-state', '--state', str(self.root)])
                self.assertEqual(code, 2)
                self.assertIn('STEWARD_ACTOR=owner, model or maintenance', json.loads(output.getvalue())['error'])
                writer.assert_not_called()
        self.assertFalse(self.root.exists())
        for actor in ('owner', 'model', 'maintenance'):
            with self.subTest(actor=actor), patch.dict(os.environ, STEWARD_ACTOR=actor), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['init-state', '--state', str(self.root)]), 0)

    def test_read_needs_no_actor_and_journal_messages_have_no_kind(self):
        with state.writer(self.root, initialize=True) as store:
            notify(store.db, 'Talk', 'A reply', sender=lambda *a, **kw: 1, now=NOW)
        output = io.StringIO()
        with patch.dict(os.environ), redirect_stdout(output):
            os.environ.pop('STEWARD_ACTOR', None)
            self.assertEqual(main(['journal', '--state', str(self.root), '--day', str(memory.local_day(NOW)), '--json']), 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report['messages'])
        self.assertNotIn('kind', report['messages'][0])

    def test_maintenance_is_stored_but_never_sent_or_reported(self):
        send = Mock(return_value=1)
        with state.writer(self.root, initialize=True) as store:
            db = store.db
            for verb in ('block choose', 'draft', 'undo draft', 'moment set', 'job add', 'run now'):
                with db:
                    cid = jobs.change(db, verb, NOW, 'maintenance')
                    pages.meta(db, 'owner_request:' + cid, 1)
                self.assertEqual(messages.visible_change(db, db.execute('SELECT * FROM changes WHERE id=?', (cid,)).fetchone()), '')
            messages.changes(db, NOW, send, {})
            for topic in TOPICS:
                result = notify(db, topic, 'Maintenance record', sender=send, now=NOW,
                                actor='maintenance', deliver='immediate')
                self.assertEqual(result['status'], 'suppressed')
            flush(db, send, NOW)
            send.assert_not_called()
            self.assertTrue(db.execute("SELECT 1 FROM changes WHERE actor='maintenance'").fetchone())
            with self.assertRaisesRegex(state.StateError, 'invalid actor'):
                jobs.change(db, 'fixture', NOW, 'other')

    def test_v17_rows_migrate_and_the_same_url_serves_your_list(self):
        self.root.mkdir()
        db = sqlite3.connect(self.root / 'state.sqlite')
        db.row_factory = sqlite3.Row
        # Build the actual old schema, rather than downgrade a current database.
        db.executescript(state.SCHEMA)
        for version in range(2, 18):
            db.executescript(getattr(state, 'SCHEMA_V' + str(version)))
        objects = {}
        def transport(method, path, **options):
            if method == 'PUT':
                objects[path] = options['data']
            return (200, objects[path]) if method == 'GET' else (204, b'')
        old = pages.put(db, 'Your items', b'# Your items\nOld list', 'old.md',
                        slug='your-items-fixture', now=NOW, transport=transport)
        with db:
            pages.meta(db, 'your_items_slug', old['slug'])
            for actor in ('supervisor', 'owner', 'model', 'tick', 'migration'):
                db.execute('INSERT INTO changes VALUES (?,?,?,?,?,NULL)',
                           (actor, NOW.isoformat(), actor, 'fixture', 'Retained evidence'))
                db.execute("INSERT INTO outbox(id,change_id,topic,kind,text,state,delivery) VALUES (?,?, 'Talk','alert','Retained message','pending','immediate')", (actor, actor))
        db.close()
        with state.writer(self.root) as store:
            db = store.db
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 18)
            actor_tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                            if 'actor' in {c[1] for c in db.execute('PRAGMA table_info(' + r[0] + ')')}]
            self.assertEqual(actor_tables, ['changes'])
            self.assertEqual(db.execute("SELECT actor FROM changes WHERE id='supervisor'").fetchone()[0], 'maintenance')
            for actor in ('owner', 'model', 'tick', 'migration'):
                self.assertEqual(db.execute('SELECT actor FROM changes WHERE id=?', (actor,)).fetchone()[0], actor)
            self.assertNotIn('kind', {r[1] for r in db.execute('PRAGMA table_info(outbox)')})
            self.assertEqual(db.execute("SELECT state FROM outbox WHERE id='supervisor'").fetchone()[0], 'suppressed')
            self.assertEqual(db.execute("SELECT text,state FROM outbox WHERE id='owner'").fetchone()[:], ('Retained message', 'pending'))
            self.assertIsNone(pages.read_meta(db, 'your_items_slug'))
            self.assertEqual(pages.your_list(db)['url'], old['url'])
            updated = pages.sync_your_list(db, dict(status='unknown', reason='fixture unavailable'), NOW, transport)
            self.assertEqual(updated['url'], old['url'])
            self.assertEqual(db.execute("SELECT title FROM pages WHERE state='live'").fetchall()[0][0], 'Your list')
            self.assertEqual(db.execute("SELECT count(*) FROM pages WHERE state='live'").fetchone()[0], 1)
            body = objects['/_api/pages/' + old['slug']].decode()
            self.assertIn('Your list', body)
            self.assertNotIn('Old list', body)
            self.assertNotIn('Your items', body)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        # The schema upgrade is one-time, not a per-open rewrite.
        with state.writer(self.root) as store:
            self.assertEqual(pages.your_list(store.db)['url'], old['url'])
