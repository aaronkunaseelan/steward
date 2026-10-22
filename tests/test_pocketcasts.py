from fixture_mind import use_settings
"""Read-only helper tested against a local HTTP server, never the real account."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


def helper():
    loader = importlib.machinery.SourceFileLoader('pocketcasts', str(Path(__file__).resolve().parents[1] / 'jobs/pocketcasts'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class PocketCastsTests(unittest.TestCase):
    def setUp(self):
        use_settings(self)
        self.client = helper()
        self.requests = []
        self.login_status = 200
        self.history_status = 200
        self.login = {'token': 'private-test-token'}
        self.history = {'total': 1, 'episodes': [{'uuid': 'episode-1', 'podcastTitle': 'Show', 'title': 'Episode', 'published': '2026-10-01', 'playedAt': '2026-10-20'}]}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append((self.path, body, self.headers.get('Authorization')))
                login = self.path == '/user/login'
                self.send_response(owner.login_status if login else owner.history_status)
                self.end_headers()
                self.wfile.write(json.dumps(owner.login if login else owner.history).encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop)
        self.client.BASE_URL = f'http://127.0.0.1:{self.server.server_port}'
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.credentials = Path(self.temp.name) / 'credentials.env'
        self.credentials.write_text('POCKETCASTS_EMAIL="test@example.invalid"\nPOCKETCASTS_PASSWORD=\'private-password\'\n')
        self.credentials.chmod(0o600)

    def stop(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def run_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(self.client, 'CREDENTIALS', self.credentials), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = self.client.main(['history', '--json'])
        return status, out.getvalue(), err.getvalue()

    def test_login_each_call_and_read_only_history(self):
        for _ in range(2):
            status, out, err = self.run_cli()
            self.assertEqual((status, err), (0, ''))
            data = json.loads(out)
            self.assertEqual(data['episodes'][0], dict(uuid='episode-1', podcast_title='Show', episode_title='Episode', published_at='2026-10-01', played_at='2026-10-20'))
            self.assertNotIn('private', out)
        self.assertEqual([r[0] for r in self.requests], ['/user/login', '/user/history'] * 2)
        self.assertEqual(self.requests[0][1], dict(email='test@example.invalid', password='private-password', scope='webplayer'))
        self.assertEqual(self.requests[1][1:], ({}, 'Bearer private-test-token'))

    def test_http_failures_are_not_empty_history_or_secret_leaks(self):
        for login_status, history_status, endpoint in [(401, 200, '/user/login'), (200, 503, '/user/history')]:
            self.login_status, self.history_status = login_status, history_status
            status, out, err = self.run_cli()
            self.assertNotEqual(status, 0)
            self.assertEqual(out, '')
            self.assertIn(f'{endpoint}: HTTP {max(login_status, history_status)}', err)
            self.assertNotIn('private', err)

    def test_malformed_success_is_unknown(self):
        for payload in ({}, {'episodes': 'not-a-list'}, {'episodes': [{}]}):
            self.history = payload
            status, out, err = self.run_cli()
            self.assertNotEqual(status, 0)
            self.assertEqual(out, '')
            self.assertIn('invalid', err)

    def test_empty_success_and_unknown_play_time(self):
        self.history['episodes'][0].pop('playedAt')
        self.history['episodes'][0]['podcastTitle'] = ''
        episode = json.loads(self.run_cli()[1])['episodes'][0]
        self.assertIsNone(episode['played_at'])
        self.assertIsNone(episode['podcast_title'])
        self.assertEqual(episode['uuid'], 'episode-1')
        self.history = {'total': 0, 'episodes': []}
        self.assertEqual(json.loads(self.run_cli()[1]), {'total': 0, 'episodes': []})

    def test_missing_token_and_unsafe_credentials(self):
        self.login = {}
        self.assertNotEqual(self.run_cli()[0], 0)
        self.assertEqual(len(self.requests), 1)
        self.credentials.chmod(0o644)
        status, out, err = self.run_cli()
        self.assertNotEqual(status, 0)
        self.assertEqual(out, '')
        self.assertIn('0600', err)
        self.assertEqual(len(self.requests), 1)
