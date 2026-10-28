from contextlib import nullcontext
from datetime import timedelta
import io
from types import SimpleNamespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fixture_mind import install
from steward import calendar_b, jobs, pages, senses
from steward.state import writer
from test_pages import NOW


class ComposedPagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        install(self, Path(self.tmp.name))
        self.db = self.enterContext(writer(Path(self.tmp.name) / 'state', initialize=True)).db
        self.calls, self.objects = [], {}
        self.raw = json.loads((Path(__file__).parent / 'fixtures/gsd.worklist.v1.json').read_text())
        sample = self.raw['items'][0]
        self.raw['items'] = [dict(sample, id='a', title='First', notion_url='https://notion.so/a'),
                             dict(sample, id='b', title='Second', notion_url='https://notion.so/b')]
        self.raw['priorities']['mine'] = [dict(id=i['id'], title=i['title'], project='Shared',
                                              due_at=i['due_at'], priority='10') for i in reversed(self.raw['items'])]

    def transport(self, method, path, **kw):
        self.calls.append((method, path))
        if method == 'PUT':
            self.objects[path] = kw['data']
        return (200, self.objects[path]) if method == 'GET' else (204, b'')

    def put(self, **kw):
        return pages.put(self.db, 'Prep', b'## Plan\nOne shared message.', 'plan.md',
                         now=NOW, transport=self.transport, **kw)

    def test_header_open_closed_and_no_gsd_prose(self):
        with patch('steward.senses.worklist', return_value=self.raw):
            result = self.put(items=['a', 'closed'])
        body = self.objects['/_api/pages/' + result['slug']].decode()
        self.assertIn('<a href="https://notion.so/a">First</a>', body)
        self.assertIn('closed: no longer open in gsd', body)
        self.assertIn('gsd as of', body)
        self.assertLess(body.index('First'), body.index('One shared message'))
        self.assertNotIn('waiting_on', body)

    def test_header_failed_read_is_unknown(self):
        with patch('steward.senses.worklist', side_effect=ValueError('fixture unavailable')):
            result = self.put(items=['a'])
        self.assertIn('gsd: unknown (read failed: ValueError)', self.objects['/_api/pages/' + result['slug']].decode())

    def test_block_header_uses_today_applied_block_and_justification(self):
        spec = dict(title='Older demand title', justification='One person moves both', items=['a', 'b'])
        with self.db:
            cid = self.db.execute("INSERT INTO changes(at,actor,verb,summary) VALUES (?,?,?,?)",
                                  (NOW.isoformat(), 'test', 'test', 'test')).lastrowid
            calendar_b.append(self.db, 'demands', dict(name='shared', version=0, state='active', spec_json=json.dumps(spec)), cid)
            calendar_b.append(self.db, 'blocks', dict(event_key='shared:once:0', version=0, demand='shared', state='applied',
                payload=json.dumps(dict(title='Together', start=NOW.isoformat(), end=(NOW + timedelta(minutes=45)).isoformat()))), cid)
        with patch('steward.senses.worklist', return_value=self.raw):
            result = self.put(items=['a', 'b'], block='shared')
        body = self.objects['/_api/pages/' + result['slug']].decode()
        self.assertIn('Together', body)
        self.assertNotIn('Older demand title', body)
        self.assertIn('23:00', body)
        self.assertIn('One person moves both', body)

    def test_prepared_pages_reuse_one_random_identity_per_subject_day(self):
        with self.db:
            cid = jobs.change(self.db, 'fixture', NOW)
            calendar_b.append(self.db, 'demands', dict(name='shared', state='active',
                spec_json=json.dumps(dict(title='Together', justification='One preparation'))), cid)
        with patch('steward.senses.worklist', return_value=self.raw):
            first = self.put(items=['a'], block='shared')
            again = self.put(items=['a'], block='shared')
            meeting = self.put(items=['a'])
            meeting_again = self.put(items=['a'])
        self.assertEqual(first['slug'], again['slug'])
        self.assertEqual(meeting['slug'], meeting_again['slug'])
        self.assertNotEqual(first['slug'], meeting['slug'])
        self.assertRegex(first['slug'], r'-[a-z2-7]{12}$')
        self.assertRegex(meeting['slug'], r'-[a-z2-7]{12}$')
        self.assertNotIn(NOW.strftime('%Y%m%d'), first['slug'])
        # Stored before upload: a failed attempt also retries the same identity.
        with patch('steward.senses.worklist', return_value=self.raw):
            failed = pages.put(self.db, 'Prep', b'ready', 'plan.md', items=['b'], now=NOW,
                               transport=lambda *a, **k: (503, b''))
            recovered = self.put(items=['b'])
        self.assertEqual(failed['slug'], recovered['slug'])
        self.assertEqual(recovered['state'], 'live')

    def test_existing_deterministic_prep_links_are_adopted_not_renamed(self):
        block_slug = 'prep-shared-' + NOW.strftime('%Y%m%d')
        item_slug = 'prep-a-' + NOW.strftime('%Y%m%d')
        with self.db:
            cid = jobs.change(self.db, 'fixture', NOW)
            calendar_b.append(self.db, 'demands', dict(name='shared', state='active',
                spec_json=json.dumps(dict(title='Together', justification='One preparation'))), cid)
        for slug in (block_slug, item_slug):
            self.put(slug=slug)
        with patch('steward.senses.worklist', return_value=self.raw):
            block = self.put(items=['a'], block='shared')
            item = self.put(items=['a'])
        self.assertEqual(block['slug'], block_slug)
        self.assertEqual(item['slug'], item_slug)

    def test_without_items_does_not_read_gsd(self):
        with patch('steward.senses.worklist') as reader:
            result = self.put()
        reader.assert_not_called()
        self.assertNotIn('gsd as of', self.objects['/_api/pages/' + result['slug']].decode())

    def test_your_list_hash_skip_and_stable_slug(self):
        sense = dict(status='ok', data=senses.gsd(NOW, lambda argv: json.dumps(self.raw)))
        first = pages.sync_your_list(self.db, sense, NOW, self.transport)
        self.assertEqual(first['state'], 'live')
        slug = first['slug']
        body = self.objects['/_api/pages/' + slug].decode()
        self.assertLess(body.index('Second'), body.index('First'))
        self.assertIn('https://notion.so/b', body)
        self.calls.clear()
        pages.sync_your_list(self.db, sense, NOW + timedelta(minutes=20), self.transport)
        self.assertEqual(self.calls, [])
        sense['data']['waiting_on_you'][0]['title'] = 'Changed'
        second = pages.sync_your_list(self.db, sense, NOW + timedelta(minutes=40), self.transport)
        self.assertEqual(second['slug'], slug)
        self.assertEqual([m for m, p in self.calls], ['PUT', 'GET'])

    def test_your_list_failed_upload_retries_same_slug(self):
        sense = dict(status='unknown', reason='fixture failure')
        first = pages.sync_your_list(self.db, sense, NOW, lambda *a, **k: (503, b''))
        self.assertEqual(first['state'], 'failed')
        second = pages.sync_your_list(self.db, sense, NOW, self.transport)
        self.assertEqual(second['slug'], first['slug'])
        self.assertEqual(second['state'], 'live')
        self.assertEqual(pages.your_list(self.db)['url'], second['url'])

    def test_cli_prep_options(self):
        from steward.cli import main
        source = Path(self.tmp.name) / 'plan.md'
        source.write_text('One message')
        with self.db:
            cid = jobs.change(self.db, 'fixture', NOW)
            calendar_b.append(self.db, 'demands', dict(name='shared', version=0, state='active',
                spec_json=json.dumps(dict(title='Shared block', justification='Prepare together'))), cid)
        output = io.StringIO()
        with patch('steward.cli.writer', return_value=nullcontext(SimpleNamespace(db=self.db))), \
             patch('steward.senses.worklist', return_value=self.raw), \
             patch('steward.pages.request', side_effect=self.transport), \
             patch('sys.stdout', output):
            self.assertEqual(main(['page', 'put', '--title', 'Prep', '--items', 'a,b', '--block', 'shared', str(source)]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['state'], 'live')
        body = self.objects['/_api/pages/' + report['slug']].decode()
        for text in ('Shared block', 'Prepare together', 'https://notion.so/a', 'https://notion.so/b', 'One message'):
            self.assertIn(text, body)

    def test_your_list_not_observed_this_tick_keeps_page(self):
        sense = dict(status='ok', data=senses.gsd(NOW, lambda argv: json.dumps(self.raw)))
        first = pages.sync_your_list(self.db, sense, NOW, self.transport)
        body = self.objects['/_api/pages/' + first['slug']]
        self.calls.clear()
        result = pages.sync_your_list(self.db, {}, NOW + timedelta(minutes=20), self.transport)
        self.assertEqual(result['url'], first['url'])
        self.assertEqual(result['state'], 'live')
        self.assertEqual(self.objects['/_api/pages/' + first['slug']], body)
        self.assertEqual(self.calls, [])

    def test_your_list_failed_sense_keeps_reason(self):
        result = pages.sync_your_list(self.db, dict(status='unknown', reason='fixture failure'), NOW, self.transport)
        self.assertIn('gsd: unknown (fixture failure)', self.objects['/_api/pages/' + result['slug']].decode())
