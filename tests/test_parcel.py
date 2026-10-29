from contextlib import redirect_stderr, redirect_stdout
import http.cookiejar
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler


CODE = 'CW553616288FR'
BASE = 'https://itemsearch.elta.gr/en-GB'
ROOT = Path(__file__).resolve().parents[1]


def history(status='Shipment in transit', date='28/10/2026 12:00', code=CODE):
    return (f'<div>Tracking Number : {code}</div><table><tr><th>Date</th>'
            f'<th>Status</th><th>Location</th></tr><tr><td>{date}</td>'
            f'<td>{status}</td><td>PATRA K. DIALOGIS</td></tr></table>')


class ParcelTests(unittest.TestCase):
    def setUp(self):
        import fixture_mind
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fixture_mind.install(self, Path(self.tmp.name))
        self.enterContext(patch.dict(os.environ, STEWARD_RUN_DIR=self.tmp.name,
                                     STEWARD_BIN='/release/steward'))
        self.argv = ['parcel-watch', CODE, '--source-timezone', 'Europe/Athens',
                     '--source-label', 'Greek time', '--route', 'Synthetic route',
                     '--partner-label', 'Synthetic carrier {code}',
                     '--partner-url', 'https://carrier.example.test/track']
        self.calls, self.reads, self.told, self.responses = [], [], set(), []
        self.enterContext(patch('subprocess.run', side_effect=self.run_cli))
        self.builder = self.enterContext(patch('urllib.request.build_opener', side_effect=self.build_opener))

    def run_cli(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1] == 'told':
            output = json.dumps({argv[-1]: argv[-1] in self.told})
        else:
            self.told.add(argv[argv.index('--told-key') + 1])
            output = '{}'
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    def build_opener(self, *handlers):
        cookies, proxy = handlers
        self.assertIsInstance(cookies, HTTPCookieProcessor)
        self.assertIsInstance(cookies.cookiejar, http.cookiejar.CookieJar)
        self.assertIsInstance(proxy, ProxyHandler)
        self.assertEqual(proxy.proxies, {})
        return SimpleNamespace(open=self.open_http)

    def open_http(self, request, **kwargs):
        self.reads.append(request)
        self.assertEqual(kwargs['timeout'], 12)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        body = response if isinstance(response, bytes) else json.dumps(response).encode()
        stream = io.BytesIO(body)
        stream.status = 200
        return stream

    def queue_history(self, view=None):
        self.responses += [b'<html>ELTA</html>',
                           {'Success': True, 'Tokens': {'X-Test-Token': 'ephemeral-test-token'}},
                           {'Success': True, 'View': history() if view is None else view}]

    def invoke(self, job='parcel-watch', exit_code=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = self.argv if job == 'parcel-watch' else [job, CODE]
        with patch('sys.argv', argv), redirect_stdout(stdout), redirect_stderr(stderr):
            if exit_code is None:
                runpy.run_path(str(ROOT / 'jobs' / job), run_name='__main__')
            else:
                with self.assertRaises(SystemExit) as error:
                    runpy.run_path(str(ROOT / 'jobs' / job), run_name='__main__')
                self.assertEqual(error.exception.code, exit_code)
        self.assertNotIn('ephemeral-test-token', stdout.getvalue() + stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def records(self):
        return [json.loads(line) for line in (Path(self.tmp.name) / 'records.jsonl').read_text().splitlines()]

    def assert_failure(self, reason, job='parcel-watch'):
        stdout, stderr = self.invoke(job=job, exit_code=1)
        self.assertIn(reason, stdout + stderr)
        if job == 'elta-track':
            self.assertFalse(json.loads(stdout)['lookup_ok'])
        self.assertEqual(self.calls, [])
        self.assertFalse((Path(self.tmp.name) / 'records.jsonl').exists())

    def test_lookup_tells_new_scan_once_and_records(self):
        for _ in range(2):
            self.queue_history()
            self.invoke()
        record, repeated = self.records()
        from steward.contracts import issues, load
        for item in (record, repeated):
            self.assertEqual(issues(load('steward.record.v1'), item), [])
        self.assertEqual(self.builder.call_count, 2)
        self.assertIsNot(self.builder.call_args_list[0].args[0].cookiejar,
                         self.builder.call_args_list[1].args[0].cookiejar)
        for start in (0, 3):
            direct, token, tracking = self.reads[start:start + 3]
            self.assertEqual([r.get_method() for r in (direct, token, tracking)], ['GET', 'POST', 'POST'])
            self.assertEqual([r.full_url for r in (direct, token, tracking)],
                             [BASE + '/Query/Direct/' + CODE, BASE + '/Security/GetToken', BASE + '/query/DirectList'])
            self.assertIsNone(direct.data)
            self.assertEqual(token.data, b'')
            self.assertEqual(tracking.data, b'codes%5B%5D=' + CODE.encode())
            self.assertEqual(tracking.get_header('X-test-token'), 'ephemeral-test-token')
            self.assertEqual(tracking.get_header('Content-type'), 'application/x-www-form-urlencoded')
        self.assertEqual([c[1] for c in self.calls], ['told', 'notify', 'told'])
        self.assertFalse(record['told_before'])
        self.assertTrue(repeated['told_before'])
        self.assertTrue(record['notified'])
        self.assertFalse(repeated.get('notified', False))
        self.assertEqual(record['key'], f'parcel:{CODE}:Shipment in transit:2026-10-28T12:00:00+02:00')
        self.assertEqual(record['latest'], dict(code='Shipment in transit',
                         date='2026-10-28T12:00:00+02:00', label='Shipment in transit'))
        self.assertEqual(repeated['latest'], record['latest'])
        text = self.calls[1][-1]
        self.assertIn('12:00 Greek time (12:00 EET Athens)', text)
        self.assertIn(f'Synthetic carrier {CODE}: [tracking](https://carrier.example.test/track)', text)
        self.assertIn(f'Parcel {CODE} (Synthetic route):', text)

    def test_delivered_scan_says_stop_and_records_final(self):
        self.queue_history(history(status='Delivered'))
        self.invoke()
        self.assertTrue(self.records()[0]['final'])
        self.assertIn("Tracking shows it as final; I'll stop watching.", self.calls[1][-1])
        self.assertNotIn('https://carrier.example.test', self.calls[1][-1])

    def test_received_by_customer_abroad_is_not_delivered(self):
        self.queue_history(history(status='Received by customer abroad'))
        self.invoke()
        self.assertFalse(self.records()[0]['final'])
        self.assertNotIn("I'll stop watching", self.calls[1][-1])

    def test_http_503_names_status_and_exits_one(self):
        for job in ('parcel-watch', 'elta-track'):
            with self.subTest(job=job):
                self.responses = [HTTPError(BASE, 503, 'unavailable', {}, io.BytesIO(b'<html>secret</html>'))]
                self.assert_failure('HTTP 503', job)

    def test_success_false_names_stage_and_exits_one(self):
        for job in ('parcel-watch', 'elta-track'):
            for stage in ('GetToken', 'DirectList'):
                with self.subTest(job=job, stage=stage):
                    self.responses = [b'page']
                    if stage == 'DirectList':
                        self.responses += [{'Success': True, 'Tokens': {'X-Test-Token': 'ephemeral-test-token'}}]
                    self.responses += [{'Success': False, 'Message': 'ephemeral-test-token'}]
                    self.assert_failure(stage + ': Success false', job)

    def test_identity_mismatch_names_failure_and_exits_one(self):
        for job in ('parcel-watch', 'elta-track'):
            with self.subTest(job=job):
                self.queue_history(history(code='AA123456789GR'))
                self.assert_failure('identity mismatch', job)

    def test_changed_markup_names_failure_and_exits_one(self):
        for view in (f'Tracking Number : {CODE}', history().replace('<td>28/10/2026 12:00</td>', ''),
                     history(date='not-a-date'), history(status='')):
            with self.subTest(view=view):
                self.queue_history(view)
                self.assert_failure('markup')

    def test_invalid_json_and_missing_tokens_are_named(self):
        for response, reason in [(b'<html>ephemeral-test-token</html>', 'invalid JSON'),
                                 ({'Success': True, 'Tokens': {}}, 'missing token mapping')]:
            with self.subTest(reason=reason):
                self.responses = [b'page', response]
                self.assert_failure(reason)

    def test_timeout_and_network_failure_are_named(self):
        for error, reason in [(TimeoutError('ephemeral-test-token'), 'request timed out'),
                              (URLError('ephemeral-test-token'), 'network request failed')]:
            with self.subTest(reason=reason):
                self.responses = [error]
                self.assert_failure(reason)

    def test_no_scan_records_none_without_telling(self):
        self.queue_history(f'Tracking Number : {CODE}<table><tr><th>Date</th><th>Status</th><th>Location</th></tr></table>')
        self.invoke()
        record = self.records()[0]
        self.assertIsNone(record['latest'])
        self.assertFalse(record['final'])
        self.assertEqual(record['key'], f'parcel:{CODE}:none')
        self.assertEqual([c[1] for c in self.calls], ['told'])

    def test_seen_through_compares_instants_across_offsets(self):
        self.argv.insert(2, '2026-10-28T13:00:00+03:00')
        self.queue_history()
        self.invoke()
        self.assertTrue(self.records()[0]['told_before'])
        self.assertEqual([c[1] for c in self.calls], ['told'])

    def test_offsetless_seen_through_uses_source_timezone(self):
        self.argv.insert(2, '2026-10-28T12:00:00')
        self.queue_history()
        self.invoke()
        self.assertTrue(self.records()[0]['told_before'])
        self.assertEqual([c[1] for c in self.calls], ['told'])

    def test_athens_summer_offset_and_date_sorting(self):
        self.queue_history(history(date='24/10/2026 12:00').replace('</table>',
                           '<tr><td>23/10/2026 12:00</td><td>Older scan</td><td>ATHENS</td></tr></table>'))
        self.invoke()
        self.assertEqual(self.records()[0]['latest']['date'], '2026-10-24T12:00:00+03:00')
        self.assertIn('12:00 Greek time (12:00 EEST Athens)', self.calls[1][-1])

    def test_reader_outputs_token_free_json_without_steward_calls(self):
        self.queue_history()
        stdout, stderr = self.invoke(job='elta-track', exit_code=0)
        result = json.loads(stdout)
        self.assertTrue(result['lookup_ok'])
        self.assertTrue(result['history_present'])
        self.assertEqual(result['tracking_number'], CODE)
        self.assertEqual(result['latest_event']['timezone'], 'Europe/Athens')
        self.assertEqual(result['latest_event']['time'], '2026-10-28T12:00+02:00')
        self.assertEqual(result['latest_event']['location'], 'PATRA K. DIALOGIS')
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(stderr, '')
