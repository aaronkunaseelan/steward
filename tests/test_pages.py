from fixture_mind import use_settings
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from steward import jobs, pages, runtime
from steward.notify import notify, render, flush
from steward.state import StateError, writer, restore

NOW = datetime(2026, 10, 18, 20, tzinfo=timezone.utc)


class PagesRequestTests(unittest.TestCase):
    setUp = use_settings

    def test_public_header_is_explicit_opt_in(self):
        with patch('steward.pages.token', return_value='synthetic'), patch('steward.pages.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.status = 204
            for public in (True, False):
                pages.request('PUT', '/_api/pages/test', data=b'notes', public=public)
                req = opener.return_value.open.call_args.args[0]
                self.assertEqual(req.get_header('X-steward-public'), '1' if public else None)

    def test_authenticated_object_read_is_bounded_at_object_cap(self):
        with patch('steward.pages.token', return_value='test-only'), patch('steward.pages.urllib.request.build_opener') as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.side_effect = io.BytesIO(b'x' * (10 * 1024 * 1024 + 100)).read
            status, body = pages.request('GET', '/_api/pages/test')
            self.assertEqual(status, 200)
            self.assertEqual(body, b'x' * (10 * 1024 * 1024 + 1))
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.get_header('Authorization'), 'Bearer test-only')

    def test_explicit_user_agent_on_every_pages_request(self):
        with patch('steward.pages.token', return_value='test-only'), patch('steward.pages.urllib.request.build_opener') as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'{}'
            for method, path in [('PUT', '/_api/pages/test'), ('DELETE', '/_api/pages/test'),
                                 ('GET', '/_api/opens?after=&limit=1000'), ('HEAD', '/test')]:
                with self.subTest(method=method):
                    pages.request(method, path, data=b'page' if method == 'PUT' else None)
                    request = opener.return_value.open.call_args.args[0]
                    self.assertEqual(request.get_header('User-agent'), 'steward/pages')
                    self.assertEqual(request.get_method(), method)
                    self.assertEqual(request.get_header('Authorization'), None if method == 'HEAD' else 'Bearer test-only')


class PagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from fixture_mind import install
        install(self, Path(self.tmp.name))
        self.root = Path(self.tmp.name) / 'state'
        self.store = self.enterContext(writer(self.root, initialize=True))
        self.db = self.store.db
        self.calls = []
        self.read_status = 200
        self.objects = {}

    def transport(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if method == 'PUT':
            self.objects[path] = kw['data']
        if method == 'GET':
            return self.read_status, self.objects[path]
        return 204, b''

    def put(self, **kw):
        return pages.put(self.db, 'The evidence for a better day', b'<p>Hello</p>', 'a.fragment.html',
                         now=NOW, transport=self.transport, **kw)

    def test_readback_verifies_object_not_login_response(self):
        for body in (b'<form>login</form>', b'wrong stored bytes'):
            with self.subTest(body=body):
                page = pages.put(self.db, 'Notes', b'expected', 'x.txt', now=NOW,
                    transport=lambda method, path, **kw: (204, b'') if method == 'PUT' else (200, body))
                self.assertEqual(page['state'], 'failed')

    def test_readback_uses_authenticated_api_and_matches_large_binary(self):
        expected = bytes(range(256)) * 2048
        calls = []
        def transport(method, path, **kw):
            calls.append((method, path))
            return (204, b'') if method == 'PUT' else (200, expected)
        page = pages.put(self.db, 'Binary', expected, 'x.bin', now=NOW, transport=transport)
        self.assertEqual(page['state'], 'live')
        self.assertEqual(calls, [('PUT', '/_api/pages/' + page['slug']),
                                 ('GET', '/_api/pages/' + page['slug'])])

    def test_visibility_defaults_private_and_overwrite_flips_both_ways(self):
        page = self.put()
        slug = page['slug']
        self.assertFalse(page['public'])
        self.assertFalse(pages.live(self.db, NOW)[0]['public'])
        for public in (True, False):
            with self.subTest(public=public):
                page = self.put(slug=slug, public=public)
                self.assertEqual(page['public'], public)
                self.assertEqual(self.calls[-2][2]['public'], public)
                self.assertEqual(jobs.snapshot(self.db, now=NOW)['pages']['items'][0]['public'], public)

    def test_slug_shape_randomness_and_limit(self):
        slugs = {pages.make_slug('The evidence for a better day') for _ in range(100)}
        self.assertEqual(len(slugs), 100)
        for slug in slugs:
            self.assertRegex(slug, r'^evidence-better-day-[a-z2-7]{12}$')
        long_slug = pages.make_slug('x' * 1000)
        self.assertRegex(long_slug, r'^x{18}-[a-z2-7]{12}$')
        self.assertLessEqual(len(long_slug), 80)
        self.assertRegex(pages.make_slug('the and of'), r'^page-[a-z2-7]{12}$')
        self.assertFalse(pages.valid_slug('a--b'))

    def test_template_and_redaction_before_upload(self):
        page = pages.put(self.db, 'Evidence & <notes>', b'<p>Bearer abcdefghijklmnop</p>', 'file.html', now=NOW, transport=self.transport)
        uploaded = self.calls[0][2]['data']
        self.assertNotIn(b'abcdefghijklmnop', uploaded)
        for part in [b'<!doctype html>', b'color-scheme:light dark', b'width=device-width', b'name="referrer" content="no-referrer"', b'Evidence &amp; &lt;notes&gt;', b'<p>Bearer [REDACTED]</p>']:
            self.assertIn(part, uploaded)
        self.assertEqual([c[0] for c in self.calls], ['PUT', 'GET'])
        row = self.db.execute('SELECT * FROM pages').fetchone()
        self.assertEqual(row['sha256'], hashlib.sha256(uploaded).hexdigest())
        self.assertEqual(row['bytes'], len(uploaded))
        self.assertEqual(page['state'], 'live')
        self.assertEqual(page['expires_at'], (NOW+timedelta(days=28)).isoformat())
        self.assertEqual(self.db.execute('SELECT count(*) FROM changes WHERE actor!=\'migration\'').fetchone()[0], 1)

    def test_text_redaction_binary_passthrough_and_forced_html(self):
        data, typ = pages.prepare('Notes', b'token=secret', 'x.txt')
        self.assertNotIn(b'secret', data)
        self.assertNotIn(b'secret', pages.prepare('Notes', b'token=secret', '-')[0])
        binary = b'\x89PNG\x00\xff'
        self.assertEqual(pages.prepare('Picture', binary, 'x.png'), (binary, 'image/png'))
        self.assertIn(b'<!doctype html>', pages.prepare('Notes', b'<b>Hi</b>', '-', True)[0])

    def test_cap_including_wrapper(self):
        with self.assertRaisesRegex(StateError, '10 MiB'):
            pages.prepare('Large', b'x'*(pages.LIMIT+1), 'x.txt')
        with self.assertRaisesRegex(StateError, 'after wrapping'):
            pages.prepare('Large', b'x'*pages.LIMIT, 'x.html')
        self.assertEqual(self.calls, [])

    def test_read_failed_page_cannot_be_linked(self):
        self.read_status = 404
        page = self.put()
        self.assertEqual(page['state'], 'failed')
        self.assertEqual(pages.read_meta(self.db, 'pages_put_error')['code'], 'read_404')
        with self.assertRaisesRegex(StateError, 'not live'):
            render(self.db, '[Evidence](' + page['url'] + ')', NOW)

    def test_upload_failure_is_recorded_and_does_not_read(self):
        calls = []
        def unavailable(method, path, **options):
            calls.append(method)
            return 503, b''
        page = pages.put(self.db, 'Notes', b'Hi', 'x.txt', now=NOW, transport=unavailable)
        self.assertEqual(page['state'], 'failed')
        self.assertEqual(pages.read_meta(self.db, 'pages_put_error')['code'], 'put_503')
        self.assertEqual(calls, ['PUT'])
        with patch('steward.pages.request', side_effect=OSError('secret must not leak')):
            page = pages.put(self.db, 'Notes', b'Hi', 'x.txt', now=NOW)
        self.assertEqual(page['state'], 'failed')
        self.assertNotIn('secret', json.dumps(pages.read_meta(self.db, 'pages_put_error')))

    def test_slug_overwrites_live_or_failed_creates_new_and_refuses_removed(self):
        page = self.put()
        again = self.put(slug=page['slug'])
        self.assertEqual(page['slug'], again['slug'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM pages').fetchone()[0], 1)
        new = self.put(slug='prep-grants-20261022')
        self.assertEqual((new['slug'], new['state']), ('prep-grants-20261022', 'live'))
        failed = pages.put(self.db, 'Notes', b'Hi', 'x.txt', slug='prep-comms-20261022', now=NOW, transport=lambda *a, **k: (503, b''))
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(self.put(slug='prep-comms-20261022')['state'], 'live')
        with self.assertRaisesRegex(StateError, 'invalid page slug'):
            self.put(slug='Bad--Slug')
        self.assertEqual(pages.remove(self.db, page['slug'], now=NOW, transport=self.transport)['state'], 'removed')
        self.assertEqual(self.calls[-1][0], 'DELETE')
        with self.assertRaisesRegex(StateError, 'removed'):
            self.put(slug=page['slug'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM pages').fetchone()[0], 3)

    def test_failed_rm_does_not_claim_removed(self):
        page = self.put()
        with self.assertRaisesRegex(StateError, 'delete failed'):
            pages.remove(self.db, page['slug'], now=NOW, transport=lambda *a, **k: (500, b''))
        self.assertEqual(self.db.execute('SELECT state FROM pages').fetchone()[0], 'live')

    def receipts(self, keys, next_key=None):
        def transport(*args, **kw):
            self.calls.append((args, kw))
            return 200, json.dumps(dict(keys=keys, next=next_key)).encode()
        return pages.poll(self.db, NOW+timedelta(minutes=1), transport)

    def test_poll_cursor_first_open_multiple_and_dedup_rescan(self):
        page = self.put()
        prefix = '_opens/' + page['slug'] + '/'
        first = prefix + '2026-10-18T20:00:01.000Z'
        second = prefix + '2026-10-18T20:00:02.000Z'
        self.assertTrue(self.receipts([first], first))
        self.assertEqual(pages.read_meta(self.db, 'pages_opens_cursor'), first)
        self.assertTrue(self.receipts([second]))
        self.assertIn('after=' + first.replace('/', '%2F').replace(':', '%3A'), self.calls[-1][0][1])
        self.assertEqual(self.calls[-1][1]['timeout'], 10)
        self.assertEqual(pages.read_meta(self.db, 'pages_opens_cursor'), '')
        self.assertTrue(self.receipts([first, second]))
        row = self.db.execute('SELECT * FROM pages').fetchone()
        self.assertEqual(row['opens'], 2)
        self.assertEqual(row['opened_at'], '2026-10-18T20:00:01+00:00')
        report = jobs.snapshot(self.db, now=NOW)['pages']
        self.assertEqual((report['live'], report['opened']), (1, 1))
        self.assertIsNotNone(report['last_poll_ok_at'])
        self.assertIsNone(report['last_error'])
        self.assertEqual(pages.live(self.db, NOW)[0]['url'], page['url'])

    def test_late_markers_that_sort_before_seen_markers_still_count(self):
        page = self.put()
        prefix = '_opens/' + page['slug'] + '/'
        later = prefix + '2026-10-18T20:00:02Z'
        earlier = prefix + '2026-10-18T20:00:01Z'
        self.assertTrue(self.receipts([later]))
        self.assertTrue(self.receipts([earlier, later]))
        self.assertTrue(self.receipts([earlier, later]))
        row = self.db.execute('SELECT opens,opened_at FROM pages').fetchone()
        self.assertEqual(row['opens'], 2)
        self.assertEqual(row['opened_at'], '2026-10-18T20:00:01+00:00')

    def test_receipts_old_before_overwrite_are_not_counted(self):
        page = self.put()
        self.assertTrue(self.receipts(['_opens/' + page['slug'] + '/2026-10-18T19:00:00Z']))
        self.assertEqual(self.db.execute('SELECT opens FROM pages').fetchone()[0], 0)

    def test_poll_failure_codes_and_atomic_validation(self):
        page = self.put()
        self.assertFalse(pages.poll(self.db, NOW, lambda *a, **k: (401, b'secret')))
        self.assertEqual(pages.status(self.db, NOW)['last_error'], dict(code='http_401', at=NOW.isoformat()))
        self.assertFalse(pages.poll(self.db, NOW, lambda *a, **k: (200, b'not json')))
        self.assertEqual(pages.status(self.db, NOW)['last_error']['code'], 'invalid_response')
        with patch('steward.pages.request', side_effect=TimeoutError('private')):
            self.assertFalse(pages.poll(self.db, NOW))
        self.assertEqual(pages.status(self.db, NOW)['last_error']['code'], 'network_or_config')
        good = '_opens/' + page['slug'] + '/2026-10-18T20:00:01Z'
        self.assertFalse(self.receipts([good, '_opens/z/bad']))
        self.assertEqual(self.db.execute('SELECT opens FROM pages').fetchone()[0], 0)
        self.assertTrue(self.receipts([]))
        self.assertIsNone(pages.status(self.db, NOW)['last_error'])

    def test_notify_conversion_escaping_preview_and_visible_length(self):
        sent = []
        sender = lambda topic, text, **options: sent.append((text, options)) or 1
        text = 'A < B & "C" [notes & details](https://example.org/?a=1&b=2)'
        notify(self.db, 'Changes', text, sender=sender, now=NOW)
        self.assertEqual(sent[0][0], 'A &lt; B &amp; &quot;C&quot; <a href="https://example.org/?a=1&amp;b=2">notes &amp; details</a>')
        self.assertEqual(sent[0][1]['parse_mode'], 'HTML')
        adjacent, visible = render(self.db, '[a](https://a.test)[b](https://b.test)', NOW)
        self.assertEqual(adjacent.count('<a href='), 2)
        self.assertEqual(visible, 'ab')
        self.assertEqual(render(self.db, '[entry](https://example.org/wiki/A_(B))', NOW)[1], 'entry')
        with patch('steward.notify.binding', return_value=dict(chat_id=1, topics={'Changes':2})), patch('steward.notify.api', return_value={'message_id':2}) as api:
            from steward.notify import telegram_sender
            telegram_sender('Changes', 'text', parse_mode='HTML')
            self.assertEqual(api.call_args.args[1]['link_preview_options'], {'is_disabled':True})
        notify(self.db, 'Today', '[x](https://example.org/' + 'a'*5000 + ')', now=NOW, sender=sender)
        self.assertEqual(len(sent), 2)

    def test_bold_is_escaped_local_markup_with_matching_visible_text(self):
        cases = [
            ('**Fri 23 Oct**\nToday', '<b>Fri 23 Oct</b>\nToday', 'Fri 23 Oct\nToday'),
            ('**Today** [Notes](https://example.org) **Ready**',
             '<b>Today</b> <a href="https://example.org">Notes</a> <b>Ready</b>', 'Today Notes Ready'),
            ('**<script>x & y</script>**', '<b>&lt;script&gt;x &amp; y&lt;/script&gt;</b>', '<script>x & y</script>'),
            ('**Unpaired', '**Unpaired', '**Unpaired'),
            ('**Across [Notes](https://example.org) link**',
             '**Across <a href="https://example.org">Notes</a> link**', '**Across Notes link**'),
            ('**Across [Notes](https://example.org) link** **Next**',
             '**Across <a href="https://example.org">Notes</a> link** <b>Next</b>', '**Across Notes link** Next'),
            ('<b>literal</b> _plain_ *plain*', '&lt;b&gt;literal&lt;/b&gt; _plain_ *plain*', '<b>literal</b> _plain_ *plain*'),
        ]
        for source, markup, visible in cases:
            with self.subTest(source=source):
                self.assertEqual(render(self.db, source, NOW), (markup, visible))

    def test_notify_refuses_bare_urls_and_unsafe_links(self):
        for text in ['see https://example.org', 'www.example.org', '<a href="https://example.org">x</a>', '[x](https://user:pass@example.org)']:
            with self.subTest(text=text), self.assertRaises(StateError):
                notify(self.db, 'Changes', text, now=NOW)

    def test_live_dead_expired_links_and_expiry_at_delivery(self):
        page = self.put()
        link = '[Evidence](' + page['url'] + ')'
        self.assertIn('<a href=', render(self.db, link, NOW)[0])
        notify(self.db, 'Finds', link, now=NOW)
        sent = []
        flush(self.db, sender=lambda *a, **k: sent.append(a) or 1, now=NOW+timedelta(days=29))
        self.assertEqual(sent, [('Finds', 'Evidence (page expired)')])
        self.assertEqual(self.db.execute('SELECT state FROM outbox').fetchone()[0], 'sent')
        with self.assertRaisesRegex(StateError, 'expired'):
            render(self.db, link, NOW+timedelta(days=29))
        with self.assertRaisesRegex(StateError, 'not live'):
            render(self.db, '[Unknown](' + pages.pages_host() + '/unknown)', NOW)
        pages.remove(self.db, page['slug'], now=NOW, transport=self.transport)
        with self.assertRaisesRegex(StateError, 'not live'):
            render(self.db, link, NOW)

    def test_removed_page_label_is_plain_escaped_text_at_send(self):
        page = self.put()
        result = notify(self.db, 'Finds', 'Read [A < B & notes](' + page['url'] + ') or [source](https://example.org).', now=NOW)
        pages.remove(self.db, page['slug'], now=NOW, transport=self.transport)
        sent = []
        flush(self.db, sender=lambda topic, text, **kw: sent.append(text) or 1, now=NOW+timedelta(days=1))
        self.assertEqual(sent, ['Read A &lt; B &amp; notes (page expired) or <a href="https://example.org">source</a>.'])
        row = self.db.execute('SELECT * FROM outbox WHERE id=?', (result['outbox_id'],)).fetchone()
        self.assertEqual(row['state'], 'sent')
        self.assertIn(page['url'], row['text'])  # Preserve the original intent.

    def test_unrenderable_row_fails_visibly_without_blocking_other_digest_rows(self):
        bad = notify(self.db, 'Finds', 'Valid at enqueue', now=NOW)
        good = notify(self.db, 'Finds', 'Useful finding', now=NOW)
        with self.db:
            self.db.execute('UPDATE outbox SET text=? WHERE id=?', ('Legacy bare https://example.org/private', bad['outbox_id']))
        sent = []
        later = NOW+timedelta(days=1)
        flush(self.db, sender=lambda topic, text, **kw: sent.append(text) or 1, now=later)
        self.assertEqual(sent, ['Useful finding'])
        states = {r['id']:r['state'] for r in self.db.execute('SELECT id,state FROM outbox')}
        self.assertEqual(states, {bad['outbox_id']:'failed', good['outbox_id']:'sent'})
        failure = jobs.snapshot(self.db, now=later)['notifications']['failed']
        self.assertEqual(failure, [dict(outbox_id=bad['outbox_id'], topic='Finds', code='render_invalid', at=later.isoformat())])
        self.assertNotIn('private', json.dumps(failure))
        flush(self.db, sender=lambda *a, **k: self.fail('terminal rows retried'), now=later)

    def test_expiry_suffixes_split_overflowing_digest(self):
        page = self.put()
        for _ in range(2):
            notify(self.db, 'Finds', 'x'*1989 + ' [notes](' + page['url'] + ')', now=NOW)
        sent = []
        flush(self.db, sender=lambda topic, text, **kw: sent.append(text) or 1, now=NOW+timedelta(days=29))
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(text.endswith('notes (page expired)') for text in sent))
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox WHERE state='sent'").fetchone()[0], 2)

    def test_unrenderable_length_fails_without_sending(self):
        page = self.put()
        result = notify(self.db, 'Finds', 'x'*3994 + ' [note](' + page['url'] + ')', now=NOW)
        later = NOW+timedelta(days=29)
        flush(self.db, sender=lambda *a, **k: self.fail('oversized message sent'), now=later)
        failure = jobs.snapshot(self.db, now=later)['notifications']['failed']
        self.assertEqual(failure[0]['outbox_id'], result['outbox_id'])
        self.assertEqual(failure[0]['code'], 'message_too_long')


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from fixture_mind import install
        install(self, Path(self.tmp.name))
        self.root = Path(self.tmp.name) / 'state'
        with writer(self.root, initialize=True) as st:
            self.page = pages.put(st.db, 'Evidence notes', b'<p>Notes</p>', 'x.html', now=NOW)

    def test_cli_visibility_is_persisted(self):
        from steward.cli import main
        for public in (False, True):
            stdin = io.TextIOWrapper(io.BytesIO(b'notes'))
            with patch('sys.stdin', stdin), patch('sys.stdout', new_callable=io.StringIO) as output:
                argv = ['page', 'put', '--state', str(self.root), '--title', 'CLI visibility']
                self.assertEqual(main(argv + (['--public'] if public else []) + ['-']), 0)
                report = json.loads(output.getvalue())
            self.assertEqual(report['public'], public)
            self.assertEqual(report['state'], 'live')
            with writer(self.root) as st:
                row = next(r for r in jobs.snapshot(st.db)['pages']['items'] if r['slug'] == report['slug'])
                self.assertEqual(row['public'], public)

    def test_tick_polls_receipts_once_even_during_skip(self):
        from steward.tick_skips import skip
        with writer(self.root) as st:
            skip(st.db, NOW.isoformat(), (NOW+timedelta(hours=1)).isoformat(), 'quiet', now=NOW)
        key = '_opens/' + self.page['slug'] + '/2026-10-18T20:00:01Z'
        with patch('steward.pages.request', return_value=(200, json.dumps(dict(keys=[key], next=None)).encode())) as transport:
            result = runtime.tick(self.root, clock=lambda: NOW+timedelta(minutes=1), settings={},
                sender=lambda *a, **k: 1, update_fetcher=lambda offset: [],
                telegram=dict(chat_id=1, owner_user_id=2, topics={'Talk':6}), wake_starter=lambda: None)
        self.assertTrue(result['pages_poll_ok'])
        self.assertIn('skipping', result)
        self.assertEqual(sum(call.args[1].startswith('/_api/opens?') for call in transport.call_args_list), 1)
        self.assertIn('your_list', result)
        with writer(self.root) as st:
            self.assertEqual(st.db.execute('SELECT opens FROM pages WHERE slug=?', (self.page['slug'],)).fetchone()[0], 1)

    def test_wake_briefing_includes_live_pages_and_receipts(self):
        (Path(self.tmp.name)/'data/wakes/1/sessions').mkdir(parents=True)
        runtime.wake(self.root, clock=lambda: NOW, settings={'wake_model':False}, prompt='fixture', sender=lambda *a, **k: 1)
        briefing = (Path(self.tmp.name)/'data/wakes/1/briefing.md').read_text()
        self.assertFalse((self.root/'wakes').exists())
        self.assertIn('live pages:', briefing)
        self.assertIn(self.page['url'], briefing)
        self.assertIn('opened at: unknown', briefing)
        self.assertIn('opens: 0', briefing)

    def test_cli_put_stdin_and_remove(self):
        from steward.cli import main
        stdin = io.TextIOWrapper(io.BytesIO(b'<p>Notes</p>'))
        with patch('sys.stdin', stdin), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['page','put','--state',str(self.root),'--title','CLI notes','--html','-']), 0)
            page = json.loads(output.getvalue())
        self.assertEqual(page['state'], 'live')
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['page','rm','--state',str(self.root),page['slug']]), 0)
            self.assertEqual(json.loads(output.getvalue())['state'], 'removed')


class MarkdownTests(unittest.TestCase):
    setUp = use_settings

    def render(self, source):
        data, typ = pages.prepare('Day close', source.encode(), 'day-close.md')
        self.assertEqual(typ, 'text/html; charset=utf-8')
        return data.decode()

    def test_day_close_shape(self):
        result = self.render('# Day close\n\n## Done today\n- First task\n- Second task\n\n'
                             '## Next days\n| Day | Plan |\n|---|---|\n| Wed | Team meeting |\n\n'
                             '[Details](https://example.org/?a=1&b=2)')
        for part in ('<h1>Day close</h1>', '<h2>Done today</h2>', '<ul>', '<li>First task</li>',
                     '<table>', '<th>Day</th>', '<td>Team meeting</td>',
                     '<a href="https://example.org/?a=1&amp;b=2">Details</a>', 'width=device-width'):
            self.assertIn(part, result)
        self.assertNotIn('##', result)
        self.assertNotIn('|---|', result)

    def test_markup_is_text_and_only_http_links_are_active(self):
        result = self.render('<script>alert(1)</script>\n\n[bad](javascript:alert(1)) '
                             '[relative](/local) [ok](http://example.org) ![image](https://example.org/x)')
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', result)
        self.assertNotIn('<script>', result)
        self.assertEqual(result.count('<a href='), 1)
        self.assertIn('[bad](javascript:alert(1))', result)
        self.assertIn('![image](https://example.org/x)', result)

    def test_inline_numbered_lists_and_fenced_code(self):
        result = self.render('A **bold** and *italic* with `a < b` and _emphasis_.\n'
                             'Second paragraph line.\n\n1. One\n2. Two\n\n'
                             '```python\n<script> **literal**\n```')
        for part in ('<strong>bold</strong>', '<em>italic</em>', '<em>emphasis</em>',
                     '<code>a &lt; b</code>', '<ol>', '<li>Two</li>',
                     '<pre><code>&lt;script&gt; **literal**</code></pre>'):
            self.assertIn(part, result)

    def test_underscores_inside_names_stay_text(self):
        result = self.render('gsd ready_by and told_key_x but _this_ is emphasis')
        self.assertIn('ready_by and told_key_x', result)
        self.assertIn('<em>this</em>', result)

    def test_credentials_are_redacted_before_markdown_creates_tags(self):
        result = self.render('token=**private-credential**')
        self.assertIn('token=&quot;[REDACTED]&quot;', result)
        self.assertNotIn('private-credential', result)
        self.assertNotIn('<strong>', result)

    def test_unclosed_fence_and_unsupported_blocks_are_safe(self):
        result = self.render('> quote\n\n```\n<img src=x onerror=alert(1)>')
        self.assertIn('&gt; quote', result)
        self.assertIn('<pre><code>&lt;img src=x onerror=alert(1)&gt;</code></pre>', result)
        self.assertNotIn('<img', result)


class MigrationTests(unittest.TestCase):
    setUp = use_settings

    def test_v12_existing_pages_become_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'state'
            with writer(root, initialize=True) as st:
                page = pages.put(st.db, 'Old page', b'notes', 'x.txt', now=NOW,
                                 transport=lambda *a, **k: (200, b'notes'))
                st.db.execute('DROP TABLE outbox_keys')
                st.db.execute('ALTER TABLE pages DROP COLUMN public')
                st.db.execute('DROP TABLE moments'); st.db.execute("DELETE FROM changes WHERE actor=\'migration\'")
                st.db.execute('ALTER TABLE runs DROP COLUMN retry_of')
                st.db.execute("ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'result'")
                st.db.execute('PRAGMA user_version=12')
                st.db.commit()
            with writer(root) as st:
                rows = jobs.snapshot(st.db, now=NOW)['pages']['items']
                self.assertEqual(rows[0]['slug'], page['slug'])
                self.assertIs(rows[0]['public'], False)

    def test_pages_migration_preserves_metadata_and_backup_restores_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'state'
            with writer(root, initialize=True) as st:
                st.db.execute('INSERT INTO message_meta VALUES (?,?)', ('keep', 'value'))
                st.db.execute('DROP TABLE pages')
                st.db.execute('DROP TABLE drafts')
                st.db.execute('DROP TABLE outbox_keys')
                st.db.execute('DROP TABLE moments'); st.db.execute("DELETE FROM changes WHERE actor=\'migration\'")
                st.db.execute('ALTER TABLE runs DROP COLUMN retry_of')
                st.db.execute("ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'result'")
                st.db.execute('PRAGMA user_version=10')
                st.db.commit()
            with writer(root) as st:
                self.assertEqual(st.db.execute('PRAGMA user_version').fetchone()[0], 18)
                self.assertEqual(st.db.execute('SELECT value FROM message_meta WHERE key="keep"').fetchone()[0], 'value')
                self.assertEqual(st.db.execute('SELECT count(*) FROM pages').fetchone()[0], 0)
                pages.put(st.db, 'Notes', b'text', 'x.txt', now=NOW, transport=lambda *a, **k: (200, b'text'))
                st.backup(Path(tmp) / 'backup')
            self.assertEqual(json.loads((Path(tmp)/'backup/manifest.json').read_text())['schema'], 18)
            restore(Path(tmp)/'backup', Path(tmp)/'restored')
            with writer(Path(tmp)/'restored') as st:
                self.assertEqual(st.db.execute('SELECT state FROM pages').fetchone()[0], 'live')
