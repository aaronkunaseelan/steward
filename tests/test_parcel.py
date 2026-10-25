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
        import fixture_mind
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fixture_mind.install(self, Path(self.tmp.name))

    def test_read_only_lookup_tells_new_scan_once_and_records(self):
        event = dict(code='scan', date='2026-10-20T12:00:00+02:00', label='In transit')
        shipment = dict(event=[event], isFinal=False, contextData={'partner': 'ELTA'})
        calls, told = [], set()
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[1] == 'told':
                output = json.dumps({argv[-1]: argv[-1] in told})
            else:
                told.add(argv[argv.index('--told-key') + 1])
                output = '{}'
            return SimpleNamespace(returncode=0, stdout=output, stderr='')
        reads = []
        def get(url, **kwargs):
            reads.append((url, kwargs))
            return SimpleNamespace(json=lambda: [dict(shipment=shipment)])
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, STEWARD_RUN_DIR=tmp, STEWARD_BIN='/release/steward'), patch.dict('sys.modules', curl_cffi=SimpleNamespace(requests=SimpleNamespace(get=get))), patch('subprocess.run', side_effect=run), patch('sys.argv', ['parcel-watch', 'TEST', '--source-timezone', 'Europe/Paris', '--source-label', 'French time', '--route', 'Synthetic route', '--partner-label', 'Synthetic carrier', '--partner-url', 'https://carrier.example.test/track']), redirect_stdout(io.StringIO()):
            for _ in range(2):
                runpy.run_path(str(Path(__file__).resolve().parents[1] / 'jobs/parcel-watch'), run_name='__main__')
            records = [json.loads(line) for line in (Path(tmp) / 'records.jsonl').read_text().splitlines()]
        record, repeated = records
        from steward.contracts import issues, load
        for item in records:
            self.assertEqual(issues(load('steward.record.v1'), item), [])
        self.assertEqual(len(reads), 2)
        self.assertTrue(all(kwargs['impersonate'] == 'chrome' for _, kwargs in reads))
        self.assertEqual([c[1] for c in calls], ['told', 'notify', 'told'])
        self.assertFalse(record['told_before'])
        self.assertTrue(repeated['told_before'])
        self.assertFalse(repeated.get('notified', False))
        self.assertEqual(repeated['latest'], event)
        text = calls[1][-1]
        self.assertIn('12:00 French time (13:00 EEST Athens)', text)
        self.assertIn('Synthetic carrier: [tracking](https://carrier.example.test/track)', text)
        self.assertIn('Parcel TEST (Synthetic route):', text)
        self.assertTrue(record['notified'])
        self.assertEqual(record['latest'], event)
