import io
import json
import os
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout


class ParcelTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import fixture_mind
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fixture_mind.install(self, Path(self.tmp.name))

    def test_read_only_lookup_tells_new_scan_once_and_records(self):
        event = dict(code='scan', date='2026-10-20T12:00:00+02:00', label='In transit')
        shipment = dict(event=[event], isFinal=False, contextData={'partner': 'ELTA'})
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps({'parcel:TEST:scan:' + event['date']: False}) if argv[1] == 'told' else '{}', stderr='')
        reads = []
        def get(url, **kwargs):
            reads.append((url, kwargs))
            return SimpleNamespace(json=lambda: [dict(shipment=shipment)])
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, STEWARD_RUN_DIR=tmp, STEWARD_BIN='/release/steward'), patch.dict('sys.modules', curl_cffi=SimpleNamespace(requests=SimpleNamespace(get=get))), patch('subprocess.run', side_effect=run), patch('sys.argv', ['parcel-watch', 'TEST', '--source-timezone', 'Europe/Paris', '--source-label', 'French time', '--route', 'Synthetic route', '--partner-label', 'Synthetic carrier', '--partner-url', 'https://carrier.example.test/track']), redirect_stdout(io.StringIO()):
            runpy.run_path(str(Path(__file__).resolve().parents[1] / 'jobs/parcel-watch'), run_name='__main__')
            record = json.loads((Path(tmp) / 'records.jsonl').read_text())
        from steward.contracts import issues, load
        self.assertEqual(issues(load('steward.record.v1'), record), [])
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0][1]['impersonate'], 'chrome')
        self.assertEqual([c[1] for c in calls], ['told', 'notify'])
        text = calls[1][-1]
        self.assertIn('12:00 French time (13:00 EEST Athens)', text)
        self.assertIn('Synthetic carrier: [tracking](https://carrier.example.test/track)', text)
        self.assertIn('Parcel TEST (Synthetic route):', text)
        self.assertTrue(record['notified'])
        self.assertEqual(record['latest'], event)
