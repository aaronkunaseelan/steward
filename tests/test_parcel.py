from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


class ParcelTests(unittest.TestCase):
    def setUp(self):
        import fixture_mind
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fixture_mind.install(self, Path(self.tmp.name))
        self.enterContext(patch.dict(os.environ, STEWARD_RUN_DIR=self.tmp.name,
                                     STEWARD_BIN='/release/steward', TRACK17_TOKEN='test-only-token'))
        self.argv = ['parcel-watch', 'TEST', '--source-timezone', 'Europe/Paris',
                     '--source-label', 'French time', '--route', 'Synthetic route',
                     '--partner-label', 'Synthetic carrier',
                     '--partner-url', 'https://carrier.example.test/track']
        self.calls, self.reads, self.told, self.responses = [], [], set(), []
        self.enterContext(patch('subprocess.run', side_effect=self.run_cli))
        self.enterContext(patch('urllib.request.urlopen', side_effect=self.open_api))

    def run_cli(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1] == 'told':
            output = json.dumps({argv[-1]: argv[-1] in self.told})
        else:
            self.told.add(argv[argv.index('--told-key') + 1])
            output = '{}'
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    def open_api(self, request, **kwargs):
        self.reads.append((request, kwargs))
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('17token'), 'test-only-token')
        self.assertEqual(kwargs['timeout'], 60)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        body = response if isinstance(response, bytes) else json.dumps(response).encode()
        stream = io.BytesIO(body)
        stream.status = 200
        return stream

    def accepted(self, track_info=None):
        item = {'number': 'TEST', 'carrier': 7071}
        if track_info is not None:
            item['track_info'] = track_info
        return {'code': 0, 'data': {'accepted': [item], 'rejected': []}}

    def rejected(self, code, message):
        return {'code': 0, 'data': {'accepted': [], 'rejected': [
            {'number': 'TEST', 'error': {'code': code, 'message': message}}]}}

    def tracking(self, status='InTransit', date='2026-10-20T12:00:00+02:00'):
        return {'latest_status': {'status': status, 'sub_status': status + '_Other'},
                'latest_event': {'time_iso': date, 'description': 'In transit',
                                 'stage': status, 'sub_status': status + '_Other'}}

    def invoke(self, exit_code=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('sys.argv', self.argv), redirect_stdout(stdout), redirect_stderr(stderr):
            if exit_code is None:
                runpy.run_path(str(Path(__file__).resolve().parents[1] / 'jobs/parcel-watch'), run_name='__main__')
            else:
                with self.assertRaises(SystemExit) as error:
                    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'jobs/parcel-watch'), run_name='__main__')
                self.assertEqual(error.exception.code, exit_code)
        self.assertNotIn('test-only-token', stdout.getvalue() + stderr.getvalue())
        return stderr.getvalue()

    def records(self):
        return [json.loads(line) for line in (Path(self.tmp.name) / 'records.jsonl').read_text().splitlines()]

    def actions(self):
        return [request.full_url.rsplit('/', 1)[-1] for request, _ in self.reads]

    def test_lookup_tells_new_scan_once_and_records(self):
        self.responses = [self.accepted(self.tracking())] * 2
        for _ in range(2):
            self.invoke()
        record, repeated = self.records()
        from steward.contracts import issues, load
        for item in (record, repeated):
            self.assertEqual(issues(load('steward.record.v1'), item), [])
        self.assertEqual(self.actions(), ['gettrackinfo', 'gettrackinfo'])
        self.assertTrue(all(request.full_url.startswith('https://api.17track.net/track/v2.4/')
                            for request, _ in self.reads))
        self.assertTrue(all(json.loads(request.data) == [{'number': 'TEST'}] for request, _ in self.reads))
        self.assertEqual([c[1] for c in self.calls], ['told', 'notify', 'told'])
        self.assertFalse(record['told_before'])
        self.assertTrue(repeated['told_before'])
        self.assertFalse(repeated.get('notified', False))
        self.assertTrue(record['notified'])
        self.assertEqual(record['key'], 'parcel:TEST:InTransit_Other:2026-10-20T12:00:00+02:00')
        self.assertEqual(record['latest'], dict(code='InTransit_Other',
                         date='2026-10-20T12:00:00+02:00', label='In transit'))
        self.assertEqual(repeated['latest'], record['latest'])
        text = self.calls[1][-1]
        self.assertIn('12:00 French time (13:00 EEST Athens)', text)
        self.assertIn('Synthetic carrier: [tracking](https://carrier.example.test/track)', text)
        self.assertIn('Parcel TEST (Synthetic route):', text)

    def test_registers_only_when_not_registered_then_reads_tracking(self):
        self.responses = [self.rejected(-18019902, 'Not registered. Please register first.'),
                          self.accepted(), self.accepted(self.tracking()), self.accepted(self.tracking())]
        self.invoke()
        self.invoke()
        self.assertEqual(self.actions(), ['gettrackinfo', 'register', 'gettrackinfo', 'gettrackinfo'])
        self.assertEqual([c[1] for c in self.calls], ['told', 'notify', 'told'])

    def test_already_registered_is_success_after_registration_race(self):
        self.responses = [self.rejected(-18019902, 'Not registered.'),
                          self.rejected(-18019901, 'Already registered.'), self.accepted(self.tracking())]
        self.invoke()
        self.assertTrue(self.records()[0]['notified'])
        self.assertEqual(self.actions(), ['gettrackinfo', 'register', 'gettrackinfo'])

    def test_explicit_carrier_sent_on_lookup_and_registration(self):
        self.argv += ['--carrier', '7071']
        self.responses = [self.rejected(-18019902, 'Not registered.'), self.accepted(), self.accepted(self.tracking())]
        self.invoke()
        self.assertTrue(all(json.loads(request.data) == [{'number': 'TEST', 'carrier': 7071}]
                            for request, _ in self.reads))

    def test_delivered_scan_says_stop_and_records_final(self):
        self.responses = [self.accepted(self.tracking(status='Delivered'))]
        self.invoke()
        self.assertTrue(self.records()[0]['final'])
        self.assertIn("Tracking shows it as final; I'll stop watching.", self.calls[1][-1])
        self.assertNotIn('https://carrier.example.test', self.calls[1][-1])

    def test_expired_is_not_delivered(self):
        self.responses = [self.accepted(self.tracking(status='Expired'))]
        self.invoke()
        self.assertFalse(self.records()[0]['final'])
        self.assertNotIn("I'll stop watching", self.calls[1][-1])

    def test_http_503_html_names_status_and_exits_one(self):
        self.responses = [HTTPError('https://api.17track.net/track/v2.4/gettrackinfo',
                                    503, 'Service Unavailable', {}, io.BytesIO(b'<html>Unavailable</html>'))]
        self.assertIn('HTTP 503', self.invoke(exit_code=1))
        self.assertEqual(self.calls, [])
        self.assertFalse((Path(self.tmp.name) / 'records.jsonl').exists())

    def test_missing_token_is_named_before_any_calls(self):
        os.environ.pop('TRACK17_TOKEN')
        self.assertIn('TRACK17_TOKEN', self.invoke(exit_code=1))
        self.assertEqual(self.reads, [])
        self.assertEqual(self.calls, [])

    def test_api_failures_name_code_and_message_without_registration(self):
        for response, code, message in [
            (self.rejected(-18019908, 'Quota exhausted.'), -18019908, 'Quota exhausted.'),
            ({'code': -18010002, 'data': {'errors': [{'code': -18010002, 'message': 'Invalid security key.'}]}},
             -18010002, 'Invalid security key.'),
        ]:
            with self.subTest(code=code):
                self.responses = [response]
                stderr = self.invoke(exit_code=1)
                self.assertIn(str(code), stderr)
                self.assertIn(message, stderr)
        self.assertEqual(self.actions(), ['gettrackinfo', 'gettrackinfo'])
        self.assertEqual(self.calls, [])

    def test_provider_error_cannot_echo_token(self):
        self.responses = [self.rejected(-18010002, 'Invalid key test-only-token.')]
        stderr = self.invoke(exit_code=1)
        self.assertIn('-18010002', stderr)
        self.assertIn('Invalid key [redacted].', stderr)

    def test_registration_failure_names_code_and_message(self):
        self.responses = [self.rejected(-18019902, 'Not registered.'),
                          self.rejected(-18019903, 'Carrier cannot be detected.')]
        stderr = self.invoke(exit_code=1)
        self.assertIn('-18019903', stderr)
        self.assertIn('Carrier cannot be detected.', stderr)
        self.assertEqual(self.actions(), ['gettrackinfo', 'register'])

    def test_invalid_json_names_the_failed_read(self):
        self.responses = [b'<html>Unavailable</html>']
        self.assertIn('invalid JSON', self.invoke(exit_code=1))
        self.assertEqual(self.calls, [])

    def test_absent_tracking_info_is_a_visible_failure(self):
        self.responses = [self.accepted()]
        self.assertIn('track_info', self.invoke(exit_code=1))
        self.assertEqual(self.calls, [])

    def test_no_scan_records_none_without_telling(self):
        self.responses = [self.accepted({'latest_event': None, 'latest_status': {'status': 'NotFound'}})]
        self.invoke()
        record = self.records()[0]
        self.assertIsNone(record['latest'])
        self.assertFalse(record['final'])
        self.assertEqual(record['key'], 'parcel:TEST:none')
        self.assertEqual([c[1] for c in self.calls], ['told'])

    def test_seen_through_compares_instants_across_offsets(self):
        self.argv.insert(2, '2026-10-20T13:00:00+03:00')
        self.responses = [self.accepted(self.tracking())]
        self.invoke()
        self.assertTrue(self.records()[0]['told_before'])
        self.assertEqual([c[1] for c in self.calls], ['told'])

    def test_offsetless_scan_uses_explicit_source_timezone(self):
        self.responses = [self.accepted(self.tracking(date='2026-10-20T12:00:00'))]
        self.invoke()
        self.assertEqual(self.records()[0]['latest']['date'], '2026-10-20T12:00:00+02:00')
        self.assertIn('12:00 French time (13:00 EEST Athens)', self.calls[1][-1])
