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
from steward import pages, senses
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
        from steward import calendar_b
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

    def test_without_items_does_not_read_gsd(self):
        with patch('steward.senses.worklist') as reader:
            result = self.put()
        reader.assert_not_called()
        self.assertNotIn('gsd as of', self.objects['/_api/pages/' + result['slug']].decode())

    def test_your_items_hash_skip_and_stable_slug(self):
        sense = dict(status='ok', data=senses.gsd(NOW, lambda argv: json.dumps(self.raw)))
        first = pages.sync_your_items(self.db, sense, NOW, self.transport)
        self.assertEqual(first['state'], 'live')
        slug = first['slug']
        body = self.objects['/_api/pages/' + slug].decode()
        self.assertLess(body.index('Second'), body.index('First'))
        self.assertIn('https://notion.so/b', body)
        self.calls.clear()
        pages.sync_your_items(self.db, sense, NOW + timedelta(minutes=20), self.transport)
        self.assertEqual(self.calls, [])
        sense['data']['waiting_on_you'][0]['title'] = 'Changed'
        second = pages.sync_your_items(self.db, sense, NOW + timedelta(minutes=40), self.transport)
        self.assertEqual(second['slug'], slug)
        self.assertEqual([m for m, p in self.calls], ['PUT', 'GET'])

    def test_your_items_failed_upload_retries_same_slug(self):
        sense = dict(status='unknown', reason='fixture failure')
        first = pages.sync_your_items(self.db, sense, NOW, lambda *a, **k: (503, b''))
        self.assertEqual(first['state'], 'failed')
        second = pages.sync_your_items(self.db, sense, NOW, self.transport)
        self.assertEqual(second['slug'], first['slug'])
        self.assertEqual(second['state'], 'live')
        self.assertEqual(pages.your_items(self.db)['url'], second['url'])

    def test_cli_prep_options(self):
        from steward.cli import main
        source = Path(self.tmp.name) / 'plan.md'
        source.write_text('One message')
        with patch('steward.cli.writer', return_value=nullcontext(SimpleNamespace(db=self.db))), \
             patch('steward.pages.put', return_value={}) as put, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['page', 'put', '--title', 'Prep', '--items', 'a,b', '--block', 'shared', str(source)]), 0)
        self.assertEqual(put.call_args.kwargs['items'], ['a', 'b'])
        self.assertEqual(put.call_args.kwargs['block'], 'shared')

    def test_your_items_not_observed_this_tick_keeps_page(self):
        # gsd is absent from a tick's observations when its interval has not passed: not unknown, no upload.
        pages.sync_your_items(self.db, {}, NOW, self.transport)
        self.assertEqual(self.calls, [])

    def test_your_items_failed_sense_keeps_reason(self):
        result = pages.sync_your_items(self.db, dict(status='unknown', reason='fixture failure'), NOW, self.transport)
        self.assertIn('gsd: unknown (fixture failure)', self.objects['/_api/pages/' + result['slug']].decode())
